"""Shared configuration: paths and settings for every stage.

Values come from the process environment first, then from ``.env`` in the
repository root, then from the defaults below (which match ``.env.example``
and the defaults in ``compose.yaml``). ``.env`` is read but never exported
into ``os.environ``; code that needs a value takes it from the ``Config``
object, e.g. ``anthropic.Anthropic(api_key=cfg.anthropic_api_key)``.

Usage::

    from pgdiag.config import get_config
    cfg = get_config()
    cfg.raw_dir, cfg.db_connect_kwargs, cfg.user_agent, ...
"""

from __future__ import annotations

import datetime as _dt
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values

DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
MIN_CRAWL_DELAY_SECONDS = 1.0  # postgresql.org politeness floor: at most one request per second
USER_AGENT_PRODUCT = "pg-misconfig-rag-research/0.1"


def find_repo_root() -> Path:
    """Return the repository root.

    ``PGDIAG_ROOT`` wins if set. Otherwise walk up from this file (the package
    is installed editable from ``src/``) to the directory holding
    ``pyproject.toml``, falling back to the current directory.
    """
    override = os.environ.get("PGDIAG_ROOT")
    if override:
        return Path(override).resolve()
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    return Path.cwd()


@dataclass(frozen=True)
class Config:
    # --- paths (all under the repository root) ---
    root: Path
    data_dir: Path  # data/            everything generated or downloaded
    raw_dir: Path  # data/raw/         cached downloads; never fetched twice
    chunks_dir: Path  # data/chunks/   chunk files produced by ingestion
    index_dir: Path  # data/index/     the persistent RAG index
    pg_logs_dir: Path  # data/pg_logs/ test-database server logs (bind mount)
    results_dir: Path  # results/      experiment outputs

    # --- test database ---
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str = field(repr=False)

    # --- diagnosis LLM ---
    anthropic_api_key: str | None = field(repr=False)
    anthropic_model: str | None

    # --- RAG index ---
    embedding_model: str

    # --- crawling postgresql.org ---
    crawl_delay_seconds: float
    crawler_contact: str | None
    mailing_list_start_year: int
    mailing_list_end_year: int

    @property
    def user_agent(self) -> str:
        """Generic User-Agent; a contact is appended only if CRAWLER_CONTACT is set."""
        details = "research crawler; max 1 req/s"
        if self.crawler_contact:
            details += f"; contact: {self.crawler_contact}"
        return f"{USER_AGENT_PRODUCT} ({details})"

    @property
    def db_connect_kwargs(self) -> dict[str, str | int]:
        """Keyword arguments for ``psycopg.connect``."""
        return {
            "host": self.db_host,
            "port": self.db_port,
            "dbname": self.db_name,
            "user": self.db_user,
            "password": self.db_password,
        }

    def ensure_dirs(self) -> None:
        """Create the data and results directories this process may write to."""
        for path in (self.raw_dir, self.chunks_dir, self.index_dir, self.results_dir):
            path.mkdir(parents=True, exist_ok=True)


def load_config(env: Mapping[str, str] | None = None, root: Path | None = None) -> Config:
    """Build a Config.

    With ``env=None`` the values are ``os.environ`` layered over ``<root>/.env``.
    Passing ``env`` uses exactly that mapping (no ``.env``, no ``os.environ``);
    tests use this. Empty strings count as "not set".
    """
    root = (root or find_repo_root()).resolve()
    if env is None:
        file_values = {k: v for k, v in dotenv_values(root / ".env").items() if v is not None}
        env = {**file_values, **os.environ}

    def get(name: str, default: str | None = None) -> str | None:
        value = env.get(name, "").strip()
        return value or default

    data_dir = root / "data"
    end_year = get("MAILING_LIST_END_YEAR")
    return Config(
        root=root,
        data_dir=data_dir,
        raw_dir=data_dir / "raw",
        chunks_dir=data_dir / "chunks",
        index_dir=data_dir / "index",
        pg_logs_dir=data_dir / "pg_logs",
        results_dir=root / "results",
        db_host=get("PGDIAG_DB_HOST", "localhost"),
        db_port=int(get("PGDIAG_DB_PORT", "5433")),
        db_name=get("PGDIAG_DB_NAME", "pgdiag"),
        db_user=get("PGDIAG_DB_USER", "pgdiag"),
        db_password=get("PGDIAG_DB_PASSWORD", "pgdiag"),
        anthropic_api_key=get("ANTHROPIC_API_KEY"),
        anthropic_model=get("ANTHROPIC_MODEL"),
        embedding_model=get("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
        crawl_delay_seconds=max(
            MIN_CRAWL_DELAY_SECONDS, float(get("CRAWL_DELAY_SECONDS", str(MIN_CRAWL_DELAY_SECONDS)))
        ),
        crawler_contact=get("CRAWLER_CONTACT"),
        mailing_list_start_year=int(get("MAILING_LIST_START_YEAR", "2015")),
        mailing_list_end_year=int(end_year) if end_year else _dt.date.today().year,
    )


@lru_cache(maxsize=1)
def get_config() -> Config:
    """The process-wide Config (environment + ``.env``), loaded once."""
    return load_config()
