"""Postgres-backed chat-session-participant repository.

Mirrors the participant + fork operations of
``app/chat/persistence.py::ChatRepository``. Returns ``app.chat.types``
dataclasses so ChatRepository can delegate transparently.

Unlike DuckDB, the FK chat_session_participants.session_id → chat_sessions.id
carries ON DELETE CASCADE (migration 0016), so participant rows are removed
automatically when a session is hard-deleted; the explicit DuckDB pre-delete
makes the same intent visible on the in-process side.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.chat.types import ChatSession, SessionParticipant, Surface


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def _row_to_participant(row) -> SessionParticipant:
    return SessionParticipant(
        id=row["id"],
        session_id=row["session_id"],
        user_email=row["user_email"],
        user_id=row["user_id"],
        role=row["role"],
        joined_at=row["joined_at"],
        left_at=row["left_at"],
    )


def _row_to_session(row) -> ChatSession:
    return ChatSession(
        id=row["id"],
        user_email=row["user_email"],
        surface=Surface(row["surface"]),
        slack_channel_id=row["slack_channel_id"],
        slack_thread_ts=row["slack_thread_ts"],
        title=row["title"],
        started_at=row["started_at"],
        last_message_at=row["last_message_at"],
        message_count=int(row["message_count"]) if row["message_count"] is not None else 0,
        archived=bool(row["archived"]),
        is_co_session=bool(row["is_co_session"]),
        ephemeral=bool(row["ephemeral"]),
        agent_id=row["agent_id"],
        sandbox_id=row["sandbox_id"],
        runner_pid=int(row["runner_pid"]) if row["runner_pid"] is not None else None,
        sandbox_paused_at=row["sandbox_paused_at"],
        relay_protocol_version=(
            int(row["relay_protocol_version"]) if row["relay_protocol_version"] is not None else None
        ),
    )


class ChatSessionParticipantPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def add_session_participant(
        self,
        *,
        session_id: str,
        user_email: str,
        user_id: str,
        role: str,
    ) -> SessionParticipant:
        pid = _gen_id("part")
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO chat_session_participants "
                    "(id, session_id, user_email, user_id, role, joined_at, left_at) "
                    "VALUES (:id, :sid, :ue, :uid, :role, :joined, NULL)"
                ),
                {"id": pid, "sid": session_id, "ue": user_email, "uid": user_id, "role": role, "joined": now},
            )
        return SessionParticipant(
            id=pid,
            session_id=session_id,
            user_email=user_email,
            user_id=user_id,
            role=role,
            joined_at=now,
            left_at=None,
        )

    def get_session_participants(self, session_id: str) -> list[SessionParticipant]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT * FROM chat_session_participants "
                        "WHERE session_id = :sid AND left_at IS NULL "
                        "ORDER BY joined_at ASC"
                    ),
                    {"sid": session_id},
                )
                .mappings()
                .all()
            )
        return [_row_to_participant(r) for r in rows]

    def remove_participant(self, session_id: str, user_email: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE chat_session_participants SET left_at = :now "
                    "WHERE session_id = :sid AND user_email = :ue AND left_at IS NULL"
                ),
                {"now": datetime.now(timezone.utc), "sid": session_id, "ue": user_email},
            )

    def update_participant_role(self, session_id: str, user_email: str, role: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE chat_session_participants SET role = :role "
                    "WHERE session_id = :sid AND user_email = :ue AND left_at IS NULL"
                ),
                {"role": role, "sid": session_id, "ue": user_email},
            )

    def list_sessions_for_participant(self, user_email: str) -> list[ChatSession]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT s.* FROM chat_sessions s "
                        "JOIN chat_session_participants p ON p.session_id = s.id "
                        "WHERE p.user_email = :ue AND p.left_at IS NULL "
                        "ORDER BY s.last_message_at DESC NULLS LAST, s.started_at DESC"
                    ),
                    {"ue": user_email},
                )
                .mappings()
                .all()
            )
        return [_row_to_session(r) for r in rows]

    def fork_session_as_co_session(
        self,
        source_id: str,
        *,
        owner_email: str,
        owner_user_id: str,
        invitee_email: str,
        invitee_user_id: str,
        seed_summary: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> ChatSession:
        """Atomic fork (single PG transaction): fresh co-session + two
        participant rows + optional seed summary message. Source untouched.
        Never blind-clones the transcript (SR-8). When a summary is seeded,
        the parent rollup columns (message_count / last_message_at) are
        maintained in the same transaction so the co-session row stays
        consistent with how chat_messages_pg.append_message rolls up every
        other message insert."""
        chat_id = session_id or _gen_id("chat")
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO chat_sessions "
                    "(id, user_email, surface, slack_channel_id, slack_thread_ts, title, "
                    "started_at, last_message_at, message_count, archived, is_co_session, ephemeral) "
                    "VALUES (:id, :ue, 'web', NULL, NULL, NULL, :now, NULL, 0, FALSE, TRUE, TRUE)"
                ),
                {"id": chat_id, "ue": owner_email, "now": now},
            )
            for email, uid, role in (
                (owner_email, owner_user_id, "owner"),
                (invitee_email, invitee_user_id, "collaborator"),
            ):
                conn.execute(
                    sa.text(
                        "INSERT INTO chat_session_participants "
                        "(id, session_id, user_email, user_id, role, joined_at, left_at) "
                        "VALUES (:id, :sid, :ue, :uid, :role, :now, NULL)"
                    ),
                    {"id": _gen_id("part"), "sid": chat_id, "ue": email, "uid": uid, "role": role, "now": now},
                )
            if seed_summary:
                conn.execute(
                    sa.text(
                        "INSERT INTO chat_messages "
                        "(id, session_id, role, content, created_at) "
                        "VALUES (:id, :sid, 'system', :content, :now)"
                    ),
                    {"id": _gen_id("msg"), "sid": chat_id, "content": seed_summary, "now": now},
                )
                # Maintain the parent rollup the same way append_message does,
                # so message_count / last_message_at are not left stale at 0.
                conn.execute(
                    sa.text(
                        "UPDATE chat_sessions "
                        "SET message_count = message_count + 1, last_message_at = :now "
                        "WHERE id = :sid"
                    ),
                    {"now": now, "sid": chat_id},
                )
        with self._engine.connect() as conn:
            row = (
                conn.execute(sa.text("SELECT * FROM chat_sessions WHERE id = :id"), {"id": chat_id}).mappings().first()
            )
        assert row is not None
        return _row_to_session(row)

    def fork_co_session_to_private(
        self,
        *,
        source_session_id: str,
        owner_email: str,
        session_id: Optional[str] = None,
    ) -> str:
        """Atomic fork of a co-session to a private non-ephemeral session.

        All messages from the co-session are copied within a single transaction
        (governed by the caller's own grants). Returns the new session id.
        """
        from src.repositories.chat_messages_pg import ChatMessagePgRepository

        msg_repo = ChatMessagePgRepository(self._engine)
        source_msgs = msg_repo.list_messages(source_session_id)

        chat_id = session_id or _gen_id("chat")
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO chat_sessions "
                    "(id, user_email, surface, slack_channel_id, slack_thread_ts, title, "
                    "started_at, last_message_at, message_count, archived, is_co_session, ephemeral) "
                    "VALUES (:id, :ue, 'web', NULL, NULL, NULL, :now, NULL, 0, FALSE, FALSE, FALSE)"
                ),
                {"id": chat_id, "ue": owner_email, "now": now},
            )
            import json

            for msg in source_msgs:
                conn.execute(
                    sa.text(
                        "INSERT INTO chat_messages "
                        "(id, session_id, role, content, tool_calls, tokens_in, tokens_out, "
                        "model, sender_email, created_at) "
                        "VALUES (:id, :sid, :role, :content, :tc, :tin, :tout, :model, :se, :ca)"
                    ),
                    {
                        "id": _gen_id("msg"),
                        "sid": chat_id,
                        "role": msg.role,
                        "content": msg.content,
                        "tc": json.dumps(msg.tool_calls) if msg.tool_calls else None,
                        "tin": msg.tokens_in,
                        "tout": msg.tokens_out,
                        "model": msg.model,
                        "se": msg.sender_email,
                        "ca": msg.created_at,
                    },
                )
            # Maintain PG rollup columns (no DuckDB FK/index bug here).
            msg_count = len(source_msgs)
            if msg_count > 0:
                conn.execute(
                    sa.text("UPDATE chat_sessions SET message_count = :n, last_message_at = :now WHERE id = :id"),
                    {"n": msg_count, "now": now, "id": chat_id},
                )
        return chat_id
