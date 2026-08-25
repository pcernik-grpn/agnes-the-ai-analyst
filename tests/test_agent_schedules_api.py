"""`/api/v1/agents/{slug}/schedules` owner CRUD + `POST /api/v1/agents/run-due`
sweep (design doc docs/superpowers/specs/2026-08-17-agent-schedules-design.md).

Auth/ownership tests reuse the `env`/`_AuthedClient` pattern from
`tests/test_agent_webhooks_api.py`.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

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

    def patch(self, url, **kw):
        kw["headers"] = self._headers(kw.get("headers"))
        return self._client.patch(url, **kw)

    def delete(self, url, **kw):
        kw["headers"] = self._headers(kw.get("headers"))
        return self._client.delete(url, **kw)


@pytest.fixture
def env(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")

    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories import agents_repo
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="owner1", email="owner@test.com", name="Owner")
    UserRepository(conn).create(id="other1", email="other@test.com", name="Other")
    UserRepository(conn).create(id="admin1", email="admin@test.com", name="Admin")
    admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()[0]
    UserGroupMembersRepository(conn).add_member("admin1", admin_gid, source="system_seed")
    conn.close()

    agent_id = str(uuid.uuid4())
    agents_repo().create(id=agent_id, owner_user_id="owner1", name="Briefing Bot", slug="briefing-bot")
    other_agent_id = str(uuid.uuid4())
    agents_repo().create(id=other_agent_id, owner_user_id="other1", name="Other's Bot", slug="others-bot")

    client = TestClient(shared_app)
    return {
        "client": client,
        "owner_token": create_access_token("owner1", "owner@test.com"),
        "other_token": create_access_token("other1", "other@test.com"),
        "admin_token": create_access_token("admin1", "admin@test.com"),
        "agent_id": agent_id,
        "other_agent_id": other_agent_id,
    }


@pytest.fixture
def owner_client(env):
    return _AuthedClient(env["client"], env["owner_token"])


@pytest.fixture
def other_client(env):
    return _AuthedClient(env["client"], env["other_token"])


@pytest.fixture
def admin_client(env):
    return _AuthedClient(env["client"], env["admin_token"])


_VALID_PAYLOAD = {"name": "morning-briefing", "schedule": "cron 0 7 * * 1-5", "prompt": "invoke the briefing skill"}


def _backdate(schedule_id: str, days: int = 3) -> None:
    """Creation anchors the cadence (last_run_at is stamped at insert), so a
    brand-new row is never immediately due. Tests that need a DUE row push
    its anchor into the past through the public claim primitive."""
    from src.repositories import agent_schedules_repo

    repo = agent_schedules_repo()
    row = repo.get(schedule_id)
    assert row is not None
    past = datetime.now(timezone.utc) - timedelta(days=days)
    assert repo.claim_for_run(schedule_id, row["last_run_at"], past) is True


# ---------------------------------------------------------------------------
# POST — create
# ---------------------------------------------------------------------------


def test_create_schedule_returns_the_row(owner_client):
    resp = owner_client.post("/api/v1/agents/briefing-bot/schedules", json=_VALID_PAYLOAD)

    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "morning-briefing"
    assert body["schedule"] == "cron 0 7 * * 1-5"
    assert body["prompt"] == "invoke the briefing skill"
    assert body["enabled"] is True
    # Cadence anchors at creation — a new schedule is NOT immediately due.
    assert body["last_run_at"] is not None
    assert body["last_status"] is None
    assert body["last_job_id"] is None


def test_create_schedule_accepts_enabled_false(owner_client):
    resp = owner_client.post("/api/v1/agents/briefing-bot/schedules", json={**_VALID_PAYLOAD, "enabled": False})
    assert resp.status_code == 201
    assert resp.json()["enabled"] is False


def test_create_schedule_unknown_agent_returns_404(owner_client):
    resp = owner_client.post("/api/v1/agents/nonexistent/schedules", json=_VALID_PAYLOAD)
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "agent_not_found"


def test_create_schedule_cross_owner_slug_returns_404(other_client):
    resp = other_client.post("/api/v1/agents/briefing-bot/schedules", json=_VALID_PAYLOAD)
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "agent_not_found"


def test_create_schedule_rejects_agent_pat(env):
    """A schedule is a standing config an owner sets up once —
    `require_session_token` rejects an agent PAT the same way it rejects a
    plain PAT (mirrors `app/api/agent_webhooks.py`)."""
    from src.repositories import access_token_repo

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

    resp = client.post("/api/v1/agents/briefing-bot/schedules", json=_VALID_PAYLOAD)

    assert resp.status_code == 403


@pytest.mark.parametrize(
    "schedule",
    ["weekly", "every -1m", "daily 25:00", "0 7 * * 1-5", "cron 99 5 7 * *"],
)
def test_create_schedule_rejects_invalid_schedule_grammar(owner_client, schedule):
    resp = owner_client.post("/api/v1/agents/briefing-bot/schedules", json={**_VALID_PAYLOAD, "schedule": schedule})
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "invalid_schedule"
    # Names the known "cron " prefix footgun.
    assert "cron " in resp.json()["detail"]["message"]


def test_create_schedule_rejects_empty_prompt(owner_client):
    resp = owner_client.post("/api/v1/agents/briefing-bot/schedules", json={**_VALID_PAYLOAD, "prompt": "   "})
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "invalid_prompt"


@pytest.mark.parametrize("name", ["", "has spaces", "has/slash", "a" * 65])
def test_create_schedule_rejects_invalid_name(owner_client, name):
    resp = owner_client.post("/api/v1/agents/briefing-bot/schedules", json={**_VALID_PAYLOAD, "name": name})
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "invalid_name"


def test_create_schedule_duplicate_name_returns_409(owner_client):
    owner_client.post("/api/v1/agents/briefing-bot/schedules", json=_VALID_PAYLOAD)
    resp = owner_client.post("/api/v1/agents/briefing-bot/schedules", json=_VALID_PAYLOAD)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "schedule_name_taken"


def test_create_schedule_same_name_different_agent_is_fine(owner_client, other_client):
    resp1 = owner_client.post("/api/v1/agents/briefing-bot/schedules", json=_VALID_PAYLOAD)
    resp2 = other_client.post("/api/v1/agents/others-bot/schedules", json=_VALID_PAYLOAD)
    assert resp1.status_code == 201
    assert resp2.status_code == 201


def test_create_schedule_enforces_cap(owner_client):
    for i in range(20):
        resp = owner_client.post(
            "/api/v1/agents/briefing-bot/schedules",
            json={**_VALID_PAYLOAD, "name": f"schedule-{i}"},
        )
        assert resp.status_code == 201
    resp = owner_client.post(
        "/api/v1/agents/briefing-bot/schedules",
        json={**_VALID_PAYLOAD, "name": "schedule-21"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "schedule_limit"


# ---------------------------------------------------------------------------
# GET — list
# ---------------------------------------------------------------------------


def test_list_schedules(owner_client):
    owner_client.post("/api/v1/agents/briefing-bot/schedules", json=_VALID_PAYLOAD)

    resp = owner_client.get("/api/v1/agents/briefing-bot/schedules")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["name"] == "morning-briefing"
    assert body["has_more"] is False


def test_list_schedules_cross_owner_returns_404(other_client):
    resp = other_client.get("/api/v1/agents/briefing-bot/schedules")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PATCH — update
# ---------------------------------------------------------------------------


def test_patch_schedule_updates_fields(owner_client):
    created = owner_client.post("/api/v1/agents/briefing-bot/schedules", json=_VALID_PAYLOAD).json()

    resp = owner_client.patch(
        f"/api/v1/agents/briefing-bot/schedules/{created['id']}",
        json={"schedule": "cron 0 9 * * 0,6", "enabled": False},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["schedule"] == "cron 0 9 * * 0,6"
    assert body["enabled"] is False
    assert body["name"] == "morning-briefing"  # untouched


def test_patch_schedule_rejects_explicit_null_enabled(owner_client):
    """An explicitly-sent `"enabled": null` survives exclude_unset; before the
    guard it hit the column's NOT NULL constraint, which the race backstop
    mapped to a misleading 409 schedule_name_taken (Devin Review on #1404)."""
    created = owner_client.post("/api/v1/agents/briefing-bot/schedules", json=_VALID_PAYLOAD).json()
    resp = owner_client.patch(
        f"/api/v1/agents/briefing-bot/schedules/{created['id']}",
        json={"enabled": None},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "invalid_enabled"


def test_patch_schedule_rejects_invalid_schedule(owner_client):
    created = owner_client.post("/api/v1/agents/briefing-bot/schedules", json=_VALID_PAYLOAD).json()
    resp = owner_client.patch(
        f"/api/v1/agents/briefing-bot/schedules/{created['id']}",
        json={"schedule": "weekly"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "invalid_schedule"


def test_patch_schedule_rename_to_taken_name_returns_409(owner_client):
    owner_client.post("/api/v1/agents/briefing-bot/schedules", json=_VALID_PAYLOAD)
    other = owner_client.post(
        "/api/v1/agents/briefing-bot/schedules", json={**_VALID_PAYLOAD, "name": "weekend-briefing"}
    ).json()

    resp = owner_client.patch(
        f"/api/v1/agents/briefing-bot/schedules/{other['id']}",
        json={"name": "morning-briefing"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "schedule_name_taken"


def test_patch_schedule_rename_to_its_own_name_is_fine(owner_client):
    created = owner_client.post("/api/v1/agents/briefing-bot/schedules", json=_VALID_PAYLOAD).json()
    resp = owner_client.patch(
        f"/api/v1/agents/briefing-bot/schedules/{created['id']}",
        json={"name": "morning-briefing"},
    )
    assert resp.status_code == 200


def test_patch_schedule_unknown_id_returns_404(owner_client):
    resp = owner_client.patch("/api/v1/agents/briefing-bot/schedules/does-not-exist", json={"enabled": False})
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "schedule_not_found"


def test_patch_schedule_cross_owner_returns_404(owner_client, other_client):
    created = owner_client.post("/api/v1/agents/briefing-bot/schedules", json=_VALID_PAYLOAD).json()
    resp = other_client.patch(
        f"/api/v1/agents/others-bot/schedules/{created['id']}",
        json={"enabled": False},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "schedule_not_found"


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------


def test_delete_schedule_returns_204(owner_client):
    created = owner_client.post("/api/v1/agents/briefing-bot/schedules", json=_VALID_PAYLOAD).json()

    resp = owner_client.delete(f"/api/v1/agents/briefing-bot/schedules/{created['id']}")
    assert resp.status_code == 204

    listed = owner_client.get("/api/v1/agents/briefing-bot/schedules").json()
    assert listed["data"] == []


def test_delete_schedule_unknown_id_returns_404(owner_client):
    resp = owner_client.delete("/api/v1/agents/briefing-bot/schedules/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "schedule_not_found"


def test_delete_schedule_cross_owner_returns_404(owner_client, other_client):
    created = owner_client.post("/api/v1/agents/briefing-bot/schedules", json=_VALID_PAYLOAD).json()

    resp = other_client.delete(f"/api/v1/agents/others-bot/schedules/{created['id']}")

    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "schedule_not_found"
    still_there = owner_client.get("/api/v1/agents/briefing-bot/schedules").json()
    assert len(still_there["data"]) == 1


# ---------------------------------------------------------------------------
# POST /run-due — the admin/scheduler-driven sweep
# ---------------------------------------------------------------------------


def test_run_due_requires_admin(owner_client):
    resp = owner_client.post("/api/v1/agents/run-due")
    assert resp.status_code == 403


def test_run_due_dispatches_a_due_schedule(env, admin_client, owner_client):
    from src.repositories import jobs_repo

    created = owner_client.post("/api/v1/agents/briefing-bot/schedules", json=_VALID_PAYLOAD).json()
    _backdate(created["id"])

    resp = admin_client.post("/api/v1/agents/run-due")

    assert resp.status_code == 200
    body = resp.json()
    assert body["dispatched"] == [created["id"]]
    assert body["count"] == 1

    row = owner_client.get("/api/v1/agents/briefing-bot/schedules").json()["data"][0]
    assert row["last_status"] == "enqueued"
    assert row["last_job_id"] is not None
    assert row["last_run_at"] is not None

    job = jobs_repo().get(row["last_job_id"])
    assert job is not None
    assert job["kind"] == "agent_response"
    payload = job["payload_json"]
    assert payload["mode"] == "fresh"
    assert payload["owner_user_id"] == "owner1"
    assert payload["owner_email"] == "owner@test.com"
    assert payload["agent_id"] == env["agent_id"]
    assert payload["prompt"] == "invoke the briefing skill"
    assert job["idempotency_key"].startswith(f"agent-schedule:{created['id']}:")


def test_run_due_skips_disabled_schedule(admin_client, owner_client):
    created = owner_client.post(
        "/api/v1/agents/briefing-bot/schedules", json={**_VALID_PAYLOAD, "enabled": False}
    ).json()
    _backdate(created["id"])  # due-but-disabled — the skip must be the flag

    resp = admin_client.post("/api/v1/agents/run-due")

    assert resp.status_code == 200
    assert resp.json()["dispatched"] == []


def test_run_due_skips_not_yet_due_schedule(admin_client, owner_client):
    from src.repositories import agent_schedules_repo

    created = owner_client.post(
        "/api/v1/agents/briefing-bot/schedules", json={**_VALID_PAYLOAD, "schedule": "every 1h"}
    ).json()
    # Creation stamps last_run_at, so a just-created hourly row is
    # inherently "just ran" — not due again for another hour.
    assert agent_schedules_repo().get(created["id"])["last_run_at"] is not None

    resp = admin_client.post("/api/v1/agents/run-due")

    assert resp.status_code == 200
    assert resp.json()["dispatched"] == []


def test_run_due_skips_schedule_for_soft_deleted_agent(env, admin_client, owner_client):
    from src.repositories import agents_repo

    created = owner_client.post("/api/v1/agents/briefing-bot/schedules", json=_VALID_PAYLOAD).json()
    _backdate(created["id"])  # due — the skip must be the soft-deleted agent
    agents_repo().soft_delete(env["agent_id"])

    resp = admin_client.post("/api/v1/agents/run-due")

    assert resp.status_code == 200
    assert resp.json()["dispatched"] == []
    assert created["id"] not in resp.json()["dispatched"]


def test_run_due_atomic_claim_under_a_concurrent_sweep(env, owner_client):
    """Two sweep ticks racing on the SAME stale snapshot of a row must not
    both dispatch it — only the first claim wins."""
    from app.api.agent_schedules import _dispatch_if_due
    from src.repositories import agent_schedules_repo

    created = owner_client.post("/api/v1/agents/briefing-bot/schedules", json=_VALID_PAYLOAD).json()
    _backdate(created["id"])
    row = agent_schedules_repo().get(created["id"])
    now = datetime.now(timezone.utc)

    first = _dispatch_if_due(dict(row), now)
    second = _dispatch_if_due(dict(row), now)

    assert first is True
    assert second is False


def test_run_due_one_bad_row_does_not_abort_the_sweep(env, admin_client, owner_client, monkeypatch):
    """A per-row exception is logged and skipped — the sweep must keep
    going and still dispatch every other due row."""
    from src.repositories import agents_repo

    created = owner_client.post("/api/v1/agents/briefing-bot/schedules", json=_VALID_PAYLOAD).json()
    _backdate(created["id"])

    real_get_by_id = agents_repo().__class__.get_by_id
    calls = {"n": 0}

    def _boom(self, agent_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real_get_by_id(self, agent_id)

    monkeypatch.setattr(agents_repo().__class__, "get_by_id", _boom)

    resp = admin_client.post("/api/v1/agents/run-due")

    assert resp.status_code == 200
    # The sweep survived the exception — it just didn't dispatch anything
    # this tick (only one row existed, and it hit the boom).
    assert created["id"] not in resp.json()["dispatched"]


def test_agent_delete_cascades_schedules(env, owner_client):
    """Schedules die with the agent — the delete cascade must clear
    agent_schedules rows, not leave them orphaned behind the run-due sweep's
    soft-deleted-agent skip."""
    from src.repositories import agent_schedules_repo

    created = owner_client.post("/api/v1/agents/briefing-bot/schedules", json=_VALID_PAYLOAD).json()
    assert agent_schedules_repo().list_for_agent(env["agent_id"]), "precondition: schedule row exists"

    resp = owner_client.delete(f"/api/v1/agents/{env['agent_id']}")
    assert resp.status_code == 204

    assert agent_schedules_repo().list_for_agent(env["agent_id"]) == []
    assert agent_schedules_repo().get(created["id"]) is None


def test_run_due_does_not_stack_jobs_while_previous_run_is_queued(env, admin_client, owner_client):
    """On a topology where no worker claims agent_response jobs, the sweep
    must not enqueue a fresh job every cadence hit — one queued job per
    schedule is the ceiling; further due ticks record 'backlogged'."""
    from src.repositories import jobs_repo

    created = owner_client.post("/api/v1/agents/briefing-bot/schedules", json=_VALID_PAYLOAD).json()
    _backdate(created["id"])

    assert admin_client.post("/api/v1/agents/run-due").json()["dispatched"] == [created["id"]]
    row = owner_client.get("/api/v1/agents/briefing-bot/schedules").json()["data"][0]
    first_job_id = row["last_job_id"]
    assert jobs_repo().get(first_job_id)["status"] == "queued"  # no worker in tests

    # Due again, previous job still queued → backlogged, no second job.
    _backdate(created["id"])
    resp = admin_client.post("/api/v1/agents/run-due")
    assert resp.json()["dispatched"] == [created["id"]]  # tick consumed
    row = owner_client.get("/api/v1/agents/briefing-bot/schedules").json()["data"][0]
    assert row["last_status"] == "backlogged"
    assert row["last_job_id"] == first_job_id
