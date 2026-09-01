"""GET /api/admin/usage/summary?window=7d|30d|all — aggregated telemetry overview.

Drives the legacy /admin/usage HTML page. All admin-only, audit-logged.

Newer endpoints — /facets, /query, /kpis — back the interactive Usage page
modeled after /admin/activity (filter dropdowns + group-by + searchable
table). They live in the same module so all telemetry HTTP entry points
share the audit-suppression cache and require_admin gate.
"""

from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query

from app.auth.access import require_admin
from app.api.activity import _should_audit
from app.services import usage_stats

from src.repositories import (
    audit_repo,
    usage_repo,
    users_repo,
)

router = APIRouter(prefix="/api/admin/telemetry", tags=["admin-telemetry"])
logger = logging.getLogger(__name__)

_GROUP_BY_COLUMNS = {
    "day": ("CAST(occurred_at AS DATE)", "day"),
    "username": ("username", "username"),
    "tool_name": ("tool_name", "tool_name"),
    "source": ("source", "source"),
    "ref_id": ("ref_id", "ref_id"),
}


@router.get("/summary")
def usage_summary(
    window: Literal["7d", "30d", "all"] = Query("7d"),
    user: dict = Depends(require_admin),
):
    """Compute seven summaries:
    - top_tools: list[{tool_name, invocations, source}]
    - top_users: list[{username, tool_calls}]
    - error_rate: list[{tool_name, invocations, errors, rate}]
    - dau_series: list[{day, active_users}] — 30 entries even when window=7d (sparkline likes 30)
    - dau_avg: float
    - slow_actions: list[{action, p50, p95, p99, max_ms, n}]
    - query_telemetry: dict — on-demand aggregation over query.remote/query.local/
      snapshot.create audit rows (top_tables, frequency, scan-byte totals,
      remote/local split). See UsageRepository.summary_query_telemetry (#410).
    """
    now = datetime.now(timezone.utc)
    if window == "7d":
        cutoff = now - timedelta(days=7)
    elif window == "30d":
        cutoff = now - timedelta(days=30)
    else:
        cutoff = datetime(1970, 1, 1, tzinfo=timezone.utc)

    repo = usage_repo()
    top_tools = repo.summary_top_tools(cutoff)
    top_users = repo.summary_top_users(cutoff)
    error_rate = repo.summary_error_rate(cutoff)

    # DAU series — always 30 days for the sparkline
    dau_start = (now - timedelta(days=30)).date()
    dau_dict = repo.summary_dau(dau_start)
    dau_series = []
    for i in range(30):
        d = dau_start + timedelta(days=i)
        dau_series.append({"day": d.isoformat(), "active_users": dau_dict.get(d, 0)})
    dau_avg = sum(s["active_users"] for s in dau_series) / 30 if dau_series else 0

    slow_actions = repo.summary_slow_actions(cutoff)
    query_telemetry = repo.summary_query_telemetry(cutoff)

    actor_id = user.get("id") or "anonymous"
    if _should_audit(actor_id, {"endpoint": "usage.summary", "window": window}):
        try:
            audit_repo().log(
                user_id=actor_id,
                action="usage.summary",
                params={"window": window},
                result="success",
                client_kind="web",
            )
        except Exception:
            logger.exception("audit_log write failed for usage.summary; continuing")

    return {
        "window": window,
        "top_tools": top_tools,
        "top_users": top_users,
        "error_rate": error_rate,
        "dau_series": dau_series,
        "dau_avg": round(dau_avg, 1),
        "slow_actions": slow_actions,
        "query_telemetry": query_telemetry,
    }


# ===========================================================================
# Interactive Usage page support — /facets /kpis /query
# ===========================================================================


def _usage_window_cutoff(since_minutes: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=since_minutes)


@router.get("/facets")
def usage_facets(
    since_minutes: int = Query(default=10080, ge=1, le=525600),  # default 7d
    _user: dict = Depends(require_admin),
):
    """Distinct values present in usage_events for the selected window so the
    UI dropdowns are closed-set instead of free-text guesses."""
    since = _usage_window_cutoff(since_minutes)
    facets = usage_repo().telemetry_facets(since)
    return {
        "window_minutes": since_minutes,
        "users": facets["users"],
        "tools": facets["tools"],
        "sources": facets["sources"],
        "event_types": facets["event_types"],
    }


@router.get("/kpis")
def usage_kpis(
    since_minutes: int = Query(default=10080, ge=1, le=525600),
    username: Optional[str] = None,
    tool_name: Optional[str] = None,
    source: Optional[str] = None,
    event_type: Optional[str] = None,
    only_errors: bool = False,
    q: Optional[str] = None,
    _user: dict = Depends(require_admin),
):
    """Four headline numbers, scoped to the same filters as /query — plus what
    the window cost.

    The cards on /admin/usage echo these as clickable quick-filters, so the
    server applies the same WHERE the table will see — otherwise the cards
    and the table tell different stories at the same time.

    ``cost_usd`` is the one figure here NOT read from ``usage_events``: token
    counts live on session summaries, so it comes from the shared read model
    (``app.services.usage_stats``) over the same user and window, rounded up to
    whole days. The narrower event filters (tool/source/event_type/only_errors)
    do not apply to it — it prices everything the selected user spent in the
    window, which is the same number /me/activity and the adoption drill-down
    report for them. It is ``null`` when it cannot be computed rather than a
    zero that would read as "free": an unresolvable ``username``, or an
    instance-wide read on a backend without the Postgres-only ``usage_turns``
    table.
    """
    since = _usage_window_cutoff(since_minutes)
    k = usage_repo().telemetry_kpis(
        {
            "since": since,
            "username": username,
            "tool_name": tool_name,
            "source": source,
            "event_type": event_type,
            "only_errors": only_errors,
            "q": q,
        }
    )
    total = k["events_total"]
    error_rate = (k["errors"] / total) if total else 0.0
    return {
        "window_minutes": since_minutes,
        "events_total": total,
        "distinct_users": k["distinct_users"],
        "distinct_tools": k["distinct_tools"],
        "errors": k["errors"],
        "error_rate": round(error_rate, 4),
        "cost_usd": _window_cost(username, since_minutes),
    }


def _window_cost(username: Optional[str], since_minutes: int) -> Optional[float]:
    """USD spent in the window by the filtered user (all users when the filter
    is absent), or ``None`` when the scope cannot be resolved.

    A ``username`` filter carries whatever ``usage_events.username`` holds — the
    email since the v60 canonicalization, its local-part on older rows — while
    the usage read model keys on ``users.id``. Resolving it here (exact email
    first, then local-part) is what lets one KPI card and the adoption
    drill-down agree about one person. An unresolvable filter yields ``None``:
    silently widening to the instance would put every user's spend on a card
    that says it is showing one.
    """
    days = max(1, -(-since_minutes // 1440))  # ceil, so a sub-day window still reads a day
    if username is None:
        return usage_stats.cost_usd_for_user(None, days)
    try:
        target = users_repo().get_by_email_ci(username) or users_repo().get_by_email_prefix(username)
    except Exception:  # pragma: no cover - defensive, a KPI card must not 500
        logger.exception("could not resolve username %r for cost attribution", username)
        return None
    if not target or not target.get("id"):
        return None
    return usage_stats.cost_usd_for_user(target["id"], days)


@router.get("/query")
def usage_query(
    since_minutes: int = Query(default=10080, ge=1, le=525600),
    username: Optional[str] = None,
    tool_name: Optional[str] = None,
    source: Optional[str] = None,
    event_type: Optional[str] = None,
    only_errors: bool = False,
    q: Optional[str] = None,
    group_by: Optional[str] = Query(default=None),
    sort: str = Query(default="invocations:desc"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0, le=50000),
    _user: dict = Depends(require_admin),
):
    """Filtered + optionally grouped read against usage_events.

    `group_by` ∈ {None, 'day', 'username', 'tool_name', 'source', 'ref_id'}.
    When grouped, returns one bucket per row with `invocations`,
    `distinct_users`, `distinct_sessions`, `errors`. When ungrouped, returns
    the raw event rows.

    `sort` syntax: `<column>:<asc|desc>`. For grouped queries the valid
    columns are `bucket`, `invocations`, `distinct_users`, `distinct_sessions`,
    `errors`. For ungrouped queries: `occurred_at`. Unknown sort keys fall
    back to a safe default rather than 400 — UIs evolve faster than this
    endpoint.
    """
    since = _usage_window_cutoff(since_minutes)
    sort_col, _, sort_dir = sort.partition(":")
    sort_dir = "ASC" if (sort_dir or "desc").lower() == "asc" else "DESC"

    return usage_repo().usage_query(
        {
            "since": since,
            "username": username,
            "tool_name": tool_name,
            "source": source,
            "event_type": event_type,
            "only_errors": only_errors,
            "q": q,
        },
        group_by=group_by,
        sort_col=sort_col,
        sort_dir=sort_dir,
        limit=limit,
        offset=offset,
    )
