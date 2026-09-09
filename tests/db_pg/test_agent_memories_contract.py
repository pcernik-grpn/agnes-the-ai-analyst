"""Cross-engine contract tests for the agent_memories repository."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.agent_memories import AgentMemoriesRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return AgentMemoriesRepository(conn), conn


def _make_pg_repo(pg_engine, monkeypatch):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from src.repositories.agent_memories_pg import AgentMemoriesPgRepository

    return AgentMemoriesPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    if request.param == "duckdb":
        r, conn = _make_duckdb_repo(tmp_path)
        yield r
        conn.close()
    else:
        r, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield r


def test_create_pending_then_approve(repo):
    repo.create(id="m1", agent_id="a1", owner_user_id="u1", content="report in CZK", source_session_id="c1")
    assert repo.list_active("a1") == []  # pending not active
    assert len(repo.list_for_agent("a1", status="pending")) == 1
    repo.approve("m1")
    active = repo.list_active("a1")
    assert len(active) == 1 and active[0]["activated_at"] is not None


def test_auto_write_is_active_immediately(repo):
    repo.create(id="m1", agent_id="a1", owner_user_id="u1", content="x", source_session_id="c1", status="active")
    assert len(repo.list_active("a1")) == 1


def test_archive_removes_from_active(repo):
    repo.create(id="m1", agent_id="a1", owner_user_id="u1", content="x", source_session_id="c1", status="active")
    repo.archive("m1")
    assert repo.list_active("a1") == []
    row = repo.get("m1")
    assert row["status"] == "archived"
    assert row["archived_at"] is not None


def test_count_recent(repo):
    repo.create(id="m1", agent_id="a1", owner_user_id="u1", content="x", source_session_id="c1")
    since = datetime.now(timezone.utc) - timedelta(minutes=1)
    assert repo.count_recent("a1", since) == 1


def test_count_recent_excludes_older_rows(repo):
    repo.create(id="m1", agent_id="a1", owner_user_id="u1", content="x", source_session_id="c1")
    since = datetime.now(timezone.utc) + timedelta(minutes=1)
    assert repo.count_recent("a1", since) == 0


def test_approve_is_noop_if_not_pending(repo):
    repo.create(id="m1", agent_id="a1", owner_user_id="u1", content="x", source_session_id="c1", status="active")
    row_before = repo.get("m1")
    repo.approve("m1")
    row_after = repo.get("m1")
    assert row_after["status"] == "active"
    # activated_at was never set by approve() since it was already active
    assert row_after["activated_at"] == row_before["activated_at"]


def test_get_missing_returns_none(repo):
    assert repo.get("missing") is None


def test_delete(repo):
    repo.create(id="m1", agent_id="a1", owner_user_id="u1", content="x", source_session_id="c1")
    repo.delete("m1")
    assert repo.get("m1") is None
    assert repo.list_for_agent("a1") == []


def test_list_for_agent_status_filter_and_ordering(repo):
    repo.create(id="m1", agent_id="a1", owner_user_id="u1", content="first", source_session_id="c1")
    repo.create(id="m2", agent_id="a1", owner_user_id="u1", content="second", source_session_id="c1")
    repo.approve("m2")
    all_rows = repo.list_for_agent("a1")
    assert [r["id"] for r in all_rows] == ["m2", "m1"]  # newest first
    pending_only = repo.list_for_agent("a1", status="pending")
    assert [r["id"] for r in pending_only] == ["m1"]


def test_count_pending(repo):
    repo.create(id="m1", agent_id="a1", owner_user_id="u1", content="x", source_session_id="c1")
    repo.create(id="m2", agent_id="a1", owner_user_id="u1", content="y", source_session_id="c1")
    repo.create(id="m3", agent_id="a1", owner_user_id="u1", content="z", source_session_id="c1", status="active")
    assert repo.count_pending("a1") == 2
    repo.approve("m1")
    assert repo.count_pending("a1") == 1


def test_count_pending_scoped_to_agent(repo):
    repo.create(id="m1", agent_id="a1", owner_user_id="u1", content="x", source_session_id="c1")
    repo.create(id="m2", agent_id="a2", owner_user_id="u1", content="y", source_session_id="c1")
    assert repo.count_pending("a1") == 1
    assert repo.count_pending("a2") == 1
    assert repo.count_pending("no-such-agent") == 0


def test_delete_for_agent(repo):
    repo.create(id="m1", agent_id="a1", owner_user_id="u1", content="x", source_session_id="c1")
    repo.create(id="m2", agent_id="a1", owner_user_id="u1", content="y", source_session_id="c1")
    repo.create(id="m3", agent_id="a2", owner_user_id="u1", content="z", source_session_id="c1")
    repo.delete_for_agent("a1")
    assert repo.list_for_agent("a1") == []
    # a different agent's memories are untouched
    assert len(repo.list_for_agent("a2")) == 1


def test_delete_for_agent_with_no_rows_is_a_noop(repo):
    repo.delete_for_agent("no-such-agent")
    assert repo.list_for_agent("no-such-agent") == []


def test_create_accepts_turn_provenance_on_both_backends(repo):
    """Memory provenance (design 2026-09-08 §3.5, migration 0113): the PG
    side stores ``source_turn_id``/``source_message_id``, the frozen DuckDB
    side accepts and silently drops both — the same accept-and-drop pattern
    as the chat_messages cache columns (migration 0092)."""
    repo.create(
        id="m1",
        agent_id="a1",
        owner_user_id="u1",
        content="x",
        source_session_id="c1",
        source_turn_id="t1",
        source_message_id="msg_1",
    )
    row = repo.get("m1")
    assert row["source_session_id"] == "c1"
    assert row.get("source_turn_id") in ("t1", None) and row.get("source_message_id") in ("msg_1", None)


def test_pg_stores_turn_provenance(repo):
    if not hasattr(repo, "_engine"):
        pytest.skip("PG side only")
    repo.create(
        id="m2",
        agent_id="a1",
        owner_user_id="u1",
        content="x",
        source_session_id="c1",
        source_turn_id="t1",
        source_message_id="msg_1",
    )
    assert repo.get("m2")["source_turn_id"] == "t1"
