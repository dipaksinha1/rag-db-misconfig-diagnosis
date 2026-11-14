"""``pgdiag-index``: build and query the RAG index (T4).

    pgdiag-index build [--rebuild]            embed all chunk files into data/index/
    pgdiag-index search "query" [-k 5] [--source docs] [--mode hybrid|vector|bm25]
    pgdiag-index get CHUNK_ID [CHUNK_ID ...]  print chunks by id (JSON)
    pgdiag-index stats                        what the index holds and how it was built
    pgdiag-index check                        acceptance (setting names) + symptom probes

``build`` does nothing when the chunk files, the model name and the options are
unchanged since the last build; ``--rebuild`` forces it.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from pgdiag.chunks import SOURCES
from pgdiag.config import get_config
from pgdiag.index import store
from pgdiag.index.build import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_MIN_DOCS_SECTION_CHARS,
    BuildOptions,
    build_index,
)
from pgdiag.index.probes import format_rank, run_probes, setting_name_check
from pgdiag.index.search import MODES, FusionParams, Hit, HybridIndex
from pgdiag.settings import TARGET_SETTINGS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pgdiag-index",
        description="Build and query the RAG index: docs and release-note chunks, embedded "
        "with a local sentence-transformers model, stored in Chroma, searched with BM25 + "
        "vector similarity fused by Reciprocal Rank Fusion.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="embed every data/chunks/*.jsonl into data/index/")
    build.add_argument("--rebuild", action="store_true", help="rebuild even if nothing changed")
    build.add_argument(
        "--min-docs-section-chars",
        type=int,
        default=DEFAULT_MIN_DOCS_SECTION_CHARS,
        help="leave out docs section chunks shorter than this (default %(default)s; 0 keeps all)",
    )
    build.add_argument(
        "--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="embedding batch size"
    )
    build.add_argument(
        "--query-prefix",
        default=None,
        help="instruction put before queries (default: the model's recommended one)",
    )

    def add_fusion_args(p: argparse.ArgumentParser) -> None:
        defaults = FusionParams()
        p.add_argument("--rrf-k", type=int, default=defaults.rrf_k, help="RRF constant")
        p.add_argument("--vector-weight", type=float, default=defaults.vector_weight)
        p.add_argument("--bm25-weight", type=float, default=defaults.bm25_weight)
        p.add_argument("--depth", type=int, default=defaults.depth, help="candidates per retriever")

    search = sub.add_parser("search", help="hybrid search")
    search.add_argument("query")
    search.add_argument("-k", type=int, default=10, help="number of results (default 10)")
    search.add_argument(
        "--source",
        action="append",
        choices=SOURCES,
        help="only this source (repeatable; default all)",
    )
    search.add_argument("--mode", choices=MODES, default="hybrid")
    search.add_argument("--text", action="store_true", help="print each chunk's text")
    search.add_argument("--json", action="store_true", help="print the hits as JSON")
    add_fusion_args(search)

    get = sub.add_parser("get", help="print chunks by id as JSON")
    get.add_argument("chunk_ids", nargs="+")

    sub.add_parser("stats", help="per-source counts and build information")

    check = sub.add_parser("check", help="setting-name acceptance check and symptom probes")
    add_fusion_args(check)
    return parser


def _fusion(args: argparse.Namespace) -> FusionParams:
    return FusionParams(
        rrf_k=args.rrf_k,
        vector_weight=args.vector_weight,
        bm25_weight=args.bm25_weight,
        depth=args.depth,
    )


def _counts_lines(counts: dict[str, int], present: set[str]) -> list[str]:
    lines = []
    for source in SOURCES:
        if source in present or counts.get(source):
            lines.append(f"  {source:<14} {counts.get(source, 0):>6}")
        else:
            lines.append(f"  {source:<14} {0:>6}  (not built)")
    return lines


def print_manifest(manifest: dict) -> None:
    emb = manifest["embedding"]
    print(f"index built {manifest['built_at']}")
    print(
        f"embedding model: {emb['model']} @ {emb['revision']} "
        f"(dim {emb['dim']}, normalized, max {emb['max_seq_length']} tokens, {emb['device']})"
    )
    print(f"query prefix: {emb['query_prefix']!r}")
    print("inputs:")
    for name, info in sorted(manifest["inputs"].items()):
        print(f"  {name:<22} {info['chunks']:>6} chunks  sha256 {info['sha256'][:16]}...")
    selection = manifest["selection"]
    left_out = sum(selection["left_out"].values())
    print(
        f"left out: {left_out} ({selection['left_out']}; docs sections shorter than "
        f"{selection['min_docs_section_chars']} characters)"
    )
    present = {s for info in manifest["inputs"].values() for s in info["sources"]}
    print(f"indexed chunks per source ({manifest['total']} total):")
    for line in _counts_lines(manifest["counts"], present):
        print(line)
    for source, kinds in sorted(manifest["chunk_types"].items()):
        print(f"  {source} chunk types: " + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())))
    timing = manifest["timing_seconds"]
    print(
        f"build time: {timing['total']} s (model load {timing['model_load']} s, "
        f"embedding {timing['embed']} s, Chroma {timing['store']} s)"
    )


def print_hits(hits: list[Hit], show_text: bool = False) -> None:
    if not hits:
        print("(no results)")
    for hit in hits:
        vector = f"{hit.vector_rank}" if hit.vector_rank is not None else "-"
        bm25 = f"{hit.bm25_rank}" if hit.bm25_rank is not None else "-"
        print(
            f"{hit.rank:>3}. {hit.score:.4f}  [vec {vector:>3} | bm25 {bm25:>3}]  "
            f"{hit.source:<13} {hit.chunk_id}"
        )
        print(f"       {hit.title}")
        if show_text:
            for line in hit.text.splitlines():
                print(f"       | {line}")


def cmd_build(args: argparse.Namespace) -> int:
    cfg = get_config()
    cfg.ensure_dirs()
    options = BuildOptions(
        min_docs_section_chars=args.min_docs_section_chars,
        batch_size=args.batch_size,
        query_prefix=args.query_prefix,
    )
    result = build_index(cfg, options=options, rebuild=args.rebuild)
    if result.skipped:
        print("index is up to date (inputs, model and options unchanged); use --rebuild to force")
    print_manifest(result.manifest)
    print(f"index: {cfg.index_dir}")
    return 0


def _load(args: argparse.Namespace) -> HybridIndex:
    fusion = _fusion(args) if hasattr(args, "rrf_k") else None
    return HybridIndex.load(get_config(), fusion=fusion)


def cmd_search(args: argparse.Namespace) -> int:
    index = _load(args)
    started = time.monotonic()
    hits = index.search(args.query, k=args.k, source=args.source, mode=args.mode)
    elapsed = time.monotonic() - started
    if args.json:
        print(json.dumps([hit.to_dict() for hit in hits], indent=2, ensure_ascii=False))
        return 0
    print_hits(hits, args.text)
    print(f"({len(hits)} hits in {elapsed:.2f} s, including model load on first use)")
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    index = _load(args)
    try:
        chunks = index.get_chunks(args.chunk_ids)
    except KeyError as exc:
        print(f"error: {exc.args[0]}", file=sys.stderr)
        return 1
    for chunk in chunks:
        print(json.dumps(chunk.to_dict(), indent=2, ensure_ascii=False))
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    index = _load(args)
    print_manifest(index.manifest)
    print("live counts per source (from Chroma):")
    for source, count in index.counts().items():
        print(f"  {source:<14} {count:>6}")
    print("target-setting coverage (indexed chunks mentioning each / dedicated docs chunk):")
    for name in TARGET_SETTINGS:
        mentions = sum(1 for cid in index.chunk_ids if name in index.get_chunk(cid).settings)
        print(f"  {name:<32} {mentions:>4}")
    print(f"index: {index.index_dir}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    index = _load(args)
    started = time.monotonic()
    index.warm_up()
    print(f"loaded model and BM25 in {time.monotonic() - started:.1f} s")
    print(f"fusion: {index.fusion}")
    print("indexed chunks per source:")
    for line in _counts_lines(index.counts(), {s for s, n in index.counts().items() if n}):
        print(line)

    print("\nacceptance: search(<setting name>, k=3) must return its docs setting chunk")
    started = time.monotonic()
    results = setting_name_check(index, k=3)
    passed = 0
    for result in results:
        ok = result.rank is not None
        passed += ok
        print(f"  {'PASS' if ok else 'FAIL'} {result.setting:<32} rank {format_rank(result.rank)}")
        for hit in result.hits:
            marker = "*" if hit.chunk_id == result.expected else " "
            print(
                f"     {marker}{hit.rank}. {hit.chunk_id}  "
                f"[vec {hit.vector_rank or '-'} | bm25 {hit.bm25_rank or '-'}]"
            )
    per_query = (time.monotonic() - started) / len(results)
    print(f"  {passed}/{len(results)} passed ({per_query * 1000:.0f} ms per search)")

    print("\nsymptom probes (informative): rank of the setting's docs chunk in the top 100")
    print(f"  {'query':<70} {'setting':<32} hybrid vector  bm25  first-mention")
    for probe in run_probes(index):
        print(
            f"  {probe.query[:70]:<70} {probe.setting:<32} {format_rank(probe.hybrid):>6} "
            f"{format_rank(probe.vector):>6} {format_rank(probe.bm25):>5}  "
            f"{format_rank(probe.first_mention):>6}"
        )
    return 0 if passed == len(results) else 1


COMMANDS = {
    "build": cmd_build,
    "search": cmd_search,
    "get": cmd_get,
    "stats": cmd_stats,
    "check": cmd_check,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for noisy in (
        "httpx",
        "chromadb",
        "sentence_transformers",
        "huggingface_hub",
        "urllib3",
        "bm25s",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        return COMMANDS[args.command](args)
    except store.IndexStoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
