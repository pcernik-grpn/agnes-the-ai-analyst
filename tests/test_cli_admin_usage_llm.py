"""CLI: `agnes admin usage llm-cost|llm-calls|feedback` — mirrors `chat-cost`.

A purely mocked ``get_client()`` asserts the request shape (path + params)
each command builds and how it renders a table / `--json`; the typed-501
Postgres hint mirrors what the server actually returns for a DuckDB-backed
instance (see tests/test_admin_llm_cost_api.py). The behaviour behind the
JSON payloads themselves (grouping, paging, filtering) is proven against a
real server in tests/db_pg/test_admin_llm_cost_pg.py.
"""

from __future__ import annotations

import json
import re
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

import cli.commands.admin_usage as mod

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _resp(status_code: int, body: dict):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = body
    r.text = json.dumps(body)
    return r


@pytest.fixture
def runner():
    return CliRunner()


class TestLlmCost:
    def test_requests_the_right_path_and_params(self, monkeypatch, runner):
        client = MagicMock()
        client.get.return_value = _resp(
            200,
            {
                "window": "30d",
                "by": "model",
                "groups": [
                    {
                        "key": "chat",
                        "calls": 3,
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "cache_read_tokens": 10,
                        "cache_creation_tokens": 0,
                        "cost_usd": 0.01,
                        "cached_input_share": 0.0909,
                        "priced_models": ["claude-haiku-4-5"],
                    }
                ],
                "totals": {
                    "calls": 3,
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cache_read_tokens": 10,
                    "cache_creation_tokens": 0,
                    "cost_usd": 0.01,
                    "cached_input_share": 0.0909,
                },
                "notes": ["a note"],
            },
        )
        monkeypatch.setattr(mod, "get_client", lambda timeout=60: client)

        result = runner.invoke(mod.app, ["llm-cost", "--window", "30d", "--by", "model"])
        assert result.exit_code == 0, _clean(result.output)
        client.get.assert_called_once_with("/api/admin/telemetry/llm-cost", params={"window": "30d", "by": "model"})
        out = _clean(result.output)
        assert "chat" in out
        assert "a note" in out

    def test_json_flag_emits_raw_json(self, monkeypatch, runner):
        client = MagicMock()
        payload = {
            "window": "7d",
            "by": "workload",
            "groups": [],
            "totals": {"calls": 0, "cost_usd": 0.0},
            "notes": [],
        }
        client.get.return_value = _resp(200, payload)
        monkeypatch.setattr(mod, "get_client", lambda timeout=60: client)

        result = runner.invoke(mod.app, ["llm-cost", "--json"])
        assert result.exit_code == 0, _clean(result.output)
        assert json.loads(result.output) == payload

    def test_empty_groups_hints_at_the_ledger(self, monkeypatch, runner):
        client = MagicMock()
        client.get.return_value = _resp(
            200,
            {"window": "7d", "by": "workload", "groups": [], "totals": {"calls": 0, "cost_usd": 0.0}, "notes": []},
        )
        monkeypatch.setattr(mod, "get_client", lambda timeout=60: client)
        result = runner.invoke(mod.app, ["llm-cost"])
        assert result.exit_code == 0, _clean(result.output)
        assert "docs/observability.md" in _clean(result.output)

    def test_bad_window_is_refused_client_side(self, monkeypatch, runner):
        client = MagicMock()
        monkeypatch.setattr(mod, "get_client", lambda timeout=60: client)
        result = runner.invoke(mod.app, ["llm-cost", "--window", "forever"])
        assert result.exit_code != 0
        client.get.assert_not_called()

    def test_501_prints_the_postgres_hint(self, monkeypatch, runner):
        client = MagicMock()
        client.get.return_value = _resp(501, {"error": "requires_postgres_backend"})
        monkeypatch.setattr(mod, "get_client", lambda timeout=60: client)
        result = runner.invoke(mod.app, ["llm-cost"])
        assert result.exit_code != 0
        assert "postgres" in _clean(result.output).lower()


class TestLlmCalls:
    def test_requires_one_id(self, monkeypatch, runner):
        client = MagicMock()
        monkeypatch.setattr(mod, "get_client", lambda timeout=60: client)
        result = runner.invoke(mod.app, ["llm-calls"])
        assert result.exit_code != 0
        client.get.assert_not_called()

    def test_session_id_builds_the_request(self, monkeypatch, runner):
        client = MagicMock()
        client.get.return_value = _resp(200, {"rows": [], "next_before": None, "notes": []})
        monkeypatch.setattr(mod, "get_client", lambda timeout=60: client)
        result = runner.invoke(mod.app, ["llm-calls", "--session-id", "s1", "--limit", "10"])
        assert result.exit_code == 0, _clean(result.output)
        client.get.assert_called_once_with("/api/admin/telemetry/llm-calls", params={"session_id": "s1", "limit": 10})

    def test_renders_the_row_and_the_paging_hint(self, monkeypatch, runner):
        client = MagicMock()
        client.get.return_value = _resp(
            200,
            {
                "rows": [
                    {
                        "id": "c1",
                        "created_at": "2026-09-08T00:00:00+00:00",
                        "kind": "completion",
                        "workload": "chat",
                        "purpose": "completion",
                        "model_response": "claude-haiku-4-5",
                        "input_tokens": 10,
                        "output_tokens": 2,
                        "cache_read_tokens": 0,
                        "cost_usd": 0.001,
                        "status": "ok",
                    }
                ],
                "next_before": "2026-09-08T00:00:00+00:00",
                "notes": [],
            },
        )
        monkeypatch.setattr(mod, "get_client", lambda timeout=60: client)
        result = runner.invoke(mod.app, ["llm-calls", "--turn-id", "t1"])
        assert result.exit_code == 0, _clean(result.output)
        out = _clean(result.output)
        assert "claude-haiku-4-5" in out
        assert "--before" in out

    def test_501_prints_the_postgres_hint(self, monkeypatch, runner):
        client = MagicMock()
        client.get.return_value = _resp(501, {"error": "requires_postgres_backend"})
        monkeypatch.setattr(mod, "get_client", lambda timeout=60: client)
        result = runner.invoke(mod.app, ["llm-calls", "--turn-id", "t1"])
        assert result.exit_code != 0
        assert "postgres" in _clean(result.output).lower()


class TestFeedback:
    def test_requests_the_right_path_and_params(self, monkeypatch, runner):
        client = MagicMock()
        client.get.return_value = _resp(200, {"window": "7d", "verdict": "down", "rows": []})
        monkeypatch.setattr(mod, "get_client", lambda timeout=60: client)
        result = runner.invoke(mod.app, ["feedback", "--verdict", "down", "--limit", "5"])
        assert result.exit_code == 0, _clean(result.output)
        client.get.assert_called_once_with(
            "/api/admin/telemetry/feedback", params={"window": "7d", "verdict": "down", "limit": 5}
        )

    def test_never_prints_the_comment_text_in_the_table(self, monkeypatch, runner):
        client = MagicMock()
        client.get.return_value = _resp(
            200,
            {
                "window": "7d",
                "verdict": None,
                "rows": [
                    {
                        "id": "f1",
                        "session_id": "s1",
                        "turn_id": "t1",
                        "user_id": "u1",
                        "verdict": "down",
                        "comment": "super secret leaked info",
                        "created_at": "2026-09-08T00:00:00+00:00",
                    }
                ],
            },
        )
        monkeypatch.setattr(mod, "get_client", lambda timeout=60: client)
        result = runner.invoke(mod.app, ["feedback"])
        assert result.exit_code == 0, _clean(result.output)
        out = _clean(result.output)
        assert "super secret leaked info" not in out
        assert "chars" in out  # comment LENGTH shown instead

    def test_json_flag_would_carry_the_comment(self, monkeypatch, runner):
        client = MagicMock()
        payload = {
            "window": "7d",
            "verdict": None,
            "rows": [{"id": "f1", "comment": "wrong number"}],
        }
        client.get.return_value = _resp(200, payload)
        monkeypatch.setattr(mod, "get_client", lambda timeout=60: client)
        result = runner.invoke(mod.app, ["feedback", "--json"])
        assert result.exit_code == 0, _clean(result.output)
        assert json.loads(result.output) == payload

    def test_501_prints_the_postgres_hint(self, monkeypatch, runner):
        client = MagicMock()
        client.get.return_value = _resp(501, {"error": "requires_postgres_backend"})
        monkeypatch.setattr(mod, "get_client", lambda timeout=60: client)
        result = runner.invoke(mod.app, ["feedback"])
        assert result.exit_code != 0
        assert "postgres" in _clean(result.output).lower()
