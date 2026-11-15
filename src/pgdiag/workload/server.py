"""Server-level configuration helpers shared by the benchmark, the probe and T6.

The healthy configuration is ``docker/postgres/postgresql.conf`` (mounted as
``/etc/pgdiag/postgresql.conf``). Anything set with ``ALTER SYSTEM`` lives in
``postgresql.auto.conf`` and overrides it; ``reset_to_baseline`` removes all of
that, restarts, verifies and re-analyzes the static workload tables.
"""

from __future__ import annotations

from collections.abc import Callable

import psycopg
from psycopg import sql

from pgdiag import db
from pgdiag.config import Config, get_config
from pgdiag.workload import datagen

BASELINE_CONFIG_FILE = "/etc/pgdiag/postgresql.conf"

# pg_settings.context -> what makes a change take effect
RESTART_CONTEXTS = {"postmaster"}
RELOAD_CONTEXTS = {"sighup"}


def setting_context(conn: psycopg.Connection, name: str) -> str:
    row = conn.execute("SELECT context FROM pg_settings WHERE name = %s", (name,)).fetchone()
    if row is None:
        raise KeyError(name)
    return row[0]


def alter_system(conn: psycopg.Connection, name: str, value: str | None) -> None:
    """``ALTER SYSTEM SET name = value`` (or ``RESET name`` when value is None). No reload."""
    if value is None:
        conn.execute(sql.SQL("ALTER SYSTEM RESET {}").format(sql.Identifier(name)))
    else:
        conn.execute(
            sql.SQL("ALTER SYSTEM SET {} = {}").format(sql.Identifier(name), sql.Literal(value))
        )


def apply_change(cfg: Config, name: str, value: str | None) -> str:
    """ALTER SYSTEM, then reload or restart as the setting requires. Returns the method used."""
    with db.connect(cfg) as conn:
        context = setting_context(conn, name)
        alter_system(conn, name, value)
        if context not in RESTART_CONTEXTS:
            conn.execute("SELECT pg_reload_conf()")
    if context in RESTART_CONTEXTS:
        db.restart_server(cfg)
        return "restart"
    return "reload"


def baseline_problems(conn: psycopg.Connection) -> list[str]:
    """Why the server is NOT running the baseline configuration (empty list = it is)."""
    problems = []
    rows = conn.execute(
        "SELECT name, setting, source, sourcefile, pending_restart FROM pg_settings "
        "WHERE source NOT IN ('default', 'override', 'client', 'session') "
        "   OR pending_restart"
    ).fetchall()
    for name, _setting, source, sourcefile, pending in rows:
        if pending:
            problems.append(f"{name}: change pending restart")
        if source == "configuration file" and sourcefile and sourcefile != BASELINE_CONFIG_FILE:
            problems.append(f"{name}: set in {sourcefile}")
    auto = conn.execute(
        "SELECT name, setting FROM pg_file_settings "
        "WHERE sourcefile LIKE '%%postgresql.auto.conf' AND applied IS NOT NULL"
    ).fetchall()
    for name, setting in auto:
        problems.append(f"{name}={setting} in postgresql.auto.conf (ALTER SYSTEM)")
    return sorted(set(problems))


def analyze_static_tables(conn: psycopg.Connection) -> None:
    from pgdiag.workload.bench import STATIC_TABLES

    for table in STATIC_TABLES:
        conn.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(table)))


def reset_to_baseline(
    cfg: Config | None = None,
    *,
    dbname: str | None = None,
    analyze: bool = True,
    progress: Callable[[str], None] | None = None,
) -> None:
    """``ALTER SYSTEM RESET ALL``, restart, verify the baseline is active, re-analyze.

    Raises RuntimeError when something still differs from the baseline file.
    """
    cfg = cfg or get_config()
    say = progress or (lambda msg: None)
    with db.connect(cfg) as conn:
        conn.execute("ALTER SYSTEM RESET ALL")
    say("ALTER SYSTEM RESET ALL; restarting ...")
    db.restart_server(cfg)
    with db.connect(cfg) as conn:
        problems = baseline_problems(conn)
    if problems:
        raise RuntimeError("server is not at the baseline configuration: " + "; ".join(problems))
    if analyze:
        with datagen.connect_db(dbname, cfg) as conn:
            analyze_static_tables(conn)
    say("baseline configuration verified")
