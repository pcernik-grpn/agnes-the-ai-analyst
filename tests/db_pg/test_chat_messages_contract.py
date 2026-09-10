"""Cross-engine contract for ``ChatMessagePgRepository.list_for_sessions``
and ``list_messages``.

Parametrises over [DuckDB ``ChatRepository``, Postgres
``ChatMessagePgRepository``] -- a frozen pre-A3 pair (see CLAUDE.md, "Dual-
backend discipline"). ``append_message`` and ``turn_id`` already have
dedicated coverage in ``tests/db_pg/test_chat_messages_turn_id_pg.py``; this
file covers the bulk-by-session-id read the conversation-corpus export uses
(design 2026-09-08 §3.12) -- grouping, within-session ordering, and the
empty-input short-circuit -- plus ``list_messages``'s own pagination cursor,
including the page-boundary tie fix (both backends order/resume on
``(created_at, id)``, never ``created_at`` alone).

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

    def list_messages(self, session_id, *, after_id=None, limit=500):
        return self._messages.list_messages(session_id, after_id=after_id, limit=limit)


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


def _force_tied_created_at(repo, message_ids, tied_at):
    """Test-only: overwrite ``created_at`` for *message_ids* to the SAME
    value on whichever backend *repo* wraps, simulating the same-millisecond
    write a real workload under load can produce without needing genuinely
    concurrent inserts to land in the same instant."""
    if isinstance(repo, _PgChatFacade):
        import sqlalchemy as sa

        with repo._messages._engine.begin() as conn:
            for message_id in message_ids:
                conn.execute(
                    sa.text("UPDATE chat_messages SET created_at = :ts WHERE id = :id"),
                    {"ts": tied_at, "id": message_id},
                )
    else:
        for message_id in message_ids:
            repo._conn.execute("UPDATE chat_messages SET created_at = ? WHERE id = ?", [tied_at, message_id])


def test_list_messages_pagination_does_not_drop_a_page_boundary_tie(repo):
    """``list_messages``'s cursor resumes on ``after_id``, resolved to that
    row's ``created_at``. Ordering/resuming on ``created_at`` ALONE loses
    the rest of a tie once the cursor lands mid-tie: a page ending on one
    of several same-timestamp rows would resume with ``created_at >
    cutoff``, which skips every sibling that shares the cutoff's exact
    timestamp. Both backends now order and resume on ``(created_at, id)``,
    which makes a position INSIDE a tie representable."""
    from datetime import UTC, datetime

    session = repo.create_session(user_email="u@x.com", surface=Surface.WEB)
    m1 = repo.append_message(session_id=session.id, role="user", content="m1")
    m2 = repo.append_message(session_id=session.id, role="user", content="m2")
    m3 = repo.append_message(session_id=session.id, role="user", content="m3")

    tied_at = datetime(2026, 1, 1, tzinfo=UTC)
    _force_tied_created_at(repo, [m1.id, m2.id, m3.id], tied_at)

    # Page through one row at a time -- the adversarial case for a cursor
    # that can only resume "after this timestamp": every row here shares
    # the SAME timestamp, so a tie-blind cursor would return the first row
    # forever (or the tail would never surface).
    page1 = repo.list_messages(session.id, limit=1)
    assert len(page1) == 1
    page2 = repo.list_messages(session.id, after_id=page1[-1].id, limit=1)
    assert len(page2) == 1
    page3 = repo.list_messages(session.id, after_id=page2[-1].id, limit=1)
    assert len(page3) == 1
    page4 = repo.list_messages(session.id, after_id=page3[-1].id, limit=1)
    assert page4 == []  # nothing left -- all three were actually consumed

    seen_ids = [page1[0].id, page2[0].id, page3[0].id]
    assert set(seen_ids) == {m1.id, m2.id, m3.id}, "a page-boundary tie must not drop a message"
    assert len(seen_ids) == len(set(seen_ids)), "a page-boundary tie must not repeat a message"
