"""Resolve datasource credentials: env > vault > None.

Environment variables are authoritative (Terraform / secret-manager
deployments stay in control); the ``system_secrets`` vault is the
UI-managed fallback. Only the known datasource secret names are
resolvable via the vault — the allow-list prevents using the vault
namespace to read arbitrary environment variables.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

DATA_SOURCE_SECRET_NAMES = (
    "KEBOOLA_STORAGE_TOKEN",
    "BIGQUERY_SERVICE_ACCOUNT_JSON",
    "AGNES_GWS_CLIENT_ID",
    "AGNES_GWS_CLIENT_SECRET",
    "SNOWFLAKE_PASSWORD",
    "SNOWFLAKE_PRIVATE_KEY",
    "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE",
    "DATABRICKS_TOKEN",
)


def datasource_secret(name: str) -> str | None:
    """Return the value for ``name`` resolving env > vault > None.

    Raises ``ValueError`` for any name outside the datasource allow-list. A
    vault lookup failure (DB unavailable, etc.) is swallowed and treated as
    unset so the caller can fall back to the YAML config path.
    """
    if name not in DATA_SOURCE_SECRET_NAMES:
        raise ValueError(f"{name!r} is not a known datasource secret name")
    env = os.environ.get(name)
    if env:
        return env
    try:
        from src.repositories import system_secrets_repo

        return system_secrets_repo().get(name)
    except Exception:  # noqa: BLE001 - vault lookup failure is best-effort fallback
        logger.warning("vault lookup for %s failed; treating as unset", name)
        return None


def keboola_instance_token(token_env: str) -> tuple[str | None, str | None]:
    """Resolve the instance-level Keboola storage token via the same 3-step
    fallback ``_keboola_credentialed()`` (``app/web/router.py``) checks: (1)
    the literal ``token_env`` env var, (2) the literal ``KEBOOLA_STORAGE_TOKEN``
    env var, (3) this module's vault slot for that same name.

    Returns ``(value, provenance)`` with THREE distinct provenance values —
    collapsing (1) and (2) into one ``"env"`` bucket previously hid a real
    "looks credentialed but isn't" bug (see below), so callers that need to
    know WHICH env name held the value get their own value each:

    - ``"env_token_env"`` — case (1): found under the literal ``token_env``
      name passed in. A *new* connection that inherits this exact same
      ``token_env`` string will independently rediscover the value via its
      own ``_resolve_token`` (``app/api/admin_source_connections.py``) —
      process-global env vars need no seeding of their own.
    - ``"env_generic"`` — case (2): ``token_env`` itself was unset (or not
      passed), but the generic ``KEBOOLA_STORAGE_TOKEN`` name holds the
      value. A new connection that inherits ``token_env`` as some OTHER,
      custom name (e.g. an admin-configured ``data_source.keboola.token_env``)
      will NOT rediscover this on its own — its ``_resolve_token`` only ever
      checks that literal custom name, never the generic fallback — so it
      DOES need seeding, exactly like the vault case.
    - ``"vault"`` — case (3): the credential lives ONLY in this
      instance-level vault slot, which a newly created connection has no
      access to on its own.

    ``(None, None)`` when nothing is set anywhere.

    Kept as the ONE place this fallback order is implemented — every caller
    (the derived Keboola card's credentialed probe, and the "Import as
    managed connection" vault-seeding step) goes through this function so
    they can never drift apart.
    """
    if token_env:
        env_val = os.environ.get(token_env, "").strip()
        if env_val:
            return env_val, "env_token_env"
    generic_val = os.environ.get("KEBOOLA_STORAGE_TOKEN", "").strip()
    if generic_val:
        return generic_val, "env_generic"
    try:
        vault_val = (datasource_secret("KEBOOLA_STORAGE_TOKEN") or "").strip()
    except Exception:  # noqa: BLE001 - treat any lookup failure as unset
        vault_val = ""
    if vault_val:
        return vault_val, "vault"
    return None, None
