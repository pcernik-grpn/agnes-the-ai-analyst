"""TCRD-222: a session's token spend must be answerable from the product.

The processor has summed per-session tokens into ``usage_session_summary``
since v44, and every assistant turn in a session JSONL carries
``message.usage`` — yet neither the session list nor the transcript viewer
surfaced any of it. An admin asked "what did this prompt cost" could see the
session's tool calls but not one token number.

Two fixes pinned here:

- The repos' session projections (``_SESSION_COLS`` / ``get_session_summary``)
  carry the four stored token counters, on both backends.
- The transcript endpoint sums ``message.usage`` from the JSONL it already
  parsed — so the detail view is exact for the file being read, independent
  of whether the processor has ticked yet (fresh uploads have a zeroed or
  missing summary row until it does).
"""

from __future__ import annotations

import json

import pytest

TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")


class TestRepoProjectionsCarryTokens:
    def test_duckdb_session_cols_include_token_counters(self):
        from src.repositories.usage import UsageRepository

        for key in TOKEN_KEYS:
            assert key in UsageRepository._SESSION_COLS

    def test_pg_session_cols_include_token_counters(self):
        from src.repositories import usage_pg

        for key in TOKEN_KEYS:
            assert key in usage_pg._SESSION_COLS

    def test_get_session_summary_returns_token_counters(self, tmp_path, monkeypatch):
        from src.db import _ensure_schema
        from src.duckdb_conn import _open_duckdb
        from src.repositories.usage import UsageRepository

        conn = _open_duckdb(str(tmp_path / "system.duckdb"))
        _ensure_schema(conn)
        conn.execute(
            """INSERT INTO usage_session_summary
               (session_file, session_id, username, processor_version,
                input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens)
               VALUES ('u1/s1.jsonl', 'sid-1', 'u1', 1, 100, 200, 300, 400)"""
        )
        row = UsageRepository(conn).get_session_summary("u1/s1.jsonl")
        assert row is not None
        assert row["input_tokens"] == 100
        assert row["output_tokens"] == 200
        assert row["cache_read_tokens"] == 300
        assert row["cache_creation_tokens"] == 400
        conn.close()


@pytest.fixture
def client(tmp_path, monkeypatch, seeded_app, admin_user):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # The session viewer reads SESSION_DATA_DIR, not DATA_DIR — same override
    # tests/test_api_admin_user_sessions.py uses.
    monkeypatch.setenv("SESSION_DATA_DIR", str(tmp_path / "user_sessions"))
    return {"client": seeded_app["client"], "admin": admin_user}


def _write_session(tmp_path, username="analyst1", fname="sess-a.jsonl"):
    """Two assistant turns with usage, one without (older-format tolerance)."""
    sdir = tmp_path / "user_sessions" / username
    sdir.mkdir(parents=True, exist_ok=True)
    turns = [
        {
            "type": "user",
            "timestamp": "2026-08-28T10:00:00Z",
            "uuid": "t1",
            "message": {"role": "user", "content": "hello"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-08-28T10:00:05Z",
            "uuid": "t2",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "hi"}],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "cache_read_input_tokens": 30,
                    "cache_creation_input_tokens": 40,
                },
            },
        },
        {
            "type": "assistant",
            "timestamp": "2026-08-28T10:00:10Z",
            "uuid": "t3",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "more"}],
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
        },
        {
            "type": "assistant",
            "timestamp": "2026-08-28T10:00:15Z",
            "uuid": "t4",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "no usage key"}]},
        },
    ]
    (sdir / fname).write_text("\n".join(json.dumps(t) for t in turns) + "\n", encoding="utf-8")
    return f"{username}/{fname}"


class TestTranscriptSumsTokensFromJsonl:
    def test_transcript_reports_token_totals(self, client, tmp_path):
        _write_session(tmp_path)
        resp = client["client"].get(
            "/api/admin/sessions/analyst1/sess-a.jsonl/transcript",
            headers=client["admin"],
        )
        assert resp.status_code == 200, resp.text
        tokens = resp.json().get("tokens")
        assert tokens is not None, "the transcript payload must carry a token summary"
        assert tokens["input"] == 11
        assert tokens["output"] == 22
        assert tokens["cache_read"] == 30
        assert tokens["cache_creation"] == 40
        assert tokens["total"] == 11 + 22 + 30 + 40

    def test_a_session_with_no_usage_fields_reports_none_not_zero(self, client, tmp_path):
        """Old JSONLs predate the usage block. 'We don't know' must not be
        spelled '0 tokens' — a zero reads as a measurement."""
        sdir = tmp_path / "user_sessions" / "analyst1"
        sdir.mkdir(parents=True, exist_ok=True)
        turns = [
            {
                "type": "assistant",
                "timestamp": "2026-08-28T10:00:00Z",
                "uuid": "t1",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "old"}]},
            }
        ]
        (sdir / "old.jsonl").write_text("\n".join(json.dumps(t) for t in turns) + "\n", encoding="utf-8")

        resp = client["client"].get(
            "/api/admin/sessions/analyst1/old.jsonl/transcript",
            headers=client["admin"],
        )
        assert resp.status_code == 200, resp.text
        assert resp.json().get("tokens") is None


class TestDetailPageRendersTokens:
    def test_detail_template_renders_the_token_line(self):
        from pathlib import Path

        template = Path("app/web/templates/admin_session_detail.html").read_text(encoding="utf-8")
        assert "renderMeta(d.summary, d.tokens)" in template, (
            "the detail page must pass the transcript payload's token summary into the meta renderer"
        )
        assert "fmtTokens" in template, "the token line must render, not just receive, the summary"
