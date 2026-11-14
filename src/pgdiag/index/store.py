"""On-disk layout of the index: the Chroma collection, its metadata and the manifest.

Layout under ``cfg.index_dir`` (``data/index/``, gitignored)::

    manifest.json   what was built, from which inputs, with which model (see build.py)
    chroma/         one persistent Chroma collection, ``pgdiag_chunks``

Every chunk is one Chroma record:

- ``id``        = ``chunk_id``, exactly
- ``document``  = ``text`` (the title is in the metadata; the embedding is of
  ``title + "\\n" + text`` and is computed by pgdiag, never by Chroma)
- ``metadata``  = scalars only (``flatten_metadata``): ``source``, ``url``,
  ``title``, ``chunk_type``, ``setting``, ``version``, ``page`` (``""`` when the
  chunk has none), ``settings`` as ``"|name1|name2|"`` (``""`` when empty),
  ``n_settings``, one boolean ``mentions_<target>`` per target setting, and
  ``meta_json``: the original ``meta`` dict as JSON.

``record_to_chunk`` rebuilds the original ``Chunk`` exactly (lists and nulls
in ``meta`` included), so the index alone is enough to get any chunk by id.

The collection is created with a cosine HNSW index, so Chroma can be queried
directly. ``pgdiag.index.search`` does not use that: it reads all embeddings
once and searches them exactly (deterministic across processes; see there).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pgdiag.chunks import Chunk
from pgdiag.settings import TARGET_SETTINGS

COLLECTION_NAME = "pgdiag_chunks"
CHROMA_DIRNAME = "chroma"
MANIFEST_FILE = "manifest.json"
#: Bump when the layout or the meaning of stored fields changes.
FORMAT_VERSION = 1
HNSW_CONFIG: dict[str, Any] = {
    "space": "cosine",
    "ef_construction": 200,
    "ef_search": 200,
    "max_neighbors": 32,
}

#: BM25 settings (bm25s). BM25 is rebuilt in memory when the index is loaded.
BM25_PARAMS: dict[str, Any] = {"library": "bm25s", "method": "lucene", "k1": 1.5, "b": 0.75}
DOCUMENT_TEMPLATE = 'title + "\\n" + text'

MetadataValue = str | int | float | bool


def document_text(chunk: Chunk) -> str:
    """What is embedded and what BM25 indexes for a chunk (``DOCUMENT_TEMPLATE``)."""
    return f"{chunk.title}\n{chunk.text}"


class IndexStoreError(RuntimeError):
    """Base class for problems with the index on disk."""


class IndexNotBuilt(IndexStoreError):
    """There is no (complete) index on disk."""


class IndexMismatch(IndexStoreError):
    """The index on disk does not match what this process expects (model, format, counts)."""


def target_flag(name: str) -> str:
    """Metadata key of the boolean "this chunk mentions target setting ``name``"."""
    return f"mentions_{name}"


def settings_field(settings: list[str]) -> str:
    """``["a", "b"]`` -> ``"|a|b|"``, so ``"|a|"`` is a substring test; ``[]`` -> ``""``."""
    return "|" + "|".join(settings) + "|" if settings else ""


def parse_settings_field(value: str) -> list[str]:
    return [name for name in value.split("|") if name]


def flatten_metadata(chunk: Chunk) -> dict[str, MetadataValue]:
    """Scalar-only Chroma metadata for a chunk (see the module docstring)."""
    meta = chunk.meta

    def text_of(key: str) -> str:
        value = meta.get(key)
        return "" if value is None else str(value)

    flat: dict[str, MetadataValue] = {
        "source": chunk.source,
        "url": chunk.url,
        "title": chunk.title,
        "chunk_type": text_of("chunk_type"),
        "setting": text_of("setting"),
        "version": text_of("version"),
        "page": text_of("page"),
        "settings": settings_field(chunk.settings),
        "n_settings": len(chunk.settings),
        "meta_json": json.dumps(meta, ensure_ascii=False, sort_keys=True),
    }
    mentioned = set(chunk.settings)
    for name in TARGET_SETTINGS:
        flat[target_flag(name)] = name in mentioned
    return flat


def chunk_to_record(chunk: Chunk) -> tuple[str, str, dict[str, MetadataValue]]:
    """``(id, document, metadata)`` for Chroma."""
    return chunk.chunk_id, chunk.text, flatten_metadata(chunk)


def record_to_chunk(chunk_id: str, document: str, metadata: dict[str, Any]) -> Chunk:
    """The original chunk from a Chroma record written by ``chunk_to_record``."""
    return Chunk(
        chunk_id=chunk_id,
        source=metadata["source"],
        url=metadata["url"],
        title=metadata["title"],
        text=document,
        settings=parse_settings_field(metadata["settings"]),
        meta=json.loads(metadata["meta_json"]),
    )


def chroma_dir(index_dir: Path) -> Path:
    return index_dir / CHROMA_DIRNAME


def chroma_client(path: Path):
    """A persistent Chroma client with telemetry off (always the same settings)."""
    import chromadb
    from chromadb.config import Settings

    return chromadb.PersistentClient(path=str(path), settings=Settings(anonymized_telemetry=False))


def manifest_path(index_dir: Path) -> Path:
    return index_dir / MANIFEST_FILE


def read_manifest(index_dir: Path) -> dict[str, Any]:
    """The manifest, or ``IndexNotBuilt`` with instructions."""
    path = manifest_path(index_dir)
    if not path.is_file() or not chroma_dir(index_dir).is_dir():
        raise IndexNotBuilt(
            f"no index at {index_dir} (missing {path.name} or {CHROMA_DIRNAME}/). "
            "Build it with `make index` (= .venv/bin/pgdiag-index build)."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def write_manifest(index_dir: Path, manifest: dict[str, Any]) -> Path:
    path = manifest_path(index_dir)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def check_manifest(manifest: dict[str, Any], expected_model: str | None) -> None:
    """Raise ``IndexMismatch`` if the index cannot be used by this code / configuration."""
    if manifest.get("format_version") != FORMAT_VERSION:
        raise IndexMismatch(
            f"index format {manifest.get('format_version')!r}, this code reads format "
            f"{FORMAT_VERSION}. Rebuild with `.venv/bin/pgdiag-index build --rebuild`."
        )
    built_with = manifest["embedding"]["model"]
    if expected_model is not None and built_with != expected_model:
        raise IndexMismatch(
            f"the index was built with embedding model {built_with!r}, but the configured "
            f"model (EMBEDDING_MODEL) is {expected_model!r}. Rebuild with "
            "`.venv/bin/pgdiag-index build --rebuild` or change EMBEDDING_MODEL back."
        )
