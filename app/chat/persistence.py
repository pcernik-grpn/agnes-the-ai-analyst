"""Chat persistence — sessions, messages, and per-user workdir markers.

DuckDB 1.5.3 bug: updating a column that is part of a secondary index
on the parent table triggers a false FK constraint violation if any
child rows exist. ``last_message_at`` is part of the
``idx_chat_sessions_user(user_email, last_message_at)`` index, so
``UPDATE chat_sessions SET last_message_at = …`` after any
``INSERT INTO chat_messages`` fails. ``message_count`` and ``archived``
are not indexed and can be UPDATEd safely. Workaround: compute both
``last_message_at`` and ``message_count`` at read time via LEFT JOIN.
When DuckDB ships the fix, this module's reads can be simplified
back to plain ``SELECT … FROM chat_sessions WHERE …``.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Optional

import duckdb

from app.chat.types import (
    RELAY_PROTOCOL_VERSION,
    ChatMessage,
    ChatSession,
    SessionParticipant,
    Surface,
    UserWorkdir,
)


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def _row_to_session(row: tuple) -> ChatSession:
    # Row order matches _SESSION_SELECT below:
    # 0  id, 1 user_email, 2 surface, 3 slack_channel_id, 4 slack_thread_ts,
    # 5  title, 6 started_at,
    # 7  last_message_at (derived via LEFT JOIN),
    # 8  message_count   (derived via LEFT JOIN),
    # 9  archived, 10 is_co_session, 11 ephemeral,
    # 12 sandbox_id, 13 runner_pid, 14 sandbox_paused_at,
    # 15 relay_protocol_version, 16 agent_id, 17 pinned_at
    return ChatSession(
        id=row[0],
        user_email=row[1],
        surface=Surface(row[2]),
        slack_channel_id=row[3],
        slack_thread_ts=row[4],
        title=row[5],
        started_at=row[6],
        last_message_at=row[7],
        message_count=int(row[8]) if row[8] is not None else 0,
        archived=bool(row[9]),
        is_co_session=bool(row[10]),
        ephemeral=bool(row[11]),
        sandbox_id=row[12],
        runner_pid=int(row[13]) if row[13] is not None else None,
        sandbox_paused_at=row[14],
        relay_protocol_version=int(row[15]) if row[15] is not None else None,
        agent_id=row[16],
        pinned_at=row[17],
    )


# Session columns derived via LEFT JOIN to compute live stats.
# DuckDB 1.5.3 FK+index bug prevents UPDATE on chat_sessions after any
# INSERT into chat_messages — so message_count / last_message_at are never
# stored; they are always computed from chat_messages at read time.
_SESSION_SELECT = (
    "SELECT s.id, s.user_email, s.surface, s.slack_channel_id, s.slack_thread_ts, "
    "s.title, s.started_at, "
    "MAX(m.created_at) AS last_message_at, "
    "COUNT(m.id) AS message_count, "
    "s.archived, s.is_co_session, s.ephemeral, "
    # Sandbox pause/resume refs — NOT indexed (DuckDB 1.5.3 FK+index bug).
    "s.sandbox_id, s.runner_pid, s.sandbox_paused_at, s.relay_protocol_version, "
    # Owning agent profile — NOT indexed (same DuckDB 1.5.3 FK+index bug).
    "s.agent_id, "
    # Pin marker — NOT indexed, and unlike the columns above this one is
    # actually UPDATEd post-INSERT, so indexing it would be the FK+index bug.
    "s.pinned_at "
    "FROM chat_sessions s "
    "LEFT JOIN chat_messages m ON m.session_id = s.id"
)
_SESSION_GROUP = (
    " GROUP BY s.id, s.user_email, s.surface, s.slack_channel_id, s.slack_thread_ts, "
    "s.title, s.started_at, s.archived, s.is_co_session, s.ephemeral, "
    "s.sandbox_id, s.runner_pid, s.sandbox_paused_at, s.relay_protocol_version, "
    "s.agent_id, s.pinned_at"
)


class ChatRepository:
    """Dual-backend chat repository.

    On DuckDB (the default / single-worker deploy) it runs the in-process
    SQL below directly against the passed connection. When the active
    backend is Postgres (``use_pg()`` — side-car or cloud deploys) it
    delegates every operation to the per-table Postgres repositories under
    ``src/repositories/*_pg.py``; the DuckDB ``conn`` is then unused.

    Public method signatures and return types are identical across both
    backends so callers (app/chat/manager.py, app/api/*, services/slack_bot/*)
    never branch on backend.
    """

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn
        # Postgres delegates — populated only when the active backend is PG.
        self._sessions_pg = None
        self._messages_pg = None
        self._workdirs_pg = None
        self._participants_pg = None
        try:
            from src.repositories import use_pg

            if use_pg():
                from src.db_pg import get_engine
                from src.repositories.chat_messages_pg import ChatMessagePgRepository
                from src.repositories.chat_sessions_pg import ChatSessionPgRepository
                from src.repositories.user_workdirs_pg import UserWorkdirPgRepository
                from src.repositories.chat_session_participants_pg import (
                    ChatSessionParticipantPgRepository,
                )

                engine = get_engine()
                self._sessions_pg = ChatSessionPgRepository(engine)
                self._messages_pg = ChatMessagePgRepository(engine)
                self._workdirs_pg = UserWorkdirPgRepository(engine)
                self._participants_pg = ChatSessionParticipantPgRepository(engine)
        except Exception:
            # If backend detection or engine construction fails, fall back to
            # the DuckDB path bound to the passed connection.
            self._sessions_pg = None
            self._messages_pg = None
            self._workdirs_pg = None
            self._participants_pg = None

    # --- sessions ----------------------------------------------------------

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
        """``session_id`` lets a caller own the id instead of taking a
        generated ``chat_<hex>`` one. Needed when an external turn engine
        shares the row's key and constrains its shape: the embedded
        ``kai-agent`` engine stores the chat id in a Postgres ``uuid`` column,
        so ``chat_<hex>`` is rejected outright and the two sides could not key
        off one value (``app/api/kai.py``). Omitted ⇒ generated, as before.
        """
        if self._sessions_pg is not None:
            return self._sessions_pg.create_session(
                user_email=user_email,
                surface=surface,
                slack_channel_id=slack_channel_id,
                slack_thread_ts=slack_thread_ts,
                title=title,
                agent_id=agent_id,
                session_id=session_id,
            )
        chat_id = session_id or _gen_id("chat")
        now = datetime.now(timezone.utc)
        self._conn.execute(
            "INSERT INTO chat_sessions "
            "(id, user_email, surface, slack_channel_id, slack_thread_ts, title, "
            "started_at, last_message_at, message_count, archived, agent_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0, FALSE, ?)",
            [chat_id, user_email, surface.value, slack_channel_id, slack_thread_ts, title, now, agent_id],
        )
        fetched = self.get_session(chat_id)
        assert fetched is not None
        return fetched

    def get_session(self, chat_id: str) -> Optional[ChatSession]:
        if self._sessions_pg is not None:
            return self._sessions_pg.get_session(chat_id)
        row = self._conn.execute(
            _SESSION_SELECT + " WHERE s.id = ?" + _SESSION_GROUP,
            [chat_id],
        ).fetchone()
        return _row_to_session(row) if row else None

    def list_sessions(self, user_email: str, *, include_archived: bool = False) -> list[ChatSession]:
        if self._sessions_pg is not None:
            return self._sessions_pg.list_sessions(user_email, include_archived=include_archived)
        where = " WHERE s.user_email = ?"
        if not include_archived:
            where += " AND s.archived = FALSE"
        # Pinned conversations lead the list, most-recently-pinned first; the
        # rest keep the plain recency order. `NULLS LAST` is spelled out rather
        # than left to the engine default because the PG sibling's default for
        # DESC is NULLS FIRST — which would sort every *unpinned* row to the top.
        q = (
            _SESSION_SELECT
            + where
            + _SESSION_GROUP
            + " ORDER BY s.pinned_at DESC NULLS LAST, MAX(m.created_at) DESC NULLS LAST, s.started_at DESC"
        )
        rows = self._conn.execute(q, [user_email]).fetchall()
        return [_row_to_session(r) for r in rows]

    def get_slack_dm_session(self, slack_channel_id: str) -> Optional[ChatSession]:
        if self._sessions_pg is not None:
            return self._sessions_pg.get_slack_dm_session(slack_channel_id)
        # intentional: no await between SELECT and INSERT — Slack uniqueness without DB partial unique index
        row = self._conn.execute(
            _SESSION_SELECT
            + " WHERE s.surface = 'slack_dm' AND s.slack_channel_id = ? AND s.archived = FALSE"
            + _SESSION_GROUP,
            [slack_channel_id],
        ).fetchone()
        return _row_to_session(row) if row else None

    def get_slack_thread_session(
        self,
        slack_channel_id: str,
        slack_thread_ts: str,
    ) -> Optional[ChatSession]:
        if self._sessions_pg is not None:
            return self._sessions_pg.get_slack_thread_session(slack_channel_id, slack_thread_ts)
        # intentional: no await between SELECT and INSERT — Slack uniqueness without DB partial unique index
        row = self._conn.execute(
            _SESSION_SELECT
            + " WHERE s.surface = 'slack_thread'"
            + " AND s.slack_channel_id = ? AND s.slack_thread_ts = ? AND s.archived = FALSE"
            + _SESSION_GROUP,
            [slack_channel_id, slack_thread_ts],
        ).fetchone()
        return _row_to_session(row) if row else None

    def archive_session(self, chat_id: str) -> None:
        if self._sessions_pg is not None:
            self._sessions_pg.archive_session(chat_id)
            return
        self._conn.execute("UPDATE chat_sessions SET archived = TRUE WHERE id = ?", [chat_id])

    def restore_session(self, chat_id: str) -> None:
        """Un-archive a session — the way back from the Chats page's Archived
        filter.

        ``archive_session`` has always been a soft delete (``archived = TRUE``),
        but until the Chats page there was no surface that listed archived rows,
        so there was nothing to restore them FROM and no route back. Idempotent:
        restoring a live session is a no-op UPDATE.

        Safe post-``chat_messages`` like ``set_title`` / ``set_pinned``:
        ``archived`` is not part of any secondary index on ``chat_sessions`` (see
        the module docstring's DuckDB 1.5.3 FK+index note).
        """
        if self._sessions_pg is not None:
            self._sessions_pg.restore_session(chat_id)
            return
        self._conn.execute("UPDATE chat_sessions SET archived = FALSE WHERE id = ?", [chat_id])

    def hard_delete_session(self, chat_id: str) -> bool:
        """Permanently delete ONE session and its messages. Returns whether a
        row was there to delete.

        The single-session sibling of ``hard_delete_user_sessions`` (the
        account-wipe path), added for the Chats page: with Archive now a
        reversible state of its own, Delete has to mean something different from
        it, and "delete" that leaves the row in the table is the kind of promise
        a UI must not make.

        Child rows go first in the same order the account-wipe uses — DuckDB has
        no ``ON DELETE CASCADE``, so the FKs on ``chat_session_participants``
        and ``chat_messages`` would block the parent delete while either exists.
        """
        if self._sessions_pg is not None:
            return self._sessions_pg.hard_delete_session(chat_id)
        row = self._conn.execute("SELECT 1 FROM chat_sessions WHERE id = ?", [chat_id]).fetchone()
        if row is None:
            return False
        self._conn.execute("DELETE FROM chat_session_participants WHERE session_id = ?", [chat_id])
        self._conn.execute("DELETE FROM chat_messages WHERE session_id = ?", [chat_id])
        self._conn.execute("DELETE FROM chat_sessions WHERE id = ?", [chat_id])
        return True

    def set_title(self, chat_id: str, title: str) -> None:
        """Persist a new title for a session. Safe to call after
        ``chat_messages`` rows exist — ``title`` is not part of any
        secondary index on ``chat_sessions``, so it's not subject to the
        DuckDB 1.5.3 FK+index bug that prevents UPDATE of
        ``last_message_at`` / ``message_count``.

        Used by the auto-title path (Haiku-generated title after the
        first assistant turn) and would also be the home for any future
        inline-rename UI."""
        if self._sessions_pg is not None:
            self._sessions_pg.set_title(chat_id, title)
            return
        self._conn.execute("UPDATE chat_sessions SET title = ? WHERE id = ?", [title, chat_id])

    def set_pinned(self, chat_id: str, pinned: bool) -> None:
        """Pin (or unpin) a conversation so the history panel keeps it on top.

        ``pinned=True`` stamps ``pinned_at`` with now; ``pinned=False`` clears it
        back to NULL. Re-pinning an already-pinned session re-stamps it, which
        moves it to the front of the Pinned group — the same "most recent action
        wins" ordering the rest of the list uses.

        Safe to call after ``chat_messages`` rows exist: ``pinned_at`` is not
        part of any secondary index on ``chat_sessions``, so it dodges the
        DuckDB 1.5.3 FK+index bug (same reasoning as ``set_title`` above — but
        load-bearing here, since pinning happens on conversations that by
        definition already have messages).
        """
        if self._sessions_pg is not None:
            self._sessions_pg.set_pinned(chat_id, pinned)
            return
        self._conn.execute(
            "UPDATE chat_sessions SET pinned_at = ? WHERE id = ?",
            [datetime.now(timezone.utc) if pinned else None, chat_id],
        )

    # --- sandbox pause/resume refs -----------------------------------------
    # The three columns (sandbox_id, runner_pid, sandbox_paused_at) are
    # intentionally NOT indexed — DuckDB 1.5.3 FK+index bug causes a false FK
    # violation when UPDATE touches any indexed column on chat_sessions after
    # chat_messages rows exist. Un-indexed column UPDATEs work fine (proof:
    # set_title above). The paused-TTL reaper query is a plain scan — fine at
    # chat-session cardinality.

    def set_sandbox_ref(self, session_id: str, *, sandbox_id: str, runner_pid: int) -> None:
        """Record the provider sandbox id and runner pid; clear paused_at (live).

        Also stamps ``relay_protocol_version`` with the current
        ``RELAY_PROTOCOL_VERSION`` (Tier 1, restart-invariant reuse):
        ``set_sandbox_ref`` is only ever called right after spawning a
        fresh runner (or reconnecting one this process itself pushed a
        ticket to), so the row can safely record "this session's sandbox
        refs point at a current-protocol runner" — the durable fact
        ``ChatManager._resume_from_row`` / ``_resume_live`` need to decide
        resume-vs-respawn after a process restart, when the in-memory
        ``_known_protocol_sessions`` set is empty.
        """
        if self._sessions_pg is not None:
            self._sessions_pg.set_sandbox_ref(session_id, sandbox_id=sandbox_id, runner_pid=runner_pid)
            return
        self._conn.execute(
            "UPDATE chat_sessions SET sandbox_id = ?, runner_pid = ?, sandbox_paused_at = NULL, "
            "relay_protocol_version = ? WHERE id = ?",
            [sandbox_id, runner_pid, RELAY_PROTOCOL_VERSION, session_id],
        )

    def clear_sandbox_ref(self, session_id: str) -> None:
        """Wipe all three sandbox columns plus relay_protocol_version —
        called on real kill/error teardown."""
        if self._sessions_pg is not None:
            self._sessions_pg.clear_sandbox_ref(session_id)
            return
        self._conn.execute(
            "UPDATE chat_sessions SET sandbox_id = NULL, runner_pid = NULL, sandbox_paused_at = NULL, "
            "relay_protocol_version = NULL WHERE id = ?",
            [session_id],
        )

    def set_sandbox_paused_at(self, session_id: str, paused_at: Optional[datetime]) -> None:
        """Set or clear the paused timestamp. Pass None to clear (resume path)."""
        if self._sessions_pg is not None:
            self._sessions_pg.set_sandbox_paused_at(session_id, paused_at)
            return
        self._conn.execute(
            "UPDATE chat_sessions SET sandbox_paused_at = ? WHERE id = ?",
            [paused_at, session_id],
        )

    def list_paused_sessions(self, *, paused_before: datetime) -> list[ChatSession]:
        """Return sessions whose sandbox_paused_at is set and older than paused_before."""
        if self._sessions_pg is not None:
            return self._sessions_pg.list_paused_sessions(paused_before=paused_before)
        rows = self._conn.execute(
            _SESSION_SELECT + " WHERE s.sandbox_paused_at IS NOT NULL AND s.sandbox_paused_at < ?" + _SESSION_GROUP,
            [paused_before],
        ).fetchall()
        return [_row_to_session(r) for r in rows]

    def list_recently_active(self, *, limit: int = 200) -> list[ChatSession]:
        """Sessions with at least one message, most-recently-active first,
        capped at *limit*. Cross-user (no owner filter) — same shape as
        ``list_paused_sessions`` above, just ordered/capped instead of
        filtered on the pause marker.

        Used by the session-pipeline chat-export sweep
        (``services/session_pipeline/runner.py``, F4 — audit-full-coverage
        plan) to find export candidates without an O(users) fan-out over
        every registered user.
        """
        if self._sessions_pg is not None:
            return self._sessions_pg.list_recently_active(limit=limit)
        rows = self._conn.execute(
            _SESSION_SELECT + _SESSION_GROUP + " HAVING COUNT(m.id) > 0 ORDER BY last_message_at DESC LIMIT ?",
            [limit],
        ).fetchall()
        return [_row_to_session(r) for r in rows]

    def get_first_user_message(self, chat_id: str) -> Optional[str]:
        """First user-role message content in a session (oldest by
        ``created_at``), or ``None`` if the session has no user turns yet.

        Used as the auto-title prompt: the first user message captures
        the topic better than any later turn (which is usually a
        follow-up / refinement)."""
        if self._messages_pg is not None:
            return self._messages_pg.get_first_user_message(chat_id)
        row = self._conn.execute(
            "SELECT content FROM chat_messages WHERE session_id = ? AND role = 'user' ORDER BY created_at ASC LIMIT 1",
            [chat_id],
        ).fetchone()
        return row[0] if row else None

    def archive_empty_user_sessions(
        self,
        user_email: str,
        *,
        surface: Optional[Surface] = None,
        exclude_id: Optional[str] = None,
    ) -> int:
        """Soft-archive every empty (zero-message) session owned by
        ``user_email``. Returns the number of rows archived.

        Called from ``POST /api/chat/sessions`` so clicking "+ New
        chat" repeatedly never accumulates Untitled-chat orphans in
        the sidebar.

        ``surface`` scopes the GC — pass ``Surface.WEB`` so a web
        click doesn't also nuke the user's empty Slack DM/thread
        placeholders, which the manager intentionally keeps around
        keyed by channel/thread id for re-attach. ``None`` (default)
        archives across every surface.

        ``exclude_id`` lets the caller protect the session it just
        created — otherwise we'd race-archive the brand-new one.

        Implementation: a single UPDATE filtered by the same LEFT JOIN
        used in ``_SESSION_SELECT`` so we only touch sessions with
        zero messages. ``archived = FALSE`` in the WHERE keeps the
        count accurate (don't re-archive what's already archived).
        """
        if self._sessions_pg is not None:
            return self._sessions_pg.archive_empty_user_sessions(user_email, surface=surface, exclude_id=exclude_id)
        params: list = [user_email]
        surface_clause = ""
        if surface is not None:
            surface_clause = " AND s.surface = ?"
        sql = (
            "UPDATE chat_sessions SET archived = TRUE "
            "WHERE user_email = ? "
            "  AND archived = FALSE "
            "  AND id IN ("
            "    SELECT s.id FROM chat_sessions s "
            "    LEFT JOIN chat_messages m ON m.session_id = s.id "
            "    WHERE s.user_email = ? AND s.archived = FALSE"
            f"{surface_clause}"
            "    GROUP BY s.id HAVING COUNT(m.id) = 0"
            "  )"
        )
        params.append(user_email)
        if surface is not None:
            params.append(surface.value)
        if exclude_id is not None:
            sql += " AND id != ?"
            params.append(exclude_id)
        before = self._conn.execute(
            "SELECT COUNT(*) FROM chat_sessions WHERE user_email = ? AND archived = FALSE",
            [user_email],
        ).fetchone()[0]
        self._conn.execute(sql, params)
        after = self._conn.execute(
            "SELECT COUNT(*) FROM chat_sessions WHERE user_email = ? AND archived = FALSE",
            [user_email],
        ).fetchone()[0]
        return before - after

    def hard_delete_user_sessions(self, user_email: str) -> int:
        if self._sessions_pg is not None:
            return self._sessions_pg.hard_delete_user_sessions(user_email)
        n = self._conn.execute("SELECT COUNT(*) FROM chat_sessions WHERE user_email = ?", [user_email]).fetchone()[0]
        # DuckDB has no ON DELETE CASCADE. Delete participant rows first so
        # the chat_session_participants FK can't block the parent delete.
        self._conn.execute(
            "DELETE FROM chat_session_participants WHERE session_id IN ("
            " SELECT id FROM chat_sessions WHERE user_email = ?)",
            [user_email],
        )
        # FK on chat_messages.session_id blocks parent delete while
        # children exist (DuckDB has no ON DELETE CASCADE — Task 1.1
        # documented this). Delete messages first.
        self._conn.execute(
            "DELETE FROM chat_messages WHERE session_id IN ( SELECT id FROM chat_sessions WHERE user_email = ?)",
            [user_email],
        )
        self._conn.execute("DELETE FROM chat_sessions WHERE user_email = ?", [user_email])
        return n

    # --- messages ----------------------------------------------------------

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
        cache_read_tokens: Optional[int] = None,
        cache_creation_tokens: Optional[int] = None,
        model: Optional[str] = None,
        sender_email: Optional[str] = None,
    ) -> ChatMessage:
        if self._messages_pg is not None:
            return self._messages_pg.append_message(
                session_id=session_id,
                role=role,
                content=content,
                tool_calls=tool_calls,
                parts=parts,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cache_read_tokens=cache_read_tokens,
                cache_creation_tokens=cache_creation_tokens,
                model=model,
                sender_email=sender_email,
            )
        # DuckDB app-state path: the two prompt-cache columns exist only on
        # Postgres (migration 0092 — the DuckDB ladder is frozen at
        # FROZEN_DUCKDB_SCHEMA_VERSION and takes no new step, A3). The
        # figures are accepted and dropped rather than refused: recording a
        # turn is the caller's actual job here, and losing an accounting
        # detail must not fail a chat. `cost_breakdown` below reports the
        # gap explicitly instead of serving zeros as if they were measured.
        msg_id = _gen_id("msg")
        now = datetime.now(timezone.utc)
        # DuckDB 1.5.3 bug: updating a column that is part of a secondary
        # index on the parent table triggers a false FK constraint violation
        # if any child rows exist. last_message_at is part of
        # idx_chat_sessions_user(user_email, last_message_at), so
        # UPDATE chat_sessions SET last_message_at = … after any
        # INSERT INTO chat_messages fails. Workaround: don't UPDATE at all
        # — compute last_message_at and message_count at read time via
        # LEFT JOIN (see _SESSION_SELECT). message_count and archived are
        # not indexed and could be UPDATEd safely, but we keep the same
        # read-time-derivation approach for both columns for consistency.
        self._conn.execute(
            "INSERT INTO chat_messages "
            "(id, session_id, role, content, tool_calls, parts, tokens_in, tokens_out, model, sender_email, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                msg_id,
                session_id,
                role,
                content,
                json.dumps(tool_calls) if tool_calls else None,
                json.dumps(parts) if parts else None,
                tokens_in,
                tokens_out,
                model,
                sender_email,
                now,
            ],
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
            created_at=now,
            sender_email=sender_email,
        )

    def list_messages(
        self,
        session_id: str,
        *,
        after_id: Optional[str] = None,
        limit: int = 500,
    ) -> list[ChatMessage]:
        if self._messages_pg is not None:
            return self._messages_pg.list_messages(session_id, after_id=after_id, limit=limit)
        if after_id:
            row = self._conn.execute("SELECT created_at FROM chat_messages WHERE id = ?", [after_id]).fetchone()
            cutoff = row[0] if row else None
        else:
            cutoff = None

        q = (
            "SELECT id, session_id, role, content, tool_calls, parts, tokens_in, tokens_out, "
            "model, sender_email, created_at FROM chat_messages WHERE session_id = ?"
        )
        params: list = [session_id]
        if cutoff is not None:
            q += " AND created_at > ?"
            params.append(cutoff)
        q += " ORDER BY created_at ASC LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(q, params).fetchall()
        return [
            ChatMessage(
                id=r[0],
                session_id=r[1],
                role=r[2],
                content=r[3],
                tool_calls=json.loads(r[4]) if r[4] else None,
                parts=json.loads(r[5]) if r[5] else None,
                tokens_in=r[6],
                tokens_out=r[7],
                model=r[8],
                sender_email=r[9],
                created_at=r[10],
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
        if self._messages_pg is not None:
            return self._messages_pg.list_recent_messages(session_id, limit=limit)
        rows = self._conn.execute(
            "SELECT id, session_id, role, content, tool_calls, parts, tokens_in, tokens_out, "
            "model, sender_email, created_at FROM chat_messages WHERE session_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            [session_id, limit],
        ).fetchall()
        return [
            ChatMessage(
                id=r[0],
                session_id=r[1],
                role=r[2],
                content=r[3],
                tool_calls=json.loads(r[4]) if r[4] else None,
                parts=json.loads(r[5]) if r[5] else None,
                tokens_in=r[6],
                tokens_out=r[7],
                model=r[8],
                sender_email=r[9],
                created_at=r[10],
            )
            for r in rows
        ]

    # --- participants ------------------------------------------------------

    def add_session_participant(
        self,
        *,
        session_id: str,
        user_email: str,
        user_id: str,
        role: str,
    ) -> SessionParticipant:
        if self._participants_pg is not None:
            return self._participants_pg.add_session_participant(
                session_id=session_id,
                user_email=user_email,
                user_id=user_id,
                role=role,
            )
        pid = _gen_id("part")
        now = datetime.now(timezone.utc)
        self._conn.execute(
            "INSERT INTO chat_session_participants "
            "(id, session_id, user_email, user_id, role, joined_at, left_at) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL)",
            [pid, session_id, user_email, user_id, role, now],
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
        """Active participants (left_at IS NULL) for a session — the live
        membership set co-drive authorization reads as its source of truth."""
        if self._participants_pg is not None:
            return self._participants_pg.get_session_participants(session_id)
        rows = self._conn.execute(
            "SELECT id, session_id, user_email, user_id, role, joined_at, left_at "
            "FROM chat_session_participants "
            "WHERE session_id = ? AND left_at IS NULL "
            "ORDER BY joined_at ASC",
            [session_id],
        ).fetchall()
        return [
            SessionParticipant(
                id=r[0],
                session_id=r[1],
                user_email=r[2],
                user_id=r[3],
                role=r[4],
                joined_at=r[5],
                left_at=r[6],
            )
            for r in rows
        ]

    def remove_participant(self, session_id: str, user_email: str) -> None:
        """Stamp left_at so the participant is no longer active. Idempotent."""
        if self._participants_pg is not None:
            self._participants_pg.remove_participant(session_id, user_email)
            return
        self._conn.execute(
            "UPDATE chat_session_participants SET left_at = ? "
            "WHERE session_id = ? AND user_email = ? AND left_at IS NULL",
            [datetime.now(timezone.utc), session_id, user_email],
        )

    def update_participant_role(self, session_id: str, user_email: str, role: str) -> None:
        if self._participants_pg is not None:
            self._participants_pg.update_participant_role(session_id, user_email, role)
            return
        self._conn.execute(
            "UPDATE chat_session_participants SET role = ? WHERE session_id = ? AND user_email = ? AND left_at IS NULL",
            [role, session_id, user_email],
        )

    def list_sessions_for_participant(self, user_email: str) -> list[ChatSession]:
        """Co-sessions where this email is an active participant."""
        if self._participants_pg is not None:
            return self._participants_pg.list_sessions_for_participant(user_email)
        ids = [
            r[0]
            for r in self._conn.execute(
                "SELECT DISTINCT session_id FROM chat_session_participants WHERE user_email = ? AND left_at IS NULL",
                [user_email],
            ).fetchall()
        ]
        out: list[ChatSession] = []
        for sid in ids:
            s = self.get_session(sid)
            if s is not None:
                out.append(s)
        return out

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
        """Create a fresh co-session (is_co_session=TRUE, ephemeral=TRUE) with
        the owner + invitee as participants. Never blind-clones the source
        transcript (SR-8): seeds only an optional intersection-produced
        ``seed_summary`` as a system message. The source session is untouched.

        DuckDB has no multi-statement transaction guard here; steps are ordered
        so a partial failure leaves at most a harmless empty ephemeral session
        that the GC sweep (5b) reclaims.
        """
        if self._participants_pg is not None:
            return self._participants_pg.fork_session_as_co_session(
                source_id,
                owner_email=owner_email,
                owner_user_id=owner_user_id,
                invitee_email=invitee_email,
                invitee_user_id=invitee_user_id,
                seed_summary=seed_summary,
                session_id=session_id,
            )
        chat_id = session_id or _gen_id("chat")
        now = datetime.now(timezone.utc)
        self._conn.execute(
            "INSERT INTO chat_sessions "
            "(id, user_email, surface, slack_channel_id, slack_thread_ts, title, "
            "started_at, last_message_at, message_count, archived, is_co_session, ephemeral) "
            "VALUES (?, ?, 'web', NULL, NULL, NULL, ?, NULL, 0, FALSE, TRUE, TRUE)",
            [chat_id, owner_email, now],
        )
        self.add_session_participant(
            session_id=chat_id,
            user_email=owner_email,
            user_id=owner_user_id,
            role="owner",
        )
        self.add_session_participant(
            session_id=chat_id,
            user_email=invitee_email,
            user_id=invitee_user_id,
            role="collaborator",
        )
        if seed_summary:
            self.append_message(
                session_id=chat_id,
                role="system",
                content=seed_summary,
            )
        fetched = self.get_session(chat_id)
        assert fetched is not None
        return fetched

    def fork_co_session_to_private(
        self,
        *,
        source_session_id: str,
        owner_email: str,
        session_id: Optional[str] = None,
    ) -> str:
        """Fork a co-session into a private non-ephemeral session for ``owner_email``.

        Copies every message from the co-session (governed by the caller's own
        grants). The new session is a normal web session (is_co_session=FALSE,
        ephemeral=FALSE). Returns the new session id.

        DuckDB ordering: create session first, then copy messages, so a partial
        failure leaves at most a GC-able empty session.
        """
        if self._participants_pg is not None:
            return self._participants_pg.fork_co_session_to_private(
                source_session_id=source_session_id,
                owner_email=owner_email,
                session_id=session_id,
            )
        chat_id = session_id or _gen_id("chat")
        now = datetime.now(timezone.utc)
        self._conn.execute(
            "INSERT INTO chat_sessions "
            "(id, user_email, surface, slack_channel_id, slack_thread_ts, title, "
            "started_at, last_message_at, message_count, archived, is_co_session, ephemeral) "
            "VALUES (?, ?, 'web', NULL, NULL, NULL, ?, NULL, 0, FALSE, FALSE, FALSE)",
            [chat_id, owner_email, now],
        )
        for msg in self.list_messages(source_session_id):
            self.append_message(
                session_id=chat_id,
                role=msg.role,
                content=msg.content,
                tool_calls=msg.tool_calls,
                tokens_in=msg.tokens_in,
                tokens_out=msg.tokens_out,
                model=msg.model,
                sender_email=msg.sender_email,
            )
        return chat_id

    # --- workdirs ----------------------------------------------------------

    def get_workdir(self, user_email: str) -> Optional[UserWorkdir]:
        if self._workdirs_pg is not None:
            return self._workdirs_pg.get_workdir(user_email)
        row = self._conn.execute(
            "SELECT user_email, last_init_at, marketplace_sha, initial_workspace_sha, "
            "agnes_version_at_init FROM user_workdirs WHERE user_email = ?",
            [user_email],
        ).fetchone()
        if not row:
            return None
        return UserWorkdir(
            user_email=row[0],
            last_init_at=row[1],
            marketplace_sha=row[2],
            initial_workspace_sha=row[3],
            agnes_version_at_init=row[4],
        )

    def upsert_workdir(
        self,
        *,
        user_email: str,
        marketplace_sha: Optional[str],
        initial_workspace_sha: Optional[str],
        agnes_version: str,
    ) -> None:
        if self._workdirs_pg is not None:
            self._workdirs_pg.upsert_workdir(
                user_email=user_email,
                marketplace_sha=marketplace_sha,
                initial_workspace_sha=initial_workspace_sha,
                agnes_version=agnes_version,
            )
            return
        # INCIDENT 2026-07-17: INSERT OR REPLACE deletes-then-inserts the
        # conflicting row internally on DuckDB, hitting the same PRIMARY KEY
        # index assertion (see UsageRepository.upsert_summary). ON CONFLICT
        # DO UPDATE updates in place instead.
        now = datetime.now(timezone.utc)
        self._conn.execute(
            "INSERT INTO user_workdirs "
            "(user_email, last_init_at, marketplace_sha, initial_workspace_sha, agnes_version_at_init) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (user_email) DO UPDATE SET "
            "last_init_at = EXCLUDED.last_init_at, "
            "marketplace_sha = EXCLUDED.marketplace_sha, "
            "initial_workspace_sha = EXCLUDED.initial_workspace_sha, "
            "agnes_version_at_init = EXCLUDED.agnes_version_at_init",
            [user_email, now, marketplace_sha, initial_workspace_sha, agnes_version],
        )

    def delete_workdir_row(self, user_email: str) -> None:
        if self._workdirs_pg is not None:
            self._workdirs_pg.delete_workdir_row(user_email)
            return
        self._conn.execute("DELETE FROM user_workdirs WHERE user_email = ?", [user_email])

    def session_total_tokens(self, session_id: str) -> int:
        """Tokens charged against ``max_session_tokens`` for this session.

        Postgres counts ``tokens_in + tokens_out + cache_creation_tokens``
        (``src.llm_pricing.budget_tokens``'s definition — a cache write is
        real billed input); the frozen DuckDB backend has no such column and
        counts the two it has, so a DuckDB instance under-counts a
        cache-heavy session. Documented, not silently divergent.

        Used by ChatManager.send_user_message to enforce
        ChatConfig.max_session_tokens. A session row is a slow-changing
        rollup; counting at read time on every send_user_message is fine
        — DuckDB indexes on session_id and DuckDB is in-process.
        """
        if self._messages_pg is not None:
            return self._messages_pg.session_total_tokens(session_id)
        row = self._conn.execute(
            "SELECT COALESCE(SUM(COALESCE(tokens_in, 0) + COALESCE(tokens_out, 0)), 0) "
            "FROM chat_messages WHERE session_id = ?",
            [session_id],
        ).fetchone()
        return int(row[0] or 0)

    def daily_anthropic_tokens(self, user_email: str) -> tuple[int, int]:
        """Sum of tokens_in / tokens_out for this user's messages since UTC midnight.

        On Postgres ``tokens_in`` folds in ``cache_creation_tokens`` so this
        durable aggregate matches what the live daily counter is incremented
        by (see ``ChatManager._record_daily_tokens``); the frozen DuckDB
        backend has no such column and returns the raw pair.
        """
        if self._messages_pg is not None:
            return self._messages_pg.daily_anthropic_tokens(user_email)
        row = self._conn.execute(
            "SELECT COALESCE(SUM(m.tokens_in), 0), COALESCE(SUM(m.tokens_out), 0) "
            "FROM chat_messages m JOIN chat_sessions s ON m.session_id = s.id "
            "WHERE s.user_email = ? AND DATE_TRUNC('day', m.created_at) = DATE_TRUNC('day', CURRENT_TIMESTAMP)",
            [user_email],
        ).fetchone()
        return int(row[0] or 0), int(row[1] or 0)

    def cost_breakdown(
        self,
        *,
        since: Optional[datetime] = None,
        user_email: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """Per-(session, model) token sums for the cost readout.

        Postgres-only: the two prompt-cache columns the readout exists to
        expose have no DuckDB sibling (migration 0092, A3 freeze). Raises
        ``RequiresPostgresBackend`` on the DuckDB backend so the route
        answers a typed ``501`` rather than serving cache-blind zeros that
        would read as a measured "this workload used no cache".
        """
        if self._messages_pg is None:
            from src.repositories import RequiresPostgresBackend

            raise RequiresPostgresBackend("chat cost breakdown")
        return self._messages_pg.cost_breakdown(since=since, user_email=user_email, limit=limit)
