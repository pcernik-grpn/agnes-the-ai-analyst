"""Cross-engine contract for the chat-session title writes.

Parametrises over [DuckDB ``ChatRepository``, Postgres
``ChatSessionPgRepository``] — a frozen pre-A3 pair (see CLAUDE.md, "Dual-
backend discipline"). The same calls go to both; the same answers must come
back. Covers ``set_title`` (the rename endpoint) and ``set_title_if_unset``
(the auto-title task's conditional write, TCRD-290): fill only while empty,
report whether this call wrote, treat an empty string as unset, and never
invent a row.

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
