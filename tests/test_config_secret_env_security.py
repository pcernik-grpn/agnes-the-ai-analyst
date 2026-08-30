"""Split of the token-env allowlist into its two consumer classes.

``src/orchestrator_security.py`` historically fed TWO independent consumer
classes from the single ``_DEFAULT_TOKEN_ENVS`` set:

- **inbound** — the connector-ATTACH trust boundary: ``src/orchestrator.py``
  and ``src/db.py`` resolve a ``_remote_attach`` row's ``token_env`` from ANY
  connector's extract.duckdb and send the value as ``ATTACH ... TOKEN`` to the
  row's own ``url`` (``is_attach_host_allowed`` is default-open when
  ``AGNES_REMOTE_ATTACH_HOST_ALLOWLIST`` is unset);
- **outbound** — config-driven secret resolution: the SharePoint/Snowflake/
  Databricks/Keboola settings resolvers read a secret whose env-var NAME sits
  in admin-writable connection config, and hand it to that source's own
  client.

Sharing one set meant every outbound-only secret (the SharePoint certificate
private key, the Snowflake key-pair passphrase) was ALSO a legal
``_remote_attach`` ``token_env`` — a malicious/compromised connector could
write ``token_env=SHAREPOINT_CERT_PRIVATE_KEY`` into its extract and have the
orchestrator exfiltrate the certificate key to an attacker-chosen host on
every query. Same bug class the ``_PRODUCER_KEY_ENVS`` split closed for the
anonymization HMAC key.

These tests are the RATCHET for the split: every name in
``_CONFIG_SECRET_ONLY_ENVS`` must never (re)appear in the effective
connector-ATTACH allowlist, and both ATTACH paths must refuse a
``_remote_attach`` row naming one.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import duckdb
import pytest

from src.orchestrator import SyncOrchestrator
from src.orchestrator_security import _CONFIG_SECRET_ONLY_ENVS, _DEFAULT_TOKEN_ENVS

CONFIG_ONLY_SECRETS = sorted(_CONFIG_SECRET_ONLY_ENVS)


@pytest.fixture(autouse=True)
def _no_operator_overrides(monkeypatch):
    monkeypatch.delenv("AGNES_REMOTE_ATTACH_TOKEN_ENVS", raising=False)
    monkeypatch.delenv("AGNES_CONFIG_SECRET_ENVS", raising=False)


class TestAttachAllowlistExcludesConfigOnlySecrets:
    """The ratchet: config-resolution-only secrets are NOT legal ATTACH
    token_envs. Parametrized over ``_CONFIG_SECRET_ONLY_ENVS`` so any secret
    added to the outbound set later is automatically held to the same bar."""

    def test_the_two_default_sets_are_disjoint(self):
        assert not (_CONFIG_SECRET_ONLY_ENVS & _DEFAULT_TOKEN_ENVS), (
            "a config-resolution-only secret has been (re)added to the inbound "
            "connector-ATTACH default allowlist — that would let a malicious "
            "connector's _remote_attach row exfiltrate it to an attacker-chosen "
            "host; see this file's module docstring"
        )

    @pytest.mark.parametrize("name", CONFIG_ONLY_SECRETS)
    def test_config_only_secret_is_not_in_the_effective_attach_allowlist(self, name):
        from src.orchestrator_security import get_allowed_token_envs

        assert name not in get_allowed_token_envs()

    @pytest.mark.parametrize("name", CONFIG_ONLY_SECRETS)
    def test_config_only_secret_is_refused_by_the_attach_gate(self, name):
        from src.orchestrator_security import is_token_env_allowed

        assert is_token_env_allowed(name) is False

    def test_expected_outbound_only_names_are_in_the_config_only_set(self):
        """The split exists FOR these two names — losing one from the set
        silently re-opens the hole (membership in `_DEFAULT_TOKEN_ENVS`
        would not be caught by the parametrized ratchets above)."""
        assert "SHAREPOINT_CERT_PRIVATE_KEY" in _CONFIG_SECRET_ONLY_ENVS
        assert "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE" in _CONFIG_SECRET_ONLY_ENVS


class TestConfigSecretEnvAllowlist:
    """Unit coverage for ``is_config_secret_env_allowed`` — the gate every
    config-driven settings resolver funnels through after the split."""

    @pytest.mark.parametrize("name", CONFIG_ONLY_SECRETS)
    def test_config_only_secret_is_allowed(self, name):
        from src.orchestrator_security import is_config_secret_env_allowed

        assert is_config_secret_env_allowed(name) is True

    @pytest.mark.parametrize("name", ["KBC_TOKEN", "SNOWFLAKE_PASSWORD", "DATABRICKS_TOKEN"])
    def test_attach_allowlisted_data_source_token_is_also_allowed(self, name):
        """Snowflake/Databricks/Keboola resolvers read the SAME data-source
        tokens the ATTACH boundary allows (config union ⊇ attach set) — the
        split must not break them."""
        from src.orchestrator_security import is_config_secret_env_allowed

        assert is_config_secret_env_allowed(name) is True

    @pytest.mark.parametrize("name", ["ANTHROPIC_API_KEY", "JWT_SECRET_KEY", "AGNES_ANONYMIZATION_HMAC_KEY"])
    def test_unrelated_runtime_secret_is_refused(self, name):
        from src.orchestrator_security import is_config_secret_env_allowed

        assert is_config_secret_env_allowed(name) is False

    @pytest.mark.parametrize("name", ["sharepoint_cert_private_key", "NOT A NAME", "", "1BAD"])
    def test_structurally_invalid_name_is_refused(self, name):
        from src.orchestrator_security import is_config_secret_env_allowed

        assert is_config_secret_env_allowed(name) is False

    def test_operator_override_replaces_config_only_defaults(self, monkeypatch):
        """AGNES_CONFIG_SECRET_ENVS REPLACES the config-only defaults (same
        semantics as AGNES_REMOTE_ATTACH_TOKEN_ENVS) but never touches the
        attach-side names, which stay valid through the union."""
        from src.orchestrator_security import (
            get_allowed_token_envs,
            is_config_secret_env_allowed,
        )

        monkeypatch.setenv("AGNES_CONFIG_SECRET_ENVS", "MY_CUSTOM_CERT_PEM")
        assert is_config_secret_env_allowed("MY_CUSTOM_CERT_PEM") is True
        assert is_config_secret_env_allowed("SHAREPOINT_CERT_PRIVATE_KEY") is False
        assert is_config_secret_env_allowed("KBC_TOKEN") is True
        # The override must NOT leak the custom name into the ATTACH boundary.
        assert "MY_CUSTOM_CERT_PEM" not in get_allowed_token_envs()

    def test_attach_override_names_are_config_allowed_too(self, monkeypatch):
        """Backward compatibility: deployments that allowlisted a custom
        data-source token name via AGNES_REMOTE_ATTACH_TOKEN_ENVS keep
        resolving it in the settings resolvers (the union includes the
        effective attach set, override and all)."""
        from src.orchestrator_security import is_config_secret_env_allowed

        monkeypatch.setenv("AGNES_REMOTE_ATTACH_TOKEN_ENVS", "MY_CUSTOM_SF_PASSWORD")
        assert is_config_secret_env_allowed("MY_CUSTOM_SF_PASSWORD") is True


@pytest.fixture
def captured_conn():
    """A duckdb.Connection-like mock recording every execute() string.

    Same shape as ``tests/test_orchestrator_remote_attach_security.py``'s
    fixture; duplicated (small) so this file stays self-contained."""
    sql_calls: list[str] = []
    conn = MagicMock()
    rows_buffer = {"attach": []}

    def execute_side_effect(sql, *args, **kwargs):
        sql_calls.append(sql)
        result = MagicMock()
        if "information_schema.tables" in sql and "_remote_attach" in sql:
            result.fetchall.return_value = [("_remote_attach",)]
        elif "FROM" in sql and "_remote_attach" in sql:
            result.fetchall.return_value = list(rows_buffer["attach"])
        elif "duckdb_databases" in sql:
            result.fetchall.return_value = []
        else:
            result.fetchall.return_value = []
        return result

    conn.execute.side_effect = execute_side_effect

    def set_attach_rows(rows):
        rows_buffer["attach"] = rows

    return conn, sql_calls, set_attach_rows


def _attach_call_count(sql_calls: list[str]) -> int:
    return sum(1 for s in sql_calls if s.lstrip().upper().startswith("ATTACH "))


class TestOrchestratorRefusesConfigOnlySecrets:
    """End-to-end on the rebuild path: even with the real secret present in
    this process's environment, a connector ``_remote_attach`` row naming it
    as ``token_env`` must be refused BEFORE any ATTACH — not merely absent
    from a set somewhere."""

    @pytest.mark.parametrize("name", CONFIG_ONLY_SECRETS)
    def test_remote_attach_row_naming_a_config_only_secret_is_refused(self, captured_conn, monkeypatch, caplog, name):
        secret_value = f"real-{name.lower()}-pem-material"
        monkeypatch.setenv(name, secret_value)
        conn, sql_calls, set_rows = captured_conn
        set_rows([("alias1", "keboola", "https://attacker.example", name)])
        with caplog.at_level(logging.ERROR):
            SyncOrchestrator()._attach_remote_extensions(conn, "src1")
        assert _attach_call_count(sql_calls) == 0
        assert not any(secret_value in s for s in sql_calls)
        assert any("token_env" in r.message and "not in the allowlist" in r.message for r in caplog.records)


class TestOperatorOverrideCannotResurrectOtherBoundaries:
    """Defense-in-depth ratchet: AGNES_REMOTE_ATTACH_TOKEN_ENVS *replaces*
    the default inbound set (``get_allowed_token_envs``), so an operator
    listing a name that belongs to a DIFFERENT consumer class there — a
    typo, or a misguided attempt to "add" a name (the override REPLACES, it
    does not add) — must not resurrect it as a legal connector-ATTACH
    ``token_env``. Covers both other boundaries this module defines:
    ``_CONFIG_SECRET_ONLY_ENVS`` (the SharePoint certificate / Snowflake
    passphrase) and ``_PRODUCER_KEY_ENVS`` (the anonymization HMAC key)."""

    @pytest.mark.parametrize("name", CONFIG_ONLY_SECRETS)
    def test_config_only_secret_in_the_override_is_scrubbed(self, monkeypatch, name):
        from src.orchestrator_security import get_allowed_token_envs

        monkeypatch.setenv("AGNES_REMOTE_ATTACH_TOKEN_ENVS", f"{name},KBC_TOKEN")
        allowed = get_allowed_token_envs()
        assert name not in allowed
        assert "KBC_TOKEN" in allowed

    def test_producer_key_in_the_override_is_scrubbed(self, monkeypatch):
        from src.orchestrator_security import get_allowed_token_envs

        monkeypatch.setenv("AGNES_REMOTE_ATTACH_TOKEN_ENVS", "AGNES_ANONYMIZATION_HMAC_KEY,KBC_TOKEN")
        allowed = get_allowed_token_envs()
        assert "AGNES_ANONYMIZATION_HMAC_KEY" not in allowed
        assert "KBC_TOKEN" in allowed

    @pytest.mark.parametrize("name", CONFIG_ONLY_SECRETS)
    def test_remote_attach_row_naming_an_overridden_config_only_secret_is_still_refused(
        self, captured_conn, monkeypatch, caplog, name
    ):
        """End-to-end: even with the operator override explicitly naming it
        (not just the compiled-in default), a connector ``_remote_attach``
        row asking for it as ``token_env`` must be refused before ATTACH."""
        secret_value = f"real-{name.lower()}-pem-material"
        monkeypatch.setenv("AGNES_REMOTE_ATTACH_TOKEN_ENVS", name)
        monkeypatch.setenv(name, secret_value)
        conn, sql_calls, set_rows = captured_conn
        set_rows([("alias1", "keboola", "https://attacker.example", name)])
        with caplog.at_level(logging.ERROR):
            SyncOrchestrator()._attach_remote_extensions(conn, "src1")
        assert _attach_call_count(sql_calls) == 0
        assert not any(secret_value in s for s in sql_calls)


def _make_extract_with_remote_attach(path: Path, alias: str, extension: str, url: str, token_env: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = duckdb.connect(str(path))
    c.execute("CREATE TABLE _remote_attach (alias VARCHAR, extension VARCHAR, url VARCHAR, token_env VARCHAR)")
    c.execute("INSERT INTO _remote_attach VALUES (?, ?, ?, ?)", [alias, extension, url, token_env])
    c.close()


class TestQueryPathRefusesConfigOnlySecrets:
    """Same refusal on the query path (``src/db.py``) — the read-only
    re-ATTACH runs on every query request and must enforce the identical
    boundary or the rebuild-path ratchet above is hollow."""

    @pytest.mark.parametrize("name", CONFIG_ONLY_SECRETS)
    def test_remote_attach_row_naming_a_config_only_secret_is_refused(self, tmp_path, monkeypatch, caplog, name):
        from src.db import _reattach_remote_extensions

        monkeypatch.setenv(name, f"real-{name.lower()}-value")
        _make_extract_with_remote_attach(
            tmp_path / "extracts" / "src1" / "extract.duckdb",
            alias="evil",
            extension="keboola",
            url="https://attacker.example",
            token_env=name,
        )
        conn = duckdb.connect()
        conn.execute(f"ATTACH '{tmp_path / 'extracts' / 'src1' / 'extract.duckdb'}' AS src1 (READ_ONLY)")
        with caplog.at_level(logging.ERROR):
            _reattach_remote_extensions(conn, tmp_path / "extracts")
        assert any("token_env" in r.message and "not in allowlist" in r.message for r in caplog.records)
        attached = {r[0] for r in conn.execute("SELECT database_name FROM duckdb_databases()").fetchall()}
        assert "evil" not in attached
