"""Repository for the ``llm_usage`` per-call token accounting ledger (v96).

The broker (Task 8) writes batches of usage rows here; the API (Task 9) reads
month-to-date totals and recent rows for a given agent.
"""

from __future__ import annotations

from typing import Any, Dict, List

import duckdb


class LlmUsageRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def _rows_to_dicts(self, rows) -> List[Dict[str, Any]]:
        if not rows:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, r)) for r in rows]

    def insert_batch(self, rows: List[Dict[str, Any]]) -> None:
        """``caller_user_id`` (C2.4, per-caller attribution) is accepted in
        each row for call-site symmetry with the PG sibling but silently
        dropped — DuckDB has no column to persist it into (PG-only under
        the A3 ratchet, ``migrations/versions/
        0074_llm_usage_caller_user_id.py``), same no-op pattern as
        ``agents.py``'s ``set_scope(granted_by=...)``."""
        if not rows:
            return
        self.conn.executemany(
            """INSERT INTO llm_usage
            (id, agent_id, user_id, session_id, model,
             input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                [
                    row["id"],
                    row.get("agent_id"),
                    row.get("user_id"),
                    row.get("session_id"),
                    row.get("model"),
                    row.get("input_tokens", 0),
                    row.get("output_tokens", 0),
                    row.get("cache_read_tokens", 0),
                    row.get("cache_creation_tokens", 0),
                ]
                for row in rows
            ],
        )

    def month_total_tokens(self, agent_id: str, year_month: str) -> int:
        row = self.conn.execute(
            """SELECT COALESCE(SUM(input_tokens + output_tokens + cache_creation_tokens), 0)
            FROM llm_usage
            WHERE agent_id = ? AND strftime(created_at, '%Y-%m') = ?""",
            [agent_id, year_month],
        ).fetchone()
        return int(row[0]) if row else 0

    def usage_breakdown_for_month(self, agent_id: str, year_month: str) -> Dict[str, int]:
        """Per-field token sums for one agent/month (Task 8, `GET
        /api/v1/agents/{slug}/usage`) — an Anthropic-shaped breakdown, not
        just the single scalar `month_total_tokens` returns.

        `total_tokens` deliberately mirrors `month_total_tokens`'s own
        definition (``input + output + cache_creation``, EXCLUDING
        `cache_read_tokens`) rather than summing all four columns — this is
        the same quantity `app.api.broker_agent_policy.check_budget` compares
        against `token_budget_monthly`, so a caller can compute
        `budget_limit - total_tokens` and get a number that actually matches
        when `429 budget_exhausted` would fire. `cache_read_tokens` is still
        reported (informational — cached reads are heavily discounted and
        excluded from budget accounting), just not folded into the total.
        """
        row = self.conn.execute(
            """SELECT
                COALESCE(SUM(input_tokens), 0),
                COALESCE(SUM(output_tokens), 0),
                COALESCE(SUM(cache_read_tokens), 0),
                COALESCE(SUM(cache_creation_tokens), 0)
            FROM llm_usage
            WHERE agent_id = ? AND strftime(created_at, '%Y-%m') = ?""",
            [agent_id, year_month],
        ).fetchone()
        input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens = (
            (int(v) for v in row) if row else (0, 0, 0, 0)
        )
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_creation_tokens": cache_creation_tokens,
            "total_tokens": input_tokens + output_tokens + cache_creation_tokens,
        }

    def usage_breakdown_by_caller_for_month(self, agent_id: str, year_month: str) -> List[Dict[str, Any]]:
        """Per-caller token sums for one agent/month (C2.4, per-caller usage
        attribution) — one row per distinct `caller_user_id` that incurred
        usage against this agent, same field shape as
        `usage_breakdown_for_month`.

        DuckDB has no `caller_user_id` column (PG-only under the A3
        ratchet — see `insert_batch`'s docstring), so every row this
        backend ever wrote is honestly unattributed: this groups under a
        single literal `NULL` bucket covering the agent's WHOLE month
        total, rather than pretending to distinguish callers it never
        recorded. Postgres's sibling groups by the real column and returns
        one row per caller (`None` for any row written before this
        feature, or by a caller who no longer resolves). Empty list when
        the agent has no usage rows that month, on either backend.
        """
        row = self.conn.execute(
            """SELECT
                NULL AS caller_user_id,
                COALESCE(SUM(input_tokens), 0),
                COALESCE(SUM(output_tokens), 0),
                COALESCE(SUM(cache_read_tokens), 0),
                COALESCE(SUM(cache_creation_tokens), 0)
            FROM llm_usage
            WHERE agent_id = ? AND strftime(created_at, '%Y-%m') = ?
            HAVING COUNT(*) > 0""",
            [agent_id, year_month],
        ).fetchall()
        return self._breakdown_rows_to_dicts(row)

    @staticmethod
    def _breakdown_rows_to_dicts(rows) -> List[Dict[str, Any]]:
        result = []
        for caller_user_id, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens in rows:
            input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens = (
                int(input_tokens),
                int(output_tokens),
                int(cache_read_tokens),
                int(cache_creation_tokens),
            )
            result.append(
                {
                    "caller_user_id": caller_user_id,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_read_tokens": cache_read_tokens,
                    "cache_creation_tokens": cache_creation_tokens,
                    "total_tokens": input_tokens + output_tokens + cache_creation_tokens,
                }
            )
        return result

    def list_for_agent(self, agent_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT * FROM llm_usage
            WHERE agent_id = ?
            ORDER BY created_at DESC
            LIMIT ?""",
            [agent_id, limit],
        ).fetchall()
        return self._rows_to_dicts(rows)

    def list_for_session(self, session_id: str, limit: int = 1000) -> List[Dict[str, Any]]:
        """All `llm_usage` rows for one chat session, filtered in SQL (review
        carry-over, Task 9) — `app.chat.agent_usage.usage_for_session` used
        to call `list_for_agent()` and filter by `session_id` in Python over
        just that agent's most recent `limit` rows, which silently
        undercounts once an agent has more than `limit` rows total (a busy
        agent's OLDER session falls out of the scan window even though its
        own rows are still in the table). `session_id` values are globally
        unique (minted by `ChatManager.create_session`), so filtering by it
        alone in SQL is both exact and cheap — no `agent_id` needed."""
        rows = self.conn.execute(
            """SELECT * FROM llm_usage
            WHERE session_id = ?
            ORDER BY created_at DESC
            LIMIT ?""",
            [session_id, limit],
        ).fetchall()
        return self._rows_to_dicts(rows)

    def prune_older_than(self, days: int) -> int:
        """Delete ``llm_usage`` rows older than ``days``, by ``created_at``.
        Returns the deleted-row count.

        The caller (``src/audit_retention.py``) owns the "days<=0 = keep
        forever, skip entirely" short-circuit — this method always executes
        the DELETE it's given, no matter the value (mirrors
        ``AuditRepository.prune_older_than``)."""
        rows = self.conn.execute(
            "DELETE FROM llm_usage WHERE created_at < (CURRENT_TIMESTAMP - INTERVAL (?) DAY) RETURNING id",
            [days],
        ).fetchall()
        return len(rows)
