# Progress log

One section per task, added when the task is finished. Each section says what
was built, how to run it, and what later tasks need to know.

## Conventions for every task

- Add the stage's console script to `[project.scripts]` in `pyproject.toml`
  and its code to the matching package under `src/pgdiag/`. Update the stage
  table in `README.md` (status "done", real command name) and add a section here.
- Pin new dependencies with `==` in `pyproject.toml`, install them, then run
  `make lock` to rewrite `requirements.lock` (full `pip freeze`, used as a
  constraints file by `make setup`).
- Take paths and settings from `pgdiag.config.get_config()`, and setting names
  from `pgdiag.settings.TARGET_SETTINGS`. Do not hard-code either.
- Tests live in `tests/` and run with `make test` (plain `pytest`). Mark tests
  that need outside resources: `@pytest.mark.db` (test database), `network`
  (postgresql.org), `llm` (Anthropic API), `slow`. Markers are registered in
  `pyproject.toml` and `--strict-markers` is on. Tests needing an outside
  resource must skip with a clear reason when it is unavailable.
- `make lint` (ruff check + format check, line length 100) should pass.
- Never commit `.env`, anything under `data/`, or anything under `results/`.

---

## T0 Scaffold (done)

### What was built

- **Package** `pgdiag` in `src/` layout, installed editable into `.venv`.
  - `pgdiag/config.py`: `get_config()` returns a frozen `Config` (cached).
    Values come from `os.environ`, then `.env` in the repo root, then defaults.
    `.env` is read but not exported into `os.environ`, so pass values
    explicitly (e.g. `Anthropic(api_key=cfg.anthropic_api_key)`).
    `load_config(env=..., root=...)` builds one from an explicit mapping for tests.
  - `pgdiag/settings.py`: `TARGET_SETTINGS`, the ten settings, the only copy.
  - `pgdiag/db.py`: `connect()`, `wait_until_ready()`, `server_version()`,
    `target_settings()` and the `pgdiag-db` CLI.
  - Empty packages (docstring only) for later stages: `ingest/docs`,
    `ingest/release_notes`, `ingest/mailing_list`, `index`, `workload`,
    `faults`, `collect`, `retrieve`, `diagnose`, `fixtest`, `experiment`,
    `evaluate`. No stub CLIs were registered for them.
- **Test database**: `compose.yaml`, service `db`, container `pgdiag-db`,
  image `postgres:16.16` (what `postgres:16` resolved to on 2026-11-13).
- **Baseline server config**: `docker/postgres/postgresql.conf`.
- **Init script**: `docker/postgres/initdb/01-extensions.sql` creates
  `pg_stat_statements` when the data volume is first initialised.
- `pyproject.toml`, `requirements.lock`, `Makefile`, `.env.example`,
  `.gitignore`, `README.md`, tests.

### How to run it

```sh
make setup        # .venv + pinned deps
make db-up        # docker compose up -d --wait, then pgdiag-db status
make test         # 25 tests; the 6 DB tests skip if the database is down
make test-db      # only the DB tests; fails instead of skipping if the DB is down
make db-down      # stop, keep the data volume
make db-reset     # stop and delete the data volume (docker compose down -v)
```

`pgdiag-db wait [--timeout N]` blocks until the server answers a query.
`pgdiag-db status` prints the server version, config file, pg_stat_statements
state, the host log directory and the current value and source of each target
setting.

### Config variables (all in `.env.example`)

| Variable | Default | `Config` attribute |
|---|---|---|
| `ANTHROPIC_API_KEY` | none | `anthropic_api_key` |
| `ANTHROPIC_MODEL` | none (`.env.example` suggests `claude-opus-5-5`) | `anthropic_model` |
| `PGDIAG_DB_HOST` | `localhost` | `db_host` |
| `PGDIAG_DB_PORT` | `5433` | `db_port` (also the host port in compose) |
| `PGDIAG_DB_NAME` / `_USER` / `_PASSWORD` | `pgdiag` | `db_name` / `db_user` / `db_password` |
| `PGDIAG_DB_MEM_LIMIT` | `1g` | compose only |
| `PGDIAG_DB_SHM_SIZE` | `256m` | compose only |
| `EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` | `embedding_model` |
| `CRAWL_DELAY_SECONDS` | `1.0` (values below 1.0 are raised to 1.0) | `crawl_delay_seconds` |
| `CRAWLER_CONTACT` | none | appended to `user_agent` when set |
| `MAILING_LIST_START_YEAR` | `2015` | `mailing_list_start_year` |
| `MAILING_LIST_END_YEAR` | current year | `mailing_list_end_year` |

Paths on `Config`: `root`, `data_dir`, `raw_dir` (`data/raw/`), `chunks_dir`
(`data/chunks/`), `index_dir` (`data/index/`), `pg_logs_dir` (`data/pg_logs/`),
`results_dir` (`results/`). `cfg.ensure_dirs()` creates the writable ones.
`cfg.db_connect_kwargs` is ready for `psycopg.connect(**...)`.
`cfg.user_agent` is `pg-misconfig-rag-research/0.1 (research crawler; max 1 req/s)`.
`PGDIAG_ROOT` overrides repo-root detection. `PGDIAG_REQUIRE_DB=1` makes DB
tests fail instead of skip.

### Test database: what later tasks need to know

- **Connecting**: host `localhost`, port `5433`, database, user and password
  all `pgdiag`. The user is a superuser. The port is published on 127.0.0.1 only.
  From Python use `pgdiag.db.connect()` (autocommit by default).
- **Memory**: `mem_limit` and `memswap_limit` are both `PGDIAG_DB_MEM_LIMIT`
  (1 GiB by default, no extra swap). Checked with
  `docker inspect pgdiag-db --format '{{.HostConfig.Memory}}'` -> `1073741824`.
  `/dev/shm` is 256 MB (parallel query memory). No CPU limit is set. T5 must
  generate more than 1 GiB of data.
- **Where the baseline config lives (important for T5 and T6)**:
  `docker/postgres/postgresql.conf` is the one place for the healthy
  configuration. It is mounted read-only and selected with
  `-c config_file=/etc/pgdiag/postgresql.conf`. The ten target settings are
  listed at the bottom of that file, commented out at PostgreSQL defaults; T5/T6
  set the tuned baseline by editing those lines. After editing, reload
  (`SELECT pg_reload_conf()`) or `docker compose restart db` for restart-only
  settings (`shared_buffers`).
- **How to break a setting (T6)**: use `ALTER SYSTEM SET ...`. It writes
  `postgresql.auto.conf` in the data volume, which is read after the baseline
  file and wins. Then reload, or restart for restart-only settings.
  `ALTER SYSTEM RESET ALL` plus reload/restart restores the baseline exactly.
  Verified for `work_mem` (reload) and `shared_buffers` (restart). Do not add
  `-c name=value` flags to the compose `command`: command-line flags outrank
  `ALTER SYSTEM`, so the fault could not be injected (a test guards this).
  Restart with `docker compose restart db` then `pgdiag-db wait`.
- **Answer leakage (T7)**: after `ALTER SYSTEM`, `pg_settings.source` and
  `sourcefile` point at `postgresql.auto.conf` for the broken setting, and
  `pg_file_settings` shows it too. The symptom collector must not include
  those columns.
- **pg_stat_statements**: preloaded, extension created in the `pgdiag`
  database, `pg_stat_statements.track = all`. `track_io_timing = on`.
  `SELECT pg_stat_statements_reset()` clears it between runs.
- **Server logs**: the logging collector writes to `/var/log/pgdiag` in the
  container, which is `data/pg_logs/` on the host (gitignored). Two files per
  UTC day, both world-readable:
  - `postgresql-YYYY-MM-DD.log`: plain text, prefix
    `%m [%p] %q[user=%u,db=%d,app=%a] ` (timestamp with milliseconds in UTC, pid).
  - `postgresql-YYYY-MM-DD.json`: the same events as JSON lines (`jsonlog`),
    with `timestamp`, `error_severity`, `message`, `backend_type`, `query_id`
    and so on. Easier to parse; statements spanning several lines stay one record.

  Logged: statements slower than 250 ms (`log_min_duration_statement`), every
  temp file (`log_temp_files = 0`), checkpoints, every autovacuum and
  autoanalyze run (`log_autovacuum_min_duration = 0`), lock waits. Because the
  collector is on, `docker compose logs db` only shows container start-up
  output, not server log lines. The log files are owned by the container's
  postgres uid (999) and survive `make db-reset`; they are appended to, so
  select lines by timestamp. `SELECT pg_rotate_logfile()` does not start a new
  file within the same day (the file name only contains the date).
- **pgbench**: ships in the image. `docker compose exec db pgbench --version`
  prints `pgbench (PostgreSQL) 16.16 (Debian 16.16-1.pgdg13+1)`. Run it inside
  the container, e.g. `docker compose exec db pgbench -U pgdiag -i -s 10 pgdiag`.
- **Healthcheck**: `pg_isready` over TCP, so the container only turns healthy
  once the real server is up (not during first-time init). `docker compose up
  -d --wait` returns when it is healthy; a fresh volume takes about 2 seconds.
- **Init scripts** only run when the data volume is empty. After changing
  anything in `docker/postgres/initdb/`, run `make db-reset` and `make db-up`.

### Dependencies installed so far

Runtime: `psycopg[binary]==3.3.9`, `python-dotenv==1.2.4`, `PyYAML==6.0.3`.
Dev: `pytest==9.2.0`, `ruff==0.17.5`. Verified on Python 3.13. Not yet
installed: HTTP/HTML libraries (T1 adds what it needs), `anthropic` (T9),
`sentence-transformers` / `chromadb` / BM25 (T4).
