"""``pgdiag-ingest-release-notes`` end to end, offline: a fake site served by a fake transport."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pgdiag.chunks import GUC_NAMES_FILE, RELEASE_NOTES_CHUNKS_FILE, read_jsonl
from pgdiag.config import load_config
from pgdiag.fetch import ROBOTS_URL, PoliteFetcher, RateLimiter
from pgdiag.ingest.release_notes import cli
from pgdiag.ingest.release_notes.cli import ingest, main, parameter_names
from pgdiag.ingest.release_notes.parse import INDEX_URL
from pgdiag.settings import TARGET_SETTINGS
from tests.fakes import FakeClock, FakeResponse, FakeSession

FIXTURES = Path(__file__).parent / "fixtures" / "release_notes"
BASE = "https://www.postgresql.org/docs/16/"
ROBOTS = b"User-agent: *\nDisallow: /docs/devel/\nDisallow: /list/\n"
#: The fixture index lists 16.2, 16.1, 16 and "Prior Releases".
RELEASE_PAGES = ("release-16", "release-16-1", "release-16-2")
NAMES = (*TARGET_SETTINGS, "debug_parallel_query", "hot_standby_feedback", "port", "role")
NO_ITEMS = b"""<html><body><div id="docContent"><div class="sect1" id="RELEASE-16-1">
<div class="titlepage"><h2 class="title">E.16. Release 16.1</h2></div>
<p><strong>Release date: </strong>2023-11-09</p>
<div class="sect2" id="RELEASE-16-1-CHANGES">
<div class="titlepage"><h3 class="title">E.16.2. Changes</h3></div><p>Nothing here.</p>
</div></div></div></body></html>"""


def url(page: str) -> str:
    return f"{BASE}{page}.html"


def fixture(page: str) -> bytes:
    return (FIXTURES / f"{page}.html").read_bytes()


def make_site(**overrides: FakeResponse) -> dict[str, FakeResponse]:
    site = {ROBOTS_URL: FakeResponse(content=ROBOTS)}
    for page in ("release", *RELEASE_PAGES, "release-prior"):
        site[url(page)] = FakeResponse(content=fixture(page))
    for page, response in overrides.items():
        site[url(page.replace("_", "-"))] = response
    return site


@pytest.fixture
def cfg(tmp_path):
    return load_config(env={}, root=tmp_path)


def make_fetcher(cfg, routes) -> tuple[PoliteFetcher, FakeSession]:
    clock = FakeClock()
    session = FakeSession(routes, clock)
    limiter = RateLimiter(cfg.crawl_delay_seconds, clock=clock.time, sleep=clock.sleep)
    return PoliteFetcher(cfg, session=session, rate_limiter=limiter, sleep=clock.sleep), session


def write_guc_names(cfg, names=NAMES) -> Path:
    path = cfg.chunks_dir / GUC_NAMES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pg_version": "16", "names": sorted(names)}), encoding="utf-8")
    return path


@pytest.fixture
def run_main(cfg, monkeypatch):
    """``main()`` on a temporary repo root with a fake transport; returns ``(run, sessions)``."""
    sessions: list[FakeSession] = []

    def run(argv: list[str], routes: dict | None = None) -> int:
        fetcher, session = make_fetcher(cfg, routes if routes is not None else {})
        sessions.append(session)
        monkeypatch.setattr(cli, "get_config", lambda: cfg)
        monkeypatch.setattr(cli, "PoliteFetcher", lambda _cfg: fetcher)
        return main(argv)

    return run, sessions


# --------------------------------------------------------------------- ingest


def test_ingest_reads_the_index_then_every_release_page_it_lists(cfg):
    fetcher, session = make_fetcher(cfg, make_site())
    result = ingest(cfg, fetcher, names=NAMES)
    assert result.problems == []
    assert [release.version for release in result.releases] == ["16.0", "16.1", "16.2"]
    assert [release.release_date for release in result.releases] == [
        "2023-09-14",
        "2023-11-09",
        "2024-02-08",
    ]
    # robots.txt, the index, then the release pages oldest first. "Prior Releases" is
    # listed in the index but carries no notes: it is never requested.
    assert session.urls == [ROBOTS_URL, INDEX_URL, *(url(page) for page in RELEASE_PAGES)]
    assert (result.pages, result.fetched, result.cached) == (4, 4, 0)
    assert result.index.ignored == [(url("release-prior"), "Prior Releases")]
    assert result.chunks == [c for release in result.releases for c in release.chunks]
    assert {c.meta["version"] for c in result.chunks} == {"16.0", "16.1", "16.2"}
    assert all(c.source == "release_notes" for c in result.chunks)

    # --chunk-only on the same cache: the same chunks without a single request.
    offline, quiet = make_fetcher(cfg, {})
    again = ingest(cfg, offline, download=False, names=NAMES)
    assert again.chunks == result.chunks and quiet.calls == []
    assert (again.pages, again.fetched, again.cached) == (4, 0, 4)
    # --download-only reads the pages but produces no chunks.
    nothing = ingest(cfg, offline, chunk=False, names=NAMES)
    assert nothing.chunks == [] and nothing.problems == [] and nothing.pages == 4


def test_ingest_reports_what_is_missing_from_the_cache(cfg):
    offline, session = make_fetcher(cfg, {})
    result = ingest(cfg, offline, download=False, names=NAMES)
    assert result.chunks == [] and session.calls == []
    assert result.problems == [f"missing: {INDEX_URL} (not in the cache)"]

    # Only the index and 16.0 are cached: both minor releases are reported.
    partial = {page: FakeResponse(500, b"down") for page in ("release_16_1", "release_16_2")}
    fetcher, _ = make_fetcher(cfg, make_site(**partial))
    fetcher.max_retries = 0
    assert len(ingest(cfg, fetcher, names=NAMES).problems) == 2
    result = ingest(cfg, offline, download=False, names=NAMES)
    assert result.problems == [
        f"missing: {url('release-16-1')} (not in the cache)",
        f"missing: {url('release-16-2')} (not in the cache)",
    ]
    assert [release.version for release in result.releases] == ["16.0"]


@pytest.mark.parametrize(
    "overrides, problem",
    [
        ({"release_16_1": FakeResponse(404, b"not found")}, "missing: {url} (HTTP 404)"),
        ({"release_16_1": FakeResponse(content=NO_ITEMS)}, "no release-note items found"),
        ({"release_16_1": FakeResponse(content=b"<html>oops</html>")}, "no documentation content"),
        # The index says 16.1, the page says 16.2.
        ({"release_16_1": FakeResponse(content=fixture("release-16-2"))}, "is release 16.2, but"),
    ],
)
def test_ingest_records_a_problem_for_a_missing_or_unparsable_release_page(cfg, overrides, problem):
    fetcher, _ = make_fetcher(cfg, make_site(**overrides))
    result = ingest(cfg, fetcher, names=NAMES)
    assert len(result.problems) == 1
    assert problem.format(url=url("release-16-1")) in result.problems[0]
    # The other releases are still parsed, so the report shows everything that is wrong.
    assert [release.version for release in result.releases] == ["16.0", "16.2"]


def test_ingest_rejects_an_index_without_the_release_list(cfg):
    broken = make_site(release=FakeResponse(content=fixture("release-prior")))
    fetcher, session = make_fetcher(cfg, broken)
    result = ingest(cfg, fetcher, names=NAMES)
    assert len(result.problems) == 1 and result.problems[0].startswith("unusable index: ")
    assert result.chunks == [] and session.urls == [ROBOTS_URL, INDEX_URL]


# ----------------------------------------------------------------------- main


def test_main_writes_the_chunk_file_and_prints_the_stats(cfg, run_main, capsys):
    run, sessions = run_main
    write_guc_names(cfg)
    assert run([], make_site()) == 0
    output = cfg.chunks_dir / RELEASE_NOTES_CHUNKS_FILE
    chunks = read_jsonl(output)  # validates the schema and id uniqueness
    assert {c.meta["version"] for c in chunks} == {"16.0", "16.1", "16.2"}
    assert all(c.source == "release_notes" and c.chunk_id.startswith("relnotes:") for c in chunks)
    out = capsys.readouterr().out
    assert "4 fetched this run, 0 from cache" in out
    assert "versions: 3 (16.0 to 16.2)" in out
    assert "ignored (no PostgreSQL 16 notes): release-prior (Prior Releases)" in out
    assert f"wrote {len(chunks)} chunks to {output}" in out
    for name in TARGET_SETTINGS:
        assert f"  {name:<32} " in out
    for line in ("  16.0     2023-09-14", "  16.1     2023-11-09", "  16.2     2024-02-08"):
        assert line in out

    # Again from the cache, to another file: identical output, no request.
    other = cfg.root / "other.jsonl"
    assert run(["--chunk-only", "--output", str(other)]) == 0
    assert other.read_bytes() == output.read_bytes()
    assert sessions[-1].calls == []
    assert "0 fetched this run, 4 from cache (0 HTTP requests)" in capsys.readouterr().out


def test_main_tags_settings_with_the_generated_names_but_not_port_or_role(cfg, run_main):
    run, _ = run_main
    names_path = write_guc_names(cfg)
    assert set(parameter_names(names_path)) == set(NAMES) - {"port", "role"}
    assert run([], make_site()) == 0
    chunks = {c.chunk_id: c for c in read_jsonl(cfg.chunks_dir / RELEASE_NOTES_CHUNKS_FILE)}
    # "Prevent the pg_signal_backend role from signalling background workers and autovacuum ..."
    assert "pg_signal_backend role" in chunks["relnotes:16.1#changes:i3"].text
    assert chunks["relnotes:16.1#changes:i3"].settings == ["autovacuum"]
    assert chunks["relnotes:16.0#migration:i2-6"].settings == [
        "debug_parallel_query",
        "hot_standby_feedback",
    ]
    assert not [c.chunk_id for c in chunks.values() if {"port", "role"} & set(c.settings)]


@pytest.mark.parametrize(
    "argv, overrides, message",
    [
        (["--chunk-only"], {}, "run without --chunk-only to download"),
        ([], {"release_16_2": FakeResponse(404, b"not found")}, "nothing written"),
        ([], {"release_16_2": FakeResponse(content=NO_ITEMS)}, "nothing written"),
    ],
)
def test_main_fails_and_writes_nothing_when_a_page_is_missing_or_unparsable(
    cfg, run_main, capsys, argv, overrides, message
):
    run, _ = run_main
    write_guc_names(cfg)
    output = cfg.chunks_dir / RELEASE_NOTES_CHUNKS_FILE
    output.write_text("previous run\n", encoding="utf-8")
    assert run(argv, make_site(**overrides)) == 1
    captured = capsys.readouterr()
    assert "PROBLEM" in captured.out and message in captured.err
    assert output.read_text(encoding="utf-8") == "previous run\n"  # untouched
    assert sorted(p.name for p in cfg.chunks_dir.iterdir()) == [
        GUC_NAMES_FILE,
        RELEASE_NOTES_CHUNKS_FILE,
    ]


def test_main_download_only_fills_the_cache_and_writes_no_chunks(cfg, run_main):
    run, sessions = run_main
    assert run(["--download-only"], make_site()) == 0  # needs no parameter-name list
    assert len(sessions[-1].urls) == 5
    assert not (cfg.chunks_dir / RELEASE_NOTES_CHUNKS_FILE).exists()


def test_main_needs_the_parameter_names_written_by_the_docs_stage(cfg, run_main, capsys):
    run, sessions = run_main
    assert run([], make_site()) == 1
    assert "pgdiag-ingest-docs" in capsys.readouterr().err
    assert sessions[-1].calls == []
    assert not (cfg.chunks_dir / RELEASE_NOTES_CHUNKS_FILE).exists()
