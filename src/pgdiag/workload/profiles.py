"""Data-size profiles shared by the generator, the query set and the benchmark.

``full`` is the real experiment dataset (several GB, larger than the container's
1 GiB memory limit). ``tiny`` is for the integration tests: same tables, same
queries, generated in seconds into a separate database so the real dataset is
never touched. Every field is an integer except ``name`` and is available to
the SQL files as a ``%(field)s`` placeholder.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields


@dataclass(frozen=True)
class DataProfile:
    name: str
    pgbench_scale: int  # pgbench -i -s
    customers: int  # wl_customers rows
    products: int  # wl_products rows
    orders: int  # wl_orders rows (the big fact table)
    order_days: int  # wl_orders.order_date spans this many days from 2023-01-01
    campaigns: int  # wl_campaign_targets: number of campaigns ...
    campaign_size: int  # ... and customers per campaign
    tasks: int  # wl_tasks live rows (work queue, re-created at the start of each benchmark)
    task_batch: int  # tasks enqueued, claimed, completed and purged per pass
    ledger: int  # wl_ledger rows (bulk-load table, re-loaded in every pass)
    pgbench_transactions: int  # per client, per pass (pgbench -t)
    pgbench_clients: int

    def to_dict(self) -> dict:
        return asdict(self)

    def int_params(self) -> dict[str, int]:
        """The integer fields, i.e. the placeholders SQL files may use."""
        return {k: v for k, v in asdict(self).items() if isinstance(v, int)}


INT_FIELDS: tuple[str, ...] = tuple(f.name for f in fields(DataProfile) if f.type in (int, "int"))

PROFILES: dict[str, DataProfile] = {
    "full": DataProfile(
        name="full",
        pgbench_scale=10,
        customers=1_000_000,
        products=50_000,
        orders=20_000_000,
        order_days=730,
        campaigns=40,
        campaign_size=15_000,
        tasks=400_000,
        task_batch=100_000,
        ledger=1_000_000,
        pgbench_transactions=1_500,
        pgbench_clients=4,
    ),
    "tiny": DataProfile(
        name="tiny",
        pgbench_scale=1,
        customers=10_000,
        products=1_000,
        orders=100_000,
        order_days=730,
        campaigns=4,
        campaign_size=150,
        tasks=4_000,
        task_batch=1_000,
        ledger=10_000,
        pgbench_transactions=20,
        pgbench_clients=2,
    ),
}

DEFAULT_PROFILE = "full"


def get_profile(name: str) -> DataProfile:
    try:
        return PROFILES[name]
    except KeyError:
        raise ValueError(f"unknown data profile {name!r}; choose from {sorted(PROFILES)}") from None
