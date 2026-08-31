"""Allowlists and policy for the connector → orchestrator trust boundary.

The orchestrator reads `_remote_attach` rows that connectors write into their
`extract.duckdb`, then calls `INSTALL`, `LOAD`, and `ATTACH` based on those
values. Treating the connector as adversarial (compromised image, supply-chain,
malicious fork) means the orchestrator picks **what** can be installed and
**which** env vars can be referenced — not the connector.

A second allowlist lives here because its consumers share the structural
checks, but it gates a DIFFERENT consumer class and must never share
membership with the ATTACH set by accident: `_CONFIG_SECRET_ONLY_ENVS` /
`is_config_secret_env_allowed` — outbound config-driven secret resolution
(settings resolvers reading an env-var NAME from admin-writable connection
config). See that set's comment for the trust-boundary argument.
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
#
# DATA-SOURCE ATTACH TOKENS ONLY. A secret that is resolved by name from
# admin-writable config but never travels as an `ATTACH ... TOKEN` belongs in
# `_CONFIG_SECRET_ONLY_ENVS` below, NOT here — membership here makes a name a
# legal `token_env` on a connector-written `_remote_attach` row, i.e.
# exfiltratable to a connector-chosen host.
_DEFAULT_TOKEN_ENVS: frozenset[str] = frozenset(
    {
        "KBC_TOKEN",
        "KBC_STORAGE_TOKEN",
        "KEBOOLA_STORAGE_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",  # path, not a secret value
        "DATABRICKS_TOKEN",  # workspace PAT for the Unity Catalog ATTACH
        "SNOWFLAKE_PASSWORD",  # Snowflake user password for the snowflake extension ATTACH
        "SNOWFLAKE_PRIVATE_KEY",  # Snowflake key-pair private key (may contain passphrase JSON)
    }
)

# Env var names resolvable ONLY through config-driven secret resolution —
# the settings resolvers (connectors/{sharepoint,snowflake}/settings.py,
# connectors/{databricks,keboola}/semantic_layer.py,
# app/api/admin_source_connections.py) where the NAME sits in admin-writable
# connection config and the value goes to that source's own client.
#
# Deliberately NOT part of `_DEFAULT_TOKEN_ENVS` above, and this set must
# NEVER be merged into it. `_DEFAULT_TOKEN_ENVS` feeds `get_allowed_token_
# envs()` / `is_token_env_allowed()`, which gate a SECOND, unrelated
# consumer: `token_env` on a connector-written `_remote_attach` row
# (src/orchestrator.py, src/db.py) — a value a CONNECTOR chooses, resolved
# and sent as an `ATTACH ... TOKEN` to a connector-chosen `url`
# (`is_attach_host_allowed` is default-open with no
# `AGNES_REMOTE_ATTACH_HOST_ALLOWLIST` configured). Listing a name here AND
# there would let a malicious/compromised connector's extract.duckdb declare
# a `_remote_attach` row with e.g. `token_env=SHAREPOINT_CERT_PRIVATE_KEY`
# and have the orchestrator exfiltrate the certificate private key to an
# attacker-controlled host on every query. Same consumer-class split the
# anonymization HMAC key got in PR #1715 (`_PRODUCER_KEY_ENVS`); ratchet:
# tests/test_config_secret_env_security.py.
#
# Operators add deployment-specific names via AGNES_CONFIG_SECRET_ENVS
# (REPLACES this set, same semantics as the other overrides). Data-source
# tokens that legitimately serve BOTH classes (KBC_*/SNOWFLAKE_PASSWORD/...)
# stay in `_DEFAULT_TOKEN_ENVS`; `get_allowed_config_secret_envs()` unions
# them in, so they are never listed twice.
_CONFIG_SECRET_ONLY_ENVS: frozenset[str] = frozenset(
    {
        # PEM private key for a SharePoint app registration's certificate —
        # Microsoft Graph refuses client secrets for app-only access, so the
        # certificate IS the credential (connectors.sharepoint.settings).
        "SHAREPOINT_CERT_PRIVATE_KEY",
        # Decrypts SNOWFLAKE_PRIVATE_KEY locally before the ATTACH — never
        # sent as the TOKEN itself (connectors.snowflake.settings).
        "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE",
    }
)

# Env var NAME for the anonymize-in-front pipeline's per-instance HMAC key
# (design spec §9.2 — PERSON_<hmac(key, ...)> etc., never a fixed marker),
# resolved by ``app.worker.kinds._resolve_anonymization_key`` and forwarded
# ONLY to the external producer subprocess's child environment.
#
# Deliberately NOT part of `_DEFAULT_TOKEN_ENVS` above, and this set must
# NEVER be merged into it. `_DEFAULT_TOKEN_ENVS` feeds `get_allowed_token_
# envs()` / `is_token_env_allowed()`, which gate a SECOND, unrelated
# consumer: `token_env` on a connector-written `_remote_attach` row
# (src/orchestrator.py, src/db.py) — a value a connector chooses, resolved
# and sent as an `ATTACH ... TOKEN` to a connector-chosen `url`
# (`is_attach_host_allowed` is default-open with no `AGNES_REMOTE_ATTACH_
# HOST_ALLOWLIST` configured). Listing this key there would let a
# malicious/compromised connector's extract.duckdb declare a
# `_remote_attach` row with `token_env=AGNES_ANONYMIZATION_HMAC_KEY` and
# have the orchestrator exfiltrate the real key value to an
# attacker-controlled host. Keeping this a separate, narrower allowlist
# means the outbound producer-key resolution and the inbound
# connector-ATTACH trust boundary can never share membership by accident.
_PRODUCER_KEY_ENVS: frozenset[str] = frozenset({"AGNES_ANONYMIZATION_HMAC_KEY"})

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
    """Return the effective token-env allowlist — the INBOUND connector-ATTACH
    boundary (``is_token_env_allowed`` / ``src/orchestrator.py`` /
    ``src/db.py``).

    Operator override AGNES_REMOTE_ATTACH_TOKEN_ENVS *replaces* the default
    set (so an operator can shrink it as well as expand it). The startup
    code logs the effective set so a typo is visible.

    Defense-in-depth: every :data:`_CONFIG_SECRET_ONLY_ENVS` and
    :data:`_PRODUCER_KEY_ENVS` member is ALWAYS subtracted back out, even
    from the override. Neither is ever a legitimate ``_remote_attach.
    token_env`` value, so an operator listing one in
    ``AGNES_REMOTE_ATTACH_TOKEN_ENVS`` (typo, or a misguided attempt to "add"
    a name to the effective set — the override REPLACES, it does not add)
    must not resurrect the exact exfiltration path the consumer-class split
    exists to close.
    """
    override = _parse_csv_env("AGNES_REMOTE_ATTACH_TOKEN_ENVS")
    base = override if override else set(_DEFAULT_TOKEN_ENVS)
    other_boundaries = _CONFIG_SECRET_ONLY_ENVS | _PRODUCER_KEY_ENVS
    blocked = base & other_boundaries
    if blocked:
        logger.warning(
            "remote_attach: ignoring name(s) %s from the effective token-env "
            "allowlist — these belong to a different consumer class "
            "(config-resolution-only secret or the anonymization producer "
            "key) and can never legitimately be a connector _remote_attach "
            "token_env",
            sorted(blocked),
        )
    return base - other_boundaries


def is_token_env_allowed(token_env: str) -> bool:
    """Return True if ``token_env`` may be read and passed as a TOKEN.

    Two checks: structural (`^[A-Z][A-Z0-9_]{0,63}$`) and membership in the
    allowlist. The structural check refuses things that aren't a valid env
    var name regardless of allowlist contents.
    """
    if not isinstance(token_env, str) or not _ENV_NAME_RE.match(token_env):
        return False
    return token_env in get_allowed_token_envs()


def get_allowed_config_secret_envs() -> set[str]:
    """Return the effective config-resolution secret allowlist.

    The union of the config-only names (AGNES_CONFIG_SECRET_ENVS override, or
    :data:`_CONFIG_SECRET_ONLY_ENVS`) and the effective connector-ATTACH set
    (:func:`get_allowed_token_envs`, override and all) — the settings
    resolvers legitimately read the same data-source tokens the ATTACH
    boundary allows. The union is one-directional by construction: nothing
    here ever feeds back into :func:`get_allowed_token_envs`.
    """
    override = _parse_csv_env("AGNES_CONFIG_SECRET_ENVS")
    config_only = override if override else set(_CONFIG_SECRET_ONLY_ENVS)
    return config_only | get_allowed_token_envs()


def is_config_secret_env_allowed(name: str) -> bool:
    """Return True if ``name`` may be resolved by a config-driven settings
    resolver (an env-var NAME taken from admin-writable connection config /
    instance yaml, value handed to that source's own client).

    Same two checks as :func:`is_token_env_allowed` (structural regex, then
    membership) but against :func:`get_allowed_config_secret_envs`. Do not
    use this function for the connector-ATTACH ``token_env`` gate — that
    boundary must stay on :func:`is_token_env_allowed`, which can never see
    the config-only secrets; see :data:`_CONFIG_SECRET_ONLY_ENVS` for why.
    """
    if not isinstance(name, str) or not _ENV_NAME_RE.match(name):
        return False
    return name in get_allowed_config_secret_envs()


def is_producer_key_env_allowed(name: str) -> bool:
    """Return True if ``name`` may be read and forwarded to an EXTERNAL
    producer subprocess's child environment (currently: the anonymize-in-
    front pipeline's HMAC key, ``app.worker.kinds._resolve_anonymization_
    key``).

    Same two checks as :func:`is_token_env_allowed` (structural regex, then
    membership) but against :data:`_PRODUCER_KEY_ENVS` — a SEPARATE, narrower
    set from :data:`_DEFAULT_TOKEN_ENVS`. Do not use this function for the
    connector-ATTACH `token_env` gate, and do not use `is_token_env_allowed`
    for producer-key resolution — see `_PRODUCER_KEY_ENVS`'s docstring for
    why the two must never share membership.
    """
    if not isinstance(name, str) or not _ENV_NAME_RE.match(name):
        return False
    return name in _PRODUCER_KEY_ENVS


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
    config_envs = get_allowed_config_secret_envs()
    has_ext_override = bool(_parse_csv_env("AGNES_REMOTE_ATTACH_EXTENSIONS"))
    has_env_override = bool(_parse_csv_env("AGNES_REMOTE_ATTACH_TOKEN_ENVS"))
    has_config_override = bool(_parse_csv_env("AGNES_CONFIG_SECRET_ENVS"))
    logger.info(
        "remote_attach policy: extensions=%s (override=%s), token_envs=%s (override=%s), "
        "config_secret_envs=%s (override=%s). "
        "Note: env-var overrides REPLACE the default — set both yours and the "
        "defaults if you want to add to them.",
        sorted(ext["community"] | ext["builtin"]),
        has_ext_override,
        sorted(envs),
        has_env_override,
        sorted(config_envs),
        has_config_override,
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


# ── Semantic-layer git sources ───────────────────────────────────────────
#
# A THIRD consumer class, and deliberately its own pair of allowlists.
#
# A `kind='git'` semantic source carries an admin-writable `config.repo_url`
# and `config.token_env`; `src/semantic/transports.py` reads that env var and
# hands the value to `src.marketplace._run_git`, whose credential helper is
# scoped to whatever host the URL names. Unguarded, two API calls (create a
# source pointing at an attacker host with `token_env=<any server secret>`,
# then sync it) exfiltrate that secret — the same shape as the connector
# `_remote_attach` hole F10/F11 closed, through a different door. The eight
# MCP tools added in #1707 put that door within reach of an agent, which is
# what made it urgent, but the hole was reachable over REST and the CLI all
# along and the fix belongs at the transport, not at any one caller.
#
# Why NOT reuse `_ATTACH_HOST_ALLOWLIST_ENV` / `_DEFAULT_TOKEN_ENVS`: this
# module's central discipline is that consumer classes never share membership
# by accident (see `_CONFIG_SECRET_ONLY_ENVS`, `_PRODUCER_KEY_ENVS`). An
# operator's ATTACH allowlist means "DuckDB endpoints a connector may ship a
# token to"; folding git remotes into it would silently authorize clones to
# those hosts AND break every git source on an instance that pinned them. A
# Keboola storage token or a Databricks PAT is likewise never a git
# credential, so the ATTACH token set is subtracted out below rather than
# reused.
_SEMANTIC_GIT_HOST_ALLOWLIST_ENV = "AGNES_SEMANTIC_GIT_HOST_ALLOWLIST"

#: Env vars whose value may be handed to git as a clone credential. Unlike
#: the host allowlist, this one is default-CLOSED: naming which secret may be
#: read costs an operator one env var, and defaulting it open is exactly the
#: finding. Conventional git-credential names only — anything else is a
#: deployment's own and goes in the override.
#:
#: Operators REPLACE this set via AGNES_SEMANTIC_GIT_TOKEN_ENVS (same
#: semantics as every other override here — it does not add).
_DEFAULT_SEMANTIC_GIT_TOKEN_ENVS: frozenset[str] = frozenset(
    {
        "AGNES_SEMANTIC_GIT_TOKEN",
        "GIT_TOKEN",
        "GITHUB_TOKEN",
        "GITLAB_TOKEN",
        "BITBUCKET_TOKEN",
    }
)

#: URL schemes a semantic git source may name. `ext::` is the reason this is
#: an allowlist and not a denylist: git's ext transport runs its argument as a
#: shell command, so `repo_url` would be remote code execution rather than
#: mere egress. `file://` is refused too — a clone of a local path reads the
#: server's own filesystem into a document set.
_SEMANTIC_GIT_SCHEMES: frozenset[str] = frozenset({"https", "ssh", "git+ssh"})


def semantic_git_host_allowlist_configured() -> bool:
    """True iff an operator has pinned the hosts semantic git sources may reach."""
    return bool(_parse_csv_env(_SEMANTIC_GIT_HOST_ALLOWLIST_ENV))


def is_semantic_git_host_allowed(url: str) -> bool:
    """Return True if a semantic source may clone — with a credential — from ``url``.

    Sibling of :func:`is_attach_host_allowed`, same mechanism and same
    default-open-with-a-warning contract (an instance that has configured
    nothing keeps working, and the caller logs that it is unpinned), against
    its own env var. Fail-closed on an unparseable host once an allowlist IS
    set, for the same reason: we cannot prove the credential is going
    somewhere approved.
    """
    allow = {h.lower() for h in _parse_csv_env(_SEMANTIC_GIT_HOST_ALLOWLIST_ENV)}
    if not allow:
        return True
    host = _url_host(url)
    if not host:
        return False
    bare = host.split(":", 1)[0]
    return host in allow or bare in allow


def get_allowed_semantic_git_token_envs() -> set[str]:
    """Return the effective semantic-git credential env allowlist.

    Override AGNES_SEMANTIC_GIT_TOKEN_ENVS *replaces*
    :data:`_DEFAULT_SEMANTIC_GIT_TOKEN_ENVS`. Every other boundary's names are
    always subtracted back out, override included: an ATTACH data-source
    token, a config-resolution-only secret and the anonymization producer key
    are none of them git credentials, and letting one through here would
    rebuild the cross-class exfiltration path the splits exist to prevent.
    """
    override = _parse_csv_env("AGNES_SEMANTIC_GIT_TOKEN_ENVS")
    base = override if override else set(_DEFAULT_SEMANTIC_GIT_TOKEN_ENVS)
    other_boundaries = set(_DEFAULT_TOKEN_ENVS) | _CONFIG_SECRET_ONLY_ENVS | _PRODUCER_KEY_ENVS
    blocked = base & other_boundaries
    if blocked:
        logger.warning(
            "semantic git: ignoring name(s) %s from the effective credential allowlist — "
            "these belong to another consumer class (connector ATTACH token, config-resolution "
            "secret, or the anonymization producer key) and are never a git credential",
            sorted(blocked),
        )
    return base - other_boundaries


def is_semantic_git_token_env_allowed(name: str) -> bool:
    """Return True if ``name`` may be read and handed to git as a credential.

    Same two checks as :func:`is_token_env_allowed` — structural regex, then
    membership — against :func:`get_allowed_semantic_git_token_envs`.
    """
    if not isinstance(name, str) or not _ENV_NAME_RE.match(name):
        return False
    return name in get_allowed_semantic_git_token_envs()


def semantic_git_scheme_refusal(url: str) -> str:
    """Why ``url`` is not a usable semantic-source repository URL, or "".

    Separate from the host allowlist because it is not operator-configurable:
    no deployment has a legitimate `ext::`-transport semantic source, and that
    scheme is command execution rather than a fetch.
    """
    from urllib.parse import urlparse

    if not isinstance(url, str) or not url.strip():
        return "config.repo_url is required for a git semantic source"
    raw = url.strip()
    try:
        scheme = urlparse(raw).scheme.lower()
    except Exception:  # noqa: BLE001 — an unparseable URL is a refusal, not a crash
        return f"config.repo_url {raw!r} is not a parseable URL"
    if not scheme:
        # `git@host:org/repo` — scp-like syntax, no scheme, and git treats it
        # as SSH. Accepted, but only in that exact shape; anything else with
        # no scheme is a local path.
        if re.match(r"^[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+:", raw):
            return ""
        return (
            f"config.repo_url {raw!r} names no URL scheme — a semantic git source must be "
            f"one of: {', '.join(sorted(_SEMANTIC_GIT_SCHEMES))} (or git@host:org/repo)"
        )
    if scheme not in _SEMANTIC_GIT_SCHEMES:
        return (
            f"config.repo_url scheme {scheme!r} is not allowed for a semantic git source "
            f"(allowed: {', '.join(sorted(_SEMANTIC_GIT_SCHEMES))}). Schemes like 'ext' run a "
            "command rather than fetching a repository, and 'file' would read the server's "
            "own filesystem."
        )
    return ""


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
