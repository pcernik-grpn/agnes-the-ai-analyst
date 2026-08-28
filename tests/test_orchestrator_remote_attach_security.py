"""Issue #81 Group A — connector → orchestrator trust-boundary tests.

The orchestrator reads `_remote_attach` rows that connectors write into their
extract.duckdb, then runs `INSTALL`/`LOAD`/`ATTACH` SQL. This file exercises
each of the C1 hardening fixes:

- A.1 extension allowlist (refuse non-allowlisted extension)
- A.2 token-env hard allowlist (refuse well-known runtime secrets)
- A.3 URL single-quote escape (no injection through the URL literal)
- Built-in vs community install path split

Each test writes a malicious `_remote_attach` row into a fixture
extract.duckdb and asserts that the orchestrator either refuses (no ATTACH
call) or issues a safely-escaped one. We capture SQL strings via a fake
DuckDB connection so the assertions don't depend on any real extension being
installed.
"""

import logging
from unittest.mock import MagicMock

import pytest

from src.orchestrator import SyncOrchestrator
from src.orchestrator_security import escape_sql_string_literal


@pytest.fixture
def captured_conn(monkeypatch, tmp_path):
    """A duckdb.Connection-like mock that records every execute() string."""
    sql_calls: list[str] = []
    conn = MagicMock()

    # information_schema.tables → return _remote_attach exists
    # _remote_attach rows are programmed per-test via attach_rows()
    rows_buffer = {"attach": []}

    def execute_side_effect(sql, *args, **kwargs):
        sql_calls.append(sql)
        result = MagicMock()
        # information_schema query
        if "information_schema.tables" in sql and "_remote_attach" in sql:
            result.fetchall.return_value = [("_remote_attach",)]
        elif "FROM" in sql and "_remote_attach" in sql:
            result.fetchall.return_value = list(rows_buffer["attach"])
        elif "duckdb_databases" in sql:
            result.fetchall.return_value = []  # nothing attached yet
        else:
            result.fetchall.return_value = []
        return result

    conn.execute.side_effect = execute_side_effect

    def set_attach_rows(rows):
        rows_buffer["attach"] = rows

    return conn, sql_calls, set_attach_rows


def _attach_call_count(sql_calls: list[str]) -> int:
    """Number of ATTACH statements actually issued against the conn."""
    return sum(1 for s in sql_calls if s.lstrip().upper().startswith("ATTACH "))


class TestExtensionAllowlist:
    def test_refuses_unknown_extension(self, captured_conn, caplog):
        conn, sql_calls, set_rows = captured_conn
        set_rows([("alias1", "httpfs", "https://x", "")])
        with caplog.at_level(logging.ERROR):
            SyncOrchestrator()._attach_remote_extensions(conn, "src1")
        assert _attach_call_count(sql_calls) == 0
        assert any("not in the allowlist" in r.message for r in caplog.records)

    def test_allows_keboola(self, captured_conn, monkeypatch):
        monkeypatch.setenv("KBC_TOKEN", "secret-token-value")
        conn, sql_calls, set_rows = captured_conn
        set_rows([("kbc", "keboola", "https://example.keboola.com", "KBC_TOKEN")])
        SyncOrchestrator()._attach_remote_extensions(conn, "src1")
        assert _attach_call_count(sql_calls) == 1


class TestTokenEnvAllowlist:
    def test_refuses_session_secret(self, captured_conn, caplog, monkeypatch):
        # Even if SESSION_SECRET were set in env (it shouldn't be in tests),
        # the allowlist refuses to read it.
        monkeypatch.setenv("SESSION_SECRET", "super-secret-jwt-signing-key")
        conn, sql_calls, set_rows = captured_conn
        set_rows([("alias1", "keboola", "https://x", "SESSION_SECRET")])
        with caplog.at_level(logging.ERROR):
            SyncOrchestrator()._attach_remote_extensions(conn, "src1")
        assert _attach_call_count(sql_calls) == 0
        assert any("token_env" in r.message and "not in the allowlist" in r.message for r in caplog.records)

    def test_refuses_jwt_secret_key(self, captured_conn, monkeypatch, caplog):
        monkeypatch.setenv("JWT_SECRET_KEY", "x" * 64)
        conn, sql_calls, set_rows = captured_conn
        set_rows([("alias1", "keboola", "https://x", "JWT_SECRET_KEY")])
        with caplog.at_level(logging.ERROR):
            SyncOrchestrator()._attach_remote_extensions(conn, "src1")
        assert _attach_call_count(sql_calls) == 0

    def test_refuses_arbitrary_custom_token_env(self, captured_conn, monkeypatch, caplog):
        # Names that pass the structural regex but aren't on the allowlist
        # are still refused — defense against accidental exposure.
        monkeypatch.setenv("MY_RANDOM_TOKEN", "value")
        conn, sql_calls, set_rows = captured_conn
        set_rows([("alias1", "keboola", "https://x", "MY_RANDOM_TOKEN")])
        with caplog.at_level(logging.ERROR):
            SyncOrchestrator()._attach_remote_extensions(conn, "src1")
        assert _attach_call_count(sql_calls) == 0

    def test_operator_override_replaces_default(self, captured_conn, monkeypatch):
        monkeypatch.setenv("AGNES_REMOTE_ATTACH_TOKEN_ENVS", "MY_RANDOM_TOKEN")
        monkeypatch.setenv("MY_RANDOM_TOKEN", "value")
        conn, sql_calls, set_rows = captured_conn
        set_rows([("alias1", "keboola", "https://x", "MY_RANDOM_TOKEN")])
        SyncOrchestrator()._attach_remote_extensions(conn, "src1")
        assert _attach_call_count(sql_calls) == 1

    def test_empty_string_override_falls_back_to_default(self, captured_conn, monkeypatch):
        """AGNES_REMOTE_ATTACH_TOKEN_ENVS='' should NOT lock everything down —
        it falls through to the default. (Operator-typo defense.)"""
        monkeypatch.setenv("AGNES_REMOTE_ATTACH_TOKEN_ENVS", "")
        monkeypatch.setenv("KBC_TOKEN", "value")
        conn, sql_calls, set_rows = captured_conn
        set_rows([("alias1", "keboola", "https://x", "KBC_TOKEN")])
        SyncOrchestrator()._attach_remote_extensions(conn, "src1")
        assert _attach_call_count(sql_calls) == 1

    def test_empty_token_env_skips_check(self, captured_conn, monkeypatch):
        """token_env='' (the BigQuery-style env-auth path) skips the
        allowlist check entirely. Verifies the BQ flow keeps working.

        Stubs get_metadata_token because the BQ branch fetches a token
        from the GCE metadata server before ATTACH — that's unreachable
        in unit tests."""
        monkeypatch.setattr(
            "src.orchestrator.get_metadata_token",
            lambda: "fake-token",
        )
        conn, sql_calls, set_rows = captured_conn
        set_rows([("bq", "bigquery", "project=x", "")])
        SyncOrchestrator()._attach_remote_extensions(conn, "src1")
        assert _attach_call_count(sql_calls) == 1

    def test_structurally_invalid_token_env_refused(self, captured_conn, monkeypatch, caplog):
        """Even names on the allowlist via override must pass the structural
        regex `^[A-Z][A-Z0-9_]{0,63}$`. A name with a space or lowercase
        letter is refused regardless of allowlist contents."""
        # Try to add a structurally-invalid name to the allowlist via
        # override; the regex inside is_token_env_allowed must still refuse.
        monkeypatch.setenv("AGNES_REMOTE_ATTACH_TOKEN_ENVS", "kbc_token,KBC TOKEN,LEGIT_TOKEN")
        monkeypatch.setenv("LEGIT_TOKEN", "value")
        conn, sql_calls, set_rows = captured_conn
        set_rows(
            [
                ("a1", "keboola", "https://x", "kbc_token"),  # lowercase
                ("a2", "keboola", "https://x", "KBC TOKEN"),  # space
                ("a3", "keboola", "https://x", "LEGIT_TOKEN"),  # OK
            ]
        )
        SyncOrchestrator()._attach_remote_extensions(conn, "src1")
        # Only a3 should attach; a1 and a2 fail the structural regex even
        # though the operator listed them in the override.
        assert _attach_call_count(sql_calls) == 1


class TestAnonymizationKeyNeverJoinsTheAttachAllowlist:
    """RBAC review, 2026-08-28: ``AGNES_ANONYMIZATION_HMAC_KEY`` (the
    anonymize-in-front pipeline's per-instance producer key,
    ``app.worker.kinds._resolve_anonymization_key``) must NEVER become a
    legal `token_env` here — this allowlist gates a SEPARATE, inbound
    trust boundary: a connector-written `_remote_attach` row naming it as
    `token_env` would get the real key value resolved and sent as an
    `ATTACH ... TOKEN` to a connector-chosen URL (`is_attach_host_allowed`
    is default-open with no host allowlist configured). This is a RATCHET —
    it must fail if the key is ever re-added to `_DEFAULT_TOKEN_ENVS`.
    """

    def test_anonymization_key_is_not_in_the_effective_allowlist(self):
        from src.orchestrator_security import get_allowed_token_envs

        assert "AGNES_ANONYMIZATION_HMAC_KEY" not in get_allowed_token_envs()

    def test_remote_attach_row_naming_the_anonymization_key_is_refused(self, captured_conn, monkeypatch, caplog):
        """End-to-end: even if the real key happens to be set in this
        process's environment, a connector `_remote_attach` row asking for
        it as `token_env` must be refused before ATTACH, not merely absent
        from a set somewhere."""
        monkeypatch.setenv("AGNES_ANONYMIZATION_HMAC_KEY", "real-instance-key-value")
        conn, sql_calls, set_rows = captured_conn
        set_rows([("alias1", "keboola", "https://attacker.example", "AGNES_ANONYMIZATION_HMAC_KEY")])
        with caplog.at_level(logging.ERROR):
            SyncOrchestrator()._attach_remote_extensions(conn, "src1")
        assert _attach_call_count(sql_calls) == 0
        assert not any("real-instance-key-value" in s for s in sql_calls)
        assert any("token_env" in r.message and "not in the allowlist" in r.message for r in caplog.records)


class TestProducerKeyEnvAllowlist:
    """Unit coverage for ``is_producer_key_env_allowed`` — the separate,
    narrower allowlist ``_resolve_anonymization_key`` uses instead of
    ``is_token_env_allowed`` (see that function's module-level docstring
    for the trust-boundary argument)."""

    def test_default_name_is_allowed(self):
        from src.orchestrator_security import is_producer_key_env_allowed

        assert is_producer_key_env_allowed("AGNES_ANONYMIZATION_HMAC_KEY") is True

    def test_attach_allowlisted_name_is_not_producer_key_allowed(self):
        """A name legal for the connector-ATTACH boundary (e.g. the
        SharePoint certificate env) must not thereby be legal here — the
        two allowlists are deliberately disjoint."""
        from src.orchestrator_security import is_producer_key_env_allowed

        assert is_producer_key_env_allowed("SHAREPOINT_CERT_PRIVATE_KEY") is False
        assert is_producer_key_env_allowed("KBC_TOKEN") is False

    def test_arbitrary_name_is_refused(self):
        from src.orchestrator_security import is_producer_key_env_allowed

        assert is_producer_key_env_allowed("ANTHROPIC_API_KEY") is False
        assert is_producer_key_env_allowed("JWT_SECRET_KEY") is False

    def test_structurally_invalid_name_is_refused(self):
        from src.orchestrator_security import is_producer_key_env_allowed

        assert is_producer_key_env_allowed("agnes_anonymization_hmac_key") is False  # lowercase
        assert is_producer_key_env_allowed("AGNES ANONYMIZATION") is False  # space
        assert is_producer_key_env_allowed("") is False


class TestUrlEscape:
    def test_single_quote_in_url_is_escaped(self, captured_conn, monkeypatch):
        monkeypatch.setenv("KBC_TOKEN", "tok")
        conn, sql_calls, set_rows = captured_conn
        # Adversarial URL trying to break out of the literal.
        adversarial_url = "https://example.com'); DROP DATABASE x; --"
        set_rows([("kbc", "keboola", adversarial_url, "KBC_TOKEN")])
        SyncOrchestrator()._attach_remote_extensions(conn, "src1")
        attach_sqls = [s for s in sql_calls if s.lstrip().upper().startswith("ATTACH ")]
        assert len(attach_sqls) == 1
        # The double-escaped form must be present, never the bare single quote.
        expected_escaped = escape_sql_string_literal(adversarial_url)
        assert f"'{expected_escaped}'" in attach_sqls[0]
        # Sanity: the un-escaped form would have ended the literal early.
        assert f"'{adversarial_url}'" not in attach_sqls[0]

    def test_token_with_single_quote_is_escaped(self, captured_conn, monkeypatch):
        # Defense-in-depth: even if a token somehow contained `'`, the
        # ATTACH literal still parses safely.
        monkeypatch.setenv("KBC_TOKEN", "abc'def")
        conn, sql_calls, set_rows = captured_conn
        set_rows([("kbc", "keboola", "https://x", "KBC_TOKEN")])
        SyncOrchestrator()._attach_remote_extensions(conn, "src1")
        attach_sqls = [s for s in sql_calls if s.lstrip().upper().startswith("ATTACH ")]
        assert "'abc''def'" in attach_sqls[0]


class TestInstallPathSplit:
    def test_community_extension_uses_install_from_community(self, captured_conn, monkeypatch):
        monkeypatch.setenv("KBC_TOKEN", "tok")
        conn, sql_calls, set_rows = captured_conn
        set_rows([("kbc", "keboola", "https://x", "KBC_TOKEN")])
        SyncOrchestrator()._attach_remote_extensions(conn, "src1")
        install_sqls = [s for s in sql_calls if "INSTALL" in s.upper()]
        assert any("FROM community" in s for s in install_sqls)

    def test_builtin_extension_uses_load_only(self, captured_conn, monkeypatch):
        # Add a fictitious built-in via the override mechanism (we have to
        # patch the module-level set since AGNES_REMOTE_ATTACH_EXTENSIONS
        # only affects community).
        from src import orchestrator_security as oms

        monkeypatch.setattr(oms, "_BUILTIN_EXTENSIONS", frozenset({"sqlite"}))
        conn, sql_calls, set_rows = captured_conn
        set_rows([("sql1", "sqlite", "/tmp/db.sqlite", "")])
        SyncOrchestrator()._attach_remote_extensions(conn, "src1")
        install_sqls = [s for s in sql_calls if "INSTALL" in s.upper()]
        # Built-in: no INSTALL, only LOAD
        assert install_sqls == []
        load_sqls = [s for s in sql_calls if s.lstrip().upper().startswith("LOAD ")]
        assert len(load_sqls) == 1
