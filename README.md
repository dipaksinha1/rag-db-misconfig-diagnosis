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
RAG index, which is built once from files downloaded from postgresql.org: the
PostgreSQL 16 documentation and the PostgreSQL 16 release notes.

## Two databases: do not mix them up

| | RAG index | Test database |
|---|---|---|
| What | Searchable knowledge base built from two sources: the PostgreSQL 16 docs and the PostgreSQL 16 release notes (16.0 and every minor release) | PostgreSQL 16 instance with generated data that we break on purpose |
| Where | Local persistent store under `data/index/` (Chroma + BM25, hybrid search) | Docker container `pgdiag-db` from `compose.yaml` |
| Role | Source of retrieved context for the LLM | Subject of the diagnosis |
| Code | `pgdiag.ingest.*`, `pgdiag.index`, `pgdiag.retrieve` | `pgdiag.db`, `pgdiag.workload`, `pgdiag.faults`, `pgdiag.collect`, `pgdiag.fixtest` |

The pgsql-performance mailing list was planned as a third source and is **not
included**. postgresql.org's `robots.txt` disallows the archive's listings and
bulk downloads for every user agent (`/list/`, `/message-id/raw/`,
`/message-id/flat/`, `/message-id/mbox/`, `/search/`), so there is no permitted
way to discover or download the threads, and this project obeys `robots.txt`
strictly. No request was made to those paths.

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
python3 -m venv .venv
.venv/bin/pip install --extra-index-url https://download.pytorch.org/whl/cpu -c requirements.lock -e ".[dev]"
docker compose up -d --wait
.venv/bin/pgdiag-db status
.venv/bin/pytest
```

Other targets: `make db-down` (stop, keep data), `make db-reset` (stop and delete
the data volume), `make db-psql`, `make db-logs`, `make test-db` (fail instead of
skip when the database is unreachable), `make test-data` (fail instead of skip
when generated files are missing), `make lint`. Run `make help` for the list.

The PyTorch CPU wheel index is needed because `torch` is pinned to its CPU-only
build (`torch==...+cpu` in `requirements.lock`); the default PyPI wheel would pull
in several GB of CUDA libraries.

Building the knowledge base:

```sh
make ingest-docs            # downloads the PostgreSQL 16 docs once (about 20 minutes at 1 request/s,
                            # cached under data/raw/), then writes data/chunks/docs.jsonl
make ingest-release-notes   # chunks the release notes 16.0 to 16.x from the same cache (no request
                            # after ingest-docs), writes data/chunks/release_notes.jsonl
make index                  # embeds every chunk file into data/index/ (Chroma + manifest; the
                            # embedding model is downloaded once from huggingface.co; about 10
                            # minutes on 4 CPU cores); does nothing if the inputs are unchanged
make index-check            # acceptance check: each target setting's name finds its docs chunk
make test                   # now also checks the real chunk files and the index
.venv/bin/pgdiag-index search "sorts spill to temporary files" -k 5 --source docs
```

Test database and workload (T5):

```sh
make data-gen         # generates the workload dataset into the test database (~2 minutes, ~3.3 GB)
make baseline         # ALTER SYSTEM RESET ALL + restart, full benchmark (~4 minutes), stores results/baseline.json
make baseline         # a second, independent run; the previous one is kept as results/baseline.prev.json
make baseline-check   # acceptance: the two baseline median totals differ by less than 10%
.venv/bin/pgdiag-workload queries            # the query set and which setting each query targets
.venv/bin/pgdiag-workload bench --preset quick --label my-run   # benchmark the CURRENT configuration
make probe            # informative: break each setting in turn and measure the slowdown (~30 minutes)
```

## Pipeline stages

Each stage is one CLI entry point. Only the stages marked "done" exist; the
other command names are the plan and may change when the stage is built.
[`PROGRESS.md`](PROGRESS.md) has the details of what each finished stage built.

| Task | Stage | Entry point | Status |
|---|---|---|---|
| T0 | Scaffold: repo layout, test database, config, tests | `pgdiag-db` (`wait`, `status`) | done |
| T1 | Ingest PostgreSQL 16 docs into chunks (`data/chunks/docs.jsonl`) | `pgdiag-ingest-docs` (`--download-only`, `--chunk-only`, `--limit N`) | done |
| T2 | Ingest PostgreSQL 16 release notes, 16.0 and every minor release (`data/chunks/release_notes.jsonl`) | `pgdiag-ingest-release-notes` (`--download-only`, `--chunk-only`) | done |
| T3 | Ingest pgsql-performance threads | none | skipped: `robots.txt` disallows the archive listings (see above) |
| T4 | Build the RAG index (`data/index/`: embeddings, Chroma, BM25, hybrid search; `pgdiag.index.search()`) | `pgdiag-index` (`build [--rebuild]`, `search`, `get`, `stats`, `check`) | done |
| T5 | Test data (~3.3 GB, larger than the 1 GiB container), workload query set, benchmark runner, baseline (`results/baseline.json`) | `pgdiag-workload` (`generate [--reset]`, `bench [--preset full\|quick]`, `baseline`, `baseline-check`, `compare A B`, `probe-sensitivity`, `queries`, `status`) | done |
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
src/pgdiag/fetch.py            the only way to download: cached, 1 request/s, robots.txt, postgresql.org only
src/pgdiag/chunks.py           chunk schema, JSONL helpers, which settings a text mentions
src/pgdiag/ingest/docs/        T1: crawl and chunk the PostgreSQL 16 documentation
src/pgdiag/ingest/release_notes/   T2: chunk the PostgreSQL 16 release notes
src/pgdiag/ingest/mailing_list/    T3: skipped, empty
src/pgdiag/index/              T4: build the index, hybrid search, get chunks by id
src/pgdiag/workload/           T5: data generator, query set (queries/*.sql), benchmark, probe
src/pgdiag/<stage>/            one package per later stage (empty until built)
tests/                         pytest suite (tests/fixtures/: trimmed real documentation pages)
data/                          gitignored: raw/ (download cache), chunks/, index/, pg_logs/
results/                       gitignored: experiment outputs (bench/, probe/, baseline*.json)
```

## Configuration

All configuration is in `.env` (see [`.env.example`](.env.example) for every
variable and its default). `.env` is gitignored and must never be committed.
Docker Compose and the Python code read the same variables, and both work with
no `.env` at all. The diagnosis model is set with `ANTHROPIC_MODEL` and the key
with `ANTHROPIC_API_KEY`.

Downloads from postgresql.org are cached under `data/raw/` so nothing is
fetched twice, and are rate-limited to at most one request per second with a
descriptive User-Agent. All of this is enforced in one place,
`pgdiag.fetch.PoliteFetcher`, which also obeys `robots.txt` and refuses any
other host.
