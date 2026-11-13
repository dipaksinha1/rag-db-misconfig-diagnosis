"""Integration tests against the running test database (compose.yaml)."""

from __future__ import annotations

import time
import uuid

import pytest

from pgdiag import db
from pgdiag.config import get_config
from pgdiag.settings import TARGET_SETTINGS

from .conftest import skip_or_fail

pytestmark = pytest.mark.db


def show(conn, name: str) -> str:
    return conn.execute(f"SHOW {name}").fetchone()[0]


def test_server_is_postgresql_16(db_conn):
    assert db_conn.info.server_version // 10000 == 16
    assert db.server_version(db_conn).startswith("16.")


def test_pg_stat_statements_is_queryable(db_conn):
    assert "pg_stat_statements" in show(db_conn, "shared_preload_libraries")

    # Statements are keyed by query id, not text, so look the probe up by its id.
    probe = "SELECT count(*) FROM pg_am"
    plan = db_conn.execute(f"EXPLAIN (VERBOSE, FORMAT JSON) {probe}").fetchone()[0]
    query_id = plan[0]["Query Identifier"]

    def probe_calls() -> int:
        row = db_conn.execute(
            "SELECT sum(calls) FROM pg_stat_statements WHERE queryid = %s", (query_id,)
        ).fetchone()
        return int(row[0] or 0)

    before = probe_calls()
    db_conn.execute(probe)
    assert probe_calls() == before + 1


def test_instrumentation_and_logging_settings_are_active(db_conn):
    assert show(db_conn, "track_io_timing") == "on"
    assert show(db_conn, "logging_collector") == "on"
    assert show(db_conn, "log_min_duration_statement") == "250ms"
    assert show(db_conn, "log_temp_files") == "0"
    assert show(db_conn, "log_checkpoints") == "on"
    assert show(db_conn, "log_autovacuum_min_duration") == "0"


def test_baseline_comes_from_the_mounted_config_file(db_conn):
    assert show(db_conn, "config_file") == "/etc/pgdiag/postgresql.conf"
    source = db_conn.execute(
        "SELECT sourcefile FROM pg_settings WHERE name = 'log_checkpoints'"
    ).fetchone()[0]
    assert source == "/etc/pgdiag/postgresql.conf"


def test_all_target_settings_exist_on_the_server(db_conn):
    rows = db.target_settings(db_conn)
    assert [row["name"] for row in rows] == list(TARGET_SETTINGS)


def test_slow_query_shows_up_in_host_readable_logs(db_conn):
    """A query over log_min_duration_statement must appear in data/pg_logs/ on the host."""
    log_dir = get_config().pg_logs_dir
    if not log_dir.is_dir():
        skip_or_fail(f"{log_dir} does not exist; was the database started from this checkout?")

    marker = f"pgdiag-log-test-{uuid.uuid4().hex}"
    db_conn.execute(f"SELECT pg_sleep(0.3) /* {marker} */")

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        for suffix in ("*.log", "*.json"):
            hits = [p for p in log_dir.glob(suffix) if marker in p.read_text(errors="replace")]
            if not hits:
                break
        else:
            return  # found in both the plain-text and the JSON log
        time.sleep(0.2)
    pytest.fail(f"slow query {marker} not found in both *.log and *.json under {log_dir}")
