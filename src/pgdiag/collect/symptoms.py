"""Collect the symptoms of the running test database into one JSON document (T7).

Protocol: reset statistics -> one workload run (1 warm-up + 1 measured pass) ->
read statistics, logs written during the run, and EXPLAIN (ANALYZE, BUFFERS) of
the slowest queries. The document holds raw current values only: no defaults,
no setting sources, no case id (see ``pgdiag.collect.leaks``).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg

from pgdiag import db
from pgdiag.config import Config, get_config
from pgdiag.faults.inject import PgServer
from pgdiag.workload import bench
from pgdiag.workload.queryset import load_queries, render_offline

SCHEMA_VERSION = 1
PRESET = bench.Preset("collect", 1, 1, "symptom collection: 1 warm-up + 1 measured pass")
EXPLAIN_TIMEOUT_MS = 60_000

# One fixed list, same order for every document (includes all TARGET_SETTINGS; a test checks).
# Settings derived from another one (e.g. wal_buffers = shared_buffers/32) are left out so that
# a case document differs from the baseline only in the broken setting.
CONFIG_PARAMS = (
    "shared_buffers", "effective_cache_size", "work_mem", "hash_mem_multiplier",
    "maintenance_work_mem", "temp_buffers", "max_connections", "huge_pages",
    "random_page_cost", "seq_page_cost", "cpu_tuple_cost", "cpu_index_tuple_cost",
    "cpu_operator_cost", "effective_io_concurrency", "max_worker_processes",
    "max_parallel_workers", "max_parallel_workers_per_gather", "max_parallel_maintenance_workers",
    "parallel_setup_cost", "parallel_tuple_cost", "min_parallel_table_scan_size",
    "jit", "jit_above_cost", "jit_inline_above_cost", "jit_optimize_above_cost",
    "default_statistics_target", "enable_seqscan", "enable_indexscan", "enable_bitmapscan",
    "enable_hashjoin", "enable_nestloop", "enable_hashagg", "enable_sort",
    "join_collapse_limit", "from_collapse_limit", "max_wal_size", "min_wal_size",
    "checkpoint_timeout", "checkpoint_completion_target", "synchronous_commit",
    "autovacuum", "autovacuum_naptime", "autovacuum_vacuum_scale_factor",
    "autovacuum_analyze_scale_factor", "track_io_timing",
)  # fmt: skip

# Log lines that would reveal a configuration change or restart: always dropped.
DROP_LOG_RE = re.compile(
    r"SIGHUP|changed to|cannot be changed|ALTER SYSTEM|shut ?down|starting PostgreSQL|"
    r"listening on|IPv6|ready to accept|database system|logical replication launcher|"
    r"aborting any active|terminated by signal|redo |configuration file",
    re.IGNORECASE,
)
ADMIN_APPS = ("pgdiag-admin",)


def strip_sql(text: str, limit: int = 300) -> str:
    """Remove SQL comments, collapse whitespace, truncate."""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    text = re.sub(r"--[^\n]*", " ", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _rows(conn: psycopg.Connection, query: str, params=None) -> list[dict]:
    cur = conn.execute(query, params)
    cols = [c.name for c in cur.description]
    out = []
    for row in cur.fetchall():
        d = {}
        for k, v in zip(cols, row, strict=True):
            if isinstance(v, float | Decimal):
                v = round(float(v), 2)
                v = int(v) if v.is_integer() else v
            d[k] = v
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# sections


def environment(conn: psycopg.Connection, cfg: Config) -> dict:
    info = db.container_info(cfg)
    sizes = _rows(
        conn,
        "SELECT relname AS table, pg_size_pretty(pg_total_relation_size(oid)) AS total_size "
        "FROM pg_class WHERE relkind = 'r' AND relnamespace = 'public'::regnamespace "
        "ORDER BY pg_total_relation_size(oid) DESC",
    )
    db_size = conn.execute("SELECT pg_size_pretty(pg_database_size(current_database()))")
    return {
        "server_version": db.server_version(conn),
        "memory_limit_mb": (info.get("mem_limit_bytes") or 0) // 2**20 or None,
        "cpu_count": info.get("cpus") or info.get("host_cpus"),
        "database_size": db_size.fetchone()[0],
        "tables": sizes,
    }


def config(conn: psycopg.Connection) -> list[dict]:
    rows = dict(
        conn.execute(
            "SELECT name, current_setting(name) FROM pg_settings WHERE name = ANY(%s)",
            (list(CONFIG_PARAMS),),
        ).fetchall()
    )
    return [{"name": n, "value": rows[n]} for n in CONFIG_PARAMS]


def top_statements(conn: psycopg.Connection, limit: int = 10) -> list[dict]:
    rows = _rows(
        conn,
        """SELECT query, calls, total_exec_time AS total_ms, mean_exec_time AS mean_ms, rows,
                  shared_blks_hit, shared_blks_read, temp_blks_read, temp_blks_written,
                  blk_read_time AS read_ms, jit_functions,
                  jit_generation_time + jit_inlining_time + jit_optimization_time
                    + jit_emission_time AS jit_ms
           FROM pg_stat_statements
           WHERE toplevel AND query ~* '^\\s*(select|update|insert|delete|with)'
             AND query !~* '(pg_|wl_meta|current_setting)'
           ORDER BY total_exec_time DESC LIMIT %s""",
        (limit,),
    )
    for r in rows:
        r["query"] = strip_sql(r["query"])
    return rows


def database_stats(conn: psycopg.Connection) -> dict:
    row = _rows(
        conn,
        """SELECT xact_commit, xact_rollback, blks_read, blks_hit,
                  round(100.0 * blks_hit / nullif(blks_hit + blks_read, 0), 2) AS hit_pct,
                  tup_returned, tup_fetched, tup_inserted, tup_updated, tup_deleted,
                  temp_files, temp_bytes, deadlocks, blk_read_time, blk_write_time
           FROM pg_stat_database WHERE datname = current_database()""",
    )
    return row[0]


def io_stats(conn: psycopg.Connection) -> list[dict]:
    return _rows(
        conn,
        """SELECT backend_type, object, context, reads, read_time, writes, write_time,
                  extends, hits, evictions, reuses, fsyncs
           FROM pg_stat_io
           WHERE coalesce(reads, 0) + coalesce(writes, 0) + coalesce(extends, 0)
                 + coalesce(hits, 0) + coalesce(evictions, 0) > 0
           ORDER BY backend_type, object, context""",
    )


def bgwriter_checkpoint_stats(conn: psycopg.Connection) -> dict:
    return _rows(conn, "SELECT * FROM pg_stat_bgwriter")[0]


def wal_stats(conn: psycopg.Connection) -> dict:
    return _rows(conn, "SELECT * FROM pg_stat_wal")[0]


def table_stats(conn: psycopg.Connection) -> list[dict]:
    return _rows(
        conn,
        """SELECT t.relname AS table, seq_scan, seq_tup_read, idx_scan, idx_tup_fetch,
                  n_tup_ins, n_tup_upd, n_tup_hot_upd, n_tup_del, n_live_tup, n_dead_tup,
                  vacuum_count, autovacuum_count, analyze_count, autoanalyze_count,
                  heap_blks_read, heap_blks_hit, idx_blks_read, idx_blks_hit
           FROM pg_stat_user_tables t JOIN pg_statio_user_tables s USING (relid)
           ORDER BY t.relname""",
    )


def _drop_stats_reset(d: dict) -> dict:
    return {k: v for k, v in d.items() if k != "stats_reset"}


def explain(conn: psycopg.Connection, query_ids: list[str], limit: int = 5000) -> list[dict]:
    """EXPLAIN (ANALYZE, BUFFERS) in text format for the given workload queries."""
    profile, _ = bench.read_profile(conn)
    queries = {q.id: q for q in load_queries()}
    conn.execute(f"SET statement_timeout = {EXPLAIN_TIMEOUT_MS}")
    out = []
    for qid in query_ids:
        sql = strip_sql(render_offline(queries[qid], profile, 1), limit=100_000)
        try:
            rows = conn.execute("EXPLAIN (ANALYZE, BUFFERS) " + sql).fetchall()
            plan = "\n".join(r[0] for r in rows)
        except psycopg.Error as exc:
            plan = f"ERROR: {str(exc).splitlines()[0]}"
        if len(plan) > limit:
            plan = plan[: limit - 15] + "\n... truncated"
        out.append({"query": strip_sql(sql), "plan": plan})
    conn.execute("RESET statement_timeout")
    return out


# ---------------------------------------------------------------------------
# logs


def _parse_ts(text: str) -> datetime:
    return datetime.strptime(text.replace(" UTC", ""), "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=UTC)


def read_log_records(logs_dir: Path, start: datetime, end: datetime) -> list[dict]:
    """jsonlog records with start <= timestamp <= end (one file per UTC day)."""
    out = []
    day = start.date()
    while day <= end.date():
        path = logs_dir / f"postgresql-{day.isoformat()}.json"
        if path.exists():
            for line in path.read_text(errors="replace").splitlines():
                try:
                    rec = json.loads(line)
                    if start <= _parse_ts(rec["timestamp"]) <= end:
                        out.append(rec)
                except (ValueError, KeyError):
                    continue
        day += timedelta(days=1)
    return out


def _norm(stmt: str) -> str:
    return re.sub(r"\b\d+\b", "N", strip_sql(stmt, 160))


def summarize_logs(records: list[dict], cap: int = 8) -> dict:
    """Aggregate slow statements, temp files, checkpoints, autovacuum and errors."""
    slow: dict[str, list[float]] = {}
    temp_count: Counter = Counter()
    temp_bytes = 0
    checkpoints: Counter = Counter()
    autovac: Counter = Counter()
    errors: Counter = Counter()
    for rec in records:
        msg = rec.get("message", "")
        if rec.get("application_name") in ADMIN_APPS or DROP_LOG_RE.search(msg):
            continue
        if m := re.match(r"duration: ([\d.]+) ms\s+(?:statement|execute [^:]*): (.*)", msg, re.S):
            slow.setdefault(_norm(m.group(2)), []).append(float(m.group(1)))
        elif m := re.match(r"temporary file: .*size (\d+)", msg):
            temp_count[_norm(rec.get("statement", "?"))] += 1
            temp_bytes += int(m.group(1))
        elif m := re.match(r"checkpoint starting: (.*)", msg):
            checkpoints[m.group(1).strip()] += 1
        elif m := re.match(r"automatic (vacuum|analyze) of table \"([^\"]+)\"", msg):
            autovac[f"{m.group(1)} {m.group(2)}"] += 1
        elif rec.get("error_severity") in ("WARNING", "ERROR", "FATAL", "PANIC"):
            errors[f"{rec['error_severity']}: {strip_sql(msg, 200)}"] += 1
    slow_list = sorted(slow.items(), key=lambda kv: -sum(kv[1]))[:cap]
    return {
        "slow_statements": [
            {"statement": s, "count": len(d), "max_ms": round(max(d)), "total_ms": round(sum(d))}
            for s, d in slow_list
        ],
        "temp_files": {
            "count": sum(temp_count.values()),
            "total_bytes": temp_bytes,
            "by_statement": [{"statement": s, "count": n} for s, n in temp_count.most_common(3)],
        },
        "checkpoints": dict(checkpoints),
        "autovacuum": dict(autovac.most_common(cap)),
        "errors": [{"message": m, "count": n} for m, n in errors.most_common(cap)],
    }


# ---------------------------------------------------------------------------
# protocol


def reset_stats(conn: psycopg.Connection) -> None:
    conn.execute("SELECT pg_stat_reset()")
    for target in ("bgwriter", "io", "wal"):
        conn.execute("SELECT pg_stat_reset_shared(%s)", (target,))
    conn.execute("SELECT pg_stat_statements_reset()")


def collect(cfg: Config | None = None, progress=None) -> dict:
    """Run the protocol against whatever configuration the server runs now."""
    cfg = cfg or get_config()
    srv = PgServer(cfg)  # quiet session: not tracked, not logged
    start = datetime.now(UTC) - timedelta(seconds=1)
    with srv.connect() as conn:
        reset_stats(conn)
    res = bench.run_benchmark(PRESET, "collect", save=False, cfg=cfg, progress=progress)
    measured = res.measured()[0]
    end = datetime.now(UTC) + timedelta(seconds=1)
    records = read_log_records(cfg.pg_logs_dir, start, end)
    with srv.connect() as conn:
        conn.execute("SELECT pg_stat_clear_snapshot()")
        doc = {
            "schema_version": SCHEMA_VERSION,
            "environment": environment(conn, cfg),
            "config": config(conn),
            "workload_timing": {
                "passes_measured": 1,
                "total_s": round(measured.total_s, 3),
                "components_s": {k: round(v, 3) for k, v in measured.components.items()},
                "pgbench_tps": {k: v.get("tps") for k, v in measured.pgbench.items()},
                "errors": {k: strip_sql(v, 200) for k, v in measured.errors.items()},
            },
            "top_statements": top_statements(conn),
            "database_stats": database_stats(conn),
            "io_stats": io_stats(conn),
            "bgwriter_checkpoint_stats": _drop_stats_reset(bgwriter_checkpoint_stats(conn)),
            "wal_stats": _drop_stats_reset(wal_stats(conn)),
            "table_stats": table_stats(conn),
            "log_summary": summarize_logs(records),
        }
        slowest = sorted(measured.queries, key=lambda q: -measured.queries[q])[:3]
        doc["explain"] = explain(conn, slowest)
    return doc
