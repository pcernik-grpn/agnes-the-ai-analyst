"""#45: per-IP rate limiting on auth endpoints.

Each test re-enables the limiter (the autouse conftest fixture disables it
by default for the rest of the suite) and resets bucket state to avoid
order-dependence. Limits live in ``app.auth.providers.*`` and
``app.auth.router`` decorators — adjust here when you bump them.
"""

from __future__ import annotations

import tempfile
import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        from src.db import close_system_db

        close_system_db()
        yield tmp
        close_system_db()


@pytest.fixture
def app_with_ratelimit(monkeypatch, fresh_db):
    """TestClient with the limiter forced on, bucket reset before each call."""
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
    monkeypatch.setenv("AGNES_AUTH_RATELIMIT_ENABLED", "1")
    # This file tests rate-limit mechanics, not the default-offering policy
    # (that's tests/test_auth_provider_defaults.py). Email is opt-in-only by
    # default since B6, so pin an explicit allowlist naming every provider
    # exercised below — otherwise the magic-link endpoints 404 before the
    # limiter ever runs.
    monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "google,email,password,keboola,microsoft")
    from app.auth.rate_limit import limiter

    limiter.enabled = True
    limiter.reset()
    from app.main import app

    return TestClient(app)


def _seed_admin(fresh_db, password: str | None = None):
    """Seed an admin user, optionally with an argon2-hashed password set."""
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.users import UserRepository

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        password_hash = None
        if password:
            from argon2 import PasswordHasher

            password_hash = PasswordHasher().hash(password)
        UserRepository(conn).create(
            id=uid,
            email="admin@test",
            name="Admin",
            password_hash=password_hash,
        )
        admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()[0]
        UserGroupMembersRepository(conn).add_member(uid, admin_gid, source="system_seed")
        return uid
    finally:
        conn.close()


def test_password_login_rate_limited_after_10_requests(app_with_ratelimit, fresh_db):
    """11th request inside the per-minute window → 429."""
    _seed_admin(fresh_db, password="correct-horse-battery-staple")
    statuses = []
    for _ in range(11):
        resp = app_with_ratelimit.post(
            "/auth/password/login",
            json={"email": "admin@test", "password": "wrong"},
        )
        statuses.append(resp.status_code)
    # First 10 may be 401 (wrong password); the 11th must be 429 from slowapi.
    assert statuses[:10] == [401] * 10, f"unexpected pre-limit statuses: {statuses[:10]}"
    assert statuses[10] == 429, f"expected 11th request to 429, got {statuses[10]}"


def test_email_send_link_rate_limited_after_5_requests(app_with_ratelimit, fresh_db):
    """6th /send-link inside the per-minute window → 429.

    Covers the email-bombing scenario: a single IP rotating through random
    recipient addresses gets throttled regardless of whether each address
    actually exists (the endpoint always returns success to prevent
    enumeration, so the limit is the only gate)."""
    statuses = []
    for i in range(6):
        resp = app_with_ratelimit.post(
            "/auth/email/send-link",
            json={"email": f"victim-{i}@example.com"},
        )
        statuses.append(resp.status_code)
    assert statuses[:5] == [200] * 5, f"unexpected pre-limit statuses: {statuses[:5]}"
    assert statuses[5] == 429, f"expected 6th request to 429, got {statuses[5]}"


def test_email_send_link_web_shares_budget_with_json(app_with_ratelimit, fresh_db):
    """The JSON /send-link and the HTML-form /send-link/web must share ONE
    5/min bucket (slowapi shared_limit scope), or an abuser gets 10/min to the
    same address by alternating endpoints (Devin review on #1288). Five JSON
    calls exhaust the budget; the very next /send-link/web call 429s."""
    for i in range(5):
        assert app_with_ratelimit.post("/auth/email/send-link", json={"email": f"v{i}@example.com"}).status_code == 200
    # Budget spent — the web variant, sharing the same scope, must 429.
    resp = app_with_ratelimit.post("/auth/email/send-link/web", data={"email": "v-web@example.com"})
    assert resp.status_code == 429, f"expected shared-budget 429 on the web variant, got {resp.status_code}"


def test_bootstrap_rate_limited_after_3_requests(app_with_ratelimit, fresh_db):
    """4th /auth/bootstrap inside the per-minute window → 429.

    Bootstrap is one-shot in normal use; the tight 3/minute limit exists
    to slow brute-force enumeration of the 'no users with password yet'
    state without breaking legitimate retry-on-typo flows."""
    statuses = []
    for i in range(4):
        resp = app_with_ratelimit.post(
            "/auth/bootstrap",
            json={"email": f"first-admin-{i}@example.com", "password": "x" * 12},
        )
        statuses.append(resp.status_code)
    # First request 200 (bootstrap path), subsequent 403 (already bootstrapped),
    # but the count includes ALL requests — 4th must be 429 regardless of
    # business-logic outcome of requests 2-3.
    assert statuses[3] == 429, f"expected 4th /bootstrap to 429, got {statuses[3]} (full sequence: {statuses})"


def test_password_reset_rate_limited_after_5_requests(app_with_ratelimit, fresh_db):
    """6th /auth/password/reset → 429. Same email-bombing surface as
    /send-link — anti-enumeration response, sends mail per request, attacker
    rotates random recipients from a single IP. Pre-fix this endpoint was
    unthrottled even though /send-link was — code-reviewer flagged the gap."""
    statuses = []
    for i in range(6):
        resp = app_with_ratelimit.post(
            "/auth/password/reset",
            data={"email": f"victim-{i}@example.com"},
        )
        statuses.append(resp.status_code)
    # Pre-limit responses are 200 (HTML "check your email" page — anti-enum).
    assert statuses[:5] == [200] * 5, f"unexpected pre-limit statuses: {statuses[:5]}"
    assert statuses[5] == 429, f"expected 6th to 429, got {statuses[5]}"


def test_password_setup_request_rate_limited_after_5_requests(app_with_ratelimit, fresh_db):
    """6th /auth/password/setup/request → 429. Same surface as /reset."""
    statuses = []
    for i in range(6):
        resp = app_with_ratelimit.post(
            "/auth/password/setup/request",
            data={"email": f"newcomer-{i}@example.com"},
        )
        statuses.append(resp.status_code)
    assert statuses[:5] == [200] * 5
    assert statuses[5] == 429


def test_reset_confirm_rate_limited_after_10_requests(app_with_ratelimit, fresh_db):
    """11th /auth/password/reset/confirm → 429. Token brute-force throttle:
    the reset token is high-entropy but partial leaks (logs, referer) have
    surfaced before; unbounded guess rate would let an attacker exhaust the
    keyspace adjacent to a leaked prefix."""
    statuses = []
    for i in range(11):
        resp = app_with_ratelimit.post(
            "/auth/password/reset/confirm",
            data={
                "email": "x@example.com",
                "token": f"guess-{i}",
                "password": "newpassword123",
                "confirm_password": "newpassword123",
            },
        )
        statuses.append(resp.status_code)
    # Pre-limit returns the form re-rendered with 'Invalid or expired'
    # error (status 200, anti-enum). Whatever the body says, the throttle
    # must trip on attempt 11.
    assert statuses[10] == 429, f"expected 11th to 429, got {statuses[10]} (full: {statuses})"


def test_password_setup_json_rate_limited_after_10_requests(app_with_ratelimit, fresh_db):
    """11th POST /auth/password/setup (JSON variant) → 429.

    Without this, the form-side ``/setup/confirm`` 10/min limit is
    bypassable — an attacker brute-forcing ``setup_token`` just switches
    to this JSON path and resumes at unbounded RPS. Caught by an
    independent code review on PR #165.
    """
    statuses = []
    for i in range(11):
        resp = app_with_ratelimit.post(
            "/auth/password/setup",
            json={
                "email": "x@example.com",
                "token": f"guess-{i}",
                "password": "newpassword123",
            },
        )
        statuses.append(resp.status_code)
    assert statuses[10] == 429, f"expected 11th to 429, got {statuses[10]} (full: {statuses})"


def test_email_verify_get_rate_limited_after_10_requests(app_with_ratelimit, fresh_db):
    """11th GET /auth/email/verify → 429. Closes the click-through bypass:
    the GET variant is what we embed in outgoing emails, so leaving it
    unthrottled while throttling POST would let an attacker just hit the
    GET endpoint to brute-force tokens at unbounded RPS."""
    statuses = []
    for i in range(11):
        resp = app_with_ratelimit.get(
            f"/auth/email/verify?email=x@example.com&token=guess-{i}",
            follow_redirects=False,
        )
        statuses.append(resp.status_code)
    assert statuses[10] == 429, f"expected 11th to 429, got {statuses[10]} (full: {statuses})"


def test_rate_limit_disabled_via_env(monkeypatch, fresh_db):
    """``AGNES_AUTH_RATELIMIT_ENABLED=0`` (operator escape hatch) must let
    every request through, no matter how many fire in the same window."""
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
    # Email is opt-in-only by default (B6) — name it explicitly, this test is
    # about the rate-limit escape hatch, not the default-offering policy.
    monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "email,password")
    from app.auth.rate_limit import limiter

    limiter.enabled = False  # mirrors what the env-var would do at module load
    limiter.reset()
    from app.main import app

    client = TestClient(app)
    statuses = [
        client.post(
            "/auth/email/send-link",
            json={"email": f"x{i}@example.com"},
        ).status_code
        for i in range(20)
    ]
    assert all(s == 200 for s in statuses), f"unexpected throttling: {statuses}"
