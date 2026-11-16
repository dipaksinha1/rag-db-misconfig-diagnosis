"""Shared fixtures.

Tests marked ``db`` use the ``db_conn`` fixture, which skips them with a clear
reason when the test database is unreachable. Set ``PGDIAG_REQUIRE_DB=1`` to
turn that skip into a failure (``make test-db`` does this).
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest

from pgdiag import db
from pgdiag.config import get_config

REPO_ROOT = Path(__file__).resolve().parents[1]


def skip_or_fail(reason: str) -> None:
    """Skip the current test, or fail it when the database is required."""
    if os.environ.get("PGDIAG_REQUIRE_DB") == "1":
        pytest.fail(reason)
    pytest.skip(reason)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


class ReconnectingConnection:
    """A test-database connection that reconnects after a server restart.

    The fault-injection tests (T6) restart the server, which kills the shared
    session connection; later tests transparently get a fresh one. ``execute``
    is retried once on a lost connection (tests only run read-only queries here).
    """

    def __init__(self, factory) -> None:
        self._factory = factory
        self._conn = factory()

    def _lost(self) -> bool:
        return self._conn.closed or self._conn.broken

    def _live(self) -> psycopg.Connection:
        if self._lost():
            self._conn = self._factory()
        return self._conn

    def execute(self, *args, **kwargs):
        try:
            return self._live().execute(*args, **kwargs)
        except psycopg.OperationalError:
            if not self._lost():
                raise
            return self._live().execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._live(), name)

    def close(self) -> None:
        self._conn.close()


@pytest.fixture(scope="session")
def db_conn():
    cfg = get_config()
    try:
        conn = ReconnectingConnection(lambda: db.connect(cfg, connect_timeout=3))
    except psycopg.OperationalError as exc:
        skip_or_fail(
            f"test database unreachable at {cfg.db_host}:{cfg.db_port} "
            f"(start it with `make db-up`): {str(exc).splitlines()[0]}"
        )
    yield conn
    conn.close()
