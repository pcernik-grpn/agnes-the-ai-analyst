"""``chat_message_feedback`` — the chat turn thumbs signal, on Postgres.

PG-side by necessity: ``chat_message_feedback`` is a Postgres-only table (A3
PG-first ratchet — ``CLAUDE.md`` -> "Dual-backend discipline"), matching
``tests/db_pg/test_semantic_feedback_pg.py``'s PG-only shape.

Design: ``docs/superpowers/specs/2026-09-08-llm-observability-design.md``
§3.5, §3.7; plan: ``docs/superpowers/plans/2026-09-08-llm-observability.md``
Task 6.
"""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def repo(pg_engine):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    from src.repositories.chat_message_feedback_pg import ChatMessageFeedbackPgRepository

    return ChatMessageFeedbackPgRepository(pg_engine)


def test_upsert_is_one_row_per_turn_and_user(repo):
    a = repo.upsert(session_id="s1", turn_id="t1", user_id="u1", verdict="down", comment="wrong number")
    b = repo.upsert(session_id="s1", turn_id="t1", user_id="u1", verdict="up", comment=None)
    assert a["id"] == b["id"] and b["verdict"] == "up" and b["comment"] is None
    assert b["updated_at"] >= a["created_at"]
    repo.upsert(session_id="s1", turn_id="t1", user_id="u2", verdict="down")
    assert len(repo.list_feedback()) == 2
    assert [r["user_id"] for r in repo.list_feedback(verdict="down")] == ["u2"]
    assert repo.get("t1", "u1")["verdict"] == "up" and repo.get("t1", "nobody") is None


def test_upsert_stores_the_message_id(repo):
    row = repo.upsert(session_id="s1", turn_id="t1", user_id="u1", verdict="up", message_id="msg_1")
    assert row["message_id"] == "msg_1"


def test_list_feedback_since_filters_by_created_at(repo):
    from datetime import datetime, timedelta

    repo.upsert(session_id="s1", turn_id="t1", user_id="u1", verdict="up")
    future = datetime.now(UTC) + timedelta(minutes=1)
    assert repo.list_feedback(since=future) == []


def test_prune_older_than(repo):
    from datetime import datetime, timedelta

    import sqlalchemy as sa

    repo.upsert(session_id="s1", turn_id="t1", user_id="u1", verdict="up")
    old = datetime.now(UTC) - timedelta(days=40)
    with repo._engine.begin() as conn:
        conn.execute(sa.text("UPDATE chat_message_feedback SET created_at = :old"), {"old": old})
    assert repo.prune_older_than(30) == 1
    assert repo.list_feedback() == []
