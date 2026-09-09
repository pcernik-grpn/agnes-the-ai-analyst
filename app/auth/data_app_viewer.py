"""Viewer identity for hosted data apps — who is looking, and (opt-in) whose
grants the app reads data with.

Two headers, two keys, one deliberate asymmetry:

``X-Agnes-Viewer`` — the **identity assertion**, on EVERY proxied request and
WS handshake (``app/api/data_apps_proxy.py``). A compact HS256 JWT signed
with a PER-APP secret the container holds as ``AGNES_VIEWER_SECRET``, so the
app verifies it offline (``server/agnesViewer.ts`` in the scaffold) and
learns ``sub``/``email``/``name``/``groups`` of the viewer. Why signed rather
than a plain trusted header: every app container sits on the one
``agnes-apps`` docker network, so app A can send app B a request with a
forged ``X-Agnes-Viewer-Email`` directly — a signature the proxy alone can
produce for B is what makes the header worth trusting. The proxy strips
every inbound ``x-agnes-viewer*`` header from the caller for the same reason.

The per-app secret is never stored: ``derive_viewer_secret`` is
``HMAC-SHA256(server_signing_key, "<info>|<slug>|<service_token_id>")``.
``redeploy_current`` (``app/api/data_apps.py``) writes a fresh
``service_token_id`` on every deploy/wake BEFORE building ``config.json``,
so rotation rides the service-token rotation for free, the proxy (which
re-reads the row per request) always derives the secret the running
container was configured with, and rollback restores it with the id. The
container learns ``HMAC(K, info)``, never ``K``.

``X-Agnes-Viewer-Token`` — the **viewer data token**, ONLY when the app's
``data_identity`` is ``'viewer'`` (``src.data_apps.identity``). A short-lived
JWT signed with the SERVER key (``create_access_token``), which the app
forwards as its bearer to the Agnes data API; ``app.auth.pat_resolver``
resolves it (via :func:`resolve_viewer_principal`) to a
``DataAppViewerPrincipal`` whose authority is ``owner ∩ viewer``, live per
request, with row-level policies bound to the viewer. Never the per-app
secret: the OWNER controls the container and therefore knows that secret,
and must not be able to mint a token Agnes accepts. Not persisted in
``personal_access_tokens`` (one row per proxied request is not acceptable);
revocation is the live re-check chain in :func:`resolve_viewer_principal`
plus the ten-minute ``exp``.

Everything here is minted AFTER the proxy's own gates (RBAC, same-origin,
``state == 'running'``) — nothing is minted for a caller who has not passed
``can_view_data_app``, and nothing for a holding page.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Mapping, Optional

from src.data_apps.identity import viewer_mode

if TYPE_CHECKING:  # pragma: no cover
    from app.auth.pat_resolver import ResolutionReason
    from app.auth.session_principal import DataAppViewerPrincipal

logger = logging.getLogger(__name__)

#: Request header carrying the per-app-signed identity assertion.
VIEWER_HEADER = "X-Agnes-Viewer"
#: Request header carrying the server-signed viewer data token (viewer mode only).
VIEWER_TOKEN_HEADER = "X-Agnes-Viewer-Token"
#: Lowercase prefix every inbound caller header is stripped by — covers both
#: names above and anything a caller might invent in the same family.
VIEWER_HEADER_PREFIX = "x-agnes-viewer"

#: ``typ`` claim of the identity assertion (per-app key) and of the viewer
#: data token (server key). Distinct on purpose: the resolver keys its branch
#: on the token's ``typ``, and an assertion must never be mistaken for one.
VIEWER_ASSERTION_TYP = "data_app_viewer_assertion"
VIEWER_TOKEN_TYP = "data_app_viewer"

VIEWER_ASSERTION_TTL_S = 300
VIEWER_TOKEN_TTL_S = 600

#: Key-derivation label — bump the version on any change to the derivation.
_SECRET_INFO_PREFIX = "agnes/data-app-viewer-secret/v1"

#: Header-size guard: the container's nginx accepts 8k header lines by default.
_MAX_GROUPS_IN_ASSERTION = 200

_GROUPS_CACHE_TTL_S = 60
_GROUPS_CACHE_MAX = 10_000
_GROUPS_CACHE: dict[str, tuple[float, list[str]]] = {}
_GROUPS_LOCK = threading.Lock()

#: One ``data_app.viewer_query`` audit row per (viewer, app) per window.
_VIEWER_QUERY_WINDOW_MINUTES = 15


def derive_viewer_secret(slug: str, service_token_id: str) -> str:
    """The per-app assertion-signing secret for ``(slug, service_token_id)``.

    Deterministic from the server signing key, so nothing new is stored and
    a rotation of ``service_token_id`` (every deploy/wake) rotates it.
    Hex-encoded HMAC-SHA256 — 32 bytes of entropy, exported to the container
    as ``AGNES_VIEWER_SECRET``.
    """
    import hashlib
    import hmac

    from app.auth.jwt import get_signing_secret

    msg = f"{_SECRET_INFO_PREFIX}|{slug}|{service_token_id}".encode()
    return hmac.new(get_signing_secret().encode(), msg, hashlib.sha256).hexdigest()


def can_view_data_app(user_id: str, row: Mapping[str, Any]) -> bool:
    """Owner, Admin, or a group grant on ``(data_app, <slug>)`` — the ONE
    definition of "may open this app", shared by the control plane's
    ``_can_view`` (``app/api/data_apps.py``), the ingress proxy and the
    viewer-token resolver's live re-check.
    """
    if user_id == row.get("owner_user_id"):
        return True
    from app.auth.access import can_access, is_user_admin
    from app.resource_types import ResourceType

    if is_user_admin(user_id):
        return True
    return can_access(user_id, ResourceType.DATA_APP.value, row["slug"])


def viewer_groups(user_id: str) -> list[str]:
    """Sorted live group names of ``user_id``, cached ``_GROUPS_CACHE_TTL_S``
    seconds per user so an app fetching thirty assets per page load does not
    hit the membership table thirty times. A revoked membership reaches the
    assertion within the cache TTL (the assertion itself lives five minutes).
    Lookup failures are not cached and read as "no groups".
    """
    now = time.monotonic()
    with _GROUPS_LOCK:
        hit = _GROUPS_CACHE.get(user_id)
        if hit is not None and hit[0] > now:
            return list(hit[1])
    try:
        from src.repositories import user_group_members_repo

        groups = sorted(set(user_group_members_repo().list_group_names_for_user(user_id)))
    except Exception:
        logger.warning("viewer group lookup failed for user %s; asserting no groups", user_id, exc_info=True)
        return []
    with _GROUPS_LOCK:
        if user_id not in _GROUPS_CACHE and len(_GROUPS_CACHE) >= _GROUPS_CACHE_MAX:
            oldest = min(_GROUPS_CACHE, key=lambda k: _GROUPS_CACHE[k][0])
            _GROUPS_CACHE.pop(oldest, None)
        _GROUPS_CACHE[user_id] = (now + _GROUPS_CACHE_TTL_S, groups)
    return list(groups)


def clear_viewer_group_cache() -> None:
    """Test seam."""
    with _GROUPS_LOCK:
        _GROUPS_CACHE.clear()


def viewer_via(user: Mapping[str, Any], via_preview: bool) -> str:
    """``via`` claim: how the viewer authenticated to the proxy."""
    if via_preview:
        return "preview"
    return "pat" if user.get("token_type") == "pat" else "session"


def mint_viewer_assertion(row: Mapping[str, Any], user: Mapping[str, Any], via: str) -> Optional[str]:
    """The per-app-signed identity assertion, or ``None`` when the row has no
    ``service_token_id`` yet (a never-deployed app — cannot be ``running``,
    so this is defensive; there is no secret the container could hold).

    Signed with :func:`derive_viewer_secret` via PyJWT directly — NOT
    ``create_access_token``, which signs with the server key and would make
    the assertion indistinguishable from a real Agnes credential.
    """
    import jwt

    from app.auth.jwt import ALGORITHM

    token_id = row.get("service_token_id") or ""
    if not token_id:
        logger.warning("data app %s has no service_token_id; no viewer assertion minted", row.get("slug"))
        return None
    slug = row["slug"]
    groups = viewer_groups(user["id"])
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": user["id"],
        "email": user.get("email") or "",
        "typ": VIEWER_ASSERTION_TYP,
        "aud": f"data-app:{slug}",
        "iat": now,
        "exp": now + VIEWER_ASSERTION_TTL_S,
        "via": via,
        "groups": groups[:_MAX_GROUPS_IN_ASSERTION],
    }
    if user.get("name"):
        payload["name"] = user["name"]
    if len(groups) > _MAX_GROUPS_IN_ASSERTION:
        payload["groups_truncated"] = True
    return jwt.encode(payload, derive_viewer_secret(slug, token_id), algorithm=ALGORITHM)


def mint_viewer_data_token(row: Mapping[str, Any], user: Mapping[str, Any]) -> str:
    """The server-signed viewer data token (viewer mode only — the caller
    checks ``viewer_mode(row)``). ``app_id`` pins it to THIS registry row so a
    token outlives neither a delete+recreate of the slug nor the mode flip
    (both re-checked live by :func:`resolve_viewer_principal`)."""
    from app.auth.jwt import create_access_token
    from app.auth.pat_resolver import DATA_APP_VIEWER_SCOPE_PREFIX

    slug = row["slug"]
    return create_access_token(
        user_id=user["id"],
        email=user.get("email") or "",
        expires_delta=timedelta(seconds=VIEWER_TOKEN_TTL_S),
        typ=VIEWER_TOKEN_TYP,
        extra_claims={"scope": f"{DATA_APP_VIEWER_SCOPE_PREFIX}{slug}", "slug": slug, "app_id": row["id"]},
    )


def verify_viewer_assertion(row: Mapping[str, Any], token: str) -> Optional[dict[str, Any]]:
    """Server-side check of an ``X-Agnes-Viewer`` assertion an app hands BACK
    (e.g. as ``viewer_assertion`` in a write body, to stamp who submitted a
    row). Returns the claims, or ``None`` for anything that does not verify
    under THIS app's current derived secret with ``aud="data-app:<slug>"``.

    Trust level, stated plainly: the per-app secret is held by the container,
    so a verified assertion proves "minted by the proxy for this app, OR by
    this app's own (owner-controlled) code" — attribution the owner could
    forge for their own app, never authorization. Use it to stamp
    ``submitted_by``; never to widen what the write itself may do. For a
    server-verifiable viewer identity use viewer mode: the container forwards
    ``X-Agnes-Viewer-Token`` as its bearer and ``pat_resolver`` resolves a
    ``DataAppViewerPrincipal`` (``viewer_user_id`` is then unforgeable).

    An assertion minted under the PREVIOUS ``service_token_id`` (a redeploy
    landed in the last five minutes) fails here by design — same as the
    container's own check.
    """
    import jwt

    from app.auth.jwt import ALGORITHM

    token_id = row.get("service_token_id") or ""
    if not token or not token_id or not row.get("slug"):
        return None
    try:
        claims = jwt.decode(
            token,
            derive_viewer_secret(row["slug"], token_id),
            algorithms=[ALGORITHM],
            audience=f"data-app:{row['slug']}",
        )
    except jwt.InvalidTokenError:
        return None
    if claims.get("typ") != VIEWER_ASSERTION_TYP or not claims.get("sub"):
        return None
    return claims


def build_viewer_headers(row: Mapping[str, Any], user: Mapping[str, Any], via: str) -> dict[str, str]:
    """The headers the proxy adds to one upstream request/handshake: the
    assertion always (when mintable), the data token only in viewer mode."""
    headers: dict[str, str] = {}
    assertion = mint_viewer_assertion(row, user, via)
    if assertion:
        headers[VIEWER_HEADER] = assertion
    if viewer_mode(row):
        headers[VIEWER_TOKEN_HEADER] = mint_viewer_data_token(row, user)
    return headers


# ---------------------------------------------------------------------------
# Resolution (called from app.auth.pat_resolver.resolve_token_to_user)
# ---------------------------------------------------------------------------

from src.audit_helpers import WindowedAuditGate  # noqa: E402  (after the constants it is sized by)

_VIEWER_QUERY_GATE = WindowedAuditGate(window_s=_VIEWER_QUERY_WINDOW_MINUTES * 60)


def resolve_viewer_principal(
    payload: Mapping[str, Any], request: Any
) -> "tuple[Optional[DataAppViewerPrincipal], Optional[ResolutionReason]]":
    """Turn a verified viewer-token payload into a ``DataAppViewerPrincipal``,
    or ``(None, reason)``. Every step fails closed — a token that decodes
    fine but names an app/viewer/owner that no longer resolves, or an app no
    longer in viewer mode, must never fall through to any other identity.

    Re-checked LIVE on every request (the token bakes in no authority):
    the app still exists and is hosted; ``app_id`` matches (slug reuse after
    delete+recreate); ``data_identity == 'viewer'`` (flipping it back kills
    outstanding tokens on the next request); the viewer is active; the owner
    exists; the viewer may still open the app (a revoked ``data_app`` grant
    takes effect within the request, not at ``exp``).
    """
    from app.auth.pat_resolver import DATA_APP_VIEWER_SCOPE_PREFIX, _data_app_path_allowed
    from app.auth.session_principal import DataAppViewerPrincipal

    scope = payload.get("scope") or ""
    if payload.get("typ") != VIEWER_TOKEN_TYP or not scope.startswith(DATA_APP_VIEWER_SCOPE_PREFIX):
        return None, "invalid_token"
    slug = scope[len(DATA_APP_VIEWER_SCOPE_PREFIX) :]

    # Same surface as the owner's service token — a hosted app is a data
    # client and nothing else. No request (git smart-HTTP, MCP-over-HTTP)
    # -> path "" -> refused, exactly as the service token is.
    path = request.url.path if request is not None else ""
    if not _data_app_path_allowed(path):
        logger.warning("data-app viewer token refused off-surface: scope=%s path=%s", scope, path or "<no-request>")
        return None, "pat_scope_forbidden"

    from src.repositories import data_apps_repo, users_repo

    row = data_apps_repo().get_by_slug(slug)
    if not row or row.get("repo_mode") == "linked" or row.get("state") == "linked_hidden":
        return None, "invalid_token"
    if payload.get("app_id") != row.get("id"):
        return None, "invalid_token"
    if not viewer_mode(row):
        return None, "pat_scope_forbidden"

    viewer = users_repo().get_by_id(payload.get("sub") or "")
    if not viewer:
        return None, "user_not_found"
    if not bool(viewer.get("active", True)):
        return None, "deactivated"
    owner = users_repo().get_by_id(row.get("owner_user_id") or "")
    if not owner:
        return None, "invalid_token"
    if not can_view_data_app(viewer["id"], row):
        return None, "pat_scope_forbidden"

    from src.grant_intersection import compute_viewer_intersection

    principal = DataAppViewerPrincipal(
        slug=slug,
        app_id=row["id"],
        owner_user_id=owner["id"],
        owner_email=owner["email"],
        viewer_user_id=viewer["id"],
        viewer_email=viewer["email"],
        intersection=compute_viewer_intersection(owner["id"], viewer["id"]),
    )

    if _VIEWER_QUERY_GATE.should_log((viewer["id"], slug)):
        from src.audit_helpers import log_safe

        log_safe(
            user_id=viewer["id"],
            action="data_app.viewer_query",
            resource=f"data_app:{slug}",
            params={"path": path, "window_minutes": _VIEWER_QUERY_WINDOW_MINUTES},
        )
    return principal, None
