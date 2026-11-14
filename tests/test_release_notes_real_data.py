"""Acceptance and sanity checks on the real ``data/chunks/release_notes.jsonl`` (T2 output).

Skipped, with instructions, when the file has not been generated. Once it
exists every check here must pass: a wrong chunk file is a failure, not a skip.
``PGDIAG_REQUIRE_DATA=1`` (``make test-data``) turns the skip into a failure.

The chunks are compared with the pages they were made from, read from the
download cache (``data/raw/``) without any request.
"""

from __future__ import annotations

import datetime as dt
import os
import re
from collections import Counter, defaultdict

import pytest
from bs4 import BeautifulSoup

from pgdiag.chunks import (
    AMBIGUOUS_NAMES,
    DOCS_CHUNKS_FILE,
    RELEASE_NOTES_CHUNKS_FILE,
    Chunk,
    find_settings,
    iter_jsonl,
    load_guc_names,
    read_jsonl,
)
from pgdiag.config import get_config
from pgdiag.fetch import PoliteFetcher
from pgdiag.ingest.docs.crawl import DOCS_BASE_URL
from pgdiag.ingest.release_notes.parse import (
    INDEX_URL,
    ITEM_SPLIT_CHARS,
    SPLIT_PART_CHARS,
    ReleaseRef,
    parse_release_index,
)

pytestmark = pytest.mark.data

VERSION_RE = re.compile(r"^16\.\d+$")
CHUNK_ID_RE = re.compile(r"^relnotes:16\.\d+#[a-z0-9~-]+(:i\d+(-\d+)?(:p\d+)?|:p\d+)?$")
#: Release dates that will not change any more.
KNOWN_DATES = {"16.0": "2023-09-14", "16.1": "2023-11-09", "16.4": "2024-08-08"}
#: No release-note chunk comes close: the longest single item is about 2,300 characters.
ABSURD_LENGTH = 4000


def _skip_or_fail(reason: str) -> None:
    if os.environ.get("PGDIAG_REQUIRE_DATA") == "1":
        pytest.fail(reason)
    pytest.skip(reason)


@pytest.fixture(scope="module")
def chunks() -> list[Chunk]:
    path = get_config().chunks_dir / RELEASE_NOTES_CHUNKS_FILE
    if not path.is_file():
        _skip_or_fail(
            f"{path} does not exist. Generate it with `make ingest-release-notes` "
            "(= .venv/bin/pgdiag-ingest-release-notes; run `make ingest-docs` first: it "
            "downloads the pages and writes the list of parameter names)."
        )
    return read_jsonl(path)  # validates the schema and id uniqueness; raises if broken


@pytest.fixture(scope="module")
def fetcher() -> PoliteFetcher:
    return PoliteFetcher(get_config())  # only read_cache() is used: no request is ever made


@pytest.fixture(scope="module")
def releases(fetcher) -> list[ReleaseRef]:
    """The PostgreSQL 16 releases listed in the cached appendix index, oldest first."""
    page = fetcher.read_cache(INDEX_URL)
    if page is None or not page.ok:
        _skip_or_fail(
            f"{INDEX_URL} is not in the download cache (data/raw/); "
            "run `make ingest-release-notes` to fetch it."
        )
    return parse_release_index(page.content, INDEX_URL).releases


@pytest.fixture(scope="module")
def by_version(chunks) -> dict[str, list[Chunk]]:
    grouped: dict[str, list[Chunk]] = defaultdict(list)
    for chunk in chunks:
        grouped[chunk.meta["version"]].append(chunk)
    return grouped


# ----------------------------------------------------------------- acceptance


def test_chunks_exist_are_tagged_release_notes_and_carry_version_metadata(chunks):
    assert len(chunks) > 300
    for chunk in chunks:
        assert chunk.source == "release_notes", chunk.chunk_id
        assert isinstance(chunk.meta.get("version"), str), chunk.chunk_id
        assert VERSION_RE.match(chunk.meta["version"]), chunk.chunk_id
        assert chunk.meta["pg_major"] == "16" and chunk.meta["pg_version"] == "16"
        assert chunk.meta["release_kind"] == (
            "major" if chunk.meta["version"] == "16.0" else "minor"
        )


def test_every_release_in_the_cached_index_is_represented(by_version, releases):
    listed = [ref.version for ref in releases]
    assert listed[0] == "16.0" and len(listed) >= 17  # 16.0 to 16.16 on 2026-11-13
    assert listed == [f"16.{minor}" for minor in range(len(listed))]
    assert sorted(by_version, key=lambda v: int(v.split(".")[1])) == listed
    for ref in releases:
        group = by_version[ref.version]
        assert group, ref.version
        assert any(c.meta["chunk_type"] == "items" for c in group), ref.version
        for chunk in group:
            assert chunk.meta["page"] == ref.page, chunk.chunk_id
            assert chunk.url.split("#")[0] == ref.url, chunk.chunk_id
            assert chunk.meta["page_title"] == ref.heading, chunk.chunk_id


def test_ids_are_unique_readable_and_do_not_collide_with_the_docs_chunks(chunks):
    ids = [c.chunk_id for c in chunks]
    assert len(set(ids)) == len(ids)
    for chunk in chunks:
        assert CHUNK_ID_RE.match(chunk.chunk_id), chunk.chunk_id
        assert chunk.chunk_id.startswith(f"relnotes:{chunk.meta['version']}#"), chunk.chunk_id
    docs_path = get_config().chunks_dir / DOCS_CHUNKS_FILE
    assert docs_path.is_file(), f"{docs_path} is written by `make ingest-docs`"
    docs_ids = {c.chunk_id for c in iter_jsonl(docs_path)}
    assert len(docs_ids) > 5000 and not docs_ids & set(ids)


def test_text_title_and_url_are_set_and_no_chunk_is_absurdly_long(chunks):
    for chunk in chunks:
        assert chunk.text.strip() and chunk.text == chunk.text.strip(), chunk.chunk_id
        version = chunk.meta["version"]
        product = f"PostgreSQL {'16' if version == '16.0' else version} release notes"
        assert chunk.title == " — ".join([product, *chunk.meta["section_path"]]), chunk.chunk_id
        assert chunk.url.startswith(f"{DOCS_BASE_URL}release-16"), chunk.chunk_id
        assert chunk.url.endswith(f".html#{chunk.meta['section_id']}"), chunk.chunk_id
        assert chunk.meta["section_id"].startswith("RELEASE-16"), chunk.chunk_id
        assert len(chunk.text) < ABSURD_LENGTH, (chunk.chunk_id, len(chunk.text))
    # Nothing was long enough to be split, and only single items exceed the part size.
    assert all(c.meta["parts"] == 1 and c.meta["part"] == 1 for c in chunks)
    assert max(len(c.text) for c in chunks) < ITEM_SPLIT_CHARS
    for chunk in chunks:
        if len(chunk.text) > SPLIT_PART_CHARS:
            assert chunk.meta["chunk_type"] == "items" and chunk.meta["item_count"] == 1


# --------------------------------------------------------------------- sanity


def test_release_dates_are_iso_dates_in_release_order(by_version, releases):
    dates = []
    for ref in releases:
        found = {c.meta["release_date"] for c in by_version[ref.version]}
        assert len(found) == 1, (ref.version, found)
        dates.append(dt.date.fromisoformat(found.pop()))
    assert dates == sorted(set(dates))
    assert dt.date(2023, 9, 1) < dates[0] and dates[-1] <= dt.date.today()
    for version, date in KNOWN_DATES.items():
        assert by_version[version][0].meta["release_date"] == date


def _item_counts(html: bytes) -> dict[str, int]:
    """Release-note items per section id, counted straight from a page.

    An item is a top-level entry of an itemized list that has a commit link;
    that leaves out the Overview's highlights and steps nested inside an item.
    """
    soup = BeautifulSoup(html, "lxml")
    counts: Counter = Counter()
    for entry in soup.select("#docContent div.itemizedlist > ul > li"):
        if entry.find_parent("li") is not None:
            continue
        section = entry.find_parent("div", class_=re.compile(r"^sect\d$"))
        if "HIGHLIGHTS" in section["id"]:
            continue
        counts[section["id"]] += 1
    return dict(counts)


def test_every_item_of_every_page_is_in_exactly_one_chunk(by_version, releases, fetcher):
    total = 0
    for ref in releases:
        page = fetcher.read_cache(ref.url)
        assert page is not None and page.ok, f"{ref.url} is not in the download cache"
        expected = _item_counts(page.content)
        covered: dict[str, list[int]] = defaultdict(list)
        for chunk in by_version[ref.version]:
            if chunk.meta["chunk_type"] != "items":
                assert "item_index" not in chunk.meta
                continue
            first, last = chunk.meta["item_index"], chunk.meta["item_last"]
            assert chunk.meta["item_count"] == last - first + 1 >= 1, chunk.chunk_id
            suffix = f":i{first}" if first == last else f":i{first}-{last}"
            assert chunk.chunk_id.endswith(suffix), chunk.chunk_id
            # One item is one block of lines; items of a group are separated by a blank line.
            if chunk.meta["item_count"] > 1:
                assert len(chunk.text.split("\n\n")) == chunk.meta["item_count"], chunk.chunk_id
            covered[chunk.meta["section_id"]].extend(range(first, last + 1))
        assert {s: len(n) for s, n in covered.items()} == expected, ref.version
        for section, numbers in covered.items():
            assert numbers == list(range(1, len(numbers) + 1)), (ref.version, section)
        total += sum(expected.values())
    assert total > 900


def test_page_text_is_in_the_chunks_without_commit_links_or_site_boilerplate(
    by_version, releases, fetcher
):
    def squeeze(text: str) -> str:
        return re.sub(r"\s+", "", text)

    for ref in releases:
        text = squeeze("".join(c.text for c in by_version[ref.version]))
        soup = BeautifulSoup(fetcher.read_cache(ref.url).content, "lxml")
        entries = soup.select("#docContent div.itemizedlist > ul > li")
        assert entries
        for entry in entries:
            # Explanatory paragraphs (everything after the summary) are kept word for word.
            for paragraph in entry.find_all("p", recursive=False)[1:]:
                assert squeeze(paragraph.get_text()) in text, (ref.version, paragraph.get_text())
            # The summary is kept up to its author credit.
            summary = entry.find("p")
            for link in summary.find_all("a", class_="ulink"):
                link.decompose()
            words = squeeze(re.sub(r"\s+\([^()]*\)\s*$", "", summary.get_text()))
            assert words in text, (ref.version, summary.get_text())
    for chunks in by_version.values():
        for chunk in chunks:
            assert "§" not in chunk.text and "postgr.es" not in chunk.text, chunk.chunk_id
            assert "\xa0" not in chunk.text and "\xa0" not in chunk.title, chunk.chunk_id
            assert "Submit correction" not in chunk.text, chunk.chunk_id
            assert not chunk.text.startswith("Release date"), chunk.chunk_id
            assert "Acknowledg" not in chunk.title, chunk.chunk_id


def test_item_chunks_keep_authors_and_commit_links_in_meta(chunks):
    items = [c for c in chunks if c.meta["chunk_type"] == "items"]
    assert len(items) > 300
    # Nearly every item has an author credit and at least one commit link (in the pages
    # cached on 2026-11-13 all 1,076 items have a credit and all but two a link).
    for field in ("authors", "commits"):
        without = [c.chunk_id for c in items if not c.meta[field]]
        assert len(without) <= len(items) // 100, (field, without)
    for chunk in items:
        for link in chunk.meta["commits"]:
            assert re.fullmatch(r"https://postgr\.es/c/[0-9a-f]+", link), chunk.chunk_id
        for author in chunk.meta["authors"]:
            assert re.fullmatch(r"[^\d()§,]{3,60}", author), (chunk.chunk_id, author)
    names = Counter(author for c in items for author in c.meta["authors"])
    assert names["Tom Lane"] > 100 and len(names) > 100


def test_each_release_has_its_migration_chunk_and_16_0_its_overview(by_version, releases):
    for ref in releases:
        group = {c.chunk_id: c for c in by_version[ref.version]}
        migration = group[f"relnotes:{ref.version}#migration"]
        assert migration.meta["chunk_type"] == "prose"
        label = "16" if ref.version == "16.0" else ref.version
        assert migration.meta["section"] == f"Migration to Version {label}"
        assert migration.text.startswith("A dump/restore ")
        kinds = Counter(c.meta["chunk_type"] for c in group.values())
        assert kinds["prose"] == 1
        assert kinds["overview"] == (1 if ref.version == "16.0" else 0)
    major = {c.chunk_id: c for c in by_version["16.0"]}
    overview = major["relnotes:16.0#highlights"]
    assert overview.title == "PostgreSQL 16 release notes — Overview"
    assert "- Allow monitoring of I/O statistics using the new pg_stat_io view" in overview.text
    # The incompatibilities listed under "Migration to Version 16" are item chunks.
    incompatibilities = [
        c for c in major.values() if c.chunk_id.startswith("relnotes:16.0#migration:i")
    ]
    assert sum(c.meta["item_count"] for c in incompatibilities) == 13
    renamed = [
        c
        for c in incompatibilities
        if "Rename server variable force_parallel_mode to debug_parallel_query" in c.text
    ]
    assert len(renamed) == 1 and "debug_parallel_query" in renamed[0].settings
    # The 16.0 notes are divided into sections; minor releases have a single "Changes" list.
    optimizer = [c for c in major.values() if c.meta["section"] == "Optimizer"]
    assert optimizer and all(
        c.meta["section_path"] == ["Changes", "Server", "Optimizer"]
        and c.title == "PostgreSQL 16 release notes — Changes — Server — Optimizer"
        and c.url == f"{DOCS_BASE_URL}release-16.html#RELEASE-16-OPTIMIZER"
        for c in optimizer
    )
    for version, group in by_version.items():
        if version != "16.0":
            assert {tuple(c.meta["section_path"]) for c in group} == {
                (f"Migration to Version {version}",),
                ("Changes",),
            }


def test_settings_field_agrees_with_the_shared_detector(chunks):
    names = load_guc_names()
    assert len(names) > 300, "data/chunks/guc_names.json is written by `make ingest-docs`"
    for chunk in chunks:
        assert chunk.settings == find_settings(chunk.text), chunk.chunk_id
        assert not AMBIGUOUS_NAMES & set(chunk.settings), chunk.chunk_id
    tagged = [c for c in chunks if c.settings]
    assert 20 <= len(tagged) < len(chunks) // 2
    mentions = Counter(name for c in chunks for name in c.settings)
    assert set(mentions) <= set(names)
    # Known mentions: the vacuum buffer limit of 16.0 "also controls autovacuum".
    usage_limit = [c for c in chunks if "vacuum_buffer_usage_limit" in c.settings]
    assert [c.meta["version"] for c in usage_limit] == ["16.0"]
    assert "autovacuum" in usage_limit[0].settings
    assert mentions["autovacuum"] >= 3 and mentions["shared_buffers"] >= 1
