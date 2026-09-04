"""PG-only tests for the SharePoint ACL-permissions snapshot (TCRD-296 gap
#79 — "SharePoint permissions captured as METADATA for every scope").

There is no DuckDB half to parametrize against (PG-first ratchet, A3) — see
``docs/migrations.md`` -> "Adding a PG-only feature". The DuckDB-side typed
501 (route reachable, ``sharepoint_state_repo()`` unavailable) lives in
``tests/test_admin_sharepoint.py::TestAclSnapshotFailsCleanOnDuckDB``.

Three layers, mirroring ``tests/db_pg/test_sharepoint_acl_phase_pg.py``:

* Storage — ``sharepoint_state_repo()`` accepts the new ``acl_snapshot:<id>``
  ``kind`` (migration ``0108_sp_acl_snapshot_kind``).
* ``run_acl_sync`` end to end — a MANUAL scope gets a snapshot captured with
  NO group/membership/grant side effect; a MIRRORED scope gets a snapshot
  captured IN ADDITION to its existing grant mirroring (unchanged).
* One full HTTP round-trip via ``build_seeded_client("pg", ...)`` — the
  route's aggregate, ``?scopes=true``, 404, RBAC (403 for a non-admin), and
  the audit row it leaves.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from connectors.sharepoint import acl_sync, graph_client

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE = "/api/admin/sharepoint/connections"


@pytest.fixture
def pg_env(tmp_path, monkeypatch, pg_engine):
    """Alembic-upgraded Postgres wired as the active backend, the
    ``sharepoint`` switch on (mirrors ``test_sharepoint_acl_phase_pg.py``'s
    own fixture of the same name)."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    monkeypatch.setenv("AGNES_SHAREPOINT_ENABLED", "true")
    monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", "unused-because-get-app-token-is-faked")

    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from tests.db_pg._parity_sweep_util import _seed_pg_system_groups

    _seed_pg_system_groups(pg_engine)
    return pg_engine


def _make_connection(connection_id: str = "conn-acl-snap-pg") -> str:
    from src.repositories import source_connections_repo

    source_connections_repo().create(
        id=connection_id,
        name="ACL Snapshot PG SharePoint",
        source_type="sharepoint",
        config={"tenant_id": "tenant-1", "client_id": "client-1", "scopes": []},
    )
    return connection_id


def _add_scope(
    connection_id: str,
    *,
    source_scope_id: str,
    collection_id: str,
    drive_id: "str | None" = "drive-1",
    access_mode: str = "manual",
) -> None:
    from src.repositories import source_connections_repo

    repo = source_connections_repo()
    row = repo.get(connection_id)
    config = dict(row.get("config") or {})
    scopes = list(config.get("scopes") or [])
    scopes.append(
        {
            "source_scope_id": source_scope_id,
            "display_path": source_scope_id,
            "anonymize": False,
            "collection_id": collection_id,
            "drive_id": drive_id,
            "access_mode": access_mode,
        }
    )
    config["scopes"] = scopes
    repo.update(connection_id, config=config)


def _make_collection(name: str) -> str:
    from src.repositories import file_corpora_repo

    return file_corpora_repo().create(
        name=name, slug=name.lower().replace(" ", "-"), description=None, created_by="test"
    )


async def _fake_get_app_token(tenant_id, client_id, private_key, *, client_secret=""):
    return "fake-token"


def _perms_fake(mapping: dict):
    async def fake(token, drive_id, item_id):
        return mapping.get((drive_id, item_id), [])

    return fake


def _user_perm(email: str) -> dict:
    return {"id": f"perm-{email}", "roles": ["read"], "grantedToV2": {"user": {"id": email, "email": email}}}


def _group_perm(oid: str) -> dict:
    return {"id": f"perm-{oid}", "roles": ["read"], "grantedToV2": {"group": {"id": oid, "displayName": "G"}}}


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def test_sharepoint_state_repo_accepts_acl_snapshot_kind(pg_env):
    from src.repositories import sharepoint_state_repo

    repo = sharepoint_state_repo()
    payload = {"source_scope_id": "s1", "principals": [], "summary": {}, "captured_at": "2026-09-04T00:00:00+00:00"}
    repo.put("conn-x", acl_sync.acl_snapshot_kind("s1"), payload)
    assert repo.get("conn-x", acl_sync.acl_snapshot_kind("s1")) == payload


# ---------------------------------------------------------------------------
# run_acl_sync — manual scope snapshot-only, mirrored scope snapshot+grants
# ---------------------------------------------------------------------------


def test_manual_scope_gets_a_snapshot_and_no_group_or_grant(pg_env, monkeypatch):
    from src.repositories import resource_grants_repo, sharepoint_state_repo, user_groups_repo

    conn_id = _make_connection()
    col_id = _make_collection("Manual Col")
    _add_scope(conn_id, source_scope_id="manual-1", collection_id=col_id, access_mode="manual")

    monkeypatch.setattr(graph_client, "get_app_token", _fake_get_app_token)
    monkeypatch.setattr(
        graph_client,
        "list_item_permissions",
        _perms_fake({("drive-1", "manual-1"): [_user_perm("alice@example.com"), _group_perm("g-1")]}),
    )

    result = acl_sync.run_acl_sync({"connection_id": conn_id})
    assert result["scopes"] == 0  # manual scopes are never counted as "synced" — no mirroring happened
    assert result["matched"] == 0 and result["unmatched"] == 0

    # No group, no grant — snapshot-only. `pg_env` seeds the system groups
    # (Admin, Everyone) up front, so the honest assertion is "no group this
    # sync itself would have created", not "the table is empty".
    assert [g for g in user_groups_repo().list_all() if g.get("created_by") == acl_sync.ACL_SYNC_SENTINEL] == []
    assert [
        g for g in resource_grants_repo().list_all(resource_type="collection") if g.get("resource_id") == col_id
    ] == []

    snap = sharepoint_state_repo().get(conn_id, acl_sync.acl_snapshot_kind("manual-1"))
    assert snap is not None
    assert snap["source_scope_id"] == "manual-1"
    kinds = {p["principal_kind"] for p in snap["principals"]}
    assert kinds == {"user", "entra_group"}
    assert snap["summary"] == {"user": 1, "entra_group": 1}
    assert snap["captured_at"]


def test_mirrored_scope_gets_a_snapshot_alongside_its_grant(pg_env, monkeypatch):
    from src.repositories import sharepoint_state_repo, users_repo

    conn_id = _make_connection("conn-acl-snap-mirrored")
    col_id = _make_collection("Mirrored Col")
    _add_scope(conn_id, source_scope_id="mirrored-1", collection_id=col_id, access_mode="mirrored")
    users_repo().create(id="u-alice", email="alice@example.com", name="Alice")

    monkeypatch.setattr(graph_client, "get_app_token", _fake_get_app_token)
    monkeypatch.setattr(
        graph_client,
        "list_item_permissions",
        _perms_fake({("drive-1", "mirrored-1"): [_user_perm("alice@example.com")]}),
    )

    result = acl_sync.run_acl_sync({"connection_id": conn_id})
    assert result["matched"] == 1  # grant mirroring still works, unchanged

    snap = sharepoint_state_repo().get(conn_id, acl_sync.acl_snapshot_kind("mirrored-1"))
    assert snap is not None
    assert snap["principals"][0]["principal_kind"] == "user"
    assert snap["principals"][0]["display_name"] == "alice@example.com"


def test_nightly_sweep_now_includes_a_manual_only_connection(pg_env, monkeypatch):
    """Before TCRD-296 gap #79, the un-targeted sweep (`connection_id=None`)
    only picked up connections with a MIRRORED scope — a manual-only
    connection was silently skipped forever. Proven here by a real
    `get_app_token` call landing (the sweep must authenticate to read
    Graph), not just by inspecting the filter function directly."""
    from src.repositories import sharepoint_state_repo

    conn_id = _make_connection("conn-manual-only-sweep")
    col_id = _make_collection("Manual Only Sweep Col")
    _add_scope(conn_id, source_scope_id="manual-sweep-1", collection_id=col_id, access_mode="manual")

    calls = []

    async def _tracking_token(tenant_id, client_id, private_key, *, client_secret=""):
        calls.append(tenant_id)
        return "fake-token"

    monkeypatch.setattr(graph_client, "get_app_token", _tracking_token)
    monkeypatch.setattr(
        graph_client,
        "list_item_permissions",
        _perms_fake({("drive-1", "manual-sweep-1"): [_user_perm("alice@example.com")]}),
    )

    result = acl_sync.run_acl_sync({})  # no connection_id -> nightly full sweep
    assert calls, "the nightly sweep must authenticate against a manual-only connection"
    assert result["connections"] == 1

    snap = sharepoint_state_repo().get(conn_id, acl_sync.acl_snapshot_kind("manual-sweep-1"))
    assert snap is not None


# ---------------------------------------------------------------------------
# HTTP round-trip
# ---------------------------------------------------------------------------


def _pg_client(tmp_path, monkeypatch, pg_engine):
    from tests.db_pg._parity_sweep_util import build_seeded_client

    return build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_connection(client, token, *, name="corp-sharepoint"):
    resp = client.post(
        "/api/admin/source-connections",
        json={"name": name, "source_type": "sharepoint", "config": {"tenant_id": "tenant-1", "client_id": "client-1"}},
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_acl_snapshot_404_for_unknown_connection(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    monkeypatch.setenv("AGNES_SHAREPOINT_ENABLED", "true")
    r = client.get(f"{BASE}/does-not-exist/acl-snapshot", headers=_auth(token))
    assert r.status_code == 404


def test_acl_snapshot_empty_aggregate_before_any_sync(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    monkeypatch.setenv("AGNES_SHAREPOINT_ENABLED", "true")
    conn_id = _create_connection(client, token)
    r = client.get(f"{BASE}/{conn_id}/acl-snapshot", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["aggregate"]["scopes_captured"] == 0
    assert "scopes" not in r.json()  # opt-in only


def test_acl_snapshot_full_round_trip_aggregate_and_scopes(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    monkeypatch.setenv("AGNES_SHAREPOINT_ENABLED", "true")
    monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", "unused-because-get-app-token-is-faked")
    conn_id = _create_connection(client, token)

    scope_resp = client.post(
        f"{BASE}/{conn_id}/scopes",
        json={"source_scope_id": "root", "display_path": "Site / Docs", "drive_id": "drive-1"},
        headers=_auth(token),
    )
    assert scope_resp.status_code == 201, scope_resp.text

    monkeypatch.setattr(graph_client, "get_app_token", _fake_get_app_token)
    monkeypatch.setattr(
        graph_client,
        "list_item_permissions",
        _perms_fake(
            {
                ("drive-1", "root"): [
                    _user_perm("bob@example.com"),
                    {"id": "l1", "roles": ["read"], "link": {"scope": "organization"}},
                ]
            }
        ),
    )
    result = acl_sync.run_acl_sync({"connection_id": conn_id})
    assert result is not None

    r = client.get(f"{BASE}/{conn_id}/acl-snapshot", headers=_auth(token))
    assert r.status_code == 200
    agg = r.json()["aggregate"]
    assert agg["scopes_captured"] == 1
    assert agg["folders_with_org_links"] == 1
    assert agg["folders_with_individual_users"] == 1
    assert agg["captured_at"]

    r2 = client.get(f"{BASE}/{conn_id}/acl-snapshot?scopes=true", headers=_auth(token))
    assert r2.status_code == 200
    scopes = r2.json()["scopes"]
    assert len(scopes) == 1
    assert scopes[0]["source_scope_id"] == "root"
    kinds = {p["principal_kind"] for p in scopes[0]["principals"]}
    assert kinds == {"user", "link_organization"}

    # Audit posture: the route's read leaves a classified row.
    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="sharepoint_connection.acl_snapshot_read", limit=10)
    assert len(rows) >= 1


def test_acl_snapshot_403_for_non_admin(tmp_path, monkeypatch, pg_engine):
    client, admin_token = _pg_client(tmp_path, monkeypatch, pg_engine)
    monkeypatch.setenv("AGNES_SHAREPOINT_ENABLED", "true")
    conn_id = _create_connection(client, admin_token)

    from app.auth.jwt import create_access_token

    analyst_token = create_access_token("analyst1", "analyst@test.com")
    r = client.get(f"{BASE}/{conn_id}/acl-snapshot", headers=_auth(analyst_token))
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Library collection detail page — "In SharePoint, this folder is visible
# to: <names>" (admin only). DuckDB degrade-clean counterpart:
# tests/test_web_library_detail_status.py::
# test_source_managed_collection_acl_snapshot_degrades_clean_on_duckdb.
# ---------------------------------------------------------------------------


def test_library_detail_shows_visible_to_line_for_admin_when_a_snapshot_exists(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    monkeypatch.setenv("AGNES_SHAREPOINT_ENABLED", "true")
    monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", "unused-because-get-app-token-is-faked")
    conn_id = _create_connection(client, token, name="lib-detail-conn")

    scope_resp = client.post(
        f"{BASE}/{conn_id}/scopes",
        json={"source_scope_id": "root", "display_path": "Site / Docs", "drive_id": "drive-1"},
        headers=_auth(token),
    )
    assert scope_resp.status_code == 201, scope_resp.text
    collection_id = scope_resp.json()["collection_id"]
    slug = client.get(f"/api/collections/{collection_id}", headers=_auth(token)).json()["slug"]

    monkeypatch.setattr(graph_client, "get_app_token", _fake_get_app_token)
    monkeypatch.setattr(
        graph_client,
        "list_item_permissions",
        _perms_fake({("drive-1", "root"): [_user_perm("carol@example.com")]}),
    )
    acl_sync.run_acl_sync({"connection_id": conn_id})

    r = client.get(f"/library/{slug}", headers=_auth(token))
    assert r.status_code == 200
    assert "In SharePoint, this folder is visible to" in r.text
    assert "carol@example.com" in r.text


def test_library_detail_omits_visible_to_line_for_a_non_admin(tmp_path, monkeypatch, pg_engine):
    """The line is admin-only — the Sharing rail fact (whatever grant the
    viewer has) is the authority for a non-admin, not SharePoint's own
    permission list."""
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    monkeypatch.setenv("AGNES_SHAREPOINT_ENABLED", "true")
    monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", "unused-because-get-app-token-is-faked")
    conn_id = _create_connection(client, token, name="lib-detail-conn-2")

    scope_resp = client.post(
        f"{BASE}/{conn_id}/scopes",
        json={"source_scope_id": "root", "display_path": "Site / Docs", "drive_id": "drive-1"},
        headers=_auth(token),
    )
    collection_id = scope_resp.json()["collection_id"]
    slug = client.get(f"/api/collections/{collection_id}", headers=_auth(token)).json()["slug"]

    monkeypatch.setattr(graph_client, "get_app_token", _fake_get_app_token)
    monkeypatch.setattr(
        graph_client, "list_item_permissions", _perms_fake({("drive-1", "root"): [_user_perm("carol@example.com")]})
    )
    acl_sync.run_acl_sync({"connection_id": conn_id})

    # Grant the analyst read access so the page itself is reachable (404
    # otherwise) — the assertion is about the ADMIN-only line, not about
    # collection visibility. "Everyone" membership is not implicit
    # (app/auth/access.py::_user_group_ids's own docstring — every
    # membership is sourced from a concrete row), so the analyst needs an
    # explicit one, not just the grant.
    from src.repositories import resource_grants_repo, user_group_members_repo, user_groups_repo

    everyone = next(g for g in user_groups_repo().list_all() if g["name"] == "Everyone")
    user_group_members_repo().add_member("analyst1", everyone["id"], source="test")
    resource_grants_repo().ensure_grant(everyone["id"], "collection", collection_id, assigned_by="test")

    from app.auth.jwt import create_access_token

    analyst_token = create_access_token("analyst1", "analyst@test.com")
    r = client.get(f"/library/{slug}", headers=_auth(analyst_token))
    assert r.status_code == 200
    assert "In SharePoint, this folder is visible to" not in r.text
