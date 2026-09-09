"""`DataAppViewerPrincipal` plumbing: the derived per-app secret, the
`owner ∩ viewer` intersection, the identity assertion, and the seams that
must admit the third principal kind.

Grants are package-shaped (``grant_table_via_package``) on purpose: the
whole reason ``compute_viewer_intersection`` is not
``compute_grant_intersection`` is that raw per-table grants no longer
surface a table to an analyst, so an intersection over them is empty on a
package-shaped deployment.
"""

from __future__ import annotations

import pytest

from src.db import SYSTEM_ADMIN_GROUP


@pytest.fixture
def conn(e2e_env):
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.users import UserRepository
    from tests.conftest import grant_table_via_package

    c = get_system_db()
    users = UserRepository(c)
    users.create(id="owner", email="owner@example.com", name="Owner")
    users.create(id="viewer", email="viewer@example.com", name="Viewer")
    users.create(id="adm", email="adm@example.com", name="Admin (no explicit grants)")
    admin_gid = c.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()[0]
    UserGroupMembersRepository(c).add_member("adm", admin_gid, source="system_seed")
    reg = TableRegistryRepository(c)
    for tid in ("t_owner_only", "t_shared", "t_viewer_only"):
        reg.register(id=tid, name=tid, source_type="keboola")
    # owner: t_owner_only + t_shared; viewer: t_shared + t_viewer_only. One
    # package per (table, group) so the SAME package id carries t_shared to
    # a group both are members of.
    grant_table_via_package(c, "t_owner_only", "owner", group_name="g-owner")
    grant_table_via_package(c, "t_viewer_only", "viewer", group_name="g-viewer")
    grant_table_via_package(c, "t_shared", "owner", group_name="g-both")
    UserGroupMembersRepository(c).add_member(
        "viewer", c.execute("SELECT id FROM user_groups WHERE name = 'g-both'").fetchone()[0], source="admin"
    )
    yield c
    c.close()


# ---------------------------------------------------------------------------
# derive_viewer_secret
# ---------------------------------------------------------------------------


def test_derive_viewer_secret_is_stable_per_slug_and_token_id(e2e_env):
    from app.auth.data_app_viewer import derive_viewer_secret

    a = derive_viewer_secret("sales", "tok-1")
    assert a == derive_viewer_secret("sales", "tok-1")
    assert len(a) == 64 and int(a, 16)  # hex sha256
    assert a != derive_viewer_secret("sales", "tok-2")  # rotates with the service token
    assert a != derive_viewer_secret("other", "tok-1")  # differs per app
    assert e2e_env  # (signing key comes from the env fixture)


def test_derive_viewer_secret_changes_with_the_server_key(monkeypatch, e2e_env):
    from app.auth import jwt as jwt_mod
    from app.auth.data_app_viewer import derive_viewer_secret

    before = derive_viewer_secret("sales", "tok-1")
    monkeypatch.setattr(jwt_mod, "get_signing_secret", lambda: "another-secret-key-of-at-least-32-chars!!")
    assert derive_viewer_secret("sales", "tok-1") != before


# ---------------------------------------------------------------------------
# compute_viewer_intersection
# ---------------------------------------------------------------------------


def test_intersection_is_owner_and_viewer_shared_tables_only(conn):
    from src.grant_intersection import compute_viewer_intersection

    inter = compute_viewer_intersection("owner", "viewer", conn)
    assert inter.get("table", frozenset()) == frozenset({"t_shared"})


def test_intersection_owner_equals_viewer_is_the_owner_reach(conn):
    from src.grant_intersection import compute_viewer_intersection

    inter = compute_viewer_intersection("owner", "owner", conn)
    assert inter["table"] == frozenset({"t_owner_only", "t_shared"})


def test_intersection_admin_viewer_without_explicit_grants_is_empty(conn):
    """SR-1: no god-mode on either side. An Admin viewer holding no explicit
    grants contributes nothing, so the app can read nothing for them."""
    from src.grant_intersection import compute_viewer_intersection

    assert compute_viewer_intersection("owner", "adm", conn).get("table", frozenset()) == frozenset()
    assert compute_viewer_intersection("adm", "owner", conn).get("table", frozenset()) == frozenset()


def test_intersection_fails_closed_on_unknown_identity(conn):
    from src.grant_intersection import compute_viewer_intersection

    assert compute_viewer_intersection("owner", "nobody", conn) == {}
    assert compute_viewer_intersection("nobody", "owner", conn) == {}
    assert compute_viewer_intersection("", "owner", conn) == {}


def test_intersection_is_package_aware_where_raw_grant_intersection_is_not(conn):
    """The reason this function exists: the co-session helper intersects RAW
    grants and sees no tables at all on a package-shaped deployment."""
    from src.grant_intersection import compute_grant_intersection, compute_viewer_intersection

    raw = compute_grant_intersection(["owner@example.com", "viewer@example.com"], conn)
    assert raw.get("table", frozenset()) == frozenset()
    assert compute_viewer_intersection("owner", "viewer", conn)["table"] == frozenset({"t_shared"})


# ---------------------------------------------------------------------------
# the principal through the RBAC seams
# ---------------------------------------------------------------------------


def _principal(intersection=None):
    from app.auth.session_principal import DataAppViewerPrincipal

    return DataAppViewerPrincipal(
        slug="sales",
        app_id="app_1",
        owner_user_id="owner",
        owner_email="owner@example.com",
        viewer_user_id="viewer",
        viewer_email="viewer@example.com",
        intersection=intersection or {"table": frozenset({"t_shared"}), "data_app": frozenset({"sales"})},
    )


def test_can_access_session_admits_the_viewer_principal():
    """The one seam that hard-coded `(SessionPrincipal, AgentPrincipal)` —
    without moving it onto `PRINCIPAL_TYPES` every `require_resource_access`
    route denied the new principal."""
    from app.auth.access import can_access_session

    p = _principal()
    assert can_access_session(p, "table", "t_shared") is True
    assert can_access_session(p, "table", "t_owner_only") is False
    assert can_access_session(p, "data_app", "sales") is True


def test_rbac_table_reads_use_the_intersection(conn):
    from src.rbac import can_access_table, get_accessible_tables

    p = _principal()
    assert can_access_table(p, "t_shared", conn) is True
    assert can_access_table(p, "t_owner_only", conn) is False
    assert "t_shared" in get_accessible_tables(p, conn)
    assert "t_owner_only" not in get_accessible_tables(p, conn)


def test_client_kind_and_identity_for_audit(conn):
    from src.audit_helpers import client_kind_from_user, identity_for_audit

    assert identity_for_audit(_principal()) == ("viewer", "viewer@example.com")
    assert client_kind_from_user(_principal()) == "web"


# ---------------------------------------------------------------------------
# assertion + groups cache
# ---------------------------------------------------------------------------


def test_mint_viewer_assertion_claims_and_group_cache(conn, monkeypatch):
    import jwt

    from app.auth import data_app_viewer as dav
    from app.auth.jwt import ALGORITHM

    dav.clear_viewer_group_cache()
    row = {"slug": "sales", "id": "app_1", "service_token_id": "tok-1"}
    user = {"id": "viewer", "email": "viewer@example.com", "name": "Viewer"}
    token = dav.mint_viewer_assertion(row, user, "session")
    claims = jwt.decode(token, dav.derive_viewer_secret("sales", "tok-1"), algorithms=[ALGORITHM], audience="data-app:sales")
    assert claims["sub"] == "viewer"
    assert claims["name"] == "Viewer"
    assert set(claims["groups"]) == {"g-viewer", "g-both"}
    assert "groups_truncated" not in claims

    # Cached: a membership change is not visible until the TTL passes.
    from src.repositories.user_group_members import UserGroupMembersRepository

    UserGroupMembersRepository(conn).add_member(
        "viewer", conn.execute("SELECT id FROM user_groups WHERE name = 'g-owner'").fetchone()[0], source="admin"
    )
    assert set(dav.viewer_groups("viewer")) == {"g-viewer", "g-both"}
    dav.clear_viewer_group_cache()
    assert set(dav.viewer_groups("viewer")) == {"g-viewer", "g-both", "g-owner"}


def test_mint_viewer_assertion_caps_groups(monkeypatch, e2e_env):
    import jwt

    from app.auth import data_app_viewer as dav
    from app.auth.jwt import ALGORITHM

    monkeypatch.setattr(dav, "viewer_groups", lambda uid: [f"g{i:04d}" for i in range(250)])
    token = dav.mint_viewer_assertion({"slug": "s", "id": "a", "service_token_id": "t"}, {"id": "u", "email": "e"}, "pat")
    claims = jwt.decode(token, dav.derive_viewer_secret("s", "t"), algorithms=[ALGORITHM], audience="data-app:s")
    assert len(claims["groups"]) == 200
    assert claims["groups_truncated"] is True


def test_no_assertion_without_a_service_token_id(e2e_env):
    from app.auth import data_app_viewer as dav

    assert dav.mint_viewer_assertion({"slug": "s", "id": "a", "service_token_id": ""}, {"id": "u", "email": "e"}, "pat") is None
    assert dav.build_viewer_headers({"slug": "s", "id": "a", "service_token_id": ""}, {"id": "u", "email": "e"}, "pat") == {}


def test_build_viewer_headers_adds_the_token_only_in_viewer_mode(conn):
    from app.auth import data_app_viewer as dav

    user = {"id": "viewer", "email": "viewer@example.com"}
    owner_mode = dav.build_viewer_headers({"slug": "s", "id": "a", "service_token_id": "t"}, user, "session")
    assert set(owner_mode) == {dav.VIEWER_HEADER}
    viewer_mode = dav.build_viewer_headers(
        {"slug": "s", "id": "a", "service_token_id": "t", "data_identity": "viewer"}, user, "session"
    )
    assert set(viewer_mode) == {dav.VIEWER_HEADER, dav.VIEWER_TOKEN_HEADER}


def test_windowed_audit_gate_debounces_and_bounds():
    from src.audit_helpers import WindowedAuditGate

    gate = WindowedAuditGate(window_s=3600, max_entries=2)
    assert gate.should_log(("u1", "s")) is True
    assert gate.should_log(("u1", "s")) is False
    assert gate.should_log(("u2", "s")) is True
    assert gate.should_log(("u3", "s")) is True  # evicts the oldest
    assert len(gate._seen) == 2
    gate.reset()
    assert gate.should_log(("u1", "s")) is True
