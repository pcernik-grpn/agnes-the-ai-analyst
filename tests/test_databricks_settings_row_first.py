"""``resolve_databricks_settings`` row-first resolution (D2.2): a registered
``source_connections`` row wins over ``data_source.databricks.*`` instance
config; zero-arg calls fall back to instance config when no row is
registered (byte-compatible)."""

from __future__ import annotations

import pytest


@pytest.fixture
def dbx_env(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)
    yield


def _fake_instance_config(monkeypatch, fake_cfg):
    monkeypatch.setattr("app.instance_config.load_instance_config", lambda **kw: fake_cfg, raising=False)
    monkeypatch.setattr("config.loader.load_instance_config", lambda **kw: fake_cfg, raising=False)
    from app.instance_config import reset_cache

    reset_cache()


class TestRowWinsOverInstanceConfig:
    def test_row_values_override_instance_config(self, dbx_env, monkeypatch):
        """Failing-first per the plan: a row with DIFFERENT values than
        instance config must win — the resolver returns the ROW's values."""
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

        from src.repositories import source_connections_repo, connection_secrets_repo

        source_connections_repo().create(
            id="dbx-row",
            name="databricks",
            source_type="databricks",
            config={"host": "https://row.cloud.databricks.com", "warehouse_id": "row-wh", "catalog": "row_catalog"},
            is_default=True,
        )
        connection_secrets_repo().upsert("dbx-row", "row-token")

        from connectors.databricks.semantic_layer import resolve_databricks_settings

        settings = resolve_databricks_settings()
        assert settings["host"] == "https://row.cloud.databricks.com"
        assert settings["warehouse_id"] == "row-wh"
        assert settings["catalog"] == "row_catalog"
        assert settings["token"] == "row-token"

    def test_explicit_connection_argument_is_honored(self, dbx_env, monkeypatch):
        from connectors.databricks.semantic_layer import resolve_databricks_settings

        monkeypatch.setenv("DATABRICKS_TOKEN", "explicit-token")
        connection = {
            "id": "explicit-1",
            "config": {"host": "https://x.cloud.databricks.com", "warehouse_id": "wh1"},
        }
        settings = resolve_databricks_settings(connection)
        assert settings["host"] == "https://x.cloud.databricks.com"
        assert settings["token"] == "explicit-token"


class TestZeroArgFallbackIsByteCompatible:
    def test_no_row_falls_back_to_instance_config(self, dbx_env, monkeypatch):
        _fake_instance_config(
            monkeypatch,
            {
                "data_source": {
                    "databricks": {
                        "host": "https://yaml-only.cloud.databricks.com",
                        "warehouse_id": "yaml-wh",
                    }
                }
            },
        )
        monkeypatch.setenv("DATABRICKS_TOKEN", "yaml-token")

        from connectors.databricks.semantic_layer import resolve_databricks_settings

        settings = resolve_databricks_settings()
        assert settings["host"] == "https://yaml-only.cloud.databricks.com"
        assert settings["token"] == "yaml-token"

    def test_no_row_and_no_config_returns_none(self, dbx_env, monkeypatch):
        _fake_instance_config(monkeypatch, {})
        from connectors.databricks.semantic_layer import resolve_databricks_settings

        assert resolve_databricks_settings() is None


class TestRowFromLegacySeed:
    def test_row_seeded_with_only_top_level_token_env_still_resolves(self, dbx_env, monkeypatch):
        """A row seeded by app.connections_seed (D2.1) carries the secret-ref
        name on the row's top-level `token_env` column, not embedded in
        config. The row-first path must still find the right env var."""
        from src.repositories import source_connections_repo

        monkeypatch.setenv("MY_CUSTOM_DBX_TOKEN", "custom-secret")
        source_connections_repo().create(
            id="dbx-legacy",
            name="databricks",
            source_type="databricks",
            config={"host": "https://acme.cloud.databricks.com", "warehouse_id": "wh1"},
            token_env="MY_CUSTOM_DBX_TOKEN",
            is_default=True,
        )

        from connectors.databricks.semantic_layer import resolve_databricks_settings

        settings = resolve_databricks_settings()
        assert settings["token"] == "custom-secret"
