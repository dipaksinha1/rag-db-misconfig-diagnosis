"""Sensitivity probe: can the workload feel each of the ten settings? (T5, informative)

For every target setting: set a plausible bad value, run a benchmark (default
preset ``quick``), compare with a baseline run made at the start of the probe,
and restore. Settings that can change per session are set with ``SET`` in the
benchmark's sessions (and ``PGOPTIONS`` for pgbench); ``autovacuum`` and
``max_wal_size`` need ``ALTER SYSTEM`` + reload, ``shared_buffers`` needs
``ALTER SYSTEM`` + restart. ``default_statistics_target`` takes effect because
every benchmark ANALYZEs the static tables in its own session at the start.
At the end: ``ALTER SYSTEM RESET ALL``, restart, verify, re-ANALYZE.

This is NOT the fault-injection stage (T6); it only shows that the workload
can react, and how strongly, to guide T6's choice of bad values.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from pgdiag.config import Config, get_config
from pgdiag.settings import TARGET_SETTINGS
from pgdiag.workload import server
from pgdiag.workload.bench import BenchResult, compare, load_result, run_benchmark
from pgdiag.workload.queryset import component_settings


@dataclass(frozen=True)
class ProbeCase:
    setting: str
    bad_value: str
    method: str  # "session" (SET + PGOPTIONS), "reload" or "restart" (ALTER SYSTEM)


PROBE_CASES: tuple[ProbeCase, ...] = (
    ProbeCase("shared_buffers", "1MB", "restart"),
    ProbeCase("work_mem", "64kB", "session"),
    ProbeCase("effective_cache_size", "1MB", "session"),
    ProbeCase("random_page_cost", "100", "session"),
    ProbeCase("max_parallel_workers_per_gather", "0", "session"),
    ProbeCase("autovacuum", "off", "reload"),
    ProbeCase("max_wal_size", "32MB", "reload"),
    ProbeCase("default_statistics_target", "1", "session"),
    ProbeCase("jit_above_cost", "0", "session"),
    ProbeCase("maintenance_work_mem", "1MB", "session"),
)
assert [c.setting for c in PROBE_CASES] == list(TARGET_SETTINGS)


def parse_overrides(items: Sequence[str]) -> dict[str, str]:
    """``["work_mem=128kB", ...]`` -> ``{"work_mem": "128kB"}`` (names must be targets)."""
    out = {}
    for item in items:
        name, sep, value = item.partition("=")
        if not sep or name not in TARGET_SETTINGS:
            raise ValueError(f"expected <target_setting>=<value>, got {item!r}")
        out[name] = value
    return out


def run_probe(
    *,
    preset: str = "quick",
    settings: Sequence[str] | None = None,
    overrides: dict[str, str] | None = None,
    resume: str | Path | None = None,
    paired: bool = True,
    cfg: Config | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict:
    """Run the probe. The report is re-saved after every case; ``resume`` (a partial
    report file) skips the cases it already has.

    ``paired`` (default) runs a fresh baseline benchmark right before every broken
    one and compares the pair: host speed drifts by 10-20% over tens of minutes
    on this machine, which would otherwise be mistaken for (or hide) an effect.
    """
    cfg = cfg or get_config()
    say = progress or print
    wanted = list(settings) if settings else list(TARGET_SETTINGS)
    overrides = overrides or {}
    cases = [
        ProbeCase(c.setting, overrides.get(c.setting, c.bad_value), c.method)
        for c in PROBE_CASES
        if c.setting in wanted
    ]
    targets = component_settings()
    rows: list[dict] = []
    if resume:
        out = Path(resume)
        previous = json.loads(out.read_text())
        if previous["preset"] != preset:
            raise ValueError(f"{out} was made with preset {previous['preset']}")
        started = previous["started_at"]
        rows = previous["cases"]
        paired = previous.get("paired", paired)
        base = load_result(previous["baseline"]["path"]) if previous["baseline"] else None
        say(f"probe: resuming {out} ({len(rows)} cases done)")
    else:
        started = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        out = cfg.results_dir / "probe" / f"{started}_probe_{preset}.json"
    done = {r["setting"] for r in rows}

    def save() -> dict:
        report = {
            "started_at": started,
            "preset": preset,
            "paired": paired,
            "baseline": {"path": base.path, "total_median": base.total_median} if base else None,
            "cases": sorted(rows, key=lambda r: list(TARGET_SETTINGS).index(r["setting"])),
        }
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2) + "\n")
        report["path"] = str(out)
        return report

    say("probe: resetting to the baseline configuration")
    server.reset_to_baseline(cfg, progress=say)
    if not resume:
        base = None
        if not paired:
            say(f"probe: baseline run (preset {preset})")
            base = run_benchmark(preset, "probe-baseline", restart=False, cfg=cfg, progress=say)
        save()

    try:
        for case in cases:
            if case.setting in done:
                continue
            ref = base
            if paired:
                say(f"probe: baseline run for {case.setting} (preset {preset})")
                ref = run_benchmark(preset, f"probe-baseline-{case.setting}", cfg=cfg, progress=say)
            say(f"probe: {case.setting} = {case.bad_value} ({case.method})")
            session: dict[str, str] = {}
            if case.method == "session":
                session = {case.setting: case.bad_value}
            else:
                server.apply_change(cfg, case.setting, case.bad_value)
            try:
                res = run_benchmark(
                    preset,
                    f"probe-{case.setting}",
                    session_settings=session,
                    cfg=cfg,
                    progress=say,
                )
            finally:
                if case.method != "session":
                    server.apply_change(cfg, case.setting, None)
            rows.append(_row(case, ref, res, targets))
            save()
            say(format_row(rows[-1]))
    finally:
        say("probe: restoring the baseline configuration")
        server.reset_to_baseline(cfg, progress=say)

    return save()


def _row(case: ProbeCase, base: BenchResult, res: BenchResult, targets: dict) -> dict:
    diff = compare(base, res)
    designed = [c for c, names in targets.items() if case.setting in names]
    comps = diff["components"]
    worst = max(comps, key=lambda c: comps[c]["rel_diff_pct"])
    return {
        **asdict(case),
        "result_path": res.path,
        "baseline_path": base.path,
        "errors": res.errors,
        "total_baseline_s": diff["total"]["a"],
        "total_broken_s": diff["total"]["b"],
        "total_slowdown_pct": diff["total"]["rel_diff_pct"],
        "designed_components": {
            c: {
                "baseline_s": comps[c]["a"],
                "broken_s": comps[c]["b"],
                "slowdown_pct": comps[c]["rel_diff_pct"],
            }
            for c in designed
        },
        "largest_component": {
            "component": worst,
            "baseline_s": comps[worst]["a"],
            "broken_s": comps[worst]["b"],
            "slowdown_pct": comps[worst]["rel_diff_pct"],
        },
        "component_slowdown_pct": {c: comps[c]["rel_diff_pct"] for c in comps},
        "settings_seen": res.settings,
    }


def format_row(row: dict) -> str:
    designed = ", ".join(
        f"{c} {v['baseline_s']:.2f}->{v['broken_s']:.2f}s ({v['slowdown_pct']:+.0f}%)"
        for c, v in row["designed_components"].items()
    )
    err = f"  [{len(row['errors'])} errors]" if row["errors"] else ""
    return (
        f"{row['setting']:<32} {row['bad_value']:>6} {row['method']:<8} total "
        f"{row['total_baseline_s']:.1f}->{row['total_broken_s']:.1f}s "
        f"({row['total_slowdown_pct']:+.0f}%) | {designed}{err}"
    )


def format_table(report: dict) -> str:
    how = "paired baselines" if report.get("paired") else "one baseline"
    lines = [
        f"sensitivity probe, preset {report['preset']}, {how}",
        "| setting | bad value | applied via | total baseline -> broken | total | "
        "designed component(s) | largest component change |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in report["cases"]:
        designed = "; ".join(
            f"{c} {v['baseline_s']:.2f}->{v['broken_s']:.2f}s ({v['slowdown_pct']:+.0f}%)"
            for c, v in r["designed_components"].items()
        )
        lc = r["largest_component"]
        lines.append(
            f"| {r['setting']} | {r['bad_value']} | {r['method']} | "
            f"{r['total_baseline_s']:.1f} -> {r['total_broken_s']:.1f}s | "
            f"{r['total_slowdown_pct']:+.0f}% | {designed} | "
            f"{lc['component']} {lc['slowdown_pct']:+.0f}% |"
        )
    return "\n".join(lines)
