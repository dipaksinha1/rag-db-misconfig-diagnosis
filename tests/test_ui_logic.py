"""The UI's plain-Python helpers (pgdiag.ui.logic); no browser, no database."""

from __future__ import annotations

import pytest

from pgdiag.faults.cases import get_case
from pgdiag.ui import logic


def test_case_for_returns_the_experiment_case_for_its_bad_value():
    assert logic.case_for("work_mem", " 64KB ", "8MB", False) == get_case("c02_work_mem")


def test_case_for_makes_a_custom_case_otherwise():
    case = logic.case_for("work_mem", "128kB", "8MB", False)
    assert case.case_id == "c99_work_mem"
    assert (case.setting, case.bad_value, case.good_value) == ("work_mem", "128kB", "8MB")
    assert not case.restart_required


def test_case_from_state_rebuilds_custom_and_real_cases():
    rec = {
        "case_id": "c99_shared_buffers",
        "setting": "shared_buffers",
        "bad_value": "2MB",
        "good_value": "64MB",
        "restart_required": True,
        "needs_analyze": False,
        "status": "applied",
    }
    case = logic.case_from_state(rec)
    assert (case.case_id, case.bad_value, case.restart_required) == (rec["case_id"], "2MB", True)
    real = logic.case_from_state({**rec, "case_id": "c01_shared_buffers"})
    assert real == get_case("c01_shared_buffers")
    assert logic.case_from_state({**rec, "status": "applying"}) is None
    assert logic.case_from_state(None) is None


def _diag(*fixes):
    return {
        "fixes": [
            {"rank": i, "setting": s, "proposed_value": v} for i, (s, v) in enumerate(fixes, 1)
        ]
    }


def test_merge_suggestions_dedupes_and_keeps_every_source():
    merged = logic.merge_suggestions(
        {
            "rag": _diag(("work_mem", "8MB"), ("random_page_cost", "1.1")),
            "norag": _diag(("work_mem", " 8mb"), ("shared_buffers", "64MB")),
        },
        [{"setting": "work_mem", "proposed_value": "16MB", "source": "you"}],
    )
    assert [(m["rank"], m["setting"], m["proposed_value"]) for m in merged] == [
        (1, "work_mem", "8MB"),
        (2, "random_page_cost", "1.1"),
        (3, "shared_buffers", "64MB"),
        (4, "work_mem", "16MB"),
    ]
    assert merged[0]["sources"] == ["rag #1", "norag #1"]
    assert merged[3]["sources"] == ["you"]


def test_merge_suggestions_uses_the_given_key():
    merged = logic.merge_suggestions(
        {"rag": _diag(("work_mem", "8MB")), "norag": _diag(("work_mem", "8192kB"))},
        key=lambda s, v: f"{s}=8192" if str(v).lower() in ("8mb", "8192kb") else f"{s}={v}",
    )
    assert len(merged) == 1 and merged[0]["sources"] == ["rag #1", "norag #1"]


def _fixtest():
    fix = {
        "rank": 1,
        "setting": "work_mem",
        "proposed_value": "8MB",
        "status": "applied",
        "median_s": 7.1,
        "vs_ref_pct": 1.4,
        "recovery_pct": 98.0,
        "within_10pct": True,
        "error": None,
    }
    rejected = {
        **fix,
        "rank": 2,
        "proposed_value": "lots",
        "status": "rejected",
        "median_s": None,
        "vs_ref_pct": None,
        "recovery_pct": None,
        "within_10pct": False,
        "error": "invalid: bad value",
    }
    return {
        "setting": "work_mem",
        "bad_value": "64kB",
        "ref_s": 7.0,
        "broken": {"median_s": 12.0},
        "broken_vs_ref_pct": 71.4,
        "fixes": [fix, rejected],
        "best": {"rank": 1},
    }


def test_result_rows():
    rows = logic.result_rows(_fixtest(), [{"rank": 1, "sources": ["rag #1", "control"]}])
    assert [r["label"] for r in rows] == [
        "healthy (baseline)",
        "broken (work_mem = 64kB)",
        "#1 work_mem = 8MB",
        "#2 work_mem = lots",
    ]
    assert rows[2]["kind"] == "best fix" and rows[2]["status"] == "applied (within 10%)"
    assert rows[2]["suggested_by"] == "rag #1, control"
    assert rows[3]["median_s"] is None and rows[3]["error"].startswith("invalid")


def test_sessions_round_trip(tmp_path):
    d = logic.new_session_dir(tmp_path, get_case("c02_work_mem"))
    assert d.parent == tmp_path / "ui" and d.name.endswith("_c02_work_mem")
    assert logic.list_sessions(tmp_path) == []  # no fix test yet
    logic.write_json(d / "fixtest.json", _fixtest())
    logic.write_json(d / "suggestions.json", [{"rank": 1, "sources": ["you"]}])
    (sess,) = logic.list_sessions(tmp_path)
    assert sess["name"] == d.name and sess["fixtest"]["ref_s"] == 7.0


def test_db_lock_allows_one_holder(tmp_path):
    path = tmp_path / "db.lock"
    with logic.db_lock(path):
        with pytest.raises(logic.Busy):
            with logic.db_lock(path):
                pass
    with logic.db_lock(path):  # released again
        pass
