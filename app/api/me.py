"""Self-scoped user endpoints for the /home onboarding flow.

POST /api/me/onboarded toggles ``users.onboarded`` for the calling user
and writes an audit_log row distinguishing the trigger source:

- ``agnes_init``       — fired by the CLI's ``agnes init`` final step.
- ``self_acknowledged`` — fired by the on-page "I've already set this up"
  button shown to users who set up locally before /home shipped.
- ``self_unmark``      — fired by the on-page "Mark me as offboarded"
  button (visible once the user is onboarded).

The body's optional ``onboarded`` field defaults to ``True`` for
backward compat with existing ``agnes init`` calls. Pass ``false`` to
flip back — useful when an analyst wipes their workspace and wants the
inline install steps back, or when an operator demos the not-onboarded
view without an SQL UPDATE.

Idempotent — a second call still returns 200 and writes a second audit
row, so duplicate fires are visible without breaking the client. See
origin: docs/brainstorms/home-page-requirements.md §2 + §6.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.auth.dependencies import get_current_user
from src.repositories import audit_repo, usage_repo, users_repo

router = APIRouter(prefix="/api/me", tags=["me"])


class OnboardedRequest(BaseModel):
    source: Literal["agnes_init", "self_acknowledged", "self_unmark"] = "agnes_init"
    onboarded: bool = True


@router.post("/onboarded")
async def post_onboarded(
    body: OnboardedRequest = OnboardedRequest(),
    user: dict = Depends(get_current_user),
):
    target = bool(body.onboarded)
    users_repo().update(user["id"], onboarded=target)
    audit_repo().log(
        user_id=user["id"],
        action="user_onboarded" if target else "user_offboarded",
        params={"source": body.source},
        result="success",
    )
    return {"status": "ok", "onboarded": target}


# ---------------------------------------------------------------------------
# PATCH /api/me/display-name — self-service display name edit (issue #1036)
# ---------------------------------------------------------------------------

_DISPLAY_NAME_MAX_LEN = 120


class DisplayNameRequest(BaseModel):
    name: str = Field(..., max_length=_DISPLAY_NAME_MAX_LEN, description="New display name (max 120 chars).")


@router.patch("/display-name", status_code=status.HTTP_200_OK)
async def patch_display_name(
    body: DisplayNameRequest,
    user: dict = Depends(get_current_user),
):
    """Update the calling user's display name.

    Auth: any authenticated user; the update is scoped to their own row.
    Email stays unchanged — it is the identity key from the auth provider
    and may only change through the provider's own flow.

    Google Workspace sync only sets ``name`` at account *creation* (first
    sign-in), not on subsequent logins or group-sync runs, so a manually-
    set name is never overwritten by the sync process.

    Returns ``{"status": "ok", "name": "<new name>"}`` on success.
    """
    stripped = body.name.strip()
    users_repo().update_display_name(user["id"], stripped)
    audit_repo().log(
        user_id=user["id"],
        action="user_display_name_updated",
        params={"name": stripped},
        result="success",
    )
    return {"status": "ok", "name": stripped}


# ---------------------------------------------------------------------------
# Admin elevation consent gate (app/auth/elevation.py)
# ---------------------------------------------------------------------------


class ElevationRequest(BaseModel):
    paused: bool


@router.post("/elevation")
async def post_elevation(
    body: ElevationRequest,
    response: Response,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Pause or resume the calling admin's own god-mode for this browser.

    Browser-session-scoped by design (a cookie the elevation middleware
    reads; the CLI has no cookie jar), so it deliberately has no CLI/MCP
    sibling. Gated on RAW Admin membership — NOT ``require_admin`` —
    because a paused admin must be able to re-elevate (require_admin
    would 403 them into a locked-out loop). The cookie only ever reduces
    privilege; see ``app/auth/elevation.py``.

    CSRF posture: rides the platform-wide SameSite=Lax + JSON-body
    protection (a cross-site form POST can neither carry the Lax cookie
    nor produce a JSON body). No double-submit token, consistent with the
    rest of ``/api/me`` and the documented platform CSRF posture.
    """
    from app.auth.access import is_user_admin
    from app.auth.elevation import ELEVATED, ELEVATION_COOKIE, PAUSED
    from app.auth.public_url import cookie_secure

    if not isinstance(user, dict) or not is_user_admin(user["id"]):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")

    value = PAUSED if body.paused else ELEVATED
    response.set_cookie(
        ELEVATION_COOKIE,
        value,
        max_age=30 * 24 * 3600,
        httponly=True,
        secure=cookie_secure(request),
        samesite="lax",
        path="/",
    )
    audit_repo().log(
        user_id=user["id"],
        action="admin_elevation_paused" if body.paused else "admin_elevation_resumed",
        params={},
        result="success",
    )
    return {"status": "ok", "paused": body.paused}


# ---------------------------------------------------------------------------
# GET /api/me/home-stats — backing data for the /home status frame
# ---------------------------------------------------------------------------


_WINDOW_INTERVALS = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
}


def _username_for_stats(user: dict) -> str:
    """Map a users row to the filesystem username used by the session
    collector and stored in ``usage_session_summary.username``.

    Mirrors ``app.api.admin_user_sessions._username_from_user``: the
    session collector writes JSONL under the OS username of the agent
    process which, for current deployments, equals the email local-part.
    Kept inline here so this endpoint has no cross-module dependency on
    an admin-only helper; if the mapping evolves both copies must update.
    """
    email: str = user.get("email", "") or ""
    return email.split("@")[0] if "@" in email else email


def compute_home_stats(user: dict, window: str = "24h") -> dict:
    """Pure helper that returns the home-stats payload for the given user.

    Shared by the HTTP endpoint and the /home Jinja handler (server-side
    initial render). Unknown windows clamp to ``24h`` so callers never
    need to pre-validate. Returns a dict with ISO-stringified
    ``last_pull_at`` (or None) so the same shape works for both JSON
    serialization and Jinja rendering.

    Routes through ``usage_repo()`` / ``users_repo()`` so the counters are
    correct on either the DuckDB or Postgres state backend.
    """
    delta = _WINDOW_INTERVALS.get(window)
    if delta is None:
        window = "24h"
        delta = _WINDOW_INTERVALS["24h"]

    username = _username_for_stats(user)
    uid = user.get("id") or ""
    since = datetime.now(timezone.utc) - delta

    stats = usage_repo().home_stats(uid, username, since)
    user_row = users_repo().get_by_id(uid) if uid else None
    last_pull_at = user_row.get("last_pull_at") if user_row else None

    input_t = stats["input_tokens"]
    output_t = stats["output_tokens"]
    cache_read = stats["cache_read"]
    cache_creation = stats["cache_creation"]
    return {
        "window": window,
        "last_pull_at": last_pull_at.isoformat() if last_pull_at else None,
        "sessions": stats["sessions"],
        "prompts": stats["prompts"],
        "tokens": {
            "input": input_t,
            "output": output_t,
            "cache_read": cache_read,
            "cache_creation": cache_creation,
            "total": input_t + output_t + cache_read + cache_creation,
        },
        "projects": stats["projects"],
    }


@router.get("/home-stats")
async def get_home_stats(
    window: str = "24h",
    user: dict = Depends(get_current_user),
):
    """Return the five counters rendered in the /home status frame for
    the calling user, over a 24-hour or 7-day window.

    Missing rows (new user, no telemetry yet) surface as zeros / null
    rather than 404 — the frame still renders cleanly for first-day
    analysts.
    """
    return compute_home_stats(user, window)


@router.get("/external-identity")
async def get_external_identity(user: dict = Depends(get_current_user)):
    """The calling user's linked external identity (design 2026-08-28).

    Live repo lookup, deliberately NOT a JWT claim: link state must stay
    revocable (an admin unlink takes effect on the next call, not after a
    30-day stateless token expires). None of the returned fields is a
    secret — the ``subject`` is the Entra directory object ID the admin
    identities list shows too. Postgres-only feature: on a DuckDB-backed
    instance the repo factory raises ``RequiresPostgresBackend`` and the
    app-wide handler answers the typed 501.
    """
    from src.repositories import user_external_identities_repo

    row = user_external_identities_repo().get_by_user_id(user["id"])
    if row is None:
        return {"linked": False}
    return {
        "linked": True,
        "provider_type": row["provider_type"],
        "tenant_id": row["tenant_id"],
        "subject": row["subject"],
        "linked_at": row["linked_at"],
        "last_login_at": row["last_login_at"],
    }
