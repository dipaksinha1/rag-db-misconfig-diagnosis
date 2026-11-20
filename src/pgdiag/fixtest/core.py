"""Try each proposed fix on the broken database and time the workload (T10).

``test_fixes(case, fixes)`` expects the case to be applied (state file) and runs::

    ref_before  reset                          -> benchmark (healthy)
                apply_case(case)
    broken                                     -> benchmark
    per fix     try_fix(setting, value)        -> benchmark -> undo -> verify(case) == []
    ref_after   reset                          -> benchmark (healthy)

The host's speed drifts by up to ~8% over minutes (T6), so fixes are compared with
``ref`` = mean of the two healthy runs that bracket them, not with a stored baseline.
The server always ends at the baseline (reset), also on errors.
"""

from __future__ import annotations

import datetime as _dt
import statistics
import time
from collections.abc import Callable, Sequence

from pgdiag.config import Config
from pgdiag.faults import inject
from pgdiag.faults.cases import FaultCase, get_case
from pgdiag.workload.bench import run_benchmark

DEFAULT_PRESET = "quick"
WITHIN_PCT = 10.0


class FixTestError(RuntimeError):
    pass


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Ops:
    """The database actions used by :func:`test_fixes` (tests swap in a fake)."""

    def __init__(self, cfg: Config | None = None, progress: Callable[[str], None] | None = None):
        self.cfg = cfg
        self.say = progress or (lambda m: None)

    def apply_case(self, case: FaultCase) -> None:
        inject.apply_case(case, cfg=self.cfg)

    def reset(self) -> None:
        try:
            inject.reset(cfg=self.cfg)
        except Exception:  # server down or config left behind: empty auto.conf, then reset
            inject.emergency_reset(self.cfg)
            inject.reset(cfg=self.cfg)

    def try_fix(self, setting: str, value) -> inject.FixAttempt:
        return inject.try_fix(setting, value, cfg=self.cfg)

    def undo(self) -> None:
        inject.undo(cfg=self.cfg)

    def verify(self, case: FaultCase | None) -> list[str]:
        return inject.verify(case, cfg=self.cfg)

    def bench(self, preset: str, label: str) -> dict:
        res = run_benchmark(preset, f"t10-{label}", save=False, cfg=self.cfg, progress=self.say)
        t = res.summary["total"]
        return {
            "median_s": round(t["median"], 4),
            "cv_pct": round(t["cv_pct"], 2),
            "components": {k: round(v, 4) for k, v in res.component_medians().items()},
            "started_at": res.started_at,
            "duration_s": round(res.duration_s, 1),
            "errors": res.errors,
        }


def _pct(a: float, b: float) -> float:
    return round((a - b) / b * 100, 2)


def _test_one(case: FaultCase, fix: dict, preset: str, ops: Ops, say) -> dict:
    t0 = time.monotonic()
    rec = {
        "rank": fix["rank"],
        "setting": fix["setting"],
        "proposed_value": fix["proposed_value"],
        "status": None,
        "method": None,
        "median_s": None,
        "components": None,
        "vs_ref_pct": None,
        "recovery_pct": None,
        "within_10pct": False,
        "error": None,
    }
    attempt = ops.try_fix(fix["setting"], fix["proposed_value"])
    if not attempt.applied:  # nothing changed (or the server was put back): no benchmark
        rec.update(status="rejected", error=f"{attempt.error_kind}: {attempt.error}")
        say(f"fix {fix['rank']}: rejected ({rec['error']})")
        rec["wall_s"] = round(time.monotonic() - t0, 1)
        return rec
    rec["method"] = (attempt.change or {}).get("method")
    say(f"fix {fix['rank']}: {fix['setting']} = {fix['proposed_value']} ({rec['method']})")
    try:
        b = ops.bench(preset, f"{case.case_id}-fix{fix['rank']}")
        rec.update(median_s=b["median_s"], components=b["components"], bench=b)
        rec["status"] = "failed" if b["errors"] else "applied"
        if b["errors"]:
            rec["error"] = "benchmark errors: " + "; ".join(b["errors"][:3])
    except Exception as exc:  # e.g. a backend OOM-killed by a huge legal work_mem
        rec.update(status="failed", error=f"benchmark: {type(exc).__name__}: {exc}")
    try:
        ops.undo()
        problems = ops.verify(case)
    except Exception as exc:
        problems = [f"undo: {type(exc).__name__}: {exc}"]
    if problems:
        say(f"fix {fix['rank']}: undo left {problems}; reset + re-apply {case.case_id}")
        rec["recovery"] = problems
        ops.reset()
        ops.apply_case(case)
    rec["wall_s"] = round(time.monotonic() - t0, 1)
    return rec


def _add_metrics(out: dict) -> None:
    ref = statistics.fmean(r["median_s"] for r in out["refs"])
    broken = out["broken"]["median_s"]
    out["ref_s"] = round(ref, 4)
    out["broken_vs_ref_pct"] = _pct(broken, ref)
    for rec in out["fixes"]:
        if rec["median_s"] is None:
            continue
        rec["vs_ref_pct"] = _pct(rec["median_s"], ref)
        if broken != ref:
            rec["recovery_pct"] = round((broken - rec["median_s"]) / (broken - ref) * 100, 1)
        rec["within_10pct"] = rec["status"] == "applied" and abs(rec["vs_ref_pct"]) <= WITHIN_PCT
    applied = [r for r in out["fixes"] if r["status"] == "applied"]
    best = min(applied, key=lambda r: r["median_s"], default=None)
    out["best"] = None
    if best:
        keys = ("rank", "setting", "proposed_value", "median_s", "vs_ref_pct", "recovery_pct")
        out["best"] = {k: best[k] for k in (*keys, "within_10pct")}


def test_fixes(
    case: FaultCase | str,
    fixes: Sequence[dict],
    *,
    preset: str = DEFAULT_PRESET,
    ops: Ops | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict:
    """Benchmark each fix ({"rank"?, "setting", "proposed_value"}) on the applied case.

    Returns a JSON-ready dict; the server is at the baseline afterwards.
    """
    case = get_case(case) if isinstance(case, str) else case
    say = progress or (lambda m: None)
    ops = ops or Ops(progress=say)
    problems = ops.verify(case)
    if problems:
        raise FixTestError(f"{case.case_id} is not applied: " + "; ".join(problems))
    fixes = [
        {
            "rank": f.get("rank", i + 1),
            "setting": f["setting"],
            "proposed_value": f["proposed_value"],
        }
        for i, f in enumerate(fixes)
    ]
    fixes.sort(key=lambda f: f["rank"])
    t0 = time.monotonic()
    out = {
        "case_id": case.case_id,
        "setting": case.setting,
        "bad_value": case.bad_value,
        "good_value": case.good_value,
        "preset": preset,
        "started_at": _now(),
        "refs": [],
        "broken": None,
        "fixes": [],
    }
    try:
        say("ref_before: reset + benchmark")
        ops.reset()
        out["refs"].append(ops.bench(preset, f"{case.case_id}-ref-before"))
        ops.apply_case(case)
        say("broken: benchmark")
        out["broken"] = ops.bench(preset, f"{case.case_id}-broken")
        for fix in fixes:
            out["fixes"].append(_test_one(case, fix, preset, ops, say))
        say("ref_after: reset + benchmark")
        ops.reset()
        out["refs"].append(ops.bench(preset, f"{case.case_id}-ref-after"))
    except BaseException:
        ops.reset()  # never leave the case (or a fix) behind
        raise
    for r in (*out["refs"], out["broken"]):
        if r["errors"]:
            raise FixTestError(f"reference benchmark had errors: {r['errors'][:3]}")
    _add_metrics(out)
    out["finished_at"] = _now()
    out["wall_s"] = round(time.monotonic() - t0, 1)
    return out


test_fixes.__test__ = False  # not a pytest test


def format_table(out: dict) -> str:
    runs = ", ".join(format(r["median_s"], ".2f") for r in out["refs"])
    lines = [
        f"{out['case_id']} ({out['preset']}): ref {out['ref_s']:.2f}s (runs {runs}), "
        f"broken {out['broken']['median_s']:.2f}s ({out['broken_vs_ref_pct']:+.1f}%), "
        f"wall {out['wall_s']:.0f}s",
        "| rank | fix | status | method | median s | vs ref % | recovery % | within 10% |",
        "|---|---|---|---|---|---|---|---|",
    ]

    def num(v, fmt):
        return "-" if v is None else format(v, fmt)

    for r in out["fixes"]:
        lines.append(
            f"| {r['rank']} | {r['setting']} = {r['proposed_value']} | {r['status']} | "
            f"{r['method'] or '-'} | {num(r['median_s'], '.2f')} | "
            f"{num(r['vs_ref_pct'], '+.1f')} | "
            f"{num(r['recovery_pct'], '.0f')} | {'yes' if r['within_10pct'] else 'no'} |"
        )
    for r in out["fixes"]:
        if r["error"]:
            lines.append(f"  rank {r['rank']}: {r['error'][:160]}")
    best = out["best"]
    lines.append(
        f"best: rank {best['rank']} {best['setting']} = {best['proposed_value']}"
        if best
        else "best: none (no fix applied)"
    )
    return "\n".join(lines)
