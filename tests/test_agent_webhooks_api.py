"""`/api/v1/agents/{slug}/webhooks` registration API (V1b Task 6) +
`app.chat.webhook_delivery.enqueue_job_event_webhooks` (the fan-out called
from a job's terminal-state transition — see `app/worker/runtime.py`).

Auth/ownership tests reuse the `env`/`_AuthedClient` pattern from
`tests/test_agents_management_api.py`. `POST` tests that need a URL to
resolve successfully monkeypatch `socket.getaddrinfo` (deterministic, no
real DNS) — the SSRF-denial tests use literal private/loopback addresses
instead, which `validate_and_resolve` rejects before any network lookup.
"""

from __future__ import annotations

import socket
import uuid

import pytest
from fastapi.testclient import TestClient

from app.auth.jwt import create_access_token


class _AuthedClient:
    def __init__(self, client: TestClient, token: str):
        self._client = client
        self._token = token

    def _headers(self, headers):
        merged = {"Authorization": f"Bearer {self._token}"}
        if headers:
            merged.update(headers)
        return merged

    def get(self, url, **kw):
        kw["headers"] = self._headers(kw.get("headers"))
        return self._client.get(url, **kw)

    def post(self, url, **kw):
        kw["headers"] = self._headers(kw.get("headers"))
        return self._client.post(url, **kw)

    def delete(self, url, **kw):
        kw["headers"] = self._headers(kw.get("headers"))
        return self._client.delete(url, **kw)


@pytest.fixture
def env(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")

    from src.db import get_system_db
    from src.repositories import agents_repo
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="owner1", email="owner@test.com", name="Owner")
    UserRepository(conn).create(id="other1", email="other@test.com", name="Other")
    conn.close()

    agent_id = str(uuid.uuid4())
    agents_repo().create(id=agent_id, owner_user_id="owner1", name="Support Bot", slug="support-bot")
    other_agent_id = str(uuid.uuid4())
    agents_repo().create(id=other_agent_id, owner_user_id="other1", name="Other's Bot", slug="others-bot")

    client = TestClient(shared_app)
    return {
        "client": client,
        "owner_token": create_access_token("owner1", "owner@test.com"),
        "other_token": create_access_token("other1", "other@test.com"),
        "agent_id": agent_id,
        "other_agent_id": other_agent_id,
    }


@pytest.fixture
def owner_client(env):
    return _AuthedClient(env["client"], env["owner_token"])


@pytest.fixture
def other_client(env):
    return _AuthedClient(env["client"], env["other_token"])


def _mock_public_dns(monkeypatch, host: str, ip: str = "93.184.216.34") -> None:
    import app.chat.webhook_delivery as webhook_delivery

    def fake_getaddrinfo(h, port, *a, **kw):
        if h != host:
            raise socket.gaierror(f"no mock DNS entry for {h!r}")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    monkeypatch.setattr(webhook_delivery.socket, "getaddrinfo", fake_getaddrinfo)


# ---------------------------------------------------------------------------
# POST — create
# ---------------------------------------------------------------------------


def test_create_webhook_returns_secret_once(owner_client, monkeypatch):
    _mock_public_dns(monkeypatch, "hooks.example.com")

    resp = owner_client.post("/api/v1/agents/support-bot/webhooks", json={"url": "https://hooks.example.com/incoming"})

    assert resp.status_code == 201
    body = resp.json()
    assert "secret" in body and len(body["secret"]) == 64  # secrets.token_hex(32)
    assert body["url"] == "https://hooks.example.com/incoming"
    assert body["events"] == ["job.completed", "job.failed"]
    assert body["active"] is True


def test_create_webhook_accepts_explicit_events(owner_client, monkeypatch):
    _mock_public_dns(monkeypatch, "hooks.example.com")

    resp = owner_client.post(
        "/api/v1/agents/support-bot/webhooks",
        json={"url": "https://hooks.example.com/incoming", "events": ["job.completed"]},
    )

    assert resp.status_code == 201
    assert resp.json()["events"] == ["job.completed"]


def test_create_webhook_rejects_invalid_event(owner_client, monkeypatch):
    _mock_public_dns(monkeypatch, "hooks.example.com")

    resp = owner_client.post(
        "/api/v1/agents/support-bot/webhooks",
        json={"url": "https://hooks.example.com/incoming", "events": ["job.bogus"]},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "invalid_event"


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.example.com/incoming",  # plain http
        "https://169.254.169.254/latest/meta-data/",  # cloud metadata
        "https://127.0.0.1/incoming",  # loopback
        "https://10.0.0.5/incoming",  # RFC1918 private
    ],
)
def test_create_webhook_ssrf_url_returns_400(owner_client, url):
    resp = owner_client.post("/api/v1/agents/support-bot/webhooks", json={"url": url})

    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "webhook_url_forbidden"


def test_create_webhook_unknown_agent_returns_404(owner_client, monkeypatch):
    _mock_public_dns(monkeypatch, "hooks.example.com")

    resp = owner_client.post("/api/v1/agents/nonexistent/webhooks", json={"url": "https://hooks.example.com/x"})

    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "agent_not_found"


def test_create_webhook_cross_owner_slug_returns_404(other_client, monkeypatch):
    """`other1` has no agent named `support-bot` (owner1's) — existence of
    owner1's agent must not leak."""
    _mock_public_dns(monkeypatch, "hooks.example.com")

    resp = other_client.post("/api/v1/agents/support-bot/webhooks", json={"url": "https://hooks.example.com/x"})

    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "agent_not_found"


def test_create_webhook_rejects_agent_pat(env, monkeypatch):
    """Registration is standing-config — `require_session_token` rejects an
    agent PAT the same way it rejects a plain PAT."""
    import hashlib

    from src.repositories import access_token_repo

    _mock_public_dns(monkeypatch, "hooks.example.com")
    token_id = str(uuid.uuid4())
    agent_pat = create_access_token(
        user_id="owner1",
        email="owner@test.com",
        token_id=token_id,
        typ="agent_pat",
        extra_claims={"agent_id": env["agent_id"]},
    )
    access_token_repo().create(
        id=token_id,
        user_id="owner1",
        name="agent-pat",
        token_hash=hashlib.sha256(agent_pat.encode()).hexdigest(),
        prefix=token_id.replace("-", "")[:8],
        agent_id=env["agent_id"],
    )
    client = _AuthedClient(env["client"], agent_pat)

    resp = client.post("/api/v1/agents/support-bot/webhooks", json={"url": "https://hooks.example.com/x"})

    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET — list
# ---------------------------------------------------------------------------


def test_list_webhooks_omits_secret(owner_client, monkeypatch):
    _mock_public_dns(monkeypatch, "hooks.example.com")
    owner_client.post("/api/v1/agents/support-bot/webhooks", json={"url": "https://hooks.example.com/incoming"})

    resp = owner_client.get("/api/v1/agents/support-bot/webhooks")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 1
    assert "secret" not in body["data"][0]
    assert body["has_more"] is False


def test_list_webhooks_cross_owner_returns_404(other_client):
    resp = other_client.get("/api/v1/agents/support-bot/webhooks")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------


def test_delete_webhook_returns_204(owner_client, monkeypatch):
    _mock_public_dns(monkeypatch, "hooks.example.com")
    created = owner_client.post(
        "/api/v1/agents/support-bot/webhooks", json={"url": "https://hooks.example.com/incoming"}
    ).json()

    resp = owner_client.delete(f"/api/v1/agents/support-bot/webhooks/{created['id']}")
    assert resp.status_code == 204

    listed = owner_client.get("/api/v1/agents/support-bot/webhooks").json()
    assert listed["data"] == []


def test_delete_webhook_unknown_id_returns_404(owner_client):
    resp = owner_client.delete("/api/v1/agents/support-bot/webhooks/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "webhook_not_found"


def test_delete_webhook_cross_owner_returns_404(owner_client, other_client, monkeypatch):
    """`other1` owns a DIFFERENT agent (`others-bot`) — even though they can
    reach the DELETE route for their own agent's slug, owner1's webhook id
    must not be deletable through it."""
    _mock_public_dns(monkeypatch, "hooks.example.com")
    created = owner_client.post(
        "/api/v1/agents/support-bot/webhooks", json={"url": "https://hooks.example.com/incoming"}
    ).json()

    resp = other_client.delete(f"/api/v1/agents/others-bot/webhooks/{created['id']}")

    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "webhook_not_found"

    # still there, undamaged
    still_there = owner_client.get("/api/v1/agents/support-bot/webhooks").json()
    assert len(still_there["data"]) == 1


# ---------------------------------------------------------------------------
# enqueue_job_event_webhooks — the terminal-state fan-out
# ---------------------------------------------------------------------------


class _FakeJobsRepo:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict]] = []

    def enqueue(self, kind, payload, **kwargs):
        self.enqueued.append((kind, payload))
        return {"id": "fake-job-id", "kind": kind, "payload_json": payload, "deduped": False}


@pytest.fixture
def fake_jobs_repo(monkeypatch):
    fake = _FakeJobsRepo()
    import src.repositories as repositories

    monkeypatch.setattr(repositories, "jobs_repo", lambda: fake)
    return fake


def test_enqueue_fires_one_webhook_deliver_job_per_active_webhook(env, monkeypatch, fake_jobs_repo):
    from src.repositories import agent_webhooks_repo

    agent_webhooks_repo().create(
        id="w1",
        agent_id=env["agent_id"],
        owner_user_id="owner1",
        url="https://hooks.example.com/a",
        secret="secret-a",
        events="job.completed,job.failed",
    )
    agent_webhooks_repo().create(
        id="w2",
        agent_id=env["agent_id"],
        owner_user_id="owner1",
        url="https://hooks.example.com/b",
        secret="secret-b",
        events="job.completed",
    )
    # Disabled — must NOT get a delivery job.
    agent_webhooks_repo().create(
        id="w3",
        agent_id=env["agent_id"],
        owner_user_id="owner1",
        url="https://hooks.example.com/c",
        secret="secret-c",
        events="job.completed",
    )
    agent_webhooks_repo().disable("w3")

    from app.chat.webhook_delivery import enqueue_job_event_webhooks

    enqueue_job_event_webhooks(agent_id=env["agent_id"], job_id="job-123", status="completed")

    assert len(fake_jobs_repo.enqueued) == 2
    webhook_ids = {payload["webhook_id"] for _kind, payload in fake_jobs_repo.enqueued}
    assert webhook_ids == {"w1", "w2"}
    for kind, payload in fake_jobs_repo.enqueued:
        assert kind == "webhook-deliver"
        notification = payload["notification"]
        # NOTIFICATION payload only (C11) — never the agent's answer/prompt.
        assert set(notification) == {"event", "job_id", "agent_slug", "status", "ts"}
        assert notification["event"] == "job.completed"
        assert notification["job_id"] == "job-123"
        assert notification["agent_slug"] == "support-bot"
        assert notification["status"] == "completed"
        assert "answer" not in notification
        assert "prompt" not in notification
        assert "result" not in notification


def test_enqueue_only_fires_for_subscribed_event(env, fake_jobs_repo):
    from src.repositories import agent_webhooks_repo

    agent_webhooks_repo().create(
        id="w1",
        agent_id=env["agent_id"],
        owner_user_id="owner1",
        url="https://hooks.example.com/a",
        secret="s",
        events="job.completed",  # NOT subscribed to job.failed
    )

    from app.chat.webhook_delivery import enqueue_job_event_webhooks

    enqueue_job_event_webhooks(agent_id=env["agent_id"], job_id="job-123", status="failed")

    assert fake_jobs_repo.enqueued == []


def test_enqueue_unknown_status_is_a_noop(env, fake_jobs_repo):
    from src.repositories import agent_webhooks_repo

    agent_webhooks_repo().create(
        id="w1",
        agent_id=env["agent_id"],
        owner_user_id="owner1",
        url="https://hooks.example.com/a",
        secret="s",
        events="job.completed,job.failed",
    )

    from app.chat.webhook_delivery import enqueue_job_event_webhooks

    enqueue_job_event_webhooks(agent_id=env["agent_id"], job_id="job-123", status="queued")

    assert fake_jobs_repo.enqueued == []


def test_enqueue_no_active_webhooks_is_a_noop(env, fake_jobs_repo):
    from app.chat.webhook_delivery import enqueue_job_event_webhooks

    enqueue_job_event_webhooks(agent_id=env["agent_id"], job_id="job-123", status="completed")

    assert fake_jobs_repo.enqueued == []
