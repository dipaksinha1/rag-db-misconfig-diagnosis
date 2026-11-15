"""Data-size profiles shared by the generator, the query set and the benchmark.

``standard`` is the experiment dataset (~700 MB on disk, about 3x the
container's 256 MB memory limit). ``tiny`` is for the integration tests: same
tables, same queries, generated in seconds into a separate database so the real
dataset is never touched. Every field is an integer except ``name`` and is
available to the SQL files as a ``%(field)s`` placeholder.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields


@dataclass(frozen=True)
class DataProfile:
    name: str
    pgbench_scale: int  # pgbench -i -s (100,000 accounts per unit)
    customers: int  # wl_customers rows (the dimension table)
    products: int  # distinct product ids in wl_orders (no table; heavily skewed)
    orders: int  # wl_orders rows (the fact table)
    order_days: int  # wl_orders.ordered_at spans this many days from 2023-01-01
    pgbench_transactions: int  # per client, per pass (pgbench -t)
    pgbench_clients: int

    def to_dict(self) -> dict:
        return asdict(self)

    def int_params(self) -> dict[str, int]:
        """The integer fields, i.e. the placeholders SQL files may use."""
        return {k: v for k, v in asdict(self).items() if isinstance(v, int)}


INT_FIELDS: tuple[str, ...] = tuple(f.name for f in fields(DataProfile) if f.type in (int, "int"))

PROFILES: dict[str, DataProfile] = {
    "standard": DataProfile(
        name="standard",
        pgbench_scale=5,
        customers=200_000,
        products=50_000,
        orders=3_500_000,
        order_days=730,
        pgbench_transactions=500,
        pgbench_clients=4,
    ),
    "tiny": DataProfile(
        name="tiny",
        pgbench_scale=1,
        customers=10_000,
        products=2_000,
        orders=100_000,
        order_days=730,
        pgbench_transactions=20,
        pgbench_clients=2,
    ),
}

DEFAULT_PROFILE = "standard"


def get_profile(name: str) -> DataProfile:
    try:
        return PROFILES[name]
    except KeyError:
        raise ValueError(f"unknown data profile {name!r}; choose from {sorted(PROFILES)}") from None
