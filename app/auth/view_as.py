"""Read-only "view this page as another user" — the mechanism, in one place.

An admin can reason about what a person sees from grant rows, and can preview
the Library's governed bands (``GET /api/admin/users/{id}/library-preview``).
Neither answers "open THIS page as them". This module is what does, and it is
deliberately the smallest thing that can: a signed, self-expiring **ticket**
in a cookie, one request-scoped contextvar, and two guards at the points where
authorization is actually decided.

Shape borrowed, not invented
----------------------------
``AgentPrincipal`` (``app/auth/session_principal.py``) is the precedent: an
agent's effective authority is a RESTRICTION resolved live at every request,
never a set of grants baked into a token. This follows the same discipline —
the ticket carries **no authority at all**, only *whose* view is being asked
for. Every request re-reads the target's live membership and grants, so
revoking a grant, deactivating the target, or demoting the VIEWER takes effect
on the next request with no stale-replay window.

It is not, however, a ``Principal``: a restricted principal is a frozen
dataclass, and ~257 ``Depends(get_current_user)`` call sites (plus every web
template) treat the caller as a user dict. Returning the TARGET's own user
dict is what makes an arbitrary PAGE render as them, and it is also what makes
the authority swap total rather than partial — authorization keys off
``user["id"]``, so there is no seam left holding the viewer's identity.

The five things that keep that safe
-----------------------------------
1. **Entry is admin-only and CSRF-gated.** ``POST /admin/view-as`` behind
   ``require_admin`` + the ``web_csrf`` double-submit token (the
   ``slack_bind_confirm`` shape, security playbook §10). There is no GET door.
2. **Narrowing only, enforced at the authorization point.** While a ticket is
   active, ``app.auth.access.is_user_admin`` answers False *for the target*
   and ``app.auth.elevation.elevation_paused`` answers True *for the target*.
   Those two are what every admin gate in the codebase is built out of
   (``require_admin``, ``can_access``'s inlined god-mode branch,
   ``src.rbac``'s short-circuit, ``_attach_admin_flag``'s chrome flag), so the
   mode cannot reach an admin surface even when the TARGET is an admin — the
   effective authority is the target's EXPLICIT grants and nothing else. Since
   the viewer must be an admin to enter, that set is always a subset of what
   they already had: the mode can only ever subtract.
3. **Strictly read-only.** ``app.middleware.view_as_readonly`` refuses every
   non-GET/HEAD request (and every WebSocket handshake) while a ticket is
   active, by method — never by route list, so a route added tomorrow is
   covered by construction. The single exception is :data:`EXIT_PATH`, whose
   handler does nothing but clear this cookie.
4. **Bound to the viewer's own session.** The ticket names the viewer, and
   both the middleware and the auth layer refuse it unless the request's own
   session cookie resolves to that same person — so a copied cookie does
   nothing in anyone else's browser, and a re-login as someone else drops the
   mode instead of inheriting it.
5. **Self-expiring and session-scoped.** ``itsdangerous`` signs a timestamp
   the reader enforces (:data:`MAX_AGE_SECONDS`), and the cookie carries no
   ``Max-Age`` so it dies with the browser session.

Deliberately NOT a JWT (same call as ``app.auth.oauth_connect_state``): this
is opaque bookkeeping, not an identity credential, and a JWT shaped payload
signed with the same key invites exactly the confusion where a ticket is fed
to ``verify_token`` and resolves to a session. A distinct ``itsdangerous``
salt makes the two namespaces disjoint by construction.

What this is NOT: a "become user" / impersonation feature. Nothing here can
act, mint, or write as the target — see the read-only middleware.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional

from urllib.parse import quote

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.auth.jwt import get_signing_secret

#: HttpOnly cookie carrying the signed ticket.
VIEW_AS_COOKIE = "agnes_view_as"

#: The ONE path the read-only guard lets through as a non-GET while the mode
#: is active. Its handler clears the cookie and does nothing else.
EXIT_PATH = "/admin/view-as/exit"

#: Ticket lifetime. Long enough to walk a few pages, short enough that a
#: forgotten tab is not a standing mode. Read at call time so a test (or a
#: future config key) can shorten it.
MAX_AGE_SECONDS = 1800

_SALT = "agnes-view-as-ticket"

_REQUIRED_KEYS = ("viewer_user_id", "viewer_email", "target_user_id", "target_email")


@dataclass(frozen=True)
class ViewAsTicket:
    """Who is viewing as whom — display copy only.

    The emails are for the banner; the ids are for the two identity checks
    (bind the ticket to its viewer, load the target fresh). No authority,
    no group list, no grant set: everything that decides access is re-read
    per request from the database.
    """

    viewer_user_id: str
    viewer_email: str
    target_user_id: str
    target_email: str

    #: Where the admin was standing when they entered — the page exit sends
    #: them back to. It lives in the TICKET rather than in the banner's exit
    #: form because the banner only ever knows the page it is rendering on:
    #: an admin who entered from the Access page and then clicked through to
    #: `/library` and `/agents` would otherwise be dropped wherever they
    #: happened to stop, as their own admin self, with the person they were
    #: investigating forgotten. Signed, so it cannot be re-pointed mid-mode,
    #: and re-validated on read. Empty means "not recorded" — an older
    #: ticket minted before this field existed, or an entry point that did
    #: not name one; :func:`return_path` derives a destination either way.
    return_to: str = ""


# Request-scoped active ticket. Default None, so every non-request context
# (scheduler, worker, CLI, a direct function call in a test) behaves exactly
# as it did before this feature existed.
_active: ContextVar[Optional[ViewAsTicket]] = ContextVar("agnes_view_as_active", default=None)


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_signing_secret(), salt=_SALT)


def sign_ticket(
    *,
    viewer_user_id: str,
    viewer_email: str,
    target_user_id: str,
    target_email: str,
    return_to: str = "",
) -> str:
    """Sign a ticket into the opaque string that rides :data:`VIEW_AS_COOKIE`.

    ``return_to`` is sanitized here as well as on read: it arrives from a form
    field, and a rejected value must degrade to "not recorded" rather than
    ride along inside a signed blob where the next reader would trust it.
    """
    return _serializer().dumps(
        {
            "viewer_user_id": viewer_user_id,
            "viewer_email": viewer_email,
            "target_user_id": target_user_id,
            "target_email": target_email,
            "return_to": safe_internal_path(return_to, ""),
        }
    )


def verify_ticket(raw: Optional[str]) -> Optional[ViewAsTicket]:
    """Signature + age + shape, or ``None``.

    Every failure mode collapses to ``None`` on purpose: callers treat a bad
    ticket exactly like no ticket (the mode simply does not engage), so there
    is nothing to be gained from distinguishing "expired" from "tampered" —
    and an error branch that told them apart would be one more place to get
    the fail-closed direction wrong.
    """
    if not raw:
        return None
    try:
        data = _serializer().loads(raw, max_age=MAX_AGE_SECONDS)
    except (SignatureExpired, BadSignature):
        return None
    except Exception:
        # A malformed blob (bad base64, a non-dict payload) raises from
        # deeper inside itsdangerous — same answer, no ticket.
        return None
    if not isinstance(data, dict):
        return None
    for key in _REQUIRED_KEYS:
        value = data.get(key)
        if not isinstance(value, str) or not value:
            return None
    return ViewAsTicket(
        viewer_user_id=data["viewer_user_id"],
        viewer_email=data["viewer_email"],
        target_user_id=data["target_user_id"],
        target_email=data["target_email"],
        # Optional on purpose: a ticket minted before this field existed is
        # still a valid ticket, and adding it to `_REQUIRED_KEYS` would log
        # every mid-session admin out of the mode on deploy. Re-validated
        # rather than trusted-because-signed — the signature proves Agnes
        # wrote it, not that what Agnes wrote was a safe redirect target.
        return_to=safe_internal_path(data.get("return_to"), ""),
    )


# ---------------------------------------------------------------------------
# Request-scoped state
# ---------------------------------------------------------------------------
#
# Stamped by ``app.middleware.view_as_readonly`` — an ASGI middleware, which
# runs in the request's own async task. That placement is load-bearing, not
# stylistic: ``get_current_user`` is a plain ``def`` and FastAPI offloads it
# to the anyio thread pool, where ``ContextVar.set()`` mutates only that
# thread's COPY of the context and is lost on return (the same trap
# ``app/middleware/audit_fallback.py`` documents at length). A value stamped
# in the middleware propagates FORWARD into every dependency and handler;
# a value stamped in a sync dependency does not propagate back out.


def set_active_for_request(ticket: Optional[ViewAsTicket]):
    """Mark this request as being in view-as; returns the reset token."""
    return _active.set(ticket)


def reset_for_request(token) -> None:
    _active.reset(token)


def active_ticket() -> Optional[ViewAsTicket]:
    """The ticket in force for this request, or ``None``."""
    return _active.get()


def active_target_user_id() -> Optional[str]:
    """Id of the user this request is being viewed AS, or ``None``.

    The single read both authorization guards use
    (``app.auth.access.is_user_admin``, ``app.auth.elevation.elevation_paused``).
    """
    ticket = _active.get()
    return ticket.target_user_id if ticket else None


def active_viewer_user_id() -> Optional[str]:
    """Id of the real human behind this request, or ``None``.

    Audit attribution reads this: a row written while the mode is active
    describes something the VIEWER did, and must never blame the target.
    """
    ticket = _active.get()
    return ticket.viewer_user_id if ticket else None


def is_narrowed_subject(subject_user_id: Optional[str]) -> bool:
    """True when ``subject_user_id`` is the identity this request is narrowed to.

    Subject-scoped for the same reason ``elevation_paused`` is: ``can_access``
    and ``is_user_admin`` are also asked about OTHER people (an admin page
    listing who is an admin, a co-drive invite checking the invitee). Only the
    question "may the CALLER do this" must be answered by the narrowed
    authority; every other subject keeps its true answer.

    ``None`` — a caller that did not say who it is asking about — is treated
    as the caller, which is the fail-closed direction: this only ever removes
    privilege.
    """
    target = active_target_user_id()
    if target is None:
        return False
    return subject_user_id is None or subject_user_id == target


def local_dev_viewer_id() -> Optional[str]:
    """The id every request authenticates as under ``LOCAL_DEV_MODE``, else ``None``.

    Not a relaxation of the binding rule — the same rule applied to the
    credential dev mode actually uses. Dev mode authenticates from
    configuration rather than from a session cookie, so there is no
    ``access_token`` for a ticket to be bound TO, and a check written only
    against that cookie does not fail the ticket, it fails to evaluate at all:
    the mode silently never engages, which is how a "View a page as them"
    button came to set a cookie, redirect, and then render no banner and
    therefore no way out.

    Off in every other mode, and it grants nothing on its own: dev mode
    already resolves every caller to this one account, so a ticket minted by
    it can only ever name the identity the request already had.
    """
    from app.auth.dependencies import _get_local_dev_user, is_local_dev_mode

    if not is_local_dev_mode():
        return None
    user = _get_local_dev_user()
    return str(user["id"]) if user and user.get("id") else None


def session_matches_viewer(session_token: Optional[str], ticket: Optional["ViewAsTicket"]) -> bool:
    """Does this request's OWN session belong to the ticket's viewer?

    :func:`verify_ticket` proves only that Agnes minted the string and that it
    has not expired. That makes a ticket a BEARER credential, and a bearer
    credential that escapes out of band — a proxy log, a HAR file attached to
    a bug report, a shared machine — would otherwise be replayable by whoever
    holds it. Binding it to the caller's own session is what makes a copied
    ticket inert, so every consumer of a ticket owes this check.

    It exists as one function because the ticket has three consumers and the
    third forgot: the middleware (``app/middleware/view_as_readonly.py``) and
    the auth layer (``app/auth/dependencies.py``) each bound the ticket at
    their own layer, while ``view_as_exit`` parsed it bare and wrote an audit
    row keyed on ``ticket.viewer_user_id`` — a leaked ticket alone could forge
    a ``view_as.end`` entry against a real admin who did nothing. A rule kept
    in three places is a rule that will be dropped in a fourth.

    Fail-closed: an unverifiable token, or no ticket → ``False``. A MISSING
    token is fail-closed too in every mode that issues one; under
    ``LOCAL_DEV_MODE`` it means the request authenticates by configuration
    instead, and the binding is checked against that identity
    (:func:`local_dev_viewer_id`) rather than skipped.
    """
    if ticket is None:
        return False

    if session_token:
        from app.auth.jwt import verify_token

        payload = verify_token(session_token) or {}
        if str(payload.get("sub") or "") == ticket.viewer_user_id:
            return True

    # The configured-identity fallback is reached whenever the session cookie
    # did not answer — absent, unreadable, or belonging to someone else.
    #
    # "Absent" alone was not enough, and the miss is worth naming: browser
    # cookies ignore the PORT, so every local instance on 127.0.0.1 shares one
    # jar. A developer who has opened any other Agnes on that host is carrying
    # an `access_token` this instance cannot verify — signed by a different
    # deployment's secret — and a check that fired only on a MISSING cookie
    # took the token path, failed to read it, and silently declined to engage.
    # Same outcome as the bug this fallback exists to fix: a cookie set, a
    # redirect served, and no banner.
    #
    # Still a binding, and still fail-closed off dev mode: a ticket naming
    # anyone other than the configured identity is inert, and
    # `local_dev_viewer_id` answers None in every mode that issues real
    # sessions — where an unverifiable token must keep failing, since there
    # the request has no other credential to fall back ON.
    dev_viewer = local_dev_viewer_id()
    return dev_viewer is not None and dev_viewer == ticket.viewer_user_id


def binding_is_possible(session_token: Optional[str]) -> bool:
    """Could a ticket minted for this request ever engage?

    Asked at ENTRY, before a cookie is set. Every consumer of a ticket binds
    it to the caller's own credential, so minting one for a request that
    carries no bindable credential produces a cookie that is inert for its
    whole lifetime — and the mode's only exit control lives in the banner that
    inert cookie never renders. Refusing up front is the difference between a
    button that fails and a button that lies.
    """
    return bool(session_token) or local_dev_viewer_id() is not None


def safe_internal_path(candidate: Optional[str], default: str) -> str:
    """A same-origin absolute path, or ``default``.

    Stricter than ``app.auth._common.safe_next_path``, which also accepts this
    deployment's data-app origins: a view-as ticket is scoped to the main host
    and there is no reason to bounce it anywhere else. Rejects protocol-
    relative (``//evil``) and backslash forms (browsers normalize ``\\`` to
    ``/``, ``urlsplit`` does not).
    """
    if not candidate or not isinstance(candidate, str):
        return default
    if not candidate.startswith("/") or candidate.startswith("//"):
        return default
    if "\\" in candidate:
        return default
    return candidate


#: Where exit lands when the ticket recorded no origin of its own. Not simply
#: ``/admin/access``: the person under investigation is the whole reason the
#: admin left that page, and the Simulate lens restores its selection from
#: ``?lens=simulate&user=`` — so the fallback rebuilds the deep link out of
#: the ticket rather than dropping the admin on an empty picker.
_DEFAULT_RETURN = "/admin/access?lens=simulate&user={target_user_id}"


def return_path(ticket: Optional[ViewAsTicket]) -> str:
    """The page exit should land on for ``ticket``.

    The ticket is the only input, deliberately. The exit route mounts no auth
    dependency (``get_current_user`` resolves to the TARGET while the mode is
    on, so ``require_admin`` would 403 the very person trying to leave), which
    makes a form-supplied destination the one attacker-influenced value on an
    endpoint that has no other. Reading it from the signed ticket instead
    means the admin can only ever be returned to the page they actually
    entered from, and there is no redirect parameter left to aim.
    """
    if ticket is None:
        return "/admin/access"
    if ticket.return_to:
        return safe_internal_path(ticket.return_to, "/admin/access")
    return _DEFAULT_RETURN.format(target_user_id=quote(ticket.target_user_id, safe=""))
