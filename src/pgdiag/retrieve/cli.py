"""``pgdiag-retrieve``: symptom document -> queries -> retrieved chunks (T8).

    pgdiag-retrieve <symptoms.json> [-k N]   print the queries and the hits
    pgdiag-retrieve eval [-k N]              all six cases: results/retrieval/<case_id>.json + score

``eval`` uses the case id (the file name) only for scoring, never for querying.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from pgdiag.config import get_config
from pgdiag.index.probes import setting_chunk_id
from pgdiag.index.search import get_index
from pgdiag.retrieve.core import DEFAULT_K, build_queries, retrieve
from pgdiag.settings import CASE_SETTINGS


def evaluate(k: int) -> int:
    cfg = get_config()
    index = get_index()
    out_dir = cfg.results_dir / "retrieval"
    out_dir.mkdir(parents=True, exist_ok=True)
    found = 0
    for i, setting in enumerate(CASE_SETTINGS, start=1):
        case_id = f"c{i:02d}_{setting}"
        doc = json.loads((cfg.results_dir / "symptoms" / f"{case_id}.json").read_text())
        hits = retrieve(doc, k=k, index=index)
        expected = setting_chunk_id(index, setting)
        rank = next((n for n, h in enumerate(hits, 1) if h.chunk_id == expected), None)
        found += rank is not None
        record = {
            "queries": build_queries(doc),
            "k": k,
            "hits": [h.to_dict() for h in hits],
        }
        (out_dir / f"{case_id}.json").write_text(json.dumps(record, indent=1) + "\n")
        print(f"{case_id:<40} {'yes' if rank else 'no ':<4} rank {rank or '-'}")
    print(f"total {found}/{len(CASE_SETTINGS)}")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pgdiag-retrieve", description=__doc__.splitlines()[0])
    parser.add_argument("target", help="a symptom JSON file, or 'eval'")
    parser.add_argument("-k", type=int, default=DEFAULT_K)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    for noisy in ("bm25s", "sentence_transformers", "chromadb", "huggingface_hub"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if args.target == "eval":
        evaluate(args.k)
        return 0
    doc = json.loads(Path(args.target).read_text())
    print("queries:")
    for q in build_queries(doc):
        print(f"  - {q}")
    print("hits:")
    for n, h in enumerate(retrieve(doc, k=args.k), start=1):
        print(f"{n:3}. [{h.source}] {h.chunk_id}  ({h.title})\n      via: {h.query}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
