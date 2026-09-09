"""Repository for the per-agent private memory notebook (v98, agent-api V1c).

A memory row moves through ``pending -> active -> archived``. Writes an
agent makes autonomously during a run land as ``pending`` and need the
owner's ``approve()`` before they show up in ``list_active`` (the set an
agent's own future runs read back); writes the owner makes directly (e.g.
via the builder UI) can be created already ``active``. ``count_recent`` and
``count_pending`` back the write rate-limit / total-pending cap enforced by
a later task (agent-api V1c Task 4, correction C3).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import duckdb


class AgentMemoriesRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def _row_to_dict(self, row) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, row))

    def _rows_to_dicts(self, rows) -> List[Dict[str, Any]]:
        if not rows:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, r)) for r in rows]

    def create(
        self,
        id: str,
        agent_id: str,
        owner_user_id: str,
        content: str,
        source_session_id: Optional[str],
        status: str = "pending",
        source_turn_id: Optional[str] = None,
        source_message_id: Optional[str] = None,
    ) -> None:
        # `source_turn_id`/`source_message_id` (migration 0113, design
        # 2026-09-08 §3.5) are Postgres-only — the frozen DuckDB app-state
        # backend has no matching columns (A3), so both are accepted and
        # silently dropped here rather than refused, the same accept-and-drop
        # pattern as the chat_messages cache columns.
        now = datetime.now(timezone.utc)
        activated_at = now if status == "active" else None
        self.conn.execute(
            """INSERT INTO agent_memories
            (id, agent_id, owner_user_id, content, source_session_id, status, created_at, activated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [id, agent_id, owner_user_id, content, source_session_id, status, now, activated_at],
        )

    def get(self, id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM agent_memories WHERE id = ?", [id]).fetchone()
        return self._row_to_dict(row)

    def list_active(self, agent_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT * FROM agent_memories
            WHERE agent_id = ? AND status = 'active' AND archived_at IS NULL
            ORDER BY created_at DESC""",
            [agent_id],
        ).fetchall()
        return self._rows_to_dicts(rows)

    def list_for_agent(self, agent_id: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if status is not None:
            rows = self.conn.execute(
                """SELECT * FROM agent_memories
                WHERE agent_id = ? AND status = ?
                ORDER BY created_at DESC""",
                [agent_id, status],
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM agent_memories WHERE agent_id = ? ORDER BY created_at DESC",
                [agent_id],
            ).fetchall()
        return self._rows_to_dicts(rows)

    def approve(self, id: str) -> None:
        """Flip a ``pending`` memory to ``active`` and stamp ``activated_at``.
        No-op if the row isn't currently ``pending`` (already-active or
        archived rows are left alone)."""
        self.conn.execute(
            """UPDATE agent_memories
            SET status = 'active', activated_at = ?
            WHERE id = ? AND status = 'pending'""",
            [datetime.now(timezone.utc), id],
        )

    def archive(self, id: str) -> None:
        self.conn.execute(
            "UPDATE agent_memories SET status = 'archived', archived_at = ? WHERE id = ?",
            [datetime.now(timezone.utc), id],
        )

    def delete(self, id: str) -> None:
        self.conn.execute("DELETE FROM agent_memories WHERE id = ?", [id])

    def delete_for_agent(self, agent_id: str) -> None:
        """Delete every memory row for `agent_id` — the cascade leg of
        `DELETE /api/v1/agents/{id}` (C5, agent-api V1c). No object-store
        blobs to scrub: memories are plain text rows, unlike artifacts."""
        self.conn.execute("DELETE FROM agent_memories WHERE agent_id = ?", [agent_id])

    def count_recent(self, agent_id: str, since: datetime) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM agent_memories WHERE agent_id = ? AND created_at > ?",
            [agent_id, since],
        ).fetchone()
        return int(row[0]) if row else 0

    def count_pending(self, agent_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM agent_memories WHERE agent_id = ? AND status = 'pending'",
            [agent_id],
        ).fetchone()
        return int(row[0]) if row else 0

    def list_for_sessions(self, session_ids: List[str]) -> Dict[str, List[Dict[str, Any]]]:
        """Every memory write attributed to ``session_ids`` (``source_session_id``),
        grouped by session and ordered oldest-first -- the conversation-corpus
        export's bulk read (design 2026-09-08 §3.12), one query for a whole
        page of sessions rather than one lookup per memory. Mirrors
        ``AgentMemoriesPgRepository.list_for_sessions``.
        """
        if not session_ids:
            return {}
        rows = self.conn.execute(
            "SELECT * FROM agent_memories WHERE source_session_id = ANY(?) "
            "ORDER BY source_session_id ASC, created_at ASC",
            [list(session_ids)],
        ).fetchall()
        out: Dict[str, List[Dict[str, Any]]] = {}
        for d in self._rows_to_dicts(rows):
            out.setdefault(d["source_session_id"], []).append(d)
        return out
