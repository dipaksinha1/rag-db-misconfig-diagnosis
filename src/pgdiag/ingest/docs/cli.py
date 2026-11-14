"""``pgdiag-ingest-docs``: download the PostgreSQL 16 documentation and chunk it (T1).

    pgdiag-ingest-docs                  download what is missing, then chunk
    pgdiag-ingest-docs --download-only  only fill the cache under data/raw/
    pgdiag-ingest-docs --chunk-only     only chunk what is cached (no network)
    pgdiag-ingest-docs --limit 20       debugging: first 20 pages, nothing written

Output: ``data/chunks/docs.jsonl`` (see ``pgdiag.chunks``) and
``data/chunks/guc_names.json`` (every parameter name found on the
server-configuration pages). Downloads are cached and rate-limited by
``pgdiag.fetch``; a full first download takes about 20 minutes.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path

from pgdiag.chunks import DOCS_CHUNKS_FILE, Chunk, save_guc_names, write_jsonl
from pgdiag.config import Config, get_config
from pgdiag.fetch import FetchBlocked, FetchError, PoliteFetcher
from pgdiag.ingest.docs.crawl import DOCS_BASE_URL, PG_VERSION, CrawlStats, crawl, page_name
from pgdiag.ingest.docs.parse import (
    DEFAULT_MAX_CHARS,
    is_release_page,
    is_skipped_page,
    parse_page,
    setting_names,
    tag_settings,
)
from pgdiag.settings import TARGET_SETTINGS

log = logging.getLogger("pgdiag.ingest.docs")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pgdiag-ingest-docs",
        description=f"Download the PostgreSQL {PG_VERSION} documentation from postgresql.org "
        "(cached, at most one request per second) and split it into chunks.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--download-only", action="store_true", help="fill the cache, do not chunk")
    mode.add_argument(
        "--chunk-only", action="store_true", help="chunk the cached pages; never use the network"
    )
    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="debugging: stop after N pages; nothing is written unless --output is given",
    )
    parser.add_argument(
        "--output",
        type=Path,
        metavar="PATH",
        help=f"chunk file (default data/chunks/{DOCS_CHUNKS_FILE})",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=DEFAULT_MAX_CHARS,
        help="target maximum characters per section chunk (default %(default)s)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser


def ingest(
    cfg: Config,
    fetcher: PoliteFetcher,
    *,
    download: bool = True,
    chunk: bool = True,
    limit: int | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> tuple[list[Chunk], CrawlStats, Counter]:
    """Walk the documentation and return ``(chunks, crawl stats, page counts)``."""
    stats = CrawlStats()
    pages: Counter = Counter()
    chunks: list[Chunk] = []
    for url, html in crawl(fetcher, offline=not download, limit=limit, stats=stats):
        page = page_name(url) or ""
        if is_release_page(page):
            pages["release notes (left for T2)"] += 1
        elif is_skipped_page(page):
            pages["skipped (index, legal notice)"] += 1
        elif chunk:
            page_chunks = parse_page(html, url, max_chars=max_chars)
            chunks.extend(page_chunks)
            pages["chunked" if page_chunks else "no text of their own (contents only)"] += 1
    if chunk:
        tag_settings(chunks, setting_names(chunks))
    return chunks, stats, pages


def print_stats(
    chunks: list[Chunk], stats: CrawlStats, pages: Counter, fetcher: PoliteFetcher, chunked: bool
) -> None:
    print(f"PostgreSQL {PG_VERSION} documentation: {DOCS_BASE_URL}")
    print(
        f"pages: {stats.pages} total, {stats.fetched} fetched this run, {stats.cached} from cache "
        f"({fetcher.network_requests} HTTP requests)"
    )
    for label, urls in (
        ("missing", stats.missing),
        ("failed", stats.failed),
        ("robots.txt", stats.disallowed),
    ):
        if urls:
            print(f"  {label}: {len(urls)} page(s), e.g. {', '.join(urls[:3])}")
    for label, count in sorted(pages.items()):
        print(f"  pages {label}: {count}")
    if not chunked:
        return
    by_type = Counter(c.meta["chunk_type"] for c in chunks)
    lengths = sorted(len(c.text) for c in chunks) or [0]
    print(f"chunks: {len(chunks)} total")
    for chunk_type, count in sorted(by_type.items()):
        print(f"  {chunk_type}: {count}")
    print(
        f"  text length: median {lengths[len(lengths) // 2]}, max {lengths[-1]} characters; "
        f"{sum(1 for c in chunks if c.settings)} chunks mention at least one parameter"
    )
    print(f"setting chunks (one per configuration parameter): {by_type.get('setting', 0)}")
    print("target settings:")
    for name in TARGET_SETTINGS:
        ids = [
            c.chunk_id
            for c in chunks
            if c.meta["chunk_type"] == "setting" and c.meta["setting"] == name
        ]
        mentions = sum(1 for c in chunks if name in c.settings)
        if len(ids) == 1:
            status = ids[0]
        elif not ids and stats.truncated:
            status = "(not reached in this partial run)"
        else:
            status = f"PROBLEM: {len(ids)} dedicated chunks {ids}"
        print(f"  {name:<32} {status}  (mentioned in {mentions} chunks)")


def target_problems(chunks: list[Chunk]) -> list[str]:
    """Target settings that do not have exactly one dedicated chunk."""
    counts = Counter(c.meta.get("setting") for c in chunks if c.meta["chunk_type"] == "setting")
    return [
        f"{name}: {counts[name]} dedicated chunks" for name in TARGET_SETTINGS if counts[name] != 1
    ]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = get_config()
    cfg.ensure_dirs()
    fetcher = PoliteFetcher(cfg)
    chunked = not args.download_only
    try:
        chunks, stats, pages = ingest(
            cfg,
            fetcher,
            download=not args.chunk_only,
            chunk=chunked,
            limit=args.limit,
            max_chars=args.max_chars,
        )
    except FetchBlocked as exc:
        print(f"error: postgresql.org refused the request: {exc}", file=sys.stderr)
        print("Stop, wait and report this; do not work around it.", file=sys.stderr)
        return 2
    except FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print_stats(chunks, stats, pages, fetcher, chunked)
    if stats.failed or stats.missing or stats.disallowed:
        hint = "run without --chunk-only to download" if args.chunk_only else "re-run to retry"
        print(f"error: the documentation is incomplete ({hint}); nothing written", file=sys.stderr)
        return 1
    if not chunked:
        return 0
    if stats.truncated and args.output is None:
        print(f"--limit {args.limit}: partial run, nothing written (pass --output to keep it)")
        return 0

    output = args.output or cfg.chunks_dir / DOCS_CHUNKS_FILE
    count = write_jsonl(output, chunks)
    print(f"wrote {count} chunks to {output}")
    if stats.truncated:
        return 0
    names_path = save_guc_names(
        setting_names(chunks),
        cfg,
        pg_version=PG_VERSION,
        source=DOCS_BASE_URL + "runtime-config.html",
    )
    print(f"wrote {len(setting_names(chunks))} parameter names to {names_path}")
    problems = target_problems(chunks)
    if problems:
        print(
            "error: target settings without exactly one dedicated chunk: " + "; ".join(problems),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
