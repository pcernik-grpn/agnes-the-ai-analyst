"""User-facing REST for per-user MCP source secrets (RFC #461 §4 phase B).

Each analyst stores their own credential for upstream MCP sources whose
``scope='per_user'`` (Notion / Slack / Linear OAuth tokens). When the
caller invokes a passthrough tool on such a source, the server forwards
under the analyst's identity rather than a shared server-wide secret.

Endpoints (all under ``/api/mcp/sources/{source_id}/my-secret``):

* ``PUT``    — store / rotate the caller's secret for this source
* ``DELETE`` — drop the caller's secret (call falls back to shared)
* ``GET``    — booleans only — ``{"has_secret": bool}``. We never
               return the cleartext, even to its owner; rotation is
               write-only.

For ``scope='shared'`` sources we still accept the PUT (operators may
flip scope later) but warn the caller that the value won't be used
until scope flips.

Every route here is human-only (``deny_principal``): the credential is the
*owner's*, and each route either reads its metadata, writes it, or opens a
live upstream connection under it. An agent-session token must not do any of
those on its owner's behalf — least of all ``/test``, which would connect to
an arbitrary source with the owner's credential, sidestepping the agent's
``connection`` scope entirely (V1d Task 5).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.dependencies import get_current_user
from app.chat.session_principal_guard import deny_principal
from app.secrets_vault import VaultKeyNotConfiguredError
from src.audit_helpers import log_safe
from src.repositories import mcp_sources_repo, per_user_secrets_repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mcp/sources", tags=["mcp-user-secrets"])


class MySecretBody(BaseModel):
    value: str


class HasSecretResponse(BaseModel):
    has_secret: bool
    source_scope: str  # 'shared' | 'per_user'
    updated_at: str | None = None  # ISO-8601 of last set; None when not connected
    # 2026-07-30 outbound MCP OAuth sources spec §3/§5: one status model for
    # both credential kinds. 'secret' (default) covers every pre-existing
    # source unchanged; 'oauth' sources report against
    # ``mcp_user_oauth_tokens`` instead of ``mcp_user_secrets`` — has_secret
    # means "has a stored token", updated_at is the token row's last write,
    # and expires_at is new (always None for 'secret' — a pasted token has
    # no known expiry).
    auth_kind: str = "secret"  # 'secret' | 'oauth'
    expires_at: str | None = None


def _require_source_grant(source_id: str, user: dict) -> None:
    """403 unless the caller is granted at least one tool on ``source_id``.

    Uses the same ``_visible_passthrough_tools`` intersection as the connect
    page and the ``/test`` endpoint (admin short-circuits to all sources), so a
    user can only read or manage their own credential for a source they're
    actually entitled to use — an ungranted caller can't probe an arbitrary
    source's existence / scope / connection timestamp, nor store a token
    against it.

    Admins short-circuit unconditionally — NOT via ``_visible_passthrough_tools``,
    whose admin path lists sources through their registered passthrough tools.
    A freshly registered source has no tools until the first introspect, and the
    register→connect-your-token→introspect flow needs the connect step to work
    exactly then (the admin connect probes run under the admin's own credential
    for per_user sources)."""
    from app.api.mcp_passthrough import _visible_passthrough_tools
    from app.api.mcp_policy import caller_authority

    if caller_authority(user).is_admin:
        return
    granted_source_ids = {t["source_id"] for t in _visible_passthrough_tools(user)}
    if source_id not in granted_source_ids:
        raise HTTPException(status_code=403, detail="not_granted")


@router.put("/{source_id}/my-secret", status_code=204)
async def set_my_secret(
    source_id: str,
    body: MySecretBody,
    user: dict = Depends(get_current_user),
):
    """Store (or rotate) the caller's per-user secret for this source.

    The value is Fernet-encrypted at rest in ``mcp_user_secrets`` using
    the same vault key as the shared secrets table; if you wonder where
    your token lives, it's in there. Cleartext is never returned.
    """
    deny_principal(user)
    if not body.value:
        raise HTTPException(status_code=400, detail="secret value required")
    if not mcp_sources_repo().get(source_id):
        raise HTTPException(status_code=404, detail="mcp_source_not_found")
    _require_source_grant(source_id, user)
    try:
        per_user_secrets_repo().upsert(source_id, user["id"], body.value)
    except VaultKeyNotConfiguredError as exc:
        raise HTTPException(
            status_code=409,
            detail="vault_key_not_configured: set AGNES_VAULT_KEY on the server before storing secrets",
        ) from exc
    # NEVER include body.value — the secret itself never enters the audit record.
    log_safe(
        user_id=user["id"],
        action="mcp_user_secret.set",
        resource=f"mcp_source:{source_id}",
    )


@router.delete("/{source_id}/my-secret", status_code=204)
async def delete_my_secret(
    source_id: str,
    user: dict = Depends(get_current_user),
):
    """Drop the caller's per-user secret. For ``scope='per_user'``
    sources the next call falls through to the shared vault path."""
    deny_principal(user)
    if not mcp_sources_repo().get(source_id):
        raise HTTPException(status_code=404, detail="mcp_source_not_found")
    try:
        _require_source_grant(source_id, user)
    except HTTPException:
        # Same own-credential carve-out as the oauth disconnect: removing
        # your own stored secret needs no live grant (Devin Review on #1167).
        if not per_user_secrets_repo().has(source_id, user["id"]):
            raise
    per_user_secrets_repo().delete(source_id, user["id"])
    log_safe(
        user_id=user["id"],
        action="mcp_user_secret.clear",
        resource=f"mcp_source:{source_id}",
    )


@router.get("/{source_id}/my-secret", response_model=HasSecretResponse)
async def get_my_secret_status(
    source_id: str,
    user: dict = Depends(get_current_user),
) -> HasSecretResponse:
    """Return ``has_secret: bool`` for the caller + the source's scope so
    a UI can show "Connect your <source>" or "Connected".

    ``auth_method='oauth'`` sources report against ``mcp_user_oauth_tokens``
    instead of the vault-backed ``mcp_user_secrets`` table — same response
    shape, ``auth_kind`` tells the caller which credential kind it's
    reading (spec §3/§5)."""
    deny_principal(user)
    source = mcp_sources_repo().get(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="mcp_source_not_found")
    _require_source_grant(source_id, user)
    log_safe(
        user_id=user["id"],
        action="mcp_user_secret.read",
        resource=f"mcp_source:{source_id}",
    )
    if (source.get("auth_method") or "").lower() == "oauth":
        from app.api.mcp_policy import oauth_connection_usable
        from src.repositories import mcp_user_oauth_tokens_repo

        row = mcp_user_oauth_tokens_repo().get(source_id, user["id"])
        expires_at = row.get("expires_at") if row else None
        updated_at = row.get("updated_at") if row else None
        return HasSecretResponse(
            has_secret=oauth_connection_usable(source_id, user["id"]),
            source_scope=(source.get("scope") or "shared"),
            updated_at=updated_at.isoformat() if updated_at else None,
            auth_kind="oauth",
            expires_at=expires_at.isoformat() if expires_at else None,
        )
    return HasSecretResponse(
        has_secret=per_user_secrets_repo().has(source_id, user["id"]),
        source_scope=(source.get("scope") or "shared"),
        updated_at=per_user_secrets_repo().get_updated_at(source_id, user["id"]),
        auth_kind="secret",
    )


# Explicit positive per-minute cap for the connectivity test. check_rate_limit
# treats None/<=0 as *disabled*, and mcp_sources has no rate_limit_pm column, so
# this must be a literal or the gate silently no-ops. Each test opens a fresh
# upstream connection (a subprocess for stdio transports), so keep it low.
_TEST_CONNECTION_RATE_LIMIT_PM = 6


def _redact_then_truncate(text: str, token: str, limit: int = 300) -> str:
    """Redact the caller's own token from the FULL string first, then truncate.
    Order matters: truncating first could split the token across the boundary so
    the substring match misses it and a fragment leaks."""
    if token:
        text = text.replace(token, "***")
    return text[:limit]


class TestResult(BaseModel):
    ok: bool
    tool_count: int | None = None
    message: str


@router.post("/{source_id}/my-secret/test", response_model=TestResult)
async def test_my_secret(source_id: str, user: dict = Depends(get_current_user)) -> TestResult:
    """Verify the caller's own stored credential works against the upstream.

    Gated in order, all before any upstream call: unknown source → 404; a
    non-per_user (shared) source → 400 (its introspection would run under the
    operator's shared credential — nothing personal to test); no grant on the
    source → 403; over the rate limit → 429; no personal credential → 403 with
    the connect remedy; a disabled source → 409; a url the #1154 policy refuses
    → 400. Only then does it introspect under the caller's token.

    The order is the contract, not an accident, and the last two sit at the end
    deliberately: they read state the caller may not be entitled to, so ahead of
    the grant and rate-limit gates they would let any signed-in user learn
    whether an arbitrary source is switched off and force an unthrottled
    ``getaddrinfo`` per attempt. Keep this list in step with the code — an
    out-of-date one is how a later edit reorders them without anyone noticing
    the contract changed (/agnes-review rbac reviewer on #1204).
    """
    from app.api.mcp_policy import (
        PerUserCredentialMissing,
        RateLimited,
        check_rate_limit,
        enforce_per_user_credential,
    )
    from connectors.mcp.client import list_tools_async

    deny_principal(user)
    source = mcp_sources_repo().get(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="mcp_source_not_found")
    if (source.get("scope") or "shared").lower() != "per_user":
        raise HTTPException(status_code=400, detail="source_scope_not_per_user")
    _require_source_grant(source_id, user)
    try:
        check_rate_limit(source_id, user["id"], _TEST_CONNECTION_RATE_LIMIT_PM)
    except RateLimited as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": str(int(exc.retry_after_seconds) + 1)},
        ) from exc
    try:
        enforce_per_user_credential(source, user["id"])
    except PerUserCredentialMissing as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    # This dials the upstream with the CALLER'S OWN credential, so it takes the
    # same two gates the runtime forwards and the admin probes take: a disabled
    # source is not dialed, and a url the #1154 policy refuses is not dialed.
    # Neither was checked here, which made this the one analyst-facing path that
    # could still reach a refused address — an admin can land one on a row by
    # patching `{url: <refused>, enabled: false}` (the disabled-row exemption in
    # admin_mcp allows that deliberately), and an analyst who reconnects
    # afterwards could then be induced to press Test (Devin Review on #1204).
    #
    # LAST, not first: the docstring's ordering contract is "all gates before
    # any upstream call", and these two are the only ones that read state the
    # caller may not be entitled to. Ahead of the grant check they let any
    # signed-in user learn whether an arbitrary source is switched off and read
    # its address out of the error, and forced an unthrottled getaddrinfo per
    # attempt — ahead of the rate limit, at that.
    # The verdict is NOT echoed to this caller. `verdict.reason` embeds the
    # literal address (`address_in_blocked_range: 169.254.169.254`,
    # `strict_mode_requires_public_address: mcp.internal.example`), and an
    # analyst is not an admin: every other failure branch in this handler
    # already returns a friendly sentence and keeps the cause in the log, and
    # `/me/connections` deliberately surfaces name/transport/hint but never
    # `url`. Echoing it here would have made this the first place a non-admin
    # learns a source's network address (/agnes-review rbac reviewer on #1204).
    from app.api.admin_mcp import _source_url_verdict

    if not source.get("enabled", True):
        # Human copy, not a machine slug: `me_connections.html` renders
        # `body.detail` straight into the card's status line, so a bare
        # `mcp_source_disabled` would reach the analyst as literal snake_case.
        # Same shape as the url-policy 400 below (Devin on #1204).
        raise HTTPException(
            status_code=409,
            detail=(f"{source.get('name') or source_id} is currently turned off by an admin, so it cannot be tested."),
        )
    verdict = await _source_url_verdict(source)
    if verdict is not None and not verdict.ok:
        logger.warning(
            "my-secret/test refused for source %s: url failed validation (%s)",
            source_id,
            verdict.reason,
        )
        raise HTTPException(
            status_code=400,
            detail=f"{source.get('name') or source_id} is not configured correctly. Ask an admin to check it.",
        )

    if (source.get("auth_method") or "").lower() == "oauth":
        # OAuth sources have no ``mcp_user_secrets`` row at all — the value
        # to redact out of a failure message is the stored access token.
        from src.repositories import mcp_user_oauth_tokens_repo

        oauth_row = mcp_user_oauth_tokens_repo().get(source_id, user["id"])
        token = (oauth_row or {}).get("access_token") or ""
    else:
        token = per_user_secrets_repo().get(source_id, user["id"]) or ""
    try:
        tools = await list_tools_async(source, caller_user_id=user["id"])
    except Exception as exc:  # upstream unreachable / bad token
        # Log the sanitized cause for operators; show the user a friendly,
        # actionable line rather than a raw SDK/TaskGroup exception string
        # (e.g. "unhandled errors in a TaskGroup (1 sub-exception)").
        source_name = source.get("name") or source_id
        logger.info(
            "my-secret test failed for source %s: %s",
            source_id,
            _redact_then_truncate(str(exc), token),
        )
        log_safe(
            user_id=user["id"],
            action="mcp_user_secret.test",
            resource=f"mcp_source:{source_id}",
            result="error",
        )
        return TestResult(
            ok=False,
            tool_count=None,
            message=f"Couldn't connect to {source_name}. Check that your token is valid and try again.",
        )
    log_safe(
        user_id=user["id"],
        action="mcp_user_secret.test",
        resource=f"mcp_source:{source_id}",
        result="success",
    )
    return TestResult(ok=True, tool_count=len(tools), message="ok")
