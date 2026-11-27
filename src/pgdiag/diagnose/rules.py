"""Rules-based baseline diagnosis: fixed rules of thumb, no LLM.

The comparison method for the LLM modes. It reads the same symptom document and returns
the same record shape (4 ranked ``fixes`` with ``setting``/``proposed_value``/
``reasoning``), so the experiment tests and scores it exactly like ``rag``/``norag``.

Two kinds of rules, in the spirit of classic tuning helpers (PGTune's sizing formulas,
postgresqltuner/pgconfig-style checks):

1. **Configuration checks**: each setting in the document's ``config`` section is compared
   with a generic recommendation for the host (memory limit and CPU count from
   ``environment``). A value far from it is flagged; the further away, the higher the
   score. The thresholds are general rules of thumb, not tuned to the fault cases.
2. **Symptom checks**: statistics that point at a setting (temporary files -> work_mem,
   low buffer hit ratio -> shared_buffers, JIT time share -> jit_above_cost, large row
   estimate errors -> default_statistics_target, no parallel workers in the plans of big
   scans -> max_parallel_workers_per_gather) add to that setting's score.

The four highest scores are the answer; if fewer than four settings are flagged, general
recommendations with a low score fill the list.
"""

from __future__ import annotations

import math
import re

from pgdiag.diagnose.core import N_FIXES

RULESET = "rules-v1"
KB = 1024
MB = 1024 * KB
GB = 1024 * MB

_MEM_UNITS = {"b": 1, "kb": KB, "mb": MB, "gb": GB, "tb": 1024 * GB}
_NUM_RE = re.compile(r"^\s*(-?[\d.]+)\s*([a-zA-Z]*)\s*$")


def parse_bytes(value: str) -> float | None:
    m = _NUM_RE.match(str(value))
    if not m:
        return None
    unit = m.group(2).lower()
    if unit and unit not in _MEM_UNITS:
        return None
    return float(m.group(1)) * _MEM_UNITS.get(unit, KB)  # bare memory numbers: kB


def parse_number(value: str) -> float | None:
    m = _NUM_RE.match(str(value))
    return float(m.group(1)) if m and not m.group(2) else None


def fmt_mb(n_bytes: float) -> str:
    return f"{max(1, round(n_bytes / MB))}MB"


class _Findings:
    def __init__(self) -> None:
        self.score: dict[str, float] = {}
        self.value: dict[str, str] = {}
        self.reasons: dict[str, list[str]] = {}
        self.order: list[str] = []

    def add(self, setting: str, score: float, value: str | None, reason: str) -> None:
        if setting not in self.score:
            self.order.append(setting)
            self.score[setting] = 0.0
            self.reasons[setting] = []
        self.score[setting] += score
        if value is not None and setting not in self.value:
            self.value[setting] = value
        self.reasons[setting].append(reason)


def _config(doc: dict) -> dict[str, str]:
    return {p["name"]: str(p["value"]) for p in doc.get("config", [])}


def config_rules(doc: dict, f: _Findings) -> None:
    cfg = _config(doc)
    env = doc.get("environment", {})
    ram = float(env.get("memory_limit_mb") or 1024) * MB
    cpus = int(env.get("cpu_count") or 1)

    def mem(name):
        return parse_bytes(cfg[name]) if name in cfg else None

    def num(name):
        return parse_number(cfg[name]) if name in cfg else None

    sb = mem("shared_buffers")
    if sb is not None and (sb < 0.10 * ram or sb > 0.50 * ram):
        rec = 0.25 * ram
        f.add(
            "shared_buffers",
            1 + abs(math.log10(rec / sb)),
            fmt_mb(rec),
            f"shared_buffers {cfg['shared_buffers']} is far from ~25% of {fmt_mb(ram)} RAM",
        )

    ecs = mem("effective_cache_size")
    if ecs is not None and (ecs < 0.5 * ram or ecs > ram):
        rec = 0.75 * ram
        f.add(
            "effective_cache_size",
            0.3 + 0.5 * abs(math.log10(rec / ecs)),
            fmt_mb(rec),
            f"effective_cache_size {cfg['effective_cache_size']} vs ~75% of RAM",
        )

    wm = mem("work_mem")
    if wm is not None and wm < 1 * MB:
        f.add(
            "work_mem",
            1 + math.log10(MB / wm),
            "4MB",
            f"work_mem {cfg['work_mem']} is below 1MB: sorts and hashes spill to disk",
        )
    elif wm is not None and wm > ram / 4:
        f.add("work_mem", 0.8, "4MB", f"work_mem {cfg['work_mem']} risks running out of memory")

    mwm = mem("maintenance_work_mem")
    if mwm is not None and mwm < 16 * MB:
        f.add("maintenance_work_mem", 0.3, "64MB", "maintenance_work_mem below 16MB")

    hmm = num("hash_mem_multiplier")
    if hmm is not None and hmm < 1:
        f.add("hash_mem_multiplier", 0.8, "2", "hash_mem_multiplier below 1")

    spc = num("seq_page_cost") or 1.0
    rpc = num("random_page_cost")
    if rpc is not None and rpc > 4 * spc:
        f.add(
            "random_page_cost",
            1 + math.log10(rpc / (4 * spc)),
            "1.1",
            f"random_page_cost {cfg['random_page_cost']} is above the HDD default 4; "
            "index scans look too expensive (SSD storage: ~1.1)",
        )
    elif rpc is not None and rpc < spc:
        f.add("random_page_cost", 0.8, "1.1", "random_page_cost below seq_page_cost")

    defaults = {"cpu_tuple_cost": 0.01, "cpu_index_tuple_cost": 0.005,
                "cpu_operator_cost": 0.0025, "parallel_setup_cost": 1000,
                "parallel_tuple_cost": 0.1}  # fmt: skip
    for name, d in defaults.items():
        v = num(name)
        if v is not None and v > 0 and abs(math.log10(v / d)) > 1:
            f.add(
                name,
                1 + abs(math.log10(v / d)) - 1,
                f"{d:g}",
                f"{name} {cfg[name]} is over 10x from its default {d:g}",
            )

    eic = num("effective_io_concurrency")
    if eic is not None and eic <= 1:
        f.add(
            "effective_io_concurrency",
            0.2,
            "200",
            "effective_io_concurrency <= 1: SSD storage can serve ~200 concurrent reads",
        )

    pw = num("max_parallel_workers_per_gather")
    if pw is not None and pw == 0 and cpus >= 4:
        f.add(
            "max_parallel_workers_per_gather",
            1.0,
            str(min(4, cpus // 2)),
            f"parallel query is off on a {cpus}-CPU host",
        )

    jit_on = cfg.get("jit", "on").lower() in ("on", "true", "1")
    jac = num("jit_above_cost")
    if jit_on and jac is not None and 0 <= jac < 10000:
        f.add(
            "jit_above_cost",
            1 + min(2.0, math.log10(10000 / max(jac, 1))),
            "100000",
            f"jit_above_cost {cfg['jit_above_cost']}: cheap queries pay for JIT compilation",
        )

    dst = num("default_statistics_target")
    if dst is not None and dst < 100:
        f.add(
            "default_statistics_target",
            1 + 0.5 * math.log10(100 / max(dst, 1)),
            "100",
            f"default_statistics_target {cfg['default_statistics_target']} is below the "
            "default 100: coarse statistics, poor row estimates",
        )

    for name, v in cfg.items():
        if name.startswith("enable_") and v.lower() in ("off", "false", "0"):
            f.add(name, 1.5, "on", f"{name} is off: the planner cannot use that plan type")

    for name in ("join_collapse_limit", "from_collapse_limit"):
        v = num(name)
        if v is not None and v < 8:
            f.add(name, 0.8, "8", f"{name} below the default 8")

    cct = num("checkpoint_completion_target")
    if cct is not None and cct < 0.7:
        f.add("checkpoint_completion_target", 0.3, "0.9", "checkpoint_completion_target < 0.7")

    mws = mem("max_wal_size")
    if mws is not None and mws < 1 * GB:
        f.add("max_wal_size", 0.3, "1GB", "max_wal_size below 1GB: frequent checkpoints")

    if cfg.get("autovacuum", "on").lower() in ("off", "false", "0"):
        f.add("autovacuum", 1.0, "on", "autovacuum is off")


_EST_RE = re.compile(r"rows=(\d+) width=\d+\) \(actual time=[\d.]+\.\.[\d.]+ rows=(\d+) loops=\d+")


def symptom_rules(doc: dict, f: _Findings) -> None:
    cfg = _config(doc)
    env = doc.get("environment", {})
    ram = float(env.get("memory_limit_mb") or 1024) * MB
    cpus = int(env.get("cpu_count") or 1)
    ds = doc.get("database_stats", {})

    temp_files = ds.get("temp_files") or 0
    temp_bytes = ds.get("temp_bytes") or 0
    if temp_files >= 1000 or temp_bytes >= 100 * MB:
        wm = parse_bytes(cfg.get("work_mem", "4MB")) or 4 * MB
        f.add(
            "work_mem",
            1.0,
            fmt_mb(min(max(4 * MB, 4 * wm), ram / 8)),
            f"{temp_files} temporary files ({temp_bytes / MB:.0f} MB) written",
        )

    hit = ds.get("hit_pct")
    if hit is not None and hit < 90:
        f.add(
            "shared_buffers",
            1.0 * (90 - hit) / 40,
            fmt_mb(0.25 * ram),
            f"buffer cache hit ratio {hit}% is low",
        )

    statements = doc.get("top_statements", [])
    total = sum(s.get("total_ms") or 0 for s in statements)
    jit = sum(s.get("jit_ms") or 0 for s in statements)
    if total and jit / total > 0.20:
        f.add(
            "jit_above_cost",
            1.0,
            "100000",
            f"JIT compilation is {100 * jit / total:.0f}% of the top statements' time",
        )

    plans = [e.get("plan", "") for e in doc.get("explain", [])]
    worst = 0.0
    for p in plans:
        for est, act in _EST_RE.findall(p):
            a, b = int(est), int(act)
            if min(a, b) > 0:
                worst = max(worst, max(a, b) / min(a, b))
    if worst >= 100:
        dst = parse_number(cfg.get("default_statistics_target", "100")) or 100
        f.add(
            "default_statistics_target",
            0.5,
            str(int(max(100, 4 * dst))),
            f"row estimates off by up to {worst:.0f}x in EXPLAIN ANALYZE",
        )

    big_scan = any(("Seq Scan" in p or "Aggregate" in p) for p in plans)
    if plans and cpus >= 4 and big_scan and not any("Workers Launched" in p for p in plans):
        f.add(
            "max_parallel_workers_per_gather",
            1.0,
            str(min(4, cpus // 2)),
            "no parallel workers in any plan, although large scans/aggregates run",
        )


FALLBACK = (
    ("effective_io_concurrency", "200", "general SSD recommendation"),
    ("effective_cache_size", None, "general recommendation: ~75% of RAM"),
    ("maintenance_work_mem", "64MB", "general recommendation"),
    ("checkpoint_completion_target", "0.9", "general recommendation"),
    ("max_wal_size", "2GB", "general recommendation"),
)


def rank(doc: dict) -> list[dict]:
    """The rules' four fixes, highest score first."""
    f = _Findings()
    config_rules(doc, f)
    symptom_rules(doc, f)
    order = sorted(f.order, key=lambda s: (-f.score[s], f.order.index(s)))
    ram = float(doc.get("environment", {}).get("memory_limit_mb") or 1024) * MB
    for name, value, why in FALLBACK:
        if len(order) >= N_FIXES:
            break
        if name not in f.score:
            f.add(name, 0.0, value or fmt_mb(0.75 * ram), why)
            order.append(name)
    return [
        {
            "rank": i,
            "setting": s,
            "proposed_value": f.value[s],
            "reasoning": f"score {f.score[s]:.2f}: " + "; ".join(f.reasons[s]),
            "cited_chunks": [],
        }
        for i, s in enumerate(order[:N_FIXES], start=1)
    ]


def diagnose(doc: dict) -> dict:
    """A diagnosis record in the LLM modes' shape (mode ``rules``, no API call)."""
    return {
        "mode": "rules",
        "model": RULESET,
        "fixes": rank(doc),
        "dropped_citations": [],
        "attempts": 1,
        "errors": [],
        "chunk_ids": [],
        "responses": [],
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
