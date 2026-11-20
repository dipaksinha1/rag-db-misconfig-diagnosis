"""Evaluation and report (T12): RAG vs no-RAG from an experiment's ``results.jsonl`` (T11).

Metrics per mode (and per case x mode):

- **diagnosis**: top-1 accuracy (rank 1 names the broken setting), top-4 accuracy, MRR
  (1/rank of the broken setting, 0 when absent). Proportions get a Wilson 95% interval.
- **fix effect** (measured by T10): top-1 fix works (within 10% of the healthy reference), any
  of the 4 works, the correct setting's proposed value works (when it was named), share of
  proposals the fix tester rejected or that failed.
- **consistency**: number of distinct rank-1 settings over the repeats of a case.
- **cost**: diagnosis attempts and tokens.
- **RAG only** (needs ``data/chunks/``): the retrieved chunks mention the broken setting;
  top-1 accuracy with vs without such a chunk; the correct fix cites a chunk that mentions its
  setting ("grounded"); share of fixes with any citation.

Paired comparison: RAG and no-RAG runs of the same (case, repeat) share one symptom document
(T11), so they are compared as pairs: exact McNemar test on each yes/no metric and a
bootstrap 95% interval (resampling pairs, fixed seed) of the mean difference in reciprocal rank.
Everything here is deterministic and offline (no database, no API, no index).
"""

from __future__ import annotations

import json
import math
import random
import statistics
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

from pgdiag.faults.cases import get_case

MODES = ("rag", "norag")
PAIRED_BINARY = ("top1_correct", "correct_in_top4", "top1_works", "any_works")
BOOTSTRAP_N = 10_000
SEED = 0


# --- loading -----------------------------------------------------------------------------


def load_rows(exp_dir: Path) -> list[dict]:
    """Rows of results.jsonl, one per (case, mode, repeat) (the last one wins)."""
    from pgdiag.experiment.core import load_rows as _load

    rows = {}
    for r in _load(exp_dir / "results.jsonl"):
        rows[(r["case_id"], r["mode"], r["repeat"])] = r
    return sorted(rows.values(), key=lambda r: (r["case_id"], r["repeat"], r["mode"]))


def chunk_settings(ids: Iterable[str], chunks_dir: Path) -> dict[str, list[str]] | None:
    """chunk id -> settings it mentions, for the given ids; None if no chunk files exist."""
    want = set(ids)
    files = sorted(chunks_dir.glob("*.jsonl")) if chunks_dir.exists() else []
    if not files:
        return None
    out: dict[str, list[str]] = {}
    for path in files:
        with path.open() as fh:
            for line in fh:
                if not want - out.keys():
                    return out
                # cheap pre-filter before parsing a ~2 KB line
                start = line.find('"chunk_id": "')
                if start < 0:
                    continue
                cid = line[start + 13 : line.find('"', start + 13)]
                if cid in want:
                    out[cid] = json.loads(line).get("settings", [])
    return out


def enrich(rows: list[dict], exp_dir: Path, chunks_dir: Path | None) -> bool:
    """Add per-row derived fields; RAG grounding fields only when chunk files exist.

    Returns True when grounding could be computed.
    """
    rag_ids: dict[int, list[str]] = {}
    for i, r in enumerate(rows):
        if r["mode"] == "rag":
            path = exp_dir / r["diagnosis"]
            rag_ids[i] = json.loads(path.read_text()).get("chunk_ids", []) if path.exists() else []
    every = {c for ids in rag_ids.values() for c in ids}
    every |= {c for r in rows for f in r["fixes"] for c in f.get("cited_chunks", [])}
    settings = chunk_settings(every, chunks_dir) if chunks_dir and every else None
    for i, r in enumerate(rows):
        s = r["score"]
        setting = get_case(r["case_id"]).setting
        rank = s["correct_rank"]
        s["rr"] = 1.0 / rank if rank else 0.0
        correct_fix = next((f for f in r["fixes"] if f["rank"] == rank), None) if rank else None
        s["correct_value_works"] = (
            bool(correct_fix["outcome"].get("within_10pct")) if correct_fix else None
        )
        s["n_cited_fixes"] = sum(bool(f.get("cited_chunks")) for f in r["fixes"])
        if r["mode"] != "rag" or settings is None:
            continue
        ids = rag_ids.get(i, [])
        s["retrieved_correct"] = any(setting in settings.get(c, ()) for c in ids)
        s["correct_fix_grounded"] = (
            any(correct_fix["setting"].lower() in settings.get(c, ()) for c in cited)
            if correct_fix and (cited := correct_fix.get("cited_chunks"))
            else (False if correct_fix else None)
        )
    return settings is not None


# --- statistics --------------------------------------------------------------------------


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, centre - half), min(1.0, centre + half))


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value from the discordant counts b (rag only), c (norag only)."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1)) / 2**n
    return min(1.0, 2 * tail)


def bootstrap_mean_ci(
    diffs: list[float], n: int = BOOTSTRAP_N, seed: int = SEED
) -> tuple[float, float] | None:
    if not diffs:
        return None
    rng = random.Random(seed)
    k = len(diffs)
    means = sorted(statistics.fmean(rng.choices(diffs, k=k)) for _ in range(n))
    return (means[int(0.025 * n)], means[int(0.975 * n) - 1])


# --- summaries ---------------------------------------------------------------------------


def _rate(scores: list[dict], key: str) -> dict:
    vals = [s[key] for s in scores if s.get(key) is not None]
    k = sum(bool(v) for v in vals)
    return {
        "k": k,
        "n": len(vals),
        "rate": k / len(vals) if vals else None,
        "ci": wilson(k, len(vals)),
    }


def summarize(rows: list[dict]) -> dict:
    """Metrics for one group of rows (one mode, optionally one case)."""
    scores = [r["score"] for r in rows]
    n_fixes = sum(len(r["fixes"]) for r in rows)
    top1 = [r["fixes"][0]["setting"].lower() for r in rows if r["fixes"]]
    out = {
        "n": len(rows),
        "top1_correct": _rate(scores, "top1_correct"),
        "correct_in_top4": _rate(scores, "correct_in_top4"),
        "mrr": statistics.fmean(s["rr"] for s in scores) if scores else None,
        "top1_works": _rate(scores, "top1_works"),
        "any_works": _rate(scores, "any_works"),
        "correct_value_works": _rate(scores, "correct_value_works"),
        "rejected_share": sum(s["n_rejected"] for s in scores) / n_fixes if n_fixes else None,
        "failed_share": sum(s["n_failed"] for s in scores) / n_fixes if n_fixes else None,
        "cited_fix_share": (sum(s["n_cited_fixes"] for s in scores) / n_fixes if n_fixes else None),
        "distinct_top1": len(set(top1)),
        "top1_settings": dict(sorted(_count(top1).items(), key=lambda kv: -kv[1])),
        "mean_attempts": statistics.fmean(r["attempts"] for r in rows) if rows else None,
        "mean_input_tokens": (
            statistics.fmean(r["usage"]["input_tokens"] for r in rows) if rows else None
        ),
        "mean_output_tokens": (
            statistics.fmean(r["usage"]["output_tokens"] for r in rows) if rows else None
        ),
    }
    if any("retrieved_correct" in s for s in scores):
        out["retrieved_correct"] = _rate(scores, "retrieved_correct")
        out["correct_fix_grounded"] = _rate(scores, "correct_fix_grounded")
        hit = [s for s in scores if s.get("retrieved_correct")]
        miss = [s for s in scores if s.get("retrieved_correct") is False]
        out["top1_correct_if_retrieved"] = _rate(hit, "top1_correct")
        out["top1_correct_if_not_retrieved"] = _rate(miss, "top1_correct")
    return out


def _count(items: Iterable[str]) -> dict[str, int]:
    c: dict[str, int] = defaultdict(int)
    for x in items:
        c[x] += 1
    return dict(c)


def paired(rows: list[dict]) -> dict:
    """RAG vs no-RAG over (case, repeat) pairs that have both modes."""
    by: dict[tuple, dict] = defaultdict(dict)
    for r in rows:
        by[(r["case_id"], r["repeat"])][r["mode"]] = r["score"]
    pairs = [(v["rag"], v["norag"]) for v in by.values() if "rag" in v and "norag" in v]
    out: dict = {"n_pairs": len(pairs)}
    for key in PAIRED_BINARY:
        b = sum(bool(a[key]) and not bool(n[key]) for a, n in pairs)
        c = sum(bool(n[key]) and not bool(a[key]) for a, n in pairs)
        out[key] = {
            "both": sum(bool(a[key]) and bool(n[key]) for a, n in pairs),
            "rag_only": b,
            "norag_only": c,
            "neither": sum(not a[key] and not n[key] for a, n in pairs),
            "p_mcnemar": mcnemar_exact(b, c),
        }
    diffs = [a["rr"] - n["rr"] for a, n in pairs]
    out["rr_diff"] = {
        "mean": statistics.fmean(diffs) if diffs else None,
        "ci": bootstrap_mean_ci(diffs),
    }
    return out


JUDGE_KEYS = ("evidence", "accuracy", "diagnosis", "overall", "unsupported_claims")


def judge_summary(rows: list[dict]) -> dict | None:
    """Mean judge scores for rows that have a judgement (None if none has)."""
    graded = [r["score"]["judge"] for r in rows if r["score"].get("judge")]
    if not graded:
        return None
    return {"n": len(graded), **{k: statistics.fmean(g[k] for g in graded) for k in JUDGE_KEYS}}


def judge_paired(rows: list[dict]) -> dict | None:
    """Per criterion: RAG minus no-RAG over graded pairs; sign test and bootstrap interval."""
    by: dict[tuple, dict] = defaultdict(dict)
    for r in rows:
        if r["score"].get("judge"):
            by[(r["case_id"], r["repeat"])][r["mode"]] = r["score"]["judge"]
    pairs = [(v["rag"], v["norag"]) for v in by.values() if "rag" in v and "norag" in v]
    if not pairs:
        return None
    out: dict = {"n_pairs": len(pairs)}
    for k in JUDGE_KEYS:
        diffs = [a[k] - n[k] for a, n in pairs]
        wins, losses = sum(d > 0 for d in diffs), sum(d < 0 for d in diffs)
        out[k] = {
            "mean_diff": statistics.fmean(diffs),
            "ci": bootstrap_mean_ci(diffs),
            "rag_higher": wins,
            "norag_higher": losses,
            "ties": len(diffs) - wins - losses,
            "p_sign": mcnemar_exact(wins, losses),  # exact two-sided sign test
        }
    return out


def evaluate(exp_dir: Path, chunks_dir: Path | None = None, judge_dir: Path | None = None) -> dict:
    from pgdiag.evaluate.judge import load_judgements

    rows = load_rows(exp_dir)
    if not rows:
        raise ValueError(f"no rows in {exp_dir / 'results.jsonl'}")
    grounding = enrich(rows, exp_dir, chunks_dir)
    judged = load_judgements(judge_dir)
    for r in rows:
        r["score"]["judge"] = judged.get((r["case_id"], r["repeat"], r["mode"]))
    manifest_path = exp_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    modes = [m for m in MODES if any(r["mode"] == m for r in rows)]
    cases = sorted({r["case_id"] for r in rows})
    return {
        "experiment": str(exp_dir),
        "manifest": manifest,
        "n_rows": len(rows),
        "grounding_available": grounding,
        "modes": modes,
        "cases": cases,
        "overall": {m: summarize([r for r in rows if r["mode"] == m]) for m in modes},
        "per_case": {
            c: {
                m: summarize([r for r in rows if r["mode"] == m and r["case_id"] == c])
                for m in modes
            }
            for c in cases
        },
        "paired": paired(rows),
        "paired_per_case": {c: paired([r for r in rows if r["case_id"] == c]) for c in cases},
        "judge": {
            "overall": {m: judge_summary([r for r in rows if r["mode"] == m]) for m in modes},
            "per_case": {
                c: {
                    m: judge_summary([r for r in rows if r["mode"] == m and r["case_id"] == c])
                    for m in modes
                }
                for c in cases
            },
            "paired": judge_paired(rows),
            "models": sorted({j["judge_model"] for j in judged.values()}),
        },
    }


# --- report ------------------------------------------------------------------------------


def _pct(rate: dict | None) -> str:
    if not rate or rate["rate"] is None:
        return "-"
    lo, hi = rate["ci"]
    return f"{rate['k']}/{rate['n']} ({rate['rate']:.0%}, {lo:.0%}-{hi:.0%})"


def _short(rate: dict | None) -> str:
    return "-" if not rate or rate["rate"] is None else f"{rate['k']}/{rate['n']}"


def _num(x, fmt=".2f") -> str:
    return "-" if x is None else format(x, fmt)


def _p(p: float) -> str:
    return "<0.001" if p < 0.001 else f"{p:.3f}"


def report(ev: dict) -> str:
    m = ev["manifest"]
    modes = ev["modes"]
    o = ev["overall"]
    lines = [
        "# RAG vs no-RAG: PostgreSQL misconfiguration diagnosis",
        "",
        f"Experiment `{ev['experiment']}`: model `{m.get('model')}`, fix-test preset "
        f"`{m.get('preset')}`, retrieval k {m.get('retrieval_k')}, {len(ev['cases'])} cases, "
        f"{ev['n_rows']} runs, git `{m.get('git_commit')}`.",
        "",
        "A run names 4 ranked settings with values. *Correct* = names the broken setting; "
        "*works* = applying the fix brings the workload within 10% of the healthy reference "
        "(measured, T10). Rates show k/n (share, Wilson 95% interval).",
        "",
        "## Overall",
        "",
        "| metric | " + " | ".join(modes) + " |",
        "|---|" + "---|" * len(modes),
    ]
    rows = [
        ("runs", lambda s: str(s["n"])),
        ("top-1 correct", lambda s: _pct(s["top1_correct"])),
        ("correct in top 4", lambda s: _pct(s["correct_in_top4"])),
        ("MRR", lambda s: _num(s["mrr"])),
        ("top-1 fix works", lambda s: _pct(s["top1_works"])),
        ("any of 4 fixes works", lambda s: _pct(s["any_works"])),
        ("correct setting's value works", lambda s: _pct(s["correct_value_works"])),
        ("proposals rejected", lambda s: _num(s["rejected_share"], ".0%")),
        ("proposals failed", lambda s: _num(s["failed_share"], ".0%")),
        ("fixes with citations", lambda s: _num(s["cited_fix_share"], ".0%")),
        ("mean attempts", lambda s: _num(s["mean_attempts"])),
        ("mean input / output tokens", lambda s: f"{_num(s['mean_input_tokens'], '.0f')} / "
         f"{_num(s['mean_output_tokens'], '.0f')}"),
    ]  # fmt: skip
    for name, fn in rows:
        lines.append(f"| {name} | " + " | ".join(fn(o[md]) for md in modes) + " |")

    pr = ev["paired"]
    if pr["n_pairs"]:
        lines += [
            "",
            f"## Paired comparison ({pr['n_pairs']} pairs: same case, repeat and symptoms)",
            "",
            "| metric | both | RAG only | no-RAG only | neither | McNemar p |",
            "|---|---|---|---|---|---|",
        ]
        for key in PAIRED_BINARY:
            d = pr[key]
            lines.append(
                f"| {key.replace('_', ' ')} | {d['both']} | {d['rag_only']} | "
                f"{d['norag_only']} | {d['neither']} | {_p(d['p_mcnemar'])} |"
            )
        rr = pr["rr_diff"]
        ci = rr["ci"]
        lines += [
            "",
            f"Reciprocal rank, RAG minus no-RAG: mean {rr['mean']:+.3f} "
            f"(bootstrap 95% interval {ci[0]:+.3f} to {ci[1]:+.3f}).",
        ]

    if "rag" in o and "retrieved_correct" in o["rag"]:
        r = o["rag"]
        lines += [
            "",
            "## Retrieval and grounding (RAG runs)",
            "",
            f"- A retrieved chunk mentions the broken setting: {_pct(r['retrieved_correct'])}.",
            f"- Top-1 correct when it does: {_pct(r['top1_correct_if_retrieved'])}; "
            f"when it does not: {_pct(r['top1_correct_if_not_retrieved'])}.",
            f"- The correct fix cites a chunk mentioning its setting: "
            f"{_pct(r['correct_fix_grounded'])}.",
        ]
    elif "rag" in o:
        lines += ["", "Grounding metrics skipped: no chunk files under `data/chunks/`."]

    lines += judge_report(ev)

    lines += [
        "",
        "## Per case",
        "",
        "| case | mode | top-1 correct | top 4 | MRR | top-1 works | any works | "
        "distinct top-1 | most common top-1 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for c in ev["cases"]:
        for md in modes:
            s = ev["per_case"][c][md]
            if not s["n"]:
                continue
            common = ", ".join(f"{k} ({v})" for k, v in list(s["top1_settings"].items())[:2])
            lines.append(
                f"| {c} | {md} | {_short(s['top1_correct'])} | {_short(s['correct_in_top4'])} | "
                f"{_num(s['mrr'])} | {_short(s['top1_works'])} | {_short(s['any_works'])} | "
                f"{s['distinct_top1']} | {common} |"
            )
    if len(modes) == 2:
        lines += ["", "Per-case paired McNemar p (top-1 correct): " + ", ".join(
            f"{c} {_p(ev['paired_per_case'][c]['top1_correct']['p_mcnemar'])}"
            for c in ev["cases"]
        ) + "."]  # fmt: skip
    lines += [
        "",
        "## Caveats",
        "",
        "- Six cases, a few repeats each: intervals are wide and per-case p-values are not "
        "corrected for multiple comparisons; read them as descriptive.",
        "- A cached fix (T11) keeps the measurement from the run that first tested it.",
        "- *Works* uses one quick benchmark per fix; T6/T10 measured ~2-8% drift between runs.",
        (
            "- Reasoning grades come from one judge model that saw the true cause; chunk ids "
            "were removed but wording can still reveal RAG answers, so blinding is partial."
            if any(ev["judge"]["overall"].values())
            else "- Reasoning quality is not graded beyond citation grounding "
            "(run `pgdiag-evaluate --judge`)."
        ),
        "",
    ]
    return "\n".join(lines)


def judge_report(ev: dict) -> list[str]:
    j = ev["judge"]
    modes = [m for m in ev["modes"] if j["overall"].get(m)]
    if not modes:
        return []
    judge_models = j.get("models") or []
    lines = [
        "",
        "## Reasoning quality (blinded judge)",
        "",
        "Scores 1-5 (higher is better) except unsupported claims (a count, lower is better). "
        "The judge saw the symptom report, the true cause and the 4 fixes with reasoning, "
        "not the mode, the documentation, the citations or the measured outcomes."
        + (f" Judge model: {', '.join(judge_models)}." if judge_models else ""),
        "",
        "| criterion | " + " | ".join(f"{m} (n={j['overall'][m]['n']})" for m in modes) + " |",
        "|---|" + "---|" * len(modes),
    ]
    for k in JUDGE_KEYS:
        lines.append(
            f"| {k.replace('_', ' ')} | "
            + " | ".join(f"{j['overall'][m][k]:.2f}" for m in modes)
            + " |"
        )
    p = j["paired"]
    if p:
        lines += [
            "",
            f"Paired ({p['n_pairs']} pairs), RAG minus no-RAG:",
            "",
            "| criterion | mean diff | 95% interval | RAG higher | no-RAG higher | ties | sign p |",
            "|---|---|---|---|---|---|---|",
        ]
        for k in JUDGE_KEYS:
            d = p[k]
            lo, hi = d["ci"]
            lines.append(
                f"| {k.replace('_', ' ')} | {d['mean_diff']:+.2f} | {lo:+.2f} to {hi:+.2f} | "
                f"{d['rag_higher']} | {d['norag_higher']} | {d['ties']} | {_p(d['p_sign'])} |"
            )
    per = j["per_case"]
    lines += ["", "Overall score per case: " + "; ".join(
        f"{c} " + ", ".join(f"{m} {per[c][m]['overall']:.1f}" for m in modes if per[c].get(m))
        for c in ev["cases"]
    ) + "."]  # fmt: skip
    return lines
