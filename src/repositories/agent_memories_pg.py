"""Postgres-backed repository for ``agent_memories`` (v98, agent-api V1c).

Mirrors ``src/repositories/agent_memories.py`` (the DuckDB impl) on the
``AgentMemoriesRepository`` public surface. Cross-engine parity is covered
by ``tests/db_pg/test_agent_memories_contract.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class AgentMemoriesPgRepository:
    """Postgres twin of ``AgentMemoriesRepository``."""

    def __init__(self, engine: Engine):
        self._engine = engine

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
        now = datetime.now(timezone.utc)
        activated_at = now if status == "active" else None
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO agent_memories
                      (id, agent_id, owner_user_id, content, source_session_id, status, created_at, activated_at,
                       source_turn_id, source_message_id)
                    VALUES
                      (:id, :agent_id, :owner_user_id, :content, :source_session_id, :status, :created_at, :activated_at,
                       :source_turn_id, :source_message_id)
                    """
                ),
                {
                    "id": id,
                    "agent_id": agent_id,
                    "owner_user_id": owner_user_id,
                    "content": content,
                    "source_session_id": source_session_id,
                    "status": status,
                    "created_at": now,
                    "activated_at": activated_at,
                    "source_turn_id": source_turn_id,
                    "source_message_id": source_message_id,
                },
            )

    def get(self, id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(sa.text("SELECT * FROM agent_memories WHERE id = :id"), {"id": id}).mappings().first()
        return dict(row) if row else None

    def list_active(self, agent_id: str) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        """
                        SELECT * FROM agent_memories
                        WHERE agent_id = :agent_id AND status = 'active' AND archived_at IS NULL
                        ORDER BY created_at DESC
                        """
                    ),
                    {"agent_id": agent_id},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def list_for_agent(self, agent_id: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            if status is not None:
                rows = (
                    conn.execute(
                        sa.text(
                            """
                            SELECT * FROM agent_memories
                            WHERE agent_id = :agent_id AND status = :status
                            ORDER BY created_at DESC
                            """
                        ),
                        {"agent_id": agent_id, "status": status},
                    )
                    .mappings()
                    .all()
                )
            else:
                rows = (
                    conn.execute(
                        sa.text("SELECT * FROM agent_memories WHERE agent_id = :agent_id ORDER BY created_at DESC"),
                        {"agent_id": agent_id},
                    )
                    .mappings()
                    .all()
                )
        return [dict(r) for r in rows]

    def approve(self, id: str) -> None:
        """See `AgentMemoriesRepository.approve`'s docstring."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    UPDATE agent_memories
                    SET status = 'active', activated_at = :activated_at
                    WHERE id = :id AND status = 'pending'
                    """
                ),
                {"activated_at": datetime.now(timezone.utc), "id": id},
            )

    def archive(self, id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE agent_memories SET status = 'archived', archived_at = :archived_at WHERE id = :id"),
                {"archived_at": datetime.now(timezone.utc), "id": id},
            )

    def delete(self, id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM agent_memories WHERE id = :id"), {"id": id})

    def delete_for_agent(self, agent_id: str) -> None:
        """See `AgentMemoriesRepository.delete_for_agent`'s docstring."""
        with self._engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM agent_memories WHERE agent_id = :agent_id"), {"agent_id": agent_id})

    def count_recent(self, agent_id: str, since: datetime) -> int:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT COUNT(*) FROM agent_memories WHERE agent_id = :agent_id AND created_at > :since"),
                {"agent_id": agent_id, "since": since},
            ).first()
        return int(row[0]) if row else 0

    def count_pending(self, agent_id: str) -> int:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT COUNT(*) FROM agent_memories WHERE agent_id = :agent_id AND status = 'pending'"),
                {"agent_id": agent_id},
            ).first()
        return int(row[0]) if row else 0
