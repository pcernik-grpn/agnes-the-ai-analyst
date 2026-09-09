"""Cross-engine contract for the chat-session title and archive writes.

Parametrises over [DuckDB ``ChatRepository``, Postgres
``ChatSessionPgRepository``] — a frozen pre-A3 pair (see CLAUDE.md, "Dual-
backend discipline"). The same calls go to both; the same answers must come
back. Covers ``set_title`` (the rename endpoint) and ``set_title_if_unset``
(the auto-title task's conditional write, TCRD-290): fill only while empty,
report whether this call wrote, treat an empty string as unset, and never
invent a row.

Also ``archive_session`` / ``restore_session``, whose contract is not just the
`archived` flag: archiving UNPINS, because a pin means "keep this at the top of
my list" and archiving means "this is not in my list". Two UPDATE statements in
two dialects, one invariant — exactly the drift this file exists to catch.

Also ``list_completed_between`` -- the conversation-corpus export's cursor
source (design 2026-09-08 §3.12): the ``[since, until)`` window, the
``surfaces``/``agent_id`` filters, and keyset paging on
``(last_message_at, id)``. DuckDB has no stored ``last_message_at`` column
(see ``app/chat/persistence.py``'s module docstring) so the seeding helper
below writes the underlying ``chat_messages``/``chat_sessions`` rows
directly on whichever backend ``repo`` is bound to, the same way
``tests/db_pg/test_conversation_export_pg.py``'s ``_seed_session`` does for
Postgres alone.

Follows the pattern of ``test_jobs_contract.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.chat.types import Surface

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_duckdb_repo(tmp_path):
    from app.chat.persistence import ChatRepository
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return ChatRepository(conn)


def _make_pg_repo(pg_engine):
    from alembic import command
    from alembic.config import Config
    from src.repositories.chat_sessions_pg import ChatSessionPgRepository

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")
    return ChatSessionPgRepository(pg_engine)


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path):
    if request.param == "duckdb":
        return _make_duckdb_repo(tmp_path)
    return _make_pg_repo(request.getfixturevalue("pg_engine"))


def test_set_title_overwrites(repo):
    s = repo.create_session(user_email="u@x.com", surface=Surface.WEB)
    repo.set_title(s.id, "First")
    repo.set_title(s.id, "Second")
    assert repo.get_session(s.id).title == "Second"


def test_set_title_if_unset_fills_only_an_empty_title(repo):
    s = repo.create_session(user_email="u@x.com", surface=Surface.WEB)
    assert repo.get_session(s.id).title in (None, "")
    assert repo.set_title_if_unset(s.id, "Model title") is True
    assert repo.get_session(s.id).title == "Model title"


def test_set_title_if_unset_loses_to_an_existing_title(repo):
    s = repo.create_session(user_email="u@x.com", surface=Surface.WEB)
    repo.set_title(s.id, "My own name")  # the user renamed it meanwhile
    assert repo.set_title_if_unset(s.id, "Model title") is False
    assert repo.get_session(s.id).title == "My own name"


def test_set_title_if_unset_treats_empty_string_as_unset(repo):
    s = repo.create_session(user_email="u@x.com", surface=Surface.WEB)
    repo.set_title(s.id, "")
    assert repo.set_title_if_unset(s.id, "Filled") is True
    assert repo.get_session(s.id).title == "Filled"


def test_set_title_if_unset_never_invents_a_row(repo):
    assert repo.set_title_if_unset("chat_does_not_exist", "x") is False
    assert repo.get_session("chat_does_not_exist") is None


# ── archive / restore ─────────────────────────────────────────────────────


def test_archiving_clears_the_pin(repo):
    """Pinned and archived are contradictory states, so the write that creates
    one clears the other."""
    s = repo.create_session(user_email="u@x.com", surface=Surface.WEB)
    repo.set_pinned(s.id, True)
    assert repo.get_session(s.id).pinned_at is not None

    repo.archive_session(s.id)
    got = repo.get_session(s.id)
    assert got.archived is True
    assert got.pinned_at is None


def test_restoring_does_not_put_the_pin_back(repo):
    """The pin is gone, not parked. Re-pinning a restored conversation is one
    click for whoever wants it; guessing on their behalf would resurrect a
    position they may have long stopped wanting."""
    s = repo.create_session(user_email="u@x.com", surface=Surface.WEB)
    repo.set_pinned(s.id, True)
    repo.archive_session(s.id)
    repo.restore_session(s.id)
    got = repo.get_session(s.id)
    assert got.archived is False
    assert got.pinned_at is None


def test_archiving_an_unpinned_session_is_unremarkable(repo):
    """The clear is unconditional, so it must be a no-op on a row that never
    had a pin rather than an error or a stray write."""
    s = repo.create_session(user_email="u@x.com", surface=Surface.WEB)
    repo.archive_session(s.id)
    got = repo.get_session(s.id)
    assert got.archived is True and got.pinned_at is None


# ── list_completed_between ────────────────────────────────────────────────


def _seed_message(repo, session_id: str, ts: datetime, content: str = "hi") -> None:
    """Insert one ``chat_messages`` row with an explicit ``created_at`` --
    what makes ``last_message_at`` deterministic for the keyset assertions
    below, on whichever backend ``repo`` is bound to. Postgres maintains
    ``chat_sessions.last_message_at`` on append (module docstring), so it is
    stamped here too; DuckDB has no such column and derives it from
    ``chat_messages`` at read time.
    """
    msg_id = f"msg_{uuid.uuid4().hex[:8]}"
    if hasattr(repo, "_engine"):
        with repo._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO chat_messages (id, session_id, role, content, created_at) "
                    "VALUES (:id, :sid, 'user', :content, :ts)"
                ),
                {"id": msg_id, "sid": session_id, "content": content, "ts": ts},
            )
            conn.execute(
                sa.text("UPDATE chat_sessions SET last_message_at = :ts WHERE id = :id"),
                {"ts": ts, "id": session_id},
            )
    else:
        repo._conn.execute(
            "INSERT INTO chat_messages (id, session_id, role, content, created_at) VALUES (?, ?, 'user', ?, ?)",
            [msg_id, session_id, content, ts],
        )


_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def test_list_completed_between_filters_by_window(repo):
    s_before = repo.create_session(user_email="u@x.com", surface=Surface.WEB)
    s_in = repo.create_session(user_email="u@x.com", surface=Surface.WEB)
    s_after = repo.create_session(user_email="u@x.com", surface=Surface.WEB)
    _seed_message(repo, s_before.id, _BASE - timedelta(days=1))
    _seed_message(repo, s_in.id, _BASE + timedelta(minutes=1))
    _seed_message(repo, s_after.id, _BASE + timedelta(days=1))

    rows = repo.list_completed_between(_BASE, _BASE + timedelta(hours=1), limit=50)

    assert [r["id"] for r in rows] == [s_in.id]


def test_list_completed_between_filters_by_surfaces(repo):
    web = repo.create_session(user_email="u@x.com", surface=Surface.WEB)
    slack = repo.create_session(user_email="u@x.com", surface=Surface.SLACK_DM)
    _seed_message(repo, web.id, _BASE + timedelta(minutes=1))
    _seed_message(repo, slack.id, _BASE + timedelta(minutes=2))

    rows = repo.list_completed_between(_BASE, _BASE + timedelta(hours=1), surfaces=("web",), limit=50)

    assert [r["id"] for r in rows] == [web.id]


def test_list_completed_between_filters_by_agent_id(repo):
    a1 = repo.create_session(user_email="u@x.com", surface=Surface.WEB, agent_id="agent-1")
    a2 = repo.create_session(user_email="u@x.com", surface=Surface.WEB, agent_id="agent-2")
    _seed_message(repo, a1.id, _BASE + timedelta(minutes=1))
    _seed_message(repo, a2.id, _BASE + timedelta(minutes=2))

    rows = repo.list_completed_between(_BASE, _BASE + timedelta(hours=1), agent_id="agent-1", limit=50)

    assert [r["id"] for r in rows] == [a1.id]


def test_list_completed_between_keyset_pages_three_pages_of_two(repo):
    sessions = [repo.create_session(user_email="u@x.com", surface=Surface.WEB) for _ in range(6)]
    for i, s in enumerate(sessions, start=1):
        _seed_message(repo, s.id, _BASE + timedelta(minutes=i))

    seen: list[str] = []
    after = None
    pages = 0
    while True:
        page = repo.list_completed_between(_BASE, _BASE + timedelta(hours=1), limit=2, after=after)
        if not page:
            break
        pages += 1
        seen.extend(r["id"] for r in page)
        last = page[-1]
        after = (last["last_message_at"], last["id"])
        if len(page) < 2:
            break

    assert pages == 3
    assert seen == [s.id for s in sessions]  # ascending order, no dupes/gaps


def test_list_completed_between_after_is_exclusive(repo):
    s1 = repo.create_session(user_email="u@x.com", surface=Surface.WEB)
    s2 = repo.create_session(user_email="u@x.com", surface=Surface.WEB)
    _seed_message(repo, s1.id, _BASE + timedelta(minutes=1))
    _seed_message(repo, s2.id, _BASE + timedelta(minutes=2))

    first_page = repo.list_completed_between(_BASE, _BASE + timedelta(hours=1), limit=50)
    assert [r["id"] for r in first_page] == [s1.id, s2.id]

    cursor = (first_page[0]["last_message_at"], first_page[0]["id"])
    rest = repo.list_completed_between(_BASE, _BASE + timedelta(hours=1), limit=50, after=cursor)
    assert [r["id"] for r in rest] == [s2.id]  # s1 itself never reappears
