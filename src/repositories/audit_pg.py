"""Postgres-backed audit repository.

Mirrors ``src/repositories/audit.py`` (the DuckDB impl) on the
``AuditRepositoryProtocol`` surface. Both must return identical results
for identical inputs; ``tests/db_pg/test_audit_contract.py`` parametrises
across both and fails on any drift.

Implementation differences vs. DuckDB:
  - JSON columns use psycopg's native JSONB adapter — params go in as
    dicts, come out as dicts. No json.dumps in the write path, no
    json.loads in the read path.
  - Keyset pagination uses the standard PG ``(timestamp, id) < (?, ?)``
    row-comparator (DuckDB supports the same syntax; this is a parity
    win, not a divergence).
  - Full-text ``q`` filter is a casts-to-text LIKE — for the future
    PG-specific FTS upgrade, see the "future improvements" section in
    docs/migrations.md.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from src.audit_context import auto_duration_ms
from src.audit_helpers import (
    AUDIT_SOURCE_CASE_SQL,
    RESULT_CLASS_CASE_SQL,
    SCHEDULER_ACTION_SQL,
    UNIFIED_TRAILS,
)

# ---------------------------------------------------------------------------
# Unified Activity Center timeline (E3 slice 2) — Postgres mirror of
# ``src/repositories/audit.py``'s ``_UNIFIED_UNION_SQL``. Same four branches,
# same canonical column set + literal ``trail``; the only dialect deltas are
# ``jsonb_build_object`` in place of DuckDB's ``json_object`` and explicit
# ``NULL::type`` casts in place of ``CAST(NULL AS type)``. Keep both in
# lockstep — ``tests/db_pg/test_audit_contract.py`` fails on any drift.
#
# ``chat_messages`` is NEVER part of this union (privacy decision) — there is
# no branch for it below, on either backend.
_UNIFIED_UNION_SQL = """
    SELECT id, timestamp, user_id, action, resource, params, result, duration_ms,
           params_before, client_ip, client_kind, correlation_id, 'audit' AS trail
    FROM audit_log

    UNION ALL

    SELECT id, synced_at AS timestamp, NULL::text AS user_id,
           'sync.table' AS action, 'table:' || table_id AS resource,
           jsonb_build_object('rows', rows, 'error', error) AS params,
           status AS result, duration_ms,
           NULL::jsonb AS params_before, NULL::text AS client_ip,
           'scheduler' AS client_kind, NULL::text AS correlation_id,
           'sync' AS trail
    FROM sync_history

    UNION ALL

    SELECT id, created_at AS timestamp, user_id,
           'llm.call' AS action,
           CASE WHEN agent_id IS NOT NULL THEN 'agent:' || agent_id ELSE NULL END AS resource,
           jsonb_build_object('model', model, 'session_id', session_id,
                              'input_tokens', input_tokens, 'output_tokens', output_tokens,
                              'cache_read_tokens', cache_read_tokens,
                              'cache_creation_tokens', cache_creation_tokens) AS params,
           NULL::text AS result, NULL::integer AS duration_ms,
           NULL::jsonb AS params_before, NULL::text AS client_ip,
           'agent' AS client_kind, NULL::text AS correlation_id,
           'llm' AS trail
    FROM llm_usage

    UNION ALL

    SELECT id, created_at AS timestamp, NULL::text AS user_id,
           'agent.spawn.scope' AS action,
           'agent:' || agent_id AS resource,
           jsonb_build_object('session_id', session_id, 'effective_scope', effective_scope) AS params,
           NULL::text AS result, NULL::integer AS duration_ms,
           NULL::jsonb AS params_before, NULL::text AS client_ip,
           'agent' AS client_kind, NULL::text AS correlation_id,
           'agent_scope' AS trail
    FROM agent_scope_snapshots
"""


class AuditPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    # -----------------------------------------------------------------
    # write
    # -----------------------------------------------------------------
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
        if duration_ms is None:
            duration_ms = auto_duration_ms()
        entry_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO audit_log
                      (id, timestamp, user_id, action, resource, params,
                       result, duration_ms, params_before, client_ip,
                       client_kind, correlation_id)
                    VALUES
                      (:id, :ts, :user_id, :action, :resource,
                       CAST(:params AS JSONB),
                       :result, :duration_ms,
                       CAST(:params_before AS JSONB),
                       :client_ip, :client_kind, :correlation_id)
                    """
                ),
                {
                    "id": entry_id,
                    "ts": now,
                    "user_id": user_id,
                    "action": action,
                    "resource": resource,
                    "params": _json_param(params),
                    "result": result,
                    "duration_ms": duration_ms,
                    "params_before": _json_param(params_before),
                    "client_ip": client_ip,
                    "client_kind": client_kind,
                    "correlation_id": correlation_id,
                },
            )
        return entry_id

    # -----------------------------------------------------------------
    # shared filter surface — query/facets/kpis build the same WHERE from
    # the same kwargs (named-param mirror of the DuckDB sibling's
    # _filters_where) so KPI cards, facets and timeline always agree.
    # -----------------------------------------------------------------
    def _filters_where(
        self,
        *,
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
    ) -> Tuple[List[str], Dict[str, Any]]:
        where: List[str] = []
        params: Dict[str, Any] = {}
        if since is not None:
            where.append("timestamp >= :since")
            params["since"] = since
        if until is not None:
            where.append("timestamp < :until")
            params["until"] = until
        if user_id is not None:
            where.append("user_id = :user_id")
            params["user_id"] = user_id
        if action is not None:
            where.append("action = :action_eq")
            params["action_eq"] = action
        if action_prefix is not None:
            where.append("action LIKE :action_prefix")
            params["action_prefix"] = action_prefix + "%"
        if action_in:
            in_keys: List[str] = []
            for i, a in enumerate(action_in):
                k = f"action_in_{i}"
                in_keys.append(f":{k}")
                params[k] = a
            where.append(f"action IN ({','.join(in_keys)})")
        if resource is not None:
            where.append("resource = :resource_eq")
            params["resource_eq"] = resource
        if resource_prefix is not None:
            where.append("resource LIKE :resource_prefix")
            params["resource_prefix"] = resource_prefix + "%"
        if result_pattern is not None:
            where.append("result LIKE :result_pattern")
            params["result_pattern"] = result_pattern
        if result_class is not None:
            where.append(f"{RESULT_CLASS_CASE_SQL} = :result_class")
            params["result_class"] = result_class
        if correlation_id is not None:
            where.append("correlation_id = :correlation_id")
            params["correlation_id"] = correlation_id
        if source is not None:
            where.append(f"{AUDIT_SOURCE_CASE_SQL} = :source_filter")
            params["source_filter"] = source
        if not include_self_reads:
            # Mirror of the DuckDB sibling: the Activity Center hides its
            # own read-audit noise by default (2026-07-28 consistency spec).
            where.append("action != 'activity.read'")
        if q:
            # Free-text scan over the JSON params blob. Mirror the DuckDB
            # impl's 7-day cap when caller hasn't passed `since`.
            if since is None:
                where.append("timestamp >= :since")
                params["since"] = datetime.now(timezone.utc) - timedelta(days=7)
            where.append("CAST(params AS TEXT) LIKE :q")
            params["q"] = f"%{q}%"
        return where, params

    # -----------------------------------------------------------------
    # read — filtered query with cursor pagination
    # -----------------------------------------------------------------
    def query(
        self,
        *,
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
        cursor: Optional[Tuple[datetime, str]] = None,
        limit: int = 100,
    ) -> Tuple[List[Dict[str, Any]], Optional[Tuple[datetime, str]]]:
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
            where.append("(timestamp, id) < (:cursor_ts, :cursor_id)")
            params["cursor_ts"] = ts
            params["cursor_id"] = cid

        # `source` is computed server-side (same rule as the DuckDB sibling)
        # so every consumer classifies rows identically.
        sql = f"SELECT *, {AUDIT_SOURCE_CASE_SQL} AS source FROM audit_log"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY timestamp DESC, id DESC LIMIT :limit_plus_one"
        params["limit_plus_one"] = limit + 1

        with self._engine.connect() as conn:
            result = conn.execute(sa.text(sql), params)
            rows = [dict(r._mapping) for r in result]

        if not rows:
            return [], None

        next_cursor: Optional[Tuple[datetime, str]] = None
        if len(rows) > limit:
            last_shown = rows[limit - 1]
            next_cursor = (last_shown["timestamp"], last_shown["id"])
            rows = rows[:limit]
        return rows, next_cursor

    # -----------------------------------------------------------------
    # read — unified Activity Center timeline (E3 slice 2)
    # -----------------------------------------------------------------
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
        cursor: Optional[Tuple[datetime, str]] = None,
        limit: int = 100,
    ) -> Tuple[List[Dict[str, Any]], Optional[Tuple[datetime, str]]]:
        """Mirror of ``AuditRepository.query_unified`` — same filter surface,
        same cursor/ordering contract, over the Postgres ``_UNIFIED_UNION_SQL``.
        See the DuckDB sibling's docstring for the full trail mapping."""
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
            where.append("trail = :trail")
            params["trail"] = trail
        if cursor is not None:
            ts, cid = cursor
            where.append("(timestamp, id) < (:cursor_ts, :cursor_id)")
            params["cursor_ts"] = ts
            params["cursor_id"] = cid

        sql = f"SELECT *, {AUDIT_SOURCE_CASE_SQL} AS source FROM ({_UNIFIED_UNION_SQL}) unified"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY timestamp DESC, id DESC LIMIT :limit_plus_one"
        params["limit_plus_one"] = limit + 1

        with self._engine.connect() as conn:
            result = conn.execute(sa.text(sql), params)
            rows = [dict(r._mapping) for r in result]

        if not rows:
            return [], None

        next_cursor: Optional[Tuple[datetime, str]] = None
        if len(rows) > limit:
            last_shown = rows[limit - 1]
            next_cursor = (last_shown["timestamp"], last_shown["id"])
            rows = rows[:limit]
        return rows, next_cursor

    # -----------------------------------------------------------------
    # helpers
    # -----------------------------------------------------------------
    def query_actions(
        self,
        actions: List[str],
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        if not actions:
            return []
        in_keys: List[str] = []
        params: Dict[str, Any] = {"limit": limit}
        for i, a in enumerate(actions):
            k = f"action_{i}"
            in_keys.append(f":{k}")
            params[k] = a
        sql = f"SELECT * FROM audit_log WHERE action IN ({','.join(in_keys)}) ORDER BY timestamp DESC LIMIT :limit"
        with self._engine.connect() as conn:
            result = conn.execute(sa.text(sql), params)
            return [dict(r._mapping) for r in result]

    def query_for_resources(
        self,
        resources: List[str],
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        if not resources:
            return []
        in_keys: List[str] = []
        params: Dict[str, Any] = {"limit": limit}
        for i, r in enumerate(resources):
            k = f"resource_{i}"
            in_keys.append(f":{k}")
            params[k] = r
        sql = f"SELECT * FROM audit_log WHERE resource IN ({','.join(in_keys)}) ORDER BY timestamp DESC LIMIT :limit"
        with self._engine.connect() as conn:
            result = conn.execute(sa.text(sql), params)
            return [dict(r._mapping) for r in result]

    # -----------------------------------------------------------------
    # aggregates — counts, governance feed, observability facets/KPIs
    # -----------------------------------------------------------------
    def count_for_user(self, user_id: str) -> int:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT COUNT(*) FROM audit_log WHERE user_id = :user_id"),
                {"user_id": user_id},
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def query_governance(
        self,
        *,
        action: Optional[str] = None,
        prefixes: Tuple[str, ...] = ("corporate_memory.", "km_"),
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        p0, p1 = prefixes
        if action:
            sql = (
                "SELECT * FROM audit_log WHERE action IN (:a0, :a1) "
                "ORDER BY timestamp DESC, id DESC LIMIT :limit OFFSET :offset"
            )
            params: Dict[str, Any] = {
                "a0": f"{p0}{action}",
                "a1": f"{p1}{action}",
                "limit": limit,
                "offset": offset,
            }
        else:
            sql = (
                "SELECT * FROM audit_log "
                "WHERE action LIKE :p0 OR action LIKE :p1 "
                "ORDER BY timestamp DESC, id DESC LIMIT :limit OFFSET :offset"
            )
            params = {
                "p0": f"{p0}%",
                "p1": f"{p1}%",
                "limit": limit,
                "offset": offset,
            }
        with self._engine.connect() as conn:
            result = conn.execute(sa.text(sql), params)
            return [dict(r._mapping) for r in result]

    def facets(
        self,
        *,
        since: datetime,
        limit: int = 50,
        **filters: Any,
    ) -> "dict[str, list[dict]]":
        """Mirror of the DuckDB sibling: filter-aware facet buckets
        (users/actions/results/result_classes/resources/sources)."""
        where, params = self._filters_where(since=since, **filters)
        w = ("WHERE " + " AND ".join(where)) if where else ""
        out: dict = {}
        with self._engine.connect() as conn:

            def _bucket(select: str, extra: str = "", group: str = "1") -> list:
                clause = w + (f" AND {extra}" if (w and extra) else (f"WHERE {extra}" if extra else ""))
                return conn.execute(
                    sa.text(
                        f"SELECT {select}, COUNT(*) AS n FROM audit_log {clause} "
                        f"GROUP BY {group} ORDER BY n DESC LIMIT :facet_limit"
                    ),
                    {**params, "facet_limit": limit},
                ).fetchall()

            users = _bucket("user_id AS id", "user_id IS NOT NULL")
            actions = _bucket("action AS label", "action IS NOT NULL")
            results = _bucket("COALESCE(result, '—') AS label", group="result")
            result_classes = _bucket(f"{RESULT_CLASS_CASE_SQL} AS label")
            resources = _bucket("resource AS label", "resource IS NOT NULL")
            source_rows = _bucket(f"{AUDIT_SOURCE_CASE_SQL} AS src")
        out = {
            "users": [{"id": r[0], "count": r[1]} for r in users],
            "actions": [{"value": r[0], "count": r[1]} for r in actions],
            "results": [{"value": r[0], "count": r[1]} for r in results],
            "result_classes": [{"value": r[0], "count": r[1]} for r in result_classes],
            "resources": [{"value": r[0], "count": r[1]} for r in resources],
            "sources": [{"value": r[0], "count": r[1]} for r in source_rows],
        }
        return out

    def prune_older_than(self, days: int) -> int:
        """Mirrors ``AuditRepository.prune_older_than``."""
        with self._engine.begin() as conn:
            res = conn.execute(
                sa.text(f"DELETE FROM audit_log WHERE timestamp < (CURRENT_TIMESTAMP - INTERVAL '{int(days)} days')")
            )
            return int(getattr(res, "rowcount", 0) or 0)

    def last_scheduler_tick(self) -> "datetime | None":
        """Mirrors ``AuditRepository.last_scheduler_tick``."""
        with self._engine.connect() as conn:
            row = conn.execute(sa.text(f"SELECT MAX(timestamp) FROM audit_log WHERE {SCHEDULER_ACTION_SQL}")).first()
        return row[0] if row else None

    def upload_filenames_since(self, since: datetime) -> "list[str]":
        """Mirror of ``AuditRepository.upload_filenames_since``."""
        import json as _json

        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT params FROM audit_log WHERE action = 'session.upload' AND timestamp >= :since"),
                {"since": since},
            ).fetchall()
        out: set[str] = set()
        for (p,) in rows:
            d = p if isinstance(p, dict) else None
            if d is None:
                try:
                    d = _json.loads(p) if p else {}
                except (TypeError, ValueError):
                    continue
            fn = (d or {}).get("filename")
            if fn:
                out.add(fn)
        return sorted(out)

    def active_users_since(self, since: datetime) -> int:
        """Mirrors ``AuditRepository.active_users_since``."""
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT COUNT(DISTINCT user_id) FROM audit_log WHERE timestamp >= :since AND user_id IS NOT NULL"
                ),
                {"since": since},
            ).first()
        return int(row[0] or 0) if row else 0

    def kpis(self, *, since: datetime, **filters: Any) -> "dict[str, Any]":
        """Mirror of the DuckDB sibling — same filter kwargs, same output
        keys. ``p95`` uses Postgres' exact ``percentile_cont`` (DuckDB uses
        ``approx_quantile``; results may differ within tolerance).
        ``active_users`` counts people (source ∉ scheduler/system);
        ``errors`` counts ``result_class = 'error'``."""
        where, params = self._filters_where(since=since, **filters)
        w = ("WHERE " + " AND ".join(where)) if where else ""
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    f"""
                    SELECT
                      COUNT(*) AS events_total,
                      COUNT(DISTINCT user_id) FILTER (
                        WHERE user_id IS NOT NULL
                          AND {AUDIT_SOURCE_CASE_SQL} NOT IN ('scheduler', 'system')
                      ) AS active_users,
                      COUNT(*) FILTER (WHERE {RESULT_CLASS_CASE_SQL} = 'error') AS errors,
                      CAST(percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS INTEGER) AS p95,
                      COUNT(duration_ms) AS measured,
                      COUNT(*) AS total
                    FROM audit_log {w}
                    """
                ),
                params,
            ).first()
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


def _json_param(v: Optional[dict]) -> Optional[str]:
    """Serialize dict to JSON text for the ``CAST(:p AS JSONB)`` bind.

    Passing ``None`` through unchanged so the DB stores SQL NULL, not the
    JSON null literal.
    """
    if v is None:
        return None
    import json

    return json.dumps(v)
