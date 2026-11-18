"""``pgdiag-diagnose``: one symptom document -> Claude -> 4 ranked fixes (T9).

    pgdiag-diagnose <symptoms.json> --mode rag|norag [--out FILE]

RAG mode retrieves chunks itself (T8). Only the document's content reaches the model, never
its path. Prints the fixes; ``--out`` writes the full record (prompt, responses, usage) as JSON.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from pgdiag.diagnose.core import DiagnosisError, diagnose


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pgdiag-diagnose", description=__doc__.splitlines()[0])
    parser.add_argument("symptoms", type=Path, help="a symptom JSON file")
    parser.add_argument("--mode", choices=["rag", "norag"], required=True)
    parser.add_argument("--out", type=Path, help="write the full record here (JSON)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    doc = json.loads(args.symptoms.read_text())
    chunks = None
    if args.mode == "rag":
        from pgdiag.retrieve import retrieve

        chunks = [h.to_dict() for h in retrieve(doc)]
    try:
        record = diagnose(doc, args.mode, chunks)
    except DiagnosisError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for f in record["fixes"]:
        cites = ", ".join(f["cited_chunks"]) or "-"
        print(f"{f['rank']}. {f['setting']} = {f['proposed_value']}\n   {f['reasoning']}")
        print(f"   cites: {cites}")
    u = record["usage"]
    print(
        f"model {record['model']}, attempts {record['attempts']}, "
        f"tokens in {u['input_tokens']} out {u['output_tokens']}, "
        f"dropped citations {record['dropped_citations'] or '-'}"
    )
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(record, indent=1) + "\n")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
