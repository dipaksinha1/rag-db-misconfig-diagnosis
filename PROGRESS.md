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
  (postgresql.org), `llm` (Anthropic API), `slow`, `data` (generated files
  under `data/`). Markers are registered in
  `pyproject.toml` and `--strict-markers` is on. Tests needing an outside
  resource must skip with a clear reason when it is unavailable.
- `make lint` (ruff check + format check, line length 100) should pass.
- Never commit `.env`, anything under `data/`, or anything under `results/`.
- Download only through `pgdiag.fetch.PoliteFetcher` (cache, one request per
  second, robots.txt, postgresql.org only). Build chunks with
  `pgdiag.chunks.Chunk` and write them with `pgdiag.chunks.write_jsonl`.

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
make test         # the 6 DB tests skip if the database is down
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
| `MAILING_LIST_START_YEAR` | `2015` | `mailing_list_start_year` (unused: T3 was skipped) |
| `MAILING_LIST_END_YEAR` | current year | `mailing_list_end_year` (unused: T3 was skipped) |

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

---

## T1 Docs ingestion (done)

### What was built

- **`pgdiag/fetch.py`: `PoliteFetcher`**, the only code that talks to
  postgresql.org. T2 and T3 must use it too.
  - Cache: every body is stored under `data/raw/<host>/<first path segment>/<slug>.<url hash>`
    with a `.meta.json` next to it (URL, final URL, redirects, status, content
    type, fetch time, bytes, sha256, User-Agent). A cached URL is never fetched
    again and there is no refresh option; delete the two files to force one.
    200, 404 and 410 are cached; errors are not.
  - Rate limit: one request per `cfg.crawl_delay_seconds` (floor 1.0 s),
    counted from the end of one request to the start of the next. It is shared
    by all fetchers in a process and, through the lock file
    `data/raw/.rate-limit`, by all pgdiag processes on the machine. Retries and
    redirect hops count as requests.
  - Only `GET https://www.postgresql.org/...`; other hosts, `http://` and
    credentials inside the URL raise `UrlNotAllowed`. Redirects are followed by
    hand (each hop checked and rate-limited).
  - `robots.txt` is fetched once, cached and obeyed (`UrlNotAllowed`).
  - `cfg.user_agent` on every request; timeouts 15 s connect, 60 s read.
  - 5xx, 429 and network errors: up to 3 retries with back-off (5, 20, 60 s,
    or `Retry-After`). 401/403, or 429 after the retries, raise `FetchBlocked`:
    stop and report, do not work around it. Other failures raise `FetchError`.
  - TLS verification and the proxy come from the environment; the module never
    touches them.
  - Optional HTTP basic auth (`auth=(user, password)`, never written to the
    cache) and binary bodies (`result.content` is bytes, `result.text` decodes).
- **`pgdiag/chunks.py`**: the chunk schema shared by T1 to T4 (below),
  `write_jsonl` / `read_jsonl` / `iter_jsonl` / `validate_chunks`,
  `find_settings(text, names=None)` and `load_guc_names()` / `save_guc_names()`.
- **`pgdiag/ingest/docs/`**: `crawl.py` (page discovery from the table of
  contents, breadth first, only `/docs/16/*.html`), `parse.py` (HTML to
  chunks), `cli.py` (`pgdiag-ingest-docs`).
- Tests: `tests/test_fetch.py`, `test_chunks.py`, `test_docs_crawl.py`,
  `test_docs_parse.py` (all offline; fake transport and clock in
  `tests/fakes.py`, trimmed real pages in `tests/fixtures/docs/`) and
  `test_docs_real_data.py` (marker `data`: the acceptance test and sanity
  checks on the real `data/chunks/docs.jsonl`).

### How to run it

```sh
make ingest-docs                          # = .venv/bin/pgdiag-ingest-docs: download what is missing, then chunk
.venv/bin/pgdiag-ingest-docs --download-only   # only fill data/raw/
.venv/bin/pgdiag-ingest-docs --chunk-only      # only chunk the cache; makes no request at all
.venv/bin/pgdiag-ingest-docs --limit 20        # debugging: first 20 pages, prints stats, writes nothing
make test                                 # includes the acceptance test once docs.jsonl exists
make test-data                            # only the real-data tests; fails if docs.jsonl is missing
```

The first download takes about 20 minutes (1,169 pages at one request per
second). It can be interrupted and re-run: finished pages come from the cache.
If any page is missing or failed, the command exits non-zero and writes nothing.
Re-chunking from the cache takes about 30 seconds.

### Output

| File | Content |
|---|---|
| `data/raw/www.postgresql.org/docs/` | 1,169 cached pages (including the 19 release-note pages `release*.html` that T2 needs) |
| `data/raw/www.postgresql.org/_root/robots.txt.*` | cached robots.txt |
| `data/chunks/docs.jsonl` | 6,555 chunks: 6,182 `section`, 373 `setting` |
| `data/chunks/guc_names.json` | `{"pg_version": "16", "source": ..., "names": [...]}`: the 372 parameter names of chapter 20 |

Stats printed by the real run:

```
PostgreSQL 16 documentation: https://www.postgresql.org/docs/16/
pages: 1169 total, 0 fetched this run, 1169 from cache (0 HTTP requests)
  pages chunked: 1133
  pages no text of their own (contents only): 15
  pages release notes (left for T2): 19
  pages skipped (index, legal notice): 2
chunks: 6555 total
  section: 6182
  setting: 373
  text length: median 1011, max 4095 characters; 1028 chunks mention at least one parameter
setting chunks (one per configuration parameter): 373
target settings:
  shared_buffers                   docs:runtime-config-resource#GUC-SHARED-BUFFERS  (mentioned in 17 chunks)
  work_mem                         docs:runtime-config-resource#GUC-WORK-MEM  (mentioned in 14 chunks)
  effective_cache_size             docs:runtime-config-query#GUC-EFFECTIVE-CACHE-SIZE  (mentioned in 3 chunks)
  random_page_cost                 docs:runtime-config-query#GUC-RANDOM-PAGE-COST  (mentioned in 6 chunks)
  max_parallel_workers_per_gather  docs:runtime-config-resource#GUC-MAX-PARALLEL-WORKERS-PER-GATHER  (mentioned in 6 chunks)
  autovacuum                       docs:runtime-config-autovacuum#GUC-AUTOVACUUM  (mentioned in 67 chunks)
  max_wal_size                     docs:runtime-config-wal#GUC-MAX-WAL-SIZE  (mentioned in 12 chunks)
  default_statistics_target        docs:runtime-config-query#GUC-DEFAULT-STATISTICS-TARGET  (mentioned in 12 chunks)
  jit_above_cost                   docs:runtime-config-query#GUC-JIT-ABOVE-COST  (mentioned in 6 chunks)
  maintenance_work_mem             docs:runtime-config-resource#GUC-MAINTENANCE-WORK-MEM  (mentioned in 17 chunks)
wrote 6555 chunks to data/chunks/docs.jsonl
wrote 372 parameter names to data/chunks/guc_names.json
```

### Chunk schema (`pgdiag.chunks.Chunk`, one JSON object per line)

| Field | Meaning |
|---|---|
| `chunk_id` | Stable, unique, no whitespace. Prefix per source: `docs:`, `relnotes:`, `ml:` (`chunks.ID_PREFIXES`; `validate()` enforces it). |
| `source` | `docs` \| `release_notes` \| `mailing_list` |
| `url` | Where the text came from, with `#fragment` when there is one |
| `title` | Heading path, e.g. `20.4. Resource Consumption — 20.4.1. Memory — work_mem` |
| `text` | Plain text. The title is not repeated in it. |
| `settings` | Sorted parameter names mentioned in `text` (`find_settings`) |
| `meta` | Source-specific, JSON-serialisable |

`meta` for docs chunks: `chunk_type` (`setting` \| `section`), `pg_version`
(`"16"`), `page` (file name without `.html`), `page_title`, `parent_title` (the
page's "Up" link, e.g. `Chapter 20. Server Configuration`), `section_path`
(list of headings), `section_id`, `part`, `parts`; on setting chunks also
`setting`, `setting_type` (`integer`, `boolean`, ...) and, for parameters
documented together, `documented_with`. Only setting chunks have `meta.setting`.

Docs chunk ids:

- setting: `docs:<page>#GUC-<NAME>`, e.g. `docs:runtime-config-resource#GUC-WORK-MEM`
- section: `docs:<page>#<SECTION-ID>`, e.g. `docs:planner-stats#PLANNER-STATS-EXTENDED`;
  with `:pN` when the section was split, e.g. `docs:routine-vacuuming#AUTOVACUUM:p2`.
  Sections whose HTML id is build-generated (`id-1.9.3.184.8`, on reference
  pages) use the heading instead: `docs:sql-vacuum#notes:p1`.

The ten target settings:

```
shared_buffers                   docs:runtime-config-resource#GUC-SHARED-BUFFERS
work_mem                         docs:runtime-config-resource#GUC-WORK-MEM
effective_cache_size             docs:runtime-config-query#GUC-EFFECTIVE-CACHE-SIZE
random_page_cost                 docs:runtime-config-query#GUC-RANDOM-PAGE-COST
max_parallel_workers_per_gather  docs:runtime-config-resource#GUC-MAX-PARALLEL-WORKERS-PER-GATHER
autovacuum                       docs:runtime-config-autovacuum#GUC-AUTOVACUUM
max_wal_size                     docs:runtime-config-wal#GUC-MAX-WAL-SIZE
default_statistics_target        docs:runtime-config-query#GUC-DEFAULT-STATISTICS-TARGET
jit_above_cost                   docs:runtime-config-query#GUC-JIT-ABOVE-COST
maintenance_work_mem             docs:runtime-config-resource#GUC-MAINTENANCE-WORK-MEM
```

### Chunking decisions

- **One chunk per parameter.** On `runtime-config-*.html` every
  `<dt id="GUC-...">` with its `<dd>` is one `setting` chunk: first line
  `name (type)`, then the description. It is never split (the longest is
  4,095 characters) and never merged.
- **Parameters documented together** (`debug_print_parse` /
  `debug_print_rewritten` / `debug_print_plan`, and `log_statement_stats` /
  `log_parser_stats` / `log_planner_stats` / `log_executor_stats`) get one chunk
  each with the shared description repeated and a "Documented together with"
  line. No target setting is affected.
- **`max_replication_slots`** is documented twice (sending servers,
  subscribers) and therefore has two setting chunks
  (`...#GUC-MAX-REPLICATION-SLOTS` and `...#GUC-MAX-REPLICATION-SLOTS-SUBSCRIBER`).
  It is the only name with more than one. So there are 373
  setting chunks for 372 names.
- The `GUC-` anchors under `ssl_ciphers` are cipher keywords, not parameters;
  they stay inside the `ssl_ciphers` chunk.
- **Sections**: a section's chunk holds only its own text; each subsection is
  its own chunk. Sections longer than 1,800 characters are cut on block
  boundaries (paragraph, list item, table, code listing) into parts; a tail
  under 180 characters joins the previous part, so the hard maximum for a
  section chunk is 1,980 characters. Tables are one line per row with ` | `
  between cells; code keeps its line breaks. Admonitions start with `Note: `,
  `Tip: `, `Warning: `.
- Small sections stay as they are: 339 section chunks are shorter than 60
  characters, mostly "See Also" lists on reference pages (`docs:sql-explain#see-also`
  is just `ANALYZE`). T4 may want to leave very short chunks out of the index.
- One parameter's term line is not `name (type)`: `recovery_target = 'immediate'`
  (so it has no `meta.setting_type`).
- **Dropped**: navigation header and footer, tables of contents, index
  markers, heading permalinks, the "Submit correction" box, the site header.
- **Pages without chunks**: `index`, `bookindex` (not even downloaded),
  `legalnotice`, and the release notes `release`, `release-16`,
  `release-16-1` ... `release-16-16`, `release-prior` (T2 owns them; they are in
  the cache). Pages that are only a table of contents produce nothing.
- The server-configuration chapter is **chapter 20** in the current
  PostgreSQL 16 documentation (16.16), not 19.
- The documentation is fetched as it is today (16.16). The cache has no expiry.

### Setting-mention detection (`find_settings`)

The text is cut into identifier tokens (`[A-Za-z_][A-Za-z0-9_]*`) and each
token is compared with the names, so `autovacuum` is not found in
`autovacuum_naptime` or `log_autovacuum_min_duration`, nor `work_mem` in
`maintenance_work_mem`. The ten targets and all names with an underscore match
case-insensitively. Other one-word names (`jit`, `ssl`, `fsync`) must match
exactly, because `JIT` or `SSL` usually mean the feature. `port` and `role` are
ignored unless passed explicitly. `find_settings(text)` uses
`load_guc_names()`: all names from `data/chunks/guc_names.json`, or just the
ten targets when that file does not exist. Note that `autovacuum` is tagged
whenever the word stands alone, whether it means the parameter or the feature.

### What later tasks need to know

- **T2 (release notes)**: the pages are already cached, so
  `PoliteFetcher().get("https://www.postgresql.org/docs/16/release-16-4.html")`
  costs no request. `pgdiag.ingest.docs.crawl.crawl(fetcher, offline=True)`
  yields `(url, html_bytes)` for every cached page; filter with
  `pgdiag.ingest.docs.parse.is_release_page(page_name(url))`. Build
  `Chunk(source="release_notes", chunk_id="relnotes:...")`, fill `settings`
  with `find_settings(text)`, write with
  `write_jsonl(cfg.chunks_dir / chunks.RELEASE_NOTES_CHUNKS_FILE, chunks)`.
  `parse.clean()` and `parse.split_into_parts()` are reusable.
- **T3 (mailing list): robots.txt forbids most of the archive.** The cached
  `https://www.postgresql.org/robots.txt` disallows `/list/` (month and thread
  listings, and the mbox downloads under `/list/<name>/mbox/`),
  `/message-id/raw/`, `/message-id/flat/`, `/message-id/mbox/`, `/search/`,
  `/account/`, `/admin/` and `/docs/devel/`, for every user agent. Individual
  message pages `https://www.postgresql.org/message-id/<id>` are allowed, but
  there is no allowed listing to discover them from. `PoliteFetcher` refuses
  disallowed URLs. `get(url, check_robots=False)` exists (it is used for
  robots.txt itself); using it for the archive is a decision for the project
  owner, not something to do silently.
- **T4 (index)**: read with `read_jsonl(cfg.chunks_dir / "docs.jsonl")`. Embed
  `title + "\n" + text`: the heading path is only in `title`. Setting chunks
  can be longer than the embedding model's 512 tokens (10 of them
  exceed 1,800 characters); the start of each (name, type, first paragraph)
  is what matters most and BM25 sees the whole text. `settings`, `source` and
  `meta.chunk_type` / `meta.setting` are ready to use as filters. For the T4
  acceptance test the expected chunk for a setting is the one with
  `meta.chunk_type == "setting" and meta.setting == name`.
- New config: none. New environment variable for tests:
  `PGDIAG_REQUIRE_DATA=1` (fail instead of skip when generated files are missing).
- New marker: `data`.

### Dependencies added

`requests==2.34.2`, `beautifulsoup4==4.15.0`, `lxml==6.1.3` (transitive:
certifi, charset-normalizer, idna, urllib3, soupsieve, typing_extensions; all
in `requirements.lock`).

---

## T2 Release notes (done)

### What was built

- **`pgdiag/ingest/release_notes/parse.py`**: `parse_release_index()` reads the
  list of release pages from the appendix index (`release.html`); file names
  are never guessed. `parse_release_page()` turns one release page into chunks.
  `group_items()` packs short neighbouring items.
- **`pgdiag/ingest/release_notes/cli.py`**: `pgdiag-ingest-release-notes`
  (`ingest()`, `print_stats()`, `main()`).
- Tests: `tests/test_release_notes_parse.py` and `test_release_notes_cli.py`
  (offline; trimmed real pages in `tests/fixtures/release_notes/`, 72 KB in
  all, and the fake transport from `tests/fakes.py`), and
  `test_release_notes_real_data.py` (marker `data`: the acceptance test and
  checks of the real chunk file against the cached pages).
- No new dependency. `requirements.lock` is unchanged.

### How to run it

```sh
make ingest-release-notes                          # = .venv/bin/pgdiag-ingest-release-notes
.venv/bin/pgdiag-ingest-release-notes --chunk-only     # only the cache; makes no request at all
.venv/bin/pgdiag-ingest-release-notes --download-only  # only fill data/raw/
.venv/bin/pgdiag-ingest-release-notes --output PATH --max-chars 1000 --min-item-chars 250
make test                                          # includes the real-data tests once the file exists
make test-data                                     # only the real-data tests; fails if a file is missing
```

Run `make ingest-docs` first: it caches the pages (so this stage makes no
request) and writes `data/chunks/guc_names.json`, which this stage needs for
`settings` (it stops with an explanation when that file is absent). Pages
that are not cached are fetched through `PoliteFetcher`. It takes about two
seconds. If the index or a release page is missing, cannot be parsed, has no
release-note item, or is not the release the index says it is, the command
lists the problems, exits with status 1 and writes nothing (an existing chunk
file is left as it was).

### Output

`data/chunks/release_notes.jsonl`: 635 chunks from 17 releases (16.0 to 16.16),
1,076 release-note items. Stats printed by the real run:

```
PostgreSQL 16 release notes: https://www.postgresql.org/docs/16/release.html
pages: 18 used (the index and 17 release pages), 0 fetched this run, 18 from cache (0 HTTP requests)
  ignored (no PostgreSQL 16 notes): release-prior (Prior Releases)
versions: 17 (16.0 to 16.16)
  version  released    items  chunks  (items / prose / overview)
  16.0     2023-09-14    206      58  (56 / 1 / 1)
  16.1     2023-11-09     59      36  (35 / 1 / 0)
  16.2     2024-02-08     70      37  (36 / 1 / 0)
  16.3     2024-05-09     57      36  (35 / 1 / 0)
  16.4     2024-08-08     54      42  (41 / 1 / 0)
  16.5     2024-11-14     54      40  (39 / 1 / 0)
  16.6     2024-11-21      8       6  (5 / 1 / 0)
  16.7     2025-02-13     55      31  (30 / 1 / 0)
  16.8     2025-02-20      2       3  (2 / 1 / 0)
  16.9     2025-05-08     48      29  (28 / 1 / 0)
  16.10    2025-08-14     59      42  (41 / 1 / 0)
  16.11    2025-11-13     59      38  (37 / 1 / 0)
  16.12    2026-02-12     45      31  (30 / 1 / 0)
  16.13    2026-02-26      6       4  (3 / 1 / 0)
  16.14    2026-05-14     50      41  (40 / 1 / 0)
  16.15    2026-08-13     95      72  (71 / 1 / 0)
  16.16    2026-11-12    149      89  (88 / 1 / 0)
chunks: 635 total
  items: 617
  overview: 1
  prose: 17
  release-note items: 1076, 457 as a chunk of their own, the rest in 160 groups of up to 6 short items
  text length: median 357, max 2293 characters; 44 chunks mention at least one parameter
target settings (release-note chunks that mention each):
  shared_buffers                   2
  work_mem                         0
  effective_cache_size             0
  random_page_cost                 0
  max_parallel_workers_per_gather  0
  autovacuum                       5
  max_wal_size                     0
  default_statistics_target        0
  jit_above_cost                   0
  maintenance_work_mem             0
wrote 635 chunks to data/chunks/release_notes.jsonl
```

The zeros are real: the cached PostgreSQL 16 release notes never name eight of
the ten target settings (checked with `grep` on the raw pages).

### Chunks

Three kinds (`meta.chunk_type`):

- `items`: one release-note item (its summary line, then its explanatory
  paragraphs, code listings and numbered steps, one per line), or a group of
  adjacent short items separated by blank lines.
- `prose`: the text of "Migration to Version X" (one per release). For 16.0
  the 13 incompatibilities listed in that section follow as `items` chunks
  (`relnotes:16.0#migration:i1` ...).
- `overview`: the "Overview" of 16.0, one chunk, its seven highlights as bullets.

| Field | Value |
|---|---|
| `chunk_id` | `relnotes:<version>#<section>` for prose and overview, `relnotes:<version>#<section>:i<N>` for one item, `...:i<N>-<M>` for a group. `<section>` is the section's HTML id without the release prefix, lower-cased. N and M count the items of that section from 1. Examples: `relnotes:16.3#changes:i12`, `relnotes:16.0#optimizer:i1-6`, `relnotes:16.3#migration`, `relnotes:16.0#highlights`. |
| `source` | `release_notes` |
| `url` | Page URL with the section's anchor, e.g. `https://www.postgresql.org/docs/16/release-16-3.html#RELEASE-16-3-CHANGES` (items have no anchors of their own) |
| `title` | `PostgreSQL 16.3 release notes — Changes`, `PostgreSQL 16 release notes — Changes — Server — Optimizer` |
| `settings` | `find_settings(text)` with every name in `guc_names.json` except `port` and `role` |

`meta`: `version` (`"16.0"` for the major release, `"16.N"` for minor ones),
`pg_major` and `pg_version` (both `"16"`; `pg_version` is the key the docs
chunks use), `release_kind` (`major` \| `minor`), `release_date` (ISO date from
the page's "Release date:" line), `page` (`release-16-3`), `page_title`
(`Release 16.3`), `section` (`Changes`), `section_path` (list, e.g.
`["Changes", "Server", "Optimizer"]`), `section_id` (the HTML id),
`section_number` (`E.14.2`), `chunk_type`, `part`, `parts`. On `items` chunks
also `item_index` (first item), `item_last`, `item_count`, `authors` (list of
names) and `commits` (list of `https://postgr.es/c/...` links).

### Parsing decisions and caveats

- **Chunk-id prefix is `relnotes:`**, not `release_notes:`. T1 fixed the
  prefixes in `chunks.ID_PREFIXES` and `Chunk.validate()` enforces them.
- **Grouping**: an item of 250 characters or more is a chunk of its own. Shorter
  neighbours in the same section are packed, in order, into groups of at most
  1,000 characters and 6 items. A group never crosses a section or a long item,
  so a short item between two long ones stays alone (the shortest chunk is 44
  characters). 457 items are alone; 619 are in 160 groups.
- **No item is split.** Splitting only starts above 3,000 characters and the
  longest item has 2,293 (`relnotes:16.5#changes:i5`, with a long SQL query).
  Two chunks exceed 1,800 characters; all have `parts == 1`.
- **Author credits** (the parenthesis ending each summary, e.g. `(Tom Lane)`)
  are removed from the text and stored in `meta.authors`. Only a parenthesis
  after a space, without digits or nested parentheses, counts as a credit;
  `(CVE-2024-4317)` and `pg_size_pretty(numeric)` stay in the text. All 1,076
  items have a credit. "Thanks ... for reporting this problem" sentences stay.
- **Commit links** (the "§" after each summary) are removed from the text and
  stored in `meta.commits`.
- **Left out**: the "Release date" line (it is in `meta.release_date`); the
  sentence "This release contains a variety of fixes from 16.N. For information
  about new features in major release 16, see ..."; the lead-in of "Changes" in
  16.0 ("Below you will find a detailed account ..."); the "Acknowledgments"
  list of names in 16.0; navigation, tables of contents, heading permalinks.
  Any other paragraph outside a list becomes a `prose` chunk, so nothing
  unexpected is dropped silently.
- **Section numbers** ("E.14.2.") are not in titles or ids, only in
  `meta.section_number`. They shift with every new minor release (16.3 was
  E.14 on the day of the download).
- **Cross-references** that show only a section number are completed with the
  section's name: `see Section E.16 (Release 16.1)`.
- **Release list**: taken from the table of contents of `release.html`. Entries
  that are not `Release 16[.N]` are ignored ("Prior Releases", which only links
  to older branches). The list must run from 16.0 to 16.N without a gap or the
  stage stops.
- **Stability of ids**: ids contain item numbers, so they stay the same as long
  as the pages and the two grouping thresholds do. Published release notes are
  rarely edited, and the cache has no expiry.
- `port` and `role` are not tagged in release-note chunks. Docs chunks do tag
  `port` (102 chunks), because T1 passes its name list to `find_settings`
  explicitly. No target setting is affected.

### What later tasks need to know

- **T4 (index)**: read `read_jsonl(cfg.chunks_dir / chunks.RELEASE_NOTES_CHUNKS_FILE)`
  next to `docs.jsonl`. Embed `title + "\n" + text` as for the docs; the
  version is only in the title and in `meta`. `meta` holds lists (`authors`,
  `commits`, `section_path`) and `release_date` could be `null` on a page
  without a date line (none today). Chroma metadata values are normally
  scalars, so check what the pinned version accepts and join or drop lists and
  nulls when writing to the store. The docs chunks have list values too. Filters ready to use: `source`,
  `settings`, `meta.version`, `meta.chunk_type`.
- **T8 (retrieval) and T12 (evaluation)**: the release notes say very little
  about the ten target settings: only `shared_buffers` (2 chunks) and
  `autovacuum` (5 chunks) are mentioned at all. Their value is in symptoms and
  behaviour changes (planner, vacuum, WAL, memory use), not in parameter
  names. Do not expect a release-note chunk for each setting.
- New config: none. New marker: none (the real-data tests use `data`).

---

## T3 Mailing list: skipped (owner decision)

T3 (pgsql-performance threads) was **not built**. The project owner decided to
skip it.

- **Why**: the cached `https://www.postgresql.org/robots.txt` disallows, for
  every user agent, `/list/` (month and thread listings and the mbox
  downloads), `/message-id/raw/`, `/message-id/flat/`, `/message-id/mbox/` and
  `/search/`. Single message pages are allowed, but there is no permitted way
  to discover them or to download threads in bulk. The owner chose to respect
  `robots.txt` strictly. No request was made to any of those paths, and
  `check_robots=False` was not used.
- **Result**: the RAG index has two sources, `docs` and `release_notes`.
  `data/chunks/` holds `docs.jsonl`, `release_notes.jsonl` and `guc_names.json`.
  There is no `mailing_list.jsonl`.
- **Left in place, unused**: the `mailing_list` source, the `ml:` id prefix and
  `MAILING_LIST_CHUNKS_FILE` in `pgdiag.chunks` (a test covers them); the empty
  package `pgdiag.ingest.mailing_list` (its docstring explains the skip); the
  config values `MAILING_LIST_START_YEAR` / `MAILING_LIST_END_YEAR`. No
  `pgdiag-ingest-mailing-list` command exists.
- **T4 must** build the index from the chunk files that exist and must not fail
  because `mailing_list.jsonl` is absent. Its per-source counts should list the
  sources present (`docs`, `release_notes`). A `source` filter of
  `mailing_list` simply returns nothing.
- **T8 to T12**: prompts, reports and the write-up must describe the knowledge
  base as PostgreSQL 16 docs plus release notes, without mailing-list threads.
