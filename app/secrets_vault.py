"""Symmetric secret vault for Universal MCP sources (RFC #461 §4).

Fernet (AES-128-CBC + HMAC-SHA256) under the hood — bundled with the
``cryptography`` package which Agnes already depends on. The vault key
is a 32-byte URL-safe-base64 string read from ``AGNES_VAULT_KEY``.

POC vs. production
------------------
Production deployments MUST set ``AGNES_VAULT_KEY`` to a stable value
(rotated through a side-channel like ``agnes admin vault rotate-key``,
not part of this slice). When it is unset, the vault generates an
ephemeral key on first use, logs a WARNING that secrets won't survive
a restart, and continues — fine for first-boot development, fatal for
real deployments. The next-iteration tweak is to refuse boot in
production-channel images when the env var is missing.

Scope
-----
Two scopes share the same encryption helpers but live in different
tables:

* ``mcp_secrets``         — server-wide, one row per ``source_id``.
                            Replaces the legacy ``auth_secret_env``
                            env-var pattern for HTTP/SSE sources.
* ``mcp_user_secrets``    — per-user, one row per ``(source_id, user_id)``.
                            Powers RFC §4 user-credential passthrough
                            (each analyst's own Notion/Slack/Linear
                            OAuth token). Lands in Phase 4b.

This module hosts only the cipher helpers + the shared-scope repository
helpers; the per-user table arrives with its own migration in Phase 4b.
"""

from __future__ import annotations

import enum
import logging
import os
from typing import Optional

import duckdb
from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_ENV_KEY_NAME = "AGNES_VAULT_KEY"
_ephemeral_key: Optional[bytes] = None


class VaultKeyNotConfiguredError(RuntimeError):
    """Raised when a secret WRITE is attempted in a non-local-dev process
    that has no AGNES_VAULT_KEY set — storing under the ephemeral key would
    silently lose the secret on restart."""


class VaultKeyInvalidError(RuntimeError):
    """Raised when AGNES_VAULT_KEY is set but is NOT a valid Fernet key, on a
    write path whose only fallback would be silently downgrading to plaintext
    storage (e.g. ``app.secrets.persist_overlay_token``'s keyless-mode
    ``.env_overlay`` file).

    Deliberately distinct from :class:`VaultKeyNotConfiguredError` (key
    ABSENT — a legitimate keyless/local-dev choice): a malformed key is a
    misconfiguration, not an intentional mode, so it must fail loudly rather
    than fold into the same "no key" fallback."""


class VaultKeyState(enum.Enum):
    """Tri-state read of ``$AGNES_VAULT_KEY``.

    Conflating ABSENT and INVALID (both used to answer ``False`` from a
    single boolean predicate) is what let a misconfigured production vault
    key be treated the same as a deliberately keyless deployment — see
    :func:`vault_key_configured` for the narrower boolean callers that only
    need "can I use the vault right now" still use, and
    ``app.secrets.persist_overlay_token`` for the write path that needs the
    full distinction to fail closed instead of downgrading to plaintext.
    """

    ABSENT = "absent"
    VALID = "valid"
    INVALID = "invalid"


def _is_local_dev_mode() -> bool:
    # Mirror app.auth.dependencies.is_local_dev_mode without importing it
    # (keeps app.secrets_vault free of an app.auth import edge).
    return os.environ.get("LOCAL_DEV_MODE", "").strip().lower() in ("1", "true", "yes")


def vault_key_state() -> VaultKeyState:
    """Tri-state read of ``$AGNES_VAULT_KEY`` — see :class:`VaultKeyState`."""
    raw = os.environ.get(_ENV_KEY_NAME, "").strip()
    if not raw:
        return VaultKeyState.ABSENT
    try:
        Fernet(raw.encode("ascii"))
        return VaultKeyState.VALID
    except (ValueError, InvalidToken):
        return VaultKeyState.INVALID


def vault_key_configured() -> bool:
    """True iff AGNES_VAULT_KEY is set to a syntactically valid Fernet key.

    A narrow "is the vault usable right now" boolean — both the ABSENT and
    INVALID states of :func:`vault_key_state` answer ``False`` here, which is
    fine for callers that only gate "should I use the vault vs. some other
    read-only fallback" (health checks, admin UI badges). A write path that
    would otherwise silently downgrade to plaintext on ``False`` MUST use
    :func:`vault_key_state` directly instead, to refuse the INVALID case
    rather than treating it like ABSENT.
    """
    return vault_key_state() is VaultKeyState.VALID


def can_store_secrets() -> bool:
    """True iff :func:`encrypt_secret` will accept a write right now.

    Exactly the predicate ``encrypt_secret`` guards on — a configured key, OR
    local-dev mode where the ephemeral fallback is deliberately allowed. Callers
    that want to reject a doomed write BEFORE doing something irreversible must
    use this rather than :func:`vault_key_configured`, which is the narrower
    "is a real key configured" question and answers ``False`` in local dev where
    the write would in fact succeed (Devin Review on #1124).
    """
    return vault_key_configured() or _is_local_dev_mode()


def _get_fernet() -> Fernet:
    """Return a Fernet instance built from ``$AGNES_VAULT_KEY``, or an
    ephemeral key when the env var is absent.

    The ephemeral fallback is held in a module-level variable so multiple
    calls within the same process return the same key (otherwise every
    encrypt/decrypt would land in a different cipher). The fallback is
    explicitly **not safe** for production — operators see a WARNING on
    first use.
    """
    global _ephemeral_key
    raw = os.environ.get(_ENV_KEY_NAME, "").strip()
    if raw:
        try:
            return Fernet(raw.encode("ascii"))
        except (ValueError, InvalidToken) as exc:
            raise RuntimeError(
                f"{_ENV_KEY_NAME!r} is set but is not a valid Fernet key "
                f"(URL-safe-base64-encoded 32-byte key required): {exc}"
            ) from exc

    if _ephemeral_key is None:
        _ephemeral_key = Fernet.generate_key()
        logger.warning(
            "%s is not set — using an ephemeral vault key. Secrets stored "
            "now WILL be unrecoverable after restart. Set %s to a stable "
            "URL-safe-base64 32-byte key for production deployments.",
            _ENV_KEY_NAME,
            _ENV_KEY_NAME,
        )
    return Fernet(_ephemeral_key)


def encrypt_secret(value: str) -> bytes:
    """Encrypt ``value`` and return ciphertext bytes (Fernet token).

    Refuses to encrypt under the ephemeral key outside LOCAL_DEV_MODE — a
    secret stored that way is lost on restart, so we fail loudly instead.
    Only the *unset* case is guarded here; a key that is set-but-invalid
    falls through to ``_get_fernet()`` which raises a clearer config error.
    """
    key_unset = not os.environ.get(_ENV_KEY_NAME, "").strip()
    if key_unset and not _is_local_dev_mode():
        raise VaultKeyNotConfiguredError(
            f"{_ENV_KEY_NAME} must be set before storing secrets — otherwise they are unrecoverable after restart."
        )
    return _get_fernet().encrypt(value.encode("utf-8"))


def decrypt_secret(token: bytes) -> str:
    """Decrypt a Fernet token previously produced by ``encrypt_secret``.

    Raises ``InvalidToken`` if the ciphertext doesn't authenticate — that
    almost always means the vault key was rotated since the value was
    written, OR the writer used the ephemeral fallback and the process
    restarted. We let it bubble so the caller can decide whether to fall
    back to the env-var lookup path.
    """
    return _get_fernet().decrypt(token).decode("utf-8")


def decrypt_optional(token: object, *, context: str = "secret", hint: str = "") -> Optional[str]:
    """Decrypt a possibly-``None``/possibly-junk Fernet token column.

    Shared by every repo that stores an encrypted secret column (client
    secrets, refresh tokens, PKCE verifiers, the shared / per-user /
    connection secrets, and the system secrets) — factors out the
    bytes-coercion + decrypt-failure-swallow logic that used to be
    hand-duplicated in each. Returns ``None`` for a NULL column or an
    unreadable value — never raises.

    "Unreadable" covers BOTH failure modes, not just the obvious one:

    * ``InvalidToken`` — the ciphertext doesn't authenticate under the
      current key (rotated key, or a value written under the ephemeral
      fallback before a restart). Per-value.
    * ``RuntimeError`` — ``AGNES_VAULT_KEY`` is set but is not a valid
      Fernet key (:func:`_get_fernet`). Process-wide: it fires on the FIRST
      read, for every value, so letting it escape turns a typo in one env
      var into a 500 on every request that touches a secret instead of the
      actionable "not connected / not configured" the callers already
      handle.

    That second catch is what makes the fold total. ``SystemSecretsRepository``
    was the one repo excluded from it, on the grounds that its wider catch did
    not belong in a shared path — but the shared path now has it, so the
    exception lost its reason and the repo joins the rest.

    ``context`` identifies the column in the WARNING log (convention:
    ``"<table>.<column>[<key>]"``); ``hint`` appends the caller's
    degradation note ("Falling back to env-var lookup.") so an operator
    reading the log knows what happens next.

    Only accepts bytes-like input — callers whose column is TEXT (Fernet
    tokens are URL-safe base64, so a text column is legitimate) encode
    before calling.
    """
    if token is None:
        return None
    if not isinstance(token, (bytes, bytearray, memoryview)):
        return None
    try:
        return decrypt_secret(bytes(token))
    except (InvalidToken, RuntimeError):
        logger.warning(
            "%s failed to decrypt — vault key rotated or malformed?%s",
            context,
            f" {hint}" if hint else "",
        )
        return None


def _reset_ephemeral_key_for_tests() -> None:
    """Test-only: reset the module-level ephemeral key so each test that
    relies on the unset-env-var fallback starts from a fresh state."""
    global _ephemeral_key
    _ephemeral_key = None


# ---------------------------------------------------------------------------
# Repository — shared (server-wide) source secrets (mcp_secrets table)
# ---------------------------------------------------------------------------


class SharedSecretsRepository:
    """Server-wide MCP source secrets. One row per ``source_id``."""

    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def upsert(self, source_id: str, value: str) -> None:
        """Store the encrypted secret for ``source_id``. Replaces any prior row."""
        token = encrypt_secret(value)
        self.conn.execute(
            """INSERT INTO mcp_secrets (source_id, secret_value_enc, updated_at)
               VALUES (?, ?, current_timestamp)
               ON CONFLICT (source_id) DO UPDATE SET
                 secret_value_enc = excluded.secret_value_enc,
                 updated_at       = excluded.updated_at""",
            [source_id, token],
        )

    def get(self, source_id: str) -> Optional[str]:
        """Return the decrypted secret for ``source_id`` or ``None`` when
        no row exists OR the row decrypts to junk (key rotation, etc.).

        Junk-decrypt returns ``None`` so the caller can fall back to the
        legacy env-var lookup; raising would break that compatibility.
        """
        row = self.conn.execute(
            "SELECT secret_value_enc FROM mcp_secrets WHERE source_id = ?",
            [source_id],
        ).fetchone()
        if row is None:
            return None
        return decrypt_optional(
            row[0],
            context=f"mcp_secrets.secret_value_enc[{source_id}]",
            hint="Falling back to env-var lookup.",
        )

    def delete(self, source_id: str) -> None:
        self.conn.execute("DELETE FROM mcp_secrets WHERE source_id = ?", [source_id])

    def has(self, source_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM mcp_secrets WHERE source_id = ? LIMIT 1",
            [source_id],
        ).fetchone()
        return row is not None

    def get_updated_at(self, source_id: str) -> Optional[str]:
        """ISO-8601 timestamp of the last upsert for ``source_id``, or
        ``None`` when no secret is stored. Never returns the secret value.

        Mirrors ``PerUserSecretsRepository.get_updated_at`` — same shape,
        one fewer key column (the shared vault has one row per source, not
        per ``(source, user)``). Powers the "last rotated" label on the
        admin vault-secret card.
        """
        row = self.conn.execute(
            "SELECT updated_at FROM mcp_secrets WHERE source_id = ?",
            [source_id],
        ).fetchone()
        return row[0].isoformat() if row and row[0] is not None else None


# ---------------------------------------------------------------------------
# Repository — server-wide system secrets (system_secrets table)
# ---------------------------------------------------------------------------


class SystemSecretsRepository:
    """Server-wide system secrets keyed by ``name`` (Slack bot tokens)."""

    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def upsert(self, name: str, value: str) -> None:
        """Store the encrypted secret for ``name``. Replaces any prior row."""
        token = encrypt_secret(value)
        self.conn.execute(
            """INSERT INTO system_secrets (name, secret_value_enc, updated_at)
               VALUES (?, ?, current_timestamp)
               ON CONFLICT (name) DO UPDATE SET
                 secret_value_enc = excluded.secret_value_enc,
                 updated_at       = excluded.updated_at""",
            [name, token],
        )

    def get(self, name: str) -> Optional[str]:
        """Decrypted secret for ``name`` or ``None`` when absent or
        undecryptable. Catches both ``InvalidToken`` (vault key rotated) and
        ``RuntimeError`` (``AGNES_VAULT_KEY`` set-but-malformed) so a bad key
        fails closed (feature disabled) instead of 500-ing every Slack request."""
        row = self.conn.execute(
            "SELECT secret_value_enc FROM system_secrets WHERE name = ?",
            [name],
        ).fetchone()
        if row is None:
            return None
        return decrypt_optional(
            row[0],
            context=f"system_secrets.secret_value_enc[{name}]",
            hint="Treating as unset.",
        )

    def delete(self, name: str) -> None:
        self.conn.execute("DELETE FROM system_secrets WHERE name = ?", [name])

    def has(self, name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM system_secrets WHERE name = ? LIMIT 1",
            [name],
        ).fetchone()
        return row is not None

    def list_names_with_prefix(self, prefix: str) -> list[str]:
        """Names of every row whose ``name`` starts with ``prefix``, sorted.

        Powers the ``env_overlay/`` namespace used by
        ``app.secrets.persist_overlay_token`` (wave 2C task 6) — the caller
        needs to discover which env vars are vault-managed without knowing
        the full set up front (it's admin-driven: marketplace PATs, the
        Anthropic chat key, the initial-workspace template PAT). Uses
        ``position(... IN ...)`` rather than ``LIKE`` so the prefix is
        matched literally — ``LIKE`` would need ``%``/``_`` escaping since
        ``env_overlay/`` itself contains an underscore. Does not decrypt.
        """
        rows = self.conn.execute(
            "SELECT name FROM system_secrets WHERE position(? IN name) = 1 ORDER BY name",
            [prefix],
        ).fetchall()
        return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Repository — per-user source secrets (mcp_user_secrets table)
# ---------------------------------------------------------------------------


class PerUserSecretsRepository:
    """Per-user MCP source secrets. One row per ``(source_id, user_id)``.

    Used when ``mcp_sources.scope = 'per_user'`` — calls forward through
    the upstream MCP under the analyst's own identity (their Notion /
    Slack / Linear OAuth token), not a shared server-wide secret.
    """

    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def upsert(self, source_id: str, user_id: str, value: str) -> None:
        token = encrypt_secret(value)
        self.conn.execute(
            """INSERT INTO mcp_user_secrets (source_id, user_id, secret_value_enc, updated_at)
               VALUES (?, ?, ?, current_timestamp)
               ON CONFLICT (source_id, user_id) DO UPDATE SET
                 secret_value_enc = excluded.secret_value_enc,
                 updated_at       = excluded.updated_at""",
            [source_id, user_id, token],
        )

    def get(self, source_id: str, user_id: str) -> Optional[str]:
        """Decrypted secret for ``(source_id, user_id)`` or ``None`` if
        absent or undecryptable (key rotation). Junk decrypt returns
        ``None`` so the caller can fall back to the shared path."""
        row = self.conn.execute(
            "SELECT secret_value_enc FROM mcp_user_secrets WHERE source_id = ? AND user_id = ?",
            [source_id, user_id],
        ).fetchone()
        if row is None:
            return None
        return decrypt_optional(
            row[0],
            context=f"mcp_user_secrets.secret_value_enc[{source_id}/{user_id}]",
            hint="Falling back to shared vault / env-var.",
        )

    def delete(self, source_id: str, user_id: str) -> None:
        self.conn.execute(
            "DELETE FROM mcp_user_secrets WHERE source_id = ? AND user_id = ?",
            [source_id, user_id],
        )

    def has(self, source_id: str, user_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM mcp_user_secrets WHERE source_id = ? AND user_id = ? LIMIT 1",
            [source_id, user_id],
        ).fetchone()
        return row is not None

    def get_updated_at(self, source_id: str, user_id: str) -> Optional[str]:
        """ISO-8601 timestamp of the last upsert for ``(source_id, user_id)``,
        or ``None`` when not connected. Never returns the secret value."""
        row = self.conn.execute(
            "SELECT updated_at FROM mcp_user_secrets WHERE source_id = ? AND user_id = ?",
            [source_id, user_id],
        ).fetchone()
        return row[0].isoformat() if row and row[0] is not None else None

    def list_for_source(self, source_id: str) -> list[str]:
        """List of user_ids that have stored a secret for this source.

        Powers the admin diagnostic ``agnes admin mcp source who-has-secret``
        without leaking any cipher text — secret values are never returned.
        """
        rows = self.conn.execute(
            "SELECT user_id FROM mcp_user_secrets WHERE source_id = ? ORDER BY user_id",
            [source_id],
        ).fetchall()
        return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Repository — source connection secrets (connection_secrets table)
# ---------------------------------------------------------------------------


class ConnectionSecretsRepository:
    """Vault scope for source_connections tokens (spec 2026-06-12 §3.1).

    Same write-only contract as ``SharedSecretsRepository``: API layers
    must only expose ``has()``; ``get()`` is for connector-side resolution.
    """

    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def upsert(self, connection_id: str, value: str) -> None:
        ct = encrypt_secret(value).decode()
        self.conn.execute(
            """INSERT INTO connection_secrets (connection_id, ciphertext, updated_at)
               VALUES (?, ?, current_timestamp)
               ON CONFLICT (connection_id) DO UPDATE SET
                   ciphertext = excluded.ciphertext,
                   updated_at = excluded.updated_at""",
            [connection_id, ct],
        )

    def get(self, connection_id: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT ciphertext FROM connection_secrets WHERE connection_id = ?",
            [connection_id],
        ).fetchone()
        if not row:
            return None
        token = row[0]
        # The str→bytes coercion stays at the callsite: ``ciphertext`` is a TEXT
        # column (a Fernet token is URL-safe base64), so DuckDB returns str,
        # which ``decrypt_optional`` treats as "not a token" and would read as
        # absent. Matches the PG sibling.
        if isinstance(token, str):
            token = token.encode()
        return decrypt_optional(token, context=f"connection_secrets.ciphertext[{connection_id}]")

    def delete(self, connection_id: str) -> None:
        self.conn.execute("DELETE FROM connection_secrets WHERE connection_id = ?", [connection_id])

    def has(self, connection_id: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM connection_secrets WHERE connection_id = ?", [connection_id]).fetchone()
        return row is not None

    def updated_at(self, connection_id: str) -> Optional[str]:
        """When this connection's vault secret was last set/rotated, or
        ``None`` if no row exists. Never touches ``ciphertext`` — a set-date
        badge (the SharePoint wizard's / source card's certificate row, spec
        §13.2) can show "set 2026-08-14" without ever reading ``get()``."""
        row = self.conn.execute(
            "SELECT updated_at FROM connection_secrets WHERE connection_id = ?",
            [connection_id],
        ).fetchone()
        return str(row[0]) if row and row[0] is not None else None
