"""Agnes HTTP MCP server — SSE transport for cowork VM access.

Mounted at /api/mcp in app/main.py. Exposes the same server-side tools as
the stdio MCP server but over HTTP, so Claude Desktop's cowork VM (which
cannot reach localhost) can connect when Agnes is deployed with a public URL.

Two HTTP transports, two auth stacks — read this before assuming either
-----------------------------------------------------------------------
Agnes serves the same tool set over two HTTP transports, mounted separately
and authenticated by DIFFERENT middleware. They were documented as if only
this one existed, and a credential that worked here returned ``401
invalid_token`` there:

- ``/api/mcp`` (this module, SSE) sits behind Agnes ``_AuthMiddleware``
  below. It accepts any credential ``resolve_token_to_user`` accepts —
  in practice a PAT (the ``/mcp-connect`` snippets issue one) or a session
  JWT — in the ``Authorization: Bearer`` header, or in the ``?token=`` query
  param for clients that cannot set headers on an SSE GET (operator-
  disablable; see ``_query_param_token_allowed``). A restricted principal
  (co-session / agent-session) is refused by design.
- ``/api/mcp/http`` (``app/api/mcp_streamable.py``, Streamable-HTTP) sits
  behind the MCP SDK's own bearer middleware, whose verifier is
  ``app.auth.mcp_oauth.AgnesMCPOAuthProvider``. It accepts an OAuth 2.1
  access token that provider issued — the flow remote connectors
  (claude.ai, Cursor, VS Code) discover and drive themselves — AND, since
  the PAT contract below is a documented promise that should not depend on
  which transport a client speaks, a plain Agnes PAT
  (``mcp_oauth._access_token_from_pat``). A session JWT is NOT a streamable
  credential; see that function for why the widening stops at ``typ="pat"``.

Auth failures from THIS transport carry a machine-readable ``reason`` beside
the human ``detail`` (see ``_send_auth_error``), and an internal error on the
auth path answers 500, not 401.

Cowork bundle settings.json points to:
    {server_url}/api/mcp/sse

with header  Authorization: Bearer <PAT>  set by Claude Code.

Tools available: the 29 foundation tools registered by
``app/api/mcp/foundation_tools.py`` — server_info, catalog, collections_list,
collection_get, collections_search, knowledge_search, collections_reingest,
schema, describe, query, skills, chat_skills, stack_browse, stack_subscribe,
stack_unsubscribe, store_rate, store_status, store_publish_markdown,
documentation_api, list_contributed_skills, contribute_skill,
delete_contributed_skill, admin_config_surface, admin_source_connections_list,
admin_knowledge_digests_list, admin_knowledge_digest_get,
admin_knowledge_digest_create, admin_knowledge_digest_update,
admin_knowledge_digest_delete.
(query_local and pull require a local analyst filesystem — not available
 in the server context.)
"""

from __future__ import annotations

import contextvars
import logging
import os

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.mcp.foundation_tools import SERVER_INSTRUCTIONS, register_foundation_tools
from app.auth.session_principal import PRINCIPAL_TYPES

logger = logging.getLogger(__name__)

# Per-request token — set by _AuthMiddleware, read by tool handlers.
_current_token: contextvars.ContextVar[str] = contextvars.ContextVar("_mcp_token", default="")
# Per-request caller user id — set by _AuthMiddleware (which already resolves
# the user), read by the passthrough tool closures so a scope='per_user'
# source forwards under the caller's own credential instead of falling back to
# the shared one.
_current_user_id: contextvars.ContextVar[str] = contextvars.ContextVar("_mcp_user_id", default="")

# Internal base URL for self-calls. Stays on HTTP/localhost since the MCP
# server runs inside the same process/container as Agnes.
#
# Devin Review on #474 flagged that reusing ``AGNES_BASE_URL`` (the
# public-facing hostname operators set so Cowork VMs can reach Agnes)
# made every self-call here round-trip through the public proxy
# (TLS + reverse-proxy + DNS), adding latency and breaking when the
# external URL isn't resolvable from inside the container (e.g. when
# the reverse proxy is air-gapped from internal traffic). Use a
# dedicated ``AGNES_MCP_INTERNAL_URL`` instead, defaulting to
# ``http://localhost:8000`` — the right shape for self-calls in the
# single-container deploy. Operators running Agnes split across
# multiple pods can point this at the in-cluster service URL.
_BASE = os.environ.get("AGNES_MCP_INTERNAL_URL", "http://localhost:8000").rstrip("/")

mcp = FastMCP(
    "Agnes",
    instructions=SERVER_INSTRUCTIONS,
    # DNS rebinding protection is redundant — _AuthMiddleware validates PAT
    # before any request reaches FastMCP, so the protection is already in place.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _headers() -> dict[str, str]:
    token = _current_token.get()
    if not token:
        raise RuntimeError("No authentication token in current MCP context")
    return {"Authorization": f"Bearer {token}"}


# ── tools ──────────────────────────────────────────────────────────────────────


_FOUNDATION_TOOL_NAMES = register_foundation_tools(mcp, base_url=_BASE, headers_fn=_headers)

# Back-compat: bind each registered tool function onto this module's globals.
# Existing unit tests call e.g. ``mcp_http.catalog(...)`` directly to exercise
# tool logic without going through the MCP protocol layer; FastMCP's
# @mcp.tool() decorator returns the original function unchanged, so the
# implementation lives in foundation_tools.py but stays reachable here.
for _name in _FOUNDATION_TOOL_NAMES:
    _tool = mcp._tool_manager.get_tool(_name)
    assert _tool is not None, f"foundation tool {_name!r} missing after registration"
    globals()[_name] = _tool.fn
del _name


def _query_param_token_allowed() -> bool:
    """Whether the ``?token=`` auth fallback is accepted on SSE GET.

    Resolved per request rather than cached at import: the value lives in the
    ``/admin/server-config`` overlay, and an operator who turns the fallback off
    after an incident should not have to restart the process for it to take
    effect. ``feature_enabled`` reads the deep-merged config, which is itself
    cached, so this is not a per-request disk hit.
    """
    from app.instance_config import feature_enabled

    return feature_enabled(
        "mcp",
        "allow_query_param_token",
        env_var="AGNES_MCP_ALLOW_QUERY_PARAM_TOKEN",
        default=True,
    )


_query_param_token_warned = False


def _warn_query_param_token_once() -> None:
    """Warn once per process when an MCP SSE request authenticates via the
    ?token= query param (the token then lands in access logs — CWE-598)."""
    global _query_param_token_warned
    if not _query_param_token_warned:
        _query_param_token_warned = True
        logger.warning(
            "MCP SSE auth used the ?token= query param — the token appears in "
            "access logs (CWE-598). Prefer the Authorization header; configure "
            "the reverse proxy to redact the 'token' query param from logs."
        )


# ── auth middleware ─────────────────────────────────────────────────────────────


class _AuthMiddleware:
    """Pure ASGI middleware: validates Bearer token, sets _current_token."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        auth = headers.get(b"authorization", b"").decode()

        # Fallback: ?token= query param for clients that can't set headers on SSE GET.
        #
        # Operator-disablable since the 2026-08-05 audit (F-3). Default stays ON
        # so no existing SSE client breaks, but an operator whose clients all
        # send the Authorization header can remove the exposure outright instead
        # of relying on the reverse proxy to redact the parameter from its logs.
        if not auth.lower().startswith("bearer ") and _query_param_token_allowed():
            from urllib.parse import parse_qs

            qs = parse_qs(scope.get("query_string", b"").decode())
            t = qs.get("token", [""])[0]
            if t:
                auth = f"bearer {t}"
                # CWE-598: a token in the query string is captured by every
                # request-logging intermediary (reverse proxy, uvicorn access
                # log, SIEM). We keep the fallback for header-incapable SSE GET
                # clients, but warn once so operators configure log redaction /
                # prefer the Authorization header. (Full fix — a short-lived
                # header-exchanged connect ticket — is tracked as a follow-up.)
                _warn_query_param_token_once()

        if not auth.lower().startswith("bearer "):
            await _send_401(scope, send, "no_token")
            return

        raw_token = auth[7:]
        try:
            from app.auth.pat_resolver import resolve_token_to_user
            from src.db import get_system_db
            from src.repositories import use_pg

            # resolve_token_to_user routes through the repository factory and
            # ignores ``conn``; on Postgres pass None so the system DuckDB is
            # never opened (forbidden invariant).
            conn = None if use_pg() else get_system_db()
            try:
                user, reason = resolve_token_to_user(conn, raw_token)
            finally:
                if conn is not None:
                    conn.close()
        except Exception:
            # NOT a 401: this is Agnes failing, not the caller presenting a bad
            # credential. Answering 401 here told an operator whose system DB
            # was unreachable that their token was rejected, and sent them off
            # to rotate a perfectly good one.
            logger.exception("MCP auth error")
            await _send_auth_error(scope, send, status=500, reason=AUTH_INTERNAL_ERROR)
            return

        if user is None:
            # The typed reason from pat_resolver (invalid_token, pat_revoked,
            # pat_expired, agent_pat_wrong_surface, deactivated, …) travels to
            # the caller as its REST wording — same vocabulary, one source.
            await _send_401(scope, send, reason or "invalid_token")
            return

        # A restricted principal (co-session / agent-session token) is refused
        # at the door. This transport's passthrough closures identify their
        # caller by a bare user id (`_current_user_id`), which cannot express
        # "the owner, minus this agent's connection scope" — resolving a
        # principal to `owner_user_id` here would hand the sandbox the OWNER's
        # full tool surface with the scope filter silently skipped. The
        # sandbox reaches MCP through the stdio server + the REST passthrough
        # endpoints (which are principal-aware), never through SSE.
        #
        # Its own reason: the credential is live and valid, it is this SURFACE
        # that refuses it, which is a different thing to tell the caller than
        # "your token is bad" (mirrors `agent_pat_wrong_surface`).
        if isinstance(user, PRINCIPAL_TYPES):
            await _send_401(scope, send, PRINCIPAL_WRONG_SURFACE)
            return

        tok = _current_token.set(raw_token)
        uid = _current_user_id.set(str(user.get("id") or ""))
        # F2c (audit-full-coverage plan, Task 5): stamp the session's client
        # kind here — the SSE session's own auth-resolution point — so any
        # audit row logged for the rest of this request's context (not only
        # the tool-call wrapper's own row) is attributed to MCP rather than
        # defaulting to "web". No reset on the way out: each real SSE
        # connection runs in its own ASGI task with a fresh contextvar copy,
        # matching every other surface-specific stamp in this plan.
        from src.audit_context import set_client_kind

        set_client_kind("mcp")
        try:
            await self.app(scope, receive, send)
        finally:
            _current_token.reset(tok)
            _current_user_id.reset(uid)


# Two reasons this transport adds to the ``pat_resolver.ResolutionReason``
# vocabulary, for outcomes that resolution itself never produces.
#
# The credential is live and valid; this SURFACE refuses it (see the middleware
# for why a restricted principal cannot be served here). Named after
# ``agent_pat_wrong_surface``, which says the same thing about an agent PAT.
PRINCIPAL_WRONG_SURFACE = "principal_wrong_surface"
# Agnes broke while checking the credential. Never a 401 — see the middleware.
AUTH_INTERNAL_ERROR = "auth_internal_error"

_TRANSPORT_DETAIL_BY_REASON = {
    PRINCIPAL_WRONG_SURFACE: "Session principal token not valid on this surface",
    AUTH_INTERNAL_ERROR: "Authentication check failed — this is a server-side problem, retry later",
}


def _auth_error_detail(reason: str) -> str:
    """Human ``detail`` for ``reason``, in the REST 401 vocabulary.

    Everything the resolver can return is worded by
    ``app.auth.dependencies.auth_detail_for_reason`` so this transport and the
    REST surface cannot drift; only the two transport-local reasons above are
    worded here.
    """
    if reason in _TRANSPORT_DETAIL_BY_REASON:
        return _TRANSPORT_DETAIL_BY_REASON[reason]
    from app.auth.dependencies import auth_detail_for_reason

    return auth_detail_for_reason(reason)


async def _send_auth_error(scope: Scope, send: Send, *, status: int, reason: str) -> None:
    """Answer an auth failure with a body that says WHICH failure it was.

    Carries a machine-readable ``reason`` (a fixed vocabulary defined in
    Agnes, never anything derived from the request) beside the human
    ``detail``. The credential itself — and any internal exception text — is
    never echoed: the caller learns which check failed, not what they sent or
    what broke behind it.
    """
    import json

    body = json.dumps({"detail": _auth_error_detail(reason), "reason": reason}).encode()
    headers = [
        [b"content-type", b"application/json"],
        [b"content-length", str(len(body)).encode()],
    ]
    if status == 401:
        headers.append([b"www-authenticate", b'Bearer realm="Agnes MCP"'])
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body, "more_body": False})


async def _send_401(scope: Scope, send: Send, reason: str = "invalid_token") -> None:
    await _send_auth_error(scope, send, status=401, reason=reason)


# ── dynamic tool registration (Universal MCP — RFC #461 §7) ───────────────────


def _register_dynamic_tools() -> None:
    """Add passthrough tools from ``tool_registry`` to the module-level ``mcp``.

    Called once from ``make_sse_app`` at app startup. Best-effort — if the
    DB is unreachable or the v61 tables are missing, log and skip so the
    cowork MCP server still comes up with the static tools.
    """
    try:
        from app.api.mcp.tools_generator import (
            install_grant_filtered_list_tools,
            install_tool_call_audit,
            register_passthrough_tools,
        )
    except Exception:  # pragma: no cover - import-time defensive
        logger.exception("Universal MCP imports unavailable; skipping dynamic tool registration")
        return
    _caller_id = lambda: _current_user_id.get() or None  # noqa: E731
    names: list[str] = []
    try:
        names = register_passthrough_tools(mcp, caller_id_fn=_caller_id)
        if names:
            logger.info("MCP HTTP: registered %d passthrough tools", len(names))
    except Exception:
        logger.exception("Universal MCP passthrough registration failed")
    # Hide passthrough tools the caller isn't granted from tools/list (their
    # invocation is already gated; this matches the REST listing's visibility).
    # Pass the registered names so the filter's hide-set is fixed at install
    # time and a runtime grant-resolution error fails closed (see the helper).
    try:
        install_grant_filtered_list_tools(mcp, caller_id_fn=_caller_id, passthrough_names=names)
    except Exception:
        logger.exception("MCP HTTP: grant-filtered tools/list install failed")
    # F2c (audit-full-coverage plan, Task 5): one audit row per tool/call,
    # foundation or passthrough alike — see the wrapper's own docstring.
    try:
        install_tool_call_audit(mcp, caller_id_fn=_caller_id)
    except Exception:
        logger.exception("MCP HTTP: tool-call audit install failed")


# ── factory ────────────────────────────────────────────────────────────────────


def make_sse_app() -> ASGIApp:
    """Return the Agnes SSE MCP app wrapped with PAT authentication."""
    _register_dynamic_tools()
    return _AuthMiddleware(mcp.sse_app())
