"""Resolve SharePoint connection settings from a ``source_connections`` row.

Identity (tenant + client id) is plain configuration: exact values copied from
another system, so they live on the row where an admin can see and correct
them. The certificate private key is a secret and reaches the connector one of
two ways, chosen by the admin when connecting:

* **their own** — uploaded once, held in this connection's encrypted vault
  slot (``PUT /api/admin/connections/{id}/secret``);
* **the server's** — already provisioned by the deployment and named by
  ``config.cert_private_key_env``; on a VM the startup script writes it into
  ``/opt/agnes/.env`` from Secret Manager.

Vault wins when both exist, mirroring
:func:`connectors.snowflake.settings._resolve_row_secret`, so an admin who
uploads a key overrides the deployment's without editing infrastructure.

SECURITY: ``cert_private_key_env`` is admin-writable, which makes it untrusted
the same way a request body is. Without a guard an admin could point it at an
unrelated secret in the environment (``ANTHROPIC_API_KEY``, ``JWT_SECRET_KEY``,
a database DSN) and have Agnes read that value out as "the SharePoint
credential". Every named lookup in this module therefore funnels through
:func:`src.orchestrator_security.is_token_env_allowed`, the same shared
allowlist the Snowflake and Databricks resolvers use — one choke point, so a
future consumer cannot reintroduce the hole by calling something else.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from src.orchestrator_security import is_token_env_allowed

logger = logging.getLogger(__name__)

#: Default env var holding the PEM private key, used when a connection asks for
#: the server's certificate without naming a variable. Deployments that inject
#: it under a different name set ``config.cert_private_key_env``; the name must
#: still be on the shared allowlist.
SHAREPOINT_CERT_PRIVATE_KEY_ENV = "SHAREPOINT_CERT_PRIVATE_KEY"

_REQUIRED_IDENTITY_FIELDS = ("tenant_id", "client_id")


class SharePointSettingsError(RuntimeError):
    """Connection settings are unusable — misconfigured, not a transport fault.

    Raised instead of returning ``None`` so the failure names what an admin has
    to fix. The message never carries a resolved secret value.
    """


@dataclass(frozen=True)
class SharePointSettings:
    """Everything the crawler needs to authenticate, and where it came from."""

    tenant_id: str
    client_id: str
    #: The PEM material for Entra's certificate-credential flow: the
    #: X.509 certificate followed by its private key, concatenated in one
    #: PEM blob (what an admin generates for an app registration's
    #: certificate credential, and what ``connectors.sharepoint.graph_client``
    #: parses to sign a client assertion — the certificate for the JWT's
    #: ``x5t`` thumbprint, the private key to sign it). This module treats it
    #: as opaque secret material; it does not itself parse or validate the PEM.
    private_key: str
    #: ``"vault"`` (the admin's own key) or ``"env"`` (the deployment's).
    #: Surfaced on the connection page so an admin can see which certificate is
    #: in use without the value ever being displayed.
    credential_source: str
    #: The env var consulted, or ``None`` when the key came from the vault.
    #: Kept for the same reason: the page shows the name, never the value.
    credential_env: Optional[str] = None
    #: WHEN a vault-provided certificate was last set/rotated (``None`` for
    #: an env-sourced one — an env var carries no timestamp of its own, and
    #: none is invented). The source card's certificate row (spec §13.2)
    #: shows this alongside ``credential_source``, never the value.
    credential_set_at: Optional[datetime] = None


def _vault_secret(connection_id: str) -> Optional[str]:
    """This connection's own encrypted secret slot, or ``None``.

    Imported lazily and tolerant of an unconfigured vault: an instance with no
    ``AGNES_VAULT_KEY`` can still run a connection whose certificate comes from
    the environment, and should not fail resolution on the way past.
    """
    try:
        from src.repositories import connection_secrets_repo

        return connection_secrets_repo().get(connection_id) or None
    except Exception:  # pragma: no cover — no vault configured, or no such row
        logger.debug("sharepoint: no vault secret for connection %s", connection_id, exc_info=True)
        return None


def _vault_secret_updated_at(connection_id: str) -> Optional[datetime]:
    """When ``_vault_secret``'s row was last set/rotated, or ``None``.

    Deliberately a SEPARATE lookup rather than folding this into
    ``_vault_secret`` — the plaintext getter's contract (a bare string or
    ``None``) stays unchanged for every other caller, and this one never
    touches ``ciphertext``. Same tolerant-of-no-vault posture: any failure
    here degrades to "set-date unknown", never a resolution failure — the
    card would rather show a blank set-date than hide a working credential.
    """
    try:
        from src.repositories import connection_secrets_repo

        raw = connection_secrets_repo().updated_at(connection_id)
        if raw is None or isinstance(raw, datetime):
            return raw
        # Both backends return ``str(row[0])`` — "2026-08-20 12:00:00[.ffffff]"
        # — while this field is a datetime its one consumer calls
        # ``.isoformat()`` on. Parse here rather than widen the repo contract,
        # which every other caller reads as a display string.
        return datetime.fromisoformat(str(raw))
    except Exception:  # no vault configured, no such row, or an unreadable stamp
        logger.debug("sharepoint: no vault set-date for connection %s", connection_id, exc_info=True)
        return None


def _env_secret(env_name: str) -> str:
    """Read a named env var, refusing any name outside the shared allowlist."""
    if not is_token_env_allowed(env_name):
        raise SharePointSettingsError(
            f"cert_private_key_env={env_name!r} is not an allowed credential variable. "
            "Add it to AGNES_REMOTE_ATTACH_TOKEN_ENVS if the deployment really injects "
            "the SharePoint certificate under that name, or upload the certificate to "
            "the connection instead."
        )

    value = os.environ.get(env_name)
    if not value:
        raise SharePointSettingsError(
            f"{env_name} is not set on the server, so the SharePoint certificate cannot "
            "be read. On a VM this variable is written into /opt/agnes/.env by the "
            "startup script — a running instance only picks up a new one after a "
            "recreate. Upload the certificate to the connection to avoid the dependency."
        )
    return value


def resolve_sharepoint_settings(connection: Dict[str, Any]) -> SharePointSettings:
    """Resolve one SharePoint ``source_connections`` row into usable settings.

    Order: identity fields first (a typo there is the likeliest mistake and
    costs nothing to report), then the credential — vault, then the named
    environment variable.
    """
    config = connection.get("config") or {}

    missing = [field for field in _REQUIRED_IDENTITY_FIELDS if not str(config.get(field) or "").strip()]
    if missing:
        raise SharePointSettingsError("SharePoint connection is missing required field(s): " + ", ".join(missing))

    connection_id = connection.get("id") or ""
    vault_value = _vault_secret(connection_id)
    if vault_value:
        return SharePointSettings(
            tenant_id=str(config["tenant_id"]).strip(),
            client_id=str(config["client_id"]).strip(),
            private_key=vault_value,
            credential_source="vault",
            credential_set_at=_vault_secret_updated_at(connection_id),
        )

    env_name = str(config.get("cert_private_key_env") or "").strip() or SHAREPOINT_CERT_PRIVATE_KEY_ENV
    return SharePointSettings(
        tenant_id=str(config["tenant_id"]).strip(),
        client_id=str(config["client_id"]).strip(),
        private_key=_env_secret(env_name),
        credential_source="env",
        credential_env=env_name,
    )
