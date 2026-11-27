"""The UI's non-Streamlit parts: custom cases, merging suggestions, saved sessions, a lock.

Everything here is plain Python so it can be tested without a browser or a database.
The UI itself (``app.py``) only lays these out and calls the existing pipeline stages:
``faults.inject`` (break / reset), ``collect`` (symptoms), ``retrieve`` + ``diagnose``
(the LLM), ``fixtest`` (benchmark each fix on the broken server).
"""

from __future__ import annotations

import datetime as _dt
import fcntl
import json
import os
import re
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from pgdiag.faults.cases import FaultCase, load_cases

CUSTOM_PREFIX = "c99"  # case ids the UI makes up for values not in cases.yaml
MODES = ("rag", "norag")


class Busy(RuntimeError):
    """Another UI session is already changing or benchmarking the database."""


def case_for(setting: str, value: str, good_value: str, restart_required: bool) -> FaultCase:
    """The experiment's case if (setting, value) is one of them, else a custom case.

    A custom case behaves like a real one for apply/verify/reset/test_fixes; its id is
    ``c99_<setting>`` and it is never shown to the model (symptoms come from the server).
    """
    value = str(value).strip()
    for c in load_cases():
        if c.setting == setting and _norm(c.bad_value) == _norm(value):
            return c
    return FaultCase(
        case_id=f"{CUSTOM_PREFIX}_{setting}",
        setting=setting,
        bad_value=value,
        good_value=str(good_value),
        restart_required=bool(restart_required),
        needs_analyze=False,
        primary_component="",
        symptom_components=(),
        rationale="custom value chosen in the UI",
    )


def case_from_state(record: dict | None) -> FaultCase | None:
    """Rebuild the applied case from the state file's ``case`` record (custom ones too)."""
    if not record or record.get("status") != "applied":
        return None
    for c in load_cases():
        if c.case_id == record.get("case_id"):
            return c
    return FaultCase(
        case_id=record["case_id"],
        setting=record["setting"],
        bad_value=str(record["bad_value"]),
        good_value=str(record["good_value"]),
        restart_required=bool(record.get("restart_required")),
        needs_analyze=bool(record.get("needs_analyze")),
        primary_component="",
        symptom_components=(),
        rationale="custom value chosen in the UI",
    )


def _norm(value) -> str:
    return re.sub(r"\s+", "", str(value)).lower()


def fix_key(setting: str, value) -> str:
    """Text-only key; the UI passes ``Steps.fix_key`` instead (``8MB`` == ``8192kB``)."""
    return f"{str(setting).strip().lower()}={_norm(value)}"


def merge_suggestions(
    diagnoses: dict[str, dict],
    extra: Iterable[dict] = (),
    key: Callable[[str, object], str] = fix_key,
) -> list[dict]:
    """One list of fixes to benchmark: each distinct (setting, value) once, with its sources.

    ``diagnoses`` maps a mode (``rag``/``norag``) to a ``diagnose()`` record; ``extra``
    are fixes added by hand (``{"setting", "proposed_value", "source"}``). Order: by best
    rank over the sources, then rag before norag before manual. Ranks are renumbered 1..n.
    """
    merged: dict[tuple[str, str], dict] = {}
    order = {"rag": 0, "norag": 1}
    entries = []
    for mode, record in diagnoses.items():
        for f in (record or {}).get("fixes", []):
            entries.append((f["rank"], order.get(mode, 2), mode, f))
    for i, f in enumerate(extra):
        entries.append((100 + i, 3, f.get("source", "manual"), f))
    for rank, prio, source, f in sorted(entries, key=lambda e: (e[0], e[1])):
        item = merged.setdefault(
            key(f["setting"], f["proposed_value"]),
            {
                "setting": str(f["setting"]).strip(),
                "proposed_value": str(f["proposed_value"]).strip(),
                "sources": [],
                "best_rank": rank,
                "prio": prio,
            },
        )
        label = source if source not in MODES else f"{source} #{rank}"
        if label not in item["sources"]:
            item["sources"].append(label)
    out = sorted(merged.values(), key=lambda m: (m["best_rank"], m["prio"]))
    for i, m in enumerate(out, start=1):
        m["rank"] = i
        del m["prio"]
    return out


def result_rows(fixtest: dict, suggestions: list[dict]) -> list[dict]:
    """Rows for the comparison table and chart: healthy, broken, then each fix."""
    sources = {s["rank"]: ", ".join(s["sources"]) for s in suggestions}
    ref = fixtest["ref_s"]
    rows = [
        {
            "label": "healthy (baseline)",
            "kind": "reference",
            "median_s": ref,
            "vs_healthy_pct": 0.0,
            "recovery_pct": None,
            "status": "reference",
            "suggested_by": "",
        },
        {
            "label": f"broken ({fixtest['setting']} = {fixtest['bad_value']})",
            "kind": "reference",
            "median_s": fixtest["broken"]["median_s"],
            "vs_healthy_pct": fixtest["broken_vs_ref_pct"],
            "recovery_pct": 0.0,
            "status": "reference",
            "suggested_by": "",
        },
    ]
    best = (fixtest.get("best") or {}).get("rank")
    for r in fixtest["fixes"]:
        rows.append(
            {
                "label": f"#{r['rank']} {r['setting']} = {r['proposed_value']}",
                "kind": "best fix" if r["rank"] == best else "fix",
                "median_s": r["median_s"],
                "vs_healthy_pct": r["vs_ref_pct"],
                "recovery_pct": r["recovery_pct"],
                "status": r["status"] + (" (within 10%)" if r["within_10pct"] else ""),
                "suggested_by": sources.get(r["rank"], ""),
                "error": r.get("error"),
            }
        )
    return rows


def oom_kill_count(memory_events: str) -> int | None:
    """``oom_kill`` from a cgroup v2 ``memory.events`` file, or None if absent."""
    for line in memory_events.splitlines():
        name, _, value = line.partition(" ")
        if name == "oom_kill" and value.strip().isdigit():
            return int(value)
    return None


# --- saved sessions (results/ui/<stamp>_<case>/) ---------------------------------


def new_session_dir(results_dir: Path, case: FaultCase) -> Path:
    stamp = _dt.datetime.now(_dt.UTC).strftime("%Y%m%d-%H%M%S")
    path = results_dir / "ui" / f"{stamp}_{case.case_id}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str) + "\n")
    os.replace(tmp, path)


def list_sessions(results_dir: Path) -> list[dict]:
    """Saved UI sessions that reached the fix test, newest first."""
    out = []
    for d in sorted((results_dir / "ui").glob("*_c*"), reverse=True):
        ft = d / "fixtest.json"
        if not ft.exists():
            continue
        try:
            fixtest = json.loads(ft.read_text())
        except json.JSONDecodeError:
            continue
        sugg_path = d / "suggestions.json"
        suggestions = json.loads(sugg_path.read_text()) if sugg_path.exists() else []
        out.append({"dir": d, "name": d.name, "fixtest": fixtest, "suggestions": suggestions})
    return out


# --- one database operation at a time -------------------------------------------


@contextmanager
def db_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive lock while changing or benchmarking the database.

    Two browser tabs (or the UI and a terminal run of this lock's users) would otherwise
    restart and benchmark the same server at once and spoil each other's numbers.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("w")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy("another UI session is using the database; wait for it to finish") from None
        fh.write(str(os.getpid()))
        fh.flush()
        yield
    finally:
        fh.close()
