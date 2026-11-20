"""``pgdiag-fixtest``: time each proposed fix on a broken case (T10).

    pgdiag-fixtest <case> --fixes fixes.json   # diagnose() record, or [{setting, proposed_value}]
    pgdiag-fixtest <case> --known-good         # the case's good value only

Applies the case if it is not applied yet, writes results/fixtest/<case_id>.<tag>.json and
prints a table. The server always ends at the baseline.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pgdiag.config import get_config
from pgdiag.faults import inject
from pgdiag.faults.cases import get_case
from pgdiag.fixtest.core import DEFAULT_PRESET, format_table, test_fixes


def load_fixes(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    if isinstance(data, dict):
        data = data["fixes"]  # a diagnose() record
    return [{k: f[k] for k in ("rank", "setting", "proposed_value") if k in f} for f in data]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pgdiag-fixtest", description=__doc__.splitlines()[0])
    parser.add_argument("case", help="case id, number or setting (see pgdiag-faults list)")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--fixes", type=Path, help="JSON: diagnose() output or a list of fixes")
    src.add_argument("--known-good", action="store_true", help="test the case's good value")
    parser.add_argument("--preset", default=DEFAULT_PRESET, help="benchmark preset (default quick)")
    parser.add_argument("--out", type=Path, help="output file (default results/fixtest/...)")
    args = parser.parse_args(argv)
    cfg = get_config()
    case = get_case(args.case)
    if args.known_good:
        fixes, tag = (
            [{"rank": 1, "setting": case.setting, "proposed_value": case.good_value}],
            "known-good",
        )
    else:
        fixes, tag = load_fixes(args.fixes), args.fixes.stem
    out_path = args.out or cfg.results_dir / "fixtest" / f"{case.case_id}.{tag}.json"
    try:
        if inject.verify(case, cfg=cfg):
            print(f"applying {case.case_id} ...")
            inject.apply_case(case, cfg=cfg)
        result = test_fixes(case, fixes, preset=args.preset, progress=print)
    finally:
        if inject.verify(None, cfg=cfg):
            print("resetting the server to baseline ...")
            inject.reset(cfg=cfg)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=1) + "\n")
    print(format_table(result))
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
