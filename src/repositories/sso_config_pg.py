"""Postgres-only repository for the ``sso_config`` singleton (design 2026-08-28).

Runtime config for the external identity login (``sso`` provider slot):
tenant/client IDs, the Fernet-encrypted client secret, the mandatory
email-domain allowlist, button label, and the enable flag.

Write-only secret discipline: ``get_config()`` never selects
``client_secret_enc`` — the plaintext is reachable only through
``get_client_secret()``, and an undecryptable value (rotated/malformed vault
key) reads as *unset* via ``app.secrets_vault.decrypt_optional`` so the
provider degrades to unavailable instead of 500-ing.

PG-first ratchet (A3): brand-new app-state surface added after the freeze,
so there is no DuckDB sibling — see ``docs/migrations.md`` -> "Adding a
PG-only feature". Reach this repo only through
``src.repositories.sso_config_repo()``; on a DuckDB-backed instance that
factory call raises ``RequiresPostgresBackend`` (translated to a ``501`` by
the app-wide handler).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.secrets_vault import decrypt_optional, encrypt_secret

_SINGLETON_ID = "default"


def normalize_domains(domains: Iterable[str]) -> list[str]:
    """Lowercase, strip, dedup (order-preserving) and drop empty entries."""
    seen: list[str] = []
    for raw in domains:
        domain = raw.strip().lower()
        if domain and domain not in seen:
            seen.append(domain)
    return seen


class SsoConfigPgRepository:
    """Singleton ``sso_config`` row (``id`` CHECK-pinned to ``'default'``)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get_config(self) -> dict[str, Any] | None:
        """The config row WITHOUT the secret column, or ``None`` when unset.

        ``allowed_email_domains`` is returned as a list; ``has_client_secret``
        reflects ciphertext presence (not decryptability — the login-time
        read is ``get_client_secret()``).
        """
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text(
                        "SELECT provider_type, tenant_id, client_id, display_name, "
                        "       allowed_email_domains, enabled, "
                        "       (client_secret_enc IS NOT NULL) AS has_client_secret, "
                        "       created_at, updated_at, updated_by "
                        "FROM sso_config WHERE id = :id"
                    ),
                    {"id": _SINGLETON_ID},
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        out = dict(row)
        out["allowed_email_domains"] = normalize_domains(out["allowed_email_domains"].split(","))
        return out

    def upsert_config(
        self,
        *,
        tenant_id: str,
        client_id: str,
        display_name: str,
        allowed_email_domains: Iterable[str],
        enabled: bool,
        updated_by: str | None,
        provider_type: str = "entra_oidc",
    ) -> None:
        """Insert or update the singleton row. Never touches the stored secret."""
        domains = ",".join(normalize_domains(allowed_email_domains))
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO sso_config "
                    "(id, provider_type, tenant_id, client_id, display_name, "
                    " allowed_email_domains, enabled, updated_by) "
                    "VALUES (:id, :provider_type, :tenant_id, :client_id, :display_name, "
                    "        :domains, :enabled, :updated_by) "
                    "ON CONFLICT (id) DO UPDATE SET "
                    "provider_type = EXCLUDED.provider_type, "
                    "tenant_id = EXCLUDED.tenant_id, "
                    "client_id = EXCLUDED.client_id, "
                    "display_name = EXCLUDED.display_name, "
                    "allowed_email_domains = EXCLUDED.allowed_email_domains, "
                    "enabled = EXCLUDED.enabled, "
                    "updated_at = CURRENT_TIMESTAMP, "
                    "updated_by = EXCLUDED.updated_by"
                ),
                {
                    "id": _SINGLETON_ID,
                    "provider_type": provider_type,
                    "tenant_id": tenant_id,
                    "client_id": client_id,
                    "display_name": display_name,
                    "domains": domains,
                    "enabled": enabled,
                    "updated_by": updated_by,
                },
            )

    def set_client_secret(self, value: str) -> bool:
        """Encrypt and store the client secret on the existing config row.

        Returns ``False`` when no config row exists yet (the API answers 404
        — a secret column has nowhere to live before the config is saved).
        Raises ``VaultKeyNotConfiguredError`` without a vault key (the API's
        409 contract).
        """
        ciphertext = encrypt_secret(value).decode()
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text("UPDATE sso_config SET client_secret_enc = :ct, updated_at = CURRENT_TIMESTAMP WHERE id = :id"),
                {"ct": ciphertext, "id": _SINGLETON_ID},
            )
        return result.rowcount > 0

    def get_client_secret(self) -> str | None:
        """Decrypted client secret, or ``None`` when absent or undecryptable."""
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT client_secret_enc FROM sso_config WHERE id = :id"),
                {"id": _SINGLETON_ID},
            ).first()
        if row is None or row[0] is None:
            return None
        # TEXT column (a Fernet token is URL-safe base64) — encode before
        # handing to decrypt_optional, which only accepts bytes-like input.
        return decrypt_optional(
            row[0].encode(),
            context="sso_config.client_secret_enc",
            hint="SSO provider will be unavailable until the secret is re-set.",
        )

    def clear_client_secret(self) -> bool:
        """Drop the stored secret. ``False`` when no config row exists."""
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text(
                    "UPDATE sso_config SET client_secret_enc = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = :id"
                ),
                {"id": _SINGLETON_ID},
            )
        return result.rowcount > 0

    def set_enabled(self, enabled: bool, *, updated_by: str | None) -> bool:
        """Flip the enable flag. ``False`` when no config row exists."""
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text(
                    "UPDATE sso_config SET enabled = :enabled, "
                    "updated_at = CURRENT_TIMESTAMP, updated_by = :updated_by "
                    "WHERE id = :id"
                ),
                {"enabled": enabled, "updated_by": updated_by, "id": _SINGLETON_ID},
            )
        return result.rowcount > 0

    def delete_config(self) -> bool:
        """Delete the config row (secret column goes with it — atomic).

        Identity rows in ``user_external_identities`` are deliberately kept:
        they are historically true links, inert unless the same tenant is
        configured again (design: "Config delete keeps identity rows").
        """
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text("DELETE FROM sso_config WHERE id = :id"),
                {"id": _SINGLETON_ID},
            )
        return result.rowcount > 0
