"""Postgres-backed chat-message repository.

Mirrors the ``chat_messages`` operations of
``app/chat/persistence.py::ChatRepository``. Returns ``app.chat.types``
dataclasses so ChatRepository can delegate transparently.

On Postgres the parent ``chat_sessions`` rollup columns
(``message_count`` / ``last_message_at``) ARE maintained on append — the
DuckDB 1.5.3 FK+index false-violation bug that forced read-time derivation
does not apply here.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.chat.types import ChatMessage


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def _decode_json_column(value):
    # JSONB columns deserialize to Python objects already; tolerate str too.
    # Shared by `tool_calls` and `parts` — both are whole-value JSON columns.
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
    return value


class ChatMessagePgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def append_message(
        self,
        *,
        session_id: str,
        role: str,
        content: str,
        tool_calls: Optional[list[dict]] = None,
        parts: Optional[list[dict]] = None,
        tokens_in: Optional[int] = None,
        tokens_out: Optional[int] = None,
        model: Optional[str] = None,
        sender_email: Optional[str] = None,
    ) -> ChatMessage:
        msg_id = _gen_id("msg")
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO chat_messages "
                    "(id, session_id, role, content, tool_calls, parts, tokens_in, "
                    "tokens_out, model, sender_email, created_at) "
                    "VALUES (:id, :session_id, :role, :content, "
                    "CAST(:tool_calls AS JSONB), CAST(:parts AS JSONB), "
                    ":tokens_in, :tokens_out, "
                    ":model, :sender_email, :created_at)"
                ),
                {
                    "id": msg_id,
                    "session_id": session_id,
                    "role": role,
                    "content": content,
                    "tool_calls": json.dumps(tool_calls) if tool_calls else None,
                    "parts": json.dumps(parts) if parts else None,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "model": model,
                    "sender_email": sender_email,
                    "created_at": now,
                },
            )
            # PG can update parent rollup columns directly (no FK+index bug).
            conn.execute(
                sa.text(
                    "UPDATE chat_sessions "
                    "SET message_count = message_count + 1, last_message_at = :now "
                    "WHERE id = :session_id"
                ),
                {"now": now, "session_id": session_id},
            )
        return ChatMessage(
            id=msg_id,
            session_id=session_id,
            role=role,
            content=content,
            tool_calls=tool_calls,
            parts=parts,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=model,
            sender_email=sender_email,
            created_at=now,
        )

    def list_messages(self, session_id: str, *, after_id: Optional[str] = None, limit: int = 500) -> list[ChatMessage]:
        with self._engine.connect() as conn:
            cutoff = None
            if after_id:
                cutoff = conn.execute(
                    sa.text("SELECT created_at FROM chat_messages WHERE id = :id"),
                    {"id": after_id},
                ).scalar()
            sql = (
                "SELECT id, session_id, role, content, tool_calls, parts, tokens_in, "
                "tokens_out, model, sender_email, created_at FROM chat_messages "
                "WHERE session_id = :session_id"
            )
            params: dict = {"session_id": session_id}
            if cutoff is not None:
                sql += " AND created_at > :cutoff"
                params["cutoff"] = cutoff
            sql += " ORDER BY created_at ASC LIMIT :limit"
            params["limit"] = limit
            rows = conn.execute(sa.text(sql), params).mappings().all()
        return [
            ChatMessage(
                id=r["id"],
                session_id=r["session_id"],
                role=r["role"],
                content=r["content"],
                tool_calls=_decode_json_column(r["tool_calls"]),
                parts=_decode_json_column(r["parts"]),
                tokens_in=r["tokens_in"],
                tokens_out=r["tokens_out"],
                model=r["model"],
                sender_email=r["sender_email"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def list_recent_messages(self, session_id: str, *, limit: int = 500) -> list[ChatMessage]:
        """Newest-first slice of the conversation — the counterpart to
        ``list_messages``'s oldest-first ``LIMIT``. A caller that only cares
        about the tail of a long conversation (redelivering the pending
        question, building a restore transcript) must use this, not
        ``list_messages``: that method's ``ORDER BY created_at ASC LIMIT``
        returns the OLDEST ``limit`` rows, so for a chat past ``limit``
        messages its last element is nowhere near the actual latest turn."""
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT id, session_id, role, content, tool_calls, parts, tokens_in, "
                        "tokens_out, model, sender_email, created_at FROM chat_messages "
                        "WHERE session_id = :session_id ORDER BY created_at DESC LIMIT :limit"
                    ),
                    {"session_id": session_id, "limit": limit},
                )
                .mappings()
                .all()
            )
        return [
            ChatMessage(
                id=r["id"],
                session_id=r["session_id"],
                role=r["role"],
                content=r["content"],
                tool_calls=_decode_json_column(r["tool_calls"]),
                parts=_decode_json_column(r["parts"]),
                tokens_in=r["tokens_in"],
                tokens_out=r["tokens_out"],
                model=r["model"],
                sender_email=r["sender_email"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def get_first_user_message(self, chat_id: str) -> Optional[str]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT content FROM chat_messages "
                    "WHERE session_id = :id AND role = 'user' "
                    "ORDER BY created_at ASC LIMIT 1"
                ),
                {"id": chat_id},
            ).first()
        return row[0] if row else None

    def session_total_tokens(self, session_id: str) -> int:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT COALESCE(SUM(COALESCE(tokens_in, 0) + "
                    "COALESCE(tokens_out, 0)), 0) "
                    "FROM chat_messages WHERE session_id = :id"
                ),
                {"id": session_id},
            ).scalar()
        return int(row or 0)

    def daily_anthropic_tokens(self, user_email: str) -> tuple[int, int]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT COALESCE(SUM(m.tokens_in), 0), "
                    "COALESCE(SUM(m.tokens_out), 0) "
                    "FROM chat_messages m "
                    "JOIN chat_sessions s ON m.session_id = s.id "
                    "WHERE s.user_email = :ue "
                    "AND DATE_TRUNC('day', m.created_at) = "
                    "DATE_TRUNC('day', CURRENT_TIMESTAMP)"
                ),
                {"ue": user_email},
            ).first()
        return int(row[0] or 0), int(row[1] or 0)
