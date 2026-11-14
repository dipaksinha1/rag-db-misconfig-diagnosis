"""Discover and download the PostgreSQL 16 HTML documentation.

Pages are found by following links, breadth first, from the table of contents
(``index.html``); file names are never guessed. Only ``/docs/16/*.html`` is in
scope: other versions, images, CSS and the alphabetical index are not fetched.
All requests go through ``pgdiag.fetch.PoliteFetcher`` (cache, 1 request per
second, robots.txt), so an interrupted crawl resumes where it stopped and a
finished crawl makes no requests at all.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from pgdiag.fetch import FetchBlocked, FetchError, PoliteFetcher, UrlNotAllowed, normalize_url

log = logging.getLogger(__name__)

PG_VERSION = "16"
DOCS_BASE_URL = f"https://www.postgresql.org/docs/{PG_VERSION}/"
START_URL = DOCS_BASE_URL + "index.html"
PAGE_URL_RE = re.compile(re.escape(DOCS_BASE_URL) + r"(?P<page>[A-Za-z0-9_.-]+)\.html$")

#: Never downloaded: the alphabetical index is one huge page of links to pages
#: that the table of contents already reaches.
SKIP_DOWNLOAD_PAGES: frozenset[str] = frozenset({"bookindex"})
MAX_CONSECUTIVE_ERRORS = 5


def page_name(url: str) -> str | None:
    """``https://www.postgresql.org/docs/16/planner-stats.html`` -> ``planner-stats``."""
    match = PAGE_URL_RE.match(normalize_url(url))
    return match.group("page") if match else None


def extract_doc_links(html: str | bytes, page_url: str) -> list[str]:
    """In-scope documentation pages linked from the content area, in document order."""
    soup = BeautifulSoup(html, "lxml")
    root = soup.find(id="docContent") or soup
    seen: dict[str, None] = {}
    for anchor in root.find_all("a", href=True):
        url = normalize_url(urljoin(page_url, anchor["href"]))
        name = page_name(url)
        if name is not None and name not in SKIP_DOWNLOAD_PAGES:
            seen.setdefault(url)
    return list(seen)


@dataclass
class CrawlStats:
    pages: int = 0  # pages available (status 200)
    fetched: int = 0  # downloaded during this run
    cached: int = 0  # served from data/raw/
    missing: list[str] = field(default_factory=list)  # not in the cache (offline) or HTTP 404
    failed: list[str] = field(default_factory=list)  # network or server errors this run
    disallowed: list[str] = field(default_factory=list)  # excluded by robots.txt
    truncated: bool = False  # stopped early because of --limit

    @property
    def complete(self) -> bool:
        return not (self.missing or self.failed or self.truncated)


def crawl(
    fetcher: PoliteFetcher,
    *,
    offline: bool = False,
    limit: int | None = None,
    stats: CrawlStats | None = None,
) -> Iterator[tuple[str, bytes]]:
    """Yield ``(url, html_bytes)`` for every documentation page, in breadth-first order.

    ``offline=True`` walks the cache only and never touches the network; pages
    that were not downloaded are recorded in ``stats.missing``. ``limit`` stops
    after that many pages (debugging). ``FetchBlocked`` (403/429) propagates: the
    caller must stop and report it.
    """
    stats = stats if stats is not None else CrawlStats()
    queue: deque[str] = deque([START_URL])
    queued: set[str] = {START_URL}
    consecutive_errors = 0
    while queue:
        if limit is not None and stats.pages >= limit:
            stats.truncated = True
            return
        url = queue.popleft()
        try:
            result = fetcher.read_cache(url) if offline else fetcher.get(url)
        except UrlNotAllowed as exc:
            log.warning("skipping %s: %s", url, exc)
            stats.disallowed.append(url)
            continue
        except FetchBlocked:
            raise
        except FetchError as exc:
            log.error("failed: %s", exc)
            stats.failed.append(url)
            consecutive_errors += 1
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                raise FetchError(
                    f"{consecutive_errors} consecutive failures; stopping the crawl. "
                    "Re-run later: finished pages are cached."
                ) from exc
            continue
        consecutive_errors = 0
        if result is None or not result.ok:
            log.warning("missing: %s (%s)", url, "not cached" if result is None else result.status)
            stats.missing.append(url)
            continue
        stats.pages += 1
        if result.from_cache:
            stats.cached += 1
        else:
            stats.fetched += 1
        if stats.pages % 50 == 0:
            log.info(
                "%d pages (%d fetched, %d cached), %d queued",
                stats.pages,
                stats.fetched,
                stats.cached,
                len(queue),
            )
        for link in extract_doc_links(result.content, result.final_url):
            if link not in queued:
                queued.add(link)
                queue.append(link)
        yield url, result.content
