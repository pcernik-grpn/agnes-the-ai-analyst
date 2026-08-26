"""Tests for app/secrets_vault.py — Fernet-backed shared MCP source secrets.

Covers the encryption helpers + the SharedSecretsRepository round-trip,
including the env-var-key path and the ephemeral-fallback path.
"""
from __future__ import annotations

import duckdb
import pytest
from cryptography.fernet import Fernet

from app.secrets_vault import (
    SharedSecretsRepository,
    VaultKeyState,
    _reset_ephemeral_key_for_tests,
    decrypt_secret,
    encrypt_secret,
    vault_key_configured,
    vault_key_state,
)


@pytest.fixture(autouse=True)
def _reset_ephemeral():
    _reset_ephemeral_key_for_tests()
    yield
    _reset_ephemeral_key_for_tests()


def _conn_with_vault_table():
    conn = duckdb.connect(":memory:")
    conn.execute(
        """CREATE TABLE mcp_secrets (
              source_id        VARCHAR PRIMARY KEY,
              secret_value_enc BLOB NOT NULL,
              created_at       TIMESTAMP NOT NULL DEFAULT current_timestamp,
              updated_at       TIMESTAMP NOT NULL DEFAULT current_timestamp
           )"""
    )
    return conn


# ── cipher helpers ────────────────────────────────────────────────────────


def test_encrypt_decrypt_round_trip_with_env_key(monkeypatch):
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    token = encrypt_secret("hunter2")
    assert isinstance(token, bytes)
    assert decrypt_secret(token) == "hunter2"


def test_encrypt_decrypt_round_trip_with_ephemeral_key(monkeypatch):
    # The ephemeral key is now a LOCAL_DEV_MODE-only convenience — storing a
    # secret with no AGNES_VAULT_KEY outside local dev is refused (it would be
    # lost on restart). Under LOCAL_DEV_MODE the ephemeral round-trip still works.
    monkeypatch.delenv("AGNES_VAULT_KEY", raising=False)
    monkeypatch.setenv("LOCAL_DEV_MODE", "1")
    import app.secrets_vault as _v
    _v._reset_ephemeral_key_for_tests()
    token = encrypt_secret("hunter2")
    assert decrypt_secret(token) == "hunter2"


def test_invalid_env_key_raises(monkeypatch):
    monkeypatch.setenv("AGNES_VAULT_KEY", "not-a-valid-fernet-key")
    with pytest.raises(RuntimeError, match="not a valid Fernet key"):
        encrypt_secret("x")


# ── vault_key_state() tri-state read ─────────────────────────────────────


def test_vault_key_state_absent_when_unset(monkeypatch):
    monkeypatch.delenv("AGNES_VAULT_KEY", raising=False)
    assert vault_key_state() is VaultKeyState.ABSENT
    assert vault_key_configured() is False


def test_vault_key_state_valid_for_real_fernet_key(monkeypatch):
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    assert vault_key_state() is VaultKeyState.VALID
    assert vault_key_configured() is True


def test_vault_key_state_invalid_for_malformed_key(monkeypatch):
    """A set-but-malformed key must read as INVALID, not fold into ABSENT —
    that conflation is what let a misconfigured production vault silently
    downgrade to plaintext overlay storage (see app.secrets.persist_overlay_token)."""
    monkeypatch.setenv("AGNES_VAULT_KEY", "not-a-valid-fernet-key")
    assert vault_key_state() is VaultKeyState.INVALID
    # vault_key_configured() stays a narrow "usable right now" boolean —
    # both ABSENT and INVALID answer False, but callers that must react
    # differently to a misconfiguration use vault_key_state() directly.
    assert vault_key_configured() is False


# ── SharedSecretsRepository ───────────────────────────────────────────────


def test_repo_upsert_get_round_trip(monkeypatch):
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    conn = _conn_with_vault_table()
    repo = SharedSecretsRepository(conn)
    assert repo.get("src_test") is None
    repo.upsert("src_test", "topsecret-123")
    assert repo.has("src_test") is True
    assert repo.get("src_test") == "topsecret-123"


def test_repo_upsert_replaces_prior(monkeypatch):
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    conn = _conn_with_vault_table()
    repo = SharedSecretsRepository(conn)
    repo.upsert("src_test", "first")
    repo.upsert("src_test", "second")
    assert repo.get("src_test") == "second"


def test_repo_delete_removes_row(monkeypatch):
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    conn = _conn_with_vault_table()
    repo = SharedSecretsRepository(conn)
    repo.upsert("src_test", "x")
    repo.delete("src_test")
    assert repo.get("src_test") is None
    assert repo.has("src_test") is False


def test_repo_returns_none_on_decrypt_failure_after_key_rotation(monkeypatch):
    """Junk-decrypt returns None so callers can fall back to the env-var path."""
    # Encrypt under key A
    key_a = Fernet.generate_key().decode()
    monkeypatch.setenv("AGNES_VAULT_KEY", key_a)
    conn = _conn_with_vault_table()
    repo = SharedSecretsRepository(conn)
    repo.upsert("src_test", "value-encrypted-with-A")

    # Rotate to key B and clear the ephemeral cache; row is unreadable
    _reset_ephemeral_key_for_tests()
    key_b = Fernet.generate_key().decode()
    monkeypatch.setenv("AGNES_VAULT_KEY", key_b)

    assert repo.get("src_test") is None


def test_malformed_vault_key_reads_as_absent_not_a_crash(monkeypatch):
    """A bad AGNES_VAULT_KEY must degrade to "no secret", not raise.

    ``_get_fernet()`` raises ``RuntimeError`` (not ``InvalidToken``) when the
    env var is set to something that is not a valid Fernet key. That is
    process-wide and fires on the FIRST read of any secret, so a read path
    that lets it escape turns one typo into a 500 on every request touching a
    secret. Only ``SystemSecretsRepository`` used to catch it.
    """
    key_a = Fernet.generate_key().decode()
    monkeypatch.setenv("AGNES_VAULT_KEY", key_a)
    conn = _conn_with_vault_table()
    repo = SharedSecretsRepository(conn)
    repo.upsert("src_test", "value")

    _reset_ephemeral_key_for_tests()
    monkeypatch.setenv("AGNES_VAULT_KEY", "not-a-fernet-key")

    assert repo.get("src_test") is None
    # `has` is a row-existence check and must stay truthful — the row IS there,
    # it just cannot be read. Only the value degrades.
    assert repo.has("src_test") is True


def test_decrypt_optional_swallows_both_failure_modes(monkeypatch):
    """The helper the OAuth repos share promises "never raises" — for both."""
    from app.secrets_vault import decrypt_optional

    key_a = Fernet.generate_key().decode()
    monkeypatch.setenv("AGNES_VAULT_KEY", key_a)
    token = encrypt_secret("refresh-token")
    assert decrypt_optional(token) == "refresh-token"

    # (1) rotated key -> InvalidToken
    _reset_ephemeral_key_for_tests()
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    assert decrypt_optional(token) is None

    # (2) malformed key -> RuntimeError out of _get_fernet()
    _reset_ephemeral_key_for_tests()
    monkeypatch.setenv("AGNES_VAULT_KEY", "@@ not base64 @@")
    assert decrypt_optional(token) is None
