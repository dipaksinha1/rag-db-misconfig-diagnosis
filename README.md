# pgdiag: does RAG help an LLM diagnose PostgreSQL misconfigurations?

A research pipeline that measures whether retrieval-augmented generation (RAG)
improves an LLM's ability to find a misconfigured PostgreSQL 16 setting.

For each case we deliberately set one configuration setting of a test database
to a bad value, collect the symptoms (statistics, query plans, log lines), and
ask Claude which setting is wrong. Every case is run twice with everything else
identical: once with chunks retrieved from a PostgreSQL knowledge base, once
without. We then compare how often each variant names the right setting and how
good its reasoning is.

No web search is used at diagnosis time. All outside knowledge comes from the
RAG index, which is built once from files downloaded from postgresql.org.

## Two databases: do not mix them up

| | RAG index | Test database |
|---|---|---|
| What | Searchable knowledge base: PostgreSQL 16 docs, PostgreSQL 16 release notes, pgsql-performance mailing-list threads | PostgreSQL 16 instance with generated data that we break on purpose |
| Where | Local persistent store under `data/index/` (Chroma + BM25, hybrid search) | Docker container `pgdiag-db` from `compose.yaml` |
| Role | Source of retrieved context for the LLM | Subject of the diagnosis |
| Code | `pgdiag.ingest.*`, `pgdiag.index`, `pgdiag.retrieve` | `pgdiag.db`, `pgdiag.workload`, `pgdiag.faults`, `pgdiag.collect`, `pgdiag.fixtest` |

The settings broken, one per case, are listed once in
[`src/pgdiag/settings.py`](src/pgdiag/settings.py): `shared_buffers`, `work_mem`,
`effective_cache_size`, `random_page_cost`, `max_parallel_workers_per_gather`,
`autovacuum`, `max_wal_size`, `default_statistics_target`, `jit_above_cost`,
`maintenance_work_mem`.

## Quickstart

Requirements: Python 3.11+, Docker with Compose v2+, `make`.

```sh
make setup            # creates .venv and installs pinned dependencies
cp .env.example .env  # optional for now; the API key is only needed from the diagnosis stage on
make db-up            # starts PostgreSQL 16 on localhost:5433, waits until healthy, prints its status
make test             # DB tests are skipped automatically if the database is down
```

Without `make`:

```sh
python3 -m venv .venv && .venv/bin/pip install -c requirements.lock -e ".[dev]"
docker compose up -d --wait
.venv/bin/pgdiag-db status
.venv/bin/pytest
```

Other targets: `make db-down` (stop, keep data), `make db-reset` (stop and delete
the data volume), `make db-psql`, `make db-logs`, `make test-db` (fail instead of
skip when the database is unreachable), `make lint`. Run `make help` for the list.

## Pipeline stages

Each stage is one CLI entry point. Only the stages marked "done" exist; the
other command names are the plan and may change when the stage is built.
[`PROGRESS.md`](PROGRESS.md) has the details of what each finished stage built.

| Task | Stage | Entry point | Status |
|---|---|---|---|
| T0 | Scaffold: repo layout, test database, config, tests | `pgdiag-db` (`wait`, `status`) | done |
| T1 | Ingest PostgreSQL 16 docs into chunks | `pgdiag-ingest-docs` | planned |
| T2 | Ingest PostgreSQL 16 release notes | `pgdiag-ingest-release-notes` | planned |
| T3 | Ingest pgsql-performance threads (2015 to present) | `pgdiag-ingest-mailing-list` | planned |
| T4 | Build the RAG index (embeddings, Chroma, BM25, hybrid search) | `pgdiag-index` | planned |
| T5 | Test data, workload queries, benchmark runner, baseline | `pgdiag-workload` | planned |
| T6 | Fault injection: apply and reset one bad setting | `pgdiag-fault` | planned |
| T7 | Symptom collector (one JSON document per case) | `pgdiag-collect` | planned |
| T8 | Retrieval: symptoms to queries to top chunks | `pgdiag-retrieve` | planned |
| T9 | Diagnosis with Claude, with and without RAG | `pgdiag-diagnose` | planned |
| T10 | Fix tester: apply each proposed fix and time the workload | `pgdiag-fixtest` | planned |
| T11 | Experiment runner: 10 cases x 2 modes x 3 repeats, resumable | `pgdiag-experiment` | planned |
| T12 | Evaluation and report | `pgdiag-evaluate` | planned |

## Repository layout

```
compose.yaml                   test database (PostgreSQL 16, memory-limited)
docker/postgres/postgresql.conf   baseline ("good") server configuration
docker/postgres/initdb/        SQL run once when the data volume is created
src/pgdiag/config.py           paths and settings, loaded from the environment and .env
src/pgdiag/settings.py         the ten target settings
src/pgdiag/db.py               test-database connection helpers and the pgdiag-db CLI
src/pgdiag/<stage>/            one package per later stage (empty until built)
tests/                         pytest suite
data/                          gitignored: raw/ (download cache), chunks/, index/, pg_logs/
results/                       gitignored: experiment outputs
```

## Configuration

All configuration is in `.env` (see [`.env.example`](.env.example) for every
variable and its default). `.env` is gitignored and must never be committed.
Docker Compose and the Python code read the same variables, and both work with
no `.env` at all. The diagnosis model is set with `ANTHROPIC_MODEL` and the key
with `ANTHROPIC_API_KEY`.

Downloads from postgresql.org are cached under `data/raw/` so nothing is
fetched twice, and are rate-limited to at most one request per second with a
descriptive User-Agent.
