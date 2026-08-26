"""`GET /api/v1/agents/{slug}/usage` (agent-api V1b Task 8).

Auth is the same `require_agent_runtime_principal` chain `/responses` uses
(owner or agent-PAT scoped to this exact agent, plus the `ResourceType.CHAT`
grant) — the `env` fixture mirrors `tests/test_agent_sessions_api.py`'s
(Everyone group granted `chat`), not `tests/test_agent_webhooks_api.py`'s
(which uses `require_session_token`, a different auth chain that needs no
grant).

Usage rows are seeded directly via `llm_usage_repo().insert_batch(...)` —
`created_at` always lands in the current UTC month (the table's own DB
default), so period-filter coverage asserts an out-of-range `period` sums
to zero rather than fabricating historical rows.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.auth.jwt import create_access_token


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _current_year_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


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

    budgeted_id = str(uuid.uuid4())
    agents_repo().create(
        id=budgeted_id,
        owner_user_id="owner1",
        name="Budgeted Bot",
        slug="budgeted-bot",
        token_budget_monthly=100,
    )

    other_agent_id = str(uuid.uuid4())
    agents_repo().create(id=other_agent_id, owner_user_id="other1", name="Other's Bot", slug="others-bot")

    client = TestClient(shared_app)
    return {
        "client": client,
        "owner_token": create_access_token("owner1", "owner@test.com"),
        "other_token": create_access_token("other1", "other@test.com"),
        "agent_id": agent_id,
        "budgeted_agent_id": budgeted_id,
        "other_agent_id": other_agent_id,
    }


def _seed_usage(agent_id: str, *, input_tokens: int, output_tokens: int, cache_read: int, cache_creation: int) -> None:
    from src.repositories import llm_usage_repo

    llm_usage_repo().insert_batch(
        [
            {
                "id": uuid.uuid4().hex,
                "agent_id": agent_id,
                "user_id": "owner1",
                "session_id": "sess-1",
                "model": "claude-sonnet-5",
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_tokens": cache_read,
                "cache_creation_tokens": cache_creation,
            }
        ]
    )


# ---------------------------------------------------------------------------
# Happy path — breakdown + defaults
# ---------------------------------------------------------------------------


def test_usage_default_period_sums_current_month(env):
    _seed_usage(env["agent_id"], input_tokens=100, output_tokens=50, cache_read=10, cache_creation=5)

    resp = env["client"].get("/api/v1/agents/support-bot/usage", headers=_auth(env["owner_token"]))

    assert resp.status_code == 200
    body = resp.json()
    assert body["period"] == _current_year_month()
    assert body["agent_slug"] == "support-bot"
    assert body["input_tokens"] == 100
    assert body["output_tokens"] == 50
    assert body["cache_read_tokens"] == 10
    assert body["cache_creation_tokens"] == 5
    # total_tokens EXCLUDES cache_read_tokens (mirrors month_total_tokens /
    # the budget-governing quantity) — 100 + 50 + 5, not +10.
    assert body["total_tokens"] == 155
    assert body["budget_limit"] is None
    assert body["budget_remaining"] is None


def test_usage_no_rows_returns_zeros(env):
    resp = env["client"].get("/api/v1/agents/support-bot/usage", headers=_auth(env["owner_token"]))

    assert resp.status_code == 200
    body = resp.json()
    assert body["input_tokens"] == 0
    assert body["output_tokens"] == 0
    assert body["cache_read_tokens"] == 0
    assert body["cache_creation_tokens"] == 0
    assert body["total_tokens"] == 0


def test_usage_explicit_current_period_matches_default(env):
    _seed_usage(env["agent_id"], input_tokens=10, output_tokens=5, cache_read=0, cache_creation=0)
    ym = _current_year_month()

    resp = env["client"].get(
        "/api/v1/agents/support-bot/usage",
        params={"period": ym},
        headers=_auth(env["owner_token"]),
    )

    assert resp.status_code == 200
    assert resp.json()["period"] == ym
    assert resp.json()["total_tokens"] == 15


def test_usage_out_of_range_period_returns_zeros(env):
    _seed_usage(env["agent_id"], input_tokens=999, output_tokens=999, cache_read=0, cache_creation=0)

    resp = env["client"].get(
        "/api/v1/agents/support-bot/usage",
        params={"period": "2019-01"},
        headers=_auth(env["owner_token"]),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["period"] == "2019-01"
    assert body["total_tokens"] == 0


@pytest.mark.parametrize("bad_period", ["2026", "26-07", "2026/07", "not-a-period", "2026-13"])
def test_usage_invalid_period_returns_400(env, bad_period):
    resp = env["client"].get(
        "/api/v1/agents/support-bot/usage",
        params={"period": bad_period},
        headers=_auth(env["owner_token"]),
    )

    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "invalid_period"


# ---------------------------------------------------------------------------
# Budget accounting
# ---------------------------------------------------------------------------


def test_usage_budget_limit_and_remaining(env):
    _seed_usage(env["budgeted_agent_id"], input_tokens=20, output_tokens=10, cache_read=0, cache_creation=0)

    resp = env["client"].get("/api/v1/agents/budgeted-bot/usage", headers=_auth(env["owner_token"]))

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_tokens"] == 30
    assert body["budget_limit"] == 100
    assert body["budget_remaining"] == 70


def test_usage_budget_remaining_floors_at_zero_when_over_budget(env):
    _seed_usage(env["budgeted_agent_id"], input_tokens=80, output_tokens=80, cache_read=0, cache_creation=0)

    resp = env["client"].get("/api/v1/agents/budgeted-bot/usage", headers=_auth(env["owner_token"]))

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_tokens"] == 160
    assert body["budget_limit"] == 100
    assert body["budget_remaining"] == 0


def test_usage_cache_read_tokens_excluded_from_budget_math(env):
    """A huge cache_read burst must not eat into budget_remaining — only
    input/output/cache_creation count against `token_budget_monthly`
    (matches `app.api.broker_agent_policy.check_budget`)."""
    _seed_usage(env["budgeted_agent_id"], input_tokens=10, output_tokens=10, cache_read=10_000, cache_creation=0)

    resp = env["client"].get("/api/v1/agents/budgeted-bot/usage", headers=_auth(env["owner_token"]))

    body = resp.json()
    assert body["cache_read_tokens"] == 10_000
    assert body["total_tokens"] == 20
    assert body["budget_remaining"] == 80


# ---------------------------------------------------------------------------
# Auth / ownership
# ---------------------------------------------------------------------------


def test_usage_unknown_agent_returns_404(env):
    resp = env["client"].get("/api/v1/agents/nonexistent/usage", headers=_auth(env["owner_token"]))
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "agent_not_found"


def test_usage_cross_owner_slug_returns_404(env):
    """`other1` has no agent named `support-bot` (owner1's) — existence of
    owner1's agent must not leak."""
    resp = env["client"].get("/api/v1/agents/support-bot/usage", headers=_auth(env["other_token"]))
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "agent_not_found"


def test_usage_requires_auth(env):
    resp = env["client"].get("/api/v1/agents/support-bot/usage")
    assert resp.status_code == 401


def test_usage_agent_pat_scoped_to_matching_agent_succeeds(env):
    import hashlib

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

    resp = env["client"].get("/api/v1/agents/support-bot/usage", headers=_auth(agent_pat))

    assert resp.status_code == 200


def test_usage_agent_pat_wrong_agent_returns_403(env):
    import hashlib

    from src.repositories import access_token_repo

    token_id = str(uuid.uuid4())
    agent_pat = create_access_token(
        user_id="owner1",
        email="owner@test.com",
        token_id=token_id,
        typ="agent_pat",
        extra_claims={"agent_id": env["budgeted_agent_id"]},
    )
    access_token_repo().create(
        id=token_id,
        user_id="owner1",
        name="agent-pat",
        token_hash=hashlib.sha256(agent_pat.encode()).hexdigest(),
        prefix=token_id.replace("-", "")[:8],
        agent_id=env["budgeted_agent_id"],
    )

    # This PAT is scoped to `budgeted-bot`, not `support-bot`.
    resp = env["client"].get("/api/v1/agents/support-bot/usage", headers=_auth(agent_pat))

    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "agent_pat_wrong_agent"
