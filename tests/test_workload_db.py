"""Integration tests for the T5 workload against the running test database.

Everything that writes uses a SEPARATE database (``pgdiag_wltest``) generated
with the ``tiny`` profile, so the real dataset in ``pgdiag`` is never touched.
The checks on the real dataset (marker ``data``) only read.
"""

from __future__ import annotations

import os
import shutil

import pytest
from psycopg.types.json import Jsonb

from pgdiag import db
from pgdiag.config import get_config
from pgdiag.settings import TARGET_SETTINGS
from pgdiag.workload import bench, cli, datagen, server
from pgdiag.workload.profiles import PROFILES
from pgdiag.workload.queryset import COMPONENT_IDS, COMPONENTS, load_queries

pytestmark = pytest.mark.db

TEST_DB = "pgdiag_wltest"
TINY = PROFILES["tiny"]


@pytest.fixture(scope="module")
def tiny_db(db_conn):
    """Generate the tiny profile into its own database once per module."""
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not available (pgbench runs inside the container)")
    report = datagen.generate(TINY, dbname=TEST_DB, reset=True)
    yield report
    datagen.drop_database(TEST_DB)


@pytest.fixture()
def tiny_conn(tiny_db):
    with datagen.connect_db(TEST_DB) as conn:
        yield conn


def count(conn, table: str) -> int:
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_generator_creates_expected_tables_and_row_counts(tiny_db, tiny_conn):
    assert set(tiny_db.steps) == set(datagen.GENERATED_TABLES)
    assert count(tiny_conn, "wl_customers") == TINY.customers
    assert count(tiny_conn, "wl_products") == TINY.products
    assert count(tiny_conn, "wl_orders") == TINY.orders
    assert count(tiny_conn, "wl_campaign_targets") == TINY.campaigns * TINY.campaign_size
    assert count(tiny_conn, "pgbench_accounts") == 100_000 * TINY.pgbench_scale
    assert count(tiny_conn, "pgbench_branches") == TINY.pgbench_scale
    assert datagen.is_generated(tiny_conn)
    meta = datagen.read_meta(tiny_conn)
    assert meta["profile"] == TINY.to_dict()
    assert meta["seed"] == datagen.DEFAULT_SEED


def test_generated_data_has_the_designed_shape(tiny_conn):
    # customer_id is a permutation of 1..N
    lo, hi, distinct = tiny_conn.execute(
        "SELECT min(customer_id), max(customer_id), count(DISTINCT customer_id) FROM wl_customers"
    ).fetchone()
    assert (lo, hi, distinct) == (1, TINY.customers, TINY.customers)
    # product_id is heavily skewed: the top product has several percent of all orders
    top_share = tiny_conn.execute(
        "SELECT max(n)::float / sum(n) FROM (SELECT count(*) AS n FROM wl_orders "
        "GROUP BY product_id) s"
    ).fetchone()[0]
    assert top_share > 0.03
    # warehouse_id determines region; city determines country
    assert (
        tiny_conn.execute(
            "SELECT count(*) FROM (SELECT warehouse_id FROM wl_orders GROUP BY 1 "
            "HAVING count(DISTINCT region) > 1) s"
        ).fetchone()[0]
        == 0
    )
    assert (
        tiny_conn.execute(
            "SELECT count(*) FROM wl_customers WHERE left(city, 2) <> country"
        ).fetchone()[0]
        == 0
    )
    # orders are appended in time order
    corr = tiny_conn.execute(
        "SELECT correlation FROM pg_stats WHERE tablename = 'wl_orders' AND attname = 'order_date'"
    ).fetchone()[0]
    assert corr > 0.99
    stats = tiny_conn.execute(
        "SELECT count(*) FROM pg_statistic_ext WHERE stxname LIKE 'wl_%%'"
    ).fetchone()[0]
    assert stats == 2


def test_generation_is_deterministic_and_resumable(tiny_db, tiny_conn):
    fingerprint = (
        "SELECT sum(customer_id::bigint * product_id), sum(amount), "
        "count(DISTINCT status) FROM wl_orders"
    )
    before = tiny_conn.execute(fingerprint).fetchone()
    again = datagen.generate(TINY, dbname=TEST_DB)  # no reset: everything is complete
    assert again.steps == {}
    assert sorted(again.skipped) == sorted(datagen.GENERATED_TABLES)
    # re-create one table from scratch with the same seed: identical rows
    with datagen.connect_db(TEST_DB) as conn:
        meta = datagen.read_meta(conn)
        complete = [t for t in meta["complete"] if t != "wl_orders"]
        conn.execute(
            "UPDATE wl_meta SET value = %s::jsonb WHERE key = 'complete'",
            (Jsonb(complete),),
        )
    rebuilt = datagen.generate(TINY, dbname=TEST_DB)
    assert list(rebuilt.steps) == ["wl_orders"]
    assert tiny_conn.execute(fingerprint).fetchone() == before


def test_seed_mismatch_requires_reset(tiny_db):
    with pytest.raises(ValueError, match="seed"):
        datagen.generate(TINY, dbname=TEST_DB, seed=datagen.DEFAULT_SEED + 1)


def test_every_query_runs(tiny_db, tiny_conn):
    bench.setup_benchmark(tiny_conn, TINY, lambda msg: None)
    bench.reset_pass(tiny_conn)
    for q in load_queries():
        elapsed = bench.run_query(tiny_conn, q, TINY)
        assert elapsed >= 0, q.id
    # the work queue keeps its size and its steady-state status mix
    assert count(tiny_conn, "wl_tasks") == TINY.tasks
    running = tiny_conn.execute(
        "SELECT count(*) FROM wl_tasks WHERE status = 'running'"
    ).fetchone()[0]
    assert running == TINY.task_batch
    assert count(tiny_conn, "wl_ledger") == TINY.ledger
    assert tiny_conn.execute("SELECT to_regclass('wl_ledger_account_idx')").fetchone()[0]


def test_every_query_explains(tiny_db, tiny_conn):
    from pgdiag.workload.queryset import render_offline

    bench.setup_benchmark(tiny_conn, TINY, lambda msg: None)
    bench.reset_pass(tiny_conn)
    for q in load_queries():
        if q.sql.lstrip().upper().startswith(("VACUUM", "CREATE")):
            continue  # not explainable
        plan = tiny_conn.execute("EXPLAIN " + render_offline(q, TINY, 1)).fetchall()
        assert plan, q.id


@pytest.mark.slow
def test_one_pass_benchmark_produces_a_valid_result(tiny_db, tmp_path):
    out = tmp_path / "smoke.json"
    res = bench.run_benchmark("smoke", "test-smoke", dbname=TEST_DB, out_path=out)
    assert res.ok, res.errors
    assert res.path == str(out) and out.exists()
    loaded = bench.load_result(out)
    assert loaded.total_median == pytest.approx(res.total_median)
    assert len(loaded.passes) == 1 and not loaded.passes[0].warmup
    assert set(loaded.passes[0].components) == set(COMPONENT_IDS)
    pgbench_components = [c.id for c in COMPONENTS if c.kind == "pgbench"]
    assert set(loaded.passes[0].pgbench) == set(pgbench_components)
    for stats in loaded.passes[0].pgbench.values():
        assert stats["transactions"] == TINY.pgbench_clients * TINY.pgbench_transactions
        assert stats["failed"] == 0 and stats["tps"] > 0
    server_stats = loaded.passes[0].server_stats
    assert server_stats["wal_bytes"] > 0
    assert server_stats["checkpoints_req"] >= 0
    assert set(loaded.settings) == set(TARGET_SETTINGS)
    assert loaded.data["profile"]["name"] == "tiny"
    assert loaded.container.get("mem_limit_bytes")
    assert loaded.total_median > 0
    diff = bench.compare(loaded, loaded)
    assert diff["total"]["rel_diff_pct"] == 0


def test_session_settings_reach_sql_and_pgbench(tiny_db, tmp_path):
    res = bench.run_benchmark(
        "smoke",
        "test-session",
        dbname=TEST_DB,
        session_settings={"work_mem": "1MB"},
        components=["pgbench", "sort_reports"],
        out_path=tmp_path / "s.json",
    )
    assert res.ok, res.errors
    assert res.components == ["pgbench", "sort_reports"]
    assert res.session_settings == {"work_mem": "1MB"}
    with pytest.raises(ValueError):
        bench.run_benchmark("smoke", dbname=TEST_DB, components=["nope"], save=False)


def test_server_is_at_baseline(db_conn):
    """No ALTER SYSTEM leftovers: earlier tests or tasks must restore the baseline."""
    assert server.baseline_problems(db_conn) == []
    detail = {row["name"]: row for row in db.target_settings(db_conn)}
    for name in TARGET_SETTINGS:
        assert detail[name]["source"] == "configuration file", name


def test_jit_is_available(db_conn):
    assert db_conn.execute("SELECT pg_jit_available()").fetchone()[0] is True
    assert db_conn.execute("SHOW jit").fetchone()[0] == "on"


# ---------------------------------------------------------------------------
# the real dataset (read-only)


def _skip_or_fail(reason: str) -> None:
    if os.environ.get("PGDIAG_REQUIRE_DATA") == "1":
        pytest.fail(reason)
    pytest.skip(reason)


@pytest.mark.data
def test_real_dataset_is_larger_than_the_container_memory(db_conn):
    meta = datagen.read_meta(db_conn)
    if not set(datagen.GENERATED_TABLES) <= set(meta.get("complete", [])):
        _skip_or_fail("full dataset not generated: run `pgdiag-workload generate`")
    assert meta["profile"]["name"] == "full"
    sizes = datagen.relation_sizes(db_conn)
    total = sum(v for k, v in sizes.items() if k not in datagen.SCRATCH_TABLES)
    limit = db.container_info().get("mem_limit_bytes") or (1 << 30)
    assert total > 2.5 * limit, (total, limit)
    assert db_conn.execute("SELECT count(*) FROM wl_orders").fetchone()[0] == (
        PROFILES["full"].orders
    )


@pytest.mark.data
def test_stored_baseline_is_valid():
    path = cli.baseline_path("full", get_config())
    if not path.exists():
        _skip_or_fail(f"{path} missing: run `pgdiag-workload baseline`")
    res = bench.load_result(path)
    assert res.ok
    assert res.preset["name"] == "full"
    assert res.data["profile"]["name"] == "full"
    assert set(res.settings) == set(TARGET_SETTINGS)
    assert not res.session_settings
    assert res.summary["total"]["spread_pct"] < 10
