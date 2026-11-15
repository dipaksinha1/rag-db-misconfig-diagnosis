PYTHON ?= python3
VENV := .venv
BIN := $(VENV)/bin
COMPOSE := docker compose
# torch must be the CPU-only build (no multi-GB CUDA wheels): pip also looks in the
# PyTorch CPU wheel index, where requirements.lock's `torch==X+cpu` lives.
TORCH_INDEX := https://download.pytorch.org/whl/cpu

.PHONY: help setup lock db-up db-down db-reset db-status db-psql db-logs \
	ingest-docs ingest-release-notes index index-rebuild index-check \
	data-gen data-gen-reset bench bench-quick baseline baseline-quick baseline-check probe \
	test test-db test-data \
	lint format

help:
	@echo "make setup      create .venv and install the package with dev dependencies"
	@echo "make lock       rewrite requirements.lock from the current .venv"
	@echo "make db-up      start the PostgreSQL 16 test database and wait until healthy"
	@echo "make db-down    stop it (data volume kept)"
	@echo "make db-reset   stop it and delete the data volume"
	@echo "make db-status  print server version, extension state and target settings"
	@echo "make db-psql    open psql inside the container"
	@echo "make db-logs    follow today's server log (data/pg_logs/)"
	@echo "make ingest-docs  download the PostgreSQL 16 docs (cached, 1 request/s) and chunk them"
	@echo "make ingest-release-notes  chunk the PostgreSQL 16 release notes (pages cached by ingest-docs)"
	@echo "make index      embed the chunks into the RAG index (data/index/); no-op if unchanged"
	@echo "make index-rebuild  rebuild the index even if nothing changed"
	@echo "make index-check    acceptance check (setting names) and symptom-query probes"
	@echo "make data-gen   generate the test-database dataset (resumable; ~2 min, ~3.3 GB)"
	@echo "make data-gen-reset  drop and regenerate the dataset"
	@echo "make bench      benchmark the CURRENT server config (full preset); make bench-quick"
	@echo "make baseline   reset config to baseline, restart, benchmark, store results/baseline.json"
	@echo "make baseline-check  compare the last two baseline runs (acceptance: < 10%)"
	@echo "make probe      break each target setting in turn and measure the slowdown (~30 min)"
	@echo "make test       run the test suite (DB and data tests skip if their resource is absent)"
	@echo "make test-db    run only the DB tests and fail if the DB is unreachable"
	@echo "make test-data  run only the generated-data tests and fail if the files are missing"
	@echo "make lint       ruff check + format check"

setup:
	test -x $(BIN)/python || $(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install --extra-index-url $(TORCH_INDEX) -c requirements.lock -e ".[dev]"

lock:
	$(BIN)/pip freeze --exclude-editable > requirements.lock

db-up:
	$(COMPOSE) up -d --wait
	$(BIN)/pgdiag-db status

db-down:
	$(COMPOSE) down

db-reset:
	$(COMPOSE) down -v

db-status:
	$(BIN)/pgdiag-db status

db-psql:
	$(COMPOSE) exec db sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'

db-logs:
	tail -F data/pg_logs/postgresql-$$(date -u +%Y-%m-%d).log

ingest-docs:
	$(BIN)/pgdiag-ingest-docs

ingest-release-notes:
	$(BIN)/pgdiag-ingest-release-notes

index:
	$(BIN)/pgdiag-index build

index-rebuild:
	$(BIN)/pgdiag-index build --rebuild

index-check:
	$(BIN)/pgdiag-index check

data-gen:
	$(BIN)/pgdiag-workload generate

data-gen-reset:
	$(BIN)/pgdiag-workload generate --reset

bench:
	$(BIN)/pgdiag-workload bench --preset full

bench-quick:
	$(BIN)/pgdiag-workload bench --preset quick

baseline:
	$(BIN)/pgdiag-workload baseline --preset full

baseline-quick:
	$(BIN)/pgdiag-workload baseline --preset quick

baseline-check:
	$(BIN)/pgdiag-workload baseline-check --preset full

probe:
	$(BIN)/pgdiag-workload probe-sensitivity

test:
	$(BIN)/pytest

test-db:
	PGDIAG_REQUIRE_DB=1 $(BIN)/pytest -m db

test-data:
	PGDIAG_REQUIRE_DATA=1 $(BIN)/pytest -m data

lint:
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

format:
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .
