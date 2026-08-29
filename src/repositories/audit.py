"""Repository for audit logging."""

import json
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Optional, List, Dict

import duckdb

from src.audit_context import (
    auto_client_ip,
    auto_client_kind,
    auto_correlation_id,
    auto_duration_ms,
    mark_audit_written,
)
from src.audit_helpers import (
    AUDIT_SOURCE_CASE_SQL,
    RESULT_CLASS_CASE_SQL,
    SCHEDULER_ACTION_SQL,
    UNIFIED_TRAILS,
)

# ---------------------------------------------------------------------------
# Unified Activity Center timeline (E3 slice 2) — a read-side UNION ALL
# projecting audit_log + sync_history + llm_usage + agent_scope_snapshots
# into the SAME canonical row shape ``query()`` returns for audit_log alone,
# plus a literal ``trail`` column identifying which physical table the row
# came from. No schema change, no new table/view — this is a plain subquery
# built fresh on every call.
#
# Mapping per non-audit trail (mirrors ``audit_pg.py``'s ``_UNIFIED_UNION_SQL``
# — keep both in lockstep):
#   sync_history         synced_at->timestamp, NULL user_id (system actor),
#                         action='sync.table', resource='table:<table_id>',
#                         result=status, client_kind='scheduler' (source).
#   llm_usage             created_at->timestamp, user_id=owner, action='llm.call',
#                         resource='agent:<agent_id>', params carries model +
#                         token counts, client_kind='agent' (source).
#   agent_scope_snapshots created_at->timestamp, NULL user_id,
#                         action='agent.spawn.scope', resource='agent:<agent_id>',
#                         params carries session_id + effective_scope,
#                         client_kind='agent' (source).
#
# ``chat_messages`` is NEVER part of this union — privacy decision (see
# docs/observability.md), enforced simply by not being one of the four
# SELECTs below.
_UNIFIED_UNION_SQL = """
    SELECT id, timestamp, user_id, action, resource, params, result, duration_ms,
           params_before, client_ip, client_kind, correlation_id, 'audit' AS trail
    FROM audit_log

    UNION ALL

    SELECT id, synced_at AS timestamp, CAST(NULL AS VARCHAR) AS user_id,
           'sync.table' AS action, 'table:' || table_id AS resource,
           json_object('rows', rows, 'error', error) AS params,
           status AS result, duration_ms,
           CAST(NULL AS JSON) AS params_before, CAST(NULL AS VARCHAR) AS client_ip,
           'scheduler' AS client_kind, CAST(NULL AS VARCHAR) AS correlation_id,
           'sync' AS trail
    FROM sync_history

    UNION ALL

    SELECT id, created_at AS timestamp, user_id,
           'llm.call' AS action,
           CASE WHEN agent_id IS NOT NULL THEN 'agent:' || agent_id ELSE NULL END AS resource,
           json_object('model', model, 'session_id', session_id,
                       'input_tokens', input_tokens, 'output_tokens', output_tokens,
                       'cache_read_tokens', cache_read_tokens,
                       'cache_creation_tokens', cache_creation_tokens) AS params,
           CAST(NULL AS VARCHAR) AS result, CAST(NULL AS INTEGER) AS duration_ms,
           CAST(NULL AS JSON) AS params_before, CAST(NULL AS VARCHAR) AS client_ip,
           'agent' AS client_kind, CAST(NULL AS VARCHAR) AS correlation_id,
           'llm' AS trail
    FROM llm_usage

    UNION ALL

    SELECT id, created_at AS timestamp, CAST(NULL AS VARCHAR) AS user_id,
           'agent.spawn.scope' AS action,
           'agent:' || agent_id AS resource,
           json_object('session_id', session_id, 'effective_scope', effective_scope) AS params,
           CAST(NULL AS VARCHAR) AS result, CAST(NULL AS INTEGER) AS duration_ms,
           CAST(NULL AS JSON) AS params_before, CAST(NULL AS VARCHAR) AS client_ip,
           'agent' AS client_kind, CAST(NULL AS VARCHAR) AS correlation_id,
           'agent_scope' AS trail
    FROM agent_scope_snapshots
"""


class AuditRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def log(
        self,
        user_id: Optional[str] = None,
        action: str = "",
        resource: Optional[str] = None,
        params: Optional[dict] = None,
        result: Optional[str] = None,
        duration_ms: Optional[int] = None,
        *,
        params_before: Optional[dict] = None,
        client_ip: Optional[str] = None,
        client_kind: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> str:
        """Insert one audit_log row. Returns the new row id.

        The four kwargs after `*` are v40 additions; legacy callers using
        positional args or the original kwargs are unaffected. `params_before`
        is only used for mutating actions where rollback / diff is meaningful;
        leave None for reads, ticks, queries.

        `client_ip` / `correlation_id` / `client_kind` autofill from the
        `src.audit_context` contextvars (F0 — audit-full-coverage plan, Task
        1) when the caller passes `None` — an explicit kwarg always wins.
        Outside a request scope (scheduler internals, services with no
        request context) the autofill functions return `None` too, so
        behavior is unchanged for those callers.
        """
        entry_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        if duration_ms is None:
            duration_ms = auto_duration_ms()
        if client_ip is None:
            client_ip = auto_client_ip()
        if correlation_id is None:
            correlation_id = auto_correlation_id()
        if client_kind is None:
            client_kind = auto_client_kind()
        self.conn.execute(
            """INSERT INTO audit_log
               (id, timestamp, user_id, action, resource, params, result, duration_ms,
                params_before, client_ip, client_kind, correlation_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                entry_id,
                now,
                user_id,
                action,
                resource,
                json.dumps(params) if params else None,
                result,
                duration_ms,
                json.dumps(params_before) if params_before else None,
                client_ip,
                client_kind,
                correlation_id,
            ],
        )
        mark_audit_written()
        return entry_id

    # -----------------------------------------------------------------
    # shared filter surface — query/facets/kpis build the same WHERE from
    # the same kwargs so the Activity Center KPI cards, facet dropdowns and
    # timeline can never tell different stories for one filter state.
    # -----------------------------------------------------------------
    def _filters_where(
        self,
        *,
        since: "Optional[datetime]" = None,
        until: "Optional[datetime]" = None,
        user_id: "Optional[str]" = None,
        action: "Optional[str]" = None,
        action_prefix: "Optional[str]" = None,
        action_in: "Optional[List[str]]" = None,
        resource: "Optional[str]" = None,
        resource_prefix: "Optional[str]" = None,
        result_pattern: "Optional[str]" = None,
        result_class: "Optional[str]" = None,
        correlation_id: "Optional[str]" = None,
        q: "Optional[str]" = None,
        source: "Optional[str]" = None,
        include_self_reads: bool = True,
    ) -> "tuple[list[str], List[Any]]":
        where: list[str] = []
        params: List[Any] = []
        if since is not None:
            where.append("timestamp >= ?")
            params.append(since)
        if until is not None:
            where.append("timestamp < ?")
            params.append(until)
        if user_id is not None:
            where.append("user_id = ?")
            params.append(user_id)
        if action is not None:
            where.append("action = ?")
            params.append(action)
        if action_prefix is not None:
            where.append("action LIKE ?")
            params.append(action_prefix + "%")
        if action_in:
            placeholders = ",".join("?" for _ in action_in)
            where.append(f"action IN ({placeholders})")
            params.extend(action_in)
        if resource is not None:
            where.append("resource = ?")
            params.append(resource)
        if resource_prefix is not None:
            where.append("resource LIKE ?")
            params.append(resource_prefix + "%")
        if result_pattern is not None:
            where.append("result LIKE ?")
            params.append(result_pattern)
        if result_class is not None:
            where.append(f"{RESULT_CLASS_CASE_SQL} = ?")
            params.append(result_class)
        if correlation_id is not None:
            where.append("correlation_id = ?")
            params.append(correlation_id)
        if source is not None:
            where.append(f"{AUDIT_SOURCE_CASE_SQL} = ?")
            params.append(source)
        if not include_self_reads:
            # The Activity Center audits its own reads; by default its
            # queries hide that self-noise (decision in the 2026-07-28
            # consistency spec) — callers pass True to see everything.
            where.append("action != 'activity.read'")
        if q:
            # Full-text search is a table scan on `params` JSON cast to text.
            # Safeguard: if caller passes `q` without a `since` filter, force a
            # 7-day cap so we don't scan the entire audit_log. Proper FTS lands
            # in Phase B/C (see parent spec §5.5).
            if since is None:
                where.append("timestamp >= ?")
                params.append(datetime.now(timezone.utc) - timedelta(days=7))
            where.append("CAST(params AS VARCHAR) LIKE ?")
            params.append(f"%{q}%")
        return where, params

    def query(
        self,
        *,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        user_id: Optional[str] = None,
        action: Optional[str] = None,  # legacy single-action filter
        action_prefix: Optional[str] = None,
        action_in: Optional[List[str]] = None,
        resource: Optional[str] = None,
        resource_prefix: Optional[str] = None,
        result_pattern: Optional[str] = None,
        result_class: Optional[str] = None,
        correlation_id: Optional[str] = None,
        q: Optional[str] = None,
        source: Optional[str] = None,
        include_self_reads: bool = True,
        cursor: Optional[tuple] = None,  # keyset (timestamp, id)
        limit: int = 100,
    ) -> tuple[List[Dict[str, Any]], Optional[tuple]]:
        """Query audit_log with rich filters; returns (rows, next_cursor).

        Cursor encodes (timestamp, id) so pagination is stable under
        same-second writes. Pass the returned cursor back as `cursor=` for
        the next page. `None` cursor on input = newest page; `None` cursor
        in return = last page reached.
        """
        where, params = self._filters_where(
            since=since,
            until=until,
            user_id=user_id,
            action=action,
            action_prefix=action_prefix,
            action_in=action_in,
            resource=resource,
            resource_prefix=resource_prefix,
            result_pattern=result_pattern,
            result_class=result_class,
            correlation_id=correlation_id,
            q=q,
            source=source,
            include_self_reads=include_self_reads,
        )
        if cursor is not None:
            ts, cid = cursor
            # Keyset: rows strictly older than the cursor, breaking ties by id desc
            where.append("(timestamp, id) < (?, ?)")
            params.extend([ts, cid])

        # `source` is computed server-side so every consumer (web, CLI, MCP)
        # classifies rows identically — no client-side re-derivation.
        sql = f"SELECT *, {AUDIT_SOURCE_CASE_SQL} AS source FROM audit_log"
        if where:
            sql += " WHERE " + " AND ".join(where)
        # Fetch limit+1 to determine whether there's a next page
        sql += " ORDER BY timestamp DESC, id DESC LIMIT ?"
        params.append(limit + 1)
        rows = self.conn.execute(sql, params).fetchall()
        if not rows:
            return [], None
        columns = [desc[0] for desc in self.conn.description]
        out = [dict(zip(columns, r)) for r in rows]

        next_cursor: Optional[tuple] = None
        if len(out) > limit:
            last_shown = out[limit - 1]
            next_cursor = (last_shown["timestamp"], last_shown["id"])
            out = out[:limit]
        return out, next_cursor

    def query_unified(
        self,
        *,
        trail: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        user_id: Optional[str] = None,
        action: Optional[str] = None,
        action_prefix: Optional[str] = None,
        action_in: Optional[List[str]] = None,
        resource: Optional[str] = None,
        resource_prefix: Optional[str] = None,
        result_pattern: Optional[str] = None,
        result_class: Optional[str] = None,
        correlation_id: Optional[str] = None,
        q: Optional[str] = None,
        source: Optional[str] = None,
        include_self_reads: bool = True,
        cursor: Optional[tuple] = None,  # keyset (timestamp, id)
        limit: int = 100,
    ) -> tuple[List[Dict[str, Any]], Optional[tuple]]:
        """The Activity Center timeline, widened across every trail.

        Same filter surface, same cursor/ordering contract as :meth:`query`
        — the only difference is the row source: a UNION ALL over
        ``audit_log`` + ``sync_history`` + ``llm_usage`` +
        ``agent_scope_snapshots`` (see :data:`_UNIFIED_UNION_SQL`), each
        mapped into the canonical row shape plus a ``trail`` column. Reuses
        ``_filters_where`` unchanged — the projected columns share audit_log's
        names, so the same WHERE fragments (incl. the ``source``/
        ``result_class`` CASE expressions) apply transparently to the union.

        ``trail`` narrows to one physical trail (``"audit"``, ``"sync"``,
        ``"llm"``, ``"agent_scope"``) — e.g. so an admin can filter back to
        audit-only. Unknown values raise ``ValueError`` rather than silently
        returning zero rows.

        ``chat_messages`` is never part of this union (privacy decision, see
        docs/observability.md) — there is no branch for it above.
        """
        if trail is not None and trail not in UNIFIED_TRAILS:
            raise ValueError(f"trail must be one of {UNIFIED_TRAILS}, got {trail!r}")

        where, params = self._filters_where(
            since=since,
            until=until,
            user_id=user_id,
            action=action,
            action_prefix=action_prefix,
            action_in=action_in,
            resource=resource,
            resource_prefix=resource_prefix,
            result_pattern=result_pattern,
            result_class=result_class,
            correlation_id=correlation_id,
            q=q,
            source=source,
            include_self_reads=include_self_reads,
        )
        if trail is not None:
            where.append("trail = ?")
            params.append(trail)
        if cursor is not None:
            ts, cid = cursor
            where.append("(timestamp, id) < (?, ?)")
            params.extend([ts, cid])

        sql = f"SELECT *, {AUDIT_SOURCE_CASE_SQL} AS source FROM ({_UNIFIED_UNION_SQL}) unified"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY timestamp DESC, id DESC LIMIT ?"
        params.append(limit + 1)
        rows = self.conn.execute(sql, params).fetchall()
        if not rows:
            return [], None
        columns = [desc[0] for desc in self.conn.description]
        out = [dict(zip(columns, r)) for r in rows]

        next_cursor: Optional[tuple] = None
        if len(out) > limit:
            last_shown = out[limit - 1]
            next_cursor = (last_shown["timestamp"], last_shown["id"])
            out = out[:limit]
        return out, next_cursor

    def query_actions(
        self,
        actions: List[str],
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Return rows whose action is in the given list, newest first."""
        if not actions:
            return []
        placeholders = ",".join("?" for _ in actions)
        sql = f"SELECT * FROM audit_log WHERE action IN ({placeholders}) ORDER BY timestamp DESC LIMIT ?"
        results = self.conn.execute(sql, list(actions) + [limit]).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def query_for_resources(
        self,
        resources: List[str],
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Activity timeline for one or more resource refs.

        Each ``resources`` entry is a full ``resource`` value (e.g.
        ``"store_submission:abc123"``, ``"store_entity:def456"``). Used
        by the submission-detail page to render *"when did each rescan /
        override / approval happen, and who did it"* — proves that the
        latest verdict on the row is fresh and not a stale render.
        """
        if not resources:
            return []
        placeholders = ",".join("?" for _ in resources)
        sql = f"SELECT * FROM audit_log WHERE resource IN ({placeholders}) ORDER BY timestamp DESC LIMIT ?"
        results = self.conn.execute(sql, list(resources) + [limit]).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        rows: List[Dict[str, Any]] = []
        for row in results:
            d = dict(zip(columns, row))
            v = d.get("params")
            if isinstance(v, str):
                try:
                    d["params"] = json.loads(v) if v else None
                except (ValueError, TypeError):
                    pass
            rows.append(d)
        return rows

    # -----------------------------------------------------------------
    # aggregates — counts, governance feed, observability facets/KPIs
    # -----------------------------------------------------------------
    def count_for_user(self, user_id: str) -> int:
        """Total audit rows recorded for one user."""
        row = self.conn.execute("SELECT COUNT(*) FROM audit_log WHERE user_id = ?", [user_id]).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def query_governance(
        self,
        *,
        action: Optional[str] = None,
        prefixes: tuple = ("corporate_memory.", "km_"),
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Governance audit feed: ``corporate_memory.*`` + legacy ``km_*`` rows.

        When ``action`` is given, match it exactly across both prefixes
        (``prefix0+action``, ``prefix1+action``); otherwise match every row
        whose action starts with either prefix. Newest first, paged by
        LIMIT/OFFSET.
        """
        p0, p1 = prefixes
        if action:
            sql = "SELECT * FROM audit_log WHERE action IN (?, ?) ORDER BY timestamp DESC, id DESC LIMIT ? OFFSET ?"
            params: List[Any] = [f"{p0}{action}", f"{p1}{action}", limit, offset]
        else:
            sql = (
                "SELECT * FROM audit_log "
                "WHERE action LIKE ? OR action LIKE ? "
                "ORDER BY timestamp DESC, id DESC LIMIT ? OFFSET ?"
            )
            params = [f"{p0}%", f"{p1}%", limit, offset]
        results = self.conn.execute(sql, params).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def facets(
        self,
        *,
        since: datetime,
        limit: int = 50,
        trail: Optional[str] = None,
        **filters: "Any",
    ) -> "dict[str, list[dict]]":
        """Distinct facet values present across the unified timeline
        (audit_log + sync_history + llm_usage + agent_scope_snapshots)
        since ``since``.

        Buckets: users, actions, results, result_classes, resources,
        sources — largest-first, capped at ``limit``. Accepts the same
        filter kwargs as :meth:`query_unified` (via ``_filters_where``), plus
        ``trail`` to narrow to one physical trail, so the dropdown counts
        always describe the same row set the (now-unified) timeline shows —
        the whole page tells one story. Source classification uses the
        shared ``AUDIT_SOURCE_CASE_SQL`` rule — no caller-supplied action
        list.
        """
        if trail is not None and trail not in UNIFIED_TRAILS:
            raise ValueError(f"trail must be one of {UNIFIED_TRAILS}, got {trail!r}")

        where, params = self._filters_where(since=since, **filters)
        if trail is not None:
            where.append("trail = ?")
            params.append(trail)
        w = ("WHERE " + " AND ".join(where)) if where else ""

        def _bucket(select: str, extra: str = "", group: str = "1") -> list:
            clause = w + (f" AND {extra}" if (w and extra) else (f"WHERE {extra}" if extra else ""))
            return self.conn.execute(
                f"SELECT {select}, COUNT(*) AS n FROM ({_UNIFIED_UNION_SQL}) unified {clause} "
                f"GROUP BY {group} ORDER BY n DESC LIMIT ?",
                params + [limit],
            ).fetchall()

        users = _bucket("user_id AS id", "user_id IS NOT NULL")
        actions = _bucket("action AS label", "action IS NOT NULL")
        results = _bucket("COALESCE(result, '—') AS label", group="result")
        result_classes = _bucket(f"{RESULT_CLASS_CASE_SQL} AS label")
        resources = _bucket("resource AS label", "resource IS NOT NULL")
        source_rows = _bucket(f"{AUDIT_SOURCE_CASE_SQL} AS src")
        return {
            "users": [{"id": r[0], "count": r[1]} for r in users],
            "actions": [{"value": r[0], "count": r[1]} for r in actions],
            "results": [{"value": r[0], "count": r[1]} for r in results],
            "result_classes": [{"value": r[0], "count": r[1]} for r in result_classes],
            "resources": [{"value": r[0], "count": r[1]} for r in resources],
            "sources": [{"value": r[0], "count": r[1]} for r in source_rows],
        }

    def prune_older_than(self, days: int) -> int:
        """Delete ``audit_log`` rows older than ``days``. Returns the
        deleted-row count.

        The caller (``src/audit_retention.py``) owns the "0/negative days =
        keep forever, skip entirely" short-circuit — this method always
        executes the DELETE it's given, no matter the value. Reads the
        count off ``fetchone()`` rather than ``res.rowcount`` — DuckDB's
        DBAPI ``rowcount`` is always ``-1`` for DML, but a DELETE's result
        set carries a single ``Count`` column with the real number."""
        res = self.conn.execute(
            f"DELETE FROM audit_log WHERE timestamp < (current_timestamp - INTERVAL '{int(days)} days')"
        )
        row = res.fetchone()
        return int(row[0]) if row else 0

    def last_scheduler_tick(self) -> "datetime | None":
        """Most recent ``run_%`` or ``marketplace.sync_all`` audit row
        timestamp, or ``None`` if the scheduler has never fired. Backs the
        Activity Center health pulse's "scheduler" freshness field
        (``app/api/activity.py`` ``_compute_health``)."""
        row = self.conn.execute(f"SELECT MAX(timestamp) FROM audit_log WHERE {SCHEDULER_ACTION_SQL}").fetchone()
        return row[0] if row else None

    def upload_filenames_since(self, since: datetime) -> "list[str]":
        """Distinct ``filename`` values from ``session.upload`` audit rows at/
        after *since*. Parsed in Python (portable across engines). Backs the
        health pulse's session-ingest reconciliation — joined against
        ``usage_session_summary.session_file`` BASENAMES, never session_id
        (resumed/forked sessions carry a different content-derived id)."""
        rows = self.conn.execute(
            "SELECT params FROM audit_log WHERE action = 'session.upload' AND timestamp >= ?",
            [since],
        ).fetchall()
        out: set[str] = set()
        for (p,) in rows:
            try:
                d = json.loads(p) if isinstance(p, str) else (p or {})
            except (TypeError, ValueError):
                continue
            fn = (d or {}).get("filename")
            if fn:
                out.add(fn)
        return sorted(out)

    def active_users_since(self, since: datetime) -> int:
        """Distinct ``user_id`` count over audit rows at/after *since*, NULL
        user_ids excluded. Backs the Activity Center health pulse's
        "active_users_today" field."""
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT user_id) FROM audit_log WHERE timestamp >= ? AND user_id IS NOT NULL",
            [since],
        ).fetchone()
        return int(row[0] or 0) if row else 0

    def kpis(self, *, since: datetime, trail: Optional[str] = None, **filters: "Any") -> "dict[str, Any]":
        """Headline KPIs over the window, across the unified timeline
        (audit_log + sync_history + llm_usage + agent_scope_snapshots):
        events, active users, errors, p95, duration coverage. Accepts the
        same filter kwargs as :meth:`query_unified`, plus ``trail`` to
        narrow to one physical trail, so the KPI cards always agree with the
        (now-unified) timeline.

        ``active_users`` counts people — rows whose computed source is
        ``scheduler``/``system`` are excluded from the distinct-user count
        (the events total still includes them). ``errors`` counts
        ``result_class = 'error'`` (denied/blocked are their own class).
        ``p95`` uses DuckDB's ``approx_quantile`` (the PG sibling uses an
        exact ``percentile_cont``; results may differ within tolerance).
        """
        if trail is not None and trail not in UNIFIED_TRAILS:
            raise ValueError(f"trail must be one of {UNIFIED_TRAILS}, got {trail!r}")

        where, params = self._filters_where(since=since, **filters)
        if trail is not None:
            where.append("trail = ?")
            params.append(trail)
        w = ("WHERE " + " AND ".join(where)) if where else ""
        row = self.conn.execute(
            f"""
            SELECT
              COUNT(*) AS events_total,
              COUNT(DISTINCT user_id) FILTER (
                WHERE user_id IS NOT NULL
                  AND {AUDIT_SOURCE_CASE_SQL} NOT IN ('scheduler', 'system')
              ) AS active_users,
              COUNT(*) FILTER (WHERE {RESULT_CLASS_CASE_SQL} = 'error') AS errors,
              CAST(approx_quantile(duration_ms, 0.95) AS INTEGER) AS p95,
              COUNT(duration_ms) AS measured,
              COUNT(*) AS total
            FROM ({_UNIFIED_UNION_SQL}) unified {w}
            """,
            params,
        ).fetchone()
        if row is None:
            return {
                "events_total": 0,
                "active_users": 0,
                "errors": 0,
                "p95": None,
                "duration_coverage": 0.0,
            }
        total = int(row[5] or 0)
        return {
            "events_total": int(row[0] or 0),
            "active_users": int(row[1] or 0),
            "errors": int(row[2] or 0),
            "p95": int(row[3]) if row[3] is not None else None,
            "duration_coverage": round((int(row[4] or 0) / total), 4) if total else 0.0,
        }
