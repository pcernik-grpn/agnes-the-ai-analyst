"""Adoption dashboard endpoints — `/api/admin/adoption/*`.

A business-facing view of how the system is actually used (active users,
time spent, skills, sessions), distinct from the technical telemetry /
sessions / activity pages. All data is aggregated on the fly from
``usage_session_summary`` (time / sessions / tokens / prompts) and
``usage_events`` (distinct-users-per-day, skill events); no new tables.

KPI cards reflect a selectable window (24h / 7d / 30d); the trend series
always covers the last 30 days at daily granularity. A per-user
drill-down mirrors the same shape scoped to one user.

All admin-only, audit-logged (suppressed via the shared cache so a busy
dashboard doesn't flood audit_log).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.access import require_admin
from app.api.activity import _should_audit

from src.repositories import (
    audit_repo,
    usage_repo,
    users_repo,
)

router = APIRouter(prefix="/api/admin/adoption", tags=["admin-adoption"])
logger = logging.getLogger(__name__)

# Window pill values → lookback delta. Unknown values clamp to 7d so the
# endpoint never 400s on a stale/garbled query string.
_WINDOW_DELTAS = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}
_TREND_DAYS = 30


def _cutoff(window: str) -> datetime:
    delta = _WINDOW_DELTAS.get(window) or _WINDOW_DELTAS["7d"]
    return datetime.now(timezone.utc) - delta


def _norm_window(window: str) -> str:
    return window if window in _WINDOW_DELTAS else "7d"


def _hours(seconds: int) -> float:
    return round((seconds or 0) / 3600.0, 1)


def _username_for_user(user: dict) -> str:
    """Filesystem username for a users row — the email local-part.

    Same rule as ``admin_user_sessions._username_from_user`` and
    ``me._username_for_stats``; kept local so this module has no
    cross-import on an admin-only helper. If the mapping evolves, all
    copies must update together.
    """
    email: str = user.get("email", "") or ""
    return email.split("@")[0] if "@" in email else email


def _local_part(email: str) -> str:
    return email.split("@")[0] if email and "@" in email else (email or "")


def _enrich_with_users(rows: list[dict]) -> list[dict]:
    """Attach the real ``users``-table identity (name / email / registered)
    to adoption rows so the UI renders the same person as ``/admin/users``.

    The adoption source (``usage_session_summary``) carries only a
    ``user_id`` (UUID == ``users.id``, present for sessions ingested after
    the v45 backfill) and ``username`` (the email local-part) — there is no
    full email column. Resolve by ``user_id`` first; for legacy rows without
    one, fall back to matching the local-part against ``users.email``, but
    only when it maps to exactly one user (a local-part owned by >1 user
    across domains is ambiguous and left unresolved). Unresolved rows get
    ``registered=False`` so the template shows a bare email + empty avatar.
    """
    users = users_repo().list_all()
    by_id: dict = {}
    by_local: dict = {}
    for u in users:
        uid = u.get("id")
        if uid:
            by_id[uid] = u
        lp = _local_part(u.get("email") or "")
        if lp:
            # None flags an ambiguous local-part (owned by >1 user) — once
            # ambiguous it stays unresolved, never silently picking one.
            by_local[lp] = None if lp in by_local else u
    for r in rows:
        match = by_id.get(r.get("user_id")) or by_local.get(r.get("username") or "")
        r["name"] = match.get("name") if match else None
        r["email"] = match.get("email") if match else None
        r["registered"] = match is not None
    return rows


def _audit(user: dict, action: str, params: dict) -> None:
    actor_id = user.get("id") or "anonymous"
    if _should_audit(actor_id, {"endpoint": action, **params}):
        try:
            audit_repo().log(
                # client_kind intentionally omitted (F0 audit-context
                # autofill, Task 1) — this read isn't necessarily browser-only.
                user_id=actor_id,
                action=action,
                params=params,
                result="success",
            )
        except Exception:
            logger.exception("audit_log write failed for %s; continuing", action)


def _trend_start() -> date:
    """First day of the 30-day trend window (inclusive), so the response
    carries exactly _TREND_DAYS entries ending today (UTC)."""
    return datetime.now(timezone.utc).date() - timedelta(days=_TREND_DAYS - 1)


def _build_series(sessions_map: dict, events_map: dict, start: date, *, per_user: bool = False) -> list[dict]:
    """Zero-filled daily rows over the 30-day window. Missing days surface
    as zeros rather than gaps so the chart x-axis is continuous."""
    out = []
    for i in range(_TREND_DAYS):
        d = start + timedelta(days=i)
        s = sessions_map.get(d, {})
        e = events_map.get(d, {})
        row = {
            "day": d.isoformat(),
            "active_hours": _hours(s.get("active_seconds", 0)),
            "wall_hours": _hours(s.get("wall_seconds", 0)),
            "sessions": s.get("sessions", 0),
            "prompts": s.get("prompts", 0),
            "tokens": s.get("tokens", 0),
            "skill_invocations": e.get("skill_events", 0),
        }
        if per_user:
            row["tool_calls"] = s.get("tool_calls", 0)
        else:
            row["active_users"] = e.get("active_users", 0)
        out.append(row)
    return out


# ===========================================================================
# Overall (system-wide) adoption
# ===========================================================================


@router.get("/kpis")
def adoption_kpis(
    window: str = Query("7d"),
    user: dict = Depends(require_admin),
):
    """Headline adoption numbers for the selected window."""
    window = _norm_window(window)
    k = usage_repo().adoption_kpis(_cutoff(window))
    _audit(user, "adoption.kpis", {"window": window})
    return {
        "window": window,
        "active_users": k["active_users"],
        "active_seconds": k["active_seconds"],
        "wall_seconds": k["wall_seconds"],
        "active_hours": _hours(k["active_seconds"]),
        "wall_hours": _hours(k["wall_seconds"]),
        "sessions": k["sessions"],
        "prompts": k["prompts"],
        "skill_invocations": k["skill_invocations"],
        "distinct_skills": k["distinct_skills"],
        "tokens": k["tokens"],
        "tool_calls": k["tool_calls"],
        "tool_errors": k["tool_errors"],
    }


@router.get("/series")
def adoption_series(_user: dict = Depends(require_admin)):
    """Daily trend over the last 30 days (independent of the KPI window)."""
    start = _trend_start()
    repo = usage_repo()
    days = _build_series(
        repo.adoption_sessions_series(start),
        repo.adoption_events_series(start),
        start,
    )
    return {"start_date": start.isoformat(), "days": days}


@router.get("/top-users")
def adoption_top_users(
    window: str = Query("7d"),
    q: Optional[str] = None,
    limit: int = Query(10, ge=1, le=100),
    _user: dict = Depends(require_admin),
):
    """Most active users in the window, ranked by active time."""
    window = _norm_window(window)
    rows = usage_repo().adoption_top_users(_cutoff(window), limit=limit, q=q)
    rows = _enrich_with_users(rows)
    for r in rows:
        r["active_hours"] = _hours(r["active_seconds"])
    return {"window": window, "rows": rows}


@router.get("/top-skills")
def adoption_top_skills(
    window: str = Query("7d"),
    limit: int = Query(10, ge=1, le=100),
    _user: dict = Depends(require_admin),
):
    window = _norm_window(window)
    rows = usage_repo().adoption_top_skills(_cutoff(window), limit=limit)
    return {"window": window, "rows": rows}


# ===========================================================================
# Per-user drill-down
# ===========================================================================


def _resolve(user_id: str) -> dict:
    target = users_repo().get_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    return target


@router.get("/users/{user_id}/kpis")
def adoption_user_kpis(
    user_id: str,
    window: str = Query("7d"),
    user: dict = Depends(require_admin),
):
    window = _norm_window(window)
    target = _resolve(user_id)
    username = _username_for_user(target)
    k = usage_repo().adoption_user_kpis(_cutoff(window), user_id, username)
    _audit(user, "adoption.user_kpis", {"window": window, "target": user_id})
    return {
        "window": window,
        "user_id": user_id,
        "username": username,
        "email": target.get("email"),
        "active_hours": _hours(k["active_seconds"]),
        "wall_hours": _hours(k["wall_seconds"]),
        **k,
    }


@router.get("/users/{user_id}/series")
def adoption_user_series(
    user_id: str,
    _user: dict = Depends(require_admin),
):
    target = _resolve(user_id)
    username = _username_for_user(target)
    start = _trend_start()
    repo = usage_repo()
    days = _build_series(
        repo.adoption_user_sessions_series(start, user_id, username),
        repo.adoption_user_events_series(start, user_id, username),
        start,
        per_user=True,
    )
    return {"start_date": start.isoformat(), "days": days}


@router.get("/users/{user_id}/top-skills")
def adoption_user_top_skills(
    user_id: str,
    window: str = Query("7d"),
    limit: int = Query(10, ge=1, le=100),
    _user: dict = Depends(require_admin),
):
    window = _norm_window(window)
    target = _resolve(user_id)
    username = _username_for_user(target)
    rows = usage_repo().adoption_user_top_skills(_cutoff(window), user_id, username, limit=limit)
    return {"window": window, "rows": rows}


@router.get("/users/{user_id}/top-tools")
def adoption_user_top_tools(
    user_id: str,
    window: str = Query("7d"),
    limit: int = Query(10, ge=1, le=100),
    _user: dict = Depends(require_admin),
):
    window = _norm_window(window)
    target = _resolve(user_id)
    username = _username_for_user(target)
    rows = usage_repo().adoption_user_top_tools(_cutoff(window), user_id, username, limit=limit)
    return {"window": window, "rows": rows}
