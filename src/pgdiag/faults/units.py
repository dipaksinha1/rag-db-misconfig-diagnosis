"""Parse and compare PostgreSQL setting values the way the server does.

A value like ``256MB`` for ``shared_buffers`` (whose ``pg_settings.unit`` is
``8kB``) normalises to ``32768.0``: the number of the setting's own units, i.e.
what ``pg_settings.setting`` shows. Two values are equal when their normalised
forms are equal, so ``1MB`` == ``1024kB`` == ``128`` (8kB pages) for
``shared_buffers``. Booleans normalise to ``on`` / ``off``; enums to lower case.

Everything here is offline: callers pass the ``vartype``, ``unit``, ``min_val``,
``max_val`` and ``enumvals`` columns of ``pg_settings`` as a :class:`SettingInfo`.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

MEMORY_UNITS: dict[str, int] = {"B": 1, "kB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
TIME_UNITS_MS: dict[str, float] = {
    "us": 0.001,
    "ms": 1.0,
    "s": 1000.0,
    "min": 60_000.0,
    "h": 3_600_000.0,
    "d": 86_400_000.0,
}

_NUM_RE = re.compile(r"^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*([A-Za-z]*)\s*$")
_UNIT_RE = re.compile(r"^(\d*)\s*([A-Za-z]+)$")


class InvalidValue(ValueError):
    """A value that PostgreSQL would reject for this setting."""


@dataclass(frozen=True)
class SettingInfo:
    """The ``pg_settings`` columns needed to parse and check a value."""

    name: str
    vartype: str  # bool, integer, real, enum, string
    unit: str | None = None  # e.g. 8kB, kB, MB, ms, s, min
    min_val: str | None = None
    max_val: str | None = None
    enumvals: tuple[str, ...] = ()
    context: str = "user"  # postmaster, sighup, superuser, user, ...

    @property
    def restart_required(self) -> bool:
        return self.context == "postmaster"


def _unit_scale(unit: str) -> tuple[str, float]:
    """``8kB`` -> ("memory", 8192); ``ms`` -> ("time", 1.0); ``s`` -> ("time", 1000.0)."""
    m = _UNIT_RE.match(unit.strip())
    if not m:
        raise InvalidValue(f"unrecognised setting unit {unit!r}")
    mult = int(m.group(1)) if m.group(1) else 1
    base = m.group(2)
    if base in MEMORY_UNITS:
        return "memory", float(mult * MEMORY_UNITS[base])
    if base in TIME_UNITS_MS:
        return "time", mult * TIME_UNITS_MS[base]
    raise InvalidValue(f"unrecognised setting unit {unit!r}")


def parse_bool(value: str) -> str:
    """``on``/``off`` for any spelling PostgreSQL accepts (on, off, true, yes, 1, prefixes ...)."""
    v = str(value).strip().lower()
    if v in ("1", "on"):
        return "on"
    if v in ("0", "of", "off"):
        return "off"
    if v and ("true".startswith(v) or "yes".startswith(v)):
        return "on"
    if v and ("false".startswith(v) or "no".startswith(v)):
        return "off"
    raise InvalidValue(f"{value!r} is not a boolean")


def normalize(value: str | int | float | bool, info: SettingInfo) -> str | float:
    """The value expressed in the setting's own unit (numbers) or canonical text.

    Raises :class:`InvalidValue` for something PostgreSQL would not accept
    (bad unit, not a number, unknown enum value). Range is checked separately
    (:func:`check_range`).
    """
    if isinstance(value, bool):
        value = "on" if value else "off"
    text = str(value).strip().strip("'\"").strip()
    vt = info.vartype
    if vt == "bool":
        return parse_bool(text)
    if vt == "enum":
        low = text.lower()
        if info.enumvals and low not in [e.lower() for e in info.enumvals]:
            raise InvalidValue(f"{info.name}: {text!r} is not one of {list(info.enumvals)}")
        return low
    if vt == "string":
        return text
    if vt not in ("integer", "real"):
        raise InvalidValue(f"{info.name}: unsupported vartype {vt!r}")
    m = _NUM_RE.match(text)
    if not m:
        raise InvalidValue(f"{info.name}: {text!r} is not a number with an optional unit")
    number = float(m.group(1))
    given_unit = m.group(2)
    if given_unit:
        if not info.unit:
            raise InvalidValue(f"{info.name} takes no unit, got {text!r}")
        kind, scale = _unit_scale(info.unit)
        table = MEMORY_UNITS if kind == "memory" else TIME_UNITS_MS
        if given_unit not in table:
            valid = ", ".join(table)
            raise InvalidValue(
                f"{info.name}: invalid unit {given_unit!r} in {text!r} (valid units: {valid})"
            )
        number = number * table[given_unit] / scale
    if vt == "integer":
        # The server rounds a value given in a unit to the nearest whole setting unit.
        if not math.isfinite(number):
            raise InvalidValue(f"{info.name}: {text!r} is not finite")
        number = float(round(number))
    return number


def check_range(normalized: str | float, info: SettingInfo) -> None:
    """Raise :class:`InvalidValue` when a numeric value lies outside min_val..max_val.

    ``-1`` is allowed where it is the setting's minimum (e.g. "disabled").
    """
    if not isinstance(normalized, float):
        return
    lo = float(info.min_val) if info.min_val not in (None, "") else -math.inf
    hi = float(info.max_val) if info.max_val not in (None, "") else math.inf
    if not (lo <= normalized <= hi):
        unit = f" {info.unit}" if info.unit else ""
        raise InvalidValue(
            f"{info.name}: {format_number(normalized)}{unit} is outside the allowed range "
            f"{info.min_val}..{info.max_val}{unit}"
        )


def validate(value: str, info: SettingInfo) -> str | float:
    """normalize + check_range; returns the normalised value."""
    norm = normalize(value, info)
    check_range(norm, info)
    return norm


def values_equal(a: str | float, b: str | float) -> bool:
    """Compare two normalised values (numbers with a relative tolerance)."""
    if isinstance(a, float) and isinstance(b, float):
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
    return a == b


def same_value(a: str, b: str, info: SettingInfo) -> bool:
    """True when two textual values mean the same thing for this setting."""
    return values_equal(normalize(a, info), normalize(b, info))


def format_number(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else repr(x)


def to_bytes(normalized: float, info: SettingInfo) -> float | None:
    """A memory setting's normalised value in bytes (None for non-memory settings)."""
    if not info.unit:
        return None
    kind, scale = _unit_scale(info.unit)
    return normalized * scale if kind == "memory" else None
