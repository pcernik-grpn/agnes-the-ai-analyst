"""Self-scoped Stats endpoints for /me/stats — the analyst's own
analytics dashboard.

Four tabs, four endpoints, all gated by ``get_current_user`` so a
caller can only see their own data:

- ``GET /api/me/stats/sessions?limit=&offset=`` — paginated session
  list joined from ``usage_session_summary`` (post-processor) with a
  filesystem scan of un-processed JSONL (matches the admin
  ``list_user_sessions`` shape).
- ``GET /api/me/stats/tokens?days=30`` — daily token series + by-model
  breakdown + top-10 biggest sessions. Powers the Tokens tab chart.
- ``GET /api/me/stats/queries?cursor_ts=&cursor_id=&limit=`` —
  ``audit_log`` rows where ``action LIKE 'query.%'`` (BQ + local
  DuckDB queries) for this user. Cursor-paginated (keyset on
  ``(timestamp, id)``).
- ``GET /api/me/stats/sync?cursor_ts=&cursor_id=&limit=`` —
  ``audit_log`` rows where action is ``sync.*`` or ``manifest.*``,
  plus the user's ``last_pull_at`` for prominent header rendering.

Identity key: every self-scoped read filters on ``users.id``. The
session pipeline writes that id into ``usage_session_summary.user_id``
while ``username`` holds the display email (since v60,
``services/session_pipeline/runner.py``) — filtering ``username`` with
an id matched nothing and rendered zero in every panel. Rows predating
the v45 ``user_id`` column carry no id and stay out of self-views; they
remain visible in the admin session browser, which also matches on
``username``.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import duckdb
from fastapi import APIRouter, Depends, Query

from app.auth.dependencies import _get_db, get_current_user

from src.repositories import (
    audit_repo,
    session_processor_state_repo,
    usage_repo,
    users_repo,
)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/me/stats", tags=["me"])


def _session_data_dir() -> Path:
    """Like ``app.api.admin_user_sessions._session_data_dir`` but also
    honoring the legacy ``AGNES_SESSION_DATA_DIR`` env var. The fallback
    default matches the admin resolver (and the upload API's actual write
    location, ``${DATA_DIR}/user_sessions``) — the old ``/data/sessions``
    default pointed at a directory nothing writes to (#640 review)."""
    return Path(
        os.environ.get("SESSION_DATA_DIR")
        or os.environ.get("AGNES_SESSION_DATA_DIR")
        or "/data/user_sessions"
    )


# ---------------------------------------------------------------------------
# Sessions tab
# ---------------------------------------------------------------------------


def _uploaded_sessions_dir(user_id: str) -> Path:
    """``${DATA_DIR}/user_sessions/<user_id>`` — where ``agnes push``
    deposits JSONL files.  Mirrors ``app.web.router.profile_sessions_page``.
    """
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    return data_dir / "user_sessions" / user_id


@router.get("/sessions")
def list_self_sessions(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> dict:
    """Paginated session list for the calling user.

    Joins ``usage_session_summary`` (processed=true) with a filesystem
    scan of un-processed JSONL so a session appears immediately even
    before the UsageProcessor runs.  Additionally enriches each row
    with verification-pipeline status (from ``session_processor_state``)
    and a ``download_url`` when the uploaded JSONL exists in
    ``${DATA_DIR}/user_sessions/<user_id>/``.
    """
    user_id: str = user["id"]
    # Both ingestion layouts, same as the admin endpoints (#640): the legacy
    # collector writes under the email LOCAL-PART, the upload API under
    # users.id — scanning one of them hid the other's sessions from the
    # self-service list until the usage processor indexed them. The base
    # comes from THIS module's ``_session_data_dir`` (it honors
    # ``AGNES_SESSION_DATA_DIR`` + this endpoint's historical default).
    email_local = (user.get("email") or "").split("@")[0]
    base = _session_data_dir()
    user_dirs = [
        base / name for name in dict.fromkeys([email_local, user_id]) if name
    ]

    try:
        rows_db = usage_repo().list_sessions_for_user_self(user_id)
    except Exception:
        rows_db = []

    processed: dict[str, dict] = {}
    for d in rows_db:
        for k in ("started_at", "ended_at"):
            v = d.get(k)
            if v is not None and hasattr(v, "isoformat"):
                d[k] = v.isoformat()
        d["tokens_total"] = (
            int(d.get("input_tokens") or 0)
            + int(d.get("output_tokens") or 0)
            + int(d.get("cache_read_tokens") or 0)
            + int(d.get("cache_creation_tokens") or 0)
        )
        d["processed"] = True
        # Dedup key: BASENAME of ``session_file``. The session-pipeline
        # runner writes ``session_file = f"{dir_name}/{filename}"`` — the
        # upload directory, i.e. the user_id, NOT the ``username`` column —
        # while the filesystem scan below walks bare filenames. Keying by
        # basename makes both views agree without normalizing the stored
        # column.
        processed[Path(d["session_file"]).name] = d

    all_rows: list[dict] = list(processed.values())
    seen_fs: set[str] = set()
    for user_dir in user_dirs:
        if not user_dir.is_dir():
            continue
        for p in sorted(
            user_dir.glob("*.jsonl"),
            key=lambda x: x.stat().st_mtime,
            reverse=True,
        ):
            if p.name in processed or p.name in seen_fs:
                continue
            seen_fs.add(p.name)
            mtime = datetime.fromtimestamp(p.stat().st_mtime).isoformat()
            all_rows.append({
                "session_file": p.name,
                "session_id": p.stem,
                "started_at": mtime,
                "ended_at": None,
                "active_seconds": 0,
                "wall_seconds": 0,
                "user_messages": 0,
                "tool_calls": 0,
                "tool_errors": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "tokens_total": 0,
                "primary_model": None,
                "processed": False,
            })

    # --- Enrich with verification-pipeline status ---
    # State table keys rows by ``<dir>/<filename>`` (the pipeline runner's
    # session_key). Build the same keys here, fetch their state rows via the
    # factory, then enrich.
    def _state_key(session_file: str) -> str:
        return session_file if "/" in session_file else f"{user_id}/{session_file}"

    state_keys = [_state_key(r["session_file"]) for r in all_rows]
    state_map: dict[str, dict] = {}
    if state_keys:
        try:
            state_map = session_processor_state_repo().get_states_for_session_files(
                "verification", state_keys
            )
        except Exception:
            state_map = {}
    _enrich_pipeline_status(all_rows, user_id, state_map)

    # --- Enrich with download URLs for uploaded JSONL ---
    uploaded_dir = _uploaded_sessions_dir(user_id)
    uploaded_names: set[str] = set()
    if uploaded_dir.is_dir():
        uploaded_names = {p.name for p in uploaded_dir.glob("*.jsonl")}
    for row in all_rows:
        # Basename — processed rows carry the pipeline's dir-prefixed
        # session_file, which would never match the uploaded-file names.
        fname = Path(row.get("session_file", "")).name
        if fname in uploaded_names:
            row["download_url"] = f"/profile/sessions/{fname}"
        else:
            row["download_url"] = None

    all_rows.sort(
        key=lambda r: r.get("started_at") or "",
        reverse=True,
    )
    total = len(all_rows)
    page = all_rows[offset : offset + limit]
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "rows": page,
    }


def _enrich_pipeline_status(
    rows: list[dict],
    user_id: str,
    state_map: dict[str, dict],
) -> None:
    """Add ``pipeline_status`` and ``items_extracted`` from
    ``session_processor_state`` (verification processor) to each row
    in-place.  *state_map* maps the pipeline runner's session_key
    (``<dir>/<filename>``) to its state row (``{'processed_at': ...,
    'items_extracted': ...}``); processed rows already carry that
    prefixed value in ``session_file``, filesystem rows carry a bare
    name uploaded under the user_id dir — prefixing unconditionally
    double-prefixed the former and matched nothing (#640 review).
    """
    if not rows:
        return

    def _state_key(session_file: str) -> str:
        return session_file if "/" in session_file else f"{user_id}/{session_file}"

    for row in rows:
        state = state_map.get(_state_key(row["session_file"]))
        if state is None:
            row["pipeline_status"] = "pending"
            row["items_extracted"] = None
        else:
            items = state.get("items_extracted")
            row["items_extracted"] = items
            row["pipeline_status"] = (
                "extracted" if items and items > 0 else "processed"
            )


# ---------------------------------------------------------------------------
# Tokens tab
# ---------------------------------------------------------------------------


@router.get("/tokens")
def get_tokens(
    days: int = Query(30, ge=1, le=365),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> dict:
    """Token breakdown for the Tokens tab.

    Returns a daily series (last *days* days), by-model breakdown
    (lifetime), top-10 biggest sessions (by total tokens, lifetime),
    and the lifetime grand total. Single round-trip via three
    sub-queries — each scans the same per-user partition of
    ``usage_session_summary`` (filtered on ``user_id``; no secondary
    index — see ``src/db.py::_v94_to_v95`` for why).
    """
    user_id: str = user["id"]

    repo = usage_repo()
    daily_series = repo.tokens_daily_series(user_id, days)
    model_breakdown = repo.tokens_by_model(user_id)
    top = repo.tokens_top_sessions(user_id)
    totals = repo.tokens_totals(user_id)

    return {
        "days": days,
        "daily": daily_series,
        "by_model": model_breakdown,
        "top_sessions": top,
        "totals": totals,
    }


# ---------------------------------------------------------------------------
# Data access tab (BQ + DuckDB queries)
# ---------------------------------------------------------------------------


@router.get("/queries")
def list_self_queries(
    limit: int = Query(50, ge=1, le=200),
    cursor_ts: Optional[datetime] = None,
    cursor_id: Optional[str] = None,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> dict:
    """Audit-log rows where ``action LIKE 'query.%'`` for the caller.

    Covers query.local (DuckDB on parquet), query.hybrid (BQ + local
    join), query.remote (BQ direct), and query.internal (admin
    internal queries that get attributed to the actor). Cursor
    pagination on (timestamp, id) so streams under concurrent
    writes don't double-render rows.
    """
    cursor = (cursor_ts, cursor_id) if cursor_ts and cursor_id else None
    rows, next_cursor = audit_repo().query(
        user_id=user["id"],
        action_prefix="query.",
        cursor=cursor,
        limit=limit,
    )
    return _audit_response(rows, next_cursor, limit)


# ---------------------------------------------------------------------------
# Sync activity tab
# ---------------------------------------------------------------------------


@router.get("/sync")
def list_self_sync_activity(
    limit: int = Query(50, ge=1, le=200),
    cursor_ts: Optional[datetime] = None,
    cursor_id: Optional[str] = None,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> dict:
    """Audit-log rows where action is ``sync.*`` or ``manifest.*``
    for the caller, plus ``users.last_pull_at`` for the header card.

    Two action prefixes are merged with a UNION-ish IN filter via
    ``AuditRepository.query(action_in=[...])`` — but the repo helper
    doesn't take both prefix and IN, so we call twice and merge.
    Cheaper alternative is two SELECTs in the repo; for now we fetch
    two pages and interleave because cursor merging across two
    independent streams is fiddly without a unified ORDER. To keep
    the code obvious, we use ``query_actions(...)`` and accept that
    the cursor is single-stream (start over to page back; first page
    is what matters for the dashboard).
    """
    actions_seen = audit_repo().query_actions(
        actions=_sync_action_list(),
        limit=limit,
    )
    # Filter to this user — query_actions doesn't take user_id.
    user_rows = [r for r in actions_seen if r.get("user_id") == user["id"]]

    row = users_repo().get_by_id(user["id"])
    last_pull_at = row.get("last_pull_at") if row else None

    return {
        "last_pull_at": last_pull_at.isoformat()
        if last_pull_at and hasattr(last_pull_at, "isoformat")
        else last_pull_at,
        "rows": [_audit_row_to_payload(r) for r in user_rows[:limit]],
        # No next_cursor in this branch — fetched a single newest-window
        # page. Pagination beyond the first page is rarely needed for
        # personal sync history; the timeline tab in /admin/activity is
        # the place for deeper dives.
        "next_cursor": None,
    }


def _sync_action_list() -> list[str]:
    """The set of audit actions that surface on the Sync activity tab.

    Concrete known actions today:
    - ``sync.trigger`` — admin manually kicks a sync.
    - ``manifest.fetch`` — added in this PR; bumped on every
      ``GET /api/sync/manifest``.

    Listed explicitly (vs. a prefix LIKE) so accidental future
    ``sync.*`` actions (e.g. an admin-only ``sync.config_change``)
    don't leak into the analyst-facing view without review.
    """
    return ["sync.trigger", "manifest.fetch"]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _audit_row_to_payload(row: dict) -> dict:
    """Convert a raw audit_log row dict to the JSON shape the Stats
    tabs render. Drops `correlation_id` (not useful per-user) and
    iso-stringifies the timestamp."""
    ts = row.get("timestamp")
    return {
        "id": row.get("id"),
        "timestamp": ts.isoformat() if ts and hasattr(ts, "isoformat") else ts,
        "action": row.get("action"),
        "resource": row.get("resource"),
        "result": row.get("result"),
        "duration_ms": row.get("duration_ms"),
        "params": row.get("params"),
        "client_kind": row.get("client_kind"),
    }


def _audit_response(rows: list[dict], next_cursor, limit: int) -> dict:
    """Shared shape for the queries and (alternate path) sync endpoints."""
    if next_cursor is not None:
        ts, cid = next_cursor
        nc = {
            "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else ts,
            "id": cid,
        }
    else:
        nc = None
    return {
        "limit": limit,
        "rows": [_audit_row_to_payload(r) for r in rows],
        "next_cursor": nc,
    }
