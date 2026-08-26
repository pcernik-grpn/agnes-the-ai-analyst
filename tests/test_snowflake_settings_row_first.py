"""``resolve_snowflake_settings`` row-first resolution (D2.2): a registered
``source_connections`` row (password OR key_pair) wins over
``data_source.snowflake.*`` instance config; zero-arg calls fall back to
instance config when no row is registered (byte-compatible)."""

from __future__ import annotations

import pytest


@pytest.fixture
def sf_env(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SNOWFLAKE_PASSWORD", raising=False)
    monkeypatch.delenv("SNOWFLAKE_PRIVATE_KEY", raising=False)
    yield


def _fake_instance_config(monkeypatch, fake_cfg):
    monkeypatch.setattr("app.instance_config.load_instance_config", lambda **kw: fake_cfg, raising=False)
    monkeypatch.setattr("config.loader.load_instance_config", lambda **kw: fake_cfg, raising=False)
    from app.instance_config import reset_cache

    reset_cache()


class TestRowWinsOverInstanceConfig:
    def test_row_values_override_instance_config(self, sf_env, monkeypatch):
        """Failing-first per the plan: a row with DIFFERENT values than
        instance config must win — the resolver returns the ROW's values."""
        _fake_instance_config(
            monkeypatch,
            {
                "data_source": {
                    "snowflake": {
                        "account": "yaml-account",
                        "user": "yaml-user",
                        "database": "YAML_DB",
                        "warehouse": "YAML_WH",
                    }
                }
            },
        )
        monkeypatch.setenv("SNOWFLAKE_PASSWORD", "yaml-password")

        from src.repositories import source_connections_repo, connection_secrets_repo

        source_connections_repo().create(
            id="sf-row",
            name="snowflake",
            source_type="snowflake",
            config={"account": "row-account", "user": "row-user", "database": "ROW_DB", "warehouse": "ROW_WH"},
            is_default=True,
        )
        connection_secrets_repo().upsert("sf-row", "row-password")

        from connectors.snowflake.settings import resolve_snowflake_settings

        settings = resolve_snowflake_settings()
        assert settings["account"] == "row-account"
        assert settings["user"] == "row-user"
        assert settings["database"] == "ROW_DB"
        assert settings["warehouse"] == "ROW_WH"
        assert settings["password"] == "row-password"

    def test_key_pair_row_resolves_private_key_and_passphrase(self, sf_env, monkeypatch):
        from src.repositories import source_connections_repo, connection_secrets_repo

        source_connections_repo().create(
            id="sf-kp",
            name="snowflake",
            source_type="snowflake",
            config={
                "account": "acme",
                "user": "svc",
                "database": "DB",
                "warehouse": "WH",
                "auth_type": "key_pair",
            },
            is_default=True,
        )
        connection_secrets_repo().upsert("sf-kp", "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----")
        monkeypatch.setenv("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE", "shh")

        from connectors.snowflake.settings import resolve_snowflake_settings

        settings = resolve_snowflake_settings()
        assert settings["auth_type"] == "key_pair"
        assert "BEGIN PRIVATE KEY" in settings["private_key"]
        assert settings["private_key_passphrase"] == "shh"

    def test_explicit_connection_argument_is_honored(self, sf_env, monkeypatch):
        """Passing a connection dict directly (not looked up) also resolves
        from its config — the threading contract other call sites rely on."""
        from connectors.snowflake.settings import resolve_snowflake_settings

        monkeypatch.setenv("SNOWFLAKE_PASSWORD", "explicit-pass")
        connection = {
            "id": "explicit-1",
            "config": {"account": "a", "user": "u", "database": "d", "warehouse": "w"},
        }
        settings = resolve_snowflake_settings(connection)
        assert settings["account"] == "a"
        assert settings["password"] == "explicit-pass"


class TestZeroArgFallbackIsByteCompatible:
    def test_no_row_falls_back_to_instance_config(self, sf_env, monkeypatch):
        _fake_instance_config(
            monkeypatch,
            {
                "data_source": {
                    "snowflake": {
                        "account": "yaml-only-account",
                        "user": "yaml-user",
                        "database": "YAML_DB",
                        "warehouse": "YAML_WH",
                    }
                }
            },
        )
        monkeypatch.setenv("SNOWFLAKE_PASSWORD", "yaml-password")

        from connectors.snowflake.settings import resolve_snowflake_settings

        settings = resolve_snowflake_settings()
        assert settings["account"] == "yaml-only-account"
        assert settings["password"] == "yaml-password"

    def test_no_row_and_no_config_returns_none(self, sf_env, monkeypatch):
        _fake_instance_config(monkeypatch, {})
        from connectors.snowflake.settings import resolve_snowflake_settings

        assert resolve_snowflake_settings() is None


class TestRowFromLegacySeed:
    def test_row_seeded_with_only_top_level_token_env_still_resolves(self, sf_env, monkeypatch):
        """A row seeded by app.connections_seed (D2.1) carries the secret-ref
        name on the row's top-level `token_env` column, not embedded in
        config. The row-first path must still find the right env var —
        provided the name is on the remote-attach allowlist (RBAC review,
        second round, 2026-08-26: `connectors.snowflake.settings._resolve_
        secret` refuses an off-allowlist name regardless of where it's
        stored), same as an operator who wants a non-default token_env name
        has to opt it in via AGNES_REMOTE_ATTACH_TOKEN_ENVS — mirrors
        `test_databricks_settings_row_first.py`'s sibling test."""
        from src.repositories import source_connections_repo

        monkeypatch.setenv("MY_CUSTOM_SF_PASSWORD", "custom-secret")
        monkeypatch.setenv("AGNES_REMOTE_ATTACH_TOKEN_ENVS", "MY_CUSTOM_SF_PASSWORD")
        source_connections_repo().create(
            id="sf-legacy",
            name="snowflake",
            source_type="snowflake",
            config={"account": "acme", "user": "svc", "database": "DB", "warehouse": "WH"},
            token_env="MY_CUSTOM_SF_PASSWORD",
            is_default=True,
        )

        from connectors.snowflake.settings import resolve_snowflake_settings

        settings = resolve_snowflake_settings()
        assert settings["password"] == "custom-secret"
        assert settings["token_env"] == "MY_CUSTOM_SF_PASSWORD"
