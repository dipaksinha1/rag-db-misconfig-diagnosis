"""Connection helpers and the ``pgdiag-db`` CLI for the TEST database.

This is the PostgreSQL 16 container from ``compose.yaml``, not the RAG index.

    pgdiag-db wait      block until the server accepts connections
    pgdiag-db status    print server version, extension state and the target settings
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence

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


def is_connection_lost(exc: BaseException | None) -> bool:
    """A lost connection or server restart (e.g. an OOM-killed backend), not a SQL error."""
    return isinstance(exc, psycopg.OperationalError) and not isinstance(
        exc, psycopg.errors.QueryCanceled
    )


# How a lost connection shows up in benchmark error strings ("<ExcName>: message"), which
# record per-query failures instead of raising.
CRASH_MARKERS = (
    "OperationalError:",
    "server closed the connection",
    "the connection is closed",
    "terminating connection because of crash",
)


def errors_show_crash(messages) -> bool:
    """True if any benchmark error message says the server connection was lost."""
    return any(m in str(msg) for msg in messages for m in CRASH_MARKERS)


class ServerCrashed(psycopg.OperationalError):
    """The server restarted under a benchmark (seen in its error strings, not raised)."""


def compose_command(cfg: Config | None = None) -> list[str]:
    """``docker compose`` bound to this repository's compose.yaml (and .env)."""
    cfg = cfg or get_config()
    return ["docker", "compose", "--project-directory", str(cfg.root)]


def restart_server(cfg: Config | None = None, *, timeout: float = 180.0) -> float:
    """``docker compose restart db``, then wait until it answers. Returns seconds taken.

    Needed after ``ALTER SYSTEM`` on restart-only settings (``shared_buffers``)
    and to make a benchmark start from a freshly started server.
    """
    cfg = cfg or get_config()
    start = time.monotonic()
    subprocess.run(
        [*compose_command(cfg), "restart", "db"],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    wait_until_ready(cfg, timeout=max(10.0, timeout - (time.monotonic() - start)))
    return time.monotonic() - start


def container_info(cfg: Config | None = None) -> dict:
    """Resource limits of the running ``db`` container (empty dict if Docker is unavailable)."""
    cfg = cfg or get_config()
    try:
        cid = subprocess.run(
            [*compose_command(cfg), "ps", "-q", "db"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        if not cid:
            return {}
        raw = subprocess.run(
            ["docker", "inspect", cid, "--format", "{{json .}}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    info = json.loads(raw)
    host = info.get("HostConfig", {})
    nano = host.get("NanoCpus") or 0
    return {
        "name": info.get("Name", "").lstrip("/"),
        "image": info.get("Config", {}).get("Image"),
        "started_at": info.get("State", {}).get("StartedAt"),
        "mem_limit_bytes": host.get("Memory"),
        "memswap_limit_bytes": host.get("MemorySwap"),
        "shm_size_bytes": host.get("ShmSize"),
        "cpus": nano / 1e9 if nano else None,  # None = no CPU limit
        "cpuset_cpus": host.get("CpusetCpus") or None,
        "host_cpus": os.cpu_count(),
    }


def server_version(conn: psycopg.Connection) -> str:
    """The server version string, e.g. ``16.16``."""
    return conn.execute("SHOW server_version").fetchone()[0].split()[0]


def target_settings(conn: psycopg.Connection, names: Sequence[str] = TARGET_SETTINGS) -> list[dict]:
    """Current state of each target setting (or of ``names``), in that order.

    ``value`` is the human-readable form (``128MB``); ``setting`` and ``unit``
    are the raw pg_settings columns (``16384`` and ``8kB``).
    """
    columns = ("name", "value", "setting", "unit", "source", "pending_restart")
    rows = conn.execute(
        "SELECT name, current_setting(name), setting, unit, source, pending_restart "
        "FROM pg_settings WHERE name = ANY(%s)",
        (list(names),),
    ).fetchall()
    by_name = {row[0]: dict(zip(columns, row, strict=True)) for row in rows}
    return [by_name[name] for name in names]


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
