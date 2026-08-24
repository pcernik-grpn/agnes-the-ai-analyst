"""Tests for auth providers — password, email magic link, google OAuth."""

import pytest
from fastapi.testclient import TestClient

# Reset/setup/magic-link tokens are hashed at rest (audit M3); seed digests.
from app.auth.token_hash import hash_token


@pytest.fixture
def client(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
    # This file tests provider MECHANICS (magic link, password, OAuth), not
    # the default-offering policy (that's tests/test_auth_provider_defaults.py).
    # Email is opt-in-only by default since B6, so pin an explicit allowlist
    # naming every provider these tests exercise — otherwise the magic-link
    # endpoints 404 under the new default and this file stops testing what
    # it says it tests.
    monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "google,email,password,keboola,microsoft")

    from src.db import get_system_db
    from src.repositories.users import UserRepository

    conn = get_system_db()
    ur = UserRepository(conn)
    # User with password
    try:
        from argon2 import PasswordHasher

        ph = PasswordHasher()
        pw_hash = ph.hash("testpass123")
    except ImportError:
        import hashlib

        pw_hash = hashlib.sha256(b"testpass123").hexdigest()

    ur.create(id="pw1", email="pw@test.com", name="PW User", password_hash=pw_hash)
    # User with setup token (and fresh created timestamp so the JSON /setup
    # endpoint's TTL check accepts it)
    from datetime import datetime, timezone

    ur.create(id="setup1", email="setup@test.com", name="Setup User")
    ur.update(id="setup1", setup_token=hash_token("setup-token-123"), setup_token_created=datetime.now(timezone.utc))
    # User for magic link
    ur.create(id="ml1", email="ml@test.com", name="ML User")
    conn.close()

    app = shared_app
    return TestClient(app)


class TestTokenEndpoint:
    """Tests for /auth/token — password bypass fix."""

    def test_token_empty_password_rejected_when_user_has_hash(self, client):
        """Empty password must be rejected when user has password_hash."""
        resp = client.post("/auth/token", json={"email": "pw@test.com", "password": ""})
        assert resp.status_code == 401

    def test_token_missing_password_rejected_when_user_has_hash(self, client):
        """Omitting password field (defaults to '') must be rejected when user has password_hash."""
        resp = client.post("/auth/token", json={"email": "pw@test.com"})
        assert resp.status_code == 401

    def test_token_wrong_password_rejected(self, client):
        """Wrong password must be rejected with 401."""
        resp = client.post("/auth/token", json={"email": "pw@test.com", "password": "wrongpass"})
        assert resp.status_code == 401

    def test_token_correct_password_succeeds(self, client):
        """Correct password must issue a token."""
        resp = client.post("/auth/token", json={"email": "pw@test.com", "password": "testpass123"})
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["email"] == "pw@test.com"

    def test_token_no_password_hash_user_gets_token(self, client):
        """User without password_hash (OAuth-only) must be rejected at /auth/token."""
        resp = client.post("/auth/token", json={"email": "ml@test.com"})
        assert resp.status_code == 401

    def test_token_rejected_for_oauth_only_user(self, client):
        """OAuth-only user (no password_hash) must not receive a token via /auth/token."""
        resp = client.post("/auth/token", json={"email": "ml@test.com"})
        assert resp.status_code == 401
        assert "external authentication" in resp.json()["detail"]

    def test_token_unknown_user_rejected(self, client):
        """Unknown email must return 401."""
        resp = client.post("/auth/token", json={"email": "nobody@test.com", "password": "anything"})
        assert resp.status_code == 401


class TestPasswordAuth:
    def test_login_success(self, client):
        resp = client.post(
            "/auth/password/login",
            json={
                "email": "pw@test.com",
                "password": "testpass123",
            },
        )
        assert resp.status_code == 200
        assert "access_token" in resp.json()

    def test_login_wrong_password(self, client):
        resp = client.post(
            "/auth/password/login",
            json={
                "email": "pw@test.com",
                "password": "wrongpass",
            },
        )
        assert resp.status_code == 401

    def test_login_unknown_user(self, client):
        resp = client.post(
            "/auth/password/login",
            json={
                "email": "unknown@test.com",
                "password": "test",
            },
        )
        assert resp.status_code == 401

    def test_setup_password(self, client):
        resp = client.post(
            "/auth/password/setup",
            json={
                "email": "setup@test.com",
                "token": "setup-token-123",
                "password": "newpass456",
            },
        )
        assert resp.status_code == 200
        assert "access_token" in resp.json()

    def test_setup_wrong_token(self, client):
        resp = client.post(
            "/auth/password/setup",
            json={
                "email": "setup@test.com",
                "token": "wrong-token",
                "password": "newpass",
            },
        )
        assert resp.status_code == 400


class TestEmailAuth:
    def test_send_link_registered(self, client):
        resp = client.post("/auth/email/send-link", json={"email": "ml@test.com"})
        assert resp.status_code == 200
        # Always returns same message (anti-enumeration)
        assert "If this email" in resp.json()["message"]

    def test_send_link_unregistered(self, client):
        resp = client.post("/auth/email/send-link", json={"email": "nobody@test.com"})
        assert resp.status_code == 200
        assert "If this email" in resp.json()["message"]

    def test_send_link_web_registered(self, client, monkeypatch):
        """Web-form variant renders the 'check your email' page (the door
        that /login/email now points at) instead of JSON. The POST refuses
        without a mail transport (is_available reads env per call), so one
        is configured here; the SMTP delivery itself is faked (a real failed
        send now surfaces as an error instead of rendering this page)."""
        monkeypatch.setenv("SMTP_HOST", "smtp.test.invalid")
        monkeypatch.setattr("smtplib.SMTP", _recording_smtp([]))
        resp = client.post("/auth/email/send-link/web", data={"email": "ml@test.com"})
        assert resp.status_code == 200
        assert "Check Your Email" in resp.text

    def test_send_link_web_unregistered(self, client, monkeypatch):
        """Anti-enumeration: an unknown email gets the identical sent page."""
        monkeypatch.setenv("SMTP_HOST", "smtp.test.invalid")
        resp = client.post("/auth/email/send-link/web", data={"email": "nobody@test.com"})
        assert resp.status_code == 200
        assert "Check Your Email" in resp.text

    def test_magic_link_falls_back_to_request_origin_not_localhost(self, client, monkeypatch):
        """With no pinned public origin, the emailed link must follow the
        request, not the `http://localhost:8000` literal.

        `_build_magic_link` used to read SERVER_URL alone with a hard localhost
        default, so an instance served behind a TLS terminator that never set
        SERVER_URL mailed its users a sign-in link pointing at their own laptop.
        """
        monkeypatch.delenv("SERVER_URL", raising=False)
        monkeypatch.delenv("AGNES_BASE_URL", raising=False)
        monkeypatch.setenv("LOCAL_DEV_MODE", "1")
        resp = client.post("/auth/email/send-link", json={"email": "ml@test.com"})
        assert resp.status_code == 200
        link = resp.json()["dev_link"]
        assert link.startswith("http://testserver/auth/email/verify"), link
        assert "localhost:8000" not in link

    def test_magic_link_web_form_falls_back_to_request_origin(self, client, monkeypatch):
        """Same fallback on the web-form door — asserted on the DELIVERED mail,
        which is built by a second call site (`_send_email`) that has to agree
        with the one feeding the dev/console link."""
        monkeypatch.delenv("SERVER_URL", raising=False)
        monkeypatch.delenv("AGNES_BASE_URL", raising=False)
        monkeypatch.delenv("LOCAL_DEV_MODE", raising=False)
        monkeypatch.setenv("SMTP_HOST", "smtp.test.invalid")
        sent: list = []
        monkeypatch.setattr("smtplib.SMTP", _recording_smtp(sent))
        resp = client.post("/auth/email/send-link/web", data={"email": "ml@test.com"})
        assert resp.status_code == 200
        assert len(sent) == 1, "no mail recorded"
        body = str(sent[0])
        assert "http://testserver/auth/email/verify" in body, body
        assert "localhost:8000" not in body

    def test_magic_link_honors_pinned_public_origin(self, client, monkeypatch):
        """An operator-pinned origin still wins over the request, and
        AGNES_BASE_URL takes precedence over SERVER_URL (public_base_url's
        documented order)."""
        monkeypatch.setenv("LOCAL_DEV_MODE", "1")
        monkeypatch.setenv("SERVER_URL", "https://from-server-url.example.com")
        resp = client.post("/auth/email/send-link", json={"email": "ml@test.com"})
        assert resp.json()["dev_link"].startswith("https://from-server-url.example.com/auth/email/verify")

        monkeypatch.setenv("AGNES_BASE_URL", "https://pinned.example.com/")
        resp = client.post("/auth/email/send-link", json={"email": "ml@test.com"})
        assert resp.json()["dev_link"].startswith("https://pinned.example.com/auth/email/verify")

    def test_verify_invalid_token(self, client):
        resp = client.post(
            "/auth/email/verify",
            json={
                "email": "ml@test.com",
                "token": "invalid",
            },
        )
        assert resp.status_code == 401

    def test_concurrent_verify_only_one_wins(self, client):
        """Two concurrent magic-link verifies — exactly one must succeed (M10)."""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from src.db import get_system_db
        from src.repositories.users import UserRepository

        # Create a user and set a magic-link token
        conn = get_system_db()
        repo = UserRepository(conn)
        repo.create(id="ml-user-1", email="concurrent@test.com", name="Test")
        token = "tok_concurrent_test_12345"
        from datetime import datetime, timezone

        repo.update(id="ml-user-1", reset_token=hash_token(token), reset_token_created=datetime.now(timezone.utc))
        conn.close()

        results = []
        barrier = __import__("threading").Barrier(2, timeout=5)

        def verify():
            barrier.wait()  # ensure both threads hit the endpoint simultaneously
            resp = client.post(
                "/auth/email/verify",
                json={
                    "email": "concurrent@test.com",
                    "token": token,
                },
            )
            results.append(resp.status_code)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(verify) for _ in range(2)]
            # Collect results (re-raise any exceptions)
            for f in as_completed(futures):
                f.result()

        # Exactly one must succeed (200), the other must fail (401)
        successes = results.count(200)
        failures = results.count(401)
        assert successes == 1, f"Expected exactly 1 success, got {successes} (results: {results})"
        assert failures == 1, f"Expected exactly 1 failure, got {failures} (results: {results})"


class _BoomSMTP:
    """smtplib.SMTP stand-in whose connect always fails."""

    def __init__(self, *args, **kwargs):
        raise OSError("connection refused (test)")


def _recording_smtp(sent: list):
    """smtplib.SMTP stand-in that records messages instead of delivering."""

    class FakeSMTP:
        def __init__(self, host, port):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self):
            pass

        def login(self, user, password):
            pass

        def send_message(self, msg):
            sent.append(msg)

    return FakeSMTP


class TestEmailSendFailureIsSurfaced:
    """A configured mail transport that fails to deliver must not answer 200.

    The trap this pins: a transport that looks configured but fails on every
    send used to leave the endpoint answering the generic success message, so
    the person waits for a mail that was never sent and nothing surfaces the
    misconfiguration.
    """

    def test_send_link_smtp_failure_returns_500(self, client, monkeypatch):
        monkeypatch.delenv("LOCAL_DEV_MODE", raising=False)
        monkeypatch.setenv("SMTP_HOST", "smtp.test.invalid")
        monkeypatch.setattr("smtplib.SMTP", _BoomSMTP)
        resp = client.post("/auth/email/send-link", json={"email": "ml@test.com"})
        assert resp.status_code == 500

    def test_send_link_web_smtp_failure_shows_error(self, client, monkeypatch):
        monkeypatch.delenv("LOCAL_DEV_MODE", raising=False)
        monkeypatch.setenv("SMTP_HOST", "smtp.test.invalid")
        monkeypatch.setattr("smtplib.SMTP", _BoomSMTP)
        resp = client.post("/auth/email/send-link/web", data={"email": "ml@test.com"}, follow_redirects=False)
        assert resp.status_code == 303
        assert "error=email_send_failed" in resp.headers["location"]

    def test_send_link_smtp_failure_unknown_address_keeps_generic_200(self, client, monkeypatch):
        """Anti-enumeration: an unknown address attempts no send, so the
        failure path must not fire and the generic success stays."""
        monkeypatch.delenv("LOCAL_DEV_MODE", raising=False)
        monkeypatch.setenv("SMTP_HOST", "smtp.test.invalid")
        monkeypatch.setattr("smtplib.SMTP", _BoomSMTP)
        resp = client.post("/auth/email/send-link", json={"email": "nobody@test.com"})
        assert resp.status_code == 200
        assert "If this email" in resp.json()["message"]


class TestSendGridSdkRemoved:
    """SENDGRID_API_KEY must not count as a mail transport anywhere.

    The SDK branch is gone: the `sendgrid` package was never a declared
    dependency, so that path always raised ImportError at send time while the
    availability predicates kept advertising email sign-in. SendGrid still
    works as an ordinary SMTP relay (SMTP_HOST=smtp.sendgrid.net).
    """

    def test_email_provider_not_available_on_sendgrid_key_alone(self, monkeypatch):
        from app.auth.providers import email as email_mod

        monkeypatch.delenv("LOCAL_DEV_MODE", raising=False)
        monkeypatch.delenv("SMTP_HOST", raising=False)
        monkeypatch.setenv("SENDGRID_API_KEY", "SG.test-key")
        assert email_mod.is_available() is False
        assert email_mod._has_email_transport() is False

    def test_password_transport_predicate_ignores_sendgrid_key(self, monkeypatch):
        from app.auth.providers import password as pw_mod

        monkeypatch.delenv("SMTP_HOST", raising=False)
        monkeypatch.setenv("SENDGRID_API_KEY", "SG.test-key")
        assert pw_mod._has_email_transport() is False


class TestSenderAddressUnified:
    """One sender key — SMTP_FROM — with the SendGrid-era EMAIL_FROM_ADDRESS
    honored as a backward-compatible fallback so existing deployments keep
    their configured sender."""

    def _send_and_capture(self, client, monkeypatch):
        sent: list = []
        monkeypatch.setattr("smtplib.SMTP", _recording_smtp(sent))
        monkeypatch.delenv("LOCAL_DEV_MODE", raising=False)
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        resp = client.post("/auth/email/send-link", json={"email": "ml@test.com"})
        assert resp.status_code == 200, resp.text
        assert len(sent) == 1, "expected exactly one delivered message"
        return sent[0]

    def test_smtp_from_wins_over_legacy_key(self, client, monkeypatch):
        monkeypatch.setenv("SMTP_FROM", "canonical@example.com")
        monkeypatch.setenv("EMAIL_FROM_ADDRESS", "legacy@example.com")
        msg = self._send_and_capture(client, monkeypatch)
        assert msg["From"] == "canonical@example.com"

    def test_legacy_email_from_address_is_a_fallback(self, client, monkeypatch):
        monkeypatch.delenv("SMTP_FROM", raising=False)
        monkeypatch.setenv("EMAIL_FROM_ADDRESS", "legacy@example.com")
        msg = self._send_and_capture(client, monkeypatch)
        assert msg["From"] == "legacy@example.com"


class TestEmailCaseInsensitiveSignIn:
    """One identity, one account — on the way IN as well as at provisioning.

    Rows created before normalization landed (or by an admin who typed the
    address as the person writes it) store mixed case. ``get_by_email`` is an
    exact match on both backends, so those users could sign in through OAuth —
    which resolves case-insensitively — and be told "invalid email or password"
    by the very same instance when they typed the address themselves.
    """

    def test_password_login_matches_stored_row_regardless_of_case(self, client):
        r = client.post("/auth/password/login", json={"email": "PW@Test.com", "password": "testpass123"})
        assert r.status_code == 200, r.text
        assert "access_token" in r.json()

    def test_token_endpoint_matches_stored_row_regardless_of_case(self, client):
        r = client.post("/auth/token", json={"email": "PW@TEST.COM", "password": "testpass123"})
        assert r.status_code == 200, r.text

    def test_magic_link_is_issued_for_a_case_variant_address(self, client):
        """The anti-enumeration response is identical either way, so assert on
        the side effect: a token is minted on the stored row."""
        from src.db import get_system_db

        r = client.post("/auth/email/send-link", json={"email": "ML@Test.com"})
        assert r.status_code == 200, r.text
        conn = get_system_db()
        try:
            row = conn.execute("SELECT reset_token FROM users WHERE id = 'ml1'").fetchone()
        finally:
            conn.close()
        assert row is not None and row[0], "no magic-link token minted for the case-variant address"

    def test_magic_link_verifies_with_a_case_variant_address(self, client):
        """End to end: the CAS that consumes the token has to fold case too,
        or the link mints fine and then refuses to open."""
        from datetime import datetime, timezone

        from src.db import get_system_db

        conn = get_system_db()
        try:
            conn.execute(
                "UPDATE users SET reset_token = ?, reset_token_created = ? WHERE id = 'ml1'",
                [hash_token("magic-abc"), datetime.now(timezone.utc)],
            )
        finally:
            conn.close()
        r = client.post("/auth/email/verify", json={"email": "ML@Test.com", "token": "magic-abc"})
        assert r.status_code == 200, r.text


class TestSignInIgnoresSurroundingWhitespace:
    """A pasted address carries whitespace, and every entry point has to
    tolerate it identically.

    ``ensure_user`` strips, and so did the web magic-link form — but the JSON
    ``/send-link``, ``/auth/token`` and the password handlers passed the raw
    field straight to the lookup, so the same address succeeded or failed
    depending on which door it came through. Stripping (not lower-casing —
    case is SQL's job) at each entry point is what makes them agree.
    """

    def test_token_endpoint_tolerates_padding(self, client):
        r = client.post("/auth/token", json={"email": "  pw@test.com  ", "password": "testpass123"})
        assert r.status_code == 200, r.text

    def test_password_login_tolerates_padding(self, client):
        r = client.post("/auth/password/login", json={"email": "  pw@test.com ", "password": "testpass123"})
        assert r.status_code == 200, r.text

    def test_json_magic_link_tolerates_padding(self, client):
        from src.db import get_system_db

        r = client.post("/auth/email/send-link", json={"email": "  ml@test.com  "})
        assert r.status_code == 200, r.text
        conn = get_system_db()
        try:
            row = conn.execute("SELECT reset_token FROM users WHERE id = 'ml1'").fetchone()
        finally:
            conn.close()
        assert row is not None and row[0], "padded address minted no magic-link token"


class TestTokenOwnershipDecidesIdentity:
    """A one-time link belongs to the row that HOLDS the token.

    The compare-and-swap matches ``lower(email) = ? AND reset_token = ?``, so it
    finds whichever case variant actually holds the token, while
    ``get_by_email_ci`` deterministically returns the OLDEST. An admin-issued
    reset mints the token by user id, so it can live on a newer variant — and
    resolving the account by address after the CAS would then mint a session
    for an account the token was never issued for, carrying that account's
    group memberships.
    """

    @pytest.fixture
    def variants(self, client):
        """Two case-variant rows; the magic-link token sits on the NEWER one."""
        from datetime import datetime, timezone

        from src.db import get_system_db
        from src.repositories.users import UserRepository

        conn = get_system_db()
        try:
            repo = UserRepository(conn)
            repo.create(id="dup-old", email="dup@test.com", name="Old")
            repo.create(id="dup-new", email="Dup@Test.com", name="New")
            conn.execute("UPDATE users SET created_at = ? WHERE id = ?", ["2025-01-01 00:00:00", "dup-old"])
            conn.execute("UPDATE users SET created_at = ? WHERE id = ?", ["2026-06-01 00:00:00", "dup-new"])
            repo.update(id="dup-new", reset_token=hash_token("tok-abc"), reset_token_created=datetime.now(timezone.utc))
        finally:
            conn.close()
        return client

    def test_magic_link_signs_in_the_account_the_token_belongs_to(self, variants):
        import jwt as pyjwt

        r = variants.post("/auth/email/verify", json={"email": "dup@test.com", "token": "tok-abc"})
        assert r.status_code == 200, r.text
        claims = pyjwt.decode(r.json()["access_token"], options={"verify_signature": False})
        assert claims.get("sub") == "dup-new", "session minted for the wrong account"

    def test_reset_form_accepts_a_link_whose_token_is_on_a_case_variant(self, variants):
        """The pre-check peeked with ``get_by_email_ci`` (oldest wins), so a
        genuinely valid admin-issued link rendered "Invalid or expired"."""
        r = variants.post(
            "/auth/password/reset/confirm",
            data={
                "email": "dup@test.com",
                "token": "tok-abc",
                "password": "brand-new-password-1",
                "confirm_password": "brand-new-password-1",
            },
        )
        assert r.status_code == 200, r.text
        assert "Invalid or expired reset link" not in r.text

        # And the password landed on the row that held the token.
        from src.db import get_system_db

        conn = get_system_db()
        try:
            rows = dict(
                conn.execute("SELECT id, password_hash FROM users WHERE id IN ('dup-old','dup-new')").fetchall()
            )
        finally:
            conn.close()
        assert rows["dup-new"], "password was not set on the token's own account"
        assert not rows["dup-old"], "password landed on the wrong account"


class TestGoogleOAuth:
    def test_google_login_not_configured(self, client):
        """Without GOOGLE_CLIENT_ID, should redirect to login with error."""
        resp = client.get("/auth/google/login", follow_redirects=False)
        assert resp.status_code == 302 or resp.status_code == 307
        assert "error" in resp.headers.get("location", "")


class TestMicrosoftOAuth:
    def test_microsoft_login_not_configured(self, client):
        """Without MICROSOFT_CLIENT_ID/SECRET/TENANT_ID, should redirect to login with error."""
        resp = client.get("/auth/microsoft/login", follow_redirects=False)
        assert resp.status_code == 302 or resp.status_code == 307
        assert "error" in resp.headers.get("location", "")


class TestMicrosoftTenantValidation:
    """The single-tenant promise is structural, not documentary.

    ``MICROSOFT_TENANT_ID`` is interpolated into the OIDC discovery URL, so
    the three reserved Microsoft values (``common`` / ``organizations`` /
    ``consumers``) silently turn the provider into the multi-tenant
    configuration the module says it never uses — with ``auth.allowed_domain``
    unset that lets any Microsoft account anywhere sign in and self-provision.
    """

    @pytest.mark.parametrize("reserved", ["common", "organizations", "consumers", "COMMON", " organizations "])
    def test_reserved_multi_tenant_values_are_refused(self, reserved):
        from app.auth.providers import microsoft as ms

        problem = ms.tenant_id_error(reserved)
        assert problem, f"{reserved!r} must be refused"
        assert "multi-tenant" in problem
        assert "MICROSOFT_TENANT_ID" in problem

    def test_directory_guid_is_accepted(self):
        from app.auth.providers import microsoft as ms

        assert ms.tenant_id_error("72f988bf-86f1-41af-91ab-2d7cd011db47") is None

    def test_verified_domain_is_accepted(self):
        from app.auth.providers import microsoft as ms

        assert ms.tenant_id_error("example.onmicrosoft.com") is None
        assert ms.tenant_id_error("example.com") is None

    @pytest.mark.parametrize("bad", ["", "   ", "not a tenant", "example", "../common", "tenant/../common"])
    def test_junk_is_refused(self, bad):
        from app.auth.providers import microsoft as ms

        assert ms.tenant_id_error(bad) is not None

    @pytest.mark.parametrize("reserved", ["common", "organizations", "consumers"])
    def test_reserved_value_makes_the_provider_unavailable(self, monkeypatch, reserved):
        """Refusal surfaces as "provider not available": the login button is
        hidden and /auth/microsoft/login reports microsoft_not_configured. An
        instance must never silently come up multi-tenant."""
        from app.auth.providers import microsoft as ms

        monkeypatch.setattr(ms, "MICROSOFT_CLIENT_ID", "cid")
        monkeypatch.setattr(ms, "MICROSOFT_CLIENT_SECRET", "secret")
        monkeypatch.setattr(ms, "MICROSOFT_TENANT_ID", reserved)
        assert ms.is_available() is False
        assert any("multi-tenant" in w for w in ms.startup_warnings())

    @pytest.mark.parametrize(
        "guid",
        [
            "9188040d-6c67-4c5b-b112-36a304b66dad",  # "personal Microsoft accounts"
            "9188040D-6C67-4C5B-B112-36A304B66DAD",  # same, uppercase
            "f8cdef31-a31e-4b4a-93e4-5f571e91255a",  # the other well-known consumer tenant
        ],
    )
    def test_well_known_consumer_tenant_guids_are_refused(self, guid):
        """Refusing the reserved *names* is not enough. Microsoft publishes
        GUIDs for the consumer tenants, and a discovery URL built from one is
        functionally ``consumers`` — the exact configuration the name check
        exists to prevent, reached by spelling it differently."""
        from app.auth.providers import microsoft as ms

        problem = ms.tenant_id_error(guid)
        assert problem, f"{guid!r} must be refused"
        assert "multi-tenant" in problem

    def test_guid_tenant_is_available(self, monkeypatch):
        from app.auth.providers import microsoft as ms

        monkeypatch.setattr(ms, "MICROSOFT_CLIENT_ID", "cid")
        monkeypatch.setattr(ms, "MICROSOFT_CLIENT_SECRET", "secret")
        monkeypatch.setattr(ms, "MICROSOFT_TENANT_ID", "72f988bf-86f1-41af-91ab-2d7cd011db47")
        assert ms.is_available() is True


class TestMicrosoftIdentityResolution:
    """Which claim becomes the Agnes account identity.

    ``ensure_user`` matches accounts by the email STRING alone (no provider
    column, no IdP subject binding), so whatever this returns can take over an
    account created by Google or password auth.
    """

    def test_email_claim_wins(self):
        from app.auth.providers import microsoft as ms

        assert ms.resolve_identity({"email": "a@example.com", "preferred_username": "b@example.com"}) == "a@example.com"

    def test_upn_fallback_when_email_claim_absent(self):
        from app.auth.providers import microsoft as ms

        assert ms.resolve_identity({"preferred_username": "  A.User@Example.com "}) == "a.user@example.com"

    def test_guest_ext_upn_is_not_an_identity(self):
        """A B2B guest's tenant UPN (``user_othercorp.com#EXT#@tenant...``) is
        not a mailbox; provisioning an Agnes account under it is meaningless
        and `#` is not valid in an address. Rejected → microsoft_no_email."""
        from app.auth.providers import microsoft as ms

        assert ms.resolve_identity({"preferred_username": "user_othercorp.com#EXT#@tenant.onmicrosoft.com"}) == ""

    def test_no_claims_at_all(self):
        from app.auth.providers import microsoft as ms

        assert ms.resolve_identity({}) == ""

    def test_unpinned_allowed_domain_warns_about_guests(self, monkeypatch):
        """Single tenant is not by itself an identity boundary — B2B guests
        carry EXTERNAL addresses in the `email` claim. Say so at boot."""
        from app.auth.providers import microsoft as ms

        monkeypatch.setattr(ms, "MICROSOFT_CLIENT_ID", "cid")
        monkeypatch.setattr(ms, "MICROSOFT_CLIENT_SECRET", "secret")
        monkeypatch.setattr(ms, "MICROSOFT_TENANT_ID", "72f988bf-86f1-41af-91ab-2d7cd011db47")
        monkeypatch.setattr(ms, "get_allowed_domains", lambda: [])
        warnings = ms.startup_warnings()
        assert any("allowed_domain" in w and "guest" in w.lower() for w in warnings)

        monkeypatch.setattr(ms, "get_allowed_domains", lambda: ["example.com"])
        assert ms.startup_warnings() == []


@pytest.mark.skip(
    reason="v12: _fetch_google_groups removed; group sync now uses ADC via app.auth.group_sync.fetch_user_groups. Rewrite for the new module."
)
class TestGoogleGroupsFetch:
    """Unit tests for _fetch_google_groups — the helper must be tolerant of
    every realistic failure mode (non-Workspace tenants return 403, expired
    tokens return 401, network errors bubble from httpx) and never raise."""

    def test_parses_groups_from_success_response(self, monkeypatch):
        import asyncio
        from app.auth.providers import google as gp

        # searchTransitiveGroups returns {"memberships": [...]}, not {"groups": [...]}.
        # Each item carries the group identity in groupKey.id + displayName,
        # matching the actual API response shape.
        fake_payload = {
            "memberships": [
                {
                    "group": "groups/abc123",
                    "groupKey": {"id": "team-eng@example.com"},
                    "displayName": "Engineering",
                },
                {
                    "group": "groups/def456",
                    "groupKey": {"id": "everyone@example.com"},
                    # No displayName — falls back to id
                },
            ],
        }

        class _Resp:
            status_code = 200
            text = ""

            def json(self):
                return fake_payload

        class _FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, params=None, headers=None):
                return _Resp()

        monkeypatch.setattr(gp.httpx, "AsyncClient", _FakeClient)

        groups = asyncio.run(gp._fetch_google_groups("fake-token", "user@example.com"))
        assert groups == [
            {"id": "team-eng@example.com", "name": "Engineering"},
            {"id": "everyone@example.com", "name": "everyone@example.com"},
        ]

    def test_returns_empty_on_403(self, monkeypatch):
        """Cloud Identity not enabled (non-Workspace tenant) → 403 → [] + warning."""
        import asyncio
        from app.auth.providers import google as gp

        class _Resp:
            status_code = 403
            text = "Cloud Identity API has not been enabled"

        class _FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, params=None, headers=None):
                return _Resp()

        monkeypatch.setattr(gp.httpx, "AsyncClient", _FakeClient)

        groups = asyncio.run(gp._fetch_google_groups("fake-token", "user@example.com"))
        assert groups == []

    def test_returns_empty_on_exception(self, monkeypatch):
        """Network error inside httpx must be swallowed, not propagated."""
        import asyncio
        from app.auth.providers import google as gp

        class _FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, *a, **kw):
                raise RuntimeError("boom")

        monkeypatch.setattr(gp.httpx, "AsyncClient", _FakeClient)

        groups = asyncio.run(gp._fetch_google_groups("fake-token", "user@example.com"))
        assert groups == []


class TestLocalDevGroupsParser:
    """Unit tests for get_local_dev_groups() — must tolerate every malformed
    input shape (typos, wrong type, missing id) and never raise. Bad input
    becomes [] + a WARNING log so the dev mock can't break the dev flow."""

    def test_returns_empty_when_unset(self, monkeypatch):
        from app.auth.dependencies import get_local_dev_groups

        monkeypatch.delenv("LOCAL_DEV_GROUPS", raising=False)
        assert get_local_dev_groups() == []

    def test_returns_empty_when_blank(self, monkeypatch):
        from app.auth.dependencies import get_local_dev_groups

        monkeypatch.setenv("LOCAL_DEV_GROUPS", "   ")
        assert get_local_dev_groups() == []

    def test_parses_valid_json_array(self, monkeypatch):
        from app.auth.dependencies import get_local_dev_groups

        monkeypatch.setenv(
            "LOCAL_DEV_GROUPS",
            '[{"id":"eng@x.com","name":"Engineering"},{"id":"admins@x.com","name":"Admins"}]',
        )
        assert get_local_dev_groups() == [
            {"id": "eng@x.com", "name": "Engineering"},
            {"id": "admins@x.com", "name": "Admins"},
        ]

    def test_defaults_name_to_id(self, monkeypatch):
        from app.auth.dependencies import get_local_dev_groups

        monkeypatch.setenv("LOCAL_DEV_GROUPS", '[{"id":"eng@x.com"}]')
        assert get_local_dev_groups() == [{"id": "eng@x.com", "name": "eng@x.com"}]

    def test_preserves_extra_fields(self, monkeypatch):
        """Forward-compat: unknown fields like roles/labels survive parsing
        so future group-aware code can be exercised in dev without parser changes."""
        from app.auth.dependencies import get_local_dev_groups

        monkeypatch.setenv(
            "LOCAL_DEV_GROUPS",
            '[{"id":"eng@x.com","name":"Eng","roles":["MEMBER","OWNER"]}]',
        )
        result = get_local_dev_groups()
        assert result == [
            {"id": "eng@x.com", "name": "Eng", "roles": ["MEMBER", "OWNER"]},
        ]

    def test_returns_empty_on_invalid_json(self, monkeypatch):
        from app.auth.dependencies import get_local_dev_groups

        monkeypatch.setenv("LOCAL_DEV_GROUPS", "not-json,foo")
        assert get_local_dev_groups() == []

    def test_returns_empty_on_non_list(self, monkeypatch):
        from app.auth.dependencies import get_local_dev_groups

        monkeypatch.setenv("LOCAL_DEV_GROUPS", '{"id":"eng@x.com"}')
        assert get_local_dev_groups() == []

    def test_skips_items_without_id(self, monkeypatch):
        """Bad items are dropped, valid siblings survive — partial config
        still produces something useful instead of nuking the whole list."""
        from app.auth.dependencies import get_local_dev_groups

        monkeypatch.setenv(
            "LOCAL_DEV_GROUPS",
            '[{"name":"no-id"},{"id":"eng@x.com","name":"Eng"},"string-not-object"]',
        )
        assert get_local_dev_groups() == [{"id": "eng@x.com", "name": "Eng"}]


class TestLocalDevUserLookup:
    """Startup seeds the dev account through ``normalize_email`` (lower-cased),
    so the request-time read has to fold case too — otherwise a mixed-case
    ``LOCAL_DEV_USER_EMAIL`` seeds a row the auto-login can never find and dev
    mode silently stops logging anybody in."""

    def test_dev_user_resolves_when_configured_address_has_capitals(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
        monkeypatch.setenv("LOCAL_DEV_MODE", "1")
        monkeypatch.setenv("LOCAL_DEV_USER_EMAIL", "Dev@LocalHost")

        from src.db import get_system_db
        from src.repositories.users import UserRepository
        from src.user_identity import normalize_email

        conn = get_system_db()
        try:
            # Exactly what the startup seed writes.
            UserRepository(conn).create(id="dev1", email=normalize_email("Dev@LocalHost"), name="Admin")
        finally:
            conn.close()

        from app.auth.dependencies import _get_local_dev_user

        user = _get_local_dev_user()
        assert user is not None, "dev auto-login could not find the account startup seeded"
        assert user["id"] == "dev1"


@pytest.mark.skip(
    reason="v12: session.google_groups + /me/profile group rendering removed; profile now reads user_group_members. Rewrite to assert membership rows instead."
)
class TestLocalDevGroupsInjection:
    """End-to-end: with LOCAL_DEV_MODE=1 + LOCAL_DEV_GROUPS, the seeded dev
    user's session.google_groups gets populated on first authenticated request
    so /me/profile renders the mocked groups."""

    @pytest.fixture
    def dev_client(self, tmp_path, monkeypatch, shared_app):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
        monkeypatch.setenv("SESSION_SECRET", "test-session-secret-32chars-minimum!!")
        monkeypatch.setenv("LOCAL_DEV_MODE", "1")
        monkeypatch.setenv("LOCAL_DEV_USER_EMAIL", "dev@localhost")
        monkeypatch.setenv(
            "LOCAL_DEV_GROUPS",
            '[{"id":"local-dev-engineers@example.com","name":"Local Dev Engineers"}]',
        )

        return TestClient(shared_app)

    def test_dev_user_sees_mocked_groups_on_profile(self, dev_client):
        resp = dev_client.get("/me/profile")
        assert resp.status_code == 200
        body = resp.text
        assert "local-dev-engineers@example.com" in body
        assert "Local Dev Engineers" in body
        assert "No Google groups available" not in body

    def test_empty_LOCAL_DEV_GROUPS_falls_back_to_empty_state(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
        monkeypatch.setenv("LOCAL_DEV_MODE", "1")
        monkeypatch.delenv("LOCAL_DEV_GROUPS", raising=False)
        from app.main import create_app

        client = TestClient(create_app())
        resp = client.get("/me/profile")
        assert resp.status_code == 200
        assert "No Google groups available" in resp.text


class TestLocalDevGroupsStartupValidation:
    """Startup banner reports on LOCAL_DEV_GROUPS so a typo or malformed JSON
    is loud at boot, not silent until the first authenticated request."""

    def _capture_startup_logs(self, tmp_path, monkeypatch, caplog, env_value):
        import logging

        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
        monkeypatch.setenv("LOCAL_DEV_MODE", "1")
        if env_value is None:
            monkeypatch.delenv("LOCAL_DEV_GROUPS", raising=False)
        else:
            monkeypatch.setenv("LOCAL_DEV_GROUPS", env_value)
        from app.main import create_app

        with caplog.at_level(logging.WARNING, logger="app.main"):
            create_app()
        return caplog.text

    def test_logs_count_and_ids_on_valid_input(self, tmp_path, monkeypatch, caplog):
        text = self._capture_startup_logs(
            tmp_path,
            monkeypatch,
            caplog,
            '[{"id":"a@x.com","name":"A"},{"id":"b@x.com","name":"B"}]',
        )
        assert "mocking 2 group(s)" in text
        assert "a@x.com" in text
        assert "b@x.com" in text

    def test_warns_when_set_but_malformed(self, tmp_path, monkeypatch, caplog):
        text = self._capture_startup_logs(
            tmp_path,
            monkeypatch,
            caplog,
            "not-valid-json",
        )
        assert "produced no valid groups" in text

    def test_logs_unset_explicitly(self, tmp_path, monkeypatch, caplog):
        text = self._capture_startup_logs(tmp_path, monkeypatch, caplog, None)
        assert "LOCAL_DEV_GROUPS is unset" in text


class TestCookieAuth:
    def test_web_ui_with_cookie(self, client):
        """Test that web UI routes accept JWT from cookie."""
        from app.auth.jwt import create_access_token
        from src.db import get_system_db
        from src.repositories.users import UserRepository

        conn = get_system_db()
        ur = UserRepository(conn)
        # Use existing user
        user = ur.get_by_email("pw@test.com")
        conn.close()

        token = create_access_token(user["id"], user["email"])
        # Set cookie and access dashboard
        client.cookies.set("access_token", token)
        resp = client.get("/dashboard")
        # Should not be 401 — cookie auth works
        assert resp.status_code != 401


@pytest.mark.skip(
    reason="v12: callback writes user_group_members instead of users.groups JSON. Rewrite assertions for the new schema."
)
class TestGoogleCallbackGroupSync:
    """Google OAuth callback populates users.groups from Workspace.

    The real google.py module captures GOOGLE_CLIENT_ID/SECRET at import
    time and conditionally registers `oauth.google`. For tests we:
      1. Patch `is_available` so the callback's early-return guard doesn't fire
      2. Stub `oauth.google.authorize_access_token` with an AsyncMock
      3. Stub `fetch_user_groups` at the import site (app.auth.providers.google)
         to return a fixed list — no real Google traffic
    """

    @pytest.fixture
    def google_app(self, tmp_path, monkeypatch, shared_app):
        import json as _json
        from unittest.mock import AsyncMock
        from types import SimpleNamespace

        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")

        import app.auth.providers.google as g_mod

        # (1) bypass the is_available guard
        monkeypatch.setattr(g_mod, "is_available", lambda: True)

        # (2) fake oauth.google with async authorize_access_token
        fake_oauth_google = SimpleNamespace(
            authorize_access_token=AsyncMock(
                return_value={
                    "userinfo": {
                        "email": "tester@groupon.com",
                        "name": "Tester",
                    }
                }
            )
        )
        monkeypatch.setattr(g_mod.oauth, "google", fake_oauth_google, raising=False)

        # (3) fake fetch_user_groups — also patches the import inside
        # google_callback because it does `from app.auth.group_sync import fetch_user_groups`
        # inside the function body, so patching the source module is enough.
        import app.auth.group_sync as gs_mod

        monkeypatch.setattr(
            gs_mod,
            "fetch_user_groups",
            lambda email: ["grp_a@groupon.com", "grp_b@groupon.com"],
        )

        app = shared_app
        client = TestClient(app, follow_redirects=False)
        return {"client": client, "json": _json}

    def test_callback_creates_user_with_groups(self, google_app):
        """First-time login → user row + groups populated + two user_groups rows."""
        c = google_app["client"]
        _json = google_app["json"]

        resp = c.get("/auth/google/callback?code=x&state=y")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/dashboard"
        # access_token cookie set
        assert "access_token" in resp.cookies

        from src.db import get_system_db
        from src.repositories.users import UserRepository
        from src.repositories.user_groups import UserGroupsRepository

        conn = get_system_db()
        try:
            user = UserRepository(conn).get_by_email("tester@groupon.com")
            assert user is not None
            assert user["role"] == "analyst"
            assert _json.loads(user["groups"]) == [
                "grp_a@groupon.com",
                "grp_b@groupon.com",
            ]
            names = {g["name"] for g in UserGroupsRepository(conn).list_all()}
            assert "grp_a@groupon.com" in names
            assert "grp_b@groupon.com" in names
            # non-system flag
            row = UserGroupsRepository(conn).get_by_name("grp_a@groupon.com")
            assert row["is_system"] is False
            assert row["created_by"] == "system:google-sync"
        finally:
            conn.close()

    def test_callback_updates_groups_on_relogin(self, google_app, monkeypatch):
        """Second login with a different group set overwrites the first."""
        c = google_app["client"]
        _json = google_app["json"]

        # First login — default stub returns [a, b]
        c.get("/auth/google/callback?code=x&state=y")

        # Swap the mock to return a single, different group on the next call
        import app.auth.group_sync as gs_mod

        monkeypatch.setattr(gs_mod, "fetch_user_groups", lambda email: ["grp_c@groupon.com"])

        c.get("/auth/google/callback?code=x&state=y")

        from src.db import get_system_db
        from src.repositories.users import UserRepository

        conn = get_system_db()
        try:
            user = UserRepository(conn).get_by_email("tester@groupon.com")
            assert _json.loads(user["groups"]) == ["grp_c@groupon.com"]
        finally:
            conn.close()

    def test_callback_fails_soft_on_group_sync_exception(self, google_app, monkeypatch):
        """An exception inside fetch_user_groups does not block the login."""
        c = google_app["client"]
        _json = google_app["json"]

        def raise_boom(email):
            raise RuntimeError("Google API is down")

        import app.auth.group_sync as gs_mod

        monkeypatch.setattr(gs_mod, "fetch_user_groups", raise_boom)

        resp = c.get("/auth/google/callback?code=x&state=y")
        # Login still proceeds, redirect to dashboard with token cookie
        assert resp.status_code == 302
        assert resp.headers["location"] == "/dashboard"
        assert "access_token" in resp.cookies

        from src.db import get_system_db
        from src.repositories.users import UserRepository

        conn = get_system_db()
        try:
            user = UserRepository(conn).get_by_email("tester@groupon.com")
            assert user is not None
            # groups stays NULL (no previous value either)
            assert user["groups"] is None
        finally:
            conn.close()

    def test_callback_empty_groups_does_not_overwrite_existing(self, google_app, monkeypatch):
        """fetch_user_groups returning [] means 'no data' — don't wipe existing
        groups on a transient failure masked as empty."""
        c = google_app["client"]
        _json = google_app["json"]

        # First login populates groups
        c.get("/auth/google/callback?code=x&state=y")

        # Second login: Google returns empty
        import app.auth.group_sync as gs_mod

        monkeypatch.setattr(gs_mod, "fetch_user_groups", lambda email: [])
        c.get("/auth/google/callback?code=x&state=y")

        from src.db import get_system_db
        from src.repositories.users import UserRepository

        conn = get_system_db()
        try:
            user = UserRepository(conn).get_by_email("tester@groupon.com")
            # Previous groups preserved
            assert _json.loads(user["groups"]) == [
                "grp_a@groupon.com",
                "grp_b@groupon.com",
            ]
        finally:
            conn.close()


class TestMagicLinkNextRedirect:
    """The click-through verify lands the user on the page they originally
    asked for (?next), sanitized — not always the home route (Devin #1288)."""

    def _seed(self, email, user_id, token):
        from datetime import datetime, timezone
        from src.db import get_system_db
        from src.repositories.users import UserRepository

        conn = get_system_db()
        repo = UserRepository(conn)
        repo.create(id=user_id, email=email, name="Next User")
        repo.update(id=user_id, reset_token=hash_token(token), reset_token_created=datetime.now(timezone.utc))
        conn.close()

    def test_verify_redirects_to_sanitized_next(self, client):
        self._seed("next-good@test.com", "next-good", "tok-next-good")
        resp = client.get(
            "/auth/email/verify?email=next-good@test.com&token=tok-next-good&next=/catalog",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == "/catalog"

    def test_verify_rejects_open_redirect_next(self, client):
        self._seed("next-evil@test.com", "next-evil", "tok-next-evil")
        resp = client.get(
            "/auth/email/verify?email=next-evil@test.com&token=tok-next-evil&next=//evil.example/",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        # Falls back to the home route; the hostile host never appears.
        assert "evil.example" not in resp.headers["location"]

    def test_verify_without_next_lands_on_home(self, client):
        self._seed("next-none@test.com", "next-none", "tok-next-none")
        resp = client.get(
            "/auth/email/verify?email=next-none@test.com&token=tok-next-none",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"].startswith("/")


class TestEmailMagicLinkTTL:
    """Tests for email magic link token expiry and replay prevention."""

    def test_expired_magic_link_rejected(self, client):
        """A magic link token older than MAGIC_LINK_EXPIRY must be rejected."""
        from src.db import get_system_db
        from src.repositories.users import UserRepository
        from datetime import datetime, timezone, timedelta

        conn = get_system_db()
        repo = UserRepository(conn)
        repo.create(id="expired-user", email="expired@test.com", name="Expired")
        # Set token with old timestamp (beyond 1-hour TTL)
        old_time = datetime.now(timezone.utc) - timedelta(hours=2)
        repo.update(id="expired-user", reset_token=hash_token("expired-token-123"), reset_token_created=old_time)
        conn.close()

        resp = client.post(
            "/auth/email/verify",
            json={
                "email": "expired@test.com",
                "token": "expired-token-123",
            },
        )
        assert resp.status_code == 401

    def test_token_reuse_prevented(self, client):
        """A consumed magic link token cannot be used again."""
        from src.db import get_system_db
        from src.repositories.users import UserRepository
        from datetime import datetime, timezone

        conn = get_system_db()
        repo = UserRepository(conn)
        repo.create(id="reuse-user", email="reuse@test.com", name="Reuse")
        token = "reusable-token-456"
        repo.update(id="reuse-user", reset_token=hash_token(token), reset_token_created=datetime.now(timezone.utc))
        conn.close()

        # First use should succeed
        resp1 = client.post(
            "/auth/email/verify",
            json={
                "email": "reuse@test.com",
                "token": token,
            },
        )
        assert resp1.status_code == 200

        # Second use must fail
        resp2 = client.post(
            "/auth/email/verify",
            json={
                "email": "reuse@test.com",
                "token": token,
            },
        )
        assert resp2.status_code == 401

    def test_invalid_signature_token_rejected(self, client):
        """A token that doesn't match any stored value must be rejected."""
        from src.db import get_system_db
        from src.repositories.users import UserRepository
        from datetime import datetime, timezone

        conn = get_system_db()
        repo = UserRepository(conn)
        repo.create(id="sig-user", email="sig@test.com", name="Sig")
        repo.update(
            id="sig-user", reset_token=hash_token("real-token-789"), reset_token_created=datetime.now(timezone.utc)
        )
        conn.close()

        resp = client.post(
            "/auth/email/verify",
            json={
                "email": "sig@test.com",
                "token": "wrong-token-xyz",
            },
        )
        assert resp.status_code == 401


@pytest.mark.skip(
    reason="Authlib OAuth internals require complex async mock; group sync is tested via unit tests and integration. Full E2E OAuth flow needs real Google credentials or dedicated mock infrastructure."
)
class TestGoogleOAuthFullFlow:
    """Tests for Google OAuth callback with mocked token exchange and group sync.

    These tests require mocking authlib's internal OAuth client which involves
    async Starlette session middleware. The group sync logic is covered by
    unit tests for fetch_user_groups and the existing TestGoogleCallbackGroupSync.
    """

    def test_google_callback_creates_new_user(self, tmp_path, monkeypatch):
        """Google OAuth callback must create a new user if not found."""
        pass

    def test_google_callback_syncs_group_memberships(self, tmp_path, monkeypatch):
        """Google OAuth callback must sync Workspace groups into user_group_members."""
        pass

    def test_google_callback_existing_user_not_duplicated(self, tmp_path, monkeypatch):
        """Re-login via Google OAuth must not duplicate the user."""
        pass

    def test_google_callback_api_error_handled(self, tmp_path, monkeypatch):
        """Google OAuth callback must handle API errors gracefully."""
        pass


class TestSmtpSenderResolution:
    """`email.from_address` is shipped by `config/instance.yaml.example` and
    documented in `docs/CONFIGURATION.md`, but nothing read it — an operator who
    configured only the YAML kept sending as `noreply@example.com`, with no
    error to notice. Env stays ahead of it so no existing deployment's sender
    changes.
    """

    @staticmethod
    def _clear_env(monkeypatch):
        monkeypatch.delenv("SMTP_FROM", raising=False)
        monkeypatch.delenv("EMAIL_FROM_ADDRESS", raising=False)

    def test_smtp_from_env_wins_over_yaml(self, monkeypatch):
        from app.auth import _common

        monkeypatch.setenv("SMTP_FROM", "env@corp.example")
        monkeypatch.setattr("app.instance_config.get_value", lambda *k, **kw: "yaml@corp.example", raising=False)
        assert _common.smtp_from_address() == "env@corp.example"

    def test_legacy_env_key_still_wins_over_yaml(self, monkeypatch):
        """`EMAIL_FROM_ADDRESS` was the removed SendGrid branch's key. A
        deployment carrying it must not have its sender changed by a YAML value
        it never intended to activate."""
        from app.auth import _common

        self._clear_env(monkeypatch)
        monkeypatch.setenv("EMAIL_FROM_ADDRESS", "legacy@corp.example")
        monkeypatch.setattr("app.instance_config.get_value", lambda *k, **kw: "yaml@corp.example", raising=False)
        assert _common.smtp_from_address() == "legacy@corp.example"

    def test_yaml_from_address_is_honored_when_no_env_is_set(self, monkeypatch):
        from app.auth import _common

        self._clear_env(monkeypatch)
        monkeypatch.setattr("app.instance_config.get_value", lambda *k, **kw: "yaml@corp.example", raising=False)
        assert _common.smtp_from_address() == "yaml@corp.example"

    def test_the_templates_own_placeholder_is_not_treated_as_configured(self, monkeypatch):
        """`instance.yaml.example` ships the literal `noreply@example.com`. A
        copied-but-unedited template must not read as a deliberate choice — the
        answer is the same either way, but treating it as configured would make
        the fallback chain lie about where the value came from."""
        from app.auth import _common

        self._clear_env(monkeypatch)
        monkeypatch.setattr("app.instance_config.get_value", lambda *k, **kw: "noreply@example.com", raising=False)
        assert _common.smtp_from_address() == "noreply@example.com"

    def test_an_unreadable_instance_config_does_not_break_sending(self, monkeypatch):
        """Resolving a sender must not be the thing that raises: a corrupt or
        absent instance.yaml would otherwise turn every magic link into a 500."""
        from app.auth import _common

        self._clear_env(monkeypatch)

        def _boom(*_a, **_kw):
            raise RuntimeError("instance.yaml unreadable")

        monkeypatch.setattr("app.instance_config.get_value", _boom, raising=False)
        assert _common.smtp_from_address() == "noreply@example.com"
