"""Alembic 0090 — ``semantic_models`` detach/override columns (F3, PG-only).

Mirrors ``tests/db_pg/test_alembic_0073_agent_scope_granted_by.py``'s shape:
drive the chain up to the revision immediately before this one, insert a row
by hand, upgrade past it, and assert on the raw table. No backfill logic here
(all six columns are plain additive columns — ``sync_mode`` defaults to
``'synced'``, everything else defaults to ``NULL``), so this file is a
column-presence + default-value + downgrade check, not a data-migration test.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[2]

_PREV = "0089_semantic_health_mutes"
_THIS = "0090_semantic_models_detach"

_NEW_COLS = {
    "sync_mode",
    "detached_at",
    "detached_by",
    "detach_base_hash",
    "source_content_hash",
    "source_missing_since",
}


def _alembic_config(db_url: str):
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = db_url
    return cfg


def _insert_model(conn, *, id, slug) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO semantic_models "
            "(id, slug, name, document, spec_version, content_hash, source, status, created_at, updated_at) "
            "VALUES (:id, :slug, :slug, 'doc', '0.2.0.dev0', 'h1', 'manual', 'valid', now(), now())"
        ),
        {"id": id, "slug": slug},
    )


def test_0077_adds_the_columns(pg_engine):
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, _THIS)

    with pg_engine.connect() as conn:
        cols = {c["name"] for c in sa.inspect(conn).get_columns("semantic_models")}
    assert _NEW_COLS <= cols


def test_0077_existing_rows_default_to_synced_with_no_detach_metadata(pg_engine):
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, _PREV)

    with pg_engine.begin() as conn:
        _insert_model(conn, id="manual/_/pre-existing", slug="pre-existing")

    command.upgrade(cfg, _THIS)

    with pg_engine.connect() as conn:
        row = (
            conn.execute(
                sa.text(
                    "SELECT sync_mode, detached_at, detached_by, detach_base_hash, "
                    "source_content_hash, source_missing_since FROM semantic_models WHERE id = :id"
                ),
                {"id": "manual/_/pre-existing"},
            )
            .mappings()
            .one()
        )
    assert row["sync_mode"] == "synced"
    assert row["detached_at"] is None
    assert row["detached_by"] is None
    assert row["detach_base_hash"] is None
    assert row["source_content_hash"] is None
    assert row["source_missing_since"] is None


def test_0077_noops_gracefully_when_semantic_models_is_absent(pg_engine):
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, _PREV)

    with pg_engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE IF EXISTS semantic_models CASCADE"))

    command.upgrade(cfg, _THIS)  # must not raise


def test_0077_downgrade_drops_the_columns(pg_engine):
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, _THIS)
    command.downgrade(cfg, _PREV)

    with pg_engine.connect() as conn:
        cols = {c["name"] for c in sa.inspect(conn).get_columns("semantic_models")}
    assert not (_NEW_COLS & cols)
