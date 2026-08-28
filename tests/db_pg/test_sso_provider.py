"""Provider tests for the external SSO login flow (design 2026-08-28).

House pattern: no live-IdP E2E — the authlib client is monkeypatched
(``_oauth_client``) and the binding algorithm is exercised as a function
(``bind_external_identity``). PG-backed via ``build_seeded_client`` (the
repos are PG-only, A3 ratchet).
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from fastapi.responses import RedirectResponse

from tests.db_pg._parity_sweep_util import build_seeded_client

TENANT_GUID = "11111111-2222-3333-4444-555555555555"
OTHER_TENANT = "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def sso_pg(tmp_path, monkeypatch, pg_engine):
    """PG-backed client with an enabled SSO config (secret stored)."""
    monkeypatch.delenv("LOCAL_DEV_MODE", raising=False)
    client, admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())

    from src.repositories import sso_config_repo

    repo = sso_config_repo()
    repo.upsert_config(
        tenant_id=TENANT_GUID,
        client_id="app-client-id",
        display_name="Fabrikam",
        allowed_email_domains=["fabrikam.com"],
        enabled=True,
        updated_by="admin1",
    )
    repo.set_client_secret("s3cret")
    return client, admin_token


def _userinfo(**overrides):
    info = {
        "email": "user@fabrikam.com",
        "name": "Fabrikam User",
        "oid": "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB",
        "tid": TENANT_GUID,
    }
    info.update(overrides)
    return info


class _FakeOAuthClient:
    """Stands in for the authlib client: records the authorize kwargs, mints
    a per-flow OAuth state (like the real client), and returns a canned
    token from the callback exchange."""

    def __init__(self, token=None, exchange_error=None):
        self.token = token
        self.exchange_error = exchange_error
        self.authorize_kwargs: dict = {}
        self.states: list[str] = []
        self.authorize_access_token = AsyncMock(side_effect=self._exchange)

    @property
    def last_state(self) -> str:
        return self.states[-1] if self.states else ""

    async def authorize_redirect(self, request, redirect_uri, **kwargs):
        self.authorize_kwargs = {"redirect_uri": redirect_uri, **kwargs}
        state = f"fake-state-{len(self.states) + 1}"
        self.states.append(state)
        return RedirectResponse(f"https://login.microsoftonline.com/fake/authorize?state={state}")

    async def _exchange(self, request):
        if self.exchange_error is not None:
            raise self.exchange_error
        return self.token


def _install_fake_client(monkeypatch, **kwargs) -> _FakeOAuthClient:
    import app.auth.providers.sso as sso

    fake = _FakeOAuthClient(**kwargs)
    monkeypatch.setattr(sso, "_oauth_client", lambda: fake)
    return fake


# ---------------------------------------------------------------------------
# binding algorithm (as a function)
# ---------------------------------------------------------------------------


def _bind(**overrides):
    from app.auth.providers.sso import bind_external_identity

    kwargs = {
        "subject": "oid-1",
        "tenant_id": TENANT_GUID.lower(),
        "email": "user@fabrikam.com",
        "name": "Fabrikam User",
    }
    kwargs.update(overrides)
    return bind_external_identity(**kwargs)


def test_bind_first_login_creates_user_and_links(sso_pg):
    user, error = _bind()
    assert error is None
    assert user["email"] == "user@fabrikam.com"

    from src.repositories import user_external_identities_repo, user_group_members_repo

    row = user_external_identities_repo().get_by_user_id(user["id"])
    assert row is not None
    assert row["subject"] == "oid-1"
    assert row["tenant_id"] == TENANT_GUID.lower()
    assert row["email_at_link"] == "user@fabrikam.com"
    assert row["last_login_at"] is not None
    # JIT user gets Everyone membership only.
    assert user_group_members_repo().list_group_names_for_user(user["id"]) == ["Everyone"]


def test_bind_attaches_to_existing_user_by_email(sso_pg):
    from src.repositories import users_repo

    users_repo().create(id="u-exist", email="user@fabrikam.com", name="Existing")
    user, error = _bind()
    assert error is None
    assert user["id"] == "u-exist"


def test_bind_subject_hit_beats_email(sso_pg):
    """An email change in the customer tenant cannot re-target the login
    onto a different existing Agnes account."""
    from src.repositories import users_repo

    users_repo().create(id="u-victim", email="victim@fabrikam.com", name="Victim")
    user, error = _bind()  # links to a JIT user for user@fabrikam.com
    linked_id = user["id"]

    # Same subject now asserts the VICTIM's email — must stay on linked_id.
    user2, error2 = _bind(email="victim@fabrikam.com")
    assert error2 is None
    assert user2["id"] == linked_id
    from src.repositories import user_external_identities_repo

    assert user_external_identities_repo().get_by_user_id("u-victim") is None


def test_bind_subject_hit_on_deactivated_account_refuses_without_touch(sso_pg, pg_engine):
    import sqlalchemy as sa

    user, _ = _bind()
    from src.repositories import user_external_identities_repo

    before = user_external_identities_repo().get_by_user_id(user["id"])["last_login_at"]
    with pg_engine.begin() as conn:
        conn.execute(sa.text("UPDATE users SET active = FALSE WHERE id = :id"), {"id": user["id"]})

    user2, error = _bind()
    assert user2 is None
    assert error == "deactivated"
    after = user_external_identities_repo().get_by_user_id(user["id"])["last_login_at"]
    assert after == before


def test_bind_deactivated_on_email_attach_path(sso_pg, pg_engine):
    import sqlalchemy as sa

    from src.repositories import users_repo

    users_repo().create(id="u-off", email="user@fabrikam.com", name="Off")
    with pg_engine.begin() as conn:
        conn.execute(sa.text("UPDATE users SET active = FALSE WHERE id = 'u-off'"))
    user, error = _bind()
    assert user is None
    assert error == "deactivated"


def test_bind_same_tenant_conflicting_subject_refuses_with_diagnostics(sso_pg, caplog):
    """Email recycling at the customer: the successor must not inherit the
    predecessor's Agnes account. WARNING carries the user id and both
    subject GUIDs, no raw emails."""
    user, _ = _bind(subject="oid-old")
    with caplog.at_level(logging.WARNING):
        user2, error = _bind(subject="oid-new")
    assert user2 is None
    assert error == "sso_identity_conflict"
    warning = next(r for r in caplog.records if "conflict" in r.getMessage())
    msg = warning.getMessage()
    assert user["id"] in msg
    assert "oid-old" in msg
    assert "oid-new" in msg
    assert "fabrikam.com" not in msg  # no raw emails in the diagnostics


def test_bind_replaces_stale_binding_from_repointed_tenant(sso_pg, caplog):
    """After the admin re-points sso_config at a different tenant, the old
    row is unreachable by the lookup and must self-heal on the next login."""
    user, _ = _bind(subject="oid-old", tenant_id=OTHER_TENANT)  # old-tenant link
    with caplog.at_level(logging.WARNING):
        user2, error = _bind(subject="oid-current")  # active-tenant login
    assert error is None
    assert user2["id"] == user["id"]

    from src.repositories import user_external_identities_repo

    row = user_external_identities_repo().get_by_user_id(user["id"])
    assert row["tenant_id"] == TENANT_GUID.lower()
    assert row["subject"] == "oid-current"
    assert any("replac" in r.getMessage() for r in caplog.records)


def test_bind_race_on_concurrent_first_login_matching_winner(sso_pg, monkeypatch):
    """Unique violation from a concurrent first login: re-read by subject and
    proceed when the winner is the same user."""
    import app.auth.providers.sso as sso

    user, _ = _bind()  # the "winner" row exists

    real_repo = sso.user_external_identities_repo()
    misses = {"by_subject": 1, "by_user": 1}

    class RacingRepo:
        def get_by_subject(self, *a):
            if misses["by_subject"]:
                misses["by_subject"] -= 1
                return None
            return real_repo.get_by_subject(*a)

        def get_by_user_id(self, *a):
            if misses["by_user"]:
                misses["by_user"] -= 1
                return None
            return real_repo.get_by_user_id(*a)

        def __getattr__(self, name):
            return getattr(real_repo, name)

    monkeypatch.setattr(sso, "user_external_identities_repo", lambda: RacingRepo())
    user2, error = _bind()
    assert error is None
    assert user2["id"] == user["id"]


def test_bind_race_with_foreign_winner_refuses(sso_pg, monkeypatch):
    import app.auth.providers.sso as sso

    _bind(email="other@fabrikam.com")  # subject oid-1 belongs to another mailbox's user

    real_repo = sso.user_external_identities_repo()
    misses = {"by_subject": 1}

    class RacingRepo:
        def get_by_subject(self, *a):
            if misses["by_subject"]:
                misses["by_subject"] -= 1
                return None
            return real_repo.get_by_subject(*a)

        def __getattr__(self, name):
            return getattr(real_repo, name)

    monkeypatch.setattr(sso, "user_external_identities_repo", lambda: RacingRepo())
    user, error = _bind(email="user@fabrikam.com")
    assert user is None
    assert error == "sso_identity_conflict"


def test_bind_email_drift_logged_not_rewritten(sso_pg, caplog, pg_engine):
    import sqlalchemy as sa

    user, _ = _bind()
    with pg_engine.begin() as conn:
        conn.execute(sa.text("UPDATE users SET email = 'renamed@fabrikam.com' WHERE id = :id"), {"id": user["id"]})
    with caplog.at_level(logging.INFO):
        user2, error = _bind()  # token still asserts user@fabrikam.com
    assert error is None
    assert user2["id"] == user["id"]
    assert any("drift" in r.getMessage() for r in caplog.records)
    from src.repositories import users_repo

    assert users_repo().get_by_id(user["id"])["email"] == "renamed@fabrikam.com"


# ---------------------------------------------------------------------------
# login route (authlib client faked)
# ---------------------------------------------------------------------------


def test_login_redirects_with_select_account_prompt(sso_pg, monkeypatch):
    client, _ = sso_pg
    fake = _install_fake_client(monkeypatch)
    r = client.get("/auth/sso/login", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert fake.authorize_kwargs["prompt"] == "select_account"
    assert fake.authorize_kwargs["redirect_uri"].endswith("/auth/sso/callback")


def test_login_404_when_disabled(sso_pg, monkeypatch):
    client, _ = sso_pg
    from src.repositories import sso_config_repo

    sso_config_repo().set_enabled(False, updated_by="admin1")
    _install_fake_client(monkeypatch)
    assert client.get("/auth/sso/login", follow_redirects=False).status_code == 404


def test_login_404_when_not_in_allowlist(sso_pg, monkeypatch):
    client, _ = sso_pg
    monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "password")
    _install_fake_client(monkeypatch)
    assert client.get("/auth/sso/login", follow_redirects=False).status_code == 404
    assert client.get("/auth/sso/callback", follow_redirects=False).status_code == 404


# ---------------------------------------------------------------------------
# callback (code exchange faked)
# ---------------------------------------------------------------------------


def _run_flow(client, monkeypatch, *, userinfo, next_path=None, exchange_error=None):
    fake = _install_fake_client(
        monkeypatch,
        token={"userinfo": userinfo} if userinfo is not None else {},
        exchange_error=exchange_error,
    )
    url = "/auth/sso/login" + (f"?next={next_path}" if next_path else "")
    r1 = client.get(url, follow_redirects=False)
    assert r1.status_code in (302, 307)
    r2 = client.get(f"/auth/sso/callback?code=x&state={fake.last_state}", follow_redirects=False)
    return fake, r2


def test_callback_happy_path_sets_cookie_and_links(sso_pg, monkeypatch, pg_engine):
    import sqlalchemy as sa

    client, _ = sso_pg
    _, r = _run_flow(client, monkeypatch, userinfo=_userinfo(), next_path="/catalog")
    assert r.status_code == 302
    assert r.headers["location"] == "/catalog"
    assert "access_token" in r.headers.get("set-cookie", "")

    from src.repositories import user_external_identities_repo, users_repo

    user = users_repo().get_by_email("user@fabrikam.com")
    row = user_external_identities_repo().get_by_user_id(user["id"])
    assert row["subject"] == _userinfo()["oid"].lower()
    assert row["tenant_id"] == TENANT_GUID.lower()
    with pg_engine.connect() as conn:
        actions = [x[0] for x in conn.execute(sa.text("SELECT action FROM audit_log")).fetchall()]
    assert "sso.identity.linked" in actions
    # The completed sign-in itself is recorded too (tests/test_audit_login.py
    # guards every cookie-minting provider).
    assert "login_success" in actions


def test_callback_error_paths(sso_pg, monkeypatch):
    client, _ = sso_pg
    cases = [
        (_userinfo(email="", preferred_username=""), "sso_no_email"),
        (_userinfo(oid=""), "sso_no_subject"),
        (_userinfo(tid=OTHER_TENANT), "sso_wrong_tenant"),
        (_userinfo(email="user@evil.example"), "domain_not_allowed"),
    ]
    for userinfo, expected in cases:
        _, r = _run_flow(client, monkeypatch, userinfo=userinfo)
        assert r.status_code in (302, 307), expected
        assert r.headers["location"] == f"/login?error={expected}"
        assert "access_token" not in r.headers.get("set-cookie", "")


def test_callback_oauth_error_redirects_and_logs_repr(sso_pg, monkeypatch, caplog):
    client, _ = sso_pg
    with caplog.at_level(logging.ERROR):
        _, r = _run_flow(
            client,
            monkeypatch,
            userinfo=None,
            exchange_error=RuntimeError("bad\r\nstate"),
        )
    assert r.headers["location"] == "/login?error=sso_oauth_failed"
    joined = " ".join(rec.getMessage() for rec in caplog.records)
    assert "\\r\\n" in joined  # %r escapes attacker-controlled newlines


def test_callback_deactivated_user(sso_pg, monkeypatch, pg_engine):
    import sqlalchemy as sa

    client, _ = sso_pg
    from src.repositories import users_repo

    users_repo().create(id="u-off", email="user@fabrikam.com", name="Off")
    with pg_engine.begin() as conn:
        conn.execute(sa.text("UPDATE users SET active = FALSE WHERE id = 'u-off'"))
    _, r = _run_flow(client, monkeypatch, userinfo=_userinfo())
    assert r.headers["location"] == "/login?error=deactivated"


# ---------------------------------------------------------------------------
# admin test sign-in (?mode=test)
# ---------------------------------------------------------------------------


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_test_mode_requires_admin(sso_pg, monkeypatch):
    client, _ = sso_pg
    _install_fake_client(monkeypatch)
    from app.auth.jwt import create_access_token

    r = client.get("/auth/sso/login?mode=test", follow_redirects=False)
    assert r.status_code == 403
    analyst = create_access_token("analyst1", "analyst@test.com")
    r = client.get("/auth/sso/login?mode=test", headers=_h(analyst), follow_redirects=False)
    assert r.status_code == 403


def test_test_mode_works_pre_enable_and_outside_allowlist_without_side_effects(sso_pg, monkeypatch):
    """The whole point of inline gating: reachable with enabled=false AND
    with an explicit allowlist not naming sso — and side-effect-free."""
    client, admin_token = sso_pg
    from src.repositories import sso_config_repo, user_external_identities_repo, users_repo

    sso_config_repo().set_enabled(False, updated_by="admin1")
    monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "password")

    fake = _install_fake_client(monkeypatch, token={"userinfo": _userinfo()})
    client.cookies.set("access_token", admin_token)
    r1 = client.get("/auth/sso/login?mode=test", follow_redirects=False)
    assert r1.status_code in (302, 307)
    assert fake.authorize_kwargs["prompt"] == "select_account"

    r2 = client.get(f"/auth/sso/callback?code=x&state={fake.last_state}", follow_redirects=False)
    assert r2.status_code == 200
    page = r2.text
    assert _userinfo()["oid"].lower() in page.lower()
    assert TENANT_GUID.lower() in page.lower()
    assert "user@fabrikam.com" in page

    # Side-effect-free: no user row, no identity row, no session cookie.
    assert users_repo().get_by_email("user@fabrikam.com") is None
    assert user_external_identities_repo().count() == 0
    assert "access_token" not in r2.headers.get("set-cookie", "")


def test_test_mode_reports_domain_verdict(sso_pg, monkeypatch):
    client, admin_token = sso_pg
    fake = _install_fake_client(monkeypatch, token={"userinfo": _userinfo(email="user@evil.example")})
    client.cookies.set("access_token", admin_token)
    client.get("/auth/sso/login?mode=test", follow_redirects=False)
    r = client.get(f"/auth/sso/callback?code=x&state={fake.last_state}", follow_redirects=False)
    assert r.status_code == 200
    assert "not permitted" in r.text.lower()
    assert fake.authorize_access_token.await_count == 1


def test_test_mode_callback_reverifies_admin_session(sso_pg, monkeypatch):
    """A stashed test marker must not complete for a session that is no
    longer (or never was) an admin."""
    client, admin_token = sso_pg
    fake = _install_fake_client(monkeypatch, token={"userinfo": _userinfo()})
    client.cookies.set("access_token", admin_token)
    client.get("/auth/sso/login?mode=test", follow_redirects=False)

    client.cookies.delete("access_token")
    r = client.get(f"/auth/sso/callback?code=x&state={fake.last_state}", follow_redirects=False)
    assert r.status_code == 403

    from src.repositories import users_repo

    assert users_repo().get_by_email("user@fabrikam.com") is None


def test_test_mode_requires_configured(sso_pg, monkeypatch):
    client, admin_token = sso_pg
    from src.repositories import sso_config_repo

    sso_config_repo().clear_client_secret()
    _install_fake_client(monkeypatch)
    client.cookies.set("access_token", admin_token)
    r = client.get("/auth/sso/login?mode=test", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/login?error=sso_not_configured"


# ---------------------------------------------------------------------------
# login page button + error copy
# ---------------------------------------------------------------------------


def test_login_page_offers_sso_button_with_display_name(sso_pg):
    client, _ = sso_pg
    page = client.get("/login").text
    assert "Sign in with Fabrikam" in page
    assert "/auth/sso/login" in page


def test_login_page_hides_button_when_disabled(sso_pg):
    client, _ = sso_pg
    from src.repositories import sso_config_repo

    sso_config_repo().set_enabled(False, updated_by="admin1")
    page = client.get("/login").text
    assert "Sign in with Fabrikam" not in page


def test_login_page_escapes_display_name(sso_pg):
    client, _ = sso_pg
    from src.repositories import sso_config_repo

    sso_config_repo().upsert_config(
        tenant_id=TENANT_GUID,
        client_id="app-client-id",
        display_name="<script>alert(1)</script>",
        allowed_email_domains=["fabrikam.com"],
        enabled=True,
        updated_by="admin1",
    )
    page = client.get("/login").text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_login_page_renders_sso_error_copy(sso_pg):
    client, _ = sso_pg
    for code in (
        "sso_not_configured",
        "sso_oauth_failed",
        "sso_no_email",
        "sso_no_subject",
        "sso_wrong_tenant",
        "sso_identity_conflict",
    ):
        page = client.get(f"/login?error={code}").text
        assert 'role="alert"' in page, code


def test_concurrent_normal_flow_does_not_hijack_test_mode(sso_pg, monkeypatch):
    """The test marker is bound to ITS flow's OAuth state: an admin starting
    a normal sign-in in a second tab must neither consume the marker (which
    would turn the test callback into a REAL sign-in) nor inherit it."""
    client, admin_token = sso_pg
    from src.repositories import user_external_identities_repo, users_repo

    fake = _install_fake_client(monkeypatch, token={"userinfo": _userinfo()})
    client.cookies.set("access_token", admin_token)

    # Tab A: admin test flow.
    client.get("/auth/sso/login?mode=test", follow_redirects=False)
    test_state = fake.last_state
    # Tab B: a normal flow in the same browser session.
    client.get("/auth/sso/login", follow_redirects=False)
    normal_state = fake.last_state
    assert normal_state != test_state

    # Tab A's callback still runs as a TEST: result page, no side effects.
    r = client.get(f"/auth/sso/callback?code=x&state={test_state}", follow_redirects=False)
    assert r.status_code == 200
    assert users_repo().get_by_email("user@fabrikam.com") is None
    assert user_external_identities_repo().count() == 0
    assert "access_token" not in r.headers.get("set-cookie", "")

    # Tab B's callback still runs as a NORMAL sign-in.
    r = client.get(f"/auth/sso/callback?code=x&state={normal_state}", follow_redirects=False)
    assert r.status_code == 302
    assert "access_token" in r.headers.get("set-cookie", "")
    assert users_repo().get_by_email("user@fabrikam.com") is not None


def test_test_mode_predicts_deactivated_refusal(sso_pg, monkeypatch, pg_engine):
    """The test page must apply the real binding checks read-only: a
    deactivated account renders as a refusal, not 'every check passed'."""
    import sqlalchemy as sa

    client, admin_token = sso_pg
    from src.repositories import users_repo

    users_repo().create(id="u-off", email="user@fabrikam.com", name="Off")
    with pg_engine.begin() as conn:
        conn.execute(sa.text("UPDATE users SET active = FALSE WHERE id = 'u-off'"))

    fake = _install_fake_client(monkeypatch, token={"userinfo": _userinfo()})
    client.cookies.set("access_token", admin_token)
    client.get("/auth/sso/login?mode=test", follow_redirects=False)
    r = client.get(f"/auth/sso/callback?code=x&state={fake.last_state}", follow_redirects=False)
    assert r.status_code == 200
    assert "deactivated" in r.text
    assert "every check passed" not in r.text


def test_test_mode_predicts_identity_conflict(sso_pg, monkeypatch):
    client, admin_token = sso_pg
    _bind(subject="oid-old")  # binds user@fabrikam.com's account to oid-old

    fake = _install_fake_client(monkeypatch, token={"userinfo": _userinfo(oid="oid-new")})
    client.cookies.set("access_token", admin_token)
    client.get("/auth/sso/login?mode=test", follow_redirects=False)
    r = client.get(f"/auth/sso/callback?code=x&state={fake.last_state}", follow_redirects=False)
    assert r.status_code == 200
    assert "sso_identity_conflict" in r.text
    assert "every check passed" not in r.text


def test_test_mode_predicts_stale_binding_replacement(sso_pg, monkeypatch):
    """A user holding a stale binding from a re-pointed tenant: the real
    callback REPLACES it — the test page must say so, not claim a plain
    first-time attach."""
    client, admin_token = sso_pg
    _bind(subject="oid-old", tenant_id=OTHER_TENANT)  # stale, other tenant

    fake = _install_fake_client(monkeypatch, token={"userinfo": _userinfo()})
    client.cookies.set("access_token", admin_token)
    client.get("/auth/sso/login?mode=test", follow_redirects=False)
    r = client.get(f"/auth/sso/callback?code=x&state={fake.last_state}", follow_redirects=False)
    assert r.status_code == 200
    assert "replacing its stale binding" in r.text
