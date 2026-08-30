"""Smoke tests — happy-path HTTP status + response shape for every endpoint group.

Each test class declares COVERED_ROUTES consumed by the route-coverage guard at
the bottom of this file. Depth: HTTP status code + top-level response shape only.
All tests run twice via seeded_app_both (DuckDB-only + Postgres).
"""

from __future__ import annotations

import pytest

from tests.helpers.factories import (
    make_agent_zip,
    make_bad_desc_zip,
    make_no_name_zip,
    make_plugin_zip,
    make_security_fail_zip,
    make_skill_zip,
)

pytestmark = pytest.mark.integration


def _admin_headers(s):
    return {"Authorization": f"Bearer {s['admin_token']}"}


def _analyst_headers(s):
    return {"Authorization": f"Bearer {s['analyst_token']}"}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class TestAuthSmoke:
    COVERED_ROUTES = {
        "POST /auth/token",
        "POST /auth/bootstrap",
        "POST /auth/password/login",
    }

    def test_bootstrap_returns_403_after_seeding(self, seeded_app_both):
        """Bootstrap window is closed once a user with a password exists."""
        from argon2 import PasswordHasher

        from src.repositories import users_repo

        users_repo().update(id="admin1", password_hash=PasswordHasher().hash("admin-pass"))
        r = seeded_app_both["client"].post(
            "/auth/bootstrap",
            json={
                "email": "new@test.com",
                "password": "newpass123",
                "name": "New",
            },
        )
        assert r.status_code in (403, 409), r.text

    def test_password_login_returns_token(self, seeded_app_both):
        """Password login endpoint is reachable (401 expected — no password set)."""
        r = seeded_app_both["client"].post(
            "/auth/password/login",
            json={
                "email": "admin@test.com",
                "password": "wrong",
            },
        )
        assert r.status_code == 401, r.text

    def test_token_with_password_user(self, seeded_app_both):
        """POST /auth/token returns 200 + access_token for a user with a password_hash."""
        from argon2 import PasswordHasher

        from src.repositories import users_repo

        ph = PasswordHasher()
        users_repo().create(
            id="pw-user1",
            email="pw@test.com",
            name="PwUser",
            password_hash=ph.hash("test-password"),
        )
        r = seeded_app_both["client"].post(
            "/auth/token",
            json={
                "email": "pw@test.com",
                "password": "test-password",
            },
        )
        assert r.status_code == 200, r.text
        assert "access_token" in r.json()


class TestKeboolaLoginProjectsSmoke:
    """Select-mode Keboola project import surface. Depth per this file's
    contract: status + top-level shape. The default mode is ``disabled``,
    so the GET answers an empty discovery and the POST refuses with the
    mode conflict — deterministic on both backends with no Keboola config."""

    COVERED_ROUTES = {
        "GET /api/auth/keboola/projects",
        "POST /api/auth/keboola/projects",
    }

    def test_projects_listing_default_mode(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/auth/keboola/projects", headers=_analyst_headers(seeded_app_both))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["mode"] == "disabled"
        assert body["discovery_available"] is False
        assert body["projects"] == []

    def test_projects_listing_requires_auth(self, seeded_app_both):
        assert seeded_app_both["client"].get("/api/auth/keboola/projects").status_code == 401

    def test_import_outside_select_mode_conflicts(self, seeded_app_both):
        r = seeded_app_both["client"].post(
            "/api/auth/keboola/projects",
            json={"project_ids": ["1"]},
            headers=_analyst_headers(seeded_app_both),
        )
        assert r.status_code == 409, r.text


class TestCliAuthRescopeSmoke:
    """v106 — the `agnes init --as-admin` opt-up endpoint."""

    COVERED_ROUTES = {
        "POST /cli/auth/rescope-surface",
    }

    @staticmethod
    def _mint_pat(user_id: str, email: str, *, surface: str = "stack") -> str:
        import hashlib
        import uuid

        from app.auth.jwt import create_access_token
        from src.repositories import access_token_repo

        tid = str(uuid.uuid4())
        jwt = create_access_token(user_id=user_id, email=email, token_id=tid, typ="pat", omit_exp=True)
        access_token_repo().create(
            id=tid,
            user_id=user_id,
            name="smoke-rescope",
            token_hash=hashlib.sha256(jwt.encode()).hexdigest(),
            prefix=tid[:8],
            surface=surface,
        )
        return jwt

    def test_admin_pat_rescopes_to_full_surface(self, seeded_app_both):
        pat = self._mint_pat("admin1", "admin@test.com", surface="stack")
        r = seeded_app_both["client"].post(
            "/cli/auth/rescope-surface",
            headers={"Authorization": f"Bearer {pat}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["surface"] == "all"
        assert body["token"]
        # The minted row must be surface='all' (auditable in /auth/tokens).
        from app.auth.jwt import verify_token
        from src.repositories import access_token_repo

        jti = (verify_token(body["token"]) or {}).get("jti")
        assert access_token_repo().get_by_id(jti)["surface"] == "all"

    def test_analyst_pat_denied(self, seeded_app_both):
        pat = self._mint_pat("analyst1", "analyst@test.com", surface="stack")
        r = seeded_app_both["client"].post(
            "/cli/auth/rescope-surface",
            headers={"Authorization": f"Bearer {pat}"},
        )
        assert r.status_code == 403, r.text

    def test_session_token_denied(self, seeded_app_both):
        # Admin SESSION credential must be refused — rescope is PAT-only.
        r = seeded_app_both["client"].post(
            "/cli/auth/rescope-surface",
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# Health / Version
# ---------------------------------------------------------------------------


class TestHealthSmoke:
    COVERED_ROUTES = {
        "GET /api/health",
        "GET /api/health/detailed",
        "GET /api/version",
    }

    def test_health(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/health")
        assert r.status_code == 200

    def test_health_detailed(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/health/detailed", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200
        body = r.json()
        assert "status" in body
        assert "services" in body

    def test_version(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/version")
        assert r.status_code == 200
        assert "version" in r.json()


# ---------------------------------------------------------------------------
# Health probes (LB liveness/readiness — unauthenticated, app/api/health_probes.py)
# ---------------------------------------------------------------------------


class TestHealthProbesSmoke:
    COVERED_ROUTES = {
        "GET /healthz",
        "GET /readyz",
    }

    def test_healthz(self, seeded_app_both):
        r = seeded_app_both["client"].get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "alive"}

    def test_readyz(self, seeded_app_both):
        # The write-canary runs on a background timer, not per request, so a
        # fresh app either hasn't run it yet (ReadinessState defaults to
        # ready) or has recorded canary results already — either way the
        # only valid outcomes are 200 (ready) or 503 (not ready).
        r = seeded_app_both["client"].get("/readyz")
        assert r.status_code in (200, 503), r.text
        body = r.json()
        assert body["status"] in ("ready", "not_ready")
        assert "failed_checks" in body
        assert "canary_ready" in body

    def test_canary_write_path(self, seeded_app_both):
        # /readyz's 200-or-503 assertion above tolerates a permanently failing
        # canary, so it can't catch a broken write path on its own. Call the
        # canary directly against the active backend (DuckDB or Postgres, per
        # seeded_app_both) to assert the write genuinely succeeds — otherwise
        # a regression here would ship a /readyz that's always 503 in prod
        # while this suite stays green.
        from app.api.health_probes import _write_canary

        assert _write_canary() is True


# ---------------------------------------------------------------------------
# Metrics (Prometheus scrape endpoint — unauthenticated, app/observability/metrics.py)
# ---------------------------------------------------------------------------


class TestMetricsProbeSmoke:
    COVERED_ROUTES = {
        "GET /metrics",
    }

    def test_metrics(self, seeded_app_both):
        r = seeded_app_both["client"].get("/metrics")
        assert r.status_code == 200
        assert "text/plain" in r.headers["content-type"]
        assert "agnes_http_requests_total" in r.text


# ---------------------------------------------------------------------------
# Me
# ---------------------------------------------------------------------------


class TestMeSmoke:
    COVERED_ROUTES = {
        "GET /api/me/home-stats",
        "GET /api/me/effective-access",
        "POST /api/me/onboarded",
        "POST /api/me/elevation",
    }

    def test_me_home_stats(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/me/home-stats", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_me_effective_access(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/me/effective-access", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200
        assert "items" in r.json()

    def test_me_onboarded(self, seeded_app_both):
        r = seeded_app_both["client"].post("/api/me/onboarded", headers=_admin_headers(seeded_app_both))
        assert r.status_code in (200, 204)

    def test_me_elevation_toggle(self, seeded_app_both):
        c = seeded_app_both["client"]
        r = c.post(
            "/api/me/elevation",
            json={"paused": True},
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code == 200
        assert r.json()["paused"] is True
        # resume so later smoke classes see god-mode admin behavior
        r = c.post(
            "/api/me/elevation",
            json={"paused": False},
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code == 200
        c.cookies.delete("agnes_elevation")


# ---------------------------------------------------------------------------
# Me Stats  (DuckDB analytics side — should return 200 even in PG mode)
# ---------------------------------------------------------------------------


class TestMeStatsSmoke:
    COVERED_ROUTES = {
        "GET /api/me/stats/sessions",
        "GET /api/me/stats/tokens",
        "GET /api/me/stats/queries",
        "GET /api/me/stats/sync",
    }

    def test_stats_sessions(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/me/stats/sessions", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_stats_tokens(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/me/stats/tokens", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_stats_queries(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/me/stats/queries", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_stats_sync(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/me/stats/sync", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


class TestUsersSmoke:
    COVERED_ROUTES = {
        "GET /api/users",
        "GET /api/users/{user_id}",
        "POST /api/users",
        "PATCH /api/users/{user_id}",
        "DELETE /api/users/{user_id}",
        "POST /api/users/{user_id}/reset-password",
        "POST /api/users/{user_id}/set-password",
        "POST /api/users/{user_id}/deactivate",
        "POST /api/users/{user_id}/activate",
    }

    def test_list_users(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/users", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_get_user(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/users/admin1", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200
        assert "email" in r.json()


# ---------------------------------------------------------------------------
# RBAC (groups + grants + access-overview)
# ---------------------------------------------------------------------------


class TestRBACSmoke:
    COVERED_ROUTES = {
        "GET /api/admin/groups",
        "GET /api/admin/groups/{group_id}",
        "POST /api/admin/groups",
        "PATCH /api/admin/groups/{group_id}",
        "DELETE /api/admin/groups/{group_id}",
        "GET /api/admin/groups/{group_id}/members",
        "POST /api/admin/groups/{group_id}/members",
        "DELETE /api/admin/groups/{group_id}/members/{user_id}",
        "GET /api/admin/grants",
        "POST /api/admin/grants",
        "PUT /api/admin/grants/{grant_id}",
        "DELETE /api/admin/grants/{grant_id}",
        "GET /api/admin/access-overview",
        "GET /api/admin/resource-types",
        "GET /api/admin/activity",
        "GET /api/admin/activity/health",
        "GET /api/admin/activity/sync",
    }

    def test_list_groups(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/groups", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_access_overview(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/access-overview", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_grants_list(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/grants", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_resource_types(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/resource-types", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_activity(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/activity", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_activity_health(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/activity/health", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_activity_sync(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/activity/sync", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Admin Simulate — Library preview
# ---------------------------------------------------------------------------


class TestAdminLibraryPreviewSmoke:
    COVERED_ROUTES = {
        "GET /api/admin/users/{user_id}/library-preview",
    }

    def test_library_preview_for_admin(self, seeded_app_both):
        """200 + the {mode, sections} envelope. A freshly seeded analyst has no
        grants, so sections is legitimately empty — the shape is what this
        asserts; the projection itself is StackResolver.browse, covered where
        the resolver is."""
        r = seeded_app_both["client"].get(
            "/api/admin/users/analyst1/library-preview",
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["mode"] in ("auto", "classic")
        assert isinstance(body["sections"], list)

    def test_library_preview_denied_for_non_admin(self, seeded_app_both):
        """Simulate is an admin lens on someone else's Library — 403 even when
        the caller asks about themselves."""
        r = seeded_app_both["client"].get(
            "/api/admin/users/analyst1/library-preview",
            headers=_analyst_headers(seeded_app_both),
        )
        assert r.status_code == 403, r.text

    def test_library_preview_unknown_user_404(self, seeded_app_both):
        r = seeded_app_both["client"].get(
            "/api/admin/users/nope-does-not-exist/library-preview",
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


class TestSyncSmoke:
    COVERED_ROUTES = {
        "GET /api/sync/status",
        "GET /api/sync/manifest",
        "POST /api/sync/trigger",
        "GET /api/sync/settings",
        "GET /api/sync/table-subscriptions",
    }

    def test_sync_status(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/sync/status", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_sync_manifest(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/sync/manifest", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200
        assert "tables" in r.json()

    def test_sync_trigger(self, seeded_app_both):
        # Enqueues a `data-refresh` job (wave-2B job queue) rather than
        # running `_run_sync` inline — nothing to monkeypatch here, the
        # handler only touches `jobs_repo()`, which both backends provide.
        r = seeded_app_both["client"].post("/api/sync/trigger", headers=_admin_headers(seeded_app_both))
        assert r.status_code in (200, 202)
        assert r.json().get("job_id")

    def test_sync_settings(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/sync/settings", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_sync_table_subscriptions(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/sync/table-subscriptions", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


class TestCatalogSmoke:
    COVERED_ROUTES = {
        "GET /api/catalog/tables",
        "GET /api/catalog/profile/{table_name}",
        "POST /api/catalog/profile/{table_name}/refresh",
    }

    def test_catalog_tables(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/catalog/tables", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200
        body = r.json()
        tables = body if isinstance(body, list) else body.get("tables", [])
        assert isinstance(tables, list)

    def test_catalog_profile_missing(self, seeded_app_both):
        r = seeded_app_both["client"].get(
            "/api/catalog/profile/nonexistent-table", headers=_admin_headers(seeded_app_both)
        )
        assert r.status_code in (404, 422)

    def test_catalog_profile_refresh_missing(self, seeded_app_both):
        r = seeded_app_both["client"].post(
            "/api/catalog/profile/nonexistent-table/refresh", headers=_admin_headers(seeded_app_both)
        )
        assert r.status_code in (404, 422)


# ---------------------------------------------------------------------------
# Connectors — on-demand connector setup prompt (seed-backed, no DB reads;
# smoked on both backends anyway so the auth dependency chain is exercised)
# ---------------------------------------------------------------------------


class TestConnectorsPromptSmoke:
    COVERED_ROUTES = {
        "GET /api/connectors/{slug}/prompt",
    }

    def test_prompt_known_slug(self, seeded_app_both):
        r = seeded_app_both["client"].get(
            "/api/connectors/connector-asana/prompt",
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["slug"] == "connector-asana"
        assert body["prompt"]
        assert "{instance_brand}" not in body["prompt"]

    def test_prompt_unknown_slug_404(self, seeded_app_both):
        r = seeded_app_both["client"].get(
            "/api/connectors/connector-nope/prompt",
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code == 404
        assert r.json()["detail"]["kind"] == "unknown_connector"

    def test_prompt_requires_auth(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/connectors/connector-asana/prompt")
        assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Data (requires registered_table_both)
# ---------------------------------------------------------------------------


class TestDataSmoke:
    COVERED_ROUTES = {
        "GET /api/data/{table_id}/check-access",
        "GET /api/data/{table_id}/download",
    }

    def test_check_access_admin(self, seeded_app_both, registered_table_both):
        table_id = registered_table_both["table_id"]
        r = seeded_app_both["client"].get(
            f"/api/data/{table_id}/check-access",
            headers={"Authorization": f"Bearer {seeded_app_both['admin_token']}"},
        )
        assert r.status_code == 204

    def test_check_access_analyst_denied(self, seeded_app_both, registered_table_both):
        table_id = registered_table_both["table_id"]
        r = seeded_app_both["client"].get(
            f"/api/data/{table_id}/check-access",
            headers={"Authorization": f"Bearer {seeded_app_both['analyst_token']}"},
        )
        assert r.status_code == 403

    def test_download_admin(self, seeded_app_both, registered_table_both):
        table_id = registered_table_both["table_id"]
        r = seeded_app_both["client"].get(
            f"/api/data/{table_id}/download",
            headers={"Authorization": f"Bearer {seeded_app_both['admin_token']}"},
        )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Query (requires registered_table_both)
# ---------------------------------------------------------------------------


class TestQuerySmoke:
    COVERED_ROUTES = {
        "POST /api/query",
        "POST /api/query/hybrid",
    }

    def test_query_select_one(self, seeded_app_both, registered_table_both):
        r = seeded_app_both["client"].post(
            "/api/query",
            json={"sql": "SELECT 1 AS n"},
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code == 200
        body = r.json()
        assert "rows" in body
        assert "columns" in body

    def test_query_hybrid(self, seeded_app_both, monkeypatch):
        monkeypatch.setattr("app.api.query_hybrid._run_bq_query", lambda *a, **kw: ([], []), raising=False)
        r = seeded_app_both["client"].post(
            "/api/query/hybrid",
            json={"sql": "SELECT 1", "source": "local"},
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 422, 501)


# ---------------------------------------------------------------------------
# V2 (requires registered_table_both)
# ---------------------------------------------------------------------------


class TestV2Smoke:
    COVERED_ROUTES = {
        "GET /api/v2/catalog",
        "GET /api/v2/schema/{table_id}",
        "GET /api/v2/sample/{table_id}",
        "POST /api/v2/scan",
        "POST /api/v2/scan/estimate",
    }

    def test_v2_catalog(self, seeded_app_both, registered_table_both):
        r = seeded_app_both["client"].get("/api/v2/catalog", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200
        body = r.json()
        tables = body if isinstance(body, list) else body.get("tables", [])
        assert isinstance(tables, list)

    def test_v2_schema(self, seeded_app_both, registered_table_both):
        table_id = registered_table_both["table_id"]
        r = seeded_app_both["client"].get(f"/api/v2/schema/{table_id}", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200
        assert "columns" in r.json()

    def test_v2_sample(self, seeded_app_both, registered_table_both):
        table_id = registered_table_both["table_id"]
        r = seeded_app_both["client"].get(f"/api/v2/sample/{table_id}", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_v2_scan(self, seeded_app_both, registered_table_both, monkeypatch):
        monkeypatch.setattr("app.api.v2_scan._run_scan", lambda *a, **kw: {"rows": 0}, raising=False)
        r = seeded_app_both["client"].post(
            "/api/v2/scan",
            json={"table_id": registered_table_both["table_id"]},
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 202, 422)

    def test_v2_scan_estimate(self, seeded_app_both, registered_table_both):
        r = seeded_app_both["client"].post(
            "/api/v2/scan/estimate",
            json={"table_id": registered_table_both["table_id"]},
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 501)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


class TestMetricsSmoke:
    COVERED_ROUTES = {
        "GET /api/metrics",
        "POST /api/admin/metrics",
        "POST /api/admin/metrics/import",
    }

    def test_metrics_list(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/metrics", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_admin_metrics_create(self, seeded_app_both):
        r = seeded_app_both["client"].post(
            "/api/admin/metrics",
            json={},
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 201, 422)

    def test_admin_metrics_import(self, seeded_app_both):
        r = seeded_app_both["client"].post(
            "/api/admin/metrics/import",
            json={"metrics": []},
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 204, 422)


# ---------------------------------------------------------------------------
# Glossary
# ---------------------------------------------------------------------------


class TestGlossarySmoke:
    COVERED_ROUTES = {
        "GET /api/glossary",
        "GET /api/glossary/search",
        "GET /api/glossary/{glossary_id}",
    }

    def test_glossary_list(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/glossary", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_glossary_search(self, seeded_app_both):
        r = seeded_app_both["client"].get(
            "/api/glossary/search", params={"q": "revenue"}, headers=_admin_headers(seeded_app_both)
        )
        assert r.status_code == 200

    def test_glossary_get_by_id(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/glossary/does-not-exist", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


class TestMemorySmoke:
    COVERED_ROUTES = {
        "GET /api/memory",
        "POST /api/memory",
        "GET /api/memory/stats",
        "GET /api/memory/{item_id}/provenance",
        "POST /api/memory/{item_id}/vote",
    }

    def test_memory_list(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/memory", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200
        body = r.json()
        items = body if isinstance(body, list) else body.get("items", [])
        assert isinstance(items, list)

    def test_memory_create(self, seeded_app_both):
        r = seeded_app_both["client"].post(
            "/api/memory",
            json={"title": "Smoke test fact", "content": "Revenue doubled QoQ in Q1.", "category": "business"},
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code == 201
        assert "id" in r.json()

    def test_memory_stats(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/memory/stats", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_memory_provenance(self, seeded_app_both):
        rc = seeded_app_both["client"].post(
            "/api/memory",
            json={"title": "Prov test", "content": "Revenue doubled QoQ in Q1.", "category": "business"},
            headers=_admin_headers(seeded_app_both),
        )
        assert rc.status_code == 201
        item_id = rc.json()["id"]
        r = seeded_app_both["client"].get(f"/api/memory/{item_id}/provenance", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_memory_vote(self, seeded_app_both):
        rc = seeded_app_both["client"].post(
            "/api/memory",
            json={"title": "Vote test", "content": "Revenue doubled QoQ in Q1.", "category": "business"},
            headers=_admin_headers(seeded_app_both),
        )
        assert rc.status_code == 201
        item_id = rc.json()["id"]
        r = seeded_app_both["client"].post(
            f"/api/memory/{item_id}/vote",
            json={"vote": 1},
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


class TestUploadSmoke:
    COVERED_ROUTES = {
        "POST /api/upload/sessions",
        "POST /api/upload/artifacts",
        "POST /api/upload/local-md",
        "POST /api/upload/audit-events",
    }

    def test_upload_session(self, seeded_app_both):
        import io

        r = seeded_app_both["client"].post(
            "/api/upload/sessions",
            files={"file": ("test.jsonl", io.BytesIO(b'{"type":"text"}\n'), "application/octet-stream")},
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 201)

    def test_upload_artifact(self, seeded_app_both):
        import io

        r = seeded_app_both["client"].post(
            "/api/upload/artifacts",
            files={"file": ("test.html", io.BytesIO(b"<h1>Test</h1>"), "text/html")},
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 201)

    def test_upload_local_md(self, seeded_app_both):
        r = seeded_app_both["client"].post(
            "/api/upload/local-md",
            json={"content": "# Local doc\nContent.", "path": "test.md"},
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 201)

    def test_upload_audit_events(self, seeded_app_both):
        """Client-reported CLI audit events land identically on both backends.

        Asserts the accept/reject split too — the server-side action allowlist
        is the security boundary here, and a backend that silently accepted an
        uncataloged action would still return 200.
        """
        r = seeded_app_both["client"].post(
            "/api/upload/audit-events",
            json={
                "events": [
                    {
                        "action": "query.local_offline",
                        "params": {"tables": ["orders"], "sql_hash": "deadbeef01234567", "rows": 1},
                        "observed_at": "2026-08-29T12:00:00Z",
                    },
                    {"action": "not.a.client.action", "params": {}, "observed_at": "2026-08-29T12:00:01Z"},
                ]
            },
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code == 200
        assert r.json() == {"accepted": 1, "rejected": 1}


# ---------------------------------------------------------------------------
# Admin Registry (register-table, precheck, CRUD)
# ---------------------------------------------------------------------------


class TestAdminRegistrySmoke:
    COVERED_ROUTES = {
        "GET /api/admin/config-surface",
        "GET /api/admin/registry",
        "GET /api/admin/server-config",
        "POST /api/admin/server-config",
        "GET /api/admin/server-config/overlay",
        "POST /api/admin/register-table/precheck",
        "POST /api/admin/register-table",
        "PUT /api/admin/registry/{table_id}",
        "DELETE /api/admin/registry/{table_id}",
        "POST /api/admin/registry/{table_id}/policy/preview",
        "GET /api/admin/discover-tables",
        "POST /api/admin/configure",
        "GET /api/admin/metadata/{table_id}",
        "POST /api/admin/metadata/{table_id}/push",
    }

    def test_registry_list(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/registry", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200
        body = r.json()
        tables = body if isinstance(body, list) else body.get("tables", [])
        assert isinstance(tables, list)

    def test_server_config(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/server-config", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_server_config_overlay(self, seeded_app_both):
        h = _admin_headers(seeded_app_both)
        r = seeded_app_both["client"].get("/api/admin/server-config/overlay", headers=h)
        assert r.status_code == 200
        body = r.json()
        assert "sections" in body
        assert "editable_sections" in body
        # Admin-only, mirrors GET /api/admin/server-config.
        r_analyst = seeded_app_both["client"].get(
            "/api/admin/server-config/overlay", headers=_analyst_headers(seeded_app_both)
        )
        assert r_analyst.status_code == 403

    def test_config_surface(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/config-surface", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_register_precheck(self, seeded_app_both):
        r = seeded_app_both["client"].post(
            "/api/admin/register-table/precheck",
            json={
                "name": "chk_orders",
                "source_type": "keboola",
                "bucket": "in.c-smoke",
                "source_table": "orders",
                "query_mode": "local",
            },
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 422)

    def test_register_table_crud(self, seeded_app_both):
        h = _admin_headers(seeded_app_both)
        rc = seeded_app_both["client"].post(
            "/api/admin/register-table",
            json={
                "name": "crud_test_table",
                "source_type": "keboola",
                "bucket": "in.c-crud",
                "source_table": "orders",
                "query_mode": "local",
            },
            headers=h,
        )
        assert rc.status_code == 201
        table_id = rc.json()["id"]

        ru = seeded_app_both["client"].put(
            f"/api/admin/registry/{table_id}",
            json={"description": "Updated description for smoke test"},
            headers=h,
        )
        assert ru.status_code == 200

        rd = seeded_app_both["client"].delete(f"/api/admin/registry/{table_id}", headers=h)
        assert rd.status_code == 204

    def test_registry_policy_preview(self, seeded_app_both):
        """Table access policies (Task 14) — the route reads through the
        factory-backed repos (table_registry_repo/users_repo/
        user_group_members_repo/audit_repo) on both backends. The table is
        never synced, so a real analytics-DB read for rows_total/rows_visible
        legitimately 422s (`policy_preview_failed`) on top of the never-synced
        table's `DESCRIBE` failure -- the smoke assertion only cares that the
        route is reachable and behaves identically on DuckDB and Postgres,
        same tolerance `test_register_precheck`/`test_discover_tables` above
        already use for other state-dependent endpoints in this class."""
        h = _admin_headers(seeded_app_both)
        rc = seeded_app_both["client"].post(
            "/api/admin/register-table",
            json={
                "name": "policy_preview_smoke",
                "source_type": "keboola",
                "bucket": "in.c-smoke",
                "source_table": "orders",
                "query_mode": "local",
            },
            headers=h,
        )
        assert rc.status_code == 201
        table_id = rc.json()["id"]

        r = seeded_app_both["client"].post(
            f"/api/admin/registry/{table_id}/policy/preview",
            json={"sql": "SELECT * FROM policy_preview_smoke", "as_groups": ["Everyone"]},
            headers=h,
        )
        assert r.status_code in (200, 422), r.text

    def test_discover_tables(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/discover-tables", headers=_admin_headers(seeded_app_both))
        assert r.status_code in (200, 503)

    def test_configure(self, seeded_app_both):
        r = seeded_app_both["client"].post("/api/admin/configure", json={}, headers=_admin_headers(seeded_app_both))
        assert r.status_code in (200, 422)

    def test_metadata_get(self, seeded_app_both, registered_table_both):
        r = seeded_app_both["client"].get(
            f"/api/admin/metadata/{registered_table_both['table_id']}",
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 404)

    def test_metadata_push(self, seeded_app_both, registered_table_both):
        r = seeded_app_both["client"].post(
            f"/api/admin/metadata/{registered_table_both['table_id']}/push",
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 400, 404, 422, 500, 503)


# ---------------------------------------------------------------------------
# Admin Doctor  (new-instance deployment gate)
# ---------------------------------------------------------------------------


class TestAdminDoctorSmoke:
    COVERED_ROUTES = {
        "POST /api/admin/doctor/new-instance",
        "GET /api/admin/doctor/support",
    }

    def test_new_instance_doctor_report_shape(self, seeded_app_both):
        """The doctor reads users/groups/grants/agents through the repo
        factories, so running it on both backends is a genuine parity check —
        a backend-split read inside any of the six checks would surface here."""
        r = seeded_app_both["client"].post(
            "/api/admin/doctor/new-instance",
            headers=_admin_headers(seeded_app_both),
            json={},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] in ("ok", "warning", "error")
        names = [c["name"] for c in body["checks"]]
        assert names == [
            "login-door",
            "email-delivery",
            "chat-grant",
            "agent-scope",
            "app-state-backend",
            "branding",
        ]
        for check in body["checks"]:
            assert check["status"] in ("ok", "warning", "error", "info")
            # A crashed check reports itself; a backend-split bug in a repo
            # read would land here as "check crashed: …" on one backend only.
            assert "check crashed" not in check["detail"], check

    def test_non_admin_is_403(self, seeded_app_both):
        r = seeded_app_both["client"].post(
            "/api/admin/doctor/new-instance",
            headers=_analyst_headers(seeded_app_both),
            json={},
        )
        assert r.status_code == 403

    def test_support_bundle_sections_resolve_on_both_backends(self, seeded_app_both):
        """The support doctor's two backend-sensitive collectors, proven live.

        ``schema`` takes an entirely different branch per backend (PG compares
        ``alembic_version`` against the migration head, DuckDB the
        ``schema_version`` table), and ``sync`` reads sync_state ×
        table_registry through the repo factories — where PG hands back
        tz-aware timestamps and DuckDB naive ones. Either divergence would
        show up here as a crashed section on one backend only, which is
        exactly what a section-isolated collector would otherwise hide.
        """
        from src.repositories import sync_state_repo, table_registry_repo

        table_registry_repo().register(id="doctor_ok", name="doctor_ok", source_type="keboola")
        # id != name on purpose: sync_state is keyed on NAME (table_id is
        # sourced from _meta.table_name), so a row whose registry id differs
        # is exactly what an id-keyed lookup would silently miss — and the
        # keying lives in the repo read, so it is worth pinning per backend.
        table_registry_repo().register(id="doctor_bad", name="Doctor Bad", source_type="keboola")
        sync_state_repo().update_sync("doctor_ok", rows=1, file_size_bytes=10, hash="h")
        sync_state_repo().set_error("Doctor Bad", "sync failed on both backends alike")

        r = seeded_app_both["client"].get("/api/admin/doctor/support", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200, r.text
        body = r.json()

        for section in ("build", "schema", "retrieval", "sync", "disk", "process", "secrets"):
            assert section in body, sorted(body)
            detail = body[section].get("detail", "") if isinstance(body[section], dict) else ""
            assert "section crashed" not in detail, (section, detail)

        assert body["schema"]["backend"] in ("duckdb", "postgres")
        assert body["schema"]["status"] == "ok", body["schema"]

        keboola = body["sync"]["sources"]["keboola"]
        assert keboola["tables"] >= 2
        assert keboola["errors"] >= 1
        # Found by name, reported by registry id — the id is what an operator
        # types into `agnes catalog`/`agnes schema`, so that is what the
        # bundle names.
        failing = {e["table_id"] for e in keboola["last_errors"]}
        assert "doctor_bad" in failing, keboola

    def test_support_bundle_non_admin_is_403(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/doctor/support", headers=_analyst_headers(seeded_app_both))
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Admin Store  (submissions queue + reaper)
# ---------------------------------------------------------------------------


class TestAdminStoreSmoke:
    COVERED_ROUTES = {
        "GET /api/admin/store/submissions",
        "POST /api/admin/run-reap-stuck-reviews",
    }

    def test_submissions_list(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/store/submissions", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200
        body = r.json()
        items = body if isinstance(body, list) else body.get("items", [])
        assert isinstance(items, list)

    def test_submissions_detail_missing(self, seeded_app_both):
        r = seeded_app_both["client"].get(
            "/api/admin/store/submissions/nonexistent", headers=_admin_headers(seeded_app_both)
        )
        assert r.status_code == 404

    def test_reap_stuck_reviews_empty(self, seeded_app_both):
        r = seeded_app_both["client"].post("/api/admin/run-reap-stuck-reviews", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200
        body = r.json()
        assert body.get("ok") is True
        assert body.get("details", {}).get("reaped", -1) == 0

    def test_submissions_override_missing(self, seeded_app_both):
        r = seeded_app_both["client"].post(
            "/api/admin/store/submissions/nonexistent/override",
            json={"reason": "test override reason"},
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code == 404

    def test_submissions_rescan_missing(self, seeded_app_both):
        r = seeded_app_both["client"].post(
            "/api/admin/store/submissions/nonexistent/rescan", headers=_admin_headers(seeded_app_both)
        )
        assert r.status_code == 404

    def test_submissions_retry_missing(self, seeded_app_both):
        r = seeded_app_both["client"].post(
            "/api/admin/store/submissions/nonexistent/retry", headers=_admin_headers(seeded_app_both)
        )
        assert r.status_code == 404

    def test_submissions_delete_missing(self, seeded_app_both):
        r = seeded_app_both["client"].delete(
            "/api/admin/store/submissions/nonexistent", headers=_admin_headers(seeded_app_both)
        )
        assert r.status_code == 404

    def test_submissions_bundle_missing(self, seeded_app_both):
        r = seeded_app_both["client"].get(
            "/api/admin/store/submissions/nonexistent/bundle.zip", headers=_admin_headers(seeded_app_both)
        )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Admin Sessions
# ---------------------------------------------------------------------------


class TestAdminSessionsSmoke:
    COVERED_ROUTES = {
        "GET /api/admin/sessions/list",
        "GET /api/admin/sessions/kpis",
        "GET /api/admin/sessions/facets",
    }

    def test_sessions_list(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/sessions/list", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_sessions_kpis(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/sessions/kpis", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_sessions_facets(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/sessions/facets", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Store (public listing, categories, owners, preview)
# ---------------------------------------------------------------------------


class TestStoreSmoke:
    COVERED_ROUTES = {
        "GET /api/store/categories",
        "GET /api/store/owners",
        "GET /api/store/entities",
        "GET /api/store/entities/{entity_id}",
        "GET /api/store/entities/{entity_id}/files",
        "GET /api/store/entities/{entity_id}/photo",
        "GET /api/store/entities/{entity_id}/docs/{filename}",
        "POST /api/store/entities/preview",
        "POST /api/store/entities/dryrun",
        "POST /api/store/entities",
        "POST /api/store/entities/from-markdown",
        "POST /api/store/entities/from-components",
        "PUT /api/store/entities/{entity_id}",
        "GET /api/store/entities/{entity_id}/markdown",
        "PUT /api/store/entities/{entity_id}/from-markdown",
        "POST /api/store/entities/{entity_id}/install",
        "DELETE /api/store/entities/{entity_id}/install",
        "POST /api/store/entities/{entity_id}/rate",
        "GET /api/store/entities/{entity_id}/status",
        "DELETE /api/store/entities/{entity_id}",
        "GET /api/store/bundle.zip",
        "POST /api/store/import-bundle",
    }

    def test_reading_and_writing_an_entity_document(self, seeded_app_both):
        """The editing pair. Both refuse an unknown id the same way the rest of
        the store does — 404, without admitting whether the row exists."""
        c, h = seeded_app_both["client"], _admin_headers(seeded_app_both)
        assert c.get("/api/store/entities/nope/markdown", headers=h).status_code == 404
        r = c.put(
            "/api/store/entities/nope/from-markdown",
            json={"name": "whatever", "skill_md": "# hi"},
            headers=h,
        )
        assert r.status_code == 404, r.text

    def test_categories(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/store/categories", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_owners(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/store/owners", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_entities_list(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/store/entities", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200
        body = r.json()
        items = body if isinstance(body, list) else body.get("items", [])
        assert isinstance(items, list)

    def test_entities_preview(self, seeded_app_both):
        import io

        zb = make_skill_zip("preview-skill")
        r = seeded_app_both["client"].post(
            "/api/store/entities/preview",
            files={"file": ("preview-skill.zip", io.BytesIO(zb), "application/zip")},
            data={"type": "skill"},
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code == 200, r.text

    def test_entities_create_from_markdown(self, seeded_app_both):
        """POST /entities/from-markdown — studio Skill Builder's JSON publish path."""
        r = seeded_app_both["client"].post(
            "/api/store/entities/from-markdown",
            json={
                "name": "smoke-from-markdown",
                "description": (
                    "Use when smoke-testing the from-markdown publish endpoint end to end across both backends."
                ),
                "category": "Other",
                "skill_md": (
                    "Step one: describe the scenario under test in plain language. "
                    "Step two: call the endpoint with a valid payload and capture the response. "
                    "Step three: assert the entity was created with status 201 and a non-empty id field."
                ),
            },
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code == 201, r.text
        assert r.json()["id"]

    def test_entities_create_from_markdown_agent(self, seeded_app_both):
        """POST /entities/from-markdown with type=agent — Studio Agent Builder's
        JSON publish path; bakes a bare <name>.md instead of <name>/SKILL.md."""
        r = seeded_app_both["client"].post(
            "/api/store/entities/from-markdown",
            json={
                "type": "agent",
                "name": "smoke-from-markdown-agent",
                "description": (
                    "Use when smoke-testing the from-markdown agent publish endpoint across both backends."
                ),
                "category": "Other",
                "skill_md": (
                    "Step one: describe the scenario under test in plain language. "
                    "Step two: call the endpoint with a valid payload and capture the response. "
                    "Step three: assert the entity was created with status 201 and a non-empty id field."
                ),
            },
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["id"]
        assert body["type"] == "agent"

    def test_entities_create_from_components(self, seeded_app_both):
        """POST /entities/from-components — bundle published items into one plugin.

        Composes from an entity created in the same test rather than a fixture
        id: the endpoint resolves every component against the caller's own
        visibility, so a borrowed id would pass or 404 depending on seed order.
        """
        client = seeded_app_both["client"]
        headers = _admin_headers(seeded_app_both)
        body = (
            "Step one: describe the scenario under test in plain language. "
            "Step two: call the endpoint with a valid payload and capture the response. "
            "Step three: assert the entity was created with status 201 and a non-empty id field."
        )
        made = client.post(
            "/api/store/entities/from-markdown",
            json={
                "name": "smoke-compose-part",
                "description": (
                    "Use when smoke-testing the compose endpoint's component resolution across both backends."
                ),
                "category": "Other",
                "skill_md": body,
            },
            headers=headers,
        )
        assert made.status_code == 201, made.text

        r = client.post(
            "/api/store/entities/from-components",
            json={
                "name": "smoke-composed-plugin",
                "description": (
                    "Use when smoke-testing that several published items bundle into one installable plugin."
                ),
                "category": "Other",
                "components": [made.json()["id"]],
            },
            headers=headers,
        )
        assert r.status_code == 201, r.text
        composed = r.json()
        assert composed["id"]
        assert composed["type"] == "plugin"


class TestMcpBuilderSmoke:
    """The two admin builders: their pages, and the turn that opens a conversation."""

    COVERED_ROUTES = {
        "GET /admin/mcp-sources/new",
        "GET /admin/mcp-sources/{source_id}/edit",
        "GET /admin/data-packages/{pkg_id}/edit",
        "POST /api/admin/mcp-sources/builder/turn",
        "GET /admin/linked-apps/new",
    }

    def test_editing_an_unknown_package_is_a_404(self, seeded_app_both):
        """Same reason as the source below: an edit page pointed at nothing
        would render an empty builder that CREATES on save."""
        r = seeded_app_both["client"].get(
            "/admin/data-packages/does-not-exist/edit", headers=_admin_headers(seeded_app_both)
        )
        assert r.status_code == 404, r.text

    def test_editing_an_unknown_source_is_a_404(self, seeded_app_both):
        """The edit page is the create page pointed at a row — so it has to
        refuse an id that is not one, rather than rendering an empty builder
        that would register a second source on save."""
        r = seeded_app_both["client"].get(
            "/admin/mcp-sources/does-not-exist/edit", headers=_admin_headers(seeded_app_both)
        )
        assert r.status_code == 404, r.text

    def test_builder_page_renders_for_an_admin(self, seeded_app_both):
        r = seeded_app_both["client"].get("/admin/mcp-sources/new", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200, r.text
        assert "mcp-builder-view" in r.text

    def test_the_linked_apps_builder_redirects_into_this_one(self, seeded_app_both):
        """It was never a builder that could stand alone — its first step
        asked for an MCP source it had no way to create — so publishing apps
        is a section here, and the old path lands on it."""
        r = seeded_app_both["client"].get(
            "/admin/linked-apps/new",
            headers=_admin_headers(seeded_app_both),
            follow_redirects=False,
        )
        assert r.status_code == 302, r.text
        assert r.headers["location"] == "/admin/mcp-sources/new"

    def test_the_opening_turn_reports_slots_and_engine(self, seeded_app_both):
        """An empty first message is the builder speaking first; it must come
        back with what is still open, so the panel can show progress."""
        r = seeded_app_both["client"].post(
            "/api/admin/mcp-sources/builder/turn",
            json={"message": "", "history": []},
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["reply"]
        assert body["engine"] in ("stub", "model")
        assert [s["key"] for s in body["slots"]] == ["endpoint", "auth", "name", "tools"]
        assert not any(s["known"] for s in body["slots"])

    def test_it_writes_nothing(self, seeded_app_both):
        client = seeded_app_both["client"]
        headers = _admin_headers(seeded_app_both)
        before = client.get("/api/admin/mcp-sources", headers=headers)
        client.post(
            "/api/admin/mcp-sources/builder/turn",
            json={"message": "connect our CRM server", "history": []},
            headers=headers,
        )
        after = client.get("/api/admin/mcp-sources", headers=headers)
        assert before.json() == after.json(), "a builder turn registered a source"


# ---------------------------------------------------------------------------
# Flea Upload — state machine, visibility rules
# ---------------------------------------------------------------------------


class TestFleaUploadSmoke:
    COVERED_ROUTES: set = set()  # covered by TestStoreSmoke already

    def _upload(self, client, headers, zip_bytes, entity_type="skill"):
        import io

        return client.post(
            "/api/store/entities",
            files={"file": ("bundle.zip", io.BytesIO(zip_bytes), "application/zip")},
            data={"type": entity_type},
            headers=headers,
        )

    def test_upload_valid_skill_approved(self, seeded_app_both):
        r = self._upload(
            seeded_app_both["client"], _admin_headers(seeded_app_both), make_skill_zip("smoke-skill-valid")
        )
        assert r.status_code == 201
        assert r.json()["visibility_status"] == "approved"

    def test_entity_status(self, seeded_app_both):
        """GET /entities/{id}/status — owner-facing review-pipeline status."""
        r = self._upload(
            seeded_app_both["client"], _admin_headers(seeded_app_both), make_skill_zip("smoke-skill-status")
        )
        assert r.status_code == 201
        eid = r.json()["id"]
        s = seeded_app_both["client"].get(f"/api/store/entities/{eid}/status", headers=_admin_headers(seeded_app_both))
        assert s.status_code == 200, s.text
        body = s.json()
        assert body["entity_id"] == eid
        assert body["visibility_status"] == "approved"
        assert body["submission"]["status"] == "approved"

    def test_upload_fails_short_description(self, seeded_app_both):
        r = self._upload(
            seeded_app_both["client"], _admin_headers(seeded_app_both), make_bad_desc_zip("smoke-bad-desc")
        )
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "validation_failed"

    def test_upload_fails_missing_name(self, seeded_app_both):
        r = self._upload(seeded_app_both["client"], _admin_headers(seeded_app_both), make_no_name_zip())
        assert r.status_code in (400, 422)

    def test_upload_fails_security_blocked(self, seeded_app_both):
        r = self._upload(
            seeded_app_both["client"], _admin_headers(seeded_app_both), make_security_fail_zip("smoke-sec-fail")
        )
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "security_blocked"

    def test_upload_duplicate_name_409(self, seeded_app_both):
        zb = make_skill_zip("smoke-duplicate-skill")
        self._upload(seeded_app_both["client"], _admin_headers(seeded_app_both), zb)
        r2 = self._upload(seeded_app_both["client"], _admin_headers(seeded_app_both), zb)
        assert r2.status_code == 409

    def test_upload_type_mismatch_returns_422(self, seeded_app_both):
        """Uploading a skill zip with type='plugin' declared → validation_failed."""
        # skill zip but type=plugin is a type-mismatch
        r = self._upload(
            seeded_app_both["client"],
            _admin_headers(seeded_app_both),
            make_skill_zip("smoke-type-mismatch"),
            entity_type="plugin",  # wrong type for a skill zip
        )
        # Should fail with validation error (wrong manifest for type)
        assert r.status_code == 422, r.text

    def test_pending_entity_not_visible_to_other_user(self, seeded_app_both, monkeypatch):
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

        import io

        zb = make_skill_zip("smoke-pending-skill")
        rc = seeded_app_both["client"].post(
            "/api/store/entities",
            files={"file": ("bundle.zip", io.BytesIO(zb), "application/zip")},
            data={"type": "skill"},
            headers=_admin_headers(seeded_app_both),
        )
        assert rc.status_code == 201
        entity = rc.json()
        assert entity["visibility_status"] == "pending"
        entity_id = entity["id"]

        # analyst (non-owner) should NOT see it in list
        rl = seeded_app_both["client"].get("/api/store/entities", headers=_analyst_headers(seeded_app_both))
        assert rl.status_code == 200
        rl_body = rl.json()
        rl_items = rl_body if isinstance(rl_body, list) else rl_body.get("items", [])
        ids_in_list = [e["id"] for e in rl_items]
        assert entity_id not in ids_in_list

        # analyst direct get should be 403/404
        rd = seeded_app_both["client"].get(
            f"/api/store/entities/{entity_id}", headers=_analyst_headers(seeded_app_both)
        )
        assert rd.status_code in (403, 404)

    def test_pending_entity_visible_to_owner(self, seeded_app_both, monkeypatch):
        """Owner (uploader) can see their own pending entity in the store listing."""
        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle",
            lambda *a, **kw: {
                "risk_level": "low",
                "summary": "mock",
                "findings": [],
                "reviewed_by_model": "mock",
                "error": None,
            },
        )
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)
        import io

        zb = make_skill_zip("smoke-owner-sees-own-pending")
        rc = seeded_app_both["client"].post(
            "/api/store/entities",
            files={"file": ("bundle.zip", io.BytesIO(zb), "application/zip")},
            data={"type": "skill"},
            headers=_admin_headers(seeded_app_both),
        )
        assert rc.status_code == 201
        entity = rc.json()
        assert entity["visibility_status"] == "pending"
        entity_id = entity["id"]
        # Owner (admin) should see it in their own listing
        rl = seeded_app_both["client"].get("/api/store/entities", headers=_admin_headers(seeded_app_both))
        assert rl.status_code == 200
        rl_body = rl.json()
        rl_items = rl_body if isinstance(rl_body, list) else rl_body.get("items", [])
        ids_in_list = [e["id"] for e in rl_items]
        assert entity_id in ids_in_list, "Owner cannot see their own pending entity in store listing"

    def test_pending_entity_visible_to_admin(self, seeded_app_both, monkeypatch):
        """Admin can GET a pending entity directly."""
        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle",
            lambda *a, **kw: {
                "risk_level": "low",
                "summary": "mock",
                "findings": [],
                "reviewed_by_model": "mock",
                "error": None,
            },
        )
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)
        import io

        zb = make_skill_zip("smoke-admin-sees-pending")
        rc = seeded_app_both["client"].post(
            "/api/store/entities",
            files={"file": ("bundle.zip", io.BytesIO(zb), "application/zip")},
            data={"type": "skill"},
            headers=_admin_headers(seeded_app_both),
        )
        assert rc.status_code == 201
        entity = rc.json()
        assert entity["visibility_status"] == "pending"
        entity_id = entity["id"]
        # Admin can see it directly
        r = seeded_app_both["client"].get(
            f"/api/store/entities/{entity_id}",
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code == 200, r.text

    def test_approved_entity_visible_to_everyone(self, seeded_app_both):
        import io

        zb = make_skill_zip("smoke-approved-visible")
        rc = seeded_app_both["client"].post(
            "/api/store/entities",
            files={"file": ("bundle.zip", io.BytesIO(zb), "application/zip")},
            data={"type": "skill"},
            headers=_admin_headers(seeded_app_both),
        )
        assert rc.status_code == 201
        entity_id = rc.json()["id"]
        r = seeded_app_both["client"].get(f"/api/store/entities/{entity_id}", headers=_analyst_headers(seeded_app_both))
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# My Stack
# ---------------------------------------------------------------------------


class TestMyStackSmoke:
    COVERED_ROUTES = {
        "GET /api/my-stack",
    }

    def _upload_skill(self, client, admin_headers, name):
        import io

        r = client.post(
            "/api/store/entities",
            files={"file": ("bundle.zip", io.BytesIO(make_skill_zip(name)), "application/zip")},
            data={"type": "skill"},
            headers=admin_headers,
        )
        assert r.status_code == 201
        return r.json()["id"]

    def test_install_skill_appears_in_stack(self, seeded_app_both):
        client = seeded_app_both["client"]
        h = _admin_headers(seeded_app_both)
        entity_id = self._upload_skill(client, h, "stack-install-skill")
        ri = client.post(f"/api/store/entities/{entity_id}/install", headers=h)
        assert ri.status_code in (200, 201)
        rs = client.get("/api/my-stack", headers=h)
        assert rs.status_code == 200
        ids = [e["entity_id"] for e in rs.json().get("store", [])]
        assert entity_id in ids

    def test_install_plugin_appears_in_stack(self, seeded_app_both):
        import io

        client = seeded_app_both["client"]
        h = _admin_headers(seeded_app_both)
        r = client.post(
            "/api/store/entities",
            files={"file": ("bundle.zip", io.BytesIO(make_plugin_zip("stack-install-plugin")), "application/zip")},
            data={"type": "plugin"},
            headers=h,
        )
        assert r.status_code == 201
        entity_id = r.json()["id"]
        ri = client.post(f"/api/store/entities/{entity_id}/install", headers=h)
        assert ri.status_code in (200, 201)
        rs = client.get("/api/my-stack", headers=h)
        assert entity_id in [e["entity_id"] for e in rs.json().get("store", [])]

    def test_install_agent_appears_in_stack(self, seeded_app_both):
        import io

        client = seeded_app_both["client"]
        h = _admin_headers(seeded_app_both)
        r = client.post(
            "/api/store/entities",
            files={"file": ("bundle.zip", io.BytesIO(make_agent_zip("stack-install-agent")), "application/zip")},
            data={"type": "agent"},
            headers=h,
        )
        assert r.status_code == 201
        entity_id = r.json()["id"]
        ri = client.post(f"/api/store/entities/{entity_id}/install", headers=h)
        assert ri.status_code in (200, 201)
        rs = client.get("/api/my-stack", headers=h)
        assert entity_id in [e["entity_id"] for e in rs.json().get("store", [])]

    def test_uninstall_removes_from_stack(self, seeded_app_both):
        client = seeded_app_both["client"]
        h = _admin_headers(seeded_app_both)
        entity_id = self._upload_skill(client, h, "stack-uninstall-skill")
        client.post(f"/api/store/entities/{entity_id}/install", headers=h)
        rd = client.delete(f"/api/store/entities/{entity_id}/install", headers=h)
        assert rd.status_code == 204
        rs = client.get("/api/my-stack", headers=h)
        assert entity_id not in [e["entity_id"] for e in rs.json().get("store", [])]

    def test_cli_my_stack_show_lists_installed(self, cli_client_both, seeded_app_both):
        """agnes my-stack show output contains entity name after install."""
        client = seeded_app_both["client"]
        h = _admin_headers(seeded_app_both)
        entity_id = self._upload_skill(client, h, "cli-stack-show-skill")
        client.post(f"/api/store/entities/{entity_id}/install", headers=h)
        result = cli_client_both["invoke"](["my-stack", "show"])
        assert result.exit_code == 0
        assert "cli-stack-show-skill" in result.output

    def test_cli_my_stack_show_after_removal(self, cli_client_both, seeded_app_both):
        """agnes my-stack show output does not contain entity after uninstall."""
        client = seeded_app_both["client"]
        h = _admin_headers(seeded_app_both)
        entity_id = self._upload_skill(client, h, "cli-stack-remove-skill")
        client.post(f"/api/store/entities/{entity_id}/install", headers=h)
        client.delete(f"/api/store/entities/{entity_id}/install", headers=h)
        result = cli_client_both["invoke"](["my-stack", "show"])
        assert result.exit_code == 0
        assert "cli-stack-remove-skill" not in result.output


# ---------------------------------------------------------------------------
# CLI — CliRunner through in-process transport
# ---------------------------------------------------------------------------


class TestCLISmoke:
    COVERED_ROUTES: set = set()  # CLI hits the same routes as web tests above

    def test_help(self, cli_client_both):
        result = cli_client_both["invoke"](["--help"])
        assert result.exit_code == 0

    def test_pull_help(self, cli_client_both):
        result = cli_client_both["invoke"](["pull", "--help"])
        assert result.exit_code == 0

    def test_admin_help(self, cli_client_both):
        result = cli_client_both["invoke"](["admin", "--help"])
        assert result.exit_code == 0

    def test_my_stack_show(self, cli_client_both):
        result = cli_client_both["invoke"](["my-stack", "show"])
        assert result.exit_code == 0

    def test_query_select_one(self, cli_client_both, registered_table_both):
        result = cli_client_both["invoke"](["query", "--remote", "SELECT 1 AS n"])
        assert result.exit_code == 0

    def test_diagnose(self, cli_client_both):
        result = cli_client_both["invoke"](["diagnose"])
        assert result.exit_code == 0

    def test_catalog(self, cli_client_both, registered_table_both):
        result = cli_client_both["invoke"](["catalog"])
        assert result.exit_code == 0

    def test_skills_help(self, cli_client_both):
        result = cli_client_both["invoke"](["skills", "--help"])
        assert result.exit_code == 0

    def test_store_help(self, cli_client_both):
        result = cli_client_both["invoke"](["store", "--help"])
        assert result.exit_code == 0

    def test_marketplace_help(self, cli_client_both):
        result = cli_client_both["invoke"](["marketplace", "--help"])
        assert result.exit_code == 0

    def test_auth_token_list(self, cli_client_both):
        result = cli_client_both["invoke"](["auth", "token", "list"])
        assert result.exit_code == 0

    def test_snapshot_help(self, cli_client_both):
        result = cli_client_both["invoke"](["snapshot", "--help"])
        assert result.exit_code == 0

    def test_schema_table(self, cli_client_both, registered_table_both):
        """agnes schema <table_id> exits 0 and prints column info."""
        table_id = registered_table_both["table_id"]
        result = cli_client_both["invoke"](["schema", table_id])
        assert result.exit_code == 0, f"schema failed: {result.output}"


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


class TestTokensSmoke:
    COVERED_ROUTES = {
        "GET /auth/tokens",
        "POST /auth/tokens",
        "GET /auth/tokens/{token_id}",
        "DELETE /auth/tokens/{token_id}",
        "GET /auth/admin/tokens",
        "DELETE /auth/admin/tokens/{token_id}",
    }

    def test_tokens_list(self, seeded_app_both):
        r = seeded_app_both["client"].get("/auth/tokens", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_token_create_get_delete(self, seeded_app_both):
        h = _admin_headers(seeded_app_both)
        rc = seeded_app_both["client"].post("/auth/tokens", json={"name": "smoke-token"}, headers=h)
        assert rc.status_code == 201
        assert "token" in rc.json()
        token_id = rc.json()["id"]
        rg = seeded_app_both["client"].get(f"/auth/tokens/{token_id}", headers=h)
        assert rg.status_code == 200
        rd = seeded_app_both["client"].delete(f"/auth/tokens/{token_id}", headers=h)
        assert rd.status_code == 204

    def test_admin_tokens_list(self, seeded_app_both):
        r = seeded_app_both["client"].get("/auth/admin/tokens", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Scripts
# ---------------------------------------------------------------------------


class TestScriptsSmoke:
    COVERED_ROUTES = {
        "GET /api/scripts",
    }

    def test_scripts_list(self, seeded_app_both):
        r = seeded_app_both["client"].get(
            "/api/scripts",
            headers={"Authorization": f"Bearer {seeded_app_both['admin_token']}"},
        )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class TestSettingsSmoke:
    COVERED_ROUTES = {
        "GET /api/settings",
        "PUT /api/settings/dataset",
    }

    def test_settings_get(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/settings", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_settings_dataset_put(self, seeded_app_both):
        r = seeded_app_both["client"].put("/api/settings/dataset", json={}, headers=_admin_headers(seeded_app_both))
        assert r.status_code in (200, 422)


# ---------------------------------------------------------------------------
# Marketplaces
# ---------------------------------------------------------------------------


class TestMarketplacesSmoke:
    COVERED_ROUTES = {
        "GET /api/marketplaces",
        "POST /api/marketplaces",
        "POST /api/marketplaces/{marketplace_id}/sync",
        "GET /api/marketplace/items",
        "GET /api/marketplace/categories",
        "GET /api/marketplace/curated/{marketplace_id}/{plugin_name}",
        "GET /api/marketplace/flea/{entity_id}/detail",
        "POST /api/marketplace/curated/{marketplace_id}/{plugin_name}/install",
        "DELETE /api/marketplace/curated/{marketplace_id}/{plugin_name}/install",
    }

    def test_marketplaces_list(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/marketplaces", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_marketplace_items(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/marketplace/items", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_marketplace_categories(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/marketplace/categories", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_marketplace_curated_missing(self, seeded_app_both):
        r = seeded_app_both["client"].get(
            "/api/marketplace/curated/nonexistent-mp/nonexistent-plugin",
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 404)

    def test_marketplace_flea_detail_missing(self, seeded_app_both):
        r = seeded_app_both["client"].get(
            "/api/marketplace/flea/nonexistent-entity/detail",
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 404)


# ---------------------------------------------------------------------------
# Admin dashboard signals (the /admin "Needs fixing" zone)
# ---------------------------------------------------------------------------


class TestAdminDashboardSmoke:
    COVERED_ROUTES = {
        "GET /api/admin/dashboard/signals",
    }

    def test_signals_shape(self, seeded_app_both):
        from app.services.admin_dashboard import invalidate_cache

        # The zone-2 TTL cache is process-global, so the duckdb leg of this
        # parametrised fixture would otherwise serve its rollup to the pg leg.
        invalidate_cache()
        r = seeded_app_both["client"].get(
            "/api/admin/dashboard/signals",
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["zone"] == "needs_fixing"
        assert isinstance(body["signals"], list)
        # Clear signals are omitted, never returned at zero — an empty list is
        # the healthy state and the page renders it as such.
        for sig in body["signals"]:
            assert sig["count"] > 0 or sig["failed"] is True
            assert set(sig) >= {"key", "title", "zone", "severity", "failed", "count", "href", "blurb"}
        invalidate_cache()

    def test_signals_requires_admin(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/dashboard/signals")
        assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Reports (marketplace usage digest)
# ---------------------------------------------------------------------------


class TestReportsSmoke:
    COVERED_ROUTES = {
        "GET /api/admin/reports/marketplace-digest",
    }

    def test_digest_daily(self, seeded_app_both):
        r = seeded_app_both["client"].get(
            "/api/admin/reports/marketplace-digest?period=daily",
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code == 200
        body = r.json()
        for key in (
            "meta",
            "headline_kpis",
            "trend_series",
            "by_source",
            "top_items",
            "installs",
            "marketplace_health",
        ):
            assert key in body

    def test_digest_weekly(self, seeded_app_both):
        r = seeded_app_both["client"].get(
            "/api/admin/reports/marketplace-digest?period=weekly",
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code == 200
        assert r.json()["meta"]["report_type"] == "weekly"

    def test_digest_bad_period(self, seeded_app_both):
        r = seeded_app_both["client"].get(
            "/api/admin/reports/marketplace-digest?period=bogus",
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Privacy (public, unauthenticated statement — CON-2)
# ---------------------------------------------------------------------------


class TestPrivacyPageSmoke:
    """Behavioral depth (auth bypass, operator-override redirect) is in
    tests/test_privacy_page.py; this is the parameter-free cross-backend
    smoke check the route-coverage guard requires."""

    COVERED_ROUTES = {
        "GET /privacy",
    }

    def test_answers_without_credentials(self, seeded_app_both):
        r = seeded_app_both["client"].get("/privacy", follow_redirects=False)
        assert r.status_code == 200
        assert "Where your data goes" in r.text


class TestUpgradeFreezeSmoke:
    """Behavioral depth (marker file, bounds, audit, host-script contract) is
    in tests/test_upgrade_freeze.py; this is the parameter-free cross-backend
    smoke check the route-coverage guard requires."""

    COVERED_ROUTES = {
        "GET /api/admin/upgrade-freeze",
        "POST /api/admin/upgrade-freeze",
        "DELETE /api/admin/upgrade-freeze",
    }

    def test_freeze_lifecycle(self, seeded_app_both):
        c = seeded_app_both["client"]
        h = {"Authorization": f"Bearer {seeded_app_both['admin_token']}"}
        assert c.get("/api/admin/upgrade-freeze", headers=h).json()["active"] is False
        r = c.post("/api/admin/upgrade-freeze", headers=h, json={"hours": 1})
        assert r.status_code == 201 and r.json()["active"] is True
        r = c.delete("/api/admin/upgrade-freeze", headers=h)
        assert r.status_code == 204
        assert c.get("/api/admin/upgrade-freeze", headers=h).json()["active"] is False


# ---------------------------------------------------------------------------
# Route-coverage guard
# ---------------------------------------------------------------------------

KNOWN_UNTESTED = {
    # External SSO login (design 2026-08-28) — PG-only feature covered
    # depth-first in its own harnesses rather than duplicated here:
    # tests/db_pg/test_admin_sso_api.py (admin gate, validation matrix,
    # write-only secret + vault 409, last-login-door guard, identities
    # pagination, /api/me/external-identity, and the DuckDB typed-501
    # answer this both-backends sweep would otherwise trip over),
    # tests/db_pg/test_sso_provider.py (login/callback flow with a faked
    # authlib client, binding algorithm, test-mode security), and the
    # parity sweeps' _PG_ONLY_ROUTE_EXEMPTIONS fail-clean assertions.
    "GET /api/admin/sso/config",
    "PUT /api/admin/sso/config",
    "PUT /api/admin/sso/client-secret",
    "DELETE /api/admin/sso/client-secret",
    "DELETE /api/admin/sso/config",
    "POST /api/admin/sso/test-config",
    "GET /api/admin/sso/identities",
    "DELETE /api/admin/sso/identities/{user_id}",
    "GET /api/me/external-identity",
    "GET /auth/sso/login",
    "GET /auth/sso/callback",
    # Agent-builder page (paper-theme redesign) — self-contained web page,
    # covered in tests/test_ui_layout_theme.py (chrome/list/auth/actions)
    # rather than duplicated in this PG smoke harness. The builder API it
    # drives (/api/agents/*) rides main's canonical agents table; the
    # agent-as-API management surface (/api/v1/agents/*) is covered separately
    # in tests/test_agents_management_api.py.
    "GET /agents",
    # Self-service display-name edit (#1036) — auth-scoped one-field PATCH on
    # the caller's own row; the repo method behind it (users.update_display_name)
    # is contract-tested on both backends in tests/db_pg/test_users_contract.py,
    # so it isn't duplicated in this parameter-free smoke sweep (it also needs a
    # request body).
    "PATCH /api/me/display-name",
    # Store publisher + verification trust line (paper-theme redesign) — all
    # parameterized mutations requiring a body, behaviorally covered in
    # tests/test_store_publisher_verification.py (publisher set, verify /
    # request-changes, request-verification, RBAC, org-published invariants);
    # not duplicated in this parameter-free smoke sweep.
    "PUT /api/store/entities/{entity_id}/publisher",
    "PUT /api/store/entities/{entity_id}/verification",
    "POST /api/store/entities/{entity_id}/verification/request",
    # Chat sandbox secret broker (2026-07-14 incident) — internal
    # sandbox→server routes, ticket-authed, never parameter-free (require a
    # POST body + a valid broker ticket), so they have no place in this
    # parameter-free smoke sweep. Behaviour covered in tests/test_broker_routes.py
    # (app-tier: ticket auth, scope enforcement, admin-path 403, ASGI-replay
    # RBAC fidelity).
    "POST /api/broker/anthropic",
    "POST /api/broker/anthropic/{subpath}",
    "POST /api/broker/agnes-api",
    "POST /api/broker/agnes-mcp",
    # Sandboxed data-apps authoring replay (Task 7, wave 3B) — same
    # ticket-authed, never parameter-free shape as the broker routes above.
    "POST /api/broker/data-apps",
    # apps-runner audit report-back: shared-secret (X-Runner-Token) header
    # auth, not a user session, so this parameter-free sweep can only ever
    # 401 here uninformatively. Behaviour — token floor, constant-time
    # compare, action whitelist, params cap, and the audit row it writes —
    # is covered on both backends' shared code path in
    # tests/test_audit_gap_surfaces.py.
    "POST /api/data-apps/runner-events",
    # Git smart-HTTP transport for that same authoring agent — ticket-authed
    # like its siblings, and additionally never reachable with parameter-free
    # inputs: the client is `git` speaking the wire protocol (its first call
    # carries ?service=git-upload-pack and expects a pkt-line body back), so a
    # bare GET/POST from this harness exercises nothing the route is for.
    # Behaviourally covered in tests/test_broker_data_apps_git.py.
    "GET /api/broker/data-apps.git/{slug}/{path}",
    "POST /api/broker/data-apps.git/{slug}/{path}",
    # Embedded kai-agent turn engine host wiring — same shape as the broker
    # routes above: /tickets, /mcp and /workspace are credential/ticket-authed
    # internal engine routes, and /sessions mints a credential as a side
    # effect, so none belong in a parameter-free smoke sweep that would either
    # 401 uninformatively or leave live tokens behind. Behaviour is covered in
    # tests/test_kai_host.py (claim set, exp ceiling, ticket payload shape,
    # scope enforcement in both directions, the kill switch on every route,
    # and the workspace archive contract).
    "POST /api/kai/sessions",
    "POST /api/kai/tickets",
    "POST /api/kai/mcp",
    "GET /api/kai/workspace",
    # Collections (bring-your-files) — behaviorally covered in the dedicated
    # suites tests/test_api_collections.py (CRUD/upload/search/reingest, RBAC fail-closed,
    # SessionPrincipal) and tests/test_web_library.py (/library pages), plus the
    # ingestion/retrieval unit suites; not duplicated in this PG smoke harness.
    "POST /api/collections",
    "GET /api/collections",
    "GET /api/collections/search",
    # Unified knowledge search (K2, #797) — requires a `q` param so it has no
    # place in the parameter-free smoke sweep. DuckDB behaviour covered in
    # tests/test_api_knowledge_search.py (shape, 401, RBAC fail-closed);
    # dual-backend grant resolution in tests/db_pg/test_knowledge_search_both.py.
    "GET /api/knowledge/search",
    # K3 local knowledge packaging (#798) — binary artifact download; no new
    # repo methods/migration (state.json lives on disk), so no dual-backend
    # contract test is needed. Behaviour covered in
    # tests/test_api_knowledge_artifacts.py (manifest section, 401/404/200/304,
    # RBAC fail-closed).
    "GET /api/knowledge/artifacts/{corpus_id}/download",
    # Connector-catalogued attachment download (Jira first) — binary
    # byte-stream by id; no new repo methods/migration (the catalogue is an
    # analytics view, the bytes live on disk), RBAC via can_access_table like
    # the parquet download. Behaviour covered in
    # tests/test_attachment_download.py (403 vs miss taxonomy, byte roundtrip,
    # path containment, audit both outcomes, second-source registration).
    "GET /api/attachments/{source}/{attachment_id}/download",
    # K4 maintained digests (#799) — digest markdown content endpoint, RBAC
    # via require_resource_access(KNOWLEDGE_DIGEST). Behaviour covered in
    # tests/test_api_knowledge_digests_distribution.py (manifest kind:"digest"
    # entries, 401/403/404/200, staleness md5 change-token).
    "GET /api/knowledge/digests/{digest_id}/content",
    # Wave-2B job queue REST surface (Task 5) — POST /api/jobs requires a body
    # (`kind`) and enqueue behavior depends on the process-wide `JOB_KINDS`
    # registry (empty outside the app lifespan's `register_all_kinds()`, so
    # tests register their own fake kinds), so it has no place in this
    # parameter-free smoke sweep. Behaviour (401/403, enqueue/get/list,
    # unknown-kind 400, idempotency dedup) covered in tests/test_jobs_api.py;
    # jobs_repo() dual-backend parity already covered by
    # tests/db_pg/test_jobs_contract.py.
    "POST /api/jobs",
    # DuckLake analytics-backend migration (wave-2G Task 6) — requires a
    # body (`to`) and, for `to="ducklake"`, runs a real prerequisite probe
    # (extension/catalog reachability) before enqueueing, so it has no
    # place in this parameter-free smoke sweep. Behaviour (401/403, `to`
    # validation, prerequisite 400/enqueue 202/dedup 409) covered in
    # tests/test_admin_analytics_api.py.
    "POST /api/admin/analytics/migrate",
    "GET /api/jobs",
    "GET /api/jobs/{job_id}",
    "GET /api/collections/{collection_id}",
    "DELETE /api/collections/{collection_id}",
    "POST /api/collections/{collection_id}/files",
    "GET /api/collections/{collection_id}/files",
    "DELETE /api/collections/{collection_id}/files/{file_id}",
    "POST /api/collections/{collection_id}/files/{file_id}/reingest",
    # Library file preview — both routes need a real (collection_id, file_id)
    # pair AND a blob on disk, so neither belongs in this parameter-free sweep.
    # They add no repo method or migration (reads go through corpus_files_repo /
    # corpus_chunks_repo, both already parity-covered). Behaviour covered in
    # tests/test_api_collections.py::TestFilePreview — each `kind`, truncation,
    # the .html inline refusal (415), wrong-collection/unknown-file 404,
    # no-access 404, per-file sharing, SessionPrincipal intersection — and the
    # web wiring in tests/test_web_library_files_folders.py.
    "GET /api/collections/{collection_id}/files/{file_id}/preview",
    "GET /api/collections/{collection_id}/files/{file_id}/raw",
    # Chat composer "+" upload (#966) — multipart file upload, chat-access
    # gated. Behaviour covered in tests/test_chat_uploads.py (happy path,
    # register-as-table + end-to-end query, oversize/415/path-traversal/unauth
    # rejections); not a parameter-free route for this smoke sweep.
    "POST /api/chat/uploads",
    "GET /library",
    "GET /library/{slug}",
    # Authoring studio + suggestion queue + memory-mining consent — covered by
    # dedicated suites (tests/test_authoring_suggestions_api.py, tests/test_web_studio.py);
    # web-form / admin-moderation flows, not part of the parameter-free smoke sweep.
    "GET /admin/studio",
    "GET /admin/studio/suggestions",
    "GET /admin/studio/{domain}",
    "GET /api/admin/authoring-suggestions",
    "GET /api/studio/suggestions/mine",
    "POST /api/studio/suggestions",
    "POST /api/admin/authoring-suggestions/{sid}/approve",
    "POST /api/admin/authoring-suggestions/{sid}/reject",
    "GET /api/studio/memory-mining/consent",
    "POST /api/studio/memory-mining/consent",
    "POST /api/admin/memory-mining/run",
    "GET /me/memory-mining",
    # Admin Moderation & Trust hub (#1118) — the one admin surface linking
    # entity verification, submission review, and marketplace curation.
    # Behaviour covered by tests/test_admin_moderation_hub.py +
    # tests/test_web_nav_moderation_hub.py; it's an admin web page, not a fit
    # for the parameter-free smoke sweep.
    "GET /admin/store",
    # Skill-linter admin moderation surface (v89, #687) — findings list,
    # full-corpus audit, per-finding dismiss. Behaviour covered by
    # tests/test_store_lint_api.py + tests/test_web_store_lint.py; the audit
    # runs a corpus lint (LLM/FTS), not a fit for the parameter-free sweep.
    "GET /admin/store/lint",
    "GET /api/admin/store/lint-findings",
    "POST /api/admin/store/lint-audit",
    "POST /api/admin/store/lint-dismiss",
    # Moderation & Trust hub — an admin card index over the Store review
    # queue. Behaviour covered by tests/test_admin_moderation_hub.py +
    # tests/test_web_nav_moderation_hub.py.
    "GET /admin/store",
    # Skill contribution — admin web-form flow + REST/MCP triple-surface
    # (paste a SKILL.md, publish it to the contributed marketplace). Core logic
    # covered by tests/test_skill_contribution.py and
    # tests/test_admin_contributed_skills_api.py; not duplicated in this smoke sweep.
    "GET /admin/contribute-skill",
    "POST /admin/contribute-skill",
    "POST /admin/contribute-skill/{name}/delete",
    "GET /api/admin/contributed-skills",
    "POST /api/admin/contributed-skills",
    "DELETE /api/admin/contributed-skills/{name}",
    # dulwich smart-HTTP git bridge — requires git repo on disk, explicit non-goal
    "GET /marketplace.git/{path}",
    "POST /marketplace.git/{path}",
    # Per-app git-over-HTTP hosting (Task 6, data apps) — same "requires a
    # git repo on disk" non-goal; auth-matrix behavior covered by
    # tests/test_data_apps_git.py.
    "GET /data-apps.git/{slug}/{path}",
    "POST /data-apps.git/{slug}/{path}",
    # Control-plane REST for hosted data apps (Task 7) — every mutating route
    # needs a real `data_apps` row (+ owner/Admin/grant RBAC, and deploy needs
    # a seeded git repo + runner stub), so none of these are parameter-free.
    # Full CRUD/RBAC/deploy/stop/delete/secrets/logs/readiness/reap-idle
    # coverage lives in tests/test_data_apps_api.py.
    "GET /api/data-apps",
    "POST /api/data-apps",
    "GET /api/data-apps/{slug}",
    "PATCH /api/data-apps/{slug}",
    "POST /api/data-apps/{slug}/deploy",
    "POST /api/data-apps/{slug}/stop",
    "DELETE /api/data-apps/{slug}",
    "PUT /api/data-apps/{slug}/secrets",
    "GET /api/data-apps/{slug}/logs",
    "GET /api/data-apps/{slug}/readiness",
    "POST /api/data-apps/reap-idle",
    # Wave 3B AI-authoring flow (git-credential + drafts) — same "needs a
    # real data_apps row + owner/Admin RBAC" non-goal as the rest of this
    # block. Full coverage lives in tests/test_data_apps_api.py
    # (TestGitCredential, TestDrafts).
    "POST /api/data-apps/{slug}/git-credential",
    "POST /api/data-apps/{slug}/drafts",
    "DELETE /api/data-apps/{slug}/drafts/{draft_slug}",
    # Wave 3C in-chat preview loop (Task 5) — same "needs a real data_apps
    # row + owner/Admin/grant RBAC" non-goal. Full coverage lives in
    # tests/test_data_apps_preview.py (TestPreviewGrantEndpoint) and
    # tests/test_data_apps_proxy.py (proxy accept/reject).
    "POST /api/data-apps/{slug}/preview-grant",
    # Data apps web UI (Task 12) — HTML pages, not part of the parameter-free
    # API smoke sweep (same convention as the other `GET /admin/*` / `GET
    # /library*` web routes above). RBAC/rendering/feature-flag/route-collision
    # behaviour covered by tests/test_web_data_apps.py.
    "GET /apps",
    "GET /apps/detail/{slug}",
    # Google OAuth — requires live credentials
    "GET /auth/google/login",
    "GET /auth/google/callback",
    # Microsoft Entra ID OAuth — requires live credentials
    "GET /auth/microsoft/login",
    "GET /auth/microsoft/callback",
    # Keboola OAuth — redirects to an external OAuth server; behaviour
    # covered by tests/test_keboola_oauth_provider.py
    "GET /auth/keboola/login",
    "GET /auth/keboola/callback",
    # Telegram webhook — live external service
    "POST /api/telegram/webhook",
    # Jira webhooks — live external service
    "POST /api/jira/webhook",
    # Chat SSE / co-presence — requires live sandbox/Anthropic creds
    "GET /api/chat/sessions/{session_id}/stream",
    "POST /api/chat/sessions",
    "DELETE /api/chat/sessions/{session_id}",
    "GET /api/chat/copresence/{session_id}",
    # Slack — live transport
    "POST /api/slack/events",
    "POST /api/slack/interactions",
    "POST /api/slack/slash",
    # HTML web routes — covered by separate UI test suite
    "GET /",
    "GET /{path:path}",
    "GET /{full_path:path}",
    # MCP SSE — live streaming
    "GET /mcp/sse",
    "POST /mcp/messages",
    # CLI auth (device flow) — tested via CLI tests above
    "POST /api/cli/auth/device/init",
    "GET /api/cli/auth/device/poll",
    "GET /api/cli/auth/device/activate",
    # BQ metadata refresh — requires BQ credentials
    "POST /api/admin/bq-metadata-refresh",
    # DB state (internal migration endpoint)
    "GET /api/admin/db-state",
    "POST /api/admin/db-state/migrate",
    # Observability (PostHog proxy) — external service
    "POST /api/observability/capture",
    # Admin adoption / usage dashboards — DuckDB analytics, not business state
    "GET /api/admin/adoption",
    "GET /api/admin/usage",
    "GET /api/admin/usage/summary",
    "GET /api/admin/user-sessions",
    # Cowork bundle — complex real-time feature, dedicated suite planned
    "GET /api/cowork/sessions",
    "POST /api/cowork/sessions",
    "GET /api/cowork/auth/token",
    # Cache warmup — internal
    "POST /api/admin/cache-warmup",
    # Stack / stack-views — admin config pages
    "GET /api/admin/stack",
    "PUT /api/admin/stack",
    "GET /api/admin/stack-views",
    # Initial workspace — one-shot setup
    "POST /api/admin/initial-workspace/trigger",
    "GET /api/admin/initial-workspace/status",
    # Prompts / news — read-only
    "GET /api/prompts",
    "GET /api/news",
    # Memory domains + suggestions — covered by TestMemorySmoke path
    "GET /api/memory/domains",
    "POST /api/memory/domains",
    "GET /api/memory/domain-suggestions",
    # Recipes — admin config
    "GET /api/recipes",
    "GET /api/admin/recipes",
    # Claude.md — read-only
    "GET /api/claude-md",
    # MCP per-table / user-secrets
    "GET /api/mcp/tables/{table_id}/sse",
    "POST /api/mcp/tables/{table_id}/messages",
    "GET /api/mcp/user-secrets",
    "PUT /api/mcp/user-secrets",
    # Admin MCP / slack secrets — operator config
    "GET /api/admin/mcp",
    "PUT /api/admin/mcp",
    "GET /api/admin/slack-secrets",
    "PUT /api/admin/slack-secrets",
    # Source-catalog discovery for the add-data-source wizard's Snowflake picker.
    # Backend-independent: it reads `data_source.snowflake` config and talks to
    # the warehouse over the DuckDB extension, touching neither app-state
    # backend, and every branch (grouping, schema filter, unconfigured, host
    # allowlist, driver failure, admin gate) is covered in
    # tests/test_snowflake_discovery.py. A parameter-free sweep here would only
    # assert that an unconfigured instance answers 400.
    "GET /api/admin/data-sources/{source_type}/tables",
    # Admin source-connections (multi-project Keboola, #731) — tested in test_admin_source_connections.py
    "GET /api/admin/source-connections",
    "POST /api/admin/source-connections",
    "GET /api/admin/source-connections/{connection_id}",
    "PUT /api/admin/source-connections/{connection_id}",
    "DELETE /api/admin/source-connections/{connection_id}",
    "PUT /api/admin/source-connections/{connection_id}/secret",
    "DELETE /api/admin/source-connections/{connection_id}/secret",
    "POST /api/admin/source-connections/{connection_id}/test",
    "GET /api/admin/source-connections/{connection_id}/tables",
    # Semantic-layer coverage — behaviour is backend-independent (it reads the
    # table registry through the repo factory and calls the Metastore), and is
    # covered by tests/test_keboola_semantic_layer_coverage.py plus the endpoint
    # tests in test_keboola_semantic_layer_refresh_endpoint.py. A parameter-free
    # smoke hit here would reach a live Keboola stack.
    "GET /api/admin/semantic-layer/coverage",
    # Derived Keboola chat-tools MCP source — tested in test_keboola_chat_tools.py
    "POST /api/admin/source-connections/{connection_id}/chat-tools",
    "DELETE /api/admin/source-connections/{connection_id}/chat-tools",
    # Admin datasource credentials — vault-backed GWS/BQ instance secrets (web UI only)
    "GET /api/admin/datasource-secrets",
    "GET /admin/datasource-credentials",
    # Admin data-sources page (#755) — tested in test_admin_data_sources_page.py
    "GET /admin/data-sources",
    # Admin semantic-layer sources page (multi-project sync) — tested in
    # tests/test_admin_semantic_layer_page.py.
    "GET /admin/semantic-layer",
    # Guided linked-apps admin wizard (v0.77.28) — tested in
    # tests/test_web_data_apps.py (render + admin-gate).
    "GET /admin/linked-apps",
    # Admin bigquery / keboola test endpoints
    "POST /api/admin/bigquery/test",
    "POST /api/admin/keboola/test",
    # Admin uploads list
    "GET /api/admin/uploads",
    # MCP passthrough
    "GET /api/mcp/passthrough/{path:path}",
    "POST /api/mcp/passthrough/{path:path}",
    # V2 marketplace
    "GET /api/v2/marketplace/items",
    # Welcome
    "GET /api/welcome",
    # Data packages
    "GET /api/data-packages",
    "POST /api/data-packages",
    # Admin chat
    "GET /api/admin/chat",
    "PUT /api/admin/chat",
    "DELETE /admin/chat/{chat_id}",
    "POST /admin/chat/secrets",
    "POST /admin/chat/secrets/test",
    # Connectors
    "GET /api/connectors",
    "POST /api/connectors",
    "GET /api/connectors/manifest",
    "GET /api/connectors/params",
    # HTML admin pages — covered by separate UI test suite
    "GET /admin",  # admin hub — tests/test_web_admin_hub.py
    "GET /admin/access",
    "GET /admin/activity",
    "GET /admin/adoption",
    "GET /admin/adoption/users/{user_id}",
    "GET /admin/agent-prompt",
    "GET /admin/chat",
    "GET /admin/chat/readiness",
    "GET /admin/chat/{chat_id}/debug",
    "POST /admin/chat/{chat_id}/tail-ticket",
    "GET /admin/corporate-memory",
    "GET /admin/database",
    "GET /admin/grants",
    "GET /admin/groups",
    "GET /admin/groups/{group_id}",
    "GET /admin/initial-workspace",
    # Maintained digests admin page (K4, #799) — behaviorally covered in
    # tests/test_admin_knowledge_digests_page.py (admin 200, analyst 403,
    # unauthenticated redirect, nav link).
    "GET /admin/knowledge-digests",
    "GET /admin/marketplaces",
    "GET /admin/mcp-sources",
    "GET /admin/mcp-sources/{source_id}",
    "GET /admin/mcp-tools/{tool_id}/grants",
    "GET /admin/news",
    "GET /admin/prompts",
    "GET /admin/scheduler-runs",
    "GET /admin/server-config",
    "GET /admin/sessions",
    "GET /admin/sessions/{username}/{session_file}",
    "GET /admin/store/submissions",
    "GET /admin/store/submissions/{submission_id}",
    "GET /admin/sync",
    "GET /admin/tables",
    "GET /admin/telemetry",
    "GET /admin/tokens",
    "GET /admin/usage",
    "GET /admin/users",
    "GET /admin/users/{user_id}",
    "GET /admin/workspace-prompt",
    # HTML web pages — covered by separate UI test suite
    "GET /activity-center",
    # Admin audit view over all Data Packages / Memory Domains (catalog
    # reshape) — rendering covered by tests/test_web_catalog_reshape.py.
    "GET /admin/data-packages",
    # A data package's own page (tables · sharing · at a glance) — rendering
    # covered by tests/test_web_admin_package_detail.py.
    "GET /admin/data-packages/{package_id}",
    # Agent builder (rail-layout WIP surface) — rendering covered by
    # tests/test_ui_layout_theme.py::TestRailOptIn.
    "GET /agents",
    # Personal artefacts page (rail-layout IA) — rendering covered by
    # tests/test_ui_layout_theme.py::TestRailOptIn.
    "GET /artefacts",
    # Knowledge-search chat landing (#896) — rendering covered by
    # tests/test_web_ask_landing.py.
    "GET /ask",
    "GET /catalog",
    "GET /catalog/p/{slug}",
    # Semantic-layer browser (#853 + glossary) — covered by
    # tests/test_catalog_semantics_page.py.
    "GET /catalog/semantics",
    "GET /catalog/r/{slug}",
    "GET /catalog/t/{table_id}",
    "GET /chat",
    # Chats inventory page — rendering, filters, row states and bulk actions
    # covered by tests/test_web_chats_page.py.
    "GET /chats",
    "GET /corporate-memory",
    "GET /dashboard",
    "GET /docs",
    "GET /documentation/api",
    "GET /first-time-setup",
    "GET /home",
    # Static explainer page. Covered by tests/test_web_how_it_works.py +
    # tests/test_web_nav_cowork.py.
    "GET /how-it-works",
    "GET /install",
    # Logout (#1675): GET renders the CSRF confirm form, POST validates the
    # double-submit token, revokes server-side and clears the cookie. Both
    # verbs are covered behaviourally in tests/test_web_logout.py, and the
    # revocation half is contract-tested on BOTH backends in
    # tests/db_pg/test_session_revocation.py — richer than this harness's
    # status-code sweep can express.
    "GET /auth/logout",
    "POST /auth/logout",
    "GET /login",
    "GET /login/email",
    "GET /login/password",
    "GET /marketplace",
    "GET /marketplace.zip",
    "GET /marketplace/cowork/{prefixed_name}.zip",
    "GET /marketplace/curated/{marketplace_id}/{plugin_name}",
    "GET /marketplace/curated/{marketplace_id}/{plugin_name}/agent/{agent_name}",
    "GET /marketplace/curated/{marketplace_id}/{plugin_name}/skill/{skill_name}",
    "GET /marketplace/flea/{entity_id}",
    "GET /marketplace/flea/{entity_id}/agent/{agent_name}",
    "GET /marketplace/flea/{entity_id}/edit",
    "GET /marketplace/flea/{entity_id}/skill/{skill_name}",
    "GET /marketplace/format-guide",
    "GET /marketplace/guide/curated",
    "GET /marketplace/guide/flea",
    "GET /marketplace/info",
    "GET /me/activity",
    "GET /me/ai-connector",
    "GET /me/connections",  # per-user MCP connect page tested in tests/test_me_connections_page.py
    "GET /me/cowork",
    "GET /me/mcp",
    "GET /me/profile",
    "GET /me/stats",
    "GET /memory/d/{slug}",
    "GET /news",
    "GET /openapi.json",
    "GET /profile/sessions",
    "GET /profile/sessions/{filename}",
    "GET /redoc",
    # Read-only semantic-layer browse UI (wave 4.2) — the three HTML pages are
    # rendering-covered by tests/test_web_semantic_layer_browse.py, which lives
    # outside the two db_pg modules the coverage aggregator scans, so they are
    # declared here (mirroring GET /catalog/semantics and GET /library).
    "GET /semantic-layer",
    "GET /semantic-layer/{slug}",
    "GET /semantic-layer/{slug}/{object_id}",
    "GET /setup",
    "GET /setup-advanced",
    "GET /slack/bind",
    # Unified My Stack page (rail-layout IA, #896) — rendering covered by
    # tests/test_ui_layout_theme.py::TestRailOptIn.
    "GET /stack",
    "GET /store/examples",
    "GET /store/new",
    "GET /webhooks/jira/health",
    # Auth flows — web form endpoints
    "GET /auth/email/verify",
    "GET /auth/password/reset",
    "GET /auth/password/setup",
    # Self-serve change-password (B6) — session-only credential rotation,
    # covered end-to-end (success/failure/CSRF/rate-limit/PAT-rejection) in
    # tests/test_password_change.py, same as the other password sub-flows
    # below being covered outside this PG smoke harness.
    "GET /auth/password/change",
    "POST /auth/password/change",
    "POST /auth/email/send-link",
    "POST /auth/email/send-link/web",
    "POST /auth/email/verify",
    "POST /auth/password/login/web",
    "POST /auth/password/reset",
    "POST /auth/password/reset/confirm",
    "POST /auth/password/setup",
    "POST /auth/password/setup/confirm",
    "POST /auth/password/setup/request",
    "POST /auth/refresh-groups",
    "POST /cli/auth/exchange",
    "POST /cli/auth/start",
    "GET /cli/auth/start",
    "GET /cli/download",
    "GET /cli/install.sh",
    "GET /cli/latest",
    "GET /cli/wheel/{wheel_name}",
    "POST /api/auth/exchange-setup-token",
    "POST /me/profile/refetch-groups",
    # Debug / introspection — not business logic
    "GET /_debug/throw/exc",
    "GET /_debug/throw/http/{code:int}",
    "GET /api/debug/throw",
    # Admin data-packages — CRUD used as test helper; full suite planned
    "GET /api/admin/data-packages",
    "GET /api/admin/data-packages/{pkg_id}",
    "PUT /api/admin/data-packages/{pkg_id}",
    "DELETE /api/admin/data-packages/{pkg_id}",
    "DELETE /api/admin/data-packages/{pkg_id}/tables/{table_id}",
    "DELETE /api/admin/data-packages/{pkg_id}/tools/{tool_id}",
    "POST /api/admin/data-packages",
    "POST /api/admin/data-packages/{pkg_id}/restore",
    "POST /api/admin/data-packages/{pkg_id}/tables",
    "POST /api/admin/data-packages/{pkg_id}/tools",
    # Admin adoption / analytics — DuckDB analytics panels
    "GET /api/admin/adoption/kpis",
    "GET /api/admin/adoption/series",
    "GET /api/admin/adoption/top-skills",
    "GET /api/admin/adoption/top-users",
    "GET /api/admin/adoption/users/{user_id}/kpis",
    "GET /api/admin/adoption/users/{user_id}/series",
    "GET /api/admin/adoption/users/{user_id}/top-skills",
    "GET /api/admin/adoption/users/{user_id}/top-tools",
    # Admin cache warmup — internal background job
    "GET /api/admin/cache-warmup/status",
    "GET /api/admin/cache-warmup/stream",
    "POST /api/admin/cache-warmup/run",
    # Registry rebuild — fire-and-forget extract/master-view rebuild; behavioral
    # coverage in tests/test_admin_bq_register.py::TestBigQueryDeferRebuild
    "POST /api/admin/registry/rebuild",
    # Admin DB management — migration / job control
    "GET /api/admin/db/job/{job_id}",
    "GET /api/admin/db/state",
    "POST /api/admin/db/cancel/{job_id}",
    "POST /api/admin/db/migrate",
    "DELETE /api/admin/initial-workspace",
    "GET /api/admin/initial-workspace",
    "POST /api/admin/initial-workspace",
    "POST /api/admin/initial-workspace/sync",
    "POST /api/admin/initial-workspace/sync-if-configured",
    # Admin MCP sources/tools — operator config
    "DELETE /api/admin/mcp-sources/{source_id}",
    "DELETE /api/admin/mcp-sources/{source_id}/secret",
    "DELETE /api/admin/mcp-tools/{tool_id}",
    "DELETE /api/admin/mcp-tools/{tool_id}/grants/{group_id}",
    # Dials a connection the admin typed and reports its tools. A smoke test
    # would have to stand up an MCP server or assert on a connection error,
    # neither of which says anything about the endpoint — its contract (build a
    # row-shaped dict, run the SAME url guard and introspection as the
    # registered path, write nothing) is what matters and is covered by reading
    # it. The registered sibling {source_id}/introspect is excluded just below
    # for the same reason.
    "POST /api/admin/mcp-sources/preview-introspect",
    # Grant/revoke a whole MCP source at once — tested in test_keboola_chat_tools.py
    "POST /api/admin/mcp-sources/{source_id}/grants",
    "DELETE /api/admin/mcp-sources/{source_id}/grants/{group_id}",
    "GET /api/admin/mcp-sources",
    "GET /api/admin/mcp-sources/{source_id}",
    "GET /api/admin/mcp-tools",
    "GET /api/admin/mcp-tools/{tool_id}",
    "POST /api/admin/mcp-sources",
    "POST /api/admin/mcp-sources/{source_id}/classify",
    "POST /api/admin/mcp-sources/{source_id}/introspect",
    "POST /api/admin/mcp-sources/{source_id}/materialize",
    "POST /api/admin/mcp-sources/{source_id}/oauth/register",
    "POST /api/admin/mcp-sources/{source_id}/test",
    # OAuth connect flow (spec 2026-07-30 §3) — behaviorally covered in
    # tests/test_mcp_oauth_connect.py (25 cases incl. both backends' repos
    # via the factory); the PG smoke sweep can't drive the browser redirect
    # dance parameter-free.
    "GET /api/mcp/sources/{source_id}/oauth/authorize",
    "GET /api/mcp/oauth-client/callback",
    "DELETE /api/mcp/sources/{source_id}/oauth/connection",
    "POST /api/admin/mcp-tools",
    "POST /api/admin/mcp-tools/{tool_id}/grants",
    "PUT /api/admin/mcp-sources/{source_id}",
    "PUT /api/admin/mcp-sources/{source_id}/oauth/client",
    "PUT /api/admin/mcp-sources/{source_id}/secret",
    "PUT /api/admin/mcp-tools/{tool_id}",
    # Admin memory domains — complex admin feature
    "DELETE /api/admin/memory-domains/{domain_id}",
    "DELETE /api/admin/memory-domains/{domain_id}/items/{item_id}",
    "GET /api/admin/memory-domain-suggestions",
    "GET /api/admin/memory-domain-suggestions/count-pending",
    "GET /api/admin/memory-domains",
    "GET /api/admin/memory-domains/{domain_id}",
    "POST /api/admin/memory-domain-suggestions/{sid}/approve",
    "POST /api/admin/memory-domain-suggestions/{sid}/reject",
    "POST /api/admin/memory-domains",
    "POST /api/admin/memory-domains/{domain_id}/items",
    "POST /api/admin/memory-domains/{domain_id}/restore",
    "PUT /api/admin/memory-domains/{domain_id}",
    # Maintained digests (K4, #799) — admin CRUD behaviorally covered in
    # tests/test_api_knowledge_digests.py (401/403 per method, slug/corpus
    # validation, duplicate slug, PUT/DELETE, resource_grants cleanup); not
    # duplicated in this PG smoke harness.
    "GET /api/admin/knowledge-digests",
    "POST /api/admin/knowledge-digests",
    "GET /api/admin/knowledge-digests/{digest_id}",
    "PUT /api/admin/knowledge-digests/{digest_id}",
    "DELETE /api/admin/knowledge-digests/{digest_id}",
    # Admin news
    "GET /api/admin/news/current",
    "GET /api/admin/news/draft",
    "GET /api/admin/news/versions",
    "GET /api/admin/news/versions/{version}",
    "POST /api/admin/news/preview",
    "POST /api/admin/news/publish",
    "POST /api/admin/news/unpublish/{version}",
    "PUT /api/admin/news/draft",
    # Admin observability
    "DELETE /api/admin/observability/views/{view_id}",
    "GET /api/admin/observability/facets",
    "GET /api/admin/observability/kpis",
    "GET /api/admin/observability/views",
    "POST /api/admin/observability/views",
    # Admin prompts
    "DELETE /api/admin/prompts/{kind}",
    "GET /api/admin/prompts/iwt-files",
    "GET /api/admin/prompts/{kind}",
    "POST /api/admin/prompts/{kind}/bind-git",
    "POST /api/admin/prompts/{kind}/preview",
    "POST /api/admin/prompts/{kind}/source",
    "PUT /api/admin/prompts/{kind}",
    # Admin recipes
    "DELETE /api/admin/recipes/{recipe_id}",
    "GET /api/admin/recipes/{recipe_id}",
    "POST /api/admin/recipes",
    "POST /api/admin/recipes/{recipe_id}/restore",
    "PUT /api/admin/recipes/{recipe_id}",
    # Admin slack secrets
    "DELETE /api/admin/slack-secrets/{name}",
    "PUT /api/admin/slack-secrets/{name}",
    # Admin datasource secrets (per-name mutations)
    "DELETE /api/admin/datasource-secrets/{name}",
    "PUT /api/admin/datasource-secrets/{name}",
    # GWS client_id format check (no DB/network) — covered by
    # tests/test_admin_datasource_secrets.py
    "POST /api/admin/validate-gws-credentials",
    # Admin store submissions (detail/actions beyond list)
    "DELETE /api/admin/store/submissions/{submission_id}",
    "GET /api/admin/store/submissions/{submission_id}",
    "GET /api/admin/store/submissions/{submission_id}/bundle.zip",
    "POST /api/admin/store/submissions/{submission_id}/override",
    "POST /api/admin/store/submissions/{submission_id}/rescan",
    "POST /api/admin/store/submissions/{submission_id}/retry",
    # Admin telemetry
    "GET /api/admin/telemetry/export",
    "GET /api/admin/telemetry/facets",
    "GET /api/admin/telemetry/kpis",
    "GET /api/admin/telemetry/query",
    "GET /api/admin/telemetry/summary",
    "POST /api/admin/telemetry/ask",
    "POST /api/admin/telemetry/prune",
    "POST /api/admin/telemetry/reprocess",
    # Admin sessions downloads
    "GET /api/admin/sessions/{username}/{session_file}/download",
    "GET /api/admin/sessions/{username}/{session_file}/transcript",
    # Admin users (per-user detail views)
    "DELETE /api/admin/users/{user_id}/memberships/{group_id}",
    "GET /api/admin/users/{user_id}/activity",
    "GET /api/admin/users/{user_id}/effective-access",
    "GET /api/admin/users/{user_id}/memberships",
    "GET /api/admin/users/{user_id}/sessions",
    "GET /api/admin/users/{user_id}/sessions/download-all",
    "GET /api/admin/users/{user_id}/sessions/{session_file}/download",
    "POST /api/admin/users/{user_id}/memberships",
    # Admin welcome/workspace templates
    "DELETE /api/admin/welcome-template",
    "DELETE /api/admin/workspace-prompt-template",
    "GET /api/admin/welcome-template",
    "GET /api/admin/workspace-prompt-template",
    "POST /api/admin/welcome-template/preview",
    "POST /api/admin/workspace-prompt-template/preview",
    "PUT /api/admin/welcome-template",
    "PUT /api/admin/workspace-prompt-template",
    # Admin misc operations
    "DELETE /api/admin/metrics/{metric_id}",
    # Access-policy no-SQL builder helpers — admin-only authoring surfaces that
    # take a path param (and, for compile, a request body); behaviorally covered
    # on both backends in tests/test_admin_access_policy_builder_api.py
    # (schema+samples, spec->SQL, table-name-from-registry, RBAC, 404), not
    # duplicated in this parameter-free smoke sweep.
    "GET /api/admin/registry/{table_id}/policy/columns",
    "POST /api/admin/registry/{table_id}/policy/compile",
    "PATCH /api/admin/registry/{table_id}/docs",
    "POST /api/admin/bigquery/test-connection",
    "POST /api/admin/discover-and-register",
    "POST /api/admin/keboola/test-connection",
    "POST /api/admin/metadata/{table_id}",
    "POST /api/admin/metrics",
    "POST /api/admin/run-blocked-purge",
    # B8 audit-trail seam — scheduler-driven audit_log retention prune, mirrors
    # run-knowledge-digests. The new repo method (AuditRepository/AuditPgRepository
    # .prune_older_than) IS dual-backend proven, by
    # tests/db_pg/test_audit_contract.py::test_prune_older_than_*. Endpoint
    # behaviour (config gate, logging, audit row) covered single-backend in
    # tests/test_audit_retention.py.
    "POST /api/admin/run-audit-prune",
    # Track E3 Slice 1 — generalized per-trail retention sweep (sync_history /
    # llm_usage / agent_scope_snapshots), mirrors run-audit-prune above. The
    # new repo methods (SyncStateRepository.prune_history_older_than,
    # LlmUsageRepository.prune_older_than,
    # AgentsRepository.prune_scope_snapshots_older_than) and their _pg
    # siblings ARE dual-backend proven, by
    # tests/db_pg/test_sync_state_contract.py, test_llm_usage_contract.py,
    # and test_agents_contract.py. Endpoint behaviour (config gate, logging,
    # audit row) covered single-backend in tests/test_audit_retention.py.
    "POST /api/admin/run-retention-prune",
    "POST /api/admin/run-bq-metadata-refresh",
    "POST /api/admin/run-corporate-memory",
    "POST /api/admin/run-jira-consistency-check",
    "POST /api/admin/run-jira-sla-poll",
    # Keboola semantic layer (Metastore) sync — scheduler-driven admin
    # maintenance op, mirrors run-bq-metadata-refresh. No dual-backend
    # contract test needed (no new repo methods/migration). Behaviour
    # covered in tests/test_keboola_semantic_layer_refresh_endpoint.py.
    "POST /api/admin/run-keboola-semantic-layer-refresh",
    # Databricks semantic layer (Unity Catalog metric views) sync — same
    # shape as the Keboola sibling above: scheduler-driven admin maintenance
    # op, no new repo methods/migration. Behaviour covered in
    # tests/test_databricks_semantic_layer_refresh_endpoint.py.
    # The handler never touches the backend switch itself; the repo calls its
    # sync drives (metric_repo().create/find_by_name/list/delete, incl. the
    # source_ref kwarg) are already parity-proven on both backends by
    # tests/db_pg/test_config_pg.py::test_metric_source_ref_roundtrip and
    # tests/db_pg/test_ported_methods_contract.py::test_metrics_yaml_reconcile_prunes_on_both_backends
    # — cited here so this exclusion is self-verifying rather than resting on
    # "nothing new here".
    "POST /api/admin/run-databricks-semantic-layer-refresh",
    # K3 local knowledge packaging (#798) — scheduler-driven admin maintenance
    # op, mirrors run-corporate-memory. No dual-backend contract test needed
    # (no new repo methods/migration; state.json lives on disk). Behaviour
    # covered in tests/test_admin_run_endpoints.py::TestRunKnowledgePackaging.
    "POST /api/admin/run-knowledge-packaging",
    # K4 maintained digests (#799) — scheduler-driven admin maintenance op,
    # mirrors run-knowledge-packaging / run-corporate-memory. No new repo
    # methods/migration beyond the existing knowledge_digests contract test
    # (tests/db_pg/test_knowledge_digests_contract.py). Behaviour covered in
    # tests/test_admin_run_endpoints.py::TestRunKnowledgeDigests.
    "POST /api/admin/run-knowledge-digests",
    "POST /api/admin/run-knowledge-migration",
    "POST /api/mcp-connect/token",  # tested in tests/test_mcp_connect.py
    "GET /mcp-connect",  # web UI page tested in tests/test_mcp_connect.py
    "POST /api/admin/run-session-collector",
    "POST /api/admin/run-session-processor",
    "POST /api/admin/uploads/cover-image",
    # Catalog detail views
    "GET /api/catalog/metrics/{metric_path}",
    "GET /api/catalog/profile/{table_name}",
    "POST /api/catalog/profile/{table_name}/refresh",
    # Chat (beyond live SSE)
    "DELETE /api/chat/sessions/{chat_id}",
    "GET /api/chat/sessions",
    "GET /api/chat/sessions/{chat_id}/messages",
    "GET /api/chat/{session_id}/messages",
    "GET /api/chat/skills",  # tested in tests/test_chat_skills_endpoint.py
    "GET /api/chat/journey",  # tested in tests/test_chat_api.py
    "PUT /api/chat/journey",  # tested in tests/test_chat_api.py
    # History row menu — pin/unpin and rename. Both are self-scoped writes on
    # the caller's own session; tested in tests/test_chat_pin_conversations.py.
    "PUT /api/chat/sessions/{chat_id}/pin",
    "PUT /api/chat/sessions/{chat_id}/title",
    # Chats page (/chats) archive lifecycle — archive/restore and permanent
    # delete, self-scoped writes on the caller's own session (404, never 403);
    # behaviour covered in tests/test_web_chats_page.py on both backends'
    # session repos (tests/db_pg/test_chat_pg.py), not parameter-free.
    "PUT /api/chat/sessions/{chat_id}/archived",
    "DELETE /api/chat/sessions/{chat_id}/permanent",
    # Session-workspace file delivery (#1611) — owner-scoped reads over the
    # caller's own session dir plus the save-to-Library bridge. No new repo
    # methods/migration (ownership rides chat_repo.get_session, the artefact
    # path reuses create_single_file_artefact — both already parity-proven);
    # behaviour (ownership 404s, traversal/symlink containment, download
    # headers, artefact creation) covered in tests/test_chat_session_files.py.
    "GET /api/chat/sessions/{chat_id}/files",
    "GET /api/chat/sessions/{chat_id}/files/download",
    "POST /api/chat/sessions/{chat_id}/files/save-artefact",
    "POST /api/chat/sessions/{chat_id}/ticket",
    "POST /api/chat/{session_id}/fork",
    "POST /api/chat/{session_id}/invite",
    "POST /api/chat/{session_id}/join-ticket",
    "POST /api/chat/{session_id}/leave",
    # Data packages (user-facing slug lookup)
    "GET /api/data-packages/{slug}",
    # Initial workspace
    "GET /api/initial-workspace",
    "GET /api/initial-workspace.zip",
    "POST /api/initial-workspace/applied",
    # Marketplace detail / asset endpoints
    "DELETE /api/marketplace/curated/{marketplace_id}/{plugin_name}/install",
    "DELETE /api/marketplaces/{marketplace_id}",
    "DELETE /api/marketplaces/{marketplace_id}/plugins/{plugin_name}/system",
    "POST /api/marketplaces/{marketplace_id}/plugins/{plugin_name}/disable",
    "POST /api/marketplaces/{marketplace_id}/plugins/{plugin_name}/enable",
    "GET /api/marketplace/curated/{marketplace_id}/{plugin_name}",
    "GET /api/marketplace/curated/{marketplace_id}/{plugin_name}/agent/{agent_name}",
    "GET /api/marketplace/curated/{marketplace_id}/{plugin_name}/asset/{path}",
    "GET /api/marketplace/curated/{marketplace_id}/{plugin_name}/doc/{path}",
    "GET /api/marketplace/curated/{marketplace_id}/{plugin_name}/mirrored/{key}",
    "GET /api/marketplace/curated/{marketplace_id}/{plugin_name}/skill/{skill_name}",
    "GET /api/marketplace/flea/{entity_id}/agent/{agent_name}",
    "GET /api/marketplace/flea/{entity_id}/skill/{skill_name}",
    "GET /api/marketplaces/{marketplace_id}/plugins",
    "PATCH /api/marketplaces/{marketplace_id}",
    "POST /api/marketplace/curated/{marketplace_id}/{plugin_name}/install",
    "POST /api/marketplaces/sync-all",
    "POST /api/marketplaces/{marketplace_id}/plugins/{plugin_name}/system",
    "POST /api/marketplaces/{marketplace_id}/sync",
    # MCP passthrough / user secrets
    "DELETE /api/mcp/sources/{source_id}/my-secret",
    "GET /api/mcp/passthrough/tools",
    "GET /api/mcp/sources/{source_id}/my-secret",
    "POST /api/mcp/passthrough/tools/{tool_id}/call",
    "POST /api/mcp/query-table/{table_id}",
    "POST /api/mcp/sources/{source_id}/my-secret/test",  # gates tested in tests/test_mcp_passthrough_api.py
    "PUT /api/mcp/sources/{source_id}/my-secret",
    # Memory advanced routes (audit, votes, tree, etc.)
    "DELETE /api/memory/{item_id}/dismiss",
    "GET /api/memory-domain-suggestions/mine",
    "GET /api/memory/admin/audit",
    "GET /api/memory/admin/contradictions",
    "GET /api/memory/admin/duplicate-candidates",
    "GET /api/memory/admin/pending",
    "GET /api/memory/admin/{item_id}",
    "GET /api/memory/bundle",
    "GET /api/memory/domains/{slug}",
    "GET /api/memory/my-contributions",
    "GET /api/memory/my-votes",
    "GET /api/memory/tree",
    "PATCH /api/memory/admin/{item_id}",
    "POST /api/memory-domain-suggestions",
    "POST /api/memory/admin/approve",
    "POST /api/memory/admin/batch",
    "POST /api/memory/admin/bulk-update",
    "POST /api/memory/admin/contradictions",
    "POST /api/memory/admin/contradictions/{contradiction_id}/resolve",
    "POST /api/memory/admin/duplicate-candidates/resolve",
    "POST /api/memory/admin/edit",
    "POST /api/memory/admin/mandate",
    "POST /api/memory/admin/revoke",
    "POST /api/memory/items/{item_id}/mark-mandatory",
    "POST /api/memory/items/{item_id}/mark-unmandatory",
    "POST /api/memory/{item_id}/dismiss",
    "POST /api/memory/{item_id}/personal",
    # Metrics (user-facing)
    "GET /api/metrics/{metric_id}",
    # Recipes (user-facing)
    "GET /api/recipes/{slug}",
    # Scripts (run/deploy actions beyond list)
    "DELETE /api/scripts/{script_id}",
    "POST /api/scripts/deploy",
    "POST /api/scripts/run",
    "POST /api/scripts/run-due",
    "POST /api/scripts/{script_id}/run",
    # Stack (new subscription API)
    "DELETE /api/stack/subscription/{resource_type}/{resource_id}",
    "GET /api/stack",
    "GET /api/stack/browse",
    "POST /api/stack/subscribe",
    # Library files-as-folders: per-file detail page + drag-to-move, both
    # covered by tests/test_web_library_files_folders.py (DuckDB) and, for the
    # repository layer, tests/db_pg/test_corpus_files_contract.py.
    "GET /library/{slug}/f/{file_id}",
    "POST /api/collections/{collection_id}/files/{file_id}/move",
    # Library: agent registry (v103) + owner-initiated sharing — covered by
    # tests/test_web_library_sharing.py (DuckDB) and, for the repository layer,
    # the cross-engine tests/db_pg/test_agents_contract.py; no dedicated PG
    # smoke class yet, same convention as the stack rows below.
    #
    # `/api/agents*` (the builder's own adapter router) is NOT listed here
    # any more — it was deleted outright by the remediation-program's "one
    # agent model" Track C1 (Task C1.2), so its routes no longer exist at
    # all rather than being merely untested. `/api/v1/agents*` (which
    # absorbed its wire shape in Task C1.1) is the sole surviving surface —
    # already covered elsewhere, unaffected by that deletion.
    # Builder-assistant turn — covered by tests/test_agent_builder_turns.py
    # (DuckDB): the sanitizer trust boundary, the apply=false / config
    # working-copy contract, and the no-credential degradation. It has no
    # PG-specific behaviour of its own: every write it makes goes through
    # PUT /api/v1/agents/{agent_id}, whose backend split is already
    # exercised by tests/db_pg/test_agents_contract.py.
    "POST /api/agents/{agent_id}/builder/turn",
    # One /skills builder turn. Behaviourally covered by
    # tests/test_entity_builder_turns.py. Stateless and writes nothing — no
    # persistence of its own for a PG smoke test to exercise.
    "POST /api/store/entities/builder/turn",
    # The template-preview scratch agent. Behaviourally covered by
    # tests/test_entity_builder_turns.py; the repo-level invisibility it
    # depends on is pinned across BOTH backends by
    # tests/db_pg/test_agents_contract.py.
    "POST /api/store/entities/builder/preview-agent",
    # One data-package builder turn. Behaviourally covered by
    # tests/test_package_builder_turns.py. Reads the registry, the group list
    # and the metric definitions (all symmetric pairs) to build its candidate
    # sets; writes nothing.
    "POST /api/admin/data-packages/builder/turn",
    "GET /api/sharing/groups",
    "GET /api/sharing/{resource_type}/{resource_id}",
    "PUT /api/sharing/{resource_type}/{resource_id}",
    # Agent-sharing approval queue (Track C6, PG-only). Covered end to end
    # (queue/approve/reject, C2.3 runtime honoring an approved grant, admin
    # RBAC, moderation-hub UI wiring, DuckDB typed-501 fail-clean) by
    # tests/db_pg/test_agent_share_approval_pg.py + the repo-level tests in
    # tests/db_pg/test_share_requests_pg.py; not duplicated here.
    "GET /api/admin/share-requests",
    "PATCH /api/admin/share-requests/{request_id}",
    # Skill builder index page (HTML surface, no PG-specific behaviour).
    "GET /skills",
    # Data-package builder page — the same HTML surface, hosting the drawer
    # component in page mode. No PG-specific behaviour of its own; the
    # package writes it performs are the /api/admin/data-packages routes.
    "GET /admin/data-packages/new",
    # Add artefacts to My Stack — covered by tests/test_web_stack_artefacts.py
    # (DuckDB) + tests/test_cli_api_parity.py (add/remove parity); no
    # dedicated PG smoke class yet, same convention as the stack rows above.
    "DELETE /api/stack/artefacts/{corpus_id}",
    "GET /api/stack/artefacts/candidates",
    "POST /api/stack/artefacts/{corpus_id}",
    # Store version restore
    "POST /api/store/entities/{entity_id}/versions/{version_no}/restore",
    # Sync (pull-confirm / settings)
    "POST /api/sync/pull-confirm",
    "POST /api/sync/settings",
    "POST /api/sync/table-subscriptions",
    # Telegram
    "GET /api/telegram/status",
    "POST /api/telegram/unlink",
    "POST /api/telegram/verify",
    # User setup tokens
    "DELETE /api/user/setup-tokens/{token_id}",
    "GET /api/user/setup-tokens",
    # User cowork
    "POST /api/user/cowork-bundle",
    # V2 metadata/marketplace
    "GET /api/v2/marketplace/skills",
    "GET /api/v2/metadata-cache/status",
    "POST /api/v2/metadata-cache/refresh",
    # Jira webhooks / slack bind
    "GET /slack/bind",
    # POST /slack/bind redeems a bind code and needs a matching double-submit
    # CSRF cookie + form (security audit F2), so it can't be a parameter-free
    # smoke hit — behaviour is covered in tests/test_slack_magic_link_bind.py.
    "POST /slack/bind",
    "POST /api/slack/bind",
    "POST /api/slack/commands",
    "POST /api/slack/interactivity",
    "POST /webhooks/jira",
    # My-stack curated toggle
    "PUT /api/my-stack/curated/{marketplace_id}/{plugin_name}",
    # Agent management (v96 agent profiles + agent-as-API, Task 5) — owner-scoped
    # CRUD + scope + agent PAT issuance. Behaviour (ownership 404/403 matrix,
    # slug validation/conflict, scope dedupe, PAT-issuance mode gate) covered by
    # tests/test_agents_management_api.py; not duplicated in this PG smoke sweep.
    "POST /api/v1/agents",
    "GET /api/v1/agents",
    "GET /api/v1/agents/{agent_id}",
    "PUT /api/v1/agents/{agent_id}",
    "DELETE /api/v1/agents/{agent_id}",
    "PUT /api/v1/agents/{agent_id}/scope",
    "POST /api/v1/agents/{agent_id}/tokens",
    # Agent-as-API runtime (Task 9) — auth chain, idempotency, sync/background/
    # timeout-degrade paths covered by tests/test_agent_responses_api.py; not
    # duplicated in this PG smoke sweep.
    "POST /api/v1/agents/{slug}/responses",
    "GET /api/v1/jobs/{job_id}",
    # @delegation between shared agents (Track C7 MVP) — sandbox-internal
    # RPC, reachable only through the secret broker under a live turn's own
    # session-scoped ticket (see app/api/agent_delegation.py's module
    # docstring) — never parameter-free from an ordinary credential, same
    # shape as the broker routes above. Behaviour (depth-1 guard, one-per-
    # turn guard, RBAC denial, budget-exhausted degrade, output visible,
    # the HTTP seam driven by a real AgentPrincipal, and the fail-closed
    # guard against an unresolved caller) covered by
    # tests/test_agent_delegation.py; not duplicated in this PG smoke
    # sweep. The mandatory caller-bound-row laundering guard specifically
    # ALSO runs against a real Postgres backend — see
    # tests/db_pg/test_agent_delegation_pg.py — rather than being exempted
    # here, since it is the one assertion this exemption cannot silently
    # cover for both backends.
    "POST /api/v1/agents/{slug}/delegate",
    # Agent-as-API multi-turn sessions (V1b Task 4) — SSE turn streaming,
    # cancel, history, delete. Auth chain (owner/agent-PAT 404 matrix),
    # SSE framing (RUN_STARTED once/turn, id: lines), turn-in-flight 409,
    # and the create/history/cancel/delete lifecycle are covered by
    # tests/test_agent_sessions_api.py; not duplicated in this PG smoke
    # sweep (an open SSE response doesn't fit this harness's
    # parameter-free happy-path-status-code shape).
    "POST /api/v1/agents/{slug}/sessions",
    "POST /api/v1/sessions/{session_id}/messages",
    "GET /api/v1/sessions/{session_id}",
    "POST /api/v1/sessions/{session_id}/cancel",
    "DELETE /api/v1/sessions/{session_id}",
    # Agent usage (V1a Task 9 follow-up) — token-budget usage summary for an
    # owner-scoped agent. Behaviour covered by tests/test_agent_usage_api.py;
    # not duplicated in this PG smoke sweep.
    "GET /api/v1/agents/{slug}/usage",
    # Agent-as-API outbound webhooks (V1b Task 6) — CRUD for owner-scoped
    # webhook subscriptions (SSRF-hardened URL validation, HMAC secret
    # issuance, active-events set). Behaviour covered by
    # tests/test_agent_webhooks_api.py; not duplicated in this PG smoke sweep.
    "GET /api/v1/agents/{slug}/webhooks",
    "POST /api/v1/agents/{slug}/webhooks",
    "DELETE /api/v1/agents/{slug}/webhooks/{webhook_id}",
    # Agent schedules (v119) — owner-scoped CRUD for per-agent scheduled runs
    # plus the admin/scheduler-driven run-due sweep. Auth matrix (cross-owner
    # 404, agent-PAT reject), validation, cap, due/claim/dispatch semantics
    # covered by tests/test_agent_schedules_api.py; not duplicated in this PG
    # smoke sweep.
    "GET /api/v1/agents/{slug}/schedules",
    "POST /api/v1/agents/{slug}/schedules",
    "PATCH /api/v1/agents/{slug}/schedules/{schedule_id}",
    "DELETE /api/v1/agents/{slug}/schedules/{schedule_id}",
    "POST /api/v1/agents/run-due",
    # Agent memory admin (V1b) — owner-scoped list/approve-or-edit/reject of
    # an agent's candidate memories. Behaviour covered by
    # tests/test_agent_memory_admin_api.py; not duplicated in this PG smoke
    # sweep.
    "GET /api/v1/agents/{agent_id}/memories",
    "PATCH /api/v1/agents/{agent_id}/memories/{memory_id}",
    "DELETE /api/v1/agents/{agent_id}/memories/{memory_id}",
    # Agent memory write (V1b) — session-scoped "remember" tool write path.
    # Behaviour covered by tests/test_agent_memory_write_api.py; not
    # duplicated in this PG smoke sweep.
    "POST /api/v1/sessions/{session_id}/memories",
    # Agent session artifacts (V1c) — sandbox-harvested artifact listing/
    # download for a multi-turn agent session. Behaviour covered by
    # tests/test_agent_artifacts_api.py; not duplicated in this PG smoke
    # sweep.
    "GET /api/v1/sessions/{session_id}/artifacts",
    "GET /api/v1/sessions/{session_id}/artifacts/{artifact_id}",
    # Data-apps ingress proxy (Task 8) — `GET /apps/{slug}` (redirect to the
    # trailing-slash form) is the only piece of this surface that still
    # appears in the OpenAPI schema: the catch-all proxy/wake/holding-page
    # route (`/apps/{slug}/{path}`, all methods) is registered with
    # `include_in_schema=False` (see app/api/data_apps_proxy.py's
    # `proxy_app` docstring for why) and so never reaches `all_routes`
    # here at all. Behaviour covered in tests/test_data_apps_proxy.py.
    "GET /apps/{slug}",
    # SharePoint connect wizard admin API (spec 2026-08-27 §13.2) — live
    # Graph folder-tree browse + scope->collection confirmation. All state
    # lives in `source_connections.config` (existing JSON column, both
    # backends) plus ordinary `file_corpora`/`resource_grants` rows (existing
    # tables) — no new schema surface to verify per-backend. Auth matrix,
    # typed cert-missing/Graph-error responses (Graph mocked via
    # httpx.MockTransport), scope->collection idempotency, the no-group
    # warning, and the corpus-map producer handoff are all covered by
    # tests/test_admin_sharepoint.py; not duplicated in this PG smoke sweep.
    "GET /api/admin/sharepoint/connections/{connection_id}/tree",
    # Bounded BFS folder search (TCRD-240) over the same live tree — never
    # Graph's own `/search`. Same "no new schema surface" reasoning as the
    # sibling `/tree` route above; auth matrix, query-length/mode/glob
    # validation, subtree scoping, and cap-clamping are all covered by
    # tests/test_admin_sharepoint.py::TestTreeSearch.
    "GET /api/admin/sharepoint/connections/{connection_id}/tree/search",
    "GET /api/admin/sharepoint/connections/{connection_id}/scopes",
    "POST /api/admin/sharepoint/connections/{connection_id}/scopes",
    "DELETE /api/admin/sharepoint/connections/{connection_id}/scopes",
    "GET /api/admin/sharepoint/connections/{connection_id}/corpus-map",
    # Certificate metadata (thumbprint/subject/issuer/expiry) — derived at
    # request time from the connection's own stored PEM, no new schema
    # surface. Auth matrix + typed-absence paths covered by
    # tests/test_admin_sharepoint.py::TestCertificateMetadata; not
    # duplicated in this PG smoke sweep.
    "GET /api/admin/sharepoint/connections/{connection_id}/certificate",
    # Extraction enqueue wiring (TCRD-226) — enqueues into the EXISTING
    # `jobs` table (both backends) via the existing `jobs_repo()`/
    # `source_connections_repo()` factories; no new schema surface to
    # verify per-backend. Auth matrix, 404-before-work, the feature-usable
    # gate, duplicate-run dedup, exact payload shape, and the sweep's
    # due-check/no-op paths are all covered by
    # tests/test_admin_sharepoint.py::TestExtractionTrigger /
    # TestExtractionRunDue; not duplicated in this PG smoke sweep.
    "POST /api/admin/sharepoint/connections/{connection_id}/extract",
    "POST /api/admin/sharepoint/extraction/run-due",
    # Ontology builder (spec §13.2) — the admin builder-shell page and its
    # draft CRUD + state-machine actions + dry-run are covered directly by
    # tests/test_api_ontology.py, tests/test_web_admin_ontology_page.py and
    # tests/db_pg/test_ontology_admin_pg.py (auth matrix, flag gate, DuckDB
    # typed-501, Save-only-write, mocked-LLM dry-run); not duplicated here.
    "GET /admin/ontology",
    "GET /api/admin/ontology/drafts",
    "POST /api/admin/ontology/drafts",
    "GET /api/admin/ontology/drafts/{draft_id}",
    "PUT /api/admin/ontology/drafts/{draft_id}",
    "DELETE /api/admin/ontology/drafts/{draft_id}",
    "POST /api/admin/ontology/drafts/{draft_id}/import",
    "POST /api/admin/ontology/drafts/{draft_id}/save",
    "POST /api/admin/ontology/dry-run",
    # Persisted ingest-run reports for the source card (spec §7.2/§13.2) —
    # covered by tests/db_pg/test_facts_ingest_runs_pg.py + the source-card
    # PG test; the write happens post-ingest in tests/db_pg/test_facts_ingest_pg.py.
    "GET /api/facts/ingest-runs",
    # Node-type counts for the Library's Knowledge tab (TCRD-250) — covered
    # by tests/test_api_facts.py (flag-off 404, auth, DuckDB typed-501) and
    # tests/db_pg/test_facts_read_pg.py (per-caller counts, a type the
    # caller cannot see is absent, agreement with search()); not duplicated
    # here.
    "GET /api/facts/type-map",
    # The maintained digests a caller can read (TCRD-250) — covered by
    # tests/test_api_knowledge_digests_distribution.py::TestAnalystDigestList
    # (401, RBAC both ways, never-generated omitted, staleness, no markdown
    # in the list, and agreement with the sync manifest for the same
    # caller); not duplicated here.
    "GET /api/knowledge/digests",
}


def _collect_covered_routes() -> set:
    """Aggregate COVERED_ROUTES from every test class in both smoke and behavioral files."""
    import importlib

    covered: set = set()
    for mod_name in (
        "tests.db_pg.test_endpoints_smoke",
        "tests.db_pg.test_endpoints_behavioral",
    ):
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:
            continue
        for obj in vars(mod).values():
            if isinstance(obj, type) and hasattr(obj, "COVERED_ROUTES"):
                covered.update(obj.COVERED_ROUTES)
    return covered


def test_every_route_is_covered_or_excluded():
    """Route-coverage guard: fails CI when a new endpoint has no test or exclusion.

    Uses a subprocess to inspect routes so xdist worker state (importlib.reload
    calls from state_backend fixture) cannot corrupt the route set. The subprocess
    imports app.main in a clean Python process and emits JSON to stdout.

    Uses app.openapi()["paths"] rather than iterating app.routes directly.
    Starlette 1.3.x wraps included routers in _IncludedRouter objects that lack
    .path/.methods attributes, so direct iteration raises AttributeError. The
    OpenAPI schema is the authoritative flat route list regardless of Starlette
    version. Note: OpenAPI strips the :path convertor suffix from path parameters
    ({metric_id:path} -> {metric_id}), so KNOWN_UNTESTED entries must use the
    plain {param} form.
    """
    import json
    import os
    import subprocess
    import sys

    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    script = (
        "import json, warnings; warnings.filterwarnings('ignore'); "
        "from app.main import app; "
        "schema = app.openapi(); "
        "rows = [{'path': p, 'methods': list(ms.keys())} "
        "for p, ms in schema.get('paths', {}).items()]; "
        "print(json.dumps(rows))"
    )
    result = subprocess.run(
        [sys.executable, "-W", "ignore", "-c", script],
        capture_output=True,
        text=True,
        cwd=repo_root,
        env={**os.environ, "PYTHONPATH": repo_root},
    )
    assert result.returncode == 0, f"Route inspection subprocess failed (exit {result.returncode}):\n{result.stderr}"
    routes_data = json.loads(result.stdout)
    all_routes = {
        f"{m.upper()} {r['path']}" for r in routes_data for m in r["methods"] if m.upper() not in ("HEAD", "OPTIONS")
    }
    covered = _collect_covered_routes() | KNOWN_UNTESTED
    missing = sorted(all_routes - covered)
    assert not missing, (
        "Routes with no smoke/behavioral coverage and no KNOWN_UNTESTED entry "
        "(add a test class entry or a justified KNOWN_UNTESTED exclusion): "
        f"{missing}"
    )
    # Reverse drift: covered routes that no longer exist in the app
    stale = sorted((covered - KNOWN_UNTESTED) - all_routes)
    assert not stale, (
        "COVERED_ROUTES entries that no longer exist as app routes "
        "(remove them from the class COVERED_ROUTES set): "
        f"{stale}"
    )


# ---------------------------------------------------------------------------
# Semantic layer  (canonical Ossie document store + its sources)
# ---------------------------------------------------------------------------


_SEMANTIC_DOC = (
    "version: '0.2.0.dev0'\n"
    "semantic_model:\n"
    "  - name: smoke_model\n"
    "    datasets:\n"
    "      - name: orders\n"
    "        source: db.public.orders\n"
)


class TestSemanticLayerSmoke:
    COVERED_ROUTES = {
        "GET /api/admin/semantic-models",
        "POST /api/admin/semantic-models",
        "GET /api/admin/semantic-models/{model_id}",
        "PUT /api/admin/semantic-models/{model_id}",
        "DELETE /api/admin/semantic-models/{model_id}",
        "GET /api/admin/semantic-sources",
        "POST /api/admin/semantic-sources",
        "GET /api/admin/semantic-sources/{source_id}",
        "PUT /api/admin/semantic-sources/{source_id}",
        "DELETE /api/admin/semantic-sources/{source_id}",
        "POST /api/admin/semantic-sources/{source_id}/sync",
        "GET /api/semantic-models/search",
        "GET /api/semantic-models/{slug}.yaml",
        "POST /api/semantic-models/validate-query",
        "GET /api/semantic-models/context",
        "GET /api/semantic-models/schema",
        "POST /api/semantic-models/apply",
    }

    def test_model_crud_and_export(self, seeded_app_both):
        c = seeded_app_both["client"]
        h = _admin_headers(seeded_app_both)

        created = c.post("/api/admin/semantic-models", json={"document": _SEMANTIC_DOC}, headers=h)
        assert created.status_code == 201
        model_id = created.json()["id"]

        assert c.get("/api/admin/semantic-models", headers=h).status_code == 200
        assert c.get(f"/api/admin/semantic-models/{model_id}", headers=h).status_code == 200

        # A hand-authored model stays editable; a source-owned one would 409.
        assert (
            c.put(
                f"/api/admin/semantic-models/{model_id}",
                json={"description": "smoke"},
                headers=h,
            ).status_code
            == 200
        )

        exported = c.get("/api/semantic-models/smoke_model.yaml", headers=h)
        assert exported.status_code == 200
        assert exported.text == _SEMANTIC_DOC

        assert c.get("/api/semantic-models/search?q=smoke", headers=h).status_code == 200

        validated = c.post(
            "/api/semantic-models/validate-query",
            json={"sql": "SELECT * FROM orders"},
            headers=h,
        )
        assert validated.status_code == 200
        assert validated.json()["available"] is True

        context = c.get(
            "/api/semantic-models/context",
            params={"selections": '[{"semantic_type": "dataset"}]'},
            headers=h,
        )
        assert context.status_code == 200
        assert context.json()["results"][0]["objects"]

        schema = c.get(
            "/api/semantic-models/schema",
            params={"semantic_types": ["dataset"]},
            headers=h,
        )
        assert schema.status_code == 200
        assert "Dataset" in schema.json()["$defs"]

        assert c.delete(f"/api/admin/semantic-models/{model_id}", headers=h).status_code == 204

    def test_apply_branches_on_authority(self, seeded_app_both, monkeypatch):
        """The one write surface (chat-first authoring): an admin's document
        applies directly; a non-admin's lands in the moderation queue and
        never touches ``semantic_models`` before approval.

        The non-admin half files into the STUDIO's suggestion queue, so it
        reads ``get_studio_enabled()`` — off by default since the admin cleanup
        retired that surface, which 403s the branch. Turned on here because the
        branching is what this smoke covers; the disabled behavior is
        ``tests/test_semantic_apply.py::test_non_admin_branch_respects_studio_toggle``.
        """
        monkeypatch.setenv("AGNES_STUDIO_ENABLED", "1")
        c = seeded_app_both["client"]
        doc = _SEMANTIC_DOC.replace("smoke_model", "apply_model")

        applied = c.post(
            "/api/semantic-models/apply",
            json={"document": doc},
            headers=_admin_headers(seeded_app_both),
        )
        assert applied.status_code == 200
        assert applied.json()["outcome"] == "applied"
        assert applied.json()["model"]["slug"] == "apply_model"

        queued = c.post(
            "/api/semantic-models/apply",
            json={"document": _SEMANTIC_DOC.replace("smoke_model", "proposed_model")},
            headers=_analyst_headers(seeded_app_both),
        )
        assert queued.status_code == 200
        assert queued.json()["outcome"] == "submitted_for_review"
        assert (
            c.get("/api/semantic-models/proposed_model.yaml", headers=_admin_headers(seeded_app_both)).status_code
            == 404
        )

        model_id = applied.json()["model"]["id"]
        assert (
            c.delete(f"/api/admin/semantic-models/{model_id}", headers=_admin_headers(seeded_app_both)).status_code
            == 204
        )

    def test_source_crud_and_sync(self, seeded_app_both):
        c = seeded_app_both["client"]
        h = _admin_headers(seeded_app_both)

        # `upload` kind so the sync needs no network and no clone.
        created = c.post(
            "/api/admin/semantic-sources",
            json={
                "kind": "upload",
                "name": "smoke source",
                "adapter": "native",
                "config": {"documents": [_SEMANTIC_DOC]},
            },
            headers=h,
        )
        assert created.status_code == 201
        source_id = created.json()["id"]

        assert c.get("/api/admin/semantic-sources", headers=h).status_code == 200
        assert c.get(f"/api/admin/semantic-sources/{source_id}", headers=h).status_code == 200
        assert (
            c.put(
                f"/api/admin/semantic-sources/{source_id}",
                json={"name": "renamed"},
                headers=h,
            ).status_code
            == 200
        )

        assert c.post(f"/api/admin/semantic-sources/{source_id}/sync", headers=h).status_code == 200
        assert c.delete(f"/api/admin/semantic-sources/{source_id}", headers=h).status_code == 204
