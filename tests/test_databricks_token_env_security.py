"""RBAC review Finding 3 (2026-08-26): Databricks's ``token_env`` was resolved
with NO allowlist check, while this same PR made Snowflake refuse a
disallowed ``token_env`` BEFORE attach (``connectors/snowflake/
extract_init.py``). ``_resolve_row_token`` (the row path) and
``_resolve_databricks_from_instance_config`` (the legacy yaml path) are the
two functions behind ``resolve_databricks_settings()`` — the single choke
point every Databricks consumer (the live Unity Catalog ATTACH in
``extract_init.rebuild_from_registry``, ``app/api/query.py``,
``app/api/v2_scan.py``, ``app/api/v2_schema.py``, the semantic-layer sync)
calls through — so gating there closes the gap everywhere at once: an
admin-set (or yaml-seeded) ``token_env`` naming an unrelated secret
(``ANTHROPIC_API_KEY``, ``JWT_SECRET_KEY``, ...) must never resolve and ship
out as the Databricks credential."""

from __future__ import annotations

import pytest


@pytest.fixture
def dbx_env(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)
    monkeypatch.delenv("AGNES_REMOTE_ATTACH_TOKEN_ENVS", raising=False)
    yield


def _fake_instance_config(monkeypatch, fake_cfg):
    monkeypatch.setattr("app.instance_config.load_instance_config", lambda **kw: fake_cfg, raising=False)
    monkeypatch.setattr("config.loader.load_instance_config", lambda **kw: fake_cfg, raising=False)
    from app.instance_config import reset_cache

    reset_cache()


class TestResolveRowTokenRefusesDisallowedTokenEnv:
    def test_disallowed_token_env_is_refused(self, dbx_env, monkeypatch):
        from connectors.databricks.semantic_layer import _resolve_row_token
        from src.orchestrator_security import is_token_env_allowed

        assert not is_token_env_allowed("ANTHROPIC_API_KEY")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-never-leak")

        token = _resolve_row_token({"id": "dbx-1"}, "ANTHROPIC_API_KEY")
        assert token == ""

    def test_allowlisted_token_env_resolves(self, dbx_env, monkeypatch):
        from connectors.databricks.semantic_layer import _resolve_row_token

        monkeypatch.setenv("DATABRICKS_TOKEN", "good-token")
        token = _resolve_row_token({"id": "dbx-2"}, "DATABRICKS_TOKEN")
        assert token == "good-token"

    def test_vault_secret_is_unaffected_by_token_env_name(self, dbx_env, monkeypatch):
        """The connection's OWN vault slot is a legitimate credential path
        regardless of the configured ``token_env`` name — only the
        env-var-by-name fallback is allowlist-gated, same as
        ``app.api.admin_source_connections._resolve_token``."""
        from src.repositories import connection_secrets_repo
        from connectors.databricks.semantic_layer import _resolve_row_token

        connection_secrets_repo().upsert("dbx-3", "vault-secret")
        token = _resolve_row_token({"id": "dbx-3"}, "ANTHROPIC_API_KEY")
        assert token == "vault-secret"


class TestResolveDatabricksSettingsRefusesDisallowedTokenEnv:
    def test_row_with_disallowed_token_env_does_not_resolve(self, dbx_env, monkeypatch):
        from src.repositories import source_connections_repo
        from connectors.databricks.semantic_layer import resolve_databricks_settings

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-never-leak")
        source_connections_repo().create(
            id="dbx-evil",
            name="databricks",
            source_type="databricks",
            config={
                "host": "https://acme.cloud.databricks.com",
                "warehouse_id": "wh1",
                "catalog": "main",
                "token_env": "ANTHROPIC_API_KEY",
            },
            is_default=True,
        )
        assert resolve_databricks_settings() is None

    def test_row_with_allowlisted_token_env_resolves(self, dbx_env, monkeypatch):
        from src.repositories import source_connections_repo
        from connectors.databricks.semantic_layer import resolve_databricks_settings

        monkeypatch.setenv("DATABRICKS_TOKEN", "good-token")
        source_connections_repo().create(
            id="dbx-good",
            name="databricks",
            source_type="databricks",
            config={"host": "https://acme.cloud.databricks.com", "warehouse_id": "wh1", "catalog": "main"},
            is_default=True,
        )
        settings = resolve_databricks_settings()
        assert settings is not None
        assert settings["token"] == "good-token"

    def test_instance_config_path_with_disallowed_token_env_does_not_resolve(self, dbx_env, monkeypatch):
        """Legacy ``data_source.databricks.*`` yaml path — same guard applies
        to the resolver at ``semantic_layer.py``'s instance-config branch."""
        from connectors.databricks.semantic_layer import resolve_databricks_settings

        _fake_instance_config(
            monkeypatch,
            {
                "data_source": {
                    "databricks": {
                        "host": "https://yaml.cloud.databricks.com",
                        "warehouse_id": "yaml-wh",
                        "catalog": "yaml_catalog",
                        "token_env": "JWT_SECRET_KEY",
                    }
                }
            },
        )
        monkeypatch.setenv("JWT_SECRET_KEY", "super-secret-do-not-leak")
        assert resolve_databricks_settings() is None

    def test_instance_config_path_with_allowlisted_token_env_resolves(self, dbx_env, monkeypatch):
        from connectors.databricks.semantic_layer import resolve_databricks_settings

        _fake_instance_config(
            monkeypatch,
            {
                "data_source": {
                    "databricks": {
                        "host": "https://yaml.cloud.databricks.com",
                        "warehouse_id": "yaml-wh",
                        "catalog": "yaml_catalog",
                    }
                }
            },
        )
        monkeypatch.setenv("DATABRICKS_TOKEN", "yaml-token")
        settings = resolve_databricks_settings()
        assert settings is not None
        assert settings["token"] == "yaml-token"


class TestRefusalHappensBeforeAttach:
    def test_rebuild_from_registry_never_attaches_with_disallowed_token_env(self, e2e_env, monkeypatch):
        """The live Unity Catalog ATTACH path
        (``extract_init.rebuild_from_registry``) must never reach the ATTACH
        call at all when the resolved settings are refused — proven with a
        spy standing in for the real attach function, mirroring
        ``test_init_extract_refuses_when_token_env_not_allowlisted`` in
        ``tests/test_snowflake_connector.py``."""
        from cryptography.fernet import Fernet

        monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
        monkeypatch.delenv("AGNES_REMOTE_ATTACH_TOKEN_ENVS", raising=False)
        monkeypatch.setattr("connectors.databricks.attach.attach_enabled", lambda: True)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-never-leak")

        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository
        from src.repositories import source_connections_repo

        source_connections_repo().create(
            id="dbx-evil-2",
            name="databricks",
            source_type="databricks",
            config={
                "host": "https://acme.cloud.databricks.com",
                "warehouse_id": "wh1",
                "catalog": "main",
                "token_env": "ANTHROPIC_API_KEY",
            },
            is_default=True,
        )
        conn = get_system_db()
        try:
            TableRegistryRepository(conn).register(
                id="dbx.sales.orders",
                name="orders",
                source_type="databricks",
                bucket="sales",
                source_table="orders_raw",
                query_mode="remote",
            )
        finally:
            conn.close()

        attach_calls = []
        monkeypatch.setattr(
            "connectors.databricks.extract_init._default_attach_fn",
            lambda conn, *, url, token: attach_calls.append(url),
        )

        from connectors.databricks.extract_init import rebuild_from_registry

        result = rebuild_from_registry()
        assert not attach_calls, "the credential must never reach a real ATTACH with a disallowed token_env"
        assert result["skipped"] is True
        assert result["reason"] == "not_configured"
