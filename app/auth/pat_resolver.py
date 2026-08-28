"""Shared token → user resolution.

Both the JSON API (Bearer header / cookie) and the git smart-HTTP endpoint
(HTTP Basic where the password field carries the PAT) need the same chain:

    verify JWT → user exists & active → if typ=pat: still valid in DB →
    best-effort audit & last-used bookkeeping → return user dict.

Extracted from `app.auth.dependencies.get_current_user` so both paths run
identical checks. Returns `(user, reason)`:

  - on success: `(user_dict, None)`
  - on failure: `(None, reason)` where reason is one of the strings below

The reason lets `get_current_user` map to a specific HTTP 401 detail
(`"Account deactivated"`, `"Token revoked"`, ...) while the WSGI git router
can discard it and just treat any non-None reason as unauthenticated.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Literal, Optional, Tuple

import duckdb
from fastapi import Request

from app.auth.jwt import verify_token

logger = logging.getLogger(__name__)

ResolutionReason = Literal[
    "no_token",
    "invalid_token",
    "user_not_found",
    "deactivated",
    "pat_unknown",
    "pat_revoked",
    "pat_expired",
    "pat_mismatch",
    "agent_pat_wrong_surface",
    "agent_pat_agent_deleted",
    "pat_scope_forbidden",
    "pat_parent_revoked",
    "session_revoked",
]

# Path prefixes an agent PAT (typ="agent_pat") is allowed to authenticate
# against. Everything else — legacy `/api/*`, `/git/`, `/marketplace.zip`,
# and the `/api/v1/agents` *management* verbs (those use session auth) —
# hard-rejects with "agent_pat_wrong_surface". Tuple (not a set) because it
# is consumed via ``str.startswith(prefixes_tuple)``.
_AGENT_PAT_ALLOWED_PREFIXES = ("/api/v1/agents/", "/api/v1/sessions/", "/api/v1/jobs/")

# JWT `typ` values that live in `personal_access_tokens` and must run the
# same DB-backed validity chain (revoked/expired/unknown/hash-mismatch +
# last-used bookkeeping) below.
_PAT_LIKE_TYPES = ("pat", "agent_pat")

# Claim naming the credential a token was minted ON BEHALF OF — set by
# `app.api.data_apps._mint_git_credential` when the mint request itself
# authenticated with a PAT. A minted successor must not outlive its minter:
# revoking a leaked PAT has to revoke what that PAT already produced, or the
# one incident-response move that matters leaves the door open for the
# successor's full TTL. Checked in `resolve_token_to_user` (not at any single
# consuming surface) so every surface that ever accepts such a token inherits
# the binding and no second, drifting copy of the rule can appear.
#
# ABSENT CLAIM MEANS NO PARENT, and is unconstrained — the container clone
# token (`_mint_container_git_token`, minted by the deploy path, not by a
# caller credential), the broker's per-request token (revoked in its own
# `finally`), credentials minted from an interactive session JWT (no
# revocable row to bind to), and every credential minted before this release.
PARENT_TOKEN_ID_CLAIM = "parent_token_id"


def _stash_payload(request: Optional[Request], payload: dict) -> None:
    """Stash the verified JWT payload on ``request.state.token_payload`` so
    `agent_id_from_request` can read claims off the request without
    re-verifying the JWT.

    Called ONLY on this function's successful-resolution return paths (never
    right after `verify_token` succeeds) — a token that decodes fine but then
    fails a later check (revoked/expired/mismatched/wrong-surface/deleted
    agent) must leave no stashed payload behind. Before this was moved here,
    the stash ran unconditionally right after JWT verification, so a
    revoked/expired/wrong-surface token still left its claims readable by
    `agent_id_from_request` for the rest of the request — including its
    `agent_id`, letting a request whose auth outright failed still resolve
    "which agent" a caller further down the dependency chain might
    mistakenly treat as authenticated.
    """
    if request is not None:
        try:
            request.state.token_payload = payload
        except Exception:
            pass


# Scope prefix minted by `app.api.data_apps._mint_git_credential` for the
# data-app git-over-HTTP authoring surface. A token carrying this scope must
# never authenticate the JSON API (or any other git-over-HTTP surface, e.g.
# the marketplace's) — only `app/api/data_apps_git.py` may pass
# `allow_data_app_git_scope=True` to accept it. Without this check the
# credential is, in practice, a full-privilege user PAT: nothing else reads
# the `scope` claim, so a sandboxed authoring agent holding it could call any
# non-admin (or admin, if the owner is Admin) REST/MCP endpoint.
DATA_APP_GIT_SCOPE_PREFIX = "data-app-git:"

# Scope prefix minted by `app.api.data_apps._mint_preview_token` for the
# short-TTL in-chat preview-iframe capability (wave 3C, spec §7 / Q4). Mirrors
# `DATA_APP_GIT_SCOPE_PREFIX` exactly: a token carrying this scope must never
# authenticate the JSON control-plane API (or anything else) — only
# `app/api/data_apps_proxy.py`'s view-only serving path may pass
# `allow_data_app_preview_scope=True` to accept it, and even there the caller
# still pins the scope's `<slug>` to the requested app before treating the
# resolved identity as authorized (a preview token minted for one app must
# never authorize viewing a different one).
DATA_APP_PREVIEW_SCOPE_PREFIX = "data-app-preview:"

# Scope prefix minted by `app.api.data_apps._mint_service_token` for the
# RUNTIME credential a hosted app calls the Agnes API with (`AGNES_TOKEN`).
#
# Unlike its two siblings above, this credential legitimately needs MANY
# endpoints — it is a data client, "exactly like the CLI and MCP surfaces"
# (spec `2026-07-21-data-apps-design.md`) — so a per-surface boolean does not
# fit. It is gated the way agent PATs are instead: a fail-closed path
# allowlist — `_DATA_APP_ALLOWED_EXACT` + `_DATA_APP_ALLOWED_SUBTREES` below,
# applied by `_data_app_path_allowed`.
#
# Note the prefixes do not overlap: `"data-app-git:x".startswith("data-app:")`
# is False (`-` vs `:` at index 8), so a clone credential still falls to its
# own gate above rather than being reclassified as a service token. That is
# load-bearing and pinned by a test.
DATA_APP_SERVICE_SCOPE_PREFIX = "data-app:"

# The API surface a hosted data app may reach.
#
# Split into exact paths and subtrees ON PURPOSE. One entry per router would
# be shorter, but it admits every route that router will ever grow: an
# earlier draft of this gate listed a bare `/api/query` + `/api/semantic-
# models` and thereby handed apps `POST /api/query/hybrid` (the admin-only
# BigQuery+local join) and `POST /api/semantic-models/apply` (create-or-
# replace of a semantic model by slug, when the owner is an Admin) for free.
# The allowlist matches paths, not handlers, so it cannot tell a read from a
# write on its own — the narrowness has to be written down here, and
# `tests/test_data_app_service_scope.py::test_the_admitted_route_set_is_pinned`
# walks the real route table so a newly-added route under one of these can
# never be admitted silently.
#
# Deliberately absent: `/api/admin/*`; `POST /cli/auth/rescope-surface`
# (admin-gated but PAT-requiring, and it mints a fresh 90-day `surface='all'`
# PAT — the one real credential-laundering path this scope was open to,
# since the service token itself is minted WITHOUT expiry); and the
# `require_session_token` minting routes (`/auth/tokens`,
# `/api/user/cowork-bundle`, `/api/mcp-connect/token`), which already refuse
# any PAT-typed credential regardless of scope. Those last three are covered
# here as defence in depth, not because this gate is what closes them.

# Exact paths — no children admitted.
_DATA_APP_ALLOWED_EXACT = frozenset(
    {
        "/api/query",  # SQL; NOT /api/query/hybrid
        "/api/catalog/tables",
        # Semantic layer, read-only members only. Never `/apply`, and the
        # `{slug}.yaml` document download is left out until an app needs it —
        # a subtree entry here would re-admit `/apply`.
        "/api/semantic-models/context",
        "/api/semantic-models/schema",
        "/api/semantic-models/search",
        "/api/semantic-models/validate-query",
        # v2 — what the `agnes` CLI actually calls. The spec sanctions
        # installing the CLI inside an app, and CLAUDE.md's discovery
        # protocol (`agnes catalog` / `schema` / `describe` / `snapshot
        # create`) is backed entirely by `/api/v2/*`. NOT
        # `/api/v2/metadata-cache/refresh` (admin) or `/api/v2/marketplace/*`.
        "/api/v2/catalog",
        "/api/v2/scan",
        "/api/v2/scan/estimate",
        "/api/v2/metadata-cache/status",
    }
)

# Subtrees — the entry itself and anything below it. Used only where the
# route carries a path parameter, so an exact list is impossible.
_DATA_APP_ALLOWED_SUBTREES = (
    "/api/data",  # /{table_id}/download, /{table_id}/check-access
    "/api/catalog/profile",  # /{table_name}, /{table_name}/refresh
    "/api/catalog/metrics",  # /{metric_path:path}
    "/api/metrics",  # bare + /{metric_id:path}
    "/api/glossary",  # bare + /search + /{glossary_id:path}
    "/api/v2/schema",  # /{table_id}
    "/api/v2/sample",  # /{table_id}
)


def _data_app_path_allowed(path: str) -> bool:
    """Is `path` on the hosted-app data surface?

    Subtrees match exact-or-child: plain `startswith` would let
    `/api/queryevil` ride in on `/api/query` and `/api/data-apps` on
    `/api/data`.
    """
    if path in _DATA_APP_ALLOWED_EXACT:
        return True
    return any(path == p or path.startswith(p + "/") for p in _DATA_APP_ALLOWED_SUBTREES)


def _client_ip(request: Optional[Request]) -> Optional[str]:
    """See app/auth/dependencies._client_ip — same trusted-hop model (F9)."""
    from app.auth.client_ip import trusted_client_ip

    return trusted_client_ip(request)


def resolve_token_to_user(
    conn: Optional[duckdb.DuckDBPyConnection],
    token: str,
    request: Optional[Request] = None,
    *,
    allow_data_app_git_scope: bool = False,
    allow_data_app_preview_scope: bool = False,
) -> Tuple[Optional[dict], Optional[ResolutionReason]]:
    """Validate a bearer token and return (user_dict, None) on success.

    On failure returns `(None, reason)` — the reason identifies which check
    failed so callers can map to a specific HTTP 401 detail. Side effects
    (last_used_at update, first-use-from-new-ip audit) are best-effort and
    never block authentication.

    ``conn`` is retained for signature stability — repositories are looked
    up via the factory in ``src.repositories`` (DuckDB or Postgres per
    ``AGNES_DB_URL``), so this argument is ignored.

    ``allow_data_app_git_scope`` gates whether a token carrying the
    ``data-app-git:<slug>`` scope (minted by
    ``app.api.data_apps._mint_git_credential``) is accepted. Every caller
    defaults to ``False`` (fail closed) except ``app/api/data_apps_git.py``,
    the one surface that credential is meant to authenticate.

    ``allow_data_app_preview_scope`` is the same fail-closed gate for a
    ``data-app-preview:<slug>`` scope (minted by
    ``app.api.data_apps._mint_preview_token``) — only
    ``app/api/data_apps_proxy.py``'s view-only serving path passes
    ``True``. Both scope checks reject their own prefix independently, so a
    caller that (mistakenly) allows one never accepts the other.

    The third data-app scope, ``data-app:<slug>`` (the runtime service token
    minted by ``app.api.data_apps._mint_service_token``), has no boolean
    because it is not confined to one surface — it is a data client. It is
    gated instead by the ``_DATA_APP_ALLOWED_EXACT`` /
    ``_DATA_APP_ALLOWED_SUBTREES`` path allowlist, so there is no parameter
    to pass: every caller gets the same enforcement.
    """
    if not token:
        return None, "no_token"

    payload = verify_token(token)
    if not payload:
        return None, "invalid_token"

    scope = payload.get("scope") or ""
    if scope.startswith(DATA_APP_GIT_SCOPE_PREFIX) and not allow_data_app_git_scope:
        return None, "pat_scope_forbidden"
    if scope.startswith(DATA_APP_PREVIEW_SCOPE_PREFIX) and not allow_data_app_preview_scope:
        return None, "pat_scope_forbidden"

    if scope.startswith(DATA_APP_SERVICE_SCOPE_PREFIX):
        # Callers that omit `request` fall through to path="" — which matches
        # no prefix, so the service token is fail-closed rejected there, the
        # same way an agent PAT is (see `_AGENT_PAT_ALLOWED_PREFIXES` below).
        # Today that means MCP-over-HTTP and the git smart-HTTP surfaces; a
        # hosted app is a REST client by design.
        path = request.url.path if request is not None else ""
        if not _data_app_path_allowed(path):
            # Log the refused path: this failure is otherwise invisible from
            # the outside — the container stays healthy and the app renders,
            # only its API calls 401 — so an operator needs to see WHICH
            # endpoint the app was refused, not just that something broke.
            logger.warning(
                "data-app service token refused off-surface: scope=%s path=%s",
                scope,
                path or "<no-request>",
            )
            return None, "pat_scope_forbidden"

    if payload.get("typ") == "agent_pat":
        # Callers that omit `request` (git smart-HTTP in
        # app/marketplace_server/git_router.py, MCP HTTP in
        # app/api/mcp_http.py — neither has a natural `Request` object to
        # pass through their auth path) fall through to path="" here, which
        # never matches `_AGENT_PAT_ALLOWED_PREFIXES` — an agent PAT is
        # fail-closed rejected on those surfaces by design, not by accident.
        path = request.url.path if request is not None else ""
        if not path.startswith(_AGENT_PAT_ALLOWED_PREFIXES):
            return None, "agent_pat_wrong_surface"

    typ = payload.get("typ")
    co_session_id = payload.get("chat_session_id")

    if typ == "co_session" or co_session_id:
        # Route chat-session reads through the repo factory so co-session
        # resolution works on either backend (DuckDB or Postgres). The old
        # path read these tables off the always-DuckDB system connection, so on
        # a PG instance the participant / is_co_session lookups came back empty
        # and every co-session token failed closed.
        from src.repositories import chat_session_participants_repo, chat_session_repo

        if typ == "co_session":
            from src.grant_intersection import compute_grant_intersection
            from app.auth.session_principal import SessionPrincipal

            participants = chat_session_participants_repo().get_session_participants(co_session_id)
            if not participants:
                return None, "invalid_token"  # no live participants -> deny
            emails = [p.user_email for p in participants]
            principal = SessionPrincipal(
                session_id=co_session_id,
                participant_user_ids=[p.user_id for p in participants],
                participant_emails=emails,
                # No conn → compute_grant_intersection resolves through the
                # factory (backend-correct) rather than a raw DuckDB conn.
                intersection=compute_grant_intersection(emails),
            )
            _stash_payload(request, payload)
            return principal, None

        if typ == "agent_session":
            # V1d: session -> agent_id -> agent row -> owner -> AgentPrincipal.
            # Fail closed on every missing link — a token that decodes fine
            # but names a session/agent/owner that no longer resolves must
            # never fall through to the owner-identity path below.
            from src.repositories import agents_repo, users_repo
            from src.agent_scope_intersection import resolve_agent_authority
            from app.auth.session_principal import AgentPrincipal

            session = chat_session_repo().get_session(co_session_id)
            if session is None:
                return None, "invalid_token"
            agent_id = getattr(session, "agent_id", None)
            if not agent_id:
                return None, "invalid_token"
            agent = agents_repo().get_by_id(agent_id)
            if not agent or agent.get("deleted_at") is not None:
                return None, "invalid_token"
            owner = users_repo().get_by_id(agent.get("owner_user_id") or "")
            if not owner:
                return None, "invalid_token"
            # C2.3 caller binding: WHO is actually driving this turn — the
            # owner when they run their own agent, a different user when the
            # agent was shared to them. The JWT itself carries no identity
            # (`mint_agent_session_jwt`'s "no baked-in authority" contract —
            # synthetic `sub`, empty `email`), so this is NOT read off
            # `payload`: it is looked up from `session.user_email`, the
            # value `ChatManager.create_session` stored server-side from the
            # AUTHENTICATED caller at session-creation time. A client cannot
            # forge this by shaping the token or any request field — the only
            # way to change it is to actually authenticate as someone else
            # and create a new session, which is exactly the intended
            # boundary. Fails closed like every other link above: a session
            # naming an email that no longer resolves to a user must not
            # silently fall back to the owner identity.
            caller = users_repo().get_by_email(session.user_email)
            if not caller:
                return None, "invalid_token"
            agent_principal = AgentPrincipal(
                session_id=co_session_id,
                agent_id=agent_id,
                owner_user_id=owner["id"],
                owner_email=owner["email"],
                intersection=resolve_agent_authority(agent_id),
                caller_user_id=caller["id"],
                caller_email=caller["email"],
            )
            _stash_payload(request, payload)
            return agent_principal, None

        # Defense-in-depth (SR-3): a plain single-user token that names a
        # co-session must never drive it, regardless of _spawn_runner.
        session = chat_session_repo().get_session(co_session_id)
        if session is not None and bool(session.is_co_session):
            return None, "invalid_token"  # FAIL CLOSED

    from src.repositories import users_repo, access_token_repo

    user = users_repo().get_by_id(payload.get("sub", ""))
    if not user:
        return None, "user_not_found"
    if not bool(user.get("active", True)):
        return None, "deactivated"

    if payload.get("typ") not in _PAT_LIKE_TYPES:
        # v106 follow-up: session-JWT-backed AGENT surfaces get the stack
        # data-read surface, mirroring the PAT default minted by `agnes
        # init` / mcp_connect. Three mint sites tag themselves via the
        # `scope` claim: the per-user chat runner (`mint_session_jwt`,
        # scope="chat" — the web-chat sandbox), the brokered solo-chat
        # replay identity (`app/api/broker.py::_mint_identity_jwt`, also
        # scope="chat" — same narrowing, deliberately), and the MCP
        # streamable-HTTP OAuth transport (`app.auth.mcp_oauth`,
        # scope="mcp-oauth" — Claude Desktop / claude.ai connectors).
        # Browser session JWTs carry no scope claim → no key → surface
        # 'all' → /admin and the web UI are untouched. Non-admins are
        # unaffected either way (the surface only gates the admin
        # short-circuit in src/rbac.py).
        if payload.get("scope") in ("chat", "mcp-oauth"):
            user["credential_surface"] = "stack"

        # Issue #1676: server-side session revocation. `session_revoked_before`
        # (PG-only column — A3 ratchet, see
        # migrations/versions/0079_session_revoked_before.py) is a
        # per-user timestamp floor: a `typ="session"` token whose `iat`
        # predates it is refused here even though its signature and `exp`
        # are both still fine. `POST /auth/logout` bumps it to "now" via
        # `users_repo().revoke_sessions(...)`, so a captured cookie stops
        # working the moment the owner logs out instead of staying valid for
        # the rest of its 30-day `exp`.
        #
        # Rides the `user` row already loaded above for the `active` check on
        # EVERY authenticated request — no additional query. On a DuckDB-
        # backed instance the column doesn't exist (frozen post-A3 schema),
        # so `session_revoked_before` is simply absent from the dict and this
        # never fires there (documented trade-off, not a silent gap — see
        # CHANGELOG.md).
        if payload.get("typ") == "session":
            revoked_before = user.get("session_revoked_before")
            iat = payload.get("iat")
            if revoked_before is not None and iat is not None:
                if isinstance(revoked_before, str):
                    revoked_before = datetime.fromisoformat(revoked_before)
                if revoked_before.tzinfo is None:
                    revoked_before = revoked_before.replace(tzinfo=timezone.utc)
                # `iat` is whole-SECOND precision (PyJWT floors a datetime to
                # int() on encode); `revoked_before` is a DB timestamp with
                # sub-second precision. Floor both to the same granularity
                # before comparing (strict `<`), or a session re-minted in the
                # SAME second as the revoke call — a plain logout-then-
                # log-back-in — would spuriously compare "before" the floored
                # revoke timestamp and get rejected.
                token_iat = datetime.fromtimestamp(iat, tz=timezone.utc)
                if token_iat < revoked_before.replace(microsecond=0):
                    return None, "session_revoked"

        _stash_payload(request, payload)
        return user, None

    # PAT / agent PAT: extra DB-backed validation (revoked/expired/unknown/hash).
    # Agent PATs live in the same `personal_access_tokens` table with
    # `agent_id` set, so this chain — including revocation and expiry — is
    # identical for both token kinds.
    tokens_repo = access_token_repo()
    record = tokens_repo.get_by_id(payload.get("jti", ""))
    if not record:
        return None, "pat_unknown"
    if record.get("revoked_at") is not None:
        return None, "pat_revoked"

    # Minted-successor binding. A token carrying `parent_token_id` is only as
    # alive as the credential that minted it — so revoking that credential
    # revokes this one too, with no sweep to run and nothing to remember.
    # A parent row that has vanished outright counts as revoked (fail closed):
    # the alternative is a successor that outlives a hard-deleted PAT.
    parent_id = payload.get(PARENT_TOKEN_ID_CLAIM)
    if parent_id:
        parent = tokens_repo.get_by_id(parent_id)
        if not parent or parent.get("revoked_at") is not None:
            return None, "pat_parent_revoked"

    exp_at = record.get("expires_at")
    if exp_at is not None:
        if isinstance(exp_at, str):
            exp_at = datetime.fromisoformat(exp_at)
        if exp_at.tzinfo is None:
            exp_at = exp_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > exp_at:
            return None, "pat_expired"

    # Defense-in-depth: stored token_hash must match sha256(bearer JWT).
    # Protects against a forged-but-unrevoked JWT using a stolen signing key.
    stored_hash = record.get("token_hash")
    if stored_hash:
        actual = hashlib.sha256(token.encode()).hexdigest()
        if actual != stored_hash:
            return None, "pat_mismatch"

    # Defense-in-depth (agent delete): `DELETE /api/v1/agents/{id}` soft-
    # deletes the agent row and then revokes its PATs as two separate,
    # non-atomic calls (see app/api/agents_admin.py::delete_agent). If the
    # revoke call fails after the soft-delete already committed, the token
    # row above would still look perfectly valid — so an agent PAT gets an
    # independent liveness check straight against the `agents` table by the
    # JWT's `agent_id` claim, not just the token row. Checked here (not
    # earlier) so a wrong-surface agent PAT still gets
    # "agent_pat_wrong_surface" first, matching the existing reason
    # ordering.
    if payload.get("typ") == "agent_pat":
        from src.repositories import agents_repo

        agent = agents_repo().get_by_id(payload.get("agent_id", ""))
        if not agent or agent.get("deleted_at") is not None:
            return None, "agent_pat_agent_deleted"

    # First-use-from-new-IP audit entry (#12 acceptance criterion).
    # Only emit when the IP changes on a *subsequent* use — the very
    # first use of a token is not surprising and doesn't need an entry.
    current_ip = _client_ip(request)
    previous_ip = record.get("last_used_ip")
    already_used = record.get("last_used_at") is not None
    if already_used and current_ip and current_ip != previous_ip:
        try:
            from src.repositories import audit_repo

            audit_repo().log(
                user_id=user["id"],
                action="token.first_use_new_ip",
                resource=f"token:{payload['jti']}",
                params={"ip": current_ip, "previous_ip": previous_ip},
            )
        except Exception:
            pass  # audit failure must not block auth

    try:
        tokens_repo.mark_used(payload["jti"], ip=current_ip)
    except Exception:
        pass

    # v106: credential data-read surface. Stashed on the user dict so the
    # RBAC primitives (src/rbac.py get_accessible_tables/can_access_table)
    # can narrow an ADMIN's read surface to their stack when the token was
    # minted with surface='stack'. The rule is "non-PAT credential ⇒ all":
    # session JWTs, the scheduler shared-secret, and local-dev bypass never
    # reach this branch and therefore never carry the key — and a missing
    # key reads as 'all' at every consumer, so their behavior is unchanged.
    # Legacy PATs are backfilled to 'all' by the v106 migration; the
    # `or "all"` below is belt-and-braces for a NULL that slipped through.
    user["credential_surface"] = record.get("surface") or "all"

    _stash_payload(request, payload)
    return user, None


def agent_id_from_request(request: Optional[Request]) -> Optional[str]:
    """agent_id claim of the presented agent PAT, or None for other creds.

    Reads the JWT payload stashed on ``request.state.token_payload`` by
    ``resolve_token_to_user`` — no re-verification. For Task 8/9 callers that
    need to know which agent is bound to the current request.

    Caller contract: only meaningful after ``get_current_user`` (or an
    equivalent that runs ``resolve_token_to_user`` against this same
    ``request``) has already succeeded for the current request. This helper
    performs no verification of its own — it trusts whatever was stashed
    earlier in the request lifecycle and returns ``None`` (never raises) if
    nothing was stashed, e.g. because auth hasn't run yet or failed.
    """
    payload = getattr(request.state, "token_payload", None) if request is not None else None
    return payload.get("agent_id") if payload and payload.get("typ") == "agent_pat" else None
