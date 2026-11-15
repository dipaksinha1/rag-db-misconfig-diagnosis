"""The canonical lists of PostgreSQL settings the project is about.

Two lists, both the only copies (do not repeat them elsewhere):

``TARGET_SETTINGS`` (10)
    The settings the knowledge base was built around. The RAG stages (T1 docs
    ingestion, T2 release notes, T4 index) tag chunks with the ones they
    mention and check that each has a dedicated docs chunk.

``CASE_SETTINGS`` (6, a subset of ``TARGET_SETTINGS``)
    The settings the experiment actually breaks, one case each (scope reduced
    by the project owner in T5b). Every experiment stage uses this list: the
    workload and its sensitivity probe, fault injection, the symptom collector,
    retrieval and diagnosis evaluation, the experiment runner and the report.
    The four dropped settings (``effective_cache_size``, ``autovacuum``,
    ``max_wal_size``, ``maintenance_work_mem``) stay at sane baseline values.

The diagnosis LLM must NEVER be shown either list (or told how many candidates
there are): it has to name the misconfigured setting on its own. The lists are
only for defining cases and scoring answers.
"""

TARGET_SETTINGS: tuple[str, ...] = (
    "shared_buffers",
    "work_mem",
    "effective_cache_size",
    "random_page_cost",
    "max_parallel_workers_per_gather",
    "autovacuum",
    "max_wal_size",
    "default_statistics_target",
    "jit_above_cost",
    "maintenance_work_mem",
)

CASE_SETTINGS: tuple[str, ...] = (
    "shared_buffers",
    "work_mem",
    "random_page_cost",
    "max_parallel_workers_per_gather",
    "jit_above_cost",
    "default_statistics_target",
)

# Target settings that are not experiment cases (kept at their baseline values).
NON_CASE_SETTINGS: tuple[str, ...] = tuple(s for s in TARGET_SETTINGS if s not in CASE_SETTINGS)
