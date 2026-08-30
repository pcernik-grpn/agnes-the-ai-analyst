"""Postgres-backed chat-session repository.

Mirrors the ``chat_sessions`` operations of
``app/chat/persistence.py::ChatRepository``. Public surface returns the same
``app.chat.types`` dataclasses so ChatRepository can delegate transparently.

Unlike the DuckDB path, Postgres has no FK+index false-violation bug, so
``message_count`` / ``last_message_at`` are kept current here (maintained by
the chat-message repo on append) and read straight off the row rather than
re-derived via LEFT JOIN. Per-surface Slack uniqueness is enforced by the
partial unique indexes created in migration 0015 (not by application code).
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.chat.types import RELAY_PROTOCOL_VERSION, ChatSession, Surface


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


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
        sandbox_id=row["sandbox_id"],
        runner_pid=int(row["runner_pid"]) if row["runner_pid"] is not None else None,
        sandbox_paused_at=row["sandbox_paused_at"],
        agent_id=row["agent_id"],
        relay_protocol_version=(
            int(row["relay_protocol_version"]) if row["relay_protocol_version"] is not None else None
        ),
        pinned_at=row["pinned_at"],
    )


class ChatSessionPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def create_session(
        self,
        *,
        user_email: str,
        surface: Surface,
        slack_channel_id: Optional[str] = None,
        slack_thread_ts: Optional[str] = None,
        title: Optional[str] = None,
        agent_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> ChatSession:
        """``session_id`` lets a caller own the id — see the DuckDB sibling's
        docstring for why an external turn engine needs it. Omitted ⇒
        generated, as before."""
        chat_id = session_id or _gen_id("chat")
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO chat_sessions "
                    "(id, user_email, surface, slack_channel_id, slack_thread_ts, "
                    "title, started_at, last_message_at, message_count, archived, agent_id) "
                    "VALUES (:id, :user_email, :surface, :slack_channel_id, "
                    ":slack_thread_ts, :title, :started_at, NULL, 0, FALSE, :agent_id)"
                ),
                {
                    "id": chat_id,
                    "user_email": user_email,
                    "surface": surface.value,
                    "slack_channel_id": slack_channel_id,
                    "slack_thread_ts": slack_thread_ts,
                    "title": title,
                    "started_at": now,
                    "agent_id": agent_id,
                },
            )
        fetched = self.get_session(chat_id)
        assert fetched is not None
        return fetched

    def get_session(self, chat_id: str) -> Optional[ChatSession]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM chat_sessions WHERE id = :id"),
                    {"id": chat_id},
                )
                .mappings()
                .first()
            )
        return _row_to_session(row) if row else None

    def list_sessions(self, user_email: str, *, include_archived: bool = False) -> list[ChatSession]:
        sql = "SELECT * FROM chat_sessions WHERE user_email = :user_email"
        if not include_archived:
            sql += " AND archived = FALSE"
        # Pinned first, most-recently-pinned leading; then plain recency. The
        # explicit NULLS LAST matters more here than on DuckDB: PG's default for
        # DESC is NULLS FIRST, which would sort every unpinned row to the top.
        sql += " ORDER BY pinned_at DESC NULLS LAST, last_message_at DESC NULLS LAST, started_at DESC"
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(sql), {"user_email": user_email}).mappings().all()
        return [_row_to_session(r) for r in rows]

    def get_slack_dm_session(self, slack_channel_id: str) -> Optional[ChatSession]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text(
                        "SELECT * FROM chat_sessions "
                        "WHERE surface = 'slack_dm' AND slack_channel_id = :cid "
                        "AND archived = FALSE"
                    ),
                    {"cid": slack_channel_id},
                )
                .mappings()
                .first()
            )
        return _row_to_session(row) if row else None

    def get_slack_thread_session(self, slack_channel_id: str, slack_thread_ts: str) -> Optional[ChatSession]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text(
                        "SELECT * FROM chat_sessions "
                        "WHERE surface = 'slack_thread' AND slack_channel_id = :cid "
                        "AND slack_thread_ts = :ts AND archived = FALSE"
                    ),
                    {"cid": slack_channel_id, "ts": slack_thread_ts},
                )
                .mappings()
                .first()
            )
        return _row_to_session(row) if row else None

    def archive_session(self, chat_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE chat_sessions SET archived = TRUE WHERE id = :id"),
                {"id": chat_id},
            )

    def restore_session(self, chat_id: str) -> None:
        """Un-archive a session. Mirrors ``ChatRepository.restore_session`` —
        idempotent, and the way back from the Chats page's Archived filter."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE chat_sessions SET archived = FALSE WHERE id = :id"),
                {"id": chat_id},
            )

    def hard_delete_session(self, chat_id: str) -> bool:
        """Permanently delete ONE session; returns whether a row existed.

        Mirrors ``ChatRepository.hard_delete_session``. Postgres has
        ``ON DELETE CASCADE`` on both child tables (``chat_messages`` in
        migration 0015, ``chat_session_participants`` in 0017), so the deletes
        the DuckDB sibling has to spell out happen here for free — the
        observable contract is identical.
        """
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text("DELETE FROM chat_sessions WHERE id = :id"),
                {"id": chat_id},
            )
        return bool(result.rowcount)

    def set_title(self, chat_id: str, title: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE chat_sessions SET title = :title WHERE id = :id"),
                {"title": title, "id": chat_id},
            )

    def set_pinned(self, chat_id: str, pinned: bool) -> None:
        """Pin (``pinned_at = now``) or unpin (``pinned_at = NULL``) a session.

        Mirrors ``ChatRepository.set_pinned``: re-pinning re-stamps the
        timestamp, moving the session to the front of the Pinned group.
        """
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE chat_sessions SET pinned_at = :ts WHERE id = :id"),
                {"ts": datetime.now(timezone.utc) if pinned else None, "id": chat_id},
            )

    def archive_empty_user_sessions(
        self,
        user_email: str,
        *,
        surface: Optional[Surface] = None,
        exclude_id: Optional[str] = None,
    ) -> int:
        """Soft-archive every empty (zero-message) session owned by
        ``user_email``. Returns the number of rows archived.

        Empty = no rows in chat_messages. ``message_count`` is kept current
        on PG, but we filter on the actual child count via NOT EXISTS so the
        semantics match the DuckDB LEFT JOIN exactly.
        """
        params: dict = {"user_email": user_email}
        sql = (
            "UPDATE chat_sessions s SET archived = TRUE "
            "WHERE s.user_email = :user_email "
            "  AND s.archived = FALSE "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM chat_messages m WHERE m.session_id = s.id)"
        )
        if surface is not None:
            sql += " AND s.surface = :surface"
            params["surface"] = surface.value
        if exclude_id is not None:
            sql += " AND s.id != :exclude_id"
            params["exclude_id"] = exclude_id
        with self._engine.begin() as conn:
            result = conn.execute(sa.text(sql), params)
        return result.rowcount if result.rowcount is not None else 0

    def hard_delete_user_sessions(self, user_email: str) -> int:
        with self._engine.begin() as conn:
            n = (
                conn.execute(
                    sa.text("SELECT COUNT(*) FROM chat_sessions WHERE user_email = :ue"),
                    {"ue": user_email},
                ).scalar()
                or 0
            )
            # ON DELETE CASCADE removes child chat_messages automatically.
            conn.execute(
                sa.text("DELETE FROM chat_sessions WHERE user_email = :ue"),
                {"ue": user_email},
            )
        return int(n)

    # --- sandbox pause/resume refs -----------------------------------------

    def set_sandbox_ref(self, session_id: str, *, sandbox_id: str, runner_pid: int) -> None:
        """Record the provider sandbox id and runner pid; clear paused_at (live).

        Also stamps ``relay_protocol_version`` with the current
        ``RELAY_PROTOCOL_VERSION`` — mirrors
        ``app.chat.persistence.ChatRepository.set_sandbox_ref`` (Tier 1,
        restart-invariant reuse). See that method's docstring for the full
        rationale.
        """
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE chat_sessions "
                    "SET sandbox_id = :sandbox_id, runner_pid = :runner_pid, sandbox_paused_at = NULL, "
                    "relay_protocol_version = :relay_protocol_version "
                    "WHERE id = :id"
                ),
                {
                    "sandbox_id": sandbox_id,
                    "runner_pid": runner_pid,
                    "relay_protocol_version": RELAY_PROTOCOL_VERSION,
                    "id": session_id,
                },
            )

    def clear_sandbox_ref(self, session_id: str) -> None:
        """Wipe all three sandbox columns plus relay_protocol_version —
        called on real kill/error teardown."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE chat_sessions "
                    "SET sandbox_id = NULL, runner_pid = NULL, sandbox_paused_at = NULL, "
                    "relay_protocol_version = NULL "
                    "WHERE id = :id"
                ),
                {"id": session_id},
            )

    def set_sandbox_paused_at(self, session_id: str, paused_at: Optional[datetime]) -> None:
        """Set or clear the paused timestamp. Pass None to clear (resume path)."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE chat_sessions SET sandbox_paused_at = :paused_at WHERE id = :id"),
                {"paused_at": paused_at, "id": session_id},
            )

    def list_paused_sessions(self, *, paused_before: datetime) -> list[ChatSession]:
        """Return sessions whose sandbox_paused_at is set and older than paused_before."""
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT * FROM chat_sessions "
                        "WHERE sandbox_paused_at IS NOT NULL AND sandbox_paused_at < :cutoff"
                    ),
                    {"cutoff": paused_before},
                )
                .mappings()
                .all()
            )
        return [_row_to_session(r) for r in rows]

    def list_recently_active(self, *, limit: int = 200) -> list[ChatSession]:
        """Sessions with at least one message, most-recently-active first,
        capped at *limit*. Cross-user (no owner filter) — same shape as
        ``list_paused_sessions`` above, just ordered/capped instead of
        filtered on the pause marker. ``last_message_at`` is maintained
        directly on this table on Postgres (see module docstring), so this
        is a plain indexless scan+sort, no derived aggregate.

        Used by the session-pipeline chat-export sweep
        (``services/session_pipeline/runner.py``, F4 — audit-full-coverage
        plan) to find export candidates without an O(users) fan-out over
        every registered user.
        """
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT * FROM chat_sessions "
                        "WHERE last_message_at IS NOT NULL "
                        "ORDER BY last_message_at DESC LIMIT :limit"
                    ),
                    {"limit": limit},
                )
                .mappings()
                .all()
            )
        return [_row_to_session(r) for r in rows]
