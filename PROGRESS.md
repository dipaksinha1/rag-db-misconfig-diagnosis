# Progress log

One section per task, added when the task is finished. Each section says what
was built, how to run it, and what later tasks need to know.

## Conventions for every task

- Add the stage's console script to `[project.scripts]` in `pyproject.toml`
  and its code to the matching package under `src/pgdiag/`. Update the stage
  table in `README.md` (status "done", real command name) and add a section here.
- Pin new dependencies with `==` in `pyproject.toml`, install them, then run
  `make lock` to rewrite `requirements.lock` (full `pip freeze`, used as a
  constraints file by `make setup`). Install with
  `--extra-index-url https://download.pytorch.org/whl/cpu` (as `make setup`
  does) so torch stays the CPU-only build (see T4).
- Take paths and settings from `pgdiag.config.get_config()`, and setting names
  from `pgdiag.settings`. Do not hard-code either. Since T5b there are two lists:
  `TARGET_SETTINGS` (10) is used only by the RAG stages (T1/T2/T4: chunk tagging,
  index flags and checks); `CASE_SETTINGS` (6, a subset) is what every experiment
  stage uses (workload, faults, collector, retrieval/diagnosis evaluation,
  experiment, report). The diagnosis LLM must NEVER be told either list or the
  number of candidates: the lists only define cases and score answers.
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
| `PGDIAG_DB_MEM_LIMIT` | `256m` (was `1g` until T5b) | compose only |
| `PGDIAG_DB_SHM_SIZE` | `128m` (was `256m` until T5b) | compose only |
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
  (1 GiB by default, no extra swap; **256 MB since T5b**, see the T5 section). Checked with
  `docker inspect pgdiag-db --format '{{.HostConfig.Memory}}'` -> `1073741824`.
  `/dev/shm` is 256 MB (parallel query memory). No CPU limit is set. T5 must
  generate more than 1 GiB of data (T5b: ~750 MB for the 256 MB container).
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

---

## T4 Build the index (done)

### What was built

- **`pgdiag/index/`**:
  - `build.py`: `build_index(cfg, embedder=None, options=BuildOptions(), rebuild=False)`.
    Reads every `data/chunks/*.jsonl` with `pgdiag.chunks.iter_jsonl` (ids
    checked for uniqueness across files), leaves out short docs sections (below),
    embeds `title + "\n" + text`, writes one Chroma collection and the manifest.
  - `search.py`: `HybridIndex` (load once, search many times), `Hit`,
    `FusionParams`, `rrf_fuse`, and module-level `search()`, `get_chunk()`,
    `get_chunks()`, `get_index()`, `reset_index()` sharing one lazily created index.
  - `store.py`: on-disk layout, metadata flattening (`flatten_metadata`,
    `chunk_to_record`, `record_to_chunk`), manifest read/write/check, errors
    `IndexStoreError` > `IndexNotBuilt`, `IndexMismatch`.
  - `embed.py`: `SentenceTransformerEmbedder` (CPU, normalised, model-aware
    prefixes) and the `Embedder` protocol (tests inject `tests.fakes.FakeEmbedder`).
  - `tokenize.py`: the BM25 tokenizer.
  - `probes.py`: the setting-name acceptance check and the symptom-style probe queries.
  - `cli.py`: `pgdiag-index`.
- Tests: `tests/test_index.py` (41 offline tests, about 3 s: tokenizer, RRF
  maths, source filter applied before truncation, metadata round trip,
  selection rule, manifest checks, build skip/rebuild, search modes, CLI `get`
  and `stats`) and `tests/test_index_real_data.py` (markers `data` and `slow`,
  about 15 s with one shared index: the acceptance test per setting, counts,
  manifest vs. current chunk files, source filter, exact `get_chunk` round trip
  of all 6,851 chunks, determinism within a process and across processes,
  exact vector search, and the probe table, printed).

### How to run it

```sh
make index                      # = pgdiag-index build; does nothing if inputs, model and options are unchanged
make index-rebuild              # = pgdiag-index build --rebuild
make index-check                # = pgdiag-index check: acceptance (exit 1 on failure) + symptom probes
.venv/bin/pgdiag-index search "sorts spill to temporary files" -k 5 --source docs [--mode vector|bm25] [--text] [--json]
.venv/bin/pgdiag-index get relnotes:16.3#changes:i12 docs:runtime-config-resource#GUC-WORK-MEM
.venv/bin/pgdiag-index stats
make test                       # includes the real-index tests once data/index/ exists
```

`build` options: `--min-docs-section-chars N` (default 60, 0 keeps all),
`--batch-size N` (default 32), `--query-prefix TEXT` (default: the model's
recommended one). `search` and `check` take `--rrf-k`, `--vector-weight`,
`--bm25-weight`, `--depth`.

**Setup (CPU-only torch).** `make setup` now runs
`pip install --extra-index-url https://download.pytorch.org/whl/cpu -c requirements.lock -e ".[dev]"`.
`pyproject.toml` pins `torch==2.15.0`; `requirements.lock` pins
`torch==2.15.0+cpu`, which only exists on that index, so a fresh clone gets the
CPU build (no `nvidia-*` packages; the whole venv is 1.8 GB). Checked with a
fresh venv: `pip freeze` afterwards equals `requirements.lock`. `make lock`
is unchanged (`pip freeze`).

**Model download.** The first build (or first search on a new machine) downloads
`BAAI/bge-small-en-v1.5` (about 130 MB) from huggingface.co into the standard
Hugging Face cache (`~/.cache/huggingface/hub`, not in the repo). Later loads
use the cache only (`local_files_only=True` first, network only as a fallback).

### Output

`data/index/` (gitignored):

| Path | Content |
|---|---|
| `chroma/` | one persistent Chroma collection `pgdiag_chunks` (cosine HNSW, telemetry off) |
| `manifest.json` | model + revision, dim, prefixes, input file hashes and counts, left-out ids, per-source counts, timings, library versions, fingerprint |

Build printout of the real run (2026-11-14):

```
index built 2026-11-14T17:55:13+00:00
embedding model: BAAI/bge-small-en-v1.5 @ 5c38ec7c405ec4b44b94cc5a9bb96e735b38267a (dim 384, normalized, max 512 tokens, cpu)
query prefix: 'Represent this sentence for searching relevant passages: '
inputs:
  docs.jsonl               6555 chunks  sha256 73ed50e1678d9e72...
  release_notes.jsonl       635 chunks  sha256 61779d4708d061d0...
left out: 339 ({'docs': 339}; docs sections shorter than 60 characters)
indexed chunks per source (6851 total):
  docs             6216
  release_notes     635
  mailing_list        0  (not built)
  docs chunk types: section 5843, setting 373
  release_notes chunk types: items 617, overview 1, prose 17
build time: 414.8 s (model load 4.8 s, embedding 400.6 s, Chroma 9.2 s)
```

### What is stored (one Chroma record per chunk)

- `id` = `chunk_id` exactly; `document` = `text` (not the title).
- `embedding` = normalised embedding of `title + "\n" + text`, computed by
  pgdiag (Chroma's own embedding function is never used).
- `metadata`, scalars only: `source`, `url`, `title`, `chunk_type`, `setting`
  (setting chunks), `version` (release notes), `page` (each `""` when absent),
  `settings` as `"|name1|name2|"` (`""` when none), `n_settings`, one boolean
  `mentions_<target>` per target setting, and `meta_json` (the original `meta`,
  lists and nulls included). `store.record_to_chunk` rebuilds the original
  `Chunk` exactly; a test compares all 6,851 with the chunk files.

### Decisions

- **Left out**: docs `section` chunks with text under 60 characters (339: 170
  "See Also" lists, 79 one-line "Compatibility" notes, short "Examples",
  "Arguments", "Return Value" ...). Never setting chunks; never release-note
  chunks (the 5 under 60 characters are real fixes, e.g. "Fix crash with DSM
  allocations larger than 4GB"). Their ids are in `manifest.selection.left_out_ids`.
- **Embedding**: `BAAI/bge-small-en-v1.5`, CPU, L2-normalised, batch 32. The
  BGE query instruction is added to queries only (`embed.default_prefixes`:
  BGE English models get it, E5 models get `query: `/`passage: `, others none).
  The prefixes and the exact revision are stored in the manifest and the
  searcher loads the model at that revision with those prefixes.
- **512-token truncation**: inputs longer than 512 tokens are cut by the model.
  Counted with the model's tokenizer: 281 of the 6,851 `title + text` strings
  are longer (272 docs sections, mostly with code listings or tables, up to
  1,013 tokens; 8 setting chunks, none of them a target setting's; 1
  release-note item). Their tails are not in the vector; BM25 sees all of the text.
- **BM25**: `bm25s` (Lucene variant, k1 1.5, b 0.75) over `title + "\n" +
  text`. Not persisted: rebuilt in memory from the stored chunks on load (1.4 s).
  Tokenizer (`tokenize.py`): lower-case; identifiers with `_` kept whole **and**
  split into parts (`max_parallel_workers_per_gather` -> itself + `max`,
  `parallel`, `worker`, `per`, `gather`); English stop words dropped; Snowball
  stemming (PyStemmer) for ordinary words; words with digits (`16.3`, `4mb`)
  kept as they are.
- **Vector search is exact, not HNSW.** The Chroma collection has a cosine
  HNSW index, but its query returned slightly different neighbour lists for the
  same query vector in different processes (seen: 2 of 3 runs agreed, which moved
  a hybrid rank from 23 to 24). The searcher therefore reads all embeddings once
  at load and scores every chunk exactly (`np.einsum`, float64; 6,851 x 384).
  Results are identical across processes (a test runs a search in a subprocess).
  `einsum` rather than `@`: BLAS's thread pool contends with torch's after
  each query embedding and cost about 90 ms per search.
- **Fusion**: Reciprocal Rank Fusion, `rrf_k = 60`, weights 1.0 / 1.0, depth
  100 candidates per retriever. A sweep (rrf_k 10/30/60/100, weights 1:1, 1:1.5,
  1.5:1, 1:2, depth 50/100/200) on the 10 probes plus 11 other paraphrased
  queries showed no setting that was better on both sets (differences of 1-2
  queries, in opposite directions), so the standard defaults stay. On those
  11 other queries hybrid has MRR 0.25 vs. 0.20 vector-only and 0.15 BM25-only.
- **Source filter** is applied inside both retrievers before the cut to
  `depth` (vector: similarity of other sources set to -inf; BM25: score set to
  0), never after taking k.
- **Determinism**: ties are broken by chunk id in both retrievers and by best
  component rank then chunk id in the fusion.
- **Rebuild check**: the fingerprint covers input file hashes, model name,
  build options, HNSW settings and the format version. A new build is written to
  `data/index/chroma.new/` and swapped in only when complete (the manifest is
  written last; no manifest = "not built").

### Acceptance (real index): `search(<setting name>, k=3)` finds the setting's docs chunk

```
  PASS shared_buffers                   rank 1
  PASS work_mem                         rank 1
  PASS effective_cache_size             rank 1
  PASS random_page_cost                 rank 1
  PASS max_parallel_workers_per_gather  rank 1
  PASS autovacuum                       rank 3   (1. docs:routine-vacuuming#AUTOVACUUM:p1, 2. ...#GUC-AUTOVACUUM-MAX-WORKERS)
  PASS max_wal_size                     rank 1
  PASS default_statistics_target        rank 1
  PASS jit_above_cost                   rank 1
  PASS maintenance_work_mem             rank 1
  10/10 passed
```

`make index-check` prints the full top 3 for each with component ranks.

### Symptom probes (informative)

Rank of the setting's dedicated docs chunk within the top 100 (`>100` = not
there), and the hybrid rank of the first chunk whose `settings` include the
setting:

| query | setting | hybrid | vector | bm25 | first mention |
|---|---|---|---|---|---|
| sorts spilling to temporary files on disk, external merge | work_mem | 24 | >100 | 2 | 24 |
| planner chooses sequential scans instead of index scans on SSD | random_page_cost | 10 | 5 | 34 | 10 |
| checkpoints occurring too frequently, checkpoint distance warnings | max_wal_size | 34 | 77 | 36 | 1 |
| queries not using parallel workers | max_parallel_workers_per_gather | 5 | 10 | 2 | 1 |
| JIT compilation time dominates short queries | jit_above_cost | 4 | 9 | 2 | 1 |
| dead tuples accumulating, tables never vacuumed | autovacuum | >100 | >100 | >100 | 9 |
| bad row estimates on skewed columns | default_statistics_target | >100 | >100 | 75 | 15 |
| CREATE INDEX is slow | maintenance_work_mem | >100 | >100 | >100 | 3 |
| low buffer cache hit ratio, many disk reads | shared_buffers | 15 | 6 | 59 | 15 |
| planner assumes little OS cache, avoids index scans | effective_cache_size | 1 | 2 | 1 | 1 |

### Performance (4 CPU cores, no GPU)

- Build: 415 s (embedding 401 s, about 17 chunks/s; model load 5 s; Chroma 9 s).
- Load: open the index and read all chunks and embeddings 2.7 s; model load 4.3 s
  (from cache); BM25 build 1.4 s. All three happen once per process
  (`HybridIndex.warm_up()` does the last two up front).
- Warm search, k=10: median 25 ms, max 47 ms; a 470-character query 34 ms.

### What later tasks need to know

- **API** (`from pgdiag.index import ...`):
  - `search(query, k=10, source=None, *, mode="hybrid", depth=None) -> list[Hit]`.
    `source`: `"docs"`, `"release_notes"`, `"mailing_list"` (always empty), a
    list of them, or `None`. Unknown source, empty query or `k < 1` raise
    `ValueError`.
  - `Hit`: `chunk_id`, `source`, `title`, `url`, `settings` (list), `text`,
    `score` (fused RRF score), `rank` (1-based), `vector_rank`, `vector_score`
    (cosine), `bm25_rank`, `bm25_score` (each `None` when the chunk was not among
    that retriever's candidates), `meta` (original dict); `hit.chunk` -> `Chunk`;
    `hit.to_dict()` is JSON-ready.
  - `get_chunk(chunk_id) -> Chunk` (exact original, a copy; `KeyError` if not
    indexed, which includes the 339 left-out ids), `get_chunks(ids, strict=True)`
    (order kept; `strict=False` skips unknown ids, e.g. hallucinated citations).
    Neither loads the embedding model. `chunk_id in get_index()` tests membership.
  - For other fusion settings or several configurations, create
    `HybridIndex.load(cfg, fusion=FusionParams(rrf_k=..., vector_weight=...,
    bm25_weight=..., depth=...))` once and reuse it.
  - `HybridIndex.load()` raises `IndexNotBuilt` (no index: run `make index`) or
    `IndexMismatch` (built with a different `EMBEDDING_MODEL`, other format, or
    inconsistent counts).
  - Do not use a `HybridIndex` opened before a `build_index` in the same
    process: the build swaps the directory.
- **T8 (retrieval)**: symptom-only queries often do not put the dedicated
  setting chunk in the top 10 (probe table). In 7 of the 10 probes a chunk
  that mentions the setting is in the top 10. Things that help: several queries per
  symptom document (one per symptom: temp files, checkpoints, dead tuples,
  plans ...), the vocabulary of the docs and the log lines ("temporary file",
  "checkpoints are occurring too frequently", "n_dead_tup", "Workers Planned"),
  and a larger k per query followed by de-duplication. Never put the answer's
  setting name into a query: that would leak the answer. `hit.settings` lists every
  parameter a chunk mentions.
- **T9 / T12**: chunk ids round-trip exactly (`relnotes:` ids included). Cite
  `hit.chunk_id`; resolve with `get_chunk`/`get_chunks`. The index describes
  itself in `get_index().manifest` (model revision, counts, input hashes) for
  the report.
- Release notes are 635 of 6,851 chunks and almost never name the target
  settings (see T2), so a `source="release_notes"` search is for behaviour
  changes and bugs, not parameters.

### Dependencies added

`torch==2.15.0` (CPU build `2.15.0+cpu` in the lock), `sentence-transformers==6.3.0`,
`chromadb==1.5.9`, `bm25s==0.3.16`, `PyStemmer==3.1.0`, `numpy==2.5.4`, plus their
transitive dependencies (transformers 5.23.0, tokenizers 0.23.3,
huggingface-hub 2.4.0, scipy 1.18.1, onnxruntime ...; all in `requirements.lock`).
New Makefile targets: `index`, `index-rebuild`, `index-check`. New config: none
(`EMBEDDING_MODEL` already existed). New markers: none.

---

## T5 Test database and workload (done; reworked in T5b)

### Scope change (T5b, project owner decision)

T5 first built a 3.6 GB dataset for a 1 GiB container and a workload for ten settings
(commit 2f274d7). The owner then reduced the scope, because the experiment should use a
small, simple test database and because four of the ten cases were weak or fragile on this
machine:

- **6 experiment settings** instead of 10: `shared_buffers`, `work_mem`, `random_page_cost`,
  `max_parallel_workers_per_gather`, `jit_above_cost`, `default_statistics_target`.
  Dropped from the experiment: `effective_cache_size`, `autovacuum`, `max_wal_size`,
  `maintenance_work_mem` (they keep sane baseline values and are recorded as context).
- **~750 MB of data** (tables + indexes) instead of 3.6 GB, with the **container memory limit
  lowered from 1 GiB to 256 MB** (`PGDIAG_DB_MEM_LIMIT=256m`, `PGDIAG_DB_SHM_SIZE=128m`, the
  compose and `.env.example` defaults): the data is still ~2.9x the memory.
- **5 repeats per case and mode in T11** (was 3). Nothing to implement before T11.

T5b rewrote this section; the T5 design (work queue, ledger batch, WAL-heavy pgbench script,
campaign tables, 3.6 GB profile `full`) is gone from the code. `generate --reset` drops the old
tables too (`datagen.OBSOLETE_TABLES`).

### `CASE_SETTINGS` convention (important for T6 to T12)

- `pgdiag.settings.TARGET_SETTINGS` (10 names) is unchanged and is used only by the RAG stages
  (T1/T2/T4 chunk tagging, index flags, `index check`) and by `pgdiag-db status`.
- `pgdiag.settings.CASE_SETTINGS` (6 names, a subset; test in `tests/test_settings.py`) is the
  list every experiment stage uses: workload and probe, fault injection, symptom collector,
  retrieval and diagnosis evaluation, experiment runner, report. `NON_CASE_SETTINGS` holds the
  other four.
- **The diagnosis LLM must never be shown either list, nor told how many candidates there
  are.** The lists exist only to define cases and to score answers; a prompt that enumerates
  them would turn diagnosis into multiple choice.

### What was built

- **`pgdiag-workload`** (`src/pgdiag/workload/cli.py`), one entry point with subcommands:
  `generate [--reset] [--profile standard|tiny] [--dbname X]`, `bench [--preset full|quick]
  [--label X] [--restart] [--components a,b]`, `baseline [--preset full|quick]`,
  `baseline-check`, `compare A.json B.json [--queries]`, `probe-sensitivity [--settings a,b]
  [--set name=value] [--resume file] [--single-baseline]`, `queries [--sql]`, `status`.
  Makefile: `data-gen`, `data-gen-reset`, `bench`, `bench-quick`, `baseline`,
  `baseline-quick`, `baseline-check`, `probe`.
- `workload/profiles.py`: `standard` (the experiment) and `tiny` (tests).
- `workload/datagen.py`: seeded, resumable generator (each table is marked complete in
  `wl_meta` only after indexes + ANALYZE; a re-run skips complete tables; `--reset` drops
  everything including the old T5 tables; a different seed or profile requires/forces a reset).
- `workload/queries/NN_<id>.sql` + `workload/queryset.py`: the versioned query set
  (`QUERY_SET_VERSION = "2"`), one statement per file with a header (`id`, `component`,
  `description`, `sensitive_to`, `repeat`). `sensitive_to` may only name CASE_SETTINGS
  (parse error otherwise); a test checks that every CASE_SETTINGS member is covered.
- `workload/bench.py`: runner, result schema, statistics, `compare`.
- `workload/server.py`: `apply_change(cfg, name, value)` (ALTER SYSTEM + reload or restart from
  `pg_settings.context`), `reset_to_baseline()` (ALTER SYSTEM RESET ALL + restart + verify +
  re-ANALYZE), `baseline_problems(conn)`.
- `workload/probe.py`: the sensitivity probe over exactly the six CASE_SETTINGS.
- `pgdiag/db.py`: `target_settings(conn, names=TARGET_SETTINGS)` now takes a list.
- Tests: `tests/test_workload_offline.py` (38, no DB), `tests/test_workload_db.py` (13, marker
  `db`; generates `tiny` into the separate database `pgdiag_wltest` and drops it; never writes
  to `pgdiag`; two `data` checks read the real dataset and baseline),
  `tests/test_settings.py` (CASE_SETTINGS). No new dependencies.

### Data (database `pgdiag`, profile `standard`, seed 20251114)

| Table | Rows | Heap | Indexes | Total | Notes |
|---|---|---|---|---|---|
| `wl_orders` (fact) | 3,500,000 | 359 MB | 283 MB | 642 MB | appended in time order (`order_id`, `ordered_at` correlated 1.0 with the heap, so recent orders are physically clustered: the last 45 days are ~22 MB); `product_id` log-uniform over 50,000 ids (Zipf ~1: top product 6.4% of orders, #2 3.8%, #3 2.6%); `customer_id` mildly skewed (`N*u^1.5`, top customer ~1,000 orders); `status` skewed and correlated with `ordered_at`; `warehouse_id` determines `region` (extended statistics `wl_orders_region_wh_stats`). Indexes: PK, `(customer_id)`, `(ordered_at)`, `(product_id, ordered_at)`. `customer_id` has `n_distinct = 200000` pinned (see default_statistics_target below) |
| `wl_customers` (dimension) | 200,000 | 19 MB | 10 MB | 30 MB | `customer_id` a permutation of the physical order; `country` Zipf over 40, `city` determined by `country` (extended statistics); `segment` 80/15/5% consumer/smb/enterprise. Indexes: PK, `(country, city)` |
| `pgbench_accounts` (+ branches, tellers, history) | 500,000 | 64 MB | 11 MB | 75 MB | `pgbench -i -s 5 -I dtGvp`, run in the container |
| **total** | | | | **746 MB** (database 755 MB) | **2.9x the 256 MB container limit** |

`pgdiag-workload generate --reset`: **24 s** (pgbench 1.2 s, customers 1.4 s, orders 20.5 s).
The generator's session uses `maintenance_work_mem = 64MB` (256MB with 2 parallel index-build
workers OOM-killed the 256 MB container once during development) and `work_mem = 16MB`.
The `tiny` profile (tests) takes ~2 s.

### Query set (pass order) and designed sensitivities

8 SQL files plus pgbench; `pgdiag-workload queries --sql` prints everything. All SQL is
read-only; pgbench is the only writer.

| Component | Statements (x repeats per pass) | Targets | Mechanism (verified with EXPLAIN / timings) |
|---|---|---|---|
| `pgbench` | built-in TPC-B, `-n -c 4 -j 2 -t 500 --random-seed=20251114` (2,000 transactions, ~1,800 tps) | jit_above_cost | every tiny statement gets JIT-compiled at `jit_above_cost = 0` (~0.7 ms extra per statement, ~11k statements) |
| `order_lookups` | `product_sales` x30: daily sales of one product (ranks 21-50) over the last 90 days | random_page_cost | `(product_id, ordered_at)` index scan, ~2 ms; at rpc 100 the planner switches to a full scan (~90 ms each). At rpc 10 or 30 nothing flips, at 50 only the most popular of the 30 products does |
| `order_export` | `order_export` x5: orders of the last 45 days whose customer is in market i, the country looked up per row with a scalar subquery (ORM-style) | shared_buffers | ~1M primary-key probes into `wl_customers` per pass; the hot set (customers + PK + recent orders, ~50 MB) fits in 64 MB of shared buffers; at 1MB every probe misses and copies the page from the kernel cache (2x slower). There is no disk I/O difference: the page cache holds the hot set either way |
| `revenue_reports` | `region_revenue`, `status_revenue`: full-table aggregates with numeric arithmetic/stddev | max_parallel_workers_per_gather | 3 participants vs 1: ~2.8x slower serially |
| `customer_reports` | `inactive_customers` (`NOT IN` subquery over yesterday's ~4,800 orders), `top_customers` (30-day ranking) | work_mem | the `NOT IN` is a *hashed* SubPlan only while its hash table fits in `work_mem x hash_mem_multiplier`; at 64kB it becomes a plain SubPlan re-scanned (spilled) per outer row: 20 ms -> ~3 s. A cliff, not a slope |
| `product_affinity` | `bought_together` x5: what the last 90 days' buyers of product rank 2-6 bought in the last 120 days (self-join on `customer_id`) | default_statistics_target | at 1 the 300-row sample keeps one MCV (product #1), so products #2-#6 are underestimated ~25x; the planner runs a nested loop into `wl_orders_customer_idx` (all orders of each buyer) instead of a hash join: ~0.12 s -> ~0.45 s per query |
| `small_lookups` | `customer_profile` x100 | jit_above_cost | ~0.3 ms lookups; JIT at cost 0 makes each ~4 ms |

Cross-effects seen in the probe (fine for the experiment, but T7/T9 should know them):
work_mem 64kB also makes `bought_together`'s hash join spill (+900%); rpc 100 and
max_parallel_workers_per_gather 0 also slow `bought_together` (+60-120%); jit 0 slows
`product_sales` (+160%); shared_buffers 1MB slows `product_sales` (+90%).

**Why `customer_id` has a pinned `n_distinct`**: with `default_statistics_target = 1` ANALYZE
samples 300 rows; if no `customer_id` repeats in the sample (~80% of the time), the planner
treats the column as unique, otherwise it estimates ~22k-44k distinct values. That flipped the
broken plan at random from benchmark to benchmark. `ALTER TABLE wl_orders ALTER COLUMN
customer_id SET (n_distinct = 200000)` (a common DBA fix, part of the generated schema) removes
that randomness: with it the nested loop was chosen in 10/10 ANALYZE samples at target 1 and
the hash join at target 100 for product ranks 2-7. The remaining skew estimate (`product_id`
MCVs) is robust. Note the attribute option shows up in `pg_attribute.attoptions` / `\d+`.

### Baseline configuration (`docker/postgres/postgresql.conf`)

The block of target settings is now split: the six case settings, then the other four.

| Setting | Baseline | Why |
|---|---|---|
| shared_buffers | 64MB | 1/4 of the container (restart) |
| work_mem | 8MB | no workload sort/hash spills (0 temp files per healthy pass); worst parallel hash 3 x 2 x 8MB = 48 MB |
| random_page_cost | 1.1 | SSD-like storage |
| max_parallel_workers_per_gather | 2 | 4 host CPUs (no CPU limit); `max_worker_processes` and `max_parallel_workers` stay 8 |
| jit_above_cost | 100000 | default; `jit = on` (default; `pg_jit_available()` is true) |
| default_statistics_target | 100 | default |
| effective_cache_size | 160MB | not a case: shared_buffers + what the page cache can hold |
| maintenance_work_mem | 32MB | not a case |
| max_wal_size | 1GB | not a case (default); a pass writes ~3 MB of WAL |
| autovacuum | on | not a case |

Reverted from T5: `autovacuum_naptime`/scale factors/cost limit and `checkpoint_timeout = 30min`
(they only served the dropped autovacuum/max_wal_size cases; every pass starts with a
CHECKPOINT, so no timed checkpoint falls inside a pass anyway). Logging (T0) is unchanged.
Memory check: after every run below, `docker inspect pgdiag-db` reports `OOMKilled=false` and
the server log has no "terminated by signal" or "out of memory" line except the one
development OOM noted above (generator, 21:50 UTC on 2026-11-15, before the fix).

### Benchmark design

- **Pass**: all components once, in the order above, in one connection
  (`application_name = pgdiag-workload`, `statement_timeout` 300 s as a safety net; failures are
  recorded and the run continues). Values are substituted client-side (plain literals in
  pg_stat_statements / logs / EXPLAIN). pgbench runs inside the container
  (`docker compose exec -T db pgbench ...`) with a fixed transaction count and random seed.
  Pass total = sum of components.
- **Per benchmark (untimed)**: ANALYZE `wl_customers`, `wl_orders` and the pgbench tables in the
  benchmark's own session (so the current `default_statistics_target`, broken or restored, is in
  effect), then CHECKPOINT.
- **Per pass (untimed)**: TRUNCATE `pgbench_history`, CHECKPOINT. Nothing else changes (the SQL
  is read-only; pgbench only changes balances), so every pass starts from an equivalent state.
- **Caching**: nothing is dropped; one warm-up pass. `run_benchmark(restart=True)` restarts the
  server first; `baseline` always restarts (via `reset_to_baseline`).
- **Per-pass server counters**: `wal_bytes`, `checkpoints_req`/`_timed`, `temp_files`,
  `temp_bytes`, pgbench tps. Healthy pass: ~2.7 MB WAL, 0 checkpoints, **0 temp files**.
- **Presets** (a pass is only ~6.5 s, so more measured passes than T5 had):
  `full` = 1 warm-up + **7** measured (baseline, T6), `quick` = 1 + **5** (T10/T11),
  `smoke` = 0 + 1 (tests only). The task suggested a 10-20 s pass; 6.5 s with more passes
  gives the same stability for less time, and each case still clears +25% (below).

### Result files

`results/bench/<UTC stamp>_<label>_<preset>.json` (gitignored), schema_version 2 (unchanged):
`settings` / `settings_detail` now hold the six CASE_SETTINGS; the four other targets are in
`context_settings` together with jit, hash_mem_multiplier, worker limits, checkpoint settings,
etc. `compare()` reports `settings_changed` (case settings) and `context_changed`.
Reference baselines: `results/baseline.json` (full) and `results/baseline-quick.json` (quick);
the previous run of each is `*.prev.json`. The T5 (3.6 GB) baselines were moved to
`results/t5-old/`.

### API for later tasks (unchanged)

```python
from pgdiag.settings import CASE_SETTINGS
from pgdiag.workload.bench import run_benchmark, compare, load_result, format_compare
from pgdiag.workload.cli import load_baseline  # load_baseline("full" | "quick")
from pgdiag.workload import server  # apply_change, reset_to_baseline

res = run_benchmark(preset="quick", label="case-work_mem")  # saves results/bench/...
diff = compare(load_baseline("quick"), res)  # diff["total"]["rel_diff_pct"], diff["components"][c]
```

### Timings (4 CPUs, no CPU limit, 256 MB container)

- One pass: **~6-7 s** (pgbench 1.3-1.5, order_lookups 0.07, order_export 2.5-2.9,
  revenue_reports 1.5-1.6, customer_reports 0.18, product_affinity 0.55-0.6, small_lookups 0.03).
- `full` preset (1 + 7): **~52-55 s** wall; `baseline` (+ RESET ALL + restart + ANALYZE) ~65 s.
- `quick` preset (1 + 5): **~40 s** wall. T11 with 6 cases x 2 modes x 5 repeats = 60 quick runs
  of the fix tester would take ~40 min of benchmarking (plus restarts for shared_buffers).
- Probe (6 settings, paired baselines, quick): **~10 min**.

### Baseline (acceptance)

`pgdiag-workload baseline` run four times in a row (full preset, each starting with ALTER
SYSTEM RESET ALL + restart), `baseline-check` after the second and the fourth:

| run | started (UTC) | median total | min-max | cv |
|---|---|---|---|---|
| 1 | 23:05:02 | 6.30 s | 5.86-6.60 | 4.7% |
| 2 | 23:05:57 | 6.83 s | 6.30-7.35 | 5.3% |
| 3 | 23:18:57 | 6.49 s | 5.87-6.71 | 5.2% |
| 4 | 23:19:52 | 6.28 s | 6.14-6.63 | 2.6% |

- Runs 1 vs 2: `PASS: the two baseline median totals differ by 8.42% (limit 10.0%)` (every
  component was 5-10% slower in run 2: host drift, not a workload change).
- Runs 3 vs 4 (the stored pair: `results/baseline.prev.json`, `results/baseline.json`):
  `PASS: the two baseline median totals differ by 3.25% (limit 10.0%)`.
- Quick baseline (`results/baseline-quick.json`): 6.54 s, cv 1.4%.

All four runs lie within 6.28-6.83 s (8.8%). Components under ~0.1 s (`order_lookups`,
`small_lookups`, `customer_reports`) vary by 20-70% between passes: compare totals, and treat
component differences under ~20% as noise.

### Sensitivity probe (`pgdiag-workload probe-sensitivity`, quick preset, paired baselines)

Each broken run is compared with a baseline run made immediately before it.
`session` = `SET` in the benchmark session + `PGOPTIONS` for pgbench (the benchmark's own
ANALYZE makes default_statistics_target take effect); `restart` = ALTER SYSTEM + restart.
Report: `results/probe/20261115T230735Z_probe_quick.json`.

| setting | bad value | applied via | total baseline -> broken | total | main component |
|---|---|---|---|---|---|
| shared_buffers | 1MB | restart | 7.0 -> 9.6 s | **+36%** | order_export 2.97 -> 5.54 s (+86%) |
| work_mem | 64kB | session | 6.7 -> 15.4 s | **+131%** | customer_reports 0.19 -> 3.40 s (+1723%; `inactive_customers` 0.02 -> 3.2 s); also bought_together 0.65 -> 6.6 s |
| random_page_cost | 100 | session | 6.7 -> 10.1 s | **+51%** | order_lookups 0.07 -> 2.80 s (+4085%) |
| max_parallel_workers_per_gather | 0 | session | 7.0 -> 9.8 s | **+41%** | revenue_reports 1.59 -> 4.66 s (+193%) |
| jit_above_cost | 0 | session | 6.3 -> 14.9 s | **+139%** | pgbench 1.34 -> 9.44 s (+602%); small_lookups 0.03 -> 0.45 s |
| default_statistics_target | 1 | session | 6.3 -> 8.1 s | **+29%** | product_affinity 0.57 -> 2.29 s (+305%) |

All six clear +25% on the total. The probe ended with ALTER SYSTEM RESET ALL + restart +
verification; `pg_file_settings` has no `postgresql.auto.conf` entry afterwards.

### Advice for T6 (fault cases)

- **Apply / reset**: `server.apply_change(cfg, name, value)` and `server.reset_to_baseline()`.
  Restart needed: shared_buffers. The other five are user-settable, but a *case* must set them
  server-wide (ALTER SYSTEM + `pg_reload_conf()`), not per session.
- **default_statistics_target needs ANALYZE**: `run_benchmark` ANALYZEs the tables at the start
  of every benchmark, so after ALTER SYSTEM + reload the bad value takes effect in the next
  benchmark, and a reset is undone by the next benchmark. If T7 collects symptoms (EXPLAIN,
  pg_stats) without `run_benchmark`, call `server.analyze_static_tables(conn)` first. Do not
  remove the pinned `n_distinct` on `wl_orders.customer_id`: without it the dst=1 plan is
  random.
- **Bad values (verified above)**: shared_buffers 1MB (128kB behaved the same in a spot test),
  work_mem 64kB (cliff: the NOT IN plan changes once the ~4,800-row hash no longer fits in
  2 x work_mem; values of a few hundred kB may already flip it, untested), random_page_cost 100
  (10 and 30 do not flip `product_sales`, 50 flips 1 of the 30 lookups: keep 100),
  max_parallel_workers_per_gather 0, jit_above_cost 0 (pgbench and small lookups; values >= ~10
  leave pgbench alone), default_statistics_target 1.
- **Fragile / weaker cases**: default_statistics_target (+29%, smallest margin; depends on the
  data's skew and the pinned n_distinct: do not change the data profile). shared_buffers
  (+36%) is CPU overhead of buffer misses only (no extra disk reads: the page cache holds the
  hot set), so its symptoms are subtle: more `shared_blks_read` in pg_stat_statements with low
  I/O time, a low buffer hit ratio. work_mem is a cliff.
- **Noise**: totals are stable to ~3-8% between independent runs (host speed drifts); measure
  broken runs close in time to a baseline (the probe pairs them).
- T7 symptoms: a healthy pass has no temp files and no requested checkpoints; queries over
  250 ms are logged (revenue reports, order_export repeats, top_customers, bought_together
  when broken, NOT IN when broken); the benchmark connection's `application_name` is
  `pgdiag-workload`.
