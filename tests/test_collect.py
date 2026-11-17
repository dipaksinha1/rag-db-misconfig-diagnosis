"""T7 symptom collector: log filtering, scrubbing and leak checks (offline), plus a data test."""

from __future__ import annotations

import copy
import json
import os

import pytest

from pgdiag.collect import leaks, symptoms
from pgdiag.config import get_config
from pgdiag.faults.cases import load_cases
from pgdiag.settings import TARGET_SETTINGS


def _doc(**config) -> dict:
    values = {name: "1" for name in symptoms.CONFIG_PARAMS} | config
    return {
        "schema_version": 1,
        "config": [{"name": n, "value": values[n]} for n in symptoms.CONFIG_PARAMS],
        "log_summary": {"errors": []},
    }


def test_config_list_covers_target_settings():
    assert set(TARGET_SETTINGS) <= set(symptoms.CONFIG_PARAMS)
    assert len(set(symptoms.CONFIG_PARAMS)) == len(symptoms.CONFIG_PARAMS)


def test_strip_sql_removes_comments_and_truncates():
    text = "-- id: x\n-- sensitive_to: work_mem\nSELECT 1 /* hint */\n  FROM t -- tail"
    assert symptoms.strip_sql(text) == "SELECT 1 FROM t"
    assert len(symptoms.strip_sql("SELECT " + "x, " * 200, limit=50)) == 50


def _rec(msg, **kw):
    return {"message": msg, "error_severity": "LOG", **kw}


def test_log_summary_drops_config_and_restart_lines():
    records = [
        _rec("received SIGHUP, reloading configuration files"),
        _rec('parameter "work_mem" changed to "64kB"'),
        _rec("database system is ready to accept connections"),
        _rec("received fast shutdown request"),
        _rec("statement: ALTER SYSTEM SET work_mem = '64kB'", error_severity="ERROR"),
        _rec("duration: 12.5 ms  statement: ANALYZE x", application_name="pgdiag-admin"),
        _rec("duration: 300.5 ms  statement: SELECT 1 FROM t WHERE id = 42"),
        _rec("duration: 400.0 ms  statement: SELECT 1 FROM t WHERE id = 7"),
        _rec('temporary file: path "base/pgsql_tmp/x", size 1024', statement="SELECT 2"),
        _rec("checkpoint starting: immediate force wait"),
        _rec('automatic vacuum of table "pgdiag.public.t": index scans: 0'),
        _rec("canceling statement due to statement timeout", error_severity="ERROR"),
    ]
    s = symptoms.summarize_logs(records)
    text = json.dumps(s)
    for bad in ("SIGHUP", "changed to", "ready to accept", "shutdown", "ALTER SYSTEM", "ANALYZE"):
        assert bad not in text
    assert s["slow_statements"] == [
        {"statement": "SELECT N FROM t WHERE id = N", "count": 2, "max_ms": 400, "total_ms": 700}
    ]
    assert s["temp_files"]["count"] == 1 and s["temp_files"]["total_bytes"] == 1024
    assert s["checkpoints"] == {"immediate force wait": 1}
    assert s["autovacuum"] == {"vacuum pgdiag.public.t": 1}
    assert s["errors"][0]["count"] == 1 and "timeout" in s["errors"][0]["message"]


def test_clean_document_passes():
    assert leaks.check_text(_doc()) == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["config"][0].update(source="configuration file"),
        lambda d: d["config"][0].update(boot_val="16384"),
        lambda d: d.update(case_id="x"),
        lambda d: d["log_summary"]["errors"].append({"message": "received SIGHUP"}),
        lambda d: d["log_summary"]["errors"].append({"message": 'parameter "x" changed to "y"'}),
        lambda d: d["log_summary"]["errors"].append({"message": "ALTER SYSTEM SET x = 1"}),
        lambda d: d.update(note="see results/bench/x.json"),
        lambda d: d.update(note="data/faults/state.json"),
        lambda d: d.update(note="label t6-broken-c02_work_mem"),
        lambda d: d.update(note=load_cases()[3].case_id),
    ],
)
def test_leak_checker_catches_leaks(mutate):
    doc = _doc()
    mutate(doc)
    assert leaks.check_text(doc)


def test_cross_document_check():
    base = _doc()
    docs = {c.case_id: _doc(**{c.setting: "2"}) for c in load_cases()}
    assert leaks.check_set(docs, base) == []

    bad = copy.deepcopy(docs)
    first = load_cases()[0]
    bad[first.case_id]["config"][-1]["value"] = "9"  # a second setting differs
    bad[load_cases()[1].case_id]["config"].reverse()  # different order
    bad[load_cases()[2].case_id]["extra"] = {}  # different sections
    assert len(leaks.check_set(bad, base)) == 3


@pytest.mark.data
def test_real_symptom_documents_do_not_leak():
    folder = get_config().results_dir / "symptoms"
    names = ["baseline", *(c.case_id for c in load_cases())]
    missing = [n for n in names if not (folder / f"{n}.json").exists()]
    if missing:
        reason = f"symptom documents missing: {missing}; run `pgdiag-collect all`"
        if os.environ.get("PGDIAG_REQUIRE_DATA") == "1":
            pytest.fail(reason)
        pytest.skip(reason)
    docs = {n: json.loads((folder / f"{n}.json").read_text()) for n in names}
    for name, doc in docs.items():
        assert leaks.check_text(doc) == [], name
        assert len(json.dumps(doc)) < 40_000, name
    cases = {n: d for n, d in docs.items() if n != "baseline"}
    assert leaks.check_set(cases, docs["baseline"]) == []
