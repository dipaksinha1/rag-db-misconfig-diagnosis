"""``pgdiag-workload``: test-database data, query set and benchmark (T5).

pgdiag-workload generate [--reset] [--profile standard|tiny] [--dbname X]
pgdiag-workload bench [--preset full|quick] [--label X] [--restart]
pgdiag-workload baseline [--preset full|quick]     reset config, restart, benchmark, store
pgdiag-workload baseline-check [--preset full|quick] [--max-diff 10]
pgdiag-workload compare A.json B.json [--queries]
pgdiag-workload probe-sensitivity [--settings a,b] [--set name=value]
pgdiag-workload queries [--sql]                    list the query set
pgdiag-workload status                             data sizes and stored baselines
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
from pathlib import Path

import psycopg

from pgdiag.config import Config, get_config
from pgdiag.settings import CASE_SETTINGS
from pgdiag.workload import bench, datagen, probe, server
from pgdiag.workload.profiles import DEFAULT_PROFILE, PROFILES, get_profile
from pgdiag.workload.queryset import (
    COMPONENTS,
    QUERY_SET_VERSION,
    component_settings,
    coverage,
    queries_by_component,
    render_offline,
)

BASELINE_MAX_DIFF_PCT = 10.0


def baseline_path(preset: str, cfg: Config | None = None, *, previous: bool = False) -> Path:
    """``results/baseline.json`` for ``full``, ``results/baseline-<preset>.json`` otherwise."""
    cfg = cfg or get_config()
    stem = "baseline" if preset == "full" else f"baseline-{preset}"
    return cfg.results_dir / f"{stem}{'.prev' if previous else ''}.json"


def load_baseline(preset: str = "full", cfg: Config | None = None) -> bench.BenchResult:
    """The stored reference baseline for ``preset`` (T6/T10/T12 read it through this)."""
    path = baseline_path(preset, cfg)
    if not path.exists():
        raise FileNotFoundError(
            f"{path} does not exist; run `pgdiag-workload baseline --preset {preset}`"
        )
    return bench.load_result(path)


def _say(msg: str) -> None:
    print(msg, flush=True)


def _human(n: int) -> str:
    for unit in ("B", "kB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def cmd_generate(args: argparse.Namespace) -> int:
    profile = get_profile(args.profile)
    t0 = time.monotonic()
    report = datagen.generate(
        profile, dbname=args.dbname, seed=args.seed, reset=args.reset, progress=_say
    )
    print(f"database {report.dbname}, profile {report.profile}, seed {report.seed}")
    for name, size in report.sizes.items():
        print(f"  {name:<22} {_human(size):>10}")
    print(f"  {'TOTAL':<22} {_human(report.total_bytes):>10}  ({time.monotonic() - t0:.0f}s)")
    return 0


def _components(arg: str | None) -> list[str] | None:
    return [c.strip() for c in arg.split(",") if c.strip()] if arg else None


def cmd_bench(args: argparse.Namespace) -> int:
    res = bench.run_benchmark(
        args.preset,
        args.label,
        dbname=args.dbname,
        restart=args.restart,
        components=_components(args.components),
        statement_timeout_s=args.statement_timeout,
        out_path=args.out,
        progress=_say,
    )
    print(bench.format_summary(res))
    return 0 if res.ok else 1


def cmd_baseline(args: argparse.Namespace) -> int:
    cfg = get_config()
    target = baseline_path(args.preset, cfg)
    _say("resetting the server to the baseline configuration (ALTER SYSTEM RESET ALL + restart)")
    server.reset_to_baseline(cfg, progress=_say)
    res = bench.run_benchmark(args.preset, f"baseline-{args.preset}", progress=_say, cfg=cfg)
    print(bench.format_summary(res))
    if not res.ok:
        print("baseline run had errors; not storing it", file=sys.stderr)
        return 1
    previous = baseline_path(args.preset, cfg, previous=True)
    if target.exists():
        shutil.copyfile(target, previous)
    shutil.copyfile(res.path, target)
    res.path = str(target)
    print(f"stored reference baseline: {target}")
    if previous.exists():
        print("\ncomparison with the previous baseline:")
        diff = bench.compare(bench.load_result(previous), bench.load_result(target))
        print(bench.format_compare(diff))
    return 0


def cmd_baseline_check(args: argparse.Namespace) -> int:
    cfg = get_config()
    cur, prev = baseline_path(args.preset, cfg), baseline_path(args.preset, cfg, previous=True)
    if not (cur.exists() and prev.exists()):
        print(
            f"need two baseline runs ({prev.name} and {cur.name}); run "
            f"`pgdiag-workload baseline --preset {args.preset}` twice",
            file=sys.stderr,
        )
        return 2
    diff = bench.compare(bench.load_result(prev), bench.load_result(cur))
    print(bench.format_compare(diff))
    d = abs(diff["total"]["rel_diff_pct"])
    verdict = "PASS" if d < args.max_diff else "FAIL"
    print(
        f"\n{verdict}: the two baseline median totals differ by {d:.2f}% (limit {args.max_diff}%)"
    )
    return 0 if verdict == "PASS" else 1


def cmd_compare(args: argparse.Namespace) -> int:
    diff = bench.compare(bench.load_result(args.a), bench.load_result(args.b))
    if args.json:
        print(json.dumps(diff, indent=2))
    else:
        print(bench.format_compare(diff, queries=args.queries))
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    settings = [s.strip() for s in args.settings.split(",")] if args.settings else None
    if settings:
        unknown = [s for s in settings if s not in CASE_SETTINGS]
        if unknown:
            print(f"unknown settings: {unknown}", file=sys.stderr)
            return 2
    report = probe.run_probe(
        preset=args.preset,
        settings=settings,
        overrides=probe.parse_overrides(args.set or []),
        resume=args.resume,
        paired=not args.single_baseline,
        progress=_say,
    )
    print(probe.format_table(report))
    print(f"saved: {report['path']}")
    return 0


def cmd_queries(args: argparse.Namespace) -> int:
    profile = PROFILES[DEFAULT_PROFILE]
    grouped = queries_by_component()
    comp_settings = component_settings()
    print(f"query set v{QUERY_SET_VERSION} (pass order)")
    for comp in COMPONENTS:
        print(f"\n[{comp.id}] {comp.description}")
        print(f"  sensitive to: {', '.join(comp_settings[comp.id])}")
        if comp.kind == "pgbench":
            print(
                f"  pgbench -n -c {profile.pgbench_clients} -t {profile.pgbench_transactions} "
                f"(built-in TPC-B-like script, scale {profile.pgbench_scale})"
            )
        for q in grouped[comp.id]:
            rep = f" x{q.repeat}" if q.repeat > 1 else ""
            print(f"  - {q.id}{rep}: {q.description} [{', '.join(q.sensitive_to)}]")
            if args.sql:
                print("    " + render_offline(q, profile, 1).replace("\n", "\n    "))
    print("\ncoverage:")
    for name, ids in coverage().items():
        print(f"  {name:<32} {', '.join(ids)}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = get_config()
    try:
        conn = datagen.connect_db(args.dbname, cfg)
    except psycopg.OperationalError as exc:
        print(f"error: cannot connect: {exc}", file=sys.stderr)
        return 1
    with conn:
        meta = datagen.read_meta(conn)
        sizes = datagen.relation_sizes(conn)
        mem = conn.execute("SELECT pg_database_size(current_database())").fetchone()[0]
    if not meta:
        print("no workload data generated (run `pgdiag-workload generate`)")
    else:
        prof = meta.get("profile", {})
        print(
            f"profile {prof.get('name')}, seed {meta.get('seed')}, "
            f"generated {meta.get('generated_at')}"
        )
        print(f"complete: {', '.join(meta.get('complete', []))}")
    for name, size in sizes.items():
        print(f"  {name:<22} {_human(size):>10}")
    print(f"  {'workload tables':<22} {_human(sum(sizes.values())):>10}")
    print(f"  {'database total':<22} {_human(mem):>10}")
    for preset in ("full", "quick"):
        path = baseline_path(preset, cfg)
        if path.exists():
            res = bench.load_result(path)
            print(
                f"baseline ({preset}): {res.total_median:.2f}s median total, "
                f"{res.started_at}, {path}"
            )
        else:
            print(f"baseline ({preset}): none")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pgdiag-workload",
        description="Test-database workload: data generator, query set and benchmark runner.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate", help="generate the dataset (resumable; --reset rebuilds)")
    g.add_argument("--profile", choices=sorted(PROFILES), default=DEFAULT_PROFILE)
    g.add_argument("--dbname", help="target database (default: PGDIAG_DB_NAME); created if missing")
    g.add_argument("--seed", type=int, default=datagen.DEFAULT_SEED)
    g.add_argument("--reset", action="store_true", help="drop and re-create all workload tables")
    g.set_defaults(func=cmd_generate)

    b = sub.add_parser("bench", help="run the benchmark with the CURRENT server configuration")
    b.add_argument("--preset", choices=sorted(bench.PRESETS), default="full")
    b.add_argument("--label", default="bench")
    b.add_argument("--restart", action="store_true", help="restart the server first")
    b.add_argument("--dbname")
    b.add_argument("--components", help="comma-separated subset of components")
    b.add_argument("--statement-timeout", type=float, default=bench.DEFAULT_STATEMENT_TIMEOUT_S)
    b.add_argument("--out", type=Path, help="result file (default results/bench/...)")
    b.set_defaults(func=cmd_bench)

    bl = sub.add_parser(
        "baseline",
        help="ALTER SYSTEM RESET ALL + restart, run the benchmark, store results/baseline*.json",
    )
    bl.add_argument("--preset", choices=["full", "quick"], default="full")
    bl.set_defaults(func=cmd_baseline)

    bc = sub.add_parser("baseline-check", help="compare the last two baseline runs (acceptance)")
    bc.add_argument("--preset", choices=["full", "quick"], default="full")
    bc.add_argument("--max-diff", type=float, default=BASELINE_MAX_DIFF_PCT)
    bc.set_defaults(func=cmd_baseline_check)

    c = sub.add_parser("compare", help="compare two benchmark result files (B against A)")
    c.add_argument("a", type=Path)
    c.add_argument("b", type=Path)
    c.add_argument("--queries", action="store_true", help="also per query")
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_compare)

    p = sub.add_parser(
        "probe-sensitivity", help="break each setting in turn and measure the slowdown"
    )
    p.add_argument("--preset", choices=sorted(bench.PRESETS), default="quick")
    p.add_argument("--settings", help="comma-separated subset of the case settings")
    p.add_argument("--set", action="append", metavar="NAME=VALUE", help="override a bad value")
    p.add_argument("--resume", type=Path, help="continue a partial results/probe/*.json report")
    p.add_argument(
        "--single-baseline",
        action="store_true",
        help="one baseline for all cases instead of one right before each (faster, drift-prone)",
    )
    p.set_defaults(func=cmd_probe)

    q = sub.add_parser("queries", help="list the query set and which settings each targets")
    q.add_argument("--sql", action="store_true", help="print the SQL (standard profile)")
    q.set_defaults(func=cmd_queries)

    s = sub.add_parser("status", help="generated data sizes and stored baselines")
    s.add_argument("--dbname")
    s.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
