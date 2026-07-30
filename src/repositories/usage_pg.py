"""Postgres-backed usage repository.

Mirrors ``src/repositories/usage.py``. ``INSERT OR IGNORE`` becomes
``ON CONFLICT DO NOTHING``; ``INSERT OR REPLACE`` becomes
``ON CONFLICT (...) DO UPDATE SET ...``.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from services.session_processors.usage_lib import (
    _MARKETPLACE_30D_TRACKER,
    WINDOW_30D_REFRESH_SECONDS,
    _aggregate_events,
)
from src.repositories.usage import _slow_actions_from_raw


_EVENT_COLS = [
    "id",
    "session_id",
    "session_file",
    "username",
    "event_uuid",
    "parent_uuid",
    "event_type",
    "tool_name",
    "skill_name",
    "subagent_type",
    "command_name",
    "is_error",
    "source",
    "ref_id",
    "model",
    "cwd",
    "occurred_at",
    "processor_version",
    "user_id",
]

# Group-by buckets for /telemetry/query — PG dialect.
# 'day' pins AT TIME ZONE 'UTC': occurred_at is timestamptz, and a bare
# CAST(ts AS DATE) buckets in the session TimeZone — day labels would drift
# on a non-UTC server. DuckDB needs no pin (session TZ pinned to UTC in
# src/duckdb_conn.py).
_GROUP_BY_COLUMNS = {
    "day": ("CAST((occurred_at AT TIME ZONE 'UTC') AS DATE)", "day"),
    "username": ("username", "username"),
    "tool_name": ("tool_name", "tool_name"),
    "source": ("source", "source"),
    "ref_id": ("ref_id", "ref_id"),
}

_SESSION_SORT_KEYS = {
    "started_at": "started_at",
    "uploaded_at": "uploaded_at",
    "ended_at": "ended_at",
    "tool_calls": "tool_calls",
    "tool_errors": "tool_errors",
    "active_seconds": "active_seconds",
    "username": "username",
    "primary_model": "primary_model",
}

_SESSION_COLS = [
    "session_file",
    "session_id",
    "username",
    "started_at",
    "ended_at",
    "uploaded_at",
    "active_seconds",
    "wall_seconds",
    "user_messages",
    "assistant_messages",
    "tool_calls",
    "tool_errors",
    "skill_invocations",
    "subagent_dispatches",
    "mcp_calls",
    "slash_commands",
    "distinct_tools",
    "distinct_skills",
    "primary_model",
]


class UsagePgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    # ------------------------------------------------------------------
    # telemetry aggregate reads (Postgres).  Mirrors UsageRepository.
    # ------------------------------------------------------------------

    @staticmethod
    def _events_where(filters: dict) -> tuple[str, dict]:
        where = ["occurred_at >= :since"]
        params: dict = {"since": filters["since"]}
        if filters.get("username"):
            where.append("username = :username")
            params["username"] = filters["username"]
        if filters.get("tool_name"):
            where.append("tool_name = :tool_name")
            params["tool_name"] = filters["tool_name"]
        if filters.get("source"):
            where.append("source = :source")
            params["source"] = filters["source"]
        if filters.get("event_type"):
            where.append("event_type = :event_type")
            params["event_type"] = filters["event_type"]
        if filters.get("only_errors"):
            where.append("is_error = TRUE")
        if filters.get("q"):
            where.append("(tool_name LIKE :q OR skill_name LIKE :q OR subagent_type LIKE :q OR command_name LIKE :q)")
            params["q"] = f"%{filters['q']}%"
        return " AND ".join(where), params

    def summary_top_tools(self, cutoff: datetime, limit: int = 10) -> List[dict]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """SELECT tool_name, source, COUNT(*) AS n
                       FROM usage_events
                       WHERE occurred_at >= :cutoff AND tool_name IS NOT NULL
                       GROUP BY tool_name, source ORDER BY n DESC LIMIT :lim"""
                ),
                {"cutoff": cutoff, "lim": limit},
            ).fetchall()
        return [{"tool_name": r[0], "source": r[1], "invocations": int(r[2])} for r in rows]

    def summary_top_users(self, cutoff: datetime, limit: int = 10) -> List[dict]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """SELECT username, COUNT(*) AS n FROM usage_events
                       WHERE occurred_at >= :cutoff
                       GROUP BY username ORDER BY n DESC LIMIT :lim"""
                ),
                {"cutoff": cutoff, "lim": limit},
            ).fetchall()
        return [{"username": r[0], "tool_calls": int(r[1])} for r in rows]

    def summary_error_rate(self, cutoff: datetime, limit: int = 10) -> List[dict]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """SELECT tool_name, COUNT(*) AS n,
                              SUM(CASE WHEN is_error THEN 1 ELSE 0 END) AS err
                       FROM usage_events
                       WHERE occurred_at >= :cutoff AND tool_name IS NOT NULL
                       GROUP BY tool_name HAVING COUNT(*) > 0
                       ORDER BY n DESC LIMIT :lim"""
                ),
                {"cutoff": cutoff, "lim": limit},
            ).fetchall()
        return [
            {
                "tool_name": r[0],
                "invocations": int(r[1]),
                "errors": int(r[2]),
                "rate": float(r[2]) / float(r[1]) if r[1] else 0.0,
            }
            for r in rows
        ]

    def summary_dau(self, start_date: date) -> Dict[date, int]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """SELECT CAST((occurred_at AT TIME ZONE 'UTC') AS DATE) AS day,
                              COUNT(DISTINCT username) AS n
                       FROM usage_events
                       WHERE CAST((occurred_at AT TIME ZONE 'UTC') AS DATE) >= :start
                       GROUP BY day ORDER BY day"""
                ),
                {"start": start_date},
            ).fetchall()
        return {r[0]: int(r[1]) for r in rows}

    def summary_slow_actions(self, cutoff: datetime, limit: int = 10) -> List[dict]:
        # PG has no approx_quantile; pull raw durations and reuse the shared
        # Python percentile helper so DuckDB and PG return identical shapes.
        with self._engine.connect() as conn:
            raw = conn.execute(
                sa.text(
                    """SELECT action, duration_ms FROM audit_log
                       WHERE timestamp >= :cutoff
                         AND duration_ms IS NOT NULL AND duration_ms > 0"""
                ),
                {"cutoff": cutoff},
            ).fetchall()
        return _slow_actions_from_raw([(r[0], r[1]) for r in raw], limit)

    def summary_query_telemetry(self, cutoff: datetime, limit: int = 10) -> dict:
        """On-demand aggregation over the query-telemetry audit rows (#410).

        PG mirror of UsageRepository.summary_query_telemetry — same output
        shape. ``resource`` is ``table:<id>`` or ``table:<id>:as:<snapshot>``;
        ``scan_bytes`` comes from the JSONB ``params->>'bytes_scanned'``.
        Like the DuckDB sibling, rows are joined against ``table_registry``
        (``registered``), split by outcome (``failed``), and ranked by
        successful queries.
        """
        # ``table:<id>[:as:<name>]`` → <id>: strip the 6-char "table:" prefix,
        # then cut any ":as:" suffix. PG substring is 1-indexed (from 7).
        table_id_expr = "split_part(substring(resource from 7), ':as:', 1)"
        # JSONB text extraction → numeric. NULLIF guards empty strings.
        bytes_expr = "NULLIF(params->>'bytes_scanned', '')::bigint"
        # `result` is 'success' or an 'error.<code>' string; NULL on rows
        # written before the column existed, which count as neither.
        failed_expr = "SUM(CASE WHEN result LIKE 'error%' THEN 1 ELSE 0 END)"

        with self._engine.connect() as conn:
            top_rows = conn.execute(
                sa.text(
                    f"""SELECT agg.table_id,
                               agg.queries,
                               agg.failed,
                               agg.scan_bytes,
                               agg.remote,
                               agg.local,
                               (reg.id IS NOT NULL) AS registered
                        FROM (
                            SELECT {table_id_expr} AS table_id,
                                   COUNT(*) AS queries,
                                   {failed_expr} AS failed,
                                   COALESCE(SUM({bytes_expr}), 0) AS scan_bytes,
                                   SUM(CASE WHEN action = 'query.remote' THEN 1 ELSE 0 END) AS remote,
                                   SUM(CASE WHEN action = 'query.local'  THEN 1 ELSE 0 END) AS local
                            FROM audit_log
                            WHERE timestamp >= :cutoff
                              AND action IN ('query.remote', 'query.local', 'snapshot.create')
                              AND resource LIKE 'table:%'
                            GROUP BY {table_id_expr}
                        ) agg
                        LEFT JOIN table_registry reg ON reg.id = agg.table_id
                        ORDER BY (agg.queries - agg.failed) DESC,
                                 agg.queries DESC,
                                 agg.scan_bytes DESC
                        LIMIT :lim"""
                ),
                {"cutoff": cutoff, "lim": limit},
            ).fetchall()
            freq_rows = conn.execute(
                sa.text(
                    f"""SELECT CAST((timestamp AT TIME ZONE 'UTC') AS DATE) AS day,
                               {table_id_expr} AS table_id,
                               SUM(CASE WHEN action = 'query.remote' THEN 1 ELSE 0 END) AS remote,
                               SUM(CASE WHEN action = 'query.local'  THEN 1 ELSE 0 END) AS local
                        FROM audit_log
                        WHERE timestamp >= :cutoff
                          AND action IN ('query.remote', 'query.local')
                          AND resource LIKE 'table:%'
                        GROUP BY day, {table_id_expr}
                        ORDER BY day DESC,
                                 (SUM(CASE WHEN action = 'query.remote' THEN 1 ELSE 0 END)
                                  + SUM(CASE WHEN action = 'query.local' THEN 1 ELSE 0 END)) DESC"""
                ),
                {"cutoff": cutoff},
            ).fetchall()
            totals = conn.execute(
                sa.text(
                    f"""SELECT COALESCE(SUM({bytes_expr}), 0) AS total_scan_bytes,
                               SUM(CASE WHEN action = 'query.remote'    THEN 1 ELSE 0 END) AS remote,
                               SUM(CASE WHEN action = 'query.local'     THEN 1 ELSE 0 END) AS local,
                               SUM(CASE WHEN action = 'snapshot.create' THEN 1 ELSE 0 END) AS snaps
                        FROM audit_log
                        WHERE timestamp >= :cutoff
                          AND action IN ('query.remote', 'query.local', 'snapshot.create')"""
                ),
                {"cutoff": cutoff},
            ).fetchone()

        top_tables = [
            {
                "table_id": r[0],
                "queries": int(r[1]),
                "failed": int(r[2] or 0),
                "scan_bytes": int(r[3] or 0),
                "remote": int(r[4] or 0),
                "local": int(r[5] or 0),
                "registered": bool(r[6]),
            }
            for r in top_rows
        ]
        frequency = [
            {
                "day": r[0].isoformat() if r[0] else None,
                "table_id": r[1],
                "remote": int(r[2] or 0),
                "local": int(r[3] or 0),
            }
            for r in freq_rows
        ]
        return {
            "top_tables": top_tables,
            "frequency": frequency,
            "total_scan_bytes": int(totals[0] or 0),
            "remote_queries": int(totals[1] or 0),
            "local_queries": int(totals[2] or 0),
            "snapshot_creates": int(totals[3] or 0),
        }

    def telemetry_facets(self, since: datetime) -> dict:
        def _facet(col: str, lim: int) -> list:
            with self._engine.connect() as conn:
                rows = conn.execute(
                    sa.text(
                        f"SELECT {col}, COUNT(*) AS n FROM usage_events "
                        f"WHERE occurred_at >= :since AND {col} IS NOT NULL "
                        f"GROUP BY {col} ORDER BY n DESC LIMIT :lim"
                    ),
                    {"since": since, "lim": lim},
                ).fetchall()
            return [{"value": r[0], "count": r[1]} for r in rows]

        return {
            "users": _facet("username", 50),
            "tools": _facet("tool_name", 50),
            "sources": _facet("source", 20),
            "event_types": _facet("event_type", 20),
        }

    def telemetry_kpis(self, filters: dict) -> dict:
        where_sql, params = self._events_where(filters)
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    f"""SELECT COUNT(*),
                              COUNT(DISTINCT username),
                              COUNT(DISTINCT tool_name),
                              SUM(CASE WHEN is_error THEN 1 ELSE 0 END)
                       FROM usage_events WHERE {where_sql}"""
                ),
                params,
            ).fetchone()
        total, users, tools, errors = (int(x or 0) for x in row)
        return {"events_total": total, "distinct_users": users, "distinct_tools": tools, "errors": errors}

    def usage_query(
        self,
        filters: dict,
        *,
        group_by: str | None,
        sort_col: str,
        sort_dir: str,
        limit: int,
        offset: int,
    ) -> dict:
        """Filtered + optionally grouped read against usage_events (PG).

        ``group_by`` ∈ {None, 'day', 'username', 'tool_name', 'source', 'ref_id'}.
        Mirrors ``UsageRepository.usage_query`` with named :params for PG.
        """
        where_sql, params = self._events_where(filters)
        sort_dir = "ASC" if sort_dir.upper() == "ASC" else "DESC"

        if group_by and group_by in _GROUP_BY_COLUMNS:
            expr, alias = _GROUP_BY_COLUMNS[group_by]
            valid_sort = {
                "bucket": expr,
                "invocations": "COUNT(*)",
                "distinct_users": "COUNT(DISTINCT username)",
                "distinct_sessions": "COUNT(DISTINCT session_id)",
                "errors": "SUM(CASE WHEN is_error THEN 1 ELSE 0 END)",
            }
            order_expr = valid_sort.get(sort_col, "COUNT(*)")
            with self._engine.connect() as conn:
                total_buckets = int(
                    conn.execute(
                        sa.text(f"SELECT COUNT(DISTINCT {expr}) FROM usage_events WHERE {where_sql}"),
                        params,
                    ).scalar()
                    or 0
                )
                rows = conn.execute(
                    sa.text(
                        f"""SELECT {expr} AS bucket,
                                   COUNT(*) AS invocations,
                                   COUNT(DISTINCT username) AS distinct_users,
                                   COUNT(DISTINCT session_id) AS distinct_sessions,
                                   SUM(CASE WHEN is_error THEN 1 ELSE 0 END) AS errors
                            FROM usage_events WHERE {where_sql}
                            GROUP BY {expr}
                            ORDER BY {order_expr} {sort_dir}
                            LIMIT :lim OFFSET :off"""
                    ),
                    dict(params, lim=limit, off=offset),
                ).fetchall()
            out = [
                {
                    "bucket": (str(r[0]) if r[0] is not None else None),
                    "invocations": int(r[1] or 0),
                    "distinct_users": int(r[2] or 0),
                    "distinct_sessions": int(r[3] or 0),
                    "errors": int(r[4] or 0),
                }
                for r in rows
            ]
            return {
                "group_by": group_by,
                "group_alias": alias,
                "rows": out,
                "total": total_buckets,
                "limit": limit,
                "offset": offset,
                "next_offset": offset + limit if (offset + limit) < total_buckets else None,
            }

        # ungrouped — raw events
        _COLS = [
            "id",
            "occurred_at",
            "username",
            "source",
            "ref_id",
            "event_type",
            "tool_name",
            "skill_name",
            "subagent_type",
            "command_name",
            "is_error",
            "session_id",
            "model",
        ]
        valid_sort_raw = {"occurred_at": "occurred_at", "invocations": "occurred_at"}
        order_expr = valid_sort_raw.get(sort_col, "occurred_at")
        with self._engine.connect() as conn:
            total = int(
                conn.execute(
                    sa.text(f"SELECT COUNT(*) FROM usage_events WHERE {where_sql}"),
                    params,
                ).scalar()
                or 0
            )
            rows = conn.execute(
                sa.text(
                    f"""SELECT {",".join(_COLS)}
                       FROM usage_events WHERE {where_sql}
                       ORDER BY {order_expr} {sort_dir}
                       LIMIT :lim OFFSET :off"""
                ),
                dict(params, lim=limit, off=offset),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(zip(_COLS, r))
            if d.get("occurred_at"):
                d["occurred_at"] = d["occurred_at"].isoformat()
            out.append(d)
        return {
            "group_by": None,
            "rows": out,
            "total": total,
            "limit": limit,
            "offset": offset,
            "next_offset": offset + limit if (offset + limit) < total else None,
        }

    # ------------------------------------------------------------------
    # admin telemetry export + text-to-SQL execution (Postgres).
    # Mirrors UsageRepository.
    # ------------------------------------------------------------------

    @staticmethod
    def _export_where(filters: dict) -> tuple[str, dict]:
        """Compose the WHERE clause for the admin export (named :params).

        Unlike ``_events_where`` every key is optional:
          since (datetime), until (datetime), username, source.
        """
        where, params = ["1=1"], {}
        if filters.get("since") is not None:
            where.append("occurred_at >= :since")
            params["since"] = filters["since"]
        if filters.get("until") is not None:
            where.append("occurred_at < :until")
            params["until"] = filters["until"]
        if filters.get("username"):
            where.append("username = :username")
            params["username"] = filters["username"]
        if filters.get("source"):
            where.append("source = :source")
            params["source"] = filters["source"]
        return " AND ".join(where), params

    def count_events_export(self, filters: dict) -> int:
        """Row count for the admin export's audit-log entry."""
        where_sql, params = self._export_where(filters)
        with self._engine.connect() as conn:
            n = conn.execute(
                sa.text(f"SELECT COUNT(*) FROM usage_events WHERE {where_sql}"),
                params,
            ).scalar()
        return int(n or 0)

    def export_events(self, filters: dict) -> tuple[list[str], list[tuple]]:
        """Full-width usage_events read for the admin export.

        Returns ``(columns, rows)`` ordered by occurred_at. Fully
        materialised — the export is an occasional admin action and the
        events table is bounded by retention pruning.
        """
        where_sql, params = self._export_where(filters)
        with self._engine.connect() as conn:
            res = conn.execute(
                sa.text(f"SELECT * FROM usage_events WHERE {where_sql} ORDER BY occurred_at"),
                params,
            )
            cols = list(res.keys())
            rows = [tuple(r) for r in res.fetchall()]
        return cols, rows

    def execute_readonly_select(self, sql: str) -> tuple[list[str], list[tuple]]:
        """Execute a caller-validated SELECT (usage.ask) on this backend.

        The caller MUST pass the statement through
        ``src.usage_ask.validate_select_only`` first — this method adds no
        validation of its own. Uses ``exec_driver_sql`` (raw DBAPI
        passthrough) so colons inside string/time literals in the
        LLM-generated SQL are not mistaken for SQLAlchemy bind params.
        Returns ``(columns, rows)``.
        """
        with self._engine.connect() as conn:
            res = conn.exec_driver_sql(sql)
            cols = list(res.keys())
            rows = [tuple(r) for r in res.fetchall()]
        return cols, rows

    # ------------------------------------------------------------------
    # /home status frame (Postgres).  Mirrors UsageRepository.home_stats.
    # ------------------------------------------------------------------

    def home_stats(self, user_id: str, username: str, since: datetime) -> dict:
        with self._engine.connect() as conn:
            sess = conn.execute(
                sa.text(
                    """
                    SELECT
                        COUNT(*)                                 AS sessions,
                        COALESCE(SUM(user_messages), 0)          AS prompts,
                        COALESCE(SUM(input_tokens), 0)            AS input_tokens,
                        COALESCE(SUM(output_tokens), 0)           AS output_tokens,
                        COALESCE(SUM(cache_read_tokens), 0)       AS cache_read,
                        COALESCE(SUM(cache_creation_tokens), 0)   AS cache_creation
                    FROM usage_session_summary
                    WHERE (user_id = :uid OR username = :uname)
                      AND started_at >= :since
                    """
                ),
                {"uid": user_id, "uname": username, "since": since},
            ).first()
            proj = conn.execute(
                sa.text(
                    """
                    SELECT COUNT(DISTINCT cwd) FROM usage_events
                    WHERE (user_id = :uid OR username = :uname)
                      AND cwd IS NOT NULL
                      AND occurred_at >= :since
                    """
                ),
                {"uid": user_id, "uname": username, "since": since},
            ).first()
        sessions, prompts, input_t, output_t, cache_read, cache_creation = (int(x or 0) for x in sess)
        return {
            "sessions": sessions,
            "prompts": prompts,
            "input_tokens": input_t,
            "output_tokens": output_t,
            "cache_read": cache_read,
            "cache_creation": cache_creation,
            "projects": int((proj[0] if proj else 0) or 0),
        }

    # ------------------------------------------------------------------
    # session summary aggregate reads (Postgres).
    # ------------------------------------------------------------------

    @staticmethod
    def _sessions_where(filters: dict) -> tuple[str, dict]:
        # anchor mirror of the DuckDB sibling (browser default: uploaded)
        anchor_col = (
            "COALESCE(uploaded_at, started_at)"
            if filters.get("anchor") == "uploaded"
            else "started_at"
        )
        where = [f"{anchor_col} >= :since"]
        params: dict = {"since": filters["since"]}
        if filters.get("username"):
            where.append("username = :username")
            params["username"] = filters["username"]
        if filters.get("model"):
            where.append("primary_model = :model")
            params["model"] = filters["model"]
        if filters.get("only_errors"):
            where.append("tool_errors > 0")
        if filters.get("q"):
            where.append("(session_id LIKE :q OR session_file LIKE :q)")
            params["q"] = f"%{filters['q']}%"
        return " AND ".join(where), params

    def sessions_count(self, filters: dict) -> int:
        where_sql, params = self._sessions_where(filters)
        with self._engine.connect() as conn:
            v = conn.execute(
                sa.text(f"SELECT COUNT(*) FROM usage_session_summary WHERE {where_sql}"),
                params,
            ).scalar()
        return int(v or 0)

    def sessions_list(self, filters: dict, *, sort_col: str, direction: str, limit: int, offset: int) -> List[dict]:
        where_sql, params = self._sessions_where(filters)
        col = _SESSION_SORT_KEYS.get(sort_col, "started_at")
        direction = "ASC" if direction.upper() == "ASC" else "DESC"
        params = dict(params, lim=limit, off=offset)
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    f"""SELECT {",".join(_SESSION_COLS)}
                       FROM usage_session_summary WHERE {where_sql}
                       ORDER BY {col} {direction}
                       LIMIT :lim OFFSET :off"""
                ),
                params,
            ).fetchall()
        return [dict(zip(_SESSION_COLS, r)) for r in rows]

    def sessions_kpis(self, filters: dict) -> dict:
        where_sql, params = self._sessions_where(filters)
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    f"""SELECT COUNT(*),
                              COUNT(DISTINCT username),
                              SUM(CASE WHEN tool_errors > 0 THEN 1 ELSE 0 END),
                              SUM(tool_calls),
                              SUM(tool_errors)
                       FROM usage_session_summary WHERE {where_sql}"""
                ),
                params,
            ).fetchone()
        sessions_total, users, error_sessions, tool_calls_total, tool_errors_total = (int(x or 0) for x in row)
        return {
            "sessions_total": sessions_total,
            "distinct_users": users,
            "error_sessions": error_sessions,
            "tool_calls_total": tool_calls_total,
            "tool_errors_total": tool_errors_total,
        }

    def session_file_basenames_since(self, since: datetime) -> "set[str]":
        """Mirror of ``UsageRepository.session_file_basenames_since``."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT session_file FROM usage_session_summary "
                    "WHERE COALESCE(uploaded_at, started_at) >= :since"
                ),
                {"since": since},
            ).fetchall()
        return {r[0].rsplit("/", 1)[-1] for r in rows if r[0]}

    def sessions_facets(self, since: datetime) -> dict:
        """Distinct usernames + models present in usage_session_summary for the window."""
        with self._engine.connect() as conn:
            users = conn.execute(
                sa.text(
                    "SELECT username, COUNT(*) AS n FROM usage_session_summary "
                    "WHERE started_at >= :since AND username IS NOT NULL "
                    "GROUP BY username ORDER BY n DESC LIMIT 50"
                ),
                {"since": since},
            ).fetchall()
            models = conn.execute(
                sa.text(
                    "SELECT primary_model, COUNT(*) AS n FROM usage_session_summary "
                    "WHERE started_at >= :since AND primary_model IS NOT NULL "
                    "GROUP BY primary_model ORDER BY n DESC LIMIT 30"
                ),
                {"since": since},
            ).fetchall()
        return {
            "users": [{"value": r[0], "count": r[1]} for r in users],
            "models": [{"value": r[0], "count": r[1]} for r in models],
        }

    def get_session_summary(self, session_file: str) -> dict | None:
        """Return a summary row dict for a single session_file, or None."""
        _KEYS = (
            "session_id",
            "started_at",
            "ended_at",
            "active_seconds",
            "wall_seconds",
            "user_messages",
            "assistant_messages",
            "tool_calls",
            "tool_errors",
            "primary_model",
        )
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT session_id, started_at, ended_at, active_seconds, wall_seconds, "
                    "user_messages, assistant_messages, tool_calls, tool_errors, "
                    "primary_model FROM usage_session_summary WHERE session_file = :sf"
                ),
                {"sf": session_file},
            ).fetchone()
        if row is None:
            return None
        return dict(zip(_KEYS, row))

    def list_sessions_for_user_admin(self, *, user_id: str, username: str) -> List[dict]:
        """PG mirror of UsageRepository.list_sessions_for_user_admin (9 cols)."""
        cols = [
            "session_file",
            "session_id",
            "started_at",
            "ended_at",
            "active_seconds",
            "wall_seconds",
            "tool_calls",
            "tool_errors",
            "primary_model",
        ]
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """
                    SELECT
                        session_file, session_id, started_at, ended_at,
                        active_seconds, wall_seconds,
                        tool_calls, tool_errors, primary_model
                    FROM usage_session_summary
                    WHERE user_id = :uid OR username = :uname
                    ORDER BY started_at DESC NULLS LAST
                    """
                ),
                {"uid": user_id, "uname": username},
            ).fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def list_sessions_for_user_self(self, username: str) -> list[dict]:
        """PG mirror of UsageRepository.list_sessions_for_user_self (14 cols)."""
        cols = [
            "session_file",
            "session_id",
            "started_at",
            "ended_at",
            "active_seconds",
            "wall_seconds",
            "user_messages",
            "tool_calls",
            "tool_errors",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
            "primary_model",
        ]
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """
                    SELECT
                        session_file, session_id, started_at, ended_at,
                        active_seconds, wall_seconds,
                        user_messages, tool_calls, tool_errors,
                        input_tokens, output_tokens,
                        cache_read_tokens, cache_creation_tokens,
                        primary_model
                    FROM usage_session_summary
                    WHERE username = :uname
                    ORDER BY started_at DESC NULLS LAST
                    """
                ),
                {"uname": username},
            ).fetchall()
        return [dict(zip(cols, r)) for r in rows]

    # ------------------------------------------------------------------
    # per-user token breakdown reads (Postgres).  Mirrors UsageRepository.
    # ------------------------------------------------------------------

    def tokens_daily_series(self, username: str, days: int) -> list[dict]:
        # PARITY: PG interval window mirrors delete_older_than's dialect.
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """
                    SELECT
                        CAST((started_at AT TIME ZONE 'UTC') AS DATE) AS day,
                        COALESCE(SUM(input_tokens), 0)          AS input_tokens,
                        COALESCE(SUM(output_tokens), 0)         AS output_tokens,
                        COALESCE(SUM(cache_read_tokens), 0)     AS cache_read,
                        COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation,
                        COUNT(*) AS sessions
                    FROM usage_session_summary
                    WHERE username = :uname
                      AND started_at >= (CURRENT_TIMESTAMP - (:days * INTERVAL '1 day'))
                    GROUP BY 1
                    ORDER BY 1
                    """
                ),
                {"uname": username, "days": days},
            ).fetchall()
        return [
            {
                "day": d.isoformat() if hasattr(d, "isoformat") else str(d),
                "input": int(i or 0),
                "output": int(o or 0),
                "cache_read": int(cr or 0),
                "cache_creation": int(cc or 0),
                "sessions": int(s or 0),
                "total": int((i or 0) + (o or 0) + (cr or 0) + (cc or 0)),
            }
            for (d, i, o, cr, cc, s) in rows
        ]

    def tokens_by_model(self, username: str) -> list[dict]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """
                    SELECT
                        COALESCE(primary_model, '(unknown)') AS model,
                        COALESCE(SUM(input_tokens), 0)          AS input_tokens,
                        COALESCE(SUM(output_tokens), 0)         AS output_tokens,
                        COALESCE(SUM(cache_read_tokens), 0)     AS cache_read,
                        COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation,
                        COUNT(*) AS sessions
                    FROM usage_session_summary
                    WHERE username = :uname
                    GROUP BY 1
                    ORDER BY (
                        COALESCE(SUM(input_tokens), 0)
                        + COALESCE(SUM(output_tokens), 0)
                        + COALESCE(SUM(cache_read_tokens), 0)
                        + COALESCE(SUM(cache_creation_tokens), 0)
                    ) DESC
                    """
                ),
                {"uname": username},
            ).fetchall()
        return [
            {
                "model": m,
                "input": int(i or 0),
                "output": int(o or 0),
                "cache_read": int(cr or 0),
                "cache_creation": int(cc or 0),
                "sessions": int(s or 0),
                "total": int((i or 0) + (o or 0) + (cr or 0) + (cc or 0)),
            }
            for (m, i, o, cr, cc, s) in rows
        ]

    def tokens_top_sessions(self, username: str, limit: int = 10) -> list[dict]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """
                    SELECT
                        session_file, session_id, started_at, primary_model,
                        input_tokens, output_tokens,
                        cache_read_tokens, cache_creation_tokens,
                        (COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)
                         + COALESCE(cache_read_tokens, 0) + COALESCE(cache_creation_tokens, 0))
                        AS tokens_total
                    FROM usage_session_summary
                    WHERE username = :uname
                    ORDER BY tokens_total DESC
                    LIMIT :lim
                    """
                ),
                {"uname": username, "lim": limit},
            ).fetchall()
        return [
            {
                "session_file": sf,
                "session_id": sid,
                "started_at": st.isoformat() if hasattr(st, "isoformat") else st,
                "primary_model": pm,
                "input": int(i or 0),
                "output": int(o or 0),
                "cache_read": int(cr or 0),
                "cache_creation": int(cc or 0),
                "total": int(tt or 0),
            }
            for (sf, sid, st, pm, i, o, cr, cc, tt) in rows
        ]

    def tokens_totals(self, username: str) -> dict:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    """
                    SELECT
                        COALESCE(SUM(input_tokens), 0),
                        COALESCE(SUM(output_tokens), 0),
                        COALESCE(SUM(cache_read_tokens), 0),
                        COALESCE(SUM(cache_creation_tokens), 0),
                        COUNT(*)
                    FROM usage_session_summary
                    WHERE username = :uname
                    """
                ),
                {"uname": username},
            ).fetchone()
        ti, to, tcr, tcc, tses = row or (0, 0, 0, 0, 0)
        return {
            "input": int(ti or 0),
            "output": int(to or 0),
            "cache_read": int(tcr or 0),
            "cache_creation": int(tcc or 0),
            "total": int((ti or 0) + (to or 0) + (tcr or 0) + (tcc or 0)),
            "sessions": int(tses or 0),
        }

    # ------------------------------------------------------------------
    # adoption dashboard reads (Postgres).  Mirrors UsageRepository.
    # ------------------------------------------------------------------

    _TOKEN_SUM = "input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens"

    def adoption_kpis(self, since: datetime) -> dict:
        with self._engine.connect() as conn:
            s = conn.execute(
                sa.text(
                    f"""SELECT COALESCE(SUM(active_seconds), 0),
                               COALESCE(SUM(wall_seconds), 0),
                               COUNT(*),
                               COALESCE(SUM(user_messages), 0),
                               COALESCE(SUM(skill_invocations), 0),
                               COALESCE(SUM({self._TOKEN_SUM}), 0),
                               COALESCE(SUM(tool_calls), 0),
                               COALESCE(SUM(tool_errors), 0),
                               COUNT(DISTINCT COALESCE(user_id, username))
                          FROM usage_session_summary WHERE started_at >= :since"""
                ),
                {"since": since},
            ).fetchone()
            dskills = conn.execute(
                sa.text(
                    "SELECT COUNT(DISTINCT skill_name) FROM usage_events "
                    "WHERE occurred_at >= :since AND skill_name IS NOT NULL"
                ),
                {"since": since},
            ).scalar()
        return {
            "active_seconds": int(s[0] or 0),
            "wall_seconds": int(s[1] or 0),
            "sessions": int(s[2] or 0),
            "prompts": int(s[3] or 0),
            "skill_invocations": int(s[4] or 0),
            "tokens": int(s[5] or 0),
            "tool_calls": int(s[6] or 0),
            "tool_errors": int(s[7] or 0),
            "active_users": int(s[8] or 0),
            "distinct_skills": int(dskills or 0),
        }

    def adoption_sessions_series(self, start_date: date) -> Dict[date, dict]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    f"""SELECT CAST((started_at AT TIME ZONE 'UTC') AS DATE) AS day,
                               COALESCE(SUM(active_seconds), 0),
                               COALESCE(SUM(wall_seconds), 0),
                               COUNT(*),
                               COALESCE(SUM(user_messages), 0),
                               COALESCE(SUM({self._TOKEN_SUM}), 0),
                               COALESCE(SUM(tool_calls), 0)
                          FROM usage_session_summary
                          WHERE CAST((started_at AT TIME ZONE 'UTC') AS DATE) >= :sd
                          GROUP BY day ORDER BY day"""
                ),
                {"sd": start_date},
            ).fetchall()
        return {
            r[0]: {
                "active_seconds": int(r[1] or 0),
                "wall_seconds": int(r[2] or 0),
                "sessions": int(r[3] or 0),
                "prompts": int(r[4] or 0),
                "tokens": int(r[5] or 0),
                "tool_calls": int(r[6] or 0),
            }
            for r in rows
        }

    def adoption_events_series(self, start_date: date) -> Dict[date, dict]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """SELECT CAST((occurred_at AT TIME ZONE 'UTC') AS DATE) AS day,
                              COUNT(DISTINCT COALESCE(user_id, username)),
                              SUM(CASE WHEN skill_name IS NOT NULL THEN 1 ELSE 0 END)
                         FROM usage_events
                         WHERE CAST((occurred_at AT TIME ZONE 'UTC') AS DATE) >= :sd
                         GROUP BY day ORDER BY day"""
                ),
                {"sd": start_date},
            ).fetchall()
        return {r[0]: {"active_users": int(r[1] or 0), "skill_events": int(r[2] or 0)} for r in rows}

    def adoption_top_users(self, since: datetime, limit: int = 10, q: Optional[str] = None) -> List[dict]:
        # adoption stays anchored on started_at (usage-over-time semantics)
        where = ["started_at >= :since"]
        params: dict = {"since": since, "lim": limit}
        if q:
            where.append("(username LIKE :q OR user_id LIKE :q)")
            params["q"] = f"%{q}%"
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    f"""SELECT MAX(user_id), MAX(username),
                               COALESCE(SUM(active_seconds), 0) AS total_active,
                               COUNT(*),
                               COALESCE(SUM(user_messages), 0),
                               COALESCE(SUM({self._TOKEN_SUM}), 0),
                               MAX(ended_at)
                          FROM usage_session_summary
                          WHERE {" AND ".join(where)}
                          GROUP BY COALESCE(user_id, username)
                          ORDER BY total_active DESC LIMIT :lim"""
                ),
                params,
            ).fetchall()
        return [
            {
                "user_id": r[0],
                "username": r[1],
                "active_seconds": int(r[2] or 0),
                "sessions": int(r[3] or 0),
                "prompts": int(r[4] or 0),
                "tokens": int(r[5] or 0),
                "last_active": r[6].isoformat() if r[6] else None,
            }
            for r in rows
        ]

    def adoption_top_skills(self, since: datetime, limit: int = 10) -> List[dict]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """SELECT skill_name, COUNT(*) AS n,
                              COUNT(DISTINCT COALESCE(user_id, username)) AS users
                         FROM usage_events
                         WHERE occurred_at >= :since AND skill_name IS NOT NULL
                         GROUP BY skill_name ORDER BY n DESC LIMIT :lim"""
                ),
                {"since": since, "lim": limit},
            ).fetchall()
        return [{"skill_name": r[0], "invocations": int(r[1]), "distinct_users": int(r[2])} for r in rows]

    # ── per-user variants ─────────────────────────────────────────────

    def adoption_user_kpis(self, since: datetime, user_id: str, username: str) -> dict:
        p = {"since": since, "uid": user_id, "uname": username}
        with self._engine.connect() as conn:
            s = conn.execute(
                sa.text(
                    f"""SELECT COALESCE(SUM(active_seconds), 0),
                               COALESCE(SUM(wall_seconds), 0),
                               COUNT(*),
                               COALESCE(SUM(user_messages), 0),
                               COALESCE(SUM({self._TOKEN_SUM}), 0),
                               COALESCE(SUM(tool_calls), 0),
                               COALESCE(SUM(tool_errors), 0),
                               MAX(ended_at)
                          FROM usage_session_summary
                          WHERE started_at >= :since
                            AND (user_id = :uid OR username = :uname)"""
                ),
                p,
            ).fetchone()
            e = conn.execute(
                sa.text(
                    """SELECT COUNT(DISTINCT tool_name),
                              COUNT(DISTINCT skill_name),
                              COUNT(DISTINCT CAST((occurred_at AT TIME ZONE 'UTC') AS DATE))
                         FROM usage_events
                         WHERE occurred_at >= :since
                           AND (user_id = :uid OR username = :uname)"""
                ),
                p,
            ).fetchone()
            models = conn.execute(
                sa.text(
                    """SELECT primary_model, COUNT(*) AS n
                         FROM usage_session_summary
                         WHERE started_at >= :since
                           AND (user_id = :uid OR username = :uname)
                           AND primary_model IS NOT NULL
                         GROUP BY primary_model ORDER BY n DESC"""
                ),
                p,
            ).fetchall()
        return {
            "active_seconds": int(s[0] or 0),
            "wall_seconds": int(s[1] or 0),
            "sessions": int(s[2] or 0),
            "prompts": int(s[3] or 0),
            "tokens": int(s[4] or 0),
            "tool_calls": int(s[5] or 0),
            "tool_errors": int(s[6] or 0),
            "last_active": s[7].isoformat() if s[7] else None,
            "distinct_tools": int(e[0] or 0),
            "distinct_skills": int(e[1] or 0),
            "active_days": int(e[2] or 0),
            "models": [{"model": m[0], "count": int(m[1])} for m in models],
        }

    def adoption_user_sessions_series(self, start_date: date, user_id: str, username: str) -> Dict[date, dict]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    f"""SELECT CAST((started_at AT TIME ZONE 'UTC') AS DATE) AS day,
                               COALESCE(SUM(active_seconds), 0),
                               COALESCE(SUM(wall_seconds), 0),
                               COUNT(*),
                               COALESCE(SUM(user_messages), 0),
                               COALESCE(SUM({self._TOKEN_SUM}), 0),
                               COALESCE(SUM(tool_calls), 0)
                          FROM usage_session_summary
                          WHERE CAST((started_at AT TIME ZONE 'UTC') AS DATE) >= :sd
                            AND (user_id = :uid OR username = :uname)
                          GROUP BY day ORDER BY day"""
                ),
                {"sd": start_date, "uid": user_id, "uname": username},
            ).fetchall()
        return {
            r[0]: {
                "active_seconds": int(r[1] or 0),
                "wall_seconds": int(r[2] or 0),
                "sessions": int(r[3] or 0),
                "prompts": int(r[4] or 0),
                "tokens": int(r[5] or 0),
                "tool_calls": int(r[6] or 0),
            }
            for r in rows
        }

    def adoption_user_events_series(self, start_date: date, user_id: str, username: str) -> Dict[date, dict]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """SELECT CAST((occurred_at AT TIME ZONE 'UTC') AS DATE) AS day,
                              SUM(CASE WHEN skill_name IS NOT NULL THEN 1 ELSE 0 END)
                         FROM usage_events
                         WHERE CAST((occurred_at AT TIME ZONE 'UTC') AS DATE) >= :sd
                           AND (user_id = :uid OR username = :uname)
                         GROUP BY day ORDER BY day"""
                ),
                {"sd": start_date, "uid": user_id, "uname": username},
            ).fetchall()
        return {r[0]: {"skill_events": int(r[1] or 0)} for r in rows}

    def adoption_user_top_skills(self, since: datetime, user_id: str, username: str, limit: int = 10) -> List[dict]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """SELECT skill_name, COUNT(*) AS n
                         FROM usage_events
                         WHERE occurred_at >= :since
                           AND (user_id = :uid OR username = :uname)
                           AND skill_name IS NOT NULL
                         GROUP BY skill_name ORDER BY n DESC LIMIT :lim"""
                ),
                {"since": since, "uid": user_id, "uname": username, "lim": limit},
            ).fetchall()
        return [{"skill_name": r[0], "invocations": int(r[1])} for r in rows]

    def adoption_user_top_tools(self, since: datetime, user_id: str, username: str, limit: int = 10) -> List[dict]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """SELECT tool_name, COUNT(*) AS n
                         FROM usage_events
                         WHERE occurred_at >= :since
                           AND (user_id = :uid OR username = :uname)
                           AND tool_name IS NOT NULL
                         GROUP BY tool_name ORDER BY n DESC LIMIT :lim"""
                ),
                {"since": since, "uid": user_id, "uname": username, "lim": limit},
            ).fetchall()
        return [{"tool_name": r[0], "invocations": int(r[1])} for r in rows]

    # ------------------------------------------------------------------
    # write methods
    # ------------------------------------------------------------------

    def upsert_events(self, rows: list[dict], *, processor_version: int) -> int:
        if not rows:
            return 0
        placeholders = ",".join(f":{c}" for c in _EVENT_COLS)
        sql = f"INSERT INTO usage_events ({','.join(_EVENT_COLS)}) VALUES ({placeholders}) ON CONFLICT (id) DO NOTHING"
        with self._engine.begin() as conn:
            for r in rows:
                params = {c: r.get(c) for c in _EVENT_COLS}
                # Backfill the DEFAULT-FALSE columns DuckDB would coerce from
                # NULL silently. PG is strict on NOT NULL even when a default
                # exists — see GH-XXX for the equivalent INSERT semantics gap.
                if params.get("is_error") is None:
                    params["is_error"] = False
                params["processor_version"] = processor_version
                conn.execute(sa.text(sql), params)
        return len(rows)

    def emit_server_event(
        self,
        *,
        event_type: str,
        user_id: Optional[str],
        username: str = "",
        props: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Insert one synthetic usage_events row for a server-side product event.

        Mirrors ``UsageRepository.emit_server_event`` (DuckDB). ``props`` is
        serialized into the ``friction_tags`` JSONB column via
        ``CAST(:friction_tags AS JSONB)`` (the project's PG-JSONB write
        convention); ``session_id`` / ``session_file`` are server-synthetic so
        the NOT NULL constraints stay satisfied.
        """
        event_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO usage_events
                       (id, session_id, session_file, username, event_type,
                        is_error, source, occurred_at, processor_version,
                        friction_tags, user_id)
                       VALUES (:id, :session_id, :session_file, :username, :event_type,
                               FALSE, 'server', :occurred_at, 1,
                               CAST(:friction_tags AS JSONB), :user_id)"""
                ),
                {
                    "id": event_id,
                    "session_id": f"server-{event_id[:8]}",
                    "session_file": f"server/{event_type}.jsonl",
                    "username": username or (user_id or "anonymous"),
                    "event_type": event_type,
                    "occurred_at": now,
                    "friction_tags": json.dumps(props) if props else None,
                    "user_id": user_id,
                },
            )
        return event_id

    def upsert_summary(self, summary: dict, *, processor_version: int) -> None:
        """Upsert on session_file PK.

        Mirrors ``UsageRepository.upsert_summary`` (src/repositories/usage.py):
        the DO UPDATE SET refreshes every column, including ``username``,
        ``started_at`` and ``user_id``, so a session first processed before
        its user account existed gets its identity backfilled on reprocess.
        Postgres never hit the DuckDB ART secondary-index corruption
        (INCIDENT 2026-07-20); the shared schema simply no longer carries
        those secondary indexes, keeping write semantics identical across
        backends.
        """
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO usage_session_summary
                        (session_file, session_id, username, started_at, ended_at,
                         active_seconds, wall_seconds, user_messages, assistant_messages,
                         tool_calls, tool_errors, skill_invocations, subagent_dispatches,
                         mcp_calls, slash_commands, distinct_tools, distinct_skills,
                         primary_model, input_tokens, output_tokens, cache_read_tokens,
                         cache_creation_tokens, processor_version, user_id,
                         uploaded_at)
                    VALUES (:sf, :sid, :u, :sa, :ea,
                            :acts, :walls, :um, :am,
                            :tc, :te, :si, :sd,
                            :mc, :sc, :dt, :ds,
                            :pm, :it, :ot, :crt,
                            :cct, :pv, :uid,
                            COALESCE(:upat, CURRENT_TIMESTAMP))
                    ON CONFLICT (session_file) DO UPDATE SET
                      session_id           = EXCLUDED.session_id,
                      username             = EXCLUDED.username,
                      started_at           = EXCLUDED.started_at,
                      ended_at             = EXCLUDED.ended_at,
                      active_seconds       = EXCLUDED.active_seconds,
                      wall_seconds         = EXCLUDED.wall_seconds,
                      user_messages        = EXCLUDED.user_messages,
                      assistant_messages   = EXCLUDED.assistant_messages,
                      tool_calls           = EXCLUDED.tool_calls,
                      tool_errors          = EXCLUDED.tool_errors,
                      skill_invocations    = EXCLUDED.skill_invocations,
                      subagent_dispatches  = EXCLUDED.subagent_dispatches,
                      mcp_calls            = EXCLUDED.mcp_calls,
                      slash_commands       = EXCLUDED.slash_commands,
                      distinct_tools       = EXCLUDED.distinct_tools,
                      distinct_skills      = EXCLUDED.distinct_skills,
                      primary_model        = EXCLUDED.primary_model,
                      input_tokens         = EXCLUDED.input_tokens,
                      output_tokens        = EXCLUDED.output_tokens,
                      cache_read_tokens    = EXCLUDED.cache_read_tokens,
                      cache_creation_tokens= EXCLUDED.cache_creation_tokens,
                      processor_version    = EXCLUDED.processor_version,
                      user_id              = EXCLUDED.user_id,
                      -- first arrival wins (anchor semantics, spec Phase C)
                      uploaded_at = COALESCE(usage_session_summary.uploaded_at, EXCLUDED.uploaded_at)
                    """
                ),
                {
                    "sf": summary["session_file"],
                    "sid": summary.get("session_id", ""),
                    "u": summary["username"],
                    "sa": summary.get("started_at"),
                    "ea": summary.get("ended_at"),
                    "acts": summary.get("active_seconds", 0),
                    "walls": summary.get("wall_seconds", 0),
                    "um": summary.get("user_messages", 0),
                    "am": summary.get("assistant_messages", 0),
                    "tc": summary.get("tool_calls", 0),
                    "te": summary.get("tool_errors", 0),
                    "si": summary.get("skill_invocations", 0),
                    "sd": summary.get("subagent_dispatches", 0),
                    "mc": summary.get("mcp_calls", 0),
                    "sc": summary.get("slash_commands", 0),
                    "dt": summary.get("distinct_tools", 0),
                    "ds": summary.get("distinct_skills", 0),
                    "pm": summary.get("primary_model"),
                    "it": summary.get("input_tokens", 0),
                    "ot": summary.get("output_tokens", 0),
                    "crt": summary.get("cache_read_tokens", 0),
                    "cct": summary.get("cache_creation_tokens", 0),
                    "pv": processor_version,
                    "uid": summary.get("user_id"),
                    "upat": summary.get("uploaded_at"),
                },
            )

    def purge_for_session(self, session_file: str) -> int:
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.text("DELETE FROM usage_events WHERE session_file = :sf RETURNING 1"),
                {"sf": session_file},
            ).all()
            events_deleted = len(rows)
            conn.execute(
                sa.text("DELETE FROM usage_session_summary WHERE session_file = :sf"),
                {"sf": session_file},
            )
        return events_deleted

    def delete_older_than(self, days: int) -> int:
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.text(
                    "DELETE FROM usage_events "
                    "WHERE occurred_at < (CURRENT_TIMESTAMP - (:days * INTERVAL '1 day')) "
                    "RETURNING 1"
                ),
                {"days": days},
            ).all()
        return len(rows)

    def count_events(self) -> int:
        """Total usage_events row count."""
        with self._engine.connect() as conn:
            v = conn.execute(sa.text("SELECT COUNT(*) FROM usage_events")).scalar()
        return int(v or 0)

    def reset_all(self, *, clear_processors: "list[str] | None" = None) -> "dict[str, int]":
        """PG mirror of UsageRepository.reset_all. Owns its own transaction via
        engine.begin(); returns per-table deleted counts. When
        ``clear_processors`` is given, the matching ``session_processor_state``
        rows are deleted in the SAME transaction (reported under ``state_rows``)
        so the reprocess reset is all-or-nothing — see the DuckDB sibling."""
        out: dict[str, int] = {}
        with self._engine.begin() as conn:
            if clear_processors:
                state_rows = conn.execute(
                    sa.text("DELETE FROM session_processor_state WHERE processor_name = ANY(:names) RETURNING 1"),
                    {"names": list(clear_processors)},
                ).all()
                out["state_rows"] = len(state_rows)
            for key, table in (
                ("events", "usage_events"),
                ("session_summary", "usage_session_summary"),
                ("tool_daily", "usage_tool_daily"),
                ("marketplace_item_daily", "usage_marketplace_item_daily"),
                ("marketplace_item_window", "usage_marketplace_item_window"),
            ):
                rows = conn.execute(sa.text(f"DELETE FROM {table} RETURNING 1")).all()
                out[key] = len(rows)
        return out

    # ------------------------------------------------------------------
    # marketplace usage rollup producer (#728).  Mirrors UsageRepository.
    #
    # Same semantics as the DuckDB sibling: ``since_day=None`` is a FULL
    # rebuild (cutoff = earliest usage_events day); an explicit ``since_day``
    # is the cheap incremental refresh (scheduler-tick steady state).
    # Attribution/aggregation stay in ``services.session_processors.usage_lib``
    # (pure Python, shared) — only the SQL dialect differs here.
    # ------------------------------------------------------------------

    def _curated_flea_lookup(self, conn) -> tuple[set, dict, set]:
        curated_plugins = {
            r[0] for r in conn.execute(sa.text("SELECT DISTINCT name FROM marketplace_plugins")).fetchall()
        }
        flea_entities = {
            r[0]: r[1]
            for r in conn.execute(
                sa.text("SELECT synthetic_name, type FROM store_entities WHERE visibility_status='approved'")
            ).fetchall()
        }
        flea_plugins = {synthetic for synthetic, ent_type in flea_entities.items() if ent_type == "plugin"}
        return curated_plugins, flea_entities, flea_plugins

    def _last_30d_due(self, conn) -> bool:
        row = conn.execute(
            sa.text(
                "SELECT processed_at FROM session_processor_state "
                "WHERE processor_name = :p AND session_file = '__rollup__'"
            ),
            {"p": _MARKETPLACE_30D_TRACKER},
        ).fetchone()
        if row is None:
            return True
        last = row[0]
        if last is None:
            return True
        now = datetime.now(timezone.utc)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (now - last).total_seconds() >= WINDOW_30D_REFRESH_SECONDS

    def _mark_last_30d_refreshed(self, conn) -> None:
        now = datetime.now(timezone.utc)
        conn.execute(
            sa.text(
                """
                INSERT INTO session_processor_state
                    (processor_name, session_file, username, processed_at, items_extracted)
                VALUES (:p, '__rollup__', 'system', :now, 0)
                ON CONFLICT (processor_name, session_file) DO UPDATE SET
                    processed_at = EXCLUDED.processed_at
                """
            ),
            {"p": _MARKETPLACE_30D_TRACKER, "now": now},
        )

    def _rebuild_window(
        self, conn, period_label: str, cutoff_day, curated_plugins: set, flea_entities: dict, flea_plugins: set
    ) -> None:
        events = conn.execute(
            sa.text(
                """
                SELECT
                    CAST((occurred_at AT TIME ZONE 'UTC') AS DATE) AS day,
                    user_id,
                    is_error,
                    skill_name,
                    subagent_type,
                    command_name,
                    event_type
                FROM usage_events
                WHERE CAST((occurred_at AT TIME ZONE 'UTC') AS DATE) >= :cutoff
                """
            ),
            {"cutoff": cutoff_day},
        ).fetchall()
        buckets = _aggregate_events(events, curated_plugins, flea_entities, flea_plugins, group_by_day=False)
        conn.execute(
            sa.text("DELETE FROM usage_marketplace_item_window WHERE period_label = :pl"),
            {"pl": period_label},
        )
        if buckets:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO usage_marketplace_item_window
                        (period_label, source, type, parent_plugin, name, invocations, distinct_users)
                    VALUES (:pl, :source, :type, :parent, :name, :inv, :users)
                    """
                ),
                [
                    {
                        "pl": period_label,
                        "source": source,
                        "type": type_,
                        "parent": parent,
                        "name": name,
                        "inv": v["count"],
                        "users": len(v["users"]),
                    }
                    for (source, type_, parent, name), v in buckets.items()
                ],
            )

    def rebuild_rollups(self, *, since_day: "date | None" = None, force_30d: bool = False) -> None:
        """PG mirror of UsageRepository.rebuild_rollups — identical semantics,
        one transaction via ``engine.begin()``.

        Intentionally still DELETE-then-bulk-INSERT here (unlike the DuckDB
        sibling's ON CONFLICT DO UPDATE): that rewrite works around a DuckDB
        1.5.4-only PRIMARY KEY index assertion bug, not a general SQL
        anti-pattern. Postgres has no equivalent issue with this pattern.
        """
        with self._engine.begin() as conn:
            if since_day is None:
                row = conn.execute(
                    sa.text("SELECT MIN(CAST((occurred_at AT TIME ZONE 'UTC') AS DATE)) FROM usage_events")
                ).fetchone()
                since_day = row[0] if row and row[0] else datetime.now(timezone.utc).date()

            curated_plugins, flea_entities, flea_plugins = self._curated_flea_lookup(conn)
            do_30d = force_30d or self._last_30d_due(conn)

            # ---- Legacy: usage_tool_daily (unchanged) ----
            conn.execute(sa.text("DELETE FROM usage_tool_daily WHERE day >= :since"), {"since": since_day})
            conn.execute(
                sa.text(
                    """
                    INSERT INTO usage_tool_daily
                        (day, tool_name, source, invocations, error_count, distinct_users, distinct_sessions)
                    SELECT
                        CAST((occurred_at AT TIME ZONE 'UTC') AS DATE) AS day,
                        tool_name,
                        source,
                        COUNT(*) AS invocations,
                        SUM(CASE WHEN is_error THEN 1 ELSE 0 END) AS error_count,
                        COUNT(DISTINCT username) AS distinct_users,
                        COUNT(DISTINCT session_id) AS distinct_sessions
                    FROM usage_events
                    WHERE CAST((occurred_at AT TIME ZONE 'UTC') AS DATE) >= :since
                      AND tool_name IS NOT NULL
                    GROUP BY day, tool_name, source
                    """
                ),
                {"since": since_day},
            )

            # ---- usage_marketplace_item_daily ----
            daily_events = conn.execute(
                sa.text(
                    """
                    SELECT
                        CAST((occurred_at AT TIME ZONE 'UTC') AS DATE) AS day,
                        user_id,
                        is_error,
                        skill_name,
                        subagent_type,
                        command_name,
                        event_type
                    FROM usage_events
                    WHERE CAST((occurred_at AT TIME ZONE 'UTC') AS DATE) >= :since
                    """
                ),
                {"since": since_day},
            ).fetchall()
            daily_buckets = _aggregate_events(
                daily_events, curated_plugins, flea_entities, flea_plugins, group_by_day=True
            )
            conn.execute(sa.text("DELETE FROM usage_marketplace_item_daily WHERE day >= :since"), {"since": since_day})
            if daily_buckets:
                conn.execute(
                    sa.text(
                        """
                        INSERT INTO usage_marketplace_item_daily
                            (day, source, type, parent_plugin, name, count, distinct_users, error_count)
                        VALUES (:day, :source, :type, :parent, :name, :count, :users, :errors)
                        """
                    ),
                    [
                        {
                            "day": day,
                            "source": source,
                            "type": type_,
                            "parent": parent,
                            "name": name,
                            "count": v["count"],
                            "users": len(v["users"]),
                            "errors": v["errors"],
                        }
                        for (day, source, type_, parent, name), v in daily_buckets.items()
                    ],
                )

            # ---- usage_marketplace_item_window period_label='last_7d' (full) ----
            cutoff_7d = (datetime.now(timezone.utc) - timedelta(days=7)).date()
            self._rebuild_window(conn, "last_7d", cutoff_7d, curated_plugins, flea_entities, flea_plugins)

            # ---- usage_marketplace_item_window period_label='last_30d' (hourly) ----
            if do_30d:
                cutoff_30d = (datetime.now(timezone.utc) - timedelta(days=30)).date()
                self._rebuild_window(conn, "last_30d", cutoff_30d, curated_plugins, flea_entities, flea_plugins)
                self._mark_last_30d_refreshed(conn)
