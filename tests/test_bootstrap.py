"""Tests for bootstrap endpoint — first admin user creation."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def fresh_client(tmp_path, monkeypatch, shared_app):
    """Client with EMPTY database — no users."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")

    app = shared_app
    return TestClient(app)


@pytest.fixture
def seeded_client(tmp_path, monkeypatch, shared_app):
    """Client with one existing seed user (no password_hash — like SEED_ADMIN_EMAIL seeding)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
    from src.db import get_system_db
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="existing", email="existing@test.com", name="E")
    conn.close()
    return TestClient(shared_app)


@pytest.fixture
def duplicate_variants_client(tmp_path, monkeypatch, shared_app):
    """Two case-variant rows for one person, the OLDER one sorting SECOND by
    email. Returns (client, older_id, newer_id)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
    from src.db import get_system_db
    from src.repositories.users import UserRepository

    conn = get_system_db()
    repo = UserRepository(conn)
    # ASCII sort puts "Ada@test.com" before "ada@test.com", so the alphabetical
    # winner is the NEWER row — the tie-break has to disagree to be visible.
    repo.create(id="newer", email="Ada@test.com", name="A")
    repo.create(id="older", email="ada@test.com", name="A")
    conn.execute("UPDATE users SET created_at = ? WHERE id = ?", ["2025-01-01 00:00:00", "older"])
    conn.execute("UPDATE users SET created_at = ? WHERE id = ?", ["2026-06-01 00:00:00", "newer"])
    conn.close()
    return TestClient(shared_app), "older", "newer"


@pytest.fixture
def password_user_client(tmp_path, monkeypatch, shared_app):
    """Client with a user who already has a password set — bootstrap must be disabled."""
    from argon2 import PasswordHasher

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
    from src.db import get_system_db
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(
        id="existing",
        email="existing@test.com",
        name="E",
        password_hash=PasswordHasher().hash("pre-existing-pass"),
    )
    conn.close()
    return TestClient(shared_app)


@pytest.fixture
def admin_member_client(tmp_path, monkeypatch, shared_app):
    """Client with a password-less user who IS in the Admin group — the OAuth /
    magic-link deployment shape (seed admin auto-promoted at startup, no
    password ever set). Bootstrap must be locked here even though no user has a
    password_hash (the pre-hardening hole)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="seed", email="seed@test.com", name="Seed")  # no password
    admin_group = UserGroupsRepository(conn).get_by_name(SYSTEM_ADMIN_GROUP)
    UserGroupMembersRepository(conn).add_member(
        user_id="seed",
        group_id=admin_group["id"],
        source="system_seed",
        added_by="test",
    )
    conn.close()
    return TestClient(shared_app)


class TestBootstrap:
    def test_bootstrap_on_empty_db(self, fresh_client):
        """First call creates admin and returns token."""
        resp = fresh_client.post(
            "/auth/bootstrap",
            json={
                "email": "admin@test.com",
                "name": "Admin",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == "admin@test.com"
        assert data["role"] == "admin"
        assert "access_token" in data

    def test_bootstrap_refuses_a_body_with_no_resolvable_address(self, fresh_client):
        """`normalize_email` collapses whitespace-only input to "", and this is
        the most privileged write in the app — without a shape check it would
        mint an ADMIN account whose identity no auth provider can ever resolve,
        and a second such call would collide on UNIQUE(email). `POST /api/users`
        already refuses this; bootstrap must too."""
        for bad in ("   ", "", "not-an-address", "@example.com", "local@"):
            resp = fresh_client.post("/auth/bootstrap", json={"email": bad, "name": "X"})
            assert resp.status_code == 422, f"{bad!r} was accepted: {resp.status_code} {resp.text}"
            assert resp.json()["detail"] == "A valid email address is required"

    def test_bootstrap_with_password(self, fresh_client):
        """Bootstrap with password sets password hash."""
        resp = fresh_client.post(
            "/auth/bootstrap",
            json={
                "email": "admin@test.com",
                "password": "securepass123",
            },
        )
        assert resp.status_code == 200
        assert "access_token" in resp.json()

        resp2 = fresh_client.get("/api/health")
        assert resp2.status_code == 200

    def test_bootstrap_activates_seed_user(self, seeded_client):
        """Bootstrap activates a password-less seed user (SEED_ADMIN_EMAIL scenario)."""
        resp = seeded_client.post(
            "/auth/bootstrap",
            json={
                "email": "existing@test.com",
                "password": "newpass123",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["role"] == "admin"

        # Login now works
        login = seeded_client.post(
            "/auth/password/login",
            json={
                "email": "existing@test.com",
                "password": "newpass123",
            },
        )
        assert login.status_code == 200

    def test_bootstrap_disabled_when_password_user_exists(self, password_user_client):
        """Bootstrap fails with 403 when any user already has a password set."""
        resp = password_user_client.post(
            "/auth/bootstrap",
            json={
                "email": "hacker@evil.com",
                "password": "should-not-work",
            },
        )
        assert resp.status_code == 403
        assert "Bootstrap disabled" in resp.json()["detail"]

    def test_bootstrap_disabled_when_admin_exists_without_password(self, admin_member_client):
        """SECURITY (CRIT-1 pre-launch hardening): an OAuth / magic-link-only
        deployment, where the seed admin never gets a password_hash, must NOT
        leave /auth/bootstrap open. The lock now keys on Admin-group membership,
        not just password presence — so an unauthenticated caller cannot mint or
        overwrite an admin here."""
        resp = admin_member_client.post(
            "/auth/bootstrap",
            json={
                "email": "hacker@evil.com",
                "password": "should-not-work",
            },
        )
        assert resp.status_code == 403
        assert "Bootstrap disabled" in resp.json()["detail"]

    def test_bootstrap_escape_hatch_with_token(self, admin_member_client, monkeypatch):
        """AGNES_BOOTSTRAP_TOKEN escape hatch: with an admin already present,
        bootstrap stays 403 for an unauthenticated caller but succeeds for one
        presenting the matching X-Bootstrap-Token (the destroy-recreate runbook).
        The token is read at request time, so setting it after app start is fine."""
        monkeypatch.setenv("AGNES_BOOTSTRAP_TOKEN", "super-secret-bootstrap-token-xyz")

        # Wrong/absent token → still locked.
        locked = admin_member_client.post(
            "/auth/bootstrap",
            json={"email": "hacker@evil.com", "password": "should-not-work"},
        )
        assert locked.status_code == 403

        # Correct token → allowed even though an admin exists.
        ok = admin_member_client.post(
            "/auth/bootstrap",
            json={"email": "opsadmin@test.com", "password": "newpass123"},
            headers={"X-Bootstrap-Token": "super-secret-bootstrap-token-xyz"},
        )
        assert ok.status_code == 200
        assert ok.json()["role"] == "admin"

    def test_bootstrap_allowed_when_only_admin_is_scheduler_user(self, tmp_path, monkeypatch):
        """The synthetic scheduler service user (scheduler@system.local) is
        auto-added to the Admin group when SCHEDULER_API_TOKEN is set. It must
        NOT count as 'an admin exists' — otherwise first-install bootstrap would
        be impossible on a fresh scheduler-token instance with no real admin."""
        import uuid

        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
        from app.auth.scheduler_token import SCHEDULER_USER_EMAIL, SCHEDULER_USER_NAME
        from app.main import create_app
        from src.db import SYSTEM_ADMIN_GROUP, get_system_db
        from src.repositories.user_group_members import UserGroupMembersRepository
        from src.repositories.user_groups import UserGroupsRepository
        from src.repositories.users import UserRepository

        conn = get_system_db()
        sched_id = str(uuid.uuid4())
        UserRepository(conn).create(id=sched_id, email=SCHEDULER_USER_EMAIL, name=SCHEDULER_USER_NAME)
        admin_group = UserGroupsRepository(conn).get_by_name(SYSTEM_ADMIN_GROUP)
        UserGroupMembersRepository(conn).add_member(
            user_id=sched_id, group_id=admin_group["id"], source="system_seed", added_by="test"
        )
        conn.close()

        client = TestClient(create_app())
        resp = client.post(
            "/auth/bootstrap",
            json={"email": "firstadmin@test.com", "password": "newpass123"},
        )
        assert resp.status_code == 200
        assert resp.json()["role"] == "admin"

    def test_bootstrap_then_login(self, fresh_client):
        """After bootstrap with password, /auth/token login works; without password it requires OAuth."""
        # Bootstrap with a password
        fresh_client.post(
            "/auth/bootstrap",
            json={
                "email": "admin@test.com",
                "password": "adminpass123",
            },
        )

        # Normal password login succeeds
        resp = fresh_client.post(
            "/auth/token",
            json={
                "email": "admin@test.com",
                "password": "adminpass123",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["role"] == "admin"

    def test_bootstrap_no_password_token_rejected(self, fresh_client):
        """After passwordless bootstrap, /auth/token must reject the user (OAuth-only flow)."""
        fresh_client.post(
            "/auth/bootstrap",
            json={
                "email": "admin@test.com",
            },
        )

        resp = fresh_client.post(
            "/auth/token",
            json={
                "email": "admin@test.com",
            },
        )
        assert resp.status_code == 401

    def test_bootstrap_second_call_fails_once_password_set(self, fresh_client):
        """Endpoint self-deactivates once any user has a password."""
        # First call WITH password — locks bootstrap
        fresh_client.post(
            "/auth/bootstrap",
            json={
                "email": "admin@test.com",
                "password": "realpass123",
            },
        )

        # Any subsequent bootstrap attempt fails
        resp = fresh_client.post(
            "/auth/bootstrap",
            json={
                "email": "second@test.com",
                "password": "other-pass",
            },
        )
        assert resp.status_code == 403

    def test_bootstrap_fresh_user_joins_admin_and_everyone(self, fresh_client):
        """Issue #748: bootstrap grants Admin (pre-existing) AND Everyone
        (new) so the very first admin also sees Everyone-scoped grants."""
        resp = fresh_client.post(
            "/auth/bootstrap",
            json={
                "email": "admin@test.com",
                "name": "Admin",
            },
        )
        assert resp.status_code == 200
        from src.db import get_system_db
        from src.repositories.users import UserRepository
        from src.repositories.user_group_members import UserGroupMembersRepository

        conn = get_system_db()
        try:
            user = UserRepository(conn).get_by_email("admin@test.com")
            assert user is not None
            rows = UserGroupMembersRepository(conn).list_groups_with_meta_for_user(user["id"])
            names = {r["name"] for r in rows}
            assert {"Admin", "Everyone"} <= names
            everyone_row = next(r for r in rows if r["name"] == "Everyone")
            assert everyone_row["source"] == "system_seed"
        finally:
            conn.close()

    def test_bootstrap_activate_seed_user_joins_admin_and_everyone(self, seeded_client):
        """Same grant applies on the activate-existing-seed branch."""
        resp = seeded_client.post(
            "/auth/bootstrap",
            json={
                "email": "existing@test.com",
                "password": "newpass123",
            },
        )
        assert resp.status_code == 200

        from src.db import get_system_db
        from src.repositories.users import UserRepository
        from src.repositories.user_group_members import UserGroupMembersRepository

        conn = get_system_db()
        try:
            user = UserRepository(conn).get_by_email("existing@test.com")
            assert user is not None
            rows = UserGroupMembersRepository(conn).list_groups_with_meta_for_user(user["id"])
            names = {r["name"] for r in rows}
            assert {"Admin", "Everyone"} <= names
        finally:
            conn.close()

    def test_bootstrap_survives_everyone_grant_failure(self, fresh_client, monkeypatch):
        """A transient failure in the non-critical Everyone grant must not
        fail the bootstrap — the operator still gets their admin token."""
        import app.auth.group_sync as group_sync

        def _boom(*args, **kwargs):
            raise RuntimeError("transient db error")

        monkeypatch.setattr(group_sync, "ensure_everyone_membership", _boom)
        resp = fresh_client.post(
            "/auth/bootstrap",
            json={"email": "admin@test.com", "name": "Admin"},
        )
        assert resp.status_code == 200
        assert resp.json()["access_token"]

    def test_full_agent_flow(self, fresh_client):
        """Simulate full AI agent deployment flow."""
        # 1. Health check (no auth — minimal endpoint)
        resp = fresh_client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        # 2. Bootstrap admin
        resp = fresh_client.post(
            "/auth/bootstrap",
            json={
                "email": "agent@company.com",
                "name": "AI Agent",
            },
        )
        assert resp.status_code == 200
        token = resp.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 3. Check manifest (empty, no data yet)
        resp = fresh_client.get("/api/sync/manifest", headers=headers)
        assert resp.status_code == 200
        assert len(resp.json()["tables"]) == 0

        # 4. List users
        resp = fresh_client.get("/api/users", headers=headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 1

        # 5. Add analyst user
        resp = fresh_client.post(
            "/api/users",
            json={
                "email": "analyst@company.com",
                "name": "Analyst",
            },
            headers=headers,
        )
        assert resp.status_code == 201

        # 6. Verify via detailed health (requires auth)
        resp = fresh_client.get("/api/health/detailed", headers=headers)
        assert resp.json()["services"]["users"]["count"] == 2


def test_bootstrap_activates_the_row_the_sign_in_doors_resolve_to(duplicate_variants_client):
    """Bootstrap must agree with the shared resolver, not with row order.

    ``repo.list_all()`` is ordered by email, so scanning it for a
    case-insensitive match tie-breaks alphabetically, while every sign-in door
    goes through ``get_by_email_ci`` and tie-breaks on OLDEST. With two case
    variants present, the two can pick different rows — and then the operator
    is told bootstrap succeeded while the password sits on a row nobody ever
    authenticates as.
    """
    client, older_id, newer_id = duplicate_variants_client
    resp = client.post("/auth/bootstrap", json={"email": "ada@test.com", "password": "bootstrap-pw-123"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["user_id"] == older_id
    # The response carries the ACCOUNT's address, not the typed spelling.
    assert resp.json()["email"] == "ada@test.com"

    # And the password actually works through the sign-in door.
    login = client.post("/auth/password/login", json={"email": "ada@test.com", "password": "bootstrap-pw-123"})
    assert login.status_code == 200, login.text
