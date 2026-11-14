"""Offline tests for the polite fetcher: the transport is a fake, time is a fake clock."""

from __future__ import annotations

import json

import pytest
import requests

from pgdiag import fetch
from pgdiag.config import load_config
from pgdiag.fetch import (
    FetchBlocked,
    FetchError,
    PoliteFetcher,
    RateLimiter,
    UrlNotAllowed,
    cache_paths,
)
from tests.fakes import FakeClock, FakeResponse, FakeSession

BASE = "https://www.postgresql.org"
PAGE = f"{BASE}/docs/16/index.html"


@pytest.fixture
def cfg(tmp_path):
    return load_config(env={}, root=tmp_path)


@pytest.fixture
def clock():
    return FakeClock()


def make_fetcher(cfg, clock, routes=None, **kwargs) -> tuple[PoliteFetcher, FakeSession]:
    session = FakeSession(routes, clock)
    kwargs.setdefault("respect_robots", False)
    fetcher = PoliteFetcher(
        cfg,
        session=session,
        rate_limiter=RateLimiter(cfg.crawl_delay_seconds, clock=clock.time, sleep=clock.sleep),
        sleep=clock.sleep,
        **kwargs,
    )
    return fetcher, session


# ----------------------------------------------------------------------- cache


def test_first_get_downloads_and_writes_body_and_audit_metadata(cfg, clock):
    fetcher, session = make_fetcher(cfg, clock, {PAGE: FakeResponse(content=b"<html>toc</html>")})

    result = fetcher.get(PAGE)

    assert (result.status, result.ok, result.from_cache) == (200, True, False)
    assert result.content == b"<html>toc</html>" and result.text == "<html>toc</html>"
    assert result.content_type == "text/html; charset=utf-8"
    assert session.urls == [PAGE]
    body, meta_path = cache_paths(cfg.raw_dir, PAGE)
    assert result.path == body and body.is_relative_to(cfg.raw_dir / "www.postgresql.org")
    assert body.read_bytes() == b"<html>toc</html>"
    meta = json.loads(meta_path.read_text())
    assert meta["url"] == PAGE and meta["final_url"] == PAGE
    assert meta["status"] == 200 and meta["bytes"] == 16
    assert meta["content_type"] == "text/html; charset=utf-8"
    assert meta["fetched_at"].startswith("20") and meta["user_agent"] == cfg.user_agent
    assert len(meta["sha256"]) == 64


def test_cache_hit_makes_zero_network_calls(cfg, clock):
    fetcher, session = make_fetcher(cfg, clock, {PAGE: FakeResponse(content=b"first")})
    fetcher.get(PAGE)

    again = fetcher.get(PAGE)
    assert again.from_cache and again.content == b"first"
    assert len(session.calls) == 1 and fetcher.network_requests == 1 and fetcher.cache_hits == 1

    # A brand-new fetcher (a later run) must not touch the network either,
    # even though the server would now answer differently.
    other, other_session = make_fetcher(cfg, clock, {PAGE: FakeResponse(content=b"changed")})
    result = other.get(PAGE + "#some-fragment")  # the fragment is not part of the cache key
    assert result.from_cache and result.content == b"first"
    assert other_session.calls == [] and other.network_requests == 0
    assert other.is_cached(PAGE)


def test_cache_file_names_are_unique_per_url_and_readable(cfg):
    a, _ = cache_paths(cfg.raw_dir, f"{BASE}/docs/16/runtime-config-resource.html")
    b, _ = cache_paths(cfg.raw_dir, f"{BASE}/docs/16/runtime-config-resource.html?x=1")
    c, _ = cache_paths(cfg.raw_dir, f"{BASE}/robots.txt")
    assert a != b and a.parent == b.parent == cfg.raw_dir / "www.postgresql.org" / "docs"
    assert a.name.startswith("16_runtime-config-resource.html.")
    assert c.parent.name == "_root" and c.name.startswith("robots.txt.")


def test_404_is_cached_and_never_refetched(cfg, clock):
    gone = f"{BASE}/docs/16/nope.html"
    fetcher, session = make_fetcher(cfg, clock)
    first = fetcher.get(gone)
    second = fetcher.get(gone)
    assert (first.status, first.ok, first.from_cache) == (404, False, False)
    assert (second.status, second.from_cache) == (404, True)
    assert len(session.calls) == 1


def test_binary_payload_and_basic_auth(cfg, clock):
    url = f"{BASE}/list/pgsql-performance/mbox/pgsql-performance.202001"
    payload = b"From x\n\xff\xfe\x00binary"
    fetcher, session = make_fetcher(
        cfg,
        clock,
        {url: FakeResponse(content=payload, headers={"Content-Type": "application/mbox"})},
    )
    result = fetcher.get(url, auth=("archives", "antispam"))
    assert result.content == payload and result.content_type == "application/mbox"
    assert session.calls[0]["auth"] == ("archives", "antispam")
    _, meta_path = cache_paths(cfg.raw_dir, url)
    assert "antispam" not in meta_path.read_text()
    assert fetcher.get(url).content == payload and len(session.calls) == 1


# --------------------------------------------------------------- request shape


def test_user_agent_timeout_and_no_automatic_redirects(cfg, clock):
    fetcher, session = make_fetcher(cfg, clock, {PAGE: FakeResponse()})
    fetcher.get(PAGE)
    call = session.calls[0]
    assert call["headers"]["User-Agent"] == cfg.user_agent
    assert cfg.user_agent.startswith("pg-misconfig-rag-research/")
    assert call["timeout"] == fetch.DEFAULT_TIMEOUT and call["allow_redirects"] is False
    assert call["auth"] is None
    assert "verify" not in call and "proxies" not in call  # TLS and proxy come from the env


@pytest.mark.parametrize(
    "url",
    [
        "http://www.postgresql.org/docs/16/index.html",
        "https://postgresql.org/docs/16/index.html",
        "https://wiki.postgresql.org/wiki/Tuning",
        "https://www.postgresql.org.example.com/docs/16/",
        "https://user:secret@www.postgresql.org/docs/16/index.html",
        "ftp://www.postgresql.org/pub/",
    ],
)
def test_only_https_www_postgresql_org_is_fetched(cfg, clock, url):
    fetcher, session = make_fetcher(cfg, clock)
    with pytest.raises(UrlNotAllowed):
        fetcher.get(url)
    assert session.calls == []


# ---------------------------------------------------------------- rate limiting


def test_at_most_one_request_per_second(cfg, clock):
    urls = [f"{BASE}/docs/16/page{i}.html" for i in range(5)]
    fetcher, session = make_fetcher(cfg, clock, {u: FakeResponse() for u in urls})
    for url in urls:
        fetcher.get(url)
    starts = [call["started"] for call in session.calls]
    gaps = [later - earlier for earlier, later in zip(starts, starts[1:], strict=False)]
    assert len(starts) == 5
    # 1.0 s after the previous request *finished* (each fake request lasts 0.25 s).
    assert all(gap >= 1.25 - 1e-9 for gap in gaps), gaps
    assert sum(clock.sleeps) == pytest.approx(4.0)


def test_cached_urls_do_not_consume_rate_limit(cfg, clock):
    fetcher, _ = make_fetcher(cfg, clock, {PAGE: FakeResponse()})
    fetcher.get(PAGE)
    slept = list(clock.sleeps)
    for _ in range(10):
        fetcher.get(PAGE)
    assert clock.sleeps == slept


def test_delay_is_floored_at_one_second(tmp_path, clock):
    assert RateLimiter(0.01).min_interval == 1.0
    assert RateLimiter(2.5).min_interval == 2.5
    assert load_config(env={"CRAWL_DELAY_SECONDS": "0.1"}, root=tmp_path).crawl_delay_seconds == 1.0
    limiter = RateLimiter(0.0, clock=clock.time, sleep=clock.sleep)
    limiter.wait()
    limiter.wait()
    assert clock.sleeps == [1.0]


def test_fetchers_share_one_limiter_per_process(cfg):
    one = PoliteFetcher(cfg, session=FakeSession())
    two = PoliteFetcher(cfg, session=FakeSession())
    assert one._limiter is two._limiter
    assert one._limiter.state_file == cfg.raw_dir / fetch.RATE_LIMIT_FILE
    assert one._limiter.min_interval >= 1.0


def test_state_file_extends_the_limit_across_processes(tmp_path, clock):
    state = tmp_path / "raw" / ".rate-limit"
    # Two limiter objects with the same file behave like two processes.
    first = RateLimiter(1.0, state_file=state, clock=clock.time, sleep=clock.sleep)
    second = RateLimiter(1.0, state_file=state, clock=clock.time, sleep=clock.sleep)
    first.wait()
    first.mark()
    clock.now += 0.4
    second.wait()
    assert clock.sleeps == [pytest.approx(0.6)]
    second.mark()
    clock.now += 5
    first.wait()  # long enough ago: no extra sleep
    assert len(clock.sleeps) == 1


def test_redirects_are_followed_by_hand_and_rate_limited(cfg, clock):
    old, new = f"{BASE}/docs/16/old.html", f"{BASE}/docs/16/new.html"
    fetcher, session = make_fetcher(
        cfg,
        clock,
        {old: FakeResponse(301, b"", {"Location": "new.html"}), new: FakeResponse(content=b"new")},
    )
    result = fetcher.get(old)
    assert session.urls == [old, new]
    assert result.url == old and result.final_url == new and result.content == b"new"
    assert session.calls[1]["started"] - session.calls[0]["started"] >= 1.25 - 1e-9
    assert fetcher.get(old).from_cache and len(session.calls) == 2


def test_redirect_to_another_host_is_refused(cfg, clock):
    fetcher, session = make_fetcher(
        cfg, clock, {PAGE: FakeResponse(302, b"", {"Location": "https://example.com/x"})}
    )
    with pytest.raises(UrlNotAllowed):
        fetcher.get(PAGE)
    assert session.urls == [PAGE] and not fetcher.is_cached(PAGE)


# ---------------------------------------------------------------------- errors


def test_server_errors_are_retried_with_backoff(cfg, clock):
    fetcher, session = make_fetcher(
        cfg, clock, {PAGE: [FakeResponse(503), FakeResponse(500), FakeResponse(content=b"fine")]}
    )
    result = fetcher.get(PAGE)
    assert result.ok and result.content == b"fine" and len(session.calls) == 3
    assert fetch.DEFAULT_BACKOFF_SECONDS[0] in clock.sleeps
    assert fetch.DEFAULT_BACKOFF_SECONDS[1] in clock.sleeps


def test_network_errors_are_retried(cfg, clock):
    fetcher, session = make_fetcher(
        cfg,
        clock,
        {PAGE: [requests.ConnectionError("reset"), requests.Timeout("slow"), FakeResponse()]},
    )
    assert fetcher.get(PAGE).ok and len(session.calls) == 3


def test_gives_up_after_limited_retries_and_caches_nothing(cfg, clock):
    fetcher, session = make_fetcher(cfg, clock, {PAGE: FakeResponse(503)}, max_retries=2)
    with pytest.raises(FetchError) as excinfo:
        fetcher.get(PAGE)
    assert not isinstance(excinfo.value, FetchBlocked)
    assert len(session.calls) == 3 and not fetcher.is_cached(PAGE)


def test_persistent_429_raises_blocked_and_honours_retry_after(cfg, clock):
    fetcher, session = make_fetcher(
        cfg, clock, {PAGE: FakeResponse(429, b"", {"Retry-After": "90"})}, max_retries=1
    )
    with pytest.raises(FetchBlocked):
        fetcher.get(PAGE)
    assert len(session.calls) == 2 and 90.0 in clock.sleeps
    assert not fetcher.is_cached(PAGE)


def test_403_raises_blocked_without_retrying(cfg, clock):
    fetcher, session = make_fetcher(cfg, clock, {PAGE: FakeResponse(403, b"forbidden")})
    with pytest.raises(FetchBlocked):
        fetcher.get(PAGE)
    assert len(session.calls) == 1 and not fetcher.is_cached(PAGE)


# ------------------------------------------------------------------ robots.txt

ROBOTS = b"User-agent: *\nDisallow: /list/\nDisallow: /docs/devel/\n"


def test_robots_txt_is_fetched_once_and_obeyed(cfg, clock):
    listing = f"{BASE}/list/pgsql-performance/"
    fetcher, session = make_fetcher(
        cfg,
        clock,
        {fetch.ROBOTS_URL: FakeResponse(content=ROBOTS), PAGE: FakeResponse()},
        respect_robots=True,
    )
    assert fetcher.get(PAGE).ok
    with pytest.raises(UrlNotAllowed, match="robots.txt"):
        fetcher.get(listing)
    assert session.urls == [fetch.ROBOTS_URL, PAGE]  # robots first, the listing never requested
    assert fetcher.allowed_by_robots(PAGE) and not fetcher.allowed_by_robots(listing)

    later, later_session = make_fetcher(cfg, clock, respect_robots=True)
    assert not later.allowed_by_robots(listing)
    assert later_session.calls == []  # robots.txt itself came from the cache


def test_robots_check_can_be_skipped_per_call(cfg, clock):
    listing = f"{BASE}/list/pgsql-performance/"
    fetcher, session = make_fetcher(
        cfg,
        clock,
        {fetch.ROBOTS_URL: FakeResponse(content=ROBOTS), listing: FakeResponse()},
        respect_robots=True,
    )
    assert fetcher.get(listing, check_robots=False).ok
    assert session.urls == [listing]


def test_robots_crawl_delay_slows_the_limiter(cfg, clock):
    robots = b"User-agent: *\nCrawl-delay: 3\nDisallow: /admin/\n"
    fetcher, _ = make_fetcher(
        cfg, clock, {fetch.ROBOTS_URL: FakeResponse(content=robots)}, respect_robots=True
    )
    assert fetcher.allowed_by_robots(PAGE)
    assert fetcher._limiter.min_interval == 3.0
