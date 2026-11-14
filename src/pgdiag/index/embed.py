"""Text embeddings for the index: a sentence-transformers model on the CPU.

``SentenceTransformerEmbedder`` wraps the model named by ``cfg.embedding_model``
(default ``BAAI/bge-small-en-v1.5``). Embeddings are always L2-normalised, so
the inner product is the cosine similarity.

Prefixes are model-aware (``default_prefixes``): BGE English models get the
recommended instruction in front of **queries only**
("Represent this sentence for searching relevant passages: "); E5 models get
``query: `` / ``passage: ``; other models get none. The index build records the
prefixes it used in ``manifest.json`` and the searcher reuses exactly those, so
queries are always embedded the way the index expects.

Inputs longer than the model's limit (512 tokens for BGE) are truncated by the
model: 281 of the 6,851 indexed chunks (4 %, mostly documentation sections with
code or tables) lose their tail in the vector index. BM25 always sees the
whole text.

Anything with the ``Embedder`` attributes and methods can be injected instead
(the tests use a deterministic fake).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import numpy as np

log = logging.getLogger("pgdiag.index")

BGE_QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "


def default_prefixes(model_name: str) -> tuple[str, str]:
    """``(query_prefix, document_prefix)`` recommended for a model."""
    name = model_name.rstrip("/").rsplit("/", 1)[-1].lower()
    if name.startswith("bge-") and "-en" in name:
        return BGE_QUERY_INSTRUCTION, ""
    if name.startswith(("e5-", "multilingual-e5-")):
        return "query: ", "passage: "
    return "", ""


class Embedder(Protocol):
    """What the index needs from an embedding model."""

    model_name: str
    revision: str | None  # exact model version (Hugging Face commit hash) if known
    dim: int
    max_seq_length: int | None
    query_prefix: str
    document_prefix: str

    def embed_documents(self, texts: Sequence[str], batch_size: int = 32) -> np.ndarray:
        """``(len(texts), dim)`` float32, L2-normalised; ``document_prefix`` is added here."""
        ...

    def embed_queries(self, texts: Sequence[str]) -> np.ndarray:
        """``(len(texts), dim)`` float32, L2-normalised; ``query_prefix`` is added here."""
        ...


def snapshot_revision(model_name: str, revision: str | None = None) -> str | None:
    """The Hugging Face commit hash of the cached model (``None`` for a local directory)."""
    if Path(model_name).is_dir():
        return None
    from huggingface_hub import try_to_load_from_cache

    path = try_to_load_from_cache(model_name, "config.json", revision=revision)
    if isinstance(path, str):
        parts = Path(path).parts
        if "snapshots" in parts:
            return parts[parts.index("snapshots") + 1]
    return revision


class SentenceTransformerEmbedder:
    """A sentence-transformers model on the CPU, loaded once in ``__init__``.

    ``revision`` pins the model version (a commit hash, as recorded in the
    index manifest). The model is loaded from the local Hugging Face cache when
    possible and downloaded from huggingface.co otherwise.
    """

    def __init__(
        self,
        model_name: str,
        *,
        revision: str | None = None,
        query_prefix: str | None = None,
        document_prefix: str | None = None,
        device: str = "cpu",
    ) -> None:
        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        from sentence_transformers import SentenceTransformer

        default_query, default_document = default_prefixes(model_name)
        self.model_name = model_name
        self.query_prefix = default_query if query_prefix is None else query_prefix
        self.document_prefix = default_document if document_prefix is None else document_prefix
        try:
            model = SentenceTransformer(
                model_name, revision=revision, device=device, local_files_only=True
            )
        except Exception as exc:  # not cached yet (or not completely): download it
            log.info("model %s not in the local cache (%s); downloading", model_name, exc)
            model = SentenceTransformer(model_name, revision=revision, device=device)
        self._model = model
        self.revision = snapshot_revision(model_name, revision)
        self.dim = int(model.get_embedding_dimension())
        self.max_seq_length = model.max_seq_length

    def _encode(self, texts: Sequence[str], batch_size: int) -> np.ndarray:
        vectors = self._model.encode(
            list(texts),
            batch_size=batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return np.asarray(vectors, dtype=np.float32)

    def embed_documents(self, texts: Sequence[str], batch_size: int = 32) -> np.ndarray:
        return self._encode([self.document_prefix + t for t in texts], batch_size)

    def embed_queries(self, texts: Sequence[str]) -> np.ndarray:
        return self._encode([self.query_prefix + t for t in texts], 32)
