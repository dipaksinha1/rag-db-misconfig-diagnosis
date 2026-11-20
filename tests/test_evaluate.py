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
