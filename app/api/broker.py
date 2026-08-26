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
CLI cannot be replayed against the other's route. Admin-mutation paths
(``/api/admin/*``) are hard-rejected — the broker only ever re-authenticates
the interactive-parity flows (catalog reads, queries, MCP tool calls), never
privileged admin writes, regardless of the resolved identity's own grants.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
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
    parse_usage,
    usage_accumulator,
)
from app.auth.access import is_user_admin, mint_agent_session_jwt, mint_co_session_jwt, require_admin
from app.auth.jwt import create_access_token
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
#: completion body far past this is pathological; usage recording is then
#: skipped (logged) rather than holding unbounded memory per request.
_SSE_USAGE_COLLECT_MAX_BYTES = 8 * 1024 * 1024

router = APIRouter(prefix="/api/broker", tags=["broker"])

# Admin mutations are never brokered — the broker replays only the
# interactive-parity surface (catalog/query/MCP), never admin writes,
# regardless of the resolved identity's own grants. The `/api/admin/` prefix
# is only a fast-path; the authoritative gate is `_route_requires_admin`,
# which catches every `Depends(require_admin)` route wherever it lives
# (e.g. `/api/users/*`, `/auth/admin/tokens/*`) — a bare path-prefix check
# missed those (Devin/agnes-review on #846, §11).
_ADMIN_PATH_PREFIX = "/api/admin/"


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
    # gate and catches admin routes at any path (§11).
    if match_path.startswith(_ADMIN_PATH_PREFIX) or _route_requires_admin(request.app, method, match_path):
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
    if row.get("scope") == "llm":
        if await asyncio.to_thread(lambda: chat_session_repo().get_session(row["session_id"])) is None:
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

    # Agent-as-API policy: per-agent model allowlist + monthly token budget
    # (Task 8, agent-profiles V1a). Sits BEFORE the credential fork below so
    # it covers all three upstream modes (static key / WIF / dispatcher), and
    # raises BEFORE any token is spent. Sessions with no bound agent (Slack,
    # legacy, or a session predating this feature) resolve `agent_row` to
    # `None` and skip all of it — behavior is unchanged for them.
    agent_row: Optional[Dict[str, Any]] = None
    caller_user_id: Optional[str] = None
    budget_headers: Dict[str, str] = {}
    chat_cfg = getattr(request.app.state, "chat_config", None)
    if is_messages_post:
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
    use_dispatcher = bool(dispatcher_url) and is_messages_post

    # Credential injection is the ONE thing that differs between auth modes; the
    # sandbox never carries either credential (it's added here, server-side).
    #   dispatcher opt-in      → x-api-key: <LLM_DISPATCHER_API_KEY>
    #   api_key (default)      → x-api-key: <static ANTHROPIC_API_KEY>
    #   workload_identity      → Authorization: Bearer <short-lived federated
    #                            token> + the oauth beta header OAuth-style
    #                            tokens require; NO static key exists.
    llm_auth = getattr(getattr(request.app.state, "chat_config", None), "llm_auth", "api_key")
    wif_mode = llm_auth == "workload_identity" and not use_dispatcher
    if use_dispatcher:
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

    upstream_base = dispatcher_url if use_dispatcher else _ANTHROPIC_BASE_URL
    # Stream-open the upstream call: status + headers arrive immediately, the
    # body stays unread. A 2xx SSE completion is then forwarded chunk-by-chunk
    # (StreamingResponse below) instead of buffered whole — buffering here
    # collapsed every token delta of the model's answer into one burst at
    # turn end, so the user stared at silence and then got the entire text
    # at once. No ``async with``: the client must outlive this handler for
    # the streaming case; the pass-through iterator's ``finally`` closes it.
    client = httpx.AsyncClient(timeout=_ANTHROPIC_TIMEOUT)
    try:
        upstream_req = client.build_request(
            request.method,
            # `normalized_upstream_path` — the SAME canonical value the policy /
            # budget / dispatcher gates classified on above — so the guard and
            # the real destination can never disagree (dot-segments already
            # refused, trailing/duplicate slashes already collapsed).
            f"{upstream_base}{normalized_upstream_path}",
            content=raw_body,
            headers=headers,
            params=request.query_params,
        )
        resp = await client.send(upstream_req, stream=True)
    except BaseException:
        await client.aclose()
        raise
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
        collect_usage = agent_row is not None and resp.status_code == 200
        collected = bytearray()
        state = {"overflow": False}

        async def _passthrough():
            try:
                async for chunk in resp.aiter_bytes():
                    if collect_usage and not state["overflow"]:
                        if len(collected) + len(chunk) <= _SSE_USAGE_COLLECT_MAX_BYTES:
                            collected.extend(chunk)
                        else:
                            state["overflow"] = True
                    yield chunk
            finally:
                await resp.aclose()
                await client.aclose()
                if collect_usage:
                    try:
                        if state["overflow"]:
                            logger.warning(
                                "SSE usage recording skipped for agent %s: stream exceeded %d bytes",
                                agent_row.get("id"),
                                _SSE_USAGE_COLLECT_MAX_BYTES,
                            )
                        else:
                            usage = parse_usage(bytes(collected), ctype)
                            if usage:
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
                            "llm usage recording failed for agent %s (stream already forwarded)",
                            agent_row.get("id"),
                        )

        return StreamingResponse(
            _passthrough(),
            status_code=resp.status_code,
            media_type=ctype,
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
    if agent_row is not None and resp.status_code == 200:
        try:
            usage = parse_usage(resp.content, resp.headers.get("content-type", ""))
            if usage:
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

    return _to_response(resp, budget_headers)


# LLM-credential failure statuses worth an operator signal: auth (invalid /
# expired / unfunded-permission key) and 400 (candidate "credit balance too
# low"). Other 4xx/5xx are the agent's own request errors, not a credential fault.
_LLM_DIAG_STATUSES = (400, 401, 403)


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
