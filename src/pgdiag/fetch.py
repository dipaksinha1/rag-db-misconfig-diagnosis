"""Polite, cached HTTP fetcher for postgresql.org, shared by every ingestion stage (T1-T3).

Guarantees:

* **Cache**: every response body is stored under ``data/raw/`` keyed by URL,
  next to a small JSON metadata file (URL, final URL, status, content type,
  fetch time, size, sha256). A cached URL is never fetched again; there is no
  "refresh" switch. To force a re-download, delete the two cache files.
* **Rate limit**: at most one request per ``cfg.crawl_delay_seconds`` (floored
  at 1.0 s), measured from the end of one request to the start of the next. The
  limit is shared by every fetcher in the process and, through a lock file in
  ``data/raw/``, by every pgdiag process on the machine. Retries and redirect
  hops count as requests.
* **Scope**: only ``GET https://www.postgresql.org/...``. Redirects are followed
  by hand so that each hop is checked and rate-limited.
* **robots.txt** is fetched once (cached like everything else) and obeyed.
* **Identification**: ``cfg.user_agent`` on every request.
* **Errors**: 5xx, 429 and network errors are retried a few times with
  back-off. 403, or 429 that persists, raises ``FetchBlocked``: callers must
  stop crawling and report it rather than work around it.

TLS verification and the proxy come from the environment (``HTTPS_PROXY``,
``REQUESTS_CA_BUNDLE``); this module never overrides them.

Usage::

    from pgdiag.fetch import PoliteFetcher
    fetcher = PoliteFetcher()                      # uses get_config()
    page = fetcher.get("https://www.postgresql.org/docs/16/index.html")
    page.status, page.from_cache, page.content (bytes), page.text (str)
    mbox = fetcher.get(url, auth=("user", "password"))   # optional basic auth
"""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import requests

from pgdiag.config import MIN_CRAWL_DELAY_SECONDS, Config, get_config

log = logging.getLogger(__name__)

ALLOWED_SCHEME = "https"
ALLOWED_HOSTS: frozenset[str] = frozenset({"www.postgresql.org"})
ROBOTS_URL = "https://www.postgresql.org/robots.txt"

#: Statuses stored in the cache. Anything else is an error and is not cached.
CACHEABLE_STATUSES: frozenset[int] = frozenset({200, 404, 410})
RETRY_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})
REDIRECT_STATUSES: frozenset[int] = frozenset({301, 302, 303, 307, 308})

DEFAULT_TIMEOUT: tuple[float, float] = (15.0, 60.0)  # connect, read (seconds)
DEFAULT_MAX_RETRIES = 3  # extra attempts after the first one
DEFAULT_BACKOFF_SECONDS: tuple[float, ...] = (5.0, 20.0, 60.0)
MAX_RETRY_AFTER_SECONDS = 300.0
MAX_REDIRECTS = 5
RATE_LIMIT_FILE = ".rate-limit"


class FetchError(RuntimeError):
    """The URL could not be fetched (after retries). Nothing was cached."""


class FetchBlocked(FetchError):
    """The server refused us (403, or 429 after back-off). Stop crawling and report it."""


class UrlNotAllowed(FetchError):
    """The URL is outside what this project may fetch (host, scheme or robots.txt)."""


@dataclass(frozen=True)
class FetchResult:
    url: str  # the URL asked for (fragment removed); the cache key
    final_url: str  # after redirects
    status: int
    content: bytes
    content_type: str | None
    fetched_at: str  # ISO 8601 UTC, when the bytes came off the network
    from_cache: bool
    path: Path  # body file in the cache

    @property
    def ok(self) -> bool:
        return self.status == 200

    @property
    def encoding(self) -> str:
        match = re.search(r"charset=([\w.-]+)", self.content_type or "", re.IGNORECASE)
        return match.group(1) if match else "utf-8"

    @property
    def text(self) -> str:
        try:
            return self.content.decode(self.encoding, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")


class RateLimiter:
    """Allow one request per ``min_interval`` seconds.

    ``wait()`` blocks until a request may start; ``mark()`` records that a
    request has just finished, so the interval runs from the end of one request
    to the start of the next. With ``state_file`` the last-request time is also
    kept in that file under an exclusive lock, which extends the limit to
    every process using the same file.
    """

    def __init__(
        self,
        min_interval: float,
        *,
        state_file: Path | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval = max(MIN_CRAWL_DELAY_SECONDS, float(min_interval))
        self.state_file = state_file
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last: float | None = None

    def raise_interval(self, min_interval: float) -> None:
        """Slow down (never speed up), e.g. for a robots.txt Crawl-delay."""
        self.min_interval = max(self.min_interval, float(min_interval))

    def wait(self) -> None:
        with self._lock:
            if self.state_file is None:
                self._wait_since(self._last)
                self._last = self._clock()
                return
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.state_file, "a+", encoding="ascii") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)  # released when the file is closed
                self._wait_since(_latest(self._last, _read_timestamp(handle)))
                self._last = self._clock()
                _write_timestamp(handle, self._last)

    def mark(self) -> None:
        with self._lock:
            self._last = self._clock()
            if self.state_file is None:
                return
            with open(self.state_file, "a+", encoding="ascii") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                _write_timestamp(handle, _latest(self._last, _read_timestamp(handle)))

    def _wait_since(self, last: float | None) -> None:
        if last is None:
            return
        # min() guards against a timestamp from the future (clock step, stale file).
        remaining = min(last + self.min_interval - self._clock(), self.min_interval)
        if remaining > 0:
            self._sleep(remaining)


def _latest(a: float | None, b: float | None) -> float | None:
    values = [v for v in (a, b) if v is not None]
    return max(values) if values else None


def _read_timestamp(handle) -> float | None:
    handle.seek(0)
    try:
        return float(handle.read().strip())
    except ValueError:
        return None


def _write_timestamp(handle, value: float | None) -> None:
    if value is None:
        return
    handle.seek(0)
    handle.truncate()
    handle.write(repr(value))
    handle.flush()


_limiters: dict[Path | None, RateLimiter] = {}
_limiters_lock = threading.Lock()


def shared_rate_limiter(min_interval: float, state_file: Path | None) -> RateLimiter:
    """The process-wide limiter for ``state_file``; its interval only ever grows."""
    with _limiters_lock:
        limiter = _limiters.get(state_file)
        if limiter is None:
            limiter = _limiters[state_file] = RateLimiter(min_interval, state_file=state_file)
        else:
            limiter.raise_interval(min_interval)
        return limiter


def normalize_url(url: str) -> str:
    """Strip the fragment and surrounding whitespace; the result is the cache key."""
    return urldefrag(url.strip()).url


def check_url_allowed(url: str, allowed_hosts: Iterable[str] = ALLOWED_HOSTS) -> None:
    parts = urlsplit(url)
    if parts.scheme != ALLOWED_SCHEME or parts.hostname not in set(allowed_hosts):
        raise UrlNotAllowed(
            f"refusing to fetch {url!r}: only https:// URLs on {sorted(allowed_hosts)} are allowed"
        )
    if parts.username or parts.password:
        raise UrlNotAllowed("credentials must be passed with auth=, not inside the URL")


def cache_paths(raw_dir: Path, url: str) -> tuple[Path, Path]:
    """Return ``(body_path, meta_path)`` for a URL.

    Layout: ``<raw_dir>/<host>/<first path segment>/<readable slug>.<url hash>``
    plus the same name with ``.meta.json``. The hash makes the name unique; the
    slug only makes it readable.
    """
    url = normalize_url(url)
    parts = urlsplit(url)
    segments = [s for s in parts.path.split("/") if s]
    bucket = _slug(segments[0]) if len(segments) > 1 else "_root"
    rest = "/".join(segments[1:] if len(segments) > 1 else segments)
    if parts.query:
        rest += "?" + parts.query
    slug = _slug(rest)[:100] or "index"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    body = raw_dir / (parts.hostname or "unknown-host") / bucket / f"{slug}.{digest}"
    return body, body.with_name(body.name + ".meta.json")


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")


class PoliteFetcher:
    """See the module docstring. One instance per stage is enough; instances share the limiter."""

    def __init__(
        self,
        cfg: Config | None = None,
        *,
        session: Any | None = None,
        rate_limiter: RateLimiter | None = None,
        allowed_hosts: Iterable[str] = ALLOWED_HOSTS,
        respect_robots: bool = True,
        timeout: tuple[float, float] = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_seconds: tuple[float, ...] = DEFAULT_BACKOFF_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cfg = cfg or get_config()
        self.raw_dir = self.cfg.raw_dir
        self.user_agent = self.cfg.user_agent
        self.allowed_hosts = frozenset(allowed_hosts)
        self.respect_robots = respect_robots
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self._sleep = sleep
        self._session = session
        self._limiter = rate_limiter or shared_rate_limiter(
            self.cfg.crawl_delay_seconds, self.raw_dir / RATE_LIMIT_FILE
        )
        self._robots: RobotFileParser | None = None
        self.network_requests = 0  # HTTP requests actually sent by this instance
        self.cache_hits = 0

    # ------------------------------------------------------------------ cache

    def is_cached(self, url: str) -> bool:
        body, meta = cache_paths(self.raw_dir, url)
        return body.is_file() and meta.is_file()

    def read_cache(self, url: str) -> FetchResult | None:
        url = normalize_url(url)
        body_path, meta_path = cache_paths(self.raw_dir, url)
        if not (body_path.is_file() and meta_path.is_file()):
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return FetchResult(
            url=url,
            final_url=meta.get("final_url", url),
            status=int(meta["status"]),
            content=body_path.read_bytes(),
            content_type=meta.get("content_type"),
            fetched_at=meta["fetched_at"],
            from_cache=True,
            path=body_path,
        )

    def _write_cache(self, url: str, final_url: str, response: Any, redirects: list[str]) -> Path:
        body_path, meta_path = cache_paths(self.raw_dir, url)
        body_path.parent.mkdir(parents=True, exist_ok=True)
        content: bytes = response.content
        meta = {
            "url": url,
            "final_url": final_url,
            "redirects": redirects,
            "status": response.status_code,
            "content_type": response.headers.get("Content-Type"),
            "last_modified": response.headers.get("Last-Modified"),
            "etag": response.headers.get("ETag"),
            "fetched_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "user_agent": self.user_agent,
        }
        # Body first, metadata last: an entry counts as cached only once both exist.
        _atomic_write(body_path, content)
        _atomic_write(meta_path, json.dumps(meta, indent=2, sort_keys=True).encode("utf-8"))
        return body_path

    # ----------------------------------------------------------------- robots

    def _load_robots(self) -> RobotFileParser:
        if self._robots is None:
            parser = RobotFileParser(ROBOTS_URL)
            result = self.get(ROBOTS_URL, check_robots=False)
            # A missing robots.txt allows everything (RFC 9309).
            parser.parse(result.text.splitlines() if result.ok else [])
            delay = parser.crawl_delay(self.user_agent)
            if delay:
                self._limiter.raise_interval(float(delay))
            self._robots = parser
        return self._robots

    def allowed_by_robots(self, url: str) -> bool:
        return self._load_robots().can_fetch(self.user_agent, normalize_url(url))

    # ------------------------------------------------------------------ fetch

    def get(
        self,
        url: str,
        *,
        auth: tuple[str, str] | None = None,
        check_robots: bool | None = None,
    ) -> FetchResult:
        """Return the cached copy of ``url`` or fetch, cache and return it.

        200, 404 and 410 responses are cached and returned (check ``.ok``).
        Raises ``UrlNotAllowed``, ``FetchBlocked`` or ``FetchError`` otherwise.
        ``auth`` is an optional ``(user, password)`` pair for HTTP basic auth; it
        is never written to the cache.
        """
        url = normalize_url(url)
        check_url_allowed(url, self.allowed_hosts)
        cached = self.read_cache(url)
        if cached is not None:
            self.cache_hits += 1
            return cached
        if self.respect_robots if check_robots is None else check_robots:
            if not self.allowed_by_robots(url):
                raise UrlNotAllowed(f"robots.txt disallows {url}")

        current = url
        redirects: list[str] = []
        while True:
            response = self._request_with_retries(current, auth)
            status = response.status_code
            if status in REDIRECT_STATUSES:
                target = normalize_url(urljoin(current, response.headers.get("Location", "")))
                check_url_allowed(target, self.allowed_hosts)
                redirects.append(target)
                if len(redirects) > MAX_REDIRECTS:
                    raise FetchError(f"too many redirects for {url}")
                current = target
                continue
            if status in CACHEABLE_STATUSES:
                path = self._write_cache(url, current, response, redirects)
                result = self.read_cache(url)
                assert result is not None, path
                return FetchResult(**{**result.__dict__, "from_cache": False})
            if status in (401, 403):
                raise FetchBlocked(f"HTTP {status} for {current}; not retrying")
            raise FetchError(f"HTTP {status} for {current}")

    def _request_with_retries(self, url: str, auth: tuple[str, str] | None) -> Any:
        last_problem = "no attempt made"
        last_status: int | None = None
        retry_after = 0.0
        for attempt in range(self.max_retries + 1):
            if attempt:
                backoff = self.backoff_seconds[min(attempt - 1, len(self.backoff_seconds) - 1)]
                pause = max(backoff, retry_after)
                log.warning(
                    "retry %d/%d for %s in %.0f s (%s)",
                    attempt,
                    self.max_retries,
                    url,
                    pause,
                    last_problem,
                )
                self._sleep(pause)
            retry_after = 0.0
            self._limiter.wait()
            try:
                self.network_requests += 1
                response = self._get_session().get(
                    url,
                    headers={"User-Agent": self.user_agent, "Accept": "*/*"},
                    timeout=self.timeout,
                    allow_redirects=False,
                    auth=auth,
                )
            except requests.RequestException as exc:
                last_problem, last_status = f"{type(exc).__name__}: {exc}", None
                continue
            finally:
                self._limiter.mark()
            if response.status_code not in RETRY_STATUSES:
                return response
            last_problem, last_status = f"HTTP {response.status_code}", response.status_code
            retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
        if last_status == 429:
            raise FetchBlocked(
                f"HTTP 429 (rate limited) for {url} after {self.max_retries} retries"
            )
        raise FetchError(f"giving up on {url} after {self.max_retries} retries: {last_problem}")

    def _get_session(self) -> Any:
        if self._session is None:
            # trust_env stays True: proxy and CA bundle come from the environment.
            self._session = requests.Session()
        return self._session


def _retry_after_seconds(value: str | None) -> float:
    try:
        return min(max(float(value or 0), 0.0), MAX_RETRY_AFTER_SECONDS)
    except ValueError:
        return 0.0  # HTTP-date form: fall back to our own back-off


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    tmp.write_bytes(data)
    os.replace(tmp, path)
