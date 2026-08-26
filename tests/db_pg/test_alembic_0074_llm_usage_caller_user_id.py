"""Alembic 0074 — `llm_usage.caller_user_id` column-add (remediation Track
C, task C2.4, per-caller usage attribution).

Mirrors `tests/db_pg/test_alembic_0073_agent_scope_granted_by.py`'s shape:
drive the chain up to the revision immediately before this one, insert a
row by hand, upgrade past it, and assert on the raw table. Unlike 0073,
there is no backfill to prove — a pre-existing row honestly has no known
caller (see the migration's own docstring), so it stays NULL.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[2]

_PREV = "0073_agent_scope_granted_by"
_THIS = "0074_llm_usage_caller_user_id"


def _alembic_config(db_url: str):
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = db_url
    return cfg


def _insert_usage_row(conn, *, id: str) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO llm_usage (id, agent_id, user_id, session_id, model, "
            "input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens) "
            "VALUES (:id, 'a1', 'owner1', 's1', 'claude-sonnet-5', 10, 5, 0, 0)"
        ),
        {"id": id},
    )


def test_0074_adds_the_column(pg_engine):
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, _THIS)

    with pg_engine.connect() as conn:
        cols = {c["name"] for c in sa.inspect(conn).get_columns("llm_usage")}
    assert "caller_user_id" in cols


def test_0074_leaves_pre_existing_rows_null(pg_engine):
    """No backfill (unlike 0073's owner backfill) — a row written before
    this column existed has no knowable caller, so it stays NULL rather
    than being defaulted to something misleading."""
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, _PREV)

    with pg_engine.begin() as conn:
        _insert_usage_row(conn, id="pre-existing")

    command.upgrade(cfg, _THIS)

    with pg_engine.connect() as conn:
        caller = conn.execute(
            sa.text("SELECT caller_user_id FROM llm_usage WHERE id = :id"), {"id": "pre-existing"}
        ).scalar_one()
    assert caller is None


def test_0074_noops_gracefully_when_llm_usage_is_absent(pg_engine):
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, _PREV)

    with pg_engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE IF EXISTS llm_usage CASCADE"))

    command.upgrade(cfg, _THIS)  # must not raise


def test_0074_downgrade_drops_the_column(pg_engine):
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, _THIS)
    command.downgrade(cfg, _PREV)

    with pg_engine.connect() as conn:
        cols = {c["name"] for c in sa.inspect(conn).get_columns("llm_usage")}
    assert "caller_user_id" not in cols
