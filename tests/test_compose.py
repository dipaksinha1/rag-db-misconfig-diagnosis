"""Static checks on the test-database definition: compose.yaml, the baseline
postgresql.conf, .env.example and .gitignore. No Docker needed."""

from __future__ import annotations

import re

import pytest
import yaml
from dotenv import dotenv_values

from pgdiag.settings import TARGET_SETTINGS


@pytest.fixture(scope="module")
def compose_text(repo_root) -> str:
    return (repo_root / "compose.yaml").read_text()


@pytest.fixture(scope="module")
def db_service(compose_text) -> dict:
    return yaml.safe_load(compose_text)["services"]["db"]


@pytest.fixture(scope="module")
def baseline_conf(repo_root) -> dict[str, str]:
    """Active (uncommented) settings in docker/postgres/postgresql.conf."""
    settings = {}
    for line in (repo_root / "docker/postgres/postgresql.conf").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            name, value = line.split("=", 1)
            settings[name.strip()] = value.strip().strip("'")
    return settings


def test_image_is_a_pinned_postgres_16_minor(db_service):
    assert re.fullmatch(r"postgres:16\.\d+", db_service["image"])


def test_memory_limit_shm_and_healthcheck_are_set(db_service):
    assert db_service["mem_limit"] == "${PGDIAG_DB_MEM_LIMIT:-256m}"
    assert db_service["memswap_limit"] == db_service["mem_limit"]
    assert db_service["shm_size"] == "${PGDIAG_DB_SHM_SIZE:-128m}"
    assert "pg_isready" in " ".join(db_service["healthcheck"]["test"])


def test_host_port_is_configurable_and_local_only(db_service):
    assert db_service["ports"] == ["127.0.0.1:${PGDIAG_DB_PORT:-5433}:5432"]


def test_volumes_cover_data_config_init_scripts_and_logs(db_service, baseline_conf, repo_root):
    volumes = db_service["volumes"]
    assert "pgdata:/var/lib/postgresql/data" in volumes
    assert "./docker/postgres:/etc/pgdiag:ro" in volumes
    assert "./docker/postgres/initdb:/docker-entrypoint-initdb.d:ro" in volumes
    # The server's log_directory is the container side of the data/pg_logs bind mount.
    assert f"./data/pg_logs:{baseline_conf['log_directory']}" in volumes

    init_sql = (repo_root / "docker/postgres/initdb/01-extensions.sql").read_text()
    assert "CREATE EXTENSION IF NOT EXISTS pg_stat_statements" in init_sql


def test_only_config_file_is_set_on_the_command_line(db_service):
    """Command-line -c flags outrank ALTER SYSTEM, which fault injection relies on."""
    command = db_service["command"]
    assert command == ["postgres", "-c", "config_file=/etc/pgdiag/postgresql.conf"]


def test_baseline_conf_enables_instrumentation_and_logging(baseline_conf):
    assert "pg_stat_statements" in baseline_conf["shared_preload_libraries"]
    assert baseline_conf["track_io_timing"] == "on"
    assert baseline_conf["listen_addresses"] == "*"

    assert baseline_conf["logging_collector"] == "on"
    assert baseline_conf["log_min_duration_statement"] == "250ms"  # slow queries
    assert baseline_conf["log_temp_files"] == "0"  # every temp file
    assert baseline_conf["log_checkpoints"] == "on"
    assert baseline_conf["log_autovacuum_min_duration"] == "0"  # every autovacuum run
    assert "%m" in baseline_conf["log_line_prefix"] and "%p" in baseline_conf["log_line_prefix"]


def test_baseline_conf_mentions_every_target_setting(repo_root):
    """The baseline file is the one place the healthy value of each target setting lives."""
    text = (repo_root / "docker/postgres/postgresql.conf").read_text()
    for name in TARGET_SETTINGS:
        assert re.search(rf"^#?{name} = ", text, flags=re.MULTILINE), name


def test_compose_defaults_match_env_example(compose_text, repo_root):
    """Compose must behave the same with no .env and with .env copied from .env.example."""
    example = dotenv_values(repo_root / ".env.example")
    interpolations = re.findall(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}", compose_text)
    assert interpolations, "expected ${VAR:-default} interpolations in compose.yaml"
    for name, default in interpolations:
        assert default, f"${{{name}}} needs a default so compose works without .env"
        assert example.get(name) == default, f"{name}: .env.example disagrees with compose.yaml"


def test_secrets_and_generated_files_are_gitignored(repo_root):
    ignored = (repo_root / ".gitignore").read_text().splitlines()
    for pattern in (".env", ".venv/", "/data/", "/results/"):
        assert pattern in ignored
