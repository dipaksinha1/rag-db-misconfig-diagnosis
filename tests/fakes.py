"""Test doubles for the HTTP transport and the clock, so fetcher tests never touch the network."""

from __future__ import annotations


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000_000.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeResponse:
    def __init__(self, status=200, content=b"<html>ok</html>", headers=None) -> None:
        self.status_code = status
        self.content = content
        self.headers = {"Content-Type": "text/html; charset=utf-8", **(headers or {})}


class FakeSession:
    """Stands in for requests.Session. ``routes`` maps URL -> response, exception or a list
    of them (consumed one per call; the last one repeats)."""

    def __init__(self, routes=None, clock: FakeClock | None = None, duration=0.25) -> None:
        self.routes = routes or {}
        self.clock = clock
        self.duration = duration
        self.calls: list[dict] = []

    def get(self, url, **kwargs):
        started = self.clock.time() if self.clock else None
        self.calls.append({"url": url, "started": started, **kwargs})
        if self.clock:
            self.clock.now += self.duration  # the request itself takes time
        outcome = self.routes.get(url, FakeResponse(404, b"not found"))
        if isinstance(outcome, list):
            outcome = outcome.pop(0) if len(outcome) > 1 else outcome[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    @property
    def urls(self) -> list[str]:
        return [call["url"] for call in self.calls]
