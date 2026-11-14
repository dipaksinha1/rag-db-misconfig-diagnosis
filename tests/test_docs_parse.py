"""Chunking of documentation pages, tested offline on trimmed real pages in tests/fixtures/docs/."""

from __future__ import annotations

from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from pgdiag.chunks import Chunk, validate_chunks
from pgdiag.ingest.docs.parse import (
    DEFAULT_MAX_CHARS,
    is_config_page,
    is_release_page,
    is_skipped_page,
    parse_page,
    setting_names,
    split_into_parts,
    tag_settings,
)
from pgdiag.settings import TARGET_SETTINGS

FIXTURES = Path(__file__).parent / "fixtures" / "docs"
BASE = "https://www.postgresql.org/docs/16/"
CONFIG_PAGES = ("runtime-config-resource", "runtime-config-logging", "runtime-config-connection")
ALL_PAGES = (*CONFIG_PAGES, "runtime-config", "planner-stats", "sql-vacuum")


def html(page: str) -> bytes:
    return (FIXTURES / f"{page}.html").read_bytes()


def parse(page: str, **kwargs) -> list[Chunk]:
    return parse_page(html(page), f"{BASE}{page}.html", **kwargs)


def by_id(chunks: list[Chunk]) -> dict[str, Chunk]:
    assert len({c.chunk_id for c in chunks}) == len(chunks)
    return {c.chunk_id: c for c in chunks}


def settings_of(chunks: list[Chunk]) -> list[Chunk]:
    return [c for c in chunks if c.meta["chunk_type"] == "setting"]


def sections_of(chunks: list[Chunk]) -> list[Chunk]:
    return [c for c in chunks if c.meta["chunk_type"] == "section"]


def squash(text: str) -> str:
    return "".join(text.split())


# ------------------------------------------------------------ setting chunks


def test_every_parameter_entry_becomes_exactly_one_setting_chunk():
    chunks = parse("runtime-config-resource")
    settings = settings_of(chunks)
    assert [c.meta["setting"] for c in settings] == [
        "shared_buffers",
        "work_mem",
        "hash_mem_multiplier",
        "maintenance_work_mem",
        "autovacuum_work_mem",
        "vacuum_cost_delay",
        "vacuum_cost_limit",
        "max_parallel_workers_per_gather",
    ]
    # One chunk per <dt id="GUC-..."> in the page, no more, no fewer.
    soup = BeautifulSoup(html("runtime-config-resource"), "lxml")
    anchors = [dt["id"] for dt in soup.select('dt[id^="GUC-"]')]
    assert [c.meta["section_id"] for c in settings] == anchors
    assert all(c.meta["part"] == 1 and c.meta["parts"] == 1 for c in settings)


def test_setting_chunk_id_url_title_and_metadata():
    chunk = by_id(parse("runtime-config-resource"))["docs:runtime-config-resource#GUC-WORK-MEM"]
    assert chunk.source == "docs"
    assert chunk.url == BASE + "runtime-config-resource.html#GUC-WORK-MEM"
    assert chunk.title == "20.4. Resource Consumption — 20.4.1. Memory — work_mem"
    assert chunk.settings == ["work_mem"]  # before tag_settings: only its own name
    assert chunk.meta == {
        "chunk_type": "setting",
        "setting": "work_mem",
        "setting_type": "integer",
        "pg_version": "16",
        "page": "runtime-config-resource",
        "page_title": "20.4. Resource Consumption",
        "parent_title": "Chapter 20. Server Configuration",
        "section_path": ["20.4. Resource Consumption", "20.4.1. Memory"],
        "section_id": "GUC-WORK-MEM",
        "part": 1,
        "parts": 1,
    }


def test_setting_chunk_text_is_the_type_line_plus_its_own_description_only():
    chunks = by_id(parse("runtime-config-resource"))
    work_mem = chunks["docs:runtime-config-resource#GUC-WORK-MEM"].text
    maintenance = chunks["docs:runtime-config-resource#GUC-MAINTENANCE-WORK-MEM"].text

    paragraphs = work_mem.split("\n\n")
    assert paragraphs[0] == "work_mem (integer)"
    assert paragraphs[1].startswith(
        "Sets the base maximum amount of memory to be used by a query operation (such as a sort "
        "or hash table) before writing to temporary disk files."
    )
    assert paragraphs[-1].endswith("that exceeds the usual work_mem base amount.")
    assert len(paragraphs) == 3

    assert maintenance.startswith(
        "maintenance_work_mem (integer)\n\nSpecifies the maximum amount of memory to be used by "
        "maintenance operations, such as VACUUM, CREATE INDEX, and ALTER TABLE ADD FOREIGN KEY."
    )
    # Neighbouring entries do not leak into each other.
    assert "maintenance operations" not in work_mem
    assert "Sets the base maximum amount of memory" not in maintenance
    assert "hash_mem_multiplier (floating point)" not in work_mem
    assert "autovacuum_work_mem (integer)" not in maintenance


def test_setting_chunks_are_never_split_however_small_the_limit():
    normal = settings_of(parse("runtime-config-resource"))
    tiny = settings_of(parse("runtime-config-resource", max_chars=200))
    assert tiny == normal
    assert max(len(c.text) for c in tiny) > 1000
    # ... while section chunks on the same page do get split.
    assert len(sections_of(parse("runtime-config-resource", max_chars=200))) > len(
        sections_of(parse("runtime-config-resource"))
    )


def test_long_setting_with_table_code_and_tips_stays_one_chunk():
    chunk = by_id(parse("runtime-config-logging"))[
        "docs:runtime-config-logging#GUC-LOG-LINE-PREFIX"
    ]
    text = chunk.text
    assert len(text) > DEFAULT_MAX_CHARS and chunk.meta["parts"] == 1
    assert text.startswith("log_line_prefix (string)\n\nThis is a printf-style string")
    # The table: one line per row, cells separated by " | ".
    assert "Escape | Effect | Session only\n%a | Application name | yes\n" in text
    assert "\n%m | Time stamp with milliseconds | no\n" in text
    # Code keeps its line breaks and indentation.
    assert (
        "SELECT to_hex(trunc(EXTRACT(EPOCH FROM backend_start))::integer) || '.' ||\n"
        "       to_hex(pid)\nFROM pg_stat_activity;"
    ) in text
    assert "\n\nTip: If you set a nonempty value for log_line_prefix, you should" in text
    assert "\n\nlog_line_prefix = '%m [%p] %q%u@%d/%a '" in text


def test_prose_around_parameter_lists_becomes_a_section_chunk():
    chunks = by_id(parse("runtime-config-resource"))
    intro = chunks["docs:runtime-config-resource#RUNTIME-CONFIG-RESOURCE-VACUUM-COST"]
    assert intro.meta["chunk_type"] == "section" and "setting" not in intro.meta
    assert intro.title == "20.4. Resource Consumption — 20.4.4. Cost-based Vacuum Delay"
    assert intro.url == BASE + "runtime-config-resource.html#RUNTIME-CONFIG-RESOURCE-VACUUM-COST"
    assert intro.text.startswith("During the execution of VACUUM and ANALYZE commands, the system")
    assert "\n\nNote: There are certain operations that hold critical locks" in intro.text
    # The parameters of that section are not repeated in the section chunk.
    assert "vacuum_cost_delay (floating point)" not in intro.text
    assert "The amount of time that the process will sleep" not in intro.text
    # A section holding nothing but parameters has no section chunk at all.
    assert "docs:runtime-config-resource#RUNTIME-CONFIG-RESOURCE-MEMORY" not in chunks
    assert len(sections_of(list(chunks.values()))) == 1


def test_parameters_documented_together_each_get_a_dedicated_chunk():
    chunks = by_id(parse("runtime-config-logging"))
    names = ["debug_print_parse", "debug_print_rewritten", "debug_print_plan"]
    group = [
        chunks[f"docs:runtime-config-logging#GUC-{n.upper().replace('_', '-')}"] for n in names
    ]
    assert [c.meta["setting"] for c in group] == names
    for chunk, name in zip(group, names, strict=True):
        others = [n for n in names if n != name]
        first, second, description = chunk.text.split("\n", 2)
        assert first == f"{name} (boolean)"
        assert second == "Documented together with: " + ", ".join(f"{n} (boolean)" for n in others)
        assert description.startswith("\nThese parameters enable various debugging output")
        assert chunk.meta["documented_with"] == others
        assert chunk.title.endswith(f"What to Log — {name}")
        # Only the first term has an anchor in the page; all three link to it.
        assert chunk.url == BASE + "runtime-config-logging.html#GUC-DEBUG-PRINT-PARSE"
    assert len({c.text.split("\n", 2)[2] for c in group}) == 1
    assert [c.meta["setting"] for c in settings_of(list(chunks.values()))] == [
        *names,
        "log_autovacuum_min_duration",
        "log_line_prefix",
        "log_temp_files",
    ]


def test_guc_anchors_nested_in_a_description_are_not_parameters():
    chunks = parse("runtime-config-connection")
    # The page has six GUC- anchors: ssl, ssl_ciphers and four cipher keywords under ssl_ciphers.
    soup = BeautifulSoup(html("runtime-config-connection"), "lxml")
    assert len(soup.select('dt[id^="GUC-"]')) == 6
    assert [c.meta["setting"] for c in settings_of(chunks)] == ["ssl", "ssl_ciphers"]
    ciphers = by_id(chunks)["docs:runtime-config-connection#GUC-SSL-CIPHERS"].text
    assert (
        "\n\nHIGH\nCipher suites that use ciphers from HIGH group (e.g., AES, Camellia, 3DES)"
        in ciphers
    )
    assert "\n\n!aNULL\nDisables anonymous cipher suites that do no authentication." in ciphers
    assert ciphers.rstrip().endswith("based on the server key type.")


def test_parameter_markup_outside_the_configuration_chapter_is_plain_text():
    # The same HTML under a non-config page name: no setting chunks, nothing lost.
    as_other = parse_page(html("runtime-config-resource"), BASE + "some-other-page.html")
    assert settings_of(as_other) == []
    text = "\n".join(c.text for c in as_other)
    assert "work_mem (integer)\nSets the base maximum amount of memory" in text


def test_setting_names_lists_every_dedicated_parameter():
    chunks = [c for page in CONFIG_PAGES for c in parse(page)]
    names = setting_names(chunks)
    assert names == sorted(names) and len(names) == 16
    assert {"work_mem", "ssl", "debug_print_plan", "log_temp_files"} <= set(names)


# ------------------------------------------------------------ section chunks


def test_sections_are_split_along_the_heading_hierarchy():
    chunks = parse("planner-stats")
    assert all(c.meta["chunk_type"] == "section" for c in chunks)
    seen: dict[str, None] = {}
    for chunk in chunks:
        seen.setdefault(chunk.meta["section_id"])
    assert list(seen) == [
        "PLANNER-STATS-SINGLE-COLUMN",
        "PLANNER-STATS-EXTENDED",
        "PLANNER-STATS-EXTENDED-FUNCTIONAL-DEPS",
        "PLANNER-STATS-EXTENDED-FUNCTIONAL-DEPS-LIMITS",
    ]
    extended = by_id(chunks)["docs:planner-stats#PLANNER-STATS-EXTENDED"]
    assert extended.url == BASE + "planner-stats.html#PLANNER-STATS-EXTENDED"
    assert extended.title == "14.2. Statistics Used by the Planner — 14.2.2. Extended Statistics"
    assert extended.meta["section_path"] == [
        "14.2. Statistics Used by the Planner",
        "14.2.2. Extended Statistics",
    ]
    assert extended.meta["parent_title"] == "Chapter 14. Performance Tips"
    assert extended.text.startswith("It is common to see slow queries running bad execution plans")
    # A section's chunk holds its own text only; subsections are separate chunks.
    assert (
        "The simplest kind of extended statistics tracks functional dependencies"
        not in extended.text
    )
    deepest = by_id(chunks)["docs:planner-stats#PLANNER-STATS-EXTENDED-FUNCTIONAL-DEPS-LIMITS"]
    assert deepest.meta["section_path"] == [
        "14.2. Statistics Used by the Planner",
        "14.2.2. Extended Statistics",
        "14.2.2.1. Functional Dependencies",
        "14.2.2.1.1. Limitations of Functional Dependencies",
    ]
    assert deepest.text.startswith("Functional dependencies are currently only applied when")


def test_chapter_page_with_only_an_introduction():
    (chunk,) = parse("runtime-config")
    assert chunk.chunk_id == "docs:runtime-config#RUNTIME-CONFIG"
    assert chunk.url == BASE + "runtime-config.html"  # the page's top section: no fragment
    assert chunk.title == "Chapter 20. Server Configuration"
    assert chunk.meta["parent_title"] == "Part III. Server Administration"
    assert chunk.text == (
        "There are many configuration parameters that affect the behavior of the database system. "
        "In the first section of this chapter we describe how to interact with configuration "
        "parameters. The subsequent sections discuss each parameter in detail."
    )


def test_reference_page_sections():
    chunks = by_id(parse("sql-vacuum"))
    top = chunks["docs:sql-vacuum#SQL-VACUUM"]
    assert top.title == "VACUUM" and top.meta["parent_title"] == "SQL Commands"
    assert top.text.startswith("VACUUM\nVACUUM — garbage-collect and optionally analyze a database")
    assert "Synopsis\nVACUUM [ ( option [, ...] ) ] [ table_and_columns [, ...] ]\n" in top.text
    assert top.url == BASE + "sql-vacuum.html"
    # Reference sections have bare <h2> headings and build-generated ids: the heading goes
    # into the title and the chunk id, the generated id only into the URL.
    notes = chunks["docs:sql-vacuum#notes:p1"]
    assert notes.title == "VACUUM — Notes (part 1/2)"
    assert notes.meta["section_path"] == ["VACUUM", "Notes"]
    assert notes.url == BASE + "sql-vacuum.html#id-1.9.3.184.8"
    assert notes.text.startswith("To vacuum a table, one must ordinarily be the table's owner")
    examples = chunks["docs:sql-vacuum#examples"]
    assert examples.title == "VACUUM — Examples"
    assert examples.text == (
        "To clean a single table onek, analyze it for the optimizer and print a detailed "
        "vacuum activity report:\n\nVACUUM (VERBOSE, ANALYZE) onek;"
    )
    assert chunks["docs:sql-vacuum#see-also"].text.startswith("vacuumdb, Section 20.4.4")
    assert not any("Parameters" in c.title for c in chunks.values())  # removed from the fixture


def test_long_sections_are_split_into_numbered_parts():
    chunks = parse("planner-stats")
    parts = [c for c in chunks if c.meta["section_id"] == "PLANNER-STATS-SINGLE-COLUMN"]
    assert len(parts) == 4
    for number, chunk in enumerate(parts, start=1):
        assert chunk.chunk_id == f"docs:planner-stats#PLANNER-STATS-SINGLE-COLUMN:p{number}"
        assert chunk.meta["part"] == number and chunk.meta["parts"] == 4
        assert chunk.title == (
            "14.2. Statistics Used by the Planner — 14.2.1. Single-Column Statistics "
            f"(part {number}/4)"
        )
        assert chunk.meta["section_path"][-1] == "14.2.1. Single-Column Statistics"
        assert chunk.url == BASE + "planner-stats.html#PLANNER-STATS-SINGLE-COLUMN"
        assert 0 < len(chunk.text) <= DEFAULT_MAX_CHARS
    # Nothing is lost or reordered by splitting.
    (whole,) = [
        c
        for c in parse("planner-stats", max_chars=10**6)
        if c.meta["section_id"] == "PLANNER-STATS-SINGLE-COLUMN"
    ]
    assert whole.chunk_id == "docs:planner-stats#PLANNER-STATS-SINGLE-COLUMN"
    assert whole.title.endswith("14.2.1. Single-Column Statistics") and whole.meta["parts"] == 1
    assert squash("".join(c.text for c in parts)) == squash(whole.text)
    # Code listings keep their line structure.
    assert (
        "SELECT relname, relkind, reltuples, relpages\nFROM pg_class\nWHERE relname LIKE 'tenk1%';"
        in parts[0].text
    )


@pytest.mark.parametrize("max_chars", [300, 700, DEFAULT_MAX_CHARS])
def test_section_parts_respect_the_size_limit(max_chars):
    for page in ALL_PAGES:
        for chunk in sections_of(parse(page, max_chars=max_chars)):
            assert len(chunk.text) <= max_chars * 1.1, chunk.chunk_id


def test_split_into_parts_packs_whole_blocks():
    blocks = ["a" * 400, "b" * 400, "c" * 400, "d" * 400]
    assert split_into_parts(blocks, 1000) == [
        "a" * 400 + "\n\n" + "b" * 400,
        "c" * 400 + "\n\n" + "d" * 400,
    ]
    assert split_into_parts(blocks, 5000) == ["\n\n".join(blocks)]
    assert split_into_parts([], 100) == [] and split_into_parts(["", "  \n"], 100) == []


def test_split_into_parts_cuts_oversized_blocks_at_natural_boundaries():
    sentences = [f"Sentence number {i} talks about the planner." for i in range(30)]
    parts = split_into_parts([" ".join(sentences)], 300)
    assert len(parts) > 3 and all(len(p) <= 300 for p in parts)
    assert all(p.endswith(".") and p.startswith("Sentence number") for p in parts)
    assert " ".join(parts) == " ".join(sentences)

    listing = "\n".join(f"  row {i:03d} | value {i * 7:05d}" for i in range(60))
    parts = split_into_parts(["Intro paragraph.", listing], 250)
    assert all(len(p) <= 250 * 1.1 for p in parts)
    assert parts[0].startswith("Intro paragraph.\n\n  row 000 | value 00000\n")
    assert all(line.startswith("  row ") for p in parts[1:] for line in p.split("\n"))
    assert "\n".join(parts).replace("Intro paragraph.\n\n", "") == listing

    unbroken = "x" * 950
    assert split_into_parts([unbroken], 400) == ["x" * 400, "x" * 400, "x" * 150]
    # A tail shorter than a tenth of the limit joins the previous part.
    assert split_into_parts(["x" * 920], 300) == ["x" * 300, "x" * 300, "x" * 320]


def test_split_into_parts_attaches_a_short_tail_to_the_previous_part():
    parts = split_into_parts(["a" * 990, "tail"], 1000)
    assert parts == ["a" * 990 + "\n\n" + "tail"]
    parts = split_into_parts(["a" * 990, "b" * 200], 1000)
    assert parts == ["a" * 990, "b" * 200]


# ---------------------------------------------------------------- boilerplate


@pytest.mark.parametrize("page", ALL_PAGES)
def test_navigation_contents_and_feedback_boilerplate_is_dropped(page):
    chunks = parse(page)
    assert chunks
    for chunk in chunks:
        for junk in (
            "Submit correction",
            "report a documentation issue",
            "Supported Versions",
            "Search the documentation",
            "Table of Contents",
            "Prev",
            "Home",
            "Copyright",
            "\xa0",
        ):
            assert junk not in chunk.text and junk not in chunk.title, (chunk.chunk_id, junk)
        assert "#" not in chunk.title  # the heading's permalink marker
        assert not any(line.rstrip().endswith(" #") for line in chunk.text.split("\n"))
        assert chunk.text == chunk.text.strip() and "\n\n\n" not in chunk.text


def test_table_of_contents_entries_do_not_become_text():
    text = "\n".join(c.text for c in parse("runtime-config-resource"))
    # "20.4.5. Background Writer" only occurs in the page's table of contents.
    assert "Background Writer" not in text
    assert "20.4.1. Memory" not in text  # headings live in the title, not in the text


@pytest.mark.parametrize(
    "page",
    ["index", "bookindex", "legalnotice", "release", "release-16", "release-16-4", "release-prior"],
)
def test_index_legal_and_release_note_pages_yield_no_chunks(page):
    assert is_skipped_page(page)
    assert parse_page(html("planner-stats"), f"{BASE}{page}.html") == []
    assert parse_page(html("index"), f"{BASE}{page}.html") == []


def test_page_classification():
    assert is_release_page("release-16-11") and not is_release_page("sql-release-savepoint")
    assert is_config_page("runtime-config-wal") and is_config_page("runtime-config")
    assert not is_config_page("config-setting") and not is_config_page("runtime")
    assert not is_skipped_page("runtime-config-resource")
    with pytest.raises(ValueError):
        parse_page(b"<html></html>", "https://www.postgresql.org/docs/15/planner-stats.html")


# ------------------------------------------------- ids, determinism, settings


def test_chunk_ids_are_unique_readable_and_deterministic():
    first = [c for page in ALL_PAGES for c in parse(page)]
    second = [c for page in ALL_PAGES for c in parse(page)]
    assert first == second
    ids = [c.chunk_id for c in first]
    assert len(set(ids)) == len(ids)
    for chunk in first:
        page, _, rest = chunk.chunk_id.removeprefix("docs:").partition("#")
        assert page == chunk.meta["page"] and rest.split(":p")[0] == chunk.meta["section_id"]
        assert chunk.url.startswith(f"{BASE}{page}.html")


def test_tag_settings_and_schema_validation():
    chunks = [c for page in ALL_PAGES for c in parse(page)]
    names = sorted({*setting_names(chunks), *TARGET_SETTINGS})
    tag_settings(chunks, names)
    validate_chunks(chunks)
    found = by_id(chunks)

    assert found["docs:runtime-config-resource#GUC-WORK-MEM"].settings == [
        "hash_mem_multiplier",
        "work_mem",
    ]
    # autovacuum_work_mem's text has the words "autovacuum" and maintenance_work_mem, but
    # work_mem only as part of longer names.
    assert found["docs:runtime-config-resource#GUC-AUTOVACUUM-WORK-MEM"].settings == [
        "autovacuum",
        "autovacuum_work_mem",
        "maintenance_work_mem",
    ]
    assert found["docs:runtime-config-resource#GUC-SHARED-BUFFERS"].settings == [
        "max_wal_size",
        "shared_buffers",
    ]
    # "autovacuum" is tagged here because the description uses the word on its own
    # ("each action executed by autovacuum"), not because of the parameter's name.
    log_av = found["docs:runtime-config-logging#GUC-LOG-AUTOVACUUM-MIN-DURATION"]
    assert log_av.settings == ["autovacuum", "log_autovacuum_min_duration"]
    assert " autovacuum " in log_av.text
    assert found["docs:runtime-config-logging#GUC-LOG-TEMP-FILES"].settings == ["log_temp_files"]
    assert found["docs:runtime-config#RUNTIME-CONFIG"].settings == []
    for chunk in settings_of(chunks):
        assert chunk.meta["setting"] in chunk.settings
    stats_parts = [c for c in chunks if c.meta["section_id"] == "PLANNER-STATS-SINGLE-COLUMN"]
    assert [c.settings for c in stats_parts][-1] == ["default_statistics_target"]
    assert all(c.settings == sorted(set(c.settings)) for c in chunks)
