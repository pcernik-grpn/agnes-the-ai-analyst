"""FastAPI auth dependencies — current user resolution.

Authorization helpers (require_admin, require_resource_access) live in
``app.auth.access`` to avoid a circular import — they need ``get_current_user``
from this module and ``_get_db``, which both come from here.
"""

import json
import logging
import os
from typing import Optional

import duckdb
from fastapi import Depends, HTTPException, Header, Request, status

from app.auth.jwt import verify_token
from src.db import get_system_db

logger = logging.getLogger(__name__)

# Default dev user used when LOCAL_DEV_MODE=1. Seeded at startup by app/main.py.
LOCAL_DEV_DEFAULT_EMAIL = "dev@localhost"

# Single-slot cache for the parsed LOCAL_DEV_GROUPS value, keyed by the raw env
# string. Avoids re-parsing JSON on every authenticated request without the
# surprise of test isolation issues — when the env changes (typical in tests),
# the key changes and the cache transparently re-parses.
_LOCAL_DEV_GROUPS_CACHE: tuple[str, list[dict]] | None = None

# Map pat_resolver.ResolutionReason → HTTP 401 `detail` string. Preserves the
# specific user-facing messages that existed before the pat_resolver refactor
# (Account deactivated, Token revoked, ...) so tests and admin UX that grep
# for these phrases keep working.
_AUTH_DETAIL_BY_REASON = {
    "deactivated": "Account deactivated",
    "user_not_found": "User not found",
    "pat_unknown": "Token unknown",
    "pat_revoked": "Token revoked",
    "pat_expired": "Token expired",
    "pat_mismatch": "Token mismatch",
    "pat_scope_forbidden": "git_scope_token_not_allowed",
    "invalid_token": "Invalid or expired token",
    "no_token": "Invalid or expired token",
    "agent_pat_wrong_surface": "Agent token not valid on this surface",
    "agent_pat_agent_deleted": "Agent deleted",
    "pat_parent_revoked": "Token revoked",
    "session_revoked": "Session revoked — please sign in again",
}


def auth_detail_for_reason(reason: str | None) -> str:
    """Human 401 ``detail`` for a ``pat_resolver.ResolutionReason``.

    The public accessor for the vocabulary above, so a non-REST surface that
    rejects a credential can say the same thing the REST surface says instead
    of inventing a second wording. Used by the MCP SSE transport's
    ``_AuthMiddleware`` (``app/api/mcp_http.py``), which used to collapse every
    rejection — and its own internal errors — into one fixed
    "Not authenticated".

    An unknown or absent reason falls back to the deliberately vague "Invalid
    or expired token".
    """
    return _AUTH_DETAIL_BY_REASON.get(reason or "", "Invalid or expired token")


# X-StorageApi-Token header rejections → 401 detail. Reasons come from
# app.auth.keboola_header.resolve_header_user.
_KEBOOLA_HEADER_DETAIL = {
    "keboola_user_unknown": "No account exists for this Keboola identity — sign in via the web login first",
    "not_master_token": "Only a master (admin) Storage API token can authenticate",
    "no_admin_identity": "The verified token carries no admin identity",
    "project_mismatch": "The token belongs to a different Keboola project than this instance",
    "role_forbidden": "This Keboola project role is not permitted on this instance",
    "deactivated": "Account deactivated",
    "invalid_token": "Invalid or expired token",
    "verify_failed": "Could not verify the token against the Keboola stack",
    "not_configured": "Keboola token authentication is not configured",
    # Transient-failure reasons ("rate_limited" is special-cased to 429 before
    # this map): the .get() fallback says "Invalid or expired token", which
    # would tell a caller hitting an outage to rotate a good credential
    # (Devin Review on PR #1288). Both must read as retryable.
    "keboola_verify_error": "Token verification failed unexpectedly — this is a server-side problem, retry later",
    "keboola_lookup_error": "The token verified but the account lookup failed — this is a server-side problem, retry later",
}


def is_local_dev_mode() -> bool:
    """True when LOCAL_DEV_MODE=1 — unsafe for production, bypasses auth."""
    return os.environ.get("LOCAL_DEV_MODE", "").lower() in ("1", "true", "yes")


def get_local_dev_email() -> str:
    """Email of the auto-logged-in dev user. Configurable via LOCAL_DEV_USER_EMAIL."""
    return os.environ.get("LOCAL_DEV_USER_EMAIL", LOCAL_DEV_DEFAULT_EMAIL)


def get_local_dev_groups() -> list[dict]:
    """Mock Google Workspace groups for the dev user when LOCAL_DEV_MODE is on.

    Reads ``LOCAL_DEV_GROUPS`` as a JSON array of objects matching the shape
    produced by ``_fetch_google_groups`` — ``[{"id": "...", "name": "..."}]``.
    Items must have a non-empty ``id``; ``name`` defaults to ``id`` when
    omitted. Extra fields are preserved verbatim so future group attributes
    (roles, labels, …) can be mocked without touching this parser.

    Returns ``[]`` on missing/empty/malformed input — dev mock must never
    break the dev flow. Malformed input is logged at WARNING.

    Cached single-slot: re-parses only when the raw env-var value changes.
    """
    global _LOCAL_DEV_GROUPS_CACHE
    raw = os.environ.get("LOCAL_DEV_GROUPS", "").strip()
    if _LOCAL_DEV_GROUPS_CACHE is not None and _LOCAL_DEV_GROUPS_CACHE[0] == raw:
        return _LOCAL_DEV_GROUPS_CACHE[1]
    result = _parse_local_dev_groups(raw)
    _LOCAL_DEV_GROUPS_CACHE = (raw, result)
    return result


def _parse_local_dev_groups(raw: str) -> list[dict]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("LOCAL_DEV_GROUPS is not valid JSON, ignoring: %s", e)
        return []
    if not isinstance(parsed, list):
        logger.warning(
            "LOCAL_DEV_GROUPS must be a JSON array, got %s — ignoring",
            type(parsed).__name__,
        )
        return []
    out: list[dict] = []
    for item in parsed:
        if not isinstance(item, dict) or not item.get("id"):
            logger.warning(
                "LOCAL_DEV_GROUPS item must be an object with 'id', skipping: %r",
                item,
            )
            continue
        # Don't mutate the parsed input — keeps the parser pure so the cache
        # value stays a fresh list on each rebuild.
        out.append({**item, "name": item.get("name") or item["id"]})
    return out


def _get_db():
    # On a Postgres instance the system state lives in PG, not the system
    # DuckDB — opening it here would create a stale ``state/system.duckdb`` file
    # (and is a hard error once ``get_system_db()`` enforces the invariant).
    # Yield ``None``: every consumer of this dependency routes its state reads
    # through the repository factory under ``use_pg()``, so the conn is
    # vestigial on Postgres.
    from src.repositories import use_pg

    if use_pg():
        yield None
        return
    conn = get_system_db()
    try:
        yield conn
    finally:
        conn.close()


def _client_ip(request: Optional[Request]) -> Optional[str]:
    """Return the request's client IP (security audit F9).

    Delegates to :func:`app.auth.client_ip.trusted_client_ip`, which trusts only
    the ``AGNES_TRUSTED_PROXY_HOPS`` rightmost X-Forwarded-For hops rather than
    the fully client-controllable leftmost hop. Value is stored in
    personal_access_tokens.last_used_ip and audit_log entries — informational
    only, never authorization.
    """
    from app.auth.client_ip import trusted_client_ip

    return trusted_client_ip(request)


def _get_local_dev_user(conn: Optional[duckdb.DuckDBPyConnection] = None) -> Optional[dict]:
    """Return the seeded dev user when LOCAL_DEV_MODE is on, else None.

    ``conn`` retained for signature compat; ignored — uses the factory.
    """
    from src.repositories import users_repo

    # Folded AND stripped: startup seeds this account through normalize_email,
    # which does both, while `get_by_email_ci` folds case in SQL but does not
    # trim. A configured address carrying a stray leading/trailing space would
    # therefore be seeded as `dev@local` and read as `" dev@local "`, and dev
    # auto-login would silently stop working. Same trim-before-the-read fix as
    # `agnes admin break-glass grant-admin`.
    from src.user_identity import normalize_email

    user = users_repo().get_by_email_ci(normalize_email(get_local_dev_email()))
    if not user:
        logger.error(
            "LOCAL_DEV_MODE is on but dev user %s is not seeded; expected app startup to seed it",
            get_local_dev_email(),
        )
    return user


def _stash_chat_session_id_from_token(request: Optional[Request], token: str) -> None:
    """Decode ``token`` and, if it carries ``scope=chat`` plus a
    ``chat_session_id`` claim, stash that claim on ``request.state``.

    Called from ``get_current_user`` after the bearer is validated. The
    per-session BigQuery scan budget in ``app/api/query.py`` reads
    ``request.state.chat_session_id`` to charge the right bucket; without
    this stash, the chat-side budget would silently never accumulate even
    though ``mint_session_jwt`` already embeds the claim. Non-chat tokens
    (regular session/PAT) leave ``request.state`` untouched.
    """
    if request is None:
        return
    try:
        from app.auth.jwt import verify_token as _verify

        payload = _verify(token) or {}
    except Exception:
        return
    if payload.get("scope") != "chat":
        return
    session_id = payload.get("chat_session_id")
    if not session_id:
        return
    try:
        request.state.chat_session_id = session_id
    except Exception:
        pass


def _stash_user(request: Optional[Request], user: dict) -> dict:
    """Park the resolved user on ``request.state.user``.

    Read by response-phase middleware (e.g. the PostHog snippet injector
    and the 500 handler) so they can identify the actor without re-running
    the auth dependency. Tolerant of ``None`` requests (background paths
    that call this helper from non-HTTP contexts).

    Also the single funnel every dict-shaped authenticated user passes
    through on its way out of ``get_current_user`` (local-dev, the scheduler
    shared secret, the X-StorageApi-Token header path, and the normal
    Authorization/cookie token path all return through here) — so this is
    "the shared get_current_user return path" the F0 audit-context contract
    means by "stamp audit identity after user resolution, one place, every
    authenticated request" (a restricted principal — SessionPrincipal /
    AgentPrincipal — returns directly from ``get_current_user`` without
    going through here, since it's a frozen dataclass this function's
    ``request.state.user = user`` assignment would reject anyway).
    """
    # Tell the elevation gate whose pause this request carries: the
    # middleware stamps the flag before authentication and cannot know the
    # caller, and can_access is also asked about OTHER users (Devin Review
    # on #1146).
    try:
        from app.auth.elevation import set_caller_for_request

        set_caller_for_request(str(user.get("id")) if user else None)
    except Exception:
        pass
    try:
        from src.audit_context import auto_client_kind, set_audit_identity, set_client_kind
        from src.audit_helpers import client_kind_from_user, identity_for_audit

        set_audit_identity(*identity_for_audit(user))
        # Never downgrade an already-stamped non-web kind (mcp/slack/
        # telegram/...) — a surface-specific stamp made before auth ran
        # (e.g. an MCP session's own context setup) must survive the
        # generic web/cli/scheduler classification below.
        if auto_client_kind() in (None, "web"):
            set_client_kind(client_kind_from_user(user))
    except Exception:
        pass
    if request is not None:
        try:
            request.state.user = user
        except Exception:
            pass
    return user


def get_current_user(
    request: Request = None,
    authorization: Optional[str] = Header(None),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> dict:
    """Extract and validate JWT from Authorization header or cookie. Returns user dict.

    No role hydration, no session caches — authorization is decided at gate
    time by ``app.auth.access`` which reads ``user_group_members`` directly.

    Plain ``def`` (not ``async def``) so FastAPI auto-offloads it to the anyio
    thread pool — the body is a synchronous RBAC/token read (``pat_resolver``,
    ``users_repo``, ``is_user_admin``) that on a Postgres instance blocks on a
    sync SQLAlchemy query. Auth runs on nearly every request; under ``async
    def`` a single slow auth query holds the single uvicorn event loop and
    freezes the whole process (→ 503 "system unavailable"). See PR #188's Tier
    1 event-loop unblocking rollout for the convention.
    """
    if is_local_dev_mode():
        user = _get_local_dev_user(conn)
        if user:
            _attach_admin_flag(user, conn)
            return _stash_user(request, user)
        # Fall through to normal auth if seed missing — surfaces the bug
        # instead of hiding it.

    token = None

    # Try Authorization header first
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ")

    # Fallback to cookie (for web UI after OAuth redirect)
    if not token and request:
        token = request.cookies.get("access_token")

    if not token:
        # X-StorageApi-Token is consulted ONLY when no bearer credential and
        # no session cookie are present — a Storage token never shadows an
        # established Agnes credential (spec precedence rule).
        sapi_token = request.headers.get("X-StorageApi-Token") if request is not None else None
        if sapi_token:
            from app.auth.keboola_header import enabled as keboola_header_enabled
            from app.auth.keboola_header import resolve_header_user

            if keboola_header_enabled():
                user, kb_reason = resolve_header_user(sapi_token, request)
                if user:
                    _attach_admin_flag(user, conn)
                    return _stash_user(request, user)
                if kb_reason == "rate_limited":
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail="Too many token verification attempts — retry later",
                    )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=_KEBOOLA_HEADER_DETAIL.get(kb_reason, "Invalid or expired token"),
                )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )

    # Shared-secret path for the in-cluster scheduler. Checked before
    # pat_resolver because the scheduler token is not a JWT — feeding it to
    # verify_token() would log a spurious decode warning every cron tick.
    # See app/auth/scheduler_token.py for the threat model.
    from app.auth.scheduler_token import get_scheduler_user, is_scheduler_token

    if is_scheduler_token(token):
        scheduler_user = get_scheduler_user(conn)
        if scheduler_user:
            _attach_admin_flag(scheduler_user, conn)
            return _stash_user(request, scheduler_user)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Scheduler user not provisioned",
        )

    from app.auth.pat_resolver import resolve_token_to_user
    from app.auth.session_principal import PRINCIPAL_TYPES

    user, reason = resolve_token_to_user(conn, token, request)
    if isinstance(user, PRINCIPAL_TYPES):
        # A restricted principal is returned verbatim: it is a frozen
        # dataclass, so ``_attach_admin_flag`` / ``_stash_user`` (both of
        # which assign into the user dict) would raise. It also must never
        # carry ``is_admin`` — the admin seam denies principals outright.
        #
        # The chat-session claim IS stashed first: the per-session BQ scan
        # accumulator (app/api/query.py::_maybe_charge_chat_session_bq_budget)
        # reads request.state.chat_session_id, and without this a scoped
        # agent's (or co-session's) brokered queries would escape the
        # per-session scan cap that scope="chat" promises to keep.
        _stash_chat_session_id_from_token(request, token)
        return user
    if user:
        _attach_admin_flag(user, conn)
        # Propagate token kind so audit helpers can tag client_kind correctly.
        payload = verify_token(token) or {}
        if payload.get("typ") == "pat":
            user["token_type"] = "pat"
        # Park chat-session claim on request.state so the BQ scan accumulator
        # in app/api/query.py can charge the per-session budget bucket.
        _stash_chat_session_id_from_token(request, token)
        return _stash_user(request, user)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=_AUTH_DETAIL_BY_REASON.get(reason, "Invalid or expired token"),
    )


def _attach_admin_flag(user: dict, conn: duckdb.DuckDBPyConnection) -> None:
    """Inject ``user["is_admin"]`` so templates and route handlers can gate
    admin-only UI without touching the legacy ``users.role`` column.

    v13 nulled out ``users.role`` and moved admin authority onto
    ``user_group_members`` (Admin system group). The web header used to
    gate its admin nav on ``session.user.role == 'admin'``, which silently
    became false for every user — so no admin saw any admin menu items
    after the v13 migration. Computing the flag once per request here
    keeps every consumer in sync with ``app.auth.access.is_user_admin``
    (the same call all server-side admin gates use).

    ``is_admin`` is EFFECTIVE authority, so it honors the elevation consent
    gate: an admin who paused their own elevation gets ``False`` here, because
    ``require_admin`` refuses that request (403 ``admin_elevation_paused``)
    and chrome gated on the raw membership sent them straight into it — the
    rail kept an Admin row that only ever produced an error page. The
    middleware stamps the pause before authentication (see app/main.py), so
    the flag is resolvable at this point.

    ``is_admin_paused`` carries the difference — in the Admin group, but
    paused — so chrome can say *why* the admin surfaces are gone and link to
    the switch (on /me/profile, deliberately outside admin-gated UI, so a
    paused admin is never stranded). Enforcement paths keep calling
    ``is_user_admin`` / ``require_admin`` directly; this pair is for
    rendering.
    """
    from app.auth.access import is_user_admin
    from app.auth.elevation import elevation_paused

    user_id = user.get("id")
    if user_id:
        try:
            in_admin_group = is_user_admin(user_id, conn)
        except Exception:
            in_admin_group = False
        # Subject-scoped: the pause is this person pausing their own god-mode
        # (an unstamped caller still honors it — reduction is always safe).
        paused = bool(in_admin_group) and elevation_paused(str(user_id))
        user["is_admin"] = bool(in_admin_group) and not paused
        user["is_admin_paused"] = paused
    else:
        user["is_admin"] = False
        user["is_admin_paused"] = False


def get_optional_user(
    request: Request = None,
    authorization: Optional[str] = Header(None),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> Optional[dict]:
    """Like get_current_user but returns None instead of 401 if no token.

    Plain ``def`` (not ``async def``) so FastAPI offloads it to the anyio
    thread pool — same Tier 1 rationale as ``get_current_user``, which it
    calls synchronously (a direct call, not via ``Depends``).
    """
    try:
        return get_current_user(request=request, authorization=authorization, conn=conn)
    except HTTPException:
        return None


def non_interactive_credential_kind(request: Request) -> Optional[str]:
    """Name the non-interactive credential kind on ``request``, or None.

    Returns a short noun phrase — ``"a Storage API token"``, ``"a service
    token"``, ``"a PAT"`` — naming why this request must not be treated as
    an interactive session, or ``None`` when it carries one (a browser
    cookie / session JWT) or no credential at all.

    This is the single definition of the rule ``require_session_token``
    enforces. It is a plain function, not a dependency, so surfaces that
    CANNOT take a FastAPI ``Depends(...)`` — the plain-Starlette OAuth
    consent bridge in ``app.auth.mcp_oauth``, which is deliberately kept off
    the documented JSON-API surface — apply the identical classification
    instead of growing a second, silently drifting hand-rolled copy. A
    hand-rolled copy is exactly how the MCP-OAuth consent route came to
    accept a plain PAT for minting a 30-day refresh token.

    Note this classifies the CREDENTIAL, not the caller's authority: it says
    nothing about whether the token is valid, live, or authorized. Callers
    must still authenticate the request separately.
    """
    auth = request.headers.get("authorization", "")
    token = None
    if auth.startswith("Bearer "):
        token = auth.removeprefix("Bearer ")
    if not token and request:
        token = request.cookies.get("access_token")
    if not token and request.headers.get("x-storageapi-token"):
        # A request authenticated by the X-StorageApi-Token header is a
        # non-interactive service credential (get_current_user resolved it) —
        # it must never mint PATs, connect MCP, or manage agents, exactly
        # like a PAT. Without this check the header path would be classified
        # as an interactive session because only Authorization/cookie are
        # inspected here.
        return "a Storage API token"
    if token:
        from app.auth.scheduler_token import is_scheduler_token

        if is_scheduler_token(token):
            return "a service token"
        from app.auth.jwt import verify_token
        from app.auth.pat_resolver import _PAT_LIKE_TYPES

        payload = verify_token(token) or {}
        if payload.get("typ") in _PAT_LIKE_TYPES:
            return "a PAT"
    return None


def revocable_credential_id(request: Request) -> Optional[str]:
    """Id of the revocable credential authenticating ``request``, or None.

    Returns the ``jti`` of a PAT-like token (``typ`` in
    ``pat_resolver._PAT_LIKE_TYPES``) — the only credential kinds backed by a
    ``personal_access_tokens`` row that an operator can actually revoke. Every
    other kind (browser cookie / session JWT, the chat-runner token, the
    scheduler shared secret, local-dev bypass, no credential at all) returns
    None: there is no row to bind to, so a token minted from one is
    unconstrained exactly as it was before.

    Used by ``app.api.data_apps`` to stamp
    ``pat_resolver.PARENT_TOKEN_ID_CLAIM`` onto a minted ``data-app-git:``
    credential, so revoking the PAT that asked for it revokes the credential
    too. Sibling of ``non_interactive_credential_kind`` in shape and for the
    same reason: a plain function, not a dependency, so a surface that cannot
    take a FastAPI ``Depends(...)`` reuses it verbatim instead of growing a
    hand-rolled copy that silently drifts.

    Classifies the CREDENTIAL, not the caller's authority: it says nothing
    about whether the token is valid, live, or authorized. Callers must still
    authenticate the request separately — in practice this runs behind
    ``get_current_user``, which has already done so.
    """
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else None
    if not token:
        token = request.cookies.get("access_token")
    if not token:
        return None
    from app.auth.pat_resolver import _PAT_LIKE_TYPES

    payload = verify_token(token) or {}
    if payload.get("typ") not in _PAT_LIKE_TYPES:
        return None
    return payload.get("jti") or None


def require_session_token(request: Request, user: dict = Depends(get_current_user)) -> dict:
    """Like get_current_user but rejects every non-interactive token kind —
    for endpoints that must not be callable via a long-lived service or CI
    credential (e.g. creating new tokens, changing password).

    Two non-interactive paths exist today:

    1. **PAT / agent PAT** — JWT with ``typ`` in ``pat_resolver._PAT_LIKE_TYPES``
       (``"pat"`` or ``"agent_pat"``). Detected by decoding the JWT and
       inspecting the claim. Agent PATs are already surface-allowlisted to
       ``/api/v1/{agents,sessions,jobs}/`` in ``pat_resolver``, but that
       allowlist is enforced in ``resolve_token_to_user`` — this dependency
       runs independently (via ``get_current_user``), so it must reject them
       here too, the same as a plain PAT.
    2. **Scheduler shared secret** — opaque string equal to
       ``SCHEDULER_API_TOKEN``. Not a JWT, so ``verify_token`` returns None
       and the PAT-claim check would silently pass — meaning a caller
       holding the scheduler secret could mint persistent PATs through
       ``POST /auth/tokens`` that survive a secret rotation. Explicit
       check here closes that bypass.

    The classification itself lives in ``non_interactive_credential_kind``
    so non-FastAPI surfaces can reuse it verbatim.

    Plain ``def`` (not ``async def``) so FastAPI offloads it to the anyio
    thread pool — the body is sync token inspection + the sync RBAC read in
    the ``get_current_user`` dependency it depends on (Tier 1, PR #188).
    """
    kind = non_interactive_credential_kind(request)
    if kind is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"This endpoint requires an interactive session, not {kind}",
        )
    return user


def require_session_or_user_pat(*, allow_stack_surface: bool = False):
    """Factory for a dependency like ``require_session_token``, but ALSO
    accepting a user PAT (``typ="pat"``) — for READ-ONLY agent-management
    endpoints only. Mutating agent-management routes
    (create/update/delete/scope/token issuance/memory writes) keep using
    ``require_session_token`` unchanged.

    Motivation: ``agnes agent list`` — the very command `agnes chat`'s own
    error text points a caller at — must work for a normally-logged-in
    analyst's own PAT, not force a fresh interactive session just to
    discover which agents exist. ``agnes login`` / ``agnes init`` both mint
    ``surface='stack'`` PATs by default (`app/api/cli_auth.py`), so a fix
    that only accepted ``surface='all'`` would not actually help the common
    case — see (3) below.

    ``allow_stack_surface`` (default ``False``) controls whether a
    ``surface='stack'`` PAT qualifies, on top of a full-surface
    (``surface='all'``) one, which always qualifies:

    - ``allow_stack_surface=True`` — used by ``GET /api/v1/agents``,
      ``GET /api/v1/agents/{id}``, ``GET /api/v1/agents/{slug}/schedules``.
      ``surface='stack'`` narrows *data reads* (it drops an admin's
      god-mode short-circuit to the analyst stack branch — see
      ``src/rbac.py``'s ``_credential_surface``); it was never meant to hide
      the caller's own agent *metadata* (name, slug, scope shape, schedule
      cadence), and accepting it here never widens data access.
    - ``allow_stack_surface=False`` (default) — used by
      ``GET /api/v1/agents/{id}/memories``. A memory notebook can hold
      free-text content the owner wrote or an agent inferred, the most
      sensitive of the four read surfaces — kept on the conservative
      full-surface-PAT-or-session default; reviewers may widen this later.

    Still rejects, fail-closed, regardless of ``allow_stack_surface``:

    1. **Every restricted principal** (``SessionPrincipal`` / ``AgentPrincipal``,
       ``PRINCIPAL_TYPES``) — neither carries a single owner identity these
       owner-scoped reads can run against, and an ``AgentPrincipal`` is the
       sandbox's own narrowed credential, which must never drive the
       owner-facing agent API.
    2. **An agent-scoped PAT** (``typ="agent_pat"``) — an agent must never
       enumerate or read its OWNER's *other* agents just because it holds a
       PAT. Denied unconditionally, regardless of surface.
    3. **Scheduler shared secret / ``X-StorageApi-Token`` header credential**
       — same non-interactive-service exclusions as ``require_session_token``.
    4. **Any ``credential_surface`` value other than ``'all'`` or (when
       allowed) ``'stack'``** — an unrecognized/future surface value fails
       closed rather than silently qualifying.

    The surface check in (4) applies to EVERY credential that carries a
    ``credential_surface`` tag, not just ``typ="pat"`` ones. Two non-PAT
    credential kinds are also stamped ``credential_surface="stack"`` by
    ``resolve_token_to_user`` (``app/auth/pat_resolver.py``): a session JWT
    minted for an AGENT surface — the chat-sandbox token from
    ``mint_session_jwt`` (no ``typ`` claim at all) and an MCP-OAuth connector
    token (``typ="session"``, ``scope="mcp-oauth"``). Gating the surface
    check on ``typ in _PAT_LIKE_TYPES`` would let both slip through
    unchecked on every route including ``memories`` — the same rule must
    apply to them as to a PAT carrying the same surface tag. A credential
    with no ``credential_surface`` key at all (a genuine interactive browser
    session) reads as ``'all'``, same convention as
    ``src/rbac.py``'s ``_credential_surface`` helper, and is unaffected.
    """

    def _dependency(request: Request, user: dict = Depends(get_current_user)) -> dict:
        """Plain ``def`` — same Tier 1 threadpool convention as
        ``require_session_token`` (PR #188)."""
        from app.auth.session_principal import PRINCIPAL_TYPES

        if isinstance(user, PRINCIPAL_TYPES):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This endpoint requires an interactive session or a user PAT",
            )

        auth = request.headers.get("authorization", "")
        token = None
        if auth.startswith("Bearer "):
            token = auth.removeprefix("Bearer ")
        if not token and request:
            token = request.cookies.get("access_token")
        if not token and request.headers.get("x-storageapi-token"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This endpoint requires an interactive session or a user PAT, not a Storage API token",
            )
        if token:
            from app.auth.scheduler_token import is_scheduler_token

            if is_scheduler_token(token):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="This endpoint requires an interactive session or a user PAT, not a service token",
                )
            from app.auth.jwt import verify_token

            payload = verify_token(token) or {}
            if payload.get("typ") == "agent_pat":
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="This endpoint requires an interactive session or a qualifying user PAT",
                )

            # Surface check is keyed on the RESOLVED credential_surface, not
            # on typ — a sandbox (mint_session_jwt) or MCP-OAuth token
            # carrying credential_surface='stack' must be held to the same
            # rule as a surface='stack' PAT (see docstring). No key at all
            # reads as 'all' and always qualifies.
            surface = user.get("credential_surface")
            if surface is not None and surface != "all":
                qualifies = allow_stack_surface and surface == "stack"
                if not qualifies:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="This endpoint requires an interactive session or a qualifying user PAT",
                    )
        return user

    return _dependency


def reject_keboola_header_credential(user: dict = Depends(get_current_user)) -> dict:
    """Block the X-StorageApi-Token header credential from routes that mint
    durable follow-on credentials — a Cowork setup bundle (setup token +
    pre-baked PAT), or a data-app service/git-push/preview-cookie token.

    Narrower than ``require_session_token``: it rejects ONLY
    ``token_type == "keboola_token"`` (the header path), not a regular PAT.
    Blocking PATs here too would be a second, unrelated hardening (tracked
    separately — #1292) and is out of scope for closing this laundering
    hole: a Storage API token — a data-plane credential meant for
    programmatic Storage access, never displayed to the holder as an
    Agnes login — must never be exchanged for a persistent Agnes PAT or a
    data-app credential, exactly as ``require_session_token`` already
    guarantees for the endpoints it gates (token issuance, MCP connect,
    agent management).

    Applied as a route-level dependency (``dependencies=[Depends(...)]`` on
    the ``@router`` decorator) rather than a handler parameter, so the
    handler's own ``user: dict = Depends(get_current_user)`` keeps
    populating ``user`` — FastAPI's per-request dependency cache dedupes
    the two ``Depends(get_current_user)`` calls, so auth only runs once.

    Plain ``def`` — same Tier 1 threadpool convention as
    ``require_session_token`` (PR #188): the body is a synchronous dict
    read, and ``async def`` would offload nothing while risking blocking
    the event loop if ``get_current_user`` itself ever grows a blocking
    call.
    """
    if isinstance(user, dict) and user.get("token_type") == "keboola_token":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint requires an interactive or PAT session, not a Storage API token",
        )
    return user
