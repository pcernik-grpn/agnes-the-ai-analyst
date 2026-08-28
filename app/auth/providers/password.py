"""Password auth provider for FastAPI."""

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
import duckdb
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.auth.jwt import create_access_token, SESSION_COOKIE_MAX_AGE_SECONDS
from app.auth.access import is_user_admin
from app.auth.dependencies import _get_db, is_local_dev_mode, require_session_token
from app.auth.login_audit import ACCOUNT_ACTIVATED, SETUP_LINK_REQUESTED, audit_auth_event, audit_login_success
from app.auth.provider_registry import require_provider
from app.auth.token_hash import hash_token
from app.auth.rate_limit import limiter as _rate_limiter


from src.repositories import (
    audit_repo,
    users_repo,
)


def _role_label(user: dict, conn: duckdb.DuckDBPyConnection) -> str:
    """Display label for the response payload only — `admin` for Admin
    group members, `user` otherwise. Authorization at runtime checks
    `is_user_admin` directly; this label is purely cosmetic for the
    response shape."""
    return "admin" if is_user_admin(user["id"], conn) else "user"


logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/auth/password",
    tags=["auth"],
    dependencies=[Depends(require_provider("password"))],
)

RESET_TOKEN_TTL = timedelta(hours=24)
SETUP_TOKEN_TTL = timedelta(days=7)
MIN_PASSWORD_LEN = 8


def _audit(user_id: str, action: str, result: str | None = None) -> None:
    """Fire-and-forget audit log entry. Swallows all errors."""
    try:
        audit_repo().log(
            user_id=user_id,
            action=action,
            resource="auth",
            result=result,
        )
    except Exception:
        pass  # Audit failure must not block auth


class PasswordLoginRequest(BaseModel):
    email: str
    password: str


class PasswordSetupRequest(BaseModel):
    email: str
    token: str
    password: str


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


def is_available() -> bool:
    return True  # Always available


def _has_email_transport() -> bool:
    # SMTP only — mirrors the email provider's predicate. SENDGRID_API_KEY
    # used to count here, but the SDK branch it advertised could never send
    # (the `sendgrid` package was not a dependency).
    return bool(os.environ.get("SMTP_HOST"))


def _cookie_secure(request: Request | None = None) -> bool:
    # Set Secure whenever the deployment is served over HTTPS. Delegates to the
    # shared resolver (previously keyed only on DOMAIN, which left the 30-day
    # session cookie non-Secure behind a non-Caddy TLS terminator that didn't
    # set DOMAIN — a cleartext-exposure gap).
    from app.auth.public_url import cookie_secure

    return cookie_secure(request)


def _set_login_cookie(response, user_id: str, email: str, request: Request | None = None) -> None:
    from app.instance_config import session_cookie_domain

    token = create_access_token(user_id, email)
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        max_age=SESSION_COOKIE_MAX_AGE_SECONDS,
        samesite="lax",
        secure=_cookie_secure(request),
        domain=session_cookie_domain(),
    )


def _base_url(request: Request) -> str:
    explicit = os.environ.get("SERVER_URL")
    if explicit:
        return explicit.rstrip("/")
    return str(request.base_url).rstrip("/")


def build_reset_url(request: Request, email: str, token: str) -> str:
    return f"{_base_url(request)}/auth/password/reset?email={quote(email, safe='')}&token={token}"


def build_setup_url(request: Request, email: str, token: str) -> str:
    return f"{_base_url(request)}/auth/password/setup?email={quote(email, safe='')}&token={token}"


def _row_verifying_password(repo, email: str, password: str) -> tuple[Optional[dict], bool]:
    """Resolve a sign-in by the CREDENTIAL, not by the address tie-break.

    ``get_by_email_ci`` answers "which account is this address" — deterministic
    oldest-first. That is the right answer for provisioning, and the wrong one
    for a credential check: where two case variants of one address coexist (the
    population the case-insensitive work exists for), the password hash may sit
    on the newer row, and checking only the oldest turns a working sign-in into
    ``401 Invalid email or password``.

    So every row carrying a hash is tried and the one whose hash verifies is
    returned. Inactive rows are tried too, deliberately: skipping them would
    answer "invalid password" for a deactivated account, and the caller who
    proved the credential is owed the honest ``Account deactivated`` instead —
    the caller gates on the returned row's own ``active`` flag.

    Returns ``(row, any_hash_present)``. ``any_hash_present`` separates "no such
    account / external auth only" from "wrong password" for callers that report
    them differently, without leaking which it was to the client.

    Same shape as ``reset_confirm``'s ``list_by_email_ci`` scan.
    """
    rows = repo.list_by_email_ci(email)
    candidates = [u for u in rows if u.get("password_hash")]
    if not candidates:
        return None, False
    ph = PasswordHasher()
    verified = None
    for u in candidates:
        try:
            ph.verify(u["password_hash"], password)
            verified = u
            break
        except VerifyMismatchError:
            continue
    if verified is None:
        return None, True
    return _shadowed_by_deactivated_identity(rows, verified), True


def _shadowed_by_deactivated_identity(rows: list[dict], verified: dict) -> dict:
    """``verified`` unless the address's resolved identity is deactivated, in
    which case that row is returned so the caller refuses.

    Scanning variants for the credential must not become a way around an
    offboarding. ``get_by_email_ci`` deliberately does not prefer an active row
    (see its docstring, and the 0.83.48 operator note): a stale disabled variant
    shadowing the live account is the safe failure, because a wrongly-refused
    sign-in is visible and fixable while a bypassed deactivation is neither.
    Its answer is ``rows[0]`` — ``list_by_email_ci`` returns the same ordering —
    so if THAT row is disabled, no sibling variant may serve the sign-in.

    Applied after the credential verified, never before: short-circuiting on the
    identity row's flag alone would answer "Account deactivated" to anyone who
    typed the address, which is an enumeration oracle. The caller still has to
    prove a credential to learn anything.

    This preserves the shipped contract rather than extending it. Two questions
    are easy to conflate here, so to be explicit about which is settled:

    * **Does this rule apply on every door?** Settled: yes. Password login,
      ``POST /auth/token``, both setup flows, ``reset_confirm`` and the email
      magic link all apply it. A rule that held on some doors and not others
      would let one instance answer "deactivated" at the login form and mint a
      session cookie on a reset link for the same pair of rows.
    * **Should a deactivated variant that is NOT the resolved identity refuse?**
      Open. That widens who is refused rather than keeping the shipped answer,
      and it is a policy call for the repository owner, not something to settle
      inside a bug fix.
    """
    identity = rows[0] if rows else verified
    if not bool(identity.get("active", True)):
        return identity
    return verified


def _row_holding_setup_token(repo, email: str, token: str) -> tuple[Optional[dict], bool]:
    """The row whose ``setup_token`` matches — again the credential, not the
    tie-break. An invitation is minted by user id, so it can sit on a case
    variant ``get_by_email_ci`` does not return, and a valid link would then
    read "Invalid or expired setup link".

    Returns ``(row, identity_active)``. Freshness and ``active`` stay the
    caller's gates so each surface keeps its own copy and status code, and the
    row returned is always the one that HOLDS the token — the freshness check
    reads ``setup_token_created`` off it, and handing back a sibling would report
    an expired link for a perfectly fresh one.

    ``identity_active`` is the separate answer to "may any variant of this
    address sign in at all": ``False`` when the row ``get_by_email_ci`` resolves
    to is disabled, which shadows every variant (see
    ``_shadowed_by_deactivated_identity`` for why, and why only after the token
    matched). Callers refuse on it at their existing deactivated gate.
    """
    hashed = hash_token(token)
    rows = repo.list_by_email_ci(email)
    match = next((u for u in rows if u.get("setup_token") == hashed), None)
    if match is None:
        return None, True
    return match, bool(rows[0].get("active", True))


def _token_is_fresh(created, ttl: timedelta) -> bool:
    if not created:
        return False
    if isinstance(created, str):
        try:
            created = datetime.fromisoformat(created)
        except ValueError:
            return False
    # DuckDB returns TIMESTAMP as offset-naive; we stored it as UTC, so assume UTC.
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created) <= ttl


def _render_message(request: Request, title: str, message: str, status_code: int = 200):
    from app.web.router import templates, _build_context

    ctx = _build_context(request, page_title=title, page_message=message)
    return templates.TemplateResponse(request, "_message.html", ctx, status_code=status_code)


def _render_reset_request_form(request: Request, email: str = "", error: str = ""):
    from app.web.router import templates, _build_context

    ctx = _build_context(request, email=email, error=error)
    return templates.TemplateResponse(request, "password_reset_request.html", ctx)


def _render_reset_form(request: Request, email: str, token: str, error: str = "", reason: str = ""):
    from app.web.router import templates, _build_context

    ctx = _build_context(
        request,
        email=email,
        token=token,
        error=error,
        forced_rotation=(reason == "must_change"),
    )
    return templates.TemplateResponse(request, "password_reset.html", ctx)


def _render_setup_form(request: Request, email: str, token: str, name: str = "", error: str = ""):
    from app.web.router import templates, _build_context

    ctx = _build_context(request, email=email, token=token, name=name, error=error)
    return templates.TemplateResponse(request, "password_setup.html", ctx)


def _send_mail(to_email: str, subject: str, body_text: str, body_html: str | None = None) -> bool:
    """Send an email via SMTP (multipart when ``body_html`` is given).
    Returns True on success, False when no transport is configured or
    delivery failed (failures are logged).

    SMTP relay is the only transport — SendGrid works through
    ``SMTP_HOST=smtp.sendgrid.net`` (see ``app.auth._common.send_smtp_email``
    for why the SDK branch is gone).
    """
    from app.auth._common import send_smtp_email

    if not _has_email_transport():
        return False
    try:
        send_smtp_email(to_email, subject, body_text, body_html)
        return True
    except Exception:
        logger.exception("Failed to send mail to %s", to_email)
    return False


def send_reset_email(request: Request, email: str, token: str) -> bool:
    """Deliver a password-reset link. In LOCAL_DEV_MODE logs the link as well."""
    from app.auth.email_templates import reset_email

    link = build_reset_url(request, email, token)
    if is_local_dev_mode():
        logger.warning("=" * 60)
        logger.warning("Password reset link for %s (LOCAL_DEV_MODE):", email)
        logger.warning("    %s", link)
        logger.warning("=" * 60)
    if not _has_email_transport():
        return False
    subject, body_text, body_html = reset_email(email, link, RESET_TOKEN_TTL)
    return _send_mail(email, subject, body_text, body_html)


def send_setup_email(request: Request, email: str, token: str) -> bool:
    from app.auth.email_templates import invite_email

    link = build_setup_url(request, email, token)
    if is_local_dev_mode():
        logger.warning("=" * 60)
        logger.warning("Account setup link for %s (LOCAL_DEV_MODE):", email)
        logger.warning("    %s", link)
        logger.warning("=" * 60)
    if not _has_email_transport():
        return False
    subject, body_text, body_html = invite_email(email, link, SETUP_TOKEN_TTL)
    return _send_mail(email, subject, body_text, body_html)


# ---- Existing flows ----


@router.post("/login")
@_rate_limiter.limit("10/minute")
async def password_login(
    request: Request,
    body: PasswordLoginRequest,
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Login with email + password."""
    repo = users_repo()
    # Strip only — case is folded by the lookup (SQL). Resolved by the
    # credential rather than the oldest-row tie-break: see
    # `_row_verifying_password`.
    try:
        user, any_hash = _row_verifying_password(repo, (body.email or "").strip(), body.password)
    except Exception:
        logger.exception("Unexpected error during password verification")
        raise HTTPException(status_code=500, detail="Internal server error")
    if user is None:
        # Same 401 whether the address is unknown, carries no hash at all
        # (external auth), or the password simply did not match — `any_hash`
        # exists for the audit trail, not for the caller.
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not bool(user.get("active", True)):
        raise HTTPException(status_code=401, detail="Account deactivated")

    # Forced rotation: the password was set by someone else (seeded admin /
    # admin-set) and emailed/shared in plaintext. The password is verified
    # first (so this can't probe which accounts are flagged), then we refuse to
    # issue a token until the user rotates via the reset flow.
    if user.get("must_change_password"):
        raise HTTPException(status_code=403, detail="password_change_required")

    role_label = _role_label(user, conn)
    token = create_access_token(user["id"], user["email"])
    # 'cli': this is the programmatic route (CLI + desktop client), not the
    # browser form below.
    audit_login_success(user["id"], provider="password", request=request, client_kind="cli")
    return {"access_token": token, "token_type": "bearer", "email": user["email"], "role": role_label}


@router.post("/login/web")
@_rate_limiter.limit("10/minute")
async def password_login_web(
    request: Request,
    email: str = Form(...),
    password: str = Form(""),
    next: str = Form(""),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Web form login — sets cookie and redirects to `next` (or /dashboard)."""
    email = (email or "").strip()
    repo = users_repo()
    # Resolved by the credential, not the oldest-row tie-break — see
    # `_row_verifying_password`.
    try:
        user, any_hash = _row_verifying_password(repo, email, password)
    except Exception:
        logger.exception("Unexpected error during web password verification for %s", email)
        return RedirectResponse(url="/login/password?err=auth_internal", status_code=302)
    if user is None:
        if any_hash:
            # M9: audit failed form-login attempts (mirrors /auth/token
            # endpoint). Attributed to the address's canonical row, since no
            # single row proved the credential.
            canonical = repo.get_by_email_ci(email)
            if canonical:
                _audit(canonical["id"], "login_failed", result="invalid_password")
        return RedirectResponse(url="/login/password?error=invalid", status_code=302)
    if not bool(user.get("active", True)):
        return RedirectResponse(url="/login/password?error=deactivated", status_code=302)

    if user.get("must_change_password"):
        # Password set by someone else (seeded / admin-set): refuse a full
        # session and route the user through the existing reset flow to choose
        # their own password. Mint a one-time reset token and redirect.
        reset_tok = secrets.token_urlsafe(32)
        users_repo().update(
            id=user["id"],
            reset_token=hash_token(reset_tok),
            reset_token_created=datetime.now(timezone.utc),
        )
        return RedirectResponse(
            url=(f"/auth/password/reset?email={quote(user['email'], safe='')}&token={reset_tok}&reason=must_change"),
            status_code=303,
        )

    if next.startswith("/") and not next.startswith("//"):
        target = next
    else:
        from app.instance_config import get_home_route

        target = get_home_route()
    response = RedirectResponse(url=target, status_code=302)
    _set_login_cookie(response, user["id"], user["email"], request)
    audit_login_success(user["id"], provider="password", request=request)
    return response


# ---- JSON programmatic setup (backward compat — used by existing tests) ----


@router.post("/setup")
@_rate_limiter.limit("10/minute")
async def password_setup(
    request: Request,
    request_body: PasswordSetupRequest,
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Set initial password using setup token (JSON API).

    Rate limited 10/min per IP — same throttle as the form sibling
    ``/setup/confirm``. Without this, the new web-form throttle is
    bypassable: an attacker brute-forcing the ``setup_token`` just
    switches to this JSON path and resumes at unbounded RPS.
    """
    repo = users_repo()
    email = (request_body.email or "").strip()
    # The invitation is minted by user id, so it can sit on a case variant the
    # oldest-wins lookup does not return — resolve by the token it carries.
    user, identity_active = _row_holding_setup_token(repo, email, request_body.token)
    if user is None:
        if not repo.get_by_email_ci(email):
            raise HTTPException(status_code=404, detail="User not found")
        raise HTTPException(status_code=400, detail="Invalid setup token")
    if not _token_is_fresh(user.get("setup_token_created"), SETUP_TOKEN_TTL):
        raise HTTPException(status_code=400, detail="Setup token has expired")
    if not bool(user.get("active", True)) or not identity_active:
        raise HTTPException(status_code=403, detail="Account deactivated")

    if len(request_body.password) < MIN_PASSWORD_LEN:
        raise HTTPException(status_code=400, detail=f"Password must be at least {MIN_PASSWORD_LEN} characters")

    ph = PasswordHasher()
    hashed = ph.hash(request_body.password)

    # The user is choosing their OWN password here, so clear any forced-rotation
    # flag (consistent with the web setup_confirm sibling). Without this, a user
    # who was admin-set (must_change_password=True) while still holding a valid
    # setup token would stay flagged after self-serving a new password.
    repo.update(
        id=user["id"],
        password_hash=hashed,
        setup_token=None,
        setup_token_created=None,
        must_change_password=False,
    )
    token = create_access_token(user["id"], user["email"])
    return {"access_token": token, "token_type": "bearer", "message": "Password set successfully"}


# ---- Web flow: password RESET ----


@router.get("/reset", response_class=HTMLResponse)
async def reset_page(
    request: Request,
    email: str = "",
    token: str = "",
    reason: str = "",
):
    """Render the reset flow's GET page.

    With ``email`` + ``token`` (arriving via an emailed reset link) this is
    the 'set new password' form. Without a token it renders the 'enter your
    email' request form — the standalone forgot-password page the login page
    links to. (It used to redirect back to the login page instead, leaving
    the hidden-email POST from the login form as the only way to request a
    reset — which silently submitted an empty address when the user clicked
    Forgot Password before typing their email.)

    ``reason=must_change`` marks the forced-rotation arrival (see
    ``password_login_web``): the credentials were correct, but the password
    was set by someone else and has to be replaced before a session is
    issued. Without it the page is indistinguishable from "you forgot your
    password", and first-time users reasonably conclude their password was
    rejected.
    """
    if not email or not token:
        return _render_reset_request_form(request, email=email)
    return _render_reset_form(request, email=email, token=token, reason=reason)


@router.post("/reset")
@_rate_limiter.limit("5/minute")
async def reset_request(
    request: Request,
    email: str = Form(""),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Request a password-reset link. Anti-enumeration: same response regardless.

    Rate limited at the same 5/min as ``/auth/email/send-link`` — the
    attack surface is identical (single IP rotates random recipient
    addresses, anti-enumeration response shape masks which addresses
    landed, attacker burns SMTP relay quota + spams real users).
    """
    # Strip only — case is folded by the lookup itself (get_by_email_ci), so a
    # mixed-case row an admin stored as-is is still found when the person types
    # their address in lower case.
    email = (email or "").strip()
    if not email:
        # Nothing could have been sent, so the "Check your email" copy below
        # would be false — re-ask for the address instead. An empty submission
        # reveals nothing, so anti-enumeration does not apply to this branch.
        return _render_reset_request_form(request, error="Enter your email address.")
    repo = users_repo()
    user = repo.get_by_email_ci(email)
    if user and bool(user.get("active", True)):
        token = secrets.token_urlsafe(32)
        repo.update(
            id=user["id"],
            reset_token=hash_token(token),
            reset_token_created=datetime.now(timezone.utc),
        )
        sent = send_reset_email(request, user["email"], token)
        if _has_email_transport() and not sent:
            # A configured transport that failed must not render the
            # success page — the person would wait for a mail that was
            # never sent. Trades a sliver of anti-enumeration away while
            # the relay is down (the 500 implies the account exists);
            # the silent failure was judged worse. Unknown addresses
            # never attempt a send, so they keep the generic page.
            # Deliberately NOT bypassed in LOCAL_DEV_MODE (unlike the
            # magic-link JSON dev path, which returns the link plus a
            # send_error field): the reset flow has no response field to
            # carry the failure, and a broken-but-configured relay is
            # worth surfacing in dev too — the link is already in the
            # logs either way.
            return _render_message(
                request,
                title="Email delivery failed",
                message="We could not send the password-reset email. "
                "Please try again later or contact your administrator.",
                status_code=500,
            )
    return _render_message(
        request,
        title="Check your email",
        message="If an account exists for that email, a password-reset link has been sent. "
        "The link is valid for 24 hours.",
    )


def _forced_rotation_reason(user: dict | None) -> str:
    """Derive the reset form's ``reason`` from an already-fetched account.

    The form only posts back ``email``/``token``/``password``/``confirm_password``
    — no ``reason`` — so an error re-render can't just thread the query param
    through like the initial GET does. Reading the account's own
    ``must_change_password`` flag is the source of truth for which copy
    belongs on the page, and unlike a hidden form field it can't be forged by
    editing the POST body to swap one explanation for the other.

    Takes the user dict rather than looking it up by email itself: callers
    must only derive this AFTER independently establishing that the caller
    holds a valid reset token (see ``reset_confirm``) — otherwise this
    becomes an account-enumeration oracle (existence + forced-rotation
    state, leaked to anyone who can post an email address).
    """
    return "must_change" if user and user.get("must_change_password") else ""


@router.post("/reset/confirm")
@_rate_limiter.limit("10/minute")
async def reset_confirm(
    request: Request,
    email: str = Form(...),
    token: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
):
    """Submit a new password using a reset token.

    Rate limited 10/min per IP to slow brute-force guessing of the 32-byte
    URL-safe ``reset_token`` — the token is high-entropy but logs / proxy
    referer leaks have surfaced partial tokens before, and there's no
    reason to allow unbounded attempts.
    """
    email = (email or "").strip()
    repo = users_repo()

    # Anti-enumeration: validate the token BEFORE deriving any
    # account-specific copy or reporting a password-mismatch/length error.
    # `_forced_rotation_reason` used to run unconditionally at the top of
    # this handler, so an unauthenticated caller could post an arbitrary
    # email with two deliberately mismatched passwords and read the
    # rendered heading to learn whether the account exists AND whether it
    # is flagged for forced rotation — without ever proving they hold the
    # reset token. Every other surface in this module (`reset_request`,
    # `setup_request`, `setup_page`) is careful not to do that.
    #
    # This lookup only PEEKS at the token — mirrors the equality check
    # `setup_confirm` does below — it does not consume it. That matters:
    # a legitimate user who mistypes their confirmation still holds a
    # valid, unconsumed token and needs the explanatory copy on the
    # re-rendered form, so validating the token here must not burn it.
    # The single-use atomic consumption still happens exactly once,
    # further down, only after the passwords pass validation.
    # Peek across every row colliding on this address, not just "the account
    # for this address": an admin-issued reset mints the token by user id, so
    # it can sit on a case variant that get_by_email_ci (oldest wins) does not
    # return — and a valid link would then render "Invalid or expired".
    hashed = hash_token(token)
    colliding = repo.list_by_email_ci(email)
    user = next(
        (
            u
            for u in colliding
            if bool(u.get("active", True))
            and u.get("reset_token") == hashed
            and _token_is_fresh(u.get("reset_token_created"), RESET_TOKEN_TTL)
        ),
        None,
    )
    # The same shadow the password doors apply: a proven token on an active
    # sibling must not outrank a deactivated resolved identity, or one instance
    # answers "deactivated" at the login form and hands out a session cookie on
    # a reset link for the same pair of rows. Judged after the token proved
    # itself, so this leaks nothing a holder did not already know.
    if user is not None and colliding and not bool(colliding[0].get("active", True)):
        user = None
    token_valid = user is not None
    if not token_valid:
        # Generic copy regardless of whether the account exists or is
        # flagged for forced rotation — the caller hasn't proven they
        # hold the token yet.
        return _render_reset_form(request, email=email, token=token, error="Invalid or expired reset link.")

    # From here the caller has proven token ownership, so it's safe to
    # derive the forced-rotation copy from the account's own state and
    # reuse it on every error re-render below — a mistyped confirmation or
    # a too-short password used to fall back to the generic "Reset Your
    # Password" copy because the reason never survived the POST, leaving a
    # forced-rotation user thinking their correct password was rejected.
    reason = _forced_rotation_reason(user)
    if password != confirm_password:
        return _render_reset_form(request, email=email, token=token, error="Passwords do not match.", reason=reason)
    if len(password) < MIN_PASSWORD_LEN:
        return _render_reset_form(
            request,
            email=email,
            token=token,
            error=f"Password must be at least {MIN_PASSWORD_LEN} characters.",
            reason=reason,
        )

    # Atomic compare-and-swap to consume the reset token. Mirrors the
    # magic-link CAS in app/auth/providers/email.py::_consume_token (issue
    # #82/M10) — without it, two concurrent POSTs with the same valid token
    # could both succeed in setting different new passwords. Lower
    # severity than the magic-link race (attacker would need the reset
    # token AND to race the legitimate user) but closes the asymmetry.
    cutoff = datetime.now(timezone.utc) - RESET_TOKEN_TTL
    consume_id = f"CONSUMED:{secrets.token_hex(16)}"
    # Atomic compare-and-swap to consume the reset token, via the repo factory so
    # it hits the ACTIVE backend. (A raw DuckDB `conn.execute` here silently
    # failed on Postgres deployments — the token was written to PG by the factory
    # but the CAS read DuckDB, so every reset / forced-rotation login 'expired'.)
    try:
        won = repo.consume_reset_token(email=email, token=hash_token(token), cutoff=cutoff, consume_id=consume_id)
        # `won` is the id of the stamped row — the account the token belongs to.
    except Exception as exc:
        err = str(exc).lower()
        if "conflict" in err or "transaction" in err:
            return _render_reset_form(
                request, email=email, token=token, error="Invalid or expired reset link.", reason=reason
            )
        raise
    if not won:
        # Token never matched, expired, account deactivated, or the race was
        # lost. Single error keeps the UX simple and avoids leaking which.
        return _render_reset_form(
            request, email=email, token=token, error="Invalid or expired reset link.", reason=reason
        )

    # Won the race — fetch the row the CAS stamped (not a second address
    # lookup, which could resolve a different case variant) and apply the
    # password change.
    user = repo.get_by_id(won)
    if not user:
        return _render_reset_form(
            request, email=email, token=token, error="Invalid or expired reset link.", reason=reason
        )

    ph = PasswordHasher()
    repo.update(
        id=user["id"],
        password_hash=ph.hash(password),
        reset_token=None,
        reset_token_created=None,
        # Clear the forced-rotation flag: the user has now set their own password.
        must_change_password=False,
    )

    response = RedirectResponse(url="/login/password?msg=password_reset", status_code=302)
    _set_login_cookie(response, user["id"], user["email"], request)
    return response


# ---- Web flow: initial SETUP ----


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(
    request: Request,
    email: str = "",
    token: str = "",
):
    """Render the initial 'set password + name' form when arriving via invite link.

    Note: we render the form based on URL params only, without a DB lookup, so
    the response is identical for valid and invalid email/token combinations
    (anti-enumeration). Token validity is checked at POST /setup/confirm."""
    if not email or not token:
        return RedirectResponse(url="/login/password", status_code=302)
    return _render_setup_form(request, email=email, token=token)


@router.post("/setup/request")
@_rate_limiter.limit("5/minute")
async def setup_request(
    request: Request,
    email: str = Form(""),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Self-service 'Request Access' — emails a setup link if user is pre-approved and unset.

    Same 5/min rate limit as ``/auth/password/reset`` and ``/send-link``
    — same email-bombing surface (anti-enumeration response, sends mail
    on each request).
    """
    # Strip only — case is folded by the lookup itself (get_by_email_ci), so a
    # mixed-case row an admin stored as-is is still found when the person types
    # their address in lower case.
    email = (email or "").strip()
    if email:
        repo = users_repo()
        user = repo.get_by_email_ci(email)
        # Only issue setup token if user exists, has no password yet, and is active.
        if user and not user.get("password_hash") and bool(user.get("active", True)):
            token = secrets.token_urlsafe(32)
            repo.update(
                id=user["id"],
                setup_token=hash_token(token),
                setup_token_created=datetime.now(timezone.utc),
            )
            sent = send_setup_email(request, user["email"], token)
            # The response below is identical whether or not the address
            # matched, by design. This row is the only place an admin can see
            # that a link was actually minted for a real account.
            audit_auth_event(
                SETUP_LINK_REQUESTED,
                user["id"],
                provider="password",
                request=request,
                email_sent=bool(sent),
            )
            if _has_email_transport() and not sent:
                # Same rationale as reset_request: a configured-but-failing
                # transport must surface, not render the success page.
                return _render_message(
                    request,
                    title="Email delivery failed",
                    message="We could not send the setup email. Please try again later or contact your administrator.",
                    status_code=500,
                )
    return _render_message(
        request,
        title="Check your email",
        message="If your account is pre-approved, a setup link has been sent to your email. "
        "Ask an administrator if you do not receive it.",
    )


@router.post("/setup/confirm")
@_rate_limiter.limit("10/minute")
async def setup_confirm(
    request: Request,
    email: str = Form(...),
    token: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    name: str = Form(""),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Web form: complete initial password setup via setup token.

    Rate limited 10/min per IP — same rationale as ``/reset/confirm``:
    high-entropy ``setup_token`` should still not be brute-forceable at
    unbounded RPS in case a partial token leaks via logs / referer.
    """
    email = (email or "").strip()
    if password != confirm_password:
        return _render_setup_form(request, email=email, token=token, name=name, error="Passwords do not match.")
    if len(password) < MIN_PASSWORD_LEN:
        return _render_setup_form(
            request,
            email=email,
            token=token,
            name=name,
            error=f"Password must be at least {MIN_PASSWORD_LEN} characters.",
        )

    repo = users_repo()
    # Resolved by the token, not the oldest-row tie-break — see
    # `_row_holding_setup_token`.
    user, identity_active = _row_holding_setup_token(repo, email, token)
    if user is None:
        return _render_setup_form(request, email=email, token=token, name=name, error="Invalid or expired setup link.")
    if not _token_is_fresh(user.get("setup_token_created"), SETUP_TOKEN_TTL):
        return _render_setup_form(
            request,
            email=email,
            token=token,
            name=name,
            error="Setup link has expired. Ask an administrator for a new one.",
        )
    if not bool(user.get("active", True)) or not identity_active:
        return _render_setup_form(request, email=email, token=token, name=name, error="This account is deactivated.")

    ph = PasswordHasher()
    updates: dict = dict(
        password_hash=ph.hash(password),
        setup_token=None,
        setup_token_created=None,
        # Clear the forced-rotation flag: the user has now set their own password.
        must_change_password=False,
    )
    if name.strip():
        updates["name"] = name.strip()
    repo.update(id=user["id"], **updates)

    from app.instance_config import get_home_route

    response = RedirectResponse(url=get_home_route(), status_code=302)
    _set_login_cookie(response, user["id"], user["email"], request)
    # Two events, not one: the invite was consumed, and the person is now
    # signed in. Collapsing them would lose the fact that this particular
    # session is the account's first.
    audit_auth_event(ACCOUNT_ACTIVATED, user["id"], provider="password", request=request)
    audit_login_success(user["id"], provider="password", request=request)
    return response


# ---- Web flow: self-serve password CHANGE (B6) ----
#
# Unlike every flow above, the caller already holds a session — this is a
# logged-in user replacing their own password, not proving ownership of an
# address via a token. `require_session_token` (not just `get_current_user`)
# gates both routes: a PAT must never be able to rotate the credential it
# was minted under, matching the other credential-minting/rotating doors
# this dependency exists for (see its docstring).


def _render_password_change_form(
    request: Request,
    user: dict,
    *,
    has_password: bool,
    error: str = "",
    success: str = "",
):
    from app.web.router import _get_or_mint_web_csrf, _set_web_csrf_cookie, _build_context, templates

    csrf_token = _get_or_mint_web_csrf(request)
    ctx = _build_context(
        request,
        user=user,
        has_password=has_password,
        error=error,
        success=success,
        csrf_token=csrf_token,
    )
    response = templates.TemplateResponse(request, "password_change.html", ctx)
    _set_web_csrf_cookie(response, request, csrf_token)
    return response


@router.get("/change", response_class=HTMLResponse)
async def password_change_page(request: Request, user: dict = Depends(require_session_token)):
    """Self-serve change-password page, linked from the account menu."""
    row = users_repo().get_by_id(user["id"]) or {}
    return _render_password_change_form(request, user, has_password=bool(row.get("password_hash")))


@router.post("/change")
@_rate_limiter.limit("5/minute")
async def password_change(
    request: Request,
    body: PasswordChangeRequest,
    user: dict = Depends(require_session_token),
):
    """Change the caller's own password. Session token only (PAT rejected by
    `require_session_token`, matching `/auth/tokens` and every other door
    that mints or rotates a credential).

    Double-submit CSRF (F2) is checked FIRST, before anything else runs —
    including the no-password-hash lookup below. A cookie-authenticated JSON
    POST is not automatically CSRF-safe (the cookie fallback in
    `get_current_user` means an ordinary cross-site fetch would still carry
    the session), so the caller must echo the `web_csrf` cookie value in the
    `X-CSRF-Token` header (same mechanism as `me_profile_refetch_groups`).
    Checking it first — rather than after the account-state read — closes a
    response-shape leak (Devin Review on PR #1548): a caller with no CSRF
    token would otherwise get a different status for an SSO-only account
    (400) than a password account (403 from the CSRF check), letting a
    same-site page without the token distinguish the two account types.
    Nothing here is safe to do before proving the caller sent this request
    on purpose. The GET page mints the `web_csrf` cookie unconditionally —
    even for an SSO-only account with no form to submit — so this ordering
    never blocks a legitimate caller who visited it first.

    Existing sessions and PATs are NOT invalidated by a password change —
    this endpoint only replaces the password hash. Revoking sessions/PATs is
    a separate action (`DELETE /auth/tokens/{id}`), same as every other
    password door in this module (reset, setup, admin reset).
    """
    from app.web.router import _web_csrf_ok

    if not _web_csrf_ok(request, request.headers.get("x-csrf-token", "")):
        raise HTTPException(status_code=403, detail="csrf_check_failed")

    repo = users_repo()
    row = repo.get_by_id(user["id"])
    if not row or not row.get("password_hash"):
        raise HTTPException(
            status_code=400,
            detail="This account signs in through single sign-on and has no password to change.",
        )

    ph = PasswordHasher()
    try:
        ph.verify(row["password_hash"], body.current_password)
    except VerifyMismatchError:
        # `invalid_password` — the same result literal `login_failed` uses
        # for a wrong credential (already classified "denied" in
        # src/audit_helpers.py) — not a new one; the action name
        # ("password_change_failed") already says which door this was.
        _audit(user["id"], "password_change_failed", result="invalid_password")
        raise HTTPException(status_code=403, detail="Current password is incorrect")
    except Exception:
        logger.exception("Unexpected error verifying current password during change")
        raise HTTPException(status_code=500, detail="Internal server error")

    if len(body.new_password) < MIN_PASSWORD_LEN:
        raise HTTPException(status_code=400, detail=f"Password must be at least {MIN_PASSWORD_LEN} characters")

    repo.update(
        id=user["id"],
        password_hash=ph.hash(body.new_password),
        # `users.reset_token` is shared between the password-reset and
        # email-magic-link flows — a self-serve change makes either stale,
        # so clear it rather than leave a live token usable after the
        # password it would have set no longer matters.
        reset_token=None,
        reset_token_created=None,
        must_change_password=False,
    )
    _audit(user["id"], "password_changed", result="success")
    return {"status": "ok"}
