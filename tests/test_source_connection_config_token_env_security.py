"""D2.2 security must-fix: a connection's config-EMBEDDED secret-ref name
(token_env / private_key_env / private_key_passphrase_env) must be
allowlist-checked at write time, the same as the top-level ``token_env``
field already is — otherwise an admin can point one of these at an
unrelated secret (e.g. ANTHROPIC_API_KEY) and exfiltrate it as a
Snowflake/Databricks credential on the first attach."""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestCreateRejectsDisallowedConfigTokenEnv:
    def test_snowflake_disallowed_token_env_in_config_is_rejected(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/source-connections",
            json={
                "name": "sf-evil",
                "source_type": "snowflake",
                "config": {
                    "account": "acme",
                    "user": "svc",
                    "database": "DB",
                    "warehouse": "WH",
                    "token_env": "ANTHROPIC_API_KEY",
                },
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 400, resp.text
        assert "ANTHROPIC_API_KEY" in resp.text

    def test_snowflake_disallowed_private_key_env_in_config_is_rejected(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/source-connections",
            json={
                "name": "sf-evil-2",
                "source_type": "snowflake",
                "config": {
                    "account": "acme",
                    "user": "svc",
                    "database": "DB",
                    "warehouse": "WH",
                    "auth_type": "key_pair",
                    "private_key_env": "JWT_SECRET_KEY",
                },
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 400, resp.text
        assert "JWT_SECRET_KEY" in resp.text

    def test_snowflake_disallowed_passphrase_env_in_config_is_rejected(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/source-connections",
            json={
                "name": "sf-evil-3",
                "source_type": "snowflake",
                "config": {
                    "account": "acme",
                    "user": "svc",
                    "database": "DB",
                    "warehouse": "WH",
                    "auth_type": "key_pair",
                    "private_key_passphrase_env": "DATABASE_URL",
                },
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 400, resp.text
        assert "DATABASE_URL" in resp.text

    def test_databricks_disallowed_token_env_in_config_is_rejected(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/source-connections",
            json={
                "name": "dbx-evil",
                "source_type": "databricks",
                "config": {
                    "host": "https://acme.cloud.databricks.com",
                    "warehouse_id": "wh1",
                    "token_env": "ANTHROPIC_API_KEY",
                },
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 400, resp.text
        assert "ANTHROPIC_API_KEY" in resp.text

    def test_allowlisted_token_env_in_config_is_accepted(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/source-connections",
            json={
                "name": "sf-ok",
                "source_type": "snowflake",
                "config": {
                    "account": "acme",
                    "user": "svc",
                    "database": "DB",
                    "warehouse": "WH",
                    "token_env": "SNOWFLAKE_PASSWORD",
                },
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 201, resp.text

    def test_other_source_types_are_not_checked_for_this_field(self, seeded_app):
        """bigquery/keboola configs don't carry secret-ref names in `config`
        at all — a `token_env`-named key there is just ordinary (unvalidated)
        free-form config, not a credential-selection field."""
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/source-connections",
            json={
                "name": "bq-unrelated-field",
                "source_type": "bigquery",
                "config": {"project": "acme-proj", "token_env": "ANYTHING_AT_ALL"},
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 201, resp.text


class TestUpdateRejectsDisallowedConfigTokenEnv:
    def test_update_rejects_disallowed_config_token_env(self, seeded_app):
        c = seeded_app["client"]
        create_resp = c.post(
            "/api/admin/source-connections",
            json={
                "name": "sf-update-target",
                "source_type": "snowflake",
                "config": {"account": "acme", "user": "svc", "database": "DB", "warehouse": "WH"},
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert create_resp.status_code == 201, create_resp.text
        conn_id = create_resp.json()["id"]

        resp = c.put(
            f"/api/admin/source-connections/{conn_id}",
            json={
                "config": {
                    "account": "acme",
                    "user": "svc",
                    "database": "DB",
                    "warehouse": "WH",
                    "token_env": "ANTHROPIC_API_KEY",
                }
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 400, resp.text
        assert "ANTHROPIC_API_KEY" in resp.text
