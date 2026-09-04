"""Alembic 0106 — the two hand-built hot-path indexes (TCRD-296 gaps #43,
#44), exercised against a real Postgres instead of the fake-``op`` unit test
in ``tests/db_pg/test_hot_path_indexes_migration.py``.

Covers what that unit test cannot: the indexes actually exist in
``pg_indexes`` after upgrading, a second ``upgrade`` to the same revision is
a no-op (the live ``IF NOT EXISTS`` behavior, not just the SQL text a fake
recorded), and ``downgrade`` actually removes them.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[2]

_PREV = "0105_fact_collection_stats"
_THIS = "0106_hot_path_indexes"

_CORPUS_CHUNKS_INDEX = "idx_corpus_chunks_corpus_id"
_FACT_ALIASES_INDEX = "idx_fact_aliases_natural_key"


def _alembic_config(db_url: str):
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = db_url
    return cfg


def _index_names(conn, table_name: str) -> set[str]:
    rows = conn.execute(
        sa.text("SELECT indexname FROM pg_indexes WHERE tablename = :t"),
        {"t": table_name},
    ).fetchall()
    return {row[0] for row in rows}


def test_upgrade_creates_both_indexes(pg_engine):
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, _PREV)
    command.upgrade(cfg, _THIS)

    with pg_engine.connect() as conn:
        assert _CORPUS_CHUNKS_INDEX in _index_names(conn, "corpus_chunks")
        assert _FACT_ALIASES_INDEX in _index_names(conn, "fact_aliases")


def test_upgrade_is_idempotent(pg_engine):
    """Re-running 0106 against a database that already has the indexes
    (e.g. an operator hand-built one during the incident) must not raise."""
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, _PREV)
    command.upgrade(cfg, _THIS)
    command.upgrade(cfg, _THIS)  # re-run must not raise or change anything

    with pg_engine.connect() as conn:
        assert _CORPUS_CHUNKS_INDEX in _index_names(conn, "corpus_chunks")
        assert _FACT_ALIASES_INDEX in _index_names(conn, "fact_aliases")


def test_downgrade_removes_both_indexes(pg_engine):
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, _THIS)
    command.downgrade(cfg, _PREV)

    with pg_engine.connect() as conn:
        assert _CORPUS_CHUNKS_INDEX not in _index_names(conn, "corpus_chunks")
        assert _FACT_ALIASES_INDEX not in _index_names(conn, "fact_aliases")
