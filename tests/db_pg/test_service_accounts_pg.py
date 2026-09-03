"""Service-account identities — issue #1534.

Service accounts are PG-only (`users.kind`, A3 ratchet — see
`migrations/versions/0096_users_kind.py`), so the creation/mint/guard
machinery is exercised primarily on Postgres. The DuckDB half of each guard
is still asserted explicitly where it matters (never a silent skip) — the
whole point of several of these tests is that the ABSENCE of the `kind`
column must never crash a login for anyone, service account or not.
"""

from __future__ import annotations


import pytest

_SECRET = "test-secret-key-minimum-32-characters!!"


@pytest.fixture
def _env(state_backend, tmp_path, monkeypatch):
    """DATA_DIR + JWT secret + (DuckDB) fresh system DB, for either backend.
    Mirrors tests/db_pg/test_session_revocation.py::_env."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", _SECRET)
    for sub in ("extracts", "analytics", "state", "notifications"):
        (tmp_path / sub).mkdir(exist_ok=True)
    if state_backend == "duckdb":
        from src.db import close_system_db, get_system_db

        close_system_db()
        get_system_db()  # triggers _ensure_schema + _seed_system_groups
    return state_backend


def _client():
    """Fresh TestClient over the app the `_env`/`state_backend` fixture just
    configured. Mirrors test_session_revocation.py::_client."""
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests._backend_pin import reregister_requires_pg_handler

    app = create_app()
    reregister_requires_pg_handler(app)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Guard 1a: create_access_token refuses an interactive (typ="session") mint
# for a kind='service' row, but not a typ="pat" mint — the ONE choke point
# every login provider shares (app/auth/jwt.py).
# ---------------------------------------------------------------------------


def test_session_mint_refused_for_a_service_account_on_pg(_env):
    if _env != "pg":
        pytest.skip("service accounts are PG-only (A3 ratchet)")
    from app.auth.jwt import create_access_token
    from src.repositories import users_repo
    from src.service_accounts import ServiceAccountInteractiveLoginError

    users_repo().create_service_account(id="svc-1", email="svc@service.local", name="Svc")

    with pytest.raises(ServiceAccountInteractiveLoginError):
        create_access_token("svc-1", "svc@service.local")


def test_pat_mint_still_works_for_a_service_account_on_pg(_env):
    """The whole point of the feature: an admin must still be able to mint a
    durable PAT FOR the service account. typ="pat" is explicit at every real
    call site (app/api/admin_service_accounts.py), so it must sail through
    the exact same function that just refused typ="session" above."""
    if _env != "pg":
        pytest.skip("service accounts are PG-only (A3 ratchet)")
    from app.auth.jwt import create_access_token
    from src.repositories import users_repo

    users_repo().create_service_account(id="svc-1", email="svc@service.local", name="Svc")

    token = create_access_token("svc-1", "svc@service.local", typ="pat", omit_exp=True)
    assert token


def test_session_mint_is_unaffected_for_an_ordinary_human_row(_env):
    """Sanity: the guard must not accidentally catch every login."""
    from app.auth.jwt import create_access_token
    from src.repositories import users_repo

    users_repo().create(id="human-1", email="human@example.com", name="H")
    token = create_access_token("human-1", "human@example.com")
    assert token


def test_session_mint_never_crashes_on_an_unknown_or_fabricated_user_id(_env):
    """A huge fraction of the test suite calls create_access_token with a
    made-up id ("admin1", "owner1", ...) that has no backing row, often with
    no DB/table set up at all. The guard's lookup must fail OPEN on any
    lookup error (missing table, no DATA_DIR wiring) — the real defense
    against a service account signing in is that no login provider can ever
    resolve a real identity to its synthetic address, so this check is
    defense-in-depth and must never turn an infra hiccup into a broken
    login for everyone else."""
    from app.auth.jwt import create_access_token

    token = create_access_token("no-such-user-at-all", "nobody@example.com")
    assert token


# ---------------------------------------------------------------------------
# Guard 1b: password reset / setup-request initiation treats a service
# account exactly like "no such account" — same generic anti-enumeration
# response, no token minted, no email attempted.
# ---------------------------------------------------------------------------


def test_password_reset_request_does_not_mint_a_token_for_a_service_account(_env):
    if _env != "pg":
        pytest.skip("service accounts are PG-only (A3 ratchet)")
    from src.repositories import users_repo

    client = _client()
    users_repo().create_service_account(id="svc-reset", email="svc-reset@service.local", name="Svc")

    resp = client.post("/auth/password/reset", data={"email": "svc-reset@service.local"})
    assert resp.status_code == 200, resp.text
    assert "Check your email" in resp.text

    row = users_repo().get_by_id("svc-reset")
    assert row["reset_token"] is None, "no reset token may be minted for a service account"


def test_password_setup_request_does_not_mint_a_token_for_a_service_account(_env):
    if _env != "pg":
        pytest.skip("service accounts are PG-only (A3 ratchet)")
    from src.repositories import users_repo

    client = _client()
    users_repo().create_service_account(id="svc-setup", email="svc-setup@service.local", name="Svc")

    resp = client.post("/auth/password/setup/request", data={"email": "svc-setup@service.local"})
    assert resp.status_code == 200, resp.text
    assert "Check your email" in resp.text

    row = users_repo().get_by_id("svc-setup")
    assert row["setup_token"] is None, "no setup token may be minted for a service account"


# ---------------------------------------------------------------------------
# Guard 2: Admin-group membership refusal — enforced once, in
# UserGroupMembers(Pg)Repository.add_member, not per call site.
# ---------------------------------------------------------------------------


def test_add_member_to_admin_group_refused_for_a_service_account_on_pg(_env):
    if _env != "pg":
        pytest.skip("service accounts are PG-only (A3 ratchet)")
    from src.db import SYSTEM_ADMIN_GROUP
    from src.repositories import user_group_members_repo, user_groups_repo, users_repo
    from src.service_accounts import ServiceAccountAdminGroupForbidden

    users_repo().create_service_account(id="svc-admin", email="svc-admin@service.local", name="Svc")
    admin_group = user_groups_repo().get_by_name(SYSTEM_ADMIN_GROUP)
    assert admin_group is not None

    with pytest.raises(ServiceAccountAdminGroupForbidden):
        user_group_members_repo().add_member(
            user_id="svc-admin", group_id=admin_group["id"], source="admin", added_by="tester@example.com"
        )
    assert user_group_members_repo().has_membership("svc-admin", admin_group["id"]) is False


def test_add_member_to_a_non_admin_group_still_works_for_a_service_account_on_pg(_env):
    """The guard is scoped to Admin specifically — a service account MUST be
    addable to any ordinary group, since scoped group grants are the entire
    point of the feature."""
    if _env != "pg":
        pytest.skip("service accounts are PG-only (A3 ratchet)")
    from src.repositories import user_group_members_repo, user_groups_repo, users_repo

    users_repo().create_service_account(id="svc-scoped", email="svc-scoped@service.local", name="Svc")
    group = user_groups_repo().create(name="data-team")

    user_group_members_repo().add_member(
        user_id="svc-scoped", group_id=group["id"], source="admin", added_by="tester@example.com"
    )
    assert user_group_members_repo().has_membership("svc-scoped", group["id"]) is True


def test_add_member_to_admin_group_is_unaffected_for_a_human_row(_env):
    """Sanity on both backends: the scheduler/semantic-drafter/memory-curator
    system identities (kind='human', the column default) must keep working —
    Admin-group self-provisioning at boot must not regress."""
    from src.db import SYSTEM_ADMIN_GROUP
    from src.repositories import user_group_members_repo, user_groups_repo, users_repo

    users_repo().create(id="human-admin", email="human-admin@example.com", name="H")
    admin_group = user_groups_repo().get_by_name(SYSTEM_ADMIN_GROUP)
    assert admin_group is not None

    user_group_members_repo().add_member(
        user_id="human-admin", group_id=admin_group["id"], source="admin", added_by="tester@example.com"
    )
    assert user_group_members_repo().has_membership("human-admin", admin_group["id"]) is True


# ---------------------------------------------------------------------------
# Guard 3: people-picker classification (issue #1534). `search_recent`
# backs GET /api/users, which is BOTH "admin user administration" (the
# /admin/users page) AND the ONLY "add someone to a group" picker in the
# product (group_drawer.js's new-group seeding, and admin_access.html's
# per-group member-add search both call the exact same `/api/users?search=`
# — there is no separate individual-user picker anywhere else to exclude
# from). Per the task's own classification both of those INCLUDE service
# accounts (grants are the point), so search_recent is deliberately left
# UNCHANGED — this test pins that as an intentional inclusion, not an
# oversight. Co-session invite (POST /api/chat/copresence/{id}/invite) takes
# a typed exact email with no candidate list, and sharing is group-based
# (GET /api/sharing/groups), so neither is a "picker" in this sense either.
# ---------------------------------------------------------------------------


def test_search_recent_includes_service_accounts_deliberately(_env):
    if _env != "pg":
        pytest.skip("service accounts are PG-only (A3 ratchet)")
    from src.repositories import users_repo

    users_repo().create(id="human-1", email="human@example.com", name="Human")
    users_repo().create_service_account(id="svc-1", email="svc@service.local", name="Svc")

    rows = users_repo().search_recent(limit=10)
    ids = {r["id"] for r in rows}
    assert "human-1" in ids
    assert "svc-1" in ids, "GET /api/users is the admin group-membership picker too — SAs must stay reachable"


# ---------------------------------------------------------------------------
# Guard 5: resolve_token_to_user authenticates a service-account PAT exactly
# like a human PAT, scoped by the SA's own group grants.
# ---------------------------------------------------------------------------


def test_service_account_pat_authenticates_scoped_by_its_own_group_grant(_env):
    if _env != "pg":
        pytest.skip("service accounts are PG-only (A3 ratchet)")
    import hashlib
    import uuid

    from app.auth.jwt import create_access_token
    from app.auth.pat_resolver import resolve_token_to_user
    from src.repositories import access_token_repo, users_repo

    users_repo().create_service_account(id="svc-pat", email="svc-pat@service.local", name="Svc")
    tid = str(uuid.uuid4())
    pat = create_access_token("svc-pat", "svc-pat@service.local", token_id=tid, typ="pat", omit_exp=True)
    access_token_repo().create(
        id=tid,
        user_id="svc-pat",
        name="ci",
        token_hash=hashlib.sha256(pat.encode()).hexdigest(),
        prefix=tid.replace("-", "")[:8],
    )

    user, reason = resolve_token_to_user(None, pat)
    assert reason is None, reason
    assert user is not None
    assert user["id"] == "svc-pat"


def test_deactivating_a_human_leaves_a_service_account_pat_working(_env):
    """The killer criterion: deactivating an unrelated human account must
    have zero effect on a service account's own PATs."""
    if _env != "pg":
        pytest.skip("service accounts are PG-only (A3 ratchet)")
    import hashlib
    import uuid

    from app.auth.jwt import create_access_token
    from app.auth.pat_resolver import resolve_token_to_user
    from src.repositories import access_token_repo, users_repo

    users_repo().create(id="human-victim", email="human-victim@example.com", name="H")
    users_repo().create_service_account(id="svc-independent", email="svc-independent@service.local", name="Svc")

    tid = str(uuid.uuid4())
    pat = create_access_token(
        "svc-independent", "svc-independent@service.local", token_id=tid, typ="pat", omit_exp=True
    )
    access_token_repo().create(
        id=tid,
        user_id="svc-independent",
        name="ci",
        token_hash=hashlib.sha256(pat.encode()).hexdigest(),
        prefix=tid.replace("-", "")[:8],
    )

    users_repo().update(id="human-victim", active=False)

    user, reason = resolve_token_to_user(None, pat)
    assert reason is None, f"deactivating an unrelated human must not affect the SA's PAT: {reason}"
    assert user is not None
    assert user["id"] == "svc-independent"


def test_deactivating_the_service_account_itself_kills_its_pat(_env):
    """The symmetric case: `users.active` is the SAME flip for a service
    account as for a human — pat_resolver's existing active check does the
    rest with zero new code."""
    if _env != "pg":
        pytest.skip("service accounts are PG-only (A3 ratchet)")
    import hashlib
    import uuid

    from app.auth.jwt import create_access_token
    from app.auth.pat_resolver import resolve_token_to_user
    from src.repositories import access_token_repo, users_repo

    users_repo().create_service_account(id="svc-deact", email="svc-deact@service.local", name="Svc")
    tid = str(uuid.uuid4())
    pat = create_access_token("svc-deact", "svc-deact@service.local", token_id=tid, typ="pat", omit_exp=True)
    access_token_repo().create(
        id=tid,
        user_id="svc-deact",
        name="ci",
        token_hash=hashlib.sha256(pat.encode()).hexdigest(),
        prefix=tid.replace("-", "")[:8],
    )

    users_repo().update(id="svc-deact", active=False)

    user, reason = resolve_token_to_user(None, pat)
    assert reason == "deactivated"
    assert user is None


# ---------------------------------------------------------------------------
# HTTP-level acceptance tests — the admin REST surface end to end.
# `build_seeded_client` (tests/db_pg/_parity_sweep_util.py) seeds admin1 +
# analyst1 and hands back a real TestClient + admin session JWT, which is
# what "an admin creates a service account without ever logging in AS it"
# actually means: everything below runs as the seeded interactive admin.
# ---------------------------------------------------------------------------


def _pg_admin_client(tmp_path, monkeypatch, pg_engine):
    from tests.db_pg._parity_sweep_util import build_seeded_client

    client, admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
    return client, {"Authorization": f"Bearer {admin_token}"}


def test_admin_creates_mints_and_uses_a_service_account_pat_over_rest(tmp_path, monkeypatch, pg_engine):
    client, admin_headers = _pg_admin_client(tmp_path, monkeypatch, pg_engine)

    r = client.post("/api/admin/service-accounts", json={"name": "CI Bot", "slug": "ci-bot"}, headers=admin_headers)
    assert r.status_code == 201, r.text
    sa = r.json()
    assert sa["email"] == "ci-bot@service.local"
    assert sa["active"] is True

    r = client.get("/api/admin/service-accounts", headers=admin_headers)
    assert r.status_code == 200, r.text
    rows = r.json()
    assert any(row["id"] == sa["id"] for row in rows)
    assert next(row for row in rows if row["id"] == sa["id"])["token_count"] == 0

    r = client.post(
        f"/api/admin/service-accounts/{sa['id']}/tokens",
        json={"name": "ci-token", "expires_in_days": 30},
        headers=admin_headers,
    )
    assert r.status_code == 201, r.text
    minted = r.json()
    assert minted["token"]

    # The token summary now reflects the mint.
    rows = client.get("/api/admin/service-accounts", headers=admin_headers).json()
    assert next(row for row in rows if row["id"] == sa["id"])["token_count"] == 1

    # The minted PAT authenticates on REST exactly like a human's.
    sa_headers = {"Authorization": f"Bearer {minted['token']}"}
    r = client.get("/auth/tokens", headers=sa_headers)
    assert r.status_code == 200, r.text
    assert len(r.json()) == 1


def test_service_account_pat_scoped_by_its_own_group_grant_over_rest(tmp_path, monkeypatch, pg_engine):
    """SA in group G with a data-package grant sees the table; without, it
    doesn't — the same RBAC surface (`GET /api/data/{id}/check-access`) a
    human PAT is judged by."""
    import uuid as _uuid

    from src.repositories import (
        data_packages_repo,
        resource_grants_repo,
        table_registry_repo,
        user_group_members_repo,
        user_groups_repo,
    )

    client, admin_headers = _pg_admin_client(tmp_path, monkeypatch, pg_engine)

    sa = client.post(
        "/api/admin/service-accounts", json={"name": "Data Bot", "slug": "data-bot"}, headers=admin_headers
    ).json()
    token = client.post(
        f"/api/admin/service-accounts/{sa['id']}/tokens", json={"name": "t"}, headers=admin_headers
    ).json()["token"]
    sa_headers = {"Authorization": f"Bearer {token}"}

    table_id = str(_uuid.uuid4())
    table_registry_repo().register(id=table_id, name=f"svc_scope_table_{table_id[:8]}", registered_by="admin1")

    # Without any grant, the SA cannot reach the table.
    r = client.get(f"/api/data/{table_id}/check-access", headers=sa_headers)
    assert r.status_code == 403, r.text

    group = user_groups_repo().create(name=f"svc-scope-group-{table_id[:8]}")
    user_group_members_repo().add_member(sa["id"], group["id"], source="admin", added_by="admin1")
    pkg_id = data_packages_repo().create(
        name="Scope pkg", slug=f"scope-pkg-{table_id[:8]}", description=None, icon=None, color=None, created_by="admin1"
    )
    data_packages_repo().add_table(pkg_id, table_id, added_by="admin1")
    # `requirement` is explicit: the PG model has no column-level default
    # (nullable, no server_default — see src/models/rbac.py::ResourceGrant),
    # and StackResolver._grants buckets strictly on the string value, so a
    # NULL requirement lands in neither the required nor the available set.
    resource_grants_repo().create(
        group_id=group["id"], resource_type="data_package", resource_id=pkg_id, requirement="available"
    )

    # With the grant (auto-membership, the default), the SA now reaches it.
    r = client.get(f"/api/data/{table_id}/check-access", headers=sa_headers)
    assert r.status_code == 204, r.text


def test_pat_authenticated_admin_cannot_mint_a_service_account_token(tmp_path, monkeypatch, pg_engine):
    """#1292: durable credentials are only ever minted from an interactive
    session — an admin holding only a PAT gets a typed 403 here too."""
    client, admin_headers = _pg_admin_client(tmp_path, monkeypatch, pg_engine)

    sa = client.post(
        "/api/admin/service-accounts", json={"name": "Locked Bot", "slug": "locked-bot"}, headers=admin_headers
    ).json()

    admin_pat = client.post("/auth/tokens", json={"name": "admin-cli"}, headers=admin_headers).json()["token"]
    admin_pat_headers = {"Authorization": f"Bearer {admin_pat}"}

    r = client.post(f"/api/admin/service-accounts/{sa['id']}/tokens", json={"name": "x"}, headers=admin_pat_headers)
    assert r.status_code == 403, r.text


def test_admin_group_add_refused_over_rest(tmp_path, monkeypatch, pg_engine):
    from src.db import SYSTEM_ADMIN_GROUP
    from src.repositories import user_groups_repo

    client, admin_headers = _pg_admin_client(tmp_path, monkeypatch, pg_engine)

    sa = client.post(
        "/api/admin/service-accounts", json={"name": "Wannabe Admin", "slug": "wannabe-admin"}, headers=admin_headers
    ).json()
    admin_group = user_groups_repo().get_by_name(SYSTEM_ADMIN_GROUP)
    assert admin_group is not None

    r = client.post(
        f"/api/admin/groups/{admin_group['id']}/members", json={"email": sa["email"]}, headers=admin_headers
    )
    assert r.status_code == 409, r.text
    assert r.json().get("error") == "service_account_admin_forbidden"


def test_deactivating_the_service_account_stops_its_pat_over_rest(tmp_path, monkeypatch, pg_engine):
    client, admin_headers = _pg_admin_client(tmp_path, monkeypatch, pg_engine)

    sa = client.post(
        "/api/admin/service-accounts", json={"name": "Kill Bot", "slug": "kill-bot"}, headers=admin_headers
    ).json()
    token = client.post(
        f"/api/admin/service-accounts/{sa['id']}/tokens", json={"name": "t"}, headers=admin_headers
    ).json()["token"]
    sa_headers = {"Authorization": f"Bearer {token}"}

    assert client.get("/auth/tokens", headers=sa_headers).status_code == 200

    r = client.patch(f"/api/admin/service-accounts/{sa['id']}", json={"active": False}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["active"] is False

    assert client.get("/auth/tokens", headers=sa_headers).status_code == 401


def test_deactivating_an_unrelated_human_leaves_the_pat_working_over_rest(tmp_path, monkeypatch, pg_engine):
    """The killer criterion, over REST this time."""
    client, admin_headers = _pg_admin_client(tmp_path, monkeypatch, pg_engine)

    sa = client.post(
        "/api/admin/service-accounts", json={"name": "Bystander Bot", "slug": "bystander-bot"}, headers=admin_headers
    ).json()
    token = client.post(
        f"/api/admin/service-accounts/{sa['id']}/tokens", json={"name": "t"}, headers=admin_headers
    ).json()["token"]
    sa_headers = {"Authorization": f"Bearer {token}"}

    r = client.post("/api/users/analyst1/deactivate", headers=admin_headers)
    assert r.status_code == 200, r.text

    assert client.get("/auth/tokens", headers=sa_headers).status_code == 200


def test_duckdb_backend_answers_typed_501_for_admin_service_account_routes(tmp_path, monkeypatch, pg_engine):
    """The DuckDB half of the A3 ratchet: every admin SA route fails clean,
    never a raw 500, on the frozen DuckDB app-state backend."""
    from tests.db_pg._parity_sweep_util import assert_pg_only_exemptions_fail_clean, build_seeded_client

    client, admin_token = build_seeded_client("duckdb", tmp_path, monkeypatch, pg_engine)
    exempt = {
        "POST /api/admin/service-accounts": "reason",
        "GET /api/admin/service-accounts": "reason",
    }
    assert_pg_only_exemptions_fail_clean(client, admin_token, exempt)

    # Path-param routes too, even though the dynamic sweep never reaches them.
    r = client.post(
        "/api/admin/service-accounts/does-not-exist/tokens",
        json={"name": "x"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 501, r.text
    assert r.json().get("error") == "requires_postgres_backend"
