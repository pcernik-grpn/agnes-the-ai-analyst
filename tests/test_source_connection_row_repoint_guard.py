"""D2.3: connection identity for Snowflake/Databricks moved off the
``data_source.<type>`` server-config yaml overlay and onto the connection
row — the repoint 409 guard (`app.connection_identity.identity_changes`)
follows it here, onto ``PUT /api/admin/source-connections/{id}``."""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register_snowflake_table(table_id: str) -> None:
    from src.repositories import table_registry_repo

    table_registry_repo().register(
        id=table_id,
        name=table_id,
        source_type="snowflake",
        bucket="GOLD",
        source_table=table_id.upper(),
        query_mode="remote",
    )


class TestRowRepointGuard:
    def test_identity_change_with_registrations_is_refused(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        create_resp = c.post(
            "/api/admin/source-connections",
            json={
                "name": "sf-guarded",
                "source_type": "snowflake",
                "config": {"account": "acme-prod", "user": "svc", "database": "PROD", "warehouse": "WH"},
            },
            headers=_auth(token),
        )
        assert create_resp.status_code == 201, create_resp.text
        conn_id = create_resp.json()["id"]
        _register_snowflake_table("rguard_orders")

        resp = c.put(
            f"/api/admin/source-connections/{conn_id}",
            json={"config": {"account": "acme-prod", "user": "svc", "database": "GOLD", "warehouse": "WH"}},
            headers=_auth(token),
        )
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["error"] == "connection_change_affects_registrations"
        assert detail["source"] == "snowflake"
        assert detail["changes"] == [{"field": "database", "before": "PROD", "after": "GOLD"}]
        assert "confirm_connection_change" in detail["hint"]

    def test_refused_repoint_leaves_the_row_untouched(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        create_resp = c.post(
            "/api/admin/source-connections",
            json={
                "name": "sf-untouched",
                "source_type": "snowflake",
                "config": {"account": "acme-prod", "user": "svc", "database": "PROD", "warehouse": "WH"},
            },
            headers=_auth(token),
        )
        conn_id = create_resp.json()["id"]
        _register_snowflake_table("rguard_untouched")

        resp = c.put(
            f"/api/admin/source-connections/{conn_id}",
            json={"config": {"account": "other-acct", "user": "svc", "database": "PROD", "warehouse": "WH"}},
            headers=_auth(token),
        )
        assert resp.status_code == 409, resp.text

        row = c.get(f"/api/admin/source-connections/{conn_id}", headers=_auth(token)).json()
        assert row["config"]["account"] == "acme-prod"

    def test_repoint_applies_when_confirmed(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        create_resp = c.post(
            "/api/admin/source-connections",
            json={
                "name": "sf-confirmed",
                "source_type": "snowflake",
                "config": {"account": "acme-prod", "user": "svc", "database": "PROD", "warehouse": "WH"},
            },
            headers=_auth(token),
        )
        conn_id = create_resp.json()["id"]
        _register_snowflake_table("rguard_confirmed")

        resp = c.put(
            f"/api/admin/source-connections/{conn_id}",
            json={
                "config": {"account": "acme-prod", "user": "svc", "database": "GOLD", "warehouse": "WH"},
                "confirm_connection_change": True,
            },
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["config"]["database"] == "GOLD"

    def test_tuning_field_is_not_guarded(self, seeded_app):
        """A tuning knob (not connection identity) is not a repoint."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        create_resp = c.post(
            "/api/admin/source-connections",
            json={
                "name": "sf-tuning",
                "source_type": "snowflake",
                "config": {"account": "acme-prod", "user": "svc", "database": "PROD", "warehouse": "WH"},
            },
            headers=_auth(token),
        )
        conn_id = create_resp.json()["id"]
        _register_snowflake_table("rguard_tuning")

        resp = c.put(
            f"/api/admin/source-connections/{conn_id}",
            json={
                "config": {
                    "account": "acme-prod",
                    "user": "svc",
                    "database": "PROD",
                    "warehouse": "WH",
                    "max_bytes_per_materialize": 1024,
                }
            },
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text

    def test_repoint_without_registrations_is_not_guarded(self, seeded_app):
        """First-time setup — nothing registered yet — has nothing to break."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        create_resp = c.post(
            "/api/admin/source-connections",
            json={
                "name": "sf-fresh",
                "source_type": "snowflake",
                "config": {"account": "acme-prod", "user": "svc", "database": "PROD", "warehouse": "WH"},
            },
            headers=_auth(token),
        )
        conn_id = create_resp.json()["id"]

        resp = c.put(
            f"/api/admin/source-connections/{conn_id}",
            json={"config": {"account": "acme-prod", "user": "svc", "database": "GOLD", "warehouse": "WH"}},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text

    def test_bigquery_row_is_not_guarded_by_this_slice(self, seeded_app):
        """Keboola/BigQuery identity relocation is out of scope for D2.3 —
        their row edits are unaffected by this guard."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        create_resp = c.post(
            "/api/admin/source-connections",
            json={"name": "bq-unguarded", "source_type": "bigquery", "config": {"project": "proj-a"}},
            headers=_auth(token),
        )
        conn_id = create_resp.json()["id"]
        from src.repositories import table_registry_repo

        table_registry_repo().register(
            id="rguard_bq",
            name="rguard_bq",
            source_type="bigquery",
            bucket="analytics",
            source_table="SESSIONS",
            query_mode="remote",
        )

        resp = c.put(
            f"/api/admin/source-connections/{conn_id}",
            json={"config": {"project": "proj-b"}},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
