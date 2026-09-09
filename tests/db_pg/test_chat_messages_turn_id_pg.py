"""``chat_messages.turn_id`` — the chat turn a message was produced in
(design 2026-09-08 §3.2, §3.7, migration 0113).

Postgres writes and reads the column; the frozen DuckDB app-state backend
accepts the keyword and drops it (the same ``chat_messages`` cache-token
precedent, migration ``0092``) — both halves are exercised here so a change
to either side is caught in the same file.
"""

from __future__ import annotations

from pathlib import Path


from src.duckdb_conn import _open_duckdb
import pytest

from app.chat.persistence import ChatRepository
from app.chat.types import Surface
from src.db import _ensure_schema

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def pg_repo(pg_engine):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    from src.repositories.chat_messages_pg import ChatMessagePgRepository

    return ChatMessagePgRepository(pg_engine)


def test_pg_append_message_stores_turn_id_and_list_messages_returns_it(pg_repo):
    from src.repositories.chat_sessions_pg import ChatSessionPgRepository

    sessions = ChatSessionPgRepository(pg_repo._engine)
    session = sessions.create_session(user_email="a@test.com", surface=Surface.WEB)

    pg_repo.append_message(session_id=session.id, role="user", content="hi", turn_id="t1")
    got = pg_repo.list_messages(session.id)
    assert got[0].turn_id == "t1"

    recent = pg_repo.list_recent_messages(session.id)
    assert recent[0].turn_id == "t1"


def test_pg_append_message_without_turn_id_is_none(pg_repo):
    from src.repositories.chat_sessions_pg import ChatSessionPgRepository

    sessions = ChatSessionPgRepository(pg_repo._engine)
    session = sessions.create_session(user_email="a@test.com", surface=Surface.WEB)

    msg = pg_repo.append_message(session_id=session.id, role="user", content="hi")
    assert msg.turn_id is None


def test_duckdb_backend_accepts_and_drops_turn_id():
    conn = _open_duckdb(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    session = repo.create_session(user_email="a@test.com", surface=Surface.WEB)

    msg = repo.append_message(session_id=session.id, role="user", content="hi", turn_id="t1")
    assert msg.turn_id is None

    got = repo.list_messages(session.id)
    assert got[0].turn_id is None
