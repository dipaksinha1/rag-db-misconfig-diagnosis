"""Integration tests for fault injection (T6) against the running test database.

They change the SERVER configuration (ALTER SYSTEM + restart) of the real test
database, so never run them while a benchmark or `pgdiag-faults measure` is
running. Data is not modified (apply/reset only ANALYZE the static tables).
Each test ends with the baseline restored. The state file goes to a temporary
directory, so a real recorded case is not touched.
"""

from __future__ import annotations

import json
import shutil

import pytest

from pgdiag.config import get_config
from pgdiag.faults import inject
from pgdiag.faults.cases import get_case, load_cases, setting_infos, validate_against_server
from pgdiag.settings import CASE_SETTINGS, TARGET_SETTINGS

from .test_faults_offline import INFOS

pytestmark = pytest.mark.db


def show(srv: inject.PgServer, name: str) -> str:
    with srv.connect() as conn:
        return conn.execute(f"SHOW {name}").fetchone()[0]


@pytest.fixture()
def srv(db_conn, tmp_path_factory):
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not available (restarts use docker compose)")
    real = inject.default_state_path()
    if inject.load_state(real).get("case"):
        pytest.skip(f"a fault case is recorded in {real}; run `pgdiag-faults reset` first")
    server = inject.PgServer()
    if server.auto_conf():
        pytest.skip("ALTER SYSTEM entries present; run `pgdiag-faults reset` first")
    yield server
    # whatever happened, leave the baseline behind
    if server.auto_conf():
        inject.reset(srv=server, state_path=tmp_path_factory.mktemp("cleanup") / "state.json")


def test_cases_validate_against_the_server(db_conn):
    infos = setting_infos(db_conn, TARGET_SETTINGS)
    assert validate_against_server(infos) == []
    contexts = {name: info.context for name, info in infos.items()}
    assert [n for n in CASE_SETTINGS if contexts[n] == "postmaster"] == ["shared_buffers"]
    assert {c.setting: c.restart_required for c in load_cases()} == {
        n: contexts[n] == "postmaster" for n in CASE_SETTINGS
    }


def test_offline_setting_infos_match_the_server(db_conn):
    """The pg_settings rows the offline tests use are the real ones."""
    names = [n for n, i in INFOS.items()]
    real = setting_infos(db_conn, names)
    for name in names:
        fake, got = INFOS[name], real[name]
        assert (fake.vartype, fake.unit, fake.context) == (got.vartype, got.unit, got.context)
        if fake.min_val is not None:
            assert (fake.min_val, fake.max_val) == (got.min_val, got.max_val), name


def _log_records_since(cfg, since: str) -> list[dict]:
    """Server-log records (jsonlog) at or after ``since`` (``YYYY-MM-DD HH:MI:SS.MS``)."""
    prefix = '{"timestamp":"'
    out = []
    for path in sorted(cfg.pg_logs_dir.glob("postgresql-*.json"))[-2:]:
        with path.open(errors="replace") as fh:
            for raw in fh:
                if raw.startswith(prefix) and raw[len(prefix) : len(prefix) + 23] < since:
                    continue  # cheap skip of old lines without parsing them
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if rec.get("timestamp", "") >= since:
                    out.append(rec)
    return out


def test_apply_reload_only_case_then_reset(srv, tmp_path):
    cfg = get_config()
    state_path = tmp_path / "state.json"
    case = get_case("c02_work_mem")
    with srv.connect() as conn:
        since = conn.execute(
            "SELECT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.MS')"
        ).fetchone()[0]
    inject.apply_case(case, srv=srv, state_path=state_path)
    assert show(srv, "work_mem") == "64kB"
    assert inject.load_state(state_path)["case"]["case_id"] == "c02_work_mem"
    assert inject.verify(case, srv=srv, state_path=state_path) == []
    assert srv.auto_conf() == {"work_mem": "64kB"}

    # nothing the harness did is in pg_stat_statements or names the setting in the log
    with srv.connect() as conn:
        tracked = conn.execute(
            "SELECT count(*) FROM pg_stat_statements WHERE query ILIKE '%%alter system%%'"
            " OR query ILIKE '%%pg_reload_conf%%' OR query ILIKE '%%work_mem%%'"
        ).fetchone()[0]
    assert tracked == 0  # apply_case also resets pg_stat_statements
    messages = [r.get("message", "") for r in _log_records_since(cfg, since)]
    assert not [m for m in messages if "work_mem" in m or "c02" in m or "SIGHUP" in m], messages

    # a fix of the broken setting, then undo: back to the case's bad value
    change = inject.set_setting("work_mem", "8MB", srv=srv, state_path=state_path)
    assert change.method == "reload" and change.previous == "64kB"
    assert show(srv, "work_mem") == "8MB"
    inject.undo(srv=srv, state_path=state_path)
    assert show(srv, "work_mem") == "64kB"
    assert inject.verify(case, srv=srv, state_path=state_path) == []

    # an invalid proposal changes nothing
    attempt = inject.try_fix("work_mem", "12 parsecs", srv=srv, state_path=state_path)
    assert not attempt.applied
    assert inject.verify(case, srv=srv, state_path=state_path) == []

    inject.reset(srv=srv, state_path=state_path)
    assert show(srv, "work_mem") == "8MB"
    assert not state_path.exists()
    assert srv.auto_conf() == {}
    assert srv.baseline_problems() == []
    assert inject.verify(None, srv=srv, state_path=state_path) == []


def test_server_side_rejection_leaves_the_case(srv, tmp_path):
    """A value only the server can reject (a string setting's check) is refused cleanly."""
    state_path = tmp_path / "state.json"
    case = get_case("c03_random_page_cost")
    inject.apply_case(case, activation="as_required", srv=srv, state_path=state_path)
    assert show(srv, "random_page_cost") == "100"
    attempt = inject.try_fix("TimeZone", "Mars/Olympus", srv=srv, state_path=state_path)
    assert not attempt.applied and attempt.error_kind == "invalid", attempt
    assert inject.verify(case, srv=srv, state_path=state_path) == []
    inject.reset(srv=srv, state_path=state_path)
    assert show(srv, "random_page_cost") == "1.1"


@pytest.mark.slow
def test_apply_restart_case_then_reset(srv, tmp_path):
    state_path = tmp_path / "state.json"
    case = get_case("c01_shared_buffers")
    inject.apply_case(case, srv=srv, state_path=state_path)
    assert show(srv, "shared_buffers") == "1MB"
    assert inject.verify(case, srv=srv, state_path=state_path) == []
    with srv.connect() as conn:
        pending = conn.execute("SELECT count(*) FROM pg_settings WHERE pending_restart").fetchone()
    assert pending[0] == 0
    # a restart-only fix on top, then undo: back to 1MB
    with inject.applied_setting("shared_buffers", "64MB", srv=srv, state_path=state_path):
        assert show(srv, "shared_buffers") == "64MB"
    assert show(srv, "shared_buffers") == "1MB"
    inject.reset(srv=srv, state_path=state_path)
    assert show(srv, "shared_buffers") == "64MB"
    assert not state_path.exists()
    assert srv.baseline_problems() == []
    assert inject.verify(None, srv=srv, state_path=state_path) == []
