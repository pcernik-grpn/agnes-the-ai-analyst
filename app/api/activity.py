"""Activity Center read API.

Three endpoints under /api/admin/activity, all gated by require_admin:

    GET /api/admin/activity            unified timeline — audit_log +
                                        sync_history + llm_usage +
                                        agent_scope_snapshots (E3 slice 2;
                                        never chat_messages, privacy
                                        decision). `trail=` narrows to one
                                        physical trail, e.g. `trail=audit`.
    GET /api/admin/activity/health     health pulse (cached 30s server-side)
    GET /api/admin/activity/sync       per-table recent sync feed

Each endpoint emits one audit_log entry per call (action='activity.read')
unless the same actor + same filter combination was logged in the last 60s
(see _should_audit / _audit_read). The dedup cache is per uvicorn worker
(see _RECENT_AUDITS for the multi-worker caveat).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.access import require_admin
from src.observability.posthog_client import get_posthog

from src.repositories import (
    audit_repo,
    session_processor_state_repo,
    sync_state_repo,
    usage_repo,
    users_repo,
)

router = APIRouter(prefix="/api/admin/activity", tags=["activity"])

_HEALTH_CACHE: dict = {"data": None, "expires_at": None}
_HEALTH_TTL_SECONDS = 30

# Per-process dedup cache.
# NOTE: This is module-global and lives only in ONE uvicorn worker.
# v40 ships requiring single-worker uvicorn (Agnes compose default).
# If multi-worker is later enabled, this must move to a shared store
# (Redis, or a TTL-cleaned DuckDB table). The dedup is a performance
# safeguard against /health polling spam, NOT a security control — a
# malicious admin polling at 61s intervals can defeat it. See parent
# spec §7.3.
_RECENT_AUDITS: dict[tuple[str, str], datetime] = {}
_AUDIT_SUPPRESS_WINDOW = timedelta(seconds=60)


def _should_audit(actor_id: str, filter_payload: dict) -> bool:
    """True if this (actor, filter) combo hasn't been audited in the last 60s."""
    key = (actor_id, hashlib.sha1(json.dumps(filter_payload, sort_keys=True, default=str).encode()).hexdigest())
    now = datetime.now(timezone.utc)
    last = _RECENT_AUDITS.get(key)
    if last is not None and (now - last) < _AUDIT_SUPPRESS_WINDOW:
        return False
    _RECENT_AUDITS[key] = now
    return True


def _audit_read(user: dict, endpoint: str, filter_payload: dict) -> None:
    """Emit a deduped audit row for an AC read endpoint."""
    actor_id = (user or {}).get("id") or "anonymous"
    if not _should_audit(actor_id, {"endpoint": endpoint, **filter_payload}):
        return
    audit_repo().log(
        user_id=actor_id,
        action="activity.read",
        params={"endpoint": endpoint, **filter_payload},
        result="success",
        # client_kind intentionally omitted (F0 audit-context autofill,
        # Task 1) — this read isn't necessarily browser-only.
    )
    try:
        get_posthog().capture(
            event=f"activity_{endpoint}_viewed",
            distinct_id=actor_id,
            properties={k: v for k, v in filter_payload.items() if v is not None},
        )
    except Exception:
        pass  # never break the request


@router.get("")
def activity_timeline(
    since_minutes: int = Query(default=1440, ge=1, le=43200),
    user_id: Optional[str] = None,
    action_prefix: Optional[str] = None,
    resource: Optional[str] = None,
    resource_prefix: Optional[str] = None,
    result_pattern: Optional[str] = None,
    result_class: Optional[str] = None,
    q: Optional[str] = None,
    source: Optional[str] = None,
    trail: Optional[str] = Query(
        default=None,
        description="Narrow to one physical trail: audit | sync | llm | agent_scope. "
        "Unset returns the unified timeline across all four.",
    ),
    include_self_reads: bool = Query(default=False),
    cursor_ts: Optional[datetime] = None,
    cursor_id: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200),
    user: dict = Depends(require_admin),
):
    since = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
    cursor = (cursor_ts, cursor_id) if cursor_ts and cursor_id else None

    try:
        rows, next_cursor = audit_repo().query_unified(
            trail=trail,
            since=since,
            user_id=user_id,
            action_prefix=action_prefix,
            resource=resource,
            resource_prefix=resource_prefix,
            result_pattern=result_pattern,
            result_class=result_class,
            q=q,
            source=source,
            include_self_reads=include_self_reads,
            cursor=cursor,
            limit=limit,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Enrich rows with users.email + users.name so the UI can render a
    # readable label (`name <email>`) instead of an opaque UUID. One small
    # IN(...) query per page; users table is small. Skipped when the page
    # carries no audit rows that reference a user.
    ids = list({r["user_id"] for r in rows if r.get("user_id")})
    if ids:
        info = users_repo().get_info_by_ids(ids)
        for r in rows:
            extra = info.get(r.get("user_id")) or {}
            r["user_email"] = extra.get("email")
            r["user_name"] = extra.get("name")

    _audit_read(
        user,
        "timeline",
        {
            "since_minutes": since_minutes,
            "user_id": user_id,
            "action_prefix": action_prefix,
            "resource": resource,
            "resource_prefix": resource_prefix,
            "result_pattern": result_pattern,
            "result_class": result_class,
            "source": source,
            "trail": trail,
            "q": q,
        },
    )
    return {
        "rows": rows,
        "next_cursor": ({"ts": next_cursor[0].isoformat(), "id": next_cursor[1]} if next_cursor else None),
        "filter": {
            "since_minutes": since_minutes,
            "user_id": user_id,
            "action_prefix": action_prefix,
            "resource": resource,
            "resource_prefix": resource_prefix,
            "result_pattern": result_pattern,
            "result_class": result_class,
            "source": source,
            "trail": trail,
            "include_self_reads": include_self_reads,
            "q": q,
        },
    }


@router.get("/health")
def activity_health(
    user: dict = Depends(require_admin),
):
    now = datetime.now(timezone.utc)
    if _HEALTH_CACHE["data"] is not None and _HEALTH_CACHE["expires_at"] > now:
        return _HEALTH_CACHE["data"]
    data = _compute_health(now)
    _HEALTH_CACHE["data"] = data
    _HEALTH_CACHE["expires_at"] = now + timedelta(seconds=_HEALTH_TTL_SECONDS)
    _audit_read(user, "health", {})
    return data


@router.get("/sync")
def activity_sync(
    since_minutes: int = Query(default=1440, ge=1, le=43200),
    limit: int = Query(default=100, ge=1, le=500),
    user: dict = Depends(require_admin),
):
    since = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
    rows = sync_state_repo().list_recent(since=since, limit=limit)
    _audit_read(user, "sync", {"since_minutes": since_minutes})
    return {"rows": rows}


def _compute_health(now: datetime) -> dict:
    """Build the health-pulse dict.

    Fields:
        scheduler: seconds since most recent run_session_processor or
                   marketplace.sync_all audit row.
        sync_24h: ok/fail counts from sync_history in last 24h.
        active_users_today: distinct user_id from audit_log since UTC midnight.
        memory_pipeline: latest verification processor run state.
        diagnose_warnings: count of active diagnose warnings (placeholder 0 in MVP).
    """
    # 1) scheduler freshness
    last_tick = audit_repo().last_scheduler_tick()
    if last_tick is None:
        scheduler_age_s = None
        scheduler_color = "yellow"
        scheduler_value = "never"
    else:
        if last_tick.tzinfo is None:
            last_tick = last_tick.replace(tzinfo=timezone.utc)
        scheduler_age_s = int((now - last_tick).total_seconds())
        if scheduler_age_s > 7200:
            scheduler_color = "red"
        elif scheduler_age_s > 1800:
            scheduler_color = "yellow"
        else:
            scheduler_color = "green"
        scheduler_value = _format_age(scheduler_age_s)

    # 2) sync 24h
    sync_counts = sync_state_repo().status_counts_since(now - timedelta(hours=24))
    ok = sync_counts.get("ok", 0)
    fail = sum(c for s, c in sync_counts.items() if s and s != "ok")
    total = ok + fail
    if total == 0:
        sync_color = "yellow"
    elif fail == 0:
        sync_color = "green"
    elif ok / total >= 0.95:
        sync_color = "yellow"
    else:
        sync_color = "red"
    sync_value = f"{ok} ok / {fail} fail"

    # 3) active users today
    midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    active = audit_repo().active_users_since(midnight)

    # 4) memory pipeline
    mem = session_processor_state_repo().activity_since("verification", now - timedelta(hours=1))
    if mem["last_processed_at"]:
        mem_color = "green"
        mem_value = f"ok ({mem['items_extracted']} items 1h)"
    else:
        mem_color = "yellow"
        mem_value = "idle 1h+"

    # 5) session-ingest reconciliation (24h): every uploaded file must have
    # a summary row. Join on the FILE basename, never session_id —
    # resumed/forked sessions carry a different content-derived id.
    up_files = set(audit_repo().upload_filenames_since(now - timedelta(hours=24)))
    if up_files:
        ingested_files = usage_repo().session_file_basenames_since(
            now - timedelta(hours=25)  # 1h grace for the processor cadence
        )
        ingest_gap = len(up_files - ingested_files)
    else:
        ingest_gap = 0
    ingest_color = "green" if ingest_gap == 0 else "yellow"
    ingest_value = f"{len(up_files)} up / {len(up_files) - ingest_gap} ingested"

    # 6) diagnose warnings — placeholder
    diag_color = "green"
    diag_value = "0"

    fields = [
        {"key": "scheduler", "value": scheduler_value, "raw": scheduler_age_s, "color": scheduler_color},
        {"key": "sync_24h", "value": sync_value, "raw": {"ok": ok, "fail": fail}, "color": sync_color},
        {"key": "active_users_today", "value": str(active), "raw": active, "color": "green"},
        {"key": "memory_pipeline", "value": mem_value, "raw": None, "color": mem_color},
        {"key": "session_ingest", "value": ingest_value, "raw": ingest_gap, "color": ingest_color},
        {"key": "diagnose_warnings", "value": diag_value, "raw": 0, "color": diag_color},
    ]

    overall = (
        "red"
        if any(f["color"] == "red" for f in fields)
        else "yellow"
        if any(f["color"] == "yellow" for f in fields)
        else "green"
    )

    sentence = _build_sentence(fields, overall)
    return {"status": overall, "fields": fields, "sentence": sentence}


def _format_age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _build_sentence(fields: list, overall: str) -> str:
    by_key = {f["key"]: f for f in fields}
    if overall == "green":
        return (
            f"All systems nominal — {by_key['active_users_today']['value']} active users, "
            f"last scheduler tick {by_key['scheduler']['value']}, "
            f"{by_key['sync_24h']['value']} in 24h."
        )
    issues = [f["key"] for f in fields if f["color"] != "green"]
    return f"Degraded: {', '.join(issues)}. Investigate Activity timeline filtered to these subsystems."
