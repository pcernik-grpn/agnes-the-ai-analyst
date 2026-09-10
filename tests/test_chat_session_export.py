"""Tests for app/chat/session_export.py — F4, audit-full-coverage plan Task 8.

Covers:
- ``messages_to_turns()`` — the pure ChatMessage -> Claude-Code-jsonl-turn
  adapter (a fixture list of rows fed straight in, no DB), including a
  round-trip through the real readers the shape has to satisfy
  (``services/session_pipeline/lib.parse_jsonl`` +
  ``app/api/admin_sessions._render_transcript``).
- ``export_chat_session_jsonl()`` end-to-end through real repos (DuckDB
  backend, the default for this suite): export -> UsageProcessor ->
  ``usage_session_summary`` -> admin sessions list/transcript, plus the
  flag-off / no-messages / unresolvable-session / unresolvable-owner /
  ``RequiresPostgresBackend`` short-circuits.
- the session-pipeline pre-scan sweep (``services/session_pipeline/runner.py``
  ``_sweep_chat_session_exports``) discovering and exporting a session with
  no prior explicit call, and skipping an already-current export.
- ``is_chat_export_stale()`` / ``ensure_chat_transcript_current()`` — the
  freshness gap an operator hit on a production instance (measured
  2026-09-09): the sweep is the only thing that (re-)writes a chat
  session's jsonl while it stays live, so a transcript viewed between the
  last message and the next sweep tick used to read as "session not
  found" — indistinguishable from a session that never had a transcript
  at all. These two functions are the ONE shared staleness definition the
  sweep and the on-demand admin transcript viewer
  (``app/api/admin_sessions.py::transcript``) both use.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.api.admin_sessions import _render_transcript
from app.chat.session_export import (
    ensure_chat_transcript_current,
    export_chat_session_jsonl,
    is_chat_export_stale,
    messages_to_turns,
)
from app.chat.types import ChatMessage, Surface
from services.session_pipeline.lib import parse_jsonl


def _msg(**kw) -> ChatMessage:
    base = {
        "id": "msg_1",
        "session_id": "chat_1",
        "role": "user",
        "content": "",
        "tool_calls": None,
        "tokens_in": None,
        "tokens_out": None,
        "model": None,
        "created_at": datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
        "sender_email": None,
        "parts": None,
    }
    base.update(kw)
    return ChatMessage(**base)


class TestMessagesToTurns:
    def test_user_text_turn(self):
        m = _msg(id="msg_u1", role="user", content="what is our MRR?")
        turns = messages_to_turns("chat_abc", [m])
        assert len(turns) == 1
        t = turns[0]
        assert t["type"] == "user"
        assert t["sessionId"] == "chat_abc"
        assert t["message"]["role"] == "user"
        assert t["message"]["content"] == [{"type": "text", "text": "what is our MRR?"}]

    def test_assistant_turn_with_parts_text_and_tool_result(self):
        m = _msg(
            id="msg_a1",
            role="assistant",
            content="Let me check.\n\nMRR is $42k.",
            model="claude-sonnet-5",
            tokens_in=100,
            tokens_out=50,
            parts=[
                {"type": "text", "text": "Let me check."},
                {
                    "type": "tool",
                    "tool_use_id": "tu_1",
                    "tool": "query",
                    "args": {"sql": "SELECT ..."},
                    "state": "output-available",
                    "result": "42000",
                    "is_error": False,
                },
                {"type": "text", "text": "MRR is $42k."},
            ],
        )
        turns = messages_to_turns("chat_abc", [m])
        # assistant turn + a synthetic follow-up "user" turn for the result —
        # real Claude Code transcripts never put a tool result in the SAME
        # turn as its call.
        assert len(turns) == 2
        assistant_turn, result_turn = turns

        assert assistant_turn["type"] == "assistant"
        assert assistant_turn["message"]["model"] == "claude-sonnet-5"
        assert assistant_turn["message"]["usage"] == {"input_tokens": 100, "output_tokens": 50}
        blocks = assistant_turn["message"]["content"]
        assert blocks[0] == {"type": "text", "text": "Let me check."}
        assert blocks[1] == {
            "type": "tool_use",
            "id": "tu_1",
            "name": "query",
            "input": {"sql": "SELECT ..."},
        }
        assert blocks[2] == {"type": "text", "text": "MRR is $42k."}

        assert result_turn["type"] == "user"
        result_block = result_turn["message"]["content"][0]
        assert result_block == {
            "type": "tool_result",
            "tool_use_id": "tu_1",
            "is_error": False,
            "content": "42000",
        }

    def test_usage_carries_cache_token_halves_when_recorded(self):
        """Postgres rows record the prompt-cache halves (migration 0092);
        the export must forward them in the Anthropic usage shape or the
        transcript viewer's token line under-reports a cache-heavy chat
        session by exactly the dominant term. The sibling test above pins
        the other side: rows without them (frozen DuckDB backend) omit the
        keys rather than exporting a fake measured zero."""
        m = _msg(
            id="msg_a_cache",
            role="assistant",
            content="hi",
            tokens_in=10,
            tokens_out=5,
            cache_read_tokens=300,
            cache_creation_tokens=40,
            parts=[{"type": "text", "text": "hi"}],
        )
        turns = messages_to_turns("chat_abc", [m])
        assert turns[0]["message"]["usage"] == {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 300,
            "cache_creation_input_tokens": 40,
        }

    def test_tool_still_running_gets_no_result_turn(self):
        m = _msg(
            id="msg_a2",
            role="assistant",
            parts=[
                {
                    "type": "tool",
                    "tool_use_id": "tu_2",
                    "tool": "query",
                    "args": {},
                    "state": "input-available",
                }
            ],
        )
        turns = messages_to_turns("chat_abc", [m])
        assert len(turns) == 1

    def test_legacy_row_without_parts_falls_back_to_content_and_tool_calls(self):
        m = _msg(
            id="msg_a3",
            role="assistant",
            content="answer",
            tool_calls=[{"tool": "query", "args": {"sql": "SELECT 1"}}],
            parts=None,
        )
        turns = messages_to_turns("chat_abc", [m])
        assert len(turns) == 1
        blocks = turns[0]["message"]["content"]
        assert blocks[0] == {"type": "text", "text": "answer"}
        assert blocks[1]["type"] == "tool_use"
        assert blocks[1]["name"] == "query"

    def test_turns_round_trip_through_the_real_readers(self, tmp_path):
        """The contract: parse_jsonl + _render_transcript must accept our
        turns without special-casing — read those two before touching the
        shape (see the module docstring)."""
        messages = [
            _msg(id="msg_u1", role="user", content="hi"),
            _msg(
                id="msg_a1",
                role="assistant",
                content="hello\n\ndone",
                model="claude-sonnet-5",
                tokens_in=10,
                tokens_out=5,
                parts=[
                    {"type": "text", "text": "hello"},
                    {
                        "type": "tool",
                        "tool_use_id": "tu_1",
                        "tool": "Bash",
                        "args": {"command": "ls"},
                        "state": "output-available",
                        "result": "file.txt",
                        "is_error": False,
                    },
                    {"type": "text", "text": "done"},
                ],
            ),
        ]
        turns = messages_to_turns("chat_abc", messages)

        f = tmp_path / "session.jsonl"
        f.write_text("\n".join(json.dumps(t, default=str) for t in turns) + "\n")
        parsed = parse_jsonl(f)
        assert parsed == turns

        events = _render_transcript(parsed)
        # The trailing "done" text (same ChatMessage, after the tool call)
        # renders BEFORE the tool_result turn — see messages_to_turns'
        # docstring for why a mid-message tool boundary isn't split into
        # its own turn. The tool_use and tool_result blocks themselves
        # still pair up correctly (asserted below).
        assert [e["kind"] for e in events] == ["text", "text", "tool_use", "text", "tool_result"]
        tool_use = next(e for e in events if e["kind"] == "tool_use")
        assert tool_use["tool_name"] == "Bash"
        tool_result = next(e for e in events if e["kind"] == "tool_result")
        assert tool_result["tool_use_id"] == tool_use["tool_use_id"]
        assert tool_result["is_error"] is False


class TestExportChatSessionJsonl:
    @staticmethod
    def _seed_chat_session(owner_email: str) -> str:
        from src.repositories import chat_message_repo, chat_session_repo

        s = chat_session_repo().create_session(user_email=owner_email, surface=Surface.WEB)
        chat_message_repo().append_message(session_id=s.id, role="user", content="hi")
        chat_message_repo().append_message(
            session_id=s.id,
            role="assistant",
            content="hello",
            model="claude-sonnet-5",
            tokens_in=3,
            tokens_out=2,
            parts=[{"type": "text", "text": "hello"}],
        )
        return s.id

    def test_flag_off_returns_none_and_writes_nothing(self, seeded_app, tmp_path, monkeypatch):
        session_dir = tmp_path / "user_sessions"
        monkeypatch.setenv("SESSION_DATA_DIR", str(session_dir))
        monkeypatch.setenv("AGNES_SESSIONS_INCLUDE_CHAT", "false")
        chat_id = self._seed_chat_session("analyst@test.com")

        assert export_chat_session_jsonl(chat_id) is None
        assert not session_dir.exists()

    def test_no_messages_returns_none(self, seeded_app, tmp_path, monkeypatch):
        monkeypatch.setenv("SESSION_DATA_DIR", str(tmp_path / "user_sessions"))
        from src.repositories import chat_session_repo

        s = chat_session_repo().create_session(user_email="analyst@test.com", surface=Surface.WEB)
        assert export_chat_session_jsonl(s.id) is None

    def test_unresolvable_session_returns_none(self, seeded_app, tmp_path, monkeypatch):
        monkeypatch.setenv("SESSION_DATA_DIR", str(tmp_path / "user_sessions"))
        assert export_chat_session_jsonl("chat_does_not_exist") is None

    def test_unresolvable_owner_returns_none(self, seeded_app, tmp_path, monkeypatch):
        monkeypatch.setenv("SESSION_DATA_DIR", str(tmp_path / "user_sessions"))
        chat_id = self._seed_chat_session("nobody@nowhere.example")
        assert export_chat_session_jsonl(chat_id) is None

    def test_requires_postgres_backend_is_caught(self, seeded_app, tmp_path, monkeypatch):
        """PG-only reality check (Task 8 Interfaces): a RequiresPostgresBackend
        raised anywhere in the repo-resolution chain must come back as a
        clean None, never propagate."""
        monkeypatch.setenv("SESSION_DATA_DIR", str(tmp_path / "user_sessions"))
        chat_id = self._seed_chat_session("analyst@test.com")

        from src.repositories import RequiresPostgresBackend

        def _raise(*_a, **_kw):
            raise RequiresPostgresBackend("chat_message")

        monkeypatch.setattr("src.repositories.chat_message_repo", _raise)
        assert export_chat_session_jsonl(chat_id) is None

    def test_export_then_pipeline_then_admin_surfaces(self, seeded_app, tmp_path, monkeypatch):
        session_dir = tmp_path / "user_sessions"
        monkeypatch.setenv("SESSION_DATA_DIR", str(session_dir))
        chat_id = self._seed_chat_session("analyst@test.com")

        result = export_chat_session_jsonl(chat_id)
        assert result is not None
        assert result.exists()
        assert result.name == f"chat-{chat_id}.jsonl"
        assert result.parent.name == "analyst1"

        from services.session_pipeline.runner import run_processor
        from services.session_processors.usage import UsageProcessor
        from src.db import get_system_db
        from src.repositories import audit_repo, usage_repo

        conn = get_system_db()
        stats = run_processor(conn, UsageProcessor(), session_data_dir=session_dir)
        assert stats["processed"] >= 1

        session_file = f"{result.parent.name}/chat-{chat_id}.jsonl"
        summary = usage_repo().get_session_summary(session_file)
        assert summary is not None
        assert summary["input_tokens"] == 3
        assert summary["output_tokens"] == 2

        # chat.session_exported audit row was written on export.
        rows, _ = audit_repo().query(action="chat.session_exported", limit=10)
        assert any(r["resource"] == f"session:{chat_id}" for r in rows)

        client = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = client.get(
            "/api/admin/sessions/list",
            headers={"Authorization": f"Bearer {token}"},
            params={"since_minutes": 10080},
        )
        assert resp.status_code == 200, resp.text
        rows = resp.json()["rows"]
        match = next((r for r in rows if r["session_file"] == session_file), None)
        assert match is not None, f"exported session missing from listing: {rows}"

        resp2 = client.get(
            f"/api/admin/sessions/{session_file}/transcript",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp2.status_code == 200, resp2.text
        events = resp2.json()["events"]
        assert events

    def test_export_includes_messages_past_the_page_size(self, seeded_app, tmp_path, monkeypatch):
        """``list_messages`` defaults to a 500-row page, oldest-first: a
        chat with more messages than that must not be silently truncated to
        the first page, or the export's mtime says "current" while the
        newest messages are unreachable through the admin transcript
        viewer. Shrinks the paging window rather than seeding 500+ real
        rows -- the truncation bug reproduces at any page size."""
        monkeypatch.setenv("SESSION_DATA_DIR", str(tmp_path / "user_sessions"))
        monkeypatch.setattr("app.chat.session_export._EXPORT_PAGE_SIZE", 3)
        from src.repositories import chat_message_repo, chat_session_repo

        s = chat_session_repo().create_session(user_email="analyst@test.com", surface=Surface.WEB)
        n_messages = 7  # more than double the shrunk page size, incl. a non-full final page
        for i in range(n_messages):
            chat_message_repo().append_message(session_id=s.id, role="user", content=f"message {i}")

        result = export_chat_session_jsonl(s.id)

        assert result is not None
        lines = result.read_text().splitlines()
        assert len(lines) == n_messages
        texts = [json.loads(line)["message"]["content"][0]["text"] for line in lines]
        # oldest-first, nothing dropped, nothing reordered.
        assert texts == [f"message {i}" for i in range(n_messages)]

    def test_export_mtime_stays_at_write_time_for_backdated_messages(self, seeded_app, tmp_path, monkeypatch):
        """A message's own ``created_at`` can be far in the past relative to
        the moment it finally gets (re-)exported -- exactly what happens
        when this fix backfills a previously 500-row-truncated
        conversation's missing tail long after those messages were sent.
        The exported jsonl's own mtime must stay at the real write time
        regardless: ``services/session_processor_state.py::scan_unprocessed_for``
        gates reprocessing on a file's mtime advancing past its previously
        recorded ``processed_at``, and a file backdated to old message
        content would look untouched to that gate and never get
        reprocessed, even though its content just changed."""
        monkeypatch.setenv("SESSION_DATA_DIR", str(tmp_path / "user_sessions"))
        chat_id = self._seed_chat_session("analyst@test.com")

        from src.db import get_system_db

        conn = get_system_db()
        backdated = datetime.now(UTC) - timedelta(hours=2)
        conn.execute("UPDATE chat_messages SET created_at = ? WHERE session_id = ?", [backdated, chat_id])

        before = datetime.now(UTC)
        result = export_chat_session_jsonl(chat_id)
        after = datetime.now(UTC)

        assert result is not None
        mtime = datetime.fromtimestamp(result.stat().st_mtime, tz=UTC)
        assert before - timedelta(seconds=5) <= mtime <= after + timedelta(seconds=5), (
            f"mtime {mtime} was not close to the real write window [{before}, {after}] "
            f"-- looks backdated to the messages' own (2h-old) timestamp"
        )


class TestSessionPipelineSweep:
    def test_sweep_exports_without_explicit_call(self, seeded_app, tmp_path, monkeypatch):
        session_dir = tmp_path / "user_sessions"
        monkeypatch.setenv("SESSION_DATA_DIR", str(session_dir))
        chat_id = TestExportChatSessionJsonl._seed_chat_session("analyst@test.com")

        from services.session_pipeline.runner import run_processor
        from services.session_processors.usage import UsageProcessor
        from src.db import get_system_db

        conn = get_system_db()
        run_processor(conn, UsageProcessor(), session_data_dir=session_dir)

        matches = list(session_dir.glob(f"*/chat-{chat_id}.jsonl"))
        assert len(matches) == 1

    def test_sweep_skips_already_current_export(self, seeded_app, tmp_path, monkeypatch):
        session_dir = tmp_path / "user_sessions"
        monkeypatch.setenv("SESSION_DATA_DIR", str(session_dir))
        TestExportChatSessionJsonl._seed_chat_session("analyst@test.com")

        from services.session_pipeline.runner import _sweep_chat_session_exports

        first = _sweep_chat_session_exports(session_dir)
        assert first == 1
        second = _sweep_chat_session_exports(session_dir)
        assert second == 0

    def test_sweep_noop_when_flag_off(self, seeded_app, tmp_path, monkeypatch):
        session_dir = tmp_path / "user_sessions"
        monkeypatch.setenv("SESSION_DATA_DIR", str(session_dir))
        monkeypatch.setenv("AGNES_SESSIONS_INCLUDE_CHAT", "false")
        TestExportChatSessionJsonl._seed_chat_session("analyst@test.com")

        from services.session_pipeline.runner import _sweep_chat_session_exports

        assert _sweep_chat_session_exports(session_dir) == 0
        assert not session_dir.exists()


class TestConcurrentExportsNeverInterleave:
    """Every writer of a chat session's exported jsonl -- the periodic
    sweep, the kill/archive teardown hooks, and the on-demand admin
    transcript route -- can race the SAME target file, potentially from
    different processes with no cross-process lock between them. The fix
    is a per-call, globally-unique temp name (see
    ``session_export._atomic_write_text``), never a fixed ``<target>.tmp``.

    Modeled without real OS threads/processes, matching
    ``tests/test_parquet_publish.py::test_two_concurrent_writers_do_not_
    clobber_each_others_temp`` (incident #1274) -- two "writers" are
    distinguished by a mocked ``uuid.uuid4()`` rather than real concurrent
    execution, which is deterministic and exercises exactly the same
    code path a genuine race would."""

    def test_two_concurrent_writers_do_not_share_or_clobber_a_temp_file(self, tmp_path, monkeypatch):
        from app.chat import session_export

        target = tmp_path / "chat-abc123.jsonl"

        # Writer A starts and is mid-write -- its temp exists on disk but it
        # has not yet called os.replace.
        monkeypatch.setattr(session_export.uuid, "uuid4", lambda: uuid.UUID(int=1))
        tmp_a = target.with_name(f"{target.name}.{uuid.UUID(int=1).hex}.tmp")
        tmp_a.write_text("A-IN-FLIGHT", encoding="utf-8")

        # Writer B (a different racer -- e.g. the on-demand transcript route
        # firing while the sweep is also mid-export) starts and finishes
        # cleanly while A's temp is still sitting on disk.
        monkeypatch.setattr(session_export.uuid, "uuid4", lambda: uuid.UUID(int=2))
        session_export._atomic_write_text(target, "B-CONTENT\n")

        assert target.read_text(encoding="utf-8") == "B-CONTENT\n"
        assert tmp_a.read_text(encoding="utf-8") == "A-IN-FLIGHT", "writer B's publish touched writer A's temp"

        # Writer A now completes. Its commit is a full, independent write --
        # never a merge with B's bytes -- and replaces `target` cleanly.
        monkeypatch.setattr(session_export.uuid, "uuid4", lambda: uuid.UUID(int=1))
        session_export._atomic_write_text(target, "A-CONTENT\n")
        assert target.read_text(encoding="utf-8") == "A-CONTENT\n"

        # No stray temp left behind by either writer.
        assert list(target.parent.glob(f"{target.name}.*.tmp")) == []

    def test_a_failed_writer_only_cleans_up_its_own_temp(self, tmp_path, monkeypatch):
        """The #1274 bug this mirrors: a shared temp name let one writer's
        failure-cleanup delete the OTHER writer's already-published file.
        Unique-per-call names make that structurally impossible -- a
        failure here must remove only its own temp and leave a
        concurrently-published `target` untouched."""
        from app.chat import session_export

        target = tmp_path / "chat-abc123.jsonl"

        monkeypatch.setattr(session_export.uuid, "uuid4", lambda: uuid.UUID(int=2))
        session_export._atomic_write_text(target, "B-CONTENT\n")
        assert target.read_text(encoding="utf-8") == "B-CONTENT\n"

        monkeypatch.setattr(session_export.uuid, "uuid4", lambda: uuid.UUID(int=1))
        tmp_a = target.with_name(f"{target.name}.{uuid.UUID(int=1).hex}.tmp")

        def _broken_write_text(self, content, encoding="utf-8"):
            raise OSError("disk full")

        monkeypatch.setattr(type(tmp_a), "write_text", _broken_write_text)
        with pytest.raises(OSError):
            session_export._atomic_write_text(target, "A-CONTENT\n")

        assert not tmp_a.exists()
        assert target.read_text(encoding="utf-8") == "B-CONTENT\n", "a failed writer must not touch B's publish"

    def test_export_chat_session_jsonl_leaves_no_stray_temp_across_two_calls(self, seeded_app, tmp_path, monkeypatch):
        """End-to-end: two exports of the SAME chat (e.g. the sweep and the
        on-demand admin route racing each other) each get their own temp
        name and neither leaves litter behind, whichever finishes last."""
        from app.chat import session_export

        monkeypatch.setenv("SESSION_DATA_DIR", str(tmp_path / "user_sessions"))
        chat_id = TestExportChatSessionJsonl._seed_chat_session("analyst@test.com")

        seen_uuids = []
        real_uuid4 = uuid.uuid4

        def _tracked_uuid4():
            u = real_uuid4()
            seen_uuids.append(u)
            return u

        monkeypatch.setattr(session_export.uuid, "uuid4", _tracked_uuid4)

        first = session_export.export_chat_session_jsonl(chat_id)
        second = session_export.export_chat_session_jsonl(chat_id)

        assert first is not None
        assert second is not None
        assert len(seen_uuids) == len(set(seen_uuids)), "every write must mint its own unique temp name"
        assert list(first.parent.glob(f"{first.name}*.tmp")) == []
        # The published file itself is intact, valid jsonl.
        for line in first.read_text(encoding="utf-8").splitlines():
            json.loads(line)


def _stamp_watermark(f, last_message_at, messages):
    """Write a sidecar that legitimately PAIRS with ``f``'s current content.

    Hand-built watermarks have to carry the digest of the transcript they
    sit beside, or :func:`session_export.is_chat_export_stale` reports the
    pair unverifiable (which is the whole point of the digest -- see
    ``session_export._content_digest``). Every test below that wants a
    *valid* watermark goes through here; a test about an INVALID pair
    writes the sidecar itself."""
    from app.chat import session_export

    session_export._write_export_watermark(
        f,
        session_export.ExportWatermark(
            last_message_at,
            messages=messages,
            content_sha256=session_export._content_digest(f.read_text(encoding="utf-8")),
        ),
    )


class TestIsChatExportStale:
    """The one staleness rule shared by the periodic sweep and the
    on-demand admin transcript viewer. Coverage is read from the sidecar
    ``.watermark`` file :func:`session_export._write_export_watermark`
    writes beside the export -- NOT the export's own mtime, which means
    "when was this written" to a different reader
    (``session_processor_state.scan_unprocessed_for``'s prefilter)."""

    def test_missing_file_is_stale(self, tmp_path):
        assert is_chat_export_stale(tmp_path / "nope.jsonl", datetime.now(UTC))

    def test_no_last_message_at_is_never_stale(self, tmp_path):
        assert not is_chat_export_stale(tmp_path / "nope.jsonl", None)

    def test_watermark_at_or_after_last_message_is_current(self, tmp_path):
        f = tmp_path / "f.jsonl"
        f.write_text("x")
        _stamp_watermark(f, datetime.now(UTC), messages=1)
        assert not is_chat_export_stale(f, datetime.now(UTC) - timedelta(hours=1))

    def test_watermark_before_last_message_is_stale(self, tmp_path):
        f = tmp_path / "f.jsonl"
        f.write_text("x")
        _stamp_watermark(f, datetime.now(UTC) - timedelta(hours=2), messages=1)
        assert is_chat_export_stale(f, datetime.now(UTC) + timedelta(hours=1))

    def test_file_present_without_a_watermark_sidecar_is_stale(self, tmp_path):
        """No coverage marker at all (a pre-this-change export, or a
        sidecar that failed to write) -- treated as unknown coverage,
        which is the safe direction: one extra re-export, never a file
        trusted with no way to vouch for its content."""
        f = tmp_path / "f.jsonl"
        f.write_text("x")
        assert is_chat_export_stale(f, datetime.now(UTC) - timedelta(hours=1))

    def test_backdated_file_mtime_does_not_fool_staleness(self, tmp_path):
        """The regression this class guards against: an export's mtime
        must play no role in the staleness decision at all -- only the
        watermark sidecar does. A file whose mtime is set far in the past
        (mirroring the OLD stamp-mtime-to-content behavior) is still
        correctly read as current once its watermark covers the message."""
        import os

        f = tmp_path / "f.jsonl"
        f.write_text("x")
        last_message_at = datetime.now(UTC) - timedelta(hours=1)
        _stamp_watermark(f, last_message_at, messages=1)
        old = (datetime.now(UTC) - timedelta(days=30)).timestamp()
        os.utime(f, (old, old))
        assert not is_chat_export_stale(f, last_message_at)


class TestWatermarkTieBreaking:
    """A message committed mid-export can carry the SAME ``created_at`` as
    the newest message the export wrote. Against the timestamp alone it is
    invisible forever -- the strict ``>`` never fires and the session's own
    ``last_message_at`` equals the recorded watermark -- so the watermark
    also records how many messages the file contains."""

    def test_a_message_tied_on_timestamp_is_stale_not_current(self, tmp_path):
        f = tmp_path / "f.jsonl"
        f.write_text("x")
        tied = datetime.now(UTC)
        _stamp_watermark(f, tied, messages=2)

        # Same instant, one more row than we wrote: the tie.
        assert is_chat_export_stale(f, tied, 3)
        # Same instant, same count: genuinely current.
        assert not is_chat_export_stale(f, tied, 2)

    def test_a_count_that_drifted_low_does_not_re_export_forever(self, tmp_path):
        """`>`, never `!=` -- a count below what we wrote must not put the
        sweep into a re-export loop on every tick."""
        f = tmp_path / "f.jsonl"
        f.write_text("x")
        tied = datetime.now(UTC)
        _stamp_watermark(f, tied, messages=5)

        assert not is_chat_export_stale(f, tied, 3)

    def test_a_pre_count_sidecar_still_reads_as_a_timestamp(self, tmp_path):
        """The older bare-timestamp sidecar shape still PARSES -- the
        timestamp half survives the read, so nothing downstream has to
        special-case it. It cannot CERTIFY the file, though: with no
        content digest it has no way to say whether the transcript beside
        it is the one it describes, which is the unverifiable case
        :func:`is_chat_export_stale` resolves as stale (see
        ``session_export._content_digest``)."""
        from app.chat import session_export

        f = tmp_path / "f.jsonl"
        f.write_text("x")
        written = datetime.now(UTC) - timedelta(hours=1)
        session_export._watermark_path(f).write_text(written.isoformat(), encoding="utf-8")

        parsed = session_export._read_export_watermark(f)
        assert parsed.last_message_at == written
        assert parsed.messages is None
        assert parsed.content_sha256 is None

        assert is_chat_export_stale(f, written, 7)

    def test_a_corrupt_sidecar_is_stale_rather_than_trusted(self, tmp_path):
        from app.chat import session_export

        f = tmp_path / "f.jsonl"
        f.write_text("x")
        session_export._watermark_path(f).write_text("{not json and not a date", encoding="utf-8")

        assert session_export._read_export_watermark(f) is None
        assert is_chat_export_stale(f, datetime.now(UTC), 1)


class TestWatermarkIsPairedToItsTranscript:
    """The transcript and its sidecar are two files. Each ``os.replace`` is
    atomic; the PAIR is not, and nothing serializes the three writers that
    can export the same session (the sweep, a teardown hook, the on-demand
    viewer). Two overlapping exports can therefore publish writer A's older
    transcript beside writer B's newer watermark -- and a watermark
    vouching for coverage the file next to it does not have is worse than
    no watermark at all: the strict ``>`` never fires, the count matches
    the one B wrote, and the session reads as current forever with A's
    truncated content on disk.

    The sidecar carries a SHA-256 of the transcript it describes, so a pair
    that does not belong together is simply stale and the next export heals
    it. Detection, not locking: no inter-process lock, and no change to the
    jsonl shape ``services/session_pipeline/lib.parse_jsonl`` consumes."""

    def test_an_export_records_the_digest_of_what_it_actually_wrote(self, seeded_app, tmp_path, monkeypatch):
        from app.chat import session_export

        monkeypatch.setenv("SESSION_DATA_DIR", str(tmp_path / "user_sessions"))
        chat_id = TestExportChatSessionJsonl._seed_chat_session("analyst@test.com")

        result = session_export.export_chat_session_jsonl(chat_id)

        assert result is not None
        watermark = session_export._read_export_watermark(result)
        assert watermark.content_sha256 == session_export._content_digest(result.read_text(encoding="utf-8"))

    def test_a_watermark_left_beside_another_writers_transcript_is_stale(self, tmp_path):
        """The interleaved pair, built directly: B's watermark (newer
        timestamp, higher count -- everything the old check looked at says
        "current") sitting on top of A's shorter transcript."""
        f = tmp_path / "f.jsonl"
        writer_b_content = '{"turn": 1}\n{"turn": 2}\n'
        f.write_text(writer_b_content, encoding="utf-8")
        newest = datetime.now(UTC)
        _stamp_watermark(f, newest, messages=2)

        # Sanity: the pair as published is current.
        assert not is_chat_export_stale(f, newest, 2)

        # Now writer A's `os.replace` lands last, after B's sidecar.
        f.write_text('{"turn": 1}\n', encoding="utf-8")

        assert is_chat_export_stale(f, newest, 2)

    def test_the_reverse_interleaving_is_stale_too(self, tmp_path):
        """A's older sidecar landing last, on top of B's fuller transcript.
        Harmless in itself -- but it must not read as verified either, or
        the check would be trusting a sidecar it cannot tie to the file."""
        f = tmp_path / "f.jsonl"
        f.write_text('{"turn": 1}\n', encoding="utf-8")
        older = datetime.now(UTC) - timedelta(hours=1)
        _stamp_watermark(f, older, messages=1)
        f.write_text('{"turn": 1}\n{"turn": 2}\n', encoding="utf-8")

        assert is_chat_export_stale(f, older, 1)

    def test_a_sidecar_carrying_no_digest_cannot_certify_the_file(self, tmp_path):
        """A JSON sidecar of the pre-digest shape: both halves present and
        agreeing, still unverifiable. Treated exactly like a missing
        sidecar -- one extra re-export, never a file trusted with no way
        to tie the watermark to its bytes."""
        from app.chat import session_export

        f = tmp_path / "f.jsonl"
        f.write_text("x")
        written = datetime.now(UTC)
        session_export._watermark_path(f).write_text(
            json.dumps({"last_message_at": written.isoformat(), "messages": 3}), encoding="utf-8"
        )

        assert session_export._read_export_watermark(f).content_sha256 is None
        assert is_chat_export_stale(f, written, 3)

    def test_the_next_export_heals_a_mismatched_pair(self, seeded_app, tmp_path, monkeypatch):
        """End-to-end: the interleaving above, then the ordinary on-demand
        refresh. A mismatch only ever costs one re-export."""
        from app.chat import session_export

        monkeypatch.setenv("SESSION_DATA_DIR", str(tmp_path / "user_sessions"))
        chat_id = TestExportChatSessionJsonl._seed_chat_session("analyst@test.com")

        published = session_export.export_chat_session_jsonl(chat_id)
        assert published is not None
        good = published.read_text(encoding="utf-8")

        # An older writer's transcript lands after the current sidecar.
        published.write_text(good.splitlines(keepends=True)[0], encoding="utf-8")
        assert published.read_text(encoding="utf-8") != good

        freshness = session_export.ensure_chat_transcript_current(chat_id)

        assert freshness.path is not None
        assert freshness.path.read_text(encoding="utf-8") == good
        # And the healed pair is verified again, not stale on every tick.
        from src.repositories import chat_session_repo

        session = chat_session_repo().get_session(chat_id)
        assert not is_chat_export_stale(freshness.path, session.last_message_at, session.message_count)


class TestExportRaceWithConcurrentInsert:
    """The writer reads messages, then replaces the file -- a message
    committed in that window is absent from the file even though it is
    OLDER than the moment the file finished being written. If the file's
    mtime were left at that write-completion instant, the missed message
    would be permanently invisible to ``is_chat_export_stale``: newer than
    "now" minus the write duration is still older than "now". The export
    must instead stamp the file's mtime to the newest message it actually
    included, so a raced-in message -- necessarily newer than that
    watermark -- is caught on the very next staleness check."""

    def test_message_committed_mid_export_is_missing_but_detected_stale(self, seeded_app, tmp_path, monkeypatch):
        from app.chat import session_export

        monkeypatch.setenv("SESSION_DATA_DIR", str(tmp_path / "user_sessions"))
        chat_id = TestExportChatSessionJsonl._seed_chat_session("analyst@test.com")

        from src.repositories import chat_message_repo, chat_session_repo

        real_list_all = session_export._list_all_chat_messages

        def _racy_list_all(chat_id_, repo):
            # A second request commits a new message right after this
            # export already fetched its message list, but before the
            # file below gets written and replaced.
            messages = real_list_all(chat_id_, repo)
            chat_message_repo().append_message(session_id=chat_id_, role="user", content="raced in mid-export")
            return messages

        monkeypatch.setattr(session_export, "_list_all_chat_messages", _racy_list_all)

        result = session_export.export_chat_session_jsonl(chat_id)

        assert result is not None
        # The raced-in message lost the race against this export's read --
        # a reader opening the file right now would not see it.
        assert "raced in mid-export" not in result.read_text()

        session = chat_session_repo().get_session(chat_id)
        assert session_export.is_chat_export_stale(result, session.last_message_at)

    def test_ensure_current_heals_a_transcript_that_raced_a_write(self, seeded_app, tmp_path, monkeypatch):
        """End-to-end: the same race, but observed through the admin-facing
        entry point -- a stale export (even one whose mtime postdates the
        write) is re-exported and the previously-missing message appears."""
        from app.chat import session_export

        monkeypatch.setenv("SESSION_DATA_DIR", str(tmp_path / "user_sessions"))
        chat_id = TestExportChatSessionJsonl._seed_chat_session("analyst@test.com")

        from src.repositories import chat_message_repo

        real_list_all = session_export._list_all_chat_messages

        def _racy_list_all(chat_id_, repo):
            messages = real_list_all(chat_id_, repo)
            chat_message_repo().append_message(session_id=chat_id_, role="user", content="raced in mid-export")
            return messages

        monkeypatch.setattr(session_export, "_list_all_chat_messages", _racy_list_all)
        first = session_export.export_chat_session_jsonl(chat_id)
        assert first is not None
        assert "raced in mid-export" not in first.read_text()

        # Un-patch: a later call reads whatever is really in the DB now,
        # including the message the race above committed.
        monkeypatch.setattr(session_export, "_list_all_chat_messages", real_list_all)

        freshness = session_export.ensure_chat_transcript_current(chat_id)

        assert freshness.path is not None
        assert "raced in mid-export" in freshness.path.read_text()


class TestReexportSurvivesPipelinePrefilter:
    """``session_processor_state.scan_unprocessed_for`` uses a jsonl's mtime
    as a cheap "already processed" prefilter, comparing it against a stored
    ``processed_at``. An earlier version of ``export_chat_session_jsonl``
    stamped that mtime with a CONTENT watermark (the newest message's
    ``created_at``) instead of leaving it at the real write time -- so a
    re-export whose watermark happened to predate an earlier processing
    pass's ``processed_at`` (which is always a real wall-clock instant,
    hence normally later than any message it covers) looked untouched to
    the prefilter even though the file's bytes had just changed, and the
    usage rollups it feeds stayed truncated forever. The fix leaves mtime
    alone as the write time and carries coverage in a separate sidecar
    (see ``session_export._write_export_watermark``), so this can no
    longer happen regardless of how old the messages inside the file are.
    """

    def test_reexport_with_backdated_messages_is_not_hidden_from_the_scan(self, seeded_app, tmp_path, monkeypatch):
        from src.db import get_system_db
        from src.repositories.session_processor_state import SessionProcessorStateRepository

        session_dir = tmp_path / "user_sessions"
        monkeypatch.setenv("SESSION_DATA_DIR", str(session_dir))
        chat_id = TestExportChatSessionJsonl._seed_chat_session("analyst@test.com")

        conn = get_system_db()
        # Backdate every message in the session far into the past -- the
        # coverage watermark this export carries is therefore old too, even
        # though the export itself is about to happen right now.
        conn.execute(
            "UPDATE chat_messages SET created_at = TIMESTAMP '2020-01-01 00:00:00' WHERE session_id = ?",
            [chat_id],
        )

        result = export_chat_session_jsonl(chat_id)
        assert result is not None
        session_file = f"{result.parent.name}/{result.name}"

        state = SessionProcessorStateRepository(conn)
        # A prior processing pass recorded its own ``processed_at`` well
        # after those (backdated) messages -- exactly what a state row
        # looks like once a real pipeline run has processed this export.
        state.mark_processed(
            "usage",
            session_file,
            result.parent.name,
            1,
            "deadbeef",
            read_at=datetime(2024, 1, 1, tzinfo=UTC),
        )

        unprocessed = state.scan_unprocessed_for("usage", session_dir)
        matches = [p for _, p in unprocessed if p == result]
        assert matches, "a re-exported file must still be surfaced even when its messages are old"

    def test_reexport_end_to_end_through_run_processor(self, seeded_app, tmp_path, monkeypatch):
        """Same gap, exercised through the real pipeline entry point rather
        than the repository directly: a session already marked processed
        gets new (backdated) content and the next tick must still pick it
        up and update the usage rollup, not skip it."""
        from services.session_pipeline.runner import run_processor
        from services.session_processors.usage import UsageProcessor
        from src.db import get_system_db
        from src.repositories import chat_message_repo, usage_repo
        from src.repositories.session_processor_state import SessionProcessorStateRepository

        session_dir = tmp_path / "user_sessions"
        monkeypatch.setenv("SESSION_DATA_DIR", str(session_dir))
        chat_id = TestExportChatSessionJsonl._seed_chat_session("analyst@test.com")

        conn = get_system_db()
        run_processor(conn, UsageProcessor(), session_data_dir=session_dir)

        result = export_chat_session_jsonl(chat_id)
        assert result is not None
        session_file = f"{result.parent.name}/{result.name}"
        summary_before = usage_repo().get_session_summary(session_file)
        assert summary_before is not None
        assert summary_before["output_tokens"] == 2

        # A new message arrives, then gets backdated (mirrors a session
        # whose true last message predates when the state row was written
        # -- the truncated-then-corrected-export scenario this closes).
        chat_message_repo().append_message(
            session_id=chat_id,
            role="assistant",
            content="more",
            model="claude-sonnet-5",
            tokens_in=5,
            tokens_out=7,
            parts=[{"type": "text", "text": "more"}],
        )
        conn.execute(
            "UPDATE chat_messages SET created_at = TIMESTAMP '2020-01-01 00:00:00' WHERE session_id = ?",
            [chat_id],
        )

        # Force the existing state row's processed_at to postdate every
        # (backdated) message -- what a real prior pipeline run's
        # wall-clock ``processed_at`` looks like relative to old content.
        # ``version`` must match the real processor's declared version, or
        # the version-mismatch branch alone would force a reprocess and the
        # test would pass without ever exercising the mtime-vs-processed_at
        # comparison this closes.
        SessionProcessorStateRepository(conn).mark_processed(
            "usage",
            session_file,
            result.parent.name,
            summary_before["output_tokens"],
            "deadbeef",
            read_at=datetime(2024, 1, 1, tzinfo=UTC),
            version=UsageProcessor().version,
        )

        export_chat_session_jsonl(chat_id)
        stats = run_processor(conn, UsageProcessor(), session_data_dir=session_dir)
        assert stats["processed"] >= 1

        summary_after = usage_repo().get_session_summary(session_file)
        assert summary_after["output_tokens"] == 9  # 2 + 7, the reprocess picked up the new message


class TestOnDemandTranscriptFreshness:
    """The freshness gap measured on a production instance (2026-09-09): the
    export sweep only (re-)writes a chat session's jsonl on its own cadence
    (``SCHEDULER_USAGE_PROCESSOR_INTERVAL``, default 10 minutes), so an
    operator opening the admin transcript viewer in the window between the
    last message and the next tick used to get an indistinguishable "session
    not found" whether the transcript was merely pending or genuinely never
    existed. ``GET .../transcript`` now brings a stale-or-missing chat
    export current on demand (``ensure_chat_transcript_current``) and, when
    it genuinely cannot, returns a structured 404 that names *why*.
    """

    def _get_transcript(self, seeded_app, username: str, session_file: str):
        return seeded_app["client"].get(
            f"/api/admin/sessions/{username}/{session_file}/transcript",
            headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        )

    def test_serves_a_session_that_was_never_exported_yet(self, seeded_app, tmp_path, monkeypatch):
        session_dir = tmp_path / "user_sessions"
        monkeypatch.setenv("SESSION_DATA_DIR", str(session_dir))
        chat_id = TestExportChatSessionJsonl._seed_chat_session("analyst@test.com")
        assert not session_dir.exists()  # sweep has never run

        resp = self._get_transcript(seeded_app, "analyst1", f"chat-{chat_id}.jsonl")

        assert resp.status_code == 200, resp.text
        assert resp.json()["events"]
        assert (session_dir / "analyst1" / f"chat-{chat_id}.jsonl").is_file()

    def test_serves_fresh_content_when_the_export_is_stale(self, seeded_app, tmp_path, monkeypatch):
        from src.repositories import chat_message_repo

        session_dir = tmp_path / "user_sessions"
        monkeypatch.setenv("SESSION_DATA_DIR", str(session_dir))
        chat_id = TestExportChatSessionJsonl._seed_chat_session("analyst@test.com")
        first = export_chat_session_jsonl(chat_id)
        assert first is not None

        # A new message lands after that export -- the sweep hasn't ticked
        # again, so the file on disk is now stale.
        chat_message_repo().append_message(session_id=chat_id, role="user", content="one more thing")

        resp = self._get_transcript(seeded_app, "analyst1", f"chat-{chat_id}.jsonl")

        assert resp.status_code == 200, resp.text
        texts = [e.get("text") for e in resp.json()["events"] if e.get("kind") == "text"]
        assert any("one more thing" in (t or "") for t in texts), texts

    def test_current_export_is_served_without_reexporting(self, seeded_app, tmp_path, monkeypatch):
        session_dir = tmp_path / "user_sessions"
        monkeypatch.setenv("SESSION_DATA_DIR", str(session_dir))
        chat_id = TestExportChatSessionJsonl._seed_chat_session("analyst@test.com")
        first = export_chat_session_jsonl(chat_id)
        assert first is not None
        mtime_before = first.stat().st_mtime

        resp = self._get_transcript(seeded_app, "analyst1", f"chat-{chat_id}.jsonl")

        assert resp.status_code == 200, resp.text
        # Already current -- no pointless re-export/re-write.
        assert first.stat().st_mtime == mtime_before

    def test_404_names_export_disabled_and_does_no_pointless_work(self, seeded_app, tmp_path, monkeypatch):
        session_dir = tmp_path / "user_sessions"
        monkeypatch.setenv("SESSION_DATA_DIR", str(session_dir))
        monkeypatch.setenv("AGNES_SESSIONS_INCLUDE_CHAT", "false")
        chat_id = TestExportChatSessionJsonl._seed_chat_session("analyst@test.com")

        resp = self._get_transcript(seeded_app, "analyst1", f"chat-{chat_id}.jsonl")

        assert resp.status_code == 404
        detail = resp.json()["detail"]
        assert detail["error"] == "chat_transcript_export_disabled"
        assert not session_dir.exists()  # never attempted a write

    def test_404_names_unknown_session_not_found(self, seeded_app, tmp_path, monkeypatch):
        session_dir = tmp_path / "user_sessions"
        monkeypatch.setenv("SESSION_DATA_DIR", str(session_dir))

        resp = self._get_transcript(seeded_app, "analyst1", "chat-does-not-exist.jsonl")

        assert resp.status_code == 404
        assert resp.json()["detail"]["error"] == "session_not_found"

    def test_a_confirmed_current_export_says_so(self, seeded_app, tmp_path, monkeypatch):
        session_dir = tmp_path / "user_sessions"
        monkeypatch.setenv("SESSION_DATA_DIR", str(session_dir))
        chat_id = TestExportChatSessionJsonl._seed_chat_session("analyst@test.com")

        resp = self._get_transcript(seeded_app, "analyst1", f"chat-{chat_id}.jsonl")

        assert resp.status_code == 200, resp.text
        assert resp.json()["freshness"] == {"verified": True}

    def test_a_lookup_failure_over_an_existing_export_serves_it_marked_unverified(
        self, seeded_app, tmp_path, monkeypatch
    ):
        """The 503 only covers a lookup failure with NO file on disk. When an
        older export exists, resolution succeeds and we serve it — which is
        right (during an outage it is often the only evidence there is) as
        long as we never let a partial record read as the whole one."""
        import src.repositories as repos

        session_dir = tmp_path / "user_sessions"
        monkeypatch.setenv("SESSION_DATA_DIR", str(session_dir))
        chat_id = TestExportChatSessionJsonl._seed_chat_session("analyst@test.com")
        assert export_chat_session_jsonl(chat_id) is not None

        class _Boom:
            def get_session(self, _chat_id):
                raise RuntimeError("session store unavailable")

        monkeypatch.setattr(repos, "chat_session_repo", lambda: _Boom())

        resp = self._get_transcript(seeded_app, "analyst1", f"chat-{chat_id}.jsonl")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["events"]  # the file we could not verify is still served
        assert body["freshness"]["verified"] is False
        assert body["freshness"]["reason"] == "session_lookup_failed"
        assert body["freshness"]["hint"]

    def test_a_legacy_filename_carries_no_freshness_claim(self, seeded_app, tmp_path, monkeypatch):
        """A CLI-collector file is not ours to refresh, so we say nothing
        about its currency rather than claim it is verified."""
        session_dir = tmp_path / "user_sessions"
        monkeypatch.setenv("SESSION_DATA_DIR", str(session_dir))
        chat_id = TestExportChatSessionJsonl._seed_chat_session("analyst@test.com")
        exported = export_chat_session_jsonl(chat_id)
        assert exported is not None
        legacy = exported.parent / "session-001.jsonl"
        legacy.write_text(exported.read_text())

        resp = self._get_transcript(seeded_app, "analyst1", "session-001.jsonl")

        assert resp.status_code == 200, resp.text
        assert "freshness" not in resp.json()

    def test_a_message_landing_mid_export_is_served_but_not_called_current(self, seeded_app, tmp_path, monkeypatch):
        """The exporter reads, then writes. A message committed in that
        window is in neither the file nor its watermark — and the response
        that TRIGGERED the export is exactly the one that would otherwise
        serve it as confirmed-current."""
        from app.chat import session_export as mod
        from src.repositories import chat_message_repo

        session_dir = tmp_path / "user_sessions"
        monkeypatch.setenv("SESSION_DATA_DIR", str(session_dir))
        chat_id = TestExportChatSessionJsonl._seed_chat_session("analyst@test.com")

        read_all = mod._list_all_chat_messages

        def _racing_read(cid, repo):
            messages = read_all(cid, repo)
            chat_message_repo().append_message(session_id=cid, role="user", content="landed mid-export")
            return messages

        monkeypatch.setattr(mod, "_list_all_chat_messages", _racing_read)

        resp = self._get_transcript(seeded_app, "analyst1", f"chat-{chat_id}.jsonl")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["events"]  # we wrote a real file and we serve it
        assert body["freshness"]["verified"] is False
        assert body["freshness"]["reason"] == "export_raced_a_new_message"
        texts = [e.get("text") or "" for e in body["events"] if e.get("kind") == "text"]
        assert not any("landed mid-export" in t for t in texts)  # ...and it really is behind

    def test_a_message_landing_before_the_file_check_is_not_certified(self, seeded_app, tmp_path, monkeypatch):
        """The session row is read before the owner lookup and the file
        check. A message committed inside THAT window leaves the existing
        export matching a snapshot that is already out of date — the fast
        path used to certify it without ever looking again."""
        import src.repositories as repos
        from src.repositories import chat_message_repo, chat_session_repo

        session_dir = tmp_path / "user_sessions"
        monkeypatch.setenv("SESSION_DATA_DIR", str(session_dir))
        chat_id = TestExportChatSessionJsonl._seed_chat_session("analyst@test.com")
        assert export_chat_session_jsonl(chat_id) is not None
        stale_snapshot = chat_session_repo().get_session(chat_id)

        # ...and now a message lands, invisible to that snapshot.
        chat_message_repo().append_message(session_id=chat_id, role="user", content="landed pre-check")

        real_repo = chat_session_repo()
        calls = {"n": 0}

        class _SnapshotThenTruth:
            def get_session(self, cid):
                calls["n"] += 1
                return stale_snapshot if calls["n"] == 1 else real_repo.get_session(cid)

        monkeypatch.setattr(repos, "chat_session_repo", lambda: _SnapshotThenTruth())

        resp = self._get_transcript(seeded_app, "analyst1", f"chat-{chat_id}.jsonl")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        texts = [e.get("text") or "" for e in body["events"] if e.get("kind") == "text"]
        # Not merely flagged behind — brought current, which is the point.
        assert any("landed pre-check" in t for t in texts), texts
        assert body["freshness"] == {"verified": True}

    def test_non_chat_filename_keeps_the_plain_404(self, seeded_app, tmp_path, monkeypatch):
        """A legacy CLI-collector filename never matches the ``chat-*``
        pattern, so it never triggers the chat lookaside at all -- the
        original, unstructured 404 stays exactly as it was."""
        session_dir = tmp_path / "user_sessions"
        monkeypatch.setenv("SESSION_DATA_DIR", str(session_dir))

        resp = self._get_transcript(seeded_app, "analyst1", "session-001.jsonl")

        assert resp.status_code == 404
        assert resp.json()["detail"] == "session not found"


class TestEnsureChatTranscriptCurrent:
    def test_unknown_chat_id_reports_session_not_found(self, seeded_app, tmp_path, monkeypatch):
        monkeypatch.setenv("SESSION_DATA_DIR", str(tmp_path / "user_sessions"))
        freshness = ensure_chat_transcript_current("chat_does_not_exist")
        assert freshness.session_found is False
        assert freshness.path is None

    def test_disabled_flag_reports_export_disabled_without_writing(self, seeded_app, tmp_path, monkeypatch):
        session_dir = tmp_path / "user_sessions"
        monkeypatch.setenv("SESSION_DATA_DIR", str(session_dir))
        monkeypatch.setenv("AGNES_SESSIONS_INCLUDE_CHAT", "false")
        chat_id = TestExportChatSessionJsonl._seed_chat_session("analyst@test.com")

        freshness = ensure_chat_transcript_current(chat_id)

        assert freshness.session_found is True
        assert freshness.export_disabled is True
        assert freshness.path is None
        assert not session_dir.exists()

    def test_missing_export_is_created_and_reported_fresh(self, seeded_app, tmp_path, monkeypatch):
        session_dir = tmp_path / "user_sessions"
        monkeypatch.setenv("SESSION_DATA_DIR", str(session_dir))
        chat_id = TestExportChatSessionJsonl._seed_chat_session("analyst@test.com")

        freshness = ensure_chat_transcript_current(chat_id)

        assert freshness.session_found is True
        assert freshness.export_disabled is False
        assert freshness.path is not None
        assert freshness.path.is_file()


def test_a_failed_session_lookup_is_not_reported_as_a_missing_session(monkeypatch):
    """A store that raised is not a store that answered "no such row".
    Collapsing the two told an admin to check an id while the real problem
    was the database — the same confident-wrong-answer shape this PR removes
    elsewhere."""
    from app.chat import session_export as mod

    class _Boom:
        def get_session(self, chat_id):
            raise RuntimeError("session store unavailable")

    monkeypatch.setattr(mod, "_session_data_dir", lambda: Path("/nonexistent"), raising=False)
    import src.repositories as repos

    monkeypatch.setattr(repos, "chat_session_repo", lambda: _Boom())
    freshness = mod.ensure_chat_transcript_current("11111111-1111-1111-1111-111111111111")
    assert freshness.lookup_failed is True
    assert freshness.session_found is False


def _session(chat_id):
    from src.repositories import chat_session_repo

    return chat_session_repo().get_session(chat_id)


class TestExportDoesNotPublishBackwards:
    """os.replace orders nothing. A slower writer that read fewer messages
    can still land last and replace a newer transcript with a coherent,
    older one -- leaving the request that just certified the newer file
    serving the older."""

    def test_a_shorter_snapshot_does_not_replace_a_longer_export(self, seeded_app, tmp_path, monkeypatch):
        from app.chat import session_export as mod

        monkeypatch.setenv("SESSION_DATA_DIR", str(tmp_path / "user_sessions"))
        chat_id = TestExportChatSessionJsonl._seed_chat_session("analyst@test.com")
        target = export_chat_session_jsonl(chat_id)
        assert target is not None
        current = target.read_text()

        # A writer whose read predates the second message tries to publish.
        read_all = mod._list_all_chat_messages
        monkeypatch.setattr(mod, "_list_all_chat_messages", lambda cid, repo: read_all(cid, repo)[:1])

        assert mod.export_chat_session_jsonl(chat_id) == target
        assert target.read_text() == current  # the newer transcript survives


class TestTranscriptErrorSurfacesPairUp:
    """REST and CLI are one surface pair: the structured ``{"error",
    "hint"}`` body the route returns has to render as guidance on both. The
    CLI formatted only the 404 and printed a raw Python dict for the 503 —
    the retryable case, i.e. the one where the operator most needs to be
    told to come back."""

    class _Resp:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    def _run(self, status, payload):
        import typer

        from cli.commands.admin_sessions import _handle_transcript_error

        with pytest.raises(typer.Exit):
            _handle_transcript_error(self._Resp(status, payload))

    def test_a_retryable_503_prints_the_hint_not_a_dict_repr(self, capsys):
        self._run(503, {"detail": {"error": "session_lookup_failed", "hint": "Retry; the store did not answer."}})
        err = capsys.readouterr().err
        assert "session_lookup_failed: Retry; the store did not answer." in err
        assert "{" not in err

    def test_a_404_still_prints_the_hint(self, capsys):
        self._run(404, {"detail": {"error": "session_not_found", "hint": "No such session."}})
        assert "session_not_found: No such session." in capsys.readouterr().err


class TestAVerdictBelongsToOneGeneration:
    """`freshness.verified` means "we checked THESE bytes against the session
    as it was a moment ago" — never "this filename holds the whole
    conversation".

    The route used to make the verdict and read the transcript in two
    independent opens of the same path, so whatever republished the file in
    between was what got rendered, under a verdict about content nothing had
    looked at: the periodic sweep on its tick, a teardown hook, or the
    residue the publish-order guard in `export_chat_session_jsonl` leaves.
    That is the "partial record reading as the whole one" failure this
    endpoint exists to end, produced by the endpoint itself.
    """

    def test_the_route_will_not_repeat_a_verdict_about_bytes_it_did_not_read(self, seeded_app, tmp_path, monkeypatch):
        from app.chat.session_export import export_chat_session_jsonl

        monkeypatch.setenv("SESSION_DATA_DIR", str(tmp_path / "user_sessions"))
        chat_id = TestExportChatSessionJsonl._seed_chat_session("analyst@test.com")
        exported = export_chat_session_jsonl(chat_id)
        assert exported is not None

        from app.chat import session_export as se

        real = se.ensure_chat_transcript_current

        def _verdict_then_swap(cid):
            freshness = real(cid)
            # Something republishes the file after the verdict and before
            # the route reads it -- an older generation, in this case.
            if freshness.path is not None:
                first = freshness.path.read_text(encoding="utf-8").splitlines(keepends=True)[0]
                freshness.path.write_text(first, encoding="utf-8")
            return freshness

        monkeypatch.setattr(se, "ensure_chat_transcript_current", _verdict_then_swap)

        resp = seeded_app["client"].get(
            f"/api/admin/sessions/analyst1/chat-{chat_id}.jsonl/transcript",
            headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        )

        assert resp.status_code == 200
        note = resp.json()["freshness"]
        assert note["verified"] is False
        assert note["reason"] == "export_replaced_while_reading"

    def test_an_untouched_transcript_is_still_reported_verified(self, seeded_app, tmp_path, monkeypatch):
        """Positive control: nothing swaps the file, so the verdict travels
        with the bytes and stays `verified`."""
        from app.chat.session_export import export_chat_session_jsonl

        monkeypatch.setenv("SESSION_DATA_DIR", str(tmp_path / "user_sessions"))
        chat_id = TestExportChatSessionJsonl._seed_chat_session("analyst@test.com")
        assert export_chat_session_jsonl(chat_id) is not None

        resp = seeded_app["client"].get(
            f"/api/admin/sessions/analyst1/chat-{chat_id}.jsonl/transcript",
            headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        )

        assert resp.status_code == 200
        assert resp.json()["freshness"] == {"verified": True}


class TestTheDigestCannotBeBorrowedFromAnotherGeneration:
    """Two ways the pairing digest could end up describing a generation
    nothing verified — both of them a read-then-act gap opened by fixing
    the previous one, which is why they are pinned rather than reasoned
    about.
    """

    def test_the_certified_digest_comes_from_the_read_that_verified_it(self, seeded_app, tmp_path, monkeypatch):
        """`ensure_chat_transcript_current` used to fetch the digest with a
        FRESH sidecar read after deciding the export was current. A writer
        publishing between the two hands it the digest of a generation
        nothing compared against the session — and the route, matching the
        bytes it then reads against that digest, reports `verified` over a
        transcript that was never checked."""
        from app.chat import session_export as se

        monkeypatch.setenv("SESSION_DATA_DIR", str(tmp_path / "user_sessions"))
        chat_id = TestExportChatSessionJsonl._seed_chat_session("analyst@test.com")
        published = se.export_chat_session_jsonl(chat_id)
        assert published is not None
        full = published.read_text(encoding="utf-8")

        real = se._is_stale_against_a_fresh_read

        def _verify_then_republish_something_older(cid, exported):
            verdict = real(cid, exported)
            # A delayed writer lands a coherent, digest-valid, SHORTER
            # generation right after the verdict — sidecar included.
            shorter = full.splitlines(keepends=True)[0]
            se._atomic_write_text(exported, shorter)
            se._write_export_watermark(
                exported,
                se.ExportWatermark(datetime.now(UTC), messages=1, content_sha256=se._content_digest(shorter)),
            )
            return verdict

        monkeypatch.setattr(se, "_is_stale_against_a_fresh_read", _verify_then_republish_something_older)

        freshness = se.ensure_chat_transcript_current(chat_id)

        # Whatever else it says, it must not certify the generation that
        # landed after the check: the digest it carries has to belong to
        # the transcript that was actually verified.
        assert freshness.content_sha256 != se._content_digest(published.read_text(encoding="utf-8"))

    def test_a_replacement_that_keeps_the_size_and_mtime_is_still_caught(self, tmp_path):
        """The reason there is no size-and-mtime shortcut in front of the
        digest. `(size, mtime_ns)` is not proof of identity: a replacement
        can preserve both — routine on a filesystem whose timestamps are
        coarse — and a shortcut keying on them would skip the digest and
        certify the old sidecar over unrelated content for as long as the
        metadata kept matching. Verification here is the feature; the
        shortcut bought 0.36 s per ten-minute sweep tick."""
        import os

        from app.chat import session_export as se

        target = tmp_path / "chat-x.jsonl"
        ours = '{"turn": 1}\n{"turn": 2}\n'
        target.write_text(ours, encoding="utf-8")
        se._write_export_watermark(
            target,
            se.ExportWatermark(datetime.now(UTC), messages=2, content_sha256=se._content_digest(ours)),
        )
        assert not is_chat_export_stale(target, datetime.now(UTC) - timedelta(hours=1), 2)

        before = target.stat()
        # Same byte count, different content, and the metadata forced back
        # to what it was — the collision, constructed.
        theirs = '{"turn": 9}\n{"turn": 8}\n'
        assert len(theirs) == len(ours)
        target.write_text(theirs, encoding="utf-8")
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert target.stat().st_size == before.st_size
        assert target.stat().st_mtime_ns == before.st_mtime_ns

        assert is_chat_export_stale(target, datetime.now(UTC) - timedelta(hours=1), 2)
