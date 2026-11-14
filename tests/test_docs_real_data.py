"""Acceptance and sanity checks on the real ``data/chunks/docs.jsonl`` (T1 output).

Skipped, with instructions, when the file has not been generated. Once it
exists every check here must pass: a wrong chunk file is a failure, not a skip.
``PGDIAG_REQUIRE_DATA=1`` (``make test-data``) turns the skip into a failure.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict

import pytest

from pgdiag.chunks import DOCS_CHUNKS_FILE, GUC_NAMES_FILE, Chunk, find_settings, read_jsonl
from pgdiag.config import get_config
from pgdiag.ingest.docs.crawl import DOCS_BASE_URL
from pgdiag.ingest.docs.parse import DEFAULT_MAX_CHARS, is_config_page, is_skipped_page
from pgdiag.settings import TARGET_SETTINGS

pytestmark = pytest.mark.data

#: Where each target setting is documented and how its description starts
#: (PostgreSQL 16 documentation, chapter 20 "Server Configuration").
TARGETS: dict[str, tuple[str, str, str]] = {
    "shared_buffers": (
        "runtime-config-resource",
        "integer",
        "Sets the amount of memory the database server uses for shared memory buffers.",
    ),
    "work_mem": (
        "runtime-config-resource",
        "integer",
        "Sets the base maximum amount of memory to be used by a query operation",
    ),
    "effective_cache_size": (
        "runtime-config-query",
        "integer",
        "Sets the planner's assumption about the effective size of the disk cache",
    ),
    "random_page_cost": (
        "runtime-config-query",
        "floating point",
        "Sets the planner's estimate of the cost of a non-sequentially-fetched disk page.",
    ),
    "max_parallel_workers_per_gather": (
        "runtime-config-resource",
        "integer",
        "Sets the maximum number of workers that can be started by a single Gather or Gather Merge",
    ),
    "autovacuum": (
        "runtime-config-autovacuum",
        "boolean",
        "Controls whether the server should run the autovacuum launcher daemon.",
    ),
    "max_wal_size": (
        "runtime-config-wal",
        "integer",
        "Maximum size to let the WAL grow during automatic checkpoints.",
    ),
    "default_statistics_target": (
        "runtime-config-query",
        "integer",
        "Sets the default statistics target for table columns without a column-specific target",
    ),
    "jit_above_cost": (
        "runtime-config-query",
        "floating point",
        "Sets the query cost above which JIT compilation is activated",
    ),
    "maintenance_work_mem": (
        "runtime-config-resource",
        "integer",
        "Specifies the maximum amount of memory to be used by maintenance operations",
    ),
}

TERM_LINE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]* \([a-z ]+\)$")


@pytest.fixture(scope="module")
def chunks() -> list[Chunk]:
    path = get_config().chunks_dir / DOCS_CHUNKS_FILE
    if not path.is_file():
        reason = (
            f"{path} does not exist. Generate it with `make ingest-docs` "
            "(= .venv/bin/pgdiag-ingest-docs; the first run downloads about 1,200 pages "
            "from postgresql.org at one request per second, roughly 20 minutes)."
        )
        if os.environ.get("PGDIAG_REQUIRE_DATA") == "1":
            pytest.fail(reason)
        pytest.skip(reason)
    return read_jsonl(path)  # validates the schema and id uniqueness; raises if broken


@pytest.fixture(scope="module")
def setting_chunks(chunks) -> list[Chunk]:
    return [c for c in chunks if c.meta.get("chunk_type") == "setting"]


def test_targets_table_covers_exactly_the_ten_target_settings():
    assert sorted(TARGETS) == sorted(TARGET_SETTINGS)


# ----------------------------------------------------------------- acceptance


@pytest.mark.parametrize("name", TARGET_SETTINGS)
def test_each_target_setting_has_exactly_one_dedicated_chunk(chunks, name):
    page, setting_type, description = TARGETS[name]
    dedicated = [
        c
        for c in chunks
        if c.source == "docs"
        and c.meta.get("chunk_type") == "setting"
        and c.meta.get("setting") == name
    ]
    assert len(dedicated) == 1, [c.chunk_id for c in dedicated]
    chunk = dedicated[0]

    anchor = "GUC-" + name.upper().replace("_", "-")
    assert chunk.chunk_id == f"docs:{page}#{anchor}"
    assert chunk.url == f"{DOCS_BASE_URL}{page}.html#{anchor}"
    assert chunk.title.endswith(f" — {name}") and chunk.title.startswith("20.")
    assert chunk.meta["page"] == page and chunk.meta["pg_version"] == "16"
    assert chunk.meta["setting_type"] == setting_type
    assert chunk.meta["part"] == 1 and chunk.meta["parts"] == 1
    assert name in chunk.settings

    # The text is the parameter's own entry: its type line, then its description.
    paragraphs = chunk.text.split("\n\n")
    assert paragraphs[0] == f"{name} ({setting_type})"
    assert paragraphs[1].startswith(description), paragraphs[1][:120]
    assert len(chunk.text) > 150
    # ... and nobody else's: no second "name (type)" line anywhere in it.
    term_lines = [line for line in chunk.text.split("\n") if TERM_LINE_RE.match(line)]
    assert term_lines == [f"{name} ({setting_type})"]


def test_no_other_chunk_claims_to_be_a_target_settings_dedicated_chunk(chunks):
    claims = Counter(c.meta["setting"] for c in chunks if "setting" in c.meta)
    assert {name: claims[name] for name in TARGET_SETTINGS} == dict.fromkeys(TARGET_SETTINGS, 1)
    # "setting" appears in meta only on setting chunks.
    assert all(c.meta["chunk_type"] == "setting" for c in chunks if "setting" in c.meta)


# --------------------------------------------------------------------- sanity


def test_all_chunks_are_docs_chunks_with_text_and_unique_ids(chunks):
    assert len(chunks) > 5000
    ids = [c.chunk_id for c in chunks]
    assert len(set(ids)) == len(ids)
    for chunk in chunks:
        assert chunk.source == "docs" and chunk.chunk_id.startswith("docs:")
        assert chunk.text.strip() and chunk.title.strip()
        assert chunk.url.startswith(DOCS_BASE_URL)
        assert chunk.meta["chunk_type"] in ("setting", "section")
        assert chunk.meta["pg_version"] == "16"
        assert chunk.chunk_id.startswith(f"docs:{chunk.meta['page']}#")


def test_release_notes_index_and_legal_pages_are_not_chunked(chunks):
    pages = {c.meta["page"] for c in chunks}
    assert len(pages) > 900
    assert not [p for p in pages if is_skipped_page(p)]
    assert not [
        c.chunk_id for c in chunks if "/release" in c.url or c.meta["page"].startswith("release")
    ]
    assert not {"index", "bookindex", "legalnotice"} & pages


def test_only_setting_chunks_may_exceed_the_size_limit(chunks):
    limit = DEFAULT_MAX_CHARS * 1.1
    too_long = [
        (c.chunk_id, len(c.text))
        for c in chunks
        if c.meta["chunk_type"] == "section" and len(c.text) > limit
    ]
    assert too_long == []
    assert max(len(c.text) for c in chunks) < 12_000  # even the longest parameter entry


def test_section_parts_are_complete_and_numbered(chunks):
    groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    for chunk in chunks:
        if chunk.meta["chunk_type"] == "section":
            groups[(chunk.meta["page"], chunk.meta["section_id"])].append(chunk.meta["part"])
            assert chunk.meta["section_path"], chunk.chunk_id
    for (page, section), parts in groups.items():
        assert parts == list(range(1, len(parts) + 1)), (page, section, parts)


def test_setting_chunks_cover_the_configuration_chapter(chunks, setting_chunks):
    assert 350 <= len(setting_chunks) <= 400
    for chunk in setting_chunks:
        name = chunk.meta["setting"]
        assert is_config_page(chunk.meta["page"]), chunk.chunk_id
        assert name in chunk.settings, chunk.chunk_id
        # Nearly always "name (type)"; recovery_target's term is "recovery_target = 'immediate'".
        assert chunk.text.startswith(f"{name} "), chunk.chunk_id
        assert chunk.meta["parts"] == 1 and "#GUC-" in chunk.url
        assert chunk.title.endswith(f" — {name}")
    # One chunk per parameter, except max_replication_slots, which the documentation
    # describes twice (for sending servers and for subscribers).
    counts = Counter(c.meta["setting"] for c in setting_chunks)
    assert {name: n for name, n in counts.items() if n != 1} == {"max_replication_slots": 2}
    # Parameters described together are split into one chunk each.
    together = {c.meta["setting"] for c in setting_chunks if "documented_with" in c.meta}
    assert {"debug_print_parse", "debug_print_plan", "log_planner_stats"} <= together
    assert not together & set(TARGET_SETTINGS)


def test_guc_names_file_matches_the_setting_chunks(setting_chunks):
    path = get_config().chunks_dir / GUC_NAMES_FILE
    assert path.is_file(), f"{path} is written together with docs.jsonl"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["pg_version"] == "16"
    assert payload["names"] == sorted({c.meta["setting"] for c in setting_chunks})
    assert set(TARGET_SETTINGS) <= set(payload["names"])
    assert {"seq_page_cost", "checkpoint_timeout", "autovacuum_naptime", "jit"} <= set(
        payload["names"]
    )


def test_settings_field_agrees_with_the_shared_detector(chunks):
    names = json.loads((get_config().chunks_dir / GUC_NAMES_FILE).read_text(encoding="utf-8"))[
        "names"
    ]
    for chunk in chunks[::25]:
        expected = set(find_settings(chunk.text, names))
        if chunk.meta["chunk_type"] == "setting":
            expected.add(chunk.meta["setting"])
        assert chunk.settings == sorted(expected), chunk.chunk_id
    mentions = Counter(name for c in chunks for name in c.settings)
    # Every target is discussed outside its own entry as well.
    assert all(mentions[name] >= 3 for name in TARGET_SETTINGS), mentions


def test_no_site_boilerplate_in_the_chunks(chunks):
    for chunk in chunks:
        assert "Submit correction" not in chunk.text, chunk.chunk_id
        assert "report a documentation issue" not in chunk.text, chunk.chunk_id
        assert "\xa0" not in chunk.text and "\xa0" not in chunk.title, chunk.chunk_id
