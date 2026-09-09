"""Chat sandbox secret broker routes (2026-07-14 incident hardening).

The in-sandbox loopback relay (``app/chat/relay.py``) never holds a real
credential — it forwards CLI/MCP traffic to these routes carrying only an
opaque, short-lived ticket (``src/repositories/ticket.py``). These routes:

- ``POST /api/broker/anthropic`` — inject the real ``ANTHROPIC_API_KEY``
  server-side and forward to the pinned Anthropic API. The agent-supplied
  request never carries (or can redirect) the real key or host.
- ``POST /api/broker/agnes-api`` / ``POST /api/broker/agnes-mcp`` — resolve
  the ticket to the caller's real Agnes identity, mint an ordinary session
  JWT for that identity, and replay the described ``{method, path, body}``
  request in-process through the *same* FastAPI app instance that received
  the broker call (``request.app``) via ``httpx.ASGITransport``. This keeps
  every access-control check (RBAC, admin gates, resource grants) exactly as
  live as a direct call — the broker adds no privilege of its own.

Ticket scope ("main" vs "mcp") must match the route: a ticket minted for one
CLI cannot be replayed against the other's route. Admin-*mutation* paths
(``/api/admin/*`` and any route gated by ``require_admin``) are hard-rejected —
the broker only ever re-authenticates the interactive-parity flows (catalog
reads, queries, MCP tool calls), never privileged admin writes, regardless of
the resolved identity's own grants. Read-only (``GET``/``HEAD``) admin routes
ARE replayed for ``main``-scoped tickets only (the CLI's leg — the MCP
subprocess's narrower ticket keeps the full refusal), switchable via
``chat.broker_admin_reads`` (default on): the
replay runs under the ticket's resolved identity and the route's own
``require_admin`` still decides live — a non-admin caller (or an
``AgentPrincipal``, which ``require_admin`` hard-denies) gets the route's own
403, and the broker never adds privilege. Every secret-shaped value on those
GET surfaces is masked server-side (``GET /api/admin/server-config`` redacts
via ``_public_view``), and the repo-wide "never mutate on GET" invariant is
what makes the method the correct read/write boundary here.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import json
import logging
import os
import random
import time
import uuid
from typing import Any, Dict, Optional
from urllib.parse import unquote, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute

from app.api.broker_agent_policy import (
    cached_month_total,
    check_budget,
    check_model,
    check_model_value,
    parse_usage,
    parse_usage_from_edges,
    usage_accumulator,
)
from app.api.broker_vertex import (
    COUNT_TOKENS_MODEL,
    count_tokens_to_vertex,
    messages_to_vertex,
    parse_vertex_path,
    sanitize_beta_header,
    validate_vertex_target,
    vertex_upstream_base,
)
from app.auth.access import is_user_admin, mint_agent_session_jwt, mint_co_session_jwt, require_admin
from app.auth.jwt import create_access_token
from app.chat.turn_context import TurnRecord, read_turn
from app.chat.turn_usage import add_turn_usage
from src.observability import content_policy as _content_policy
from src.observability import otel as _otel
from src.observability.llm_context import LlmCallContext
from src.observability.llm_record import LlmCallRecord, build_record
from src.observability.otlp_scrub import OtlpBatchUndecodable, empty_logs_response, scrub_logs, scrub_traces
from src.agent_scope_intersection import agent_is_passthrough
from src.repositories import (
    access_token_repo,
    agents_repo,
    audit_repo,
    chat_session_repo,
    data_apps_repo,
    ticket_repo,
    users_repo,
)

logger = logging.getLogger(__name__)

#: Cap on the SSE bytes mirrored for streaming usage recording — a
#: completion body far past this is pathological; the full mirror stops
#: growing past this point (logged), but usage still survives via the
#: bounded head/tail edges below.
_SSE_USAGE_COLLECT_MAX_BYTES = 8 * 1024 * 1024
#: Size of the head and tail edge buffers kept for EVERY streamed completion,
#: on top of the full mirror above. Anthropic puts usage in ``message_start``
#: (the stream's first bytes) and ``message_delta`` (its last), so 64 KiB at
#: each end is enough to recover tokens, cost, model and stop reason from a
#: stream whose full body blew past the mirror cap — only the content
#: summary is then lost.
_SSE_EDGE_BYTES = 64 * 1024

router = APIRouter(prefix="/api/broker", tags=["broker"])

# Admin mutations are never brokered — the broker replays only the
# interactive-parity surface (catalog/query/MCP), never admin writes,
# regardless of the resolved identity's own grants. The `/api/admin/` prefix
# is only a fast-path; the authoritative gate is `_route_requires_admin`,
# which catches every `Depends(require_admin)` route wherever it lives
# (e.g. `/api/users/*`, `/auth/admin/tokens/*`) — a bare path-prefix check
# missed those (Devin/agnes-review on #846, §11).
_ADMIN_PATH_PREFIX = "/api/admin/"

#: Read-only methods a brokered admin route may be replayed with. Safe as the
#: read/write boundary because "never mutate on GET" is a repo-wide security
#: invariant (see .claude/skills/agnes-conventions/references/security.md) —
#: state-changing web/API handlers are POST/PUT/PATCH/DELETE by contract.
_ADMIN_READ_METHODS = frozenset({"GET", "HEAD"})


def _broker_admin_reads_enabled() -> bool:
    """Operator switch for replaying read-only admin routes (`chat_broker_admin_reads`).

    Read live per request (`effect="live"` in the switch registry) so an
    operator can turn the surface off without a restart. Default on: the
    replay still runs under the ticket's resolved identity and the route's
    own ``require_admin`` decides — non-admin users and restricted principals
    (AgentPrincipal/SessionPrincipal) get the route's own 403, so the switch
    only widens what an *actual admin's* interactive session may read.
    """
    from app.instance_config import feature_enabled

    return feature_enabled("chat", "broker_admin_reads", env_var="AGNES_CHAT_BROKER_ADMIN_READS", default=True)


def _dependant_calls(dependant: Any) -> set:
    """Every dependency callable in a route's dependant tree (recursive)."""
    calls: set = set()
    stack = [dependant]
    while stack:
        d = stack.pop()
        call = getattr(d, "call", None)
        if call is not None:
            calls.add(call)
        stack.extend(getattr(d, "dependencies", None) or [])
    return calls


def _route_requires_admin(app: Any, method: str, path: str) -> bool:
    """True if the concrete ``method`` + ``path`` resolves to an app route
    gated by ``Depends(require_admin)``.

    Introspects the real route table (not a path prefix), so it catches admin
    mutations at any path — the exact gap the prefix-only check left open.
    A path that matches no route returns False (the replay will 404 harmlessly).

    Fail-closed over ALL matching routes: if *any* route that matches this
    method+path is admin-gated, the whole request is treated as admin — never
    just the first match. This removes any dependence on route-registration
    order (a non-admin catch-all like ``/{full_path:path}`` registered before
    the real admin route must not shadow the gate) — the safe direction.
    """
    m = method.upper()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if m not in (route.methods or set()):
            continue
        if not route.path_regex.match(path):
            continue
        if require_admin in _dependant_calls(route.dependant):
            return True
    return False


def _normalize_broker_path(raw: Any) -> httpx.URL:
    """Canonicalize an agent-supplied replay path to the EXACT URL the ASGI
    dispatch will route on, pinned to the loopback authority, or 400.

    The admin-route gate and the ``httpx.ASGITransport`` dispatch must decide
    on the *same* path — otherwise a string that reads as non-admin to the
    gate but dispatches to an admin route bypasses the gate (RBAC review on
    #849 reproduced this end-to-end). ASGITransport routes on ``request.url.path``,
    which httpx produces by **percent-decoding and collapsing dot-segments** —
    so a literal check on the raw string diverges from what actually
    dispatches (``/api/sync/tri%67ger`` and ``/api/foo/../sync/trigger`` both
    resolve to ``/api/sync/trigger``).

    The fix: reject authority smuggling on the raw input (absolute URL,
    protocol-relative ``//host``, backslash, percent-encoded ``//``), then
    build the target ``httpx.URL`` ONCE against the pinned loopback host. The
    caller reads ``.path`` from this object for the gate **and** dispatches
    this very object — so the gate and the dispatch cannot diverge for any
    encoding. Query string is preserved; over-blocks at worst.
    """
    parsed = urlsplit(str(raw or ""))
    if parsed.scheme or parsed.netloc:
        raise HTTPException(status_code=400, detail="broker_path_must_be_local")
    path = parsed.path
    # Reject authority smuggling in both literal and percent-decoded forms
    # BEFORE canonicalizing (a leading `//` after decode is a protocol-relative
    # host; backslash is a `/` to some clients).
    for form in (path, unquote(path)):
        if not form.startswith("/") or form.startswith("//") or "\\" in form:
            raise HTTPException(status_code=400, detail="broker_path_must_be_local")
    reconstructed = f"{path}?{parsed.query}" if parsed.query else path
    target = httpx.URL("http://broker-replay" + reconstructed)
    # Dot-segment collapse can't produce a leading `//`, but re-validate the
    # canonical path defensively — it is what the gate and dispatch both use.
    if not target.path.startswith("/") or target.path.startswith("//"):
        raise HTTPException(status_code=400, detail="broker_path_must_be_local")
    return target


def _normalize_upstream_path(path: str) -> str:
    """Canonicalize an upstream Anthropic subpath to the EXACT path the outbound
    httpx request will send on, or 400. Strips trailing slashes and collapses
    duplicate slashes, e.g. ``"/v1/messages/"`` or ``"//v1//messages"`` ->
    ``"/v1/messages"``; a literal ``.``/``..`` dot-segment (or a backslash some
    clients treat as ``/``) is REFUSED, never silently collapsed.

    ``anthropic_proxy`` is registered on a ``{subpath:path}`` wildcard, so
    ``upstream_path`` is whatever raw string the caller put after
    ``/api/broker/anthropic``. Three consumers must decide on the SAME value:
    the per-agent model-allowlist/budget gate, the ``use_dispatcher`` check,
    AND the outbound URL — otherwise the guard and the real destination
    disagree. httpx canonicalizes the URL at send time (collapsing dot-segments
    and duplicate slashes), so a raw ``/v1/./messages`` that a strip-empty-only
    normalizer classifies as NON-message would still reach the real
    ``/v1/messages``, skipping the model allowlist and monthly budget. Rejecting
    dot-segment/backslash smuggling (rather than canonicalizing it to match)
    keeps that guard/destination pair honest and refuses authority-smuggling
    tricks outright. The caller feeds THIS return value to both the gate and the
    outbound request, so they cannot drift apart.
    """
    for form in (path, unquote(path)):
        for seg in form.split("/"):
            if seg in (".", "..") or "\\" in seg:
                raise HTTPException(status_code=400, detail="broker_upstream_path_invalid")
    return "/" + "/".join(seg for seg in path.split("/") if seg)


# Anthropic traffic is always forwarded to this pinned host — the sandbox's
# request never gets to choose where its "anthropic" call actually goes.
_ANTHROPIC_BASE_URL = "https://api.anthropic.com"

# LLM completions routinely run for tens of seconds to minutes; httpx's 5s
# default read timeout makes EVERY real completion fail with httpx.ReadTimeout,
# leaving the sandbox agent with an empty response (chat looks "broken" even
# though isolation/auth are correct). Use a generous read timeout while keeping
# connect/write/pool bounded so a dead upstream still fails fast.
_ANTHROPIC_TIMEOUT = httpx.Timeout(connect=15.0, read=600.0, write=60.0, pool=15.0)

# Upstream statuses worth one more attempt before the caller sees a failure.
# ONLY 429: the provider rejected the request without processing it (a Vertex
# per-minute token/request quota is the common one), so replaying it is safe
# and usually succeeds within seconds. Agnes's OWN 429 — the per-agent
# ``budget_exhausted`` refusal — is raised as an HTTPException far above this
# point and deliberately carries no Retry-After so nothing auto-retries it; it
# never reaches this forward, and must not be added here.
_RETRYABLE_UPSTREAM_STATUSES = (429,)
# Two retries = three attempts total. Bounded low on purpose: a chat turn is
# interactive, and a quota that is still exhausted after ~3s of waiting is a
# capacity problem the operator needs to see, not one to hide behind a longer
# stall.
_MAX_UPSTREAM_RETRIES = 2
# Honour the provider's own Retry-After, but never stall an interactive turn
# for longer than this — a 60s Retry-After is a signal to give up and say so,
# not to freeze the UI for a minute.
_RETRY_AFTER_CAP_SEC = 10.0
_RETRY_BASE_DELAY_SEC = 0.5

# Response headers worth forwarding back to the in-sandbox SDK. The SDK's own
# retry logic reads Retry-After; without it, it backs off blind. The
# anthropic-ratelimit-* family is what a client uses to pace itself before
# hitting the wall at all. Everything else stays dropped — forwarding
# content-length/content-encoding from a response we may have re-read would
# corrupt the body, so this is an allowlist, never a copy-all.
_FORWARDED_RESPONSE_HEADER_PREFIXES = ("anthropic-ratelimit-",)
_FORWARDED_RESPONSE_HEADERS = ("retry-after",)


def _passthrough_response_headers(resp: httpx.Response) -> Dict[str, str]:
    """Rate-limit headers from ``resp`` that the caller should see verbatim."""
    out: Dict[str, str] = {}
    for key, value in resp.headers.items():
        lowered = key.lower()
        if lowered in _FORWARDED_RESPONSE_HEADERS or lowered.startswith(_FORWARDED_RESPONSE_HEADER_PREFIXES):
            out[key] = value
    return out


def _retry_after_seconds(resp: httpx.Response, attempt: int) -> float:
    """How long to wait before retrying ``resp``.

    Prefers the provider's own ``Retry-After`` (delta-seconds form, which is
    what Vertex and the Anthropic API both send), clamped to
    ``_RETRY_AFTER_CAP_SEC``. Falls back to exponential backoff with jitter so
    several sandboxes hitting the same quota ceiling don't retry in lockstep.
    """
    raw = resp.headers.get("retry-after", "")
    try:
        wait = float(raw)
    except (TypeError, ValueError):
        wait = -1.0
    if wait < 0:
        wait = _RETRY_BASE_DELAY_SEC * (2**attempt)
    return min(max(wait, 0.0), _RETRY_AFTER_CAP_SEC) + random.uniform(0, 0.25)


def _add_anthropic_beta(headers: Dict[str, str], beta: str) -> None:
    """Ensure ``beta`` is present in the ``anthropic-beta`` header, appending to
    any value the in-sandbox SDK already set rather than overwriting it. The
    header lookup is case-insensitive (the SDK may send ``Anthropic-Beta``)."""
    for key in list(headers.keys()):
        if key.lower() == "anthropic-beta":
            existing = [v.strip() for v in headers[key].split(",") if v.strip()]
            if beta not in existing:
                existing.append(beta)
            headers[key] = ", ".join(existing)
            return
    headers["anthropic-beta"] = beta


def require_broker_ticket(request: Request) -> Dict[str, Any]:
    """Resolve the bearer ticket on the request. 401s if missing/unknown/expired.

    Plain ``def`` (not ``async def``) so FastAPI offloads it to the anyio
    thread pool — the body does a synchronous ``ticket_repo().resolve`` DB
    read that must not run on the single uvicorn event loop (Tier 1, PR #188).
    """
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else ""
    if not token:
        raise HTTPException(status_code=401, detail="missing_broker_ticket")
    row = ticket_repo().resolve(token)
    if row is None:
        raise HTTPException(status_code=401, detail="invalid_or_expired_ticket")
    return row


def _require_scope(row: Dict[str, Any], *scopes: str) -> None:
    """Hard-deny (401) + audit a ticket presented against the wrong-scope route.

    A ticket minted for the MCP loopback must never authenticate the main
    CLI's broker route and vice versa — the spawn-time scope is the
    contract, not the identity behind it.

    Several scopes may be listed for a route that more than one kind of caller
    legitimately reaches. `anthropic_proxy` is the only such route: `main` is
    the native chat sandbox's general ticket, and `llm` is the embedded
    engine's LLM-ONLY ticket, which deliberately does not open `agnes-api`.
    Listing scopes here rather than widening a scope's meaning keeps the
    narrower ticket narrow.
    """
    if row.get("scope") not in scopes:
        try:
            audit_repo().log(
                action="broker_ticket_scope_mismatch",
                params={
                    "expected_scope": "|".join(scopes),
                    "actual_scope": row.get("scope"),
                    "session_id": row.get("session_id"),
                },
                result="denied",
                client_kind="broker",
            )
        except Exception:
            # Audit logging must never break the deny path itself.
            pass
        raise HTTPException(status_code=401, detail="ticket_scope_mismatch")


def _mint_identity_jwt(session_id: str) -> str:
    """Mint the JWT the replayed request runs under, for the ticket's session.

    - **Co-session**: mint a ``co_session`` JWT (``mint_co_session_jwt``). It
      carries a synthetic ``sub`` and NO baked-in identity; the downstream
      auth path recomputes the participant grant-intersection **live, per
      request** (``compute_grant_intersection`` over ``chat_session_participants``
      with ``left_at IS NULL``). Resolving a co-session to its single stored
      owner (the previous behaviour) both over-authorized guests and went
      stale when the owner left — see §11.
    - **Solo session with an agent** (V1d): a deleted/missing agent fails
      CLOSED (401 ``ticket_agent_not_found`` — it must not regain the
      owner's full authority via the fall-through). A live agent that is
      not explicitly all-``'all'`` (``agent_is_passthrough``) — including a
      misconfigured/unknown mode value — mints an ``agent_session`` JWT
      (``mint_agent_session_jwt``). Same no-baked-in-authority contract as
      the co-session branch: the resolver rebuilds owner-grants ∩
      agent-scope live, per request. Only an explicit all-``'all'`` agent
      (every user's lazily-seeded default) — and only on a session whose
      user IS the agent's owner — falls through to the plain owner-identity
      branch below so web chat's JWT shape is unchanged (identical authority
      either way; an optimization, not a security exception). A session
      whose user is NOT the owner (Slack channel binding: the mentioner)
      always takes the agent-session path, or the turn would run with the
      mentioning user's own authority instead of the agent's.
    - **Solo session, no narrowing agent**: resolve the owner via the
      dual-backend chat-session + users lookup and mint an ordinary
      identity JWT (unchanged legacy/no-agent path).

    All three carry ``chat_session_id`` so ``execute_query``'s per-session
    BigQuery budget accounting works identically to a direct call.
    """
    session = chat_session_repo().get_session(session_id)
    if session is None:
        raise HTTPException(status_code=401, detail="ticket_session_not_found")
    if getattr(session, "is_co_session", False):
        return mint_co_session_jwt(session_id)
    agent_id = getattr(session, "agent_id", None)
    agent = None
    if agent_id:
        agent = agents_repo().get_by_id(agent_id)
        if agent is None or agent.get("deleted_at") is not None:
            # Fail CLOSED, mirroring pat_resolver: a session attributed to a
            # deleted/missing agent must not silently regain the owner's
            # full authority via the plain-identity fall-through below.
            raise HTTPException(status_code=401, detail="ticket_agent_not_found")
        if not agent_is_passthrough(agent):
            # Anything that is not an explicit all-'all' agent — including a
            # misconfigured/unknown mode value — takes the enforced
            # agent-session path; the resolver rebuilds the intersection
            # live and fails closed on bad rows.
            return mint_agent_session_jwt(session_id)
    user = users_repo().get_by_email(session.user_email)
    if user is None:
        raise HTTPException(status_code=401, detail="ticket_user_not_found")
    if agent is not None and str(user["id"]) != str(agent.get("owner_user_id")):
        # An all-'all' agent on a session whose user is NOT the agent's owner
        # (a Slack channel binding: the session user is the MENTIONER). The
        # passthrough optimization's premise — "identical authority either
        # way" — holds only when session user == owner; a plain identity JWT
        # here would run the agent's turn with the mentioning user's own
        # authority (admin short-circuit included). Take the enforced
        # agent-session path so the turn carries the OWNER-derived
        # AgentPrincipal regardless of who mentioned the bot.
        return mint_agent_session_jwt(session_id)
    # scope="chat" is what makes `_stash_chat_session_id_from_token` stash the
    # chat_session_id that `execute_query`'s per-session BigQuery budget keys
    # off — it ignores the claim without that scope. The pre-broker solo token
    # (`mint_session_jwt`) carried it; keep it so the scan-budget cap still
    # applies to brokered solo sessions. (security review on #849)
    return create_access_token(
        user_id=user["id"],
        email=user["email"],
        extra_claims={"scope": "chat", "chat_session_id": session_id},
    )


async def _replay(request: Request, row: Dict[str, Any], body: Dict[str, Any]) -> httpx.Response:
    """Replay a ``{method, path, body}`` request in-process under a freshly
    minted session JWT for the ticket's resolved identity.

    Uses ``request.app`` (the exact FastAPI instance that received this
    broker call) for the ASGI transport, so the replay always targets the
    same app/config/DB the broker itself is running against — no reliance
    on a module-level app singleton.
    """
    method = str(body.get("method") or "GET").upper()

    # Canonicalize the agent-supplied path FIRST into the exact URL the ASGI
    # dispatch routes on, and read the gate's path from that same object — so
    # an absolute-URL / protocol-relative / percent-encoded / dot-segment path
    # can't defeat the gate while still hitting the real handler (RBAC review on
    # #849). A smuggling attempt is a probe → audit + 400.
    try:
        target = _normalize_broker_path(body.get("path"))
    except HTTPException:
        try:
            audit_repo().log(
                action="broker_path_rejected",
                params={"raw_path": str(body.get("path"))[:200], "session_id": row.get("session_id")},
                result="denied",
                client_kind="broker",
            )
        except Exception:
            pass
        raise

    # The gate decides on the SAME canonical path the dispatch will use
    # (``target.path`` is exactly ``request.url.path`` at replay time).
    match_path = target.path

    # Admin mutations are never brokered — refuse before touching identity,
    # regardless of whether the resolved identity is itself an admin. The
    # `/api/admin/` prefix is a fast-path; route introspection is the real
    # gate and catches admin routes at any path (§11). Read-only (GET/HEAD)
    # admin routes are the deliberate exception (switchable): they replay
    # under the resolved identity and the route's own `require_admin` decides
    # live — so `agnes admin list-users`/`list-tables` work for an actual
    # admin in chat, while a non-admin or an AgentPrincipal still gets 403
    # from the route itself, and mutations stay interactive-only. The
    # allowance is scoped to the MAIN ticket (the CLI's leg): the MCP
    # subprocess has no admin commands, so its narrower ticket keeps the
    # pre-existing full refusal — least privilege over symmetry (Devin
    # review on this PR).
    is_admin_route = match_path.startswith(_ADMIN_PATH_PREFIX) or _route_requires_admin(request.app, method, match_path)
    admin_read_allowed = method in _ADMIN_READ_METHODS and row.get("scope") == "main" and _broker_admin_reads_enabled()
    if is_admin_route and not admin_read_allowed:
        try:
            audit_repo().log(
                action="broker_admin_route_rejected",
                params={"path": match_path, "method": method, "session_id": row.get("session_id")},
                result="denied",
                client_kind="broker",
            )
        except Exception:
            pass
        raise HTTPException(status_code=403, detail="admin_mutations_require_interactive_auth")
    if is_admin_route:
        # Allowed read — keep the same audit trail the deny path has, so an
        # operator can see exactly which admin surfaces a sandbox session read.
        try:
            audit_repo().log(
                action="broker_admin_read_replayed",
                params={"path": match_path, "method": method, "session_id": row.get("session_id")},
                result="success",
                client_kind="broker",
            )
        except Exception:
            pass

    jwt_token = _mint_identity_jwt(row["session_id"])
    transport = httpx.ASGITransport(app=request.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://broker-replay") as client:
        return await client.request(
            method,
            target,
            headers={"Authorization": f"Bearer {jwt_token}"},
            json=body.get("body"),
        )


def _to_response(resp: httpx.Response, extra_headers: Optional[Dict[str, str]] = None) -> Response:
    response = Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type"),
    )
    # Retry-After / anthropic-ratelimit-* first, so an Agnes-issued header of
    # the same name (budget_headers) still wins — the per-agent budget refusal
    # deliberately controls its own Retry-After semantics.
    for key, value in _passthrough_response_headers(resp).items():
        response.headers[key] = value
    for key, value in (extra_headers or {}).items():
        response.headers[key] = value
    return response


def _agent_and_caller_for_ticket(row: Dict[str, Any]) -> "tuple[Optional[Dict[str, Any]], Optional[str]]":
    """Resolve the ticket's chat session once and return ``(agent_row,
    caller_user_id)``.

    ``agent_row`` is ``None`` when there is nothing to resolve
    (Slack/legacy sessions with no ``agent_id``, or a session that no
    longer exists). Callers must treat that as "behave exactly as before
    this feature existed" — no policy/ledger/budget enforcement runs for
    those sessions.

    ``caller_user_id`` (C2.4, per-caller usage attribution) is the session's
    own ``user_email`` — set SERVER-SIDE at session-creation time
    (``ChatManager.create_session`` for native chat/agent sessions,
    ``app.api.kai._create_session_and_credential`` for the embedded turn
    engine's ``llm``-scoped ticket) — resolved to a user id. It is never
    re-derived from anything the ticket-holder (sandbox relay or engine)
    could shape, so it cannot be spoofed by the caller's own request. For a
    session running a SHARED agent (C2.3) this is the grantee actually
    driving the turn, not the agent's owner — the whole point of
    attributing usage to the caller rather than the agent. ``None`` when
    the session's named user no longer resolves to a live account.

    Caches nothing beyond the single caller's request — one DB round-trip
    (session lookup, shared by both halves) plus, when there IS a bound
    agent, one more for the agent row and one for the caller's user row —
    matching the per-request caching note in the task brief
    (``anthropic_proxy`` calls this once and reuses the result for both the
    pre-forward checks and the post-response usage recording)."""
    session_id = row.get("session_id")
    if not session_id:
        return None, None
    session = chat_session_repo().get_session(session_id)
    if session is None:
        return None, None
    agent_id = getattr(session, "agent_id", None)
    agent_row = agents_repo().get_by_id(agent_id) if agent_id else None
    if agent_row is None:
        # No bound agent → both halves of the result are unused downstream
        # (every `caller_user_id` consumer sits behind `agent_row is not
        # None`). Return before the user lookup so an agent-less session
        # costs exactly what it did before this feature existed.
        return None, None
    caller = users_repo().get_by_email(session.user_email)
    caller_user_id = caller["id"] if caller else None
    return agent_row, caller_user_id


@router.post("/agnes-api")
async def agnes_api(request: Request, row: Dict[str, Any] = Depends(require_broker_ticket)) -> Response:
    """Replay a main-CLI request under the ticket's resolved identity."""
    _require_scope(row, "main")
    body = await request.json()
    resp = await _replay(request, row, body)
    return _to_response(resp)


@router.post("/agnes-mcp")
async def agnes_mcp(request: Request, row: Dict[str, Any] = Depends(require_broker_ticket)) -> Response:
    """Replay an MCP-subprocess request under the ticket's resolved identity."""
    _require_scope(row, "mcp")
    body = await request.json()
    resp = await _replay(request, row, body)
    return _to_response(resp)


# Sandboxed data-apps authoring is confined to this prefix — the ticket's
# `data_apps` scope grants replay access to the data-apps control-plane API
# only, never the wider `/api/*` surface `agnes-api`/`agnes-mcp` expose.
#
# `/api/sharing/*` (owner-scoped Library/data-app sharing, incl. `agnes app
# share` / `data_app_share*` for the `data_app` resource type — TCRD-291) does
# NOT need adding here: it is not `require_admin`-gated, so a `main`- or
# `mcp`-scoped ticket already reaches it through the ordinary `agnes-api`/
# `agnes-mcp` replay above (`_route_requires_admin` only refuses admin
# mutations, and `/api/sharing/*` isn't one). Widening THIS prefix would be
# the wrong fix for a surface that was never narrowed in the first place.
_DATA_APPS_PATH_PREFIX = "/api/data-apps"


def _within_data_apps_prefix(path: str) -> bool:
    return path == _DATA_APPS_PATH_PREFIX or path.startswith(_DATA_APPS_PATH_PREFIX + "/")


def _ticket_owner_for_git(session_id: str) -> Dict[str, Any]:
    """The user a brokered git request runs as — solo sessions only.

    Deliberately narrower than `_mint_identity_jwt`. Pushing to an app's repo
    is a write with no notion of a partial identity, and the two narrowed
    session kinds have exactly that:

    - a **co-session** has no single owner (its authority is the live
      participant intersection), so there is nobody to attribute a commit to;
    - an **agent session** runs under owner-grants ∩ agent-scope, and nothing
      in that intersection describes repository access.

    Both fail closed with 403 rather than falling through to the owner, which
    is the mistake `_mint_identity_jwt` documents for its own co-session
    branch. Solo sessions — what web chat actually uses — resolve to their
    owner.
    """
    session = chat_session_repo().get_session(session_id)
    if session is None:
        raise HTTPException(status_code=401, detail="ticket_session_not_found")
    if getattr(session, "is_co_session", False):
        raise HTTPException(status_code=403, detail="git_not_available_to_co_session")
    agent_id = getattr(session, "agent_id", None)
    if agent_id:
        agent = agents_repo().get_by_id(agent_id)
        if agent is None or agent.get("deleted_at") is not None:
            raise HTTPException(status_code=401, detail="ticket_agent_not_found")
        if not agent_is_passthrough(agent):
            raise HTTPException(status_code=403, detail="git_not_available_to_scoped_agent")
    user = users_repo().get_by_email(session.user_email)
    if user is None:
        raise HTTPException(status_code=401, detail="ticket_user_not_found")
    return user


async def data_apps_git_broker(
    slug: str,
    path: str,
    request: Request,
    row: Dict[str, Any] = Depends(require_broker_ticket),
) -> Response:
    """Carry a sandbox's git traffic to a hosted app's repo.

    Without this the authoring flow has no transport at all. The sandbox
    reaches Agnes only through the in-sandbox relay — `runner.py` rewrites
    `AGNES_SERVER` to it so "the relay is the only thing that ever holds a
    real credential" — and the relay's `data_apps` ticket is confined by
    `_within_data_apps_prefix` to `/api/data-apps*`. A repo lives at
    `/data-apps.git/<slug>`, a different top-level prefix, so `git clone`
    from a sandbox was refused by the broker and (going direct) by the
    sandbox's own egress hook. Watched live: the agent fetched a credential,
    then failed to clone by name, by hostname and by IP.

    Unlike its `{method, path, body}` siblings this is a **proxy**, not an
    envelope replay: git speaks binary bodies and its own content types, and
    its first call is a GET. It is also the one broker route that attaches a
    credential rather than an identity JWT — the git surface authenticates
    only a `data-app-git:<slug>` PAT in basic auth (see
    `app/api/data_apps_git.py`). That token is minted per request and revoked
    in `finally`, so the sandbox never holds one and nothing outlives the
    call.

    Authorization is layered, not replaced: this route pins the slug to an app
    the ticket's owner may reach, and the git surface then applies its own
    owner-or-admin check and its own scope-vs-slug pin. Bodies are buffered
    (an app repo is a scaffold, not a monorepo); a genuinely large push is a
    reason to revisit, not a reason to stream today.
    """
    _require_scope(row, "data_apps")

    from app.api.data_apps import mint_git_token

    user = _ticket_owner_for_git(row["session_id"])
    app_row = data_apps_repo().get_by_slug(slug)
    if app_row is None:
        raise HTTPException(status_code=404, detail="data_app_not_found")
    if app_row.get("owner_user_id") != user["id"] and not is_user_admin(user["id"]):
        try:
            audit_repo().log(
                action="broker_data_apps_git_rejected",
                params={"slug": slug, "session_id": row.get("session_id")},
                result="denied",
                client_kind="broker",
            )
        except Exception:
            pass
        raise HTTPException(status_code=403, detail="forbidden")

    # A DRAFT shares its parent's repository — `create_draft` never gives it
    # one of its own — so both the token's scope and the target path must name
    # the PARENT. Minted and aimed at the draft's own slug, the git surface
    # refuses the token (its scope pins the slug to the repo being requested)
    # and `repo_path(<draft_slug>)` has no HEAD to serve. That is the path the
    # data-apps skill actually walks: it tells the agent to work on a draft.
    # Ownership was already checked against `app_row` above — the parent is
    # the same owner by construction (`create_draft` copies `owner_user_id`).
    # (Devin Review on this PR.)
    repo_row = app_row
    if app_row.get("is_draft") and app_row.get("parent_app_id"):
        parent = data_apps_repo().get(app_row["parent_app_id"])
        if parent is None:
            raise HTTPException(status_code=404, detail="draft_parent_not_found")
        repo_row = parent
    repo_slug = repo_row["slug"]

    token_id, token = mint_git_token(repo_row)
    try:
        basic = base64.b64encode(f"agnes:{token}".encode()).decode()
        headers = {"Authorization": f"Basic {basic}"}
        ctype = request.headers.get("content-type")
        if ctype:
            headers["Content-Type"] = ctype
        # Modern git sends `Git-Protocol: version=2` on the initial
        # `info/refs` GET, and the git surface turns it into `GIT_PROTOCOL`
        # for `git http-backend`. Dropping it silently downgraded every
        # brokered clone and fetch to the v0 protocol — correct output, but
        # the whole ref advertisement on every call instead of the filtered
        # v2 one. Forwarded verbatim; the header carries no credential.
        # (Devin Review on this PR.)
        git_protocol = request.headers.get("git-protocol")
        if git_protocol:
            headers["Git-Protocol"] = git_protocol
        target = f"/data-apps.git/{repo_slug}/{path}"
        if request.url.query:
            target = f"{target}?{request.url.query}"
        transport = httpx.ASGITransport(app=request.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://broker-replay") as client:
            upstream = await client.request(
                request.method,
                target,
                headers=headers,
                content=await request.body(),
                timeout=120.0,
            )
    finally:
        try:
            # DELETE, not revoke. This token lives for one git call and is
            # minted on every one of them — a clone is several — so revoking
            # left a permanent dead row per request in the owner's token list,
            # drowning the PATs they actually manage. Revocation is for a
            # credential someone might still hold; nobody ever held this one
            # but the broker, and it is dead before the response returns.
            # (Devin Review on this PR.)
            access_token_repo().delete(token_id)
        except Exception:
            logger.warning("could not delete per-request git token %s", token_id, exc_info=True)

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )


# Registered as two distinct routes rather than one `methods=["GET", "POST"]`
# route so each verb gets its own `operation_id` — same rationale as
# `data_apps_git_get`/`data_apps_git_post` in app/api/data_apps_git.py, which
# FastAPI otherwise warns about as a duplicate operation id.
router.add_api_route(
    "/data-apps.git/{slug}/{path:path}",
    data_apps_git_broker,
    methods=["GET"],
    operation_id="broker_data_apps_git_get",
)
router.add_api_route(
    "/data-apps.git/{slug}/{path:path}",
    data_apps_git_broker,
    methods=["POST"],
    operation_id="broker_data_apps_git_post",
)


@router.post("/data-apps")
async def data_apps_broker(request: Request, row: Dict[str, Any] = Depends(require_broker_ticket)) -> Response:
    """Replay a sandboxed authoring agent's request under the ticket identity,
    confined to the `/api/data-apps` control-plane surface.

    Twin of `agnes-api`/`agnes-mcp`: same ticket-scope + in-process ASGI
    replay pattern, gated on the `data_apps` scope. The extra path-prefix
    check keeps a data_apps-scoped ticket from reaching any other `/api/*`
    route even though `_replay` itself would happily dispatch there (its own
    gate only blocks admin-mutation routes).

    The confinement check MUST decide on the same canonicalized path
    `_replay`/ASGITransport actually dispatches on — a raw-string
    ``.startswith()`` check on the agent-supplied path diverges from the
    real dispatch target exactly like the admin-route gate's pre-#849 bug:
    ``{"path": "/api/data-apps/../catalog"}`` passes a literal prefix check
    but resolves (via `_normalize_broker_path`, the same canonicalizer
    `_replay` uses) to `/api/catalog` — a real, non-admin, out-of-prefix
    route. So canonicalize FIRST, gate on the canonicalized path, and hand
    the SAME canonical path to `_replay` (never the raw agent-supplied
    string) so the gate and the dispatch cannot diverge.

    Beyond the prefix check, any canonicalized path that still contains a
    literal ``..`` segment is rejected outright — httpx's dot-segment
    collapse (which `_normalize_broker_path` relies on) only resolves
    *literal* ``..`` at URL-construction time; a percent-encoded segment
    (``%2e%2e``, ``..%2f``) survives decoding as a literal ``..`` string
    that still starts with ``/api/data-apps/`` without being collapsed. No
    legitimate `/api/data-apps/*` call ever needs a `..` segment, so this is
    pure defense-in-depth against relying on "it happens to 404".
    """
    _require_scope(row, "data_apps")
    body = await request.json()
    raw_path = body.get("path")

    try:
        target = _normalize_broker_path(raw_path)
    except HTTPException:
        try:
            audit_repo().log(
                action="broker_data_apps_path_rejected",
                params={"raw_path": str(raw_path)[:200], "session_id": row.get("session_id")},
                result="denied",
                client_kind="broker",
            )
        except Exception:
            pass
        raise

    norm_path = target.path
    has_dot_segment = ".." in norm_path.split("/") or "." in norm_path.split("/")
    if has_dot_segment or not _within_data_apps_prefix(norm_path):
        try:
            audit_repo().log(
                action="broker_data_apps_path_rejected",
                params={
                    "raw_path": str(raw_path)[:200],
                    "normalized_path": norm_path,
                    "session_id": row.get("session_id"),
                },
                result="denied",
                client_kind="broker",
            )
        except Exception:
            pass
        raise HTTPException(status_code=403, detail="path_not_allowed")

    # Hand `_replay` the SAME canonical path+query just validated — never the
    # raw agent-supplied string — so its own (idempotent) re-normalization
    # cannot land anywhere but here.
    canonical = norm_path
    if target.query:
        canonical = f"{norm_path}?{target.query.decode('ascii')}"
    body = {**body, "path": canonical}

    resp = await _replay(request, row, body)
    return _to_response(resp)


def _completion_request_hints(raw_body: bytes, vertex_target: Any) -> "tuple[Optional[str], bool]":
    """``(model, stream)`` as the request declares them — the model from the
    Vertex path when the call is a native Vertex invocation, else from the
    Messages body. Never raises: a malformed body is the upstream's 400."""
    model: Optional[str] = getattr(vertex_target, "model", None) if vertex_target is not None else None
    stream = False
    try:
        parsed = json.loads(raw_body)
        if isinstance(parsed, dict):
            model = model or (str(parsed["model"]) if parsed.get("model") else None)
            stream = bool(parsed.get("stream"))
    except (ValueError, TypeError):
        pass
    return model, stream


def _turn_for_row(row: Dict[str, Any]) -> Optional[TurnRecord]:
    """The live chat turn for the ticket's session, if one was published.

    ``None`` when the ticket carries no session, when no turn is in flight,
    and when the coordination backend cannot say — a completion whose turn
    is unknown is a root span with a null ``turn_id``, never a failed
    forward (spec 3.2).
    """
    try:
        session_id = row.get("session_id")
        return read_turn(session_id) if session_id else None
    except Exception:  # noqa: BLE001 - a measurement never costs a forward
        logger.debug("broker: could not read the turn record", exc_info=True)
        return None


def _completion_context(
    row: Dict[str, Any],
    *,
    agent_row: Optional[Dict[str, Any]],
    caller_user_id: Optional[str],
    turn: Optional[TurnRecord] = None,
) -> LlmCallContext:
    """The labels for one brokered completion: what work it is, whose it is.

    Everything the broker knows first-hand from the ticket it just
    validated — no session read, no email. The identity is the resolved
    user id (never the address, spec 3.6) and the bound agent's id when
    there is one; an agent-less session is labelled all the same, because a
    call nobody can attribute is exactly the one a cost report must not
    lose.

    ``turn`` is the chat turn that caused the call (``_turn_for_row``). It
    supplies what the ticket cannot: which turn this is, whether the
    session is agent-API or chat work, and — only where the ticket has
    nothing of its own — the user and agent ChatManager resolved when the
    turn opened. The ticket's own resolution always wins: it is first-hand,
    and the turn record may be one turn stale.
    """
    return LlmCallContext(
        workload=turn.workload if turn else "chat",
        purpose="completion",
        session_id=row.get("session_id"),
        turn_id=turn.turn_id if turn else None,
        user_id=caller_user_id or (turn.user_id if turn else None),
        agent_id=(agent_row.get("id") if agent_row else (turn.agent_id if turn else None)),
    )


def _record_completion(
    *,
    context: LlmCallContext,
    span: Any,
    upstream: str,
    model_requested: Optional[str],
    usage: Optional[Dict[str, Any]],
    status_code: Optional[int],
    latency_ms: Optional[int],
    summary: "_otel.CompletionSummary",
    error: Optional[BaseException] = None,
    response_truncated: bool = False,
) -> Optional[LlmCallRecord]:
    """Build, price and buffer the ``llm_calls`` row for one completion.

    Runs for EVERY completion — export on or off, agent-bound or not, and
    for a failure too (a zero-cost error row, never a gap in the ledger).
    The span's ids go on the row when a span was opened, so the row and the
    exported span describe the same call. Returns the record so the caller
    can finish the span with the price it just computed; returns ``None``
    if anything went wrong, because a measurement never costs a forward.
    ``response_truncated`` marks a streamed completion whose usage was
    recovered from the head/tail edges after the full-body mirror
    overflowed — the tokens and price are still real (spec 3.1).
    """
    try:
        trace_id, span_id = _otel.span_ids(span) if span is not None else (None, None)
        failed = error is not None or (status_code is not None and status_code >= 400)
        # A STREAM that never reached a stop reason -- the client walked
        # away, the upstream cut the connection mid-answer -- returned a
        # perfectly good HTTP 200, so a status derived from the status code
        # alone would file a half-delivered answer under "ok" and quietly
        # flatter every error and quality summary built on this ledger.
        # It gets its own status instead. `stream_complete` is None for a
        # non-streaming call (only `text/event-stream` sets it), so nothing
        # but a real interrupted stream can land here.
        incomplete = not failed and summary.stream_complete is False
        record = build_record(
            kind="completion",
            context=context,
            provider="gcp.vertex_ai" if upstream == "vertex" else "anthropic",
            upstream=upstream,
            model_requested=model_requested,
            model_response=(usage or {}).get("model") or summary.model,
            usage=usage,
            latency_ms=latency_ms,
            status=("error" if failed else "incomplete" if incomplete else "ok"),
            error_type=(
                type(error).__name__
                if error is not None
                else (str(status_code) if failed else ("stream_incomplete" if incomplete else None))
            ),
            http_status=status_code,
            prompt_chars=summary.prompt_chars,
            completion_chars=summary.completion_chars,
            stop_reason=summary.stop_reason,
            stream_complete=summary.stream_complete,
            response_truncated=response_truncated,
            trace_id=trace_id,
            span_id=span_id,
        )
        usage_accumulator.add_call(record.to_row())
        return record
    except Exception:  # noqa: BLE001 - a measurement never costs a forward
        logger.debug("broker: could not record the completion", exc_info=True)
        return None


async def _start_otel_completion_span(
    *,
    row: Dict[str, Any],
    raw_body: bytes,
    vertex_target: Any,
    upstream: str,
    context: LlmCallContext,
    parent_context: Any = None,
) -> Any:
    """Open the broker's completion span, labelled by the call context the
    caller already built. No session read on this path: identity comes from
    the ticket's own resolution, so tracing costs no extra query — and never
    the user's email, which is personal data with no join value off-instance
    (spec 3.6). ``parent_context`` is the chat turn's span when one is known."""
    model, stream = _completion_request_hints(raw_body, vertex_target)
    return _otel.start_completion_span(
        upstream=upstream,
        model=model,
        stream=stream,
        session_id=row.get("session_id"),
        ticket_scope=row.get("scope"),
        user_id=context.user_id,
        agent_id=context.agent_id,
        context=context,
        parent_context=parent_context,
    )


# --- OTLP telemetry egress for the embedded engine's sandbox ----------------
#
# The engine's sandbox exports its own spans (turn → step → tool) through the
# in-sandbox relay's ``otlp`` scope, which forwards ``/otlp/<rest>`` to this
# instance's ``HOST_BROKER_OTLP_URL`` with a ``kai_otlp`` ticket and no other
# credential (docs/observability.md → "OpenTelemetry export"). This route is
# the broker half: it swaps the ticket for the collector credential the
# instance's own export already holds (``OTEL_EXPORTER_OTLP_HEADERS``) and
# forwards the batch to the same collector, so the sandbox's traces land in
# the same place as the broker's completion spans.

#: The three OTLP/HTTP signal paths an SDK exports to. Exact allowlist — the
#: relay forwards whatever path the sandbox names, so anything else is refused
#: here and never resolved against the collector.
_OTLP_SIGNALS = frozenset({"traces", "metrics", "logs"})
#: One OTLP export batch is bounded by the SDK's batch processor (hundreds of
#: KiB at the very most); anything past this is not telemetry.
_OTLP_MAX_BODY_BYTES = 8 * 1024 * 1024
_OTLP_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)
#: Inbound headers worth forwarding: the wire format and its compression. The
#: relay already dropped the credential/hop-by-hop sets; everything else is
#: the sandbox's business, not the collector's. ``content-encoding`` survives
#: only when the batch is forwarded as received — a batch the content policy
#: decoded and re-serialised leaves here uncompressed, and says so.
_OTLP_FORWARDED_REQUEST_HEADERS = frozenset({"content-type", "content-encoding"})


# The collector (base endpoint + operator headers) comes from ONE place —
# ``src.observability.otel.collector`` — which is also what
# ``/api/kai/tickets`` mints the ``kai_otlp`` ticket from, so the ticket and
# the route can never disagree about whether there is somewhere to forward to.
_otlp_collector = _otel.collector
_parse_otlp_headers = _otel.parse_otlp_headers


@router.post("/otlp/v1/{signal}", name="otlp_proxy")
async def otlp_proxy(signal: str, request: Request, row: Dict[str, Any] = Depends(require_broker_ticket)) -> Response:
    """Forward one OTLP/HTTP export batch from the engine's sandbox to the
    instance's collector, credential injected server-side.

    Accepts ONLY the ``kai_otlp`` scope — the ticket ``/api/kai/tickets`` mints
    for the relay's ``otlp`` scope, and only while this instance exports OTLP
    itself (``OTEL_EXPORTER_OTLP_ENDPOINT``); an ``llm``/``main`` ticket is
    refused with the usual scope-mismatch audit. Without a configured
    collector the route answers ``503 otlp_export_not_configured`` so a
    misordered rollout (engine env set before the export) fails loudly per
    batch instead of silently swallowing telemetry — the turn itself is
    unaffected, the SDK's exporter just logs the refusal.

    The body is the SDK's protobuf batch. It is forwarded under the
    instance's content-export policy (``observability.content_export`` —
    :mod:`src.observability.content_policy`), the same record the broker's
    own completion spans obey, because the sandbox's spans carry the prompt
    and the answer in their attributes:

    - ``full`` — forwarded byte-for-byte with its compression header.
    - ``pseudonymized`` — traces and log bodies rewritten through the
      instance anonymizer; the batch is re-serialised uncompressed.
    - ``off`` (the default) — the content attributes are stripped from
      traces (the structural turn/step/tool spans still flow) and a logs
      batch is accepted and dropped, so the exporter sees a 2xx rather than
      retrying a decision the operator made.

    Metrics carry counts, never content, and are always forwarded as sent.

    **The relay fails closed on content:** under ``off`` / ``pseudonymized``
    a batch it cannot decode is refused with ``400 otlp_batch_undecodable``,
    never forwarded unstripped — a relay that cannot read a batch cannot
    claim the batch is free of content.

    The collector's 2xx body comes back as-is (the OTLP success response),
    its error text never does — the status (and ``Retry-After``, which the
    exporter's retry honours) is enough.
    """
    _require_scope(row, "kai_otlp")
    if signal not in _OTLP_SIGNALS:
        raise HTTPException(status_code=404, detail={"code": "otlp_signal_not_supported"})
    collector = _otlp_collector()
    if collector is None:
        raise HTTPException(status_code=503, detail={"code": "otlp_export_not_configured"})
    base, operator_headers = collector
    # Refuse a declared-oversized batch before buffering it; the post-read
    # check below still catches an undeclared or lying length.
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > _OTLP_MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail={"code": "otlp_batch_too_large"})
    body = await request.body()
    if len(body) > _OTLP_MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail={"code": "otlp_batch_too_large"})

    # The relay is workload `chat` by definition (spec 3.6): the engine's
    # sandbox exports one turn's own spans, so its content obeys the same
    # `chat` allowlist entry as the broker's own completion spans, never a
    # different one.
    mode = _content_policy.content_export_mode(workload="chat")
    encoding: Optional[str] = request.headers.get("content-encoding")
    try:
        if signal == "traces" and mode != "full":
            body = scrub_traces(body, mode=mode, content_encoding=encoding)
            encoding = None
        elif signal == "logs" and mode != "full":
            if mode == "off":
                return Response(content=empty_logs_response(), status_code=200, media_type="application/x-protobuf")
            body = scrub_logs(body, mode=mode, content_encoding=encoding) or b""
            encoding = None
    except OtlpBatchUndecodable as exc:
        logger.warning("broker: refused an undecodable %s batch under content mode %s", signal, mode)
        raise HTTPException(status_code=400, detail={"code": "otlp_batch_undecodable"}) from exc

    headers = {k: v for k, v in request.headers.items() if k.lower() in _OTLP_FORWARDED_REQUEST_HEADERS}
    headers.setdefault("content-type", "application/x-protobuf")
    if encoding is None:
        # The batch was decoded and re-serialised — whatever the sandbox
        # compressed, what leaves here is plain protobuf.
        headers = {k: v for k, v in headers.items() if k.lower() != "content-encoding"}
    headers.update(operator_headers)
    try:
        async with httpx.AsyncClient(timeout=_OTLP_TIMEOUT) as client:
            upstream = await client.post(f"{base}/v1/{signal}", content=body, headers=headers)
    except httpx.HTTPError as exc:
        # The collector's hostname/credential is operator config; the sandbox
        # gets a typed 502 and the operator gets the class of failure.
        logger.warning("broker: OTLP collector unreachable for %s batch: %s", signal, type(exc).__name__)
        raise HTTPException(status_code=502, detail={"code": "otlp_collector_unreachable"}) from exc
    passthrough: Dict[str, str] = {}
    retry_after = upstream.headers.get("retry-after")
    if retry_after:
        passthrough["retry-after"] = retry_after
    if 200 <= upstream.status_code < 300:
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type"),
            headers=passthrough or None,
        )
    logger.warning("broker: OTLP collector answered %s for a %s batch", upstream.status_code, signal)
    return Response(status_code=upstream.status_code, headers=passthrough or None)


@router.post("/anthropic", name="anthropic_proxy_bare")
@router.post("/anthropic/{subpath:path}", name="anthropic_proxy_subpath")
async def anthropic_proxy(request: Request, row: Dict[str, Any] = Depends(require_broker_ticket)) -> Response:
    """Inject the real Anthropic API key server-side and forward to the
    pinned Anthropic API — the sandbox's dummy key is discarded, and the
    target host is never taken from the agent-supplied request.

    Registered for both the bare path and any sub-path: the Anthropic SDK
    appends ``/v1/messages`` (etc.) to its base URL, so the real request
    arrives at ``/api/broker/anthropic/v1/messages``. The sub-path is
    recomputed from ``request.url.path`` and forwarded to the pinned host —
    the agent-supplied request still cannot choose the target host (Devin
    review on #849). When LLM_DISPATCHER_URL is set, POST /v1/messages is
    instead forwarded to that dispatcher with LLM_DISPATCHER_API_KEY
    (token-arbitrage PoC); all other subpaths keep the pinned Anthropic
    upstream.

    Accepts `llm` alongside `main`: the embedded turn engine's egress ticket
    (`app/api/kai.py`) is minted in that narrower scope precisely so it cannot
    also authenticate `agnes-api`, which requires `main` and replays the whole
    non-admin `/api/*` surface. Found by Devin Review on #1235."""
    _require_scope(row, "main", "llm")
    # An `llm` ticket belongs to the embedded kai-agent engine, and its
    # authority is bounded by the session ROW, not only by its own TTL. This
    # route is the one place an already-issued egress ticket can still spend the
    # instance's LLM budget, and nothing else on the path checks the row:
    # `_require_session_credential` gates `/api/kai/*`, so it stops a deleted
    # conversation from minting NEW tickets while leaving an outstanding one
    # spendable.
    #
    # `app/api/kai.py` used to justify that gap by pointing at the scope-blind
    # `revoke_session` sweep that `ChatManager.kill` runs on permanent delete.
    # That sweep is conditional: `_kill_quietly` returns early when
    # `app.state.chat_manager is None`, which is the NORMAL state for an
    # instance that embeds the engine without running Agnes's own sandbox chat
    # (six branches in `app/main.py` set it, `chat.enabled` false among them).
    # On exactly the deployment this integration targets, nothing revoked the
    # ticket at all. Checked here instead — the fix that module already named as
    # the honest one.
    #
    # Narrowed to `llm` on purpose: `main` is the native relay's scope and its
    # traffic is the busy path, so it keeps paying nothing. Offloaded because a
    # synchronous DB read must not run on the event loop.
    # Found by Devin Review on this PR.
    llm_scope_session = None
    if row.get("scope") == "llm":
        llm_scope_session = await asyncio.to_thread(lambda: chat_session_repo().get_session(row["session_id"]))
        if llm_scope_session is None:
            raise HTTPException(status_code=401, detail="ticket_session_gone")
    raw_body = await request.body()
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "authorization", "content-length", "x-api-key")
    }

    upstream_path = request.url.path[len("/api/broker/anthropic") :] or "/"
    # Normalized ONCE and reused for both the policy gate below and the
    # `use_dispatcher` check further down — two independent `== "/v1/messages"`
    # comparisons against a `{subpath:path}` wildcard is exactly how they'd
    # diverge for a trailing/duplicate-slash variant (see
    # `_normalize_upstream_path`'s docstring).
    normalized_upstream_path = _normalize_upstream_path(upstream_path)
    is_messages_post = request.method == "POST" and normalized_upstream_path == "/v1/messages"
    is_count_tokens_post = request.method == "POST" and normalized_upstream_path == "/v1/messages/count_tokens"

    chat_cfg = getattr(request.app.state, "chat_config", None)
    # Vertex mode (chat.llm.provider: vertex): the sandbox CLI runs in native
    # Vertex gateway mode and sends Vertex-shaped model paths; Messages-format
    # clients (the kai-agent engine) keep sending /v1/messages and get
    # rewritten below. Unlike anthropic mode's open forward, vertex mode is
    # FAIL CLOSED on subpaths: anything that is neither a valid Vertex model
    # invocation nor a known Messages endpoint is refused, never forwarded.
    vertex_mode = getattr(chat_cfg, "llm_provider", "anthropic") == "vertex"
    vertex_target = parse_vertex_path(normalized_upstream_path) if vertex_mode else None
    if vertex_mode:
        if vertex_target is not None:
            target_err = validate_vertex_target(
                vertex_target,
                getattr(chat_cfg, "vertex_project_id", ""),
                getattr(chat_cfg, "vertex_region", ""),
            )
            if target_err:
                # The sandbox's env only carries routing hints; THIS equality
                # check against instance config is what pins where spend lands.
                try:
                    audit_repo().log(
                        action="broker_vertex_target_rejected",
                        params={
                            "raw_path": str(upstream_path)[:200],
                            "session_id": row.get("session_id"),
                        },
                        result="denied",
                        client_kind="broker",
                    )
                except Exception:  # noqa: BLE001, S110 — audit logging must never break the deny path
                    pass
                raise HTTPException(status_code=403, detail={"code": target_err})
        elif not (is_messages_post or is_count_tokens_post):
            try:
                audit_repo().log(
                    action="broker_vertex_path_rejected",
                    params={
                        "raw_path": str(upstream_path)[:200],
                        "session_id": row.get("session_id"),
                    },
                    result="denied",
                    client_kind="broker",
                )
            except Exception:  # noqa: BLE001, S110 — audit logging must never break the deny path
                pass
            raise HTTPException(status_code=404, detail={"code": "vertex_path_not_supported"})

    # A completion spends tokens; count_tokens (either spelling) does not.
    is_completion = is_messages_post or (vertex_target is not None and vertex_target.model != COUNT_TOKENS_MODEL)

    # Agent-as-API policy: per-agent model allowlist + monthly token budget
    # (Task 8, agent-profiles V1a). Sits BEFORE the credential fork below so
    # it covers all upstream modes (static key / WIF / dispatcher / vertex),
    # and raises BEFORE any token is spent. Sessions with no bound agent
    # (Slack, legacy, or a session predating this feature) resolve `agent_row`
    # to `None` and skip all of it — behavior is unchanged for them.
    agent_row: dict[str, Any] | None = None
    caller_user_id: str | None = None
    budget_headers: dict[str, str] = {}
    if is_completion:
        agent_row, caller_user_id = _agent_and_caller_for_ticket(row)
    if agent_row is not None:
        utility_models = getattr(chat_cfg, "agent_api_utility_models", []) or []
        budget_ttl_s = getattr(chat_cfg, "agent_api_budget_cache_ttl_s", 60)
        budget = agent_row.get("token_budget_monthly")
        month_total: Optional[int] = None
        if budget is not None:
            month_total = cached_month_total(agent_row["id"], budget_ttl_s)
            budget_headers = {
                "x-agnes-budget-limit": str(budget),
                "x-agnes-budget-used": str(month_total),
            }
        # Vertex native paths carry the model in the URL, not the body; both
        # forms compare canonically, so pinned models in either spelling work.
        if vertex_target is not None:
            model_err = check_model_value(vertex_target.model, agent_row, utility_models)
        else:
            model_err = check_model(raw_body, agent_row, utility_models)
        if model_err:
            raise HTTPException(status_code=403, detail={"code": model_err}, headers=budget_headers or None)
        if month_total is not None:
            budget_err = check_budget(agent_row, month_total)
            if budget_err:
                # NO Retry-After header — SDKs must not auto-retry a budget
                # exhaustion (spec §3); `budget_headers` carries only the
                # x-agnes-budget-* pair, never Retry-After.
                raise HTTPException(status_code=429, detail={"code": budget_err}, headers=budget_headers or None)

    # Opt-in LLM dispatcher (token-arbitrage PoC). When LLM_DISPATCHER_URL is
    # set, chat completions (POST /v1/messages) forward to the dispatcher
    # authenticated with the dispatcher's own team key — the key doubles as
    # the ledger identity for this deployment. Every other subpath
    # (count_tokens, ...) keeps the pinned Anthropic upstream: the dispatcher
    # only implements /v1/messages. The target host still never comes from
    # the agent-supplied request (env-configured, operator-owned). When set,
    # this takes precedence over llm_auth — including workload_identity —
    # for /v1/messages. Deliberately NO fallback to direct Anthropic on
    # dispatcher failure: silently bypassing the cost-routing PoC would
    # corrupt its measurements; the sandbox sees the ordinary upstream error.
    dispatcher_url = os.environ.get("LLM_DISPATCHER_URL", "").strip().rstrip("/")
    # Never in vertex mode: the dispatcher speaks the first-party Messages
    # API. The boot gate refuses the combination; this guard is belt-and-braces.
    use_dispatcher = bool(dispatcher_url) and is_messages_post and not vertex_mode
    if dispatcher_url and vertex_mode:
        logger.warning("LLM_DISPATCHER_URL is set but chat.llm.provider=vertex — dispatcher ignored")

    # Credential injection is the ONE thing that differs between auth modes; the
    # sandbox never carries either credential (it's added here, server-side).
    #   vertex                 → Authorization: Bearer <Google OAuth token from
    #                            ADC>; NO Anthropic credential exists at all.
    #   dispatcher opt-in      → x-api-key: <LLM_DISPATCHER_API_KEY>
    #   api_key (default)      → x-api-key: <static ANTHROPIC_API_KEY>
    #   workload_identity      → Authorization: Bearer <short-lived federated
    #                            token> + the oauth beta header OAuth-style
    #                            tokens require; NO static key exists.
    llm_auth = getattr(getattr(request.app.state, "chat_config", None), "llm_auth", "api_key")
    wif_mode = llm_auth == "workload_identity" and not use_dispatcher and not vertex_mode
    if vertex_mode:
        from app.auth.vertex_gcp import VertexAuthError, get_vertex_access_token

        try:
            # Offload the (synchronous, possibly network-bound) token
            # resolution/refresh so it can't stall the chat event loop.
            google_token = await asyncio.to_thread(get_vertex_access_token)
        except VertexAuthError as exc:
            # Full detail goes to the audit trail (server-side only); the
            # sandbox-facing caller gets a GENERIC message — never echo
            # credential-chain detail across the isolation boundary.
            try:
                audit_repo().log(
                    action="broker_vertex_token_failed",
                    params={"error": str(exc)[:500], "session_id": row.get("session_id")},
                    result="error",
                    client_kind="broker",
                )
            except Exception:  # noqa: BLE001, S110 — audit logging must never break the request path
                pass
            raise HTTPException(status_code=502, detail="vertex credential resolution failed") from exc
        headers["Authorization"] = f"Bearer {google_token}"
        # Vertex VALIDATES ``anthropic-beta`` and 400s the whole request on
        # any value it does not recognize (the first-party API ignores
        # unknowns). The kai-agent engine's SDK speaks first-party and sends
        # betas Vertex has never heard of — filter to the values Vertex
        # accepts (renaming where its spelling differs) and drop the rest.
        # Header names/values carry no secrets, so the drop is loggable.
        for _beta_key in list(headers.keys()):
            if _beta_key.lower() == "anthropic-beta":
                kept_betas, dropped_betas = sanitize_beta_header(headers.pop(_beta_key))
                if kept_betas:
                    headers["anthropic-beta"] = kept_betas
                if dropped_betas:
                    logger.info(
                        "vertex mode: dropped anthropic-beta value(s) the Vertex endpoint does not accept: %s",
                        ", ".join(dropped_betas),
                    )
    elif use_dispatcher:
        # strip() guards against trailing newlines/spaces from secret managers
        # (same normalization the URL gets above) — an invisible \n in the key
        # is a hard-to-debug dispatcher 401.
        dispatcher_key = os.environ.get("LLM_DISPATCHER_API_KEY", "").strip()
        if not dispatcher_key:
            # Misconfiguration (URL set, key missing) fails loud at the
            # dispatcher with a 401 — log it server-side so the operator sees
            # the cause; the sandbox-facing behavior stays a plain upstream 401.
            logger.warning(
                "LLM_DISPATCHER_URL is set but LLM_DISPATCHER_API_KEY is empty — "
                "forwarding without a key; the dispatcher will reject this request"
            )
        headers["x-api-key"] = dispatcher_key
    elif wif_mode:
        from app.auth.wif import WIFAuthError, get_federated_access_token

        try:
            # Offload the (synchronous, network-bound, ~10s-timeout) token
            # exchange so a refresh can't stall the single-worker chat event
            # loop for the whole app.
            token = await asyncio.to_thread(get_federated_access_token)
        except WIFAuthError as exc:
            # The exchange error can carry up to 200 chars of Anthropic's raw
            # response body (org/rule/service-account ids on invalid_grant).
            # Record the full detail in the audit trail (server-side only, like
            # every other deny path here) and return a GENERIC message to the
            # sandbox-facing caller — never echo upstream error text across the
            # isolation boundary.
            try:
                audit_repo().log(
                    action="broker_wif_exchange_failed",
                    params={"error": str(exc)[:500], "session_id": row.get("session_id")},
                    result="error",
                    client_kind="broker",
                )
            except Exception:
                # Audit logging must never break the request path itself.
                pass
            raise HTTPException(
                status_code=502,
                detail="workload_identity token exchange failed",
            ) from exc
        headers["Authorization"] = f"Bearer {token}"
        _add_anthropic_beta(headers, "oauth-2025-04-20")
    else:
        headers["x-api-key"] = os.environ.get("ANTHROPIC_API_KEY", "")

    # Outbound path/body: identical to the inbound pair everywhere except
    # vertex mode. A native Vertex path is REBUILT from its parsed, validated
    # groups (never the raw string); a Messages-format call is rewritten into
    # the Vertex shape (model body→URL, anthropic_version injected) so the
    # kai-agent engine and other Messages clients need no change.
    outbound_path = normalized_upstream_path
    outbound_body = raw_body
    if vertex_mode:
        if vertex_target is not None:
            outbound_path = vertex_target.upstream_path
        else:
            project_id = getattr(chat_cfg, "vertex_project_id", "")
            region = getattr(chat_cfg, "vertex_region", "")
            try:
                if is_messages_post:
                    outbound_path, outbound_body, _model = messages_to_vertex(raw_body, project_id, region)
                else:  # is_count_tokens_post — the only other path allowed above
                    outbound_path, outbound_body = count_tokens_to_vertex(raw_body, project_id, region)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail={"code": "vertex_body_invalid"}) from exc

    upstream_base = (
        dispatcher_url
        if use_dispatcher
        else (vertex_upstream_base(getattr(chat_cfg, "vertex_region", "")) if vertex_mode else _ANTHROPIC_BASE_URL)
    )
    # Stream-open the upstream call: status + headers arrive immediately, the
    # body stays unread. A 2xx SSE completion is then forwarded chunk-by-chunk
    # (StreamingResponse below) instead of buffered whole — buffering here
    # collapsed every token delta of the model's answer into one burst at
    # turn end, so the user stared at silence and then got the entire text
    # at once. No ``async with``: the client must outlive this handler for
    # the streaming case; the pass-through iterator's ``finally`` closes it.
    # Opt-in OTLP span per completion (src/observability/otel.py): opened
    # here, after every gate that could refuse the call, and closed where
    # the forward ends — in the stream's ``finally`` or after the buffered
    # read below — so its duration is the upstream's, not the gates'.
    #
    # The call's LABELS, on the other hand, are built for every completion
    # whether or not the export is on: the `llm_calls` ledger row below is
    # written either way (spec 3.1), and it is the on-instance record — the
    # OTLP span is the optional copy of it, not the other way round.
    upstream_label = "dispatcher" if use_dispatcher else ("vertex" if vertex_mode else "anthropic")
    completion_context: Optional[LlmCallContext] = None
    requested_model: Optional[str] = None
    # Which chat turn this call belongs to (spec 3.2). The engine propagates
    # no trace context, so the turn announces itself over the coordination
    # backend and the broker reads it here — one small read per completion.
    # A `traceparent` request header would be preferred if the engine ever
    # sent one; until then this is the only link that exists.
    turn: Optional[TurnRecord] = None
    if is_completion:
        turn = _turn_for_row(row)
        completion_context = _completion_context(row, agent_row=agent_row, caller_user_id=caller_user_id, turn=turn)
        requested_model, _requested_stream = _completion_request_hints(raw_body, vertex_target)
    otel_span = None
    if completion_context is not None and _otel.is_enabled():
        try:
            # The turn's span normally lives in another process, so the
            # parent is a remote, non-recording context; the collector
            # stitches the two on the trace id.
            parent_context = _otel.remote_parent_context(turn.trace_id, turn.span_id) if turn else None
            otel_span = await _start_otel_completion_span(
                row=row,
                raw_body=raw_body,
                vertex_target=vertex_target,
                upstream=upstream_label,
                context=completion_context,
                parent_context=parent_context,
            )
        except Exception:  # noqa: BLE001 - a measurement must never cost a forward
            logger.debug("broker: could not open the completion span", exc_info=True)
            otel_span = None
    # The clock the record's `latency_ms` reads: wall time from just before
    # the upstream request to the end of the forward (stream end or buffered
    # read), so it measures the provider, not this instance's own gates.
    forward_started = time.monotonic()
    client = httpx.AsyncClient(timeout=_ANTHROPIC_TIMEOUT)
    # Retry loop for upstream rate limiting. A provider 429 (a Vertex
    # per-minute token/request quota is the usual one) means the request was
    # refused WITHOUT being processed, so replaying it is safe and normally
    # succeeds within a second or two. Without this, one quota blip became a
    # user-visible "Something went wrong" in the middle of a conversation —
    # the request is rebuilt each attempt because a sent httpx request is not
    # reusable, and the previous response is closed before the retry so the
    # connection returns to the pool.
    attempt = 0
    while True:
        try:
            upstream_req = client.build_request(
                request.method,
                # `outbound_path` — either the SAME canonical value the policy /
                # budget / dispatcher gates classified on above, or (vertex mode)
                # a path rebuilt from that value's parsed+validated groups — so
                # the guard and the real destination can never disagree
                # (dot-segments already refused, slashes already collapsed).
                f"{upstream_base}{outbound_path}",
                content=outbound_body,
                headers=headers,
                params=request.query_params,
            )
            resp = await client.send(upstream_req, stream=True)
        except httpx.TransportError as exc:
            # The upstream (Anthropic / dispatcher / Vertex) could not be
            # reached AT ALL — connection refused, DNS failure, or a connect
            # that timed out — never a completed call the provider itself
            # rejected (that path forwards the real status above/below and is
            # classified separately by `_record_llm_health`). Left unhandled,
            # this reached the sandbox's own HTTP client as an opaque,
            # retry-hostile 500 with no signal beyond a raw transport-error
            # string — the chat surface's `chatErrorCopy` classifies THAT
            # text defensively, but the honest fix is a typed response here:
            # a 503 with `Retry-After` and a cataloged health signal, exactly
            # like the 401/403/400 branch below gives the admin readiness
            # banner (#884's own precedent, extended to "never got a response
            # at all"). Not retried by this loop — a genuine connection
            # failure rarely clears within the loop's own short backoff, and
            # the typed 503 already tells the caller to retry on its own.
            await client.aclose()
            from app.chat.readiness import record_llm_runtime_failure

            diag = record_llm_runtime_failure(request.app.state, None, str(exc))
            try:
                audit_repo().log(
                    action="broker_llm_unreachable",
                    params={"reason": diag.get("reason"), "detail": diag.get("detail")},
                    result="error",
                    client_kind="broker",
                )
            except Exception:
                # Audit logging must never break the deny path itself.
                pass
            # The same record every other outcome gets: a call that never
            # reached the provider is a zero-cost error row, and its span is
            # finished here rather than abandoned mid-flight (an unended span
            # is never exported at all, so this branch used to drop the whole
            # completion from the trace as well as from the ledger).
            if completion_context is not None:
                _record_completion(
                    context=completion_context,
                    span=otel_span,
                    upstream=upstream_label,
                    model_requested=requested_model,
                    usage=None,
                    status_code=None,
                    latency_ms=int((time.monotonic() - forward_started) * 1000),
                    summary=_otel.CompletionSummary(),
                    error=exc,
                )
            if otel_span is not None:
                _otel.end_completion_span(
                    otel_span,
                    error=exc,
                    workload=completion_context.workload if completion_context is not None else None,
                )
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "llm_upstream_unreachable",
                    "message": "The instance is restarting or temporarily unavailable — try again in a minute.",
                },
                headers={"Retry-After": "30"},
            ) from exc
        except BaseException as _exc:
            await client.aclose()
            if completion_context is not None:
                # A call that never returned is still a call: a zero-cost
                # error row, so a burst of them shows up in the ledger
                # instead of looking like traffic that simply stopped.
                _record_completion(
                    context=completion_context,
                    span=otel_span,
                    upstream=upstream_label,
                    model_requested=requested_model,
                    usage=None,
                    status_code=None,
                    latency_ms=int((time.monotonic() - forward_started) * 1000),
                    summary=_otel.CompletionSummary(),
                    error=_exc,
                )
            if otel_span is not None:
                _otel.end_completion_span(
                    otel_span,
                    error=_exc,
                    workload=completion_context.workload if completion_context is not None else None,
                )
            raise
        if resp.status_code not in _RETRYABLE_UPSTREAM_STATUSES or attempt >= _MAX_UPSTREAM_RETRIES:
            break
        delay = _retry_after_seconds(resp, attempt)
        await resp.aclose()
        attempt += 1
        logger.info(
            "broker: upstream %s on %s — retry %s/%s in %.2fs",
            resp.status_code,
            outbound_path,
            attempt,
            _MAX_UPSTREAM_RETRIES,
            delay,
        )
        try:
            await asyncio.sleep(delay)
        except BaseException as _exc:
            await client.aclose()
            if completion_context is not None:
                # A call that never returned is still a call: a zero-cost
                # error row, so a burst of them shows up in the ledger
                # instead of looking like traffic that simply stopped.
                _record_completion(
                    context=completion_context,
                    span=otel_span,
                    upstream=upstream_label,
                    model_requested=requested_model,
                    usage=None,
                    status_code=None,
                    latency_ms=int((time.monotonic() - forward_started) * 1000),
                    summary=_otel.CompletionSummary(),
                    error=_exc,
                )
            if otel_span is not None:
                _otel.end_completion_span(
                    otel_span,
                    error=_exc,
                    workload=completion_context.workload if completion_context is not None else None,
                )
            raise
    # A 401 in vertex mode means the cached Google token was revoked before
    # its declared expiry — drop it so the next request re-resolves.
    if vertex_mode and resp.status_code == 401:
        from app.auth.vertex_gcp import clear_token_cache as clear_vertex_token_cache

        clear_vertex_token_cache()
    # A 401 in WIF mode means the cached token was revoked before its declared
    # expiry — drop it so the next request re-mints.
    if wif_mode and resp.status_code == 401:
        from app.auth.wif import clear_token_cache

        clear_token_cache()

    ctype = resp.headers.get("content-type", "")
    if 200 <= resp.status_code < 300 and ctype.startswith("text/event-stream"):
        # A 2xx forward clears any stale credential diagnostic — the health
        # recorder only reads the status on the success path, so it is safe
        # to call before the body has been consumed.
        _record_llm_health(request.app.state, resp)

        # ``aiter_bytes`` (not ``aiter_raw``) so httpx undoes any upstream
        # ``Content-Encoding`` — that header is not forwarded, so passing
        # still-compressed raw bytes through would corrupt the stream.
        # Cleanup lives in a try/finally INSIDE the iterator, not a Starlette
        # ``background=`` task: the background callback only runs on the
        # happy path, so a client disconnect or upstream drop mid-stream
        # (routine for completions running tens of seconds) would leak the
        # upstream response + per-request client — the finalized/abandoned
        # generator still runs its ``finally`` (RBAC review on #1020).
        #
        # Usage recording (streaming path): the buffered recording below
        # never runs for a 2xx SSE stream, so a mirror of it lives in the
        # iterator's ``finally`` — the passthrough bytes are collected
        # (capped, skip-on-overflow) and parsed once the stream ends.
        # Without this, ordinary streamed turns never reach the llm_usage
        # ledger and per-agent monthly budgets never fire. Running in
        # ``finally`` also catches a client disconnect mid-stream: the
        # partial body still carries ``message_start``'s input tokens.
        # Turn-usage counters (app/chat/turn_usage.py) are recorded for ANY
        # session-bound completion — the agent gate below is only for the
        # llm_usage budget ledger. Without this, an agent-less session's
        # bytes were never even mirrored for parsing.
        turn_session_id = row.get("session_id") if is_completion else None
        collect_usage = (agent_row is not None or turn_session_id is not None) and resp.status_code == 200
        collected = bytearray()
        state = {"overflow": False}
        # The bounded edges kept for EVERY streamed completion regardless of
        # the full mirror's own overflow state: a 64 KiB append-only head and
        # a 64 KiB rolling tail (whole chunks, so the cap is approximate, not
        # exact). Anthropic's usage lives in `message_start` (first bytes)
        # and `message_delta` (last bytes), so these two windows are what a
        # completion whose body blew past `_SSE_USAGE_COLLECT_MAX_BYTES`
        # falls back to (`parse_usage_from_edges` below) — tokens, cost,
        # model and stop reason survive; only the content summary is lost.
        head = bytearray()
        tail: collections.deque[bytes] = collections.deque()
        tail_bytes = 0
        # Mirror the passthrough bytes when SOMETHING downstream reads them:
        # the budget ledger, the span, or the call record (which is built for
        # every completion, export on or off).
        mirror_body = collect_usage or otel_span is not None or completion_context is not None

        async def _passthrough():
            nonlocal tail_bytes
            try:
                async for chunk in resp.aiter_bytes():
                    if mirror_body:
                        if len(head) < _SSE_EDGE_BYTES:
                            head.extend(chunk[: _SSE_EDGE_BYTES - len(head)])
                        tail.append(chunk)
                        tail_bytes += len(chunk)
                        while len(tail) > 1 and tail_bytes - len(tail[0]) >= _SSE_EDGE_BYTES:
                            tail_bytes -= len(tail.popleft())
                        if not state["overflow"]:
                            if len(collected) + len(chunk) <= _SSE_USAGE_COLLECT_MAX_BYTES:
                                collected.extend(chunk)
                            else:
                                state["overflow"] = True
                    yield chunk
            finally:
                await resp.aclose()
                await client.aclose()
                body = bytes(collected)
                # Parsed ONCE for all three consumers below (budget ledger,
                # call record, span) — the same figures, by construction. An
                # overflowed stream recovers usage from the edges instead of
                # losing it outright (spec 3.1).
                if state["overflow"]:
                    usage = parse_usage_from_edges(bytes(head), b"".join(tail), ctype)
                else:
                    usage = parse_usage(body, ctype)
                if collect_usage:
                    try:
                        if state["overflow"]:
                            logger.warning(
                                "usage recovered from stream edges; content summary truncated "
                                "(session %s, stream exceeded %d bytes)",
                                row.get("session_id"),
                                _SSE_USAGE_COLLECT_MAX_BYTES,
                            )
                        if usage and agent_row is not None:
                            usage_accumulator.add(
                                {
                                    **usage,
                                    "id": str(uuid.uuid4()),
                                    "agent_id": agent_row["id"],
                                    "user_id": agent_row.get("owner_user_id"),
                                    "caller_user_id": caller_user_id,
                                    "session_id": row.get("session_id"),
                                },
                                budget_ttl_s=budget_ttl_s,
                            )
                        if usage and turn_session_id:
                            add_turn_usage(turn_session_id, usage)
                    except Exception:
                        logger.exception(
                            "llm usage recording failed for agent %s (stream already forwarded)",
                            agent_row.get("id"),
                        )
                summary = None
                record = None
                if completion_context is not None:
                    summary = _otel.describe_completion(
                        request_body=raw_body,
                        response_body=bytes(head) if state["overflow"] else body,
                        content_type=ctype,
                        response_truncated=state["overflow"],
                    )
                    if state["overflow"] and usage:
                        # `describe_completion` never re-parses a truncated
                        # body — stop reason and stream-completeness come
                        # from the tail's own `message_delta` instead.
                        summary.stop_reason = usage.get("stop_reason")
                        summary.stream_complete = bool(usage.get("stop_reason"))
                    record = _record_completion(
                        context=completion_context,
                        span=otel_span,
                        upstream=upstream_label,
                        model_requested=requested_model,
                        usage=usage,
                        status_code=resp.status_code,
                        latency_ms=int((time.monotonic() - forward_started) * 1000),
                        summary=summary,
                        response_truncated=state["overflow"],
                    )
                if otel_span is not None:
                    _otel.end_completion_span(
                        otel_span,
                        status_code=resp.status_code,
                        usage=usage,
                        request_body=raw_body,
                        response_body=body,
                        content_type=ctype,
                        response_truncated=state["overflow"],
                        summary=summary,
                        cost_usd=record.cost_usd if record is not None else None,
                        workload=completion_context.workload if completion_context is not None else None,
                    )

        return StreamingResponse(
            _passthrough(),
            status_code=resp.status_code,
            media_type=ctype,
            headers=_passthrough_response_headers(resp) or None,
        )

    # Non-stream responses (JSON endpoints such as count_tokens, upstream
    # errors): buffer exactly as before — the credential diagnostics below
    # inspect the (small) body.
    try:
        await resp.aread()
    finally:
        await resp.aclose()
        await client.aclose()

    # Surface an actionable operator diagnostic for LLM-credential failures.
    # An auth (401/403) or credit-exhaustion (400) response otherwise reaches
    # the in-sandbox agent and becomes an opaque synthetic assistant message —
    # operators get no clear signal the cause is the LLM credential (#884). We
    # classify it (reusing readiness.classify_llm_failure) into a health signal
    # the admin readiness banner reads, and audit it (never the key itself).
    _record_llm_health(request.app.state, resp)

    # Usage recording (Task 8): AFTER the forward completes, for every
    # upstream mode. Buffered into `usage_accumulator` (bulk-flushed to the
    # llm_usage ledger — never one synchronous write per LLM call) rather
    # than written here. Must never break the response path: any parse/add
    # failure is caught and logged, not raised.
    turn_session_id = row.get("session_id") if is_completion else None
    # Parsed ONCE for all three consumers below (budget ledger, call record,
    # span) — the same figures, by construction.
    usage = parse_usage(resp.content, ctype) if resp.status_code == 200 else None
    if (agent_row is not None or turn_session_id is not None) and resp.status_code == 200:
        try:
            if usage and turn_session_id:
                add_turn_usage(turn_session_id, usage)
            if usage and agent_row is not None:
                usage_accumulator.add(
                    {
                        **usage,
                        "id": str(uuid.uuid4()),
                        "agent_id": agent_row["id"],
                        "user_id": agent_row.get("owner_user_id"),
                        "caller_user_id": caller_user_id,
                        "session_id": row.get("session_id"),
                    },
                    budget_ttl_s=budget_ttl_s,
                )
        except Exception:
            logger.exception(
                "llm usage recording failed for agent %s (response already forwarded)", agent_row.get("id")
            )
    summary = None
    record = None
    if completion_context is not None:
        summary = _otel.describe_completion(
            request_body=raw_body,
            response_body=resp.content,
            content_type=ctype,
        )
        record = _record_completion(
            context=completion_context,
            span=otel_span,
            upstream=upstream_label,
            model_requested=requested_model,
            usage=usage,
            status_code=resp.status_code,
            latency_ms=int((time.monotonic() - forward_started) * 1000),
            summary=summary,
        )
    if otel_span is not None:
        _otel.end_completion_span(
            otel_span,
            status_code=resp.status_code,
            usage=usage,
            request_body=raw_body,
            response_body=resp.content,
            content_type=ctype,
            summary=summary,
            cost_usd=record.cost_usd if record is not None else None,
            workload=completion_context.workload if completion_context is not None else None,
        )

    return _to_response(resp, budget_headers)


# LLM-credential failure statuses worth an operator signal: auth (invalid /
# expired / unfunded-permission key), 400 (candidate "credit balance too low"),
# and 429 — a rate limit that SURVIVED ``_MAX_UPSTREAM_RETRIES`` is no longer a
# blip, it is sustained quota exhaustion, and without a signal here the only
# person who learns about it is whoever's chat happens to be open at the time.
# Other 4xx/5xx are the agent's own request errors, not a credential fault.
_LLM_DIAG_STATUSES = (400, 401, 403, 429)


def _anthropic_error_message(resp: httpx.Response) -> str:
    """Best-effort extract the provider error message from an error response.

    The Anthropic API returns ``{"error": {"type": ..., "message": ...}}`` on
    failures; fall back to raw text. Never raises."""
    try:
        body = resp.json()
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
    except Exception:
        pass
    try:
        return resp.text[:500]
    except Exception:
        return ""


def _record_llm_health(app_state: Any, resp: httpx.Response) -> None:
    """Record or clear the runtime LLM-credential diagnostic from a forward.

    A successful (2xx) forward clears any stale signal; an auth/credit failure
    records a classified, key-free diagnostic + an audit row. Never raises."""
    from app.chat.readiness import clear_llm_runtime_diagnostic, record_llm_runtime_failure

    status = resp.status_code
    if 200 <= status < 300:
        clear_llm_runtime_diagnostic(app_state)
        return
    if status not in _LLM_DIAG_STATUSES:
        return
    message = _anthropic_error_message(resp)
    # A plain 400 that isn't a credit-balance error is an agent request bug, not
    # a credential fault — don't raise a false operator alarm for it.
    if status == 400 and "credit" not in message.lower():
        return
    diag = record_llm_runtime_failure(app_state, status, message)
    try:
        audit_repo().log(
            action="broker_llm_auth_failure",
            params={"reason": diag.get("reason"), "status_code": status, "detail": diag.get("detail")},
            result="error",
            client_kind="broker",
        )
    except Exception:
        # Audit logging must never break the request path itself.
        pass
