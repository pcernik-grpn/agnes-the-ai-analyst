"""Memory provenance — ``remember`` fills ``source_turn_id`` /
``source_message_id`` from the session's live chat turn, and the audit row
carries ``turn_id`` (design 2026-09-08 §3.5).

Reuses the fixture pattern from ``tests/test_agent_memory_write_api.py``.
Runs on the DuckDB app-state backend (the default in this test process): the
provenance columns are Postgres-only (A3), so the assertions here are on
what ``repo.create`` was CALLED with (a spy) and on the audit row, not on
what a DuckDB ``get()`` returns — the DuckDB-vs-Postgres storage split is
covered by ``tests/db_pg/test_agent_memories_contract.py``.
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app.auth.jwt import create_access_token
from app.coordination.factory import reset_coordination_for_tests


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _audit_params(row: dict) -> dict:
    v = row.get("params")
    return json.loads(v) if isinstance(v, str) else (v or {})


@pytest.fixture(autouse=True)
def _reset_coordination():
    reset_coordination_for_tests()
    yield
    reset_coordination_for_tests()


@pytest.fixture
def env(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")

    from app.chat.types import Surface
    from src.db import SYSTEM_EVERYONE_GROUP, get_system_db
    from src.repositories import (
        agents_repo,
        chat_session_repo,
        resource_grants_repo,
        user_group_members_repo,
        user_groups_repo,
    )
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="owner1", email="owner@test.com", name="Owner")
    conn.close()

    everyone = user_groups_repo().get_by_name(SYSTEM_EVERYONE_GROUP)
    user_group_members_repo().add_member("owner1", everyone["id"], source="system_seed")
    resource_grants_repo().create(everyone["id"], "chat", "chat")

    agent_id = str(uuid.uuid4())
    agents_repo().create(
        id=agent_id,
        owner_user_id="owner1",
        name="auto-agent",
        slug="auto-agent",
        memory_write_mode="auto",
    )
    session = chat_session_repo().create_session(user_email="owner@test.com", surface=Surface.API, agent_id=agent_id)

    client = TestClient(shared_app)
    return {
        "client": client,
        "owner_token": create_access_token("owner1", "owner@test.com"),
        "agent_id": agent_id,
        "session_id": session.id,
    }


def _spy_on_create(monkeypatch):
    """Wrap the live ``agent_memories_repo()`` singleton's ``create`` so the
    test can see exactly what kwargs the endpoint passed, while still
    exercising the real write path."""
    from src.repositories import agent_memories_repo

    repo = agent_memories_repo()
    real_create = repo.create
    captured: dict = {}

    def _create(*args, **kwargs):
        captured.update(kwargs)
        return real_create(*args, **kwargs)

    monkeypatch.setattr(repo, "create", _create)
    monkeypatch.setattr("app.api.agent_memory.agent_memories_repo", lambda: repo)
    return captured


def test_remember_fills_provenance_from_the_live_turn(env, monkeypatch):
    from app.chat.turn_context import TurnRecord, publish_turn
    from src.repositories import audit_repo

    publish_turn(
        env["session_id"],
        TurnRecord(
            turn_id="t1",
            trace_id=None,
            span_id=None,
            started_at="2026-09-08T00:00:00+00:00",
            user_id="owner1",
            agent_id=env["agent_id"],
            surface="api",
            workload="agent_api",
            message_id="msg_1",
        ),
    )

    captured = _spy_on_create(monkeypatch)

    r = env["client"].post(
        f"/api/v1/sessions/{env['session_id']}/memories",
        json={"content": "remember this"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 201, r.text
    memory_id = r.json()["id"]

    assert captured.get("source_turn_id") == "t1"
    assert captured.get("source_message_id") == "msg_1"

    rows, _ = audit_repo().query(action="agent.memory.write", limit=50)
    matches = [row for row in rows if _audit_params(row).get("memory_id") == memory_id]
    assert len(matches) == 1
    assert _audit_params(matches[0])["turn_id"] == "t1"


def test_remember_without_a_live_turn_writes_none_provenance(env, monkeypatch):
    from src.repositories import audit_repo

    captured = _spy_on_create(monkeypatch)

    r = env["client"].post(
        f"/api/v1/sessions/{env['session_id']}/memories",
        json={"content": "no turn yet"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 201, r.text
    memory_id = r.json()["id"]

    assert captured.get("source_turn_id") is None
    assert captured.get("source_message_id") is None

    rows, _ = audit_repo().query(action="agent.memory.write", limit=50)
    matches = [row for row in rows if _audit_params(row).get("memory_id") == memory_id]
    assert len(matches) == 1
    assert _audit_params(matches[0])["turn_id"] is None


def test_remember_with_a_closed_turn_writes_none_provenance(env, monkeypatch):
    """Finding B: a write that lands after the turn already ended (``_close_
    turn`` re-published with ``ended_at`` set) must not stamp that turn — the
    session's LAST turn is not necessarily the turn this write belongs to."""
    from app.chat.turn_context import TurnRecord, publish_turn

    publish_turn(
        env["session_id"],
        TurnRecord(
            turn_id="t-closed",
            trace_id=None,
            span_id=None,
            started_at="2026-09-08T00:00:00+00:00",
            user_id="owner1",
            agent_id=env["agent_id"],
            surface="api",
            workload="agent_api",
            message_id="msg_closed",
            ended_at="2026-09-08T00:05:00+00:00",
        ),
    )

    captured = _spy_on_create(monkeypatch)

    r = env["client"].post(
        f"/api/v1/sessions/{env['session_id']}/memories",
        json={"content": "an owner note, not mid-turn"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 201, r.text

    assert captured.get("source_turn_id") is None
    assert captured.get("source_message_id") is None


def test_remember_with_a_legacy_record_missing_ended_at_writes_none_provenance(env, monkeypatch):
    """A record published by a replica running before ``ended_at`` existed
    has no such key at all; that reads as "unknown", never as "open", and
    must not raise."""
    from app.chat.turn_context import turn_key
    from app.coordination.factory import coordination

    legacy = json.dumps(
        {
            "turn_id": "t-legacy",
            "trace_id": None,
            "span_id": None,
            "started_at": "2026-09-08T00:00:00+00:00",
            "user_id": "owner1",
            "agent_id": env["agent_id"],
            "surface": "api",
            "workload": "agent_api",
            "message_id": "msg_legacy",
            # no "ended_at" key at all — a pre-fix record
        }
    )
    coordination().kv_set(turn_key(env["session_id"]), legacy, ttl_s=3600)

    captured = _spy_on_create(monkeypatch)

    r = env["client"].post(
        f"/api/v1/sessions/{env['session_id']}/memories",
        json={"content": "a legacy record must not crash this"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 201, r.text

    assert captured.get("source_turn_id") is None
    assert captured.get("source_message_id") is None


def test_remember_with_a_turn_started_after_the_write_writes_none_provenance(env, monkeypatch):
    """Finding A's rule reused for finding B: an OPEN turn record whose own
    ``started_at`` is after this write began cannot be the turn responsible
    for it — a race with a session that just started a brand-new turn."""
    from app.chat.turn_context import TurnRecord, publish_turn

    publish_turn(
        env["session_id"],
        TurnRecord(
            turn_id="t-future",
            trace_id=None,
            span_id=None,
            started_at="2099-01-01T00:00:00+00:00",
            user_id="owner1",
            agent_id=env["agent_id"],
            surface="api",
            workload="agent_api",
            message_id="msg_future",
        ),
    )

    captured = _spy_on_create(monkeypatch)

    r = env["client"].post(
        f"/api/v1/sessions/{env['session_id']}/memories",
        json={"content": "should not borrow the future turn"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 201, r.text

    assert captured.get("source_turn_id") is None
    assert captured.get("source_message_id") is None
