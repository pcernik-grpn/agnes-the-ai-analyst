"""Admin elevation consent gate.

Historically an Admin-group member's god-mode applied unconditionally on
every request. This module makes elevation a per-browser choice: an admin
can PAUSE their own elevation, and while paused the Admin short-circuit in
``can_access`` is skipped and ``require_admin`` refuses — the admin
experiences the app with exactly their explicit group grants, like any
other user.

State rides a dedicated cookie (``agnes_elevation``) read into a request
context variable by middleware in ``app/main.py``. The cookie can only
ever REDUCE privilege: enforcement remains the server-side Admin-group
membership check either way, so a forged cookie value cannot add
authority a non-admin doesn't have — it can at most re-state the default
an admin already holds. This is a consent gate against accidental
god-mode use (and an audit hook), not a containment boundary: an admin
can re-elevate at will via ``POST /api/me/elevation``.

The instance default flips via ``access.admin_default_elevation`` in
``instance.yaml`` (``"elevated"`` — historical behavior — or
``"paused"`` for consent-first deployments). The default applies to
**browser sessions only**: a request authenticated with a Bearer token
(CLI, PATs, service tokens) and carrying no elevation cookie always
runs elevated — the consent gate is a browser-surface concept, and CLI
automation has no cookie jar to re-elevate with, so a paused instance
default must not silently 403 every ``agnes admin …`` call. An explicit
``paused`` cookie is still honored even alongside a Bearer header
(reduction is always allowed). Non-HTTP contexts (scheduler internals,
background jobs, direct calls) likewise run elevated via the contextvar
default.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Optional

ELEVATION_COOKIE = "agnes_elevation"

#: Values the cookie/config may carry.
ELEVATED = "elevated"
PAUSED = "paused"

# Request-scoped elevation-paused flag. Default False: non-request
# contexts (scheduler, CLI) and instances that never touch the feature
# behave exactly as before.
_elevation_paused: ContextVar[bool] = ContextVar("agnes_elevation_paused", default=False)
#: Whose pause it is. Stamped once the auth dependency resolves the caller —
#: the middleware runs before authentication and cannot know it.
_elevation_caller: ContextVar[Optional[str]] = ContextVar("agnes_elevation_caller", default=None)


def default_elevation() -> str:
    """Instance-wide default for admins that have not chosen: config key
    ``access.admin_default_elevation``, normalized; unknown values fall
    back to ``elevated`` (the historical behavior)."""
    from app.instance_config import get_value

    # get_value takes one positional segment PER NESTING LEVEL with the
    # fallback as the ``default=`` keyword — a single dotted string would
    # silently never match (review finding on #1146).
    raw = str(get_value("access", "admin_default_elevation", default=ELEVATED) or ELEVATED).lower()
    return PAUSED if raw == PAUSED else ELEVATED


def resolve_from_cookie(cookie_value: str | None, *, bearer_auth: bool = False) -> bool:
    """True (= paused) for this request, given the raw cookie value.

    An explicit cookie wins (a ``paused`` cookie is honored even for a
    Bearer-authenticated request — reduction is always allowed). With no
    usable cookie, Bearer-authenticated callers (CLI, PATs, service
    tokens) run elevated regardless of the instance default — see the
    module docstring — while browser sessions fall through to
    ``default_elevation()``.
    """
    if cookie_value == PAUSED:
        return True
    if cookie_value == ELEVATED:
        return False
    if bearer_auth:
        return False
    return default_elevation() == PAUSED


def request_is_noninteractive(*, authorization: str, has_session_cookie: bool, has_sapi_header: bool) -> bool:
    """True when a request authenticates via a NON-interactive credential, so
    the instance-wide elevation default must not apply (it has no cookie jar to
    re-elevate with). Two such credentials:

    - ``Authorization: Bearer`` — CLI, PATs, service tokens.
    - the ``X-StorageApi-Token`` header, but ONLY when it is actually the
      credential: ``get_current_user`` consults it just when there is no bearer
      and no session cookie, so a request carrying an ``access_token`` cookie
      authenticated interactively even if it also sends the header. Merely
      adding the header must not exempt a paused admin from the default.
    """
    if authorization.lower().startswith("bearer "):
        return True
    return has_sapi_header and not has_session_cookie


def set_paused_for_request(paused: bool):
    """Set the request-scoped flag; returns the contextvar token so the
    middleware can reset it after the response."""
    return _elevation_paused.set(paused)


def reset_for_request(token) -> None:
    _elevation_paused.reset(token)


def set_caller_for_request(user_id: Optional[str]):
    """Record WHOSE elevation the request-scoped pause belongs to."""
    return _elevation_caller.set(user_id)


def reset_caller_for_request(token) -> None:
    _elevation_caller.reset(token)


def elevation_paused(subject_user_id: Optional[str] = None) -> bool:
    """Is admin elevation paused for ``subject_user_id`` on this request?

    False outside request context by construction (contextvar default).

    The pause is a person pausing THEIR OWN god-mode, so it must only apply to
    authorization questions asked about that person. ``can_access`` is also
    called about OTHER users — the co-drive invite checks the invitee's access,
    not the caller's — and consulting the caller's pause there told an admin
    their admin colleague "lacks chat access" while the colleague's own
    permissions were untouched (Devin Review on #1146).

    Passing no subject keeps the old request-scoped meaning, and an unknown
    caller still honours the pause: it only ever reduces privilege, so the
    conservative answer is the safe one.

    A read-only view-as (``app/auth/view_as.py``) always reads as paused for
    the identity it narrows to. ``can_access`` does not route its god-mode
    branch through ``is_user_admin`` — it inlines the Admin-group membership
    test and then consults this function — so this is the second half of the
    same suppression, not a duplicate of it. Both halves are needed: one
    covers the gates built on ``is_user_admin``, this one covers the grant
    check.
    """
    from app.auth.view_as import is_narrowed_subject

    if is_narrowed_subject(subject_user_id):
        return True
    if not _elevation_paused.get():
        return False
    if subject_user_id is None:
        return True
    caller = _elevation_caller.get()
    return caller is None or caller == subject_user_id
