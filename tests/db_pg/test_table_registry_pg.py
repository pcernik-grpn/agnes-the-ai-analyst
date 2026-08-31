"""PG-only tests for ``TableRegistryPgRepository``'s auto-draft dedup flag.

``mark_semantic_draft_pending`` / ``clear_semantic_draft_pending``
(semantic-phase5 wave 2) back the headless auto-draft sweep's dedup
bookkeeping. ``table_registry.semantic_draft_pending_at`` is a Postgres-only
column (A3 PG-first ratchet — the DuckDB app-state migration ladder is
frozen at v124, see CLAUDE.md -> "Dual-backend discipline" and
``migrations/versions/0086_semantic_draft_pending.py``), so there is no
DuckDB half to parametrize against here — this exercises the PG repo
directly, following the shape of ``tests/db_pg/test_recipes_pg.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def repo(pg_engine, monkeypatch):
    """Per-test ``table_registry`` PG repo bound to a freshly-migrated schema."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from src.repositories.table_registry_pg import TableRegistryPgRepository

    return TableRegistryPgRepository(db_pg.get_engine())


def _seed(repo, id: str, name: str) -> None:
    repo.register(id=id, name=name)


def test_starts_unset(repo):
    _seed(repo, "t1", "Table One")
    row = repo.get("t1")
    assert row["semantic_draft_pending_at"] is None


def test_mark_sets_a_timestamp(repo):
    _seed(repo, "t1", "Table One")
    repo.mark_semantic_draft_pending("t1")
    row = repo.get("t1")
    assert row["semantic_draft_pending_at"] is not None


def test_clear_unsets_it(repo):
    _seed(repo, "t1", "Table One")
    repo.mark_semantic_draft_pending("t1")
    repo.clear_semantic_draft_pending("t1")
    row = repo.get("t1")
    assert row["semantic_draft_pending_at"] is None


def test_mark_and_clear_are_scoped_to_one_row(repo):
    _seed(repo, "t1", "Table One")
    _seed(repo, "t2", "Table Two")
    repo.mark_semantic_draft_pending("t1")
    assert repo.get("t2")["semantic_draft_pending_at"] is None
