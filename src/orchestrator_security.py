"""Allowlists and policy for the connector → orchestrator trust boundary.

The orchestrator reads `_remote_attach` rows that connectors write into their
`extract.duckdb`, then calls `INSTALL`, `LOAD`, and `ATTACH` based on those
values. Treating the connector as adversarial (compromised image, supply-chain,
malicious fork) means the orchestrator picks **what** can be installed and
**which** env vars can be referenced — not the connector.
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

# DuckDB extensions the orchestrator is willing to load on behalf of a
# connector. Built-in extensions go in `_BUILTIN_EXTENSIONS`; community
# extensions go in `_COMMUNITY_EXTENSIONS`. The two sets are disjoint and
# tell the install path whether to issue `INSTALL ... FROM community` or
# only `LOAD`.
_BUILTIN_EXTENSIONS: frozenset[str] = frozenset()  # none in current OSS
_COMMUNITY_EXTENSIONS: frozenset[str] = frozenset(
    {
        "keboola",
        "bigquery",
        # Unity Catalog ATTACH for Databricks `query_mode='remote'` rows.
        # `uc_catalog` reads table data through `delta`, so both must be
        # loadable or the ATTACH resolves nothing. Allowlisted, not enabled:
        # an extract only carries a `uc_catalog` _remote_attach row when the
        # operator set `data_source.databricks.attach_enabled`, so on every
        # other instance these two names are simply never requested.
        "uc_catalog",
        "delta",
        "snowflake",
    }
)

# Env vars whose values may be passed as the auth `TOKEN` in `ATTACH`. The
# default is intentionally tight — every name in the runtime env that is not
# on this list cannot be exfiltrated to a connector-controlled URL.
# Operators add deployment-specific names via AGNES_REMOTE_ATTACH_TOKEN_ENVS.
_DEFAULT_TOKEN_ENVS: frozenset[str] = frozenset(
    {
        "KBC_TOKEN",
        "KBC_STORAGE_TOKEN",
        "KEBOOLA_STORAGE_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",  # path, not a secret value
        "DATABRICKS_TOKEN",  # workspace PAT for the Unity Catalog ATTACH
        "SNOWFLAKE_PASSWORD",  # Snowflake user password for the snowflake extension ATTACH
        "SNOWFLAKE_PRIVATE_KEY",  # Snowflake key-pair private key (may contain passphrase JSON)
        # Decrypts SNOWFLAKE_PRIVATE_KEY locally before the ATTACH — never sent
        # as the TOKEN itself, but resolved by the same name-selected lookup
        # (connectors.snowflake.settings._resolve_secret) as the two names
        # above, so it needs the same allowlist membership or the module's
        # own default key-pair passphrase path breaks for every deploy that
        # relies on it (RBAC review second round, 2026-08-26).
        "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE",
    }
)

# Names must additionally match this regex (defense against weird input).
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

# Security audit F10/F11: the `url` in a connector-written `_remote_attach` row
# is untrusted. The extension + token_env allowlists constrain WHICH secret is
# sent but not WHERE — a malicious connector can point `url` at
# `https://attacker.example` and the orchestrator would ship the allowlisted
# credential there via `ATTACH ... TOKEN '<real token>'`. Operators pin the set
# of hosts a credential may be sent to via this env var (CSV of host[:port]).
_ATTACH_HOST_ALLOWLIST_ENV = "AGNES_REMOTE_ATTACH_HOST_ALLOWLIST"


def _parse_csv_env(name: str) -> set[str]:
    """Parse a comma-separated env var into a stripped set of non-empty tokens."""
    raw = os.environ.get(name, "")
    return {t.strip() for t in raw.split(",") if t.strip()}


def get_allowed_extensions() -> dict[str, set[str]]:
    """Return the effective extension allowlist as a dict of {kind: set}.

    `kind` is "builtin" or "community" — the install path needs to know
    which to use. Operator override AGNES_REMOTE_ATTACH_EXTENSIONS replaces
    the default community set; built-ins are not configurable from env (a
    typo there would silently disable a working integration with no clear
    failure mode, and built-ins do not pose a supply-chain risk).
    """
    override = _parse_csv_env("AGNES_REMOTE_ATTACH_EXTENSIONS")
    community = override if override else set(_COMMUNITY_EXTENSIONS)
    return {"builtin": set(_BUILTIN_EXTENSIONS), "community": community}


def is_extension_allowed(extension: str) -> bool:
    allow = get_allowed_extensions()
    return extension in allow["builtin"] or extension in allow["community"]


def is_builtin_extension(extension: str) -> bool:
    return extension in get_allowed_extensions()["builtin"]


def get_allowed_token_envs() -> set[str]:
    """Return the effective token-env allowlist.

    Operator override AGNES_REMOTE_ATTACH_TOKEN_ENVS *replaces* the default
    set (so an operator can shrink it as well as expand it). The startup
    code logs the effective set so a typo is visible.
    """
    override = _parse_csv_env("AGNES_REMOTE_ATTACH_TOKEN_ENVS")
    return override if override else set(_DEFAULT_TOKEN_ENVS)


def is_token_env_allowed(token_env: str) -> bool:
    """Return True if ``token_env`` may be read and passed as a TOKEN.

    Two checks: structural (`^[A-Z][A-Z0-9_]{0,63}$`) and membership in the
    allowlist. The structural check refuses things that aren't a valid env
    var name regardless of allowlist contents.
    """
    if not isinstance(token_env, str) or not _ENV_NAME_RE.match(token_env):
        return False
    return token_env in get_allowed_token_envs()


def log_effective_policy() -> None:
    """Log the effective extension + token-env allowlists at INFO once.

    Called from app startup. Makes operator typos visible — if
    AGNES_REMOTE_ATTACH_EXTENSIONS=httpfs is set with the intent to ADD
    httpfs (but the override REPLACES the default), the operator sees
    'effective extension allowlist: {httpfs}' and notices keboola and
    bigquery are missing. Idempotent — safe to call multiple times.
    """
    ext = get_allowed_extensions()
    envs = get_allowed_token_envs()
    has_ext_override = bool(_parse_csv_env("AGNES_REMOTE_ATTACH_EXTENSIONS"))
    has_env_override = bool(_parse_csv_env("AGNES_REMOTE_ATTACH_TOKEN_ENVS"))
    logger.info(
        "remote_attach policy: extensions=%s (override=%s), token_envs=%s (override=%s). "
        "Note: env-var overrides REPLACE the default — set both yours and the "
        "defaults if you want to add to them.",
        sorted(ext["community"] | ext["builtin"]),
        has_ext_override,
        sorted(envs),
        has_env_override,
    )


def attach_host_allowlist_configured() -> bool:
    """True iff an operator has configured the ATTACH host allowlist."""
    return bool(_parse_csv_env(_ATTACH_HOST_ALLOWLIST_ENV))


def _url_host(url: str) -> str:
    """Return the lowercase host[:port] of ``url``, or "" if unparseable."""
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url if "://" in url else f"//{url}", scheme="")
    except Exception:
        return ""
    if not parsed.hostname:
        return ""
    host = parsed.hostname.lower()
    return f"{host}:{parsed.port}" if parsed.port else host


def is_attach_host_allowed(url: str) -> bool:
    """Return True if a credential may be paired into an ``ATTACH`` for ``url``.

    Security audit F10/F11. When ``AGNES_REMOTE_ATTACH_HOST_ALLOWLIST`` is set,
    the URL's host (with or without an explicit ``:port``) must be a member —
    otherwise the orchestrator refuses to send the secret, closing the
    credential-exfiltration-to-attacker-host hole. When the env var is UNSET the
    function returns True for backward compatibility, but callers log a warning
    (the risk is visible and operators are steered to configure the allowlist).

    Fail-closed on an unparseable host is DELIBERATE: with an allowlist set, if
    we cannot extract a host we cannot prove the credential is going somewhere
    approved, so we refuse rather than send it blind. The credentialed ATTACH
    branch that calls this only handles ``token_env``-based secrets — in the
    shipped connectors that is the Keboola Storage URL, a standard
    ``https://connection.<region>.gcp.keboola.com`` that parses cleanly (BigQuery
    uses the metadata path with ``token_env=''`` and never reaches here). A
    connector whose credentialed ``url`` is a non-URL connection string would be
    refused only when an operator has opted into strict host pinning; that is the
    correct trade-off for a security control (and the caller logs the refused
    url so the operator can act).
    """
    allow = {h.lower() for h in _parse_csv_env(_ATTACH_HOST_ALLOWLIST_ENV)}
    if not allow:
        return True
    host = _url_host(url)
    if not host:
        return False
    # Accept a bare-host allowlist entry matching a host[:port] url and vice versa.
    bare = host.split(":", 1)[0]
    return host in allow or bare in allow


def escape_sql_string_literal(value: str) -> str:
    """Double single-quotes for safe use inside DuckDB single-quoted literals.

    Mirrors `src/db.py:_attach_extracts` (line ~411) so the read-only query
    path and the orchestrator rebuild path use the same escape.
    """
    return value.replace("'", "''")


def resolve_remote_attach_token(token_env: str) -> str:
    """Resolve a remote-attach token from env, falling back to the vault.

    Environment variables remain authoritative. When the env var is unset,
    the named secret may have been stored through the admin UI's datasource
    secrets endpoint, so we fall back to ``app.datasource_secrets`` for
    allow-listed datasource secret names.
    """
    if not token_env:
        return ""
    value = os.environ.get(token_env, "")
    if value:
        return value
    try:
        from app.datasource_secrets import datasource_secret

        return datasource_secret(token_env) or ""
    except Exception:
        logger.debug("vault lookup failed for remote-attach token_env %s", token_env, exc_info=True)
        return ""
