"""Tests for FastAPI endpoints."""

import pytest
from fastapi.testclient import TestClient


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def app_client(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")

    app = shared_app
    return TestClient(app)


@pytest.fixture
def seeded_client(tmp_path, monkeypatch, shared_app):
    """Client with a pre-created admin user and JWT token."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token

    from argon2 import PasswordHasher

    ph = PasswordHasher()

    from tests.helpers.auth import grant_admin

    conn = get_system_db()
    repo = UserRepository(conn)
    repo.create(id="admin1", email="admin@acme.com", name="Admin", password_hash=ph.hash("adminpass"))
    repo.create(id="analyst1", email="analyst@acme.com", name="Analyst", password_hash=ph.hash("analystpass"))
    grant_admin(conn, "admin1")
    conn.close()

    app = shared_app
    client = TestClient(app)

    admin_token = create_access_token("admin1", "admin@acme.com")
    analyst_token = create_access_token("analyst1", "analyst@acme.com")

    return client, admin_token, analyst_token


# ---- Health ----


class TestHealth:
    def test_health_no_auth(self, app_client):
        resp = app_client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    def test_health_detailed_requires_auth(self, app_client):
        resp = app_client.get("/api/health/detailed")
        assert resp.status_code in (401, 403)

    def test_health_detailed_has_duckdb_check(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/api/health/detailed", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("healthy", "degraded", "unhealthy")
        assert "services" in data
        assert "duckdb_state" in data["services"]
        assert data["services"]["duckdb_state"]["status"] == "ok"


# ---- Auth ----


class TestAuth:
    def test_token_for_existing_user(self, seeded_client):
        client, _, _ = seeded_client
        resp = client.post("/auth/token", json={"email": "admin@acme.com", "password": "adminpass"})
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["role"] == "admin"

    def test_token_for_unknown_user(self, seeded_client):
        client, _, _ = seeded_client
        resp = client.post("/auth/token", json={"email": "nobody@acme.com"})
        assert resp.status_code == 401

    def test_protected_endpoint_without_token(self, seeded_client):
        client, _, _ = seeded_client
        resp = client.get("/api/users")
        assert resp.status_code == 401

    def test_protected_endpoint_with_token(self, seeded_client):
        client, admin_token, _ = seeded_client
        resp = client.get("/api/users", headers={"Authorization": f"Bearer {admin_token}"})
        assert resp.status_code == 200


# ---- RBAC ----


class TestRBAC:
    def test_admin_can_list_users(self, seeded_client):
        client, admin_token, _ = seeded_client
        resp = client.get("/api/users", headers={"Authorization": f"Bearer {admin_token}"})
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_analyst_cannot_list_users(self, seeded_client):
        client, _, analyst_token = seeded_client
        resp = client.get("/api/users", headers={"Authorization": f"Bearer {analyst_token}"})
        assert resp.status_code == 403

    def test_analyst_cannot_trigger_sync(self, seeded_client):
        client, _, analyst_token = seeded_client
        resp = client.post("/api/sync/trigger", headers={"Authorization": f"Bearer {analyst_token}"})
        assert resp.status_code == 403


# ---- Sync Manifest ----


class TestSyncManifest:
    def test_manifest_returns_tables(self, seeded_client):
        client, admin_token, _ = seeded_client
        # Seed some sync state
        from src.db import get_system_db
        from src.repositories.sync_state import SyncStateRepository

        conn = get_system_db()
        repo = SyncStateRepository(conn)
        repo.update_sync(table_id="orders", rows=1000, file_size_bytes=5000, hash="abc")
        conn.close()

        resp = client.get("/api/sync/manifest", headers={"Authorization": f"Bearer {admin_token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert "tables" in data
        assert "orders" in data["tables"]
        assert data["tables"]["orders"]["rows"] == 1000


# ---- Users CRUD ----


class TestUsersCRUD:
    def test_create_user(self, seeded_client):
        client, admin_token, _ = seeded_client
        resp = client.post(
            "/api/users",
            json={"email": "new@acme.com", "name": "New User"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 201
        assert resp.json()["email"] == "new@acme.com"

    def test_create_user_joins_everyone(self, seeded_client):
        """Issue #748: POST /api/users grants the new user Everyone
        (source='system_seed') by default (AGNES_GROUP_EVERYONE_EMAIL unset)."""
        client, admin_token, _ = seeded_client
        resp = client.post(
            "/api/users",
            json={"email": "everyone-check@acme.com", "name": "New User"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 201
        new_id = resp.json()["id"]

        from src.db import get_system_db
        from src.repositories.user_group_members import UserGroupMembersRepository

        conn = get_system_db()
        try:
            rows = UserGroupMembersRepository(conn).list_groups_with_meta_for_user(new_id)
            everyone_rows = [r for r in rows if r["name"] == "Everyone"]
            assert len(everyone_rows) == 1
            assert everyone_rows[0]["source"] == "system_seed"
        finally:
            conn.close()

    def test_create_duplicate_user(self, seeded_client):
        client, admin_token, _ = seeded_client
        resp = client.post(
            "/api/users",
            json={"email": "admin@acme.com", "name": "Duplicate"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 409

    def test_create_user_stores_the_address_normalized(self, seeded_client):
        """Admin-created rows are the main source of the case-variant duplicates
        the OAuth path then has to reconcile — normalize on the way in."""
        client, admin_token, _ = seeded_client
        resp = client.post(
            "/api/users",
            json={"email": "  Mixed.Case@Acme.com  ", "name": "Mixed"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["email"] == "mixed.case@acme.com"

    def test_create_duplicate_user_is_refused_regardless_of_case(self, seeded_client):
        client, admin_token, _ = seeded_client
        resp = client.post(
            "/api/users",
            json={"email": "ADMIN@Acme.com", "name": "Duplicate"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 409, resp.text

    def test_create_user_refuses_a_non_address(self, seeded_client):
        """``normalize_email`` collapses whitespace-only input to the empty
        string, and the payload's ``email`` is a plain ``str`` — so without a
        shape check an admin could create a row whose identity is "" (or any
        non-address), which then matches nothing and breaks every lookup that
        assumes an address."""
        client, admin_token, _ = seeded_client
        for bad in ("   ", "", "not-an-address"):
            resp = client.post(
                "/api/users",
                json={"email": bad, "name": "Bad"},
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert resp.status_code == 422, f"{bad!r} → {resp.status_code}: {resp.text}"

    def test_delete_user(self, seeded_client):
        client, admin_token, _ = seeded_client
        resp = client.delete(
            "/api/users/analyst1",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 204


# ---- Knowledge / Memory ----


class TestMemory:
    def test_create_and_list(self, seeded_client):
        client, _, analyst_token = seeded_client
        headers = {"Authorization": f"Bearer {analyst_token}"}

        # Create
        resp = client.post(
            "/api/memory",
            json={
                "title": "MRR Definition",
                "content": "Monthly recurring revenue",
                "category": "metrics",
            },
            headers=headers,
        )
        assert resp.status_code == 201

        # List
        resp = client.get("/api/memory", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["count"] == 1

    def test_vote(self, seeded_client):
        client, _, analyst_token = seeded_client
        headers = {"Authorization": f"Bearer {analyst_token}"}

        resp = client.post(
            "/api/memory",
            json={
                "title": "Test",
                "content": "test",
                "category": "test",
            },
            headers=headers,
        )
        item_id = resp.json()["id"]

        resp = client.post(f"/api/memory/{item_id}/vote", json={"vote": 1}, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["upvotes"] == 1

    def test_search(self, seeded_client):
        client, _, analyst_token = seeded_client
        headers = {"Authorization": f"Bearer {analyst_token}"}

        client.post(
            "/api/memory",
            json={
                "title": "Revenue report",
                "content": "MRR trends",
                "category": "finance",
            },
            headers=headers,
        )
        client.post(
            "/api/memory",
            json={
                "title": "Support SLA",
                "content": "Response times",
                "category": "support",
            },
            headers=headers,
        )

        resp = client.get("/api/memory?search=revenue", headers=headers)
        assert resp.json()["count"] == 1


# ---- Upload ----


class TestUpload:
    def test_upload_session(self, seeded_client):
        client, _, analyst_token = seeded_client
        headers = {"Authorization": f"Bearer {analyst_token}"}
        resp = client.post(
            "/api/upload/sessions",
            files={"file": ("session.jsonl", b'{"role":"user","content":"hello"}', "application/jsonl")},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["size"] > 0

    def test_upload_local_md(self, seeded_client):
        client, _, analyst_token = seeded_client
        headers = {"Authorization": f"Bearer {analyst_token}"}
        resp = client.post(
            "/api/upload/local-md",
            json={"content": "# My knowledge\n\nMRR = Monthly Recurring Revenue"},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["user"] == "analyst@acme.com"


# ---- Metrics API ----

SAMPLE_METRIC = {
    "id": "finance/mrr",
    "name": "mrr",
    "display_name": "Monthly Recurring Revenue",
    "category": "finance",
    "sql": "SELECT SUM(amount) FROM subscriptions WHERE active = true",
}


class TestMetricsAPI:
    def test_list_metrics_empty(self, seeded_client):
        client, admin_token, _ = seeded_client
        headers = {"Authorization": f"Bearer {admin_token}"}
        resp = client.get("/api/metrics", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        assert data["metrics"] == []

    def test_create_and_list_metric(self, seeded_client):
        client, admin_token, _ = seeded_client
        headers = {"Authorization": f"Bearer {admin_token}"}

        resp = client.post("/api/admin/metrics", json=SAMPLE_METRIC, headers=headers)
        assert resp.status_code == 201

        resp = client.get("/api/metrics", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["metrics"][0]["id"] == "finance/mrr"

    def test_get_metric_detail(self, seeded_client):
        client, admin_token, _ = seeded_client
        headers = {"Authorization": f"Bearer {admin_token}"}

        client.post("/api/admin/metrics", json=SAMPLE_METRIC, headers=headers)

        resp = client.get("/api/metrics/finance/mrr", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "finance/mrr"
        assert data["display_name"] == "Monthly Recurring Revenue"
        assert data["category"] == "finance"

    def test_get_metric_not_found(self, seeded_client):
        client, admin_token, _ = seeded_client
        headers = {"Authorization": f"Bearer {admin_token}"}

        resp = client.get("/api/metrics/nonexistent/metric", headers=headers)
        assert resp.status_code == 404

    def test_delete_metric(self, seeded_client):
        client, admin_token, _ = seeded_client
        headers = {"Authorization": f"Bearer {admin_token}"}

        client.post("/api/admin/metrics", json=SAMPLE_METRIC, headers=headers)

        resp = client.delete("/api/admin/metrics/finance/mrr", headers=headers)
        assert resp.status_code == 204

        resp = client.get("/api/metrics/finance/mrr", headers=headers)
        assert resp.status_code == 404

    def test_analyst_cannot_create_metric(self, seeded_client):
        client, _, analyst_token = seeded_client
        headers = {"Authorization": f"Bearer {analyst_token}"}

        resp = client.post("/api/admin/metrics", json=SAMPLE_METRIC, headers=headers)
        assert resp.status_code == 403

    def test_list_metrics_filter_by_category(self, seeded_client):
        client, admin_token, _ = seeded_client
        headers = {"Authorization": f"Bearer {admin_token}"}

        finance_metric = {**SAMPLE_METRIC}
        support_metric = {
            "id": "support/tickets",
            "name": "tickets",
            "display_name": "Open Tickets",
            "category": "support",
            "sql": "SELECT COUNT(*) FROM tickets WHERE status = 'open'",
        }
        client.post("/api/admin/metrics", json=finance_metric, headers=headers)
        client.post("/api/admin/metrics", json=support_metric, headers=headers)

        resp = client.get("/api/metrics?category=finance", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["metrics"][0]["category"] == "finance"

    def test_import_metrics_yaml(self, seeded_client):
        client, admin_token, _ = seeded_client
        yaml_content = b"- name: test_metric\n  display_name: Test\n  category: test\n  sql: SELECT 1\n"
        resp = client.post(
            "/api/admin/metrics/import",
            files={"file": ("test.yml", yaml_content, "application/x-yaml")},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["count"] == 1


class TestMetricsRBAC:
    """A metric's `table_name`/`tables` can reveal schema/business info about
    a table the caller has no Data Package grant on — metric reads must be
    gated the same way table reads are (#953 security fix)."""

    def _register_table(self, table_id: str, table_name: str):
        from src.repositories import table_registry_repo

        table_registry_repo().register(
            id=table_id,
            name=table_name,
            description="test table",
            source_type="keboola",
            query_mode="materialized",
        )

    def _grant(self, table_id: str, user_id: str = "analyst1"):
        from src.db import get_system_db
        from tests.conftest import grant_table_via_package

        conn = get_system_db()
        grant_table_via_package(conn, table_id, user_id)
        conn.close()

    def test_analyst_without_grant_cannot_list_metric(self, seeded_client):
        client, admin_token, analyst_token = seeded_client
        self._register_table("orders_tbl", "orders_tbl")
        metric = {**SAMPLE_METRIC, "id": "finance/orders_total", "name": "orders_total", "table_name": "orders_tbl"}
        client.post("/api/admin/metrics", json=metric, headers=_auth(admin_token))

        resp = client.get("/api/metrics", headers=_auth(analyst_token))
        assert resp.status_code == 200
        ids = {m["id"] for m in resp.json()["metrics"]}
        assert "finance/orders_total" not in ids

    def test_analyst_without_grant_get_metric_returns_403(self, seeded_client):
        client, admin_token, analyst_token = seeded_client
        self._register_table("orders_tbl2", "orders_tbl2")
        metric = {**SAMPLE_METRIC, "id": "finance/orders_total2", "name": "orders_total2", "table_name": "orders_tbl2"}
        client.post("/api/admin/metrics", json=metric, headers=_auth(admin_token))

        resp = client.get("/api/metrics/finance/orders_total2", headers=_auth(analyst_token))
        assert resp.status_code == 403
        assert "orders_tbl2" in resp.json()["detail"]

    def test_analyst_with_grant_sees_metric(self, seeded_client):
        client, admin_token, analyst_token = seeded_client
        self._register_table("orders_tbl3", "orders_tbl3")
        self._grant("orders_tbl3")
        metric = {**SAMPLE_METRIC, "id": "finance/orders_total3", "name": "orders_total3", "table_name": "orders_tbl3"}
        client.post("/api/admin/metrics", json=metric, headers=_auth(admin_token))

        resp = client.get("/api/metrics", headers=_auth(analyst_token))
        assert resp.status_code == 200
        ids = {m["id"] for m in resp.json()["metrics"]}
        assert "finance/orders_total3" in ids

        resp = client.get("/api/metrics/finance/orders_total3", headers=_auth(analyst_token))
        assert resp.status_code == 200
        assert resp.json()["id"] == "finance/orders_total3"

    def test_admin_sees_metrics_regardless_of_stack(self, seeded_client):
        client, admin_token, _ = seeded_client
        self._register_table("orders_tbl4", "orders_tbl4")
        metric = {**SAMPLE_METRIC, "id": "finance/orders_total4", "name": "orders_total4", "table_name": "orders_tbl4"}
        client.post("/api/admin/metrics", json=metric, headers=_auth(admin_token))

        resp = client.get("/api/metrics", headers=_auth(admin_token))
        assert resp.status_code == 200
        ids = {m["id"] for m in resp.json()["metrics"]}
        assert "finance/orders_total4" in ids

        resp = client.get("/api/metrics/finance/orders_total4", headers=_auth(admin_token))
        assert resp.status_code == 200

    def test_metric_with_no_table_reference_is_always_visible(self, seeded_client):
        """SAMPLE_METRIC sets no table_name/tables — nothing to gate."""
        client, admin_token, analyst_token = seeded_client
        client.post("/api/admin/metrics", json=SAMPLE_METRIC, headers=_auth(admin_token))

        resp = client.get("/api/metrics", headers=_auth(analyst_token))
        assert resp.status_code == 200
        ids = {m["id"] for m in resp.json()["metrics"]}
        assert "finance/mrr" in ids

    def test_join_metric_hidden_unless_all_tables_accessible(self, seeded_client):
        client, admin_token, analyst_token = seeded_client
        self._register_table("orders_a", "orders_a")
        self._register_table("orders_b", "orders_b")
        metric = {
            **SAMPLE_METRIC,
            "id": "finance/join_metric",
            "name": "join_metric",
            "table_name": None,
            "tables": ["orders_a", "orders_b"],
        }
        client.post("/api/admin/metrics", json=metric, headers=_auth(admin_token))

        # No access to either table.
        resp = client.get("/api/metrics", headers=_auth(analyst_token))
        assert "finance/join_metric" not in {m["id"] for m in resp.json()["metrics"]}

        # Access to only one of the two tables — still hidden.
        self._grant("orders_a")
        resp = client.get("/api/metrics", headers=_auth(analyst_token))
        assert "finance/join_metric" not in {m["id"] for m in resp.json()["metrics"]}

        # Access to both — now visible.
        self._grant("orders_b")
        resp = client.get("/api/metrics", headers=_auth(analyst_token))
        assert "finance/join_metric" in {m["id"] for m in resp.json()["metrics"]}

        resp = client.get("/api/metrics/finance/join_metric", headers=_auth(analyst_token))
        assert resp.status_code == 200

    def test_table_name_resolved_via_registry_not_used_as_id_directly(self, seeded_client):
        """`table_name` stores the table_registry NAME, not the id — these can
        differ (e.g. name 'Orders EU' slugifies to id 'orders_eu'). The gate
        must resolve via `get_by_name` before checking access, not treat the
        stored `table_name` as if it were the id."""
        client, admin_token, analyst_token = seeded_client
        self._register_table("orders_eu", "Orders EU")
        self._grant("orders_eu")
        metric = {
            **SAMPLE_METRIC,
            "id": "finance/orders_eu_total",
            "name": "orders_eu_total",
            "table_name": "Orders EU",
        }
        client.post("/api/admin/metrics", json=metric, headers=_auth(admin_token))

        resp = client.get("/api/metrics", headers=_auth(analyst_token))
        assert resp.status_code == 200
        ids = {m["id"] for m in resp.json()["metrics"]}
        assert "finance/orders_eu_total" in ids

        resp = client.get("/api/metrics/finance/orders_eu_total", headers=_auth(analyst_token))
        assert resp.status_code == 200

    def test_glossary_endpoints_unaffected_by_table_stack(self, seeded_client):
        """Regression guard: glossary terms are business vocabulary, not
        table-derived data — must stay visible with zero table grants."""
        client, admin_token, analyst_token = seeded_client
        from src.db import get_system_db
        from src.repositories.glossary import GlossaryRepository

        conn = get_system_db()
        GlossaryRepository(conn).create(id="mrr", term="MRR", definition="Monthly Recurring Revenue")
        conn.close()

        resp = client.get("/api/glossary", headers=_auth(analyst_token))
        assert resp.status_code == 200
        terms = {t["id"] for t in resp.json()["terms"]}
        assert "mrr" in terms


class TestMetadataAPI:
    def test_get_metadata_empty(self, seeded_client):
        client, admin_token, _ = seeded_client
        resp = client.get("/api/admin/metadata/orders", headers={"Authorization": f"Bearer {admin_token}"})
        assert resp.status_code == 200
        assert resp.json()["columns"] == []

    def test_save_and_get_metadata(self, seeded_client):
        client, admin_token, _ = seeded_client
        resp = client.post(
            "/api/admin/metadata/orders",
            json={
                "columns": [
                    {"column_name": "id", "basetype": "STRING", "description": "Order ID"},
                    {"column_name": "total", "basetype": "NUMERIC", "description": "Total"},
                ]
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert resp.json()["count"] == 2

        resp = client.get("/api/admin/metadata/orders", headers={"Authorization": f"Bearer {admin_token}"})
        assert resp.status_code == 200
        assert len(resp.json()["columns"]) == 2

    def test_analyst_cannot_save_metadata(self, seeded_client):
        client, _, analyst_token = seeded_client
        resp = client.post(
            "/api/admin/metadata/orders",
            json={"columns": [{"column_name": "id", "basetype": "STRING"}]},
            headers={"Authorization": f"Bearer {analyst_token}"},
        )
        assert resp.status_code == 403

    def test_push_non_keboola_table_fails(self, seeded_client):
        client, admin_token, _ = seeded_client
        resp = client.post(
            "/api/admin/metadata/orders/push",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        # 'orders' is not in table_registry — expect 404 or 400
        assert resp.status_code in (400, 404)

    def test_push_keboola_table(self, seeded_client, monkeypatch):  # noqa: F811
        client, admin_token, _ = seeded_client

        # 1. Register a keboola table
        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository

        conn = get_system_db()
        TableRegistryRepository(conn).register(
            id="kbc_orders",
            name="Orders",
            source_type="keboola",
            source_table="in.c-main.orders",
        )
        conn.close()

        # 2. Save column metadata
        client.post(
            "/api/admin/metadata/kbc_orders",
            json={
                "columns": [
                    {"column_name": "id", "basetype": "STRING", "description": "Order ID"},
                ]
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )

        # 3. Set required env vars
        monkeypatch.setenv("KBC_STACK_URL", "https://connection.keboola.com")
        monkeypatch.setenv("KBC_STORAGE_TOKEN", "test-token")

        # 4. Mock httpx.AsyncClient so no real HTTP call is made
        import httpx as _httpx
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_response = _httpx.Response(200, json={"ok": True})
        mock_post = AsyncMock(return_value=mock_response)

        # AsyncClient is used as "async with httpx.AsyncClient() as client: await client.post(...)"
        # We patch the class so the context-manager instance has our mock_post.
        mock_async_client_instance = MagicMock()
        mock_async_client_instance.post = mock_post
        mock_async_client_instance.__aenter__ = AsyncMock(return_value=mock_async_client_instance)
        mock_async_client_instance.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_async_client_instance):
            resp = client.post(
                "/api/admin/metadata/kbc_orders/push",
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        # 5. Assertions
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("pushed") == 1

        mock_post.assert_called_once()
        call_args = mock_post.call_args
        # Verify URL contains the source table name
        called_url = call_args[0][0] if call_args[0] else call_args.kwargs.get("url", "")
        assert "in.c-main.orders" in called_url
        # Verify auth header
        called_headers = call_args.kwargs.get("headers", {})
        assert called_headers.get("X-StorageApi-Token") == "test-token"
        # Verify payload structure
        called_json = call_args.kwargs.get("json", {})
        assert called_json.get("provider") == "ai-metadata-enrichment"
        assert isinstance(called_json.get("metadata"), list)


# ---- Hybrid Query ----


class TestHybridQueryAPI:
    def test_hybrid_query_requires_admin(self, seeded_client):
        client, _, analyst_token = seeded_client
        resp = client.post(
            "/api/query/hybrid",
            json={"sql": "SELECT 1 AS val", "register_bq": {}},
            headers={"Authorization": f"Bearer {analyst_token}"},
        )
        assert resp.status_code == 403

    def test_hybrid_query_local_only(self, seeded_client):
        client, admin_token, _ = seeded_client
        resp = client.post(
            "/api/query/hybrid",
            json={"sql": "SELECT 1 AS val", "register_bq": {}},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "columns" in data
        assert "rows" in data
        assert data["columns"] == ["val"]
        assert data["rows"] == [[1]]

    def test_hybrid_query_blocked_sql(self, seeded_client):
        client, admin_token, _ = seeded_client
        resp = client.post(
            "/api/query/hybrid",
            json={"sql": "DROP TABLE users", "register_bq": {}},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 400
        assert "query_error" in resp.json()["detail"]

    def test_hybrid_query_blocked_bq_sql(self, seeded_client):
        client, admin_token, _ = seeded_client
        resp = client.post(
            "/api/query/hybrid",
            json={
                "sql": "SELECT 1",
                "register_bq": {"bad_alias": "DROP TABLE sensitive"},
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 400
        assert "query_error" in resp.json()["detail"]
