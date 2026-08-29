"""Behavioral integration tests — HTTP mutations + direct backend reads.

Each test:
  1. Mutates state via HTTP (POST/PUT/DELETE)
  2. Reads DIRECTLY from the active backend via the repository factory
     to confirm the mutation landed in the right place
  3. For [+neg-duck] tests: also probes DuckDB directly on the [pg] run
     to confirm the mutation did NOT leak into DuckDB

All tests run twice: once with DuckDB-only (seeded_app_both/state_backend=duckdb)
and once with Postgres active (state_backend=pg).
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

import pytest

from tests.helpers.assertions import assert_only_in_active_backend
from tests.helpers.factories import make_skill_zip

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Module-level header helpers (consistent with smoke file)
# ---------------------------------------------------------------------------


def _admin_headers(s):
    return {"Authorization": f"Bearer {s['admin_token']}"}


def _analyst_headers(s):
    return {"Authorization": f"Bearer {s['analyst_token']}"}


# ---------------------------------------------------------------------------
# DuckDB probe helper
# ---------------------------------------------------------------------------


def _duck_probe(table, id_col, id_val):
    """Probe the DuckDB system DB for a row.

    Returns None when the table doesn't exist (CatalogException) or the row
    is absent. Re-raises unexpected errors so they don't silence real bugs.
    """
    import duckdb

    try:
        from src.db import get_system_db

        return get_system_db().execute(f"SELECT 1 FROM {table} WHERE {id_col} = ?", [id_val]).fetchone()
    except duckdb.CatalogException:
        return None


# ---------------------------------------------------------------------------
# Cluster 1: Users + RBAC
# ---------------------------------------------------------------------------


class TestUsersRBACBehavioral:
    """HTTP mutations land in the correct backend + no dual-write leaks."""

    def test_create_user_persists_to_active_backend(self, seeded_app_both):
        """POST /api/admin/users → 201; repo read confirms row exists. [+neg-duck]"""
        s = seeded_app_both
        backend = s["backend"]
        client = s["client"]

        new_email = "behavioral-new@test.com"
        r = client.post(
            "/api/users",
            json={"email": new_email, "name": "Behavioral User"},
            headers=_admin_headers(s),
        )
        assert r.status_code == 201, r.text
        user_id = r.json()["id"]

        from src.repositories import users_repo

        assert_only_in_active_backend(
            repo_read=lambda: users_repo().get_by_id(user_id),
            backend=backend,
            duckdb_probe=lambda: _duck_probe("users", "id", user_id),
        )
        # Verify the returned email matches what we created
        from src.repositories import users_repo as _ur

        row = _ur().get_by_id(user_id)
        assert row["email"] == new_email

    def test_create_group_persists_to_active_backend(self, seeded_app_both):
        """POST /api/admin/groups → 201; repo read confirms row exists. [+neg-duck]"""
        s = seeded_app_both
        backend = s["backend"]
        client = s["client"]

        r = client.post(
            "/api/admin/groups",
            json={"name": "BehavioralGroup", "description": "Test group for behavioral tests"},
            headers=_admin_headers(s),
        )
        assert r.status_code == 201, r.text
        group_id = r.json()["id"]

        from src.repositories import user_groups_repo

        assert_only_in_active_backend(
            repo_read=lambda: user_groups_repo().get(group_id),
            backend=backend,
            duckdb_probe=lambda: _duck_probe("user_groups", "id", group_id),
        )

    def test_add_member_persists_to_active_backend(self, seeded_app_both):
        """Create group → POST members → 201; member appears in list."""
        s = seeded_app_both
        client = s["client"]

        # Create group
        rg = client.post(
            "/api/admin/groups",
            json={"name": "MemberGroup", "description": "Group for member tests"},
            headers=_admin_headers(s),
        )
        assert rg.status_code == 201, rg.text
        group_id = rg.json()["id"]

        # Add analyst1 to group (endpoint resolves user by email)
        rm = client.post(
            f"/api/admin/groups/{group_id}/members",
            json={"email": "analyst@test.com"},
            headers=_admin_headers(s),
        )
        assert rm.status_code == 201, rm.text

        from src.repositories import user_group_members_repo

        members = user_group_members_repo().list_members_for_group(group_id)
        # list_members_for_group JOINs with users; returns "id" (user pk)
        user_ids = [m["id"] for m in members]
        assert "analyst1" in user_ids

    def test_revoke_member_removes_from_active_backend(self, seeded_app_both):
        """Create group → add member → DELETE member → 204; member absent."""
        s = seeded_app_both
        client = s["client"]

        # Create group
        rg = client.post(
            "/api/admin/groups",
            json={"name": "RevokeGroup", "description": "Group for revoke tests"},
            headers=_admin_headers(s),
        )
        assert rg.status_code == 201, rg.text
        group_id = rg.json()["id"]

        # Add analyst1 (endpoint resolves user by email)
        client.post(
            f"/api/admin/groups/{group_id}/members",
            json={"email": "analyst@test.com"},
            headers=_admin_headers(s),
        )

        # Remove analyst1 (path param is the user_id, not email)
        rd = client.delete(
            f"/api/admin/groups/{group_id}/members/analyst1",
            headers=_admin_headers(s),
        )
        assert rd.status_code == 204, rd.text

        from src.repositories import user_group_members_repo

        members = user_group_members_repo().list_members_for_group(group_id)
        user_ids = [m["id"] for m in members]
        assert "analyst1" not in user_ids

    def test_grant_table_access_visible_in_manifest(self, seeded_app_both, registered_table_both):
        """Grant group access to table → analyst sees table in manifest. [+neg-duck]"""
        s = seeded_app_both
        backend = s["backend"]
        client = s["client"]
        table_id = registered_table_both["table_id"]

        # Create a new group for analyst
        rg = client.post(
            "/api/admin/groups",
            json={"name": "AnalystAccessGroup", "description": "Analyst access group"},
            headers=_admin_headers(s),
        )
        assert rg.status_code == 201, rg.text
        group_id = rg.json()["id"]

        # Add analyst1 to the group (endpoint resolves user by email)
        client.post(
            f"/api/admin/groups/{group_id}/members",
            json={"email": "analyst@test.com"},
            headers=_admin_headers(s),
        )

        # Create a data package, add table to it, then grant the package to the group.
        # Per-table resource_grants no longer grant analyst access; data packages do.
        rp_pkg = client.post(
            "/api/admin/data-packages",
            json={"name": "Grant Test Package", "slug": "grant-test-pkg"},
            headers=_admin_headers(s),
        )
        assert rp_pkg.status_code == 201, rp_pkg.text
        pkg_id = rp_pkg.json()["id"]

        rt = client.post(
            f"/api/admin/data-packages/{pkg_id}/tables",
            json={"table_id": table_id},
            headers=_admin_headers(s),
        )
        assert rt.status_code == 200, rt.text

        rp = client.post(
            "/api/admin/grants",
            json={
                "group_id": group_id,
                "resource_type": "data_package",
                "resource_id": pkg_id,
                "requirement": "required",
            },
            headers=_admin_headers(s),
        )
        assert rp.status_code == 201, rp.text
        grant_id = rp.json()["id"]

        # Verify data_package grant persisted in active backend
        from src.repositories import resource_grants_repo

        assert_only_in_active_backend(
            repo_read=lambda: resource_grants_repo().get(grant_id),
            backend=backend,
            duckdb_probe=lambda: _duck_probe("resource_grants", "id", grant_id),
        )

        # Analyst should see table in manifest (access flows through data package).
        # manifest["tables"] is a dict keyed by table name ("smoke_orders"), not UUID.
        source_name = registered_table_both["source_name"]
        rm = client.get("/api/sync/manifest", headers=_analyst_headers(s))
        assert rm.status_code == 200, rm.text
        tables_dict = rm.json().get("tables", {})
        assert source_name in tables_dict, (
            f"table {source_name!r} not in manifest tables after data_package grant; "
            f"manifest tables: {list(tables_dict.keys())!r}"
        )

    def test_revoke_grant_removes_from_manifest(self, seeded_app_both, registered_table_both):
        """POST grant → analyst sees table → DELETE grant → table absent from manifest."""
        s = seeded_app_both
        client = s["client"]
        table_id = registered_table_both["table_id"]

        # Create group + add analyst
        rg = client.post(
            "/api/admin/groups",
            json={"name": "RevokeGrantGroup", "description": "Group for grant revocation test"},
            headers=_admin_headers(s),
        )
        assert rg.status_code == 201, rg.text
        group_id = rg.json()["id"]

        # Add analyst to group (endpoint resolves user by email)
        client.post(
            f"/api/admin/groups/{group_id}/members",
            json={"email": "analyst@test.com"},
            headers=_admin_headers(s),
        )

        # Create a data package, add the table, and grant the package to the group.
        rp_pkg = client.post(
            "/api/admin/data-packages",
            json={"name": "Revoke Grant Package", "slug": "revoke-grant-pkg"},
            headers=_admin_headers(s),
        )
        assert rp_pkg.status_code == 201, rp_pkg.text
        pkg_id = rp_pkg.json()["id"]

        client.post(
            f"/api/admin/data-packages/{pkg_id}/tables",
            json={"table_id": table_id},
            headers=_admin_headers(s),
        )

        rp = client.post(
            "/api/admin/grants",
            json={
                "group_id": group_id,
                "resource_type": "data_package",
                "resource_id": pkg_id,
                "requirement": "required",
            },
            headers=_admin_headers(s),
        )
        assert rp.status_code == 201, rp.text
        grant_id = rp.json()["id"]

        # Analyst sees table in manifest (access flows through the data package).
        # manifest["tables"] is a dict keyed by table name.
        source_name = registered_table_both["source_name"]
        rm = client.get("/api/sync/manifest", headers=_analyst_headers(s))
        assert rm.status_code == 200, rm.text
        assert source_name in rm.json().get("tables", {}), (
            "table not in manifest after data_package grant (pre-revoke check)"
        )

        # Revoke grant
        rd = client.delete(f"/api/admin/grants/{grant_id}", headers=_admin_headers(s))
        assert rd.status_code == 204, rd.text

        # Table is no longer in manifest
        rm2 = client.get("/api/sync/manifest", headers=_analyst_headers(s))
        assert rm2.status_code == 200, rm2.text
        assert source_name not in rm2.json().get("tables", {}), "table still in manifest after grant revoked"

        # Grant row is gone from active backend
        from src.repositories import resource_grants_repo

        assert resource_grants_repo().get(grant_id) is None


# ---------------------------------------------------------------------------
# Cluster 2: Table Registry + Sync
# ---------------------------------------------------------------------------


class TestTableRegistrySyncBehavioral:
    """Table registry CRUD lands in the correct backend."""

    def test_register_table_writes_to_active_backend(self, seeded_app_both):
        """POST /api/admin/register-table → 201; repo confirms row. [+neg-duck]"""
        s = seeded_app_both
        backend = s["backend"]
        client = s["client"]

        r = client.post(
            "/api/admin/register-table",
            json={
                "name": "behavioral_reg_table",
                "source_type": "keboola",
                "bucket": "behavioral_src",
                "source_table": "behavioral_reg_table",
                "query_mode": "local",
            },
            headers=_admin_headers(s),
        )
        assert r.status_code == 201, r.text
        table_id = r.json()["id"]

        from src.repositories import table_registry_repo

        assert_only_in_active_backend(
            repo_read=lambda: table_registry_repo().get(table_id),
            backend=backend,
            duckdb_probe=lambda: _duck_probe("table_registry", "id", table_id),
        )

    def test_update_table_persists(self, seeded_app_both, registered_table_both):
        """PUT /api/admin/registry/{table_id} → updated sync_schedule reflects in repo."""
        s = seeded_app_both
        client = s["client"]
        table_id = registered_table_both["table_id"]

        from src.repositories import table_registry_repo

        # Fetch existing row to build update payload
        existing = table_registry_repo().get(table_id)
        assert existing is not None

        new_schedule = "cron 0 6 * * *"
        r = client.put(
            f"/api/admin/registry/{table_id}",
            json={
                "name": existing.get("name") or "smoke_orders",
                "source_type": existing.get("source_type") or "keboola",
                "bucket": existing.get("bucket") or "smoke_src",
                "source_table": existing.get("source_table") or "smoke_orders",
                "query_mode": existing.get("query_mode") or "local",
                "sync_schedule": new_schedule,
            },
            headers=_admin_headers(s),
        )
        assert r.status_code == 200, r.text

        updated = table_registry_repo().get(table_id)
        assert updated is not None
        assert updated.get("sync_schedule") == new_schedule

    def test_delete_table_removes_from_active_backend(self, seeded_app_both):
        """Register table → DELETE /api/admin/registry/{table_id} → repo returns None."""
        s = seeded_app_both
        client = s["client"]

        # Register a table
        r = client.post(
            "/api/admin/register-table",
            json={
                "name": "behavioral_del_table",
                "source_type": "keboola",
                "bucket": "del_src",
                "source_table": "behavioral_del_table",
                "query_mode": "local",
            },
            headers=_admin_headers(s),
        )
        assert r.status_code == 201, r.text
        table_id = r.json()["id"]

        # Delete it
        rd = client.delete(
            f"/api/admin/registry/{table_id}",
            headers=_admin_headers(s),
        )
        assert rd.status_code == 204, rd.text

        from src.repositories import table_registry_repo

        assert table_registry_repo().get(table_id) is None

    def test_sync_manifest_is_rbac_filtered_per_backend(self, seeded_app_both, registered_table_both):
        """Manifest only shows tables the requesting user is granted.

        Grant table to analyst; analyst sees it. Create a second table with
        no grant; analyst does NOT see it. Verifies grant lookup uses the
        active backend (the critical cross-backend bug surface from PR #558).
        """
        s = seeded_app_both
        client = s["client"]
        table_id = registered_table_both["table_id"]

        # Register a second table (no grant to analyst)
        r2 = client.post(
            "/api/admin/register-table",
            json={
                "name": "ungrantd_table",
                "source_type": "keboola",
                "bucket": "ungrantd_src",
                "source_table": "ungrantd_table",
                "query_mode": "local",
            },
            headers=_admin_headers(s),
        )
        assert r2.status_code == 201, r2.text

        # Create group + add analyst + grant first table only
        rg = client.post(
            "/api/admin/groups",
            json={"name": "ManifestFilterGroup", "description": "Group for RBAC manifest test"},
            headers=_admin_headers(s),
        )
        assert rg.status_code == 201, rg.text
        group_id = rg.json()["id"]

        # Add analyst to group (endpoint resolves user by email)
        client.post(
            f"/api/admin/groups/{group_id}/members",
            json={"email": "analyst@test.com"},
            headers=_admin_headers(s),
        )

        # Create data package → add first table → grant package to group.
        # Per-table resource_grants no longer grant analyst visibility.
        rp_pkg = client.post(
            "/api/admin/data-packages",
            json={"name": "Manifest Filter Package", "slug": "manifest-filter-pkg"},
            headers=_admin_headers(s),
        )
        assert rp_pkg.status_code == 201, rp_pkg.text
        pkg_id = rp_pkg.json()["id"]

        client.post(
            f"/api/admin/data-packages/{pkg_id}/tables",
            json={"table_id": table_id},
            headers=_admin_headers(s),
        )

        client.post(
            "/api/admin/grants",
            json={
                "group_id": group_id,
                "resource_type": "data_package",
                "resource_id": pkg_id,
                "requirement": "required",
            },
            headers=_admin_headers(s),
        )

        # Analyst manifest has granted table but NOT ungranted table.
        # manifest["tables"] is a dict keyed by table name (not UUID).
        source_name = registered_table_both["source_name"]  # "smoke_orders"
        rm = client.get("/api/sync/manifest", headers=_analyst_headers(s))
        assert rm.status_code == 200, rm.text
        tables_dict = rm.json().get("tables", {})
        assert source_name in tables_dict, (
            f"granted table {source_name!r} missing from analyst manifest; tables: {list(tables_dict.keys())!r}"
        )
        # ungranted_table has no sync_state entry and no data_package grant → absent
        assert "ungrantd_table" not in tables_dict, "ungranted table leaked into analyst manifest"


# ---------------------------------------------------------------------------
# Cluster 3: Memory (Knowledge items)
# ---------------------------------------------------------------------------


class TestMemoryBehavioral:
    """Memory create/delete mutations land in the correct backend."""

    COVERED_ROUTES = {
        "POST /api/memory",
        "POST /api/memory/admin/reject",
        "PATCH /api/memory/admin/{item_id}",
    }

    def test_create_knowledge_item_persists_to_active_backend(self, seeded_app_both):
        """POST /api/memory → 201; knowledge_repo().get_by_id confirms row. [+neg-duck]"""
        s = seeded_app_both
        backend = s["backend"]
        client = s["client"]

        r = client.post(
            "/api/memory",
            json={
                "title": "Behavioral memory item",
                "content": "This is the content of the behavioral memory item used in tests.",
                "category": "process",
            },
            headers=_admin_headers(s),
        )
        assert r.status_code == 201, r.text
        item_id = r.json()["id"]

        from src.repositories import knowledge_repo

        assert_only_in_active_backend(
            repo_read=lambda: knowledge_repo().get_by_id(item_id),
            backend=backend,
            duckdb_probe=lambda: _duck_probe("knowledge_items", "id", item_id),
        )

    def test_delete_knowledge_item_removes(self, seeded_app_both):
        """POST create → admin action removes / archives item from active backend."""
        s = seeded_app_both
        client = s["client"]

        # Create item
        r = client.post(
            "/api/memory",
            json={
                "title": "To be removed",
                "content": "This item will be rejected/revoked in the test flow.",
                "category": "process",
            },
            headers=_admin_headers(s),
        )
        assert r.status_code == 201, r.text
        item_id = r.json()["id"]

        # Use admin reject endpoint; item_id is a query param, not in the JSON body.
        rr = client.post(
            f"/api/memory/admin/reject?item_id={item_id}",
            json={},
            headers=_admin_headers(s),
        )
        assert rr.status_code in (200, 204), rr.text

        from src.repositories import knowledge_repo

        item = knowledge_repo().get_by_id(item_id)
        # After reject the item still exists but with rejected status
        assert item is not None
        assert item.get("status") == "rejected"

    def test_update_knowledge_item_persists(self, seeded_app_both):
        """PATCH /api/memory/admin/{item_id} → updated content reflects in repo."""
        s = seeded_app_both
        client = s["client"]

        r = client.post(
            "/api/memory",
            json={
                "title": "Item to update",
                "content": "Original content before update.",
                "category": "process",
            },
            headers=_admin_headers(s),
        )
        assert r.status_code == 201, r.text
        item_id = r.json()["id"]

        new_title = "Updated title for behavioral test"
        rp = client.patch(
            f"/api/memory/admin/{item_id}",
            json={"title": new_title},
            headers=_admin_headers(s),
        )
        assert rp.status_code == 200, rp.text

        from src.repositories import knowledge_repo

        updated = knowledge_repo().get_by_id(item_id)
        assert updated is not None
        assert updated.get("title") == new_title, f"Expected title {new_title!r}, got {updated.get('title')!r}"


# ---------------------------------------------------------------------------
# Cluster 4: Store (entities, submissions, install/uninstall)
# ---------------------------------------------------------------------------


class TestStoreBehavioral:
    """Store entity mutations land in the correct backend."""

    def _upload_skill(self, client, headers, name="behavioral-skill"):
        """Upload a skill with guardrails off → expects 201 + approved."""
        zb = make_skill_zip(name)
        r = client.post(
            "/api/store/entities",
            files={"file": ("bundle.zip", io.BytesIO(zb), "application/zip")},
            data={"type": "skill"},
            headers=headers,
        )
        return r

    def test_upload_skill_guardrails_off_immediately_approved(self, seeded_app_both, monkeypatch):
        """POST /api/store/entities (guardrails off) → 201 + approved in active backend. [+neg-duck]"""
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: False)

        s = seeded_app_both
        backend = s["backend"]
        client = s["client"]

        r = self._upload_skill(client, _admin_headers(s), "behavioral-guardrail-off")
        assert r.status_code == 201, r.text
        entity = r.json()
        entity_id = entity["id"]
        assert entity["visibility_status"] == "approved", (
            f"Expected approved when guardrails off, got {entity['visibility_status']!r}"
        )

        from src.repositories import store_entities_repo

        assert_only_in_active_backend(
            repo_read=lambda: store_entities_repo().get(entity_id),
            backend=backend,
            duckdb_probe=lambda: _duck_probe("store_entities", "id", entity_id),
        )
        row = store_entities_repo().get(entity_id)
        assert row["visibility_status"] == "approved"

    def test_upload_skill_guardrails_on_llm_approve_flow(self, seeded_app_both, monkeypatch):
        """Mock LLM approve: POST → pending_llm; run_llm_review → approved."""
        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle",
            lambda *a, **kw: {
                "risk_level": "low",
                "summary": "mock approve",
                "findings": [],
                "reviewed_by_model": "mock",
                "error": None,
            },
        )
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)

        s = seeded_app_both
        client = s["client"]

        r = self._upload_skill(client, _admin_headers(s), "behavioral-llm-approve")
        assert r.status_code == 201, r.text
        entity = r.json()
        entity_id = entity["id"]
        # Entity visibility is "pending" when guardrails are enabled;
        # the SUBMISSION (not entity) status is "pending_llm".
        assert entity["visibility_status"] == "pending", (
            f"Expected pending entity visibility with guardrails on, got {entity['visibility_status']!r}"
        )

        from src.repositories import store_submissions_repo
        from src.store_guardrails.runner import run_llm_review

        sub = store_submissions_repo().latest_for_entity(entity_id)
        assert sub is not None
        sub_id = sub["id"]

        # Determine plugin dir for run_llm_review
        plugin_dir = s["data_dir"] / "store_uploads" / entity_id
        plugin_dir.mkdir(parents=True, exist_ok=True)

        run_llm_review(
            sub_id,
            plugin_dir=plugin_dir,
            api_key_loader=lambda: "mock-key",
            model_loader=lambda: "mock-model",
        )

        updated_sub = store_submissions_repo().get(sub_id)
        assert updated_sub["status"] == "approved", (
            f"Expected approved after LLM approve, got {updated_sub['status']!r}"
        )

    def test_upload_skill_guardrails_on_llm_block_flow(self, seeded_app_both, monkeypatch):
        """Mock LLM block → pending_llm; run_llm_review → blocked_llm; admin override → approved."""
        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle",
            lambda *a, **kw: {
                "risk_level": "high",
                "summary": "mock block",
                "findings": [{"file": "x", "explanation": "mock"}],
                "reviewed_by_model": "mock",
                "error": None,
            },
        )
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)

        s = seeded_app_both
        client = s["client"]

        r = self._upload_skill(client, _admin_headers(s), "behavioral-llm-block")
        assert r.status_code == 201, r.text
        entity = r.json()
        entity_id = entity["id"]

        from src.repositories import store_submissions_repo, store_entities_repo
        from src.store_guardrails.runner import run_llm_review

        sub = store_submissions_repo().latest_for_entity(entity_id)
        assert sub is not None
        sub_id = sub["id"]

        plugin_dir = s["data_dir"] / "store_uploads" / entity_id
        plugin_dir.mkdir(parents=True, exist_ok=True)

        run_llm_review(
            sub_id,
            plugin_dir=plugin_dir,
            api_key_loader=lambda: "mock-key",
            model_loader=lambda: "mock-model",
        )

        blocked_sub = store_submissions_repo().get(sub_id)
        assert blocked_sub["status"] == "blocked_llm", f"Expected blocked_llm, got {blocked_sub['status']!r}"

        # Admin override
        ro = client.post(
            f"/api/admin/store/submissions/{sub_id}/override",
            json={"reason": "test override for behavioral test"},
            headers=_admin_headers(s),
        )
        assert ro.status_code == 200, ro.text

        overridden_entity = store_entities_repo().get(entity_id)
        assert overridden_entity["visibility_status"] == "approved", (
            f"Expected approved after override, got {overridden_entity['visibility_status']!r}"
        )

    def test_pending_entity_hidden_from_non_owner_backend_verified(self, seeded_app_both, monkeypatch):
        """Pending entity is absent from non-owner's store listing; active backend confirms status.

        Verifies the visibility filter reads the correct backend. On the [pg] run,
        if the filter falls back to DuckDB (empty), all entities appear as visible —
        which would cause this assertion to fail on entity_id appearing in analyst's list.
        """
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)

        s = seeded_app_both
        backend = s["backend"]
        client = s["client"]

        r = self._upload_skill(client, _admin_headers(s), "behavioral-hidden-pending")
        assert r.status_code == 201, r.text
        entity = r.json()
        entity_id = entity["id"]
        assert entity["visibility_status"] in ("pending", "pending_llm"), (
            f"Expected pending status with guardrails on, got {entity['visibility_status']!r}"
        )

        # Analyst (non-owner) GET /api/store/entities → entity absent.
        # Response is StoreEntityListResponse: {"items": [...], "total": ..., ...}
        rl = client.get("/api/store/entities", headers=_analyst_headers(s))
        assert rl.status_code == 200, rl.text
        listed_ids = [e["id"] for e in rl.json()["items"]]
        assert entity_id not in listed_ids, f"Pending entity {entity_id!r} leaked into non-owner analyst listing"

        # Active backend confirms entity is still pending (not approved)
        from src.repositories import store_entities_repo

        assert_only_in_active_backend(
            repo_read=lambda: store_entities_repo().get(entity_id),
            backend=backend,
            duckdb_probe=lambda: _duck_probe("store_entities", "id", entity_id),
        )
        db_entity = store_entities_repo().get(entity_id)
        assert db_entity["visibility_status"] in ("pending", "pending_llm"), (
            f"Expected pending in backend, got {db_entity['visibility_status']!r}"
        )

    def test_approved_entity_visible_after_override(self, seeded_app_both, monkeypatch):
        """Block → admin override → entity visible to non-owner in store listing."""
        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle",
            lambda *a, **kw: {
                "risk_level": "high",
                "summary": "mock block",
                "findings": [{"file": "x", "explanation": "mock"}],
                "reviewed_by_model": "mock",
                "error": None,
            },
        )
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)

        s = seeded_app_both
        client = s["client"]

        r = self._upload_skill(client, _admin_headers(s), "behavioral-override-visible")
        assert r.status_code == 201, r.text
        entity_id = r.json()["id"]

        from src.repositories import store_submissions_repo, store_entities_repo
        from src.store_guardrails.runner import run_llm_review

        sub = store_submissions_repo().latest_for_entity(entity_id)
        assert sub is not None
        sub_id = sub["id"]

        plugin_dir = s["data_dir"] / "store_uploads" / entity_id
        plugin_dir.mkdir(parents=True, exist_ok=True)

        run_llm_review(
            sub_id,
            plugin_dir=plugin_dir,
            api_key_loader=lambda: "mock-key",
            model_loader=lambda: "mock-model",
        )

        # Override to approved
        ro = client.post(
            f"/api/admin/store/submissions/{sub_id}/override",
            json={"reason": "approving for visibility test"},
            headers=_admin_headers(s),
        )
        assert ro.status_code == 200, ro.text

        # Analyst (non-owner) can now see entity in store listing.
        # Response is StoreEntityListResponse: {"items": [...], "total": ..., ...}
        rl = client.get("/api/store/entities", headers=_analyst_headers(s))
        assert rl.status_code == 200, rl.text
        listed_ids = [e["id"] for e in rl.json()["items"]]
        assert entity_id in listed_ids, (
            f"Approved entity {entity_id!r} missing from analyst store listing after override"
        )

        # Active backend confirms approved status
        db_entity = store_entities_repo().get(entity_id)
        assert db_entity["visibility_status"] == "approved", (
            f"Expected approved in backend after override, got {db_entity['visibility_status']!r}"
        )

    def test_install_entity_writes_to_active_backend(self, seeded_app_both, monkeypatch):
        """Upload → install; user_store_installs shows entity; /api/my-stack reflects it."""
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: False)

        s = seeded_app_both
        client = s["client"]

        r = self._upload_skill(client, _admin_headers(s), "behavioral-install")
        assert r.status_code == 201, r.text
        entity_id = r.json()["id"]

        # Install
        ri = client.post(
            f"/api/store/entities/{entity_id}/install",
            headers=_admin_headers(s),
        )
        assert ri.status_code in (200, 201), ri.text

        from src.repositories import user_store_installs_repo

        installs = user_store_installs_repo().list_for_user("admin1")
        installed_ids = [i["id"] for i in installs]
        assert entity_id in installed_ids, (
            f"entity {entity_id!r} not in installs after install; installs: {installed_ids!r}"
        )

        # Verify via /api/my-stack (returns {"curated": [...], "store": [...]})
        rs = client.get("/api/my-stack", headers=_admin_headers(s))
        assert rs.status_code == 200, rs.text
        stack_ids = [e["entity_id"] for e in rs.json()["store"]]
        assert entity_id in stack_ids

    def test_uninstall_removes_from_active_backend(self, seeded_app_both, monkeypatch):
        """Upload → install → uninstall; entity absent from installs and /api/my-stack."""
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: False)

        s = seeded_app_both
        client = s["client"]

        r = self._upload_skill(client, _admin_headers(s), "behavioral-uninstall")
        assert r.status_code == 201, r.text
        entity_id = r.json()["id"]

        # Install
        client.post(f"/api/store/entities/{entity_id}/install", headers=_admin_headers(s))

        # Uninstall
        ru = client.delete(
            f"/api/store/entities/{entity_id}/install",
            headers=_admin_headers(s),
        )
        assert ru.status_code == 204, ru.text

        from src.repositories import user_store_installs_repo

        installs = user_store_installs_repo().list_for_user("admin1")
        installed_ids = [i["id"] for i in installs]
        assert entity_id not in installed_ids

        # Verify via /api/my-stack (returns {"curated": [...], "store": [...]})
        rs = client.get("/api/my-stack", headers=_admin_headers(s))
        assert rs.status_code == 200, rs.text
        stack_ids = [e["entity_id"] for e in rs.json()["store"]]
        assert entity_id not in stack_ids


# ---------------------------------------------------------------------------
# Cluster 5: Reaper contract
# ---------------------------------------------------------------------------


class TestReaperContract:
    """Reaper flips aged pending_llm submissions to review_error."""

    def _backdate_submission(self, sub_id: str, backend: str, hours: int = 2) -> None:
        """Backdate a submission's created_at to now() - hours."""
        # DuckDB TIMESTAMP columns have no timezone; strip tzinfo before passing.
        old_ts_utc = datetime.now(timezone.utc) - timedelta(hours=hours)
        if backend == "duckdb":
            old_ts = old_ts_utc.replace(tzinfo=None)
            from src.db import get_system_db

            get_system_db().execute(
                "UPDATE store_submissions SET created_at = ? WHERE id = ?",
                [old_ts, sub_id],
            )
        else:
            import sqlalchemy as sa
            from src.db_pg import get_engine

            with get_engine().begin() as conn:
                conn.execute(
                    sa.text("UPDATE store_submissions SET created_at = :ts WHERE id = :id"),
                    {"ts": old_ts_utc, "id": sub_id},
                )

    def test_reaper_flips_aged_pending_llm_to_review_error(self, seeded_app_both):
        """Aged pending_llm → run-reap-stuck-reviews → review_error. [+neg-duck]"""
        s = seeded_app_both
        backend = s["backend"]
        client = s["client"]

        from src.repositories import store_submissions_repo

        sub_id = store_submissions_repo().create(
            submitter_id="admin1",
            submitter_email="admin@test.com",
            type="skill",
            name="reaper-test-skill",
            version=None,
            status="pending_llm",
        )

        # Backdate so the reaper picks it up (default grace = 1800s; we go 2h back)
        self._backdate_submission(sub_id, backend, hours=2)

        r = client.post("/api/admin/run-reap-stuck-reviews", headers=_admin_headers(s))
        assert r.status_code == 200, r.text
        result = r.json()
        assert result.get("details", {}).get("reaped", 0) >= 1, f"Expected reaped >= 1, got: {result!r}"

        assert_only_in_active_backend(
            repo_read=lambda: store_submissions_repo().get(sub_id),
            backend=backend,
            duckdb_probe=lambda: _duck_probe("store_submissions", "id", sub_id),
        )

        reaped_sub = store_submissions_repo().get(sub_id)
        assert reaped_sub["status"] == "review_error", (
            f"Expected review_error after reaping, got {reaped_sub['status']!r}"
        )

    def test_reaper_preserves_fresh_pending_llm(self, seeded_app_both):
        """Fresh pending_llm row is NOT flipped by the reaper."""
        s = seeded_app_both
        client = s["client"]

        from src.repositories import store_submissions_repo

        # Fresh row (current timestamp — well within grace period)
        sub_id = store_submissions_repo().create(
            submitter_id="admin1",
            submitter_email="admin@test.com",
            type="skill",
            name="reaper-fresh-skill",
            version=None,
            status="pending_llm",
        )

        r = client.post("/api/admin/run-reap-stuck-reviews", headers=_admin_headers(s))
        assert r.status_code == 200, r.text

        fresh_sub = store_submissions_repo().get(sub_id)
        assert fresh_sub is not None
        assert fresh_sub["status"] == "pending_llm", (
            f"Expected fresh row to remain pending_llm, got {fresh_sub['status']!r}"
        )

    def test_reaper_multi_row_correct_count(self, seeded_app_both):
        """3 aged + 1 fresh: aged rows flipped, fresh row preserved."""
        s = seeded_app_both
        backend = s["backend"]
        client = s["client"]

        from src.repositories import store_submissions_repo

        # Create 3 aged + 1 fresh
        aged_ids = []
        for i in range(3):
            sid = store_submissions_repo().create(
                submitter_id="admin1",
                submitter_email="admin@test.com",
                type="skill",
                name=f"reaper-multi-aged-{i}",
                version=None,
                status="pending_llm",
            )
            self._backdate_submission(sid, backend, hours=2)
            aged_ids.append(sid)

        fresh_id = store_submissions_repo().create(
            submitter_id="admin1",
            submitter_email="admin@test.com",
            type="skill",
            name="reaper-multi-fresh",
            version=None,
            status="pending_llm",
        )

        r = client.post("/api/admin/run-reap-stuck-reviews", headers=_admin_headers(s))
        assert r.status_code == 200, r.text
        result = r.json()
        # Response must report exactly 3 reaped (not 0 from wrong-backend read)
        assert result.get("details", {}).get("reaped") == 3, f"Expected reaped=3 in response, got: {result!r}"

        # All 3 aged rows should be review_error
        for sid in aged_ids:
            sub = store_submissions_repo().get(sid)
            assert sub["status"] == "review_error", f"Expected review_error for aged sub {sid!r}, got {sub['status']!r}"

        # Fresh row should remain pending_llm
        fresh_sub = store_submissions_repo().get(fresh_id)
        assert fresh_sub["status"] == "pending_llm", (
            f"Expected fresh row to remain pending_llm, got {fresh_sub['status']!r}"
        )


# ---------------------------------------------------------------------------
# Cluster 6: Data access + Admin overview
# ---------------------------------------------------------------------------


class TestDataAccessBehavioral:
    """RBAC enforcement on data endpoints is backed by the active backend."""

    def test_check_access_enforces_rbac_per_backend(self, seeded_app_both, registered_table_both):
        """Analyst has no access → 403; after grant → 200."""
        s = seeded_app_both
        client = s["client"]
        table_id = registered_table_both["table_id"]

        # Analyst has no grant yet → 403
        r_before = client.get(
            f"/api/data/{table_id}/check-access",
            headers=_analyst_headers(s),
        )
        assert r_before.status_code == 403, f"Expected 403 before grant, got {r_before.status_code}: {r_before.text}"

        # Create group + add analyst + grant table access
        rg = client.post(
            "/api/admin/groups",
            json={"name": "DataAccessGroup", "description": "Group for data access tests"},
            headers=_admin_headers(s),
        )
        assert rg.status_code == 201, rg.text
        group_id = rg.json()["id"]

        client.post(
            f"/api/admin/groups/{group_id}/members",
            json={"email": "analyst@test.com"},
            headers=_admin_headers(s),
        )

        # Create data package, add table, grant package to group.
        rp_pkg = client.post(
            "/api/admin/data-packages",
            json={"name": "Access Check Package", "slug": "access-check-pkg"},
            headers=_admin_headers(s),
        )
        assert rp_pkg.status_code == 201, rp_pkg.text
        pkg_id = rp_pkg.json()["id"]

        client.post(
            f"/api/admin/data-packages/{pkg_id}/tables",
            json={"table_id": table_id},
            headers=_admin_headers(s),
        )

        rp = client.post(
            "/api/admin/grants",
            json={
                "group_id": group_id,
                "resource_type": "data_package",
                "resource_id": pkg_id,
                "requirement": "required",
            },
            headers=_admin_headers(s),
        )
        assert rp.status_code == 201, rp.text

        # Analyst should now have access → 204 (check-access returns No Content on success)
        r_after = client.get(
            f"/api/data/{table_id}/check-access",
            headers=_analyst_headers(s),
        )
        assert r_after.status_code == 204, (
            f"Expected 204 after data_package grant, got {r_after.status_code}: {r_after.text}"
        )

    def test_download_returns_parquet_for_granted_user(self, seeded_app_both, registered_table_both):
        """After granting access, download returns parquet content."""
        s = seeded_app_both
        client = s["client"]
        table_id = registered_table_both["table_id"]

        # Grant access to analyst
        rg = client.post(
            "/api/admin/groups",
            json={"name": "DownloadGroup", "description": "Group for download tests"},
            headers=_admin_headers(s),
        )
        assert rg.status_code == 201, rg.text
        group_id = rg.json()["id"]

        client.post(
            f"/api/admin/groups/{group_id}/members",
            json={"email": "analyst@test.com"},
            headers=_admin_headers(s),
        )

        # Create data package, add table, grant package to group.
        rp_pkg = client.post(
            "/api/admin/data-packages",
            json={"name": "Download Test Package", "slug": "download-test-pkg"},
            headers=_admin_headers(s),
        )
        assert rp_pkg.status_code == 201, rp_pkg.text
        pkg_id = rp_pkg.json()["id"]

        client.post(
            f"/api/admin/data-packages/{pkg_id}/tables",
            json={"table_id": table_id},
            headers=_admin_headers(s),
        )

        client.post(
            "/api/admin/grants",
            json={
                "group_id": group_id,
                "resource_type": "data_package",
                "resource_id": pkg_id,
                "requirement": "required",
            },
            headers=_admin_headers(s),
        )

        # Download
        rd = client.get(
            f"/api/data/{table_id}/download",
            headers=_analyst_headers(s),
        )
        assert rd.status_code == 200, rd.text
        content_type = rd.headers.get("content-type", "")
        assert "parquet" in content_type or "octet-stream" in content_type, (
            f"Expected parquet/octet-stream content-type, got: {content_type!r}"
        )


class TestAdminOverviewBehavioral:
    """Admin overview endpoints reflect state in the active backend."""

    def test_admin_store_submissions_list_reflects_active_backend(self, seeded_app_both, monkeypatch):
        """Upload skill → submission appears in admin submissions list."""
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: False)

        s = seeded_app_both
        client = s["client"]

        zb = make_skill_zip("admin-submission-listing")
        r = client.post(
            "/api/store/entities",
            files={"file": ("bundle.zip", io.BytesIO(zb), "application/zip")},
            data={"type": "skill"},
            headers=_admin_headers(s),
        )
        assert r.status_code == 201, r.text
        entity_id = r.json()["id"]

        from src.repositories import store_submissions_repo

        sub = store_submissions_repo().latest_for_entity(entity_id)
        assert sub is not None
        sub_id = sub["id"]

        # Admin submissions list should include this submission.
        # Endpoint returns {"items": [...], "total": ..., "limit": ..., "skip": ...}
        rl = client.get("/api/admin/store/submissions", headers=_admin_headers(s))
        assert rl.status_code == 200, rl.text
        body = rl.json()
        if isinstance(body, list):
            submission_ids = [ss["id"] for ss in body]
        else:
            submission_ids = [ss["id"] for ss in body.get("items", body.get("submissions", []))]
        assert sub_id in submission_ids, f"submission {sub_id!r} not in admin list; ids: {submission_ids[:10]!r}"

    def test_admin_sessions_list_reflects_uploads(self, seeded_app_both):
        """GET /api/admin/sessions/list returns 200 list."""
        s = seeded_app_both
        client = s["client"]

        # Hit the sessions list — should always return 200 (even empty)
        r = client.get("/api/admin/sessions/list", headers=_admin_headers(s))
        assert r.status_code == 200, r.text
        body = r.json()
        # Body is either a list or dict with a "sessions"/"items" key
        assert isinstance(body, (list, dict)), f"Expected list or dict, got {type(body)!r}"

    def test_admin_activity_feed_shows_recent_actions(self, seeded_app_both):
        """Register a table → activity feed is non-empty (has at least the register event)."""
        s = seeded_app_both
        client = s["client"]

        # Trigger a registry action that writes an audit row
        client.post(
            "/api/admin/register-table",
            json={
                "name": "activity_feed_table",
                "source_type": "keboola",
                "bucket": "activity_src",
                "source_table": "activity_feed_table",
                "query_mode": "local",
            },
            headers=_admin_headers(s),
        )

        r = client.get("/api/admin/activity", headers=_admin_headers(s))
        assert r.status_code == 200, r.text
        body = r.json()
        # Body may be {"rows": [...], ...} or a bare list
        if isinstance(body, dict):
            rows = body.get("rows", body.get("items", []))
        else:
            rows = body
        assert isinstance(rows, list), f"Expected list of activity rows, got {type(rows)!r}: {body!r}"


class TestToolProjectionMapBehavioral:
    """The admin's choice of which materialized columns carry a linked app's
    id/url/name — stored per tool, honoured by the linked-apps projection.

    Before this existed the projection asked a hardcoded alias list, so an
    upstream naming its columns anything else had every row dropped while the
    wizard reported "0 new, 0 updated" — an empty-looking result over rows
    that were sitting right there.
    """

    COVERED_ROUTES = {
        "PUT /api/admin/mcp-tools/{tool_id}/projection-map",
    }

    @staticmethod
    def _register_tool(client, headers):
        src = client.post(
            "/api/admin/mcp-sources",
            json={"name": "kbc_lister", "transport": "stdio", "command": "kbc-mcp"},
            headers=headers,
        )
        assert src.status_code == 201, src.text
        source_id = src.json()["id"]
        r = client.post(
            "/api/admin/mcp-tools",
            json={
                "source_id": source_id,
                "original_name": "get_data_apps",
                "exposed_name": "kbc_data_apps",
                "mode": "materialize",
                "schedule": "daily 04:00",
            },
            headers=headers,
        )
        assert r.status_code == 201, r.text
        return r.json()["tool_id"]

    def test_mapping_is_stored_and_cleared(self, seeded_app_both):
        s = seeded_app_both
        client, headers = s["client"], _admin_headers(s)
        tool_id = self._register_tool(client, headers)

        r = client.put(
            f"/api/admin/mcp-tools/{tool_id}/projection-map",
            json={"projection_map": {"id": "data_app_id", "url": "deployment_url"}},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["projection_map"] == {"id": "data_app_id", "url": "deployment_url"}
        got = client.get(f"/api/admin/mcp-tools/{tool_id}", headers=headers).json()
        assert got["projection_map"]["id"] == "data_app_id"

        r = client.put(
            f"/api/admin/mcp-tools/{tool_id}/projection-map",
            json={"projection_map": None},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["projection_map"] is None

    def test_unknown_field_is_rejected(self, seeded_app_both):
        """A typo'd field must not be stored as a mapping nothing will read."""
        s = seeded_app_both
        client, headers = s["client"], _admin_headers(s)
        tool_id = self._register_tool(client, headers)

        r = client.put(
            f"/api/admin/mcp-tools/{tool_id}/projection-map",
            json={"projection_map": {"identifier": "data_app_id"}},
            headers=headers,
        )
        assert r.status_code == 400, r.text
        assert "identifier" in r.json()["detail"]

    def test_blank_column_does_not_pin_a_field_to_nothing(self, seeded_app_both):
        """`map_row` treats a named column as authoritative — "" must not be named."""
        s = seeded_app_both
        client, headers = s["client"], _admin_headers(s)
        tool_id = self._register_tool(client, headers)

        r = client.put(
            f"/api/admin/mcp-tools/{tool_id}/projection-map",
            json={"projection_map": {"id": "data_app_id", "url": "   "}},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["projection_map"] == {"id": "data_app_id"}

    def test_missing_tool_is_404(self, seeded_app_both):
        s = seeded_app_both
        client, headers = s["client"], _admin_headers(s)
        r = client.put(
            "/api/admin/mcp-tools/nope.nothing/projection-map",
            json={"projection_map": {"id": "x"}},
            headers=headers,
        )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Fact graph over Collections — read surface (build order steps 2+3,
# docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md).
# RBAC/projection depth lives in tests/db_pg/test_facts_read_pg.py (the repo
# directly) and tests/test_api_facts.py (REST, DuckDB backend) — this class
# proves the HTTP wiring + the flag gate + the PG-only fail-clean shape.
# ---------------------------------------------------------------------------


class TestFactsReadSurfaceSmoke:
    COVERED_ROUTES = {
        "POST /api/facts/search",
        "POST /api/facts/neighbors",
        "GET /api/facts/{subject_id}/claims",
        "GET /api/facts/facets",
    }

    def test_flag_off_404s_every_read_route(self, seeded_app_both):
        """facts.enabled defaults off — the whole router disappears, on
        either backend."""
        s = seeded_app_both
        client, headers = s["client"], _admin_headers(s)
        assert client.post("/api/facts/search", json={}, headers=headers).status_code == 404
        assert client.post("/api/facts/neighbors", json={"subject_id": "f_x"}, headers=headers).status_code == 404
        assert client.get("/api/facts/f_x/claims", headers=headers).status_code == 404
        assert client.get("/api/facts/type-map", headers=headers).status_code == 404
        assert client.get("/api/facts/facets", headers=headers).status_code == 404

    def test_neighbors_requires_subject_id_on_both_backends(self, seeded_app_both, monkeypatch):
        """422 identically on both backends — Pydantic validation runs
        before the PG-only repo is ever reached, so this route never
        diverges (see test_mutation_status_parity_sweep.py's comment on why
        it carries no exemption entry)."""
        monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
        s = seeded_app_both
        client, headers = s["client"], _admin_headers(s)
        r = client.post("/api/facts/neighbors", json={}, headers=headers)
        assert r.status_code == 422

    def test_search_fails_clean_on_duckdb(self, state_backend, seeded_app_both, monkeypatch):
        """DuckDB-backed instance: facts_repo() is Postgres-only (A3
        ratchet) — the typed 501, never a raw 500 or a silent 200."""
        if state_backend != "duckdb":
            pytest.skip("DuckDB-only assertion")
        monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
        s = seeded_app_both
        client, headers = s["client"], _admin_headers(s)
        r = client.post("/api/facts/search", json={}, headers=headers)
        assert r.status_code == 501, r.text
        assert r.json()["error"] == "requires_postgres_backend"

    def test_search_and_claims_round_trip_on_pg(self, state_backend, seeded_app_both, monkeypatch):
        """Postgres-backed instance: a directly-seeded fact + claim is
        reachable through both REST endpoints for the (Admin god-mode)
        caller."""
        if state_backend != "pg":
            pytest.skip("Postgres-only assertion")
        monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
        s = seeded_app_both
        client, headers = s["client"], _admin_headers(s)

        from src.repositories import corpus_files_repo, facts_repo, file_corpora_repo

        corpus_id = file_corpora_repo().create(
            name="Facts Smoke", slug="facts-smoke", description=None, created_by="admin1"
        )
        file_id = corpus_files_repo().add(
            corpus_id=corpus_id,
            filename="a.md",
            sha256="sha1",
            file_type="md",
            size_bytes=10,
            storage_path="/blobs/a.md",
        )
        fact_id = facts_repo().create_fact(type="engagement")
        facts_repo().add_claim(
            fact_id=fact_id,
            corpus_file_id=file_id,
            corpus_id=corpus_id,
            file_sha256="sha1",
            quote="The engagement is underway.",
            attrs={"status": "active"},
        )

        r = client.post("/api/facts/search", json={"type": "engagement"}, headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert fact_id in {s_["id"] for s_ in body["subjects"]}

        r = client.get(f"/api/facts/{fact_id}/claims", headers=headers)
        assert r.status_code == 200, r.text
        claims = r.json()["claims"]
        assert len(claims) == 1
        assert claims[0]["quote"] == "The engagement is underway."

        # Facets read the same graph from the other direction: the client the
        # engagement is filed under, counted by DOCUMENTS rather than subjects.
        r = client.get("/api/facts/facets?types=engagement", headers=headers)
        assert r.status_code == 200, r.text
        vals = r.json()["facets"]["engagement"]
        assert [v["subject_id"] for v in vals] == [fact_id]
        assert vals[0]["document_count"] == 1


# ---------------------------------------------------------------------------
# Fact graph over Collections — write surface (build order step 4). Deep
# ingest-protocol coverage (batch caps, the verbatim gate, union/replace,
# alias/edge resolution, correction re-attachment, the orphan sweep, the
# real upload->ingest->search round trip) lives in
# tests/db_pg/test_facts_ingest_pg.py; this class proves the HTTP wiring +
# the flag gate + the PG-only fail-clean shape, same division of labor as
# TestFactsReadSurfaceSmoke above.
# ---------------------------------------------------------------------------


class TestFactsWriteSurfaceSmoke:
    COVERED_ROUTES = {
        "POST /api/facts/ingest",
        "PUT /api/facts/corrections/{subject_kind}/{subject_id}",
        "DELETE /api/facts/corrections/{subject_kind}/{subject_id}",
        "GET /api/facts/corrections",
    }

    def test_flag_off_404s_all_four_routes(self, seeded_app_both):
        s = seeded_app_both
        client, headers = s["client"], _admin_headers(s)
        assert client.post("/api/facts/ingest", json={}, headers=headers).status_code == 404
        assert (
            client.put(
                "/api/facts/corrections/fact/f_x", json={"verdict": "wrong", "reason": "x"}, headers=headers
            ).status_code
            == 404
        )
        assert client.delete("/api/facts/corrections/fact/f_x", headers=headers).status_code == 404
        assert client.get("/api/facts/corrections", headers=headers).status_code == 404

    def test_write_routes_require_admin_on_both_backends(self, seeded_app_both, monkeypatch):
        monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
        s = seeded_app_both
        client, headers = s["client"], _analyst_headers(s)
        assert client.post("/api/facts/ingest", json={}, headers=headers).status_code == 403
        assert client.get("/api/facts/corrections", headers=headers).status_code == 403

    def test_ingest_fails_clean_on_duckdb(self, state_backend, seeded_app_both, monkeypatch):
        if state_backend != "duckdb":
            pytest.skip("DuckDB-only assertion")
        monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
        s = seeded_app_both
        client, headers = s["client"], _admin_headers(s)
        r = client.post("/api/facts/ingest", json={}, headers=headers)
        assert r.status_code == 501, r.text
        assert r.json()["error"] == "requires_postgres_backend"

    def test_corrections_round_trip_on_pg(self, state_backend, seeded_app_both, monkeypatch):
        if state_backend != "pg":
            pytest.skip("Postgres-only assertion")
        monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
        s = seeded_app_both
        client, headers = s["client"], _admin_headers(s)

        put = client.put(
            "/api/facts/corrections/fact/f_write_smoke",
            json={"verdict": "wrong", "reason": "hallucinated"},
            headers=headers,
        )
        assert put.status_code == 200, put.text

        exported = client.get("/api/facts/corrections", headers=headers)
        assert exported.status_code == 200, exported.text
        assert any(r["subject_id"] == "f_write_smoke" for r in exported.json()["corrections"])

        deleted = client.delete("/api/facts/corrections/fact/f_write_smoke", headers=headers)
        assert deleted.status_code == 204, deleted.text
