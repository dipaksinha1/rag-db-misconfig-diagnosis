"""Deterministic data generator for the TEST database (T5).

Creates, in one database:

* the pgbench tables (``pgbench -i``, run inside the container);
* ``wl_customers``  1M customers. ``customer_id`` is a permutation of the
  physical row order (so its index is uncorrelated with the heap), ``country``
  is Zipf-skewed and ``city`` is functionally dependent on ``country``;
* ``wl_products``   50k products, ``category`` Zipf-skewed;
* ``wl_orders``     16M orders, appended in time order (``order_id`` and
  ``order_date`` follow the physical order). ``product_id`` is heavily skewed
  (log-uniform, i.e. Zipf with exponent ~1: the top product has ~5% of all
  orders, the 100th ~0.1%), ``customer_id`` moderately skewed, ``status``
  skewed and correlated with ``order_date``, ``warehouse_id`` functionally
  dependent on ``region``. Extended statistics cover the correlated pairs;
* ``wl_campaign_targets`` a small list of customer ids (a marketing list);
* ``wl_meta``       what was generated, with which profile and seed.

Every random value comes from ``random()`` after ``setseed()`` in a single
session, so the same seed and profile give the same rows. Each table is marked
complete in ``wl_meta`` only after its indexes and ANALYZE; running the
generator again skips complete tables (resumable), ``reset=True`` drops and
re-creates everything.

The workload's scratch tables (``wl_tasks``, ``wl_ledger``) are created by the
benchmark itself (see ``pgdiag.workload.bench``), not here.
"""

from __future__ import annotations

import logging
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from pgdiag import db
from pgdiag.config import Config, get_config
from pgdiag.workload.profiles import DataProfile

log = logging.getLogger(__name__)

DEFAULT_SEED = 20251114
GENERATOR_VERSION = 1

# Odd primes other than 5 are coprime with every row count used here (10^k
# multiples), so (i * P) % N is a permutation of 0..N-1.
CUSTOMER_PERM = 7919
ORDER_CUSTOMER_PERM = 104729
PRODUCT_PERM = 7919

COUNTRIES = (
    "US DE GB FR IN BR JP CA IT ES NL AU MX PL SE BE CH AT TR KR "
    "NO DK FI IE PT CZ RO HU GR IL ZA AR CL CO NZ SG MY TH PH VN"
).split()

GENERATED_TABLES = ("pgbench", "wl_customers", "wl_products", "wl_orders", "wl_campaign_targets")
WL_TABLES = ("wl_customers", "wl_products", "wl_orders", "wl_campaign_targets")
SCRATCH_TABLES = ("wl_tasks", "wl_ledger", "wl_audience")
PGBENCH_TABLES = ("pgbench_accounts", "pgbench_branches", "pgbench_tellers", "pgbench_history")


@dataclass
class GenerateReport:
    dbname: str
    profile: str
    seed: int
    steps: dict[str, float] = field(default_factory=dict)  # step -> seconds (skipped: absent)
    skipped: list[str] = field(default_factory=list)
    sizes: dict[str, int] = field(default_factory=dict)  # relation -> bytes incl. indexes

    @property
    def total_bytes(self) -> int:
        return sum(self.sizes.values())


# ---------------------------------------------------------------------------
# connections and the pgbench tool


def connect_db(dbname: str | None = None, cfg: Config | None = None) -> psycopg.Connection:
    """Autocommit connection to ``dbname`` (default: the configured database)."""
    cfg = cfg or get_config()
    kwargs = dict(cfg.db_connect_kwargs)
    if dbname:
        kwargs["dbname"] = dbname
    return psycopg.connect(**kwargs, autocommit=True, connect_timeout=5, application_name="pgdiag")


def ensure_database(dbname: str, cfg: Config | None = None) -> bool:
    """Create ``dbname`` if it does not exist. Returns True if it was created."""
    cfg = cfg or get_config()
    if dbname == cfg.db_name:
        return False
    with connect_db("postgres", cfg) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)).fetchone()
        if exists:
            return False
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
        return True


def drop_database(dbname: str, cfg: Config | None = None) -> None:
    cfg = cfg or get_config()
    if dbname == cfg.db_name:
        raise ValueError("refusing to drop the main experiment database")
    with connect_db("postgres", cfg) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname))
        )


def compose_exec(
    args: list[str], cfg: Config | None = None, **kwargs
) -> subprocess.CompletedProcess:
    """Run a command inside the ``db`` service container (``docker compose exec -T db ...``)."""
    cfg = cfg or get_config()
    cmd = ["docker", "compose", "--project-directory", str(cfg.root), "exec", "-T", "db", *args]
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kwargs)


def pgbench_args(dbname: str, cfg: Config | None = None) -> list[str]:
    """Common pgbench arguments: run inside the container over the unix socket."""
    cfg = cfg or get_config()
    return ["pgbench", "-U", cfg.db_user, "-d", dbname]


# ---------------------------------------------------------------------------
# SQL


def _countries_array() -> str:
    return "ARRAY[" + ",".join(f"'{c}'" for c in COUNTRIES) + "]"


def customers_sql(p: DataProfile) -> list[str]:
    n = p.customers
    nc = len(COUNTRIES)
    return [
        "DROP TABLE IF EXISTS wl_customers CASCADE",
        """CREATE TABLE wl_customers (
            customer_id  integer NOT NULL,
            country      char(2) NOT NULL,
            city         text    NOT NULL,
            segment      text    NOT NULL,
            signup_date  date    NOT NULL,
            credit_limit numeric(12,2) NOT NULL,
            email        text    NOT NULL,
            notes        text
        )""",
        # Physical order = signup order; customer_id is a permutation of it.
        f"""INSERT INTO wl_customers
        SELECT ((g - 1) * {CUSTOMER_PERM}::bigint % {n} + 1)::int,
               ({_countries_array()})[ci],
               ({_countries_array()})[ci] || '-city' || lpad(cr::text, 2, '0'),
               CASE WHEN r1 < 0.80 THEN 'consumer' WHEN r1 < 0.95 THEN 'smb'
                    ELSE 'enterprise' END,
               date '2015-01-01' + ((g - 1) * 3650 / {n})::int,
               round((500 + r2 * r2 * 49500)::numeric, 2),
               'user' || ((g - 1) * {CUSTOMER_PERM}::bigint % {n} + 1) || '@example.'
                   || lower(({_countries_array()})[ci]),
               CASE WHEN r3 < 0.3 THEN 'prefers email; ' || repeat('-', (r3 * 60)::int) END
        FROM (SELECT g,
                     least({nc}, floor(exp(random() * ln({nc + 1}))))::int AS ci,
                     least(50, floor(exp(random() * ln(51))))::int AS cr,
                     random() AS r1, random() AS r2, random() AS r3
              FROM generate_series(1::bigint, {n}) AS g) AS s""",
        "ALTER TABLE wl_customers ADD PRIMARY KEY (customer_id)",
        "CREATE INDEX wl_customers_country_city_idx ON wl_customers (country, city)",
        "CREATE INDEX wl_customers_signup_idx ON wl_customers (signup_date)",
        "CREATE STATISTICS wl_customers_country_city_stats (dependencies, mcv) "
        "ON country, city FROM wl_customers",
        "VACUUM (ANALYZE, PARALLEL 0) wl_customers",
    ]


def products_sql(p: DataProfile) -> list[str]:
    n = p.products
    return [
        "DROP TABLE IF EXISTS wl_products CASCADE",
        """CREATE TABLE wl_products (
            product_id integer PRIMARY KEY,
            category   text    NOT NULL,
            name       text    NOT NULL,
            list_price numeric(10,2) NOT NULL
        )""",
        f"""INSERT INTO wl_products
        SELECT g,
               'cat-' || lpad(cat::text, 3, '0'),
               'product ' || g,
               round((2 + r * r * 998)::numeric, 2)
        FROM (SELECT g, least(200, floor(exp(random() * ln(201))))::int AS cat, random() AS r
              FROM generate_series(1::bigint, {n}) AS g) AS s""",
        "CREATE INDEX wl_products_category_idx ON wl_products (category)",
        "VACUUM (ANALYZE, PARALLEL 0) wl_products",
    ]


ORDERS_CHUNK = 2_000_000


def orders_sql(p: DataProfile) -> list[str]:
    n, nc, npr, days = p.orders, p.customers, p.products, p.order_days
    stmts = [
        "DROP TABLE IF EXISTS wl_orders CASCADE",
        """CREATE TABLE wl_orders (
            order_id     bigint   NOT NULL,
            customer_id  integer  NOT NULL,
            product_id   integer  NOT NULL,
            order_date   date     NOT NULL,
            status       text     NOT NULL,
            region       smallint NOT NULL,
            warehouse_id smallint NOT NULL,
            quantity     smallint NOT NULL,
            amount       numeric(12,2) NOT NULL,
            discount     numeric(4,2)  NOT NULL,
            note         text
        )""",
    ]
    for start in range(1, n + 1, ORDERS_CHUNK):
        end = min(n, start + ORDERS_CHUNK - 1)
        stmts.append(
            f"""INSERT INTO wl_orders
            SELECT g,
                   (floor({nc} * rc * rc)::bigint * {ORDER_CUSTOMER_PERM} % {nc} + 1)::int,
                   ((least({npr}, floor(exp(rp * ln({npr + 1}))))::bigint - 1)
                        * {PRODUCT_PERM} % {npr} + 1)::int,
                   date '2023-01-01' + d,
                   CASE WHEN {days} - d <= 3 THEN
                            CASE WHEN rs < 0.6 THEN 'pending' ELSE 'shipped' END
                        WHEN {days} - d <= 14 THEN
                            CASE WHEN rs < 0.7 THEN 'shipped' ELSE 'delivered' END
                        ELSE CASE WHEN rs < 0.94 THEN 'delivered'
                                  WHEN rs < 0.985 THEN 'returned' ELSE 'cancelled' END
                   END,
                   reg,
                   (reg * 10 + floor(rw * 6))::smallint,
                   q,
                   round(((5 + ra * ra * 495) * q)::numeric, 2),
                   CASE WHEN rd < 0.7 THEN 0 WHEN rd < 0.85 THEN 0.05
                        WHEN rd < 0.95 THEN 0.10 ELSE 0.20 END,
                   'ord-' || g || '/' || repeat('.', (rw * 24)::int)
            FROM (SELECT g,
                         ((g - 1) * {days} / {n})::int AS d,
                         random() AS rc, random() AS rp, random() AS rs,
                         least(8, floor(exp(random() * ln(9))))::smallint AS reg,
                         random() AS rw,
                         (1 + floor(random() * random() * 10))::smallint AS q,
                         random() AS ra, random() AS rd
                  FROM generate_series({start}::bigint, {end}) AS g) AS s"""
        )
    stmts += [
        "ALTER TABLE wl_orders ADD PRIMARY KEY (order_id)",
        "CREATE INDEX wl_orders_customer_idx ON wl_orders (customer_id)",
        "CREATE INDEX wl_orders_date_idx ON wl_orders (order_date)",
        "CREATE INDEX wl_orders_product_idx ON wl_orders (product_id)",
        "CREATE STATISTICS wl_orders_region_wh_stats (dependencies, mcv) "
        "ON region, warehouse_id FROM wl_orders",
        "VACUUM (ANALYZE, PARALLEL 0) wl_orders",
    ]
    return stmts


def campaign_targets_sql(p: DataProfile) -> list[str]:
    # campaign_size distinct customers per campaign: (g * P + c * Q) mod N is
    # injective in g because P is coprime with N.
    return [
        "DROP TABLE IF EXISTS wl_campaign_targets CASCADE",
        """CREATE TABLE wl_campaign_targets (
            campaign    smallint NOT NULL,
            customer_id integer  NOT NULL,
            score       real     NOT NULL,
            PRIMARY KEY (campaign, customer_id)
        )""",
        f"""INSERT INTO wl_campaign_targets
        SELECT c, ((g - 1) * {ORDER_CUSTOMER_PERM} + c * {CUSTOMER_PERM}) % {p.customers} + 1,
               random()
        FROM generate_series(1, {p.campaigns}) AS c,
             generate_series(1::bigint, {p.campaign_size}) AS g
        ORDER BY c, g""",
        "VACUUM (ANALYZE, PARALLEL 0) wl_campaign_targets",
    ]


META_SQL = """CREATE TABLE IF NOT EXISTS wl_meta (
    key        text PRIMARY KEY,
    value      jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
)"""


# ---------------------------------------------------------------------------
# generator


def _seed_value(seed: int, table: str) -> float:
    """Map (seed, table) to setseed()'s range [-1, 1], independent per table."""
    import hashlib

    digest = hashlib.sha256(f"{seed}:{table}".encode()).digest()
    return (int.from_bytes(digest[:6], "big") / float(1 << 48)) * 2 - 1


def read_meta(conn: psycopg.Connection) -> dict:
    """Contents of wl_meta as {key: value}; empty if the table does not exist."""
    exists = conn.execute("SELECT to_regclass('wl_meta')").fetchone()[0]
    if not exists:
        return {}
    return dict(conn.execute("SELECT key, value FROM wl_meta").fetchall())


def _set_meta(conn: psycopg.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO wl_meta (key, value) VALUES (%s, %s::jsonb) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (key, Jsonb(value)),
    )


def relation_sizes(conn: psycopg.Connection) -> dict[str, int]:
    """Total on-disk size (heap + indexes + TOAST) of every workload table that exists."""
    names = [*PGBENCH_TABLES, *WL_TABLES, *SCRATCH_TABLES]
    rows = conn.execute(
        "SELECT c.relname, pg_total_relation_size(c.oid) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname = ANY(%s)",
        (names,),
    ).fetchall()
    order = {name: i for i, name in enumerate(names)}
    return dict(sorted(rows, key=lambda r: order[r[0]]))


def generate(
    profile: DataProfile,
    *,
    dbname: str | None = None,
    seed: int = DEFAULT_SEED,
    reset: bool = False,
    cfg: Config | None = None,
    progress: Callable[[str], None] | None = None,
) -> GenerateReport:
    """Generate the dataset for ``profile`` into ``dbname`` (default: the main database)."""
    cfg = cfg or get_config()
    dbname = dbname or cfg.db_name
    say = progress or (lambda msg: log.info("%s", msg))
    report = GenerateReport(dbname=dbname, profile=profile.name, seed=seed)

    if ensure_database(dbname, cfg):
        say(f"created database {dbname}")

    with connect_db(dbname, cfg) as conn:
        # Session settings so generation speed does not depend on a broken server config.
        conn.execute("SET maintenance_work_mem = '256MB'")
        conn.execute("SET max_parallel_maintenance_workers = 2")
        conn.execute("SET synchronous_commit = off")
        conn.execute("SET work_mem = '64MB'")
        conn.execute(META_SQL)
        meta = read_meta(conn)
        if reset or (meta.get("profile") and meta["profile"] != profile.to_dict()):
            if not reset:
                say(f"profile changed ({meta['profile'].get('name')} -> {profile.name}); resetting")
            say("dropping workload tables")
            for table in (*WL_TABLES, *SCRATCH_TABLES, *PGBENCH_TABLES):
                conn.execute(
                    sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(sql.Identifier(table))
                )
            conn.execute("DELETE FROM wl_meta")
            meta = {}
        if meta.get("seed") not in (None, seed):
            raise ValueError(
                f"{dbname} was generated with seed {meta['seed']}; use reset to change it"
            )
        _set_meta(conn, "profile", profile.to_dict())
        _set_meta(conn, "seed", seed)
        _set_meta(conn, "generator_version", GENERATOR_VERSION)
        complete = set(meta.get("complete", []))

        def run_step(name: str, action: Callable[[], None]) -> None:
            if name in complete:
                report.skipped.append(name)
                say(f"{name}: already complete, skipped")
                return
            say(f"{name}: generating ...")
            t0 = time.monotonic()
            action()
            report.steps[name] = time.monotonic() - t0
            complete.add(name)
            _set_meta(conn, "complete", sorted(complete))
            say(f"{name}: done in {report.steps[name]:.1f}s")

        def run_sql(table: str, stmts: list[str]) -> Callable[[], None]:
            def action() -> None:
                conn.execute("SELECT setseed(%s)", (_seed_value(seed, table),))
                for stmt in stmts:
                    conn.execute(stmt)

            return action

        def pgbench_init() -> None:
            # d: drop, t: create tables, G: server-side data generation, v: vacuum, p: keys.
            compose_exec(
                [
                    *pgbench_args(dbname, cfg),
                    "-i",
                    "-q",
                    "-I",
                    "dtGvp",
                    "-s",
                    str(profile.pgbench_scale),
                ],
                cfg,
            )

        run_step("pgbench", pgbench_init)
        run_step("wl_customers", run_sql("wl_customers", customers_sql(profile)))
        run_step("wl_products", run_sql("wl_products", products_sql(profile)))
        run_step("wl_orders", run_sql("wl_orders", orders_sql(profile)))
        run_step(
            "wl_campaign_targets", run_sql("wl_campaign_targets", campaign_targets_sql(profile))
        )
        conn.execute("CHECKPOINT")

        report.sizes = relation_sizes(conn)
        _set_meta(conn, "sizes", report.sizes)
        _set_meta(conn, "generated_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    return report


def is_generated(conn: psycopg.Connection) -> bool:
    """True when every generated table is marked complete in wl_meta."""
    meta = read_meta(conn)
    return set(GENERATED_TABLES) <= set(meta.get("complete", []))


def db_connect_for(dbname: str | None, cfg: Config | None = None) -> psycopg.Connection:
    """Alias kept short for callers: main database when ``dbname`` is None."""
    if dbname is None:
        return db.connect(cfg)
    return connect_db(dbname, cfg)
