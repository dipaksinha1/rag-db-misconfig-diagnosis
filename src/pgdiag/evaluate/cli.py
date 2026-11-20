"""``pgdiag-evaluate``: metrics and a Markdown report for an experiment (T12).

    pgdiag-evaluate [--name main] [--out DIR] [--no-grounding] [--judge [--judge-limit N]]

Reads ``results/experiment/<name>/`` and writes ``results/evaluation/<name>/metrics.json`` and
``report.md`` (also printed). Offline (no database, no API, no index) unless ``--judge``: then
the reasoning of every run not graded yet is graded first by the judge model
(``PGDIAG_JUDGE_MODEL``, else ``ANTHROPIC_MODEL``; one API call per run, cached in
``results/evaluation/<name>/judge/``). Cached grades are always included in the report.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pgdiag.config import get_config
from pgdiag.diagnose.core import DiagnosisError
from pgdiag.evaluate.core import evaluate, load_rows, report
from pgdiag.evaluate.judge import JudgeError, run_judge


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pgdiag-evaluate", description=__doc__.splitlines()[0])
    parser.add_argument("--name", default="main", help="experiment name (T11 --name)")
    parser.add_argument("--out", type=Path, help="output folder (default results/evaluation/NAME)")
    parser.add_argument(
        "--no-grounding", action="store_true", help="skip the chunk-file lookups for RAG runs"
    )
    parser.add_argument("--judge", action="store_true", help="grade reasoning first (API calls)")
    parser.add_argument("--judge-limit", type=int, help="grade at most N runs this time")
    args = parser.parse_args(argv)
    cfg = get_config()
    exp_dir = cfg.results_dir / "experiment" / args.name
    out = args.out or cfg.results_dir / "evaluation" / args.name
    judge_dir = out / "judge"
    status = 0
    try:
        if args.judge:
            rows = load_rows(exp_dir)
            if not rows:
                raise ValueError(f"no rows in {exp_dir / 'results.jsonl'}")
            res = run_judge(exp_dir, judge_dir, rows=rows, limit=args.judge_limit, progress=print)
            print(
                f"judge: {res['graded']} graded, {res['cached']} cached, "
                f"{len(res['failed'])} failed"
            )
            status = 1 if res["failed"] else 0
        ev = evaluate(exp_dir, None if args.no_grounding else cfg.chunks_dir, judge_dir)
    except (ValueError, FileNotFoundError, JudgeError, DiagnosisError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    out.mkdir(parents=True, exist_ok=True)
    (out / "metrics.json").write_text(json.dumps(ev, indent=1) + "\n")
    text = report(ev)
    (out / "report.md").write_text(text)
    print(text)
    print(f"wrote {out / 'report.md'} and {out / 'metrics.json'}")
    return status


if __name__ == "__main__":
    sys.exit(main())
