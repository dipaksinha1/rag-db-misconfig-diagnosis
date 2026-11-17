"""``pgdiag-collect``: collect symptom documents (T7).

    pgdiag-collect run [--out FILE]   collect for the configuration running now
    pgdiag-collect case <case>        apply the case, collect, reset
    pgdiag-collect all [--force]      baseline + all six cases (skips existing files)

Documents go to ``results/symptoms/<case_id>.json`` and ``results/symptoms/baseline.json``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pgdiag.collect import leaks, symptoms
from pgdiag.config import get_config
from pgdiag.faults import inject
from pgdiag.faults.cases import get_case, load_cases


def out_dir() -> Path:
    return get_config().results_dir / "symptoms"


def save(doc: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1) + "\n")


def summary(name: str, path: Path) -> str:
    doc = json.loads(path.read_text())
    problems = leaks.check_text(doc)
    db = doc["database_stats"]
    top = doc["top_statements"][0] if doc["top_statements"] else {}
    return (
        f"{name:<40} {path.stat().st_size / 1024:5.1f} KB  "
        f"workload {doc['workload_timing']['total_s']:.2f}s  hit {db['hit_pct']}%  "
        f"temp_files {db['temp_files']}  top stmt {top.get('total_ms', 0):.0f} ms  "
        f"leak check {'OK' if not problems else 'FAIL: ' + '; '.join(problems[:3])}"
    )


def collect_to(path: Path) -> None:
    save(symptoms.collect(), path)


def run_case(case_id: str, path: Path) -> None:
    inject.apply_case(case_id)
    try:
        collect_to(path)
    finally:
        inject.reset()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pgdiag-collect", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run", help="collect for the configuration running now")
    p_run.add_argument("--out", type=Path, default=None)
    p_case = sub.add_parser("case", help="apply a case, collect, reset")
    p_case.add_argument("case")
    p_all = sub.add_parser("all", help="baseline + every case (resumable)")
    p_all.add_argument("--force", action="store_true", help="recollect existing files")
    args = parser.parse_args(argv)

    if args.command == "run":
        path = args.out or out_dir() / "current.json"
        collect_to(path)
        print(summary(path.stem, path))
        return 0
    if args.command == "case":
        case = get_case(args.case)
        path = out_dir() / f"{case.case_id}.json"
        run_case(case.case_id, path)
        print(summary(case.case_id, path))
        return 0

    names = ["baseline", *(c.case_id for c in load_cases())]
    for name in names:
        path = out_dir() / f"{name}.json"
        if path.exists() and not args.force:
            print(f"{name}: exists, skipped")
        elif name == "baseline":
            inject.reset()  # same restart + ANALYZE trace as a case
            collect_to(path)
        else:
            run_case(name, path)
        print(summary(name, path))
    docs = {n: json.loads((out_dir() / f"{n}.json").read_text()) for n in names}
    problems = leaks.check_set({n: d for n, d in docs.items() if n != "baseline"}, docs["baseline"])
    print("cross-document check:", "OK" if not problems else "FAIL")
    for p in problems:
        print("  " + p)
    verify = inject.verify()
    print("server:", "baseline OK" if not verify else verify)
    return 1 if problems or verify else 0


if __name__ == "__main__":
    sys.exit(main())
