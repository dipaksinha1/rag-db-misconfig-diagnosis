"""Experiment runner (T11): cases x modes x repeats, resumable.

One *unit* is one (repeat, case). Units run repeat-major (all six cases of repeat 1, then
repeat 2, ...), so a partial run is still balanced across cases. Per unit::

    apply_case(case)                     the server starts from the baseline (RESET ALL inside)
    collect()  -> symptoms.json          ONE document, shared by both modes (leak-checked)
    per mode   -> diagnosis.<mode>.json  rag: retrieve(doc) + diagnose; norag: diagnose
    test_fixes(new fixes) -> fixtest.json   only fixes not measured before for this case
    -> one row per mode in results.jsonl

Both modes see the same symptom document, so the only difference between them is the
docs block (T9). Fix results are cached per (case, setting, normalised value) in
``fixcache.json``: an LLM repeats itself across repeats, and each new fix costs ~1 min of
benchmark. A cached fix keeps the metrics (vs_ref_pct, recovery_pct, within_10pct) measured
against the bracketing references of the test that measured it.

Resume: every file is written once (atomically) and reused when present; a unit is done when
results.jsonl has a row for each of its modes. A failed diagnosis (``DiagnosisError``) leaves
``diagnosis.<mode>.error.json`` and the unit unfinished, so the next run retries it.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from pgdiag.config import Config, get_config
from pgdiag.faults.cases import FaultCase, get_case, load_cases

MODES = ("rag", "norag")
DEFAULT_REPEATS = 5
DEFAULT_PRESET = "quick"
DEFAULT_NAME = "main"
SCHEMA_VERSION = 1
# These must match on resume: mixing them inside one experiment would confound the comparison.
MANIFEST_FIXED = ("model", "preset", "retrieval_k")
# A bad setting (e.g. work_mem = 64kB) can make one query's hash-join batch buffers outgrow
# the 256 MB container; the kernel kills that backend and PostgreSQL restarts itself, with
# the broken setting still applied. Collection and fix tests are then redone from scratch.
DB_CRASH_RETRIES = 2


def is_db_crash(exc: BaseException | None) -> bool:
    from pgdiag.db import is_connection_lost

    return is_connection_lost(exc)


class ExperimentError(RuntimeError):
    pass


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json(path: Path, obj) -> None:
    """Write atomically (temp file + rename), so an interrupted run never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1) + "\n")
    os.replace(tmp, path)


def read_json(path: Path):
    return json.loads(path.read_text())


def text_key(setting: str, value) -> str:
    """Fallback fix key: cleaned name and value text (``"8MB"`` != ``"8192kB"`` here)."""
    from pgdiag.faults.inject import clean_name, clean_value

    return f"{clean_name(setting)}={clean_value(value).lower().replace(' ', '')}"


class Steps:
    """The real actions of a unit (tests swap in a fake with the same methods)."""

    def __init__(self, cfg: Config | None = None, progress: Callable[[str], None] | None = None):
        self.cfg = cfg or get_config()
        self.say = progress or (lambda m: None)
        self._index = None
        self._client = None
        self._srv = None

    # --- run settings -------------------------------------------------------------------
    def model(self) -> str:
        return self.cfg.anthropic_model or ""

    def check_ready(self) -> list[str]:
        problems = []
        if not self.cfg.llm_provider:
            problems.append(
                "OPENROUTER_API_KEY (or ANTHROPIC_API_KEY) is not set (.env or environment)"
            )
        if not self.cfg.anthropic_model:
            problems.append("ANTHROPIC_MODEL is not set (.env or environment)")
        return problems

    # --- database -----------------------------------------------------------------------
    def apply_case(self, case: FaultCase) -> None:
        from pgdiag.faults import inject

        inject.apply_case(case, cfg=self.cfg)

    def reset(self) -> None:
        from pgdiag.fixtest.core import Ops

        Ops(self.cfg).reset()

    def verify(self, case: FaultCase | None) -> list[str]:
        from pgdiag.faults import inject

        return inject.verify(case, cfg=self.cfg)

    def _after_crash(self, what: str, exc: BaseException, attempt: int) -> None:
        from pgdiag.db import wait_until_ready

        self.say(
            f"{what}: the database connection was lost ({type(exc).__name__}: "
            f"{str(exc).strip()[:120]}); waiting for the server, then retry "
            f"{attempt}/{DB_CRASH_RETRIES}"
        )
        self._srv = None
        wait_until_ready(self.cfg, timeout=120.0)

    def collect(self, case: FaultCase | None = None) -> dict:
        """Symptom document; redone after a server crash (the case survives the restart)."""
        from pgdiag.collect import symptoms

        for attempt in range(DB_CRASH_RETRIES + 1):
            try:
                return symptoms.collect(self.cfg)
            except Exception as exc:
                if not is_db_crash(exc) or attempt == DB_CRASH_RETRIES:
                    raise
                self._after_crash("collect", exc, attempt + 1)
                if case is not None and self.verify(case):
                    self.apply_case(case)
        raise AssertionError("unreachable")

    def test_fixes(self, case: FaultCase, fixes: list[dict], preset: str) -> dict:
        """Fix-test result; after a server crash the case is re-applied and the test redone."""
        from pgdiag.fixtest.core import Ops, test_fixes

        for attempt in range(DB_CRASH_RETRIES + 1):
            ops = Ops(self.cfg, progress=self.say)
            try:
                return test_fixes(case, fixes, preset=preset, ops=ops, progress=self.say)
            except Exception as exc:
                crashed = is_db_crash(exc) or is_db_crash(exc.__context__ or exc)
                if not crashed or attempt == DB_CRASH_RETRIES:
                    raise
                self._after_crash("fix test", exc, attempt + 1)
                self.reset()
                self.apply_case(case)
        raise AssertionError("unreachable")

    def fix_key(self, setting: str, value) -> str:
        """``name=normalised value`` using pg_settings units (``8MB`` == ``8192kB``), else text."""
        from pgdiag.faults import inject, units

        name = inject.clean_name(setting)
        try:
            if self._srv is None:
                self._srv = inject.PgServer(self.cfg)
            info = self._srv.info(name)
            norm = units.normalize(inject.clean_value(value), info)
        except Exception:  # unknown setting, invalid value, ...: the fix tester will reject it
            return text_key(setting, value)
        if isinstance(norm, float):
            norm = units.format_number(norm)
        return f"{info.name.lower()}={norm}"

    # --- retrieval and diagnosis --------------------------------------------------------
    def retrieve(self, doc: dict, k: int) -> list[dict]:
        from pgdiag.index import get_index
        from pgdiag.retrieve import retrieve

        if self._index is None:
            self._index = get_index()
        return [h.to_dict() for h in retrieve(doc, k=k, index=self._index)]

    def diagnose(self, doc: dict, mode: str, chunks: list[dict] | None) -> dict:
        from pgdiag.diagnose.core import diagnose, make_client

        if self._client is None:
            self._client = make_client()
        return diagnose(doc, mode, chunks, client=self._client, model=self.model())

    def leak_problems(self, doc: dict) -> list[str]:
        from pgdiag.collect import leaks

        return leaks.check_text(doc)


def git_commit(root: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True
        )
    except OSError:
        return None
    return out.stdout.strip() or None


# --- results file ------------------------------------------------------------------------


def load_rows(path: Path) -> list[dict]:
    """Rows of results.jsonl; a truncated last line (interrupted append) is ignored."""
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def append_row(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(row, sort_keys=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def score(case: FaultCase, fixes: list[dict], outcomes: list[dict]) -> dict:
    """Per-run summary used by T12 (scoring by setting name and by measured effect)."""
    correct = [f["rank"] for f in fixes if f["setting"].strip().lower() == case.setting]
    working = [f["rank"] for f, o in zip(fixes, outcomes, strict=True) if o.get("within_10pct")]
    applied = [o for o in outcomes if o.get("status") == "applied"]
    best = min(applied, key=lambda o: o["median_s"], default=None)
    return {
        "correct_rank": correct[0] if correct else None,  # rank naming the broken setting
        "top1_correct": bool(correct) and correct[0] == 1,
        "correct_in_top4": bool(correct),
        "first_working_rank": working[0] if working else None,  # first fix within 10% of ref
        "top1_works": bool(working) and working[0] == 1,
        "any_works": bool(working),
        "n_rejected": sum(o.get("status") == "rejected" for o in outcomes),
        "n_failed": sum(o.get("status") == "failed" for o in outcomes),
        "best_vs_ref_pct": best["vs_ref_pct"] if best else None,
    }


# --- the runner --------------------------------------------------------------------------


class Experiment:
    def __init__(
        self,
        out_dir: Path,
        *,
        cases: Sequence[FaultCase] | None = None,
        repeats: int = DEFAULT_REPEATS,
        modes: Sequence[str] = MODES,
        preset: str = DEFAULT_PRESET,
        retrieval_k: int | None = None,
        steps: Steps | None = None,
        progress: Callable[[str], None] | None = None,
    ):
        from pgdiag.retrieve.core import DEFAULT_K

        bad = [m for m in modes if m not in MODES]
        if bad or not modes:
            raise ValueError(f"modes must be a non-empty subset of {MODES}, got {list(modes)}")
        if repeats < 1:
            raise ValueError("repeats must be >= 1")
        self.out_dir = out_dir
        self.cases = list(cases or load_cases())
        self.repeats = repeats
        self.modes = list(modes)
        self.preset = preset
        self.retrieval_k = retrieval_k or DEFAULT_K
        self.say = progress or (lambda m: None)
        self.steps = steps or Steps(progress=self.say)
        self.rows_path = out_dir / "results.jsonl"
        self.cache_path = out_dir / "fixcache.json"

    # --- bookkeeping ---
    def unit_dir(self, case: FaultCase, repeat: int) -> Path:
        return self.out_dir / "runs" / case.case_id / f"r{repeat}"

    def units(self) -> list[tuple[int, FaultCase]]:
        return [(r, c) for r in range(1, self.repeats + 1) for c in self.cases]

    def done_keys(self) -> set[tuple[str, int, str]]:
        return {(r["case_id"], r["repeat"], r["mode"]) for r in load_rows(self.rows_path)}

    def unit_done(self, case: FaultCase, repeat: int, done: set | None = None) -> bool:
        done = self.done_keys() if done is None else done
        return all((case.case_id, repeat, m) in done for m in self.modes)

    def load_cache(self) -> dict:
        return read_json(self.cache_path) if self.cache_path.exists() else {}

    def manifest(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "model": self.steps.model(),
            "preset": self.preset,
            "retrieval_k": self.retrieval_k,
            "modes": self.modes,
            "repeats": self.repeats,
            "cases": [c.case_id for c in self.cases],
        }

    def check_manifest(self) -> None:
        """Create manifest.json, or check that a resumed run uses the same fixed settings."""
        path = self.out_dir / "manifest.json"
        now = self.manifest()
        if not path.exists():
            now.update(created_at=_now(), git_commit=git_commit(get_config().root))
            write_json(path, now)
            return
        old = read_json(path)
        diff = [
            f"{k}: {old.get(k)!r} -> {now[k]!r}" for k in MANIFEST_FIXED if old.get(k) != now[k]
        ]
        if diff:
            raise ExperimentError(
                f"{self.out_dir} was started with different settings ({'; '.join(diff)}); "
                "use another --name"
            )
        # cases / repeats / modes may grow on resume; remember the union.
        merged = dict(old)
        merged["repeats"] = max(old.get("repeats", 0), self.repeats)
        merged["cases"] = list(dict.fromkeys([*old.get("cases", []), *now["cases"]]))
        merged["modes"] = list(dict.fromkeys([*old.get("modes", []), *now["modes"]]))
        if merged != old:
            write_json(path, merged)

    # --- one unit ---
    def _symptoms(self, case: FaultCase, udir: Path) -> dict:
        path = udir / "symptoms.json"
        if path.exists():
            return read_json(path)
        self.say(f"[{case.case_id}] apply case + collect symptoms")
        self.steps.apply_case(case)
        doc = self.steps.collect(case)
        problems = self.steps.leak_problems(doc)
        if problems:
            raise ExperimentError(f"symptom document leaks the answer: {problems[:3]}")
        write_json(path, doc)
        return doc

    def _diagnosis(self, case: FaultCase, udir: Path, mode: str, doc: dict) -> dict | None:
        from pgdiag.diagnose.core import DiagnosisError
        from pgdiag.llm import LLMFatalError

        path = udir / f"diagnosis.{mode}.json"
        if path.exists():
            return read_json(path)
        chunks = None
        if mode == "rag":
            chunks = self.steps.retrieve(doc, self.retrieval_k)
        self.say(f"[{case.case_id}] diagnose ({mode})")
        try:
            record = self.steps.diagnose(doc, mode, chunks)
        except LLMFatalError as exc:  # bad key / no credit: every later unit would fail too
            raise ExperimentError(f"LLM API refused the request, stopping: {exc}") from exc
        except DiagnosisError as exc:
            write_json(
                udir / f"diagnosis.{mode}.error.json",
                {"mode": mode, "error": str(exc), "at": _now()},
            )
            self.say(f"[{case.case_id}] diagnosis ({mode}) failed: {exc}")
            return None
        if chunks is not None:
            record["retrieved"] = [
                {k: c[k] for k in ("chunk_id", "source", "title", "query", "rank") if k in c}
                for c in chunks
            ]
        write_json(path, record)
        (udir / f"diagnosis.{mode}.error.json").unlink(missing_ok=True)
        return record

    def _test_new_fixes(
        self, case: FaultCase, udir: Path, keyed: list[tuple[str, dict]], cache: dict
    ) -> None:
        """Measure fixes whose key is not cached yet (one test_fixes call: one set of refs)."""
        todo: dict[str, dict] = {}
        for key, fix in keyed:
            ckey = f"{case.case_id}|{key}"
            if ckey not in cache and ckey not in todo:
                todo[ckey] = fix
        if not todo:
            self.say(f"[{case.case_id}] all {len(keyed)} fixes already measured (cache)")
            return
        fixes = [
            {"rank": i, "setting": f["setting"], "proposed_value": f["proposed_value"]}
            for i, f in enumerate(todo.values(), start=1)
        ]
        self.say(f"[{case.case_id}] fix test: {len(fixes)} new of {len(keyed)}")
        if self.steps.verify(case):
            self.steps.apply_case(case)
        result = self.steps.test_fixes(case, fixes, self.preset)
        n = len(list(udir.glob("fixtest*.json")))  # >0 only if a mode was retried later
        path = udir / ("fixtest.json" if n == 0 else f"fixtest.{n + 1}.json")
        write_json(path, result)
        source = str(path.relative_to(self.out_dir))
        by_rank = {r["rank"]: r for r in result["fixes"]}
        for i, ckey in enumerate(todo, start=1):
            rec = dict(by_rank[i])
            rec.pop("rank", None)
            rec.pop("bench", None)
            rec.update(source=source, ref_s=result["ref_s"], broken_s=result["broken"]["median_s"])
            cache[ckey] = rec
        write_json(self.cache_path, cache)

    def run_unit(self, case: FaultCase, repeat: int, done: set) -> bool:
        """Run (or finish) one unit; True if every mode now has its row."""
        udir = self.unit_dir(case, repeat)
        t0 = time.monotonic()
        doc = self._symptoms(case, udir)
        # alternate the order so neither mode is always first after the collection
        order = self.modes if repeat % 2 else list(reversed(self.modes))
        records = {m: self._diagnosis(case, udir, m, doc) for m in order}
        records = {m: r for m, r in records.items() if r is not None}
        keyed = {
            m: [(self.steps.fix_key(f["setting"], f["proposed_value"]), f) for f in r["fixes"]]
            for m, r in records.items()
        }
        cache = self.load_cache()
        self._test_new_fixes(case, udir, [kf for m in keyed for kf in keyed[m]], cache)
        for mode in self.modes:
            if mode not in records or (case.case_id, repeat, mode) in done:
                continue
            row = self._row(case, repeat, mode, records[mode], keyed[mode], cache, udir)
            append_row(self.rows_path, row)
            done.add((case.case_id, repeat, mode))
        self.say(f"[{case.case_id}] r{repeat} unit wall {time.monotonic() - t0:.0f}s")
        return self.unit_done(case, repeat, done)

    def _row(self, case, repeat, mode, record, keyed, cache, udir) -> dict:
        fixes, outcomes = [], []
        for key, f in keyed:
            o = cache[f"{case.case_id}|{key}"]
            outcome = {
                k: o.get(k)
                for k in (
                    "status", "method", "median_s", "vs_ref_pct", "recovery_pct",
                    "within_10pct", "error", "source",
                )
            }  # fmt: skip
            outcomes.append(outcome)
            fixes.append(
                {
                    "rank": f["rank"],
                    "setting": f["setting"],
                    "proposed_value": f["proposed_value"],
                    "key": key,
                    "cited_chunks": f.get("cited_chunks", []),
                    "outcome": outcome,
                }
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "case_id": case.case_id,
            "setting": case.setting,
            "mode": mode,
            "repeat": repeat,
            "model": record["model"],
            "fixes": fixes,
            "score": score(case, fixes, outcomes),
            "attempts": record["attempts"],
            "usage": record["usage"],
            "n_chunks": len(record.get("chunk_ids", [])),
            "dropped_citations": record.get("dropped_citations", []),
            "symptoms": str((udir / "symptoms.json").relative_to(self.out_dir)),
            "diagnosis": str((udir / f"diagnosis.{mode}.json").relative_to(self.out_dir)),
            "finished_at": _now(),
        }

    # --- whole run ---
    def run(self, limit: int | None = None) -> dict:
        """Run pending units (at most ``limit``); the server ends at the baseline."""
        problems = self.steps.check_ready()
        if problems:
            raise ExperimentError("; ".join(problems))
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.check_manifest()
        done = self.done_keys()
        pending = [(r, c) for r, c in self.units() if not self.unit_done(c, r, done)]
        if limit is not None:
            pending = pending[:limit]
        total = len(self.units())
        self.say(f"{total - len(pending)}/{total} units done, running {len(pending)}")
        finished, incomplete = 0, []
        try:
            for i, (repeat, case) in enumerate(pending, start=1):
                self.say(f"=== unit {i}/{len(pending)}: {case.case_id} repeat {repeat}")
                if self.run_unit(case, repeat, done):
                    finished += 1
                else:
                    incomplete.append(f"{case.case_id} r{repeat}")
        finally:
            if self.steps.verify(None):
                self.say("resetting the server to baseline")
                self.steps.reset()
        return {"ran": len(pending), "finished": finished, "incomplete": incomplete}


def progress_table(out_dir: Path) -> str:
    """Per case and mode: runs done, setting named at rank 1 / in top 4, a fix within 10%."""
    rows = load_rows(out_dir / "results.jsonl")
    if not rows:
        return f"no results in {out_dir}"
    manifest = read_json(out_dir / "manifest.json") if (out_dir / "manifest.json").exists() else {}
    cases = manifest.get("cases") or sorted({r["case_id"] for r in rows})
    modes = manifest.get("modes") or list(MODES)
    lines = [
        f"{out_dir}  model {manifest.get('model')}  preset {manifest.get('preset')}  "
        f"repeats {manifest.get('repeats')}  rows {len(rows)}",
        "| case | mode | runs | top1 correct | correct in top4 | top1 works | any works |",
        "|---|---|---|---|---|---|---|",
    ]
    totals: dict[str, list[int]] = {m: [0, 0, 0, 0, 0] for m in modes}
    for cid in cases:
        for m in modes:
            sel = [r["score"] for r in rows if r["case_id"] == cid and r["mode"] == m]
            vals = [
                len(sel),
                sum(s["top1_correct"] for s in sel),
                sum(s["correct_in_top4"] for s in sel),
                sum(s["top1_works"] for s in sel),
                sum(s["any_works"] for s in sel),
            ]
            totals[m] = [a + b for a, b in zip(totals[m], vals, strict=True)]
            n = vals[0]
            lines.append(
                f"| {cid} | {m} | {n} | " + " | ".join(f"{v}/{n}" for v in vals[1:]) + " |"
            )
    for m in modes:
        n = totals[m][0]
        lines.append(
            f"| **all** | {m} | {n} | " + " | ".join(f"{v}/{n}" for v in totals[m][1:]) + " |"
        )
    errors = sorted(out_dir.glob("runs/*/*/diagnosis.*.error.json"))
    if errors:
        lines.append(f"pending diagnosis errors: {len(errors)}")
        lines += [f"  {p.relative_to(out_dir)}" for p in errors[:10]]
    return "\n".join(lines)


def resolve_cases(spec: str | None) -> list[FaultCase]:
    if not spec:
        return list(load_cases())
    return [get_case(s.strip()) for s in spec.split(",") if s.strip()]
