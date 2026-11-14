"""RAG index (T4): embeddings, one Chroma collection, hybrid BM25 + vector search.

Build it with ``pgdiag-index build`` (``make index``); see ``build.py``. Query
it with::

    from pgdiag.index import search, get_chunk, get_chunks, HybridIndex

    hits = search("temporary files written by sorts", k=10, source="docs")
    chunk = get_chunk(hits[0].chunk_id)

``search`` / ``get_chunk`` / ``get_chunks`` use one shared ``HybridIndex``
created on first use; see ``search.py`` for the details and ``store.py`` for
what is stored.
"""

from pgdiag.index.search import (
    FusionParams,
    Hit,
    HybridIndex,
    get_chunk,
    get_chunks,
    get_index,
    reset_index,
    search,
)
from pgdiag.index.store import IndexMismatch, IndexNotBuilt, IndexStoreError

__all__ = [
    "FusionParams",
    "Hit",
    "HybridIndex",
    "IndexMismatch",
    "IndexNotBuilt",
    "IndexStoreError",
    "get_chunk",
    "get_chunks",
    "get_index",
    "reset_index",
    "search",
]
