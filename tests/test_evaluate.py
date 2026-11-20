"""T12 evaluation: statistics, metrics and report on synthetic experiment output (offline)."""

from __future__ import annotations

import json

import pytest

from pgdiag.evaluate import cli, core
from pgdiag.experiment import core as exp_core
from tests.test_experiment import C02, C05, FakeSteps


def test_mcnemar_exact():
    assert core.mcnemar_exact(0, 0) == 1.0
    assert core.mcnemar_exact(3, 3) == 1.0
    # 6 vs 0 discordant: 2 * 0.5**6
    assert core.mcnemar_exact(6, 0) == pytest.approx(2 / 64)
    assert core.mcnemar_exact(1, 5) == pytest.approx(2 * (1 + 6) / 64)


def test_wilson():
    assert core.wilson(0, 0) is None
    lo, hi = core.wilson(5, 10)
    assert lo == pytest.approx(0.2366, abs=1e-3) and hi == pytest.approx(0.7634, abs=1e-3)
    assert core.wilson(10, 10)[1] == 1.0


def test_bootstrap_deterministic():
    d = [0.0, 1.0, 0.5, -0.5, 1.0]
    assert core.bootstrap_mean_ci(d, n=500) == core.bootstrap_mean_ci(d, n=500)
    lo, hi = core.bootstrap_mean_ci([0.25] * 4, n=200)
    assert lo == hi == 0.25
    assert core.bootstrap_mean_ci([]) is None


def score(rank, works_rank=None):
    return {
        "correct_rank": rank,
        "top1_correct": rank == 1,
        "correct_in_top4": rank is not None,
        "first_working_rank": works_rank,
        "top1_works": works_rank == 1,
        "any_works": works_rank is not None,
        "n_rejected": 0,
        "n_failed": 0,
        "best_vs_ref_pct": None,
    }


def write_exp(tmp_path, rows, chunk_ids=("docs:wm", "docs:other")):
    exp = tmp_path / "exp"
    for r in rows:
        r.setdefault("diagnosis", f"runs/{r['case_id']}/r{r['repeat']}/diagnosis.{r['mode']}.json")
        p = exp / r["diagnosis"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"chunk_ids": list(chunk_ids) if r["mode"] == "rag" else []}))
    (exp / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    chunks = tmp_path / "chunks"
    chunks.mkdir()
    (chunks / "docs.jsonl").write_text(
        json.dumps({"chunk_id": "docs:wm", "settings": ["work_mem"], "text": "x"})
        + "\n"
        + json.dumps({"chunk_id": "docs:other", "settings": ["jit"], "text": "y"})
        + "\n"
    )
    return exp, chunks


def row(case, mode, repeat, rank, works=None, cites=()):
    names = ["jit", "shared_buffers", "random_page_cost", "effective_cache_size"]
    fixes = []
    for i in range(1, 5):
        setting = "work_mem" if i == rank else names[i - 1]
        fixes.append(
            {
                "rank": i,
                "setting": setting,
                "proposed_value": "8MB",
                "cited_chunks": list(cites) if setting == "work_mem" else [],
                "outcome": {"status": "applied", "within_10pct": i == works},
            }
        )
    return {
        "case_id": case,
        "mode": mode,
        "repeat": repeat,
        "fixes": fixes,
        "score": score(rank, works),
        "attempts": 1,
        "usage": {"input_tokens": 100, "output_tokens": 10},
    }


def test_metrics_paired_and_grounding(tmp_path):
    rows = [
        row("c02_work_mem", "rag", 1, 1, works=1, cites=["docs:wm"]),
        row("c02_work_mem", "norag", 1, 3, works=3),
        row("c02_work_mem", "rag", 2, 1, works=1, cites=["docs:other"]),
        row("c02_work_mem", "norag", 2, 1, works=1),
        row("c02_work_mem", "rag", 3, 2),
        row("c02_work_mem", "norag", 3, None),
    ]
    exp, chunks = write_exp(tmp_path, rows)
    ev = core.evaluate(exp, chunks)
    assert ev["grounding_available"]
    rag, norag = ev["overall"]["rag"], ev["overall"]["norag"]
    assert (rag["top1_correct"]["k"], rag["top1_correct"]["n"]) == (2, 3)
    assert rag["mrr"] == pytest.approx((1 + 1 + 0.5) / 3)
    assert norag["mrr"] == pytest.approx((1 / 3 + 1 + 0) / 3)
    assert norag["correct_in_top4"]["k"] == 2
    # correct_value_works only counts runs that named the setting
    assert (rag["correct_value_works"]["k"], rag["correct_value_works"]["n"]) == (2, 3)
    assert rag["retrieved_correct"]["k"] == 3
    assert (rag["correct_fix_grounded"]["k"], rag["correct_fix_grounded"]["n"]) == (1, 3)
    assert "retrieved_correct" not in norag
    p = ev["paired"]
    assert p["n_pairs"] == 3
    assert p["top1_correct"] == {
        "both": 1, "rag_only": 1, "norag_only": 0, "neither": 1, "p_mcnemar": 1.0,
    }  # fmt: skip
    assert p["rr_diff"]["mean"] == pytest.approx(((1 - 1 / 3) + 0 + 0.5) / 3)
    text = core.report(ev)
    assert "## Paired comparison (3 pairs" in text
    assert "| top-1 correct | 2/3 (67%" in text
    assert "## Retrieval and grounding" in text


def test_no_chunks_skips_grounding(tmp_path):
    exp, _ = write_exp(tmp_path, [row("c02_work_mem", "rag", 1, 1)])
    ev = core.evaluate(exp, tmp_path / "missing")
    assert not ev["grounding_available"]
    assert "Grounding metrics skipped" in core.report(ev)
    assert ev["paired"]["n_pairs"] == 0


def test_duplicate_rows_last_wins(tmp_path):
    exp, chunks = write_exp(
        tmp_path, [row("c02_work_mem", "rag", 1, None), row("c02_work_mem", "rag", 1, 1)]
    )
    ev = core.evaluate(exp, chunks)
    assert ev["n_rows"] == 1 and ev["overall"]["rag"]["top1_correct"]["k"] == 1


def test_empty_experiment_errors(tmp_path):
    (tmp_path / "exp").mkdir()
    with pytest.raises(ValueError, match="no rows"):
        core.evaluate(tmp_path / "exp")


def test_end_to_end_with_t11_runner(tmp_path, monkeypatch, capsys):
    """T11 (fake steps) -> results.jsonl -> pgdiag-evaluate CLI -> report.md + metrics.json."""
    monkeypatch.setenv("PGDIAG_ROOT", str(tmp_path))
    from pgdiag.config import get_config

    get_config.cache_clear()
    try:
        exp_dir = get_config().results_dir / "experiment" / "main"
        exp_core.Experiment(
            exp_dir, cases=[C02, C05], repeats=2, steps=FakeSteps(), retrieval_k=12
        ).run()
        assert cli.main(["--no-grounding"]) == 0
        out = get_config().results_dir / "evaluation" / "main"
    finally:
        get_config.cache_clear()
    metrics = json.loads((out / "metrics.json").read_text())
    assert metrics["n_rows"] == 8 and metrics["paired"]["n_pairs"] == 4
    # the fake always answers work_mem first (rag) / second (norag)
    assert metrics["per_case"]["c02_work_mem"]["rag"]["top1_correct"]["k"] == 2
    assert metrics["per_case"]["c05_jit_above_cost"]["rag"]["correct_in_top4"]["k"] == 0
    assert metrics["paired"]["top1_correct"]["rag_only"] == 2
    report = (out / "report.md").read_text()
    assert report.startswith("# RAG vs no-RAG") and "| c05_jit_above_cost | norag |" in report
    assert "wrote" in capsys.readouterr().out


# --- reasoning judge -------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from pgdiag.evaluate import judge  # noqa: E402


def grade(**over) -> str:
    g = {"evidence": 4, "accuracy": 4, "diagnosis": 5, "overall": 4, "unsupported_claims": 0,
         "comment": "ok"}  # fmt: skip
    g.update(over)
    return json.dumps(g)


class FakeJudgeClient:
    """Answers with ``texts`` in order; with ``by_mode`` it picks a grade from the prompt."""

    def __init__(self, *texts, by_marker=None):
        self.texts, self.by_marker = list(texts), by_marker or {}
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        prompt = kw["messages"][0]["content"]
        text = next((t for m, t in self.by_marker.items() if m in prompt), None)
        if text is None:
            text = self.texts.pop(0)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=50, output_tokens=5),
        )


def test_scrub_and_prompt_is_blind():
    fixes = [
        {"rank": 2, "setting": "jit", "proposed_value": "off", "reasoning": "JIT time high.",
         "cited_chunks": ["docs:jit#X"]},
        {"rank": 1, "setting": "work_mem", "proposed_value": "8MB",
         "reasoning": "35k temp files; see [docs:runtime-config-resource#GUC-WORK-MEM] and "
                      "relnotes:16.1#3.", "cited_chunks": ["docs:a"]},
    ]  # fmt: skip
    prompt = judge.build_prompt("c02_work_mem", {"database_stats": {"temp_files": 35000}}, fixes)
    assert "docs:" not in prompt and "relnotes:" not in prompt and "cited" not in prompt
    assert prompt.count("[reference]") == 2
    assert "`work_mem` was set to `64kB`" in prompt
    assert prompt.index("1. work_mem = 8MB") < prompt.index("2. jit = off")
    for leak in ("rag", "RAG", "norag", "c02_work_mem", "within_10pct", "Reference documentation"):
        assert leak not in prompt


@pytest.mark.parametrize(
    "text",
    [
        "nope",
        grade(evidence=0),
        grade(overall=6),
        grade(accuracy="4"),
        grade(diagnosis=True),
        grade(unsupported_claims=-1),
        json.dumps({"evidence": 3}),
    ],
)
def test_parse_scores_rejects(text):
    with pytest.raises(ValueError):
        judge.parse_scores(text)


def test_parse_scores_fenced():
    s = judge.parse_scores("```json\n" + grade(overall=2) + "\n```")
    assert s["overall"] == 2 and s["comment"] == "ok"


def test_judge_one_repairs_then_fails():
    client = FakeJudgeClient("bad", grade())
    res = judge.judge_one("p", client, "j-1")
    assert res["attempts"] == 2 and res["errors"] and res["scores"]["diagnosis"] == 5
    assert client.calls[1]["messages"][-1]["content"].startswith("That answer was invalid")
    with pytest.raises(judge.JudgeError):
        judge.judge_one("p", FakeJudgeClient("x", "y", "z"), "j-1")


def t11_experiment(tmp_path):
    exp_dir = tmp_path / "exp"
    exp_core.Experiment(
        exp_dir, cases=[C02, C05], repeats=2, steps=FakeSteps(), retrieval_k=12
    ).run()
    return exp_dir


def test_run_judge_caches_and_limits(tmp_path):
    exp_dir = t11_experiment(tmp_path)
    rows = core.load_rows(exp_dir)
    jdir = tmp_path / "judge"
    client = FakeJudgeClient(*[grade()] * 8)
    res = judge.run_judge(exp_dir, jdir, rows=rows, client=client, model="j-1", limit=3)
    assert res == {"graded": 3, "failed": [], "cached": 0}
    res = judge.run_judge(exp_dir, jdir, rows=rows, client=client, model="j-1")
    assert res == {"graded": 5, "failed": [], "cached": 3}
    assert len(client.calls) == 8
    assert judge.run_judge(exp_dir, jdir, rows=rows, client=None, model="j-1")["graded"] == 0
    rec = json.loads(next(jdir.glob("c02_work_mem.r1.rag.json")).read_text())
    assert rec["judge_model"] == "j-1" and "rag" not in rec["prompt"]["user"].lower().split()


def test_run_judge_failure_is_retried_later(tmp_path):
    exp_dir = t11_experiment(tmp_path)
    rows = core.load_rows(exp_dir)[:1]
    jdir = tmp_path / "judge"
    res = judge.run_judge(
        exp_dir, jdir, rows=rows, client=FakeJudgeClient("x", "y", "z"), model="j"
    )
    assert res["failed"] and not list(jdir.glob("*.json"))
    res = judge.run_judge(exp_dir, jdir, rows=rows, client=FakeJudgeClient(grade()), model="j")
    assert res["graded"] == 1


def test_evaluate_with_judge_scores(tmp_path):
    exp_dir = t11_experiment(tmp_path)
    rows = core.load_rows(exp_dir)
    jdir = tmp_path / "judge"
    # the fake RAG answer ranks work_mem first, the no-RAG one shared_buffers
    client = FakeJudgeClient(
        by_marker={"1. work_mem": grade(overall=5, evidence=5), "1. shared_buffers": grade(
            overall=2, evidence=3, unsupported_claims=2)}
    )  # fmt: skip
    judge.run_judge(exp_dir, jdir, rows=rows, client=client, model="j-1")
    ev = core.evaluate(exp_dir, None, jdir)
    j = ev["judge"]
    assert j["models"] == ["j-1"]
    assert j["overall"]["rag"]["overall"] == 5 and j["overall"]["norag"]["overall"] == 2
    assert j["paired"]["n_pairs"] == 4
    assert j["paired"]["overall"]["mean_diff"] == 3 and j["paired"]["overall"]["rag_higher"] == 4
    assert j["paired"]["overall"]["p_sign"] == pytest.approx(0.125)
    assert j["paired"]["unsupported_claims"]["mean_diff"] == -2
    text = core.report(ev)
    assert "## Reasoning quality (blinded judge)" in text and "Judge model: j-1" in text
    assert "| overall | +3.00 |" in text
    assert "blinding is partial" in text


def test_report_without_judge_mentions_flag(tmp_path):
    ev = core.evaluate(t11_experiment(tmp_path))
    text = core.report(ev)
    assert "Reasoning quality (blinded judge)" not in text and "--judge" in text
    assert ev["judge"]["paired"] is None


def test_cli_judge_end_to_end(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PGDIAG_ROOT", str(tmp_path))
    monkeypatch.setenv("PGDIAG_JUDGE_MODEL", "j-cli")
    from pgdiag.config import get_config
    from pgdiag.diagnose import core as dcore

    client = FakeJudgeClient(*[grade()] * 8)
    monkeypatch.setattr(dcore, "make_client", lambda: client)
    get_config.cache_clear()
    try:
        exp_dir = get_config().results_dir / "experiment" / "main"
        exp_core.Experiment(
            exp_dir, cases=[C02], repeats=1, steps=FakeSteps(), retrieval_k=12
        ).run()
        assert cli.main(["--judge", "--no-grounding"]) == 0
        out = get_config().results_dir / "evaluation" / "main"
    finally:
        get_config.cache_clear()
    assert len(client.calls) == 2 and client.calls[0]["model"] == "j-cli"
    assert len(list((out / "judge").glob("*.json"))) == 2
    assert "judge: 2 graded, 0 cached, 0 failed" in capsys.readouterr().out
    assert "Reasoning quality" in (out / "report.md").read_text()
