"""Hybrid search over the RAG index: BM25 + vector similarity, fused with RRF.

Usage (T8 and later)::

    from pgdiag.index import search, get_chunk, get_chunks

    hits = search("sorts spill to temporary files on disk", k=10)
    hits = search("checkpoints happen too often", k=5, source="docs")
    hits = search("...", source=["docs", "release_notes"])
    for hit in hits:
        hit.chunk_id, hit.source, hit.title, hit.url, hit.settings, hit.text
        hit.score, hit.vector_rank, hit.vector_score, hit.bm25_rank, hit.bm25_score
    chunk = get_chunk("relnotes:16.3#changes:i12")   # the original Chunk, exactly

The module-level functions share one lazily created ``HybridIndex`` (built
from ``get_config()``). For other settings create a ``HybridIndex`` yourself
and reuse it: loading the chunks takes about a second, the embedding model a
few seconds on first use, and every search after that is fast.

How a search works
------------------
1. The source filter (``None``, one source or a list) is applied inside both
   retrievers, before anything is cut off.
2. Vector: the query is embedded with the same model, revision and query prefix
   as the index (from ``manifest.json``) and compared with every stored
   embedding of the allowed sources (exact cosine similarity; the embeddings
   are read from Chroma when the index is loaded); the ``depth`` nearest are
   taken. Exact search instead of Chroma's HNSW query, because the HNSW query
   was seen to return slightly different neighbour lists in different
   processes for the same query vector; exact search is deterministic and
   costs about a millisecond for ~7,000 vectors.
3. BM25: the query is tokenized with ``pgdiag.index.tokenize`` and every chunk
   of the allowed sources is scored (bm25s, Lucene variant, k1 1.5, b 0.75);
   the ``depth`` best with a positive score are taken.
4. Reciprocal Rank Fusion: ``score = w_vec / (rrf_k + vector_rank) +
   w_bm25 / (rrf_k + bm25_rank)`` (a list the chunk is missing from adds
   nothing). Ties are broken by the better component rank, then by chunk id, so
   identical queries always give identical results.

``mode="vector"`` or ``mode="bm25"`` uses one retriever only (for comparisons);
the hit's ``score`` is then that retriever's own score.
"""

from __future__ import annotations

import copy
import logging
import threading
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import bm25s  # imported here: it sets its logger to DEBUG on import, before the CLI quiets it
import numpy as np

from pgdiag.chunks import SOURCES, Chunk
from pgdiag.config import Config, get_config
from pgdiag.index import store
from pgdiag.index.embed import Embedder, SentenceTransformerEmbedder
from pgdiag.index.tokenize import tokenize

log = logging.getLogger("pgdiag.index")

MODES = ("hybrid", "vector", "bm25")


@dataclass(frozen=True)
class FusionParams:
    """Reciprocal Rank Fusion settings."""

    rrf_k: int = 60
    vector_weight: float = 1.0
    bm25_weight: float = 1.0
    #: Candidates taken from each retriever, after the source filter.
    depth: int = 100


@dataclass
class Hit:
    chunk_id: str
    source: str
    title: str
    url: str
    settings: list[str]
    text: str
    score: float  # fused RRF score (hybrid) or the single retriever's score
    rank: int  # 1-based position in this result list
    vector_rank: int | None = None  # 1-based rank among the vector candidates, None if absent
    vector_score: float | None = None  # cosine similarity
    bm25_rank: int | None = None
    bm25_score: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def chunk(self) -> Chunk:
        return Chunk(
            chunk_id=self.chunk_id,
            source=self.source,
            url=self.url,
            title=self.title,
            text=self.text,
            settings=list(self.settings),
            meta=copy.deepcopy(self.meta),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_sources(source: str | Iterable[str] | None) -> tuple[str, ...] | None:
    """``None`` (all sources) or a sorted tuple of known source names."""
    if source is None:
        return None
    names = (source,) if isinstance(source, str) else tuple(source)
    unknown = [name for name in names if name not in SOURCES]
    if unknown:
        raise ValueError(f"unknown source(s) {unknown}; known: {list(SOURCES)}")
    if not names:
        raise ValueError("empty source filter; pass None for all sources")
    return tuple(sorted(set(names)))


def rrf_fuse(
    rankings: Sequence[tuple[Sequence[str], float]], rrf_k: int
) -> list[tuple[str, float, int]]:
    """Fuse ranked id lists (best first, each with a weight) by Reciprocal Rank Fusion.

    Returns ``(id, score, best_rank)`` sorted by score, then best component
    rank, then id (deterministic).
    """
    scores: dict[str, float] = {}
    best: dict[str, int] = {}
    for ids, weight in rankings:
        for rank, chunk_id in enumerate(ids, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (rrf_k + rank)
            best[chunk_id] = min(best.get(chunk_id, rank), rank)
    fused = [(chunk_id, score, best[chunk_id]) for chunk_id, score in scores.items()]
    fused.sort(key=lambda item: (-item[1], item[2], item[0]))
    return fused


class HybridIndex:
    """The loaded index: chunks, Chroma collection, BM25 and (lazily) the embedding model.

    ``expected_model``: refuse an index built with another embedding model
    (``HybridIndex.load`` passes ``cfg.embedding_model``). ``embedder``: inject
    an embedder (tests); by default the model recorded in the manifest is
    loaded on the first vector search.
    """

    def __init__(
        self,
        index_dir: Path,
        *,
        expected_model: str | None = None,
        embedder: Embedder | None = None,
        fusion: FusionParams | None = None,
    ) -> None:
        self.index_dir = index_dir
        self.manifest = store.read_manifest(index_dir)
        store.check_manifest(self.manifest, expected_model)
        self.model_name: str = self.manifest["embedding"]["model"]
        if embedder is not None and embedder.model_name != self.model_name:
            raise store.IndexMismatch(
                f"embedder {embedder.model_name!r} given, index built with {self.model_name!r}"
            )
        self.fusion = fusion or FusionParams()
        self._embedder = embedder
        self._bm25 = None
        self._lock = threading.Lock()

        client = store.chroma_client(store.chroma_dir(index_dir))
        try:
            self._collection = client.get_collection(store.COLLECTION_NAME, embedding_function=None)
        except Exception as exc:
            raise store.IndexNotBuilt(
                f"{store.chroma_dir(index_dir)} has no collection {store.COLLECTION_NAME!r} "
                f"({exc}). Rebuild with `.venv/bin/pgdiag-index build --rebuild`."
            ) from exc
        records = self._collection.get(include=["documents", "metadatas", "embeddings"])
        chunks = [
            store.record_to_chunk(chunk_id, document, metadata)
            for chunk_id, document, metadata in zip(
                records["ids"], records["documents"], records["metadatas"], strict=True
            )
        ]
        if len(chunks) != self.manifest["total"]:
            raise store.IndexMismatch(
                f"Chroma holds {len(chunks)} chunks, the manifest says {self.manifest['total']}. "
                "Rebuild with `.venv/bin/pgdiag-index build --rebuild`."
            )
        order = sorted(range(len(chunks)), key=lambda i: chunks[i].chunk_id)
        self._chunks: list[Chunk] = [chunks[i] for i in order]
        self._position = {c.chunk_id: i for i, c in enumerate(self._chunks)}
        dim = self.manifest["embedding"]["dim"]
        vectors = np.asarray(records["embeddings"], dtype=np.float64)
        if vectors.shape != (len(chunks), dim):
            raise store.IndexMismatch(
                f"stored embeddings have shape {vectors.shape}, expected ({len(chunks)}, {dim}). "
                "Rebuild with `.venv/bin/pgdiag-index build --rebuild`."
            )
        self._vectors = vectors[order]  # same order as self._chunks
        sources = np.array([c.source for c in self._chunks])
        self._source_masks = {name: sources == name for name in SOURCES}

    @classmethod
    def load(cls, cfg: Config | None = None, **kwargs: Any) -> HybridIndex:
        """The index of ``cfg`` (default ``get_config()``), checked against its embedding model."""
        cfg = cfg or get_config()
        return cls(cfg.index_dir, expected_model=cfg.embedding_model, **kwargs)

    # ------------------------------------------------------------ chunks by id

    def __len__(self) -> int:
        return len(self._chunks)

    def __contains__(self, chunk_id: object) -> bool:
        return chunk_id in self._position

    @property
    def chunk_ids(self) -> list[str]:
        return [c.chunk_id for c in self._chunks]

    def counts(self) -> dict[str, int]:
        """Number of indexed chunks per source (every known source, 0 when absent)."""
        return {name: int(mask.sum()) for name, mask in self._source_masks.items()}

    def get_chunk(self, chunk_id: str) -> Chunk:
        """The original chunk (a copy); ``KeyError`` if the id is not in the index."""
        position = self._position.get(chunk_id)
        if position is None:
            raise KeyError(f"chunk {chunk_id!r} is not in the index")
        return copy.deepcopy(self._chunks[position])

    def get_chunks(self, chunk_ids: Iterable[str], *, strict: bool = True) -> list[Chunk]:
        """Chunks in the order asked. Unknown ids raise ``KeyError`` (``strict``) or are skipped."""
        ids = list(chunk_ids)
        missing = [chunk_id for chunk_id in ids if chunk_id not in self._position]
        if missing and strict:
            raise KeyError(f"chunks not in the index: {missing}")
        return [self.get_chunk(chunk_id) for chunk_id in ids if chunk_id in self._position]

    # -------------------------------------------------------------- retrievers

    @property
    def embedder(self) -> Embedder:
        with self._lock:
            if self._embedder is None:
                emb = self.manifest["embedding"]
                self._embedder = SentenceTransformerEmbedder(
                    emb["model"],
                    revision=emb.get("revision"),
                    query_prefix=emb.get("query_prefix", ""),
                    document_prefix=emb.get("document_prefix", ""),
                )
                if self._embedder.dim != emb["dim"]:
                    raise store.IndexMismatch(
                        f"model {emb['model']} gives {self._embedder.dim}-dimensional vectors, "
                        f"the index holds {emb['dim']}-dimensional ones"
                    )
            return self._embedder

    @property
    def bm25(self):
        with self._lock:
            if self._bm25 is None:
                retriever = bm25s.BM25(
                    method=store.BM25_PARAMS["method"],
                    k1=store.BM25_PARAMS["k1"],
                    b=store.BM25_PARAMS["b"],
                )
                retriever.index(
                    [tokenize(store.document_text(c)) for c in self._chunks], show_progress=False
                )
                self._bm25 = retriever
            return self._bm25

    def warm_up(self) -> None:
        """Load the embedding model and build BM25 now instead of on the first search."""
        _ = self.embedder, self.bm25

    def _mask(self, sources: tuple[str, ...] | None) -> np.ndarray | None:
        if sources is None:
            return None
        mask = np.zeros(len(self._chunks), dtype=bool)
        for name in sources:
            mask |= self._source_masks[name]
        return mask

    def vector_ranking(
        self, query: str, depth: int, sources: tuple[str, ...] | None = None
    ) -> list[tuple[str, float]]:
        """``(chunk_id, cosine similarity)`` of the ``depth`` nearest chunks of ``sources``.

        Exact search over every stored embedding (all are L2-normalised, so the
        dot product is the cosine similarity); chunks of other sources are
        masked out before the cut.
        """
        mask = self._mask(sources)
        available = len(self._chunks) if mask is None else int(mask.sum())
        n_results = min(depth, available)
        if n_results == 0:
            return []
        vector = np.asarray(self.embedder.embed_queries([query])[0], dtype=np.float64)
        # einsum, not ``@``: it bypasses BLAS, whose thread pool fights with torch's right
        # after the query is embedded (about 90 ms lost per query), and its single-threaded
        # summation order is fixed.
        similarity = np.einsum("ij,j->i", self._vectors, vector)
        if mask is not None:
            similarity = np.where(mask, similarity, -np.inf)
        order = np.argsort(-similarity, kind="stable")[:n_results]  # ties: chunk-id order
        return [(self._chunks[i].chunk_id, float(similarity[i])) for i in order]

    def bm25_ranking(
        self, query: str, depth: int, sources: tuple[str, ...] | None = None
    ) -> list[tuple[str, float]]:
        """``(chunk_id, BM25 score)`` of the ``depth`` best chunks of ``sources`` (score > 0)."""
        tokens = tokenize(query)
        if not tokens:
            return []
        scores = np.asarray(self.bm25.get_scores(tokens), dtype=np.float64)
        mask = self._mask(sources)
        if mask is not None:
            scores = np.where(mask, scores, 0.0)
        order = np.argsort(-scores, kind="stable")[:depth]  # ties: chunk-id order
        return [(self._chunks[i].chunk_id, float(scores[i])) for i in order if scores[i] > 0]

    # ------------------------------------------------------------------ search

    def search(
        self,
        query: str,
        k: int = 10,
        source: str | Iterable[str] | None = None,
        *,
        mode: str = "hybrid",
        depth: int | None = None,
    ) -> list[Hit]:
        """The ``k`` best chunks for ``query``, optionally only from ``source``.

        ``source``: ``"docs"``, ``"release_notes"``, ``"mailing_list"`` (always
        empty: never built), a list of them, or ``None`` for all. ``mode``:
        ``"hybrid"`` (default), ``"vector"`` or ``"bm25"``. ``depth``: candidates
        per retriever (default ``fusion.depth``, at least ``k``).
        """
        if not query or not query.strip():
            raise ValueError("empty query")
        if k < 1:
            raise ValueError(f"k must be at least 1, got {k}")
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
        sources = normalize_sources(source)
        depth = max(depth or self.fusion.depth, k)

        vector = self.vector_ranking(query, depth, sources) if mode != "bm25" else []
        lexical = self.bm25_ranking(query, depth, sources) if mode != "vector" else []
        vector_info = {cid: (rank, score) for rank, (cid, score) in enumerate(vector, start=1)}
        bm25_info = {cid: (rank, score) for rank, (cid, score) in enumerate(lexical, start=1)}

        if mode == "hybrid":
            fused = rrf_fuse(
                [
                    ([cid for cid, _ in vector], self.fusion.vector_weight),
                    ([cid for cid, _ in lexical], self.fusion.bm25_weight),
                ],
                self.fusion.rrf_k,
            )
            ranked = [(cid, score) for cid, score, _ in fused]
        else:
            ranked = vector if mode == "vector" else lexical

        hits = []
        for rank, (chunk_id, score) in enumerate(ranked[:k], start=1):
            chunk = self._chunks[self._position[chunk_id]]
            v_rank, v_score = vector_info.get(chunk_id, (None, None))
            b_rank, b_score = bm25_info.get(chunk_id, (None, None))
            hits.append(
                Hit(
                    chunk_id=chunk.chunk_id,
                    source=chunk.source,
                    title=chunk.title,
                    url=chunk.url,
                    settings=list(chunk.settings),
                    text=chunk.text,
                    score=float(score),
                    rank=rank,
                    vector_rank=v_rank,
                    vector_score=v_score,
                    bm25_rank=b_rank,
                    bm25_score=b_score,
                    meta=copy.deepcopy(chunk.meta),
                )
            )
        return hits


# ---------------------------------------------------------------- module level

_default_index: HybridIndex | None = None
_default_lock = threading.Lock()


def get_index() -> HybridIndex:
    """The process-wide ``HybridIndex`` for ``get_config()``, created on first use."""
    global _default_index
    with _default_lock:
        if _default_index is None:
            _default_index = HybridIndex.load()
        return _default_index


def reset_index() -> None:
    """Forget the process-wide index (e.g. after rebuilding it in the same process)."""
    global _default_index
    with _default_lock:
        _default_index = None


def search(
    query: str, k: int = 10, source: str | Iterable[str] | None = None, **kwargs: Any
) -> list[Hit]:
    """``get_index().search(...)``; see ``HybridIndex.search``."""
    return get_index().search(query, k, source, **kwargs)


def get_chunk(chunk_id: str) -> Chunk:
    """The original chunk with this id (``KeyError`` if it is not in the index)."""
    return get_index().get_chunk(chunk_id)


def get_chunks(chunk_ids: Iterable[str], *, strict: bool = True) -> list[Chunk]:
    return get_index().get_chunks(chunk_ids, strict=strict)
