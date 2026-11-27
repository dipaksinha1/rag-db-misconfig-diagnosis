"""Leak checks for symptom documents: nothing may reveal which setting was changed."""

from __future__ import annotations

import json

from pgdiag.faults.cases import all_cases

FORBIDDEN_KEYS = {
    "source", "sourcefile", "sourceline", "boot_val", "reset_val", "pending_restart",
    "case_id", "case", "label", "sensitive_to", "bad_value", "good_value",
}  # fmt: skip
FORBIDDEN_TEXT = (
    "alter system", "sighup", "changed to", "postgresql.auto.conf", "results/",
    "data/faults", "state.json", "pgdiag-admin", "t6-broken", "t6-fixed",
)  # fmt: skip


def _walk(obj, path="$"):
    """Yield (path, key or None, string value or None) for every node."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield path, k, None
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")
    elif isinstance(obj, str):
        yield path, None, obj


def check_document(doc: dict) -> list[str]:
    """Problems in one document (empty list = no leak found)."""
    case_ids = [c.case_id.lower() for c in all_cases()]
    problems = []
    for path, key, text in _walk(doc):
        if key is not None and key.lower() in FORBIDDEN_KEYS:
            problems.append(f"forbidden key {key!r} at {path}")
        low = (text or key or "").lower()
        for bad in (*FORBIDDEN_TEXT, *case_ids):
            if bad in low:
                problems.append(f"forbidden text {bad!r} at {path}")
    return problems


def check_set(docs: dict[str, dict], baseline: dict) -> list[str]:
    """Cross-document checks: case id -> document, all against the baseline document.

    Same sections and config parameter list (same order) everywhere; in the config
    section a case document differs from the baseline only in its own setting.
    """
    problems = []
    base_cfg = {p["name"]: p["value"] for p in baseline["config"]}
    base_names = [p["name"] for p in baseline["config"]]
    cases = {c.case_id: c for c in all_cases()}
    for case_id, doc in docs.items():
        if list(doc) != list(baseline):
            problems.append(f"{case_id}: sections {list(doc)} differ from the baseline")
        names = [p["name"] for p in doc["config"]]
        if names != base_names:
            problems.append(f"{case_id}: config parameter list differs from the baseline")
        changed = {p["name"] for p in doc["config"] if base_cfg.get(p["name"]) != p["value"]}
        if changed != set(cases[case_id].settings):
            problems.append(f"{case_id}: config differs from the baseline in {sorted(changed)}")
    return problems


def check_text(doc: dict) -> list[str]:
    """``check_document`` plus a raw-text pass over the serialized JSON."""
    raw = json.dumps(doc).lower()
    extra = [f"forbidden text {b!r} in the JSON" for b in FORBIDDEN_TEXT if b in raw]
    return check_document(doc) + extra
