PYTHON ?= python3
VENV := .venv
BIN := $(VENV)/bin
COMPOSE := docker compose

.PHONY: help setup lock db-up db-down db-reset db-status db-psql db-logs \
	ingest-docs ingest-release-notes test test-db test-data lint format

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
	@echo "make test       run the test suite (DB and data tests skip if their resource is absent)"
	@echo "make test-db    run only the DB tests and fail if the DB is unreachable"
	@echo "make test-data  run only the generated-data tests and fail if the files are missing"
	@echo "make lint       ruff check + format check"

setup:
	test -x $(BIN)/python || $(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -c requirements.lock -e ".[dev]"

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
