"""Postgres repository for ``llm_calls`` — the LLM observability ledger.

PG-ONLY (A3 PG-first ratchet — ``CLAUDE.md`` -> "Dual-backend discipline").
There is deliberately no ``src/repositories/llm_calls.py`` DuckDB sibling:
the table landed after the DuckDB app-state backend was frozen, is
registered ``PG``-only in :data:`src.repositories._REGISTRY`, and resolving
it on a DuckDB-backed instance raises
:class:`src.repositories.RequiresPostgresBackend` (translated to a typed
``501`` by ``app/main.py``). See ``docs/migrations.md`` -> "Adding a PG-only
feature (post-A3)". Reach it only through
``src.repositories.llm_calls_repo()``, never by instantiating this class.

Writers (``src/observability/llm_ledger.py``, ``app/api/broker_agent_policy.
py::UsageAccumulator``) call :meth:`insert_batch` from paths that must not
fail because telemetry is unavailable — the write is idempotent on ``id``
rather than transactional, so a retried flush or a duplicate emission both
converge on the same row instead of double-counting a call.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

#: Columns :meth:`insert_batch` writes, in statement order — exactly
#: ``LlmCallRecord.to_row()``'s keys (``src/observability/llm_record.py``),
#: one to one.
_COLUMNS: Sequence[str] = (
    "id",
    "created_at",
    "kind",
    "workload",
    "purpose",
    "session_id",
    "turn_id",
    "user_id",
    "agent_id",
    "job_id",
    "subject_id",
    "trace_id",
    "span_id",
    "provider",
    "upstream",
    "model_requested",
    "model_response",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "cost_usd",
    "priced_as",
    "latency_ms",
    "status",
    "error_type",
    "http_status",
    "prompt_chars",
    "completion_chars",
    "stop_reason",
    "stream_complete",
    "response_truncated",
)

#: Rows per INSERT statement — see ``usage_turns_pg.py``'s identical comment
#: on the 65535-bound-parameter Postgres cap; 31 columns * 500 rows keeps
#: comfortably clear of it.
_CHUNK_ROWS = 500

_TOKEN_COLUMNS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")

#: ``by`` -> the SQL group-by expression. A fixed lookup table, never a
#: user-supplied string spliced into SQL: an unknown ``by`` raises
#: ``ValueError`` before any query is built.
_GROUP_EXPR: dict[str, str] = {
    "workload": "workload",
    "agent": "agent_id",
    "user": "user_id",
    "model": "COALESCE(model_response, model_requested)",
    "purpose": "purpose",
}


def _decode_priced_as(value: Any) -> dict[str, Any] | None:
    # JSONB deserializes to a Python dict already under psycopg; tolerate a
    # str too (the driver-dependent fallback the chat-messages repo takes).
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
    return value


def _decode_row(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    if out.get("created_at") is not None:
        out["created_at"] = out["created_at"].isoformat()
    out["priced_as"] = _decode_priced_as(out.get("priced_as"))
    if out.get("cost_usd") is not None:
        out["cost_usd"] = float(out["cost_usd"])
    return out


class LlmCallsPgRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # ------------------------------------------------------------------ write

    def insert_batch(self, rows: Iterable[dict[str, Any]]) -> int:
        """Store call rows; returns how many were actually NEW.

        Keys are column names (``LlmCallRecord.to_row()``'s shape); every
        row must carry all of them — the writers always pass a full
        ``to_row()`` dict, so no per-key defaulting happens here.
        ``priced_as`` is written as JSONB. ``ON CONFLICT (id) DO NOTHING``
        makes a retried flush or a duplicate emission a no-op instead of a
        double-counted call, and the return value is the count the database
        actually inserted, not ``len(rows)``.
        """
        materialized = [dict(r) for r in rows]
        if not materialized:
            return 0

        inserted = 0
        columns = ", ".join(_COLUMNS)
        with self._engine.begin() as conn:
            for start in range(0, len(materialized), _CHUNK_ROWS):
                chunk = materialized[start : start + _CHUNK_ROWS]
                params: dict[str, Any] = {}
                tuples: list[str] = []
                for i, row in enumerate(chunk):
                    placeholders = []
                    for column in _COLUMNS:
                        key = f"{column}_{i}"
                        value = row.get(column)
                        if column == "priced_as":
                            placeholders.append(f"CAST(:{key} AS JSONB)")
                            value = json.dumps(value) if value is not None else None
                        else:
                            placeholders.append(f":{key}")
                        params[key] = value
                    tuples.append("(" + ", ".join(placeholders) + ")")
                result = conn.execute(
                    sa.text(
                        f"INSERT INTO llm_calls ({columns}) VALUES {', '.join(tuples)} "
                        "ON CONFLICT (id) DO NOTHING RETURNING id"
                    ),
                    params,
                )
                inserted += len(result.fetchall())
        return inserted

    # ------------------------------------------------------------------- read

    def list_calls(
        self,
        *,
        session_id: str | None = None,
        turn_id: str | None = None,
        job_id: str | None = None,
        user_id: str | None = None,
        limit: int = 100,
        before: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Detail rows for one turn/session/job/user, newest first.

        ``before`` is the pagination cursor: pass the previous page's last
        ``created_at`` (a ``datetime``, not the ISO string this method
        returns) to fetch the next older page.
        """
        clauses: list[str] = []
        params: dict[str, Any] = {"limit": limit}
        if session_id is not None:
            clauses.append("session_id = :session_id")
            params["session_id"] = session_id
        if turn_id is not None:
            clauses.append("turn_id = :turn_id")
            params["turn_id"] = turn_id
        if job_id is not None:
            clauses.append("job_id = :job_id")
            params["job_id"] = job_id
        if user_id is not None:
            clauses.append("user_id = :user_id")
            params["user_id"] = user_id
        if before is not None:
            clauses.append("created_at < :before")
            params["before"] = before
        where = " AND ".join(clauses) if clauses else "TRUE"
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(f"SELECT * FROM llm_calls WHERE {where} ORDER BY created_at DESC LIMIT :limit"),
                    params,
                )
                .mappings()
                .all()
            )
        return [_decode_row(dict(r)) for r in rows]

    def cost_summary(self, *, since: datetime | None, by: str) -> list[dict[str, Any]]:
        """Grouped totals for the ``llm-cost`` read surface.

        ``by`` must be one of :data:`_GROUP_EXPR`'s keys; anything else
        raises ``ValueError`` before touching the database — the group
        expression is looked up in a fixed dict, never spliced from the
        caller's string, so there is no injection surface here regardless.
        Rows are ordered by ``cost_usd`` descending so the biggest spender
        under the grouping reads first; ``key`` may be ``None`` (an
        ungrouped call, e.g. no ``agent_id``).
        """
        if by not in _GROUP_EXPR:
            raise ValueError(f"unknown cost_summary group: {by!r} (expected one of {sorted(_GROUP_EXPR)})")
        expr = _GROUP_EXPR[by]

        where = "TRUE"
        params: dict[str, Any] = {}
        if since is not None:
            where = "created_at >= :since"
            params["since"] = since

        sql = (
            f"SELECT {expr} AS key, "
            "COUNT(*) AS calls, "
            "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
            "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
            "COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens, "
            "COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens, "
            "COALESCE(SUM(cost_usd), 0) AS cost_usd, "
            f"array_remove(array_agg(DISTINCT COALESCE(model_response, model_requested)), NULL) AS priced_models "
            f"FROM llm_calls WHERE {where} GROUP BY {expr} ORDER BY cost_usd DESC"
        )
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(sql), params).mappings().all()

        out: list[dict[str, Any]] = []
        for r in rows:
            row = dict(r)
            row["cost_usd"] = float(row["cost_usd"])
            for c in _TOKEN_COLUMNS:
                row[c] = int(row[c])
            row["calls"] = int(row["calls"])
            row["priced_models"] = sorted(row.get("priced_models") or [])
            out.append(row)
        return out

    def prune_older_than(self, days: int) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        with self._engine.begin() as conn:
            result = conn.execute(sa.text("DELETE FROM llm_calls WHERE created_at < :cutoff"), {"cutoff": cutoff})
        return result.rowcount or 0

    def count(self) -> int:
        with self._engine.connect() as conn:
            return int(conn.execute(sa.text("SELECT COUNT(*) FROM llm_calls")).scalar() or 0)
