"""``pgdiag-ingest-release-notes``: chunk the PostgreSQL 16 release notes (T2).

    pgdiag-ingest-release-notes                  download what is missing, then chunk
    pgdiag-ingest-release-notes --download-only  only fill the cache under data/raw/
    pgdiag-ingest-release-notes --chunk-only     only chunk what is cached (no network)

The release pages are found from the appendix index (``release.html``): 16.0
and every 16.x minor release. ``pgdiag-ingest-docs`` (T1) has normally cached
all of them already, so this makes no request at all; anything missing is
fetched through ``pgdiag.fetch`` (cached, one request per second).

Output: ``data/chunks/release_notes.jsonl`` (see ``pgdiag.chunks`` and
``pgdiag.ingest.release_notes.parse``). If the index or any release page is
missing, or a page does not yield at least one release-note item, the command
exits non-zero and writes nothing.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from pgdiag.chunks import (
    AMBIGUOUS_NAMES,
    GUC_NAMES_FILE,
    RELEASE_NOTES_CHUNKS_FILE,
    Chunk,
    load_guc_names,
    write_jsonl,
)
from pgdiag.config import Config, get_config
from pgdiag.fetch import FetchBlocked, FetchError, FetchResult, PoliteFetcher, UrlNotAllowed
from pgdiag.ingest.docs.crawl import PG_VERSION, page_name
from pgdiag.ingest.release_notes.parse import (
    DEFAULT_MAX_CHARS,
    DEFAULT_MIN_ITEM_CHARS,
    INDEX_URL,
    Release,
    ReleaseIndex,
    ReleaseNotesError,
    parse_release_index,
    parse_release_page,
)
from pgdiag.settings import TARGET_SETTINGS

log = logging.getLogger("pgdiag.ingest.release_notes")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pgdiag-ingest-release-notes",
        description=f"Split the PostgreSQL {PG_VERSION} release notes ({PG_VERSION}.0 and every "
        "minor release) into chunks. Pages come from the download cache; missing ones are "
        "fetched from postgresql.org (cached, at most one request per second).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--download-only", action="store_true", help="fill the cache, do not chunk")
    mode.add_argument(
        "--chunk-only", action="store_true", help="chunk the cached pages; never use the network"
    )
    parser.add_argument(
        "--output",
        type=Path,
        metavar="PATH",
        help=f"chunk file (default data/chunks/{RELEASE_NOTES_CHUNKS_FILE})",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=DEFAULT_MAX_CHARS,
        help="maximum characters in a group of short items (default %(default)s)",
    )
    parser.add_argument(
        "--min-item-chars",
        type=int,
        default=DEFAULT_MIN_ITEM_CHARS,
        help="an item at least this long is a chunk of its own (default %(default)s)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser


@dataclass
class IngestResult:
    index: ReleaseIndex | None = None
    releases: list[Release] = field(default_factory=list)  # oldest first
    chunks: list[Chunk] = field(default_factory=list)
    pages: int = 0  # pages read (the index and the release pages)
    fetched: int = 0  # downloaded during this run
    cached: int = 0  # served from data/raw/
    problems: list[str] = field(default_factory=list)  # anything here means: write nothing


def _load(fetcher: PoliteFetcher, url: str, download: bool, result: IngestResult) -> bytes | None:
    """The page body, or None after recording why it is unavailable."""
    try:
        page: FetchResult | None = fetcher.get(url) if download else fetcher.read_cache(url)
    except FetchBlocked:
        raise
    except UrlNotAllowed as exc:
        result.problems.append(f"not allowed: {url} ({exc})")
        return None
    except FetchError as exc:
        result.problems.append(f"failed: {url} ({exc})")
        return None
    if page is None:
        result.problems.append(f"missing: {url} (not in the cache)")
        return None
    if not page.ok:
        result.problems.append(f"missing: {url} (HTTP {page.status})")
        return None
    result.pages += 1
    if page.from_cache:
        result.cached += 1
    else:
        result.fetched += 1
    return page.content


def ingest(
    cfg: Config,
    fetcher: PoliteFetcher,
    *,
    download: bool = True,
    chunk: bool = True,
    names: Iterable[str] | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    min_item_chars: int = DEFAULT_MIN_ITEM_CHARS,
) -> IngestResult:
    """Read the release index and every release page it lists; chunk them.

    ``download=False`` reads the cache only and never touches the network.
    ``names`` are the parameter names for ``Chunk.settings`` (default: what
    ``pgdiag.chunks.find_settings`` uses, i.e. ``load_guc_names()`` without
    ``port`` and ``role``). ``FetchBlocked`` (403/429) propagates.
    Every other problem is recorded in ``IngestResult.problems``.
    """
    result = IngestResult()
    names = tuple(names) if names is not None else None
    html = _load(fetcher, INDEX_URL, download, result)
    if html is None:
        return result
    try:
        result.index = parse_release_index(html, INDEX_URL)
    except ReleaseNotesError as exc:
        result.problems.append(f"unusable index: {exc}")
        return result
    for ref in result.index.releases:
        html = _load(fetcher, ref.url, download, result)
        if html is None or not chunk:
            continue
        try:
            release = parse_release_page(
                html, ref.url, names=names, max_chars=max_chars, min_item_chars=min_item_chars
            )
        except ReleaseNotesError as exc:
            result.problems.append(f"unparsable: {exc}")
            continue
        if release.version != ref.version:
            result.problems.append(
                f"unparsable: {ref.url} is release {release.version}, "
                f"but the index lists it as {ref.version}"
            )
            continue
        result.releases.append(release)
        result.chunks.extend(release.chunks)
    return result


def parameter_names(path: Path) -> tuple[str, ...]:
    """Every PostgreSQL parameter name from ``guc_names.json`` except ``port`` and ``role``.

    Those two are also everyday words ("the default port number", "a role");
    ``find_settings`` leaves them out by default and so do the release notes.
    """
    return tuple(name for name in load_guc_names(path) if name not in AMBIGUOUS_NAMES)


def print_stats(result: IngestResult, fetcher: PoliteFetcher, chunked: bool) -> None:
    print(f"PostgreSQL {PG_VERSION} release notes: {INDEX_URL}")
    print(
        f"pages: {result.pages} used (the index and {max(result.pages - 1, 0)} release pages), "
        f"{result.fetched} fetched this run, {result.cached} from cache "
        f"({fetcher.network_requests} HTTP requests)"
    )
    if result.index is not None:
        for url, heading in result.index.ignored:
            print(
                f"  ignored (no PostgreSQL {PG_VERSION} notes): {page_name(url) or url} ({heading})"
            )
    for problem in result.problems:
        print(f"  PROBLEM {problem}")
    if not chunked or not result.releases:
        return
    chunks = result.chunks
    versions = [release.version for release in result.releases]
    print(f"versions: {len(versions)} ({versions[0]} to {versions[-1]})")
    print("  version  released    items  chunks  (items / prose / overview)")
    for release in result.releases:
        kinds = Counter(c.meta["chunk_type"] for c in release.chunks)
        print(
            f"  {release.version:<8} {release.release_date or 'unknown':<10} {release.items:>6} "
            f"{len(release.chunks):>7}  ({kinds['items']} / {kinds['prose']} / {kinds['overview']})"
        )
    by_type = Counter(c.meta["chunk_type"] for c in chunks)
    item_chunks = [c for c in chunks if c.meta["chunk_type"] == "items"]
    single = sum(1 for c in item_chunks if c.meta["item_count"] == 1)
    lengths = sorted(len(c.text) for c in chunks)
    print(f"chunks: {len(chunks)} total")
    for chunk_type, count in sorted(by_type.items()):
        print(f"  {chunk_type}: {count}")
    print(
        f"  release-note items: {sum(release.items for release in result.releases)}, "
        f"{single} as a chunk of their own, the rest in {len(item_chunks) - single} groups "
        f"of up to {max((c.meta['item_count'] for c in item_chunks), default=0)} short items"
    )
    print(
        f"  text length: median {lengths[len(lengths) // 2]}, max {lengths[-1]} characters; "
        f"{sum(1 for c in chunks if c.settings)} chunks mention at least one parameter"
    )
    print("target settings (release-note chunks that mention each):")
    for name in TARGET_SETTINGS:
        print(f"  {name:<32} {sum(1 for c in chunks if name in c.settings)}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = get_config()
    cfg.ensure_dirs()
    chunked = not args.download_only
    names_path = cfg.chunks_dir / GUC_NAMES_FILE
    if chunked and not names_path.is_file():
        print(
            f"error: {names_path} does not exist. It lists every PostgreSQL parameter name and "
            "is written by `pgdiag-ingest-docs` (T1); run that first.",
            file=sys.stderr,
        )
        return 1
    fetcher = PoliteFetcher(cfg)
    try:
        result = ingest(
            cfg,
            fetcher,
            download=not args.chunk_only,
            chunk=chunked,
            names=parameter_names(names_path) if chunked else None,
            max_chars=args.max_chars,
            min_item_chars=args.min_item_chars,
        )
    except FetchBlocked as exc:
        print(f"error: postgresql.org refused the request: {exc}", file=sys.stderr)
        print("Stop, wait and report this; do not work around it.", file=sys.stderr)
        return 2

    print_stats(result, fetcher, chunked)
    if result.problems:
        hint = "run without --chunk-only to download" if args.chunk_only else "see above"
        print(f"error: the release notes are incomplete ({hint}); nothing written", file=sys.stderr)
        return 1
    if not chunked:
        return 0
    output = args.output or cfg.chunks_dir / RELEASE_NOTES_CHUNKS_FILE
    count = write_jsonl(output, result.chunks)
    print(f"wrote {count} chunks to {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
