"""`pseudonymize_keyed` through the real read surfaces, with real data.

One ``server_only`` table, ``invoices_keyed``, carrying the compiler's own
output for a keyed pseudonym on ``email`` plus a cost-centre row filter:

    SELECT "id", "cost_center", agnes_hmac("email") AS "email", "amount"
    FROM "invoices_keyed" WHERE list_contains($user_groups, "cost_center")

Asserted from the outside, per surface: a non-admin gets hex digests that
equal ``hmac.new(key, email, sha256).hexdigest()`` -- so the mask is a real
keyed pseudonym, not an opaque placeholder that would also pass a bare "not
plaintext" check -- while the admin (who bypasses policies) gets plaintext.
Four surfaces, on two DIFFERENT kinds of connection that execute a policy body:
``/api/query`` and ``/api/mcp/query-table`` on ``get_analytics_db_readonly()``,
``/api/v2/sample`` and ``/api/v2/scan`` on their own throwaway ``:memory:``
connection over the parquet. A missing UDF registration on any one of them
turns that surface into a 500 -- fail-closed, but broken.

And the invariant the whole design hangs on: the key itself never appears in a
response, in the stored policy SQL, or in the audit log.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

KEY = "0123456789abcdef0123456789abcdef"

POLICY_SQL = (
    'SELECT "id", "cost_center", agnes_hmac("email") AS "email", "amount" '
    'FROM "invoices_keyed" WHERE list_contains($user_groups, "cost_center")'
)

ROWS = [
    {"id": "1", "email": "alice@example.com", "cost_center": "CCA", "amount": "100"},
    {"id": "2", "email": "bob@example.com", "cost_center": "CCA", "amount": "150"},
    {"id": "3", "email": "carol@example.com", "cost_center": "CCB", "amount": "300"},
]
CCA_IDS = {"1", "2"}


def _expected(value: str, key: str = KEY) -> str:
    return hmac.new(key.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def keyed(seeded_app, mock_extract_factory, monkeypatch):
    from app.auth.jwt import create_access_token
    from src.access_policy_udf import reset_key_cache
    from src.db import get_system_db
    from src.orchestrator import SyncOrchestrator
    from src.repositories.table_registry import TableRegistryRepository
    from src.repositories.users import UserRepository
    from tests.conftest import grant_table_via_package

    monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "true")
    monkeypatch.setenv("AGNES_ANONYMIZATION_HMAC_KEY", KEY)
    reset_key_cache()

    env = seeded_app["env"]
    mock_extract_factory("keboola", [{"name": "invoices_keyed", "data": ROWS}])
    SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

    conn = get_system_db()
    try:
        registry = TableRegistryRepository(conn)
        registry.register(
            id="invoices_keyed",
            name="invoices_keyed",
            source_type="keboola",
            query_mode="local",
            server_only=True,
            bucket="in.c-finance",
            source_table="invoices_keyed",
        )
        registry.set_access_policy(
            "invoices_keyed",
            sql=POLICY_SQL,
            note="cost-centre filter + keyed email pseudonym",
            updated_by="admin",
        )
        # A remote sibling with no policy of its own -- the save-time refusal
        # test below attaches one through the admin API, which must reject it.
        registry.register(
            id="invoices_keyed_remote",
            name="invoices_keyed_remote",
            source_type="keboola",
            query_mode="remote",
            bucket="in.c-finance",
            source_table="invoices_keyed_remote",
        )

        users = UserRepository(conn)
        users.create(id="u_keyed", email="keyed@example.com", name="CCA")
        grant_table_via_package(conn, "invoices_keyed", "u_keyed", group_name="CCA")
    finally:
        conn.close()

    yield {**seeded_app, "token": create_access_token("u_keyed", "keyed@example.com")}
    reset_key_cache()


class TestApiQuery:
    def test_non_admin_gets_the_keyed_digest_admin_gets_plaintext(self, keyed):
        c = keyed["client"]
        r = c.post("/api/query", json={"sql": "SELECT * FROM invoices_keyed"}, headers=_auth(keyed["token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        id_idx, email_idx = body["columns"].index("id"), body["columns"].index("email")
        assert {row[id_idx] for row in body["rows"]} == CCA_IDS
        by_id = {row[id_idx]: row[email_idx] for row in body["rows"]}
        assert by_id["1"] == _expected("alice@example.com")
        assert by_id["2"] == _expected("bob@example.com")
        assert "alice@example.com" not in r.text

        admin = c.post("/api/query", json={"sql": "SELECT * FROM invoices_keyed"}, headers=_auth(keyed["admin_token"]))
        assert admin.status_code == 200, admin.text
        assert "alice@example.com" in admin.text

    def test_the_digest_is_not_the_unkeyed_md5(self, keyed):
        """The whole point of the mask: an analyst holding a dictionary of
        candidate emails cannot md5 their way back to the identity."""
        c = keyed["client"]
        r = c.post("/api/query", json={"sql": "SELECT * FROM invoices_keyed"}, headers=_auth(keyed["token"]))
        assert hashlib.md5(b"alice@example.com").hexdigest() not in r.text

    def test_the_key_never_reaches_the_caller(self, keyed):
        c = keyed["client"]
        r = c.post("/api/query", json={"sql": "SELECT * FROM invoices_keyed"}, headers=_auth(keyed["token"]))
        assert KEY not in r.text
        assert KEY not in str(dict(r.headers))

    def test_caller_authored_sql_may_not_call_the_function(self, keyed):
        """Otherwise the mask is worth no more than md5: any analyst could
        hash a candidate value on the same connection and compare."""
        c = keyed["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT agnes_hmac('alice@example.com') AS probe"},
            headers=_auth(keyed["token"]),
        )
        assert r.status_code == 400, r.text
        assert "agnes_hmac" in r.text
        assert _expected("alice@example.com") not in r.text

    def test_an_admin_may_not_call_it_either(self, keyed):
        c = keyed["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT agnes_hmac(email) FROM invoices_keyed"},
            headers=_auth(keyed["admin_token"]),
        )
        assert r.status_code == 400, r.text


class TestOtherReadSurfaces:
    def test_v2_sample_serves_the_digest(self, keyed):
        c = keyed["client"]
        r = c.get("/api/v2/sample/invoices_keyed?n=10", headers=_auth(keyed["token"]))
        assert r.status_code == 200, r.text
        rows = r.json()["rows"]
        assert {row["id"] for row in rows} == CCA_IDS
        assert {row["email"] for row in rows} == {_expected("alice@example.com"), _expected("bob@example.com")}

    def test_v2_scan_serves_the_digest(self, keyed):
        from app.api.v2_arrow import parse_ipc_bytes

        c = keyed["client"]
        r = c.post("/api/v2/scan", json={"table_id": "invoices_keyed"}, headers=_auth(keyed["token"]))
        assert r.status_code == 200, r.text
        table = parse_ipc_bytes(r.content)
        assert set(table.column("id").to_pylist()) == CCA_IDS
        assert set(table.column("email").to_pylist()) == {
            _expected("alice@example.com"),
            _expected("bob@example.com"),
        }

    def test_mcp_query_table_serves_the_digest(self, keyed):
        c = keyed["client"]
        r = c.post(
            "/api/mcp/query-table/invoices_keyed",
            json={"filter": {}, "limit": 10},
            headers=_auth(keyed["token"]),
        )
        assert r.status_code == 200, r.text
        assert "alice@example.com" not in r.text
        assert _expected("alice@example.com") in r.text


class TestSaveTime:
    def test_saving_the_policy_through_the_admin_api_probes_and_accepts(self, keyed):
        """`probe_policy` actually RUNS the candidate body (LIMIT 0) on the
        analytics connection, so this is also the regression test for the UDF
        being registered there at save time, not only at read time."""
        c = keyed["client"]
        r = c.put(
            "/api/admin/registry/invoices_keyed",
            json={
                "access_policy_sql": POLICY_SQL,
                "access_policy_note": "keyed pseudonym on email, re-saved through the API",
            },
            headers=_auth(keyed["admin_token"]),
        )
        assert r.status_code == 200, r.text

        from src.repositories import table_registry_repo

        stored = table_registry_repo().get("invoices_keyed")["access_policy_sql"]
        assert "agnes_hmac" in stored
        assert KEY not in stored

    def test_the_admin_preview_runs_the_function(self, keyed):
        c = keyed["client"]
        r = c.post(
            "/api/admin/registry/invoices_keyed/policy/preview",
            json={"sql": POLICY_SQL, "as_groups": ["CCA"]},
            headers=_auth(keyed["admin_token"]),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # `sample_rows` is the POLICIED slice; `base_sample_rows` is the raw
        # before-image the authoring admin is entitled to see, so plaintext in
        # the response as a whole proves nothing -- the masked slice is what
        # must carry the digest.
        assert {row["email"] for row in body["sample_rows"]} == {
            _expected("alice@example.com"),
            _expected("bob@example.com"),
        }
        assert body["rows_visible"] == 2
        assert KEY not in r.text

    def test_a_remote_table_refuses_the_function_at_save_time(self, keyed):
        c = keyed["client"]
        r = c.put(
            "/api/admin/registry/invoices_keyed_remote",
            json={
                "access_policy_sql": (
                    'SELECT "id", agnes_hmac("email") AS "email" FROM "invoices_keyed_remote"'
                ),
                "access_policy_note": "should never be attachable to a remote table",
            },
            headers=_auth(keyed["admin_token"]),
        )
        assert r.status_code == 422, r.text
        assert "policy_function_duckdb_only" in r.json()["detail"]

        from src.repositories import table_registry_repo

        assert table_registry_repo().get("invoices_keyed_remote")["access_policy_sql"] is None


class TestAuditLog:
    def test_the_key_is_never_written_to_the_audit_log(self, keyed):
        c = keyed["client"]
        c.post("/api/query", json={"sql": "SELECT * FROM invoices_keyed"}, headers=_auth(keyed["token"]))
        c.put(
            "/api/admin/registry/invoices_keyed",
            json={"access_policy_sql": POLICY_SQL, "access_policy_note": "re-saved"},
            headers=_auth(keyed["admin_token"]),
        )

        from src.repositories import audit_repo

        rows, _ = audit_repo().query(limit=200)
        rendered = str(rows)
        assert KEY not in rendered
        assert _expected("alice@example.com") not in rendered
