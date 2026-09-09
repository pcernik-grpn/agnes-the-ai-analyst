"""Tests for `agnes admin conversations export` (design 2026-09-08 §3.12).

Pure CLI-logic tests: `cli.commands.admin_conversations.api_get` is
monkeypatched to a fake sequence of canned HTTP responses (no app, no
database) so these exercise the pagination-following/`--out`/`--json`
mechanics in isolation. The route's own behaviour is covered by
``tests/db_pg/test_conversation_export_pg.py`` and
``tests/test_conversation_export_api.py``.
"""

from __future__ import annotations

import json
import re

import httpx
from typer.testing import CliRunner

import cli.commands.admin_conversations as mod

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _ndjson_page(records: list[dict], *, next_cursor: str | None = None) -> httpx.Response:
    body = "".join(json.dumps(r) + "\n" for r in records)
    headers = {"X-Next-Cursor": next_cursor} if next_cursor else {}
    return httpx.Response(200, text=body, headers=headers)


def _fake_pages(pages: list[httpx.Response]):
    """A stand-in for ``api_get`` that returns ``pages`` in order,
    regardless of the params passed (the pagination logic is what is under
    test, not the server-side filtering)."""
    calls: list[dict] = []
    it = iter(pages)

    def _get(path, **kwargs):
        calls.append(kwargs.get("params") or {})
        return next(it)

    _get.calls = calls
    return _get


def test_out_file_is_written_with_every_page(tmp_path, monkeypatch):
    page1 = _ndjson_page([{"thread_id": "a"}, {"thread_id": "b"}], next_cursor="cur1")
    page2 = _ndjson_page([{"thread_id": "c"}])
    fake = _fake_pages([page1, page2])
    monkeypatch.setattr(mod, "api_get", fake)

    out = tmp_path / "export.jsonl"
    result = CliRunner().invoke(mod.app, ["--since", "2026-01-01", "--out", str(out)])

    assert result.exit_code == 0, result.output
    lines = out.read_text().splitlines()
    assert [json.loads(ln)["thread_id"] for ln in lines] == ["a", "b", "c"]
    assert len(fake.calls) == 2
    assert "cursor" not in fake.calls[0]
    assert fake.calls[1]["cursor"] == "cur1"


def test_pages_are_followed_via_the_cursor_header_until_it_stops(monkeypatch):
    page1 = _ndjson_page([{"thread_id": "a"}], next_cursor="cur1")
    page2 = _ndjson_page([{"thread_id": "b"}], next_cursor="cur2")
    page3 = _ndjson_page([{"thread_id": "c"}])  # no next cursor -> stop
    fake = _fake_pages([page1, page2, page3])
    monkeypatch.setattr(mod, "api_get", fake)

    result = CliRunner().invoke(mod.app, ["--since", "2026-01-01"])

    assert result.exit_code == 0, result.output
    assert len(fake.calls) == 3
    assert fake.calls[0].get("cursor") is None
    assert fake.calls[1]["cursor"] == "cur1"
    assert fake.calls[2]["cursor"] == "cur2"
    printed = [json.loads(ln)["thread_id"] for ln in _clean(result.output).splitlines() if ln.strip()]
    assert printed == ["a", "b", "c"]


def test_json_flag_reassembles_pages_into_one_array(tmp_path, monkeypatch):
    page1 = _ndjson_page([{"thread_id": "a"}], next_cursor="cur1")
    page2 = _ndjson_page([{"thread_id": "b"}])
    fake = _fake_pages([page1, page2])
    monkeypatch.setattr(mod, "api_get", fake)

    out = tmp_path / "export.json"
    result = CliRunner().invoke(mod.app, ["--since", "2026-01-01", "--json", "--out", str(out)])

    assert result.exit_code == 0, result.output
    payload = json.loads(out.read_text())
    assert [r["thread_id"] for r in payload] == ["a", "b"]


def test_json_flag_without_out_prints_one_array_to_stdout(monkeypatch):
    fake = _fake_pages([_ndjson_page([{"thread_id": "a"}, {"thread_id": "b"}])])
    monkeypatch.setattr(mod, "api_get", fake)

    result = CliRunner().invoke(mod.app, ["--since", "2026-01-01", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(_clean(result.output))
    assert [r["thread_id"] for r in payload] == ["a", "b"]


def test_filters_and_limit_are_forwarded_as_query_params(monkeypatch):
    fake = _fake_pages([_ndjson_page([])])
    monkeypatch.setattr(mod, "api_get", fake)

    CliRunner().invoke(
        mod.app,
        [
            "--since",
            "2026-01-01",
            "--until",
            "2026-02-01",
            "--surface",
            "web",
            "--agent-id",
            "agent_1",
            "--limit",
            "50",
        ],
    )

    assert fake.calls[0] == {
        "since": "2026-01-01",
        "limit": 50,
        "until": "2026-02-01",
        "surface": "web",
        "agent_id": "agent_1",
    }


def test_since_is_required_client_side(monkeypatch):
    fake = _fake_pages([])
    monkeypatch.setattr(mod, "api_get", fake)

    result = CliRunner().invoke(mod.app, [])

    assert result.exit_code != 0
    assert fake.calls == []


def test_no_records_says_what_to_do_next(monkeypatch):
    fake = _fake_pages([_ndjson_page([])])
    monkeypatch.setattr(mod, "api_get", fake)

    result = CliRunner().invoke(mod.app, ["--since", "2026-01-01"])

    assert result.exit_code == 0, result.output
    combined = _clean(result.output)
    assert "No conversations" in combined
    assert "--since" in combined or "--until" in combined


def test_non_admin_401_is_a_clean_exit(monkeypatch):
    fake = _fake_pages([httpx.Response(401, json={"detail": "unauthorized"})])
    monkeypatch.setattr(mod, "api_get", fake)

    result = CliRunner().invoke(mod.app, ["--since", "2026-01-01"])

    assert result.exit_code == 1
    assert "authentication required" in _clean(result.output)


def test_content_export_disabled_names_the_reason(monkeypatch):
    fake = _fake_pages(
        [httpx.Response(403, json={"detail": {"error": "content_export_disabled", "reason": "workload_excluded"}})]
    )
    monkeypatch.setattr(mod, "api_get", fake)

    result = CliRunner().invoke(mod.app, ["--since", "2026-01-01"])

    assert result.exit_code == 1
    combined = _clean(result.output)
    assert "content-export policy" in combined
    assert "workload_excluded" in combined


def test_duckdb_501_is_a_clean_exit(monkeypatch):
    fake = _fake_pages([httpx.Response(501, json={"error": "requires_postgres_backend", "feature": "llm_calls"})])
    monkeypatch.setattr(mod, "api_get", fake)

    result = CliRunner().invoke(mod.app, ["--since", "2026-01-01"])

    assert result.exit_code == 1
    assert "Postgres" in _clean(result.output)
