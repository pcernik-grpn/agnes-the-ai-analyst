"""External identity login (``sso`` provider slot) — runtime-configured
Entra ID OIDC (design 2026-08-28).

One additional, customer-owned external identity login per instance,
configured by an admin at runtime (``/api/admin/sso/*`` — tenant ID, client
ID, Fernet-encrypted client secret, mandatory email-domain allowlist, button
label) and stored in the PG-only ``sso_config`` singleton. Unlike the env-var
``microsoft`` provider, everything here is read LIVE from the database, so
config changes take effect on the next request with no restart.

The slot name is deliberately generic (``sso``, not ``entra``): the protocol
lives in the config row's ``provider_type`` discriminator (``'entra_oidc'``
only today; SAML may widen it later without a redesign). Exactly one external
IdP per instance — the config table is a singleton by construction.

This module deliberately does NOT duplicate the hardened pure helpers of the
``microsoft`` provider — ``tenant_id_error``, ``resolve_identity`` et al. are
imported from ``app.auth.providers.microsoft`` where needed.

Availability contract: :func:`is_available` (and :func:`is_configured`) must
NEVER let ``RequiresPostgresBackend`` escape — the provider-registry probes
treat a raising probe as "could not tell", which would suppress the registry
lockout rescue and spam warnings on every login render of a DuckDB-backed
instance. On DuckDB the answer is simply ``False``.
"""

from __future__ import annotations

import logging
from typing import Any

from src.repositories import RequiresPostgresBackend, sso_config_repo

logger = logging.getLogger(__name__)

PROVIDER_TYPE = "entra_oidc"


def _config_state() -> tuple[dict[str, Any] | None, str | None]:
    """``(config, decrypted_client_secret)`` — ``(None, None)`` when the
    app-state backend is DuckDB (PG-only repo) or nothing is configured."""
    try:
        repo = sso_config_repo()
        cfg = repo.get_config()
        if cfg is None:
            return None, None
        return cfg, repo.get_client_secret()
    except RequiresPostgresBackend:
        return None, None


def is_configured() -> bool:
    """Config-completeness: row present, non-empty domain allowlist, secret
    stored AND decryptable. Independent of the ``enabled`` flag — the admin
    test sign-in works pre-enable on exactly this predicate."""
    cfg, secret = _config_state()
    return bool(cfg and cfg["allowed_email_domains"] and secret)


def is_available() -> bool:
    """Postgres backend + configured + enabled — the login-page/registry probe."""
    cfg, secret = _config_state()
    return bool(cfg and cfg["enabled"] and cfg["allowed_email_domains"] and secret)


def startup_warnings() -> list[str]:
    """Operator-facing boot messages, emitted from ``app.main``'s lifespan.

    An enabled external login is always announced — the instance trusts a
    third party's tenant to assert identities for the permitted domains, and
    the operator should see that in the boot log. An enabled row whose secret
    no longer decrypts (rotated/malformed vault key) is a loud error: the
    button silently disappeared from the login page.
    """
    try:
        repo = sso_config_repo()
        cfg = repo.get_config()
        if cfg is None or not cfg["enabled"]:
            return []
        if repo.get_client_secret() is None:
            return [
                (
                    "External SSO sign-in is enabled but its client secret is missing or no longer "
                    "decrypts (vault key rotated?). The login button is HIDDEN until the secret is "
                    "re-set on the SSO admin panel."
                )
            ]
        return [
            (
                f"External SSO sign-in (Entra ID) is ENABLED: tenant {cfg['tenant_id']!r} may assert "
                f"identities for: {', '.join(cfg['allowed_email_domains'])}. Keep that allowlist "
                "scoped to domains the external tenant is entitled to assert."
            )
        ]
    except RequiresPostgresBackend:
        return []
