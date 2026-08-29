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
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from app.api.admin_sessions import _render_transcript
from app.chat.session_export import export_chat_session_jsonl, messages_to_turns
from app.chat.types import ChatMessage, Surface
from services.session_pipeline.lib import parse_jsonl


def _msg(**kw) -> ChatMessage:
    base = dict(
        id="msg_1",
        session_id="chat_1",
        role="user",
        content="",
        tool_calls=None,
        tokens_in=None,
        tokens_out=None,
        model=None,
        created_at=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
        sender_email=None,
        parts=None,
    )
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
