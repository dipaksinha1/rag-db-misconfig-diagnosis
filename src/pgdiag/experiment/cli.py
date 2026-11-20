"""``pgdiag-experiment``: run the experiment (T11), resumable.

    pgdiag-experiment run [--name main] [--repeats 5] [--cases c01,c02] [--modes rag,norag]
                          [--preset quick] [--limit N]
    pgdiag-experiment status [--name main]

Output in ``results/experiment/<name>/``: ``manifest.json``, ``results.jsonl`` (one row per
case x mode x repeat), ``fixcache.json`` and ``runs/<case_id>/r<k>/`` (symptoms, diagnosis per
mode, fix test). Re-running ``run`` continues where it stopped. The server ends at the baseline.
"""

from __future__ import annotations

import argparse
import sys

from pgdiag.config import get_config
from pgdiag.experiment.core import (
    DEFAULT_NAME,
    DEFAULT_PRESET,
    DEFAULT_REPEATS,
    MODES,
    Experiment,
    ExperimentError,
    progress_table,
    resolve_cases,
)


def _stamp(msg: str) -> None:
    import datetime as dt

    print(f"{dt.datetime.now(dt.UTC):%H:%M:%S} {msg}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pgdiag-experiment", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run", help="run pending units (resumable)")
    p_run.add_argument("--name", default=DEFAULT_NAME, help="experiment name (output folder)")
    p_run.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    p_run.add_argument("--cases", help="comma-separated case ids/numbers (default: all six)")
    p_run.add_argument("--modes", default=",".join(MODES), help="rag,norag (default both)")
    p_run.add_argument("--preset", default=DEFAULT_PRESET, help="fix-test benchmark preset")
    p_run.add_argument("--limit", type=int, help="run at most N units, then stop")
    p_status = sub.add_parser("status", help="progress and per-case scores so far")
    p_status.add_argument("--name", default=DEFAULT_NAME)
    args = parser.parse_args(argv)

    out_dir = get_config().results_dir / "experiment" / args.name
    if args.command == "status":
        print(progress_table(out_dir))
        return 0

    exp = Experiment(
        out_dir,
        cases=resolve_cases(args.cases),
        repeats=args.repeats,
        modes=[m.strip() for m in args.modes.split(",") if m.strip()],
        preset=args.preset,
        progress=_stamp,
    )
    try:
        summary = exp.run(limit=args.limit)
    except ExperimentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(progress_table(out_dir))
    if summary["incomplete"]:
        print("incomplete (re-run to retry): " + ", ".join(summary["incomplete"]))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
