"""The workload's query set: versioned SQL files plus the order of components.

Each file in ``pgdiag/workload/queries/`` holds ONE SQL statement and a header
of ``-- key: value`` lines::

    -- id: product_sales
    -- component: order_lookups
    -- description: Daily sales chart of one product over the last 90 days.
    -- sensitive_to: random_page_cost
    -- repeat: 5

* ``id``           unique; also the file name without the ``NN_`` prefix
* ``component``    the timing group it belongs to (see ``COMPONENTS``)
* ``description``  one line, what an application would use it for
* ``sensitive_to`` the case settings (``pgdiag.settings.CASE_SETTINGS``) whose
                   bad value is meant to slow it down
* ``repeat``       how often it runs per pass (default 1). The statement may use
                   ``%(i)s`` (1..repeat) to vary its constants between runs.

Placeholders are psycopg named parameters. Besides ``i``, every field of the
data profile is available (``%(customers)s``, ``%(products)s``, ``%(order_days)s``
...), so the same SQL scales to the ``tiny`` test
profile. Values are bound client-side, so the server (pg_stat_statements, the
slow-query log, EXPLAIN) sees plain literals. Write ``mod(a, b)`` rather than
the ``%`` operator (``%`` starts a placeholder).

Within a component, files run in file-name order. Components run in the order
of ``COMPONENTS``; ``pgbench`` is not SQL but a fixed-size ``pgbench`` run.
Bump ``QUERY_SET_VERSION`` whenever a query or the order changes: benchmark
results record it, and results from different versions are not comparable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources

from pgdiag.settings import CASE_SETTINGS
from pgdiag.workload.profiles import INT_FIELDS, DataProfile

QUERY_SET_VERSION = "2"  # 2: T5b small dataset, six case settings


@dataclass(frozen=True)
class Component:
    id: str
    description: str
    kind: str = "sql"  # "sql", or "pgbench" (pgbench's built-in TPC-B-like script)
    # Settings this component targets beyond those its queries declare (pgbench has no files).
    extra_sensitive_to: tuple[str, ...] = ()


# Pass order. Every component is read-only except pgbench (account balances and
# pgbench_history, which is truncated before every pass).
COMPONENTS: tuple[Component, ...] = (
    Component(
        "pgbench",
        "pgbench TPC-B-like transactions, fixed transaction count (many tiny statements)",
        kind="pgbench",
        extra_sensitive_to=("jit_above_cost",),
    ),
    Component("order_lookups", "selective order lookups through indexes"),
    Component("order_export", "order export with per-row customer lookups"),
    Component("revenue_reports", "full-table scan-and-aggregate reports"),
    Component("customer_reports", "customer reports with a NOT IN subquery, aggregates and sorts"),
    Component("product_affinity", "reports on best-selling products (skewed data)"),
    Component("small_lookups", "many short application queries"),
)

COMPONENT_IDS: tuple[str, ...] = tuple(c.id for c in COMPONENTS)
HEADER_KEYS = ("id", "component", "description", "sensitive_to", "repeat")
_HEADER_RE = re.compile(r"^--\s*([a-z_]+)\s*:\s*(.*?)\s*$")


class QuerySetError(ValueError):
    pass


@dataclass(frozen=True)
class Query:
    id: str
    component: str
    description: str
    sensitive_to: tuple[str, ...]
    sql: str
    repeat: int = 1
    filename: str = ""
    placeholders: frozenset[str] = field(default_factory=frozenset)

    def params(self, profile: DataProfile, i: int = 1) -> dict[str, int]:
        values = {**profile.int_params(), "i": i}
        return {name: values[name] for name in self.placeholders}


def _placeholders(sql_text: str) -> frozenset[str]:
    names = set(re.findall(r"%\((\w+)\)s", sql_text))
    stray = re.sub(r"%\(\w+\)s|%%", "", sql_text)
    if "%" in stray:
        raise QuerySetError("use mod() instead of the % operator, or escape it as %%")
    return frozenset(names)


def parse_query_file(filename: str, text: str) -> Query:
    header: dict[str, str] = {}
    body: list[str] = []
    in_header = True
    for line in text.splitlines():
        match = _HEADER_RE.match(line) if in_header else None
        if match and match.group(1) in HEADER_KEYS:
            header[match.group(1)] = match.group(2)
            continue
        if in_header and (not line.strip() or line.startswith("--")):
            continue
        in_header = False
        body.append(line)
    sql_text = "\n".join(body).strip().rstrip(";").strip()
    where = f"{filename}:"
    for key in ("id", "component", "description", "sensitive_to"):
        if not header.get(key):
            raise QuerySetError(f"{where} missing header '-- {key}: ...'")
    if not sql_text:
        raise QuerySetError(f"{where} no SQL statement")
    if ";" in sql_text:
        raise QuerySetError(f"{where} one statement per file (found ';')")
    settings = tuple(s.strip() for s in header["sensitive_to"].split(",") if s.strip())
    unknown = [s for s in settings if s not in CASE_SETTINGS]
    if unknown:
        raise QuerySetError(f"{where} sensitive_to names non-case settings: {unknown}")
    if header["component"] not in COMPONENT_IDS:
        raise QuerySetError(f"{where} unknown component {header['component']!r}")
    try:
        repeat = int(header.get("repeat", "1"))
    except ValueError:
        raise QuerySetError(f"{where} repeat must be an integer") from None
    if repeat < 1:
        raise QuerySetError(f"{where} repeat must be >= 1")
    try:
        placeholders = _placeholders(sql_text)
    except QuerySetError as exc:
        raise QuerySetError(f"{where} {exc}") from None
    allowed = set(INT_FIELDS) | {"i"}
    if placeholders - allowed:
        raise QuerySetError(f"{where} unknown placeholders {sorted(placeholders - allowed)}")
    expected_name = re.sub(r"^\d+_", "", filename.removesuffix(".sql"))
    if expected_name != header["id"]:
        raise QuerySetError(f"{where} id {header['id']!r} must match the file name")
    return Query(
        id=header["id"],
        component=header["component"],
        description=header["description"],
        sensitive_to=settings,
        sql=sql_text,
        repeat=repeat,
        filename=filename,
        placeholders=placeholders,
    )


@lru_cache(maxsize=1)
def load_queries() -> tuple[Query, ...]:
    """All queries in pass order (component order, then file name)."""
    directory = resources.files("pgdiag.workload").joinpath("queries")
    files = sorted(
        (f for f in directory.iterdir() if f.name.endswith(".sql")), key=lambda f: f.name
    )
    queries = [parse_query_file(f.name, f.read_text()) for f in files]
    ids = [q.id for q in queries]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise QuerySetError(f"duplicate query ids: {dupes}")
    order = {cid: n for n, cid in enumerate(COMPONENT_IDS)}
    return tuple(sorted(queries, key=lambda q: (order[q.component], q.filename)))


def queries_by_component() -> dict[str, list[Query]]:
    grouped: dict[str, list[Query]] = {cid: [] for cid in COMPONENT_IDS}
    for q in load_queries():
        grouped[q.component].append(q)
    return grouped


def get_query(query_id: str) -> Query:
    for q in load_queries():
        if q.id == query_id:
            return q
    raise KeyError(query_id)


def component_settings() -> dict[str, tuple[str, ...]]:
    """Case settings each component is designed to be sensitive to."""
    grouped = queries_by_component()
    out = {}
    for comp in COMPONENTS:
        names = set(comp.extra_sensitive_to)
        for q in grouped[comp.id]:
            names.update(q.sensitive_to)
        out[comp.id] = tuple(s for s in CASE_SETTINGS if s in names)
    return out


def coverage() -> dict[str, list[str]]:
    """For each case setting, the ids of queries/components declared sensitive to it."""
    cov: dict[str, list[str]] = {s: [] for s in CASE_SETTINGS}
    for q in load_queries():
        for s in q.sensitive_to:
            cov[s].append(q.id)
    for comp in COMPONENTS:
        for s in comp.extra_sensitive_to:
            cov[s].append(comp.id)
    return cov


def render_offline(query: Query, profile: DataProfile, i: int = 1) -> str:
    """The statement with its parameters filled in: exactly what the server receives.

    Every placeholder is an integer (profile sizes and the repeat index), so
    plain substitution is safe and needs no database connection.
    """
    params = query.params(profile, i)
    text = re.sub(r"%\((\w+)\)s", lambda m: str(int(params[m.group(1)])), query.sql)
    return text.replace("%%", "%")
