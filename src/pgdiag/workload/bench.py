"""Benchmark runner for the TEST database workload (T5).

One *pass* runs every component of the query set once, in the fixed order of
``queryset.COMPONENTS``: SQL components execute their statements in one
connection, the ``pgbench`` component runs a fixed number of transactions
(``pgbench -n -t``) inside the container. A *benchmark* is a few warm-up passes
followed by N measured passes; its result is the MEDIAN of the measured pass
totals (and of each component).

State between runs (why passes are comparable)
----------------------------------------------
* Static tables (pgbench_accounts/branches/tellers, wl_customers, wl_products,
  wl_orders, wl_campaign_targets) are never modified by SQL components. They are
  ANALYZEd at the start of every benchmark, in the benchmark's session, so
  their statistics always reflect the current ``default_statistics_target``
  (a broken value takes effect, a restored one is undone).
* ``wl_tasks`` (work queue) is re-created, vacuumed and analyzed at the start
  of every BENCHMARK, not every pass. Each pass completes, purges, enqueues and
  claims one batch, so the live row count stays constant. With autovacuum
  running the table reaches a steady state; with ``autovacuum = off`` dead rows,
  index bloat, stale statistics and an outdated visibility map accumulate from
  pass to pass. Resetting it per pass would hide exactly that.
* Per-pass reset (untimed): ``pgbench_history`` truncated, ``wl_ledger``
  dropped and re-created empty (the batch then loads, updates, indexes and
  vacuums it), ``wl_audience`` truncated, then a CHECKPOINT so that every pass
  starts with no dirty buffers and an empty checkpoint backlog. None of this
  undoes a broken setting: frequent checkpoints, spills, plan changes and bloat
  all happen inside the timed components.
* Caches are not dropped. The warm-up pass(es) bring shared buffers and the
  container's page cache (the 1 GiB cgroup limit includes page cache) to the
  workload's steady state; ``restart=True`` restarts the server first.

The function API is what later tasks use::

    from pgdiag.workload.bench import run_benchmark, compare, load_result
    res = run_benchmark(preset="quick", label="case-work_mem")
    diff = compare(load_result("results/baseline-quick.json"), res)
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import re
import statistics
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

import psycopg
from psycopg import sql

from pgdiag import db
from pgdiag.config import Config, get_config
from pgdiag.settings import TARGET_SETTINGS
from pgdiag.workload import datagen
from pgdiag.workload.profiles import DataProfile
from pgdiag.workload.queryset import (
    COMPONENT_IDS,
    COMPONENTS,
    QUERY_SET_VERSION,
    Query,
    queries_by_component,
    render_offline,
)

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2
DEFAULT_STATEMENT_TIMEOUT_S = 300.0
APPLICATION_NAME = "pgdiag-workload"

# Non-target settings recorded with every result (for context; not changed here).
CONTEXT_SETTINGS = (
    "jit",
    "hash_mem_multiplier",
    "max_worker_processes",
    "max_parallel_workers",
    "max_parallel_maintenance_workers",
    "autovacuum_naptime",
    "autovacuum_vacuum_scale_factor",
    "autovacuum_analyze_scale_factor",
    "autovacuum_vacuum_insert_scale_factor",
    "autovacuum_vacuum_cost_limit",
    "checkpoint_timeout",
    "checkpoint_completion_target",
    "min_wal_size",
    "seq_page_cost",
    "synchronous_commit",
)


@dataclass(frozen=True)
class Preset:
    name: str
    warmup: int
    passes: int
    description: str


PRESETS: dict[str, Preset] = {
    "full": Preset("full", 1, 5, "baseline and fault cases (T5/T6): 1 warm-up + 5 measured passes"),
    "quick": Preset("quick", 1, 3, "fix testing (T10/T11): 1 warm-up + 3 measured passes"),
    "smoke": Preset("smoke", 0, 1, "tests only: a single measured pass, no warm-up"),
}


def get_preset(preset: str | Preset) -> Preset:
    if isinstance(preset, Preset):
        if preset.passes < 1 or preset.warmup < 0:
            raise ValueError("a preset needs passes >= 1 and warmup >= 0")
        return preset
    try:
        return PRESETS[preset]
    except KeyError:
        raise ValueError(f"unknown preset {preset!r}; choose from {sorted(PRESETS)}") from None


class WorkloadNotGenerated(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# statistics


def summarize(values: Sequence[float]) -> dict:
    """median / mean / min / max / stdev and relative spread of a list of timings."""
    if not values:
        raise ValueError("no values")
    vals = [float(v) for v in values]
    med = statistics.median(vals)
    stdev = statistics.stdev(vals) if len(vals) > 1 else 0.0
    return {
        "n": len(vals),
        "median": med,
        "mean": statistics.fmean(vals),
        "min": min(vals),
        "max": max(vals),
        "stdev": stdev,
        # (max - min) / median, in percent: the run-to-run spread inside a benchmark
        "spread_pct": (max(vals) - min(vals)) / med * 100 if med else 0.0,
        "cv_pct": stdev / statistics.fmean(vals) * 100 if statistics.fmean(vals) else 0.0,
    }


def rel_diff_pct(a: float, b: float) -> float:
    """(b - a) / a in percent: positive means b is slower."""
    if a == 0:
        raise ZeroDivisionError("reference value is zero")
    return (b - a) / a * 100


# ---------------------------------------------------------------------------
# result


@dataclass
class PassResult:
    index: int  # 0-based over all passes, warm-up included
    warmup: bool
    started_at: str
    total_s: float
    components: dict[str, float]  # component id -> seconds
    queries: dict[str, float]  # query id -> seconds (all repeats)
    errors: dict[str, str] = field(default_factory=dict)  # query/component id -> message
    # pgbench component id -> {elapsed_s, tps, latency_ms, transactions, failed}
    pgbench: dict = field(default_factory=dict)
    # server counters over the pass: wal_bytes, checkpoints_timed/_req, temp_files, temp_bytes
    server_stats: dict = field(default_factory=dict)


@dataclass
class BenchResult:
    label: str
    preset: dict
    started_at: str
    finished_at: str
    duration_s: float
    git: dict
    query_set_version: str
    components: list[str]
    data: dict  # dbname, profile, seed, sizes
    settings: dict[str, str]  # the 10 target settings: name -> current value
    settings_detail: list[dict]  # name, value, setting, unit, source, pending_restart
    context_settings: dict[str, str]
    session_settings: dict[str, str]  # extra SET applied to the benchmark sessions (probe only)
    server_version: str
    container: dict
    passes: list[PassResult]
    summary: dict  # {"total": summarize(...), "components": {...}, "queries": {...}}
    errors: list[str] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION
    path: str | None = None  # where it was saved (not part of the JSON)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def total_median(self) -> float:
        return self.summary["total"]["median"]

    def component_medians(self) -> dict[str, float]:
        return {k: v["median"] for k, v in self.summary["components"].items()}

    def measured(self) -> list[PassResult]:
        return [p for p in self.passes if not p.warmup]

    def to_dict(self) -> dict:
        out = asdict(self)
        out.pop("path")
        return out

    def save(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=False) + "\n")
        tmp.replace(path)
        self.path = str(path)
        return path

    @classmethod
    def from_dict(cls, data: dict) -> BenchResult:
        data = dict(data)
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"unsupported result schema_version {data.get('schema_version')!r}")
        data["passes"] = [PassResult(**p) for p in data["passes"]]
        data.pop("path", None)
        return cls(**data)


def load_result(path: str | Path) -> BenchResult:
    res = BenchResult.from_dict(json.loads(Path(path).read_text()))
    res.path = str(path)
    return res


def build_summary(passes: Sequence[PassResult], components: Sequence[str]) -> dict:
    measured = [p for p in passes if not p.warmup]
    if not measured:
        raise ValueError("no measured passes")
    query_ids = list(measured[0].queries)
    return {
        "total": summarize([p.total_s for p in measured]),
        "components": {c: summarize([p.components[c] for p in measured]) for c in components},
        "queries": {q: summarize([p.queries[q] for p in measured]) for q in query_ids},
    }


def compare(a: BenchResult, b: BenchResult) -> dict:
    """Relative difference of b against a (reference): total and per component medians.

    ``rel_diff_pct`` > 0 means b is slower; ``ratio`` is b / a.
    """

    def entry(x: float, y: float) -> dict:
        return {"a": x, "b": y, "ratio": y / x if x else None, "rel_diff_pct": rel_diff_pct(x, y)}

    comps = [c for c in a.summary["components"] if c in b.summary["components"]]
    queries = [q for q in a.summary.get("queries", {}) if q in b.summary.get("queries", {})]
    warnings = []
    if a.query_set_version != b.query_set_version:
        warnings.append(
            f"query set versions differ ({a.query_set_version} vs {b.query_set_version})"
        )
    if a.data.get("profile") != b.data.get("profile"):
        warnings.append("data profiles differ")
    if a.preset.get("name") != b.preset.get("name"):
        warnings.append(f"presets differ ({a.preset.get('name')} vs {b.preset.get('name')})")
    return {
        "a": {"label": a.label, "path": a.path, "started_at": a.started_at},
        "b": {"label": b.label, "path": b.path, "started_at": b.started_at},
        "total": entry(a.total_median, b.total_median),
        "components": {
            c: entry(a.summary["components"][c]["median"], b.summary["components"][c]["median"])
            for c in comps
        },
        "queries": {
            q: entry(a.summary["queries"][q]["median"], b.summary["queries"][q]["median"])
            for q in queries
        },
        "settings_changed": {
            k: [a.settings.get(k), b.settings.get(k)]
            for k in TARGET_SETTINGS
            if a.settings.get(k) != b.settings.get(k)
        },
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# setup and per-pass reset SQL


def tasks_setup_sql(p: DataProfile) -> list[str]:
    """Re-create the work-queue table in its steady state: (tasks - batch) done + batch running."""
    done = p.tasks - p.task_batch
    return [
        "DROP TABLE IF EXISTS wl_tasks",
        """CREATE TABLE wl_tasks (
            task_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            customer_id integer  NOT NULL,
            kind        text     NOT NULL,
            priority    smallint NOT NULL,
            status      text     NOT NULL,
            worker      text,
            created_at  timestamptz NOT NULL,
            started_at  timestamptz,
            finished_at timestamptz,
            payload     text     NOT NULL,
            result      text
        )""",
        f"""INSERT INTO wl_tasks (customer_id, kind, priority, status, worker, created_at,
                                  started_at, finished_at, payload, result)
        SELECT 1 + mod(g * 7919, {p.customers}),
               (ARRAY['email', 'export', 'invoice', 'sync'])[1 + mod(g, 4)],
               mod(g, 5),
               CASE WHEN g <= {done} THEN 'done' ELSE 'running' END,
               'w' || mod(g, 8),
               now() - interval '1 hour' + g * interval '1 millisecond',
               now() - interval '1 hour' + g * interval '1 millisecond',
               CASE WHEN g <= {done} THEN now() - interval '30 minutes' END,
               repeat('p', 300 + mod(g::int, 100)),
               CASE WHEN g <= {done} THEN 'ok by w' || mod(g, 8) END
        FROM generate_series(1::bigint, {p.tasks}) AS g""",
        "CREATE INDEX wl_tasks_status_idx ON wl_tasks (status, priority, task_id)",
        "CREATE INDEX wl_tasks_customer_idx ON wl_tasks (customer_id)",
        "VACUUM (ANALYZE) wl_tasks",
    ]


STATIC_TABLES = (
    "pgbench_accounts",
    "pgbench_branches",
    "pgbench_tellers",
    "wl_customers",
    "wl_products",
    "wl_orders",
    "wl_campaign_targets",
)

PASS_RESET_SQL = (
    "TRUNCATE pgbench_history",
    "DROP TABLE IF EXISTS wl_ledger",
    # Batch staging table: the batch vacuums it itself, so autovacuum leaves it alone.
    """CREATE TABLE wl_ledger (
        entry_id   bigint PRIMARY KEY,
        account_id integer NOT NULL,
        posted_at  timestamp NOT NULL,
        amount     numeric(12,2) NOT NULL,
        balance    numeric(14,2) NOT NULL,
        status     text NOT NULL,
        memo       text,
        tags       integer[] NOT NULL
    ) WITH (autovacuum_enabled = off)""",
    """CREATE TABLE IF NOT EXISTS wl_audience (
        campaign     smallint NOT NULL,
        customer_id  integer  NOT NULL,
        country      char(2)  NOT NULL,
        segment      text     NOT NULL,
        credit_limit numeric(12,2) NOT NULL,
        score        real     NOT NULL
    ) WITH (autovacuum_enabled = off)""",
    "TRUNCATE wl_audience",
    "CHECKPOINT",
)


# ---------------------------------------------------------------------------
# running


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def git_info(cfg: Config | None = None) -> dict:
    cfg = cfg or get_config()

    def run(*args: str) -> str | None:
        try:
            return subprocess.run(
                ["git", *args], cwd=cfg.root, check=True, capture_output=True, text=True
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None

    status = run("status", "--porcelain", "--untracked-files=no")
    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(status) if status is not None else None,
    }


def read_profile(conn: psycopg.Connection) -> tuple[DataProfile, dict]:
    """Profile the database was generated with, plus the raw wl_meta contents."""
    meta = datagen.read_meta(conn)
    if not set(datagen.GENERATED_TABLES) <= set(meta.get("complete", [])):
        raise WorkloadNotGenerated(
            "the workload data is not (completely) generated in this database; "
            "run `pgdiag-workload generate` first"
        )
    return DataProfile(**meta["profile"]), meta


def apply_session_settings(conn: psycopg.Connection, settings: Mapping[str, str]) -> None:
    for name, value in settings.items():
        conn.execute("SELECT set_config(%s, %s, false)", (name, str(value)))


def _connect(dbname: str | None, cfg: Config, settings: Mapping[str, str], timeout_s: float):
    conn = datagen.connect_db(dbname, cfg)
    conn.execute("SELECT set_config('application_name', %s, false)", (APPLICATION_NAME,))
    conn.execute("SELECT set_config('statement_timeout', %s, false)", (f"{int(timeout_s * 1000)}",))
    apply_session_settings(conn, settings)
    return conn


PGBENCH_RANDOM_SEED = 20251114

_TPS_RE = re.compile(r"tps = ([\d.]+) \(without initial connection time\)")
_LAT_RE = re.compile(r"latency average = ([\d.]+) ms")
_FAILED_RE = re.compile(r"number of failed transactions: (\d+)")


def run_pgbench(
    profile: DataProfile,
    dbname: str,
    cfg: Config,
    settings: Mapping[str, str],
    timeout_s: float,
    script: str | None = None,
) -> dict:
    """One fixed-size pgbench run inside the container. Returns tps/latency/elapsed.

    ``script`` is a custom pgbench script template (``{accounts}`` etc. filled from
    the profile), fed through stdin; None runs the built-in TPC-B-like script.
    """
    clients = profile.pgbench_clients
    stdin = None
    script_args: list[str] = []
    if script is not None:
        stdin = script.format(accounts=100_000 * profile.pgbench_scale, **profile.int_params())
        script_args = ["-f", "-"]
    env_args: list[str] = []
    if settings:
        opts = " ".join(f"-c {k}={v}" for k, v in settings.items())
        env_args = ["-e", f"PGOPTIONS={opts}"]
    cmd = [
        *db.compose_command(cfg),
        "exec",
        "-T",
        *env_args,
        "db",
        *datagen.pgbench_args(dbname, cfg),
        "-n",  # tables are vacuumed by autovacuum (or not), never by pgbench
        "-c",
        str(clients),
        "-j",
        str(min(clients, 2)),
        "-t",
        str(profile.pgbench_transactions),
        f"--random-seed={PGBENCH_RANDOM_SEED}",
        *script_args,
    ]
    start = time.perf_counter()
    proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout_s)
    elapsed = time.perf_counter() - start
    out = proc.stdout + proc.stderr
    if proc.returncode != 0:
        raise RuntimeError(f"pgbench failed ({proc.returncode}): {out.strip()[-500:]}")
    tps = _TPS_RE.search(out)
    lat = _LAT_RE.search(out)
    failed = _FAILED_RE.search(out)
    return {
        "elapsed_s": elapsed,
        "tps": float(tps.group(1)) if tps else None,
        "latency_ms": float(lat.group(1)) if lat else None,
        "transactions": clients * profile.pgbench_transactions,
        "failed": int(failed.group(1)) if failed else 0,
    }


def run_query(conn: psycopg.Connection, query: Query, profile: DataProfile) -> float:
    """Execute every repeat of ``query`` (fetching all rows). Returns elapsed seconds."""
    start = time.perf_counter()
    for i in range(1, query.repeat + 1):
        with conn.cursor() as cur:
            cur.execute(render_offline(query, profile, i))
            if cur.description is not None:
                cur.fetchall()
    return time.perf_counter() - start


def setup_benchmark(
    conn: psycopg.Connection, profile: DataProfile, say: Callable[[str], None]
) -> None:
    """Untimed per-benchmark setup: ANALYZE static tables, re-create wl_tasks, checkpoint."""
    t0 = time.monotonic()
    for table in STATIC_TABLES:
        conn.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(table)))
    for stmt in tasks_setup_sql(profile):
        conn.execute(stmt)
    conn.execute("CHECKPOINT")
    say(f"setup: analyzed static tables, re-created wl_tasks ({time.monotonic() - t0:.1f}s)")


def reset_pass(conn: psycopg.Connection) -> None:
    for stmt in PASS_RESET_SQL:
        conn.execute(stmt)


SERVER_STATS_SQL = """
SELECT pg_current_wal_lsn()::text,
       (SELECT checkpoints_timed FROM pg_stat_bgwriter),
       (SELECT checkpoints_req FROM pg_stat_bgwriter),
       (SELECT temp_files FROM pg_stat_database WHERE datname = current_database()),
       (SELECT temp_bytes FROM pg_stat_database WHERE datname = current_database())
"""


def server_counters(conn: psycopg.Connection) -> tuple:
    conn.execute("SELECT pg_stat_clear_snapshot()")
    return conn.execute(SERVER_STATS_SQL).fetchone()


def counters_delta(conn: psycopg.Connection, before: tuple, after: tuple) -> dict:
    wal = conn.execute("SELECT pg_wal_lsn_diff(%s, %s)", (after[0], before[0])).fetchone()[0]
    return {
        "wal_bytes": int(wal),
        "checkpoints_timed": after[1] - before[1],
        "checkpoints_req": after[2] - before[2],
        "temp_files": after[3] - before[3],
        "temp_bytes": after[4] - before[4],
    }


def run_pass(
    index: int,
    warmup: bool,
    profile: DataProfile,
    dbname: str,
    cfg: Config,
    settings: Mapping[str, str],
    timeout_s: float,
    components: Sequence[str],
) -> PassResult:
    grouped = queries_by_component()
    comp_times: dict[str, float] = {}
    query_times: dict[str, float] = {}
    errors: dict[str, str] = {}
    pgb: dict = {}
    started = _now()
    with _connect(dbname, cfg, settings, timeout_s) as conn:
        reset_pass(conn)
        before = server_counters(conn)
        for comp in COMPONENTS:
            if comp.id not in components:
                continue
            if comp.kind == "pgbench":
                t0 = time.perf_counter()
                try:
                    pgb[comp.id] = run_pgbench(
                        profile, dbname, cfg, settings, timeout_s, comp.pgbench_script
                    )
                except (RuntimeError, subprocess.TimeoutExpired) as exc:
                    errors[comp.id] = str(exc)
                comp_times[comp.id] = time.perf_counter() - t0
                continue
            t_comp = time.perf_counter()
            failed = False
            if comp.transaction:
                conn.execute("BEGIN")
            for q in grouped[comp.id]:
                t0 = time.perf_counter()
                if not failed:
                    try:
                        run_query(conn, q, profile)
                    except psycopg.Error as exc:
                        errors[q.id] = f"{type(exc).__name__}: {str(exc).strip()}"
                        log.warning("query %s failed: %s", q.id, exc)
                        # in a transaction block the rest would fail too; skip it
                        failed = comp.transaction
                query_times[q.id] = time.perf_counter() - t0
            if comp.transaction:
                conn.execute("ROLLBACK" if failed else "COMMIT")
            comp_times[comp.id] = time.perf_counter() - t_comp
        stats = counters_delta(conn, before, server_counters(conn))
    return PassResult(
        index=index,
        warmup=warmup,
        started_at=started,
        total_s=sum(comp_times.values()),
        components=comp_times,
        queries=query_times,
        errors=errors,
        pgbench=pgb,
        server_stats=stats,
    )


def format_pass(p: PassResult, n_total: int) -> str:
    kind = "warm-up" if p.warmup else "measured"
    parts = " ".join(f"{c}={t:.2f}" for c, t in p.components.items())
    err = f"  ERRORS: {sorted(p.errors)}" if p.errors else ""
    return f"pass {p.index + 1}/{n_total} ({kind}) total {p.total_s:.2f}s | {parts}{err}"


def default_result_path(cfg: Config, label: str, preset: str, started: str) -> Path:
    stamp = started.replace("-", "").replace(":", "")
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip("-") or "bench"
    return cfg.results_dir / "bench" / f"{stamp}_{safe}_{preset}.json"


def run_benchmark(
    preset: str | Preset = "full",
    label: str = "bench",
    *,
    dbname: str | None = None,
    restart: bool = False,
    session_settings: Mapping[str, str] | None = None,
    components: Sequence[str] | None = None,
    statement_timeout_s: float = DEFAULT_STATEMENT_TIMEOUT_S,
    save: bool = True,
    out_path: Path | None = None,
    cfg: Config | None = None,
    progress: Callable[[str], None] | None = None,
) -> BenchResult:
    """Run warm-up + measured passes of the workload and return (and save) the result.

    ``session_settings`` are applied with ``SET`` to the benchmark's SQL
    sessions and via ``PGOPTIONS`` to pgbench; they exist for the sensitivity
    probe. Fault injection (T6) changes the server instead (``ALTER SYSTEM``).
    ``components`` restricts the run to some components (default: all).
    """
    cfg = cfg or get_config()
    p = get_preset(preset)
    say = progress or (lambda msg: log.info("%s", msg))
    settings = dict(session_settings or {})
    comps = list(components) if components else list(COMPONENT_IDS)
    unknown = [c for c in comps if c not in COMPONENT_IDS]
    if unknown:
        raise ValueError(f"unknown components {unknown}")
    dbname = dbname or cfg.db_name

    if restart:
        say("restarting the database server ...")
        say(f"restarted in {db.restart_server(cfg):.1f}s")

    started_at = _now()
    t_start = time.monotonic()
    with _connect(dbname, cfg, settings, max(statement_timeout_s, 3600)) as conn:
        profile, meta = read_profile(conn)
        version = db.server_version(conn)
        detail = db.target_settings(conn)
        context = dict(
            conn.execute(
                "SELECT name, current_setting(name) FROM pg_settings WHERE name = ANY(%s)",
                (list(CONTEXT_SETTINGS),),
            ).fetchall()
        )
        setup_benchmark(conn, profile, say)

    passes: list[PassResult] = []
    n_total = p.warmup + p.passes
    for idx in range(n_total):
        pr = run_pass(
            idx, idx < p.warmup, profile, dbname, cfg, settings, statement_timeout_s, comps
        )
        passes.append(pr)
        say(format_pass(pr, n_total))

    errors = [f"pass {pr.index + 1}: {k}: {v}" for pr in passes for k, v in pr.errors.items()]
    result = BenchResult(
        label=label,
        preset=asdict(p),
        started_at=started_at,
        finished_at=_now(),
        duration_s=time.monotonic() - t_start,
        git=git_info(cfg),
        query_set_version=QUERY_SET_VERSION,
        components=comps,
        data={
            "dbname": dbname,
            "profile": profile.to_dict(),
            "seed": meta.get("seed"),
            "sizes": meta.get("sizes"),
            "generated_at": meta.get("generated_at"),
        },
        settings={row["name"]: row["value"] for row in detail},
        settings_detail=[{k: v for k, v in row.items()} for row in detail],
        context_settings=context,
        session_settings=settings,
        server_version=version,
        container=db.container_info(cfg),
        passes=passes,
        summary=build_summary(passes, comps),
        errors=errors,
    )
    if save:
        result.save(out_path or default_result_path(cfg, label, p.name, started_at))
    return result


# ---------------------------------------------------------------------------
# printing


def format_summary(res: BenchResult) -> str:
    s = res.summary
    lines = [
        f"benchmark '{res.label}' preset={res.preset['name']} "
        f"({res.preset['warmup']} warm-up + {res.preset['passes']} measured passes), "
        f"query set v{res.query_set_version}, {res.duration_s:.0f}s wall",
        "settings: " + ", ".join(f"{k}={v}" for k, v in res.settings.items()),
    ]
    if res.session_settings:
        lines.append(
            "session overrides: " + ", ".join(f"{k}={v}" for k, v in res.session_settings.items())
        )
    t = s["total"]
    lines.append(
        f"TOTAL median {t['median']:.2f}s  (min {t['min']:.2f}, max {t['max']:.2f}, "
        f"spread {t['spread_pct']:.1f}%, cv {t['cv_pct']:.1f}%)"
    )
    lines.append(f"  {'component':<18} {'median':>8} {'min':>8} {'max':>8} {'spread':>7}")
    for c, v in s["components"].items():
        lines.append(
            f"  {c:<18} {v['median']:>8.2f} {v['min']:>8.2f} {v['max']:>8.2f} "
            f"{v['spread_pct']:>6.1f}%"
        )
    if res.errors:
        lines.append(f"ERRORS ({len(res.errors)}):")
        lines.extend(f"  {e}" for e in res.errors[:20])
    if res.path:
        lines.append(f"saved: {res.path}")
    return "\n".join(lines)


def format_compare(diff: dict, *, queries: bool = False) -> str:
    a, b = diff["a"], diff["b"]
    lines = [
        f"A: {a['label']} ({a['started_at']}) {a['path'] or ''}",
        f"B: {b['label']} ({b['started_at']}) {b['path'] or ''}",
        f"  {'':<26} {'A median':>9} {'B median':>9} {'B vs A':>8}",
    ]

    def row(name: str, e: dict) -> str:
        return f"  {name:<26} {e['a']:>9.2f} {e['b']:>9.2f} {e['rel_diff_pct']:>+7.1f}%"

    lines.append(row("TOTAL", diff["total"]))
    for c, e in diff["components"].items():
        lines.append(row(c, e))
    if queries:
        lines.append("  per query:")
        for q, e in diff["queries"].items():
            lines.append(row("  " + q, e))
    if diff["settings_changed"]:
        lines.append(
            "settings changed: "
            + ", ".join(f"{k}: {v[0]} -> {v[1]}" for k, v in diff["settings_changed"].items())
        )
    for w in diff["warnings"]:
        lines.append(f"WARNING: {w}")
    return "\n".join(lines)
