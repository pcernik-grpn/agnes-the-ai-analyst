"""Postgres repository for ``usage_turns`` — token usage at the assistant-turn
grain, across Claude Code sessions and every chat surface.

PG-ONLY (A3 PG-first ratchet — ``CLAUDE.md`` -> "Dual-backend discipline").
There is deliberately no ``src/repositories/usage_turns.py`` DuckDB sibling:
the table landed after the DuckDB app-state backend was frozen, is registered
``PG``-only in :data:`src.repositories._REGISTRY`, and resolving it on a
DuckDB-backed instance raises
:class:`src.repositories.RequiresPostgresBackend` (translated to a typed
``501`` by ``app/main.py``). See ``docs/migrations.md`` -> "Adding a PG-only
feature (post-A3)". Reach it only through
``src.repositories.usage_turns_repo()``, never by instantiating this class.

This repository is deliberately narrow: it stores turns and answers three
questions about them (one session's rows, one session's cache totals, one
user's per-model totals). Cost is NOT computed here — pricing is a read-time
concern (``src/pricing.py``), so a price change re-prices history instead of
freezing yesterday's rate into a stored column.

Writers call :meth:`insert_batch` from paths that must not fail because
telemetry is unavailable, which is why the write is idempotent rather than
transactional across a session: re-inserting a turn already recorded is a
no-op, so a retry, a re-process, or an overlapping sweep all converge on the
same rows.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Engine

#: Columns :meth:`insert_batch` writes, in statement order. ``extracted_at``
#: is omitted on purpose — it is the row's arrival stamp and belongs to the
#: database's clock, not to a caller that might be replaying old data.
_INSERT_COLUMNS: Sequence[str] = (
    "id",
    "session_file",
    "session_id",
    "user_id",
    "surface",
    "turn_uuid",
    "parent_uuid",
    "model",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "occurred_at",
    "processor_version",
)

#: Defaults for the columns a caller may legitimately not know. They mirror
#: the table's own server defaults so a row written through this repository
#: and a row written by raw SQL are indistinguishable.
_INSERT_DEFAULTS: Dict[str, Any] = {
    "session_id": None,
    "user_id": None,
    "surface": "claude_code",
    "parent_uuid": None,
    "model": None,
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_read_tokens": 0,
    "cache_creation_tokens": 0,
    "occurred_at": None,
    "processor_version": 0,
}

#: Rows per INSERT statement. Postgres caps a statement at 65535 bound
#: parameters; at 14 columns a 500-row chunk uses 7000, comfortably clear of
#: it while still collapsing a long backfill into a handful of round trips.
_CHUNK_ROWS = 500

_TOKEN_COLUMNS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")


def _coerce_dt(value: Any) -> Optional[datetime]:
    """Normalize a timestamp to a ``datetime``, accepting ISO-8601 text.

    The Claude Code processor reads the jsonl event ``timestamp`` verbatim —
    a string, usually with a trailing ``Z`` — while the chat manager passes a
    real ``datetime``. Normalizing in Python rather than letting the driver
    guess keeps both writers on one, testable behavior.
    """
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise TypeError(f"occurred_at must be a datetime or an ISO-8601 string, got {type(value).__name__}")


def _decode_row(row: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(row)
    for key in ("occurred_at", "extracted_at"):
        if out.get(key) is not None:
            out[key] = out[key].isoformat()
    return out


class UsageTurnsPgRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # ------------------------------------------------------------------ write

    def insert_batch(self, rows: Iterable[Dict[str, Any]]) -> int:
        """Store turns; returns how many were actually NEW.

        Keys are column names; only ``session_file`` and ``turn_uuid`` are
        required, everything else falls back to the column default. ``id`` is
        generated when absent.

        ``ON CONFLICT (session_file, turn_uuid) DO NOTHING`` makes this
        idempotent, and the return value is the count of rows the database
        actually inserted — not the length of ``rows`` — so a caller can tell
        "re-processed, nothing new" from "recorded 12 turns" without a second
        query. Duplicates WITHIN one batch collapse the same way.
        """
        materialized = [dict(r) for r in rows]
        if not materialized:
            return 0

        inserted = 0
        columns = ", ".join(_INSERT_COLUMNS)
        with self._engine.begin() as conn:
            for start in range(0, len(materialized), _CHUNK_ROWS):
                chunk = materialized[start : start + _CHUNK_ROWS]
                params: Dict[str, Any] = {}
                tuples: List[str] = []
                for i, row in enumerate(chunk):
                    if not row.get("session_file") or not row.get("turn_uuid"):
                        raise ValueError("usage_turns rows require both 'session_file' and 'turn_uuid'")
                    values = {**_INSERT_DEFAULTS, **row}
                    values["id"] = row.get("id") or str(uuid4())
                    values["occurred_at"] = _coerce_dt(values.get("occurred_at"))
                    tuples.append("(" + ", ".join(f":{c}_{i}" for c in _INSERT_COLUMNS) + ")")
                    for column in _INSERT_COLUMNS:
                        params[f"{column}_{i}"] = values[column]
                result = conn.execute(
                    sa.text(
                        f"INSERT INTO usage_turns ({columns}) VALUES {', '.join(tuples)} "
                        "ON CONFLICT (session_file, turn_uuid) DO NOTHING RETURNING id"
                    ),
                    params,
                )
                inserted += len(result.fetchall())
        return inserted

    # ------------------------------------------------------------------- read

    def list_for_session_file(self, session_file: str) -> List[Dict[str, Any]]:
        """Every recorded turn of one session, oldest first.

        Turns with no ``occurred_at`` sort last (they cannot be placed in the
        sequence); ``turn_uuid`` breaks ties so the order is stable across
        calls rather than dependent on physical row order.
        """
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT * FROM usage_turns WHERE session_file = :session_file "
                        "ORDER BY occurred_at ASC NULLS LAST, turn_uuid ASC"
                    ),
                    {"session_file": session_file},
                )
                .mappings()
                .all()
            )
        return [_decode_row(dict(r)) for r in rows]

    def cache_totals_for_session_file(self, session_file: str) -> Dict[str, int]:
        """``{"cache_read_tokens": int, "cache_creation_tokens": int}`` for one
        session.

        A session with no turns reports measured zeros, not ``None`` — the
        chat-summary overlay adds this to a summary row for every session it
        renders, and a ``None`` there would poison the addition instead of
        saying "nothing cached".
        """
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text(
                        "SELECT COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens, "
                        "       COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens "
                        "FROM usage_turns WHERE session_file = :session_file"
                    ),
                    {"session_file": session_file},
                )
                .mappings()
                .one()
            )
        return {
            "cache_read_tokens": int(row["cache_read_tokens"]),
            "cache_creation_tokens": int(row["cache_creation_tokens"]),
        }

    def totals_for_user(self, user_id: Optional[str], since_days: Optional[int] = None) -> Dict[str, Any]:
        """Token totals plus a per-model breakdown.

        ``user_id=None`` means instance-wide (admin surfaces only — the
        caller is responsible for having established that). ``since_days``
        windows on ``occurred_at``; a turn with no timestamp cannot be placed
        in time, so a windowed read drops it rather than counting it as
        "now". Both are visible in the all-time read.

        Returns::

            {"input_tokens": …, "output_tokens": …, "cache_read_tokens": …,
             "cache_creation_tokens": …, "turns": …,
             "by_model": [{"model": …, <the four token sums>, "turns": …}, …]}

        ``by_model`` is ordered by total tokens descending, so the row that
        dominates the bill reads first; a turn whose model was never recorded
        groups under ``model=None`` and still counts in the headline totals.
        """
        where = ["TRUE"]
        params: Dict[str, Any] = {}
        if user_id is not None:
            where.append("user_id = :user_id")
            params["user_id"] = user_id
        if since_days is not None:
            where.append("occurred_at >= :since")
            params["since"] = datetime.now(timezone.utc) - timedelta(days=since_days)
        clause = " AND ".join(where)

        sums = ", ".join(f"COALESCE(SUM({c}), 0) AS {c}" for c in _TOKEN_COLUMNS)
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        f"SELECT model, {sums}, COUNT(*) AS turns FROM usage_turns "
                        f"WHERE {clause} GROUP BY model "
                        "ORDER BY (COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0) "
                        "+ COALESCE(SUM(cache_read_tokens), 0) + COALESCE(SUM(cache_creation_tokens), 0)) DESC, "
                        "model ASC NULLS LAST"
                    ),
                    params,
                )
                .mappings()
                .all()
            )

        by_model = [
            {"model": r["model"], **{c: int(r[c]) for c in _TOKEN_COLUMNS}, "turns": int(r["turns"])} for r in rows
        ]
        totals: Dict[str, Any] = {c: sum(m[c] for m in by_model) for c in _TOKEN_COLUMNS}
        totals["turns"] = sum(m["turns"] for m in by_model)
        totals["by_model"] = by_model
        return totals
