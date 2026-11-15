"""Offline tests for the T5 workload: statistics, presets, result schema, compare
maths, query-set integrity, generator SQL and the CLI parser. No database needed."""

from __future__ import annotations

import json
import re

import pytest

from pgdiag.config import load_config
from pgdiag.settings import TARGET_SETTINGS
from pgdiag.workload import bench, cli, datagen, probe
from pgdiag.workload.profiles import INT_FIELDS, PROFILES, DataProfile, get_profile
from pgdiag.workload.queryset import (
    COMPONENT_IDS,
    COMPONENTS,
    QuerySetError,
    component_settings,
    coverage,
    load_queries,
    parse_query_file,
    queries_by_component,
    render_offline,
)

# ---------------------------------------------------------------------------
# statistics


def test_summarize_median_spread_and_cv():
    s = bench.summarize([10.0, 12.0, 11.0, 30.0, 11.5])
    assert s["n"] == 5
    assert s["median"] == 11.5
    assert s["min"] == 10.0 and s["max"] == 30.0
    assert s["spread_pct"] == pytest.approx((30 - 10) / 11.5 * 100)
    assert s["mean"] == pytest.approx(74.5 / 5)
    assert s["cv_pct"] > 0


def test_summarize_even_count_and_single_value():
    assert bench.summarize([1.0, 2.0, 3.0, 4.0])["median"] == 2.5
    one = bench.summarize([7.0])
    assert one["median"] == 7.0 and one["stdev"] == 0.0 and one["spread_pct"] == 0.0
    with pytest.raises(ValueError):
        bench.summarize([])


def test_rel_diff_pct_sign_and_zero_reference():
    assert bench.rel_diff_pct(10.0, 12.0) == pytest.approx(20.0)
    assert bench.rel_diff_pct(10.0, 9.0) == pytest.approx(-10.0)
    with pytest.raises(ZeroDivisionError):
        bench.rel_diff_pct(0.0, 1.0)


# ---------------------------------------------------------------------------
# presets


def test_presets_full_quick_smoke():
    full, quick = bench.get_preset("full"), bench.get_preset("quick")
    assert full.passes >= 5 and full.warmup >= 1
    assert quick.passes >= 3 and quick.warmup >= 1
    assert quick.warmup + quick.passes < full.warmup + full.passes
    assert bench.get_preset("smoke").passes == 1
    custom = bench.Preset("x", 0, 2, "custom")
    assert bench.get_preset(custom) is custom
    with pytest.raises(ValueError):
        bench.get_preset("nope")
    with pytest.raises(ValueError):
        bench.get_preset(bench.Preset("bad", 0, 0, ""))


# ---------------------------------------------------------------------------
# result schema and compare


def make_result(label: str, totals: list[float], scale: dict[str, float] | None = None, warmup=1):
    comps = list(COMPONENT_IDS)
    scale = scale or {}
    passes = []
    for i, total in enumerate([totals[0]] * warmup + totals):
        share = {c: total / len(comps) * scale.get(c, 1.0) for c in comps}
        passes.append(
            bench.PassResult(
                index=i,
                warmup=i < warmup,
                started_at="2026-01-01T00:00:00Z",
                total_s=sum(share.values()),
                components=share,
                queries={"q1": 1.0 * scale.get("q1", 1.0)},
            )
        )
    return bench.BenchResult(
        label=label,
        preset={"name": "full", "warmup": warmup, "passes": len(totals), "description": ""},
        started_at="2026-01-01T00:00:00Z",
        finished_at="2026-01-01T00:05:00Z",
        duration_s=300.0,
        git={"commit": "abc", "branch": "x", "dirty": False},
        query_set_version="1",
        components=comps,
        data={"dbname": "pgdiag", "profile": PROFILES["full"].to_dict(), "seed": 1, "sizes": {}},
        settings={name: "v" for name in TARGET_SETTINGS},
        settings_detail=[],
        context_settings={},
        session_settings={},
        server_version="16.16",
        container={"mem_limit_bytes": 1 << 30},
        passes=passes,
        summary=bench.build_summary(passes, comps),
    )


def test_build_summary_uses_only_measured_passes():
    res = make_result("a", [10.0, 20.0, 30.0])
    res.passes[0].total_s = 1000.0  # warm-up must not count
    res.summary = bench.build_summary(res.passes, res.components)
    assert res.summary["total"]["median"] == pytest.approx(20.0)
    assert res.summary["total"]["n"] == 3
    assert set(res.summary["components"]) == set(COMPONENT_IDS)
    assert len(res.measured()) == 3


def test_result_json_round_trip_and_schema(tmp_path):
    res = make_result("a", [10.0, 11.0, 12.0])
    path = res.save(tmp_path / "sub" / "r.json")
    data = json.loads(path.read_text())
    for key in (
        "schema_version",
        "label",
        "preset",
        "started_at",
        "git",
        "query_set_version",
        "data",
        "settings",
        "container",
        "passes",
        "summary",
        "errors",
    ):
        assert key in data, key
    assert "path" not in data
    assert set(data["settings"]) == set(TARGET_SETTINGS)
    assert {"median", "min", "max", "spread_pct"} <= set(data["summary"]["total"])
    loaded = bench.load_result(path)
    assert loaded.total_median == res.total_median
    assert loaded.passes == res.passes
    assert loaded.path == str(path)
    assert loaded.ok

    data["schema_version"] = 999
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        bench.load_result(path)


def test_compare_total_and_components():
    a = make_result("a", [10.0, 10.0, 10.0])
    b = make_result("b", [12.0, 12.0, 12.0], scale={"pgbench": 2.0})
    b.settings["work_mem"] = "64kB"
    diff = bench.compare(a, b)
    expected_b_total = b.total_median
    assert diff["total"]["a"] == pytest.approx(10.0)
    assert diff["total"]["b"] == pytest.approx(expected_b_total)
    assert diff["total"]["rel_diff_pct"] == pytest.approx((expected_b_total - 10) / 10 * 100)
    assert diff["components"]["pgbench"]["ratio"] == pytest.approx(2.4)
    assert diff["components"]["tasks_queue"]["rel_diff_pct"] == pytest.approx(20.0)
    assert diff["settings_changed"] == {"work_mem": ["v", "64kB"]}
    assert diff["warnings"] == []
    text = bench.format_compare(diff, queries=True)
    assert "TOTAL" in text and "work_mem" in text


def test_compare_warns_about_different_presets():
    a = make_result("a", [10.0])
    b = make_result("b", [10.0])
    b.preset["name"] = "quick"
    assert any("presets differ" in w for w in bench.compare(a, b)["warnings"])


def test_format_summary_mentions_every_component():
    text = bench.format_summary(make_result("a", [10.0, 11.0]))
    for comp in COMPONENT_IDS:
        assert comp in text


def test_default_result_path_is_under_results_bench(tmp_path):
    cfg = load_config(env={}, root=tmp_path)
    path = bench.default_result_path(cfg, "case work_mem/1", "quick", "2026-01-02T03:04:05Z")
    assert path.parent == tmp_path / "results" / "bench"
    assert path.name == "20260102T030405Z_case-work_mem-1_quick.json"


def test_baseline_paths(tmp_path):
    cfg = load_config(env={}, root=tmp_path)
    assert cli.baseline_path("full", cfg) == tmp_path / "results" / "baseline.json"
    assert cli.baseline_path("quick", cfg) == tmp_path / "results" / "baseline-quick.json"
    assert cli.baseline_path("full", cfg, previous=True).name == "baseline.prev.json"


# ---------------------------------------------------------------------------
# query set


def test_query_ids_unique_and_match_file_names():
    queries = load_queries()
    ids = [q.id for q in queries]
    assert len(ids) == len(set(ids))
    for q in queries:
        assert re.sub(r"^\d+_", "", q.filename) == f"{q.id}.sql"


def test_every_target_setting_is_covered():
    cov = coverage()
    assert set(cov) == set(TARGET_SETTINGS)
    for name, ids in cov.items():
        assert ids, f"no query or component is designed to be sensitive to {name}"
    flat = component_settings()
    assert set().union(*flat.values()) == set(TARGET_SETTINGS)


def test_every_sql_component_has_queries_and_pgbench_has_none():
    grouped = queries_by_component()
    for comp in COMPONENTS:
        if comp.kind == "pgbench":
            assert grouped[comp.id] == []
        else:
            assert grouped[comp.id], comp.id


def test_queries_are_in_component_order():
    order = {cid: i for i, cid in enumerate(COMPONENT_IDS)}
    positions = [order[q.component] for q in load_queries()]
    assert positions == sorted(positions)


@pytest.mark.parametrize("profile_name", sorted(PROFILES))
def test_every_query_renders_without_placeholders(profile_name):
    profile = PROFILES[profile_name]
    for q in load_queries():
        for i in (1, q.repeat):
            text = render_offline(q, profile, i)
            assert "%(" not in text and "%%" not in text, q.id
            assert text.count("(") == text.count(")"), q.id
            first = text.lstrip().split(None, 1)[0].upper()
            assert first in {"SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "VACUUM"}, q.id


def test_repeat_index_changes_the_statement_where_used():
    profile = PROFILES["full"]
    q = next(q for q in load_queries() if q.id == "audience_snapshot")
    assert q.repeat > 1
    assert render_offline(q, profile, 1) != render_offline(q, profile, 2)


GOOD = """-- id: x_query
-- component: sort_reports
-- description: test
-- sensitive_to: work_mem, random_page_cost
-- repeat: 3
SELECT mod(%(i)s, %(customers)s);
"""


def test_parse_query_file_accepts_a_valid_file():
    q = parse_query_file("99_x_query.sql", GOOD)
    assert q.id == "x_query" and q.repeat == 3
    assert q.sensitive_to == ("work_mem", "random_page_cost")
    assert q.sql == "SELECT mod(%(i)s, %(customers)s)"
    assert q.params(PROFILES["tiny"], 2) == {"i": 2, "customers": PROFILES["tiny"].customers}


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (GOOD.replace("-- id: x_query\n", ""), "missing header"),
        (GOOD.replace("work_mem, random_page_cost", "work_memory"), "unknown settings"),
        (GOOD.replace("sort_reports", "nope"), "unknown component"),
        (GOOD.replace("mod(%(i)s, %(customers)s)", "%(i)s % 3"), "mod()"),
        (GOOD.replace("%(customers)s", "%(bogus)s"), "unknown placeholders"),
        (GOOD.replace(";", "; SELECT 2"), "one statement"),
        (GOOD.replace("-- repeat: 3", "-- repeat: 0"), "repeat"),
        (GOOD.replace("-- id: x_query", "-- id: other"), "file name"),
    ],
)
def test_parse_query_file_rejects_bad_files(text, message):
    with pytest.raises(QuerySetError, match=re.escape(message)):
        parse_query_file("99_x_query.sql", text)


# ---------------------------------------------------------------------------
# profiles, generator SQL, probe, baseline config


def test_profiles():
    assert set(PROFILES) == {"full", "tiny"}
    assert get_profile("tiny").name == "tiny"
    with pytest.raises(ValueError):
        get_profile("huge")
    assert set(INT_FIELDS) == set(DataProfile.__dataclass_fields__) - {"name"}
    full, tiny = PROFILES["full"], PROFILES["tiny"]
    for name in INT_FIELDS:
        assert getattr(tiny, name) <= getattr(full, name), name
    for p in (full, tiny):
        assert p.task_batch < p.tasks
        assert p.campaign_size < p.customers


@pytest.mark.parametrize("profile_name", sorted(PROFILES))
def test_generator_sql_is_complete(profile_name):
    p = PROFILES[profile_name]
    stmts = (
        datagen.customers_sql(p)
        + datagen.products_sql(p)
        + datagen.orders_sql(p)
        + datagen.campaign_targets_sql(p)
        + bench.tasks_setup_sql(p)
    )
    text = "\n".join(stmts)
    for table in ("wl_customers", "wl_products", "wl_orders", "wl_campaign_targets", "wl_tasks"):
        assert f"CREATE TABLE {table}" in text
    assert "CREATE STATISTICS" in text  # correlated columns
    assert text.count("(") == text.count(")")
    inserts = [s for s in datagen.orders_sql(p) if s.lstrip().startswith("INSERT")]
    assert len(inserts) == -(-p.orders // datagen.ORDERS_CHUNK)


def test_seed_values_are_deterministic_and_in_range():
    a = datagen._seed_value(1, "wl_orders")
    assert a == datagen._seed_value(1, "wl_orders")
    assert a != datagen._seed_value(2, "wl_orders")
    assert a != datagen._seed_value(1, "wl_customers")
    assert -1.0 <= a <= 1.0


def test_probe_cases_cover_every_target_once():
    assert [c.setting for c in probe.PROBE_CASES] == list(TARGET_SETTINGS)
    assert {c.method for c in probe.PROBE_CASES} <= {"session", "reload", "restart"}
    methods = {c.setting: c.method for c in probe.PROBE_CASES}
    assert methods["shared_buffers"] == "restart"
    assert methods["autovacuum"] == methods["max_wal_size"] == "reload"
    assert probe.parse_overrides(["work_mem=128kB"]) == {"work_mem": "128kB"}
    for bad in (["work_mem"], ["fsync=off"]):
        with pytest.raises(ValueError):
            probe.parse_overrides(bad)


def _baseline_conf(repo_root) -> dict[str, str]:
    settings = {}
    for line in (repo_root / "docker/postgres/postgresql.conf").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            name, value = line.split("=", 1)
            settings[name.strip()] = value.strip().strip("'")
    return settings


def _mb(value: str) -> float:
    m = re.fullmatch(r"(\d+)\s*(kB|MB|GB)", value)
    assert m, value
    return int(m.group(1)) * {"kB": 1 / 1024, "MB": 1, "GB": 1024}[m.group(2)]


def test_baseline_conf_sets_every_target_and_fits_in_memory(repo_root):
    conf = _baseline_conf(repo_root)
    for name in TARGET_SETTINGS:
        assert name in conf, f"{name} must be set explicitly in the baseline file"
    assert conf["autovacuum"] == "on"
    assert int(conf["max_parallel_workers_per_gather"]) >= 2
    # 1 GiB container: shared_buffers + a parallel hash (3 participants x 2 x work_mem)
    # + maintenance_work_mem must leave room for the page cache.
    budget = (
        _mb(conf["shared_buffers"])
        + 3 * 2 * _mb(conf["work_mem"])
        + _mb(conf["maintenance_work_mem"])
    )
    assert budget <= 700, budget
    # parallel VACUUM puts maintenance_work_mem worth of dead-tuple space into /dev/shm (256 MB)
    assert _mb(conf["maintenance_work_mem"]) < 256


def test_cli_parser_knows_every_subcommand():
    parser = cli.build_parser()
    for argv in (
        ["generate", "--reset", "--profile", "tiny"],
        ["bench", "--preset", "quick", "--label", "x"],
        ["baseline"],
        ["baseline-check", "--max-diff", "5"],
        ["compare", "a.json", "b.json"],
        ["probe-sensitivity", "--settings", "work_mem", "--set", "work_mem=128kB"],
        ["queries"],
        ["status"],
    ):
        assert callable(parser.parse_args(argv).func)


def test_cli_queries_lists_everything(capsys):
    assert cli.main(["queries"]) == 0
    out = capsys.readouterr().out
    for q in load_queries():
        assert q.id in out
    for name in TARGET_SETTINGS:
        assert name in out
