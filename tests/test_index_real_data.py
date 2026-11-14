"""Acceptance and sanity checks on the real index under ``data/index/`` (T4 output).

Skipped, with instructions, when the index has not been built. Once it exists
every check here must pass: a stale or wrong index is a failure, not a skip.
``PGDIAG_REQUIRE_DATA=1`` (``make test-data``) turns the skip into a failure.

One ``HybridIndex`` (and so one model load, a few seconds) is shared by the
whole module.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter

import numpy as np
import pytest

from pgdiag.chunks import SOURCES, Chunk
from pgdiag.config import get_config
from pgdiag.index import store
from pgdiag.index.build import file_sha256, find_chunk_files, select_chunks
from pgdiag.index.probes import (
    SYMPTOM_PROBES,
    format_rank,
    run_probes,
    setting_chunk_id,
)
from pgdiag.index.search import HybridIndex
from pgdiag.settings import TARGET_SETTINGS

pytestmark = [pytest.mark.data, pytest.mark.slow]


def _skip_or_fail(reason: str) -> None:
    if os.environ.get("PGDIAG_REQUIRE_DATA") == "1":
        pytest.fail(reason)
    pytest.skip(reason)


@pytest.fixture(scope="module")
def index() -> HybridIndex:
    cfg = get_config()
    try:
        index = HybridIndex.load(cfg)  # a model mismatch raises IndexMismatch: a failure
    except store.IndexNotBuilt as exc:
        _skip_or_fail(
            f"{exc} It needs data/chunks/docs.jsonl and release_notes.jsonl (`make ingest-docs`, "
            "`make ingest-release-notes`); embedding takes about 10 minutes on 4 CPU cores."
        )
    index.warm_up()
    return index


@pytest.fixture(scope="module")
def file_chunks() -> list[Chunk]:
    from pgdiag.chunks import read_jsonl

    files = find_chunk_files(get_config().chunks_dir)
    return [chunk for path in files for chunk in read_jsonl(path)]


# ----------------------------------------------------------------- acceptance


@pytest.mark.parametrize("name", TARGET_SETTINGS)
def test_searching_a_setting_name_returns_its_docs_chunk_in_the_top_3(index, name):
    hits = index.search(name, k=3)
    expected = [
        h
        for h in hits
        if h.source == "docs"
        and h.meta.get("chunk_type") == "setting"
        and h.meta.get("setting") == name
    ]
    assert expected, [(h.rank, h.chunk_id) for h in hits]
    assert expected[0].chunk_id == setting_chunk_id(index, name)


def test_counts_per_source_equal_the_chunk_files_minus_documented_drops(index, file_chunks):
    manifest = index.manifest
    kept, dropped = select_chunks(file_chunks, manifest["selection"]["min_docs_section_chars"])
    expected = {source: 0 for source in SOURCES} | dict(Counter(c.source for c in kept))
    assert index.counts() == expected
    assert manifest["counts"] == expected
    assert expected["docs"] > 6000 and expected["release_notes"] > 600
    assert expected["mailing_list"] == 0  # T3 was skipped
    assert sorted(manifest["selection"]["left_out_ids"]) == sorted(c.chunk_id for c in dropped)
    assert all(c.meta["chunk_type"] == "section" and c.source == "docs" for c in dropped)
    setting_ids = {c.chunk_id for c in file_chunks if c.meta.get("chunk_type") == "setting"}
    assert len(setting_ids) > 350 and all(cid in index for cid in setting_ids)


def test_manifest_matches_the_current_chunk_files_and_model(index):
    manifest = index.manifest
    files = find_chunk_files(get_config().chunks_dir)
    assert {path.name: file_sha256(path) for path in files} == {
        name: info["sha256"] for name, info in manifest["inputs"].items()
    }, "the chunk files changed since the index was built: run `make index`"
    emb = manifest["embedding"]
    assert emb["model"] == get_config().embedding_model
    assert emb["revision"] and len(emb["revision"]) == 40  # a Hugging Face commit hash
    assert emb["normalized"] is True and emb["dim"] == index.embedder.dim
    assert emb["document_template"] == store.DOCUMENT_TEMPLATE


def test_source_filter_returns_only_that_source(index):
    for source in ("docs", "release_notes"):
        hits = index.search("autovacuum fails to remove dead tuples", k=25, source=source)
        assert len(hits) == 25 and {h.source for h in hits} == {source}
    both = index.search("shared_buffers memory", k=50, source=["docs", "release_notes"])
    assert {h.source for h in both} <= {"docs", "release_notes"}
    assert index.search("work_mem", k=5, source="mailing_list") == []


def test_get_chunk_round_trips_every_indexed_chunk_exactly(index, file_chunks):
    by_id = {c.chunk_id: c for c in file_chunks}
    for chunk_id in index.chunk_ids:
        assert index.get_chunk(chunk_id) == by_id[chunk_id], chunk_id
    relnotes = [cid for cid in index.chunk_ids if cid.startswith("relnotes:")]
    assert len(relnotes) == index.counts()["release_notes"]
    assert index.get_chunk("relnotes:16.0#highlights").source == "release_notes"


def test_identical_queries_give_identical_results(index):
    query = "temporary files written by hash joins and sorts"
    first = [h.to_dict() for h in index.search(query, k=20)]
    assert [h.to_dict() for h in index.search(query, k=20)] == first


def test_results_are_identical_in_another_process(index):
    """Same query, fresh process: same chunks, same order (Chroma's HNSW query was not)."""
    query = "sorts spilling to temporary files on disk, external merge"
    code = (
        "import json, logging; logging.disable(logging.WARNING)\n"
        "from pgdiag.index import search\n"
        f"print(json.dumps([h.chunk_id for h in search({query!r}, k=100)]))"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=300
    )
    other = json.loads(done.stdout.strip().splitlines()[-1])
    assert other == [h.chunk_id for h in index.search(query, k=100)]


def test_vector_search_is_exact_and_chroma_agrees(index):
    """Vector retrieval is exact cosine over the stored embeddings; Chroma's own HNSW
    query over the same vectors finds (almost) the same neighbours."""
    records = index._collection.get(include=["embeddings"])
    ids = records["ids"]
    matrix = np.asarray(records["embeddings"], dtype=np.float32)
    assert np.allclose(np.linalg.norm(matrix, axis=1), 1.0, atol=1e-3)  # normalised
    recalls = []
    for query, _ in SYMPTOM_PROBES:
        vector = index.embedder.embed_queries([query])[0]
        exact = [ids[i] for i in np.argsort(-(matrix @ vector), kind="stable")[:50]]
        ours = [cid for cid, _ in index.vector_ranking(query, 50)]
        assert set(ours) == set(exact)
        hnsw = index._collection.query(query_embeddings=[vector], n_results=50)["ids"][0]
        recalls.append(len(set(exact) & set(hnsw)) / 50)
    assert min(recalls) >= 0.9, recalls


def test_symptom_probes_report(index, capsys):
    """Informative: prints where each symptom query ranks the relevant setting chunk."""
    results = run_probes(index)
    assert len(results) == len(SYMPTOM_PROBES)
    with capsys.disabled():
        print("\nsymptom probes: rank of the setting's docs chunk (top 100)")
        print(f"  {'setting':<32} hybrid vector  bm25")
        for r in results:
            print(
                f"  {r.setting:<32} {format_rank(r.hybrid):>6} {format_rank(r.vector):>6} "
                f"{format_rank(r.bm25):>5}"
            )
