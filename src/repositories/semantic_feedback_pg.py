"""Postgres repository for ``semantic_feedback``.

PG-ONLY (A3 PG-first ratchet — ``CLAUDE.md`` -> "Dual-backend discipline").
There is deliberately no ``src/repositories/semantic_feedback.py`` DuckDB
sibling: the table landed after the DuckDB app-state backend was frozen, is
registered ``PG``-only in :data:`src.repositories._REGISTRY`, and resolving it
on a DuckDB-backed instance raises
:class:`src.repositories.RequiresPostgresBackend` (translated to a typed
``501`` by ``app/main.py``). See ``docs/migrations.md`` -> "Adding a PG-only
feature (post-A3)".

The queue this backs is one report per "that answer looked wrong" — filed by
whoever read the answer (analyst, admin, or the agent itself), worked by an
admin. ``"sql"`` is quoted in every statement below: it is a keyword in the
SQL standard, and an unquoted column of that name is exactly the sort of thing
that works on one server version and stops parsing on the next.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class SemanticFeedbackPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def create(
        self,
        *,
        question: str,
        sql: Optional[str] = None,
        metric_id: Optional[str] = None,
        model_content_hash: Optional[str] = None,
        comment: Optional[str] = None,
        created_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """File one report; returns the stored row, always ``status='open'``.

        Only ``question`` is required. A reporter who has the question but not
        the SQL — or an agent that noticed a concept is undefined and so has no
        SQL at all — must still be able to file: the alternative is a channel
        that only accepts reports from people who already know the answer.
        """
        feedback_id = f"sfb_{uuid4().hex[:16]}"
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO semantic_feedback
                      (id, question, "sql", metric_id, model_content_hash, comment,
                       status, created_by, created_at)
                    VALUES
                      (:id, :question, :sql, :metric_id, :model_content_hash, :comment,
                       'open', :created_by, current_timestamp)
                    """
                ),
                {
                    "id": feedback_id,
                    "question": question,
                    "sql": sql,
                    "metric_id": metric_id,
                    "model_content_hash": model_content_hash,
                    "comment": comment,
                    "created_by": created_by,
                },
            )
        row = self.get(feedback_id)
        assert row is not None  # just inserted in the same engine
        return row

    def get(self, feedback_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM semantic_feedback WHERE id = :id"),
                    {"id": feedback_id},
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def list(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """The queue, newest first; ``status`` narrows it (``None`` = all).

        ``id`` breaks the ordering tie so two reports filed in the same
        transaction-clock tick still come back in a stable order — a queue that
        reshuffles between two page loads reads as if rows appeared and
        vanished.
        """
        clause = "WHERE status = :status" if status else ""
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(f"SELECT * FROM semantic_feedback {clause} ORDER BY created_at DESC, id DESC"),
                    {"status": status} if status else {},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def resolve(
        self,
        feedback_id: str,
        *,
        resolved_by: str,
        resolution_note: Optional[str] = None,
    ) -> bool:
        """Close one report. ``False`` when it does not exist OR is already
        resolved.

        A GUARDED transition (``status != 'resolved'`` in the WHERE), not a
        blind UPDATE: two admins working the queue at once would otherwise
        both "succeed", and the second write would erase who actually fixed it
        and what they did about it. The endpoint turns ``False`` into 404 or
        409 by first asking :meth:`get` whether the row exists at all.
        """
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text(
                    """
                    UPDATE semantic_feedback
                       SET status = 'resolved',
                           resolved_by = :resolved_by,
                           resolution_note = :resolution_note,
                           resolved_at = current_timestamp
                     WHERE id = :id
                       AND status <> 'resolved'
                    """
                ),
                {
                    "id": feedback_id,
                    "resolved_by": resolved_by,
                    "resolution_note": resolution_note,
                },
            )
        return bool(result.rowcount)
