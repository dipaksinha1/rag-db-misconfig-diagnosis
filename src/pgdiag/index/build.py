"""Build the RAG index (T4): embed every chunk and store it in one Chroma collection.

Inputs: every ``data/chunks/*.jsonl`` chunk file that exists (today
``docs.jsonl`` and ``release_notes.jsonl``; ``mailing_list.jsonl`` was never
built, see PROGRESS.md "T3"), read and validated with ``pgdiag.chunks``.

Selection: documentation ``section`` chunks whose text is shorter than
``min_docs_section_chars`` (60) are left out: 339 of them, nearly all "See
Also" lists of command names and one-line "Compatibility" notes. Setting chunks
and release-note chunks are never left out (the shortest release-note items are
real fixes such as "Fix crash with DSM allocations larger than 4GB"). The ids
left out are listed in the manifest.

Embedding: ``title + "\\n" + text`` (the heading path is only in the title),
L2-normalised, CPU only. The model's commit hash is recorded so the searcher
loads exactly the same model version.

Output: ``data/index/chroma/`` and ``data/index/manifest.json`` (see
``pgdiag.index.store``). The new store is written next to the old one and
swapped in only when complete. If the inputs, the model name and the options are
unchanged since the last build, nothing is done unless ``rebuild=True``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import platform
import shutil
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from importlib import metadata as importlib_metadata
from itertools import chain
from pathlib import Path
from typing import Any

from pgdiag.chunks import SOURCES, Chunk, iter_jsonl, validate_chunks
from pgdiag.config import Config
from pgdiag.index import store
from pgdiag.index.embed import Embedder, SentenceTransformerEmbedder
from pgdiag.index.tokenize import TOKENIZER_DESCRIPTION, TOKENIZER_VERSION

log = logging.getLogger("pgdiag.index")

DEFAULT_MIN_DOCS_SECTION_CHARS = 60
DEFAULT_BATCH_SIZE = 32
#: Records per Chroma ``add`` call (Chroma's own limit is above 5,000).
CHROMA_ADD_BATCH = 1000
#: Texts per embedding call; only affects progress logging.
EMBED_SLICE = 512
VERSIONED_PACKAGES = (
    "chromadb",
    "sentence-transformers",
    "transformers",
    "tokenizers",
    "torch",
    "huggingface-hub",
    "bm25s",
    "PyStemmer",
    "numpy",
    "scipy",
)


@dataclass(frozen=True)
class BuildOptions:
    min_docs_section_chars: int = DEFAULT_MIN_DOCS_SECTION_CHARS
    batch_size: int = DEFAULT_BATCH_SIZE
    #: ``None`` means "the model's recommended prefix" (``embed.default_prefixes``).
    query_prefix: str | None = None
    document_prefix: str | None = None


@dataclass
class BuildResult:
    manifest: dict[str, Any]
    skipped: bool = False  # True: inputs unchanged, the existing index was kept
    dropped: list[Chunk] = field(default_factory=list)


def find_chunk_files(chunks_dir: Path) -> list[Path]:
    return sorted(chunks_dir.glob("*.jsonl"))


def is_left_out(chunk: Chunk, min_docs_section_chars: int) -> bool:
    return (
        chunk.source == "docs"
        and chunk.meta.get("chunk_type") == "section"
        and len(chunk.text.strip()) < min_docs_section_chars
    )


def select_chunks(chunks: list[Chunk], min_docs_section_chars: int) -> tuple[list, list]:
    """``(kept, left_out)``; setting chunks and release-note chunks are always kept."""
    kept, dropped = [], []
    for chunk in chunks:
        (dropped if is_left_out(chunk, min_docs_section_chars) else kept).append(chunk)
    return kept, dropped


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fingerprint(input_hashes: dict[str, str], model_name: str, options: BuildOptions) -> str:
    payload = {
        "format_version": store.FORMAT_VERSION,
        "inputs": input_hashes,
        "model": model_name,
        "options": asdict(options),
        "hnsw": store.HNSW_CONFIG,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def library_versions() -> dict[str, str]:
    versions = {"python": platform.python_version()}
    for name in VERSIONED_PACKAGES:
        try:
            versions[name] = importlib_metadata.version(name)
        except importlib_metadata.PackageNotFoundError:
            versions[name] = "not installed"
    return versions


def _existing_index_is_current(index_dir: Path, fp: str) -> dict[str, Any] | None:
    try:
        manifest = store.read_manifest(index_dir)
    except store.IndexNotBuilt:
        return None
    if manifest.get("fingerprint") != fp or manifest.get("format_version") != store.FORMAT_VERSION:
        return None
    client = store.chroma_client(store.chroma_dir(index_dir))
    try:
        count = client.get_collection(store.COLLECTION_NAME, embedding_function=None).count()
    except Exception as exc:  # unreadable store: rebuild it
        log.warning("existing index unreadable (%s); rebuilding", exc)
        return None
    finally:
        client.close()
    return manifest if count == manifest["total"] else None


def _embed(embedder: Embedder, texts: list[str], batch_size: int):
    """Embed in length order (less padding), return vectors in the original order."""
    import numpy as np

    order = sorted(range(len(texts)), key=lambda i: len(texts[i]), reverse=True)
    vectors = np.zeros((len(texts), embedder.dim), dtype=np.float32)
    started = time.monotonic()
    for start in range(0, len(order), EMBED_SLICE):
        rows = order[start : start + EMBED_SLICE]
        vectors[rows] = embedder.embed_documents([texts[i] for i in rows], batch_size=batch_size)
        log.info(
            "embedded %d/%d chunks (%.0f s)",
            min(start + EMBED_SLICE, len(order)),
            len(order),
            time.monotonic() - started,
        )
    return vectors


def _write_store(path: Path, chunks: list[Chunk], vectors, manifest_model: str) -> None:
    if path.exists():
        shutil.rmtree(path)
    client = store.chroma_client(path)
    collection = client.create_collection(
        store.COLLECTION_NAME,
        configuration={"hnsw": dict(store.HNSW_CONFIG)},
        metadata={"format_version": store.FORMAT_VERSION, "embedding_model": manifest_model},
        embedding_function=None,
    )
    for start in range(0, len(chunks), CHROMA_ADD_BATCH):
        batch = chunks[start : start + CHROMA_ADD_BATCH]
        ids, documents, metadatas = zip(*(store.chunk_to_record(c) for c in batch), strict=True)
        collection.add(
            ids=list(ids),
            documents=list(documents),
            metadatas=list(metadatas),
            embeddings=vectors[start : start + len(batch)],
        )
    count = collection.count()
    client.close()  # flush and release the files before the directory is moved
    if count != len(chunks):
        raise store.IndexStoreError(f"Chroma holds {count} records after adding {len(chunks)}")


def _swap_in(index_dir: Path, new_store: Path, manifest: dict[str, Any]) -> None:
    """Replace the live store with ``new_store`` and write the manifest last."""
    live = store.chroma_dir(index_dir)
    old = index_dir / f"{store.CHROMA_DIRNAME}.old"
    store.manifest_path(index_dir).unlink(missing_ok=True)  # no manifest = "not built"
    if old.exists():
        shutil.rmtree(old)
    if live.exists():
        live.rename(old)
    new_store.rename(live)
    store.write_manifest(index_dir, manifest)
    if old.exists():
        shutil.rmtree(old)


def build_index(
    cfg: Config,
    *,
    embedder: Embedder | None = None,
    options: BuildOptions | None = None,
    rebuild: bool = False,
) -> BuildResult:
    """Build ``cfg.index_dir`` from the chunk files in ``cfg.chunks_dir``.

    ``embedder`` defaults to ``SentenceTransformerEmbedder(cfg.embedding_model)``
    (created only when a build is needed). Tests inject a fake one.

    The new store replaces ``data/index/chroma/`` by renaming directories; a
    ``HybridIndex`` opened on the same directory before the build must not be
    used afterwards (load a new one).
    """
    options = options or BuildOptions()
    started = time.monotonic()
    index_dir = cfg.index_dir
    files = find_chunk_files(cfg.chunks_dir)
    if not files:
        raise store.IndexStoreError(
            f"no chunk files (*.jsonl) in {cfg.chunks_dir}. Run `make ingest-docs` and "
            "`make ingest-release-notes` first."
        )
    input_hashes = {path.name: file_sha256(path) for path in files}
    model_name = embedder.model_name if embedder is not None else cfg.embedding_model
    fp = fingerprint(input_hashes, model_name, options)
    if not rebuild:
        current = _existing_index_is_current(index_dir, fp)
        if current is not None:
            log.info("index is up to date (inputs, model and options unchanged); not rebuilding")
            return BuildResult(manifest=current, skipped=True)

    chunks_by_file = {path.name: list(iter_jsonl(path)) for path in files}
    all_chunks = validate_chunks(chain.from_iterable(chunks_by_file.values()))  # ids unique
    kept, dropped = select_chunks(all_chunks, options.min_docs_section_chars)
    log.info(
        "%d chunks from %d files; %d left out (docs sections under %d characters); %d to index",
        len(all_chunks),
        len(files),
        len(dropped),
        options.min_docs_section_chars,
        len(kept),
    )

    load_started = time.monotonic()
    if embedder is None:
        embedder = SentenceTransformerEmbedder(
            cfg.embedding_model,
            query_prefix=options.query_prefix,
            document_prefix=options.document_prefix,
        )
    model_load_seconds = time.monotonic() - load_started
    log.info(
        "model %s (revision %s, dim %d) loaded in %.1f s",
        embedder.model_name,
        embedder.revision,
        embedder.dim,
        model_load_seconds,
    )

    embed_started = time.monotonic()
    vectors = _embed(embedder, [store.document_text(c) for c in kept], options.batch_size)
    embed_seconds = time.monotonic() - embed_started

    index_dir.mkdir(parents=True, exist_ok=True)
    new_store = index_dir / f"{store.CHROMA_DIRNAME}.new"
    store_started = time.monotonic()
    _write_store(new_store, kept, vectors, embedder.model_name)
    store_seconds = time.monotonic() - store_started

    counts = Counter(c.source for c in kept)
    chunk_types: dict[str, dict[str, int]] = {}
    for chunk in kept:
        per_source = chunk_types.setdefault(chunk.source, {})
        kind = str(chunk.meta.get("chunk_type", ""))
        per_source[kind] = per_source.get(kind, 0) + 1
    manifest: dict[str, Any] = {
        "format_version": store.FORMAT_VERSION,
        "fingerprint": fp,
        "built_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "timing_seconds": {
            "model_load": round(model_load_seconds, 1),
            "embed": round(embed_seconds, 1),
            "store": round(store_seconds, 1),
            "total": round(time.monotonic() - started, 1),
        },
        "embedding": {
            "model": embedder.model_name,
            "revision": embedder.revision,
            "dim": embedder.dim,
            "normalized": True,
            "max_seq_length": embedder.max_seq_length,
            "query_prefix": embedder.query_prefix,
            "document_prefix": embedder.document_prefix,
            "document_template": store.DOCUMENT_TEMPLATE,
            "device": "cpu",
            "batch_size": options.batch_size,
        },
        "store": {
            "backend": "chroma",
            "path": store.CHROMA_DIRNAME,
            "collection": store.COLLECTION_NAME,
            "hnsw": store.HNSW_CONFIG,
        },
        "bm25": {
            **store.BM25_PARAMS,
            "text": store.DOCUMENT_TEMPLATE,
            "tokenizer_version": TOKENIZER_VERSION,
            "tokenizer": TOKENIZER_DESCRIPTION,
            "persisted": False,
            "note": "rebuilt in memory from the stored chunks each time the index is loaded",
        },
        "inputs": {
            name: {
                "sha256": input_hashes[name],
                "bytes": (cfg.chunks_dir / name).stat().st_size,
                "chunks": len(chunks_by_file[name]),
                "sources": dict(Counter(c.source for c in chunks_by_file[name])),
            }
            for name in input_hashes
        },
        "selection": {
            "rule": "leave out docs chunks with chunk_type 'section' whose text is shorter than "
            "min_docs_section_chars; never setting chunks or other sources",
            "min_docs_section_chars": options.min_docs_section_chars,
            "left_out": dict(Counter(c.source for c in dropped)),
            "left_out_ids": [c.chunk_id for c in dropped],
        },
        "counts": {source: counts.get(source, 0) for source in SOURCES},
        "chunk_types": chunk_types,
        "total": len(kept),
        "versions": library_versions(),
    }
    _swap_in(index_dir, new_store, manifest)
    log.info("index written to %s in %.0f s", index_dir, time.monotonic() - started)
    return BuildResult(manifest=manifest, skipped=False, dropped=dropped)
