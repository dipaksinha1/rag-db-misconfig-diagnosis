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


class FakeEmbedder:
    """Deterministic stand-in for the sentence-transformers model (no model, no network).

    A hashed bag of words: each lower-case word adds 1 to one of ``dim`` buckets
    (chosen by md5), and the vector is L2-normalised. Texts sharing words are
    therefore similar, which is enough to test the index plumbing.
    """

    def __init__(self, model_name: str = "fake/bag-of-words", dim: int = 64) -> None:
        self.model_name = model_name
        self.revision = "fake-revision"
        self.dim = dim
        self.max_seq_length = None
        self.query_prefix = ""
        self.document_prefix = ""
        self.calls: list[list[str]] = []

    def _vectors(self, texts):
        import hashlib
        import re

        import numpy as np

        self.calls.append(list(texts))
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            for word in re.findall(r"[a-z0-9_]+", text.lower()):
                bucket = int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dim
                out[row, bucket] += 1.0
            norm = np.linalg.norm(out[row])
            out[row] = out[row] / norm if norm else out[row]
            if not norm:
                out[row, 0] = 1.0
        return out

    def embed_documents(self, texts, batch_size: int = 32):
        return self._vectors(texts)

    def embed_queries(self, texts):
        return self._vectors(texts)
