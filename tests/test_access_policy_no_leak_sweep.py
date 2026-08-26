"""Exhaustive adversarial "no data leaks anywhere" sweep for table access
policies (table access policies design doc §5/§8 for the enforcement
surfaces, §16/§17 for the fail-closed error contracts, §3 for the
distribution interlock).

One ``server_only`` local table, ``invoices``, carrying real parquet data
that spans TWO tenants (``cost_center IN ('CCA', 'CCB')``) plus a PII
column (``national_id``) and an email column that the policy masks:

    SELECT * EXCLUDE (national_id, email), md5(email) AS email FROM invoices
    WHERE list_contains($user_groups, cost_center)

Three principals: ``u_cca`` (group ``CCA``), ``u_ccb`` (group ``CCB``), and
the seeded admin. Every read surface is asserted to give A only-CCA rows,
B only-CCB rows, admin everything, ``national_id`` never anywhere for a
non-admin, and the email column never plaintext for a non-admin. Every
known bypass (SQL-string table functions, CTE name collision, a direct
physical-source-catalog reference, a distributable registry twin, clearing
``server_only`` on a policied table) is asserted blocked.

Group names double as the ``cost_center`` values the policy compares
against (``list_contains($user_groups, cost_center)`` requires the two
namespaces to coincide) — they are spelled WITHOUT ``_``/``%`` on purpose:
``$user_groups`` binds the caller's ENTIRE live group list (not merely the
group relevant to this table), and ``policied_relation`` (§6.3) raises
``PolicyError`` for the whole request the moment ANY of those group names
contains a LIKE/SIMILAR-TO metacharacter — so ``CC_A``/``CC_B`` (the
design doc's own example values, which contain ``_``) would 500 every
request from a member of them. That guard is real, working, security
behaviour; using group names that trip it here would break the fixture,
not exercise the feature.

Two things this file deliberately does NOT try to be:

- It is not the surface ratchet (``tests/test_access_policy_surface_ratchet.py``
  enumerates every read path statically); this file re-verifies the
  surfaces that ratchet already covers, end to end, with real data, from
  the outside.
- It is not a second copy of Tasks 7-17's own targeted tests (each surface
  already has a dedicated file with more granular fixtures — CTE-shaped
  policies, cache-leak regressions, disclosure envelopes, ...). This file
  exists to catch a leak that *crosses* surfaces or hides in the
  interaction between two individually-correct pieces — which is exactly
  what it found (see the bottom section).

REAL FINDING, not synthetic — read before skimming past it: the design
doc's OWN canonical policy wording (§1) — ``SELECT * EXCLUDE (national_id),
md5(email) AS email FROM invoices WHERE ...`` — EXCLUDEs only
``national_id``; it does not also exclude ``email`` before re-deriving
``md5(email) AS email``. DuckDB accepts that and returns a result with TWO
columns literally named ``email``: the star's own plaintext copy first,
the masked one second. None of ``/api/query``, ``/api/v2/sample``, or
``/api/mcp/query-table/{id}`` deduplicate columns before serializing a
response, so an admin who copied the design doc's own wording verbatim
would have shipped the exact plaintext value the policy exists to hide —
on ``/api/v2/sample``/``/api/mcp/query-table`` the leak would have been
even sharper than on ``/api/query``: pandas' ``fetchdf()`` renames the
SECOND (masked) occurrence to ``email_1``, so a caller reading
``row["email"]`` would get the PLAINTEXT value under the exact key they
expect, with no visible sign anything is wrong.

FIXED: ``probe_policy`` (``src/access_policy_validate.py``) now rejects any
policy whose ``DESCRIBE``-resolved output carries a case-insensitive
duplicate column name — ``policy_duplicate_output_column`` — at the two
places that run it, ``PUT /api/admin/registry/{id}`` (save) and
``POST .../policy/preview`` (candidate preview), so the design doc's
uncorrected wording is rejected before it can ever be attached. This
sweep's own fixture below uses the CORRECTED form (``EXCLUDE (national_id,
email)``) so the rest of this file tests real, working enforcement rather
than being derailed by this unrelated authoring pitfall — the pitfall
itself, and the fix, are pinned in ``TestDesignDocCanonicalExampleRejectedAtSave``
at the bottom, which attempts to attach/preview the design doc's exact,
uncorrected wording and asserts both are now rejected.
"""

from __future__ import annotations

import hashlib

import pytest

# ---------------------------------------------------------------------------
# The policy under test — the CORRECTED form of the design doc's own
# canonical example (§1): both the PII column AND the column being
# re-derived under the same name are excluded from the star.
# ---------------------------------------------------------------------------
POLICY_SQL = (
    "SELECT * EXCLUDE (national_id, email), md5(email) AS email FROM invoices "
    "WHERE list_contains($user_groups, cost_center)"
)

# The design doc's literal, UNCORRECTED wording (§1) — see the module
# docstring and TestDesignDocCanonicalExampleRejectedAtSave below (adapted
# to reference ``invoices_canonical_demo``, that class's own table, in
# place of the design doc's own example table name -- the STRUCTURAL
# shape that leaks, EXCLUDE-ing only the PII column and re-deriving the
# email column without also excluding it, is unchanged). Never attached to
# a table via the repository directly (that would bypass the very
# save-time validation under test) — only ever submitted through the admin
# API's PUT/preview endpoints, which must reject it.
CANONICAL_BUT_LEAKY_POLICY_SQL = (
    "SELECT * EXCLUDE (national_id), md5(email) AS email FROM invoices_canonical_demo "
    "WHERE list_contains($user_groups, cost_center)"
)

# The CORRECTED form of the same policy, for the same table — used only to
# show the loop closes: the rejection's own suggested fix is accepted and
# does not leak.
CORRECTED_CANONICAL_DEMO_POLICY_SQL = (
    "SELECT * EXCLUDE (national_id, email), md5(email) AS email FROM invoices_canonical_demo "
    "WHERE list_contains($user_groups, cost_center)"
)

ROWS = [
    {"id": "1", "national_id": "N-1", "email": "alice@example.com", "cost_center": "CCA", "amount": "100"},
    {"id": "2", "national_id": "N-2", "email": "bob@example.com", "cost_center": "CCA", "amount": "150"},
    {"id": "3", "national_id": "N-3", "email": "carol@example.com", "cost_center": "CCB", "amount": "300"},
    {"id": "4", "national_id": "N-4", "email": "dave@example.com", "cost_center": "CCB", "amount": "400"},
]
CCA_IDS = {"1", "2"}
CCB_IDS = {"3", "4"}
CCA_AMOUNT_SUM = 250.0
CCB_AMOUNT_SUM = 700.0
TOTAL_AMOUNT_SUM = 950.0


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register(c, token, **kwargs):
    """POST /api/admin/register-table, returning the raw response (unlike
    most other test files' ``_register`` helper) — the bypass test below
    needs the failing status code and detail, not just a happy-path id."""
    kwargs.setdefault("source_type", "keboola")
    kwargs.setdefault("query_mode", "local")
    return c.post("/api/admin/register-table", json=kwargs, headers=_auth(token))


@pytest.fixture
def sweep(seeded_app, mock_extract_factory, monkeypatch):
    """One ``server_only`` ``invoices`` table (Keboola local), carrying
    ``POLICY_SQL``, plus a sibling ``invoices_canonical_demo`` table —
    same data, registered but deliberately left WITHOUT any access policy
    attached here: ``TestDesignDocCanonicalExampleRejectedAtSave`` below
    attaches/previews ``CANONICAL_BUT_LEAKY_POLICY_SQL`` on it itself,
    through the admin API, to prove that submission is rejected — pre-
    attaching it via the repository directly (bypassing the validated
    write path) would prove nothing about that path.

    Three principals: ``u_cca`` (group ``CCA``), ``u_ccb`` (group
    ``CCB``), and ``seeded_app``'s own admin.
    """
    from app.auth.jwt import create_access_token
    from src.db import get_system_db
    from src.orchestrator import SyncOrchestrator
    from src.repositories.table_registry import TableRegistryRepository
    from src.repositories.users import UserRepository
    from tests.conftest import grant_table_via_package

    monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "true")

    env = seeded_app["env"]
    mock_extract_factory(
        "keboola",
        [
            {"name": "invoices", "data": ROWS},
            {"name": "invoices_canonical_demo", "data": ROWS},
            {"name": "products", "data": [{"id": "1", "sku": "widget"}]},
        ],
    )
    SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

    conn = get_system_db()
    try:
        registry = TableRegistryRepository(conn)
        registry.register(
            id="invoices",
            name="invoices",
            source_type="keboola",
            query_mode="local",
            server_only=True,
            bucket="in.c-finance",
            source_table="invoices",
        )
        registry.set_access_policy(
            "invoices", sql=POLICY_SQL, note="cost-centre filter + email pseudonymization", updated_by="admin"
        )

        registry.register(
            id="invoices_canonical_demo",
            name="invoices_canonical_demo",
            source_type="keboola",
            query_mode="local",
            server_only=True,
        )
        # No access policy attached here on purpose — see the docstring
        # above and TestDesignDocCanonicalExampleRejectedAtSave below.

        registry.register(id="products", name="products", source_type="keboola", query_mode="local")

        users = UserRepository(conn)
        users.create(id="u_cca", email="cca@example.com", name="CCA")
        users.create(id="u_ccb", email="ccb@example.com", name="CCB")

        grant_table_via_package(conn, "invoices", "u_cca", group_name="CCA")
        grant_table_via_package(conn, "invoices", "u_ccb", group_name="CCB")
        grant_table_via_package(conn, "invoices_canonical_demo", "u_cca", group_name="CCA")
        grant_table_via_package(conn, "products", "u_cca", group_name="CCA")
    finally:
        conn.close()

    return {
        **seeded_app,
        "cca_token": create_access_token("u_cca", "cca@example.com"),
        "ccb_token": create_access_token("u_ccb", "ccb@example.com"),
    }


# ---------------------------------------------------------------------------
# POST /api/query — row filtering, column masking, aggregates.
# ---------------------------------------------------------------------------


class TestApiQuery:
    def test_cca_sees_only_cca_rows_no_national_id_masked_email(self, sweep):
        c = sweep["client"]
        r = c.post("/api/query", json={"sql": "SELECT * FROM invoices"}, headers=_auth(sweep["cca_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["row_count"] == 2, body
        assert "national_id" not in body["columns"], body["columns"]
        id_idx = body["columns"].index("id")
        assert {row[id_idx] for row in body["rows"]} == CCA_IDS
        email_idx = body["columns"].index("email")
        for row in body["rows"]:
            assert row[email_idx] != "alice@example.com"
            assert row[email_idx] != "bob@example.com"
            assert "@example.com" not in row[email_idx]
        # The masked value is a deterministic md5 of the real email, not an
        # opaque placeholder — pins that masking is real pseudonymization,
        # not e.g. a blanked-out constant that would also (accidentally)
        # pass a bare "not plaintext" check.
        by_id = {row[id_idx]: row[email_idx] for row in body["rows"]}
        assert by_id["1"] == hashlib.md5(b"alice@example.com").hexdigest()
        assert by_id["2"] == hashlib.md5(b"bob@example.com").hexdigest()

    def test_ccb_sees_only_ccb_rows(self, sweep):
        c = sweep["client"]
        r = c.post("/api/query", json={"sql": "SELECT * FROM invoices"}, headers=_auth(sweep["ccb_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["row_count"] == 2, body
        id_idx = body["columns"].index("id")
        assert {row[id_idx] for row in body["rows"]} == CCB_IDS
        assert "national_id" not in body["columns"]

    def test_admin_sees_every_row_and_national_id(self, sweep):
        c = sweep["client"]
        r = c.post("/api/query", json={"sql": "SELECT * FROM invoices"}, headers=_auth(sweep["admin_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["row_count"] == 4, body
        assert "national_id" in body["columns"]
        # Admin gets the raw plaintext email too — nothing to disclose.
        assert "alice@example.com" in r.text

    def test_aggregate_count_is_the_tenant_slice_not_the_company_total(self, sweep):
        """§10's own worry: an analyst computing COUNT(*)/SUM() over their
        own slice and mistaking it for the whole table."""
        c = sweep["client"]
        r = c.post("/api/query", json={"sql": "SELECT count(*) AS c FROM invoices"}, headers=_auth(sweep["cca_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["rows"][0][body["columns"].index("c")] == 2

    def test_aggregate_sum_is_the_tenant_slice_not_the_company_total(self, sweep):
        c = sweep["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT sum(CAST(amount AS DOUBLE)) AS s FROM invoices"},
            headers=_auth(sweep["cca_token"]),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["rows"][0][body["columns"].index("s")] == CCA_AMOUNT_SUM

    def test_admin_aggregate_sum_is_the_true_total(self, sweep):
        c = sweep["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT sum(CAST(amount AS DOUBLE)) AS s FROM invoices"},
            headers=_auth(sweep["admin_token"]),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["rows"][0][body["columns"].index("s")] == TOTAL_AMOUNT_SUM

    def test_disclosure_row_scope_present_for_non_admin_absent_for_admin(self, sweep):
        c = sweep["client"]
        r_a = c.post("/api/query", json={"sql": "SELECT * FROM invoices"}, headers=_auth(sweep["cca_token"]))
        assert r_a.json()["row_scope"] is not None
        assert "invoices" in r_a.json()["row_scope"]["policied_tables"]
        r_admin = c.post("/api/query", json={"sql": "SELECT * FROM invoices"}, headers=_auth(sweep["admin_token"]))
        assert r_admin.json()["row_scope"] is None


# ---------------------------------------------------------------------------
# POST /api/v2/sample
# ---------------------------------------------------------------------------


class TestV2Sample:
    def test_cca_sees_only_cca_rows(self, sweep):
        c = sweep["client"]
        r = c.get("/api/v2/sample/invoices?n=10", headers=_auth(sweep["cca_token"]))
        assert r.status_code == 200, r.text
        rows = r.json()["rows"]
        assert len(rows) == 2
        assert {row["id"] for row in rows} == CCA_IDS
        assert all("national_id" not in row for row in rows)
        assert all(row["email"] != "alice@example.com" for row in rows)
        assert all(row["email"] != "bob@example.com" for row in rows)

    def test_ccb_sees_only_ccb_rows(self, sweep):
        c = sweep["client"]
        r = c.get("/api/v2/sample/invoices?n=10", headers=_auth(sweep["ccb_token"]))
        assert r.status_code == 200, r.text
        rows = r.json()["rows"]
        assert len(rows) == 2
        assert {row["id"] for row in rows} == CCB_IDS

    def test_admin_sees_everything(self, sweep):
        c = sweep["client"]
        r = c.get("/api/v2/sample/invoices?n=10", headers=_auth(sweep["admin_token"]))
        assert r.status_code == 200, r.text
        rows = r.json()["rows"]
        assert len(rows) == 4
        assert any("national_id" in row for row in rows)


# ---------------------------------------------------------------------------
# POST /api/v2/scan
# ---------------------------------------------------------------------------


class TestV2Scan:
    def test_cca_sees_only_cca_rows(self, sweep):
        from app.api.v2_arrow import parse_ipc_bytes

        c = sweep["client"]
        r = c.post("/api/v2/scan", json={"table_id": "invoices"}, headers=_auth(sweep["cca_token"]))
        assert r.status_code == 200, r.text
        table = parse_ipc_bytes(r.content)
        assert "national_id" not in table.column_names
        assert table.num_rows == 2
        assert set(table.column("id").to_pylist()) == CCA_IDS
        assert "alice@example.com" not in table.column("email").to_pylist()

    def test_ccb_sees_only_ccb_rows(self, sweep):
        from app.api.v2_arrow import parse_ipc_bytes

        c = sweep["client"]
        r = c.post("/api/v2/scan", json={"table_id": "invoices"}, headers=_auth(sweep["ccb_token"]))
        assert r.status_code == 200, r.text
        table = parse_ipc_bytes(r.content)
        assert table.num_rows == 2
        assert set(table.column("id").to_pylist()) == CCB_IDS

    def test_admin_sees_everything(self, sweep):
        from app.api.v2_arrow import parse_ipc_bytes

        c = sweep["client"]
        r = c.post("/api/v2/scan", json={"table_id": "invoices"}, headers=_auth(sweep["admin_token"]))
        assert r.status_code == 200, r.text
        table = parse_ipc_bytes(r.content)
        assert "national_id" in table.column_names
        assert table.num_rows == 4


# ---------------------------------------------------------------------------
# GET /api/v2/schema/{id}
# ---------------------------------------------------------------------------


class TestV2Schema:
    def test_national_id_absent_for_non_admin(self, sweep):
        c = sweep["client"]
        r = c.get("/api/v2/schema/invoices", headers=_auth(sweep["cca_token"]))
        assert r.status_code == 200, r.text
        names = {col["name"] for col in r.json()["columns"]}
        assert "national_id" not in names

    def test_national_id_present_for_admin(self, sweep):
        c = sweep["client"]
        r = c.get("/api/v2/schema/invoices", headers=_auth(sweep["admin_token"]))
        assert r.status_code == 200, r.text
        names = {col["name"] for col in r.json()["columns"]}
        assert "national_id" in names


# ---------------------------------------------------------------------------
# POST /api/mcp/query-table/{id}
# ---------------------------------------------------------------------------


class TestMcpQueryTable:
    def test_cca_sees_only_cca_rows(self, sweep):
        c = sweep["client"]
        r = c.post("/api/mcp/query-table/invoices", json={"filter": {}, "limit": 10}, headers=_auth(sweep["cca_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["row_count"] == 2
        assert "national_id" not in body["columns"]
        assert {row["id"] for row in body["rows"]} == CCA_IDS
        assert all(row["email"] not in ("alice@example.com", "bob@example.com") for row in body["rows"])

    def test_ccb_sees_only_ccb_rows(self, sweep):
        c = sweep["client"]
        r = c.post("/api/mcp/query-table/invoices", json={"filter": {}, "limit": 10}, headers=_auth(sweep["ccb_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["row_count"] == 2
        assert {row["id"] for row in body["rows"]} == CCB_IDS

    def test_admin_sees_everything(self, sweep):
        c = sweep["client"]
        r = c.post(
            "/api/mcp/query-table/invoices", json={"filter": {}, "limit": 10}, headers=_auth(sweep["admin_token"])
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["row_count"] == 4
        assert "national_id" in body["columns"]

    def test_masked_column_400_never_reveals_it(self, sweep):
        """A filter attempt on the EXCLUDE'd column must not be silently
        honored (it would leak whether a hidden value matches) NOR appear
        in the 400's `allowed` column set."""
        c = sweep["client"]
        r = c.post(
            "/api/mcp/query-table/invoices",
            json={"filter": {"national_id": "N-1"}, "limit": 10},
            headers=_auth(sweep["cca_token"]),
        )
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert detail["error"] == "unknown_filter_columns"
        assert "national_id" in detail["unknown"]
        assert "national_id" not in detail["allowed"]


# ---------------------------------------------------------------------------
# GET /api/catalog/profile/{id} (+ /refresh)
# ---------------------------------------------------------------------------


def _seed_profile(table_id: str, columns: list) -> None:
    from src.db import get_system_db
    from src.repositories.profiles import ProfileRepository

    conn = get_system_db()
    try:
        ProfileRepository(conn).save(table_id, {"columns": columns, "row_count": 999})
    finally:
        conn.close()


class TestCatalogProfile:
    def test_non_admin_gets_no_stats_no_national_id_no_plaintext_email(self, sweep):
        _seed_profile(
            "invoices",
            [
                {"name": "national_id", "type": "VARCHAR", "sample_values": ["N-1", "N-2"]},
                {"name": "email", "type": "VARCHAR", "sample_values": ["alice@example.com"]},
                {"name": "cost_center", "type": "VARCHAR", "sample_values": ["CCA"]},
            ],
        )
        c = sweep["client"]
        r = c.get("/api/catalog/profile/invoices", headers=_auth(sweep["cca_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("policy_restricted") is True
        assert "columns" not in body
        assert "national_id" not in r.text
        assert "alice@example.com" not in r.text

    def test_admin_still_sees_the_stats(self, sweep):
        _seed_profile("invoices", [{"name": "national_id", "type": "VARCHAR", "sample_values": ["N-1"]}])
        c = sweep["client"]
        r = c.get("/api/catalog/profile/invoices", headers=_auth(sweep["admin_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("policy_restricted") is not True
        assert body["columns"][0]["sample_values"] == ["N-1"]

    def test_refresh_response_has_no_stats_for_non_admin_but_store_stays_unfiltered(self, sweep):
        from app.utils import resolve_local_parquet

        c = sweep["client"]
        assert resolve_local_parquet("invoices") is not None, "fixture must have synced a local parquet"

        r = c.post("/api/catalog/profile/invoices/refresh", headers=_auth(sweep["cca_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("policy_restricted") is True
        assert "columns" not in body

        admin_r = c.get("/api/catalog/profile/invoices", headers=_auth(sweep["admin_token"]))
        assert admin_r.status_code == 200, admin_r.text
        assert admin_r.json().get("policy_restricted") is not True


# ---------------------------------------------------------------------------
# GET /api/data/{id}/download — server_only must never leave the server.
# ---------------------------------------------------------------------------


class TestDownload:
    @pytest.mark.parametrize("token_key", ["cca_token", "ccb_token", "admin_token"])
    def test_download_is_403_for_everyone_server_only_is_not_a_grant_bypass(self, sweep, token_key):
        c = sweep["client"]
        r = c.get("/api/data/invoices/download", headers=_auth(sweep[token_key]))
        assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# GET /api/me/effective-access
# ---------------------------------------------------------------------------


class TestEffectiveAccess:
    def test_reports_the_policy_and_the_callers_own_row_count(self, sweep):
        c = sweep["client"]
        r = c.get("/api/me/effective-access", headers=_auth(sweep["cca_token"]))
        assert r.status_code == 200, r.text
        tables = r.json()["tables"]
        entry = next((t for t in tables if t["table_id"] == "invoices"), None)
        assert entry is not None, tables
        assert entry["policy"]["applies"] is True
        assert entry["policy"]["reason"] == "ok"
        assert entry["policy"]["rows_visible"] == 2

    def test_other_persona_sees_their_own_slice(self, sweep):
        c = sweep["client"]
        r = c.get("/api/me/effective-access", headers=_auth(sweep["ccb_token"]))
        assert r.status_code == 200, r.text
        entry = next((t for t in r.json()["tables"] if t["table_id"] == "invoices"), None)
        assert entry is not None
        assert entry["policy"]["rows_visible"] == 2


# ---------------------------------------------------------------------------
# Known bypasses — each must be blocked.
# ---------------------------------------------------------------------------


class TestBypassSqlStringTableFunction:
    """The #1264 class: DuckDB's ``query('<sql>')`` takes its target as a
    string literal, so a name-based RBAC/rewrite pass has nothing to match
    — the guard is on the parsed call, not the text."""

    def test_query_function_wrapping_a_policied_table_is_400(self, sweep):
        c = sweep["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT * FROM query('SELECT * FROM invoices')"},
            headers=_auth(sweep["cca_token"]),
        )
        assert r.status_code == 400, r.text


class TestBypassCteNameCollision:
    def test_cte_named_after_the_policied_table_is_400(self, sweep):
        c = sweep["client"]
        r = c.post(
            "/api/query",
            json={"sql": "WITH invoices AS (SELECT 1 AS x) SELECT * FROM invoices"},
            headers=_auth(sweep["cca_token"]),
        )
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert detail["reason"] == "policy_name_collision"
        assert detail["table"] == "invoices"


class TestBypassPhysicalSourceCatalogReference:
    """A non-admin never needs to name a source's own attached catalog —
    the master view under the registry name is the only legitimate
    surface. A catalog-qualified reference (``<source>.main.<table>``)
    reads the RAW extract table directly, entirely outside the rewrite
    (which matches by registry NAME, never by physical catalog path) —
    the exact hazard §5.2 rule 6 names. Pre-existing RBAC (#868) already
    denies ANY such reference for a non-admin, policied table or not; this
    pins that it also closes the access-policy-specific instance of it."""

    def test_direct_catalog_qualified_reference_is_403(self, sweep):
        c = sweep["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT * FROM keboola.main.invoices"},
            headers=_auth(sweep["cca_token"]),
        )
        assert r.status_code == 403, r.text
        assert "national_id" not in r.text
        assert "alice@example.com" not in r.text


class TestBypassDistributableTwin:
    """§3.2 — a second registry row resolving to the SAME physical Keboola
    source as the policied ``invoices`` table (same ``bucket``/
    ``source_table``), but distributable, would materialize the raw,
    unfiltered rows to a parquet ``agnes pull`` ships to every analyst
    granted the twin — no policy involved at all."""

    def test_register_table_twin_is_422(self, sweep):
        c = sweep["client"]
        resp = _register(
            c,
            sweep["admin_token"],
            name="invoices_twin",
            bucket="in.c-finance",
            source_table="invoices",
        )
        assert resp.status_code == 422, resp.text
        assert "access_policy_physical_source_conflict" in resp.text
        assert "invoices" in resp.text

        from src.repositories import table_registry_repo

        assert table_registry_repo().get("invoices_twin") is None


class TestBypassClearingServerOnly:
    """§3.1 case B — one PUT away from publishing the raw table."""

    def test_clearing_server_only_on_the_policied_table_is_422(self, sweep):
        c = sweep["client"]
        r = c.put(
            "/api/admin/registry/invoices",
            json={"server_only": False},
            headers=_auth(sweep["admin_token"]),
        )
        assert r.status_code == 422, r.text
        assert "access_policy_requires_undistributed" in r.text

        from src.repositories import table_registry_repo

        row = table_registry_repo().get("invoices")
        assert row["server_only"] is True
        assert row["access_policy_sql"] is not None


# ---------------------------------------------------------------------------
# Reproduce-first: prove the sweep is not vacuous.
#
# Every module above that reads a table's rows/columns imports
# `policied_relation`/`rewrite_sql` with a plain
# `from src.access_policy import ...` at its OWN top level (query.py,
# v2_sample.py, v2_scan.py, mcp_per_table.py) — each of those keeps its
# OWN bound reference to the ORIGINAL function object from import time.
# Patching `src.access_policy.policied_relation` alone does NOT reach any
# of them (a classic "patched the wrong reference" trap), and `rewrite_sql`'s
# own `resolve=policied_relation` default parameter is evaluated ONCE at
# `def` time, so it isn't reachable that way either. Disabling enforcement
# for real therefore means patching each consuming module's own bound name
# directly — which is also exactly what a genuine regression in any ONE of
# those modules would look like, so this doubles as the most realistic
# reproduction available.
# ---------------------------------------------------------------------------


def _disable_enforcement(monkeypatch):
    import src.access_policy as ap

    def _passthrough(table_id, principal, *, dialect="duckdb"):
        from src.repositories import table_registry_repo
        from src.sql_ident import quote_ident

        repo = table_registry_repo()
        row = repo.get(table_id) or repo.get_by_name(table_id)
        if row is None:
            raise ap.PolicyError(table_id)
        return ap.PoliciedRelation(
            relation_sql=f"SELECT * FROM {quote_ident(row['name'])}",
            params={},
            policied=False,
            table_id=row["id"],
        )

    def _passthrough_rewrite_sql(sql, principal, **_kwargs):
        # `**_kwargs` rather than a spelled-out signature: this stub only has
        # to swallow whatever the real `rewrite_sql` accepts, and pinning the
        # keyword list here means every new one (`dialect`, added for the
        # Databricks policy path) breaks this guard with an unrelated
        # TypeError instead of testing what it exists to test.
        return sql, {}, []

    monkeypatch.setattr("app.api.query.rewrite_sql", _passthrough_rewrite_sql)
    monkeypatch.setattr("app.api.v2_sample.policied_relation", _passthrough)
    monkeypatch.setattr("app.api.v2_scan.policied_relation", _passthrough)
    monkeypatch.setattr("app.api.mcp_per_table.policied_relation", _passthrough)


def test_the_sweep_would_catch_a_leak(sweep, monkeypatch):
    """Green-that-asserts-nothing guard: simulate the resolver regressing to
    passthrough on every module this sweep exercises, and confirm the SAME
    assertions the rest of this file relies on now FAIL — i.e. this sweep
    is a real detector, not a tautology that would pass however Agnes
    behaved. Scoped to this one test via ``monkeypatch``; every other test
    in this file runs against real, unpatched enforcement.
    """
    _disable_enforcement(monkeypatch)
    c = sweep["client"]

    # `_sample_cache` is a process-global TTL cache keyed on
    # (table_id, n, caller-identity) that outlives any one test function —
    # an earlier, REAL (unpatched) call elsewhere in this file for this
    # exact key would otherwise short-circuit before ever reaching the
    # patched resolver, masking the very regression this test exists to
    # detect (not hypothetical: it is what happened the first time this
    # test was run, against TestV2Sample's own real call for the same
    # table/n/identity). Clear it both before (so a stale FILTERED entry
    # can't hide the leak here) and after, in `finally` (so this test's own
    # LEAKED entry can't survive to poison a later test that reuses the
    # same key with enforcement for real).
    from app.api.v2_sample import _sample_cache

    _sample_cache.clear()
    try:
        # /api/query: B's rows now leak to A, and national_id reappears.
        r = c.post("/api/query", json={"sql": "SELECT * FROM invoices"}, headers=_auth(sweep["cca_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["row_count"] == 4, "expected the leak: all 4 rows, not just A's 2"
        assert "national_id" in body["columns"], "expected the leak: national_id back in the column list"
        assert "alice@example.com" in r.text or "carol@example.com" in r.text, "expected the leak: plaintext email"

        # /api/v2/sample: same story.
        r = c.get("/api/v2/sample/invoices?n=10", headers=_auth(sweep["cca_token"]))
        assert r.status_code == 200, r.text
        rows = r.json()["rows"]
        assert len(rows) == 4, "expected the leak: all 4 rows"
        assert any("national_id" in row for row in rows), "expected the leak: national_id present"

        # /api/mcp/query-table/{id}: same story.
        r = c.post("/api/mcp/query-table/invoices", json={"filter": {}, "limit": 10}, headers=_auth(sweep["cca_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["row_count"] == 4, "expected the leak: all 4 rows"
        assert "national_id" in body["columns"], "expected the leak: national_id present"

        # /api/v2/schema: effective_schema is DEFINED in src/access_policy.py
        # and looks `policied_relation` up dynamically from that module's own
        # globals at call time, so it is unaffected by the per-module patches
        # above and would need `src.access_policy.policied_relation` itself
        # patched to demonstrate the same failure — left out here on purpose so
        # this test's own patch set matches exactly what it asserts on.
    finally:
        _sample_cache.clear()


# ---------------------------------------------------------------------------
# REAL FINDING, FIXED (see module docstring): the design doc's own canonical
# policy wording, submitted verbatim, used to produce a duplicate 'email'
# output column that DuckDB accepts silently and none of the read surfaces
# deduplicate before serializing — so the plaintext email would leak past a
# masked one under the exact key a caller expects. ``probe_policy`` now
# rejects any policy whose resolved output carries a case-insensitive
# duplicate column name (``policy_duplicate_output_column``), at both places
# it runs: save (``PUT /api/admin/registry/{id}``) and preview
# (``POST .../policy/preview``). These were previously three
# ``xfail(strict=True)`` tests pinning the leak on three different read
# surfaces; now that the fix rejects the policy before it can ever be
# attached, the meaningful assertion is that the submission itself is
# rejected — not that some particular read surface happens not to leak a
# policy that was never allowed to exist.
# ---------------------------------------------------------------------------


class TestDesignDocCanonicalExampleRejectedAtSave:
    def test_put_save_rejects_the_uncorrected_canonical_policy(self, sweep):
        c = sweep["client"]
        r = c.put(
            "/api/admin/registry/invoices_canonical_demo",
            json={
                "access_policy_sql": CANONICAL_BUT_LEAKY_POLICY_SQL,
                "access_policy_note": "design-doc canonical wording, verbatim",
            },
            headers=_auth(sweep["admin_token"]),
        )
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        assert "policy_duplicate_output_column" in detail
        assert "email" in detail

        from src.repositories import table_registry_repo

        assert table_registry_repo().get("invoices_canonical_demo")["access_policy_sql"] is None

    def test_preview_candidate_rejects_the_uncorrected_canonical_policy(self, sweep):
        c = sweep["client"]
        r = c.post(
            "/api/admin/registry/invoices_canonical_demo/policy/preview",
            json={"sql": CANONICAL_BUT_LEAKY_POLICY_SQL, "as_groups": ["CCA"]},
            headers=_auth(sweep["admin_token"]),
        )
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        assert "policy_duplicate_output_column" in detail
        assert "email" in detail

    def test_correcting_the_policy_as_the_rejection_suggests_is_accepted_and_no_longer_leaks(self, sweep):
        """Closes the loop end to end: the CORRECTED form of the exact same
        policy — excluding ``email`` before re-deriving it, exactly as the
        rejection's own detail message suggests — is accepted, and a real
        read through it never returns the plaintext value."""
        c = sweep["client"]
        put = c.put(
            "/api/admin/registry/invoices_canonical_demo",
            json={
                "access_policy_sql": CORRECTED_CANONICAL_DEMO_POLICY_SQL,
                "access_policy_note": "corrected design-doc canonical wording",
            },
            headers=_auth(sweep["admin_token"]),
        )
        assert put.status_code == 200, put.text

        r = c.post(
            "/api/query",
            json={"sql": "SELECT * FROM invoices_canonical_demo"},
            headers=_auth(sweep["cca_token"]),
        )
        assert r.status_code == 200, r.text
        assert "alice@example.com" not in r.text
        assert "bob@example.com" not in r.text


# ---------------------------------------------------------------------------
# Read-path duplicate-output-column guard (attach-before-sync gap)
# ---------------------------------------------------------------------------
# `probe_policy` rejects a duplicate-output-column masking policy at SAVE time,
# but only once the base table has a resolvable schema. A policy stored while
# the table has no columns yet (registered, not yet synced) slips through and
# leaks the plaintext once data arrives. These tests store the leaky canonical
# policy DIRECTLY via the repository (bypassing save validation — exactly the
# attach-before-sync state) on a table that DOES have data, then confirm every
# read surface fails closed (no plaintext email ever ships).

_PLAINTEXT_EMAILS = (b"alice@example.com", b"bob@example.com", b"carol@example.com", b"dave@example.com")


def _store_leaky_policy_on_demo():
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository

    conn = get_system_db()
    try:
        TableRegistryRepository(conn).set_access_policy(
            "invoices_canonical_demo",
            sql=CANONICAL_BUT_LEAKY_POLICY_SQL,
            note="leaky form stored directly, simulating attach-before-sync",
            updated_by="admin",
        )
    finally:
        conn.close()


def _assert_no_plaintext(resp):
    body = resp.content or b""
    for needle in _PLAINTEXT_EMAILS:
        assert needle not in body, f"PLAINTEXT LEAK: {needle!r} present in response body"


class TestReadPathGuardCatchesStoredLeakyPolicy:
    def test_api_query_fails_closed(self, sweep):
        _store_leaky_policy_on_demo()
        c = sweep["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT * FROM invoices_canonical_demo"},
            headers=_auth(sweep["cca_token"]),
        )
        assert r.status_code >= 400, r.text
        _assert_no_plaintext(r)

    def test_v2_sample_fails_closed(self, sweep):
        _store_leaky_policy_on_demo()
        c = sweep["client"]
        r = c.get("/api/v2/sample/invoices_canonical_demo?n=10", headers=_auth(sweep["cca_token"]))
        assert r.status_code >= 400, r.text
        _assert_no_plaintext(r)

    def test_v2_scan_fails_closed(self, sweep):
        _store_leaky_policy_on_demo()
        c = sweep["client"]
        r = c.post("/api/v2/scan", json={"table_id": "invoices_canonical_demo"}, headers=_auth(sweep["cca_token"]))
        assert r.status_code >= 400, r.text
        _assert_no_plaintext(r)

    def test_mcp_query_table_fails_closed(self, sweep):
        _store_leaky_policy_on_demo()
        c = sweep["client"]
        r = c.post(
            "/api/mcp/query-table/invoices_canonical_demo",
            json={"filter": {}, "limit": 10},
            headers=_auth(sweep["cca_token"]),
        )
        assert r.status_code >= 400, r.text
        _assert_no_plaintext(r)

    def test_v2_scan_from_query_fails_closed(self, sweep):
        """`/api/v2/scan`'s from_query branch — `agnes snapshot create
        --from-query` and every `agnes query --remote --auto-snapshot` —
        runs through `run_remote_select_to_arrow`, which had no
        duplicate-output-column guard of its own. Arrow permits duplicate
        field names, so the plaintext copy and the masked re-derivation
        both landed in the analyst's parquet, ON DISK, where every other
        enforcement point is already behind them."""
        _store_leaky_policy_on_demo()
        c = sweep["client"]
        r = c.post(
            "/api/v2/scan",
            json={"from_query": "SELECT * FROM invoices_canonical_demo", "as": "leaky_snap"},
            headers=_auth(sweep["cca_token"]),
        )
        assert r.status_code >= 400, r.text
        _assert_no_plaintext(r)

    def test_guard_is_not_vacuous(self, sweep, monkeypatch):
        """Neutralize the guard on every surface's bound reference → the
        plaintext leak must reappear, proving the tests above are real."""
        import app.api.mcp_per_table as mcp_mod
        import app.api.v2_sample as sample_mod
        import src.access_policy as ap

        noop = lambda *a, **k: None  # noqa: E731
        # /api/query calls assert_policied_reads_unique -> assert_unique_output_columns
        # (both in src.access_policy); the table_id surfaces bound their own copy.
        monkeypatch.setattr(ap, "assert_unique_output_columns", noop)
        monkeypatch.setattr(ap, "assert_policied_reads_unique", noop)
        monkeypatch.setattr(sample_mod, "assert_unique_output_columns", noop)
        monkeypatch.setattr(mcp_mod, "assert_unique_output_columns", noop)
        _store_leaky_policy_on_demo()
        c = sweep["client"]

        r = c.get("/api/v2/sample/invoices_canonical_demo?n=10", headers=_auth(sweep["cca_token"]))
        # With the guard off, the leaky policy returns rows and the plaintext
        # email surfaces (pandas renamed the masked dup to `email_1`).
        assert r.status_code == 200, r.text
        assert b"alice@example.com" in (r.content or b""), "expected the leak to reappear with the guard disabled"


# ---------------------------------------------------------------------------
# §3.2, second half — the twins the interlock did NOT cover, and the
# engine-qualified paths that reach a policied physical source without ever
# naming the policied registry row.
#
# Both classes below are the same hazard the file already pins for
# distributable twins and for `<source>.main.<table>` references, arriving
# through the two doors those guards structurally cannot see:
# `_is_distributable_registry_row` is False for every non-distributable
# shape, and the rewrite matches by registry NAME, which `sf.*` / `bq.*`
# paths never spell.
# ---------------------------------------------------------------------------


def _register_engine_row_with_policy(source_type: str, table_id: str, bucket: str, source_table: str) -> None:
    """A `query_mode='remote'` row for one engine, carrying a policy.

    Registered through the repository rather than the admin API on purpose
    (unlike ``TestDesignDocCanonicalExampleRejectedAtSave``, which is about
    the write path): registering a remote row through the API kicks off
    that connector's extract rebuild, and these tests are about the READ
    gate, which never gets that far.
    """
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository

    conn = get_system_db()
    try:
        registry = TableRegistryRepository(conn)
        registry.register(
            id=table_id,
            name=table_id,
            source_type=source_type,
            query_mode="remote",
            bucket=bucket,
            source_table=source_table,
        )
        registry.set_access_policy(
            table_id,
            sql=f"SELECT * FROM {table_id} WHERE list_contains($user_groups, cost_center)",
            note="engine-path gate test",
            updated_by="admin",
        )
    finally:
        conn.close()


class TestBypassUnpoliciedNonDistributableTwin:
    """§3.2 — the twin interlock keys on ``_is_distributable_registry_row``,
    so it only ever fires for ``local``/``materialized`` twins. A second
    registry row over the SAME physical source that is itself
    non-distributable (``server_only=true``, or ``query_mode='remote'``)
    slips past both directions: it can be registered next to a policied
    table, and a policy can be attached next to it. Either way the raw rows
    stay readable server-side under the twin's own name by anyone granted
    it — the policy protects one name, not the data."""

    def test_registering_server_only_twin_of_policied_table_is_422(self, sweep):
        c = sweep["client"]
        resp = _register(
            c,
            sweep["admin_token"],
            name="invoices_server_only_twin",
            bucket="in.c-finance",
            source_table="invoices",
            server_only=True,
        )
        assert resp.status_code == 422, resp.text
        assert "access_policy_physical_source_conflict" in resp.text
        assert "invoices" in resp.text

        from src.repositories import table_registry_repo

        assert table_registry_repo().get("invoices_server_only_twin") is None

    def test_attaching_a_policy_while_an_unpolicied_twin_exists_is_422(self, sweep):
        """The mirror direction: two unpolicied rows over one physical
        source is legal (nothing to protect yet), but the moment a policy
        goes onto one of them the other becomes the open door."""
        c = sweep["client"]
        token = sweep["admin_token"]
        for name in ("payroll_a", "payroll_b"):
            resp = _register(
                c,
                token,
                name=name,
                bucket="in.c-hr",
                source_table="payroll",
                server_only=True,
            )
            assert resp.status_code in (200, 201), resp.text

        resp = c.put(
            "/api/admin/registry/payroll_a",
            json={
                "access_policy_sql": "SELECT * FROM payroll_a WHERE list_contains($user_groups, cost_center)",
                "access_policy_note": "cost-centre filter",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "access_policy_physical_source_conflict" in resp.text
        assert "payroll_b" in resp.text

        from src.repositories import table_registry_repo

        assert table_registry_repo().get("payroll_a")["access_policy_sql"] is None


class TestBypassEngineQualifiedPath:
    """A direct ``sf."SCHEMA"."TABLE"`` / ``bq."dataset"."table"`` reference
    names the PHYSICAL source, never the registry row, so
    ``rewrite_sql`` — which substitutes by registry name — never fires and
    the policy simply does not apply. Each engine's own gate checks only
    that the path is registered and that the caller holds a grant on the
    row it resolved to, which a caller granted the policied table passes.
    Fail closed instead: point them at the registered name, where the
    policy is enforced."""

    def test_sf_path_to_a_policied_source_is_403(self, sweep):
        from src.db import get_system_db
        from tests.conftest import grant_table_via_package

        _register_engine_row_with_policy("snowflake", "sf_invoices", "FINANCE", "INVOICES")
        conn = get_system_db()
        try:
            grant_table_via_package(conn, "sf_invoices", "u_cca", group_name="CCA")
        finally:
            conn.close()

        c = sweep["client"]
        r = c.post(
            "/api/query",
            json={"sql": 'SELECT * FROM sf."FINANCE"."INVOICES"'},
            headers=_auth(sweep["cca_token"]),
        )
        assert r.status_code == 403, r.text
        detail = r.json().get("detail")
        assert isinstance(detail, dict), detail
        assert detail.get("reason") == "sf_path_policied", detail
        assert detail.get("registered_as") == "sf_invoices", detail

    def test_bq_path_to_a_policied_source_is_403(self, sweep):
        from src.db import get_system_db
        from tests.conftest import grant_table_via_package

        _register_engine_row_with_policy("bigquery", "bq_invoices", "finance", "invoices_bq")
        conn = get_system_db()
        try:
            grant_table_via_package(conn, "bq_invoices", "u_cca", group_name="CCA")
        finally:
            conn.close()

        c = sweep["client"]
        r = c.post(
            "/api/query",
            json={"sql": 'SELECT * FROM bq."finance"."invoices_bq"'},
            headers=_auth(sweep["cca_token"]),
        )
        assert r.status_code == 403, r.text
        detail = r.json().get("detail")
        assert isinstance(detail, dict), detail
        assert detail.get("reason") == "bq_path_policied", detail
        assert detail.get("registered_as") == "bq_invoices", detail

    def test_an_unpolicied_engine_path_is_not_rejected_by_this_gate(self, sweep):
        """Non-vacuity: the same path shape over a source with NO policy
        anywhere must not pick up the new rejection — it fails later (no
        real warehouse in tests), never with ``*_path_policied``."""
        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository
        from tests.conftest import grant_table_via_package

        conn = get_system_db()
        try:
            TableRegistryRepository(conn).register(
                id="sf_plain",
                name="sf_plain",
                source_type="snowflake",
                query_mode="remote",
                bucket="FINANCE",
                source_table="PLAIN",
            )
            grant_table_via_package(conn, "sf_plain", "u_cca", group_name="CCA")
        finally:
            conn.close()

        c = sweep["client"]
        r = c.post(
            "/api/query",
            json={"sql": 'SELECT * FROM sf."FINANCE"."PLAIN"'},
            headers=_auth(sweep["cca_token"]),
        )
        assert "sf_path_policied" not in r.text, r.text
