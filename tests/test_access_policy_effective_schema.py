"""Task 9 -- effective schema: hide/mask columns in schema surfaces (table
access policies design doc §11; plan Task 9).

``/api/v2/schema`` and the where-validator both read the raw, unfiltered
column list today, so a policy's ``EXCLUDE (national_id)`` is invisible to
them: an analyst sees a column that no longer exists in any read surface,
and ``agnes snapshot create --select national_id`` would pass validation and
then fail at execution.

``effective_schema(table_id, principal)`` closes that gap by DESCRIBEing the
resolved policy relation and comparing it to the table's own raw schema —
this is the FIRST test that exercises the function itself (no HTTP) and its
wiring into ``GET /api/v2/schema/{table_id}``.
"""

from __future__ import annotations

import pytest

# The design doc's own canonical example (§1) -- reused verbatim so this test
# stays anchored to the same policy body every other task's tests use.
POLICY_SQL = (
    "SELECT * EXCLUDE (national_id), md5(email) AS email FROM invoices WHERE list_contains($user_groups, cost_center)"
)


@pytest.fixture
def policied_invoices(seeded_app, mock_extract_factory, monkeypatch):
    """A ``server_only`` ``invoices`` table carrying the design doc's own
    canonical row+column policy, plus a sibling ``products`` table with no
    policy at all -- the inert-case control every task in this plan tests
    against. ``id == name == "invoices"`` (and same for ``products``),
    matching ``tests/test_access_policy_table_id_surfaces.py``'s fixture:
    the local-schema branch resolves its parquet by registry ``id``
    (``app/utils.py::resolve_local_parquet_glob``), so id and name must
    coincide for both the raw AND the policied schema paths to see the same
    table.
    """
    from app.auth.jwt import create_access_token
    from src.db import get_system_db
    from src.orchestrator import SyncOrchestrator
    from src.repositories.table_registry import TableRegistryRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.users import UserRepository
    from tests.conftest import grant_table_via_package

    monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "true")

    env = seeded_app["env"]
    mock_extract_factory(
        "keboola",
        [
            {
                "name": "invoices",
                "data": [
                    {
                        "id": "1",
                        "national_id": "N-1",
                        "email": "a@example.com",
                        "cost_center": "Finance",
                        "amount": "100",
                    },
                    {
                        "id": "2",
                        "national_id": "N-2",
                        "email": "b@example.com",
                        "cost_center": "Marketing",
                        "amount": "200",
                    },
                ],
            },
            {
                "name": "products",
                "data": [
                    {"id": "1", "sku": "A1"},
                    {"id": "2", "sku": "A2"},
                ],
            },
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
        )
        registry.set_access_policy("invoices", sql=POLICY_SQL, note="cost-centre filter", updated_by="admin")

        # No access_policy_sql on this one -- the inert-case control.
        registry.register(
            id="products",
            name="products",
            source_type="keboola",
            query_mode="local",
        )

        users = UserRepository(conn)
        users.create(id="u_finance", email="finance@example.com", name="Finance")

        groups = UserGroupsRepository(conn)
        finance_gid = groups.create(name="Finance")["id"]
        UserGroupMembersRepository(conn).add_member("u_finance", finance_gid, source="admin")

        grant_table_via_package(conn, "invoices", "u_finance", group_name="Finance")
        grant_table_via_package(conn, "products", "u_finance", group_name="Finance")
    finally:
        conn.close()

    return {
        **seeded_app,
        "finance_user": {"id": "u_finance", "email": "finance@example.com"},
        "finance_token": create_access_token("u_finance", "finance@example.com"),
    }


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── src.access_policy.effective_schema (direct, no HTTP) ───────────────


class TestEffectiveSchemaResolver:
    def test_not_policied_table_returns_none(self, policied_invoices):
        from src.access_policy import effective_schema

        assert effective_schema("products", policied_invoices["finance_user"]) is None

    def test_admin_bypass_returns_none(self, policied_invoices):
        """Admin (full-surface credential) is passthrough (§12) -- the raw
        schema stays authoritative for them, so there is nothing for a
        caller to override."""
        from src.access_policy import effective_schema

        admin = {"id": "admin1", "email": "admin@test.com"}
        assert effective_schema("invoices", admin) is None

    def test_excluded_column_is_marked_hidden(self, policied_invoices):
        from src.access_policy import effective_schema

        columns = effective_schema("invoices", policied_invoices["finance_user"])
        by_name = {c["name"]: c for c in columns}

        assert by_name["national_id"]["hidden"] is True
        assert by_name["cost_center"]["hidden"] is False
        assert by_name["amount"]["hidden"] is False
        assert by_name["id"]["hidden"] is False

    def test_present_column_reports_the_effective_type(self, policied_invoices):
        """``md5(email) AS email`` -- the type reported for a surviving
        column comes from DESCRIBEing the POLICY-WRAPPED relation, not
        copied from the base table's own schema."""
        from src.access_policy import effective_schema

        columns = effective_schema("invoices", policied_invoices["finance_user"])
        by_name = {c["name"]: c for c in columns}

        assert by_name["email"]["hidden"] is False
        assert by_name["email"]["type"] == "VARCHAR"

    def test_masked_column_is_marked(self, policied_invoices):
        """``md5(email) AS email`` -- the canonical example a runtime
        DESCRIBE diff cannot detect (same name, same VARCHAR type on both
        sides) but a static read of the policy body's own SELECT list can."""
        from src.access_policy import effective_schema

        columns = effective_schema("invoices", policied_invoices["finance_user"])
        by_name = {c["name"]: c for c in columns}

        assert by_name["email"]["masked"] is True

    def test_untouched_columns_are_not_marked_masked(self, policied_invoices):
        from src.access_policy import effective_schema

        columns = effective_schema("invoices", policied_invoices["finance_user"])
        by_name = {c["name"]: c for c in columns}

        assert by_name["cost_center"]["masked"] is False
        assert by_name["amount"]["masked"] is False
        assert by_name["id"]["masked"] is False

    def test_hidden_column_is_not_also_marked_masked(self, policied_invoices):
        """A star-excluded column is ``hidden``, not ``masked`` -- the two
        markers are disjoint by construction."""
        from src.access_policy import effective_schema

        columns = effective_schema("invoices", policied_invoices["finance_user"])
        by_name = {c["name"]: c for c in columns}

        assert by_name["national_id"]["hidden"] is True
        assert by_name["national_id"]["masked"] is False

    def test_does_not_crash_on_duplicate_output_column_name(self, policied_invoices):
        """The canonical policy body projects both the star's pass-through
        ``email`` AND ``md5(email) AS email`` without also excluding the
        first -- DuckDB accepts it and DESCRIBE reports the name twice.
        ``effective_schema`` must not raise, and every OTHER column must
        still come through once."""
        from src.access_policy import effective_schema

        columns = effective_schema("invoices", policied_invoices["finance_user"])
        names = [c["name"] for c in columns]

        assert names.count("national_id") == 1
        assert names.count("id") == 1
        assert names.count("cost_center") == 1
        assert "email" in names

    def test_co_drive_session_propagates_identity_unresolvable(self, policied_invoices):
        """``effective_schema`` never catches ``policied_relation``'s own
        exceptions -- it resolves first thing and only proceeds once it
        knows ``.policied`` is True, so a co-drive session's ``§12``
        refusal surfaces unchanged, for ``/api/v2/schema``'s except-clause
        (same 403 ``policy_identity_unresolvable`` shape Task 7/8 already
        give every other surface) to translate."""
        from app.auth.session_principal import SessionPrincipal
        from src.access_policy import PolicyIdentityUnresolvable, effective_schema

        session = SessionPrincipal(
            session_id="sess-1",
            participant_user_ids=["u_finance", "u_other"],
            participant_emails=["finance@example.com", "other@example.com"],
            intersection={},
        )
        with pytest.raises(PolicyIdentityUnresolvable):
            effective_schema("invoices", session)


# ── GET /api/v2/schema/{table_id} wiring ────────────────────────────────


class TestSchemaEndpointWiring:
    def test_non_admin_does_not_see_the_excluded_column(self, policied_invoices):
        """The wire-level response DROPS the hidden column entirely (rather
        than merely marking it) -- `build_schema`'s `columns` list is also
        `/api/v2/scan`'s own schema source (`_resolve_schema` in
        `app/api/v2_scan.py`, feeding the where-validator's `select`/`where`/
        `order_by` checks), and that consumer collapses the list to a bare
        `{name: type}` dict with no idea a `hidden` key exists -- a marked
        (not dropped) entry would still validate as a real, referenceable
        column there."""
        c = policied_invoices["client"]
        r = c.get("/api/v2/schema/invoices", headers=_auth(policied_invoices["finance_token"]))
        assert r.status_code == 200, r.text
        columns = r.json()["columns"]
        names = {col["name"] for col in columns}
        by_name = {col["name"]: col for col in columns}

        assert "national_id" not in names
        assert by_name["cost_center"]["hidden"] is False

    def test_masked_column_is_flagged_over_the_wire(self, policied_invoices):
        """``GET /api/v2/schema`` emits ``masked`` the same way it already
        emits ``hidden`` -- a ``md5(email) AS email`` column stays present
        (not dropped, unlike ``hidden``) but is flagged so a caller can
        tell it apart from an untouched pass-through column."""
        c = policied_invoices["client"]
        r = c.get("/api/v2/schema/invoices", headers=_auth(policied_invoices["finance_token"]))
        assert r.status_code == 200, r.text
        by_name = {col["name"]: col for col in r.json()["columns"]}

        assert by_name["email"]["masked"] is True
        assert by_name["cost_center"]["masked"] is False

    def test_admin_sees_the_raw_unfiltered_schema(self, policied_invoices):
        c = policied_invoices["client"]
        r = c.get("/api/v2/schema/invoices", headers=_auth(policied_invoices["admin_token"]))
        assert r.status_code == 200, r.text
        names = {col["name"] for col in r.json()["columns"]}
        assert "national_id" in names

    def test_non_policied_sibling_table_is_byte_identical(self, policied_invoices):
        """The inert case: a table with no access_policy_sql returns the
        exact same shape it did before this feature existed -- no `hidden`
        or `masked` key anywhere in the column list, for admin OR non-admin."""
        c = policied_invoices["client"]
        for token in (policied_invoices["finance_token"], policied_invoices["admin_token"]):
            r = c.get("/api/v2/schema/products", headers=_auth(token))
            assert r.status_code == 200, r.text
            columns = r.json()["columns"]
            names = {col["name"] for col in columns}
            assert names == {"id", "sku"}
            assert all("hidden" not in col for col in columns)
            assert all("masked" not in col for col in columns)

    def test_cache_does_not_leak_admins_raw_schema_to_a_non_admin(self, policied_invoices):
        """Regression guard for the cache fix: `_schema_cache` is keyed on
        `table_id` alone. Warm it via the admin (raw schema, national_id
        included) FIRST, then confirm a following non-admin request still
        gets the effective (masked) schema instead of the cached raw one."""
        c = policied_invoices["client"]
        admin_r = c.get("/api/v2/schema/invoices", headers=_auth(policied_invoices["admin_token"]))
        assert admin_r.status_code == 200, admin_r.text
        assert "national_id" in {col["name"] for col in admin_r.json()["columns"]}

        finance_r = c.get("/api/v2/schema/invoices", headers=_auth(policied_invoices["finance_token"]))
        assert finance_r.status_code == 200, finance_r.text
        assert "national_id" not in {col["name"] for col in finance_r.json()["columns"]}

    def test_cache_does_not_leak_a_non_admins_masked_schema_to_the_admin(self, policied_invoices):
        """The other direction: warm the cache via the non-admin FIRST,
        confirm the admin still gets the raw schema afterwards."""
        c = policied_invoices["client"]
        finance_r = c.get("/api/v2/schema/invoices", headers=_auth(policied_invoices["finance_token"]))
        assert finance_r.status_code == 200, finance_r.text
        assert "national_id" not in {col["name"] for col in finance_r.json()["columns"]}


# ── downstream benefit: /api/v2/scan's where-validator (§11) ───────────


class TestScanWhereValidatorSeesTheEffectiveSchema:
    """`/api/v2/scan`'s own schema source (`_resolve_schema` in
    ``app/api/v2_scan.py``) is `build_schema` -- the same function this
    task wired. §11 promises "the where-validator validates against it";
    this is the end-to-end proof that promise holds WITHOUT touching
    `v2_scan.py` at all, because dropping (not merely marking) the hidden
    column from `build_schema`'s own `columns` is what makes it invisible
    to `v2_scan.py`'s `{name: type}` schema dict too."""

    def test_select_of_the_excluded_column_400s_for_a_non_admin(self, policied_invoices):
        c = policied_invoices["client"]
        r = c.post(
            "/api/v2/scan",
            json={"table_id": "invoices", "select": ["national_id"]},
            headers=_auth(policied_invoices["finance_token"]),
        )
        assert r.status_code == 400, r.text
        assert "national_id" in r.json()["detail"]

    def test_select_of_the_same_column_is_not_rejected_for_admin(self, policied_invoices):
        c = policied_invoices["client"]
        r = c.post(
            "/api/v2/scan",
            json={"table_id": "invoices", "select": ["national_id"]},
            headers=_auth(policied_invoices["admin_token"]),
        )
        assert r.status_code == 200, r.text

        admin_r = c.get("/api/v2/schema/invoices", headers=_auth(policied_invoices["admin_token"]))
        assert admin_r.status_code == 200, admin_r.text
        assert "national_id" in {col["name"] for col in admin_r.json()["columns"]}
