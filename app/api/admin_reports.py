"""GET /api/admin/reports/marketplace-digest?period=daily|weekly — one
consolidated, report-shaped JSON payload for an external rendering pipeline
(e.g. an n8n workflow whose LLM node fills an HTML template from this JSON and
publishes it).

Why a dedicated endpoint rather than the existing /api/admin/telemetry/* ones:
those expose tool/user/error/DAU rollups only. A marketplace digest also needs
per-item usage (curated/flea/builtin), installs/adoption, sync health, and
"what's not landing" — composed here from the backend-aware repository layer so
the digest reads the right backend (DuckDB or Postgres) and downstream
processing stays near zero.

Admin-only (PAT-gated for headless callers), audit-logged with the same
burst-suppression cache as the other admin telemetry entry points.
"""

from __future__ import annotations

import logging
from datetime import datetime, date, timezone, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.access import require_admin
from app.api.activity import _should_audit
from src.repositories import (
    audit_repo,
    marketplace_plugins_repo,
    marketplace_registry_repo,
    reports_repo,
)

router = APIRouter(prefix="/api/admin/reports", tags=["admin-reports"])
logger = logging.getLogger(__name__)

_STALE_SYNC_HOURS = 48
_TOP_N = 10
_MOVERS_N = 5
_ZERO_USAGE_N = 25


def _midnight(d: date) -> datetime:
    """UTC midnight at the start of `d` — usage_events.occurred_at is a
    timezone-aware TIMESTAMP, so we compare against aware datetimes."""
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _delta_pct(value: float, prev: float) -> Optional[float]:
    """Percentage change vs the comparison period. None when there's no base
    (prev == 0) so the renderer can show "new" instead of a divide-by-zero
    spike."""
    if not prev:
        return None
    return round((value - prev) / prev * 100.0, 1)


def _kpi(value: float, prev: float) -> dict:
    return {"value": value, "prev": prev, "delta_pct": _delta_pct(value, prev)}


@router.get("/marketplace-digest")
def marketplace_digest(
    period: Literal["daily", "weekly"] = Query("daily"),
    date_str: Optional[str] = Query(
        None, alias="date",
        description="Anchor day (YYYY-MM-DD, UTC) = most recent complete day to "
                    "report. Defaults to yesterday.",
    ),
    user: dict = Depends(require_admin),
):
    """Compose the full digest payload for `period`.

    Windows (all UTC, upper bound exclusive):
    - daily:  primary = the anchor day; comparison = the day before;
              trend_series = trailing 14 days ending on the anchor.
    - weekly: primary = 7 days ending on the anchor; comparison = the 7 days
              before that; trend_series = trailing 30 days ending on the anchor.
    """
    # ---- resolve the anchor + the primary / comparison / trend ranges --------
    if date_str:
        try:
            anchor = date.fromisoformat(date_str)
        except ValueError:
            raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")
    else:
        anchor = (datetime.now(timezone.utc) - timedelta(days=1)).date()

    if period == "daily":
        p_start, p_end = anchor, anchor + timedelta(days=1)
        c_start, c_end = anchor - timedelta(days=1), anchor
        trend_len = 14
    else:  # weekly
        p_start, p_end = anchor - timedelta(days=6), anchor + timedelta(days=1)
        c_start, c_end = anchor - timedelta(days=13), anchor - timedelta(days=6)
        trend_len = 30

    p_start_ts, p_end_ts = _midnight(p_start), _midnight(p_end)
    c_start_ts, c_end_ts = _midnight(c_start), _midnight(c_end)
    trend_start = anchor - timedelta(days=trend_len - 1)
    trend_start_ts = _midnight(trend_start)

    rr = reports_repo()

    # ---- headline KPIs -------------------------------------------------------
    p_ev = rr.event_window(p_start_ts, p_end_ts)
    c_ev = rr.event_window(c_start_ts, c_end_ts)
    p_sessions = rr.session_count(p_start_ts, p_end_ts)
    c_sessions = rr.session_count(c_start_ts, c_end_ts)
    p_inst = rr.install_counts(p_start_ts, p_end_ts)
    c_inst = rr.install_counts(c_start_ts, c_end_ts)
    # Opt-in installs only. System-plugin subscriptions are platform
    # provisioning, not adoption — one system rollout otherwise reports more
    # "new installs" than the platform had active users that day, which is what
    # made this KPI row read as self-contradictory. Kept as `system_installs`.
    p_installs = p_inst["curated"] + p_inst["flea"]
    c_installs = c_inst["curated"] + c_inst["flea"]
    p_rate = round(p_ev["errors"] / p_ev["invocations"], 4) if p_ev["invocations"] else 0.0
    c_rate = round(c_ev["errors"] / c_ev["invocations"], 4) if c_ev["invocations"] else 0.0

    # active_users counts PEOPLE with >=1 synced session event in the window;
    # new_installs counts (user x item) ROWS. Different units — read
    # active_users against `distinct_installers`, not against `new_installs`.
    # Same UNIT, but not the same cohort: active_users comes from
    # usage_events.username, installers from the ledgers' user_id, and someone
    # can install without being active in the window. Do not derive a rate.
    headline_kpis = {
        "active_users": _kpi(p_ev["active_users"], c_ev["active_users"]),
        "sessions":     _kpi(p_sessions, c_sessions),
        "invocations":  _kpi(p_ev["invocations"], c_ev["invocations"]),
        "errors":       _kpi(p_ev["errors"], c_ev["errors"]),
        "error_rate":   _kpi(p_rate, c_rate),
        "new_installs": _kpi(p_installs, c_installs),
        "distinct_installers": _kpi(p_inst["distinct_installers"], c_inst["distinct_installers"]),
        "system_installs": _kpi(p_inst["system"], c_inst["system"]),
    }

    # ---- trend_series (per-day, for sparklines / charts) ---------------------
    ev_by_day = rr.events_daily(trend_start_ts, p_end_ts)
    inst_by_day = rr.installs_daily(trend_start_ts, p_end_ts)
    trend_series = []
    for i in range(trend_len):
        d = trend_start + timedelta(days=i)
        ev = ev_by_day.get(d, {})
        trend_series.append({
            "day": d.isoformat(),
            "active_users": ev.get("active_users", 0),
            "invocations": ev.get("invocations", 0),
            "errors": ev.get("errors", 0),
            "installs": inst_by_day.get(d, 0),
        })

    # ---- usage by source (primary window) ------------------------------------
    by_source = rr.by_source(p_start_ts, p_end_ts)

    # ---- marketplace-item aggregates -----------------------------------------
    # Aggregate the primary and comparison windows separately, then take the
    # union of keys so an item that dropped to zero still shows up in `falling`.
    primary = rr.items_window(p_start, p_end)
    compare = rr.items_window(c_start, c_end)

    # Per-item distinct users. The daily rollup column is a per-day distinct, so
    # SUM-ing it over a multi-day window overcounts users active on several days.
    # Daily reports span one day, so the rollup value is exact. There is no
    # window-aligned true-distinct source for an arbitrary weekly window (the
    # precomputed sliding-window snapshot refreshes from "now" and would be
    # inconsistent with this report's invocation counts), so weekly per-item
    # distinct_users is reported as null rather than an inflated or misaligned
    # number. Headline active_users (from usage_events) stays window-accurate.
    items = []
    for key in set(primary) | set(compare):
        source, type_, parent, name = key
        p = primary.get(key, {})
        inv = p.get("invocations", 0)
        err = p.get("error_count", 0)
        du = p.get("distinct_users", 0) if period == "daily" else None
        prev_inv = compare.get(key, {}).get("invocations", 0)
        items.append({
            "source": source, "type": type_, "parent_plugin": parent, "name": name,
            "invocations": inv, "distinct_users": du, "error_count": err,
            "prev_invocations": prev_inv, "delta_pct": _delta_pct(inv, prev_inv),
        })

    def _public(it: dict) -> dict:
        return {k: it[k] for k in (
            "source", "type", "parent_plugin", "name",
            "invocations", "distinct_users", "error_count", "delta_pct",
        )}

    top_items = []
    for rank, it in enumerate(
        sorted([i for i in items if i["invocations"] > 0],
               key=lambda x: x["invocations"], reverse=True)[:_TOP_N],
        start=1,
    ):
        row = _public(it)
        row["rank"] = rank
        top_items.append(row)

    # Rising includes brand-new / reactivated items (prev == 0 → delta_pct None),
    # which are exactly the ones worth surfacing. They have no finite growth rate,
    # so rank them first (treat as +inf), then by largest delta, then volume.
    rising = [
        {"name": it["name"], "source": it["source"], "type": it["type"],
         "invocations": it["invocations"], "delta_pct": it["delta_pct"]}
        for it in sorted(
            [i for i in items if i["invocations"] > i["prev_invocations"]],
            key=lambda x: (x["delta_pct"] if x["delta_pct"] is not None else float("inf"),
                           x["invocations"]),
            reverse=True,
        )[:_MOVERS_N]
    ]
    falling = [
        {"name": it["name"], "source": it["source"], "type": it["type"],
         "invocations": it["invocations"], "delta_pct": it["delta_pct"]}
        for it in sorted(
            [i for i in items if i["invocations"] < i["prev_invocations"] and i["delta_pct"] is not None],
            key=lambda x: x["delta_pct"],
        )[:_MOVERS_N]
    ]
    failures = [
        {"name": it["name"], "source": it["source"], "type": it["type"],
         "invocations": it["invocations"], "errors": it["error_count"],
         "error_rate": round(it["error_count"] / it["invocations"], 4) if it["invocations"] else 0.0}
        for it in sorted(
            [i for i in items if i["error_count"] > 0],
            key=lambda x: (x["error_count"], x["invocations"]), reverse=True,
        )[:_TOP_N]
    ]

    # ---- installs / adoption (primary window) --------------------------------
    installs = {
        "curated": rr.installs_curated_detail(p_start_ts, p_end_ts, _TOP_N),
        "flea": rr.installs_flea_detail(p_start_ts, p_end_ts, _TOP_N),
        "total": p_installs,
        "system_total": p_inst["system"],
        "distinct_installers": p_inst["distinct_installers"],
    }

    # ---- registry-derived sections (zero-usage + health) ---------------------
    registry = marketplace_registry_repo().list_all()
    reg_by_id = {r["id"]: r for r in registry}
    plug_repo = marketplace_plugins_repo()
    plugin_counts = plug_repo.count_by_marketplace()
    all_plugins = plug_repo.list_all()

    # A curated plugin "landed" if it (or one of its skills/agents) saw any
    # invocation in the primary window. Matched by plugin NAME: the usage rollup
    # (usage_marketplace_item_daily) is not keyed by marketplace_id, so if two
    # curated marketplaces ship a plugin with the same name they are treated as
    # one here (a same-named plugin in another marketplace could be suppressed
    # from zero_usage). Curated plugin names are effectively a shared namespace
    # in the served marketplace, so collisions are not expected; scoping this by
    # marketplace would require marketplace_id in the rollup (out of scope).
    used_curated = set()
    for it in items:
        if it["source"] != "curated" or it["invocations"] <= 0:
            continue
        # A plugin counts as used when the invocation landed on the plugin
        # itself (a plugin-level row) or on one of its skills/agents (whose
        # parent_plugin names it). We must NOT treat a child item's own name as
        # a plugin name - a skill could share a name with an unrelated plugin.
        if it["parent_plugin"]:
            used_curated.add(it["parent_plugin"])
        if it["type"] == "plugin":
            used_curated.add(it["name"])

    zero_usage = []
    for p in all_plugins:
        # Admin-disabled plugins can't meaningfully be "not landing": they
        # are hidden from served surfaces and cannot receive usage at all
        # (mirrors the served-surface filters). `is_system` was checked here
        # too, for the same reason a mandatory plugin cannot be "not
        # landing"; 0098 made that an ordinary required grant, and a granted
        # plugin with no usage IS a report-worthy finding.
        if p.get("admin_disabled"):
            continue
        reg = reg_by_id.get(p["marketplace_id"]) or {}
        # "Not landing" is about admin-curated department content. Built-in
        # marketplace plugins (usage attributed as source='builtin') are not
        # curated and would otherwise show up here as false zero-usage rows.
        if reg.get("is_builtin"):
            continue
        if p["name"] in used_curated:
            continue
        zero_usage.append({
            "marketplace_id": p["marketplace_id"], "name": p["name"],
            "curator_name": reg.get("curator_name"),
        })
        if len(zero_usage) >= _ZERO_USAGE_N:
            break

    now = datetime.now(timezone.utc)
    marketplace_health = []
    for r in registry:
        last_synced = r.get("last_synced_at")
        last_error = r.get("last_error")
        if last_error:
            status = "error"
        elif r.get("is_builtin"):
            # The built-in marketplace is seeded locally and intentionally
            # skipped by the nightly git sync, so it has no last_synced_at -
            # that's healthy, not stale.
            status = "ok"
        elif last_synced is None:
            status = "stale"
        else:
            synced = last_synced if last_synced.tzinfo else last_synced.replace(tzinfo=timezone.utc)
            status = "stale" if (now - synced) > timedelta(hours=_STALE_SYNC_HOURS) else "ok"
        marketplace_health.append({
            "id": r["id"], "name": r.get("name"), "curator_name": r.get("curator_name"),
            "plugin_count": int(plugin_counts.get(r["id"], 0) or 0),
            "last_synced_at": last_synced.isoformat() if last_synced else None,
            "sync_status": status,
            "last_error": last_error,
        })

    # ---- audit (burst-suppressed, same cache as telemetry endpoints) ---------
    actor_id = user.get("id") or "anonymous"
    if _should_audit(actor_id, {"endpoint": "reports.marketplace_digest", "period": period}):
        try:
            audit_repo().log(
                user_id=actor_id,
                action="reports.marketplace_digest",
                params={"period": period, "anchor": anchor.isoformat()},
                result="success",
                client_kind="web",
            )
        except Exception:
            logger.exception("audit_log write failed for reports.marketplace_digest; continuing")

    return {
        "meta": {
            "report_type": period,
            "generated_at": now.isoformat(),
            "period_start": p_start.isoformat(),
            "period_end": (p_end - timedelta(days=1)).isoformat(),  # inclusive last day
            "comparison_start": c_start.isoformat(),
            "comparison_end": (c_end - timedelta(days=1)).isoformat(),
            "timezone": "UTC",
        },
        "headline_kpis": headline_kpis,
        "trend_series": trend_series,
        "by_source": by_source,
        "top_items": top_items,
        "rising": rising,
        "falling": falling,
        "failures": failures,
        "installs": installs,
        "zero_usage": zero_usage,
        "marketplace_health": marketplace_health,
    }
