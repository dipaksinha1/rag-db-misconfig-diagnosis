"""Retrieval checks on the real index: the T4 acceptance test and symptom-style probes.

- ``setting_name_check``: for each target setting, search its bare name and
  report where its dedicated documentation chunk lands (acceptance: top 3).
- ``SYMPTOM_PROBES`` / ``run_probes``: descriptive, symptom-style queries of
  the kind T8 will send, with the rank of the relevant setting's chunk under
  hybrid, vector-only and BM25-only search. Informative only: used to check
  the fusion settings, not an acceptance criterion, and deliberately not tuned
  query by query.
"""

from __future__ import annotations

from dataclasses import dataclass

from pgdiag.index.search import Hit, HybridIndex
from pgdiag.settings import TARGET_SETTINGS

#: (symptom-style query, the setting whose chunk should be found)
SYMPTOM_PROBES: tuple[tuple[str, str], ...] = (
    ("sorts spilling to temporary files on disk, external merge", "work_mem"),
    ("planner chooses sequential scans instead of index scans on SSD", "random_page_cost"),
    ("checkpoints occurring too frequently, checkpoint distance warnings", "max_wal_size"),
    ("queries not using parallel workers", "max_parallel_workers_per_gather"),
    ("JIT compilation time dominates short queries", "jit_above_cost"),
    ("dead tuples accumulating, tables never vacuumed", "autovacuum"),
    ("bad row estimates on skewed columns", "default_statistics_target"),
    ("CREATE INDEX is slow", "maintenance_work_mem"),
    ("low buffer cache hit ratio, many disk reads", "shared_buffers"),
    ("planner assumes little OS cache, avoids index scans", "effective_cache_size"),
)

PROBE_DEPTH = 100


def setting_chunk_id(index: HybridIndex, name: str) -> str:
    """Id of the dedicated documentation chunk of parameter ``name``."""
    for chunk_id in index.chunk_ids:
        if chunk_id.startswith("docs:") and "#GUC-" in chunk_id:
            chunk = index.get_chunk(chunk_id)
            if chunk.meta.get("chunk_type") == "setting" and chunk.meta.get("setting") == name:
                return chunk_id
    raise KeyError(f"no dedicated docs chunk for {name!r} in the index")


def rank_of(hits: list[Hit], chunk_id: str) -> int | None:
    return next((hit.rank for hit in hits if hit.chunk_id == chunk_id), None)


@dataclass
class NameResult:
    setting: str
    expected: str
    rank: int | None  # rank of the expected chunk in the top k, None if absent
    hits: list[Hit]


def setting_name_check(index: HybridIndex, k: int = 3) -> list[NameResult]:
    results = []
    for name in TARGET_SETTINGS:
        expected = setting_chunk_id(index, name)
        hits = index.search(name, k=k)
        results.append(NameResult(name, expected, rank_of(hits, expected), hits))
    return results


@dataclass
class ProbeResult:
    query: str
    setting: str
    expected: str
    hybrid: int | None  # rank of the setting's chunk within the top PROBE_DEPTH
    vector: int | None
    bm25: int | None
    first_mention: int | None  # hybrid rank of the first chunk whose settings include it


def run_probes(
    index: HybridIndex, probes: tuple[tuple[str, str], ...] = SYMPTOM_PROBES
) -> list[ProbeResult]:
    results = []
    for query, name in probes:
        expected = setting_chunk_id(index, name)
        ranks = {}
        for mode in ("hybrid", "vector", "bm25"):
            hits = index.search(query, k=PROBE_DEPTH, mode=mode)
            ranks[mode] = rank_of(hits, expected)
            if mode == "hybrid":
                mention = next((h.rank for h in hits if name in h.settings), None)
        results.append(
            ProbeResult(
                query, name, expected, ranks["hybrid"], ranks["vector"], ranks["bm25"], mention
            )
        )
    return results


def format_rank(rank: int | None) -> str:
    return str(rank) if rank is not None else f">{PROBE_DEPTH}"
