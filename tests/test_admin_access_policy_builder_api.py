"""Table access policy builder — Task 2/3 of the no-SQL builder UX plan
(``docs/superpowers/plans/2026-08-17-access-policy-builder-ux.md``).

``GET /api/admin/registry/{table_id}/policy/columns`` gives the builder UI
real schema + sample values so an admin never has to know a table's
structure up front; ``POST /api/admin/registry/{table_id}/policy/compile``
turns a structured spec into the same validated SQL
``src.access_policy_compile.compile_policy`` (Task 1) already generates
in-process — the stored artifact stays SQL, this endpoint only returns it.

Mirrors ``tests/test_admin_access_policy_api.py`` for the ``seeded_app`` +
``mock_extract_factory`` + real-data fixture shape; both new routes sit
right next to ``preview_table_policy`` and share its posture (admin-only,
never gated on ``access_policies.enabled`` — that flag gates ATTACHING a
policy via PUT only, per its own hint text in ``app/api/admin.py``).
"""

from __future__ import annotations

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def policy_builder_table(seeded_app, mock_extract_factory):
    """A real, ``server_only`` (eligible) local table with a handful of
    columns a mask/row-rule spec can reference, synced through the real
    orchestrator so ``DESCRIBE`` on the analytics connection has something
    to report — mirrors ``policied_invoices_for_preview`` in
    ``tests/test_admin_access_policy_api.py``.
    """
    from src.db import get_system_db
    from src.orchestrator import SyncOrchestrator
    from src.repositories.table_registry import TableRegistryRepository

    env = seeded_app["env"]
    mock_extract_factory(
        "keboola",
        [
            {
                "name": "policy_builder_invoices",
                "data": [
                    {
                        "invoice_id": 1,
                        "cost_center": "Finance",
                        "email": "a@example.com",
                        "national_id": "111",
                        "amount_eur": 100.0,
                    },
                    {
                        "invoice_id": 2,
                        "cost_center": "Ops",
                        "email": "b@example.com",
                        "national_id": "222",
                        "amount_eur": 200.0,
                    },
                ],
            }
        ],
    )
    SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

    conn = get_system_db()
    try:
        registry = TableRegistryRepository(conn)
        registry.register(
            id="policy_builder_invoices",
            name="policy_builder_invoices",
            source_type="keboola",
            query_mode="local",
            server_only=True,
        )
    finally:
        conn.close()

    return seeded_app


@pytest.fixture
def policy_builder_table_with_profile(policy_builder_table):
    """Same table as ``policy_builder_table``, plus a saved profile so the
    columns endpoint's ``samples``/``distinct`` fields have real data to
    surface (the profile is content computed at sync time, independent of
    this test's assertions about the live endpoint)."""
    from src.repositories import profile_repo

    profile_repo().save(
        "policy_builder_invoices",
        {
            "columns": [
                {
                    "name": "invoice_id",
                    "type": "VARCHAR",
                    "type_category": "TEXT",
                    "sample_values": ["1", "2"],
                    "unique_count": 2,
                    "alerts": ["unique"],
                },
                {
                    "name": "cost_center",
                    "type": "VARCHAR",
                    "type_category": "CATEGORICAL",
                    "sample_values": ["Finance", "Ops"],
                    "unique_count": 2,
                    "alerts": [],
                },
                {
                    "name": "email",
                    "type": "VARCHAR",
                    "type_category": "TEXT",
                    "sample_values": ["a@example.com", "b@example.com"],
                    "unique_count": 2,
                    "alerts": ["unique"],
                },
                {
                    "name": "national_id",
                    "type": "VARCHAR",
                    "type_category": "TEXT",
                    "sample_values": ["111", "222"],
                    "unique_count": 2,
                    "alerts": ["unique"],
                },
                {
                    "name": "amount_eur",
                    "type": "DOUBLE",
                    "type_category": "NUMERIC",
                    "sample_values": [100.0, 200.0],
                    "unique_count": 2,
                    "alerts": [],
                },
            ]
        },
    )
    return policy_builder_table


# ── Task 2: GET /registry/{table_id}/policy/columns ────────────────────


@pytest.mark.journey
class TestPolicyBuilderColumns:
    def test_columns_endpoint_returns_schema_and_samples(self, policy_builder_table_with_profile):
        c = policy_builder_table_with_profile["client"]
        token = policy_builder_table_with_profile["admin_token"]

        resp = c.get(
            "/api/admin/registry/policy_builder_invoices/policy/columns",
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        names = [col["name"] for col in body["columns"]]
        assert "email" in names
        assert "national_id" in names
        assert all("type" in col and "samples" in col for col in body["columns"])

        by_name = {col["name"]: col for col in body["columns"]}
        assert by_name["email"]["samples"] == ["a@example.com", "b@example.com"]
        assert by_name["email"]["distinct"] == 2
        assert by_name["email"]["pii"] is True

        assert "mapping_tables" in body
        assert body["eligible"] is True

    def test_columns_endpoint_writes_an_audit_row_without_the_sample_values(self, policy_builder_table_with_profile):
        """RBAC-reviewer finding on #1979: `samples` are real (potentially
        PII) row values, the same class of content `catalog.sample` is
        audited for -- this route must leave a real audit row, and that
        row's params must carry metadata only, never the sample values or
        column names themselves."""
        c = policy_builder_table_with_profile["client"]
        token = policy_builder_table_with_profile["admin_token"]

        resp = c.get(
            "/api/admin/registry/policy_builder_invoices/policy/columns",
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        from src.repositories import audit_repo

        rows, _ = audit_repo().query(action="access_policy.columns_view", resource="policy_builder_invoices")
        rows = list(rows)
        assert rows, "the columns read left no audit trail"

        import json as _json

        raw_params = rows[0]["params"]
        params = _json.loads(raw_params) if isinstance(raw_params, str) else raw_params
        assert params["table_id"] == "policy_builder_invoices"
        assert params["column_count"] == len(body["columns"])
        assert params["samples_included"] is True
        assert set(params.keys()) == {"table_id", "column_count", "samples_included"}

        # None of the real sample values leak into the audit params, neither
        # under a known key nor smuggled anywhere else.
        for col in body["columns"]:
            for sample in col["samples"]:
                assert sample not in (raw_params if isinstance(raw_params, str) else _json.dumps(params))

    def test_columns_endpoint_without_a_profile_returns_empty_samples(self, policy_builder_table):
        """No profile has been saved yet — the endpoint must still answer
        200 with real column names/types, just no samples (never 500)."""
        c = policy_builder_table["client"]
        token = policy_builder_table["admin_token"]

        resp = c.get(
            "/api/admin/registry/policy_builder_invoices/policy/columns",
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        names = [col["name"] for col in body["columns"]]
        assert "cost_center" in names
        by_name = {col["name"]: col for col in body["columns"]}
        assert by_name["cost_center"]["samples"] == []
        assert by_name["cost_center"]["distinct"] is None

    def test_columns_endpoint_reports_ineligible_table(self, seeded_app):
        """A table that is neither ``query_mode='remote'`` nor
        ``server_only`` can't carry a policy at all (the distribution
        interlock) — the endpoint still answers 200 so the builder UI can
        show the "make server-only first" nudge (Task 5), it just reports
        ``eligible: false``."""
        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository

        conn = get_system_db()
        try:
            TableRegistryRepository(conn).register(
                id="ineligible_tbl",
                name="ineligible_tbl",
                source_type="keboola",
                query_mode="local",
            )
        finally:
            conn.close()

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get(
            "/api/admin/registry/ineligible_tbl/policy/columns",
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["eligible"] is False

    def test_columns_endpoint_says_when_the_schema_could_not_be_read(self, seeded_app):
        """An empty column list has two very different causes and the builder
        has to tell them apart.

        `_policy_builder_describe` runs on a fresh read-only analytics
        connection where a `query_mode='remote'` view's external catalog is not
        re-ATTACHed, so a failed DESCRIBE is the ordinary outcome for exactly
        the remote rows a policy is most often written for. Reporting that as
        "No columns found" sends the admin looking for a data problem, and the
        real reason only surfaced once a compile was attempted and 422'd.
        """
        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository

        conn = get_system_db()
        try:
            TableRegistryRepository(conn).register(
                id="undescribable_tbl",
                name="undescribable_tbl",
                source_type="bigquery",
                query_mode="remote",
            )
        finally:
            conn.close()

        c = seeded_app["client"]
        resp = c.get(
            "/api/admin/registry/undescribable_tbl/policy/columns",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["columns"] == []
        assert body["schema_available"] is False, "an unreadable schema is reported as an empty table"

    def test_columns_endpoint_reports_a_readable_schema_as_available(self, policy_builder_table):
        c = policy_builder_table["client"]
        resp = c.get(
            "/api/admin/registry/policy_builder_invoices/policy/columns",
            headers=_auth(policy_builder_table["admin_token"]),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["schema_available"] is True

    def test_columns_endpoint_lists_mapping_tables(self, policy_builder_table):
        from src.repositories import table_registry_repo

        table_registry_repo().set_policy_mapping("policy_builder_invoices", True)

        c = policy_builder_table["client"]
        token = policy_builder_table["admin_token"]
        resp = c.get(
            "/api/admin/registry/policy_builder_invoices/policy/columns",
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        assert "policy_builder_invoices" in resp.json()["mapping_tables"]

    def test_columns_endpoint_is_admin_only(self, policy_builder_table):
        c = policy_builder_table["client"]
        token = policy_builder_table["analyst_token"]
        resp = c.get(
            "/api/admin/registry/policy_builder_invoices/policy/columns",
            headers=_auth(token),
        )
        assert resp.status_code == 403, resp.text

    def test_columns_endpoint_404_for_unknown_table(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get(
            "/api/admin/registry/does-not-exist/policy/columns",
            headers=_auth(token),
        )
        assert resp.status_code == 404, resp.text

    def test_columns_endpoint_reads_bigquery_schema_for_a_never_synced_remote_table(self, seeded_app, monkeypatch):
        """A `query_mode='remote'` BigQuery table has no view in the shared
        analytics connection until something queries through it -- the old
        bare `DESCRIBE` returned `[]` for exactly this shape, rendering "No
        columns found" indistinguishable from a table that genuinely has
        none. The columns endpoint must reach BigQuery's own schema
        instead, the same way `agnes schema` already does via
        `build_schema_uncached`."""
        from app.api import v2_schema
        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository

        monkeypatch.setattr(
            v2_schema,
            "_fetch_bq_schema",
            lambda bq, dataset, table, **kw: [
                {"name": "revenue", "type": "FLOAT64", "nullable": True, "description": ""},
                {"name": "cost_center", "type": "STRING", "nullable": True, "description": ""},
            ],
        )
        monkeypatch.setattr(v2_schema, "_fetch_bq_table_options", lambda bq, dataset, table, **kw: {})

        conn = get_system_db()
        try:
            TableRegistryRepository(conn).register(
                id="mgmt_pl_remote",
                name="mgmt_pl_remote",
                source_type="bigquery",
                bucket="finance",
                source_table="mgmt_pl_remote",
                query_mode="remote",
            )
        finally:
            conn.close()

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get(
            "/api/admin/registry/mgmt_pl_remote/policy/columns",
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [col["name"] for col in body["columns"]] == ["revenue", "cost_center"]
        assert body["columns_error"] is None
        assert body["eligible"] is True

    def test_columns_endpoint_reports_a_schema_lookup_error_instead_of_a_silent_empty_list(
        self, seeded_app, monkeypatch
    ):
        """When the schema lookup itself fails (an expired token, an
        unreachable warehouse), the builder must say so -- `columns: []`
        with a `columns_error` explaining why -- rather than rendering the
        same "No columns found" a table with a genuinely empty schema
        would."""
        from app.api import v2_schema
        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository

        def _boom(bq, dataset, table, **kw):
            raise RuntimeError("BQ token expired")

        monkeypatch.setattr(v2_schema, "_fetch_bq_schema", _boom)

        conn = get_system_db()
        try:
            TableRegistryRepository(conn).register(
                id="mgmt_pl_broken",
                name="mgmt_pl_broken",
                source_type="bigquery",
                bucket="finance",
                source_table="mgmt_pl_broken",
                query_mode="remote",
            )
        finally:
            conn.close()

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get(
            "/api/admin/registry/mgmt_pl_broken/policy/columns",
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["columns"] == []
        assert body["columns_error"]
        assert "BQ token expired" in body["columns_error"]


# ── Task 3: POST /registry/{table_id}/policy/compile ────────────────────


@pytest.mark.journey
class TestPolicyBuilderCompile:
    def test_compile_endpoint_builds_safe_sql(self, policy_builder_table):
        c = policy_builder_table["client"]
        token = policy_builder_table["admin_token"]

        resp = c.post(
            "/api/admin/registry/policy_builder_invoices/policy/compile",
            json={
                "row_rules": [{"column": "cost_center", "op": "in_caller_groups"}],
                "row_combine": "and",
                "column_masks": {"email": "hash", "national_id": "hide"},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        sql = body["sql"]
        # The compiler must never emit SELECT *; the projection is fixed so a
        # newly-added source column cannot leak through an implicit *.
        assert "SELECT *" not in sql
        assert "EXCLUDE" not in sql
        # hidden columns are omitted from the projection entirely
        assert '"national_id"' not in sql
        assert '"email"' in sql
        assert 'md5("email") AS "email"' in sql
        assert 'list_contains($user_groups, "cost_center")' in sql
        assert '"policy_builder_invoices"' in sql
        assert body["warnings"] == []

    def test_compile_endpoint_ignores_unknown_columns_with_a_warning(self, policy_builder_table):
        c = policy_builder_table["client"]
        token = policy_builder_table["admin_token"]

        resp = c.post(
            "/api/admin/registry/policy_builder_invoices/policy/compile",
            json={"row_rules": [], "column_masks": {"not_a_real_column": "hide"}},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "not_a_real_column" not in body["sql"]
        assert any("not_a_real_column" in w for w in body["warnings"])

    def test_compile_endpoint_never_trusts_a_client_supplied_table_name(self, policy_builder_table):
        """The request body has no ``table`` field at all — even if a
        client sends one, the compiled SQL always names the REGISTRY
        row's own name."""
        c = policy_builder_table["client"]
        token = policy_builder_table["admin_token"]

        resp = c.post(
            "/api/admin/registry/policy_builder_invoices/policy/compile",
            json={"row_rules": [], "column_masks": {}, "table": "some_other_table"},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        sql = resp.json()["sql"]
        assert "some_other_table" not in sql
        assert '"policy_builder_invoices"' in sql

    def test_compile_endpoint_is_admin_only(self, policy_builder_table):
        c = policy_builder_table["client"]
        token = policy_builder_table["analyst_token"]
        resp = c.post(
            "/api/admin/registry/policy_builder_invoices/policy/compile",
            json={"row_rules": [], "column_masks": {}},
            headers=_auth(token),
        )
        assert resp.status_code == 403, resp.text

    def test_compile_endpoint_404_for_unknown_table(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/registry/does-not-exist/policy/compile",
            json={"row_rules": [], "column_masks": {}},
            headers=_auth(token),
        )
        assert resp.status_code == 404, resp.text

    def test_compile_endpoint_rejects_a_malformed_spec_with_4xx(self, policy_builder_table):
        """A typo'd row op or mask choice is a CLIENT mistake, not a server
        fault. ``compile_policy`` raises a bare ``ValueError`` for both
        ("unknown row op", "unknown mask"); the handler must translate that
        into a 4xx the builder can render inline -- never a 500 / unhandled
        exception.
        """
        c = policy_builder_table["client"]
        token = policy_builder_table["admin_token"]

        malformed = [
            {
                "row_rules": [{"column": "cost_center", "op": "equalz", "value": "Finance"}],
                "column_masks": {},
            },
            {"row_rules": [], "column_masks": {"email": "obfuscate"}},
        ]
        for spec in malformed:
            resp = c.post(
                "/api/admin/registry/policy_builder_invoices/policy/compile",
                json=spec,
                headers=_auth(token),
            )
            assert resp.status_code == 422, resp.text
            assert resp.json()["detail"].startswith("policy_compile_invalid_spec:"), resp.text

    def test_compile_endpoint_unmask_supports_multi_group_allowlist(self, policy_builder_table):
        c = policy_builder_table["client"]
        token = policy_builder_table["admin_token"]

        resp = c.post(
            "/api/admin/registry/policy_builder_invoices/policy/compile",
            json={
                "row_rules": [],
                "column_masks": {
                    "email": {"choice": "unmask", "groups": ["Finance", "Legal"]},
                    "amount_eur": {"choice": "unmask", "groups": ["Finance"]},
                },
            },
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        sql = resp.json()["sql"]
        # multi-group condition is ORed
        assert "list_contains($user_groups, 'Finance') OR list_contains($user_groups, 'Legal')" in sql
        # text-like column gets the fixed redaction string, numeric a cast NULL
        assert "ELSE '*****'" in sql
        assert "ELSE CAST(NULL AS" in sql
        # the projection is explicit and preserves the original column order
        assert "SELECT *" not in sql
        assert "EXCLUDE" not in sql

    def test_compile_endpoint_schema_unavailable_returns_422(self, seeded_app):
        """DESCRIBE on a registered-but-not-materialized table returns an empty
        schema; the endpoint must fail closed with a clear message instead of
        pretending the table has no columns."""
        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository

        conn = get_system_db()
        try:
            TableRegistryRepository(conn).register(
                id="ghost_tbl",
                name="ghost_tbl",
                source_type="keboola",
                query_mode="local",
                server_only=True,
            )
        finally:
            conn.close()

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/registry/ghost_tbl/policy/compile",
            json={"row_rules": [], "column_masks": {"email": "hide"}},
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "policy_builder_schema_unavailable" in resp.json()["detail"], resp.text

    def test_compile_endpoint_hiding_all_columns_returns_422(self, policy_builder_table):
        """A spec that would project no columns must not fall back to SELECT *."""
        c = policy_builder_table["client"]
        token = policy_builder_table["admin_token"]

        resp = c.post(
            "/api/admin/registry/policy_builder_invoices/policy/compile",
            json={
                "row_rules": [],
                "column_masks": {
                    "invoice_id": "hide",
                    "cost_center": "hide",
                    "email": "hide",
                    "national_id": "hide",
                    "amount_eur": "hide",
                },
            },
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "select no columns" in resp.json()["detail"], resp.text

    def test_compile_endpoint_refuses_an_unknown_row_rule_column(self, policy_builder_table):
        """A row rule naming a column the table does not have used to be
        DROPPED with a warning — so a spec whose only rule referenced a
        since-renamed column compiled to a WHERE-less policy handing the whole
        table to everyone, at authoring time, behind a 200 and a warning nobody
        had to read. It is a refused compile now."""
        c = policy_builder_table["client"]
        token = policy_builder_table["admin_token"]

        resp = c.post(
            "/api/admin/registry/policy_builder_invoices/policy/compile",
            json={
                "row_rules": [{"column": "renamed_away", "op": "in_caller_groups"}],
                "column_masks": {},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert detail.startswith("policy_compile_invalid_spec:"), resp.text
        assert "renamed_away" in detail, resp.text

    def test_compile_endpoint_supports_the_partial_masks(self, policy_builder_table):
        c = policy_builder_table["client"]
        token = policy_builder_table["admin_token"]

        resp = c.post(
            "/api/admin/registry/policy_builder_invoices/policy/compile",
            json={
                "row_rules": [],
                "column_masks": {"email": "email_partial", "national_id": "last4"},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        sql = resp.json()["sql"]
        assert "SELECT *" not in sql
        assert "EXCLUDE" not in sql
        # Each masked column is projected exactly once — never beside a
        # plaintext sibling under the same output name.
        assert sql.count('AS "email"') == 1
        assert sql.count('AS "national_id"') == 1
        assert "REGEXP_REPLACE(\"email\", '^[^@]*', '')" in sql
        assert "CONCAT('****', SUBSTRING(\"national_id\", -4))" in sql
        # The compiled body must survive the gate every save runs, remote
        # transpiles included — the builder may not hand an admin SQL the PUT
        # would then refuse.
        from src.access_policy_validate import validate_policy_sql

        validate_policy_sql(
            sql,
            table_id="policy_builder_invoices",
            table_name="policy_builder_invoices",
            mapping_table_names=set(),
            for_remote=True,
        )

    def test_compile_endpoint_refuses_a_partial_mask_on_a_non_text_column(self, policy_builder_table):
        """`last4`/`email_partial` are string surgery; on a DOUBLE column the
        only options are silently changing the output column's type or emitting
        nonsense, so the compiler refuses and the endpoint 422s."""
        c = policy_builder_table["client"]
        token = policy_builder_table["admin_token"]

        resp = c.post(
            "/api/admin/registry/policy_builder_invoices/policy/compile",
            json={"row_rules": [], "column_masks": {"amount_eur": "last4"}},
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert detail.startswith("policy_compile_invalid_spec:"), resp.text
        assert "amount_eur" in detail, resp.text

    def test_compile_endpoint_builds_a_tiered_mask_chain(self, policy_builder_table):
        """A multi-tier spec is compiled server-side into ONE ordered CASE
        chain -- the endpoint stays a pure generator, so the stored artifact
        is still SQL, never the tier structure."""
        c = policy_builder_table["client"]
        token = policy_builder_table["admin_token"]

        resp = c.post(
            "/api/admin/registry/policy_builder_invoices/policy/compile",
            json={
                "row_rules": [],
                "column_masks": {
                    "national_id": {
                        "choice": "tiered",
                        "tiers": [
                            {"groups": ["Compliance"], "reveal": "show"},
                            {"groups": ["Finance"], "reveal": "last4"},
                        ],
                        "default": "nullify",
                    }
                },
            },
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        sql = resp.json()["sql"]
        assert "SELECT *" not in sql
        # One output column for the masked column, in tier order.
        assert sql.count('AS "national_id"') == 1
        assert sql.index("'Compliance'") < sql.index("'Finance'")
        assert "list_contains($user_groups, 'Compliance')" in sql
        assert "CAST(NULL AS VARCHAR) END" in sql

    def test_compile_endpoint_rejects_a_tiered_spec_that_masks_nothing(self, policy_builder_table):
        """A tier chain whose default is ``show`` reveals the column to
        everyone the tiers do not name -- a no-op policy that reads like a
        restriction. ``compile_policy`` refuses it; the endpoint must surface
        that as a 4xx the builder can render, never a 500."""
        c = policy_builder_table["client"]
        token = policy_builder_table["admin_token"]

        resp = c.post(
            "/api/admin/registry/policy_builder_invoices/policy/compile",
            json={
                "row_rules": [],
                "column_masks": {
                    "national_id": {
                        "choice": "tiered",
                        "tiers": [{"groups": ["Compliance"], "reveal": "show"}],
                        "default": "show",
                    }
                },
            },
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "default" in resp.json()["detail"]
