"""``pgdiag-faults``: fault-injection cases for the TEST database (T6).

pgdiag-faults list [--json]              the six cases (bad/good value, restart, analyze)
pgdiag-faults validate                   check cases.yaml against the baseline file and pg_settings
pgdiag-faults apply <case>               reset, ALTER SYSTEM SET <setting> = <bad>, restart, verify
pgdiag-faults reset [--emergency]        ALTER SYSTEM RESET ALL, restart, verify baseline, ANALYZE
pgdiag-faults status [--json]            state file, ALTER SYSTEM entries, the ten running values
pgdiag-faults verify [<case>]            exit 1 unless the server runs exactly <case> (or baseline)
pgdiag-faults set <name> <value>         stack a change on top (T10: try a fix); `undo` pops it
pgdiag-faults undo [--all]
pgdiag-faults measure [--cases a,b] [--preset full|quick] [--fresh]
pgdiag-faults table [--run ID] [--write]

<case> is a case id (c02_work_mem), its number (2, c02) or the setting name.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from pgdiag.config import get_config
from pgdiag.faults import inject, measure
from pgdiag.faults.cases import (
    get_case,
    load_cases,
    setting_infos,
    validate_against_server,
    validate_cases,
)


def _say(msg: str) -> None:
    print(msg, flush=True)


def cmd_list(args: argparse.Namespace) -> int:
    cases = load_cases()
    if args.json:
        print(json.dumps([c.to_dict() for c in cases], indent=2))
        return 0
    print(f"{'case':<38} {'bad':>6} {'good':>7}  restart analyze  primary component")
    for c in cases:
        print(
            f"{c.case_id:<38} {c.bad_value:>6} {c.good_value:>7}  "
            f"{'yes' if c.restart_required else 'no':<7} {'yes' if c.needs_analyze else 'no':<7}"
            f"  {c.primary_component}"
        )
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    problems = validate_cases()
    srv = inject.PgServer()
    with srv.connect() as conn:
        infos = setting_infos(conn, [c.setting for c in load_cases()])
    problems += validate_against_server(infos)
    for p in problems:
        print(f"PROBLEM: {p}")
    if not problems:
        print("cases.yaml OK: good values equal the baseline file; bad/good values parse and lie")
        print("within pg_settings min/max; restart_required matches pg_settings.context")
    return 1 if problems else 0


def cmd_apply(args: argparse.Namespace) -> int:
    case = get_case(args.case)
    out = inject.apply_case(case, activation=args.activation, progress=_say)
    problems = inject.verify(case)
    for p in problems:
        print(f"PROBLEM: {p}", file=sys.stderr)
    print(f"applied {case.case_id} in {out.seconds:.0f}s" + (" (NOT verified)" if problems else ""))
    return 1 if problems else 0


def cmd_reset(args: argparse.Namespace) -> int:
    if args.emergency:
        inject.emergency_reset(progress=_say)
    out = inject.reset(progress=_say)
    print(f"reset to baseline in {out.seconds:.0f}s (was: {out.case_id or 'no case recorded'})")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    st = inject.status()
    if args.json:
        print(json.dumps(st, indent=2))
        return 0
    case = st["state"].get("case")
    print(f"state file: {st['state_file']}")
    if case:
        print(
            f"applied case: {case['case_id']} ({case['setting']} = {case['bad_value']}), "
            f"status {case.get('status')}, at {case.get('applied_at')}"
        )
    else:
        print("applied case: none (baseline)")
    for o in st["state"].get("overrides", []):
        print(
            f"  stacked change: {o['name']} = {o['value']} ({o['method']}; "
            f"previous ALTER SYSTEM value: {o['previous']})"
        )
    for r in st["state"].get("rejected", []):
        print(f"  rejected proposal: {r['name']} = {r['value']}: {r['error']}")
    print(f"ALTER SYSTEM (postgresql.auto.conf): {st['auto_conf'] or 'nothing'}")
    for row in st["settings"]:
        unit = f" {row['unit']}" if row["unit"] else ""
        mark = "" if row["at_baseline"] else "   <- differs from baseline"
        print(f"  {row['name']:<34} {row['running'] + unit:<14} baseline {row['baseline']}{mark}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    case = get_case(args.case) if args.case else None
    problems = inject.verify(case)
    for p in problems:
        print(f"PROBLEM: {p}")
    if problems:
        return 1
    state_case = inject.load_state(inject.default_state_path()).get("case")
    what = case.case_id if case else (state_case["case_id"] if state_case else "baseline")
    print(f"OK: the server runs {what}")
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    attempt = inject.try_fix(args.name, args.value)
    if not attempt.applied:
        print(f"rejected ({attempt.error_kind}): {attempt.error}", file=sys.stderr)
        return 2
    c = attempt.change
    print(
        f"{c['name']} = {c['value']} ({c['method']}); previous ALTER SYSTEM value: "
        f"{c['previous']}; was running {c['before_running']}, now {c['running']}"
    )
    return 0


def cmd_undo(args: argparse.Namespace) -> int:
    changes = inject.undo_all() if args.all else [c for c in [inject.undo()] if c]
    if not changes:
        print("nothing to undo")
    for c in changes:
        back = c.previous if c.previous is not None else "(ALTER SYSTEM entry removed)"
        print(f"undid {c.name} = {c.value}: back to {back}, running {c.before_running}")
    return 0


def cmd_measure(args: argparse.Namespace) -> int:
    keys = [k.strip() for k in args.cases.split(",")] if args.cases else None
    run = measure.run_measure(
        measure.resolve_cases(keys),
        preset=args.preset,
        fresh=args.fresh,
        max_baseline_age_s=args.max_baseline_age * 60,
        progress=_say,
    )
    print(measure.format_table(run))
    print(f"records: {measure.jsonl_path()}")
    print(f"table: {measure.table_path()}  summary: {measure.summary_path()}")
    return 0 if all(r["reset_ok"] for r in run["rows"]) else 1


def cmd_table(args: argparse.Namespace) -> int:
    run = measure.load_run(args.run)
    print(measure.format_table(run))
    if args.write:
        for kind, path in measure.write_outputs(run).items():
            print(f"wrote {kind}: {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pgdiag-faults", description="Fault injection for the test database (T6)."
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="list the cases")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("validate", help="check cases.yaml (baseline file + pg_settings)")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("apply", help="break the server with one case (resets first)")
    p.add_argument("case")
    p.add_argument(
        "--activation",
        choices=["restart", "as_required"],
        default="restart",
        help="restart for every case (default: same log trace for all) or reload where possible",
    )
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("reset", help="restore and verify the baseline configuration")
    p.add_argument(
        "--emergency",
        action="store_true",
        help="the server does not start: empty postgresql.auto.conf with a one-off container first",
    )
    p.set_defaults(func=cmd_reset)

    p = sub.add_parser("status", help="applied case and the running target settings")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("verify", help="check the server runs <case> (default: the recorded state)")
    p.add_argument("case", nargs="?")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("set", help="ALTER SYSTEM SET on top of the current case (undo with undo)")
    p.add_argument("name")
    p.add_argument("value")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("undo", help="take back the last `set` (or all of them)")
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_undo)

    p = sub.add_parser("measure", help="baseline vs broken vs fixed timing per case (resumable)")
    p.add_argument("--cases", help="comma-separated case ids/numbers/settings (default: all)")
    p.add_argument("--preset", choices=["quick", "full"], default="full")
    p.add_argument("--fresh", action="store_true", help="start a new run instead of resuming")
    p.add_argument(
        "--max-baseline-age",
        type=float,
        default=measure.DEFAULT_MAX_BASELINE_AGE_S / 60,
        help="minutes a post-reset run stays usable as the next case's baseline (default 15)",
    )
    p.set_defaults(func=cmd_measure)

    p = sub.add_parser("table", help="print the table of a measure run (default: latest)")
    p.add_argument("--run", help="run id (see results/faults/measure.jsonl)")
    p.add_argument(
        "--write", action="store_true", help="also rewrite table.md / cases_measured.json"
    )
    p.set_defaults(func=cmd_table)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    get_config().ensure_dirs()
    try:
        return args.func(args)
    except KeyError as exc:
        print(f"error: {exc.args[0] if exc.args else exc}", file=sys.stderr)
        return 2
    except inject.FaultError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
