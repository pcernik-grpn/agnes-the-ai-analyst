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

    from src.db import SYSTEM_ADMIN_GROUP, SYSTEM_EVERYONE_GROUP, get_system_db
    from src.repositories import agents_repo, resource_grants_repo, user_group_members_repo, user_groups_repo
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="owner1", email="owner@test.com", name="Owner")
    UserRepository(conn).create(id="other1", email="other@test.com", name="Other")
    UserRepository(conn).create(id="admin1", email="admin@test.com", name="Admin")
    UserRepository(conn).create(id="grantee1", email="grantee@test.com", name="Grantee")
    conn.close()

    everyone = user_groups_repo().get_by_name(SYSTEM_EVERYONE_GROUP)
    for uid in ("owner1", "other1", "admin1", "grantee1"):
        user_group_members_repo().add_member(uid, everyone["id"], source="system_seed")
    resource_grants_repo().create(everyone["id"], "chat", "chat")

    admin_group = user_groups_repo().get_by_name(SYSTEM_ADMIN_GROUP)
    user_group_members_repo().add_member("admin1", admin_group["id"], source="system_seed")

    agent_id = str(uuid.uuid4())
    agents_repo().create(id=agent_id, owner_user_id="owner1", name="Support Bot", slug="support-bot")

    # C2.4 (shared-agent, C2.3): `grantee1` is neither owner nor admin, but
    # was shared this agent via a `ResourceType.AGENT` grant, same as
    # `tests/test_agent_sessions_api.py::_grant_agent_to_group`.
    grantee_group = user_groups_repo().create(name="c24-usage-grantee-group", created_by="owner1")
    user_group_members_repo().add_member("grantee1", grantee_group["id"], source="admin", added_by="owner1")
    resource_grants_repo().create(grantee_group["id"], "agent", agent_id, assigned_by="owner1")

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
        "admin_token": create_access_token("admin1", "admin@test.com"),
        "grantee_token": create_access_token("grantee1", "grantee@test.com"),
        "agent_id": agent_id,
        "budgeted_agent_id": budgeted_id,
        "other_agent_id": other_agent_id,
    }


def _seed_usage(
    agent_id: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read: int,
    cache_creation: int,
    caller_user_id: str | None = None,
) -> None:
    from src.repositories import llm_usage_repo

    llm_usage_repo().insert_batch(
        [
            {
                "id": uuid.uuid4().hex,
                "agent_id": agent_id,
                "user_id": "owner1",
                "caller_user_id": caller_user_id,
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


# ---------------------------------------------------------------------------
# C2.4 — per-caller usage attribution: `by_caller` breakdown, owner-or-admin
# only (`env`'s `grantee1` was shared `support-bot` via a `ResourceType.
# AGENT` grant, C2.3 — a plain runnable grantee must never see other
# callers' usage).
#
# The RBAC gating below is exercised through a fake `llm_usage_repo()`
# rather than real seeded rows: the real per-caller breakdown is a
# genuinely Postgres-only capability (`caller_user_id` is a PG-only column,
# `tests/db_pg/test_llm_usage_contract.py` pins the exact per-backend
# content), and this test suite's default backend is DuckDB, which would
# silently degrade every row to a single `caller_user_id=None` bucket —
# hiding a broken RBAC gate behind a backend limitation. The fake isolates
# "does THIS endpoint show/hide `by_caller` for THIS caller" from "does
# THIS backend persist enough to compute it".
# ---------------------------------------------------------------------------


class _FakeBreakdownRepo:
    def __init__(self):
        self.by_caller_calls = 0

    def usage_breakdown_for_month(self, agent_id, year_month):
        return {
            "input_tokens": 30,
            "output_tokens": 15,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "total_tokens": 45,
        }

    def usage_breakdown_by_caller_for_month(self, agent_id, year_month):
        self.by_caller_calls += 1
        return [
            {
                "caller_user_id": "owner1",
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "total_tokens": 15,
            },
            {
                "caller_user_id": "grantee1",
                "input_tokens": 20,
                "output_tokens": 10,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "total_tokens": 30,
            },
        ]


def test_usage_by_caller_breakdown_visible_to_owner(env, monkeypatch):
    fake = _FakeBreakdownRepo()
    monkeypatch.setattr("app.api.agent_runtime.llm_usage_repo", lambda: fake)

    resp = env["client"].get("/api/v1/agents/support-bot/usage", headers=_auth(env["owner_token"]))

    assert resp.status_code == 200
    body = resp.json()
    # Aggregate is unaffected by attribution -- still the SUM across callers.
    assert body["total_tokens"] == 45
    by_caller = {row["caller_user_id"]: row for row in body["by_caller"]}
    assert set(by_caller) == {"owner1", "grantee1"}
    assert by_caller["owner1"]["total_tokens"] == 15
    assert by_caller["grantee1"]["total_tokens"] == 30
    assert fake.by_caller_calls == 1


def test_usage_by_caller_breakdown_visible_to_admin_with_no_grant(env, monkeypatch):
    """An admin who is neither the owner nor a grantee still sees the
    breakdown -- inspection, not run authority (see
    `require_agent_usage_principal`'s docstring). Addressed by id, since
    `support-bot` is only unique within OWNER1's own slug namespace."""
    fake = _FakeBreakdownRepo()
    monkeypatch.setattr("app.api.agent_runtime.llm_usage_repo", lambda: fake)

    resp = env["client"].get(f"/api/v1/agents/{env['agent_id']}/usage", headers=_auth(env["admin_token"]))

    assert resp.status_code == 200
    body = resp.json()
    by_caller = {row["caller_user_id"]: row for row in body["by_caller"]}
    assert set(by_caller) == {"owner1", "grantee1"}


def test_usage_by_caller_breakdown_hidden_from_a_plain_grantee(env, monkeypatch):
    """`grantee1` was shared `support-bot` (runs it, C2.3) but is not its
    owner and not an admin -- it can see the AGGREGATE total, never the
    per-caller split (that would leak the owner's own usage). The
    breakdown repo method is never even CALLED for this caller."""
    fake = _FakeBreakdownRepo()
    monkeypatch.setattr("app.api.agent_runtime.llm_usage_repo", lambda: fake)

    resp = env["client"].get(f"/api/v1/agents/{env['agent_id']}/usage", headers=_auth(env["grantee_token"]))

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_tokens"] == 45
    assert body["by_caller"] is None
    assert fake.by_caller_calls == 0


def test_usage_unrelated_user_by_id_still_404s(env):
    """The admin inspection fallback in `require_agent_usage_principal`
    must not accidentally widen access for a NON-admin -- `other1` has no
    relationship to `support-bot` (not owner, not a grantee, not admin)."""
    resp = env["client"].get(f"/api/v1/agents/{env['agent_id']}/usage", headers=_auth(env["other_token"]))

    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "agent_not_found"
