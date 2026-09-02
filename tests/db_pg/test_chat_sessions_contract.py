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

Follows the pattern of ``test_jobs_contract.py``.
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
