"""Email magic link auth provider for FastAPI."""

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel
import duckdb

from app.auth.jwt import create_access_token, SESSION_COOKIE_MAX_AGE_SECONDS
from app.auth.token_hash import hash_token
from app.auth.access import is_user_admin
from app.auth.dependencies import _get_db, is_local_dev_mode
from app.auth.public_url import public_base_url
from app.auth.provider_registry import require_provider
from app.auth.rate_limit import limiter as _rate_limiter


from src.repositories import (
    users_repo,
)


def _role_label(user: dict, conn: duckdb.DuckDBPyConnection) -> str:
    """Display label for the response payload only — `admin` if the user is
    in the Admin system group, otherwise `user`. Authorization at runtime
    checks `is_user_admin` directly; this label is purely cosmetic."""
    return "admin" if is_user_admin(user["id"], conn) else "user"


logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/auth/email",
    tags=["auth"],
    dependencies=[Depends(require_provider("email"))],
)

MAGIC_LINK_EXPIRY = 3600  # 1 hour


class MagicLinkRequest(BaseModel):
    email: str


class MagicLinkVerify(BaseModel):
    email: str
    token: str


def is_available() -> bool:
    # In dev mode the link is rendered to logs + response, so the provider is "available"
    # even without SMTP. Keeps the login UI showing the magic-link option.
    if is_local_dev_mode():
        return True
    return _has_email_transport()


def _has_email_transport() -> bool:
    # SMTP only. SENDGRID_API_KEY used to count here, but the SDK branch it
    # advertised could never send (the `sendgrid` package was not a
    # dependency), so the key alone turned the login UI's magic-link option
    # into a dead end.
    return bool(os.environ.get("SMTP_HOST"))


def _build_magic_link(email: str, token: str, next_path: str = "", base_url: str | None = None) -> str:
    """Absolute sign-in URL for the emailed link.

    ``base_url`` is the caller's already-resolved public origin (the handlers
    pass ``public_base_url(request=request)``, which is proxy-aware). Without
    it this resolves the same way every other outbound link does — pinned
    ``AGNES_BASE_URL`` / ``SERVER_URL`` first, ``http://localhost:8000`` only
    as the local-dev floor. This used to read ``SERVER_URL`` alone with a hard
    localhost default, so an instance behind a TLS terminator that never set
    ``SERVER_URL`` mailed its users a sign-in link pointing at their own
    laptop — a dead end with nothing naming the cause.
    """
    # URL-encode email: a literal '+' in a query string decodes to space per
    # application/x-www-form-urlencoded, which would break addresses like
    # "user+tag@gmail.com" on the GET /verify side.
    server_url = (base_url or public_base_url()).rstrip("/")
    link = f"{server_url}/auth/email/verify?email={quote(email, safe='')}&token={token}"
    # Carry the post-login destination through the emailed link so the click-
    # through verify can land the user where they originally asked. next_path
    # is already same-origin-sanitized by the caller and re-sanitized on the
    # verify side (defense in depth against a tampered link).
    if next_path:
        link += f"&next={quote(next_path, safe='')}"
    return link


def _generate_and_deliver_magic_link(
    email: str, next_path: str = "", base_url: str | None = None
) -> tuple[dict | None, str | None, str | None]:
    """Look up the user, mint + persist a magic-link token, and attempt
    delivery via SMTP. Shared by the JSON (``/send-link``) and web
    form (``/send-link/web``) variants so the token/email plumbing lives in
    one place.

    Returns ``(user, link, send_error)``. ``user`` is ``None`` when the
    account doesn't exist — callers must still respond as if a link was
    sent (anti-enumeration) and must not use ``link``/``send_error`` in
    that case. ``send_error`` carries the exception string when the
    transport is configured but delivery failed.
    """
    # Strip here, in the shared helper, so the JSON /send-link and the web
    # form agree: the form used to strip and the JSON path did not, which made
    # a pasted address work through one door and not the other. Case is folded
    # by the lookup itself (SQL), never here.
    email = (email or "").strip()
    repo = users_repo()
    user = repo.get_by_email_ci(email)
    if not user:
        return None, None, None

    token = secrets.token_urlsafe(32)
    repo.update(
        id=user["id"],
        reset_token=hash_token(token),
        reset_token_created=datetime.now(timezone.utc),
    )

    # The link and the delivery address are the RESOLVED account's, not the
    # spelling that was typed — the link's own verify path folds case either
    # way, but a mail sent to the typed string is a mail to an unverified
    # address.
    link = _build_magic_link(user["email"], token, next_path, base_url)
    send_error: str | None = None
    if _has_email_transport():
        try:
            _send_email(user["email"], token, next_path, base_url)
        except Exception as e:
            send_error = str(e)
            logger.error("Failed to send magic link email to %s: %s", email, e)

    return user, link, send_error


@router.post("/send-link")
@_rate_limiter.shared_limit("5/minute", scope="magic_link_send")
async def send_magic_link(
    request: Request,
    body: MagicLinkRequest,
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Send a magic link to the user's email.

    When SMTP is not configured, or LOCAL_DEV_MODE=1, the link is
    logged to stderr and returned in the response body so a developer can
    click it without an email transport.
    """
    # The delivery helper does a blocking SMTP send (+ sync repo writes);
    # offload it so a slow mail server can't freeze the single event
    # loop for every other request (the Tier-1 convention in get_current_user).
    user, link, send_error = await run_in_threadpool(
        _generate_and_deliver_magic_link,
        body.email,
        base_url=public_base_url(request=request),
    )

    # Always return success to prevent email enumeration
    if not user:
        return {"message": "If this email is registered, you will receive a login link."}

    # Dev fallback: expose the link in logs + response so you can click it without SMTP.
    # Scoped strictly to LOCAL_DEV_MODE so test and production behavior are unchanged.
    if is_local_dev_mode():
        logger.warning("=" * 60)
        logger.warning("Magic link for %s (LOCAL_DEV_MODE fallback):", body.email)
        logger.warning("    %s", link)
        logger.warning("=" * 60)
        response: dict = {
            "message": "Magic link generated (LOCAL_DEV_MODE) — click dev_link to log in.",
            "dev_link": link,
        }
        if send_error:
            response["send_error"] = send_error
        return response

    if send_error:
        # A configured transport that failed is a server-side fault the caller
        # must see — answering the generic success left people waiting for a
        # mail that was never sent, with nothing surfacing the misconfiguration.
        # This trades a sliver of anti-enumeration away while the relay is down
        # (a 500 implies the account exists); the silent failure was judged
        # worse. Unknown addresses never attempt a send, so they still get the
        # generic success below.
        raise HTTPException(
            status_code=500,
            detail="Failed to send the sign-in email. Contact your administrator.",
        )

    return {"message": "If this email is registered, you will receive a login link."}


@router.post("/send-link/web")
@_rate_limiter.shared_limit("5/minute", scope="magic_link_send")
async def send_magic_link_web(
    request: Request,
    email: str = Form(...),
    next: str = Form(""),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Web form variant of ``/send-link`` — renders the 'check your email'
    page instead of returning JSON. Mirrors ``password_login_web``'s
    HTML-form sibling of the JSON ``/auth/password/login`` endpoint.

    Same anti-enumeration behavior as ``send_magic_link``: always renders
    the sent-page, regardless of whether the account exists.

    ``next`` round-trips into the hidden field on ``login_magic_link.html``
    but does not yet thread through the magic-link token to the post-verify
    redirect — that redirect still lands on the operator-configured home
    route (see ``verify_magic_link_get``).
    """
    from app.auth._common import safe_next_path
    from app.web.router import _build_context, templates

    # Mirror the GET page's availability guard (login_email_page): without a
    # mail transport the sent-page's "We sent a sign-in link" would be a lie —
    # _generate_and_deliver_magic_link silently skips delivery. Reachable
    # without a fresh GET (stale form, bookmark, scripted client), so the
    # POST must refuse on its own (Devin Review on PR #1288).
    if not is_available():
        return RedirectResponse(url="/login?error=email_not_configured", status_code=303)

    # Strip early so the rendered "we sent a link to <address>" copy shows the
    # cleaned address; the shared helper strips again, harmlessly.
    email = (email or "").strip()
    next_path = safe_next_path(next, default="")

    # Offload the blocking SMTP send off the event loop — same Tier-1
    # rationale as the JSON /send-link variant.
    user, link, send_error = await run_in_threadpool(
        _generate_and_deliver_magic_link,
        email,
        next_path,
        base_url=public_base_url(request=request),
    )

    console_mode = bool(user) and is_local_dev_mode()
    if send_error and not console_mode:
        # Same trade as the JSON sibling: a configured-but-failing transport
        # must not render the "we sent a link" page. send_error is only set
        # for existing accounts, so unknown addresses keep the generic page.
        return RedirectResponse(url="/login?error=email_send_failed", status_code=303)
    if console_mode:
        logger.warning("=" * 60)
        logger.warning("Magic link for %s (LOCAL_DEV_MODE fallback):", email)
        logger.warning("    %s", link)
        logger.warning("=" * 60)

    ctx = _build_context(
        request,
        email=email,
        console_mode=console_mode,
        magic_url=link if console_mode else None,
        next_path=next_path,
        # The sent-page expiry sentence renders from the real token TTL — a
        # hand-copied number in the template drifted to "15 minutes" while
        # the token lived an hour (Devin Review on PR #1288).
        expires_minutes=MAGIC_LINK_EXPIRY // 60,
    )
    return templates.TemplateResponse(request, "login_magic_link_sent.html", ctx)


def _consume_token(email: str, token: str) -> dict:
    """Validate & consume a magic-link token atomically. Returns the user dict or raises 401.

    Compare-and-swap routed through the repository factory so the read/write
    hits the ACTIVE backend (Postgres when configured). The raw CAS that used
    to run on a DuckDB ``_get_db`` connection here read the frozen DuckDB
    system file on PG instances — the token written by ``send_magic_link``
    (factory) lived in PG, so verification never matched and magic-link login
    401'd (#518). ``users_repo().consume_reset_token`` stamps a unique
    CONSUMED marker and returns True iff THIS call won the race.

    The marker is not cleared afterwards: ``reset_token_created`` is NULL'd by
    the CAS so the stale ``CONSUMED:…`` value can never match a real token, and
    the next ``send_magic_link`` overwrites it. (The old step-3 cleanup was
    explicitly best-effort — "not a lockout".)
    """
    # TTL cutoff computed in Python (parameterized INTERVAL arithmetic isn't
    # portable across backends).
    email = (email or "").strip()
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=MAGIC_LINK_EXPIRY)
    # Unique marker for this consumption attempt — the CAS stamps it so the
    # repo can report who won the race without relying on affected-row counts.
    consume_id = f"CONSUMED:{secrets.token_hex(16)}"

    repo = users_repo()
    # The CAS returns the row it stamped, and THAT is the account this link
    # belongs to. Re-resolving by address would go through get_by_email_ci,
    # which returns the oldest case variant — but an admin-issued reset mints
    # the token by user id, so it can sit on a newer one. Minting the session
    # from a second address lookup would then log the person in as a different
    # account, with that account's group memberships.
    consumed_id = repo.consume_reset_token(email=email, token=hash_token(token), cutoff=cutoff, consume_id=consume_id)
    if not consumed_id:
        raise HTTPException(status_code=401, detail="Invalid or expired link")

    user = repo.get_by_id(consumed_id)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid link")
    # The CAS carries its own `active = TRUE` predicate, so the stamped row is
    # live — but "live" is not the same as "this address may sign in". The
    # password doors refuse when the row `get_by_email_ci` resolves to is
    # deactivated, however good the credential on a sibling case variant is
    # (`_shadowed_by_deactivated_identity` in the password provider), and a
    # magic link that ignored that would let the same instance answer
    # "deactivated" at the login form while minting a session here.
    colliding = repo.list_by_email_ci(user.get("email") or email)
    if colliding and not bool(colliding[0].get("active", True)):
        raise HTTPException(status_code=403, detail="Account deactivated")
    return user


@router.post("/verify")
@_rate_limiter.limit("10/minute")
async def verify_magic_link(
    request: Request,
    body: MagicLinkVerify,
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Verify a magic link token and issue JWT (JSON API for programmatic clients).

    Rate limited 10/min per IP to slow brute-forcing the 32-byte
    ``reset_token`` (the same column doubles as the magic-link token).
    """
    user = _consume_token(body.email, body.token)
    role_label = _role_label(user, conn)
    jwt_token = create_access_token(user["id"], user["email"])
    return {"access_token": jwt_token, "token_type": "bearer", "email": user["email"], "role": role_label}


@router.get("/verify")
@_rate_limiter.limit("10/minute")
async def verify_magic_link_get(
    request: Request,
    email: str,
    token: str,
    next: str = "",
):
    """Click-through variant — verifies token, sets cookie, redirects to the
    page the user originally asked for (``?next``) or the operator-configured
    home route.

    This is the URL we embed in outgoing emails (and the dev-fallback link), so
    clicking it in a mail client logs the user in without a separate API call.

    Rate limited 10/min per IP for the same reason as the POST variant —
    don't let the click-through path bypass the brute-force throttle.
    """
    user = _consume_token(email, token)
    jwt_token = create_access_token(user["id"], user["email"])
    # Secure whenever served over HTTPS (proxy-aware via request scheme +
    # resolved public origin), not only when DOMAIN is set — see
    # app.auth.public_url.cookie_secure.
    from app.auth.public_url import cookie_secure

    use_secure = cookie_secure(request)
    # Re-sanitize the destination even though it was sanitized when the link
    # was minted — the link is user-visible and could be tampered with.
    # safe_next_path falls back to the home route for empty/hostile values.
    from app.auth._common import safe_next_path

    target = safe_next_path(next)
    response = RedirectResponse(url=target, status_code=302)
    from app.instance_config import session_cookie_domain

    response.set_cookie(
        key="access_token",
        value=jwt_token,
        httponly=True,
        max_age=SESSION_COOKIE_MAX_AGE_SECONDS,
        samesite="lax",
        secure=use_secure,
        domain=session_cookie_domain(),
    )
    return response


def _send_email(email: str, token: str, next_path: str = "", base_url: str | None = None):
    """Send the magic-link email via SMTP (raises on delivery failure).

    SMTP relay is the only transport — SendGrid works through
    ``SMTP_HOST=smtp.sendgrid.net`` (see ``app.auth._common.send_smtp_email``
    for why the SDK branch is gone).
    """
    from app.auth._common import send_smtp_email

    link = _build_magic_link(email, token, next_path, base_url)
    send_smtp_email(email, "Login Link", f"Login link: {link}")
