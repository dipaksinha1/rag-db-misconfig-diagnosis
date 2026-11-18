"""T8 retrieval: offline tests for build_queries / merge, and one real-data check."""

from __future__ import annotations

import json
import os

import pytest

from pgdiag.config import get_config
from pgdiag.index.search import Hit
from pgdiag.retrieve.core import build_queries, merge
from pgdiag.settings import CASE_SETTINGS

PLAN_SLOW_NO_GATHER = (
    "Aggregate  (cost=1.00..2.00 rows=1 width=8) (actual time=900.0..950.0 rows=1 loops=1)\n"
    "  ->  Seq Scan on t  (cost=0.00..1.00 rows=5000 width=4) "
    "(actual time=0.1..800.0 rows=5000 loops=1)"
)


def doc(**over) -> dict:
    base = {
        "config": [{"name": "work_mem", "setting": "64kB"}],
        "database_stats": {"hit_pct": 99.0, "temp_files": 0},
        "top_statements": [{"total_ms": 1000.0, "jit_ms": 0.0}],
        "table_stats": [{"table": "t", "idx_scan": 0}],
        "explain": [],
    }
    base.update(over)
    return base


def joined(d: dict) -> str:
    return " ".join(build_queries(d)).lower()


def test_quiet_document_gives_only_generic_queries():
    assert build_queries(doc()) == [
        "PostgreSQL server configuration performance tuning slow queries"
    ]


def test_deterministic_and_no_config_or_case_leak():
    d = doc(database_stats={"hit_pct": 50.0, "temp_files": 5000})
    assert build_queries(d) == build_queries(json.loads(json.dumps(d)))
    text = joined(d)
    for setting in CASE_SETTINGS:
        assert setting not in text
    assert "c02" not in text and "64kb" not in text


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"database_stats": {"hit_pct": 99.0, "temp_files": 5000}}, "temporary disk files"),
        ({"database_stats": {"hit_pct": 50.0, "temp_files": 0}}, "cache hit ratio"),
        ({"top_statements": [{"total_ms": 1000.0, "jit_ms": 600.0}]}, "jit compilation"),
        ({"explain": [{"plan": PLAN_SLOW_NO_GATHER}]}, "parallel query not used"),
        (
            {"explain": [{"plan": PLAN_SLOW_NO_GATHER}], "table_stats": [{"idx_scan": 3}]},
            "sequential scan instead of index scan",
        ),
    ],
)
def test_rules_fire(over, needle):
    assert needle not in joined(doc())
    assert needle in joined(doc(**over))


def test_row_misestimate_rule():
    plan = "Sort  (cost=1..2 rows=100 width=4) (actual time=1.0..2.0 rows=50000 loops=1)"
    assert "row count estimates" in joined(doc(explain=[{"plan": plan}]))


def hit(cid: str) -> Hit:
    return Hit(cid, "docs", cid, "u", [], "x" * 3000, score=0.0, rank=1)


def test_merge_round_robin_dedup_and_trim():
    out = merge([("q1", [hit("a"), hit("b")]), ("q2", [hit("a"), hit("c")])], k=10)
    assert [(r.chunk_id, r.query, r.rank) for r in out] == [
        ("a", "q1", 1),
        ("b", "q1", 2),
        ("c", "q2", 2),
    ]
    assert len(out[0].text) == 1500
    assert len(merge([("q", [hit(str(i)) for i in range(20)])], k=5)) == 5


@pytest.mark.data
@pytest.mark.slow
def test_real_cases_retrieve_setting_chunk(capsys):
    from pgdiag.index import store
    from pgdiag.retrieve.cli import evaluate

    cfg = get_config()
    missing = [
        s
        for i, s in enumerate(CASE_SETTINGS, 1)
        if not (cfg.results_dir / "symptoms" / f"c{i:02d}_{s}.json").exists()
    ]
    try:
        if missing:
            raise store.IndexNotBuilt(
                f"symptom documents missing for {missing} (pgdiag-collect all)"
            )
        found = evaluate(k=12)
    except store.IndexNotBuilt as exc:
        if os.environ.get("PGDIAG_REQUIRE_DATA") == "1":
            pytest.fail(str(exc))
        pytest.skip(str(exc))
    print(capsys.readouterr().out)
    assert found >= 4
