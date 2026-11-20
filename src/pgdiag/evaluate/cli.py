"""``pgdiag-evaluate``: metrics and a Markdown report for an experiment (T12).

    pgdiag-evaluate [--name main] [--out DIR] [--no-grounding]

Reads ``results/experiment/<name>/`` and writes ``results/evaluation/<name>/metrics.json`` and
``report.md`` (also printed). Offline: no database, no API, no index.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pgdiag.config import get_config
from pgdiag.evaluate.core import evaluate, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pgdiag-evaluate", description=__doc__.splitlines()[0])
    parser.add_argument("--name", default="main", help="experiment name (T11 --name)")
    parser.add_argument("--out", type=Path, help="output folder (default results/evaluation/NAME)")
    parser.add_argument(
        "--no-grounding", action="store_true", help="skip the chunk-file lookups for RAG runs"
    )
    args = parser.parse_args(argv)
    cfg = get_config()
    exp_dir = cfg.results_dir / "experiment" / args.name
    try:
        ev = evaluate(exp_dir, None if args.no_grounding else cfg.chunks_dir)
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    out = args.out or cfg.results_dir / "evaluation" / args.name
    out.mkdir(parents=True, exist_ok=True)
    (out / "metrics.json").write_text(json.dumps(ev, indent=1) + "\n")
    text = report(ev)
    (out / "report.md").write_text(text)
    print(text)
    print(f"wrote {out / 'report.md'} and {out / 'metrics.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
