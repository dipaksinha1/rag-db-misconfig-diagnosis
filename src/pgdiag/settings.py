"""The canonical list of PostgreSQL settings the experiment breaks, one per case.

Every stage imports the names from here: ingestion tags chunks with the
settings they mention, fault injection defines one case per setting, and
evaluation scores diagnoses against them. Do not copy the list elsewhere.
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
