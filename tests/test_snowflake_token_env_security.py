"""RBAC review, second round (2026-08-26): the Databricks ``token_env``
allowlist fix (``connectors/databricks/semantic_layer.py::_resolve_row_token``
+ its instance-config sibling, ``resolve_databricks_settings``) sits in the
SHARED resolver every consumer calls through, so gating there closes the gap
for every Databricks consumer at once. Snowflake only got the check in
``connectors/snowflake/extract_init.py`` — leaving ``extractor.py``
(``materialize_query``, every scheduler tick), ``remote.py`` (the
``query_mode='remote'`` schema fetch / ATTACH), ``discovery.py``,
``semantic_ossie.py``, and the legacy
``settings.py::_resolve_from_instance_config`` yaml path resolving the
secret with NO allowlist check.

This closes the gap at the true choke point:
``connectors.snowflake.settings._resolve_secret`` — the single function every
named-env-var lookup in the module funnels through (the row path's env
fallback via ``_resolve_row_secret``, the legacy yaml path, and the key-pair
passphrase lookup for both), which is itself the one dependency of
``resolve_snowflake_settings()``, the single entry point every Snowflake
consumer calls. ``extract_init.py``'s own pre-existing check becomes
redundant defense-in-depth; the tests below exercise the OTHER consumers it
never covered."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def sf_env(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SNOWFLAKE_PASSWORD", raising=False)
    monkeypatch.delenv("SNOWFLAKE_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE", raising=False)
    monkeypatch.delenv("AGNES_REMOTE_ATTACH_TOKEN_ENVS", raising=False)
    yield


def _fake_instance_config(monkeypatch, fake_cfg):
    monkeypatch.setattr("app.instance_config.load_instance_config", lambda **kw: fake_cfg, raising=False)
    monkeypatch.setattr("config.loader.load_instance_config", lambda **kw: fake_cfg, raising=False)
    from app.instance_config import reset_cache

    reset_cache()


class TestResolveSecretRefusesDisallowedTokenEnv:
    """Unit-level: the choke point itself."""

    def test_disallowed_name_is_refused(self, sf_env, monkeypatch):
        from connectors.snowflake.settings import _resolve_secret
        from src.orchestrator_security import is_token_env_allowed

        assert not is_token_env_allowed("ANTHROPIC_API_KEY")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-never-leak")

        assert _resolve_secret("ANTHROPIC_API_KEY") == ""

    def test_allowlisted_name_resolves(self, sf_env, monkeypatch):
        from connectors.snowflake.settings import _resolve_secret

        monkeypatch.setenv("SNOWFLAKE_PASSWORD", "good-password")
        assert _resolve_secret("SNOWFLAKE_PASSWORD") == "good-password"

    def test_default_passphrase_env_name_still_resolves(self, sf_env, monkeypatch):
        """The passphrase's own module default name must stay usable — the
        write-time guard already allowlist-checks
        ``private_key_passphrase_env``
        (``app.api.admin_source_connections._reject_disallowed_config_token_envs``),
        so gating resolve-time WITHOUT the default name on the allowlist
        would break the default key-pair passphrase path for every existing
        deploy, not just close a hole."""
        from connectors.snowflake.settings import SF_PRIVATE_KEY_PASSPHRASE_ENV, _resolve_secret

        monkeypatch.setenv(SF_PRIVATE_KEY_PASSPHRASE_ENV, "shh")
        assert _resolve_secret(SF_PRIVATE_KEY_PASSPHRASE_ENV) == "shh"


class TestResolveSnowflakeSettingsRefusesDisallowedTokenEnv:
    def test_row_with_disallowed_token_env_does_not_resolve(self, sf_env, monkeypatch):
        from src.repositories import source_connections_repo
        from connectors.snowflake.settings import resolve_snowflake_settings

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-never-leak")
        source_connections_repo().create(
            id="sf-evil",
            name="snowflake",
            source_type="snowflake",
            config={
                "account": "acme",
                "user": "svc",
                "database": "DB",
                "warehouse": "WH",
                "token_env": "ANTHROPIC_API_KEY",
            },
            is_default=True,
        )
        assert resolve_snowflake_settings() is None

    def test_row_with_disallowed_private_key_env_does_not_resolve(self, sf_env, monkeypatch):
        from src.repositories import source_connections_repo
        from connectors.snowflake.settings import resolve_snowflake_settings

        monkeypatch.setenv("JWT_SECRET_KEY", "super-secret-do-not-leak")
        source_connections_repo().create(
            id="sf-evil-kp",
            name="snowflake",
            source_type="snowflake",
            config={
                "account": "acme",
                "user": "svc",
                "database": "DB",
                "warehouse": "WH",
                "auth_type": "key_pair",
                "private_key_env": "JWT_SECRET_KEY",
            },
            is_default=True,
        )
        assert resolve_snowflake_settings() is None

    def test_row_with_allowlisted_token_env_resolves(self, sf_env, monkeypatch):
        from src.repositories import source_connections_repo
        from connectors.snowflake.settings import resolve_snowflake_settings

        monkeypatch.setenv("SNOWFLAKE_PASSWORD", "good-password")
        source_connections_repo().create(
            id="sf-good",
            name="snowflake",
            source_type="snowflake",
            config={"account": "acme", "user": "svc", "database": "DB", "warehouse": "WH"},
            is_default=True,
        )
        settings = resolve_snowflake_settings()
        assert settings is not None
        assert settings["password"] == "good-password"

    def test_instance_config_path_with_disallowed_token_env_does_not_resolve(self, sf_env, monkeypatch):
        """Legacy ``data_source.snowflake.*`` yaml path — same guard applies
        to the resolver's instance-config branch."""
        from connectors.snowflake.settings import resolve_snowflake_settings

        _fake_instance_config(
            monkeypatch,
            {
                "data_source": {
                    "snowflake": {
                        "account": "yaml-account",
                        "user": "yaml-user",
                        "database": "YAML_DB",
                        "warehouse": "YAML_WH",
                        "token_env": "JWT_SECRET_KEY",
                    }
                }
            },
        )
        monkeypatch.setenv("JWT_SECRET_KEY", "super-secret-do-not-leak")
        assert resolve_snowflake_settings() is None

    def test_instance_config_path_with_allowlisted_token_env_resolves(self, sf_env, monkeypatch):
        from connectors.snowflake.settings import resolve_snowflake_settings

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
        settings = resolve_snowflake_settings()
        assert settings is not None
        assert settings["password"] == "yaml-password"

    def test_vault_secret_is_unaffected_by_token_env_name(self, sf_env, monkeypatch):
        """The connection's OWN vault slot is a legitimate credential path
        regardless of the configured ``token_env`` name — only the
        env-var-by-name fallback is allowlist-gated, same as
        ``connectors.databricks.semantic_layer._resolve_row_token``."""
        from src.repositories import connection_secrets_repo, source_connections_repo
        from connectors.snowflake.settings import resolve_snowflake_settings

        source_connections_repo().create(
            id="sf-vault",
            name="snowflake",
            source_type="snowflake",
            config={
                "account": "acme",
                "user": "svc",
                "database": "DB",
                "warehouse": "WH",
                "token_env": "ANTHROPIC_API_KEY",
            },
            is_default=True,
        )
        connection_secrets_repo().upsert("sf-vault", "vault-password")
        settings = resolve_snowflake_settings()
        assert settings is not None
        assert settings["password"] == "vault-password"


class TestNonExtractInitConsumersNeverResolveADisallowedCredential:
    """``extract_init.py`` already refused before this round; prove the OTHER
    consumers the reviewer named — ``materialize_query`` (the scheduler's
    materialized pass) and ``remote.fetch_schema`` (``agnes schema`` on a
    ``query_mode='remote'`` row) — never even get a resolved credential to
    attach with, now that the choke point is gated."""

    def test_materialized_pass_never_calls_materialize_query(self, sf_env, monkeypatch, tmp_path):
        from app.api.sync import _run_materialized_pass
        from src.repositories import source_connections_repo

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-never-leak")
        source_connections_repo().create(
            id="sf-mat-evil",
            name="snowflake",
            source_type="snowflake",
            config={
                "account": "acme",
                "user": "svc",
                "database": "DB",
                "warehouse": "WH",
                "token_env": "ANTHROPIC_API_KEY",
            },
            is_default=True,
        )

        monkeypatch.setattr("app.api.sync._get_data_dir", lambda: str(tmp_path))
        monkeypatch.setattr("app.api.sync.is_table_due", lambda schedule, last: True)

        sf_materialize = MagicMock()
        monkeypatch.setattr("connectors.snowflake.extractor.materialize_query", sf_materialize)

        registry = MagicMock()
        registry.list_all.return_value = [
            {
                "name": "orders_summary",
                "id": "orders_summary",
                "source_type": "snowflake",
                "query_mode": "materialized",
                "source_query": "SELECT * FROM sf.public.orders",
                "bucket": "public",
                "source_table": "orders",
                "sync_schedule": None,
            }
        ]
        state = MagicMock()
        state.get_last_sync.return_value = None
        monkeypatch.setattr("app.api.sync.table_registry_repo", lambda: registry)
        monkeypatch.setattr("app.api.sync.sync_state_repo", lambda: state)

        summary = _run_materialized_pass(None, None, source_type="snowflake")

        sf_materialize.assert_not_called()
        assert summary["materialized"] == []
        assert summary["errors"], (
            "an unconfigured (refused) Snowflake connection must surface as an error, not a silent no-op"
        )

    def test_remote_schema_endpoint_never_attaches(self, seeded_app, monkeypatch):
        """``/api/v2/schema/{id}`` on a ``query_mode='remote'`` row must 404
        instead of reaching ``connectors.snowflake.remote.fetch_schema``
        (which opens a real ATTACH) once the resolved settings are refused."""
        from src.repositories import source_connections_repo, table_registry_repo

        monkeypatch.delenv("AGNES_REMOTE_ATTACH_TOKEN_ENVS", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-never-leak")
        source_connections_repo().create(
            id="sf-schema-evil",
            name="snowflake",
            source_type="snowflake",
            config={
                "account": "acme",
                "user": "svc",
                "database": "DB",
                "warehouse": "WH",
                "token_env": "ANTHROPIC_API_KEY",
            },
            is_default=True,
        )
        repo = table_registry_repo()
        repo.register(
            id="sfschema_evil",
            name="sfschema_evil",
            source_type="snowflake",
            bucket="GOLD",
            source_table="T",
            query_mode="remote",
        )
        try:
            import connectors.snowflake.remote as sf_remote

            fetch_calls = []
            monkeypatch.setattr(
                sf_remote,
                "fetch_schema",
                lambda row, *, settings, allow_empty=True: fetch_calls.append(row) or [],
            )

            c = seeded_app["client"]
            resp = c.get(
                "/api/v2/schema/sfschema_evil",
                headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
            )
            assert resp.status_code == 404, resp.text
            assert not fetch_calls, (
                "the credential must never reach fetch_schema (a real ATTACH) with a disallowed token_env"
            )
        finally:
            try:
                repo.unregister("sfschema_evil")
            except Exception:
                pass
