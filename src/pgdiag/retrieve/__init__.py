"""Retrieval step (T8): symptom document -> search queries -> top chunks. See ``core.py``."""

from pgdiag.retrieve.core import Retrieved, build_queries, retrieve

__all__ = ["Retrieved", "build_queries", "retrieve"]
