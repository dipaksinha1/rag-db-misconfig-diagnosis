"""Offline tests of the RAG index (T4): tokenizer, RRF, metadata, manifest, build and search.

No real model and no network: a tiny corpus is indexed with ``FakeEmbedder``
(a deterministic hashed bag of words) into a Chroma store under ``tmp_path``.
"""

from __future__ import annotations

import importlib
import json

import pytest

from pgdiag.chunks import (
    DOCS_CHUNKS_FILE,
    RELEASE_NOTES_CHUNKS_FILE,
    Chunk,
    write_jsonl,
)
from pgdiag.config import get_config, load_config
from pgdiag.index import cli, store
from pgdiag.index.build import BuildOptions, build_index, select_chunks
from pgdiag.index.embed import BGE_QUERY_INSTRUCTION, default_prefixes
from pgdiag.index.search import HybridIndex, normalize_sources, rrf_fuse
from pgdiag.index.tokenize import tokenize
from pgdiag.settings import TARGET_SETTINGS
from tests.fakes import FakeEmbedder

# ``pgdiag.index.search`` is also the name of the function the package exports.
search_module = importlib.import_module("pgdiag.index.search")

DOCS = "https://www.postgresql.org/docs/16/"


def setting_chunk(page: str, name: str, text: str) -> Chunk:
    anchor = "GUC-" + name.upper().replace("_", "-")
    return Chunk(
        chunk_id=f"docs:{page}#{anchor}",
        source="docs",
        url=f"{DOCS}{page}.html#{anchor}",
        title=f"20.4. Resource Consumption — {name}",
        text=f"{name} (integer)\n\n{text}",
        settings=[name],
        meta={
            "chunk_type": "setting",
            "setting": name,
            "page": page,
            "pg_version": "16",
            "section_path": ["20.4. Resource Consumption", name],
            "part": 1,
            "parts": 1,
        },
    )


def section_chunk(chunk_id: str, title: str, text: str, settings=()) -> Chunk:
    page, section = chunk_id.removeprefix("docs:").split("#")
    return Chunk(
        chunk_id=chunk_id,
        source="docs",
        url=f"{DOCS}{page}.html#{section}",
        title=title,
        text=text,
        settings=sorted(settings),
        meta={"chunk_type": "section", "page": page, "pg_version": "16", "section_path": [title]},
    )


def relnote_chunk(chunk_id: str, text: str, **meta) -> Chunk:
    version = chunk_id.removeprefix("relnotes:").split("#")[0]
    return Chunk(
        chunk_id=chunk_id,
        source="release_notes",
        url=f"{DOCS}release-{version.replace('.', '-')}.html#RELEASE-CHANGES",
        title=f"PostgreSQL {version} release notes — Changes",
        text=text,
        settings=[],
        meta={
            "chunk_type": "items",
            "version": version,
            "release_date": None,  # a null, as on a page without a date line
            "authors": ["Tom Lane", "Andres Freund"],
            "commits": ["https://postgr.es/c/abc123"],
            "section_path": ["Changes"],
        },
    )


DOCS_CHUNKS = [
    setting_chunk(
        "runtime-config-resource",
        "work_mem",
        "Sets the base maximum amount of memory to be used by a query operation (such as a "
        "sort or hash table) before writing to temporary disk files.",
    ),
    setting_chunk(
        "runtime-config-resource",
        "maintenance_work_mem",
        "Specifies the maximum amount of memory to be used by maintenance operations, such as "
        "VACUUM, CREATE INDEX, and ALTER TABLE ADD FOREIGN KEY.",
    ),
    setting_chunk(
        "runtime-config-resource",
        "max_parallel_workers_per_gather",
        "Sets the maximum number of workers that can be started by a single Gather or Gather "
        "Merge node. Parallel workers are taken from the pool of processes.",
    ),
    setting_chunk(
        "runtime-config-wal",
        "max_wal_size",
        "Maximum size to let the WAL grow during automatic checkpoints. Checkpoints that happen "
        "too often are logged with a checkpoint_warning.",
    ),
    *[
        section_chunk(
            f"docs:sorting#SORT-{n}",
            f"14.{n}. Sorting",
            f"Sorts that do not fit in memory spill to temporary files on disk ({n}). "
            "An external merge sort writes temporary files on disk.",
            settings=["work_mem"] if n == 1 else [],
        )
        for n in range(1, 6)
    ],
    section_chunk("docs:sql-explain#see-also", "EXPLAIN — See Also", "ANALYZE"),  # left out
]
RELNOTE_CHUNKS = [
    relnote_chunk(
        "relnotes:16.3#changes:i12",
        "Fix leak of a temporary file when a sort spills to disk during a parallel query.",
    ),
    relnote_chunk("relnotes:16.5#changes:i53", "Fix building with Strawberry Perl on Windows"),
    relnote_chunk(
        "relnotes:16.0#highlights", "Allow parallelization of FULL and internal RIGHT joins."
    ),
]
ALL_CHUNKS = DOCS_CHUNKS + RELNOTE_CHUNKS
LEFT_OUT = {"docs:sql-explain#see-also"}


def make_corpus(root):
    cfg = load_config(env={}, root=root)
    write_jsonl(cfg.chunks_dir / DOCS_CHUNKS_FILE, DOCS_CHUNKS)
    write_jsonl(cfg.chunks_dir / RELEASE_NOTES_CHUNKS_FILE, RELNOTE_CHUNKS)
    return cfg


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    cfg = make_corpus(tmp_path_factory.mktemp("index-root"))
    result = build_index(cfg, embedder=FakeEmbedder())
    return cfg, result


@pytest.fixture(scope="module")
def index(built):
    cfg, _ = built
    return HybridIndex(cfg.index_dir, expected_model="fake/bag-of-words", embedder=FakeEmbedder())


# ------------------------------------------------------------------ tokenizer


def test_tokenizer_keeps_parameter_names_whole_and_adds_their_parts():
    tokens = tokenize("Raise max_parallel_workers_per_gather.")
    assert tokens[0] == "rais"
    assert tokens[1] == "max_parallel_workers_per_gather"
    assert tokens[2:] == ["max", "parallel", "worker", "per", "gather"]
    # "parallel workers" in a query matches the parts.
    assert set(tokenize("parallel workers")) <= set(tokens)


def test_tokenizer_is_case_insensitive_and_does_not_merge_neighbouring_names():
    assert tokenize("WORK_MEM Work_Mem") == tokenize("work_mem work_mem")
    tokens = tokenize("maintenance_work_mem")
    assert "maintenance_work_mem" in tokens and "work_mem" not in tokens
    assert tokenize("autovacuum_naptime")[0] == "autovacuum_naptime"


def test_tokenizer_stems_drops_stop_words_and_keeps_numbers():
    assert tokenize("The sorts are spilling to temporary files") == [
        "sort",
        "spill",
        "temporari",
        "file",
    ]
    assert tokenize("PostgreSQL 16.3 with 4MB") == ["postgresql", "16.3", "4mb"]
    assert tokenize("pg_stat_statements.track = all")[:2] == ["pg_stat_statements", "pg"]
    assert "track" in tokenize("pg_stat_statements.track = all")
    assert tokenize("a of the _ __ x") == []


# -------------------------------------------------------------------- fusion


def test_rrf_fuse_sums_weighted_reciprocal_ranks():
    fused = rrf_fuse([(["a", "b", "c"], 1.0), (["c", "a"], 1.0)], rrf_k=60)
    scores = {cid: score for cid, score, _ in fused}
    assert scores["a"] == pytest.approx(1 / 61 + 1 / 62)
    assert scores["c"] == pytest.approx(1 / 63 + 1 / 61)
    assert scores["b"] == pytest.approx(1 / 62)
    assert [cid for cid, _, _ in fused] == ["a", "c", "b"]


def test_rrf_fuse_weights_and_tie_breaks_are_deterministic():
    fused = rrf_fuse([(["a", "b"], 1.0), (["b", "a"], 0.0)], rrf_k=10)
    assert [cid for cid, _, _ in fused] == ["a", "b"]
    # Equal scores and equal best ranks: chunk id decides.
    fused = rrf_fuse([(["y"], 1.0), (["x"], 1.0)], rrf_k=60)
    assert [(cid, best) for cid, _, best in fused] == [("x", 1), ("y", 1)]
    # Equal scores, different best ranks: the better single rank wins.
    fused = rrf_fuse([(["p", "q"], 1.0), (["q"], 0.0)], rrf_k=0)
    assert fused[0][0] == "p"
    assert rrf_fuse([([], 1.0)], rrf_k=60) == []


def test_normalize_sources():
    assert normalize_sources(None) is None
    assert normalize_sources("docs") == ("docs",)
    assert normalize_sources(["release_notes", "docs", "docs"]) == ("docs", "release_notes")
    with pytest.raises(ValueError, match="unknown source"):
        normalize_sources("wiki")
    with pytest.raises(ValueError, match="empty"):
        normalize_sources([])


# ------------------------------------------------------- metadata round trip


@pytest.mark.parametrize("chunk", ALL_CHUNKS, ids=lambda c: c.chunk_id)
def test_metadata_is_scalar_and_round_trips(chunk):
    chunk_id, document, metadata = store.chunk_to_record(chunk)
    assert chunk_id == chunk.chunk_id and document == chunk.text
    assert all(isinstance(value, str | int | float | bool) for value in metadata.values())
    assert store.record_to_chunk(chunk_id, document, metadata) == chunk


def test_flattened_metadata_fields():
    work_mem = DOCS_CHUNKS[0]
    flat = store.flatten_metadata(work_mem)
    assert flat["settings"] == "|work_mem|" and flat["n_settings"] == 1
    assert flat["chunk_type"] == "setting" and flat["setting"] == "work_mem"
    assert flat["version"] == "" and flat["source"] == "docs"
    assert flat[store.target_flag("work_mem")] is True
    assert flat[store.target_flag("maintenance_work_mem")] is False
    assert {k for k in flat if k.startswith("mentions_")} == {
        store.target_flag(n) for n in TARGET_SETTINGS
    }
    relnote = store.flatten_metadata(RELNOTE_CHUNKS[0])
    assert relnote["settings"] == "" and relnote["version"] == "16.3"
    assert json.loads(relnote["meta_json"])["release_date"] is None
    assert store.parse_settings_field("|a|b_c|") == ["a", "b_c"]
    assert store.parse_settings_field("") == []


def test_selection_leaves_out_only_short_docs_sections():
    short_setting = setting_chunk("runtime-config-resource", "work_mem", "x")
    kept, dropped = select_chunks([*ALL_CHUNKS, short_setting], 60)
    assert {c.chunk_id for c in dropped} == LEFT_OUT
    assert short_setting in kept
    assert RELNOTE_CHUNKS[1] in kept  # 45-character release-note item
    kept, dropped = select_chunks(ALL_CHUNKS, 0)
    assert dropped == []


def test_model_aware_prefixes():
    assert default_prefixes("BAAI/bge-small-en-v1.5") == (BGE_QUERY_INSTRUCTION, "")
    assert default_prefixes("BAAI/bge-base-en-v1.5")[0] == BGE_QUERY_INSTRUCTION
    assert default_prefixes("intfloat/e5-small-v2") == ("query: ", "passage: ")
    assert default_prefixes("sentence-transformers/all-MiniLM-L6-v2") == ("", "")


# ------------------------------------------------------------------ manifest


def test_missing_index_is_a_clear_error(tmp_path):
    with pytest.raises(store.IndexNotBuilt, match="make index"):
        store.read_manifest(tmp_path)
    with pytest.raises(store.IndexNotBuilt):
        HybridIndex(tmp_path)


def test_manifest_checks():
    manifest = {"format_version": store.FORMAT_VERSION, "embedding": {"model": "m1"}}
    store.check_manifest(manifest, "m1")
    store.check_manifest(manifest, None)
    with pytest.raises(store.IndexMismatch, match="EMBEDDING_MODEL"):
        store.check_manifest(manifest, "m2")
    with pytest.raises(store.IndexMismatch, match="format"):
        store.check_manifest({**manifest, "format_version": 0}, "m1")


def test_build_writes_manifest_with_counts_hashes_and_model(built):
    cfg, result = built
    manifest = result.manifest
    assert not result.skipped
    assert manifest == json.loads((cfg.index_dir / store.MANIFEST_FILE).read_text())
    assert manifest["counts"] == {"docs": 9, "release_notes": 3, "mailing_list": 0}
    assert manifest["total"] == 12
    assert manifest["selection"]["left_out"] == {"docs": 1}
    assert set(manifest["selection"]["left_out_ids"]) == LEFT_OUT
    assert set(manifest["inputs"]) == {DOCS_CHUNKS_FILE, RELEASE_NOTES_CHUNKS_FILE}
    assert manifest["inputs"][DOCS_CHUNKS_FILE]["chunks"] == len(DOCS_CHUNKS)
    assert len(manifest["inputs"][DOCS_CHUNKS_FILE]["sha256"]) == 64
    emb = manifest["embedding"]
    assert (emb["model"], emb["revision"], emb["dim"]) == ("fake/bag-of-words", "fake-revision", 64)
    assert emb["normalized"] is True
    assert manifest["chunk_types"]["docs"] == {"section": 5, "setting": 4}
    assert {"chromadb", "bm25s", "torch", "sentence-transformers"} <= set(manifest["versions"])
    assert not (cfg.index_dir / "chroma.new").exists()
    assert not (cfg.index_dir / "chroma.old").exists()


def test_build_embeds_title_and_text(tmp_path):
    cfg = make_corpus(tmp_path)
    embedder = FakeEmbedder()
    build_index(cfg, embedder=embedder)
    embedded = {text for call in embedder.calls for text in call}
    assert store.document_text(DOCS_CHUNKS[0]) in embedded
    assert store.document_text(DOCS_CHUNKS[0]).startswith(DOCS_CHUNKS[0].title + "\n")


def test_build_is_skipped_when_nothing_changed_and_redone_otherwise(tmp_path):
    cfg = make_corpus(tmp_path)
    first = build_index(cfg, embedder=FakeEmbedder())
    again = build_index(cfg, embedder=FakeEmbedder())
    assert again.skipped and again.manifest["fingerprint"] == first.manifest["fingerprint"]
    assert build_index(cfg, embedder=FakeEmbedder(), rebuild=True).skipped is False
    assert not build_index(cfg, embedder=FakeEmbedder(), options=BuildOptions(0)).skipped
    write_jsonl(cfg.chunks_dir / RELEASE_NOTES_CHUNKS_FILE, RELNOTE_CHUNKS[:2])
    changed = build_index(cfg, embedder=FakeEmbedder(), options=BuildOptions(0))
    assert not changed.skipped
    assert changed.manifest["counts"]["release_notes"] == 2
    index = HybridIndex(cfg.index_dir, embedder=FakeEmbedder())
    assert len(index) == 12 and "docs:sql-explain#see-also" in index  # min chars 0: kept


def test_build_without_chunk_files_fails_clearly(tmp_path):
    cfg = load_config(env={}, root=tmp_path)
    with pytest.raises(store.IndexStoreError, match="ingest-docs"):
        build_index(cfg, embedder=FakeEmbedder())


def test_index_refuses_another_model_and_inconsistent_counts(built, tmp_path):
    cfg, _ = built
    with pytest.raises(store.IndexMismatch, match="EMBEDDING_MODEL"):
        HybridIndex(cfg.index_dir, expected_model="BAAI/bge-small-en-v1.5")
    with pytest.raises(store.IndexMismatch):
        HybridIndex(cfg.index_dir, embedder=FakeEmbedder("other/model"))
    other = make_corpus(tmp_path)
    build_index(other, embedder=FakeEmbedder())
    manifest = store.read_manifest(other.index_dir)
    store.write_manifest(other.index_dir, {**manifest, "total": 99})
    with pytest.raises(store.IndexMismatch, match="99"):
        HybridIndex(other.index_dir, embedder=FakeEmbedder())


# -------------------------------------------------------------------- search


def test_get_chunk_round_trips_every_indexed_chunk(index):
    assert len(index) == 12
    assert index.counts() == {"docs": 9, "release_notes": 3, "mailing_list": 0}
    for chunk in ALL_CHUNKS:
        if chunk.chunk_id in LEFT_OUT:
            assert chunk.chunk_id not in index
            continue
        assert index.get_chunk(chunk.chunk_id) == chunk
    assert index.get_chunk("relnotes:16.0#highlights").meta["release_date"] is None
    ids = ["relnotes:16.3#changes:i12", DOCS_CHUNKS[0].chunk_id]
    assert [c.chunk_id for c in index.get_chunks(ids)] == ids
    with pytest.raises(KeyError, match="no-such"):
        index.get_chunks([*ids, "docs:no-such#chunk"])
    assert len(index.get_chunks([*ids, "docs:no-such#chunk"], strict=False)) == 2
    with pytest.raises(KeyError):
        index.get_chunk("docs:sql-explain#see-also")
    # Returned chunks are copies: changing one does not change the index.
    index.get_chunk(ids[0]).meta["authors"].append("someone")
    assert index.get_chunk(ids[0]).meta["authors"] == ["Tom Lane", "Andres Freund"]


def test_search_by_setting_name_finds_its_setting_chunk(index):
    for name, chunk in [
        ("work_mem", DOCS_CHUNKS[0]),
        ("maintenance_work_mem", DOCS_CHUNKS[1]),
        ("max_parallel_workers_per_gather", DOCS_CHUNKS[2]),
    ]:
        hits = index.search(name, k=3)
        assert hits[0].chunk_id == chunk.chunk_id, [h.chunk_id for h in hits]


def test_hits_carry_chunk_fields_and_component_ranks(index):
    hits = index.search("temporary files sort spill", k=5)
    assert [h.rank for h in hits] == [1, 2, 3, 4, 5]
    assert all(a.score >= b.score for a, b in zip(hits, hits[1:], strict=False))
    for hit in hits:
        original = next(c for c in ALL_CHUNKS if c.chunk_id == hit.chunk_id)
        assert hit.chunk == original
        assert (hit.title, hit.url, hit.text, hit.settings) == (
            original.title,
            original.url,
            original.text,
            original.settings,
        )
        assert hit.vector_rank is not None or hit.bm25_rank is not None
        if hit.vector_rank is not None:
            assert -1.0 <= hit.vector_score <= 1.0
        if hit.bm25_rank is not None:
            assert hit.bm25_score > 0
    assert json.loads(json.dumps(hits[0].to_dict()))["chunk_id"] == hits[0].chunk_id


def test_source_filter_is_applied_before_truncation(index):
    query = "sorts spill to temporary files on disk, external merge sort"
    # Unfiltered, the five docs sections fill the top of both retrievers.
    top = index.search(query, k=2, depth=2)
    assert {h.source for h in top} == {"docs"}
    # With only two candidates per retriever, a post-hoc filter would find nothing.
    hits = index.search(query, k=1, source="release_notes", depth=2)
    assert [h.chunk_id for h in hits] == ["relnotes:16.3#changes:i12"]
    for mode in ("vector", "bm25"):
        hits = index.search(query, k=1, source="release_notes", depth=2, mode=mode)
        assert [h.chunk_id for h in hits] == ["relnotes:16.3#changes:i12"]
    hits = index.search(query, k=20, source="release_notes")
    assert hits and {h.source for h in hits} == {"release_notes"}
    hits = index.search(query, k=20, source=["docs", "release_notes"])
    assert {h.source for h in hits} == {"docs", "release_notes"}
    assert index.search(query, k=5, source="mailing_list") == []


def test_search_is_deterministic(index):
    query = "parallel workers per gather node"
    first = [h.to_dict() for h in index.search(query, k=10)]
    for _ in range(3):
        assert [h.to_dict() for h in index.search(query, k=10)] == first


def test_search_modes(index):
    vector = index.search("work_mem", k=3, mode="vector")
    assert all(h.bm25_rank is None and h.vector_rank == h.rank for h in vector)
    assert vector[0].score == vector[0].vector_score
    bm25 = index.search("work_mem", k=3, mode="bm25")
    assert all(h.vector_rank is None and h.bm25_rank == h.rank for h in bm25)
    # Only stop words: BM25 has nothing, vector search still answers.
    assert index.search("the of and", k=3, mode="bm25") == []
    assert index.search("the of and", k=3)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"query": " "}, "empty query"),
        ({"query": "x", "k": 0}, "k must be"),
        ({"query": "x", "mode": "fuzzy"}, "mode"),
        ({"query": "x", "source": "wiki"}, "unknown source"),
    ],
)
def test_search_rejects_bad_arguments(index, kwargs, message):
    with pytest.raises(ValueError, match=message):
        index.search(**kwargs)


def test_module_level_functions_use_the_shared_index(index, monkeypatch):
    monkeypatch.setattr(search_module, "_default_index", index)
    import pgdiag.index as api

    assert api.get_index() is index
    assert api.search("work_mem", k=1)[0].chunk_id == DOCS_CHUNKS[0].chunk_id
    assert api.get_chunk("relnotes:16.3#changes:i12") == RELNOTE_CHUNKS[0]
    assert [c.chunk_id for c in api.get_chunks(["relnotes:16.0#highlights"])] == [
        "relnotes:16.0#highlights"
    ]


def test_cli_get_and_stats(built, monkeypatch, capsys):
    cfg, _ = built
    monkeypatch.setenv("PGDIAG_ROOT", str(cfg.root))
    monkeypatch.setenv("EMBEDDING_MODEL", "fake/bag-of-words")
    get_config.cache_clear()
    try:
        assert cli.main(["get", "relnotes:16.3#changes:i12"]) == 0
        printed = json.loads(capsys.readouterr().out)
        assert Chunk.from_dict(printed) == RELNOTE_CHUNKS[0]
        assert cli.main(["get", "docs:nope#x"]) == 1
        assert cli.main(["stats"]) == 0
        out = capsys.readouterr().out
        assert "docs                9" in out and "release_notes       3" in out
        assert "mailing_list        0  (not built)" in out
        monkeypatch.setenv("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
        get_config.cache_clear()
        assert cli.main(["stats"]) == 1
        assert "EMBEDDING_MODEL" in capsys.readouterr().err
    finally:
        get_config.cache_clear()
