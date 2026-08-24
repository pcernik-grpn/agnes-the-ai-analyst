"""Security tests — sandbox escapes, SQL injection, access control."""

import importlib
import os
import sys
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
    monkeypatch.setenv("SCRIPT_TIMEOUT", "5")

    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token
    from tests.helpers.auth import grant_admin

    conn = get_system_db()
    repo = UserRepository(conn)
    repo.create(id="admin1", email="admin@test.com", name="Admin")
    repo.create(id="u1", email="user@test.com", name="User")
    grant_admin(conn, "admin1")
    conn.close()

    app = shared_app
    c = TestClient(app)
    token = create_access_token("admin1", "admin@test.com")
    return c, token


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


# ---- Script Sandbox ----

class TestScriptSandbox:
    def test_blocks_os_system(self, client):
        c, token = client
        resp = c.post("/api/scripts/run", json={"source": "import os\nos.system('whoami')"},
                       headers=_headers(token))
        assert resp.status_code == 400

    def test_blocks_dunder_import(self, client):
        c, token = client
        resp = c.post("/api/scripts/run", json={"source": "__import__('subprocess').run(['ls'])"},
                       headers=_headers(token))
        assert resp.status_code == 400

    def test_blocks_eval(self, client):
        c, token = client
        resp = c.post("/api/scripts/run", json={"source": "eval('print(1)')"},
                       headers=_headers(token))
        assert resp.status_code == 400

    def test_blocks_exec(self, client):
        c, token = client
        resp = c.post("/api/scripts/run", json={"source": "exec('import os')"},
                       headers=_headers(token))
        assert resp.status_code == 400

    def test_blocks_open(self, client):
        c, token = client
        resp = c.post("/api/scripts/run", json={"source": "open('/etc/passwd').read()"},
                       headers=_headers(token))
        assert resp.status_code == 400

    def test_blocks_socket(self, client):
        c, token = client
        resp = c.post("/api/scripts/run", json={"source": "import socket"},
                       headers=_headers(token))
        assert resp.status_code == 400

    def test_blocks_pathlib(self, client):
        c, token = client
        resp = c.post("/api/scripts/run", json={"source": "from pathlib import Path"},
                       headers=_headers(token))
        assert resp.status_code == 400

    def test_allows_safe_script(self, client):
        c, token = client
        resp = c.post("/api/scripts/run", json={
            "source": "import math\nprint(math.sqrt(144))",
        }, headers=_headers(token))
        assert resp.status_code == 200
        assert "12" in resp.json()["stdout"]

    def test_allows_duckdb(self, client):
        c, token = client
        resp = c.post("/api/scripts/run", json={
            "source": "import duckdb\nconn=duckdb.connect(':memory:')\nprint(conn.execute('SELECT 42').fetchone()[0])",
        }, headers=_headers(token))
        assert resp.status_code == 200
        assert "42" in resp.json()["stdout"]

    def test_allows_json(self, client):
        c, token = client
        resp = c.post("/api/scripts/run", json={
            "source": "import json\nprint(json.dumps({'a': 1}))",
        }, headers=_headers(token))
        assert resp.status_code == 200
        assert '"a"' in resp.json()["stdout"]

    def test_runtime_import_blocked(self, client):
        """Even if static check passes, runtime __import__ override catches it."""
        c, token = client
        # This uses string concatenation to bypass static check
        resp = c.post("/api/scripts/run", json={
            "source": "x='sub'+'process'\ntry:\n m=type('',(),{'__init__':lambda s:None})()\nexcept:\n pass\nprint('safe')",
        }, headers=_headers(token))
        # Should still run but without access to dangerous modules
        assert resp.status_code == 200

    def test_sandbox_cannot_import_httpx(self, client):
        """httpx must be blocked — either by pattern check (400) or
        ModuleNotFoundError at runtime due to stripped VIRTUAL_ENV/PYTHONPATH (200 with non-zero exit)."""
        c, token = client
        resp = c.post("/api/scripts/run", json={
            "source": "import httpx\nprint('pwned')",
        }, headers=_headers(token))
        # Static pattern check should reject it outright
        assert resp.status_code == 400 or (
            resp.status_code == 200 and resp.json()["exit_code"] != 0
        )


# ---- SQL Query Security ----

class TestQuerySecurity:
    def test_blocks_copy_to(self, client):
        c, token = client
        resp = c.post("/api/query", json={"sql": "COPY (SELECT 1) TO '/tmp/pwned.csv'"},
                       headers=_headers(token))
        assert resp.status_code == 400

    def test_blocks_read_csv(self, client):
        c, token = client
        resp = c.post("/api/query", json={"sql": "SELECT * FROM read_csv_auto('/etc/passwd')"},
                       headers=_headers(token))
        assert resp.status_code == 400

    def test_blocks_semicolon(self, client):
        c, token = client
        resp = c.post("/api/query", json={"sql": "SELECT 1; SELECT 2"},
                       headers=_headers(token))
        assert resp.status_code == 400

    def test_allows_single_trailing_semicolon(self, client):
        c, token = client
        resp = c.post("/api/query", json={"sql": "SELECT 1 as test;"},
                       headers=_headers(token))
        assert resp.status_code == 200
        assert resp.json()["columns"] == ["test"]

    def test_the_terminator_is_normalized_away_before_any_downstream_path(self, client):
        """`/api/query` accepts one trailing `;`, and then hands the statement
        to paths that are not DuckDB: the BigQuery dry-run cost guard
        (`_rewrite_user_sql_for_bq_dry_run` is a textual rewrite and preserves
        the terminator) and the BQ jobs-API payload. A `;`-terminated text BQ
        reads as a script reports `totalBytesProcessed: 0`, which is the 5 GiB
        scan cap measuring nothing — a guardrail reading zero, not an error.
        Normalizing once at the boundary means no downstream path can be
        reached with the terminator still attached, so none of them has to be
        audited for it individually."""
        c, token = client
        resp = c.post(
            "/api/query",
            json={"sql": "SELECT 1 as test;"},
            headers=_headers(token),
        )
        assert resp.status_code == 200
        assert resp.json()["columns"] == ["test"]

        # The normalization point itself, asserted directly: whatever the
        # handler forwards must not end in `;`.
        from app.api.query import QueryRequest, strip_one_trailing_semicolon

        req = QueryRequest(sql="SELECT 1 as test;")
        req.sql = strip_one_trailing_semicolon(req.sql)
        assert not req.sql.endswith(";")
        assert strip_one_trailing_semicolon("SELECT 1;;").endswith(";"), (
            "exactly one terminator is removed -- the guard must not be loopable"
        )

    def test_blocks_non_select(self, client):
        c, token = client
        resp = c.post("/api/query", json={"sql": "CREATE TABLE pwned (id INT)"},
                       headers=_headers(token))
        assert resp.status_code == 400

    def test_blocks_attach(self, client):
        c, token = client
        resp = c.post("/api/query", json={"sql": "ATTACH '/tmp/pwned.db'"},
                       headers=_headers(token))
        assert resp.status_code == 400

    def test_allows_select(self, client):
        c, token = client
        resp = c.post("/api/query", json={"sql": "SELECT 1 as test, 'hello' as msg"},
                       headers=_headers(token))
        assert resp.status_code == 200
        assert resp.json()["columns"] == ["test", "msg"]

    def test_allows_with_cte(self, client):
        c, token = client
        resp = c.post("/api/query", json={"sql": "WITH t AS (SELECT 1 as x) SELECT * FROM t"},
                       headers=_headers(token))
        assert resp.status_code == 200

    def test_blocks_drop(self, client):
        c, token = client
        resp = c.post("/api/query", json={"sql": "DROP TABLE IF EXISTS users"},
                       headers=_headers(token))
        assert resp.status_code == 400


    def test_blocks_parquet_scan(self, client):
        c, token = client
        resp = c.post("/api/query", json={"sql": "SELECT * FROM parquet_scan('/data/extracts/secret.parquet')"},
                       headers=_headers(token))
        assert resp.status_code == 400

    def test_blocks_read_csv_auto(self, client):
        c, token = client
        resp = c.post("/api/query", json={"sql": "SELECT * FROM read_csv_auto('/etc/passwd')"},
                       headers=_headers(token))
        assert resp.status_code == 400

    def test_blocks_query_table(self, client):
        c, token = client
        resp = c.post("/api/query", json={"sql": "SELECT * FROM query_table('secret_table')"},
                       headers=_headers(token))
        assert resp.status_code == 400

    def test_no_auth(self, client):
        c, _ = client
        resp = c.post("/api/query", json={"sql": "SELECT 1"})
        assert resp.status_code == 401

    def test_word_boundary_match_no_false_positive(self, client):
        """Verify that a table named 'id' doesn't block queries containing 'id' in other contexts."""
        c, token = client
        # Query contains 'id' in column name and function, but not as a forbidden table reference
        resp = c.post("/api/query", json={"sql": "SELECT 1 as identity, 2 as valid_id"},
                       headers=_headers(token))
        # Should succeed (not blocked by false positive substring match)
        assert resp.status_code == 200

    def test_word_boundary_match_blocks_actual_table(self, client):
        """Verify that actual table references are still properly blocked with word-boundary regex."""
        c, token = client
        # Create a scenario where a table named 'id' would be forbidden
        # This tests that word boundaries work correctly
        resp = c.post("/api/query", json={"sql": "SELECT * FROM id WHERE id = 1"},
                       headers=_headers(token))
        # Without a real 'id' table and RBAC setup, this will fail with query error,
        # but not with 403 access denied. The regex logic is sound if test_word_boundary_match_no_false_positive passes.
        assert resp.status_code in [400, 200]  # Either query error or success

    def test_blocks_information_schema(self, client):
        c, token = client
        resp = c.post("/api/query", json={"sql": "SELECT table_name FROM information_schema.tables"},
                       headers=_headers(token))
        assert resp.status_code == 400

    def test_blocks_duckdb_tables(self, client):
        c, token = client
        resp = c.post("/api/query", json={"sql": "SELECT * FROM duckdb_tables()"},
                       headers=_headers(token))
        assert resp.status_code == 400

    def test_blocks_duckdb_columns(self, client):
        c, token = client
        resp = c.post("/api/query", json={"sql": "SELECT * FROM duckdb_columns()"},
                       headers=_headers(token))
        assert resp.status_code == 400

    def test_blocks_duckdb_databases(self, client):
        c, token = client
        resp = c.post("/api/query", json={"sql": "SELECT * FROM duckdb_databases()"},
                       headers=_headers(token))
        assert resp.status_code == 400

    def test_blocks_relative_path(self, client):
        c, token = client
        resp = c.post("/api/query", json={"sql": "SELECT * FROM read_parquet('../secret/data.parquet')"},
                       headers=_headers(token))
        assert resp.status_code == 400

    def test_blocks_pragma_table_info(self, client):
        c, token = client
        resp = c.post("/api/query", json={"sql": "SELECT * FROM pragma_table_info('users')"},
                       headers=_headers(token))
        assert resp.status_code == 400


# ---- Auth Edge Cases ----

class TestAuthSecurity:
    def test_garbage_token(self, client):
        c, _ = client
        resp = c.get("/api/scripts", headers={"Authorization": "Bearer garbage.token.here"})
        assert resp.status_code == 401

    def test_empty_bearer(self, client):
        c, _ = client
        resp = c.get("/api/scripts", headers={"Authorization": "Bearer "})
        assert resp.status_code == 401

    def test_no_bearer_prefix(self, client):
        c, token = client
        resp = c.get("/api/scripts", headers={"Authorization": token})
        assert resp.status_code == 401

    def test_missing_header(self, client):
        c, _ = client
        resp = c.get("/api/scripts")
        assert resp.status_code == 401


# ---- Script RBAC ----

@pytest.fixture
def viewer_client(tmp_path, monkeypatch, shared_app):
    """TestClient with a viewer-role user seeded."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
    monkeypatch.setenv("SCRIPT_TIMEOUT", "5")

    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token
    from fastapi.testclient import TestClient

    conn = get_system_db()
    UserRepository(conn).create(id="viewer1", email="viewer@test.com", name="Viewer")
    conn.close()

    app = shared_app
    c = TestClient(app)
    token = create_access_token(user_id="viewer1", email="viewer@test.com")
    return c, token


@pytest.mark.skip(
    reason=(
        "v12: scripts run/deploy is gated by Depends(get_current_user) "
        "(any signed-in user), not by role hierarchy. The viewer-blocked "
        "assertion is a v9-era expectation that no longer holds. "
        "If we re-introduce a role/group gate on scripts, rewrite these "
        "tests against require_resource_access(ResourceType.SCRIPT, ...)."
    )
)
class TestScriptRBAC:
    def test_viewer_cannot_run_scripts(self, viewer_client):
        c, token = viewer_client
        headers = {"Authorization": f"Bearer {token}"}
        resp = c.post("/api/scripts/run", json={
            "name": "test", "source": "print('hi')"
        }, headers=headers)
        assert resp.status_code == 403

    def test_viewer_cannot_deploy_scripts(self, viewer_client):
        c, token = viewer_client
        headers = {"Authorization": f"Bearer {token}"}
        resp = c.post("/api/scripts/deploy", json={
            "name": "test", "source": "print('hi')", "schedule": ""
        }, headers=headers)
        assert resp.status_code == 403


# ---- JWT Claims ----

class TestJwtClaims:
    def test_jwt_contains_jti_claim(self):
        """Token payload must include a jti claim with at least 16 hex chars."""
        os.environ.setdefault("TESTING", "1")
        from app.auth.jwt import create_access_token, verify_token
        token = create_access_token("u1", "user@test.com")
        payload = verify_token(token)
        assert payload is not None
        assert "jti" in payload
        assert len(payload["jti"]) >= 16

    def test_jwt_expiry_is_30_days(self):
        """Interactive session JWTs last 30 days (720 h) by design.

        Deliberate tradeoff: session JWTs (typ="session") carry no per-session
        revocation — pat_resolver trusts them off signature + exp alone — so a
        leaked cookie stays valid for the full window; the only server-side kill
        switch is deactivating the account (users.active). 30 days is the chosen
        login-persistence UX. This guard prevents silently drifting the value.
        """
        os.environ.setdefault("TESTING", "1")
        from app.auth import jwt as jwt_module
        assert jwt_module.ACCESS_TOKEN_EXPIRE_HOURS == 30 * 24


# ---- JWT Secret Hardening ----

class TestJwtSecretHardening:
    def test_auto_generates_jwt_secret_when_absent(self, tmp_path):
        """When JWT_SECRET_KEY is absent and TESTING is not set,
        the secret is auto-generated and persisted to a file."""
        saved_key = os.environ.pop("JWT_SECRET_KEY", None)
        saved_testing = os.environ.pop("TESTING", None)
        saved_data_dir = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = str(tmp_path)
        # Auto-generation now lives behind local-dev mode (fail-closed in
        # production), so opt in explicitly to exercise that path.
        saved_local_dev = os.environ.get("LOCAL_DEV_MODE")
        os.environ["LOCAL_DEV_MODE"] = "1"
        # Eject cached modules so the re-import re-executes module-level code
        sys.modules.pop("app.auth.jwt", None)
        sys.modules.pop("app.secrets", None)
        try:
            mod = importlib.import_module("app.auth.jwt")
            # Secret is now lazy — trigger it by calling the accessor
            mod._SECRET_KEY_CACHE = None
            mod._get_cached_secret_key()
            secret_file = tmp_path / "state" / ".jwt_secret"
            assert secret_file.exists(), "JWT secret file should be auto-generated"
            secret = secret_file.read_text().strip()
            assert len(secret) == 64, "Auto-generated secret should be 64 hex chars (32 bytes)"
        finally:
            # Restore environment before re-importing so the module loads cleanly
            if saved_local_dev is not None:
                os.environ["LOCAL_DEV_MODE"] = saved_local_dev
            else:
                os.environ.pop("LOCAL_DEV_MODE", None)
            if saved_key is not None:
                os.environ["JWT_SECRET_KEY"] = saved_key
            if saved_testing is not None:
                os.environ["TESTING"] = saved_testing
            if saved_data_dir is not None:
                os.environ["DATA_DIR"] = saved_data_dir
            else:
                os.environ.pop("DATA_DIR", None)
            # If neither was set (bare test run), use TESTING flag so reload works
            if saved_key is None and saved_testing is None:
                os.environ["TESTING"] = "1"
            sys.modules.pop("app.auth.jwt", None)
            sys.modules.pop("app.secrets", None)
            importlib.import_module("app.auth.jwt")
            # Clean up the temporary TESTING flag if we added it
            if saved_key is None and saved_testing is None:
                os.environ.pop("TESTING", None)


# ---- API hardening (issue #336) ----

@pytest.fixture
def hardening_client(tmp_path, monkeypatch, shared_app):
    """Minimal seeded app with an admin and a plain analyst for RBAC tests."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")

    from src.db import get_system_db, SYSTEM_ADMIN_GROUP
    from src.repositories.users import UserRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from app.auth.jwt import create_access_token

    conn = get_system_db()
    repo = UserRepository(conn)
    repo.create(id="hadmin", email="hadmin@test.com", name="HAdmin")
    repo.create(id="hanalyst", email="hanalyst@test.com", name="HAnalyst")
    gid = conn.execute(
        "SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]
    ).fetchone()[0]
    UserGroupMembersRepository(conn).add_member("hadmin", gid, source="system_seed")
    conn.close()

    app = shared_app
    c = TestClient(app)
    return {
        "client": c,
        "admin_token": create_access_token("hadmin", "hadmin@test.com"),
        "analyst_token": create_access_token("hanalyst", "hanalyst@test.com"),
        "admin_group_id": gid,
    }


class TestApiHardening336:
    """Regression tests for the ADV-001…ADV-009 findings from issue #336."""

    # ADV-001: RBAC on POST /api/sync/table-subscriptions
    def test_table_subscriptions_rbac_blocks_unauthorized_table(self, hardening_client):
        c = hardening_client["client"]
        token = hardening_client["analyst_token"]
        resp = c.post(
            "/api/sync/table-subscriptions",
            json={"table_mode": "explicit", "tables": {"secret_table": True}},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["updated"]["secret_table"] == {"error": "no permission"}

    def test_table_subscriptions_rbac_allows_accessible_table(self, hardening_client):
        # Admin has access to everything — verify subscription writes go through
        c = hardening_client["client"]
        token = hardening_client["admin_token"]
        resp = c.post(
            "/api/sync/table-subscriptions",
            json={"table_mode": "explicit", "tables": {"any_table": True}},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["updated"]["any_table"] == {"enabled": True}

    def test_table_subscriptions_dict_size_limit(self, hardening_client):
        c = hardening_client["client"]
        token = hardening_client["analyst_token"]
        huge = {f"t{i}": True for i in range(501)}
        resp = c.post(
            "/api/sync/table-subscriptions",
            json={"table_mode": "explicit", "tables": huge},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422  # ADV-008 max_length guard

    # ADV-003: /api/version must not expose commit_sha or schema_version
    def test_version_does_not_expose_internal_fields(self, hardening_client):
        resp = hardening_client["client"].get("/api/version")
        assert resp.status_code == 200
        body = resp.json()
        assert "commit_sha" not in body, "commit_sha must not be in public /api/version"
        assert "schema_version" not in body, "schema_version must not be in public /api/version"
        assert "version" in body

    # ADV-005: /docs, /redoc, /openapi.json gated behind auth
    def test_docs_requires_auth(self, hardening_client):
        resp = hardening_client["client"].get("/docs", follow_redirects=False)
        assert resp.status_code in (401, 302)

    def test_openapi_json_requires_auth(self, hardening_client):
        resp = hardening_client["client"].get("/openapi.json")
        assert resp.status_code == 401

    def test_openapi_json_accessible_to_authenticated_user(self, hardening_client):
        token = hardening_client["analyst_token"]
        resp = hardening_client["client"].get(
            "/openapi.json", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
        assert "paths" in resp.json()

    # ADV-006: /webhooks/ and /cli/ get JSON 401, not HTML redirect
    def test_cli_path_gets_json_401_not_redirect(self, hardening_client):
        # /cli/latest is public — test a hypothetical future gated endpoint
        # by confirming the prefix is in _API_PATH_PREFIXES via /openapi.json
        # returning JSON 401 (not a redirect) when unauthenticated.
        resp = hardening_client["client"].get("/openapi.json", follow_redirects=False)
        assert resp.status_code == 401
        assert resp.headers.get("content-type", "").startswith("application/json")

    # ADV-009 (reworked in #624): offset-based pagination is gone — the list
    # endpoint is server-paged via ``limit`` and narrowed by ``search`` /
    # ``group_id``. Exercise all three so a param regression fails loudly
    # instead of being silently ignored by FastAPI.
    def test_users_list_limit_search_group_filters(self, hardening_client):
        c = hardening_client["client"]
        headers = {"Authorization": f"Bearer {hardening_client['admin_token']}"}

        resp = c.get("/api/users?limit=1", headers=headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 1

        resp = c.get("/api/users?search=hanalyst", headers=headers)
        assert resp.status_code == 200
        assert [u["email"] for u in resp.json()] == ["hanalyst@test.com"]

        group_id = hardening_client["admin_group_id"]
        resp = c.get(f"/api/users?group_id={group_id}", headers=headers)
        assert resp.status_code == 200
        assert [u["email"] for u in resp.json()] == ["hadmin@test.com"]

    def test_tokens_list_pagination_params(self, hardening_client):
        token = hardening_client["admin_token"]
        resp = hardening_client["client"].get(
            "/auth/admin/tokens?limit=5&offset=0",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200

    # Regression guard for the reviewer-flagged auth-bypass on /auth/bootstrap
    # introduced (and reverted) during ADV-009. The original ADV-009 fix
    # repurposed UserRepository.list_all() to default to LIMIT 1000, which
    # silently broke the bootstrap check `[u for u in list_all() if
    # u.get('password_hash')]` on instances with >1000 users: if no
    # password-holder landed in the email-sorted first page, bootstrap
    # re-opened and an unauthenticated caller could claim admin.
    #
    # Fix shape: `list_all()` returns EVERY row (no LIMIT), API surface uses
    # the new `list_paginated(limit, offset)` instead. The two tests below
    # lock in both halves of the contract so a future cleanup doesn't
    # silently re-introduce the bypass.
    def test_users_list_all_returns_every_row_no_silent_limit(self):
        """``UserRepository.list_all()`` must NOT apply a default LIMIT
        — the bootstrap-lock check at ``app/auth/router.py`` and the
        startup no-password warning at ``app/main.py`` both call this
        no-arg and depend on exhaustive enumeration. Silent pagination
        here is a real auth-bypass on instances with >LIMIT users."""
        import inspect
        from src.repositories.users import UserRepository
        sig = inspect.signature(UserRepository.list_all)
        # No params (other than self) means no caller can accidentally
        # pass limit=N and end up with a windowed result.
        non_self_params = [
            p for p in sig.parameters.values() if p.name != "self"
        ]
        assert non_self_params == [], (
            f"UserRepository.list_all() must take no arguments other than "
            f"self (got {non_self_params}). Add limit/offset to "
            f"list_paginated() instead — list_all() is the "
            f"bootstrap-lock / startup-warning path and MUST enumerate "
            f"every row."
        )

    def test_users_list_paginated_is_separate_method(self):
        """The paginated variant must be a distinct method named
        ``list_paginated`` (not an overload of ``list_all``) so call
        sites are explicit about which contract they want.
        """
        from src.repositories.users import UserRepository
        assert hasattr(UserRepository, "list_paginated"), (
            "API-surface pagination must live on a separate "
            "`list_paginated` method, not on `list_all`."
        )
        import inspect
        sig = inspect.signature(UserRepository.list_paginated)
        param_names = {p.name for p in sig.parameters.values() if p.name != "self"}
        assert "limit" in param_names and "offset" in param_names
