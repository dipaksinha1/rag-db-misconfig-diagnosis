from __future__ import annotations

import datetime as dt

from dotenv import dotenv_values

from pgdiag.config import DEFAULT_EMBEDDING_MODEL, load_config


def test_defaults_with_empty_environment(tmp_path):
    cfg = load_config(env={}, root=tmp_path)

    assert cfg.root == tmp_path
    assert cfg.raw_dir == tmp_path / "data" / "raw"
    assert cfg.chunks_dir == tmp_path / "data" / "chunks"
    assert cfg.index_dir == tmp_path / "data" / "index"
    assert cfg.pg_logs_dir == tmp_path / "data" / "pg_logs"
    assert cfg.results_dir == tmp_path / "results"

    assert cfg.db_connect_kwargs == {
        "host": "localhost",
        "port": 5433,
        "dbname": "pgdiag",
        "user": "pgdiag",
        "password": "pgdiag",
    }
    assert cfg.anthropic_api_key is None
    assert cfg.anthropic_model is None
    assert cfg.embedding_model == DEFAULT_EMBEDDING_MODEL == "BAAI/bge-small-en-v1.5"
    assert cfg.crawl_delay_seconds == 1.0
    assert cfg.mailing_list_start_year == 2015
    assert cfg.mailing_list_end_year == dt.date.today().year


def test_environment_overrides_defaults(tmp_path):
    cfg = load_config(
        env={
            "PGDIAG_DB_HOST": "db.internal",
            "PGDIAG_DB_PORT": "6000",
            "ANTHROPIC_API_KEY": "sk-test",
            "ANTHROPIC_MODEL": "some-model",
            "EMBEDDING_MODEL": "other/model",
            "CRAWL_DELAY_SECONDS": "2.5",
            "MAILING_LIST_START_YEAR": "2018",
            "MAILING_LIST_END_YEAR": "2020",
        },
        root=tmp_path,
    )
    assert (cfg.db_host, cfg.db_port) == ("db.internal", 6000)
    assert cfg.anthropic_api_key == "sk-test"
    assert cfg.anthropic_model == "some-model"
    assert cfg.embedding_model == "other/model"
    assert cfg.crawl_delay_seconds == 2.5
    assert (cfg.mailing_list_start_year, cfg.mailing_list_end_year) == (2018, 2020)


def test_empty_values_count_as_unset(tmp_path):
    cfg = load_config(env={"ANTHROPIC_API_KEY": "", "PGDIAG_DB_PORT": " "}, root=tmp_path)
    assert cfg.anthropic_api_key is None
    assert cfg.db_port == 5433


def test_crawl_delay_cannot_go_below_one_second(tmp_path):
    cfg = load_config(env={"CRAWL_DELAY_SECONDS": "0.1"}, root=tmp_path)
    assert cfg.crawl_delay_seconds == 1.0


def test_user_agent_is_generic_and_contact_is_optional(tmp_path):
    plain = load_config(env={}, root=tmp_path).user_agent
    assert plain == "pg-misconfig-rag-research/0.1 (research crawler; max 1 req/s)"

    with_contact = load_config(env={"CRAWLER_CONTACT": "ops@example.org"}, root=tmp_path)
    assert with_contact.user_agent == (
        "pg-misconfig-rag-research/0.1 (research crawler; max 1 req/s; contact: ops@example.org)"
    )


def test_secrets_are_not_in_repr(tmp_path):
    cfg = load_config(
        env={"ANTHROPIC_API_KEY": "sk-secret", "PGDIAG_DB_PASSWORD": "hunter2"}, root=tmp_path
    )
    assert "sk-secret" not in repr(cfg)
    assert "hunter2" not in repr(cfg)


def test_dotenv_file_is_read_and_real_environment_wins(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("ANTHROPIC_MODEL=from-file\nEMBEDDING_MODEL=file/model\n")
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    monkeypatch.setenv("EMBEDDING_MODEL", "env/model")

    cfg = load_config(root=tmp_path)

    assert cfg.anthropic_model == "from-file"
    assert cfg.embedding_model == "env/model"


def test_ensure_dirs_creates_output_directories(tmp_path):
    cfg = load_config(env={}, root=tmp_path)
    cfg.ensure_dirs()
    assert cfg.raw_dir.is_dir() and cfg.chunks_dir.is_dir()
    assert cfg.index_dir.is_dir() and cfg.results_dir.is_dir()


def test_env_example_matches_code_defaults(repo_root, tmp_path):
    """Copying .env.example to .env must not change behaviour (apart from the model name)."""
    example = {k: v or "" for k, v in dotenv_values(repo_root / ".env.example").items()}
    assert example["ANTHROPIC_API_KEY"] == "", ".env.example must not contain a key"

    from_example = load_config(env=example, root=tmp_path)
    defaults = load_config(env={"ANTHROPIC_MODEL": example["ANTHROPIC_MODEL"]}, root=tmp_path)
    assert from_example == defaults
