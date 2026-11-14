"""Chunking of release-note pages, offline.

The pages are trimmed real ones in ``tests/fixtures/release_notes/``.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

import pytest

from pgdiag.chunks import Chunk, validate_chunks
from pgdiag.ingest.release_notes.parse import (
    DEFAULT_MAX_CHARS,
    DEFAULT_MIN_ITEM_CHARS,
    INDEX_URL,
    ITEM_SPLIT_CHARS,
    MAX_GROUP_ITEMS,
    SPLIT_PART_CHARS,
    Item,
    Release,
    ReleaseNotesError,
    group_items,
    is_boilerplate,
    parse_release_index,
    parse_release_page,
    release_version,
    split_credit,
    split_heading,
)
from pgdiag.settings import TARGET_SETTINGS

FIXTURES = Path(__file__).parent / "fixtures" / "release_notes"
BASE = "https://www.postgresql.org/docs/16/"
PAGES = ("release-16", "release-16-1", "release-16-2", "release-16-3")
#: Parameter names for ``settings``; passed explicitly so the tests do not depend on data/.
NAMES = (
    *TARGET_SETTINGS,
    "archive_command",
    "archive_library",
    "debug_parallel_query",
    "enable_presorted_aggregate",
    "hot_standby_feedback",
    "jit",
    "send_abort_for_crash",
    "send_abort_for_kill",
    "vacuum_buffer_usage_limit",
    "wal_sync_method",
)
CHUNK_ID_RE = re.compile(r"^relnotes:16\.\d+#[a-z0-9~-]+(:i\d+(-\d+)?(:p\d+)?|:p\d+)?$")


def html(page: str) -> bytes:
    return (FIXTURES / f"{page}.html").read_bytes()


def parse(page: str, **kwargs) -> Release:
    kwargs.setdefault("names", NAMES)
    return parse_release_page(html(page), f"{BASE}{page}.html", **kwargs)


def by_id(release: Release) -> dict[str, Chunk]:
    assert len({c.chunk_id for c in release.chunks}) == len(release.chunks)
    return {c.chunk_id: c for c in release.chunks}


def ids(release: Release) -> list[str]:
    return [c.chunk_id.split("#", 1)[1] for c in release.chunks]


def synthetic_page(
    items: list[str],
    *,
    heading: str = "E.8.&nbsp;Release 16.9",
    date: str | None = "2025-05-08",
    intro: str = "",
) -> str:
    """A minimal page with the markup of a minor release: one Changes list."""
    date_line = f"<p><strong>Release date:&nbsp;</strong>{date}</p>" if date is not None else ""
    listing = (
        '<div class="itemizedlist"><ul class="itemizedlist">'
        + "".join(f'<li class="listitem">{item}</li>' for item in items)
        + "</ul></div>"
        if items
        else ""
    )
    return f"""<html><body><div id="docContent">
<div class="sect1" id="RELEASE-16-9">
 <div class="titlepage"><h2 class="title">{heading} <a class="id_link" href="#x">#</a></h2></div>
 {date_line}{intro}
 <div class="sect2" id="RELEASE-16-9-CHANGES">
  <div class="titlepage"><h3 class="title">E.8.2.&nbsp;Changes</h3></div>
  {listing}
 </div>
</div></div></body></html>"""


def synthetic(items: list[str], **kwargs) -> Release:
    return parse_release_page(
        synthetic_page(items, **kwargs), BASE + "release-16-9.html", names=NAMES
    )


# ------------------------------------------------------------------ the index


def test_index_lists_the_release_pages_oldest_first_and_ignores_prior_releases():
    index = parse_release_index(html("release"), INDEX_URL)
    assert [(ref.version, ref.page, ref.heading) for ref in index.releases] == [
        ("16.0", "release-16", "Release 16"),
        ("16.1", "release-16-1", "Release 16.1"),
        ("16.2", "release-16-2", "Release 16.2"),
    ]
    assert [ref.url for ref in index.releases] == [
        BASE + "release-16.html",
        BASE + "release-16-1.html",
        BASE + "release-16-2.html",
    ]
    # A pointer to the notes of older branches: listed in the appendix, carries no notes.
    assert index.ignored == [(BASE + "release-prior.html", "Prior Releases")]


def toc(*entries: tuple[str, str]) -> str:
    rows = "".join(
        f'<dt><span class="sect1"><a href="{href}">{text}</a></span></dt>'
        f'<dd><dl><dt><span class="sect2"><a href="{href}#X">E.1.1. Changes</a></span></dt>'
        "</dl></dd>"
        for href, text in entries
    )
    return f'<div id="docContent"><div class="toc"><dl class="toc">{rows}</dl></div></div>'


def test_index_keeps_only_postgresql_16_releases():
    index = parse_release_index(
        toc(
            ("release-16-1.html", "E.1.\xa0Release 16.1"),
            ("release-16.html", "E.2. Release 16"),
            ("release-15-4.html", "E.3. Release 15.4"),
            ("https://example.com/docs/16/release-16-2.html", "E.4. Release 16.2"),
        )
    )
    assert [ref.version for ref in index.releases] == ["16.0", "16.1"]
    assert [heading for _, heading in index.ignored] == ["Release 15.4", "Release 16.2"]


@pytest.mark.parametrize(
    "page, problem",
    [
        ("<div id='docContent'><p>no contents</p></div>", "no table of contents"),
        (toc(("release-prior.html", "E.1. Prior Releases")), "found none"),
        (toc(("release-16-1.html", "E.1. Release 16.1")), r"found \['16.1'\]"),  # no 16.0
        (
            toc(("release-16-3.html", "E.1. Release 16.3"), ("release-16.html", "E.2. Release 16")),
            r"found \['16.0', '16.3'\]",  # a gap
        ),
        (
            toc(("release-16.html", "E.1. Release 16"), ("release-16-0.html", "E.2. Release 16.0")),
            "listed twice",
        ),
    ],
)
def test_incomplete_or_unreadable_index_is_rejected(page, problem):
    with pytest.raises(ReleaseNotesError, match=problem):
        parse_release_index(page)


# ---------------------------------------------------------- headings, versions


def test_headings_and_versions():
    assert split_heading("E.14.2.\xa0Changes") == ("E.14.2", "Changes")
    assert split_heading("E.17.3.1.1. Optimizer") == ("E.17.3.1.1", "Optimizer")
    assert split_heading("19.6. Upgrading a PostgreSQL Cluster") == (
        "19.6",
        "Upgrading a PostgreSQL Cluster",
    )
    assert split_heading("pg_dumpall") == (None, "pg_dumpall")
    assert release_version("Release 16") == "16.0"
    assert release_version("Release 16.3") == "16.3"
    assert release_version("Release 16.16") == "16.16"
    assert release_version("Prior Releases") is None
    assert release_version("Release Notes") is None


@pytest.mark.parametrize(
    "page, version, date",
    [
        ("release-16", "16.0", "2023-09-14"),
        ("release-16-1", "16.1", "2023-11-09"),
        ("release-16-2", "16.2", "2024-02-08"),
        ("release-16-3", "16.3", "2024-05-09"),
    ],
)
def test_version_and_release_date_come_from_the_page(page, version, date):
    release = parse(page)
    assert (release.version, release.release_date, release.page) == (version, date, page)
    for chunk in release.chunks:
        assert chunk.source == "release_notes"
        assert chunk.meta["version"] == version and chunk.meta["release_date"] == date
        assert chunk.meta["pg_major"] == "16" and chunk.meta["pg_version"] == "16"
        assert chunk.meta["release_kind"] == ("major" if version == "16.0" else "minor")
        assert chunk.meta["page"] == page
        assert chunk.chunk_id.startswith(f"relnotes:{version}#")
        assert chunk.url.startswith(f"{BASE}{page}.html#RELEASE-16")


def test_release_date_may_be_absent_but_not_malformed():
    release = synthetic(["<p>Fix something (Tom Lane)</p>"], date=None)
    assert release.release_date is None
    assert all(c.meta["release_date"] is None for c in release.chunks)
    with pytest.raises(ReleaseNotesError, match="not an ISO date"):
        synthetic(["<p>Fix something (Tom Lane)</p>"], date="May 8, 2025")


# ------------------------------------------------------------- minor releases


def test_minor_release_long_items_stand_alone_and_short_neighbours_are_grouped():
    release = parse("release-16-1")
    assert release.items == 14
    assert ids(release) == [
        "migration",
        "changes:i1",
        "changes:i2",
        "changes:i3",
        "changes:i4",
        "changes:i5",
        "changes:i6",
        "changes:i7",
        "changes:i8-11",
        "changes:i12",
        "changes:i13-14",
    ]
    chunks = by_id(release)
    group = chunks["relnotes:16.1#changes:i8-11"]
    assert (group.meta["item_index"], group.meta["item_last"], group.meta["item_count"]) == (
        8,
        11,
        4,
    )
    # Items are separated by a blank line; each of these four is shorter than the threshold.
    members = group.text.split("\n\n")
    assert len(members) == 4 and all(len(m) < DEFAULT_MIN_ITEM_CHARS for m in members)
    assert members[1] == "Fix intra-query memory leak in Memoize execution"
    assert len(group.text) <= DEFAULT_MAX_CHARS
    single = chunks["relnotes:16.1#changes:i12"]
    assert single.meta["item_count"] == 1 and len(single.text) >= DEFAULT_MIN_ITEM_CHARS


def test_item_chunk_fields():
    chunk = by_id(parse("release-16-1"))["relnotes:16.1#changes:i3"]
    assert chunk.url == BASE + "release-16-1.html#RELEASE-16-1-CHANGES"
    assert chunk.title == "PostgreSQL 16.1 release notes — Changes"
    assert chunk.settings == ["autovacuum"]
    assert chunk.meta == {
        "chunk_type": "items",
        "version": "16.1",
        "pg_major": "16",
        "pg_version": "16",
        "release_kind": "minor",
        "release_date": "2023-11-09",
        "page": "release-16-1",
        "page_title": "Release 16.1",
        "section": "Changes",
        "section_path": ["Changes"],
        "section_id": "RELEASE-16-1-CHANGES",
        "section_number": "E.16.2",
        "item_index": 3,
        "item_last": 3,
        "item_count": 1,
        "authors": ["Noah Misch", "Jelte Fennema-Nio"],
        "commits": chunk.meta["commits"],
        "part": 1,
        "parts": 1,
    }
    assert chunk.meta["commits"] and all(
        re.fullmatch(r"https://postgr\.es/c/[0-9a-f]+", link) for link in chunk.meta["commits"]
    )


def test_item_text_is_the_summary_and_its_explanation_without_credit_or_commit_links():
    chunk = by_id(parse("release-16-2"))["relnotes:16.2#changes:i2"]
    summary, *rest = chunk.text.split("\n")
    # In the page: "Fix memory leak when performing JIT inlining (Andres Freund, Daniel
    # Gustafsson) §".
    assert summary == "Fix memory leak when performing JIT inlining"
    assert len(rest) == 1 and rest[0].startswith("There have been multiple reports of backend")
    assert chunk.meta["authors"] == ["Andres Freund", "Daniel Gustafsson"]
    assert chunk.meta["commits"] == ["https://postgr.es/c/2cf50585e"]
    assert chunk.settings == []  # "JIT" is the feature, not the parameter jit
    # An item with two commit links ("... (Heikki Linnakangas) § §") keeps both, in order.
    first = by_id(parse("release-16-2"))["relnotes:16.2#changes:i1"]
    assert first.text.startswith(
        "Tighten security restrictions within REFRESH MATERIALIZED VIEW CONCURRENTLY\n"
    )
    assert first.meta["authors"] == ["Heikki Linnakangas"]
    assert first.meta["commits"] == [
        "https://postgr.es/c/d6a61cb3b",
        "https://postgr.es/c/fb3836855",
    ]

    for page in PAGES:
        raw = html(page).decode("utf-8")
        assert "§" in raw and "(Tom Lane)" in raw  # the fixtures do contain them
        for chunk in parse(page).chunks:
            assert "§" not in chunk.text and "postgr.es" not in chunk.text, chunk.chunk_id
            assert "\xa0" not in chunk.text and "\xa0" not in chunk.title, chunk.chunk_id
            assert "(Tom Lane)" not in chunk.text, chunk.chunk_id
            if chunk.meta["chunk_type"] == "items":
                assert chunk.meta["authors"] and chunk.meta["commits"], chunk.chunk_id
            else:
                assert "authors" not in chunk.meta and "item_index" not in chunk.meta


def test_item_with_procedure_steps_and_code_listings_stays_one_chunk():
    release = parse("release-16-3")
    chunk = by_id(release)["relnotes:16.3#changes:i1"]
    assert chunk.meta["item_count"] == 1 and chunk.meta["parts"] == 1
    assert DEFAULT_MAX_CHARS < len(chunk.text) < ITEM_SPLIT_CHARS  # long, but not split
    lines = chunk.text.split("\n")
    assert lines[0] == (
        "Restrict visibility of pg_stats_ext and pg_stats_ext_exprs entries to the table owner"
    )
    # A parenthesis that is not the credit at the end of the summary stays.
    assert (
        "The PostgreSQL Project thanks Lukas Fittl for reporting this problem. (CVE-2024-4317)"
        in (lines)
    )
    assert [line[:2] for line in lines if re.match(r"\d\. ", line)] == ["1.", "2.", "3."]
    assert "\\i /usr/share/postgresql/fix-CVE-2024-4317.sql" in lines
    assert "ALTER DATABASE template0 WITH ALLOW_CONNECTIONS true;" in lines
    assert chunk.meta["authors"] == ["Nathan Bossart"]
    # The nested steps are not release-note items of their own.
    assert release.items == 8
    assert ids(release) == [
        "migration",
        "changes:i1",
        "changes:i2",
        "changes:i3-6",
        "changes:i7",
        "changes:i8",
    ]


def test_migration_section_of_a_minor_release_is_one_prose_chunk():
    release = parse("release-16-2")
    chunk = by_id(release)["relnotes:16.2#migration"]
    assert chunk.title == "PostgreSQL 16.2 release notes — Migration to Version 16.2"
    assert chunk.url == BASE + "release-16-2.html#RELEASE-16-2-MIGRATION"
    assert chunk.meta["chunk_type"] == "prose"
    assert chunk.meta["section"] == "Migration to Version 16.2"
    assert chunk.meta["section_number"] == "E.15.1"
    assert chunk.text.split("\n\n") == [
        "A dump/restore is not required for those running 16.X.",
        "However, one bug was fixed that could have resulted in corruption of GIN indexes "
        "during concurrent updates. If you suspect such corruption, reindex affected indexes "
        "after installing this update.",
        # The bare section number is completed with the section's title.
        "Also, if you are upgrading from a version earlier than 16.1, see Section E.16 "
        "(Release 16.1).",
    ]
    # Exactly one prose chunk; the release date and the standard opening sentence are not text.
    assert [c.meta["chunk_type"] for c in release.chunks].count("prose") == 1
    assert "This release contains a variety of fixes from 16.1." in html("release-16-2").decode()
    for c in release.chunks:
        assert "Release date" not in c.text and "This release contains" not in c.text


def test_boilerplate_sentences():
    assert is_boilerplate("Release date: 2024-02-08")
    assert is_boilerplate(
        "This release contains a variety of fixes from 16.1. For information about new "
        "features in major release 16, see Section E.17 (Release 16)."
    )
    assert is_boilerplate(
        "This release contains a small number of fixes from 16.12. For information about new "
        "features in major release 16, see Section E.17."
    )
    assert is_boilerplate(
        "Below you will find a detailed account of the changes between PostgreSQL 16 and the "
        "previous major release."
    )
    assert not is_boilerplate("A dump/restore is not required for those running 16.X.")
    assert not is_boilerplate(
        "This release contains a variety of fixes from 16.1. It also changes the default of "
        "work_mem."
    )


def test_unexpected_prose_at_release_level_is_kept():
    release = synthetic(
        ["<p>Fix something (Tom Lane)</p>"],
        intro="<p>This release contains a few fixes from 16.8. For information about new "
        "features in major release 16, see Section E.17.</p>"
        "<p>This release also raises the default of <code>work_mem</code>.</p>",
    )
    assert ids(release) == ["intro", "changes:i1"]
    intro = release.chunks[0]
    assert intro.text == "This release also raises the default of work_mem."
    assert intro.title == "PostgreSQL 16.9 release notes" and intro.settings == ["work_mem"]
    assert intro.url == BASE + "release-16-9.html" and intro.meta["chunk_type"] == "prose"


# -------------------------------------------------------------- major release


def test_major_release_sections_ids_and_titles():
    release = parse("release-16")
    assert release.version == "16.0" and release.items == 23
    assert [(c.chunk_id, c.title) for c in release.chunks] == [
        ("relnotes:16.0#highlights", "PostgreSQL 16 release notes — Overview"),
        ("relnotes:16.0#migration", "PostgreSQL 16 release notes — Migration to Version 16"),
        ("relnotes:16.0#migration:i1", "PostgreSQL 16 release notes — Migration to Version 16"),
        ("relnotes:16.0#migration:i2-6", "PostgreSQL 16 release notes — Migration to Version 16"),
        (
            "relnotes:16.0#optimizer:i1-6",
            "PostgreSQL 16 release notes — Changes — Server — Optimizer",
        ),
        (
            "relnotes:16.0#performance:i1-2",
            "PostgreSQL 16 release notes — Changes — Server — General Performance",
        ),
        (
            "relnotes:16.0#performance:i3",
            "PostgreSQL 16 release notes — Changes — Server — General Performance",
        ),
        (
            "relnotes:16.0#performance:i4",
            "PostgreSQL 16 release notes — Changes — Server — General Performance",
        ),
        (
            "relnotes:16.0#server-config:i1-3",
            "PostgreSQL 16 release notes — Changes — Server — Server Configuration",
        ),
        (
            "relnotes:16.0#server-config:i4",
            "PostgreSQL 16 release notes — Changes — Server — Server Configuration",
        ),
        (
            "relnotes:16.0#client-apps:i1",
            "PostgreSQL 16 release notes — Changes — Client Applications",
        ),
        (
            "relnotes:16.0#psql:i1-2",
            "PostgreSQL 16 release notes — Changes — Client Applications — psql",
        ),
    ]
    optimizer = by_id(release)["relnotes:16.0#optimizer:i1-6"]
    assert optimizer.url == BASE + "release-16.html#RELEASE-16-OPTIMIZER"
    assert optimizer.meta["section"] == "Optimizer"
    assert optimizer.meta["section_path"] == ["Changes", "Server", "Optimizer"]
    assert optimizer.meta["section_number"] == "E.17.3.1.1"
    assert optimizer.meta["section_id"] == "RELEASE-16-OPTIMIZER"
    assert optimizer.meta["release_kind"] == "major"
    assert optimizer.text.split("\n\n")[:2] == [
        "Allow incremental sorts in more cases, including DISTINCT",
        "Add the ability for aggregates having ORDER BY or DISTINCT to use pre-sorted data\n"
        "The new server variable enable_presorted_aggregate can be used to disable this.",
    ]
    assert optimizer.settings == ["enable_presorted_aggregate"]
    # A section with items of its own and a subsection: each has its own item numbering.
    psql = by_id(release)["relnotes:16.0#psql:i1-2"]
    assert psql.meta["section_path"] == ["Changes", "Client Applications", "psql"]
    assert psql.meta["item_index"] == 1


def test_major_release_overview_is_one_chunk_with_the_highlights_as_bullets():
    chunk = by_id(parse("release-16"))["relnotes:16.0#highlights"]
    assert chunk.meta["chunk_type"] == "overview" and chunk.meta["section"] == "Overview"
    assert chunk.url == BASE + "release-16.html#RELEASE-16-HIGHLIGHTS"
    lead, bullets, closing = chunk.text.split("\n\n")
    assert lead == "PostgreSQL 16 contains many new features and enhancements, including:"
    lines = bullets.split("\n")
    assert len(lines) == 7 and all(line.startswith("- ") for line in lines)
    assert lines[0] == "- Allow parallelization of FULL and internal right OUTER hash joins"
    assert lines[5] == "- Improve performance of vacuum freezing"
    assert closing.startswith("The above items and other new features")
    # The highlights are not counted or chunked as release-note items.
    assert "item_index" not in chunk.meta


def test_major_release_migration_has_prose_and_its_incompatibility_items():
    chunks = by_id(parse("release-16"))
    prose = chunks["relnotes:16.0#migration"]
    assert prose.meta["chunk_type"] == "prose"
    assert prose.text.split("\n\n") == [
        "A dump/restore using pg_dumpall or use of pg_upgrade or logical replication is required "
        "for those wishing to migrate data from any previous release. See Section 19.6 "
        "(Upgrading a PostgreSQL Cluster) for general information on migrating to new major "
        "releases.",
        "Version 16 contains a number of changes that may affect compatibility with previous "
        "releases. Observe the following incompatibilities:",
    ]
    first = chunks["relnotes:16.0#migration:i1"]
    assert (
        first.meta["chunk_type"] == "items" and first.meta["section"] == "Migration to Version 16"
    )
    assert first.text.startswith("Change assignment rules for PL/pgSQL bound cursor variables\n")
    group = chunks["relnotes:16.0#migration:i2-6"]
    assert "Rename server variable force_parallel_mode to debug_parallel_query" in group.text
    assert "Remove the server variable vacuum_defer_cleanup_age" in group.text
    # Only names of PostgreSQL 16 parameters are tagged; the removed ones are not in the list.
    assert group.settings == ["debug_parallel_query", "hot_standby_feedback"]
    # The lead-in of "Changes" is boilerplate: that section has no prose chunk.
    assert "relnotes:16.0#changes" not in chunks
    assert "Below you will find a detailed account" in html("release-16").decode()


def test_acknowledgments_navigation_and_site_chrome_are_dropped():
    raw = html("release-16").decode()
    assert "Abhijit Menon-Sen" in raw and "Submit correction" in raw and "nav-item" in raw
    for page in PAGES:
        for chunk in parse(page).chunks:
            text = chunk.text
            assert "Abhijit Menon-Sen" not in text and "Acknowledg" not in chunk.title
            assert "Submit correction" not in text and "Supported Versions" not in text
            assert "Prev" not in text.split() and "Table of Contents" not in text
            assert not text.endswith("#") and " #\n" not in text
    assert not [c for c in parse("release-16").chunks if "acknowledg" in c.chunk_id.lower()]


def test_settings_use_the_given_parameter_names():
    chunk = by_id(parse("release-16"))["relnotes:16.0#performance:i3"]
    assert "server variable vacuum_buffer_usage_limit, which also controls autovacuum" in chunk.text
    assert chunk.settings == ["autovacuum", "vacuum_buffer_usage_limit"]
    only_targets = parse("release-16", names=TARGET_SETTINGS)
    assert by_id(only_targets)["relnotes:16.0#performance:i3"].settings == ["autovacuum"]
    for page in PAGES:
        for c in parse(page).chunks:
            assert c.settings == sorted(set(c.settings)) and set(c.settings) <= set(NAMES)


# ------------------------------------------------------------------- grouping


def items_of(*lengths: int) -> list[Item]:
    return [Item(index=i, blocks=["x" * n]) for i, n in enumerate(lengths, start=1)]


def indexes(groups: list[list[Item]]) -> list[list[int]]:
    return [[item.index for item in group] for group in groups]


def test_group_items_packs_short_neighbours_and_leaves_long_items_alone():
    groups = group_items(items_of(50, 60, 400, 70, 80, 90, 300), max_chars=1000, min_item_chars=300)
    assert indexes(groups) == [[1, 2], [3], [4, 5, 6], [7]]


def test_group_items_respects_the_size_and_count_limits():
    # 100 + 2 + 100 = 202 fits, a third item would make 304.
    groups = group_items(items_of(100, 100, 100, 100, 100), max_chars=250, min_item_chars=300)
    assert indexes(groups) == [[1, 2], [3, 4], [5]]
    groups = group_items(items_of(100, 98), max_chars=200, min_item_chars=300)
    assert indexes(groups) == [[1, 2]]  # exactly max_chars with the blank line between them
    groups = group_items(items_of(*[10] * 8), max_chars=1000, min_item_chars=300, max_items=3)
    assert indexes(groups) == [[1, 2, 3], [4, 5, 6], [7, 8]]
    assert indexes(group_items(items_of(*[10] * 8))) == [[1, 2, 3, 4, 5, 6], [7, 8]]
    assert MAX_GROUP_ITEMS == 6


@pytest.mark.parametrize(
    "lengths",
    [
        (),
        (10,),
        (5000,),
        (10, 5000, 10),
        (299, 300, 301, 299, 299, 299, 299, 2, 1000, 1, 1, 1),
        tuple(range(1, 400, 7)),
    ],
)
def test_group_items_keeps_every_item_once_and_in_order(lengths):
    items = items_of(*lengths)
    groups = group_items(items)
    assert [item for group in groups for item in group] == items
    for group in groups:
        text = "\n\n".join(item.text for item in group)
        if len(group) > 1:
            assert len(text) <= DEFAULT_MAX_CHARS and len(group) <= MAX_GROUP_ITEMS
            assert all(len(item.text) < DEFAULT_MIN_ITEM_CHARS for item in group)


@pytest.mark.parametrize("page", PAGES)
def test_groups_stay_inside_their_section_and_cover_every_item(page):
    release = parse(page)
    ranges: dict[str, list[int]] = defaultdict(list)
    for chunk in release.chunks:
        if chunk.meta["chunk_type"] != "items":
            continue
        first, last = chunk.meta["item_index"], chunk.meta["item_last"]
        assert chunk.meta["item_count"] == last - first + 1
        ranges[chunk.meta["section_id"]].extend(range(first, last + 1))
        suffix = f":i{first}" if first == last else f":i{first}-{last}"
        assert chunk.chunk_id.endswith(suffix)
    for section, numbers in ranges.items():
        assert numbers == list(range(1, len(numbers) + 1)), section
    assert sum(len(numbers) for numbers in ranges.values()) == release.items


def test_thresholds_can_be_changed():
    one_per_item = parse("release-16-1", min_item_chars=0)
    item_chunks = [c for c in one_per_item.chunks if c.meta["chunk_type"] == "items"]
    assert len(item_chunks) == one_per_item.items == 14
    assert all(c.meta["item_count"] == 1 for c in item_chunks)
    assert item_chunks[12].text == (
        "Throw the intended error if pgrowlocks() is applied to a partitioned table\n"
        "Previously, a not-on-point complaint “only heap AM is supported” would be raised."
    )
    tight = parse("release-16-1", max_chars=200)
    assert all(len(c.text) <= 200 for c in tight.chunks if c.meta.get("item_count", 1) > 1) and len(
        tight.chunks
    ) > len(parse("release-16-1").chunks)


# -------------------------------------------------------------------- credits


@pytest.mark.parametrize(
    "summary, expected",
    [
        ("Fix foo in bar (Tom Lane)", ("Fix foo in bar", ["Tom Lane"])),
        (
            "Allow parallelization (Melanie Plageman, Thomas Munro)",
            ("Allow parallelization", ["Melanie Plageman", "Thomas Munro"]),
        ),
        (
            "Guard against stack overflow (Tom Lane, with hat tip to Nick Wellnhofer)",
            ("Guard against stack overflow", ["Tom Lane", "Nick Wellnhofer"]),
        ),
        (
            "Fix it (Álvaro Herrera, Dagfinn Ilmari Mannsåker)",
            ("Fix it", ["Álvaro Herrera", "Dagfinn Ilmari Mannsåker"]),
        ),
        ("Fix it (jian he, Tom Lane, jian he)", ("Fix it", ["jian he", "Tom Lane"])),
        # Not credits: nothing in parentheses at the end, digits, nested parentheses.
        ("Improve performance of vacuum freezing", ("Improve performance of vacuum freezing", [])),
        ("Fix the bug (CVE-2024-4317)", ("Fix the bug (CVE-2024-4317)", [])),
        ("Fix crash in to_tsvector()", ("Fix crash in to_tsvector()", [])),
        ("Fix pg_size_pretty(numeric)", ("Fix pg_size_pretty(numeric)", [])),
        ("Change f(x (y))", ("Change f(x (y))", [])),
        ("(Tom Lane)", ("(Tom Lane)", [])),
        # Only the last parenthesis is the credit.
        ("Fix f() (again) (Tom Lane)", ("Fix f() (again)", ["Tom Lane"])),
    ],
)
def test_split_credit(summary, expected):
    assert split_credit(summary) == expected


def test_only_the_summary_paragraph_loses_its_credit():
    release = synthetic(
        [
            "<p>Fix <code>VACUUM</code> (Tom Lane) "
            '<a class="ulink" href="https://postgr.es/c/abc123">§</a> '
            '<a class="ulink" href="https://postgr.es/c/def456">§</a></p>'
            "<p>This was reported upstream (Jane Doe)</p>"
        ]
    )
    (chunk,) = release.chunks
    assert chunk.text == "Fix VACUUM\nThis was reported upstream (Jane Doe)"
    assert chunk.meta["authors"] == ["Tom Lane"]
    assert chunk.meta["commits"] == ["https://postgr.es/c/abc123", "https://postgr.es/c/def456"]


# ----------------------------------------------------------- very long items


def test_only_an_extremely_long_item_is_split_into_parts():
    sentence = "The checkpointer could fall behind when max_wal_size was small. "
    paragraphs = "".join(f"<p>{sentence * 12}</p>" for _ in range(5))  # 5 x 768 characters
    release = synthetic(
        [f"<p>Fix checkpoint scheduling (Tom Lane)</p>{paragraphs}", "<p>Short fix (Tom Lane)</p>"]
    )
    assert ids(release) == ["changes:i1:p1", "changes:i1:p2", "changes:i1:p3", "changes:i2"]
    parts = release.chunks[:3]
    assert sum(len(c.text) for c in parts) > ITEM_SPLIT_CHARS
    assert all(len(c.text) <= SPLIT_PART_CHARS * 1.1 for c in parts)
    assert [c.title for c in parts] == [
        f"PostgreSQL 16.9 release notes — Changes (part {n}/3)" for n in (1, 2, 3)
    ]
    assert [(c.meta["part"], c.meta["parts"]) for c in parts] == [(1, 3), (2, 3), (3, 3)]
    assert all(c.meta["item_index"] == c.meta["item_last"] == 1 for c in parts)
    assert parts[0].text.startswith("Fix checkpoint scheduling\n\n")
    assert all(c.settings == ["max_wal_size"] for c in parts)
    # Nothing is lost by splitting.
    assert sum(c.text.count("The checkpointer could fall behind") for c in parts) == 60
    assert release.items == 2


# ---------------------------------------------------- ids, schema, bad pages


def test_chunk_ids_are_deterministic_unique_and_readable():
    first = [chunk for page in PAGES for chunk in parse(page).chunks]
    second = [chunk for page in PAGES for chunk in parse(page).chunks]
    assert first == second
    assert [c.chunk_id for c in first] == [c.chunk_id for c in second]
    validate_chunks(first)  # schema, "relnotes:" prefix, no duplicate ids
    for chunk in first:
        assert CHUNK_ID_RE.match(chunk.chunk_id), chunk.chunk_id
        assert chunk.text.strip() and chunk.title.startswith("PostgreSQL 16")
        assert chunk.meta["chunk_type"] in ("items", "prose", "overview")
        assert (chunk.meta["part"], chunk.meta["parts"]) == (1, 1)
        assert Chunk.from_dict(chunk.to_dict()) == chunk


def test_sections_with_the_same_heading_get_distinct_ids():
    page = synthetic_page(["<p>Fix one thing (Tom Lane)</p>"]).replace(
        '<div class="sect2" id="RELEASE-16-9-CHANGES">',
        '<div class="sect2"><div class="titlepage"><h3 class="title">E.8.1. Changes</h3></div>'
        '<div class="itemizedlist"><ul><li><p>Fix another thing (Tom Lane)</p></li></ul></div>'
        '</div><div class="sect2">',
    )
    release = parse_release_page(page, BASE + "release-16-9.html", names=NAMES)
    assert ids(release) == ["changes:i1", "changes~2:i1"]
    assert all(c.url == BASE + "release-16-9.html" for c in release.chunks)  # no HTML ids


@pytest.mark.parametrize(
    "page, url, problem",
    [
        (html("release-prior"), BASE + "release-prior.html", "is not 'Release <version>'"),
        (html("release"), BASE + "release.html", "no release section"),
        (synthetic_page([]), BASE + "release-16-9.html", "no release-note items found"),
        (synthetic_page(["<p> </p>"]), BASE + "release-16-9.html", "no release-note items found"),
        (
            "<html><body><p>Service unavailable</p></body></html>",
            BASE + "release-16-9.html",
            "no documentation content",
        ),
        (
            synthetic_page(["<p>Fix (Tom Lane)</p>"]),
            "https://www.postgresql.org/about/",
            "not a PostgreSQL 16 documentation page",
        ),
    ],
)
def test_pages_that_are_not_release_notes_are_rejected(page, url, problem):
    with pytest.raises(ReleaseNotesError, match=problem):
        parse_release_page(page, url, names=NAMES)
