"""Postgres repository for ``chat_message_feedback`` — the thumbs up/down
quality signal on a chat turn (design 2026-09-08, §3.5).

PG-ONLY (A3 PG-first ratchet — ``CLAUDE.md`` -> "Dual-backend discipline").
There is deliberately no ``src/repositories/chat_message_feedback.py`` DuckDB
sibling: the table landed after the DuckDB app-state backend was frozen, is
registered ``PG``-only in :data:`src.repositories._REGISTRY`, and resolving
it on a DuckDB-backed instance raises
:class:`src.repositories.RequiresPostgresBackend` (translated to a typed
``501`` by ``app/main.py``). See ``docs/migrations.md`` -> "Adding a PG-only
feature (post-A3)". Reach it only through
``src.repositories.chat_message_feedback_repo()``, never by instantiating
this class.

One row per ``(turn_id, user_id)`` — a second submit on the same turn
UPDATEs it (``ON CONFLICT``) rather than piling up a second opinion, which
is also what makes "the user changed their mind" show up as one current
verdict instead of a history to reconcile.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class ChatMessageFeedbackPgRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def upsert(
        self,
        *,
        session_id: str,
        turn_id: str,
        user_id: str,
        verdict: str,
        comment: str | None = None,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        """File (or update) one verdict for ``(turn_id, user_id)``.

        A first submit inserts a fresh row and stamps ``id``; a second
        submit for the same turn/user pair UPDATEs verdict/comment/
        message_id/``updated_at`` in place and keeps the ORIGINAL ``id`` and
        ``created_at`` — the row's identity is the (turn, user) pair, not
        this call.
        """
        new_id = str(uuid4())
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO chat_message_feedback
                      (id, session_id, turn_id, message_id, user_id, verdict, comment, created_at, updated_at)
                    VALUES
                      (:id, :session_id, :turn_id, :message_id, :user_id, :verdict, :comment,
                       current_timestamp, current_timestamp)
                    ON CONFLICT (turn_id, user_id) DO UPDATE
                      SET verdict = EXCLUDED.verdict,
                          comment = EXCLUDED.comment,
                          message_id = EXCLUDED.message_id,
                          updated_at = current_timestamp
                    """
                ),
                {
                    "id": new_id,
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "message_id": message_id,
                    "user_id": user_id,
                    "verdict": verdict,
                    "comment": comment,
                },
            )
        row = self.get(turn_id, user_id)
        assert row is not None  # just inserted-or-updated in the same engine
        return row

    def get(self, turn_id: str, user_id: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM chat_message_feedback WHERE turn_id = :turn_id AND user_id = :user_id"),
                    {"turn_id": turn_id, "user_id": user_id},
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def list_feedback(
        self,
        *,
        since: datetime | None = None,
        verdict: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """The feedback queue, newest first; ``since``/``verdict`` narrow it."""
        clauses: list[str] = []
        params: dict[str, Any] = {"limit": limit}
        if since is not None:
            clauses.append("created_at >= :since")
            params["since"] = since
        if verdict is not None:
            clauses.append("verdict = :verdict")
            params["verdict"] = verdict
        where = " AND ".join(clauses) if clauses else "TRUE"
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(f"SELECT * FROM chat_message_feedback WHERE {where} ORDER BY created_at DESC LIMIT :limit"),
                    params,
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def prune_older_than(self, days: int) -> int:
        """Not wired to the retention sweep (kept for symmetry with the
        other trails, in case an operator wants it later)."""
        cutoff = datetime.now(UTC) - timedelta(days=days)
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text("DELETE FROM chat_message_feedback WHERE created_at < :cutoff"), {"cutoff": cutoff}
            )
        return result.rowcount or 0

    def list_for_sessions(self, session_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        """Every feedback row for ``session_ids``, grouped by session and
        ordered oldest-first -- the conversation-corpus export's bulk read
        (design 2026-09-08 §3.12), one query for a whole page of sessions.
        """
        if not session_ids:
            return {}
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT * FROM chat_message_feedback "
                        "WHERE session_id = ANY(:session_ids) ORDER BY session_id ASC, created_at ASC"
                    ),
                    {"session_ids": list(session_ids)},
                )
                .mappings()
                .all()
            )
        out: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            out.setdefault(r["session_id"], []).append(dict(r))
        return out
