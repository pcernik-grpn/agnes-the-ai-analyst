"""Audit coverage for the agent-as-API runtime surface — `agent.invoke`
(POST /api/v1/agents/{slug}/responses), `agent.session.create/message/
cancel/delete` (POST/GET/DELETE /api/v1/agents/{slug}/sessions +
/api/v1/sessions/{id}/...), and `agent.webhook.create/delete`
(POST/DELETE /api/v1/agents/{slug}/webhooks[/{id}]) — F2b, audit-full-
coverage plan, Task 4.

Reuses the `env`/monkeypatch conventions from `tests/test_agent_responses_
api.py`, `tests/test_agent_sessions_api.py`, and `tests/test_agent_webhooks_
api.py` — these are audit-contract tests layered on the same seams, not a
duplicate of those files' auth/behavior coverage.
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app.auth.jwt import create_access_token


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _rows(action: str, resource: str | None = None):
    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action=action, limit=50)
    if resource is not None:
        rows = [r for r in rows if r.get("resource") == resource]
    return rows


def _params(row: dict) -> dict:
    """`audit_repo().query()` returns `params` as the raw stored JSON
    string, not a parsed dict — decode it here (same helper as
    `tests/test_agent_memory_write_api.py`)."""
    v = row.get("params")
    return json.loads(v) if isinstance(v, str) else (v or {})


@pytest.fixture
def env(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")

    from src.db import SYSTEM_EVERYONE_GROUP, get_system_db
    from src.repositories import agents_repo, resource_grants_repo, user_group_members_repo, user_groups_repo
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="owner1", email="owner@test.com", name="Owner")
    conn.close()

    everyone = user_groups_repo().get_by_name(SYSTEM_EVERYONE_GROUP)
    user_group_members_repo().add_member("owner1", everyone["id"], source="system_seed")
    resource_grants_repo().create(everyone["id"], "chat", "chat")

    agent_id = str(uuid.uuid4())
    agents_repo().create(id=agent_id, owner_user_id="owner1", name="Support Bot", slug="support-bot")

    client = TestClient(shared_app)
    return {
        "client": client,
        "owner_token": create_access_token("owner1", "owner@test.com"),
        "agent_id": agent_id,
    }


# ---------------------------------------------------------------------------
# agent.invoke — POST /api/v1/agents/{slug}/responses
# ---------------------------------------------------------------------------


def _patch_run_one_shot(monkeypatch, *, chat_id="chat-1", answer="hi there", timed_out=False):
    async def _fake_run_one_shot(manager, *, user_email, agent_id, prompt, timeout_s, **_kwargs):
        return {"chat_id": chat_id, "answer": answer, "timed_out": timed_out}

    import app.api.agent_runtime as agent_runtime

    monkeypatch.setattr(agent_runtime, "run_one_shot", _fake_run_one_shot)
    monkeypatch.setattr(agent_runtime, "get_current_chat_manager", lambda: object())


def test_sync_invoke_writes_agent_invoke_row(env, monkeypatch):
    _patch_run_one_shot(monkeypatch, answer="the answer")
    r = env["client"].post(
        "/api/v1/agents/support-bot/responses",
        json={"input": "what's up"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 200, r.text

    rows = _rows("agent.invoke", resource="agent:support-bot")
    assert rows
    row = rows[0]
    assert row["user_id"] == "owner1"
    assert _params(row)["mode"] == "sync"


def test_background_invoke_writes_job_mode(env, monkeypatch):
    _patch_run_one_shot(monkeypatch)
    r = env["client"].post(
        "/api/v1/agents/support-bot/responses",
        json={"input": "do this later", "background": True},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 202, r.text

    rows = _rows("agent.invoke", resource="agent:support-bot")
    assert any(_params(row)["mode"] == "job" for row in rows)


def test_invoke_error_writes_second_row_with_error_result(env, monkeypatch):
    async def _boom(manager, **kwargs):
        raise RuntimeError("boom")

    import app.api.agent_runtime as agent_runtime

    monkeypatch.setattr(agent_runtime, "run_one_shot", _boom)
    monkeypatch.setattr(agent_runtime, "get_current_chat_manager", lambda: object())

    with pytest.raises(RuntimeError):
        env["client"].post(
            "/api/v1/agents/support-bot/responses",
            json={"input": "boom"},
            headers=_auth(env["owner_token"]),
        )

    rows = _rows("agent.invoke", resource="agent:support-bot")
    # The unconditional pre-run row, plus the error row.
    assert len(rows) >= 2
    assert any((row.get("result") or "").startswith("error:RuntimeError") for row in rows)


# ---------------------------------------------------------------------------
# agent.session.create/message/cancel/delete
# ---------------------------------------------------------------------------


class FakeManager:
    """Minimal fake for the attach/stream/cancel/kill seam — mirrors
    `tests/test_agent_sessions_api.py::FakeManager`, trimmed to what the
    audit-coverage assertions below need."""

    async def create_session(self, *, user_email, surface, agent_id=None, **kwargs):
        from src.repositories import chat_session_repo

        return chat_session_repo().create_session(user_email=user_email, surface=surface, agent_id=agent_id)

    async def attach(self, chat_id, sink, is_primary: bool = True) -> None:
        for i, frame in enumerate(
            [{"type": "ready"}, {"type": "assistant_message", "content": "hi"}, {"type": "done"}], start=1
        ):
            await sink.send_json({**frame, "id": f"{chat_id}:{i}", "seq": i})

    async def send_user_message(self, chat_id, text, *, sender_email=None, **kwargs) -> None:
        pass

    async def detach_sink(self, chat_id, sink) -> None:
        pass

    async def cancel(self, chat_id) -> None:
        pass

    async def kill(self, chat_id, *, reason: str) -> None:
        pass


def _patch_manager(monkeypatch, manager: FakeManager) -> None:
    import app.api.agent_sessions as agent_sessions

    monkeypatch.setattr(agent_sessions, "get_current_chat_manager", lambda: manager)


def test_create_session_writes_agent_session_create_row(env, monkeypatch):
    _patch_manager(monkeypatch, FakeManager())
    r = env["client"].post(
        "/api/v1/agents/support-bot/sessions",
        json={},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 201, r.text
    session_id = r.json()["session_id"]

    rows = _rows("agent.session.create", resource=f"session:{session_id}")
    assert rows
    assert rows[0]["user_id"] == "owner1"
    assert _params(rows[0])["agent_id"] == env["agent_id"]


def test_post_message_writes_agent_session_message_row(env, monkeypatch):
    _patch_manager(monkeypatch, FakeManager())
    create = env["client"].post("/api/v1/agents/support-bot/sessions", json={}, headers=_auth(env["owner_token"]))
    session_id = create.json()["session_id"]

    r = env["client"].post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"input": "hello there"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 200, r.text

    rows = _rows("agent.session.message", resource=f"session:{session_id}")
    assert rows
    assert _params(rows[0])["chars"] == len("hello there")


def test_cancel_session_writes_agent_session_cancel_row(env, monkeypatch):
    _patch_manager(monkeypatch, FakeManager())
    create = env["client"].post("/api/v1/agents/support-bot/sessions", json={}, headers=_auth(env["owner_token"]))
    session_id = create.json()["session_id"]

    r = env["client"].post(
        f"/api/v1/sessions/{session_id}/cancel",
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 202, r.text

    rows = _rows("agent.session.cancel", resource=f"session:{session_id}")
    assert rows


def test_delete_session_writes_agent_session_delete_row(env, monkeypatch):
    _patch_manager(monkeypatch, FakeManager())
    create = env["client"].post("/api/v1/agents/support-bot/sessions", json={}, headers=_auth(env["owner_token"]))
    session_id = create.json()["session_id"]

    r = env["client"].delete(
        f"/api/v1/sessions/{session_id}",
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 204, r.text

    rows = _rows("agent.session.delete", resource=f"session:{session_id}")
    assert rows


# ---------------------------------------------------------------------------
# agent.webhook.create/delete
# ---------------------------------------------------------------------------


def _mock_public_dns(monkeypatch, host: str, ip: str = "93.184.216.34") -> None:
    import socket

    import app.chat.webhook_delivery as webhook_delivery

    def fake_getaddrinfo(h, port, *a, **kw):
        if h != host:
            raise socket.gaierror(f"no mock DNS entry for {h!r}")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    monkeypatch.setattr(webhook_delivery.socket, "getaddrinfo", fake_getaddrinfo)


def test_create_webhook_writes_agent_webhook_create_row(env, monkeypatch):
    _mock_public_dns(monkeypatch, "hooks.example.com")
    r = env["client"].post(
        "/api/v1/agents/support-bot/webhooks",
        json={"url": "https://hooks.example.com/incoming"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 201, r.text
    webhook_id = r.json()["id"]

    rows = _rows("agent.webhook.create", resource=f"agent_webhook:{webhook_id}")
    assert rows
    assert rows[0]["user_id"] == "owner1"
    assert _params(rows[0])["agent_id"] == env["agent_id"]


def test_delete_webhook_writes_agent_webhook_delete_row(env, monkeypatch):
    _mock_public_dns(monkeypatch, "hooks.example.com")
    created = env["client"].post(
        "/api/v1/agents/support-bot/webhooks",
        json={"url": "https://hooks.example.com/incoming"},
        headers=_auth(env["owner_token"]),
    )
    webhook_id = created.json()["id"]

    r = env["client"].delete(
        f"/api/v1/agents/support-bot/webhooks/{webhook_id}",
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 204, r.text

    rows = _rows("agent.webhook.delete", resource=f"agent_webhook:{webhook_id}")
    assert rows
