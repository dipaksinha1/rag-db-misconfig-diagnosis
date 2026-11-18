"""Symptom document -> search queries -> merged, de-duplicated top chunks (T8).

``build_queries`` reads only the document's content (never a file name or case
id, never the ``config`` section) and applies fixed, generic rules: each rule
that fires adds one query in documentation vocabulary; two generic queries
(slow-statement plan nodes, workload summary) are always added. ``retrieve``
runs every query with hybrid search and merges the result lists round-robin
(rank 1 of every query, then rank 2, ...), de-duplicated by chunk id.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from pgdiag.index.search import HybridIndex, get_index

DEFAULT_K = 12
PER_QUERY = 10  # hits fetched per query before merging
MAX_CHARS = 1500  # each chunk's text is trimmed to this for the prompt

# Generic thresholds, the same for every document.
TEMP_FILES_MIN = 100
HIT_PCT_MAX = 90.0
JIT_SHARE_MIN = 0.25  # JIT time / execution time of the top statements
MISESTIMATE_FACTOR = 10
MISESTIMATE_MIN_ROWS = 1000
SLOW_PLAN_MS = 500

_ROWS = re.compile(
    r"rows=(\d+) width=\d+\) \(actual time=[\d.]+\.\.([\d.]+) rows=(\d+) loops=(\d+)"
)
_NODE = re.compile(r"(?:->  |^)([A-Z][A-Za-z ]+?)(?: on | using |  \()", re.M)


@dataclass
class Retrieved:
    chunk_id: str
    source: str
    title: str
    url: str
    text: str
    query: str  # the query that found it (its best rank)
    rank: int  # its rank within that query's results

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _plans(symptoms: dict) -> list[str]:
    return [e.get("plan", "") for e in symptoms.get("explain", [])]


def _node_types(plan: str) -> list[str]:
    seen: list[str] = []
    for name in _NODE.findall(plan):
        if name not in seen:
            seen.append(name)
    return seen


def build_queries(symptoms: dict) -> list[str]:
    """Search queries for one symptom document, deterministic and content-only."""
    db = symptoms.get("database_stats", {})
    stmts = symptoms.get("top_statements", [])
    plans = _plans(symptoms)
    text = "\n".join(plans)
    queries: list[str] = []

    if db.get("temp_files", 0) >= TEMP_FILES_MIN or "external merge" in text:
        queries.append(
            "sort and hash operations spill to temporary disk files, external merge, "
            "memory used by query operations before writing to temporary files"
        )
    if db.get("hit_pct", 100.0) < HIT_PCT_MAX:
        queries.append(
            "low shared buffer cache hit ratio, many blocks read from disk, "
            "amount of memory the server uses for shared memory buffers"
        )
    exec_ms = sum(s.get("total_ms", 0) for s in stmts)
    jit_ms = sum(s.get("jit_ms", 0) for s in stmts)
    if exec_ms and jit_ms / exec_ms >= JIT_SHARE_MIN:
        queries.append(
            "JIT compilation time dominates short queries, query cost above which JIT is activated"
        )
    if "Seq Scan" in text and any(t.get("idx_scan", 0) for t in symptoms.get("table_stats", [])):
        queries.append(
            "planner chooses sequential scan instead of index scan, "
            "planner cost estimate of a non-sequentially-fetched disk page"
        )
    slow = any(float(m.group(2)) >= SLOW_PLAN_MS for m in _ROWS.finditer(text))
    if slow and "Gather" not in text:
        queries.append(
            "parallel query not used, no Gather node, maximum number of parallel workers "
            "that can be started by a single Gather"
        )
    for m in _ROWS.finditer(text):
        est, actual = int(m.group(1)), int(m.group(3))
        big, small = max(est, actual), max(min(est, actual), 1)
        if big >= MISESTIMATE_MIN_ROWS and big / small >= MISESTIMATE_FACTOR:
            queries.append(
                "planner row count estimates far from actual rows, column statistics "
                "collected by ANALYZE, most common values and histogram"
            )
            break
    if plans:
        nodes = _node_types(plans[0])[:6]
        queries.append("slow query execution plan " + " ".join(nodes))
    queries.append("PostgreSQL server configuration performance tuning slow queries")
    return queries


def merge(results: list[tuple[str, list]], k: int) -> list[Retrieved]:
    """Round-robin merge of per-query hit lists, de-duplicated by chunk id."""
    out: dict[str, Retrieved] = {}
    depth = max((len(hits) for _, hits in results), default=0)
    for i in range(depth):
        for query, hits in results:
            if i < len(hits) and hits[i].chunk_id not in out and len(out) < k:
                h = hits[i]
                out[h.chunk_id] = Retrieved(
                    h.chunk_id, h.source, h.title, h.url, h.text[:MAX_CHARS], query, i + 1
                )
    return list(out.values())


def retrieve(
    symptoms: dict, k: int = DEFAULT_K, index: HybridIndex | None = None
) -> list[Retrieved]:
    """Top ``k`` chunks for a symptom document (see the module docstring)."""
    index = index or get_index()
    results = [(q, index.search(q, k=PER_QUERY)) for q in build_queries(symptoms)]
    return merge(results, k)
