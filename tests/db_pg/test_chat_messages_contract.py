"""Cross-engine contract for ``ChatMessagePgRepository.list_for_sessions``.

Parametrises over [DuckDB ``ChatRepository``, Postgres
``ChatMessagePgRepository``] -- a frozen pre-A3 pair (see CLAUDE.md, "Dual-
backend discipline"). ``append_message``/``list_messages`` already have
dedicated coverage in ``tests/db_pg/test_chat_messages_turn_id_pg.py``; this
file is only for the bulk-by-session-id read the conversation-corpus export
uses (design 2026-09-08 §3.12) -- grouping, within-session ordering, and the
empty-input short-circuit.

Follows the pattern of ``test_chat_sessions_contract.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.chat.types import Surface

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_duckdb_repo(tmp_path):
    from app.chat.persistence import ChatRepository
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return ChatRepository(conn)


class _PgChatFacade:
    """Bundles ``ChatMessagePgRepository`` + ``ChatSessionPgRepository``
    behind the ``create_session``/``append_message``/``list_for_sessions``
    surface DuckDB's ``ChatRepository`` exposes natively on one object --
    so the parametrized tests below call identical methods on both
    backends."""

    def __init__(self, engine):
        from src.repositories.chat_messages_pg import ChatMessagePgRepository
        from src.repositories.chat_sessions_pg import ChatSessionPgRepository

        self._messages = ChatMessagePgRepository(engine)
        self._sessions = ChatSessionPgRepository(engine)

    def create_session(self, **kwargs):
        return self._sessions.create_session(**kwargs)

    def append_message(self, **kwargs):
        return self._messages.append_message(**kwargs)

    def list_for_sessions(self, session_ids):
        return self._messages.list_for_sessions(session_ids)

    def has_turn(self, session_id, turn_id):
        return self._messages.has_turn(session_id, turn_id)


def _make_pg_repo(pg_engine):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")
    return _PgChatFacade(pg_engine)


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path):
    if request.param == "duckdb":
        return _make_duckdb_repo(tmp_path)
    return _make_pg_repo(request.getfixturevalue("pg_engine"))


def test_list_for_sessions_groups_by_session_ordered_oldest_first(repo):
    s1 = repo.create_session(user_email="u@x.com", surface=Surface.WEB)
    s2 = repo.create_session(user_email="u@x.com", surface=Surface.WEB)
    repo.append_message(session_id=s1.id, role="user", content="first")
    repo.append_message(session_id=s1.id, role="assistant", content="second")
    repo.append_message(session_id=s2.id, role="user", content="other-session")

    by_session = repo.list_for_sessions([s1.id, s2.id])

    assert set(by_session.keys()) == {s1.id, s2.id}
    assert [m.content for m in by_session[s1.id]] == ["first", "second"]  # oldest first
    assert [m.content for m in by_session[s2.id]] == ["other-session"]


def test_list_for_sessions_omits_sessions_with_no_messages(repo):
    s1 = repo.create_session(user_email="u@x.com", surface=Surface.WEB)
    repo.append_message(session_id=s1.id, role="user", content="hi")

    by_session = repo.list_for_sessions([s1.id, "no-such-session"])

    assert set(by_session.keys()) == {s1.id}


def test_list_for_sessions_empty_input_returns_empty_dict(repo):
    s1 = repo.create_session(user_email="u@x.com", surface=Surface.WEB)
    repo.append_message(session_id=s1.id, role="user", content="hi")

    assert repo.list_for_sessions([]) == {}


def test_has_turn_vouches_only_for_the_sessions_own_turns(repo, request):
    """``has_turn`` is the feedback endpoint's ownership check. Postgres
    answers from ``chat_messages.turn_id``; the frozen DuckDB backend has no
    such column (``append_message`` drops ``turn_id``) and therefore answers
    ``False`` for every turn -- fail closed, never "cannot check, so allow".
    """
    from app.chat.types import Surface

    mine = repo.create_session(user_email="a@test.com", surface=Surface.WEB)
    other = repo.create_session(user_email="b@test.com", surface=Surface.WEB)
    repo.append_message(session_id=mine.id, role="user", content="hello", turn_id="t-mine")
    repo.append_message(session_id=other.id, role="user", content="hi", turn_id="t-other")

    assert repo.has_turn(mine.id, "t-other") is False
    assert repo.has_turn(mine.id, "t-nowhere") is False
    assert repo.has_turn("", "t-mine") is False
    on_postgres = request.node.callspec.params["repo"] == "pg"
    assert repo.has_turn(mine.id, "t-mine") is on_postgres
