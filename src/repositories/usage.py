"""Repository for usage_events and usage_session_summary tables (schema v41)."""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import duckdb

from services.session_processors.usage_lib import (
    _MARKETPLACE_30D_TRACKER,
    WINDOW_30D_REFRESH_SECONDS,
    _aggregate_events,
)


# Group-by buckets shared by the /telemetry/query endpoint. The first element
# is the SQL expression, the second a stable alias the UI keys on.
_GROUP_BY_COLUMNS = {
    "day": ("CAST(occurred_at AS DATE)", "day"),
    "username": ("username", "username"),
    "tool_name": ("tool_name", "tool_name"),
    "source": ("source", "source"),
    "ref_id": ("ref_id", "ref_id"),
}


class UsageRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    # ------------------------------------------------------------------
    # telemetry aggregate reads (DuckDB).  Mirrored in UsagePgRepository.
    # ------------------------------------------------------------------

    @staticmethod
    def _events_where(filters: dict) -> tuple[str, list]:
        """Compose a parametrised WHERE clause over usage_events.

        ``filters`` keys (all optional except ``since``):
          since (datetime, required), username, tool_name, source,
          event_type, only_errors (bool), q (free-text).
        """
        where = ["occurred_at >= ?"]
        params: list = [filters["since"]]
        if filters.get("username"):
            where.append("username = ?")
            params.append(filters["username"])
        if filters.get("tool_name"):
            where.append("tool_name = ?")
            params.append(filters["tool_name"])
        if filters.get("source"):
            where.append("source = ?")
            params.append(filters["source"])
        if filters.get("event_type"):
            where.append("event_type = ?")
            params.append(filters["event_type"])
        if filters.get("only_errors"):
            where.append("is_error = TRUE")
        if filters.get("q"):
            where.append("(tool_name LIKE ? OR skill_name LIKE ? OR subagent_type LIKE ? OR command_name LIKE ?)")
            like = f"%{filters['q']}%"
            params.extend([like, like, like, like])
        return " AND ".join(where), params

    def summary_top_tools(self, cutoff: datetime, limit: int = 10) -> List[dict]:
        rows = self.conn.execute(
            """SELECT tool_name, source, COUNT(*) AS n
               FROM usage_events
               WHERE occurred_at >= ? AND tool_name IS NOT NULL
               GROUP BY tool_name, source ORDER BY n DESC LIMIT ?""",
            [cutoff, limit],
        ).fetchall()
        return [{"tool_name": r[0], "source": r[1], "invocations": int(r[2])} for r in rows]

    def summary_top_users(self, cutoff: datetime, limit: int = 10) -> List[dict]:
        rows = self.conn.execute(
            """SELECT username, COUNT(*) AS n FROM usage_events
               WHERE occurred_at >= ? GROUP BY username ORDER BY n DESC LIMIT ?""",
            [cutoff, limit],
        ).fetchall()
        return [{"username": r[0], "tool_calls": int(r[1])} for r in rows]

    def summary_error_rate(self, cutoff: datetime, limit: int = 10) -> List[dict]:
        rows = self.conn.execute(
            """SELECT tool_name, COUNT(*) AS n,
                      SUM(CASE WHEN is_error THEN 1 ELSE 0 END) AS err
               FROM usage_events
               WHERE occurred_at >= ? AND tool_name IS NOT NULL
               GROUP BY tool_name HAVING COUNT(*) > 0 ORDER BY n DESC LIMIT ?""",
            [cutoff, limit],
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
        """DAU map: day → distinct active users, for days >= start_date."""
        rows = self.conn.execute(
            """SELECT CAST(occurred_at AS DATE) AS day, COUNT(DISTINCT username) AS n
               FROM usage_events
               WHERE CAST(occurred_at AS DATE) >= ?
               GROUP BY day ORDER BY day""",
            [start_date],
        ).fetchall()
        return {r[0]: int(r[1]) for r in rows}

    def summary_slow_actions(self, cutoff: datetime, limit: int = 10) -> List[dict]:
        """Percentile latency per audit action over the window. Uses
        approx_quantile; on failure falls back to pulling raw durations and
        computing percentiles in Python."""
        try:
            rows = self.conn.execute(
                """SELECT action,
                          approx_quantile(duration_ms, 0.5)  AS p50,
                          approx_quantile(duration_ms, 0.95) AS p95,
                          approx_quantile(duration_ms, 0.99) AS p99,
                          MAX(duration_ms) AS max_ms,
                          COUNT(*) AS n
                   FROM audit_log
                   WHERE timestamp >= ? AND duration_ms IS NOT NULL AND duration_ms > 0
                   GROUP BY action HAVING n >= 5
                   ORDER BY p95 DESC LIMIT ?""",
                [cutoff, limit],
            ).fetchall()
            return [
                {
                    "action": r[0],
                    "p50": int(r[1] or 0),
                    "p95": int(r[2] or 0),
                    "p99": int(r[3] or 0),
                    "max_ms": int(r[4] or 0),
                    "n": int(r[5]),
                }
                for r in rows
            ]
        except Exception:
            raw = self.conn.execute(
                """SELECT action, duration_ms FROM audit_log
                   WHERE timestamp >= ? AND duration_ms IS NOT NULL AND duration_ms > 0""",
                [cutoff],
            ).fetchall()
            return _slow_actions_from_raw(raw, limit)

    def summary_query_telemetry(self, cutoff: datetime, limit: int = 10) -> dict:
        """On-demand aggregation over the query-telemetry audit rows (#410).

        Aggregates ``audit_log`` rows with action ∈ {query.remote, query.local,
        snapshot.create} written by ``app/api/query.py`` / ``app/api/v2_scan.py``.
        The queried table id is parsed from ``resource`` (``table:<id>`` or
        ``table:<id>:as:<snapshot>``); rows without a table resource (``adhoc``)
        count toward totals but not the per-table ranking. ``scan_bytes`` is
        summed from ``params.$.bytes_scanned`` (NULL on the local path).

        The parsed id comes from a best-effort SQL regex, so it is not
        guaranteed to name a real table. Rows are therefore joined against
        ``table_registry`` (``registered``) and split by outcome (``failed``),
        and the ranking is by *successful* queries: an id that never resolved
        cannot outrank a table with genuine usage just by being retried.

        Returns:
            top_tables: [{table_id, queries, failed, registered, scan_bytes,
                          remote, local}]
            frequency:  [{day, table_id, remote, local}]
            total_scan_bytes, remote_queries, local_queries, snapshot_creates
        """
        # ``table:<id>`` or ``table:<id>:as:<name>`` → <id>. split_part on the
        # 4-char-stripped tail (drop the leading "table:") then cut any ":as:".
        table_id_expr = "split_part(substr(resource, 7), ':as:', 1)"
        bytes_expr = "TRY_CAST(json_extract_string(params, '$.bytes_scanned') AS BIGINT)"
        # `result` is 'success' or an 'error.<code>' string; NULL on rows
        # written before the column existed, which count as neither.
        failed_expr = "SUM(CASE WHEN result LIKE 'error%' THEN 1 ELSE 0 END)"

        # Per-table ranking (only rows that carry a table resource).
        top_rows = self.conn.execute(
            f"""SELECT agg.table_id,
                       agg.queries,
                       agg.failed,
                       agg.scan_bytes,
                       agg.remote,
                       agg.local,
                       CASE WHEN reg.id IS NULL THEN false ELSE true END AS registered
                FROM (
                    SELECT {table_id_expr} AS table_id,
                           COUNT(*) AS queries,
                           {failed_expr} AS failed,
                           COALESCE(SUM({bytes_expr}), 0) AS scan_bytes,
                           SUM(CASE WHEN action = 'query.remote' THEN 1 ELSE 0 END) AS remote,
                           SUM(CASE WHEN action = 'query.local'  THEN 1 ELSE 0 END) AS local
                    FROM audit_log
                    WHERE timestamp >= ?
                      AND action IN ('query.remote', 'query.local', 'snapshot.create')
                      AND resource LIKE 'table:%'
                    GROUP BY table_id
                ) agg
                LEFT JOIN table_registry reg ON reg.id = agg.table_id
                ORDER BY (agg.queries - agg.failed) DESC,
                         agg.queries DESC,
                         agg.scan_bytes DESC
                LIMIT ?""",
            [cutoff, limit],
        ).fetchall()
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

        # Per-day per-table remote/local frequency (table rows only).
        freq_rows = self.conn.execute(
            f"""SELECT CAST(timestamp AS DATE) AS day,
                       {table_id_expr} AS table_id,
                       SUM(CASE WHEN action = 'query.remote' THEN 1 ELSE 0 END) AS remote,
                       SUM(CASE WHEN action = 'query.local'  THEN 1 ELSE 0 END) AS local
                FROM audit_log
                WHERE timestamp >= ?
                  AND action IN ('query.remote', 'query.local')
                  AND resource LIKE 'table:%'
                GROUP BY day, table_id
                ORDER BY day DESC, (remote + local) DESC""",
            [cutoff],
        ).fetchall()
        frequency = [
            {
                "day": r[0].isoformat() if r[0] else None,
                "table_id": r[1],
                "remote": int(r[2] or 0),
                "local": int(r[3] or 0),
            }
            for r in freq_rows
        ]

        # Window totals across all query-telemetry rows (incl. adhoc).
        totals = self.conn.execute(
            f"""SELECT COALESCE(SUM({bytes_expr}), 0) AS total_scan_bytes,
                       SUM(CASE WHEN action = 'query.remote'    THEN 1 ELSE 0 END) AS remote,
                       SUM(CASE WHEN action = 'query.local'     THEN 1 ELSE 0 END) AS local,
                       SUM(CASE WHEN action = 'snapshot.create' THEN 1 ELSE 0 END) AS snaps
                FROM audit_log
                WHERE timestamp >= ?
                  AND action IN ('query.remote', 'query.local', 'snapshot.create')""",
            [cutoff],
        ).fetchone()
        return {
            "top_tables": top_tables,
            "frequency": frequency,
            "total_scan_bytes": int(totals[0] or 0),
            "remote_queries": int(totals[1] or 0),
            "local_queries": int(totals[2] or 0),
            "snapshot_creates": int(totals[3] or 0),
        }

    def telemetry_facets(self, since: datetime) -> dict:
        users = self.conn.execute(
            "SELECT username, COUNT(*) AS n FROM usage_events WHERE occurred_at >= ? "
            "AND username IS NOT NULL GROUP BY username ORDER BY n DESC LIMIT 50",
            [since],
        ).fetchall()
        tools = self.conn.execute(
            "SELECT tool_name, COUNT(*) AS n FROM usage_events WHERE occurred_at >= ? "
            "AND tool_name IS NOT NULL GROUP BY tool_name ORDER BY n DESC LIMIT 50",
            [since],
        ).fetchall()
        sources = self.conn.execute(
            "SELECT source, COUNT(*) AS n FROM usage_events WHERE occurred_at >= ? "
            "AND source IS NOT NULL GROUP BY source ORDER BY n DESC LIMIT 20",
            [since],
        ).fetchall()
        event_types = self.conn.execute(
            "SELECT event_type, COUNT(*) AS n FROM usage_events WHERE occurred_at >= ? "
            "AND event_type IS NOT NULL GROUP BY event_type ORDER BY n DESC LIMIT 20",
            [since],
        ).fetchall()
        return {
            "users": [{"value": r[0], "count": r[1]} for r in users],
            "tools": [{"value": r[0], "count": r[1]} for r in tools],
            "sources": [{"value": r[0], "count": r[1]} for r in sources],
            "event_types": [{"value": r[0], "count": r[1]} for r in event_types],
        }

    def telemetry_kpis(self, filters: dict) -> dict:
        where_sql, params = self._events_where(filters)
        row = self.conn.execute(
            f"""SELECT COUNT(*),
                      COUNT(DISTINCT username),
                      COUNT(DISTINCT tool_name),
                      SUM(CASE WHEN is_error THEN 1 ELSE 0 END)
               FROM usage_events WHERE {where_sql}""",
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
        """Filtered + optionally grouped read against usage_events.

        ``group_by`` ∈ {None, 'day', 'username', 'tool_name', 'source', 'ref_id'}.
        When grouped returns one bucket per row; when ungrouped returns raw events.
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
            total_buckets = int(
                self.conn.execute(
                    f"SELECT COUNT(DISTINCT {expr}) FROM usage_events WHERE {where_sql}",
                    params,
                ).fetchone()[0]
                or 0
            )
            rows = self.conn.execute(
                f"""SELECT {expr} AS bucket,
                           COUNT(*) AS invocations,
                           COUNT(DISTINCT username) AS distinct_users,
                           COUNT(DISTINCT session_id) AS distinct_sessions,
                           SUM(CASE WHEN is_error THEN 1 ELSE 0 END) AS errors
                    FROM usage_events WHERE {where_sql}
                    GROUP BY {expr}
                    ORDER BY {order_expr} {sort_dir}
                    LIMIT ? OFFSET ?""",
                params + [limit, offset],
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
        total = int(
            self.conn.execute(
                f"SELECT COUNT(*) FROM usage_events WHERE {where_sql}",
                params,
            ).fetchone()[0]
            or 0
        )
        rows = self.conn.execute(
            f"""SELECT {",".join(_COLS)}
               FROM usage_events WHERE {where_sql}
               ORDER BY {order_expr} {sort_dir}
               LIMIT ? OFFSET ?""",
            params + [limit, offset],
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
    # admin telemetry export + text-to-SQL execution (DuckDB).
    # Mirrored in UsagePgRepository.
    # ------------------------------------------------------------------

    @staticmethod
    def _export_where(filters: dict) -> tuple[str, list]:
        """Compose the WHERE clause for the admin export.

        Unlike ``_events_where`` every key is optional:
          since (datetime), until (datetime), username, source.
        """
        where, params = ["1=1"], []
        if filters.get("since") is not None:
            where.append("occurred_at >= ?")
            params.append(filters["since"])
        if filters.get("until") is not None:
            where.append("occurred_at < ?")
            params.append(filters["until"])
        if filters.get("username"):
            where.append("username = ?")
            params.append(filters["username"])
        if filters.get("source"):
            where.append("source = ?")
            params.append(filters["source"])
        return " AND ".join(where), params

    def count_events_export(self, filters: dict) -> int:
        """Row count for the admin export's audit-log entry."""
        where_sql, params = self._export_where(filters)
        row = self.conn.execute(f"SELECT COUNT(*) FROM usage_events WHERE {where_sql}", params).fetchone()
        return int(row[0] or 0)

    def export_events(self, filters: dict) -> tuple[list[str], list[tuple]]:
        """Full-width usage_events read for the admin export.

        Returns ``(columns, rows)`` ordered by occurred_at. Fully
        materialised — the export is an occasional admin action and the
        events table is bounded by retention pruning.
        """
        where_sql, params = self._export_where(filters)
        rel = self.conn.execute(
            f"SELECT * FROM usage_events WHERE {where_sql} ORDER BY occurred_at",
            params,
        )
        cols = [d[0] for d in rel.description]
        return cols, rel.fetchall()

    def execute_readonly_select(self, sql: str) -> tuple[list[str], list[tuple]]:
        """Execute a caller-validated SELECT (usage.ask) on this backend.

        The caller MUST pass the statement through
        ``src.usage_ask.validate_select_only`` first — this method adds no
        validation of its own. Returns ``(columns, rows)``.
        """
        rel = self.conn.execute(sql)
        cols = [d[0] for d in rel.description]
        return cols, rel.fetchall()

    # ------------------------------------------------------------------
    # /home status frame (DuckDB).  Source: app.api.me.compute_home_stats.
    # ------------------------------------------------------------------

    def home_stats(self, user_id: str, username: str, since: datetime) -> dict:
        """Sessions / prompts / tokens / distinct-projects counters for the
        /home status frame, over ``[since, now)``.

        Matches on both ``user_id`` (stable, populated by the v45 pipeline)
        and ``username`` (legacy rows before the v45 backfill) so stats stay
        complete during the transition period. Mirrored in
        ``UsagePgRepository.home_stats`` with identical aggregation semantics.
        """
        sess = self.conn.execute(
            """
            SELECT
                COUNT(*)                                 AS sessions,
                COALESCE(SUM(user_messages), 0)          AS prompts,
                COALESCE(SUM(input_tokens), 0)            AS input_tokens,
                COALESCE(SUM(output_tokens), 0)           AS output_tokens,
                COALESCE(SUM(cache_read_tokens), 0)       AS cache_read,
                COALESCE(SUM(cache_creation_tokens), 0)   AS cache_creation
            FROM usage_session_summary
            WHERE (user_id = ? OR username = ?)
              AND started_at >= ?
            """,
            [user_id, username, since],
        ).fetchone()
        proj = self.conn.execute(
            """
            SELECT COUNT(DISTINCT cwd) FROM usage_events
            WHERE (user_id = ? OR username = ?)
              AND cwd IS NOT NULL
              AND occurred_at >= ?
            """,
            [user_id, username, since],
        ).fetchone()
        sessions, prompts, input_t, output_t, cache_read, cache_creation = (int(x or 0) for x in sess)
        return {
            "sessions": sessions,
            "prompts": prompts,
            "input_tokens": input_t,
            "output_tokens": output_t,
            "cache_read": cache_read,
            "cache_creation": cache_creation,
            "projects": int(proj[0] or 0),
        }

    # ------------------------------------------------------------------
    # session summary aggregate reads (DuckDB).
    # ------------------------------------------------------------------

    @staticmethod
    def _sessions_where(filters: dict) -> tuple[str, list]:
        # anchor: 'started' windows on when the analyst ran the session;
        # 'uploaded' (browser default) on when it ARRIVED — late queue
        # catch-ups stay visible in recent windows (spec Phase C).
        # COALESCE guards rows that predate the v105 backfill.
        anchor_col = (
            "COALESCE(uploaded_at, started_at)"
            if filters.get("anchor") == "uploaded"
            else "started_at"
        )
        where = [f"{anchor_col} >= ?"]
        params: list = [filters["since"]]
        if filters.get("username"):
            where.append("username = ?")
            params.append(filters["username"])
        if filters.get("model"):
            where.append("primary_model = ?")
            params.append(filters["model"])
        if filters.get("only_errors"):
            where.append("tool_errors > 0")
        if filters.get("q"):
            where.append("(session_id LIKE ? OR session_file LIKE ?)")
            like = f"%{filters['q']}%"
            params.extend([like, like])
        return " AND ".join(where), params

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

    def sessions_count(self, filters: dict) -> int:
        where_sql, params = self._sessions_where(filters)
        return int(
            self.conn.execute(
                f"SELECT COUNT(*) FROM usage_session_summary WHERE {where_sql}",
                params,
            ).fetchone()[0]
            or 0
        )

    def sessions_list(self, filters: dict, *, sort_col: str, direction: str, limit: int, offset: int) -> List[dict]:
        where_sql, params = self._sessions_where(filters)
        col = self._SESSION_SORT_KEYS.get(sort_col, "started_at")
        direction = "ASC" if direction.upper() == "ASC" else "DESC"
        rows = self.conn.execute(
            f"""SELECT {",".join(self._SESSION_COLS)}
               FROM usage_session_summary WHERE {where_sql}
               ORDER BY {col} {direction}
               LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()
        return [dict(zip(self._SESSION_COLS, r)) for r in rows]

    def sessions_kpis(self, filters: dict) -> dict:
        where_sql, params = self._sessions_where(filters)
        row = self.conn.execute(
            f"""SELECT COUNT(*),
                      COUNT(DISTINCT username),
                      SUM(CASE WHEN tool_errors > 0 THEN 1 ELSE 0 END),
                      SUM(tool_calls),
                      SUM(tool_errors)
               FROM usage_session_summary WHERE {where_sql}""",
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
        """Basenames of ``session_file`` for summaries whose arrival
        (``COALESCE(uploaded_at, started_at)``) is at/after *since* — the
        ingested half of the health pulse's upload reconciliation."""
        rows = self.conn.execute(
            "SELECT session_file FROM usage_session_summary "
            "WHERE COALESCE(uploaded_at, started_at) >= ?",
            [since],
        ).fetchall()
        return {r[0].rsplit("/", 1)[-1] for r in rows if r[0]}

    def sessions_facets(self, since: datetime) -> dict:
        """Distinct usernames + models present in usage_session_summary for the window."""
        users = self.conn.execute(
            "SELECT username, COUNT(*) AS n FROM usage_session_summary "
            "WHERE started_at >= ? AND username IS NOT NULL "
            "GROUP BY username ORDER BY n DESC LIMIT 50",
            [since],
        ).fetchall()
        models = self.conn.execute(
            "SELECT primary_model, COUNT(*) AS n FROM usage_session_summary "
            "WHERE started_at >= ? AND primary_model IS NOT NULL "
            "GROUP BY primary_model ORDER BY n DESC LIMIT 30",
            [since],
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
        row = self.conn.execute(
            "SELECT session_id, started_at, ended_at, active_seconds, wall_seconds, "
            "user_messages, assistant_messages, tool_calls, tool_errors, "
            "primary_model FROM usage_session_summary WHERE session_file = ?",
            [session_file],
        ).fetchone()
        if row is None:
            return None
        return dict(zip(_KEYS, row))

    def list_sessions_for_user_admin(self, *, user_id: str, username: str) -> List[dict]:
        """Admin per-user session list (9 cols). Matches on user_id OR username
        so both ingestion paths + pre-v45 rows surface. Source:
        app/api/admin_user_sessions.py list_user_sessions."""
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
        rows = self.conn.execute(
            """
            SELECT
                session_file, session_id, started_at, ended_at,
                active_seconds, wall_seconds,
                tool_calls, tool_errors, primary_model
            FROM usage_session_summary
            WHERE user_id = ? OR username = ?
            ORDER BY started_at DESC NULLS LAST
            """,
            [user_id, username],
        ).fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def list_sessions_for_user_self(self, username: str) -> list[dict]:
        """Self per-user session list (14 cols). Filters on username only.
        KEPT SEPARATE from the admin variant. Source: app/api/me_stats.py
        list_self_sessions."""
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
        rows = self.conn.execute(
            """
            SELECT
                session_file, session_id, started_at, ended_at,
                active_seconds, wall_seconds,
                user_messages, tool_calls, tool_errors,
                input_tokens, output_tokens,
                cache_read_tokens, cache_creation_tokens,
                primary_model
            FROM usage_session_summary
            WHERE username = ?
            ORDER BY started_at DESC NULLS LAST
            """,
            [username],
        ).fetchall()
        return [dict(zip(cols, r)) for r in rows]

    # ------------------------------------------------------------------
    # per-user token breakdown reads (DuckDB).  Source: me_stats.get_tokens.
    # ------------------------------------------------------------------

    def tokens_daily_series(self, username: str, days: int) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT
                CAST(started_at AS DATE) AS day,
                COALESCE(SUM(input_tokens), 0)          AS input_tokens,
                COALESCE(SUM(output_tokens), 0)         AS output_tokens,
                COALESCE(SUM(cache_read_tokens), 0)     AS cache_read,
                COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation,
                COUNT(*) AS sessions
            FROM usage_session_summary
            WHERE username = ?
              AND started_at >= current_timestamp - INTERVAL (?) DAY
            GROUP BY 1
            ORDER BY 1
            """,
            [username, days],
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
        rows = self.conn.execute(
            """
            SELECT
                COALESCE(primary_model, '(unknown)') AS model,
                COALESCE(SUM(input_tokens), 0)          AS input_tokens,
                COALESCE(SUM(output_tokens), 0)         AS output_tokens,
                COALESCE(SUM(cache_read_tokens), 0)     AS cache_read,
                COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation,
                COUNT(*) AS sessions
            FROM usage_session_summary
            WHERE username = ?
            GROUP BY 1
            ORDER BY (
                COALESCE(SUM(input_tokens), 0)
                + COALESCE(SUM(output_tokens), 0)
                + COALESCE(SUM(cache_read_tokens), 0)
                + COALESCE(SUM(cache_creation_tokens), 0)
            ) DESC
            """,
            [username],
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
        rows = self.conn.execute(
            """
            SELECT
                session_file, session_id, started_at, primary_model,
                input_tokens, output_tokens,
                cache_read_tokens, cache_creation_tokens,
                (COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)
                 + COALESCE(cache_read_tokens, 0) + COALESCE(cache_creation_tokens, 0))
                AS tokens_total
            FROM usage_session_summary
            WHERE username = ?
            ORDER BY tokens_total DESC
            LIMIT ?
            """,
            [username, limit],
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
        row = self.conn.execute(
            """
            SELECT
                COALESCE(SUM(input_tokens), 0),
                COALESCE(SUM(output_tokens), 0),
                COALESCE(SUM(cache_read_tokens), 0),
                COALESCE(SUM(cache_creation_tokens), 0),
                COUNT(*)
            FROM usage_session_summary
            WHERE username = ?
            """,
            [username],
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
    # adoption dashboard reads (DuckDB).  Mirrored in UsagePgRepository.
    #
    # Time / sessions / tokens / prompts / skill_invocations are read
    # from usage_session_summary (bucketed on started_at); distinct
    # users-per-day and skill *events* come from usage_events (bucketed
    # on occurred_at). User identity is COALESCE(user_id, username)
    # everywhere so a person appearing under both keys counts once.
    # Seconds stay raw ints here; the API layer converts to hours.
    # ------------------------------------------------------------------

    _TOKEN_SUM = "input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens"

    def adoption_kpis(self, since: datetime) -> dict:
        s = self.conn.execute(
            f"""SELECT COALESCE(SUM(active_seconds), 0),
                       COALESCE(SUM(wall_seconds), 0),
                       COUNT(*),
                       COALESCE(SUM(user_messages), 0),
                       COALESCE(SUM(skill_invocations), 0),
                       COALESCE(SUM({self._TOKEN_SUM}), 0),
                       COALESCE(SUM(tool_calls), 0),
                       COALESCE(SUM(tool_errors), 0),
                       COUNT(DISTINCT COALESCE(user_id, username))
                  FROM usage_session_summary WHERE started_at >= ?""",
            [since],
        ).fetchone()
        dskills = self.conn.execute(
            "SELECT COUNT(DISTINCT skill_name) FROM usage_events WHERE occurred_at >= ? AND skill_name IS NOT NULL",
            [since],
        ).fetchone()[0]
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
        rows = self.conn.execute(
            f"""SELECT CAST(started_at AS DATE) AS day,
                       COALESCE(SUM(active_seconds), 0),
                       COALESCE(SUM(wall_seconds), 0),
                       COUNT(*),
                       COALESCE(SUM(user_messages), 0),
                       COALESCE(SUM({self._TOKEN_SUM}), 0),
                       COALESCE(SUM(tool_calls), 0)
                  FROM usage_session_summary
                  WHERE CAST(started_at AS DATE) >= ?
                  GROUP BY day ORDER BY day""",
            [start_date],
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
        rows = self.conn.execute(
            """SELECT CAST(occurred_at AS DATE) AS day,
                      COUNT(DISTINCT COALESCE(user_id, username)),
                      SUM(CASE WHEN skill_name IS NOT NULL THEN 1 ELSE 0 END)
                 FROM usage_events
                 WHERE CAST(occurred_at AS DATE) >= ?
                 GROUP BY day ORDER BY day""",
            [start_date],
        ).fetchall()
        return {r[0]: {"active_users": int(r[1] or 0), "skill_events": int(r[2] or 0)} for r in rows}

    def adoption_top_users(self, since: datetime, limit: int = 10, q: Optional[str] = None) -> List[dict]:
        where = ["started_at >= ?"]
        params: list = [since]
        if q:
            where.append("(username LIKE ? OR user_id LIKE ?)")
            like = f"%{q}%"
            params.extend([like, like])
        params.append(limit)
        rows = self.conn.execute(
            f"""SELECT MAX(user_id), MAX(username),
                       COALESCE(SUM(active_seconds), 0) AS total_active,
                       COUNT(*),
                       COALESCE(SUM(user_messages), 0),
                       COALESCE(SUM({self._TOKEN_SUM}), 0),
                       MAX(ended_at)
                  FROM usage_session_summary
                  WHERE {" AND ".join(where)}
                  GROUP BY COALESCE(user_id, username)
                  ORDER BY total_active DESC LIMIT ?""",
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
        rows = self.conn.execute(
            """SELECT skill_name, COUNT(*) AS n,
                      COUNT(DISTINCT COALESCE(user_id, username)) AS users
                 FROM usage_events
                 WHERE occurred_at >= ? AND skill_name IS NOT NULL
                 GROUP BY skill_name ORDER BY n DESC LIMIT ?""",
            [since, limit],
        ).fetchall()
        return [{"skill_name": r[0], "invocations": int(r[1]), "distinct_users": int(r[2])} for r in rows]

    # ── per-user variants (one user_id + legacy username) ──────────────

    def adoption_user_kpis(self, since: datetime, user_id: str, username: str) -> dict:
        s = self.conn.execute(
            f"""SELECT COALESCE(SUM(active_seconds), 0),
                       COALESCE(SUM(wall_seconds), 0),
                       COUNT(*),
                       COALESCE(SUM(user_messages), 0),
                       COALESCE(SUM({self._TOKEN_SUM}), 0),
                       COALESCE(SUM(tool_calls), 0),
                       COALESCE(SUM(tool_errors), 0),
                       MAX(ended_at)
                  FROM usage_session_summary
                  WHERE started_at >= ? AND (user_id = ? OR username = ?)""",
            [since, user_id, username],
        ).fetchone()
        e = self.conn.execute(
            """SELECT COUNT(DISTINCT tool_name),
                      COUNT(DISTINCT skill_name),
                      COUNT(DISTINCT CAST(occurred_at AS DATE))
                 FROM usage_events
                 WHERE occurred_at >= ? AND (user_id = ? OR username = ?)""",
            [since, user_id, username],
        ).fetchone()
        models = self.conn.execute(
            """SELECT primary_model, COUNT(*) AS n
                 FROM usage_session_summary
                 WHERE started_at >= ? AND (user_id = ? OR username = ?)
                   AND primary_model IS NOT NULL
                 GROUP BY primary_model ORDER BY n DESC""",
            [since, user_id, username],
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
        rows = self.conn.execute(
            f"""SELECT CAST(started_at AS DATE) AS day,
                       COALESCE(SUM(active_seconds), 0),
                       COALESCE(SUM(wall_seconds), 0),
                       COUNT(*),
                       COALESCE(SUM(user_messages), 0),
                       COALESCE(SUM({self._TOKEN_SUM}), 0),
                       COALESCE(SUM(tool_calls), 0)
                  FROM usage_session_summary
                  WHERE CAST(started_at AS DATE) >= ?
                    AND (user_id = ? OR username = ?)
                  GROUP BY day ORDER BY day""",
            [start_date, user_id, username],
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
        rows = self.conn.execute(
            """SELECT CAST(occurred_at AS DATE) AS day,
                      SUM(CASE WHEN skill_name IS NOT NULL THEN 1 ELSE 0 END)
                 FROM usage_events
                 WHERE CAST(occurred_at AS DATE) >= ?
                   AND (user_id = ? OR username = ?)
                 GROUP BY day ORDER BY day""",
            [start_date, user_id, username],
        ).fetchall()
        return {r[0]: {"skill_events": int(r[1] or 0)} for r in rows}

    def adoption_user_top_skills(self, since: datetime, user_id: str, username: str, limit: int = 10) -> List[dict]:
        rows = self.conn.execute(
            """SELECT skill_name, COUNT(*) AS n
                 FROM usage_events
                 WHERE occurred_at >= ? AND (user_id = ? OR username = ?)
                   AND skill_name IS NOT NULL
                 GROUP BY skill_name ORDER BY n DESC LIMIT ?""",
            [since, user_id, username, limit],
        ).fetchall()
        return [{"skill_name": r[0], "invocations": int(r[1])} for r in rows]

    def adoption_user_top_tools(self, since: datetime, user_id: str, username: str, limit: int = 10) -> List[dict]:
        rows = self.conn.execute(
            """SELECT tool_name, COUNT(*) AS n
                 FROM usage_events
                 WHERE occurred_at >= ? AND (user_id = ? OR username = ?)
                   AND tool_name IS NOT NULL
                 GROUP BY tool_name ORDER BY n DESC LIMIT ?""",
            [since, user_id, username, limit],
        ).fetchall()
        return [{"tool_name": r[0], "invocations": int(r[1])} for r in rows]

    # ------------------------------------------------------------------
    # write methods
    # ------------------------------------------------------------------

    def upsert_events(self, rows: list[dict], *, processor_version: int) -> int:
        """INSERT OR IGNORE keyed by event id. Returns number of input rows passed (not new inserts;
        DuckDB returns rowcount=-1 for INSERT OR IGNORE so we cannot cheaply count new vs duplicate).
        """
        if not rows:
            return 0
        cols = [
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
        placeholders = ",".join("?" for _ in cols)
        sql = f"INSERT OR IGNORE INTO usage_events ({','.join(cols)}) VALUES ({placeholders})"
        self.conn.executemany(
            sql, [[r.get(c) if c != "processor_version" else processor_version for c in cols] for r in rows]
        )
        return len(rows)

    def upsert_summary(self, summary: dict, *, processor_version: int) -> None:
        """Upsert on session_file PK.

        INCIDENT 2026-07-17: ``INSERT OR REPLACE`` deterministically hit the
        same DuckDB 1.5.4 PRIMARY KEY index assertion as the DELETE-then-
        bulk-INSERT rollup producers fixed in #909 (`INSERT OR REPLACE`
        deletes-then-inserts the conflicting row internally — the same
        crash trigger) — twice, on two different session_file keys, ~24h
        apart, taking down the whole app process both times. Switched to
        `INSERT ... ON CONFLICT DO UPDATE`, which updates the existing row
        in place instead of deleting it.

        INCIDENT 2026-07-20: ``usage_session_summary`` carried 3 non-unique
        secondary ART indexes (on ``username``, ``started_at`` and
        ``user_id``). The DO UPDATE SET below refreshes those columns on
        every ~10-minute re-process tick; updating an ART-indexed column
        runs as delete-old-entry + insert-new-entry, and a single corrupt
        index entry turned that delete into a FATAL, connection-
        invalidating error that recurred each tick. The fix drops those 3
        indexes (v95, see ``src/db.py::_v94_to_v95``): updating an
        *unindexed* column is a plain in-place write with no ART
        maintenance, so the DO UPDATE keeps refreshing all columns —
        including the late-resolution backfill of ``user_id``/``username``
        for a session first processed before its user account existed. Do
        NOT re-add secondary indexes on ``username``/``started_at``/
        ``user_id`` — that index maintenance on update is what made the
        rewrite fatal.
        """
        self.conn.execute(
            """
            INSERT INTO usage_session_summary
                (session_file, session_id, username, started_at, ended_at,
                 active_seconds, wall_seconds, user_messages, assistant_messages,
                 tool_calls, tool_errors, skill_invocations, subagent_dispatches,
                 mcp_calls, slash_commands, distinct_tools, distinct_skills,
                 primary_model, input_tokens, output_tokens, cache_read_tokens,
                 cache_creation_tokens, processor_version, user_id, uploaded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, current_timestamp))
            ON CONFLICT (session_file) DO UPDATE SET
                session_id = EXCLUDED.session_id,
                username = EXCLUDED.username,
                started_at = EXCLUDED.started_at,
                ended_at = EXCLUDED.ended_at,
                active_seconds = EXCLUDED.active_seconds,
                wall_seconds = EXCLUDED.wall_seconds,
                user_messages = EXCLUDED.user_messages,
                assistant_messages = EXCLUDED.assistant_messages,
                tool_calls = EXCLUDED.tool_calls,
                tool_errors = EXCLUDED.tool_errors,
                skill_invocations = EXCLUDED.skill_invocations,
                subagent_dispatches = EXCLUDED.subagent_dispatches,
                mcp_calls = EXCLUDED.mcp_calls,
                slash_commands = EXCLUDED.slash_commands,
                distinct_tools = EXCLUDED.distinct_tools,
                distinct_skills = EXCLUDED.distinct_skills,
                primary_model = EXCLUDED.primary_model,
                input_tokens = EXCLUDED.input_tokens,
                output_tokens = EXCLUDED.output_tokens,
                cache_read_tokens = EXCLUDED.cache_read_tokens,
                cache_creation_tokens = EXCLUDED.cache_creation_tokens,
                processor_version = EXCLUDED.processor_version,
                -- first arrival wins: keep the original ingest stamp on
                -- re-process ticks (anchor semantics, spec Phase C)
                uploaded_at = COALESCE(usage_session_summary.uploaded_at, EXCLUDED.uploaded_at),
                user_id = EXCLUDED.user_id
            """,
            [
                summary["session_file"],
                summary.get("session_id", ""),
                summary["username"],
                summary.get("started_at"),
                summary.get("ended_at"),
                summary.get("active_seconds", 0),
                summary.get("wall_seconds", 0),
                summary.get("user_messages", 0),
                summary.get("assistant_messages", 0),
                summary.get("tool_calls", 0),
                summary.get("tool_errors", 0),
                summary.get("skill_invocations", 0),
                summary.get("subagent_dispatches", 0),
                summary.get("mcp_calls", 0),
                summary.get("slash_commands", 0),
                summary.get("distinct_tools", 0),
                summary.get("distinct_skills", 0),
                summary.get("primary_model"),
                summary.get("input_tokens", 0),
                summary.get("output_tokens", 0),
                summary.get("cache_read_tokens", 0),
                summary.get("cache_creation_tokens", 0),
                processor_version,
                summary.get("user_id"),
                summary.get("uploaded_at"),
            ],
        )

    def purge_for_session(self, session_file: str) -> int:
        """DELETE events + summary for one session — used on reprocess."""
        r = self.conn.execute("DELETE FROM usage_events WHERE session_file = ?", [session_file])
        events_deleted = r.rowcount if r.rowcount else 0
        self.conn.execute("DELETE FROM usage_session_summary WHERE session_file = ?", [session_file])
        return events_deleted

    def emit_server_event(
        self,
        *,
        event_type: str,
        user_id: Optional[str],
        username: str = "",
        props: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Insert one synthetic usage_events row for a server-side product event.

        v49 unified-stack telemetry — Section 9.2 of the design spec. Reuses
        the existing usage_events table for ``stack.subscribe``,
        ``memory.dismiss``, ``data_package.view`` etc. so /admin/telemetry can
        slice them through the existing summary tools.

        Conventions:
          - ``event_type``   → goes in the column of the same name (e.g.
            ``stack.subscribe``); dotted namespacing is intentional so admins
            can prefix-filter.
          - ``source='server'`` distinguishes these from CC-session events.
          - ``session_id`` / ``session_file`` are server-synthetic UUIDs so
            the NOT NULL constraints stay satisfied. Telemetry consumers
            should key on ``user_id`` + ``event_type``, not session.
          - ``props`` is serialized into ``friction_tags`` (JSON column we
            piggyback for arbitrary event payload — the rename is tracked in
            spec Section 9 as a follow-up).
        """
        event_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO usage_events
               (id, session_id, session_file, username, event_type,
                is_error, source, occurred_at, processor_version,
                friction_tags, user_id)
               VALUES (?, ?, ?, ?, ?, FALSE, 'server', ?, 1, ?, ?)""",
            [
                event_id,
                f"server-{event_id[:8]}",
                f"server/{event_type}.jsonl",
                username or (user_id or "anonymous"),
                event_type,
                now,
                json.dumps(props) if props else None,
                user_id,
            ],
        )
        return event_id

    def delete_older_than(self, days: int) -> int:
        """Retention prune — DELETE events older than now - days."""
        # DuckDB DELETE without RETURNING reports rowcount = -1, so count the
        # RETURNING rows instead (matches reset_all / the other delete methods).
        rows = self.conn.execute(
            """
            DELETE FROM usage_events
            WHERE occurred_at < (CURRENT_TIMESTAMP - INTERVAL (?) DAY)
            RETURNING 1
            """,
            [days],
        ).fetchall()
        return len(rows)

    def count_events(self) -> int:
        """Total usage_events row count."""
        return int(self.conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] or 0)

    def reset_all(self, *, clear_processors: "list[str] | None" = None) -> "dict[str, int]":
        """Wipe all usage fact + rollup tables in ONE transaction, returning
        per-table deleted counts.

        When ``clear_processors`` is given, the matching
        ``session_processor_state`` checkpoint rows are deleted in the SAME
        transaction (reported under ``state_rows``). This keeps the
        ``reprocess_usage`` admin reset all-or-nothing: clearing the rollups
        without their processor checkpoints (or vice-versa) would leave the
        scheduler inconsistent. ``session_processor_state`` isn't a usage table,
        but it IS the checkpoint for the usage processors, so resetting both
        together is a cohesive usage-domain operation."""
        out: dict[str, int] = {}
        self.conn.execute("BEGIN")
        try:
            if clear_processors:
                placeholders = ",".join("?" for _ in clear_processors)
                state_deleted = self.conn.execute(
                    f"DELETE FROM session_processor_state WHERE processor_name IN ({placeholders}) RETURNING 1",
                    list(clear_processors),
                ).fetchall()
                out["state_rows"] = len(state_deleted)
            for key, table in (
                ("events", "usage_events"),
                ("session_summary", "usage_session_summary"),
                ("tool_daily", "usage_tool_daily"),
                ("marketplace_item_daily", "usage_marketplace_item_daily"),
                ("marketplace_item_window", "usage_marketplace_item_window"),
            ):
                deleted = self.conn.execute(f"DELETE FROM {table} RETURNING 1").fetchall()
                out[key] = len(deleted)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return out

    # ------------------------------------------------------------------
    # marketplace usage rollup producer (#728).  Mirrored in UsagePgRepository.
    #
    # Rebuilds usage_tool_daily (legacy) + usage_marketplace_item_daily +
    # usage_marketplace_item_window from usage_events. ``since_day=None``
    # means a FULL rebuild (covers every day with data, not just a rolling
    # window) — callers wanting the cheap incremental refresh pass an
    # explicit cutoff (e.g. today - 7 days, the scheduler-tick behaviour).
    # Attribution (`_attribute_event`) and aggregation (`_aggregate_events`)
    # are pure-Python and shared with the PG sibling via
    # ``services.session_processors.usage_lib`` — only the SQL differs
    # per backend.
    # ------------------------------------------------------------------

    def _curated_flea_lookup(self) -> tuple[set, dict, set]:
        curated_plugins = {r[0] for r in self.conn.execute("SELECT DISTINCT name FROM marketplace_plugins").fetchall()}
        flea_entities = {
            r[0]: r[1]
            for r in self.conn.execute(
                "SELECT synthetic_name, type FROM store_entities WHERE visibility_status='approved'"
            ).fetchall()
        }
        flea_plugins = {synthetic for synthetic, ent_type in flea_entities.items() if ent_type == "plugin"}
        return curated_plugins, flea_entities, flea_plugins

    def _last_30d_due(self) -> bool:
        row = self.conn.execute(
            "SELECT processed_at FROM session_processor_state WHERE processor_name = ? AND session_file = '__rollup__'",
            [_MARKETPLACE_30D_TRACKER],
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

    def _mark_last_30d_refreshed(self) -> None:
        # Pass the timestamp explicitly — DuckDB parses bare current_timestamp
        # in an ON CONFLICT … DO UPDATE SET clause as a column name on the
        # right-hand side, then can't bind it.
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """
            INSERT INTO session_processor_state
                (processor_name, session_file, username, processed_at, items_extracted)
            VALUES (?, '__rollup__', 'system', ?, 0)
            ON CONFLICT (processor_name, session_file) DO UPDATE SET
                processed_at = EXCLUDED.processed_at
            """,
            [_MARKETPLACE_30D_TRACKER, now],
        )

    def _delete_stale_by_anti_join(
        self,
        *,
        table: str,
        column_defs: str,
        key_columns: str,
        where_clause: str,
        where_params: list,
        fresh_keys: list,
    ) -> None:
        """Anti-join DELETE of ``table`` rows whose key isn't in ``fresh_keys``.

        Loads ``fresh_keys`` into a temp table via `executemany` instead of
        inlining them as a `(VALUES ...)` literal — a full rebuild
        (``since_day=None``) can produce tens of thousands of keys, and
        inlining that many bound parameters into one SQL statement risks a
        very large query text / parameter vector. The temp table scales to
        any key count via DuckDB's normal bulk-insert path.
        """
        self.conn.execute("DROP TABLE IF EXISTS _rollup_fresh_keys")
        self.conn.execute(f"CREATE TEMP TABLE _rollup_fresh_keys ({column_defs})")
        if fresh_keys:
            placeholders = ", ".join("?" for _ in key_columns.split(", "))
            self.conn.executemany(f"INSERT INTO _rollup_fresh_keys VALUES ({placeholders})", fresh_keys)
        self.conn.execute(
            f"""
            DELETE FROM {table}
            WHERE {where_clause}
              AND ({key_columns}) NOT IN (SELECT {key_columns} FROM _rollup_fresh_keys)
            """,
            where_params,
        )
        self.conn.execute("DROP TABLE _rollup_fresh_keys")

    def _rebuild_window(
        self, period_label: str, cutoff_day, curated_plugins: set, flea_entities: dict, flea_plugins: set
    ) -> None:
        events = self.conn.execute(
            """
            SELECT
                CAST(occurred_at AS DATE) AS day,
                user_id,
                is_error,
                skill_name,
                subagent_type,
                command_name,
                event_type
            FROM usage_events
            WHERE CAST(occurred_at AS DATE) >= ?
            """,
            [cutoff_day],
        ).fetchall()
        buckets = _aggregate_events(events, curated_plugins, flea_entities, flea_plugins, group_by_day=False)
        # DuckDB 1.5.4: a DELETE of the whole period_label followed by a bulk
        # re-INSERT of overlapping keys in the same transaction can hit an
        # internal PRIMARY KEY index assertion (duplicate-key false positive)
        # that aborts the process uncatchably. INSERT ... ON CONFLICT DO
        # UPDATE never deletes the row, so it never hits that path. Stale
        # keys (no longer present in `buckets`) are removed via an anti-join
        # against the fresh key set, never a key about to be upserted, so
        # this can't delete-then-reinsert the same key either.
        if buckets:
            fresh_keys = list(buckets.keys())  # (source, type_, parent, name)
            self._delete_stale_by_anti_join(
                table="usage_marketplace_item_window",
                column_defs="source VARCHAR, type VARCHAR, parent_plugin VARCHAR, name VARCHAR",
                key_columns="source, type, parent_plugin, name",
                where_clause="period_label = ?",
                where_params=[period_label],
                fresh_keys=fresh_keys,
            )
            self.conn.executemany(
                """
                INSERT INTO usage_marketplace_item_window
                    (period_label, source, type, parent_plugin, name, invocations, distinct_users)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (period_label, source, type, parent_plugin, name) DO UPDATE SET
                    invocations = EXCLUDED.invocations,
                    distinct_users = EXCLUDED.distinct_users,
                    refreshed_at = now()
                """,
                [
                    (period_label, source, type_, parent, name, v["count"], len(v["users"]))
                    for (source, type_, parent, name), v in buckets.items()
                ],
            )
        else:
            self.conn.execute(
                "DELETE FROM usage_marketplace_item_window WHERE period_label = ?",
                [period_label],
            )

    def rebuild_rollups(self, *, since_day: "date | None" = None, force_30d: bool = False) -> None:
        """Rebuild marketplace + legacy tool rollups from usage_events.

        ``since_day=None`` triggers a FULL rebuild: the effective cutoff
        becomes the earliest day present in ``usage_events`` (or today, if
        the table is empty), so every historical day's fact row is
        refreshed — not just the last 7 days. Callers driving the
        steady-state scheduler tick pass an explicit ``since_day`` (today -
        7 days) to keep the cheap incremental behaviour.

        All updates run in a single transaction so a partial failure never
        leaves the rollup set inconsistent.
        """
        try:
            # The PG sibling wraps these same reads in its engine.begin()
            # block; keep them inside the transaction here too so both
            # backends compute the effective cutoff / lookup snapshot under
            # the same isolation.
            self.conn.execute("BEGIN")

            if since_day is None:
                row = self.conn.execute("SELECT MIN(CAST(occurred_at AS DATE)) FROM usage_events").fetchone()
                since_day = row[0] if row and row[0] else datetime.now(timezone.utc).date()

            curated_plugins, flea_entities, flea_plugins = self._curated_flea_lookup()
            do_30d = force_30d or self._last_30d_due()

            # ---- Legacy: usage_tool_daily ----
            # DuckDB 1.5.4: DELETE of this range then bulk INSERT-SELECT of
            # overlapping (day, tool_name, source) keys in the same
            # transaction deterministically hit an internal PRIMARY KEY index
            # assertion ("Failed to append to PRIMARY_usage_tool_daily_*:
            # duplicate key") that aborts the whole process uncatchably —
            # observed in production every ~10-minute tick once ``since_day``'s
            # boundary day held a key that got deleted and reinserted in the
            # same commit. INSERT ... ON CONFLICT DO UPDATE never deletes the
            # row, so it can't hit that path.
            #
            # Stale keys (an event was deleted/corrected so a (day, tool,
            # source) combo no longer has any qualifying events) still need
            # removing to match Postgres semantics — but only keys ABSENT
            # from the freshly computed set, never one that's about to be
            # upserted, so this can never delete-then-reinsert the same key
            # in one transaction (the crash trigger above).
            self.conn.execute(
                """
                DELETE FROM usage_tool_daily
                WHERE day >= ?
                  AND (day, tool_name, source) NOT IN (
                      SELECT CAST(occurred_at AS DATE) AS day, tool_name, source
                      FROM usage_events
                      WHERE CAST(occurred_at AS DATE) >= ?
                        AND tool_name IS NOT NULL
                      GROUP BY day, tool_name, source
                  )
                """,
                [since_day, since_day],
            )
            self.conn.execute(
                """
                INSERT INTO usage_tool_daily
                    (day, tool_name, source, invocations, error_count, distinct_users, distinct_sessions)
                SELECT
                    CAST(occurred_at AS DATE) AS day,
                    tool_name,
                    source,
                    COUNT(*) AS invocations,
                    SUM(CASE WHEN is_error THEN 1 ELSE 0 END) AS error_count,
                    COUNT(DISTINCT username) AS distinct_users,
                    COUNT(DISTINCT session_id) AS distinct_sessions
                FROM usage_events
                WHERE CAST(occurred_at AS DATE) >= ?
                  AND tool_name IS NOT NULL
                GROUP BY day, tool_name, source
                ON CONFLICT (day, tool_name, source) DO UPDATE SET
                    invocations = EXCLUDED.invocations,
                    error_count = EXCLUDED.error_count,
                    distinct_users = EXCLUDED.distinct_users,
                    distinct_sessions = EXCLUDED.distinct_sessions
                """,
                [since_day],
            )

            # ---- New: usage_marketplace_item_daily ----
            daily_events = self.conn.execute(
                """
                SELECT
                    CAST(occurred_at AS DATE) AS day,
                    user_id,
                    is_error,
                    skill_name,
                    subagent_type,
                    command_name,
                    event_type
                FROM usage_events
                WHERE CAST(occurred_at AS DATE) >= ?
                """,
                [since_day],
            ).fetchall()
            daily_buckets = _aggregate_events(
                daily_events, curated_plugins, flea_entities, flea_plugins, group_by_day=True
            )
            # Same DELETE-then-bulk-INSERT-of-overlapping-keys hazard as
            # usage_tool_daily above — see that comment. ON CONFLICT DO
            # UPDATE avoids the delete+reinsert-same-key path entirely. Stale
            # keys (no longer present in daily_buckets — an event was
            # deleted/corrected) are removed via an anti-join against the
            # fresh key set below, never a key about to be upserted, so this
            # can't delete-then-reinsert the same key either.
            if daily_buckets:
                fresh_keys = list(daily_buckets.keys())  # (day, source, type_, parent, name)
                self._delete_stale_by_anti_join(
                    table="usage_marketplace_item_daily",
                    column_defs="day DATE, source VARCHAR, type VARCHAR, parent_plugin VARCHAR, name VARCHAR",
                    key_columns="day, source, type, parent_plugin, name",
                    where_clause="day >= ?",
                    where_params=[since_day],
                    fresh_keys=fresh_keys,
                )
                self.conn.executemany(
                    """
                    INSERT INTO usage_marketplace_item_daily
                        (day, source, type, parent_plugin, name, count, distinct_users, error_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (day, source, type, parent_plugin, name) DO UPDATE SET
                        count = EXCLUDED.count,
                        distinct_users = EXCLUDED.distinct_users,
                        error_count = EXCLUDED.error_count
                    """,
                    [
                        (day, source, type_, parent, name, v["count"], len(v["users"]), v["errors"])
                        for (day, source, type_, parent, name), v in daily_buckets.items()
                    ],
                )
            else:
                self.conn.execute("DELETE FROM usage_marketplace_item_daily WHERE day >= ?", [since_day])

            # ---- usage_marketplace_item_window period_label='last_7d' (full) ----
            cutoff_7d = (datetime.now(timezone.utc) - timedelta(days=7)).date()
            self._rebuild_window("last_7d", cutoff_7d, curated_plugins, flea_entities, flea_plugins)

            # ---- usage_marketplace_item_window period_label='last_30d' (hourly) ----
            if do_30d:
                cutoff_30d = (datetime.now(timezone.utc) - timedelta(days=30)).date()
                self._rebuild_window("last_30d", cutoff_30d, curated_plugins, flea_entities, flea_plugins)
                self._mark_last_30d_refreshed()

            self.conn.execute("COMMIT")
        except Exception:
            try:
                self.conn.execute("ROLLBACK")
            except Exception:
                pass
            raise


def _percentile(values: list[float], p: float) -> float:
    """Pure-Python percentile (linear interpolation)."""
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    idx = p * (n - 1)
    lo, hi = int(idx), min(int(idx) + 1, n - 1)
    frac = idx - lo
    return s[lo] + frac * (s[hi] - s[lo])


def _slow_actions_from_raw(raw_rows: list, limit: int) -> list[dict]:
    """Compute p50/p95/p99/max per action from (action, duration_ms) rows."""
    action_durations: Dict[str, List[float]] = {}
    for action, duration_ms in raw_rows:
        action_durations.setdefault(action, []).append(float(duration_ms))
    out = []
    for action, vals in action_durations.items():
        if len(vals) < 5:
            continue
        out.append(
            {
                "action": action,
                "p50": int(_percentile(vals, 0.5)),
                "p95": int(_percentile(vals, 0.95)),
                "p99": int(_percentile(vals, 0.99)),
                "max_ms": int(max(vals)),
                "n": len(vals),
            }
        )
    return sorted(out, key=lambda x: x["p95"], reverse=True)[:limit]
