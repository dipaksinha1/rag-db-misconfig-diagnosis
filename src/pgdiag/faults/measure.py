"""Measure every case against baselines benchmarked right before and after it (T6 acceptance).

For each case, in order::

    baseline    reset (RESET ALL + restart + ANALYZE)                    -> benchmark
    broken      apply_case (RESET ALL + SET bad + restart + ANALYZE)     -> benchmark
    fix         try_fix(setting, good_value) on top of the broken case   -> benchmark
                undo (the bad value is back; verified)
    post_reset  reset                                                    -> benchmark

The post-reset benchmark of one case is the baseline of the next one (when it is
at most ``max_baseline_age_s`` old and nothing ran after it), so N cases cost
3N + 1 benchmarks and every broken run is compared with a baseline measured a
few minutes before it. The host's speed drifts (5-9% between back-to-back
baselines on a bad day), so the slowdown is computed against the paired
baseline right before the case, and the fix's recovery against the mean of the
two baselines that bracket it (``ref`` = mean of baseline and post_reset).

Every step and every finished case is appended to ``results/faults/measure.jsonl``
(one JSON object per line, tagged with the run id). An interrupted run resumes
case by case (an unfinished case is redone; its baseline is reused when fresh).
When a run finishes, ``results/faults/table.md`` and
``results/faults/cases_measured.json`` (per-case medians for T10/T12) are written.
"""

from __future__ import annotations

import datetime as _dt
import json
import statistics
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from pgdiag.config import Config, get_config
from pgdiag.faults import inject, units
from pgdiag.faults.cases import FaultCase, baseline_config_values, get_case, load_cases
from pgdiag.workload.bench import BenchResult, rel_diff_pct, run_benchmark

RECORD_SCHEMA = 1
RECOVERY_MAX_PCT = 10.0  # T10's acceptance: the known-correct fix is within 10% of baseline
RESET_MAX_PCT = 10.0  # post-reset vs paired baseline
DEFAULT_MAX_BASELINE_AGE_S = 15 * 60
STEP_KINDS = ("baseline", "broken", "fix", "post_reset")


def faults_dir(cfg: Config | None = None) -> Path:
    return (cfg or get_config()).results_dir / "faults"


def jsonl_path(cfg: Config | None = None) -> Path:
    return faults_dir(cfg) / "measure.jsonl"


def table_path(cfg: Config | None = None) -> Path:
    return faults_dir(cfg) / "table.md"


def summary_path(cfg: Config | None = None) -> Path:
    return faults_dir(cfg) / "cases_measured.json"


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(stamp: str) -> float:
    return _dt.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.UTC).timestamp()


# ---------------------------------------------------------------------------
# the record file


def append_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps({"schema": RECORD_SCHEMA, **record}) + "\n")
        fh.flush()


def read_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a line cut short by an interruption
    return out


def run_records(records: list[dict], run_id: str) -> list[dict]:
    return [r for r in records if r.get("run") == run_id]


def latest_run(records: list[dict], *, preset: str | None = None, finished: bool | None = None):
    """The id of the most recent run (optionally: with this preset / (un)finished)."""
    done = {r["run"] for r in records if r.get("type") == "finished"}
    for r in reversed(records):
        if r.get("type") != "run":
            continue
        if preset is not None and r.get("preset") != preset:
            continue
        if finished is not None and (r["run"] in done) != finished:
            continue
        return r["run"]
    return None


# ---------------------------------------------------------------------------
# rows


def step_record(kind: str, case_id: str, res: BenchResult) -> dict:
    t = res.summary["total"]
    return {
        "type": "step",
        "kind": kind,
        "case_id": case_id,
        "bench_path": res.path,
        "started_at": res.started_at,
        "finished_at": res.finished_at,
        "total_median": t["median"],
        "total_cv_pct": t["cv_pct"],
        "total_spread_pct": t["spread_pct"],
        "pass_totals": [p.total_s for p in res.measured()],
        "component_medians": res.component_medians(),
        "component_cv_pct": {c: v["cv_pct"] for c, v in res.summary["components"].items()},
        "settings": res.settings,
        "errors": res.errors,
        "temp_files": sum(p.server_stats.get("temp_files", 0) for p in res.measured()),
    }


def _pct(a: float, b: float) -> float:
    return rel_diff_pct(a, b)


def build_row(
    case: FaultCase,
    steps: dict[str, dict],
    *,
    reset_config_ok: bool,
    undo_ok: bool,
    fix: dict,
) -> dict:
    """The comparison for one case from its four steps (baseline, broken, fix, post_reset)."""
    pre, broken, fixed, post = (steps[k] for k in STEP_KINDS)
    pc = case.primary_component
    ref_total = statistics.fmean([pre["total_median"], post["total_median"]])
    ref_comp = {
        c: statistics.fmean([pre["component_medians"][c], post["component_medians"][c]])
        for c in pre["component_medians"]
    }
    post_vs_pre = _pct(pre["total_median"], post["total_median"])
    fix_vs_ref = _pct(ref_total, fixed["total_median"])
    row = {
        **case.to_dict(),
        "baseline_total_s": pre["total_median"],
        "baseline_cv_pct": pre["total_cv_pct"],
        "broken_total_s": broken["total_median"],
        "broken_cv_pct": broken["total_cv_pct"],
        "slowdown_pct": _pct(pre["total_median"], broken["total_median"]),
        "slowdown_vs_ref_pct": _pct(ref_total, broken["total_median"]),
        "primary_baseline_s": pre["component_medians"][pc],
        "primary_broken_s": broken["component_medians"][pc],
        "primary_ratio": broken["component_medians"][pc] / pre["component_medians"][pc],
        "component_slowdown_pct": {
            c: _pct(pre["component_medians"][c], broken["component_medians"][c])
            for c in pre["component_medians"]
            if pre["component_medians"][c]
        },
        "broken_temp_files": broken["temp_files"],
        "fix": fix,
        "fix_total_s": fixed["total_median"],
        "fix_cv_pct": fixed["total_cv_pct"],
        "fix_vs_baseline_pct": _pct(pre["total_median"], fixed["total_median"]),
        "fix_vs_post_reset_pct": _pct(post["total_median"], fixed["total_median"]),
        "fix_vs_ref_pct": fix_vs_ref,
        "fix_primary_s": fixed["component_medians"][pc],
        "fix_primary_vs_ref_pct": _pct(ref_comp[pc], fixed["component_medians"][pc]),
        "fix_recovered": abs(fix_vs_ref) <= RECOVERY_MAX_PCT,
        "undo_ok": undo_ok,
        "post_reset_total_s": post["total_median"],
        "post_reset_cv_pct": post["total_cv_pct"],
        "post_reset_vs_baseline_pct": post_vs_pre,
        "reset_config_ok": reset_config_ok,
        "reset_timing_ok": abs(post_vs_pre) <= RESET_MAX_PCT,
        "reset_ok": reset_config_ok and abs(post_vs_pre) <= RESET_MAX_PCT,
        "ref_total_s": ref_total,
        "settings_seen": {k: steps[k]["settings"].get(case.setting) for k in STEP_KINDS},
        "errors": {k: steps[k]["errors"] for k in STEP_KINDS if steps[k]["errors"]},
        "bench": {k: steps[k]["bench_path"] for k in STEP_KINDS},
        "started_at": pre["started_at"],
        "finished_at": post["finished_at"],
    }
    return row


def settings_problems(
    srv: inject.ServerControl, seen: dict[str, str], expected: dict[str, str]
) -> list[str]:
    """Compare a benchmark's recorded case settings with what they should have been."""
    problems = []
    for name, got in seen.items():
        want = expected.get(name)
        if want is not None and not units.same_value(got, want, srv.info(name)):
            problems.append(f"{name} = {got}, expected {want}")
    return problems


# ---------------------------------------------------------------------------
# the run


def run_measure(
    cases: Sequence[FaultCase] | None = None,
    *,
    preset: str = "full",
    fresh: bool = False,
    max_baseline_age_s: float = DEFAULT_MAX_BASELINE_AGE_S,
    cfg: Config | None = None,
    srv: inject.ServerControl | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict:
    cfg = cfg or get_config()
    srv = srv or inject.PgServer(cfg)
    say = progress or print
    cases = list(cases or load_cases())
    path = jsonl_path(cfg)
    state_path = inject.default_state_path(cfg)
    base_values = baseline_config_values(cfg)

    records = read_records(path)
    run_id = None if fresh else latest_run(records, preset=preset, finished=False)
    if run_id is None:
        run_id = f"{preset}-{_now().replace('-', '').replace(':', '')}"
        append_record(
            path,
            {
                "type": "run",
                "run": run_id,
                "preset": preset,
                "cases": [c.case_id for c in cases],
                "started_at": _now(),
            },
        )
        say(f"measure: new run {run_id} ({len(cases)} cases, preset {preset})")
    else:
        say(f"measure: resuming run {run_id}")

    def mine() -> list[dict]:
        return run_records(read_records(path), run_id)

    def done_cases() -> set[str]:
        return {r["case_id"] for r in mine() if r.get("type") == "case"}

    def record_step(kind: str, case: FaultCase) -> dict:
        label = f"t6-{kind}-{case.case_id}"
        res = run_benchmark(preset, label, cfg=cfg, progress=lambda m: say("    " + m))
        rec = {"run": run_id, **step_record(kind, case.case_id, res)}
        append_record(path, rec)
        say(
            f"  {kind}: total median {rec['total_median']:.2f}s (cv {rec['total_cv_pct']:.1f}%)"
            + (f" ERRORS {len(rec['errors'])}" if rec["errors"] else "")
        )
        return rec

    def fresh_baseline() -> dict | None:
        steps = [r for r in mine() if r.get("type") == "step"]
        if not steps:
            return None
        last = steps[-1]
        if last["kind"] not in ("baseline", "post_reset") or last["errors"]:
            return None
        if time.time() - _epoch(last["finished_at"]) > max_baseline_age_s:
            return None
        return last

    # Whatever happened before (crash in the middle of a case), start from the baseline.
    if inject.load_state(state_path).get("case") or srv.auto_conf():
        say("measure: a case or ALTER SYSTEM entry is applied; resetting first")
        inject.reset(srv=srv, cfg=cfg, progress=lambda m: say("  " + m))

    try:
        for case in cases:
            if case.case_id in done_cases():
                continue
            say(f"measure: {case.case_id} ({case.setting}: {case.good_value} -> {case.bad_value})")
            steps: dict[str, dict] = {}
            pre = fresh_baseline()
            if pre is not None:
                say(f"  baseline: reusing the step finished at {pre['finished_at']}")
            else:
                inject.reset(srv=srv, cfg=cfg, progress=lambda m: say("  " + m))
                pre = record_step("baseline", case)
            steps["baseline"] = pre

            inject.apply_case(case, srv=srv, cfg=cfg, progress=lambda m: say("  " + m))
            problems = inject.verify(case, srv=srv, cfg=cfg)
            if problems:
                raise inject.FaultError(f"{case.case_id}: " + "; ".join(problems))
            steps["broken"] = record_step("broken", case)

            attempt = inject.try_fix(case.setting, case.good_value, srv=srv, cfg=cfg)
            if not attempt.applied:
                raise inject.FaultError(f"{case.case_id}: the good value failed: {attempt.error}")
            say(f"  fix: {case.setting} = {case.good_value} ({attempt.change['method']})")
            steps["fix"] = record_step("fix", case)
            inject.undo(srv=srv, cfg=cfg)
            undo_problems = inject.verify(case, srv=srv, cfg=cfg)
            say("  undo: " + ("bad value restored" if not undo_problems else str(undo_problems)))

            inject.reset(srv=srv, cfg=cfg, progress=lambda m: say("  " + m))
            config_ok = not inject.verify(None, srv=srv, cfg=cfg)
            steps["post_reset"] = record_step("post_reset", case)
            config_ok = config_ok and not settings_problems(
                srv, steps["post_reset"]["settings"], base_values
            )
            row = build_row(
                case,
                steps,
                reset_config_ok=config_ok,
                undo_ok=not undo_problems,
                fix=attempt.to_dict(),
            )
            append_record(
                path, {"run": run_id, "type": "case", "case_id": case.case_id, "row": row}
            )
            say("measure: " + format_row_text(row))
    finally:
        if inject.load_state(state_path).get("case"):
            say("measure: restoring the baseline")
            inject.reset(srv=srv, cfg=cfg, progress=lambda m: say("  " + m))

    append_record(path, {"run": run_id, "type": "finished", "finished_at": _now()})
    run = load_run(run_id, cfg)
    write_outputs(run, cfg)
    return run


def load_run(run_id: str | None = None, cfg: Config | None = None) -> dict:
    """A run's header and its case rows (default: the latest finished run, else the latest)."""
    records = read_records(jsonl_path(cfg))
    run_id = run_id or latest_run(records, finished=True) or latest_run(records)
    if run_id is None:
        raise FileNotFoundError(f"no measure run in {jsonl_path(cfg)}")
    mine = run_records(records, run_id)
    header = next(r for r in mine if r.get("type") == "run")
    rows = {r["case_id"]: r["row"] for r in mine if r.get("type") == "case"}  # last one wins
    order = {c.case_id: i for i, c in enumerate(load_cases())}
    return {
        "run": run_id,
        "preset": header["preset"],
        "started_at": header["started_at"],
        "finished": any(r.get("type") == "finished" for r in mine),
        "rows": sorted(rows.values(), key=lambda r: order.get(r["case_id"], 99)),
    }


def format_row_text(r: dict) -> str:
    return (
        f"{r['case_id']}: total {r['baseline_total_s']:.2f} -> {r['broken_total_s']:.2f}s "
        f"({r['slowdown_pct']:+.0f}%), {r['primary_component']} "
        f"{r['primary_baseline_s']:.2f} -> {r['primary_broken_s']:.2f}s (x{r['primary_ratio']:.1f})"
        f"; fix {r['fix_total_s']:.2f}s ({r['fix_vs_ref_pct']:+.1f}% vs ref); post-reset "
        f"{r['post_reset_total_s']:.2f}s ({r['post_reset_vs_baseline_pct']:+.1f}%), "
        f"reset {'OK' if r['reset_ok'] else 'NOT OK'}"
    )


def format_table(run: dict) -> str:
    lines = [
        f"Fault cases, preset `{run['preset']}` (medians of the measured passes; [cv] = "
        f"coefficient of variation of the pass totals), run `{run['run']}`.",
        "Slowdown is against the paired baseline benchmarked right before the case; the "
        "good-value fix is compared with ref = mean(baseline, post-reset).",
        "",
        "| case | setting | bad value | good value | restart? | baseline total (s) [cv] | "
        "broken total (s) [cv] | slowdown % | primary component baseline→broken | "
        "good-value-fix total (s) [cv] | fix vs ref % | post-reset total (s) [cv] | reset OK? |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in run["rows"]:
        reset = "yes" if r["reset_ok"] else "NO"
        reset += f" (cfg {'ok' if r['reset_config_ok'] else 'NOT ok'}, "
        reset += f"{r['post_reset_vs_baseline_pct']:+.1f}%)"
        fix_mark = "" if r["fix_recovered"] else " (!)"
        lines.append(
            f"| {r['case_id']} | {r['setting']} | {r['bad_value']} | {r['good_value']} | "
            f"{'yes' if r['restart_required'] else 'no'} | "
            f"{r['baseline_total_s']:.2f} [{r['baseline_cv_pct']:.1f}%] | "
            f"{r['broken_total_s']:.2f} [{r['broken_cv_pct']:.1f}%] | "
            f"{r['slowdown_pct']:+.0f}% | "
            f"{r['primary_component']} {r['primary_baseline_s']:.2f}→"
            f"{r['primary_broken_s']:.2f}s (x{r['primary_ratio']:.1f}) | "
            f"{r['fix_total_s']:.2f} [{r['fix_cv_pct']:.1f}%] | "
            f"{r['fix_vs_ref_pct']:+.1f}%{fix_mark} | "
            f"{r['post_reset_total_s']:.2f} [{r['post_reset_cv_pct']:.1f}%] | {reset} |"
        )
    lines += [
        "",
        f"(!) = the good-value fix is more than {RECOVERY_MAX_PCT:.0f}% away from ref. "
        "reset OK = configuration verified (ten target settings at baseline, nothing in "
        "postgresql.auto.conf, no pending restart) and post-reset total within "
        f"{RESET_MAX_PCT:.0f}% of the paired baseline.",
    ]
    return "\n".join(lines)


SUMMARY_KEYS = (
    "setting",
    "bad_value",
    "good_value",
    "restart_required",
    "needs_analyze",
    "primary_component",
    "baseline_total_s",
    "baseline_cv_pct",
    "broken_total_s",
    "broken_cv_pct",
    "slowdown_pct",
    "primary_baseline_s",
    "primary_broken_s",
    "primary_ratio",
    "component_slowdown_pct",
    "fix_total_s",
    "fix_cv_pct",
    "fix_vs_ref_pct",
    "fix_primary_vs_ref_pct",
    "fix_recovered",
    "post_reset_total_s",
    "post_reset_vs_baseline_pct",
    "ref_total_s",
    "reset_ok",
    "bench",
)


def write_outputs(run: dict, cfg: Config | None = None) -> dict[str, Path]:
    """``results/faults/table.md`` and ``results/faults/cases_measured.json``."""
    cfg = cfg or get_config()
    out = {"table": table_path(cfg), "summary": summary_path(cfg)}
    out["table"].parent.mkdir(parents=True, exist_ok=True)
    out["table"].write_text(format_table(run) + "\n")
    summary = {
        "run": run["run"],
        "preset": run["preset"],
        "started_at": run["started_at"],
        "generated_at": _now(),
        "source": str(jsonl_path(cfg)),
        "notes": (
            "baseline_total_s is the paired baseline benchmarked right before the case; "
            "ref_total_s = mean(baseline, post-reset). Medians of the measured passes."
        ),
        "cases": {r["case_id"]: {k: r[k] for k in SUMMARY_KEYS} for r in run["rows"]},
    }
    tmp = out["summary"].with_suffix(".json.tmp")
    tmp.write_text(json.dumps(summary, indent=2) + "\n")
    tmp.replace(out["summary"])
    return out


def load_measurements(cfg: Config | None = None) -> dict:
    """Per-case summary (``results/faults/cases_measured.json``) for T10/T12."""
    return json.loads(summary_path(cfg).read_text())


def resolve_cases(keys: Sequence[str] | None) -> list[FaultCase]:
    return [get_case(k) for k in keys] if keys else list(load_cases())
