"""T10 fix tester: offline tests with a fake Ops (no database)."""

from __future__ import annotations

import json

import pytest

from pgdiag.faults.cases import get_case
from pgdiag.faults.inject import FixAttempt
from pgdiag.fixtest import cli, core

CASE = get_case("c02_work_mem")


class FakeOps:
    """Records calls; ``times`` maps bench label suffix -> median; ``invalid`` settings are
    rejected; ``boom`` settings make the benchmark raise; ``undo_breaks`` makes verify fail
    once after undo."""

    def __init__(self, times, invalid=(), boom=(), undo_breaks=False, applied=True, crash=None):
        self.times, self.invalid, self.boom = times, set(invalid), set(boom)
        self.crash = dict(crash or {})  # setting -> how many benchmarks lose the connection
        self.undo_breaks, self.applied = undo_breaks, applied
        self.calls: list[tuple] = []
        self.current: str | None = None  # setting of the active fix

    def apply_case(self, case):
        self.calls.append(("apply", case.case_id))
        self.applied = True

    def reset(self):
        self.calls.append(("reset",))
        self.applied, self.current = False, None

    def try_fix(self, setting, value):
        self.calls.append(("try_fix", setting, value))
        if setting in self.invalid:
            return FixAttempt(setting, value, False, "bad value", "invalid")
        self.current = setting
        return FixAttempt(setting, value, True, change={"method": "reload"})

    def undo(self):
        self.calls.append(("undo",))
        self.current = None

    def verify(self, case):
        self.calls.append(("verify",))
        if self.undo_breaks and self.calls[-2:-1] == [("undo",)]:
            self.undo_breaks = False
            return ["work_mem is 8MB, expected 64kB"]
        return [] if self.applied else ["not applied"]

    def wait_ready(self):
        self.calls.append(("wait",))

    def bench(self, preset, label):
        self.calls.append(("bench", label))
        if self.crash.get(self.current):
            import psycopg

            self.crash[self.current] -= 1
            raise psycopg.OperationalError("the connection is closed")
        if self.current in self.boom:
            raise RuntimeError("server closed the connection unexpectedly")
        key = self.current or label.rsplit("-", 1)[-1]
        return {"median_s": self.times[key], "components": {"x": 1.0}, "errors": []}


TIMES = {"before": 6.0, "after": 7.0, "broken": 16.5, "work_mem": 6.8, "jit": 16.0}


def run(ops, fixes):
    return core.test_fixes(CASE, fixes, ops=ops)


def labels(ops):
    return [c[0] if c[0] != "bench" else "bench:" + c[1].rsplit("-", 1)[-1] for c in ops.calls]


def test_order_undo_after_every_fix_and_metrics():
    ops = FakeOps(TIMES)
    out = run(
        ops,
        [
            {"rank": 2, "setting": "jit", "proposed_value": "off"},
            {"rank": 1, "setting": "work_mem", "proposed_value": "8MB"},
        ],
    )
    assert labels(ops) == [
        "verify",
        "reset",
        "bench:before",
        "apply",
        "bench:broken",
        "try_fix",
        "bench:fix1",
        "undo",
        "verify",
        "try_fix",
        "bench:fix2",
        "undo",
        "verify",
        "reset",
        "bench:after",
    ]
    assert [r["rank"] for r in out["fixes"]] == [1, 2]
    assert out["ref_s"] == 6.5
    wm, jit = out["fixes"]
    assert wm["status"] == "applied" and wm["method"] == "reload"
    assert wm["vs_ref_pct"] == pytest.approx(4.62, abs=0.01)
    assert wm["recovery_pct"] == pytest.approx(97.0)
    assert wm["within_10pct"] and not jit["within_10pct"]
    assert jit["recovery_pct"] == 5.0
    assert out["best"]["rank"] == 1 and out["broken_vs_ref_pct"] == pytest.approx(153.85)
    json.dumps(out)
    assert "best: rank 1 work_mem = 8MB" in core.format_table(out)


def test_rejected_fix_not_benchmarked():
    ops = FakeOps(TIMES, invalid={"work_mem"})
    out = run(ops, [{"setting": "work_mem", "proposed_value": "lots"}])
    (rec,) = out["fixes"]
    assert rec["status"] == "rejected" and "invalid" in rec["error"]
    assert not any(c[0] == "undo" or c == ("bench", "c02_work_mem-fix1") for c in ops.calls)
    assert out["best"] is None


def test_benchmark_error_is_failed_fix_and_server_recovers():
    ops = FakeOps(TIMES, boom={"work_mem"}, undo_breaks=True)
    out = run(
        ops,
        [
            {"setting": "work_mem", "proposed_value": "1GB"},
            {"setting": "jit", "proposed_value": "off"},
        ],
    )
    bad, jit = out["fixes"]
    assert bad["status"] == "failed" and "server closed" in bad["error"]
    assert bad["recovery"] == ["work_mem is 8MB, expected 64kB"]
    i = ops.calls.index(("undo",))
    assert ops.calls[i + 1 : i + 4] == [("verify",), ("reset",), ("apply", CASE.case_id)]
    assert jit["status"] == "applied" and out["best"]["setting"] == "jit"


def test_case_must_be_applied():
    ops = FakeOps(TIMES, applied=False)
    with pytest.raises(core.FixTestError):
        run(ops, [{"setting": "jit", "proposed_value": "off"}])
    assert ops.calls == [("verify",)]


def test_reset_in_finally_on_unexpected_error():
    ops = FakeOps(TIMES)

    def apply_case(case):
        raise KeyboardInterrupt

    ops.apply_case = apply_case
    with pytest.raises(KeyboardInterrupt):
        run(ops, [{"setting": "jit", "proposed_value": "off"}])
    assert ops.calls[-1] == ("reset",)


def test_load_fixes_accepts_record_or_list(tmp_path):
    rec = tmp_path / "d.json"
    rec.write_text(json.dumps({"fixes": [{"rank": 1, "setting": "a", "proposed_value": "1"}]}))
    lst = tmp_path / "l.json"
    lst.write_text(json.dumps([{"setting": "a", "proposed_value": "1", "reasoning": "x"}]))
    assert cli.load_fixes(rec) == [{"rank": 1, "setting": "a", "proposed_value": "1"}]
    assert cli.load_fixes(lst) == [{"setting": "a", "proposed_value": "1"}]


def test_crashed_fix_benchmark_is_retried_once():
    ops = FakeOps(TIMES, crash={"jit": 1})
    out = run(ops, [{"setting": "jit", "proposed_value": "off"}])
    rec = out["fixes"][0]
    assert rec["status"] == "applied" and rec["median_s"] == 16.0 and rec["error"] is None
    assert rec["crash_retries"] == 1
    assert labels(ops).count("wait") == 1


def test_fix_benchmark_crashing_twice_is_failed():
    ops = FakeOps(TIMES, crash={"jit": 2})
    rec = run(ops, [{"setting": "jit", "proposed_value": "off"}])["fixes"][0]
    assert rec["status"] == "failed" and "OperationalError" in rec["error"]
    assert labels(ops).count("bench:fix1") == 2
    assert sum(1 for c in ops.calls if c == ("wait",)) == 1


class CrashInErrors(FakeOps):
    """The benchmark survives a crash but records it as per-query errors (``n`` times)."""

    def __init__(self, *a, n=1, where="jit", **kw):
        super().__init__(*a, **kw)
        self.n, self.where = n, where

    def bench(self, preset, label):
        b = super().bench(preset, label)
        hit = self.current == self.where or label.endswith(self.where)
        if self.n and hit:
            self.n -= 1
            return {**b, "errors": ["pass 2: bought_together: OperationalError: server closed"]}
        return b


def test_crash_reported_as_benchmark_errors_is_retried():
    ops = CrashInErrors(TIMES, n=1)
    rec = run(ops, [{"setting": "jit", "proposed_value": "off"}])["fixes"][0]
    assert rec["status"] == "applied" and rec["crash_retries"] == 1 and rec["error"] is None


def test_crash_in_reference_benchmark_raises_server_crashed():
    from pgdiag.db import ServerCrashed

    ops = CrashInErrors(TIMES, n=1, where="broken")
    with pytest.raises(ServerCrashed):
        run(ops, [{"setting": "jit", "proposed_value": "off"}])


def test_errors_show_crash():
    from pgdiag.db import errors_show_crash

    assert errors_show_crash(["pass 1: q: OperationalError: the connection is closed"])
    assert errors_show_crash(["pgbench: server closed the connection unexpectedly"])
    assert not errors_show_crash(["pass 1: q: QueryCanceled: canceling statement due to timeout"])
    assert not errors_show_crash([])
