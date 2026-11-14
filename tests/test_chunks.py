"""The shared chunk schema, the JSONL helpers and setting-mention detection."""

from __future__ import annotations

import json

import pytest

from pgdiag import chunks as chunks_module
from pgdiag.chunks import (
    Chunk,
    ChunkError,
    find_settings,
    load_guc_names,
    read_jsonl,
    save_guc_names,
    validate_chunks,
    write_jsonl,
)
from pgdiag.config import load_config
from pgdiag.settings import TARGET_SETTINGS


def make_chunk(**overrides) -> Chunk:
    values = {
        "chunk_id": "docs:runtime-config-resource#GUC-WORK-MEM",
        "source": "docs",
        "url": "https://www.postgresql.org/docs/16/runtime-config-resource.html#GUC-WORK-MEM",
        "title": "20.4. Resource Consumption — work_mem",
        "text": "work_mem (integer)\n\nSets the base maximum amount of memory…",
        "settings": ["hash_mem_multiplier", "work_mem"],
        "meta": {"chunk_type": "setting", "setting": "work_mem", "pg_version": "16"},
    }
    values.update(overrides)
    return Chunk(**values)


# ---------------------------------------------------------------------- schema


def test_valid_chunk_passes_validation_for_every_source():
    assert make_chunk().validate().chunk_id.startswith("docs:")
    make_chunk(chunk_id="relnotes:16.3#item-12", source="release_notes").validate()
    make_chunk(chunk_id="ml:CAKJS1f8=abc@mail.gmail.com", source="mailing_list").validate()


@pytest.mark.parametrize(
    "overrides",
    [
        {"chunk_id": ""},
        {"chunk_id": "docs:has space"},
        {"chunk_id": "work_mem"},  # no source prefix
        {"chunk_id": "ml:123"},  # prefix of another source
        {"source": "wiki"},
        {"url": "/docs/16/runtime-config-resource.html"},
        {"title": "  "},
        {"text": ""},
        {"settings": ["work_mem", "hash_mem_multiplier"]},  # not sorted
        {"settings": ["work_mem", "work_mem"]},
        {"settings": "work_mem"},
        {"meta": ["not", "a", "dict"]},
        {"meta": {"when": object()}},
    ],
)
def test_invalid_chunks_are_rejected(overrides):
    with pytest.raises(ChunkError):
        make_chunk(**overrides).validate()


def test_from_dict_rejects_missing_and_unknown_fields():
    data = make_chunk().to_dict()
    assert Chunk.from_dict(data) == make_chunk()
    with pytest.raises(ChunkError, match="missing"):
        Chunk.from_dict({k: v for k, v in data.items() if k != "text"})
    with pytest.raises(ChunkError, match="unknown"):
        Chunk.from_dict({**data, "embedding": [0.1]})
    minimal = {k: data[k] for k in ("chunk_id", "source", "url", "title", "text")}
    assert Chunk.from_dict(minimal).settings == [] and Chunk.from_dict(minimal).meta == {}


def test_duplicate_chunk_ids_are_rejected():
    with pytest.raises(ChunkError, match="duplicate"):
        validate_chunks([make_chunk(), make_chunk(text="other text")])


# ----------------------------------------------------------------------- JSONL


def test_jsonl_round_trip_preserves_everything(tmp_path):
    originals = [
        make_chunk(),
        make_chunk(
            chunk_id="docs:planner-stats#PLANNER-STATS-EXTENDED:p2",
            title="14.2. Statistics Used by the Planner — 14.2.2. Extended Statistics (part 2/3)",
            text="SELECT *\n  FROM t;\n\n“quoted” — naïve ünïcödé\ttab",
            settings=[],
            meta={"chunk_type": "section", "section_path": ["14.2. A", "14.2.2. B"], "part": 2},
        ),
    ]
    path = tmp_path / "chunks" / "docs.jsonl"
    assert write_jsonl(path, originals) == 2
    assert read_jsonl(path) == originals
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and all(json.loads(line)["source"] == "docs" for line in lines)
    assert "ünïcödé" in lines[1]  # stored as UTF-8, not \u escapes
    assert not list(path.parent.glob("*.tmp*"))


def test_write_jsonl_refuses_invalid_input_and_keeps_the_old_file(tmp_path):
    path = tmp_path / "docs.jsonl"
    write_jsonl(path, [make_chunk()])
    with pytest.raises(ChunkError):
        write_jsonl(path, [make_chunk(), make_chunk()])
    with pytest.raises(ChunkError):
        write_jsonl(path, [make_chunk(text="")])
    assert read_jsonl(path) == [make_chunk()]


def test_read_jsonl_reports_file_and_line_of_a_bad_record(tmp_path):
    path = tmp_path / "docs.jsonl"
    good = json.dumps(make_chunk().to_dict())
    path.write_text(good + "\n" + good.replace('"docs"', '"wiki"') + "\n", encoding="utf-8")
    with pytest.raises(ChunkError, match=r"docs\.jsonl:2"):
        read_jsonl(path)
    path.write_text(good + "\n\n" + good + "\n", encoding="utf-8")
    with pytest.raises(ChunkError, match="duplicate"):
        read_jsonl(path)


# ------------------------------------------------------------ setting mentions


def test_find_settings_detects_each_target_setting():
    for name in TARGET_SETTINGS:
        assert find_settings(f"We raised {name} and it helped.", TARGET_SETTINGS) == [name]
    text = " ".join(TARGET_SETTINGS)
    assert find_settings(text, TARGET_SETTINGS) == sorted(TARGET_SETTINGS)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Longer names that merely contain a target name do not count.
        ("autovacuum_naptime and log_autovacuum_min_duration were changed", []),
        ("autovacuum_vacuum_scale_factor = 0.01; ALTER TABLE t SET (autovacuum_enabled = off)", []),
        ("maintenance_work_mem = 1GB", ["maintenance_work_mem"]),
        ("autovacuum_work_mem and logical_decoding_work_mem", []),
        ("min_wal_size", []),
        ("my_shared_buffers_copy, shared_buffers2, xwork_mem", []),
        # Real mentions in the forms people write them.
        ("SET work_mem='64MB';", ["work_mem"]),
        ("work_mem=4MB,maintenance_work_mem=64MB", ["maintenance_work_mem", "work_mem"]),
        ("(shared_buffers)", ["shared_buffers"]),
        ("Is autovacuum on? autovacuum=off.", ["autovacuum"]),
        (
            "Autovacuum was disabled; WORK_MEM is 4MB; Shared_Buffers 128MB",
            ["autovacuum", "shared_buffers", "work_mem"],
        ),
        ("random_page_cost\nvs seq_page_cost", ["random_page_cost"]),
        ('`jit_above_cost`, "max_wal_size".', ["jit_above_cost", "max_wal_size"]),
        ("see #GUC-WORK-MEM", []),
        ("", []),
    ],
)
def test_find_settings_uses_whole_identifier_matching(text, expected):
    assert find_settings(text, TARGET_SETTINGS) == expected


def test_find_settings_with_the_full_parameter_list():
    names = [
        *TARGET_SETTINGS,
        "autovacuum_naptime",
        "seq_page_cost",
        "jit",
        "ssl",
        "port",
        "DateStyle",
    ]
    found = find_settings(
        "autovacuum_naptime, Seq_Page_Cost, jit = off and DateStyle; JIT and SSL are features; "
        "the port is 5432",
        names,
    )
    # Underscore names are case-insensitive; single words must match exactly.
    assert found == ["DateStyle", "autovacuum_naptime", "jit", "port", "seq_page_cost"]
    assert find_settings("JIT, SSL, datestyle", names) == []


def test_ambiguous_everyday_words_are_ignored_in_the_generated_list(tmp_path, monkeypatch):
    cfg = load_config(env={}, root=tmp_path)
    monkeypatch.setattr(chunks_module, "get_config", lambda: cfg)
    load_guc_names.cache_clear()
    try:
        # No generated file yet: fall back to the ten targets.
        assert load_guc_names() == tuple(sorted(TARGET_SETTINGS))
        assert find_settings("port 5432, seq_page_cost 1, work_mem 4MB") == ["work_mem"]

        path = save_guc_names(["port", "role", "seq_page_cost", "jit"], cfg, pg_version="16")
        assert path == cfg.chunks_dir / "guc_names.json"
        assert json.loads(path.read_text())["pg_version"] == "16"
        names = load_guc_names()
        assert set(TARGET_SETTINGS) <= set(names) and "seq_page_cost" in names
        assert find_settings("the role on port 5432 set seq_page_cost and work_mem; jit off") == [
            "jit",
            "seq_page_cost",
            "work_mem",
        ]
    finally:
        load_guc_names.cache_clear()
