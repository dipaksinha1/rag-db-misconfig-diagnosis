"""Apply and reset fault cases, and set/undo arbitrary settings on top of a case (T6).

Layers of configuration (lowest to highest precedence):

1. ``docker/postgres/postgresql.conf``: the healthy baseline (never edited here);
2. ``postgresql.auto.conf`` (``ALTER SYSTEM``): the case's bad value, plus any
   fixes stacked on top with :func:`set_setting`.

``apply_case`` always starts from a clean slate in one step:
``ALTER SYSTEM RESET ALL`` + ``ALTER SYSTEM SET <setting> = <bad>`` + server
RESTART, then ANALYZE of the static workload tables, then verification that the
running value is the bad value and the other nine target settings are at
baseline. By default every case is activated with a restart, not only
``shared_buffers`` (``activation="restart"``): a reload writes ``received SIGHUP``
and ``parameter "work_mem" changed to "64kB"`` into the server log, which the
symptom collector (T7) reads, and a restart that only one case had would itself
identify that case. With a restart (and an ANALYZE) for every case, all six
leave the same trace. ``activation="as_required"`` reloads where possible.

``reset`` = ``ALTER SYSTEM RESET ALL`` + restart + verify all ten target settings
at baseline (and nothing left in ``postgresql.auto.conf``, no pending restart) +
ANALYZE + clear the state file. It does NOT reset pg_stat_statements or the
cumulative statistics (T7/T10 decide what they need). If the server cannot start
any more (e.g. a restart-only fix it cannot run with), :func:`emergency_reset`
empties ``postgresql.auto.conf`` with a one-off container and starts it again.

The applied case is recorded OUTSIDE the database, in ``data/faults/state.json``.
Nothing written to the database names the case: the admin connections use
``application_name = pgdiag-admin`` and switch off pg_stat_statements tracking
and statement logging for themselves, so ALTER SYSTEM / ANALYZE do not show up
in pg_stat_statements or the server log.

T10 uses :func:`try_fix` (or :func:`set_setting` / :func:`undo`, or the
:func:`applied_setting` context manager) to try a proposed fix on top of the
broken case and take it back, restoring exactly the previous ALTER SYSTEM layer
(the case's bad value when the fix targeted the broken setting itself).
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

import psycopg
from psycopg import sql

from pgdiag import db
from pgdiag.config import Config, get_config
from pgdiag.faults import units
from pgdiag.faults.cases import (
    FaultCase,
    baseline_config_values,
    get_case,
    load_cases,
    setting_infos,
)
from pgdiag.settings import TARGET_SETTINGS

ADMIN_APPLICATION_NAME = "pgdiag-admin"
STATE_VERSION = 1
RELOAD_TIMEOUT_S = 20.0
# set_setting refuses restart-only memory values above this share of the container's memory
# limit: a server that cannot start again cannot be fixed with ALTER SYSTEM.
MAX_RESTART_MEMORY_FRACTION = 0.75
DEFAULT_MEMORY_LIMIT_BYTES = 256 * 2**20  # compose.yaml's PGDIAG_DB_MEM_LIMIT default (256m)

# Settings a proposed fix may not touch: changing them would cut the harness off from
# the server (connections, paths, preload libraries, logging destination) rather than
# tune it. Prefixes end with "_" or ".".
PROTECTED_SETTINGS = frozenset(
    {
        "listen_addresses",
        "port",
        "max_connections",
        "superuser_reserved_connections",
        "reserved_connections",
        "unix_socket_directories",
        "unix_socket_group",
        "unix_socket_permissions",
        "config_file",
        "hba_file",
        "ident_file",
        "data_directory",
        "external_pid_file",
        "shared_preload_libraries",
        "session_preload_libraries",
        "local_preload_libraries",
        "logging_collector",
        "log_destination",
        "log_directory",
        "log_filename",
        "password_encryption",
        "default_transaction_read_only",
        "ssl",
    }
)
PROTECTED_PREFIXES = ("ssl_",)
_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)?$")


class FaultError(RuntimeError):
    pass


def default_state_path(cfg: Config | None = None) -> Path:
    cfg = cfg or get_config()
    return cfg.data_dir / "faults" / "state.json"


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# state file (outside the database)


def empty_state() -> dict:
    return {
        "version": STATE_VERSION,
        "case": None,
        "overrides": [],
        "rejected": [],
        "updated_at": None,
    }


def load_state(path: Path) -> dict:
    """The state file's contents; an empty state when it does not exist."""
    path = Path(path)
    if not path.exists():
        return empty_state()
    data = json.loads(path.read_text())
    if data.get("version") != STATE_VERSION:
        raise FaultError(f"{path}: unsupported state version {data.get('version')!r}")
    data.setdefault("overrides", [])
    data.setdefault("rejected", [])
    return data


def save_state(path: Path, state: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {**state, "updated_at": _now()}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    os.replace(tmp, path)


def clear_state(path: Path) -> None:
    Path(path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# server control (real implementation + the interface tests fake)


class ServerControl(Protocol):
    def info(self, name: str) -> units.SettingInfo: ...  # raises KeyError if unknown
    def running(self, name: str) -> str: ...  # pg_settings.setting in a NEW session
    def auto_conf(self) -> dict[str, str]: ...  # ALTER SYSTEM layer: name -> value
    def alter_system(self, name: str, value: str | None) -> None: ...  # None = RESET
    def reset_all(self) -> None: ...
    def reload(self) -> None: ...
    def restart(self) -> None: ...
    def analyze(self) -> None: ...
    def sleep(self, seconds: float) -> None: ...
    def baseline_problems(self) -> list[str]: ...  # pending restarts, other config files
    def memory_limit_bytes(self) -> int | None: ...
    def reset_statement_stats(self) -> None: ...  # pg_stat_statements_reset()


class PgServer:
    """The test database in Docker (``ServerControl`` for real)."""

    def __init__(self, cfg: Config | None = None, *, dbname: str | None = None) -> None:
        self.cfg = cfg or get_config()
        self.dbname = dbname
        self._infos: dict[str, units.SettingInfo] = {}

    def connect(self) -> psycopg.Connection:
        kwargs = dict(self.cfg.db_connect_kwargs)
        if self.dbname:
            kwargs["dbname"] = self.dbname
        conn = psycopg.connect(
            **kwargs, autocommit=True, connect_timeout=5, application_name=ADMIN_APPLICATION_NAME
        )
        # Keep this session's statements out of pg_stat_statements and the server log.
        conn.execute("SET pg_stat_statements.track = 'none'")
        conn.execute("SET log_min_duration_statement = -1")
        conn.execute("SET log_statement = 'none'")
        conn.execute("SET log_temp_files = -1")  # ANALYZE under a tiny work_mem spills
        return conn

    def info(self, name: str) -> units.SettingInfo:
        """pg_settings metadata; names are case-insensitive (``timezone`` is ``TimeZone``)."""
        key = name.lower()
        if key not in self._infos:
            with self.connect() as conn:
                row = conn.execute(
                    "SELECT name FROM pg_settings WHERE lower(name) = %s", (key,)
                ).fetchone()
                found = setting_infos(conn, [row[0]]) if row else {}
            if not found:
                raise KeyError(name)
            self._infos[key] = next(iter(found.values()))
        return self._infos[key]

    def running(self, name: str) -> str:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT setting FROM pg_settings WHERE lower(name) = lower(%s)", (name,)
            )
            value = row.fetchone()
        if value is None:
            raise KeyError(name)
        return value[0]

    def auto_conf(self) -> dict[str, str]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT name, setting FROM pg_file_settings "
                "WHERE sourcefile LIKE '%%postgresql.auto.conf' ORDER BY seqno"
            ).fetchall()
        return {name: setting for name, setting in rows}

    def alter_system(self, name: str, value: str | None) -> None:
        with self.connect() as conn:
            if value is None:
                conn.execute(sql.SQL("ALTER SYSTEM RESET {}").format(sql.Identifier(name)))
            else:
                conn.execute(
                    sql.SQL("ALTER SYSTEM SET {} = {}").format(
                        sql.Identifier(name), sql.Literal(str(value))
                    )
                )

    def reset_all(self) -> None:
        with self.connect() as conn:
            conn.execute("ALTER SYSTEM RESET ALL")

    def reload(self) -> None:
        with self.connect() as conn:
            conn.execute("SELECT pg_reload_conf()")

    def restart(self) -> None:
        db.restart_server(self.cfg)

    def analyze(self) -> None:
        from pgdiag.workload.server import analyze_static_tables

        with self.connect() as conn:
            analyze_static_tables(conn)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def baseline_problems(self) -> list[str]:
        from pgdiag.workload import server

        with self.connect() as conn:
            return server.baseline_problems(conn)

    def memory_limit_bytes(self) -> int | None:
        return db.container_info(self.cfg).get("mem_limit_bytes") or None

    def reset_statement_stats(self) -> None:
        with self.connect() as conn:
            conn.execute("SELECT pg_stat_statements_reset()")


# ---------------------------------------------------------------------------
# helpers


def wait_for_value(
    srv: ServerControl, name: str, expected: str, *, timeout: float = RELOAD_TIMEOUT_S
) -> str:
    """Poll (new sessions) until ``name`` runs with ``expected``. Returns the raw setting."""
    info = srv.info(name)
    want = units.normalize(expected, info)
    waited = 0.0
    while True:
        raw = srv.running(name)
        if units.values_equal(units.normalize(raw, info), want):
            return raw
        if waited >= timeout:
            raise FaultError(
                f"{name} runs with {raw!r}{_unit(info)}, expected {expected!r} "
                f"(after {waited:.0f}s)"
            )
        srv.sleep(0.25)
        waited += 0.25


def check_targets(
    srv: ServerControl, expected: dict[str, str], *, names=TARGET_SETTINGS
) -> list[str]:
    """Differences between the running target settings and ``expected`` (name -> value)."""
    problems = []
    for name in names:
        info = srv.info(name)
        raw = srv.running(name)
        if not units.values_equal(
            units.normalize(raw, info), units.normalize(expected[name], info)
        ):
            problems.append(f"{name} = {raw}{_unit(info)}, expected {expected[name]}")
    return problems


def expected_values(case: FaultCase | None, baseline: dict[str, str]) -> dict[str, str]:
    """The ten target settings as they should run with ``case`` applied (None: baseline)."""
    values = dict(baseline)
    if case is not None:
        values[case.setting] = case.bad_value
    return values


def _unit(info: units.SettingInfo) -> str:
    return f" {info.unit}" if info.unit else ""


def _activate(srv: ServerControl, info: units.SettingInfo, *, force_restart: bool = False) -> str:
    if force_restart or info.restart_required:
        srv.restart()
        return "restart"
    srv.reload()
    return "reload"


# ---------------------------------------------------------------------------
# apply / reset / verify


@dataclass
class Outcome:
    action: str
    case_id: str | None
    seconds: float
    details: dict

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "case_id": self.case_id,
            "seconds": self.seconds,
            **self.details,
        }


def _case_record(case: FaultCase) -> dict:
    return {
        "case_id": case.case_id,
        "setting": case.setting,
        "bad_value": case.bad_value,
        "good_value": case.good_value,
        "restart_required": case.restart_required,
        "needs_analyze": case.needs_analyze,
    }


def apply_case(
    case: FaultCase | str,
    *,
    activation: str = "restart",
    reset_statements: bool = True,
    srv: ServerControl | None = None,
    state_path: Path | None = None,
    cfg: Config | None = None,
    progress: Callable[[str], None] | None = None,
) -> Outcome:
    """Break the server with ``case`` (starting from the baseline), verify, record the state.

    ``activation``: ``"restart"`` (default, every case restarts: uniform log trace) or
    ``"as_required"`` (reload unless the setting needs a restart). The static tables
    are always ANALYZEd afterwards (needed for ``default_statistics_target``; done for
    every case so they all look alike). ``reset_statements`` (default) empties
    pg_stat_statements at the end, so it only holds what runs on the broken server.
    """
    if activation not in ("restart", "as_required"):
        raise ValueError(f"activation must be 'restart' or 'as_required', not {activation!r}")
    cfg = cfg or get_config()
    srv = srv or PgServer(cfg)
    state_path = state_path or default_state_path(cfg)
    say = progress or (lambda m: None)
    if isinstance(case, str):
        case = get_case(case)
    baseline = baseline_config_values(cfg)
    info = srv.info(case.setting)
    units.validate(case.bad_value, info)
    t0 = time.monotonic()
    # Record intent first: if we die half-way, `reset` still knows something was applied.
    save_state(state_path, {**empty_state(), "case": {**_case_record(case), "status": "applying"}})
    srv.reset_all()
    srv.alter_system(case.setting, case.bad_value)
    method = _activate(srv, info, force_restart=activation == "restart")
    say(f"ALTER SYSTEM RESET ALL; ALTER SYSTEM SET {case.setting} = {case.bad_value}; {method}")
    if method == "reload":
        # Other settings may have been left by a previous case: RESET ALL + reload restores
        # them, except restart-only ones, which check_targets would report.
        wait_for_value(srv, case.setting, case.bad_value)
    problems = check_targets(srv, expected_values(case, baseline))
    if problems:
        raise FaultError(f"{case.case_id} not applied as expected: " + "; ".join(problems))
    srv.analyze()
    say("analyzed the static workload tables")
    if reset_statements:
        # pg_stat_statements survives restarts and may hold older harness statements
        # (e.g. T5's probe ran `alter system set shared_buffers='1MB'` in tracked sessions).
        srv.reset_statement_stats()
        say("pg_stat_statements reset")
    raw = srv.running(case.setting)
    save_state(
        state_path,
        {
            **empty_state(),
            "case": {
                **_case_record(case),
                "status": "applied",
                "activation": method,
                "applied_at": _now(),
                "running_setting": raw,
            },
        },
    )
    say(f"{case.case_id}: {case.setting} running with {raw}{_unit(info)}")
    return Outcome(
        "apply",
        case.case_id,
        time.monotonic() - t0,
        {
            "setting": case.setting,
            "bad_value": case.bad_value,
            "running_setting": raw,
            "activation": method,
        },
    )


def reset(
    *,
    srv: ServerControl | None = None,
    state_path: Path | None = None,
    cfg: Config | None = None,
    analyze: bool = True,
    progress: Callable[[str], None] | None = None,
) -> Outcome:
    """Back to the baseline: RESET ALL, restart, verify the ten targets, ANALYZE, clear state."""
    cfg = cfg or get_config()
    srv = srv or PgServer(cfg)
    state_path = state_path or default_state_path(cfg)
    say = progress or (lambda m: None)
    t0 = time.monotonic()
    previous = load_state(state_path)
    srv.reset_all()
    say("ALTER SYSTEM RESET ALL; restart")
    srv.restart()
    problems = check_targets(srv, baseline_config_values(cfg))
    leftovers = srv.auto_conf()
    if leftovers:
        problems.append(f"postgresql.auto.conf still sets {sorted(leftovers)}")
    problems.extend(srv.baseline_problems())
    if problems:
        raise FaultError("reset did not restore the baseline: " + "; ".join(problems))
    if analyze:
        srv.analyze()
        say("analyzed the static workload tables")
    clear_state(state_path)
    say("baseline configuration verified (all ten target settings, no ALTER SYSTEM entries)")
    prev_case = (previous.get("case") or {}).get("case_id")
    return Outcome("reset", prev_case, time.monotonic() - t0, {"verified": True})


def emergency_reset(
    cfg: Config | None = None, *, progress: Callable[[str], None] | None = None
) -> None:
    """Empty ``postgresql.auto.conf`` while the server is DOWN, then start it again.

    For the case where a restart-only change keeps the server from starting, so
    that ``ALTER SYSTEM RESET ALL`` is impossible. Uses a one-off container on the
    same data volume. Follow with :func:`reset` to verify and clear the state file.
    """
    cfg = cfg or get_config()
    say = progress or (lambda m: None)
    compose = db.compose_command(cfg)
    subprocess.run([*compose, "stop", "db"], check=True, capture_output=True, timeout=180)
    script = (
        'f="$PGDATA/postgresql.auto.conf"; '
        "printf '# Do not edit this file manually!\\n"
        '# It will be overwritten by the ALTER SYSTEM command.\\n\' > "$f"; '
        'chown postgres:postgres "$f"; chmod 600 "$f"'
    )
    subprocess.run(
        [*compose, "run", "--rm", "--no-deps", "-T", "--entrypoint", "bash", "db", "-c", script],
        check=True,
        capture_output=True,
        timeout=180,
    )
    say("emptied postgresql.auto.conf; starting the server")
    subprocess.run([*compose, "up", "-d", "db"], check=True, capture_output=True, timeout=180)
    db.wait_until_ready(cfg, timeout=120)


def verify(
    case: FaultCase | None = None,
    *,
    srv: ServerControl | None = None,
    state_path: Path | None = None,
    cfg: Config | None = None,
) -> list[str]:
    """Problems with the running server for ``case`` (None: the state file's case, or baseline).

    Checks the ten target settings against bad/baseline values (plus stacked fixes),
    the ALTER SYSTEM layer against the case + recorded fixes, and the state file.
    """
    cfg = cfg or get_config()
    srv = srv or PgServer(cfg)
    state = load_state(state_path or default_state_path(cfg))
    st_case = state.get("case")
    if case is None and st_case:
        case = get_case(st_case["case_id"])
    problems: list[str] = []
    if case is not None and (not st_case or st_case.get("case_id") != case.case_id):
        problems.append(
            f"state file records {st_case.get('case_id') if st_case else 'no case'}, "
            f"not {case.case_id}"
        )
    if st_case and st_case.get("status") != "applied":
        problems.append(f"state file: case {st_case['case_id']} is {st_case.get('status')}")
    expected = expected_values(case, baseline_config_values(cfg))
    overrides = state.get("overrides", [])
    for o in overrides:
        if o["name"] in expected:
            expected[o["name"]] = o["value"]
    problems.extend(check_targets(srv, expected))
    want_auto = {case.setting: case.bad_value} if case else {}
    for o in overrides:
        want_auto[o["name"]] = o["value"]
    auto = srv.auto_conf()
    if set(auto) != set(want_auto):
        problems.append(f"ALTER SYSTEM sets {sorted(auto)}, expected {sorted(want_auto)}")
    else:
        for name, value in want_auto.items():
            info = srv.info(name)
            if not units.same_value(auto[name], value, info):
                problems.append(f"ALTER SYSTEM sets {name} = {auto[name]}, expected {value}")
    return problems


# ---------------------------------------------------------------------------
# set / undo (T10: try a proposed fix on top of the broken case)


class InvalidFix(units.InvalidValue):
    """A proposed change that is rejected before anything is changed."""


@dataclass
class Change:
    name: str
    value: str
    previous: str | None  # the ALTER SYSTEM value before (None: not set there)
    before_running: str  # pg_settings.setting before the change
    method: str  # reload | restart
    analyzed: bool
    running: str = ""  # pg_settings.setting after the change

    def to_dict(self) -> dict:
        return asdict(self)


def clean_name(name: str) -> str:
    """Normalise a setting name as an LLM might write it (``Work_Mem``, ``"work_mem"``)."""
    return str(name).strip().strip("'\"`").strip().lower()


def clean_value(value) -> str:
    """Normalise a value as an LLM might write it (quotes, surrounding blanks, a trailing ';')."""
    if isinstance(value, bool):
        return "on" if value else "off"
    text = str(value).strip().rstrip(";").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"`":
        text = text[1:-1].strip()
    return text


def check_fix(srv: ServerControl, name: str, value: str) -> tuple[units.SettingInfo, str | float]:
    """Validate a proposed ``name = value`` without touching the server's configuration.

    Raises :class:`InvalidFix` (unknown name, protected or read-only setting, value
    the server would reject, restart-only memory value too large for the container).
    """
    if not _NAME_RE.match(name):
        raise InvalidFix(f"{name!r} is not a valid setting name")
    try:
        info = srv.info(name)
    except KeyError:
        raise InvalidFix(f"unknown setting {name!r}") from None
    if info.context == "internal":
        raise InvalidFix(f"{name} cannot be changed (internal, read-only setting)")
    if name in PROTECTED_SETTINGS or name.startswith(PROTECTED_PREFIXES):
        raise InvalidFix(f"{name} is not changed by the fix tester (it would break the harness)")
    if value == "":
        raise InvalidFix(f"{name}: empty value")
    try:
        norm = units.validate(value, info)
    except units.InvalidValue as exc:
        raise InvalidFix(str(exc)) from None
    if info.restart_required and isinstance(norm, float):
        size = units.to_bytes(norm, info)
        limit = srv.memory_limit_bytes() or DEFAULT_MEMORY_LIMIT_BYTES
        if size is not None and size > MAX_RESTART_MEMORY_FRACTION * limit:
            raise InvalidFix(
                f"{name} = {value} exceeds {MAX_RESTART_MEMORY_FRACTION:.0%} of the container's "
                f"memory limit ({limit / 2**20:.0f} MB); the server might not start again, so "
                "it is not applied"
            )
    return info, norm


def set_setting(
    name: str,
    value,
    *,
    srv: ServerControl | None = None,
    state_path: Path | None = None,
    cfg: Config | None = None,
    analyze: bool | None = None,
) -> Change:
    """``ALTER SYSTEM SET name = value`` on top of whatever is applied, activate, verify.

    The change is pushed onto a stack in the state file; :func:`undo` pops it and
    restores the previous ALTER SYSTEM value (e.g. the case's bad value) or removes
    the entry. Restarts for ``postmaster`` settings, reloads otherwise. ``analyze``
    (default: only for ``default_statistics_target``) re-ANALYZEs the static tables.
    Raises :class:`InvalidFix` before changing anything for a proposal that cannot or
    must not be applied. If the server refuses the value or it does not become
    active, the previous value is restored and :class:`FaultError` raised.
    """
    cfg = cfg or get_config()
    srv = srv or PgServer(cfg)
    state_path = state_path or default_state_path(cfg)
    name, value = clean_name(name), clean_value(value)
    info, _ = check_fix(srv, name, value)
    state = load_state(state_path)
    previous = srv.auto_conf().get(name)
    before = srv.running(name)
    try:
        srv.alter_system(name, value)
    except psycopg.Error as exc:
        # ALTER SYSTEM validates the value and writes nothing when it refuses it.
        raise InvalidFix(f"the server rejected {name} = {value}: {str(exc).strip()}") from None
    try:
        method = _activate(srv, info)
        running = wait_for_value(srv, name, value)
    except Exception as exc:
        # e.g. the server refused the value on reload: put the previous layer back
        try:
            srv.alter_system(name, previous)
            _activate(srv, info)
        except Exception as exc2:
            raise FaultError(
                f"{name} = {value} did not take effect ({exc}) and restoring the previous "
                f"value failed ({exc2}); run `pgdiag-faults reset --emergency`"
            ) from exc2
        raise FaultError(f"{name} = {value} did not take effect ({exc}); restored") from exc
    do_analyze = (name == "default_statistics_target") if analyze is None else analyze
    if do_analyze:
        srv.analyze()
    change = Change(name, value, previous, before, method, do_analyze, running)
    state.setdefault("overrides", []).append(change.to_dict())
    save_state(state_path, state)
    return change


def undo(
    *,
    srv: ServerControl | None = None,
    state_path: Path | None = None,
    cfg: Config | None = None,
) -> Change | None:
    """Take back the most recent :func:`set_setting` (None when there is nothing to undo)."""
    cfg = cfg or get_config()
    srv = srv or PgServer(cfg)
    state_path = state_path or default_state_path(cfg)
    state = load_state(state_path)
    if not state.get("overrides"):
        return None
    change = Change(**state["overrides"][-1])
    info = srv.info(change.name)
    srv.alter_system(change.name, change.previous)
    _activate(srv, info)
    wait_for_value(srv, change.name, change.before_running)
    if change.analyzed:
        srv.analyze()
    state["overrides"].pop()
    save_state(state_path, state)
    return change


def undo_all(**kwargs) -> list[Change]:
    out = []
    while (change := undo(**kwargs)) is not None:
        out.append(change)
    return out


@contextlib.contextmanager
def applied_setting(name: str, value, **kwargs) -> Iterator[Change]:
    """``with applied_setting("work_mem", "8MB"): run_benchmark(...)`` -> undone afterwards."""
    change = set_setting(name, value, **kwargs)
    try:
        yield change
    finally:
        undo(**kwargs)


@dataclass
class FixAttempt:
    """What :func:`try_fix` did with one proposal (JSON-ready via ``to_dict``)."""

    name: str  # as proposed
    value: str  # as proposed
    applied: bool
    error: str | None = None
    error_kind: str | None = None  # invalid (rejected, nothing changed) | failed (restored)
    change: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def try_fix(
    name: str,
    value,
    *,
    srv: ServerControl | None = None,
    state_path: Path | None = None,
    cfg: Config | None = None,
) -> FixAttempt:
    """:func:`set_setting` that never raises for a bad proposal (T10's entry point).

    An invalid proposal (unknown name, bad value or unit, out of range, protected
    setting) is rejected before anything changes; a value the server refuses on
    activation is rolled back. Either way the error is returned and appended to
    ``rejected`` in the state file, and the server keeps its previous configuration.
    On success, call :func:`undo` after measuring.
    """
    cfg = cfg or get_config()
    srv = srv or PgServer(cfg)
    state_path = state_path or default_state_path(cfg)
    raw_name, raw_value = str(name), str(value)
    try:
        change = set_setting(name, value, srv=srv, state_path=state_path, cfg=cfg)
    except (InvalidFix, units.InvalidValue, FaultError) as exc:
        kind = "failed" if isinstance(exc, FaultError) else "invalid"
        state = load_state(state_path)
        state["rejected"].append(
            {"name": raw_name, "value": raw_value, "error": str(exc), "kind": kind, "at": _now()}
        )
        save_state(state_path, state)
        return FixAttempt(raw_name, raw_value, False, str(exc), kind)
    return FixAttempt(raw_name, raw_value, True, change=change.to_dict())


def status(
    *, srv: ServerControl | None = None, state_path: Path | None = None, cfg: Config | None = None
) -> dict:
    cfg = cfg or get_config()
    srv = srv or PgServer(cfg)
    state_path = state_path or default_state_path(cfg)
    state = load_state(state_path)
    baseline = baseline_config_values(cfg)
    rows = []
    for name in TARGET_SETTINGS:
        info = srv.info(name)
        raw = srv.running(name)
        at_base = units.values_equal(
            units.normalize(raw, info), units.normalize(baseline[name], info)
        )
        rows.append(
            {
                "name": name,
                "running": raw,
                "unit": info.unit,
                "baseline": baseline[name],
                "at_baseline": at_base,
            }
        )
    return {
        "state_file": str(state_path),
        "state": state,
        "auto_conf": srv.auto_conf(),
        "settings": rows,
        "cases": [c.case_id for c in load_cases()],
    }
