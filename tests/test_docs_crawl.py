"""Page discovery for the documentation crawl, offline: a fake site served by a fake transport."""

from __future__ import annotations

from pathlib import Path

import pytest

from pgdiag.config import load_config
from pgdiag.fetch import ROBOTS_URL, FetchBlocked, FetchError, PoliteFetcher, RateLimiter
from pgdiag.ingest.docs.cli import ingest
from pgdiag.ingest.docs.crawl import (
    DOCS_BASE_URL,
    PAGE_URL_RE,
    START_URL,
    CrawlStats,
    crawl,
    extract_doc_links,
    page_name,
)
from tests.fakes import FakeClock, FakeResponse, FakeSession

FIXTURES = Path(__file__).parent / "fixtures" / "docs"
LEAF = b'<html><body><div id="docContent"><p>leaf page</p></div></body></html>'
ROBOTS = b"User-agent: *\nDisallow: /docs/devel/\nDisallow: /list/\n"


def url(page: str) -> str:
    return f"{DOCS_BASE_URL}{page}.html"


def fixture(page: str) -> bytes:
    return (FIXTURES / f"{page}.html").read_bytes()


INDEX_LINKS = [
    "preface",  # the navigation header's "Next" link comes first
    "legalnotice",
    "intro-whatis",
    "sql",
    "performance-tips",
    "admin",
    "runtime-config",
    "runtime-config-resource",
    "appendixes",
    "release",
    "glossary",
    "biblio",
]


def make_site() -> dict[str, FakeResponse]:
    """index and three real pages from the fixtures; every other linked page is a leaf."""
    site = {ROBOTS_URL: FakeResponse(content=ROBOTS)}
    for page in ("index", "runtime-config", "runtime-config-resource", "planner-stats"):
        site[url(page)] = FakeResponse(content=fixture(page))
    for page in ("index", "runtime-config", "runtime-config-resource", "planner-stats"):
        for link in extract_doc_links(fixture(page), url(page)):
            site.setdefault(link, FakeResponse(content=LEAF))
    return site


@pytest.fixture
def cfg(tmp_path):
    return load_config(env={}, root=tmp_path)


def make_fetcher(cfg, routes) -> tuple[PoliteFetcher, FakeSession]:
    clock = FakeClock()
    session = FakeSession(routes, clock)
    limiter = RateLimiter(cfg.crawl_delay_seconds, clock=clock.time, sleep=clock.sleep)
    return PoliteFetcher(cfg, session=session, rate_limiter=limiter, sleep=clock.sleep), session


def test_page_name_accepts_only_version_16_html_pages():
    assert page_name(url("runtime-config-resource")) == "runtime-config-resource"
    assert page_name(url("planner-stats") + "#PLANNER-STATS-EXTENDED") == "planner-stats"
    for other in (
        "https://www.postgresql.org/docs/15/planner-stats.html",
        "https://www.postgresql.org/docs/current/planner-stats.html",
        "https://www.postgresql.org/docs/16/",
        "https://www.postgresql.org/docs/16/sub/dir.html",
        "https://www.postgresql.org/docs/16/postgresql-16-A4.pdf",
        "https://www.postgresql.org/media/img/docs/bg_hdr.png",
        "http://www.postgresql.org/docs/16/planner-stats.html",
        "https://example.com/docs/16/planner-stats.html",
    ):
        assert page_name(other) is None, other


def test_links_come_from_the_content_area_in_order_without_duplicates():
    links = extract_doc_links(fixture("index"), START_URL)
    assert links == [url(page) for page in INDEX_LINKS]
    # Not followed: other versions (also in the site header), the alphabetical index, other
    # hosts, images, PDFs, nested paths; fragments are dropped and duplicates collapse.
    assert all(PAGE_URL_RE.match(link) for link in links)
    assert url("bookindex") not in links and url("index") not in links


def test_links_on_a_chapter_page():
    links = extract_doc_links(fixture("runtime-config"), url("runtime-config"))
    names = [page_name(link) for link in links]
    assert len(names) == len(set(names))
    assert {"runtime-config-resource", "runtime-config-query", "runtime-config-wal"} <= set(names)
    assert {"index", "admin"} <= set(names)  # navigation links are fine: they are in scope
    assert all(name and "#" not in link for name, link in zip(names, links, strict=True))


def test_crawl_is_breadth_first_in_scope_and_fetches_each_page_once(cfg):
    fetcher, session = make_fetcher(cfg, make_site())
    stats = CrawlStats()
    pages = [page_name(page_url) for page_url, _ in crawl(fetcher, stats=stats)]

    assert pages[: 1 + len(INDEX_LINKS)] == ["index", *INDEX_LINKS]
    assert "runtime-config-wal" in pages and len(pages) == len(set(pages))
    assert pages.index("runtime-config-wal") > pages.index("biblio")  # second level comes later
    assert stats.pages == stats.fetched == len(pages) and stats.cached == 0 and stats.complete
    assert session.urls[0] == ROBOTS_URL
    requested = session.urls[1:]
    assert len(requested) == len(set(requested)) == len(pages)
    assert all(PAGE_URL_RE.match(u) for u in requested)
    assert all(call["headers"]["User-Agent"] == cfg.user_agent for call in session.calls)
    starts = [call["started"] for call in session.calls]
    assert all(b - a >= 1.0 for a, b in zip(starts, starts[1:], strict=False))


def test_second_crawl_is_served_entirely_from_the_cache(cfg):
    fetcher, _ = make_fetcher(cfg, make_site())
    first = [page_url for page_url, _ in crawl(fetcher)]

    again, session = make_fetcher(cfg, {})  # a server that would answer 404 to everything
    stats = CrawlStats()
    assert [page_url for page_url, _ in crawl(again, stats=stats)] == first
    assert session.calls == [] and stats.fetched == 0 and stats.cached == len(first)

    offline, session = make_fetcher(cfg, {})
    assert [page_url for page_url, _ in crawl(offline, offline=True)] == first
    assert session.calls == []


def test_offline_crawl_never_uses_the_network_and_reports_what_is_missing(cfg):
    fetcher, session = make_fetcher(cfg, make_site())
    stats = CrawlStats()
    assert list(crawl(fetcher, offline=True, stats=stats)) == []
    assert session.calls == [] and stats.missing == [START_URL] and not stats.complete


def test_interrupted_crawl_resumes_where_it_stopped(cfg):
    fetcher, session = make_fetcher(cfg, make_site())
    stats = CrawlStats()
    assert len(list(crawl(fetcher, limit=5, stats=stats))) == 5
    assert stats.truncated and not stats.complete
    done = len(session.calls)

    resumed, session = make_fetcher(cfg, make_site())
    stats = CrawlStats()
    total = len(list(crawl(resumed, stats=stats)))
    assert stats.cached == 5 and stats.fetched == total - 5 and stats.complete
    assert len(session.calls) == total - 5 and done == 6  # 5 pages + robots.txt


def test_missing_pages_are_recorded_and_the_crawl_continues(cfg):
    site = make_site()
    del site[url("glossary")]  # the fake server answers 404
    fetcher, _ = make_fetcher(cfg, site)
    stats = CrawlStats()
    pages = [page_name(page_url) for page_url, _ in crawl(fetcher, stats=stats)]
    assert "glossary" not in pages and "biblio" in pages
    assert stats.missing == [url("glossary")] and not stats.complete


def test_blocked_crawl_stops_immediately(cfg):
    site = make_site()
    site[url("legalnotice")] = FakeResponse(403, b"forbidden")
    fetcher, session = make_fetcher(cfg, site)
    with pytest.raises(FetchBlocked):
        list(crawl(fetcher))
    assert session.urls == [ROBOTS_URL, START_URL, url("preface"), url("legalnotice")]


def test_repeated_server_errors_stop_the_crawl(cfg):
    site = {ROBOTS_URL: FakeResponse(content=ROBOTS)}
    site[START_URL] = FakeResponse(content=fixture("index"))
    for page in INDEX_LINKS:
        site[url(page)] = FakeResponse(503)
    fetcher, _ = make_fetcher(cfg, site)
    fetcher.max_retries = 0
    stats = CrawlStats()
    with pytest.raises(FetchError, match="consecutive failures"):
        list(crawl(fetcher, stats=stats))
    assert len(stats.failed) == 5 and stats.pages == 1


def test_ingest_chunks_the_crawled_pages_and_leaves_release_notes_alone(cfg):
    fetcher, _ = make_fetcher(cfg, make_site())
    chunks, stats, pages = ingest(cfg, fetcher)
    assert stats.complete
    by_page = {c.meta["page"] for c in chunks}
    assert {"runtime-config", "runtime-config-resource"} <= by_page
    assert not by_page & {"index", "legalnotice", "release"}
    assert pages["release notes (left for T2)"] == 1
    assert pages["skipped (index, legal notice)"] == 2
    work_mem = [c for c in chunks if c.meta.get("setting") == "work_mem"]
    assert len(work_mem) == 1 and work_mem[0].settings == ["hash_mem_multiplier", "work_mem"]

    # --chunk-only on the same cache gives the same chunks without any request.
    offline, session = make_fetcher(cfg, {})
    again, _, _ = ingest(cfg, offline, download=False)
    assert again == chunks and session.calls == []
    # --download-only produces no chunks.
    assert ingest(cfg, offline, chunk=False)[0] == []
