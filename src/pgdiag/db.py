"""Connection helpers and the ``pgdiag-db`` CLI for the TEST database.

This is the PostgreSQL 16 container from ``compose.yaml``, not the RAG index.

    pgdiag-db wait      block until the server accepts connections
    pgdiag-db status    print server version, extension state and the target settings
"""

from __future__ import annotations

import argparse
import sys
import time

import psycopg

from pgdiag.config import Config, get_config
from pgdiag.settings import TARGET_SETTINGS


def connect(
    cfg: Config | None = None, *, autocommit: bool = True, connect_timeout: int = 5
) -> psycopg.Connection:
    """Open a connection to the test database."""
    cfg = cfg or get_config()
    return psycopg.connect(
        **cfg.db_connect_kwargs,
        autocommit=autocommit,
        connect_timeout=connect_timeout,
        application_name="pgdiag",
    )


def wait_until_ready(
    cfg: Config | None = None, *, timeout: float = 60.0, interval: float = 1.0
) -> None:
    """Block until a query succeeds; raise TimeoutError after ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            with connect(cfg, connect_timeout=3) as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.OperationalError as exc:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"test database not reachable after {timeout:.0f}s: {exc}"
                ) from exc
            time.sleep(interval)


def server_version(conn: psycopg.Connection) -> str:
    """The server version string, e.g. ``16.16``."""
    return conn.execute("SHOW server_version").fetchone()[0].split()[0]


def target_settings(conn: psycopg.Connection) -> list[dict]:
    """Current state of each target setting, in TARGET_SETTINGS order.

    ``value`` is the human-readable form (``128MB``); ``setting`` and ``unit``
    are the raw pg_settings columns (``16384`` and ``8kB``).
    """
    columns = ("name", "value", "setting", "unit", "source", "pending_restart")
    rows = conn.execute(
        "SELECT name, current_setting(name), setting, unit, source, pending_restart "
        "FROM pg_settings WHERE name = ANY(%s)",
        (list(TARGET_SETTINGS),),
    ).fetchall()
    by_name = {row[0]: dict(zip(columns, row, strict=True)) for row in rows}
    return [by_name[name] for name in TARGET_SETTINGS]


def _cmd_wait(args: argparse.Namespace) -> int:
    try:
        wait_until_ready(timeout=args.timeout)
    except TimeoutError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("test database is ready")
    return 0


def _cmd_status(_: argparse.Namespace) -> int:
    cfg = get_config()
    try:
        conn = connect(cfg)
    except psycopg.OperationalError as exc:
        print(
            f"error: cannot connect to {cfg.db_host}:{cfg.db_port}/{cfg.db_name}: {exc}".rstrip(),
            file=sys.stderr,
        )
        print("hint: start it with `make db-up` (docker compose up -d --wait)", file=sys.stderr)
        return 1
    with conn:
        print(f"server_version: {server_version(conn)}")
        print(f"connection: {cfg.db_user}@{cfg.db_host}:{cfg.db_port}/{cfg.db_name}")
        print(f"config_file: {conn.execute('SHOW config_file').fetchone()[0]}")
        try:
            count = conn.execute("SELECT count(*) FROM pg_stat_statements").fetchone()[0]
            print(f"pg_stat_statements: ok ({count} statements tracked)")
        except psycopg.Error as exc:
            print(f"pg_stat_statements: NOT AVAILABLE ({exc})".rstrip())
        print(f"server logs (host): {cfg.pg_logs_dir}")
        print("target settings:")
        for row in target_settings(conn):
            flag = "  [pending restart]" if row["pending_restart"] else ""
            print(f"  {row['name']:<34} {row['value']:<10} source={row['source']}{flag}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pgdiag-db", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    wait = sub.add_parser("wait", help="block until the test database accepts connections")
    wait.add_argument("--timeout", type=float, default=60.0, help="seconds to wait (default: 60)")
    wait.set_defaults(func=_cmd_wait)

    status = sub.add_parser("status", help="print server version, extensions and target settings")
    status.set_defaults(func=_cmd_status)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
