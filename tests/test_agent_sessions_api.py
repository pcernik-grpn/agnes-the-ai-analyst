"""`POST /api/v1/agents/{slug}/sessions`, `POST /api/v1/sessions/{id}/messages`
(SSE), `GET/POST(cancel)/DELETE /api/v1/sessions/{id}` (V1b Task 4).

The chat manager is faked at the attach/stream seam
(`app.api.agent_sessions.get_current_chat_manager`) — these are API-contract
+ auth-chain tests, not chat-sandbox integration tests. `FakeManager.attach`
feeds the seated `StreamingSink` a canned `ready -> token -> assistant_message
-> done` frame sequence, mirroring what `ChatManager._seat_sink` +
`_pump_subprocess_to_ws` do for a real session. Session rows themselves are
real (`chat_session_repo()`/`chat_message_repo()`) so `require_session_principal`
exercises its actual DB-backed ownership check, not a mock.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient

from app.auth.jwt import create_access_token


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class FakeManager:
    """Fakes the attach/stream/cancel/kill seam; delegates session creation
    to the REAL chat_session_repo() so require_session_principal's DB-backed
    ownership check has a real row to look up."""

    def __init__(self, *, stall_after_ready: bool = False):
        self.attached: list[str] = []
        self.detached: list[str] = []
        self.detached_sinks: list[object] = []
        self.sent_messages: list[tuple[str, str, str | None]] = []
        self.cancelled: list[str] = []
        self.killed: list[tuple[str, str]] = []
        self.attach_raises: Exception | None = None
        self.send_raises: Exception | None = None
        # Simulates a runner that never emits anything past "ready" (wedged
        # / crashed silently) — exercises the SSE generator's idle timeout.
        self.stall_after_ready = stall_after_ready

    async def create_session(self, *, user_email, surface, agent_id=None, **kwargs):
        from src.repositories import chat_session_repo

        return chat_session_repo().create_session(
            user_email=user_email,
            surface=surface,
            agent_id=agent_id,
        )

    async def attach(self, chat_id, sink, is_primary: bool = True) -> None:
        if self.attach_raises is not None:
            raise self.attach_raises
        self.attached.append(chat_id)
        if self.stall_after_ready:
            await sink.send_json({"type": "ready", "id": f"{chat_id}:1", "seq": 1})
            return
        frames = [
            {"type": "ready"},
            {"type": "token", "content": "Hel"},
            {"type": "assistant_message", "content": "Hello!"},
            {"type": "done"},
        ]
        for i, frame in enumerate(frames, start=1):
            await sink.send_json({**frame, "id": f"{chat_id}:{i}", "seq": i})

    async def send_user_message(self, chat_id, text, *, sender_email=None, **kwargs):
        if self.send_raises is not None:
            raise self.send_raises
        self.sent_messages.append((chat_id, text, sender_email))

    async def detach_sink(self, chat_id, sink) -> None:
        self.detached.append(chat_id)
        self.detached_sinks.append(sink)

    async def cancel(self, chat_id) -> None:
        self.cancelled.append(chat_id)

    async def kill(self, chat_id, *, reason: str) -> None:
        self.killed.append((chat_id, reason))


@pytest.fixture
def env(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")

    from src.db import SYSTEM_EVERYONE_GROUP, get_system_db
    from src.repositories import agents_repo, resource_grants_repo, user_group_members_repo, user_groups_repo
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="owner1", email="owner@test.com", name="Owner")
    UserRepository(conn).create(id="other1", email="other@test.com", name="Other")
    conn.close()

    everyone = user_groups_repo().get_by_name(SYSTEM_EVERYONE_GROUP)
    user_group_members_repo().add_member("owner1", everyone["id"], source="system_seed")
    user_group_members_repo().add_member("other1", everyone["id"], source="system_seed")
    resource_grants_repo().create(everyone["id"], "chat", "chat")

    agent_id = str(uuid.uuid4())
    agents_repo().create(id=agent_id, owner_user_id="owner1", name="Support Bot", slug="support-bot")
    other_agent_id = str(uuid.uuid4())
    agents_repo().create(id=other_agent_id, owner_user_id="owner1", name="Other Agent", slug="other-agent")

    client = TestClient(shared_app)
    return {
        "client": client,
        "owner_token": create_access_token("owner1", "owner@test.com"),
        "other_token": create_access_token("other1", "other@test.com"),
        "agent_id": agent_id,
        "other_agent_id": other_agent_id,
    }


def _mint_agent_pat(owner_email: str, owner_id: str, agent_id: str, token_id: str) -> str:
    return create_access_token(
        user_id=owner_id,
        email=owner_email,
        token_id=token_id,
        typ="agent_pat",
        extra_claims={"agent_id": agent_id},
    )


def _register_agent_pat_row(owner_id: str, agent_id: str, token: str, token_id: str) -> None:
    from src.repositories import access_token_repo

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    access_token_repo().create(
        id=token_id,
        user_id=owner_id,
        name="agent-pat",
        token_hash=token_hash,
        prefix=token_id.replace("-", "")[:8],
        agent_id=agent_id,
    )


def _patch_manager(monkeypatch, manager: FakeManager) -> None:
    import app.api.agent_sessions as agent_sessions

    monkeypatch.setattr(agent_sessions, "get_current_chat_manager", lambda: manager)


def _create_session(env, monkeypatch, slug="support-bot", token=None) -> str:
    manager = FakeManager()
    _patch_manager(monkeypatch, manager)
    r = env["client"].post(
        f"/api/v1/agents/{slug}/sessions",
        json={},
        headers=_auth(token or env["owner_token"]),
    )
    assert r.status_code == 201, r.text
    return r.json()["session_id"]


# ---------------------------------------------------------------------------
# POST /api/v1/agents/{slug}/sessions
# ---------------------------------------------------------------------------


def test_create_session_returns_201_with_session_id(env, monkeypatch):
    manager = FakeManager()
    _patch_manager(monkeypatch, manager)
    r = env["client"].post(
        "/api/v1/agents/support-bot/sessions",
        json={},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 201
    assert r.json()["session_id"]


def test_create_session_chat_disabled_returns_503(env, monkeypatch):
    """L2: `ChatManager.create_session` raises `RuntimeError("chat.enabled
    is false")` when the config-level chat.enabled flag is off — this must
    map to `503 chat_disabled` (matching the `manager is None` branch just
    above it and app/api/chat.py's `_get_manager`), not fall through to the
    catch-all 500 handler."""

    class DisabledManager(FakeManager):
        async def create_session(self, *, user_email, surface, agent_id=None, **kwargs):
            raise RuntimeError("chat.enabled is false")

    manager = DisabledManager()
    _patch_manager(monkeypatch, manager)
    r = env["client"].post(
        "/api/v1/agents/support-bot/sessions",
        json={},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "chat_disabled"


def test_create_session_unknown_slug_returns_404(env, monkeypatch):
    manager = FakeManager()
    _patch_manager(monkeypatch, manager)
    r = env["client"].post(
        "/api/v1/agents/does-not-exist/sessions",
        json={},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/sessions/{id}/messages — SSE
# ---------------------------------------------------------------------------


def test_post_message_streams_agui_events(env, monkeypatch):
    session_id = _create_session(env, monkeypatch)
    manager = FakeManager()
    _patch_manager(monkeypatch, manager)

    r = env["client"].post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"input": "hello there"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["x-request-id"]

    body = r.text
    assert "event: RUN_STARTED" in body
    assert "event: TEXT_MESSAGE_CONTENT" in body
    assert "event: RUN_FINISHED" in body
    assert f"id: {session_id}:1" in body
    assert f"id: {session_id}:4" in body

    assert manager.attached == [session_id]
    assert manager.detached == [session_id]
    assert manager.sent_messages == [(session_id, "hello there", "owner@test.com")]


def test_post_message_run_started_appears_exactly_once(env, monkeypatch):
    session_id = _create_session(env, monkeypatch)
    manager = FakeManager()
    _patch_manager(monkeypatch, manager)

    r = env["client"].post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"input": "hi"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 200
    assert r.text.count("event: RUN_STARTED") == 1


def test_post_message_idle_timeout_emits_run_error_and_releases_lock(env, monkeypatch):
    """C9: a turn that never reaches a terminal frame (runner wedged) must
    not hang the response forever — the generator force-terminates with a
    RUN_ERROR after `_IDLE_TIMEOUT_S`, and the turn lock is released so a
    follow-up call isn't wedged too."""
    import app.api.agent_sessions as agent_sessions

    monkeypatch.setattr(agent_sessions, "_IDLE_TIMEOUT_S", 0.05)

    session_id = _create_session(env, monkeypatch)
    manager = FakeManager(stall_after_ready=True)
    _patch_manager(monkeypatch, manager)

    r = env["client"].post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"input": "hi"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 200
    body = r.text
    assert "event: RUN_STARTED" in body
    assert "event: RUN_ERROR" in body
    assert '"code": "idle_timeout"' in body
    assert manager.detached == [session_id]

    # Lock released -> a follow-up call on the same session succeeds rather
    # than 409ing.
    manager2 = FakeManager()
    _patch_manager(monkeypatch, manager2)
    r2 = env["client"].post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"input": "hi again"},
        headers=_auth(env["owner_token"]),
    )
    assert r2.status_code == 200


def test_post_message_send_failure_after_attach_detaches_sink_and_releases_lock(env, monkeypatch):
    """M1: if `attach()` seats the sink but `send_user_message` then raises,
    the sink must still be detached — otherwise it lingers in `live.sinks`
    forever (undrained queue, skewed linger/pause lifecycle) — and the turn
    lock must still be released so a follow-up call isn't wedged too."""
    from fastapi.testclient import TestClient

    from app.chat.streaming_sink import StreamingSink

    session_id = _create_session(env, monkeypatch)
    manager = FakeManager()
    manager.send_raises = RuntimeError("boom")
    _patch_manager(monkeypatch, manager)

    # The default TestClient re-raises unhandled server exceptions in-test
    # (see tests/test_request_id_middleware.py for the same pattern) — use a
    # non-raising client wrapping the SAME app so we can assert on the
    # response status instead of catching the RuntimeError ourselves.
    non_raising_client = TestClient(env["client"].app, raise_server_exceptions=False)
    r = non_raising_client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"input": "hi"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 500

    assert manager.attached == [session_id]
    assert manager.detached == [session_id]
    assert len(manager.detached_sinks) == 1
    assert isinstance(manager.detached_sinks[0], StreamingSink)

    from app.coordination.factory import coordination

    lock_key = f"agent-session-turn:{session_id}"
    # Lock released -> a follow-up call on the same session succeeds rather
    # than 409ing.
    manager2 = FakeManager()
    _patch_manager(monkeypatch, manager2)
    r2 = env["client"].post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"input": "hi again"},
        headers=_auth(env["owner_token"]),
    )
    assert r2.status_code == 200
    # Sanity: the lock really was free before the second call (not just
    # incidentally re-acquirable) — acquiring/releasing it directly confirms
    # no leftover holder from the first request.
    assert coordination().lease_acquire(lock_key, "probe", ttl_s=1)
    coordination().lease_release(lock_key, "probe")


def test_post_message_attach_session_not_found_does_not_call_detach(env, monkeypatch):
    """Counterpart to the regression above: when `attach()` itself raises
    `SessionNotFound` (before seating a sink), there is nothing to detach —
    `detach_sink` must not be called."""
    from app.chat.manager import SessionNotFound

    session_id = _create_session(env, monkeypatch)
    manager = FakeManager()
    manager.attach_raises = SessionNotFound(session_id)
    _patch_manager(monkeypatch, manager)

    r = env["client"].post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"input": "hi"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 404
    assert manager.detached == []
    assert manager.detached_sinks == []


def test_post_message_accepts_response_format_without_error(env, monkeypatch):
    session_id = _create_session(env, monkeypatch)
    manager = FakeManager()
    _patch_manager(monkeypatch, manager)

    r = env["client"].post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"input": "hi", "response_format": {"type": "json_schema", "schema": {"type": "object"}}},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 200


def test_post_message_empty_input_returns_422(env, monkeypatch):
    session_id = _create_session(env, monkeypatch)
    manager = FakeManager()
    _patch_manager(monkeypatch, manager)

    r = env["client"].post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"input": "   "},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 422


def test_post_message_cross_owner_returns_404(env, monkeypatch):
    session_id = _create_session(env, monkeypatch)
    manager = FakeManager()
    _patch_manager(monkeypatch, manager)

    r = env["client"].post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"input": "hi"},
        headers=_auth(env["other_token"]),
    )
    assert r.status_code == 404


def test_post_message_revoked_chat_grant_returns_403(env, monkeypatch):
    """L1: `require_session_principal` re-checks the `ResourceType.CHAT`
    grant AFTER the owner match, matching `require_agent_runtime_principal`
    (`/responses` + create-session). A session outlives the grant that let
    its owner create it — a caller whose CHAT grant is later revoked must
    not keep driving `/messages` on an existing session. `403
    chat_access_denied` mirrors `/responses`'s `test_missing_chat_grant_returns_403`.
    """
    session_id = _create_session(env, monkeypatch)
    manager = FakeManager()
    _patch_manager(monkeypatch, manager)

    from src.repositories import resource_grants_repo

    resource_grants_repo().delete_by_resource("chat", "chat")

    r = env["client"].post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"input": "hi"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "chat_access_denied"


def test_get_session_revoked_chat_grant_returns_403(env, monkeypatch):
    session_id = _create_session(env, monkeypatch)

    from src.repositories import resource_grants_repo

    resource_grants_repo().delete_by_resource("chat", "chat")

    r = env["client"].get(f"/api/v1/sessions/{session_id}", headers=_auth(env["owner_token"]))
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "chat_access_denied"


def test_post_message_wrong_agent_pat_returns_404(env, monkeypatch):
    session_id = _create_session(env, monkeypatch, slug="support-bot")
    manager = FakeManager()
    _patch_manager(monkeypatch, manager)

    token_id = "tok-wrong-agent-session"
    token = _mint_agent_pat("owner@test.com", "owner1", env["other_agent_id"], token_id)
    _register_agent_pat_row("owner1", env["other_agent_id"], token, token_id)

    r = env["client"].post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"input": "hi"},
        headers=_auth(token),
    )
    assert r.status_code == 404


def test_post_message_matching_agent_pat_succeeds(env, monkeypatch):
    session_id = _create_session(env, monkeypatch, slug="support-bot")
    manager = FakeManager()
    _patch_manager(monkeypatch, manager)

    token_id = "tok-right-agent-session"
    token = _mint_agent_pat("owner@test.com", "owner1", env["agent_id"], token_id)
    _register_agent_pat_row("owner1", env["agent_id"], token, token_id)

    r = env["client"].post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"input": "hi"},
        headers=_auth(token),
    )
    assert r.status_code == 200


def test_post_message_unknown_session_returns_404(env, monkeypatch):
    manager = FakeManager()
    _patch_manager(monkeypatch, manager)
    r = env["client"].post(
        "/api/v1/sessions/does-not-exist/messages",
        json={"input": "hi"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 404


def test_second_concurrent_message_returns_409_turn_in_flight(env, monkeypatch):
    session_id = _create_session(env, monkeypatch)
    manager = FakeManager()
    _patch_manager(monkeypatch, manager)

    from app.coordination.factory import coordination

    lock_key = f"agent-session-turn:{session_id}"
    acquired = coordination().lease_acquire(lock_key, "some-other-request", ttl_s=300)
    assert acquired

    r = env["client"].post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"input": "hi"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "turn_in_flight"

    coordination().lease_release(lock_key, "some-other-request")
    r2 = env["client"].post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"input": "hi"},
        headers=_auth(env["owner_token"]),
    )
    assert r2.status_code == 200


# ---------------------------------------------------------------------------
# GET /api/v1/sessions/{id}
# ---------------------------------------------------------------------------


def test_get_session_returns_state_and_history(env, monkeypatch):
    session_id = _create_session(env, monkeypatch)

    from src.repositories import chat_message_repo

    chat_message_repo().append_message(session_id=session_id, role="user", content="hi there")
    chat_message_repo().append_message(session_id=session_id, role="assistant", content="hello!")

    r = env["client"].get(f"/api/v1/sessions/{session_id}", headers=_auth(env["owner_token"]))
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == session_id
    assert body["agent_id"] == env["agent_id"]
    assert body["state"]
    assert len(body["messages"]) == 2
    assert body["messages"][0]["role"] == "user"
    assert body["messages"][0]["content"] == "hi there"
    assert body["messages"][1]["role"] == "assistant"


def test_get_session_cross_owner_returns_404(env, monkeypatch):
    session_id = _create_session(env, monkeypatch)
    r = env["client"].get(f"/api/v1/sessions/{session_id}", headers=_auth(env["other_token"]))
    assert r.status_code == 404


def test_get_session_unknown_returns_404(env):
    r = env["client"].get("/api/v1/sessions/does-not-exist", headers=_auth(env["owner_token"]))
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/sessions/{id}/cancel
# ---------------------------------------------------------------------------


def test_cancel_session_returns_202_and_calls_manager_cancel(env, monkeypatch):
    session_id = _create_session(env, monkeypatch)
    manager = FakeManager()
    _patch_manager(monkeypatch, manager)

    r = env["client"].post(f"/api/v1/sessions/{session_id}/cancel", headers=_auth(env["owner_token"]))
    assert r.status_code == 202
    assert manager.cancelled == [session_id]


def test_cancel_session_cross_owner_returns_404(env, monkeypatch):
    session_id = _create_session(env, monkeypatch)
    manager = FakeManager()
    _patch_manager(monkeypatch, manager)

    r = env["client"].post(f"/api/v1/sessions/{session_id}/cancel", headers=_auth(env["other_token"]))
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /api/v1/sessions/{id}
# ---------------------------------------------------------------------------


def test_delete_session_returns_204_and_archives(env, monkeypatch):
    session_id = _create_session(env, monkeypatch)
    manager = FakeManager()
    _patch_manager(monkeypatch, manager)

    r = env["client"].delete(f"/api/v1/sessions/{session_id}", headers=_auth(env["owner_token"]))
    assert r.status_code == 204
    assert manager.killed == [(session_id, "agent_api_delete")]

    from src.repositories import chat_session_repo

    s = chat_session_repo().get_session(session_id)
    assert s.archived is True


def test_delete_session_cross_owner_returns_404(env, monkeypatch):
    session_id = _create_session(env, monkeypatch)
    manager = FakeManager()
    _patch_manager(monkeypatch, manager)

    r = env["client"].delete(f"/api/v1/sessions/{session_id}", headers=_auth(env["other_token"]))
    assert r.status_code == 404
