"""T11 experiment runner: offline tests with fake steps (no database, no API)."""

from __future__ import annotations

import json

import pytest

from pgdiag.diagnose.core import DiagnosisError
from pgdiag.experiment import cli, core
from pgdiag.faults.cases import get_case

C02 = get_case("c02_work_mem")
C05 = get_case("c05_jit_above_cost")


def fix(rank, setting, value):
    return {
        "rank": rank,
        "setting": setting,
        "proposed_value": value,
        "reasoning": f"{setting} looks wrong",
        "cited_chunks": [],
    }


DEFAULT_ANSWERS = {
    "rag": [
        fix(1, "work_mem", "8MB"),
        fix(2, "jit", "off"),
        fix(3, "shared_buffers", "64MB"),
        fix(4, "random_page_cost", "1.1"),
    ],
    "norag": [
        fix(1, "shared_buffers", "64MB"),
        fix(2, "work_mem", "8192kB"),  # same as 8MB once normalised
        fix(3, "jit", "off"),
        fix(4, "effective_cache_size", "128MB"),
    ],
}


class FakeSteps:
    def __init__(self, answers=None, fail_modes=(), leak=False, model="m-1"):
        self.answers = answers or DEFAULT_ANSWERS
        self.fail_modes = set(fail_modes)
        self.leak = leak
        self._model = model
        self.calls: list[tuple] = []
        self.applied: str | None = None

    def model(self):
        return self._model

    def check_ready(self):
        return []

    def apply_case(self, case):
        self.calls.append(("apply", case.case_id))
        self.applied = case.case_id

    def reset(self):
        self.calls.append(("reset",))
        self.applied = None

    def verify(self, case):
        want = case.case_id if case else None
        return [] if self.applied == want else [f"expected {want}, running {self.applied}"]

    def collect(self):
        self.calls.append(("collect", self.applied))
        return {"database_stats": {"temp_files": 1}, "running": self.applied}

    def leak_problems(self, doc):
        return ["forbidden text"] if self.leak else []

    def retrieve(self, doc, k):
        self.calls.append(("retrieve", k))
        return [{"chunk_id": "docs:a", "source": "docs", "title": "A", "text": "t", "rank": 1}]

    def diagnose(self, doc, mode, chunks):
        self.calls.append(("diagnose", mode, doc["running"], chunks is not None))
        if mode in self.fail_modes:
            raise DiagnosisError("no valid answer")
        return {
            "mode": mode,
            "model": self._model,
            "fixes": self.answers[mode],
            "attempts": 1,
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "chunk_ids": [c["chunk_id"] for c in chunks or []],
            "dropped_citations": [],
        }

    def fix_key(self, setting, value):
        v = {"8192kB": "8MB"}.get(value, value)
        return f"{setting}={v.lower()}"

    def test_fixes(self, case, fixes, preset):
        self.calls.append(
            ("test_fixes", case.case_id, [(f["setting"], f["proposed_value"]) for f in fixes])
        )
        assert self.applied == case.case_id
        good = {"work_mem": "8MB"}
        recs = []
        for f in fixes:
            ok = good.get(f["setting"]) == f["proposed_value"]
            rejected = f["setting"] == "effective_cache_size"
            recs.append(
                {
                    "rank": f["rank"],
                    "setting": f["setting"],
                    "proposed_value": f["proposed_value"],
                    "status": "rejected" if rejected else "applied",
                    "method": None if rejected else "reload",
                    "median_s": None if rejected else (7.0 if ok else 14.0),
                    "vs_ref_pct": None if rejected else (1.0 if ok else 100.0),
                    "recovery_pct": None if rejected else (99.0 if ok else 0.0),
                    "within_10pct": ok,
                    "error": "invalid: nope" if rejected else None,
                    "bench": {"big": True},
                }
            )
        self.applied = None  # test_fixes ends at the baseline
        return {"case_id": case.case_id, "ref_s": 7.0, "broken": {"median_s": 14.0}, "fixes": recs}


def make(tmp_path, steps, **kw):
    kw.setdefault("cases", [C02])
    kw.setdefault("repeats", 1)
    return core.Experiment(tmp_path / "exp", steps=steps, retrieval_k=12, **kw)


def rows(exp):
    return core.load_rows(exp.rows_path)


def test_one_unit_protocol_and_rows(tmp_path):
    steps = FakeSteps()
    exp = make(tmp_path, steps)
    summary = exp.run()
    assert summary == {"ran": 1, "finished": 1, "incomplete": []}
    kinds = [c[0] for c in steps.calls]
    # collect once on the broken server, before any diagnosis; fix test last
    assert kinds[:2] == ["apply", "collect"] and steps.calls[1] == ("collect", "c02_work_mem")
    assert kinds.count("collect") == 1 and kinds.count("test_fixes") == 1
    assert kinds.index("test_fixes") > max(i for i, k in enumerate(kinds) if k == "diagnose")
    # both modes saw the same document; only rag retrieved
    assert {c[1:] for c in steps.calls if c[0] == "diagnose"} == {
        ("rag", "c02_work_mem", True),
        ("norag", "c02_work_mem", False),
    }
    assert kinds.count("retrieve") == 1
    # union of fixes, de-duplicated by key (8MB == 8192kB, jit and shared_buffers shared)
    tested = next(c for c in steps.calls if c[0] == "test_fixes")[2]
    assert len(tested) == 5
    out = {r["mode"]: r for r in rows(exp)}
    assert set(out) == {"rag", "norag"}
    assert out["rag"]["score"]["top1_correct"] and out["rag"]["score"]["top1_works"]
    assert out["norag"]["score"]["correct_rank"] == 2
    assert out["norag"]["score"]["first_working_rank"] == 2  # 8192kB reused 8MB's result
    assert out["norag"]["score"]["n_rejected"] == 1
    assert out["norag"]["fixes"][1]["outcome"]["within_10pct"] is True
    assert "bench" not in json.dumps(out["rag"]["fixes"][0]["outcome"])
    udir = exp.unit_dir(C02, 1)
    for name in ("symptoms.json", "diagnosis.rag.json", "diagnosis.norag.json", "fixtest.json"):
        assert (udir / name).exists()
    assert json.loads((udir / "diagnosis.rag.json").read_text())["retrieved"][0]["chunk_id"]
    manifest = json.loads((exp.out_dir / "manifest.json").read_text())
    assert manifest["model"] == "m-1" and manifest["cases"] == ["c02_work_mem"]


def test_cache_skips_measured_fixes_across_repeats(tmp_path):
    steps = FakeSteps()
    exp = make(tmp_path, steps, repeats=2)
    exp.run()
    tests = [c for c in steps.calls if c[0] == "test_fixes"]
    assert len(tests) == 1  # repeat 2 proposed the same fixes: nothing new to measure
    assert steps.calls.count(("collect", "c02_work_mem")) == 2
    assert len(rows(exp)) == 4
    assert {r["repeat"] for r in rows(exp)} == {1, 2}


def test_resume_does_not_redo_work(tmp_path):
    steps = FakeSteps()
    exp = make(tmp_path, steps, cases=[C02, C05], repeats=1)
    assert exp.run(limit=1)["ran"] == 1
    assert len(rows(exp)) == 2
    steps2 = FakeSteps()
    exp2 = make(tmp_path, steps2, cases=[C02, C05], repeats=1)
    assert exp2.run()["ran"] == 1
    assert ("collect", "c02_work_mem") not in steps2.calls
    assert ("collect", "c05_jit_above_cost") in steps2.calls
    assert len(rows(exp2)) == 4
    # a third run has nothing to do
    steps3 = FakeSteps()
    assert make(tmp_path, steps3, cases=[C02, C05]).run()["ran"] == 0
    assert not [c for c in steps3.calls if c[0] in ("collect", "diagnose", "test_fixes")]


def test_resume_after_interrupt_reuses_files(tmp_path):
    steps = FakeSteps()
    exp = make(tmp_path, steps)

    def boom(case, fixes, preset):
        raise KeyboardInterrupt

    steps.test_fixes = boom
    with pytest.raises(KeyboardInterrupt):
        exp.run()
    assert ("reset",) in steps.calls  # left the server at baseline
    assert rows(exp) == []
    steps2 = FakeSteps()
    make(tmp_path, steps2).run()
    kinds = [c[0] for c in steps2.calls]
    assert "collect" not in kinds and "diagnose" not in kinds  # files reused
    assert kinds.count("test_fixes") == 1 and len(rows(exp)) == 2


def test_diagnosis_error_leaves_unit_incomplete_and_retries(tmp_path):
    steps = FakeSteps(fail_modes={"rag"})
    exp = make(tmp_path, steps)
    summary = exp.run()
    assert summary["incomplete"] == ["c02_work_mem r1"]
    assert [r["mode"] for r in rows(exp)] == ["norag"]
    udir = exp.unit_dir(C02, 1)
    assert (udir / "diagnosis.rag.error.json").exists()
    assert "pending diagnosis errors: 1" in core.progress_table(exp.out_dir)
    steps2 = FakeSteps()
    assert make(tmp_path, steps2).run()["incomplete"] == []
    assert [c[1] for c in steps2.calls if c[0] == "diagnose"] == ["rag"]
    assert not (udir / "diagnosis.rag.error.json").exists()
    assert sorted(r["mode"] for r in rows(exp)) == ["norag", "rag"]
    # rag's new fix (random_page_cost) measured in a second fix-test file
    assert (udir / "fixtest.2.json").exists()
    rag = next(r for r in rows(exp) if r["mode"] == "rag")
    assert rag["fixes"][3]["outcome"]["source"].endswith("fixtest.2.json")


def test_leak_aborts_before_diagnosis(tmp_path):
    steps = FakeSteps(leak=True)
    with pytest.raises(core.ExperimentError, match="leaks"):
        make(tmp_path, steps).run()
    assert "diagnose" not in [c[0] for c in steps.calls]
    assert not (make(tmp_path, steps).unit_dir(C02, 1) / "symptoms.json").exists()


def test_manifest_mismatch_refused(tmp_path):
    make(tmp_path, FakeSteps(model="m-1")).run()
    with pytest.raises(core.ExperimentError, match="model"):
        make(tmp_path, FakeSteps(model="m-2")).run()


def test_not_ready_fails_before_any_work(tmp_path):
    steps = FakeSteps()
    steps.check_ready = lambda: ["ANTHROPIC_API_KEY is not set"]
    with pytest.raises(core.ExperimentError, match="ANTHROPIC_API_KEY"):
        make(tmp_path, steps).run()
    assert steps.calls == []


def test_mode_order_alternates(tmp_path):
    steps = FakeSteps()
    make(tmp_path, steps, repeats=2).run()
    order = [c[1] for c in steps.calls if c[0] == "diagnose"]
    assert order == ["rag", "norag", "norag", "rag"]


def test_units_are_repeat_major():
    exp = core.Experiment(core.Path("/nonexistent"), cases=[C02, C05], repeats=2, steps=FakeSteps())
    assert [(r, c.case_id) for r, c in exp.units()] == [
        (1, "c02_work_mem"),
        (1, "c05_jit_above_cost"),
        (2, "c02_work_mem"),
        (2, "c05_jit_above_cost"),
    ]


def test_score():
    outcomes = [{"status": "applied", "median_s": 9.0, "vs_ref_pct": 30.0, "within_10pct": False},
                {"status": "rejected"},
                {"status": "applied", "median_s": 7.0, "vs_ref_pct": 2.0, "within_10pct": True},
                {"status": "failed"}]  # fmt: skip
    fixes = [fix(1, "jit", "off"), fix(2, "x", "1"), fix(3, "Work_Mem", "8MB"), fix(4, "y", "2")]
    s = core.score(C02, fixes, outcomes)
    assert s["correct_rank"] == 3 and not s["top1_correct"] and s["correct_in_top4"]
    assert s["first_working_rank"] == 3 and s["any_works"] and not s["top1_works"]
    assert s["n_rejected"] == 1 and s["n_failed"] == 1 and s["best_vs_ref_pct"] == 2.0


def test_load_rows_ignores_truncated_line(tmp_path):
    p = tmp_path / "r.jsonl"
    p.write_text('{"a": 1}\n{"a": 2')
    assert core.load_rows(p) == [{"a": 1}]


def test_text_key():
    assert core.text_key(" Work_Mem ", "'8MB';") == "work_mem=8mb"


def test_cli_status_empty(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PGDIAG_ROOT", str(tmp_path))
    from pgdiag.config import get_config

    get_config.cache_clear()
    try:
        assert cli.main(["status", "--name", "nothing"]) == 0
    finally:
        get_config.cache_clear()
    assert "no results" in capsys.readouterr().out
