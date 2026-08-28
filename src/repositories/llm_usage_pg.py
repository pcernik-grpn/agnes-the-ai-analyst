"""Postgres-backed repository for ``llm_usage`` (v96).

Mirrors ``src/repositories/llm_usage.py`` (the DuckDB impl) on the
``LlmUsageRepository`` public surface. Cross-engine parity is covered by
``tests/db_pg/test_llm_usage_contract.py``.
"""

from __future__ import annotations

from typing import Any, Dict, List

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class LlmUsagePgRepository:
    """Postgres twin of ``LlmUsageRepository``."""

    def __init__(self, engine: Engine):
        self._engine = engine

    def insert_batch(self, rows: List[Dict[str, Any]]) -> None:
        """``caller_user_id`` (C2.4, per-caller attribution) persists here —
        see ``LlmUsageRepository.insert_batch``'s docstring for why the
        DuckDB sibling accepts-but-drops it instead."""
        if not rows:
            return
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO llm_usage
                      (id, agent_id, user_id, caller_user_id, session_id, model,
                       input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens)
                    VALUES
                      (:id, :agent_id, :user_id, :caller_user_id, :session_id, :model,
                       :input_tokens, :output_tokens, :cache_read_tokens, :cache_creation_tokens)
                    """
                ),
                [
                    {
                        "id": row["id"],
                        "agent_id": row.get("agent_id"),
                        "user_id": row.get("user_id"),
                        "caller_user_id": row.get("caller_user_id"),
                        "session_id": row.get("session_id"),
                        "model": row.get("model"),
                        "input_tokens": row.get("input_tokens", 0),
                        "output_tokens": row.get("output_tokens", 0),
                        "cache_read_tokens": row.get("cache_read_tokens", 0),
                        "cache_creation_tokens": row.get("cache_creation_tokens", 0),
                    }
                    for row in rows
                ],
            )

    def month_total_tokens(self, agent_id: str, year_month: str) -> int:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    """
                    SELECT COALESCE(SUM(input_tokens + output_tokens + cache_creation_tokens), 0)
                    FROM llm_usage
                    WHERE agent_id = :agent_id AND to_char(created_at, 'YYYY-MM') = :ym
                    """
                ),
                {"agent_id": agent_id, "ym": year_month},
            ).first()
        return int(row[0]) if row else 0

    def usage_breakdown_for_month(self, agent_id: str, year_month: str) -> Dict[str, int]:
        """See `LlmUsageRepository.usage_breakdown_for_month`'s docstring —
        `total_tokens` mirrors `month_total_tokens` (excludes
        `cache_read_tokens`)."""
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    """
                    SELECT
                        COALESCE(SUM(input_tokens), 0),
                        COALESCE(SUM(output_tokens), 0),
                        COALESCE(SUM(cache_read_tokens), 0),
                        COALESCE(SUM(cache_creation_tokens), 0)
                    FROM llm_usage
                    WHERE agent_id = :agent_id AND to_char(created_at, 'YYYY-MM') = :ym
                    """
                ),
                {"agent_id": agent_id, "ym": year_month},
            ).first()
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
        """See `LlmUsageRepository.usage_breakdown_by_caller_for_month`'s
        docstring — this backend actually groups by the real column, so a
        shared agent (C2.3) run by several callers gets one row per
        caller. `caller_user_id` is `NULL` for a row written before this
        column existed, or by a caller who no longer resolves — never
        silently merged into another caller's bucket."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """
                    SELECT
                        caller_user_id,
                        COALESCE(SUM(input_tokens), 0) AS input_tokens,
                        COALESCE(SUM(output_tokens), 0) AS output_tokens,
                        COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                        COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens
                    FROM llm_usage
                    WHERE agent_id = :agent_id AND to_char(created_at, 'YYYY-MM') = :ym
                    GROUP BY caller_user_id
                    """
                ),
                {"agent_id": agent_id, "ym": year_month},
            ).all()
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
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        """
                        SELECT * FROM llm_usage
                        WHERE agent_id = :agent_id
                        ORDER BY created_at DESC
                        LIMIT :limit
                        """
                    ),
                    {"agent_id": agent_id, "limit": limit},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def list_for_session(self, session_id: str, limit: int = 1000) -> List[Dict[str, Any]]:
        """See `LlmUsageRepository.list_for_session`'s docstring."""
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        """
                        SELECT * FROM llm_usage
                        WHERE session_id = :session_id
                        ORDER BY created_at DESC
                        LIMIT :limit
                        """
                    ),
                    {"session_id": session_id, "limit": limit},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def prune_older_than(self, days: int) -> int:
        """Mirrors ``LlmUsageRepository.prune_older_than``."""
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.text(
                    "DELETE FROM llm_usage "
                    "WHERE created_at < (CURRENT_TIMESTAMP - (:days * INTERVAL '1 day')) "
                    "RETURNING 1"
                ),
                {"days": days},
            ).all()
        return len(rows)
