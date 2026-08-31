"""The session-transcript viewer must let an operator tie inputs to outputs.

Reported against a live instance: the detail header's "Tool calls" number
disagreed with the tool cards in the transcript below it (the summary row's
pre-v10 ``tool_calls`` excluded MCP/subagent calls), and a ``tool_result``
card was labeled only by its opaque ``toolu_…`` id, so there was no way to
tell which call produced which output.

Three fixes pinned here:

- the transcript payload carries ``counts`` — tool calls/errors computed
  from the file being viewed (every ``tool_use`` block, whatever the tool
  name), the same exact-for-this-file principle as the token sum (TCRD-222);
- ``tool_result`` events carry the ``tool_name`` of the call they answer,
  resolved server-side via ``tool_use_id``;
- the detail template renders the exact counts and wires call↔result jump
  links keyed on ``tool_use_id``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch, seeded_app, admin_user):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # The session viewer reads SESSION_DATA_DIR, not DATA_DIR — same override
    # tests/test_session_detail_tokens.py uses.
    monkeypatch.setenv("SESSION_DATA_DIR", str(tmp_path / "user_sessions"))
    return {"client": seeded_app["client"], "admin": admin_user}


def _write_session(tmp_path, username="analyst1", fname="tools.jsonl"):
    """One plain Bash call (ok) + one MCP call (failed) + one slash command."""
    sdir = tmp_path / "user_sessions" / username
    sdir.mkdir(parents=True, exist_ok=True)
    turns = [
        {
            "type": "user",
            "timestamp": "2026-08-28T10:00:00Z",
            "uuid": "u1",
            "message": {"role": "user", "content": "<command-name>/compact</command-name>"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-08-28T10:00:05Z",
            "uuid": "a1",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "tu_bash", "name": "Bash", "input": {"command": "ls"}}],
            },
        },
        {
            "type": "user",
            "timestamp": "2026-08-28T10:00:06Z",
            "uuid": "u2",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_bash",
                        "is_error": False,
                        "content": [{"type": "text", "text": "file.txt"}],
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "timestamp": "2026-08-28T10:00:10Z",
            "uuid": "a2",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_mcp",
                        "name": "mcp__agnes__query",
                        "input": {"sql": "SELECT 1"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "timestamp": "2026-08-28T10:00:11Z",
            "uuid": "u3",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_mcp",
                        "is_error": True,
                        "content": [{"type": "text", "text": "boom"}],
                    }
                ],
            },
        },
    ]
    (sdir / fname).write_text("\n".join(json.dumps(t) for t in turns) + "\n", encoding="utf-8")
    return f"{username}/{fname}"


class TestTranscriptCounts:
    def test_counts_include_every_tool_call_kind(self, client, tmp_path):
        """An MCP call is a tool call. The header number must match the tool
        cards an operator can count in the transcript below it."""
        _write_session(tmp_path)
        resp = client["client"].get(
            "/api/admin/sessions/analyst1/tools.jsonl/transcript",
            headers=client["admin"],
        )
        assert resp.status_code == 200, resp.text
        counts = resp.json().get("counts")
        assert counts == {"tool_calls": 2, "tool_errors": 1}

    def test_counts_present_even_without_summary_row(self, client, tmp_path):
        """The counts come from the file, not the UsageProcessor — a fresh
        upload (no summary row yet) still gets exact numbers."""
        _write_session(tmp_path, fname="fresh.jsonl")
        resp = client["client"].get(
            "/api/admin/sessions/analyst1/fresh.jsonl/transcript",
            headers=client["admin"],
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("summary") == {}
        assert body["counts"]["tool_calls"] == 2


class TestToolResultCarriesToolName:
    def test_result_events_are_labeled_by_tool(self, client, tmp_path):
        _write_session(tmp_path)
        resp = client["client"].get(
            "/api/admin/sessions/analyst1/tools.jsonl/transcript",
            headers=client["admin"],
        )
        assert resp.status_code == 200, resp.text
        results = [e for e in resp.json()["events"] if e["kind"] == "tool_result"]
        assert [r["tool_name"] for r in results] == ["Bash", "mcp__agnes__query"]
        # The pairing key itself still rides along for the UI's jump links.
        assert [r["tool_use_id"] for r in results] == ["tu_bash", "tu_mcp"]

    def test_orphan_result_gets_no_invented_name(self, client, tmp_path):
        """A result whose call is missing from the file (truncated JSONL)
        must be labeled honestly — null, falling back to the id in the UI."""
        sdir = tmp_path / "user_sessions" / "analyst1"
        sdir.mkdir(parents=True, exist_ok=True)
        turns = [
            {
                "type": "user",
                "timestamp": "2026-08-28T10:00:00Z",
                "uuid": "u1",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_gone",
                            "is_error": False,
                            "content": "late output",
                        }
                    ],
                },
            }
        ]
        (sdir / "orphan.jsonl").write_text("\n".join(json.dumps(t) for t in turns) + "\n", encoding="utf-8")
        resp = client["client"].get(
            "/api/admin/sessions/analyst1/orphan.jsonl/transcript",
            headers=client["admin"],
        )
        assert resp.status_code == 200, resp.text
        results = [e for e in resp.json()["events"] if e["kind"] == "tool_result"]
        assert results[0]["tool_name"] is None


class TestDetailPagePairsCallsWithResults:
    def test_template_wires_counts_and_pairing(self):
        template = Path("app/web/templates/admin_session_detail.html").read_text(encoding="utf-8")
        assert "renderMeta(d.summary, d.tokens, d.counts)" in template, (
            "the detail page must pass the transcript payload's exact counts into the meta renderer"
        )
        assert "wirePairs()" in template, "the detail page must wire call↔result jump links after rendering"
        assert "data-tuid" in template or "dataset.tuid" in template, (
            "cards must be keyed by tool_use_id so pairing survives several calls to the same tool"
        )
