"""Alembic 0073 — `agent_scope.granted_by` column-add + owner backfill
(remediation Track C, task C2.1).

Mirrors `tests/db_pg/test_alembic_0070_builder_scope.py`'s shape: drive the
chain up to the revision immediately before this one, insert rows by hand,
upgrade past it, and assert on the raw table.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[2]

_PREV = "0072_sync_state_id_v124"
_THIS = "0073_agent_scope_granted_by"


def _alembic_config(db_url: str):
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = db_url
    return cfg


def _insert_agent(conn, *, id, owner_user_id, slug) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO agents (id, owner_user_id, name, slug, created_at, updated_at) "
            "VALUES (:id, :owner, :slug, :slug, now(), now())"
        ),
        {"id": id, "owner": owner_user_id, "slug": slug},
    )


def _insert_scope(conn, *, agent_id, item_type, item_id) -> None:
    conn.execute(
        sa.text("INSERT INTO agent_scope (agent_id, item_type, item_id) VALUES (:a, :t, :i)"),
        {"a": agent_id, "t": item_type, "i": item_id},
    )


def _granted_by(conn, agent_id: str, item_type: str, item_id: str):
    return conn.execute(
        sa.text("SELECT granted_by FROM agent_scope WHERE agent_id = :a AND item_type = :t AND item_id = :i"),
        {"a": agent_id, "t": item_type, "i": item_id},
    ).scalar_one()


def test_0073_adds_the_column(pg_engine):
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, _THIS)

    with pg_engine.connect() as conn:
        cols = {c["name"] for c in sa.inspect(conn).get_columns("agent_scope")}
    assert "granted_by" in cols


def test_0073_backfills_existing_rows_to_the_agents_owner(pg_engine):
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, _PREV)

    agent_id = str(uuid.uuid4())
    with pg_engine.begin() as conn:
        _insert_agent(conn, id=agent_id, owner_user_id="owner-1", slug="pre-existing")
        _insert_scope(conn, agent_id=agent_id, item_type="table", item_id="t1")
        _insert_scope(conn, agent_id=agent_id, item_type="plugin", item_id="p1")

    command.upgrade(cfg, _THIS)

    with pg_engine.connect() as conn:
        assert _granted_by(conn, agent_id, "table", "t1") == "owner-1"
        assert _granted_by(conn, agent_id, "plugin", "p1") == "owner-1"


def test_0073_does_not_overwrite_an_already_set_granted_by(pg_engine):
    """Idempotent replay: a row a LATER write already attributed keeps its
    own writer rather than being reset to the agent's owner."""
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, _THIS)  # column already exists

    agent_id = str(uuid.uuid4())
    with pg_engine.begin() as conn:
        _insert_agent(conn, id=agent_id, owner_user_id="owner-1", slug="already-set")
        conn.execute(
            sa.text(
                "INSERT INTO agent_scope (agent_id, item_type, item_id, granted_by) "
                "VALUES (:a, 'table', 't1', 'admin-1')"
            ),
            {"a": agent_id},
        )

    command.upgrade(cfg, _THIS)  # replay must not clobber the explicit value

    with pg_engine.connect() as conn:
        assert _granted_by(conn, agent_id, "table", "t1") == "admin-1"


def test_0073_noops_gracefully_when_agent_scope_is_absent(pg_engine):
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, _PREV)

    with pg_engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE IF EXISTS agent_scope CASCADE"))

    command.upgrade(cfg, _THIS)  # must not raise


def test_0073_downgrade_drops_the_column(pg_engine):
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, _THIS)
    command.downgrade(cfg, _PREV)

    with pg_engine.connect() as conn:
        cols = {c["name"] for c in sa.inspect(conn).get_columns("agent_scope")}
    assert "granted_by" not in cols
