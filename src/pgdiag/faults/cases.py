"""The fault cases (``cases.yaml``): typed loader, offline and server-side validation.

One case per experiment setting (``pgdiag.settings.CASE_SETTINGS``, same order).

Later stages import this::

    from pgdiag.faults.cases import load_cases, get_case
    for case in load_cases():
        case.case_id, case.setting, case.bad_value, case.good_value, case.restart_required

The healthy values come from ``docker/postgres/postgresql.conf``
(:func:`baseline_config_values`); every case's ``good_value`` must equal the
value there (checked by :func:`validate_cases` and a test).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from pathlib import Path

import yaml

from pgdiag.config import Config, get_config
from pgdiag.faults import units
from pgdiag.settings import CASE_SETTINGS, TARGET_SETTINGS

CASES_FILE = "cases.yaml"
BASELINE_CONF = Path("docker/postgres/postgresql.conf")
REQUIRED_FIELDS = (
    "case_id",
    "setting",
    "bad_value",
    "good_value",
    "restart_required",
    "needs_analyze",
    "primary_component",
    "symptom_components",
    "rationale",
)
_CASE_ID_RE = re.compile(r"^c(\d{2})_([a-z_]+)$")


class CaseError(ValueError):
    pass


@dataclass(frozen=True)
class FaultCase:
    case_id: str
    setting: str
    bad_value: str
    good_value: str
    restart_required: bool
    needs_analyze: bool
    primary_component: str
    symptom_components: tuple[str, ...]
    rationale: str
    extra: dict = field(default_factory=dict, compare=False)
    # further broken settings of a multi-fault case: (setting, bad_value, good_value)
    also: tuple[tuple[str, str, str], ...] = ()

    @property
    def number(self) -> int:
        return int(self.case_id[1:3])

    @property
    def faults(self) -> tuple[tuple[str, str, str], ...]:
        """Every broken setting as (setting, bad_value, good_value); the first is ``setting``."""
        return ((self.setting, self.bad_value, self.good_value), *self.also)

    @property
    def settings(self) -> tuple[str, ...]:
        return tuple(f[0] for f in self.faults)

    @property
    def is_multi(self) -> bool:
        return bool(self.also)

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "setting": self.setting,
            "bad_value": self.bad_value,
            "good_value": self.good_value,
            "restart_required": self.restart_required,
            "needs_analyze": self.needs_analyze,
            "primary_component": self.primary_component,
            "symptom_components": list(self.symptom_components),
            "rationale": self.rationale,
            **({"also": [list(f) for f in self.also]} if self.also else {}),
        }


def _as_text(value, where: str) -> str:
    if isinstance(value, bool):
        # YAML turns bare on/off into booleans; keep PostgreSQL's spelling.
        return "on" if value else "off"
    if isinstance(value, (int, float, str)):
        return str(value).strip()
    raise CaseError(f"{where}: expected a scalar value, got {value!r}")


def parse_cases(data: dict) -> tuple[FaultCase, ...]:
    """Build and check the case list from the parsed YAML (offline checks only)."""
    from pgdiag.workload.queryset import COMPONENT_IDS

    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise CaseError("cases file must be a mapping with a 'cases' list")
    out: list[FaultCase] = []
    for n, raw in enumerate(data["cases"], 1):
        where = f"case #{n}"
        if not isinstance(raw, dict):
            raise CaseError(f"{where}: expected a mapping")
        missing = [k for k in REQUIRED_FIELDS if k not in raw]
        if missing:
            raise CaseError(f"{where}: missing fields {missing}")
        where = f"case {raw['case_id']}"
        for flag in ("restart_required", "needs_analyze"):
            if not isinstance(raw[flag], bool):
                raise CaseError(f"{where}: {flag} must be true or false")
        comps = raw["symptom_components"]
        if not isinstance(comps, list) or not comps:
            raise CaseError(f"{where}: symptom_components must be a non-empty list")
        unknown = [c for c in [*comps, raw["primary_component"]] if c not in COMPONENT_IDS]
        if unknown:
            raise CaseError(f"{where}: unknown workload components {unknown}")
        if raw["primary_component"] not in comps:
            raise CaseError(f"{where}: primary_component must be one of symptom_components")
        m = _CASE_ID_RE.match(str(raw["case_id"]))
        if not m or m.group(1) != f"{n:02d}" or m.group(2) != raw["setting"]:
            raise CaseError(f"{where}: case_id must be c{n:02d}_{raw['setting']}")
        rationale = str(raw["rationale"]).strip()
        if not rationale:
            raise CaseError(f"{where}: empty rationale")
        case = FaultCase(
            case_id=str(raw["case_id"]),
            setting=str(raw["setting"]),
            bad_value=_as_text(raw["bad_value"], where),
            good_value=_as_text(raw["good_value"], where),
            restart_required=raw["restart_required"],
            needs_analyze=raw["needs_analyze"],
            primary_component=str(raw["primary_component"]),
            symptom_components=tuple(comps),
            rationale=rationale,
            extra={k: v for k, v in raw.items() if k not in REQUIRED_FIELDS},
        )
        if case.bad_value == case.good_value:
            raise CaseError(f"{where}: bad_value equals good_value")
        out.append(case)
    settings = [c.setting for c in out]
    if settings != list(CASE_SETTINGS):
        raise CaseError(f"cases must cover CASE_SETTINGS exactly once, in order; got {settings}")
    return tuple(out)


_COMBO_ID_RE = re.compile(r"^m(\d{2})_[a-z_]+$")


def parse_combos(data: dict, cases: tuple[FaultCase, ...]) -> tuple[FaultCase, ...]:
    """Multi-fault cases (``combos:``): two or more single cases applied together.

    Each combo names the single cases it combines (``combine: [c03, c04]``); their bad and
    good values are taken from those cases, so the combo breaks exactly what they break.
    """
    from pgdiag.workload.queryset import COMPONENT_IDS

    out: list[FaultCase] = []
    for n, raw in enumerate(data.get("combos") or [], 1):
        if not isinstance(raw, dict):
            raise CaseError(f"combo #{n}: expected a mapping")
        where = f"combo {raw.get('case_id')}"
        m = _COMBO_ID_RE.match(str(raw.get("case_id", "")))
        if not m or m.group(1) != f"{n:02d}":
            raise CaseError(f"{where}: case_id must look like m{n:02d}_<name>")
        keys = raw.get("combine")
        if not isinstance(keys, list) or len(keys) < 2:
            raise CaseError(f"{where}: combine must list at least two single cases")
        parts = [get_case(str(k), cases) for k in keys]
        if len({p.setting for p in parts}) != len(parts):
            raise CaseError(f"{where}: combines the same setting twice")
        rationale = str(raw.get("rationale", "")).strip()
        if not rationale:
            raise CaseError(f"{where}: empty rationale")
        comps = list(dict.fromkeys(c for p in parts for c in p.symptom_components))
        primary = str(raw.get("primary_component") or parts[0].primary_component)
        if primary not in COMPONENT_IDS or primary not in comps:
            raise CaseError(f"{where}: bad primary_component {primary!r}")
        first = parts[0]
        out.append(
            FaultCase(
                case_id=str(raw["case_id"]),
                setting=first.setting,
                bad_value=first.bad_value,
                good_value=first.good_value,
                restart_required=any(p.restart_required for p in parts),
                needs_analyze=any(p.needs_analyze for p in parts),
                primary_component=primary,
                symptom_components=tuple(comps),
                rationale=rationale,
                extra={"combine": [p.case_id for p in parts]},
                also=tuple((p.setting, p.bad_value, p.good_value) for p in parts[1:]),
            )
        )
    return tuple(out)


def read_cases_text(path: str | Path | None = None) -> str:
    if path is not None:
        return Path(path).read_text()
    return resources.files("pgdiag.faults").joinpath(CASES_FILE).read_text()


@lru_cache(maxsize=4)
def _load_cached(path: str | None) -> tuple[tuple[FaultCase, ...], tuple[FaultCase, ...]]:
    data = yaml.safe_load(read_cases_text(path))
    cases = parse_cases(data)
    return cases, parse_combos(data, cases)


def load_cases(path: str | Path | None = None) -> tuple[FaultCase, ...]:
    """All six single-fault cases in CASE_SETTINGS order (default: the packaged cases.yaml)."""
    return _load_cached(str(path) if path is not None else None)[0]


def load_combos(path: str | Path | None = None) -> tuple[FaultCase, ...]:
    """The multi-fault cases (``combos:`` in cases.yaml)."""
    return _load_cached(str(path) if path is not None else None)[1]


def all_cases(path: str | Path | None = None) -> tuple[FaultCase, ...]:
    return (*load_cases(path), *load_combos(path))


def get_case(key: str, cases: tuple[FaultCase, ...] | None = None) -> FaultCase:
    """Look a case up by case id (``c02_work_mem``, ``m01_...``), setting name or number
    (``2``/``c02``/``m01``); multi-fault cases are found by id or ``mNN`` only."""
    cases = cases or all_cases()
    k = key.strip()
    for c in cases:
        if k in (c.case_id, c.setting, c.case_id[:3]) or (k.isdigit() and int(k) == c.number):
            return c
    raise KeyError(f"no fault case {key!r}; known: {[c.case_id for c in cases]}")


# ---------------------------------------------------------------------------
# baseline configuration file

_CONF_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_.]*)\s*=?\s*(.*?)\s*$")


def _strip_conf_value(rest: str) -> str:
    rest = rest.strip()
    if rest.startswith("'"):
        end = rest.find("'", 1)
        while end != -1 and rest[end + 1 : end + 2] == "'":  # '' escapes a quote
            end = rest.find("'", end + 2)
        return rest[1:end].replace("''", "'") if end != -1 else rest[1:]
    return rest.split("#", 1)[0].strip()


def parse_conf(text: str) -> dict[str, str]:
    """name -> raw value of every active line of a postgresql.conf (last one wins)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _CONF_LINE_RE.match(stripped)
        if m:
            out[m.group(1).lower()] = _strip_conf_value(m.group(2))
    return out


def baseline_config_values(cfg: Config | None = None) -> dict[str, str]:
    """The ten target settings (the six case settings and the four others) as written in
    docker/postgres/postgresql.conf. Reset verifies all ten."""
    cfg = cfg or get_config()
    values = parse_conf((cfg.root / BASELINE_CONF).read_text())
    missing = [s for s in TARGET_SETTINGS if s not in values]
    if missing:
        raise CaseError(f"{BASELINE_CONF} does not set {missing}")
    return {s: values[s] for s in TARGET_SETTINGS}


def validate_cases(
    cases: tuple[FaultCase, ...] | None = None, cfg: Config | None = None
) -> list[str]:
    """Offline problems: good_value must equal the baseline file's value."""
    cases = cases or load_cases()
    base = baseline_config_values(cfg)
    return [
        f"{c.case_id}: good_value {c.good_value!r} != baseline {base[c.setting]!r}"
        for c in cases
        if c.good_value != base[c.setting]
    ]


# ---------------------------------------------------------------------------
# server-side validation (pg_settings)

SETTING_INFO_SQL = (
    "SELECT name, vartype, unit, min_val, max_val, enumvals, context "
    "FROM pg_settings WHERE name = ANY(%s)"
)


def setting_infos(conn, names) -> dict[str, units.SettingInfo]:
    rows = conn.execute(SETTING_INFO_SQL, (list(names),)).fetchall()
    return {
        r[0]: units.SettingInfo(
            name=r[0],
            vartype=r[1],
            unit=r[2],
            min_val=r[3],
            max_val=r[4],
            enumvals=tuple(r[5] or ()),
            context=r[6],
        )
        for r in rows
    }


def validate_against_server(
    infos: dict[str, units.SettingInfo], cases: tuple[FaultCase, ...] | None = None
) -> list[str]:
    """Problems found by checking each case against ``pg_settings`` (via ``infos``).

    The name must exist; bad and good values must parse with the setting's unit
    and lie within min/max; ``restart_required`` must match ``context``; the bad
    value must actually differ from the good one.
    """
    cases = cases or load_cases()
    problems: list[str] = []
    for c in cases:
        info = infos.get(c.setting)
        if info is None:
            problems.append(f"{c.case_id}: {c.setting} is not in pg_settings")
            continue
        try:
            bad = units.validate(c.bad_value, info)
            good = units.validate(c.good_value, info)
        except units.InvalidValue as exc:
            problems.append(f"{c.case_id}: {exc}")
            continue
        if units.values_equal(bad, good):
            problems.append(f"{c.case_id}: bad and good values are the same setting value")
        if c.restart_required != info.restart_required:
            problems.append(
                f"{c.case_id}: restart_required={c.restart_required} but pg_settings.context "
                f"is {info.context!r}"
            )
        if info.context == "internal":
            problems.append(f"{c.case_id}: {c.setting} cannot be changed (internal)")
    return problems
