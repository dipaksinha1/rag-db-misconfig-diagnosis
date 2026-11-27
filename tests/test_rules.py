"""Rules-based baseline diagnosis: offline tests on synthetic symptom documents."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pgdiag.config import get_config
from pgdiag.diagnose import rules
from pgdiag.faults.cases import baseline_config_values, load_cases

BASE_CONFIG = {
    "shared_buffers": "64MB", "effective_cache_size": "160MB", "work_mem": "8MB",
    "hash_mem_multiplier": "2", "maintenance_work_mem": "32MB", "max_connections": "100",
    "random_page_cost": "1.1", "seq_page_cost": "1", "cpu_tuple_cost": "0.01",
    "cpu_index_tuple_cost": "0.005", "cpu_operator_cost": "0.0025",
    "effective_io_concurrency": "1", "max_parallel_workers_per_gather": "2",
    "parallel_setup_cost": "1000", "parallel_tuple_cost": "0.1", "jit": "on",
    "jit_above_cost": "100000", "default_statistics_target": "100", "enable_seqscan": "on",
    "join_collapse_limit": "8", "max_wal_size": "1GB", "checkpoint_completion_target": "0.9",
    "autovacuum": "on",
}  # fmt: skip


def doc(config=None, **over):
    d = {
        "environment": {"memory_limit_mb": 256, "cpu_count": 16},
        "config": [{"name": k, "value": v} for k, v in {**BASE_CONFIG, **(config or {})}.items()],
        "database_stats": {"hit_pct": 99.0, "temp_files": 2, "temp_bytes": 1000},
        "top_statements": [{"total_ms": 100.0, "jit_ms": 1.0}],
        "explain": [{"query": "q", "plan": "Gather (...)\n  Workers Launched: 2\n  Seq Scan"}],
    }
    d.update(over)
    return d


def test_healthy_document_has_no_strong_finding():
    fixes = rules.rank(doc())
    assert len(fixes) == 4 and [f["rank"] for f in fixes] == [1, 2, 3, 4]
    assert len({f["setting"] for f in fixes}) == 4
    assert all(float(f["reasoning"].split()[1].rstrip(":")) < 0.5 for f in fixes)


@pytest.mark.parametrize("case", load_cases(), ids=lambda c: c.case_id)
def test_each_bad_value_is_ranked_first_from_the_config(case):
    fixes = rules.rank(doc({case.setting: case.bad_value}))
    assert fixes[0]["setting"] == case.setting


def test_values_are_generic_recommendations():
    by = {f["setting"]: f["proposed_value"] for f in rules.rank(doc({"shared_buffers": "1MB"}))}
    assert by["shared_buffers"] == "64MB"  # 25% of the 256 MB memory limit
    fixes = rules.rank(doc({"max_parallel_workers_per_gather": "0"}))
    assert fixes[0]["proposed_value"] == "4"  # min(4, cpus / 2)
    fixes = rules.rank(
        doc({"max_parallel_workers_per_gather": "0"}, environment={"memory_limit_mb": 256})
    )
    assert fixes[0]["setting"] != "max_parallel_workers_per_gather"  # 1 CPU: not flagged


def test_symptoms_alone_point_at_settings():
    temp = doc(database_stats={"hit_pct": 99.0, "temp_files": 5000, "temp_bytes": 500 * 2**20})
    assert rules.rank(temp)[0]["setting"] == "work_mem"
    jit = doc(top_statements=[{"total_ms": 100.0, "jit_ms": 60.0}])
    assert rules.rank(jit)[0]["setting"] == "jit_above_cost"
    serial = doc(explain=[{"query": "q", "plan": "Finalize Aggregate\n  Seq Scan on t"}])
    assert rules.rank(serial)[0]["setting"] == "max_parallel_workers_per_gather"
    est = doc(
        explain=[
            {
                "query": "q",
                "plan": "Workers Launched: 2\nHash Join (cost=1..2 rows=10 width=8) "
                "(actual time=0.1..9.0 rows=5000 loops=1)",
            }
        ]
    )
    assert rules.rank(est)[0]["setting"] == "default_statistics_target"
    cache = doc(database_stats={"hit_pct": 50.0, "temp_files": 0, "temp_bytes": 0})
    assert rules.rank(cache)[0]["setting"] == "shared_buffers"


def test_two_bad_settings_both_in_the_four():
    fixes = rules.rank(doc({"random_page_cost": "100", "max_parallel_workers_per_gather": "0"}))
    top2 = {f["setting"] for f in fixes[:2]}
    assert top2 == {"random_page_cost", "max_parallel_workers_per_gather"}


def test_record_shape_matches_llm_modes():
    rec = rules.diagnose(doc())
    assert rec["mode"] == "rules" and rec["model"] == rules.RULESET
    assert rec["attempts"] == 1 and rec["usage"] == {"input_tokens": 0, "output_tokens": 0}
    for f in rec["fixes"]:
        assert set(f) == {"rank", "setting", "proposed_value", "reasoning", "cited_chunks"}


def test_does_not_mutate_the_document():
    d = doc({"work_mem": "64kB"})
    before = copy.deepcopy(d)
    rules.diagnose(d)
    assert d == before


def test_base_config_matches_the_repo_baseline():
    base = baseline_config_values()
    assert {k: BASE_CONFIG[k] for k in base if k in BASE_CONFIG} == {
        k: v for k, v in base.items() if k in BASE_CONFIG
    }


@pytest.mark.data
def test_saved_symptom_documents():
    sym = get_config().results_dir / "symptoms"
    if not (sym / "baseline.json").exists():
        pytest.skip("symptom documents missing; run `pgdiag-collect all`")
    for case in load_cases():
        d = json.loads(Path(sym / f"{case.case_id}.json").read_text())
        assert rules.rank(d)[0]["setting"] == case.setting
