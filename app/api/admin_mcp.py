"""Admin REST API for Universal MCP — sources + tool registry + grants (RFC #461 M5).

Surface (all gated by ``Depends(require_admin)``):

  - ``POST   /api/admin/mcp-sources``                                — create / register
  - ``GET    /api/admin/mcp-sources``                                — list (?enabled_only=)
  - ``GET    /api/admin/mcp-sources/{source_id}``                    — detail (includes tools)
  - ``PUT    /api/admin/mcp-sources/{source_id}``                    — patch (partial)
  - ``DELETE /api/admin/mcp-sources/{source_id}``                    — cascade tools + grants

  - ``POST   /api/admin/mcp-sources/{source_id}/introspect``         — discover tools live
  - ``POST   /api/admin/mcp-sources/{source_id}/classify``           — introspect + heuristic
  - ``POST   /api/admin/mcp-sources/{source_id}/test``               — connectivity check
  - ``POST   /api/admin/mcp-sources/{source_id}/materialize``        — run extractor

  - ``POST   /api/admin/mcp-tools``                                  — register tool row
  - ``GET    /api/admin/mcp-tools``                                  — list (?source_id=)
  - ``GET    /api/admin/mcp-tools/{tool_id}``                        — detail
  - ``PUT    /api/admin/mcp-tools/{tool_id}``                        — patch (partial)
  - ``DELETE /api/admin/mcp-tools/{tool_id}``                        — drop + grants
  - ``POST   /api/admin/mcp-tools/{tool_id}/grants``                 — add group grant
  - ``DELETE /api/admin/mcp-tools/{tool_id}/grants/{group_id}``      — revoke

The ``mcp-tools`` prefix (rather than ``tools``) avoids collision with any
future generic-tool admin surface. Every mutation writes an ``audit_log``
row mirroring the ``data_packages`` admin router pattern.

POC scope: no vault, no policy engine, no PII redaction. Plain CRUD plus
the four connector helpers (introspect/classify/test/materialize).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict, List, Optional

import duckdb
from sqlalchemy import exc as sa_exc
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator, model_validator

from app.auth.access import require_admin
from app.auth.dependencies import _get_db
from src.identifier_validation import is_safe_identifier

from src.repositories.mcp_source_oauth_clients import KEEP_STORED
from app.secrets_vault import (
    VaultKeyNotConfiguredError,
    can_store_secrets,
)
from connectors.mcp import classifier as mcp_classifier

# ``connectors.mcp.extractor`` (pulls in pandas) and ``connectors.mcp.client``
# (pulls in the full ``mcp`` SDK) are import-time heavy — ~280ms / ~190ms
# respectively — and only ever touched inside the four connector-action
# handlers below (introspect/classify/test/materialize + OAuth register), so
# they're imported locally at each call site instead of here. Both are always
# imported as the MODULE (dotted access, e.g. ``mcp_extractor.introspect_
# source_async(...)``), never as unpacked names, so this is exactly as
# monkeypatch-friendly as the former top-level import — a test that does
# ``from connectors.mcp import extractor as mcp_extractor; monkeypatch.
# setattr(mcp_extractor, "_materialize_one_tool", ...)`` still patches the
# same cached module object ``sys.modules`` hands back here.
from src.repositories import (
    audit_repo,
    mcp_sources_repo,
    per_user_secrets_repo,
    shared_secrets_repo,
    tool_registry_repo,
)
from src.repositories.mcp_sources import (  # noqa: F401  # MCPSourceRepository kept for type-only imports + tests that monkeypatch the symbol
    MCPSourceRepository,
    validate_oauth_scope_coupling,
    validate_source_fields,
)
from src.repositories.tool_registry import (
    MATERIALIZE,
    PASSTHROUGH,
    ToolRegistryRepository,  # noqa: F401  # kept for type-only imports + tests that monkeypatch the symbol
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin-mcp"])


# ---------------------------------------------------------------------------
# Constants + small validators
# ---------------------------------------------------------------------------

_VALID_TRANSPORTS = ("stdio", "http", "sse")
_VALID_MODES = (MATERIALIZE, PASSTHROUGH)


def _validate_transport(v: str) -> str:
    v = (v or "").strip().lower()
    if v not in _VALID_TRANSPORTS:
        raise ValueError(f"transport must be one of {list(_VALID_TRANSPORTS)}")
    return v


def _validate_mode(v: str) -> str:
    v = (v or "").strip().lower()
    if v not in _VALID_MODES:
        raise ValueError(f"mode must be one of {list(_VALID_MODES)}")
    return v


# ---------------------------------------------------------------------------
# Request / response models — sources
# ---------------------------------------------------------------------------


_VALID_SCOPES = ("shared", "per_user")


def _validate_scope(v: Optional[str]) -> str:
    if v is None:
        return "shared"
    v = (v or "").strip().lower()
    if v not in _VALID_SCOPES:
        raise ValueError(f"scope must be one of {list(_VALID_SCOPES)}")
    return v


def _validate_auth_method(v: Optional[str]) -> Optional[str]:
    """Normalize ``auth_method`` at the API boundary, like ``transport`` and
    ``scope`` already are.

    Without this the column stored whatever the admin typed, and only SOME
    readers normalized: a pasted ``"oauth "`` passed the coupling validator
    and the repository guard (both strip), was persisted with the space, and
    then read as NOT-oauth by ``update_mcp_source``'s flip check — which
    purged every user's tokens and the client registration as if the admin
    had deliberately turned OAuth off. ``_require_oauth_source`` then refused
    to re-register the source, so a trailing space silently disconnected
    everyone and bricked the source (invariant sweep on #1124).

    Normalizing here means the column can only ever hold the canonical form,
    which keeps every downstream ``.lower()`` honest.
    """
    if v is None:
        return None
    return v.strip().lower() or None


#: Re-exported so the request models below 400 on a bad combo before the repo
#: ever sees it. The rule itself lives with the repository that enforces it —
#: it was a second copy here until it drifted (Devin Review on #1124).
_validate_oauth_scope_coupling = validate_oauth_scope_coupling


class CreateMCPSourceRequest(BaseModel):
    name: str
    transport: str
    command: Optional[str] = None
    args: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None
    url: Optional[str] = None
    auth_method: Optional[str] = None
    auth_secret_env: Optional[str] = None
    enabled: bool = True
    scope: Optional[str] = None  # 'shared' (default) | 'per_user'
    connect_hint: Optional[str] = None

    @field_validator("transport")
    @classmethod
    def _check_transport(cls, v: str) -> str:
        return _validate_transport(v)

    @field_validator("scope")
    @classmethod
    def _check_scope(cls, v: Optional[str]) -> Optional[str]:
        return _validate_scope(v) if v is not None else None

    @field_validator("auth_method")
    @classmethod
    def _check_auth_method(cls, v: Optional[str]) -> Optional[str]:
        return _validate_auth_method(v)

    @model_validator(mode="after")
    def _check_oauth_scope_coupling(self) -> "CreateMCPSourceRequest":
        _validate_oauth_scope_coupling(self.auth_method, self.transport, self.scope or "shared")
        return self


class UpdateMCPSourceRequest(BaseModel):
    """Partial update — all fields optional. Omitted = leave unchanged.

    Because the underlying repository uses ``INSERT … ON CONFLICT DO UPDATE``
    with all columns, we merge the patch against the existing row in the
    handler before calling ``upsert``.
    """

    name: Optional[str] = None
    transport: Optional[str] = None
    command: Optional[str] = None
    args: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None
    url: Optional[str] = None
    auth_method: Optional[str] = None
    auth_secret_env: Optional[str] = None
    enabled: Optional[bool] = None
    scope: Optional[str] = None
    connect_hint: Optional[str] = None

    @field_validator("transport")
    @classmethod
    def _check_transport(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _validate_transport(v)

    @field_validator("scope")
    @classmethod
    def _check_scope(cls, v: Optional[str]) -> Optional[str]:
        return _validate_scope(v) if v is not None else None

    @field_validator("auth_method")
    @classmethod
    def _check_auth_method(cls, v: Optional[str]) -> Optional[str]:
        return _validate_auth_method(v)


class MaterializeRequest(BaseModel):
    tool_id: Optional[str] = None
    # Linked data apps: ONLY when the caller explicitly designates the
    # targeted tool as the data-app lister (the /admin/linked-apps wizard
    # does) is its output table projected into `data_apps`. Without this,
    # every targeted run — e.g. the per-tool "Materialize now" button on the
    # source detail page — would be treated as the lister and could fabricate
    # linked rows from an unrelated table (Devin Review on #1116).
    lister: bool = False


# ---------------------------------------------------------------------------
# Request / response models — tools
# ---------------------------------------------------------------------------


class CreateToolRequest(BaseModel):
    tool_id: Optional[str] = None  # auto-generated when omitted
    source_id: str
    original_name: str
    exposed_name: str
    mode: str
    table_id: Optional[str] = None
    input_schema: Optional[Dict[str, Any]] = None
    description: Optional[str] = None
    mutating: bool = False
    pii_fields: Optional[List[str]] = None
    rate_limit_pm: Optional[int] = None
    schedule: Optional[str] = None
    enabled: bool = True

    @field_validator("mode")
    @classmethod
    def _check_mode(cls, v: str) -> str:
        return _validate_mode(v)


class UpdateToolRequest(BaseModel):
    """Partial update — merge against existing row before re-upsert."""

    source_id: Optional[str] = None
    original_name: Optional[str] = None
    exposed_name: Optional[str] = None
    mode: Optional[str] = None
    table_id: Optional[str] = None
    input_schema: Optional[Dict[str, Any]] = None
    description: Optional[str] = None
    mutating: Optional[bool] = None
    pii_fields: Optional[List[str]] = None
    rate_limit_pm: Optional[int] = None
    schedule: Optional[str] = None
    enabled: Optional[bool] = None

    @field_validator("mode")
    @classmethod
    def _check_mode(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _validate_mode(v)


class AddGrantRequest(BaseModel):
    group_id: str
    # v121: opt this group into the tool's MUTATING surface. Tri-state:
    # omitted (None) leaves an existing grant's flag unchanged (a new grant
    # lands read-only); an explicit true/false sets it — re-POSTing with a
    # value is the edit path. Consumed by the per-tool endpoint only; the
    # source-wide bulk grant is deliberately read-only (opting a group into
    # every write tool of an upstream in one action is too coarse an act to
    # be one flag away).
    allow_mutating: Optional[bool] = None


# ---------------------------------------------------------------------------
# Request / response models — outbound OAuth client (2026-07-30 spec §2)
# ---------------------------------------------------------------------------


class OAuthRegisterRequest(BaseModel):
    """Body for ``POST …/oauth/register`` — RFC 7591 dynamic registration.

    Everything else (issuer, endpoints, PKCE support) is discovered live
    from the source's own ``url``; ``scopes`` is the one admin override the
    spec calls out (default empty — AS/resource defaults apply)."""

    scopes: Optional[str] = None


class OAuthClientConfigRequest(BaseModel):
    """Body for ``PUT …/oauth/client`` — the manual escape hatch for an AS
    without dynamic client registration.

    ``issuer`` is optional: when omitted it defaults to the
    ``authorization_endpoint``'s origin — the schema's ``issuer`` column is
    NOT NULL but the spec's manual-config shape doesn't call out a separate
    issuer field, so this keeps the escape hatch a 4-field form in the
    common case (same-origin AS) while still accepting an explicit value
    when the issuer differs from the endpoint host."""

    client_id: str
    client_secret: Optional[str] = None
    authorization_endpoint: str
    token_endpoint: str
    issuer: Optional[str] = None
    scopes: Optional[str] = None


#: The tuple a stored user token is bound to. A token minted by one
#: authorization server, for one client, at one pair of endpoints is useless
#: — and dangerous to send — anywhere else, so a change to ANY of these
#: invalidates every user's tokens for the source (Devin Review on #1124).
_OAUTH_IDENTITY_FIELDS = ("issuer", "authorization_endpoint", "token_endpoint", "client_id")


def _oauth_identity_changed(existing: Dict[str, Any], **written: Optional[str]) -> bool:
    """True when the about-to-be-written client row addresses a different
    authorization-server identity than the stored one.

    Shared by the DCR re-register and the manual ``PUT …/oauth/client`` so the
    two cannot drift — the manual path silently keeping tokens across an
    endpoint repoint, while the DCR path dropped them, is exactly the
    asymmetry this collapses.
    """
    return any((existing.get(k) or "") != (written.get(k) or "") for k in _OAUTH_IDENTITY_FIELDS)


def _serialize_oauth_client(row: Dict[str, Any]) -> Dict[str, Any]:
    """Project an ``mcp_source_oauth_clients`` row to the API shape.

    Write-only fields (``client_secret``, ``registration_access_token``) are
    NEVER echoed back — same contract as the vault secret endpoints
    (``has_vault_secret`` flag, never the value). ``has_client_secret``
    mirrors that pattern here.
    """
    return {
        "source_id": row.get("source_id"),
        "issuer": row.get("issuer"),
        "client_id": row.get("client_id"),
        # Ciphertext presence, not decryptability: after a vault-key rotation
        # the secret is unreadable but still THERE, and reporting False would
        # tell an admin the registration is a public client when it is not
        # (Devin Review on #1124).
        "has_client_secret": bool(row.get("client_secret_present", row.get("client_secret"))),
        "authorization_endpoint": row.get("authorization_endpoint"),
        "token_endpoint": row.get("token_endpoint"),
        "scopes": row.get("scopes"),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _audit(
    conn: duckdb.DuckDBPyConnection,
    actor_id: str,
    action: str,
    resource: str,
    params: Optional[Dict[str, Any]] = None,
    params_before: Optional[Dict[str, Any]] = None,
) -> None:
    """Best-effort audit row. Mirrors ``app/api/data_packages._audit``."""
    try:
        audit_repo().log(
            user_id=actor_id,
            action=action,
            resource=resource,
            params=params,
            params_before=params_before,
        )
    except Exception:
        logger.warning("audit log failed for %s/%s", action, resource)


def _url_policy_report(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """A per-row url-policy verdict, DNS-free (#1216 part 1: the sweep/report).

    ``check_source_url`` gates a source's ``url`` at CONFIGURATION time only
    — a row registered before the guard existed, or before
    ``mcp.source_url_strict`` was turned on, can stay enabled and forward
    credentials to an address the CURRENT policy would refuse
    (``test_an_unrelated_edit_does_not_revalidate_an_already_live_url``).
    This surfaces that gap on the existing list/detail response so an admin
    can find and fix those rows without dialing anything.

    Judged with :func:`check_source_url_dns_free` — NOT the full,
    resolver-backed check :func:`_source_url_verdict` uses for the
    connectivity-test probe. A list endpoint iterating every registered
    source must stay cheap and deterministic, not fire a blocking
    ``getaddrinfo`` per row; the trade-off is that an ordinary hostname whose
    resolved address the full policy WOULD refuse reports ``ok`` here rather
    than ``would_refuse`` (see the module docstring in
    ``src/net/mcp_source_url.py`` on why an unresolved host is never treated
    as refused).

    ``None`` for ``stdio``, the same exemption every other caller of this
    policy applies: the secret rides the subprocess environment and ``url``
    is inert documentation nothing ever dials.
    """
    if (row.get("transport") or "") not in ("http", "sse"):
        return None
    from app.instance_config import get_mcp_source_url_strict, get_ssrf_allowed_hosts
    from src.net.mcp_source_url import check_source_url_dns_free

    verdict = check_source_url_dns_free(
        row.get("url") or "",
        strict=get_mcp_source_url_strict(),
        allowed_hosts=get_ssrf_allowed_hosts(),
    )
    return {
        "verdict": "ok" if verdict.ok else "would_refuse",
        "reasons": [verdict.reason] if verdict.reason else [],
    }


def _serialize_source(
    row: Dict[str, Any],
    *,
    has_vault_secret: bool = False,
    vault_secret_updated_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Project a ``mcp_sources`` row to the API shape (timestamps as ISO).

    ``has_vault_secret`` is a write-only-secret status flag — True iff a
    vault-stored secret exists for this source (the value is never read
    back into the API). ``vault_secret_updated_at`` is that same secret's
    last-rotated timestamp (ISO-8601, or ``None`` when unset/not computed by
    the caller) — distinct from ``updated_at`` below, which is the SOURCE
    row's own last-edit time, not the secret's.
    """
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "transport": row.get("transport"),
        "command": row.get("command"),
        "args": row.get("args") or [],
        "env": row.get("env") or {},
        "url": row.get("url"),
        "auth_method": row.get("auth_method"),
        "auth_secret_env": row.get("auth_secret_env"),
        "enabled": bool(row.get("enabled")) if row.get("enabled") is not None else True,
        "scope": row.get("scope") or "shared",
        "connect_hint": row.get("connect_hint"),
        "has_vault_secret": has_vault_secret,
        "vault_secret_updated_at": vault_secret_updated_at,
        "url_policy_verdict": _url_policy_report(row),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


def _serialize_tool(row: Dict[str, Any]) -> Dict[str, Any]:
    """Project a ``tool_registry`` row to the API shape."""
    return {
        "tool_id": row.get("tool_id"),
        "source_id": row.get("source_id"),
        "original_name": row.get("original_name"),
        "exposed_name": row.get("exposed_name"),
        "mode": row.get("mode"),
        "table_id": row.get("table_id"),
        "input_schema": row.get("input_schema"),
        "description": row.get("description"),
        "mutating": bool(row.get("mutating")) if row.get("mutating") is not None else False,
        "pii_fields": row.get("pii_fields") or [],
        "rate_limit_pm": row.get("rate_limit_pm"),
        "schedule": row.get("schedule"),
        "projection_map": row.get("projection_map") or None,
        "enabled": bool(row.get("enabled")) if row.get("enabled") is not None else True,
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


def _per_user_secret_coverage(source_id: str) -> List[Dict[str, Any]]:
    """Who has connected their OWN credential for ``source_id``, and when.

    Read-only admin diagnostic for the detail page — never returns a secret
    value, only identity (``user_id`` / ``email`` / ``name``) + the ISO-8601
    timestamp of their last upsert. Ordered by ``user_id`` (what
    ``list_for_source`` already returns), so the result is deterministic.

    Built entirely from the two existing, already-parity-tested
    ``PerUserSecretsRepository`` methods — ``list_for_source`` (previously
    called only from the delete/purge cleanup paths in this module and in
    ``admin_source_connections.py``) and ``get_updated_at`` (added for the
    per-user connect status endpoint) — rather than a new repo method, so
    this diagnostic adds no new surface to keep in DuckDB/Postgres parity.
    """
    from src.repositories import users_repo

    pu_secrets = per_user_secrets_repo()
    user_ids = pu_secrets.list_for_source(source_id)
    if not user_ids:
        return []
    info = users_repo().get_info_by_ids(user_ids)
    return [
        {
            "user_id": uid,
            "email": info.get(uid, {}).get("email"),
            "name": info.get(uid, {}).get("name"),
            "updated_at": pu_secrets.get_updated_at(source_id, uid),
        }
        for uid in user_ids
    ]


def _merge_source_patch(existing: Dict[str, Any], patch: UpdateMCPSourceRequest) -> Dict[str, Any]:
    """Merge a partial source patch onto the existing row.

    Returns the kwargs dict to pass to ``MCPSourceRepository.upsert``.

    Fields with a non-nullable default (``scope``, ``enabled``) are normalized
    HERE rather than at the call sites. ``exclude_unset`` treats an explicit
    JSON ``null`` as set, so ``{"scope": null}`` used to survive the merge as
    ``None`` — and a caller that substituted a default while validating, but
    passed this dict verbatim to ``upsert``, would validate one value and write
    another. On the update path that divergence sat in front of the
    irreversible credential purge: the request validated as ``'shared'``,
    purged, and only then 400'd inside ``upsert`` on ``scope=None``. Normalizing
    the merged row makes the validated value and the written value the same
    object by construction, instead of by every call site remembering to use
    the same expression (Devin Review on #1124).
    """
    data = patch.model_dump(exclude_unset=True)
    merged = {
        "id": existing["id"],
        "name": data.get("name", existing.get("name")),
        "transport": data.get("transport", existing.get("transport")),
        "command": data.get("command", existing.get("command")),
        "args": data.get("args", existing.get("args")),
        "env": data.get("env", existing.get("env")),
        "url": data.get("url", existing.get("url")),
        "auth_method": data.get("auth_method", existing.get("auth_method")),
        "auth_secret_env": data.get("auth_secret_env", existing.get("auth_secret_env")),
        "enabled": data.get(
            "enabled",
            bool(existing.get("enabled")) if existing.get("enabled") is not None else True,
        ),
        "scope": data.get("scope", existing.get("scope")),
        "connect_hint": data.get("connect_hint", existing.get("connect_hint")),
    }
    # Explicit-null semantics, and why they differ per field (asked on #1124):
    # null means CLEAR for the nullable columns (`url`, `command`, `args`,
    # `auth_method`, `auth_secret_env`, `connect_hint`) because it is the only
    # way to clear them, and the admin UI relies on exactly that — its save
    # handler sends `auth_method: <value> || null` and, on a transport switch,
    # `url: null` / `command: null` to mean "unset this". Taking that away
    # would leave no way to blank a field.
    #
    # `scope` and `enabled` are NOT nullable (`enabled BOOLEAN NOT NULL`;
    # `scope` is validated against a fixed set), so "clear" has no meaning for
    # them and null can only be noise from a client that serializes its unset
    # optionals — fall back to the stored value, then the type's default.
    # Coercing to the default instead silently re-enabled a source an admin had
    # disabled (Devin Review on #1124).
    if merged["enabled"] is None:
        merged["enabled"] = bool(existing["enabled"]) if existing.get("enabled") is not None else True
    merged["scope"] = merged["scope"] or existing.get("scope") or "shared"
    # `name` belongs to the same non-nullable group (`name VARCHAR NOT NULL
    # UNIQUE`). It was missed because the handler's own empty-name guard reads
    # the PATCH (`payload.name is not None and not new_name`) and an explicit
    # null never trips it, while `validate_source_fields` does not look at
    # `name` at all — so a `{"name": null}` rode all the way past the
    # irreversible credential purge and only died on the NOT NULL constraint,
    # surfacing as a bogus "name_exists" 409 (Devin Review on #1124).
    if merged["name"] is None:
        merged["name"] = existing.get("name")
    return merged


def _merge_tool_patch(existing: Dict[str, Any], patch: UpdateToolRequest) -> Dict[str, Any]:
    """Merge a partial tool patch onto the existing row → upsert kwargs."""
    data = patch.model_dump(exclude_unset=True)
    return {
        "tool_id": existing["tool_id"],
        "source_id": data.get("source_id", existing.get("source_id")),
        "original_name": data.get("original_name", existing.get("original_name")),
        "exposed_name": data.get("exposed_name", existing.get("exposed_name")),
        "mode": data.get("mode", existing.get("mode")),
        "table_id": data.get("table_id", existing.get("table_id")),
        "input_schema": data.get("input_schema", existing.get("input_schema")),
        "description": data.get("description", existing.get("description")),
        "mutating": data.get(
            "mutating",
            bool(existing.get("mutating")) if existing.get("mutating") is not None else False,
        ),
        "pii_fields": data.get("pii_fields", existing.get("pii_fields")),
        "rate_limit_pm": data.get("rate_limit_pm", existing.get("rate_limit_pm")),
        "schedule": data.get("schedule", existing.get("schedule")),
        "enabled": data.get(
            "enabled",
            bool(existing.get("enabled")) if existing.get("enabled") is not None else True,
        ),
    }


def _probe_caller_user_id(src: Dict[str, Any], user: dict) -> Optional[str]:
    """Caller identity for the admin connect probes (introspect/classify/test).

    A ``per_user``-scoped source is probed under the calling admin's own
    connected secret when they have one; otherwise (and always for
    ``shared`` scope) the probe stays on the caller-less shared-vault path,
    preserving the pre-existing fallback behavior. The client's fail-closed
    rule (an identified caller never borrows the shared credential) is why
    this pre-check lives here rather than passing ``user["id"]`` blindly.

    ``auth_method='oauth'`` sources have no ``mcp_user_secrets`` row at all
    — the probe instead checks ``mcp_user_oauth_tokens`` for the calling
    admin's own OAuth connection (2026-07-30 outbound MCP OAuth sources spec
    §5), so an admin who has connected their own account gets probed under
    it; otherwise the probe stays caller-less exactly like every other
    per_user source with no stored credential.
    """
    if (src.get("scope") or "shared").strip().lower() != "per_user":
        return None
    try:
        if per_user_secrets_repo().get(src["id"], user["id"]):
            return user["id"]
    except Exception:  # vault/db unavailable — keep the legacy path
        pass
    try:
        from src.repositories import mcp_user_oauth_tokens_repo

        if mcp_user_oauth_tokens_repo().has(src["id"], user["id"]):
            return user["id"]
    except Exception:  # vault/db unavailable — keep the legacy path
        pass
    return None


# ---------------------------------------------------------------------------
# Source CRUD
# ---------------------------------------------------------------------------


def _require_safe_source_name(name: str) -> None:
    """Reject names the orchestrator's extract scan would refuse to ATTACH.

    The extractor writes ``/data/extracts/<name>/`` and the orchestrator
    validates that directory with the STRICT identifier rule before
    attaching — a name that fails it (hyphen, leading digit, >64 chars)
    materializes "successfully" but silently never reaches the catalog,
    with only a server-log WARNING. Same validator, no second regex.
    """
    if not is_safe_identifier(name):
        raise HTTPException(
            status_code=400,
            detail=(
                "source name must be a safe SQL identifier — letters, "
                "digits and underscores, not starting with a digit, max "
                f"64 chars (the sync engine refuses to attach anything "
                f"else); got {name!r}"
            ),
        )


async def _source_url_verdict(row: dict):
    """The source-url verdict for a row, or ``None`` when the url is not live.

    Split out of :func:`_check_source_url_or_400` so a caller can *report* the
    verdict instead of refusing on it. The connectivity-test probe needs that:
    its contract is HTTP 200 with a diagnostic, so answering a refused url with
    a 400 would withhold the one answer the admin pressed the button for.

    ``None`` for ``stdio``: there the secret goes into the subprocess
    environment and ``url`` is never dialed — it is inert documentation, and
    refusing a note nobody connects to would be theatre. Mirrors the same
    transport test the credential purge uses, so the two agree about when a url
    is live.

    ``check_source_url`` does a BLOCKING ``getaddrinfo``; callers are ``async
    def`` handlers, so it goes through ``asyncio.to_thread`` rather than
    stalling the event loop for the resolver timeout — same hazard, and same
    remedy, as ``set_oauth_client_config``.
    """
    if (row.get("transport") or "") not in ("http", "sse"):
        return None
    from app.instance_config import get_mcp_source_url_strict, get_ssrf_allowed_hosts
    from src.net.mcp_source_url import check_source_url

    strict = get_mcp_source_url_strict()
    # The SAME allowlist every other admin-configured URL consults
    # (`_validate_url_not_private` in app/api/admin.py). A host an operator has
    # already declared trusted must not have to be declared twice.
    return await asyncio.to_thread(
        lambda: check_source_url(
            row.get("url") or "",
            strict=strict,
            allowed_hosts=get_ssrf_allowed_hosts(),
        )
    )


async def _check_source_url_or_400(row: dict) -> str:
    """Gate a source row's ``url`` (#1154). Returns the warning to audit, if any.

    No-op for ``stdio``: there the secret goes into the subprocess environment
    and ``url`` is never dialed — it is inert documentation, and refusing a
    note nobody connects to would be theatre. Mirrors the same transport test
    the credential purge uses, so the two agree about when a url is live.

    ``check_source_url`` does a BLOCKING ``getaddrinfo``; this is an ``async
    def`` handler, so it goes through ``asyncio.to_thread`` rather than
    stalling the event loop for the resolver timeout — same hazard, and same
    remedy, as ``set_oauth_client_config``.
    """
    verdict = await _source_url_verdict(row)
    if verdict is None:
        return ""
    from app.instance_config import get_mcp_source_url_strict

    strict = get_mcp_source_url_strict()
    if not verdict.ok:
        raise HTTPException(
            status_code=400,
            detail=(
                f"url failed validation: {verdict.reason}"
                + ("" if strict else " (set mcp.source_url_strict to also require https to a public address)")
            ),
        )
    return verdict.warning


@router.post("/mcp-sources", status_code=201)
async def create_mcp_source(
    payload: CreateMCPSourceRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Register a new MCP source. Returns ``{"id": ...}``.

    ``name`` is unique (DB constraint); the repo's ``upsert`` keys on ``id``,
    so we generate one and translate UNIQUE-name collisions to 409.
    """
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    _require_safe_source_name(name)
    repo = mcp_sources_repo()
    if repo.get_by_name(name) is not None:
        raise HTTPException(status_code=409, detail="name_exists")
    # #1154 — see the same call in update_mcp_source. Runs before the row is
    # written so a refused url never reaches the registry at all.
    url_warning = await _check_source_url_or_400(
        {"transport": payload.transport, "url": payload.url},
    )
    source_id = str(uuid.uuid4())
    try:
        repo.upsert(
            id=source_id,
            name=name,
            transport=payload.transport,
            command=payload.command,
            args=payload.args,
            env=payload.env,
            url=payload.url,
            auth_method=payload.auth_method,
            auth_secret_env=payload.auth_secret_env,
            enabled=payload.enabled,
            scope=payload.scope or "shared",
            connect_hint=payload.connect_hint,
        )
    except ValueError as exc:
        # transport/command/url validation errors from the repo
        raise HTTPException(status_code=400, detail=str(exc))
    except (duckdb.ConstraintException, sa_exc.IntegrityError):
        # Same duplicate name, same 409, either backend — see update_mcp_source.
        raise HTTPException(status_code=409, detail="name_exists")
    _audit(
        conn,
        user["id"],
        "mcp_source.create",
        f"mcp_source:{source_id}",
        {
            "name": name,
            "transport": payload.transport,
            **({"url_warning": url_warning} if url_warning else {}),
        },
    )
    return {"id": source_id}


@router.get("/mcp-sources")
async def list_mcp_sources(
    enabled_only: bool = False,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = mcp_sources_repo()
    rows = repo.list_all(enabled_only=enabled_only)
    secrets = shared_secrets_repo()
    return [_serialize_source(r, has_vault_secret=secrets.has(r["id"])) for r in rows]


@router.get("/mcp-sources/{source_id}")
async def get_mcp_source(
    source_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Detail view — includes the list of tools registered against this
    source, the shared vault secret's last-rotated timestamp
    (``vault_secret_updated_at``), and per-user secret coverage
    (``per_user_secrets`` — who has connected their own credential, and
    when; never the secret value)."""
    repo = mcp_sources_repo()
    src = repo.get(source_id)
    if not src:
        raise HTTPException(status_code=404, detail="mcp_source_not_found")
    tools_repo = tool_registry_repo()
    tools = tools_repo.list_for_source(source_id)
    secrets = shared_secrets_repo()
    out = _serialize_source(
        src,
        has_vault_secret=secrets.has(source_id),
        vault_secret_updated_at=secrets.get_updated_at(source_id),
    )
    out["tools"] = [_serialize_tool(t) for t in tools]
    out["per_user_secrets"] = _per_user_secret_coverage(source_id)
    return out


@router.put("/mcp-sources/{source_id}")
async def update_mcp_source(
    source_id: str,
    payload: UpdateMCPSourceRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Partial update. Audit row carries before/after for the changed fields.

    **Changing ``url`` on an http/sse source discards every stored credential
    for it** — the shared vault secret, all per-user secrets, and (when the
    source is OAuth) its client registration, user tokens and in-flight flows.
    This is irreversible and happens without a separate confirmation, because
    a credential must never be forwarded to a host that did not issue it, and
    a path-only edit can still be a different protected resource. Users
    re-connect afterwards. The audit row records ``credentials_purged`` so the
    effect is traceable after the fact.

    Flipping a ``stdio`` source to http/sse purges too: it makes an already
    stored ``url`` live for the first time. Editing ``url`` on a row that stays
    ``stdio`` does not — there the secret goes into the subprocess environment
    and the url is never read.
    """
    repo = mcp_sources_repo()
    existing = repo.get(source_id)
    if not existing:
        raise HTTPException(status_code=404, detail="mcp_source_not_found")

    # If renaming, ensure no collision against a different source.
    new_name = (payload.name or "").strip() if payload.name is not None else None
    if payload.name is not None and not new_name:
        raise HTTPException(status_code=400, detail="name is required")
    if new_name and new_name != existing.get("name"):
        _require_safe_source_name(new_name)
        collision = repo.get_by_name(new_name)
        if collision and collision["id"] != source_id:
            raise HTTPException(status_code=409, detail="name_exists")

    merged = _merge_source_patch(existing, payload)
    if new_name is not None:
        # Store the STRIPPED name. _merge_source_patch passes the raw payload
        # value through, so a padded " valid_name" would validate above (on
        # the stripped form) yet be persisted with whitespace — which the
        # orchestrator's identifier check then rejects at attach time, the
        # exact silent failure this validation exists to prevent.
        merged["name"] = new_name
    before = {k: existing.get(k) for k in ("name", "transport", "command", "url", "enabled")}
    # Validate the MERGED row against the repo's own rules before anything is
    # written or purged. A partial update can flip auth_method to 'oauth' (or
    # transport/scope away from what oauth requires) without ever mentioning
    # the other two fields, so the patch alone is not enough to judge. Running
    # the repo's validator — not a local restatement of it — is what keeps the
    # purge below from firing for a request that is about to 400 anyway.
    #
    # Every value below is read straight out of `merged`, with no defaulting:
    # substituting one here would validate a row that differs from the one
    # `upsert` is handed, and the purge sits between the two. Defaults belong
    # to _merge_source_patch, which applies them once.
    try:
        validate_source_fields(
            transport=merged.get("transport"),
            command=merged.get("command"),
            url=merged.get("url"),
            auth_method=merged.get("auth_method"),
            scope=merged["scope"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # #1154: the source's own url is dialed with a credential attached on EVERY
    # forward, so it earns the same configuration-time check the OAuth
    # endpoints get — with a policy that does not outlaw internal MCP servers
    # (see src/net/mcp_source_url.py for where the line falls and why).
    #
    # Placed here for the same reason `validate_source_fields` is: it must sit
    # AFTER the merge (a patch that only flips transport can make a stored url
    # live for the first time) and BEFORE the purge, so a request that is about
    # to 400 cannot destroy credentials on its way out.
    #
    # Skipped when the result is DISABLED. The check exists to stop a rejected
    # address becoming reachable, and enforcing it on a row being turned off
    # instead traps the operator: a source registered before this guard (or
    # before `mcp.source_url_strict` was turned on) would 400 on every update,
    # including the update that turns it OFF — the one action that reduces the
    # risk (Devin Review on #1204). Remediation is now coherent: disable, fix
    # the url, re-enable — and re-enabling validates, so nothing reaches a live
    # state unchecked.
    #
    # What `enabled=False` actually buys is the two RUNTIME forwards, which
    # re-fetch the row and refuse (`mcp/tools_generator.py`,
    # `mcp_passthrough.py`). It is NOT "never dialed": the admin probes do not
    # consult `enabled`, so they carry their own copy of this check rather than
    # leaning on the flag.
    #
    # Also skipped when this write does not touch what the check polices. The
    # guard resolves DNS, so running it on EVERY update makes an unrelated
    # edit — a rename, a `connect_hint` tweak — fail during a resolver blip;
    # on a strict instance that is a hard 400 on a field the admin never
    # touched. The module docstring rules that footgun out by design, and
    # re-running the check on an unchanged url quietly reintroduced it (Devin
    # on #1204).
    #
    # So the trigger is the same predicate the credential purge below uses —
    # `url_repointed`, "this write puts a (new) address in front of the
    # credentials": the url changed, or a stdio row went network and made a
    # stored url live for the first time. Plus off→on, which does not repoint
    # anything but does turn the forwards on. What is left over — an already
    # live row whose url this write does not touch — is the legacy-row case,
    # and re-validating it here would only ever fire on an edit that had
    # nothing to do with it; closing that gap is a sweep's job (see the reply
    # on the runtime-forwards thread), not this handler's.
    now_network = (merged.get("transport") or "") in ("http", "sse")
    was_network = (existing.get("transport") or "") in ("http", "sse")
    url_repointed = now_network and ((existing.get("url") or "") != (merged.get("url") or "") or not was_network)
    becoming_enabled = bool(merged.get("enabled")) and not bool(existing.get("enabled"))
    if merged.get("enabled") and (url_repointed or becoming_enabled):
        url_warning = await _check_source_url_or_400(merged)
    else:
        url_warning = ""

    was_oauth = (existing.get("auth_method") or "").strip().lower() == "oauth"
    still_oauth = (merged.get("auth_method") or "").strip().lower() == "oauth"
    # Repointing `url` sends every stored credential for this source to a
    # host that did not issue it. That is true of ALL credential kinds, not
    # just OAuth: a `bearer` source's per-user token and a `shared` source's
    # vault secret are forwarded as `Authorization` headers by the same seam,
    # which reads the freshly-written url. Any change counts — a path-only
    # edit can still be a different protected resource (Devin Review on #1124
    # for the OAuth half; invariant sweep for the rest).
    #
    # It is only true where the url is LIVE, though. On a `stdio` row the
    # secret is injected into the subprocess environment under
    # `auth_secret_env` and `url` is never read at all — inert documentation.
    # Purging there is pure data loss: an admin filling in or correcting that
    # note would destroy the vault secret and every analyst's per-user secret
    # for a field the credential never travels to (Devin Review on #1124).
    #
    # The gate is the NEW transport, which also covers the case that is not a
    # url edit at all: flipping a stdio source to http/sse makes an already
    # stored url live for the first time, so a secret minted for a subprocess
    # starts being sent as an `Authorization` header to a host it was never
    # meant for. Same exposure, so the same purge.
    #
    # `url_repointed` is computed above, next to the url check, because that
    # check asks the same question this purge does — "does this write put a
    # (new) address in front of credentials?" — and two copies of that
    # predicate would be free to drift apart.
    # The purge runs BEFORE the row is repointed, deliberately. There is no
    # transaction spanning the sources row and the vault tables, so one of the
    # two orders has to lose on a mid-sequence failure — and they do not lose
    # equally. Purge-last leaves the source already pointing at the new host
    # with the old credentials still on file, i.e. exactly the state the purge
    # exists to prevent, and the very next forward ships them to that host.
    # Purge-first leaves credentials gone while the source still points at the
    # old host: nothing is disclosed, and the recovery is the one this endpoint
    # already documents — users re-connect. The failure modes an operator can
    # actually trigger (invalid oauth coupling, name collision) are ruled out
    # above so they cannot reach the purge (Devin Review on #1124).
    # What the audit records must be what actually RAN, not the predicate that
    # was expected to trigger it. Keying the flag off `url_repointed` alone
    # missed the second branch entirely: flipping `auth_method` off oauth
    # destroys every analyst's tokens and the client registration with
    # `url_repointed` False, so the row said nothing was purged while an
    # operator hunting "why did everyone lose access" saw an ordinary field
    # edit (Devin Review on #1124).
    purged: List[str] = []
    if url_repointed:
        # Per-user secrets go through the factory — a raw repo would write to
        # the always-DuckDB connection and orphan rows on a PG instance.
        shared_secrets_repo().delete(source_id)
        pu_secrets = per_user_secrets_repo()
        for uid in pu_secrets.list_for_source(source_id):
            pu_secrets.delete(source_id, uid)
        purged.append("vault_secrets")
    if (was_oauth and not still_oauth) or (was_oauth and still_oauth and url_repointed):
        # Flipping away from oauth strands the OAuth trio — the client
        # registration, every user's tokens, and in-flight flows are useless
        # under any other auth_method and must not linger as orphaned
        # credential material (Devin Review on #1124). Flipping BACK later
        # means re-registering + users re-connecting, same as a new source.
        from src.repositories import (
            mcp_oauth_flows_repo,
            mcp_source_oauth_clients_repo,
            mcp_user_oauth_tokens_repo,
        )

        mcp_user_oauth_tokens_repo().delete_for_source(source_id)
        mcp_oauth_flows_repo().delete_for_source(source_id)
        mcp_source_oauth_clients_repo().delete(source_id)
        purged.append("oauth_client_and_tokens")

    try:
        repo.upsert(**merged)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except (duckdb.ConstraintException, sa_exc.IntegrityError):
        # The pre-check above rules this out for everything except a rename
        # racing a concurrent one, so it is the last narrow window where the
        # purge has already run. Both backends must land on the same 409 —
        # on Postgres the unique violation arrives as SQLAlchemy
        # IntegrityError, which fell through to a 500 and, now that the purge
        # runs first, would have reported an internal error for a request that
        # had already dropped the source's credentials (Devin Review on #1124).
        raise HTTPException(status_code=409, detail="name_exists")
    fresh = repo.get(source_id)
    after = {k: (fresh or {}).get(k) for k in ("name", "transport", "command", "url", "enabled")}
    _audit(
        conn,
        user["id"],
        "mcp_source.update",
        f"mcp_source:{source_id}",
        # credentials_purged is recorded because the purge is irreversible
        # and is a side effect of an ordinary field edit — without it the
        # audit trail shows a url change and no trace of what it destroyed
        # (review finding on #1124). purged_kinds names WHICH, since the two
        # branches fire independently and cost the operator different things.
        # url_warning: an accepted-but-notable url (a credentialed forward to an
        # internal address). Recorded rather than merely logged so "does this
        # instance talk to anything on the intranet" is answerable from the
        # audit trail instead of from a grep over server logs (#1154).
        {
            "after": after,
            "credentials_purged": bool(purged),
            "purged_kinds": purged,
            **({"url_warning": url_warning} if url_warning else {}),
        },
        params_before={"before": before},
    )
    return (
        _serialize_source(fresh, has_vault_secret=shared_secrets_repo().has(source_id)) if fresh else {"id": source_id}
    )


@router.delete("/mcp-sources/{source_id}", status_code=204)
async def delete_mcp_source(
    source_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Hard delete — cascades to ``tool_registry`` + ``tool_grants`` for this
    source via :py:meth:`ToolRegistryRepository.delete_for_source` (which
    deletes grants per tool before the registry row)."""
    src_repo = mcp_sources_repo()
    existing = src_repo.get(source_id)
    if not existing:
        raise HTTPException(status_code=404, detail="mcp_source_not_found")
    tool_repo = tool_registry_repo()
    tool_count = len(tool_repo.list_for_source(source_id))
    tool_repo.delete_for_source(source_id)
    src_repo.delete(source_id)
    # Clean up vault secrets so a deleted source leaves no orphaned encrypted
    # blobs — the shared secret plus any per-user rows. (Devin Review on #530.)
    shared_secrets_repo().delete(source_id)
    # Per-user secrets were migrated to Postgres (#530), so they must be dropped
    # through the factory — a raw PerUserSecretsRepository(conn) deletes from the
    # always-DuckDB connection and leaves orphaned rows on a PG instance.
    pu_secrets = per_user_secrets_repo()
    for uid in pu_secrets.list_for_source(source_id):
        pu_secrets.delete(source_id, uid)
    # OAuth trio: every user's tokens, in-flight PKCE flows, and Agnes's own
    # client registration for this source — a deleted source must leave no
    # orphaned credential material (Devin Review on #1124).
    from src.repositories import (
        mcp_oauth_flows_repo,
        mcp_source_oauth_clients_repo,
        mcp_user_oauth_tokens_repo,
    )

    mcp_user_oauth_tokens_repo().delete_for_source(source_id)
    mcp_oauth_flows_repo().delete_for_source(source_id)
    mcp_source_oauth_clients_repo().delete(source_id)
    _audit(
        conn,
        user["id"],
        "mcp_source.delete",
        f"mcp_source:{source_id}",
        {"name": existing.get("name"), "tool_count": tool_count},
    )


# ---------------------------------------------------------------------------
# Source secret (server-wide vault) — RFC #461 §4
# ---------------------------------------------------------------------------


class SecretBody(BaseModel):
    value: str


@router.put("/mcp-sources/{source_id}/secret", status_code=204)
async def set_mcp_source_secret(
    source_id: str,
    body: SecretBody,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Store (or rotate) the server-wide vault secret for ``source_id``.

    The plaintext lives only in the request body — Fernet-encrypted at
    rest in ``mcp_secrets``. ``connectors/mcp/client._lookup_secret_for_source``
    pulls it on every call, falling back to the legacy
    ``auth_secret_env`` lookup if the vault has no row, so an operator
    can roll out the vault without a flag-day rewrite of source rows.
    """
    src_repo = mcp_sources_repo()
    if not src_repo.get(source_id):
        raise HTTPException(status_code=404, detail="mcp_source_not_found")
    if not body.value:
        raise HTTPException(status_code=400, detail="secret value required")
    try:
        shared_secrets_repo().upsert(source_id, body.value)
    except VaultKeyNotConfiguredError as exc:
        raise HTTPException(
            status_code=409,
            detail="vault_key_not_configured: set AGNES_VAULT_KEY on the server before storing secrets",
        ) from exc
    _audit(
        conn,
        user["id"],
        "mcp_source.secret.set",
        f"mcp_source:{source_id}",
        {},
    )


@router.delete("/mcp-sources/{source_id}/secret", status_code=204)
async def delete_mcp_source_secret(
    source_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Drop the vault row for ``source_id``. Source then falls back to
    its ``auth_secret_env`` env-var, or to anonymous if neither is set."""
    src_repo = mcp_sources_repo()
    if not src_repo.get(source_id):
        raise HTTPException(status_code=404, detail="mcp_source_not_found")
    shared_secrets_repo().delete(source_id)
    _audit(
        conn,
        user["id"],
        "mcp_source.secret.delete",
        f"mcp_source:{source_id}",
        {},
    )


# ---------------------------------------------------------------------------
# Outbound OAuth client registration (2026-07-30 spec §2)
# ---------------------------------------------------------------------------


def _url_origin(url: str) -> str:
    """``scheme://netloc`` of ``url`` — used as the default ``issuer`` for a
    manually-configured OAuth client when the admin omits it."""
    from urllib.parse import urlparse

    parts = urlparse(url)
    return f"{parts.scheme}://{parts.netloc}"


def _require_oauth_source(src: Dict[str, Any]) -> None:
    if (src.get("auth_method") or "").lower() != "oauth":
        raise HTTPException(
            status_code=400,
            detail="source is not configured with auth_method='oauth'",
        )


def _oauth_redirect_uri() -> str:
    """``{server_url}/api/mcp/oauth-client/callback`` — the outbound
    client's callback path, deliberately distinct from the inbound issuer's
    ``/api/mcp/oauth/*`` (spec §3 routing note)."""
    from app.instance_config import get_public_url

    base = get_public_url()
    if not base:
        raise HTTPException(
            status_code=409,
            detail="server.public_url is not configured — required to build the OAuth redirect_uri",
        )
    return f"{base}/api/mcp/oauth-client/callback"


@router.post("/mcp-sources/{source_id}/oauth/register")
async def register_oauth_client(
    source_id: str,
    payload: Optional[OAuthRegisterRequest] = None,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """RFC 9728 + RFC 8414 discovery, PKCE-S256 fail-closed check, RFC 7591
    dynamic client registration against the source's own authorization
    server. Idempotent — a second call replaces the row, best-effort
    revoking the OLD registration first (spec §2 step 5).
    """
    from connectors.mcp.oauth_client import (
        OAuthDiscoveryError,
        best_effort_revoke_registration,
        build_oauth_http_client,
        discover_as_metadata,
        discover_protected_resource_metadata,
        register_dynamic_client,
        require_https_endpoints,
        require_pkce_s256,
        resolve_issuer,
    )

    import httpx

    from connectors.mcp.client import exc_summary as _exc_summary
    from src.net.ssrf_safe_client import SSRFRejected
    from src.repositories import mcp_source_oauth_clients_repo

    src_repo = mcp_sources_repo()
    src = src_repo.get(source_id)
    if not src:
        raise HTTPException(status_code=404, detail="mcp_source_not_found")
    _require_oauth_source(src)
    if not src.get("url"):
        raise HTTPException(status_code=400, detail="source has no 'url' to discover OAuth metadata from")

    redirect_uri = _oauth_redirect_uri()
    scopes = payload.scopes if payload else None
    clients_repo = mcp_source_oauth_clients_repo()
    existing = clients_repo.get(source_id)

    # This is the fifth path that dials the source's own url, and it
    # deliberately does NOT take `_check_source_url_or_400`. It does not need
    # it: `build_oauth_http_client()` is https-only and SSRF-safe, which is a
    # strictly tighter guard than the source-url policy — adding the policy on
    # top could only ever loosen nothing and refuse more.
    #
    # The two do disagree, in the opposite direction from #1154: the SSRF-safe
    # client refuses an intranet address that the source-url policy allows on
    # purpose (see src/net/mcp_source_url.py on why an internal MCP server is
    # an ordinary deployment). So an OAuth source on an internal address
    # cannot complete RFC 9728 discovery, whatever `mcp.source_url_strict`
    # says. That is intended: discovery negotiates with an authorization
    # server, and relaxing the client to reach one on the intranet would give
    # up the guard that makes every OAuth hop safe (Devin on #1204).
    try:
        async with build_oauth_http_client() as http_client:
            resource_meta = await discover_protected_resource_metadata(src["url"], client=http_client)
            issuer = resolve_issuer(resource_meta)
            as_metadata = await discover_as_metadata(issuer, client=http_client)
            require_https_endpoints(as_metadata)
            require_pkce_s256(as_metadata)

            # Register the NEW client first, revoke the OLD one only after
            # success — revoke-then-register left a failure window where the
            # stored row pointed at a cancelled registration and every user
            # silently lost access (Devin Review on #1124). A failed revoke
            # merely leaves a dangling upstream registration (harmless).
            registered = await register_dynamic_client(
                as_metadata,
                redirect_uri=redirect_uri,
                scopes=scopes,
                client=http_client,
            )

            # Only when the AS actually minted a DIFFERENT identity. RFC 7591
            # does not require a fresh client_id per registration — an AS that
            # dedupes on client_name/redirect_uris hands back the SAME one, and
            # revoking it would delete the registration just re-issued, leaving
            # the stored row pointing at nothing and every token call failing
            # with invalid_client (Devin Review on #1124). Same guard the user-
            # token purge below already carries.
            if existing and existing["client_id"] != registered.client_id:
                await best_effort_revoke_registration(
                    registration_endpoint=as_metadata.get("registration_endpoint"),
                    client_id=existing["client_id"],
                    registration_access_token=existing.get("registration_access_token"),
                    client=http_client,
                )
    except OAuthDiscoveryError as exc:
        raise HTTPException(status_code=502, detail=f"oauth_discovery_failed: {_exc_summary(exc)}")
    except SSRFRejected as exc:
        # The discovery target (or an endpoint it advertised) resolved to a
        # blocked address — an admin-actionable configuration problem, not an
        # internal error (Devin Review on #1124).
        raise HTTPException(status_code=400, detail=f"oauth_endpoint_rejected: {_exc_summary(exc)}")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"oauth_discovery_failed: {_exc_summary(exc)}")

    # Same "keep what's on file" rule the manual PUT carries, for the same
    # reason: a write of this row must not drop a field just because the
    # inbound side had nothing to say about it. RFC 7591 does not require an
    # AS to re-issue anything on a deduped registration, so an AS that hands
    # back the SAME client_id may answer with no client_secret and no
    # registration access token — passing those through wipes both. Losing the
    # RAT silently disables upstream deregistration (best_effort_revoke_
    # registration no-ops without one); losing the secret is worse, demoting a
    # confidential registration to a public one so _client_auth_kwargs stops
    # sending Basic auth and every token call fails. A DIFFERENT client_id is
    # a new registration and replaces both wholesale (Devin Review on #1124
    # for the RAT; the secret has the same exposure).
    # Same predicate as the manual PUT and as the token purge below: discovery
    # can hand back new endpoints for an unchanged client_id, and a secret
    # minted by the old authorization server must not be paired with the new
    # one (Devin Review on #1124).
    same_client = bool(existing) and not _oauth_identity_changed(
        existing,
        issuer=registered.issuer,
        authorization_endpoint=registered.authorization_endpoint,
        token_endpoint=registered.token_endpoint,
        client_id=registered.client_id,
    )
    keep_secret = registered.client_secret
    if keep_secret is None and same_client:
        keep_secret = existing.get("client_secret")
        if keep_secret is None and existing.get("client_secret_present"):
            # Stored ciphertext we can no longer open (vault key rotated).
            # Unlike the manual PUT, refusing here is not an option — the DCR
            # call upstream has already happened, so a 409 would strand it.
            # Say so instead of demoting the registration in silence.
            logger.warning(
                "mcp oauth re-register: source %s keeps client_id %s but its stored client secret is "
                "undecryptable and the AS returned none — the registration is being written as a public "
                "client. Re-enter the secret via PUT .../oauth/client if it is a confidential one.",
                source_id,
                registered.client_id,
            )
    keep_rat = registered.registration_access_token
    if keep_rat is None and same_client:
        keep_rat = existing.get("registration_access_token")
        if keep_rat is None and existing.get("registration_access_token_present"):
            # Present but unreadable under the current vault key. `get()` cannot
            # distinguish that from "no token", so writing the decrypted None
            # back would destroy still-valid ciphertext and silently disable
            # upstream deregistration forever — best_effort_revoke_registration
            # returns early without a token. Preserve the column instead
            # (Devin Review on #1124).
            keep_rat = KEEP_STORED
    # The one operator-triggerable failure of the write below is a missing
    # vault key. Check it here so a request that is going to 409 cannot first
    # destroy everyone's tokens (the write itself still raises, for the race).
    # Two things the check must NOT do, both of which would refuse a request
    # that works: fire in local-dev mode, where encrypt_secret falls back to
    # the ephemeral key on purpose, and fire for a public PKCE client, which
    # stores no secret material and so never reaches encrypt_secret at all
    # (Devin Review on #1124).
    if (keep_secret or keep_rat) and not can_store_secrets():
        raise HTTPException(
            status_code=409,
            detail="vault_key_not_configured: set AGNES_VAULT_KEY on the server before storing secrets",
        )
    # Ordering, deliberately: the purge runs BEFORE the client row is written.
    # There is no transaction spanning mcp_source_oauth_clients and
    # mcp_user_oauth_tokens, so a failure between them has to land somewhere,
    # and purge-last lands it in the dangerous place — the client row already
    # addressing the NEW authorization server while the OLD server's refresh
    # tokens are still on file. connectors/mcp/client.py's refresh path reads
    # token_endpoint / client_id / client_secret straight off that fresh row,
    # so the next forward POSTs the old server's refresh token, and the client
    # secret via Basic auth, to the new host. Purge-first leaves the row
    # untouched instead: users re-connect, nothing is disclosed. Same rule and
    # same reasoning as the source `url` repoint in update_mcp_source
    # (Devin Review on #1124).
    # A stored token is only usable against the exact (issuer, endpoints,
    # client_id) it was minted for, so ANY change to that tuple strands it —
    # discovery can hand back new endpoints for the same client_id just as
    # easily as a new client_id. They keep working until they expire, then
    # refresh against whatever is now on the row: a spec-compliant AS answers
    # invalid_grant (clean reconnect prompt) but others answer invalid_client,
    # which is not classified as invalid_grant — the user would be stuck on an
    # opaque upstream 401 indefinitely. Drop them so the next call asks for a
    # reconnect straight away (Devin Review on #1124). Same predicate as the
    # manual PUT below; keep the two in step.
    if existing and _oauth_identity_changed(
        existing,
        issuer=registered.issuer,
        authorization_endpoint=registered.authorization_endpoint,
        token_endpoint=registered.token_endpoint,
        client_id=registered.client_id,
    ):
        from src.repositories import mcp_oauth_flows_repo, mcp_user_oauth_tokens_repo

        mcp_user_oauth_tokens_repo().delete_for_source(source_id)
        mcp_oauth_flows_repo().delete_for_source(source_id)
    try:
        clients_repo.upsert(
            source_id,
            issuer=registered.issuer,
            client_id=registered.client_id,
            authorization_endpoint=registered.authorization_endpoint,
            token_endpoint=registered.token_endpoint,
            client_secret=keep_secret,
            registration_access_token=keep_rat,
            scopes=registered.scopes,
        )
    except VaultKeyNotConfiguredError as exc:
        # The DCR registration already happened upstream; the next successful
        # register re-registers and best-effort revokes it. Same actionable
        # 409 the other credential endpoints give (Devin Review on #1124).
        raise HTTPException(
            status_code=409,
            detail="vault_key_not_configured: set AGNES_VAULT_KEY on the server before storing secrets",
        ) from exc
    _audit(
        conn,
        user["id"],
        "mcp_oauth.client_register",
        f"mcp_source:{source_id}",
        {
            "method": "dcr",
            "issuer": registered.issuer,
            "client_id": registered.client_id,
            "re_registered": bool(existing),
        },
    )
    fresh = clients_repo.get(source_id)
    return _serialize_oauth_client(fresh) if fresh else {"source_id": source_id}


@router.put("/mcp-sources/{source_id}/oauth/client")
async def set_oauth_client_config(
    source_id: str,
    payload: OAuthClientConfigRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Manual OAuth client configuration — the escape hatch for an
    authorization server without dynamic client registration (spec §2).

    Same https + SSRF-safe validation as the discovered path, even though
    no outbound call happens here: these endpoint URLs get dialed for real
    at token exchange/refresh time, so a bad (internal/metadata) URL must
    be rejected at configuration time, not silently accepted and only
    caught when a real caller triggers a forward.
    """
    from src.net.ssrf_safe_client import resolve_safe
    from src.repositories import mcp_source_oauth_clients_repo

    src_repo = mcp_sources_repo()
    src = src_repo.get(source_id)
    if not src:
        raise HTTPException(status_code=404, detail="mcp_source_not_found")
    _require_oauth_source(src)

    for field, url in (
        ("authorization_endpoint", payload.authorization_endpoint),
        ("token_endpoint", payload.token_endpoint),
    ):
        # resolve_safe() does a BLOCKING socket.getaddrinfo(); this is an
        # `async def` handler, so calling it inline stalls the whole process's
        # event loop for the resolver timeout, twice per save. Same hazard —
        # and same remedy — as SSRFGuardAsyncTransport (Devin Review on #1124).
        ok, reason, _ip = await asyncio.to_thread(resolve_safe, url, https_only=True)
        if not ok:
            raise HTTPException(status_code=400, detail=f"{field} failed SSRF/https validation: {reason}")

    issuer = payload.issuer or _url_origin(payload.authorization_endpoint)

    clients_repo = mcp_source_oauth_clients_repo()
    # A PUT that leaves the registration's IDENTITY alone is a tweak (scopes,
    # a re-typed secret) — keep the stored secret and registration access
    # token, since a form re-save has nothing to resubmit for a write-only
    # field. Anything that moves the identity replaces the registration, and
    # the old credentials belong to the old one.
    #
    # "Identity" is the same four fields the token purge below uses, NOT
    # client_id alone. Keying retention on client_id let the two disagree for
    # the request where it matters most: repointing issuer/token_endpoint at a
    # DIFFERENT authorization server while re-typing the same client name
    # purged the user tokens (identity changed) yet kept the previous
    # provider's client secret — which _client_auth_kwargs then sends as HTTP
    # Basic to the new token_endpoint, and best_effort_revoke_registration
    # bearer-sends the retained registration token to the new provider too
    # (Devin Review on #1124).
    existing = clients_repo.get(source_id)
    same_client = bool(existing) and not _oauth_identity_changed(
        existing,
        issuer=issuer,
        authorization_endpoint=payload.authorization_endpoint,
        token_endpoint=payload.token_endpoint,
        client_id=payload.client_id,
    )
    keep_rat = existing.get("registration_access_token") if same_client else None
    if keep_rat is None and same_client and existing.get("registration_access_token_present"):
        # See register_oauth_client: a decrypted None cannot round-trip a
        # column the current vault key can no longer open, and overwriting it
        # loses the only means of deregistering upstream.
        keep_rat = KEEP_STORED
    # The secret is write-only over the API (GET never echoes it), so a form
    # re-saving scopes has nothing to resubmit and arrives with
    # client_secret=None. Treat that as "leave it alone" for an UNCHANGED
    # identity — passing it through wipes client_secret_enc and breaks every
    # user's refresh (Devin Review on #1124). An explicit "" still clears it
    # for a public PKCE-only client, and a changed identity replaces the
    # registration and its secret wholesale.
    client_secret = payload.client_secret
    if client_secret is None and same_client:
        client_secret = existing.get("client_secret")
        if client_secret is None and existing.get("client_secret_present"):
            # The column holds ciphertext we can no longer open (vault key
            # rotated). Carrying the decrypted None forward would write NULL
            # and silently demote a confidential registration to a public
            # PKCE-only one — _client_auth_kwargs would stop sending Basic
            # auth. Refusing is the honest move: the admin either re-enters
            # the secret or clears it deliberately (Devin Review on #1124).
            raise HTTPException(
                status_code=409,
                detail=(
                    "client_secret_undecryptable: a client secret is stored for this source but "
                    "cannot be decrypted with the current vault key. Re-send it explicitly to "
                    'replace it, or send "" to clear it and convert this to a public client.'
                ),
            )
    # See the same check in register_oauth_client: match the predicate
    # encrypt_secret actually guards on (a key, OR local dev), and skip it
    # entirely when there is no secret material to encrypt.
    if (client_secret or keep_rat) and not can_store_secrets():
        raise HTTPException(
            status_code=409,
            detail="vault_key_not_configured: set AGNES_VAULT_KEY on the server before storing secrets",
        )
    # Ordering, deliberately: the purge runs BEFORE the client row is written.
    # There is no transaction spanning mcp_source_oauth_clients and
    # mcp_user_oauth_tokens, so a failure between them has to land somewhere,
    # and purge-last lands it in the dangerous place — the client row already
    # addressing the NEW authorization server while the OLD server's refresh
    # tokens are still on file. connectors/mcp/client.py's refresh path reads
    # token_endpoint / client_id / client_secret straight off that fresh row,
    # so the next forward POSTs the old server's refresh token, and the client
    # secret via Basic auth, to the new host. Purge-first leaves the row
    # untouched instead: users re-connect, nothing is disclosed. Same rule and
    # same reasoning as the source `url` repoint in update_mcp_source
    # (Devin Review on #1124). ANY change to the (issuer, endpoints, client_id)
    # tuple strands the tokens, not just a client_id swap — repointing
    # token_endpoint while keeping client_id is the dangerous case.
    if existing and _oauth_identity_changed(
        existing,
        issuer=issuer,
        authorization_endpoint=payload.authorization_endpoint,
        token_endpoint=payload.token_endpoint,
        client_id=payload.client_id,
    ):
        from src.repositories import mcp_oauth_flows_repo, mcp_user_oauth_tokens_repo

        mcp_user_oauth_tokens_repo().delete_for_source(source_id)
        mcp_oauth_flows_repo().delete_for_source(source_id)
    try:
        clients_repo.upsert(
            source_id,
            issuer=issuer,
            client_id=payload.client_id,
            authorization_endpoint=payload.authorization_endpoint,
            token_endpoint=payload.token_endpoint,
            client_secret=client_secret,
            registration_access_token=keep_rat,
            scopes=payload.scopes,
        )
    except VaultKeyNotConfiguredError as exc:
        raise HTTPException(
            status_code=409,
            detail="vault_key_not_configured: set AGNES_VAULT_KEY on the server before storing secrets",
        ) from exc
    _audit(
        conn,
        user["id"],
        "mcp_oauth.client_register",
        f"mcp_source:{source_id}",
        {"method": "manual", "issuer": issuer, "client_id": payload.client_id},
    )
    fresh = clients_repo.get(source_id)
    return _serialize_oauth_client(fresh) if fresh else {"source_id": source_id}


# ---------------------------------------------------------------------------
# Source actions — introspect / classify / test / materialize
# ---------------------------------------------------------------------------


class PreviewIntrospectRequest(BaseModel):
    """A connection the admin has typed but not yet registered."""

    transport: str
    url: Optional[str] = None
    command: Optional[str] = None
    args: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None
    auth_method: Optional[str] = None
    auth_secret_env: Optional[str] = None

    @field_validator("transport")
    @classmethod
    def _check_transport(cls, v: str) -> str:
        return _validate_transport(v)

    @field_validator("auth_method")
    @classmethod
    def _check_auth_method(cls, v: Optional[str]) -> Optional[str]:
        return _validate_auth_method(v) if v is not None else None


@router.post("/mcp-sources/preview-introspect")
async def preview_introspect_mcp_source(
    payload: PreviewIntrospectRequest,
    user: dict = Depends(require_admin),
):
    """Dial a connection the admin has typed and list its tools. Writes nothing.

    The builder needs to show what a server exposes BEFORE the admin presses
    Save — you cannot sensibly choose which tools to grant, or name the source,
    without seeing them. The alternative was registering a disabled row first
    and introspecting that, which puts a source in the list that the admin
    never agreed to create.

    Same shape as ``POST /entities/preview``: the values that would be saved
    go in, a verdict comes back, and the temp state is gone before the response
    is. It builds a row-shaped dict and runs the SAME guard and the SAME
    introspection the registered path does — a probe dials with a credential
    attached, so it takes the configuration-time url check whether or not a row
    exists yet.

    The secret is NOT in this payload and never should be: ``auth_secret_env``
    names an environment variable, and the credential resolves out of the vault
    exactly as it does for a registered source. A connection whose secret is not
    stored yet fails here with the reason, which is the truthful answer — store
    it, then check.
    """
    from connectors.mcp import extractor as mcp_extractor
    from connectors.mcp.client import exc_summary as _exc_summary

    row = {
        "id": "",
        "name": "(preview)",
        "transport": payload.transport,
        "url": payload.url or "",
        "command": payload.command or "",
        "args": payload.args or [],
        "env": payload.env or {},
        "auth_method": payload.auth_method or "",
        "auth_secret_env": payload.auth_secret_env or "",
        "scope": "shared",
        "enabled": True,
    }
    await _check_source_url_or_400(row)
    try:
        tools = await mcp_extractor.introspect_source_async(row, caller_user_id=user["id"])
    except Exception as exc:
        logger.warning("preview introspect failed: %s", _exc_summary(exc))
        raise HTTPException(status_code=502, detail=f"introspect_failed: {_exc_summary(exc)}")
    return {"tools": tools}


@router.post("/mcp-sources/{source_id}/introspect")
async def introspect_mcp_source(
    source_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Live-connect to the source and list its tools verbatim."""
    from connectors.mcp import extractor as mcp_extractor
    from connectors.mcp.client import exc_summary as _exc_summary

    src_repo = mcp_sources_repo()
    src = src_repo.get(source_id)
    if not src:
        raise HTTPException(status_code=404, detail="mcp_source_not_found")
    # A probe DIALS the source, with a credential attached, exactly as a
    # forward does — so it takes the same configuration-time check. Without
    # this the admin probes were the one path that could reach a url the
    # guard refuses: the two runtime forwards re-fetch the row and stop on
    # `enabled`, but these do not, so "a disabled source is never dialed"
    # held for the forwards and not for here (Devin Review on #1204).
    await _check_source_url_or_400(src)
    try:
        # introspect_source_async — async-safe; the sync variant calls
        # asyncio.run() which blows up inside FastAPI's running loop.
        tools = await mcp_extractor.introspect_source_async(src, caller_user_id=_probe_caller_user_id(src, user))
    except Exception as exc:
        logger.exception("introspect failed for source %s", source_id)
        raise HTTPException(status_code=502, detail=f"introspect_failed: {_exc_summary(exc)}")
    _audit(
        conn,
        user["id"],
        "mcp_source.introspect",
        f"mcp_source:{source_id}",
        {"tool_count": len(tools)},
    )
    return {"tools": tools}


@router.post("/mcp-sources/{source_id}/classify")
async def classify_mcp_source(
    source_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Introspect + run heuristic classifier; return per-tool proposals."""
    from connectors.mcp.client import exc_summary as _exc_summary

    src_repo = mcp_sources_repo()
    src = src_repo.get(source_id)
    if not src:
        raise HTTPException(status_code=404, detail="mcp_source_not_found")
    # A probe DIALS the source, with a credential attached, exactly as a
    # forward does — so it takes the same configuration-time check. Without
    # this the admin probes were the one path that could reach a url the
    # guard refuses: the two runtime forwards re-fetch the row and stop on
    # `enabled`, but these do not, so "a disabled source is never dialed"
    # held for the forwards and not for here (Devin Review on #1204).
    await _check_source_url_or_400(src)
    try:
        from connectors.mcp.client import list_tools_async as _list_tools_async

        tool_infos = await _list_tools_async(src, caller_user_id=_probe_caller_user_id(src, user))
    except Exception as exc:
        logger.exception("classify (list_tools) failed for source %s", source_id)
        raise HTTPException(status_code=502, detail=f"introspect_failed: {_exc_summary(exc)}")
    proposals = mcp_classifier.classify_all(tool_infos)
    _audit(
        conn,
        user["id"],
        "mcp_source.classify",
        f"mcp_source:{source_id}",
        {"tool_count": len(proposals)},
    )
    return {
        "proposals": [
            {
                "name": p.name,
                "suggested_mode": p.suggested_mode,
                "reason": p.reason,
                "description": p.description,
                "input_schema": p.input_schema,
            }
            for p in proposals
        ]
    }


@router.post("/mcp-sources/{source_id}/test")
async def test_mcp_source(
    source_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Lightweight connectivity probe. Returns ``{ok, tool_count, error}``;
    HTTP 200 even on connect failure so the UI can render the diagnostic."""
    from connectors.mcp import extractor as mcp_extractor
    from connectors.mcp.client import exc_summary as _exc_summary

    src_repo = mcp_sources_repo()
    src = src_repo.get(source_id)
    if not src:
        raise HTTPException(status_code=404, detail="mcp_source_not_found")
    # This probe dials like the others, so it must not reach a refused url —
    # but it REPORTS rather than refuses. The contract above ("HTTP 200 even on
    # connect failure so the UI can render the diagnostic") is the whole point
    # of the button: an admin presses it to find out what is wrong. Answering a
    # refused url with a 400 would withhold the one answer they came for, which
    # is worse than either the unguarded version or a plain refusal. So the
    # verdict becomes the diagnostic, and nothing is dialed
    # (Devin Review on #1204).
    verdict = await _source_url_verdict(src)
    if verdict is not None and not verdict.ok:
        result = {"ok": False, "tool_count": 0, "error": f"url failed validation: {verdict.reason}"}
    else:
        try:
            tools = await mcp_extractor.introspect_source_async(src, caller_user_id=_probe_caller_user_id(src, user))
            result = {"ok": True, "tool_count": len(tools), "error": None}
        except Exception as exc:
            summary = _exc_summary(exc)
            logger.warning("test connection failed for source %s: %s", source_id, summary)
            result = {"ok": False, "tool_count": 0, "error": summary}
    _audit(
        conn,
        user["id"],
        "mcp_source.test",
        f"mcp_source:{source_id}",
        {"ok": result["ok"], "tool_count": result["tool_count"]},
    )
    return result


@router.post("/mcp-sources/{source_id}/materialize")
async def materialize_mcp_source(
    source_id: str,
    payload: Optional[MaterializeRequest] = None,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Run the extractor for this source (optionally restricted to one tool).

    Returns the extractor's summary dict (source_name, extract_duckdb path,
    tables, errors). Use the SyncOrchestrator's next rebuild to attach.
    """
    from connectors.mcp import extractor as mcp_extractor

    src_repo = mcp_sources_repo()
    src = src_repo.get(source_id)
    if not src:
        raise HTTPException(status_code=404, detail="mcp_source_not_found")
    # The FOURTH connector helper, and it dials with the source's real
    # credential exactly as the other three do. The module header above names
    # the group — "introspect/classify/test/materialize" — and the first three
    # took this guard while this one did not, which is the whole failure mode:
    # the reasoning was written against a remembered list rather than the code
    # (/agnes-review rbac reviewer on #1204).
    #
    # `extract_source_async` refuses a DISABLED source on its own, so the
    # disabled-row exemption in `update_mcp_source` is covered. What was not is
    # an ENABLED legacy row carrying a url this policy refuses — registered
    # before the guard existed, unreachable through introspect/classify/test,
    # and still fully dialable here on every run.
    await _check_source_url_or_400(src)
    only_tool_id = payload.tool_id if payload else None
    try:
        # extract_source_async — async-safe; the sync variant wraps
        # ``_materialize_one_tool`` with asyncio.run() which blows up
        # inside FastAPI's running event loop (same root cause as the
        # introspect/classify/test handlers above).
        result = await mcp_extractor.extract_source_async(
            system_conn=conn,
            source_id=source_id,
            only_tool_id=only_tool_id,
        )
    except ValueError as exc:
        # source disabled / not found / no list-of-dicts in response, etc.
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("materialize failed for source %s", source_id)
        raise HTTPException(status_code=500, detail=f"materialize_failed: {exc}")
    # Linked data apps: project the lister's FRESHLY materialized table into
    # the data_apps registry as grantable `linked` rows. Two gates, both from
    # `result["tables"]` (what this run actually wrote) rather than mere table
    # presence in the persistent extract file — an `only_tool_id` run for an
    # unrelated tool would otherwise re-project an arbitrarily stale lister
    # table and re-activate rows a previous reconcile had hidden:
    #  * targeted run (`only_tool_id` — the wizard's path): the admin has
    #    explicitly designated THAT tool as the data-app lister, so project
    #    from the table it wrote under its own exposed_name (the extractor
    #    names tables by exposed_name, which is almost never the literal
    #    `keboola_data_apps` — Devin Review on #1116);
    #  * full-source run: no tool is designated, so keep the conservative
    #    documented contract — only a table literally named
    #    `keboola_data_apps` projects.
    # Best-effort — a projection failure must not fail the materialize call.
    try:
        from src.data_apps.keboola_adapter import MATERIALIZED_TABLE
        from src.data_apps.linked_projection import project_from_extract

        freshly_written = {t.get("table") for t in result.get("tables") or []}
        lister_table = None
        projection_map = None
        if only_tool_id and payload is not None and payload.lister:
            # Explicit designation only (the wizard's path) — see the
            # MaterializeRequest.lister comment. A targeted run WITHOUT the
            # flag (per-tool "Materialize now") never projects.
            tool = tool_registry_repo().get(only_tool_id)
            exposed = (tool or {}).get("exposed_name") or ""
            projection_map = (tool or {}).get("projection_map") or None
            if exposed in freshly_written:
                lister_table = exposed
        elif not only_tool_id and MATERIALIZED_TABLE in freshly_written:
            lister_table = MATERIALIZED_TABLE
        proj = None
        if lister_table:
            proj = project_from_extract(
                source_id,
                result.get("extract_duckdb"),
                table_name=lister_table,
                projection_map=projection_map,
            )
        if proj is not None:
            result["linked_projection"] = {
                "created": proj.created,
                "updated": proj.updated,
                "hidden": proj.hidden,
                # The wizard needs both to explain a zero: how many rows it
                # could not read, and which columns it had to choose from.
                "skipped": proj.skipped,
                "columns": list(proj.columns),
                "projection_map": projection_map,
            }
    except Exception:
        logger.exception("linked-app projection failed for source %s", source_id)

    # Counts alone hid the two outcomes an operator actually needs to see: a
    # table whose upstream went empty (its rows just left analytics) and a tool
    # that produced nothing because it has no rows AND no previous snapshot.
    # Record both by name, not just as a tally.
    run_tables = result.get("tables") or []
    run_errors = result.get("errors") or []
    _audit(
        conn,
        user["id"],
        "mcp_source.materialize",
        f"mcp_source:{source_id}",
        {
            "only_tool_id": only_tool_id,
            "table_count": len(run_tables),
            "emptied_tables": [t.get("table") for t in run_tables if t.get("empty_upstream")],
            "error_count": len(run_errors),
            "empty_upstream_tools": [e.get("tool") for e in run_errors if e.get("code") == "empty_upstream"],
        },
    )
    return result


# ---------------------------------------------------------------------------
# Tool CRUD
# ---------------------------------------------------------------------------


@router.post("/mcp-tools", status_code=201)
async def create_mcp_tool(
    payload: CreateToolRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Register a tool row against an existing source.

    The repo enforces mode-specific rules (e.g. materialize requires
    ``schedule``); we surface those as 400s.
    """
    src_repo = mcp_sources_repo()
    if not src_repo.get(payload.source_id):
        raise HTTPException(status_code=404, detail="mcp_source_not_found")
    tool_id = payload.tool_id or str(uuid.uuid4())
    repo = tool_registry_repo()
    if repo.get(tool_id) is not None:
        raise HTTPException(status_code=409, detail="tool_id_exists")
    try:
        repo.upsert(
            tool_id=tool_id,
            source_id=payload.source_id,
            original_name=payload.original_name,
            exposed_name=payload.exposed_name,
            mode=payload.mode,
            table_id=payload.table_id,
            input_schema=payload.input_schema,
            description=payload.description,
            mutating=payload.mutating,
            pii_fields=payload.pii_fields,
            rate_limit_pm=payload.rate_limit_pm,
            schedule=payload.schedule,
            enabled=payload.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except duckdb.ConstraintException as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    _audit(
        conn,
        user["id"],
        "mcp_tool.create",
        f"mcp_tool:{tool_id}",
        {
            "source_id": payload.source_id,
            "exposed_name": payload.exposed_name,
            "mode": payload.mode,
        },
    )
    return {"tool_id": tool_id}


@router.get("/mcp-tools")
async def list_mcp_tools(
    source_id: Optional[str] = None,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """List all tools, optionally restricted to one source."""
    repo = tool_registry_repo()
    rows = repo.list_for_source(source_id) if source_id else repo.list_all()
    return [_serialize_tool(r) for r in rows]


@router.get("/mcp-tools/{tool_id}")
async def get_mcp_tool(
    tool_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Detail view — includes the list of group_ids granted access."""
    repo = tool_registry_repo()
    row = repo.get(tool_id)
    if not row:
        raise HTTPException(status_code=404, detail="mcp_tool_not_found")
    out = _serialize_tool(row)
    out["grants"] = repo.grants_for_tool(tool_id)
    # v121: same grants with their allow_mutating flags. `grants` (bare ids)
    # stays for existing consumers.
    out["grant_rows"] = repo.grant_rows_for_tool(tool_id)
    return out


@router.put("/mcp-tools/{tool_id}")
async def update_mcp_tool(
    tool_id: str,
    payload: UpdateToolRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Partial update. Audit row carries before/after for changed fields."""
    repo = tool_registry_repo()
    existing = repo.get(tool_id)
    if not existing:
        raise HTTPException(status_code=404, detail="mcp_tool_not_found")

    # If source_id is being changed, validate the new source exists.
    if payload.source_id and payload.source_id != existing.get("source_id"):
        if not mcp_sources_repo().get(payload.source_id):
            raise HTTPException(status_code=404, detail="mcp_source_not_found")

    merged = _merge_tool_patch(existing, payload)
    before = {k: existing.get(k) for k in ("source_id", "exposed_name", "mode", "schedule", "enabled")}
    try:
        repo.upsert(**merged)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except duckdb.ConstraintException as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    fresh = repo.get(tool_id)
    after = {k: (fresh or {}).get(k) for k in ("source_id", "exposed_name", "mode", "schedule", "enabled")}
    _audit(
        conn,
        user["id"],
        "mcp_tool.update",
        f"mcp_tool:{tool_id}",
        {"after": after},
        params_before={"before": before},
    )
    return _serialize_tool(fresh) if fresh else {"tool_id": tool_id}


_PROJECTION_FIELDS = ("id", "url", "name", "description")


class ProjectionMapRequest(BaseModel):
    """Which materialized columns carry a linked app's id / url / name.

    A separate endpoint from ``PUT /mcp-tools/{id}`` on purpose: the admin
    picks these AFTER a fetch, against the columns the tool actually emitted,
    and clearing the mapping has to be expressible — which the tool-update
    path cannot do (its upsert preserves the mapping precisely so a reclassify
    does not wipe it).
    """

    projection_map: Optional[Dict[str, str]] = None


@router.put("/mcp-tools/{tool_id}/projection-map")
async def set_mcp_tool_projection_map(
    tool_id: str,
    payload: ProjectionMapRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Set (or clear, with a null map) the tool's projection column mapping."""
    repo = tool_registry_repo()
    existing = repo.get(tool_id)
    if not existing:
        raise HTTPException(status_code=404, detail="mcp_tool_not_found")

    mapping = payload.projection_map
    if mapping is not None:
        unknown = sorted(set(mapping) - set(_PROJECTION_FIELDS))
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"unknown projection field(s): {', '.join(unknown)}; expected {', '.join(_PROJECTION_FIELDS)}",
            )
        # Drop blanks rather than storing them: an empty column name is a
        # choice nobody made, and `map_row` treats a named column as
        # authoritative — storing "" would pin a field to nothing.
        mapping = {k: v.strip() for k, v in mapping.items() if isinstance(v, str) and v.strip()}
        mapping = mapping or None

    repo.set_projection_map(tool_id, mapping)
    _audit(
        conn,
        user["id"],
        "mcp_tool.projection_map",
        f"mcp_tool:{tool_id}",
        {"after": mapping},
        params_before={"before": existing.get("projection_map")},
    )
    fresh = repo.get(tool_id)
    return _serialize_tool(fresh) if fresh else {"tool_id": tool_id}


@router.delete("/mcp-tools/{tool_id}", status_code=204)
async def delete_mcp_tool(
    tool_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Hard delete — cascades grants via the repo."""
    repo = tool_registry_repo()
    existing = repo.get(tool_id)
    if not existing:
        raise HTTPException(status_code=404, detail="mcp_tool_not_found")
    grant_count = len(repo.grants_for_tool(tool_id))
    repo.delete(tool_id)
    _audit(
        conn,
        user["id"],
        "mcp_tool.delete",
        f"mcp_tool:{tool_id}",
        {
            "source_id": existing.get("source_id"),
            "exposed_name": existing.get("exposed_name"),
            "grant_count": grant_count,
        },
    )


# ---------------------------------------------------------------------------
# Tool grants
# ---------------------------------------------------------------------------


@router.post("/mcp-sources/{source_id}/grants")
async def add_mcp_source_grant(
    source_id: str,
    payload: AddGrantRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Grant a user group every tool registered under one source.

    The per-tool grant is the right granularity for curating an upstream a
    handful of tools at a time. It is the wrong one for a source that arrives
    with its whole toolset at once — a connected Keboola project registers
    around forty, and granting them one page at a time is the friction the
    chat-tools switch exists to remove. This grants the set.

    Idempotent per tool, and it does NOT widen anything the source did not
    register: the set is exactly ``tool_registry`` rows for this source, so a
    tool that arrives later needs another call rather than being granted in
    advance. Returns what changed and what was already granted, because
    "granted 0 of 37" and "granted 37 of 37" mean very different things to an
    admin and both look like success otherwise.
    """
    src = mcp_sources_repo().get(source_id)
    if src is None:
        raise HTTPException(status_code=404, detail="mcp_source_not_found")
    if payload.allow_mutating is not None:
        # Shared body model with the per-tool endpoint — but the bulk grant is
        # deliberately read-only (see AddGrantRequest). Silently ignoring the
        # field would tell an admin they opened write access across a server
        # when nothing was opened; refuse loudly and point at the per-tool
        # opt-in instead.
        raise HTTPException(
            status_code=400,
            detail={
                "error": "allow_mutating_not_supported_here",
                "message": (
                    "The source-wide grant is read-only by design. Opt groups into "
                    "mutating tools one at a time: POST /api/admin/mcp-tools/{tool_id}/grants "
                    "with allow_mutating, or `agnes admin mcp tool grant <tool_id> "
                    "--group <g> --allow-mutating`."
                ),
            },
        )
    group_id = (payload.group_id or "").strip()
    if not group_id:
        raise HTTPException(status_code=400, detail="group_id is required")

    from src.repositories import user_groups_repo

    if not user_groups_repo().get(group_id):
        raise HTTPException(status_code=404, detail="user_group_not_found")

    repo = tool_registry_repo()
    # Only ENABLED tools. A disabled row is invisible to the agent today
    # (`list_by_mode(..., enabled_only=True)` builds the passthrough surface),
    # so granting it writes an access decision nobody can see the effect of —
    # and re-enabling the tool later makes it reachable for the granted group
    # without anyone granting again. Grant narrow. Revoking, below, stays wide
    # on purpose: taking access away must not skip a row because it happens to
    # be switched off. (Devin Review on this PR.)
    all_tools = repo.list_for_source(source_id)
    tools = [t for t in all_tools if t.get("enabled", True)]
    skipped_disabled = len(all_tools) - len(tools)
    if not tools:
        # Saying "granted" over an empty set would report success for a source
        # whose tools were never registered — the failure this whole feature
        # keeps running into.
        raise HTTPException(
            status_code=409,
            detail={
                "error": "no_tools_registered",
                "message": (
                    f"Source {source_id!r} has no enabled tools to grant"
                    + (f" ({skipped_disabled} are disabled)" if skipped_disabled else "")
                    + ". Register them first (for a connected project, turn chat "
                    "tools on), then grant."
                ),
            },
        )

    granted, already = [], []
    for tool in tools:
        tool_id = tool["tool_id"]
        if group_id in repo.grants_for_tool(tool_id):
            already.append(tool_id)
            continue
        repo.add_grant(tool_id, group_id)
        granted.append(tool_id)

    _audit(
        conn,
        user["id"],
        "mcp_source.grant.add",
        f"mcp_source:{source_id}",
        {
            "group_id": group_id,
            "granted": len(granted),
            "already_granted": len(already),
            "skipped_disabled": skipped_disabled,
        },
    )
    return {
        "granted": len(granted),
        "already_granted": len(already),
        "total": len(tools),
        # This bulk grant is deliberately read-only: a `mutating=True` tool
        # stays unreachable for the group until the admin opts it in per tool
        # (POST /mcp-tools/{id}/grants with allow_mutating=true — v121). On an
        # upstream that annotates nothing that is ALL of them, so reporting
        # only "granted 37 of 37" would promise the group an access they do
        # not have. Same reasoning as `tools_admin_only` on enable.
        "admin_only": sum(1 for t in tools if t.get("mutating")),
        # Said out loud rather than silently omitted: "granted 5 of 5" over a
        # source with three switched-off tools reads as complete coverage.
        "skipped_disabled": skipped_disabled,
    }


@router.delete("/mcp-sources/{source_id}/grants/{group_id}", status_code=204)
async def remove_mcp_source_grant(
    source_id: str,
    group_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Revoke a group's access to every tool of one source (idempotent).

    The counterpart matters more than the convenience: an admin who granted a
    whole project in one action must be able to take it back in one action,
    rather than revoking forty tools by hand while access stays live.
    """
    if mcp_sources_repo().get(source_id) is None:
        raise HTTPException(status_code=404, detail="mcp_source_not_found")
    repo = tool_registry_repo()
    removed = 0
    for tool in repo.list_for_source(source_id):
        if group_id in repo.grants_for_tool(tool["tool_id"]):
            repo.remove_grant(tool["tool_id"], group_id)
            removed += 1
    _audit(
        conn,
        user["id"],
        "mcp_source.grant.remove",
        f"mcp_source:{source_id}",
        {"group_id": group_id, "removed": removed},
    )


@router.post("/mcp-tools/{tool_id}/grants")
async def add_mcp_tool_grant(
    tool_id: str,
    payload: AddGrantRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Grant a user group access to the tool. Idempotent; re-POSTing an
    existing grant with an explicit ``allow_mutating`` updates the flag in
    place (the flag is part of the grant, so the re-POST is the edit path),
    while omitting it leaves an existing grant's flag untouched."""
    repo = tool_registry_repo()
    if not repo.get(tool_id):
        raise HTTPException(status_code=404, detail="mcp_tool_not_found")
    group_id = (payload.group_id or "").strip()
    if not group_id:
        raise HTTPException(status_code=400, detail="group_id is required")
    # Validate the group exists so we don't dangle FK-less rows. Backend-aware:
    # user_groups lives in the active backend (Postgres on a PG instance).
    from src.repositories import user_groups_repo

    if not user_groups_repo().get(group_id):
        raise HTTPException(status_code=404, detail="user_group_not_found")
    try:
        repo.add_grant(tool_id, group_id, allow_mutating=payload.allow_mutating)
    except duckdb.ConstraintException as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    # Report the STORED flag, not the request's: with allow_mutating omitted
    # (None) an existing grant keeps whatever it had.
    stored = next(
        (g["allow_mutating"] for g in repo.grant_rows_for_tool(tool_id) if g["group_id"] == group_id),
        False,
    )
    _audit(
        conn,
        user["id"],
        "mcp_tool.grant.add",
        f"mcp_tool:{tool_id}",
        {"group_id": group_id, "allow_mutating": stored},
    )
    return {
        "granted": True,
        "tool_id": tool_id,
        "group_id": group_id,
        "allow_mutating": stored,
    }


@router.delete("/mcp-tools/{tool_id}/grants/{group_id}", status_code=204)
async def remove_mcp_tool_grant(
    tool_id: str,
    group_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Revoke a group grant. Idempotent (DELETE missing row is a no-op)."""
    repo = tool_registry_repo()
    if not repo.get(tool_id):
        raise HTTPException(status_code=404, detail="mcp_tool_not_found")
    repo.remove_grant(tool_id, group_id)
    _audit(
        conn,
        user["id"],
        "mcp_tool.grant.remove",
        f"mcp_tool:{tool_id}",
        {"group_id": group_id},
    )
