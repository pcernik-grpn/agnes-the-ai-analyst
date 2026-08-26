"""POST/PUT /api/admin/source-connections must validate `source_type` +
`config` via `src.connection_specs.validate_connection_config` (spec
2026-08-26 Track D2, task D2.1).

Before this change the endpoint stored any `source_type` string and any
`config` shape unchecked — a typo'd `source_type` or a malformed config (a
`stack_url` without `https://`) landed in the registry and only surfaced
later as a confusing sync failure. This suite pins the new contract:

- an unknown `source_type` -> 400
- a malformed config for a KNOWN source_type -> 400, naming the field
- an EMPTY config at create/update still succeeds (the "Add data source"
  wizard creates a connection row before its config is complete — see
  `_validate_stack_url`'s own `required=False` note in
  `app/api/admin_source_connections.py`); only a NON-empty, malformed
  config is rejected
- a fully valid config for every registered spec (keboola, bigquery,
  databricks, snowflake) succeeds
"""

from __future__ import annotations

BASE = "/api/admin/source-connections"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestCreateRejectsGarbage:
    def test_unknown_source_type_rejected(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            BASE,
            json={"name": "test-oracle", "source_type": "oracle", "config": {}},
            headers=_auth(token),
        )
        assert resp.status_code == 400, resp.text
        assert "oracle" in resp.json()["detail"]

    def test_malformed_keboola_config_rejected(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            BASE,
            json={"name": "test-ftp-stack", "source_type": "keboola", "config": {"stack_url": "ftp://evil"}},
            headers=_auth(token),
        )
        assert resp.status_code == 400, resp.text
        assert "stack_url" in resp.json()["detail"]

    def test_malformed_bigquery_config_rejected(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            BASE,
            json={"name": "test-bq-no-project", "source_type": "bigquery", "config": {"location": "us"}},
            headers=_auth(token),
        )
        assert resp.status_code == 400, resp.text
        assert "project" in resp.json()["detail"]

    def test_malformed_databricks_config_rejected(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            BASE,
            json={"name": "test-dbx-no-host", "source_type": "databricks", "config": {"warehouse_id": "abc"}},
            headers=_auth(token),
        )
        assert resp.status_code == 400, resp.text
        assert "host" in resp.json()["detail"]

    def test_malformed_snowflake_config_rejected(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            BASE,
            json={
                "name": "test-sf-no-warehouse",
                "source_type": "snowflake",
                "config": {"account": "xy12345", "user": "u", "database": "d"},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 400, resp.text
        assert "warehouse" in resp.json()["detail"]


class TestCreateAcceptsEachValidSpec:
    def test_keboola(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            BASE,
            json={
                "name": "valid-keboola",
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201, resp.text

    def test_bigquery(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            BASE,
            json={"name": "valid-bigquery", "source_type": "bigquery", "config": {"project": "my-gcp-project"}},
            headers=_auth(token),
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["config"]["location"] == "us"  # normalized default

    def test_databricks(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            BASE,
            json={
                "name": "valid-databricks",
                "source_type": "databricks",
                "config": {"host": "https://dbc-x.cloud.databricks.com", "warehouse_id": "abc123"},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201, resp.text

    def test_snowflake(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            BASE,
            json={
                "name": "valid-snowflake",
                "source_type": "snowflake",
                "config": {
                    "account": "xy12345",
                    "user": "svc_agnes",
                    "database": "ANALYTICS",
                    "warehouse": "COMPUTE_WH",
                },
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["config"]["auth_type"] == "password"  # normalized default

    def test_empty_config_still_allowed_for_wizard_multi_step_create(self, seeded_app):
        """Not a regression on the documented `_validate_stack_url`
        `required=False` contract: the wizard creates a connection row
        before its config is complete."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            BASE,
            json={"name": "empty-config-keboola", "source_type": "keboola", "config": {}},
            headers=_auth(token),
        )
        assert resp.status_code == 201, resp.text


class TestUpdateRejectsGarbage:
    def test_update_rejects_malformed_config(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        created = c.post(
            BASE,
            json={
                "name": "update-me-keboola",
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
            },
            headers=_auth(token),
        )
        assert created.status_code == 201, created.text
        conn_id = created.json()["id"]

        resp = c.put(
            f"{BASE}/{conn_id}",
            json={"config": {"stack_url": "ftp://evil"}},
            headers=_auth(token),
        )
        assert resp.status_code == 400, resp.text
        assert "stack_url" in resp.json()["detail"]

    def test_update_accepts_valid_config(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        created = c.post(
            BASE,
            json={
                "name": "update-me-keboola-2",
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
            },
            headers=_auth(token),
        )
        assert created.status_code == 201, created.text
        conn_id = created.json()["id"]

        resp = c.put(
            f"{BASE}/{conn_id}",
            json={"config": {"stack_url": "https://connection2.example.com"}},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
