"""CLI tests for `agnes agent` — profile CRUD, scope, token minting, ask (Task 11).

Mocks the HTTP layer the same way ``tests/test_cli_admin_digest.py`` does:
patch ``cli.commands.agent.api_{get,post,put,delete}`` with a MagicMock
response, invoke via Typer's CliRunner, assert method/path/payload +
rendering (table and ``--json``).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import typer
from typer.testing import CliRunner

from cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    yield tmp_path


def _resp(status_code=200, json_data=None, text=""):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data if json_data is not None else {}
    r.text = text
    return r


_AGENT_ROW = {
    "id": "ag_1",
    "slug": "research",
    "name": "Research",
    "description": None,
    "model": "claude-x",
    "token_budget_monthly": 1000,
    "plugins_mode": "selected",
    "connections_mode": "selected",
    "tables_mode": "selected",
    "memory_mode": "selected",
    "memory_write_mode": "off",
    "is_default": False,
    "created_at": "2026-07-01",
    "updated_at": "2026-07-01",
}


class TestList:
    def test_list_text(self):
        with patch(
            "cli.commands.agent.api_get",
            return_value=_resp(200, {"data": [_AGENT_ROW], "has_more": False, "next_cursor": None}),
        ) as m:
            result = runner.invoke(app, ["agent", "list"])
        assert result.exit_code == 0
        assert m.call_args.args[0] == "/api/v1/agents"
        assert "research" in result.output
        assert "Research" in result.output

    def test_list_json(self):
        with patch(
            "cli.commands.agent.api_get",
            return_value=_resp(200, {"data": [_AGENT_ROW], "has_more": False, "next_cursor": None}),
        ):
            result = runner.invoke(app, ["agent", "list", "--json"])
        data = json.loads(result.output)
        assert data[0]["slug"] == "research"

    def test_list_empty_hints_create(self):
        with patch(
            "cli.commands.agent.api_get",
            return_value=_resp(200, {"data": [], "has_more": False, "next_cursor": None}),
        ):
            result = runner.invoke(app, ["agent", "list"])
        assert result.exit_code == 0
        assert "agnes agent create" in result.output


class TestCreate:
    def test_create_success(self):
        created = dict(_AGENT_ROW)
        with patch(
            "cli.commands.agent.api_post",
            return_value=_resp(201, created),
        ) as m:
            result = runner.invoke(
                app,
                ["agent", "create", "Research", "--slug", "research", "--model", "claude-x", "--budget", "1000"],
            )
        assert result.exit_code == 0
        assert m.call_args.args[0] == "/api/v1/agents"
        assert m.call_args.kwargs["json"] == {
            "name": "Research",
            "slug": "research",
            "model": "claude-x",
            "token_budget_monthly": 1000,
        }
        assert "ag_1" in result.output
        assert "research" in result.output

    def test_create_with_prompt_file(self, tmp_path):
        f = tmp_path / "prompt.txt"
        f.write_text("You are a research assistant.\n", encoding="utf-8")
        with patch(
            "cli.commands.agent.api_post",
            return_value=_resp(201, dict(_AGENT_ROW)),
        ) as m:
            result = runner.invoke(
                app,
                ["agent", "create", "Research", "--slug", "research", "--prompt-file", str(f)],
            )
        assert result.exit_code == 0
        assert m.call_args.kwargs["json"]["system_prompt"] == "You are a research assistant."

    def test_create_slug_taken_renders_detail_code(self):
        with patch(
            "cli.commands.agent.api_post",
            return_value=_resp(409, {"detail": {"code": "slug_taken", "message": "slug 'research' is already in use"}}),
        ):
            result = runner.invoke(app, ["agent", "create", "Research", "--slug", "research"])
        assert result.exit_code == 1
        assert "slug_taken" in result.output
        assert "already in use" in result.output


class TestShow:
    def test_show_found(self):
        with patch(
            "cli.commands.agent.api_get",
            return_value=_resp(200, {"data": [_AGENT_ROW], "has_more": False, "next_cursor": None}),
        ):
            result = runner.invoke(app, ["agent", "show", "research"])
        assert result.exit_code == 0
        assert "ag_1" in result.output
        assert "claude-x" in result.output

    def test_show_json(self):
        with patch(
            "cli.commands.agent.api_get",
            return_value=_resp(200, {"data": [_AGENT_ROW], "has_more": False, "next_cursor": None}),
        ):
            result = runner.invoke(app, ["agent", "show", "research", "--json"])
        data = json.loads(result.output)
        assert data["id"] == "ag_1"

    def test_show_not_found_hints_list(self):
        with patch(
            "cli.commands.agent.api_get",
            return_value=_resp(200, {"data": [], "has_more": False, "next_cursor": None}),
        ):
            result = runner.invoke(app, ["agent", "show", "nope"])
        assert result.exit_code == 1
        assert "agnes agent list" in result.output

    def test_show_warns_about_a_policied_table_in_scope(self):
        """Design doc §12 — a Slack channel bound to this agent, or a
        scheduled run, answers everyone with the OWNER's slice of a policied
        table, so `agnes agent show` surfaces the same warning the `/agents`
        page renders, straight off the management API's
        `policied_tables_in_scope` / `policied_tables` fields."""
        row = dict(
            _AGENT_ROW,
            policied_tables_in_scope=["tbl_orders"],
            policied_tables=[
                {
                    "table_id": "tbl_orders",
                    "name": "orders",
                    "access_policy": True,
                    "policy": {"applies": True, "rows_visible": 2, "reason": "ok", "note": None},
                }
            ],
        )
        with patch(
            "cli.commands.agent.api_get",
            return_value=_resp(200, {"data": [row], "has_more": False, "next_cursor": None}),
        ):
            result = runner.invoke(app, ["agent", "show", "research"])
        assert result.exit_code == 0
        assert "1 table" in result.output
        assert "filtered by an access policy" in result.output
        assert "owner" in result.output.lower()
        assert "orders" in result.output
        assert "2 row(s) visible to the owner" in result.output

    def test_show_json_still_carries_the_raw_fields(self):
        row = dict(_AGENT_ROW, policied_tables_in_scope=["tbl_orders"], policied_tables=[])
        with patch(
            "cli.commands.agent.api_get",
            return_value=_resp(200, {"data": [row], "has_more": False, "next_cursor": None}),
        ):
            result = runner.invoke(app, ["agent", "show", "research", "--json"])
        data = json.loads(result.output)
        assert data["policied_tables_in_scope"] == ["tbl_orders"]

    def test_show_no_warning_without_a_policied_table(self):
        with patch(
            "cli.commands.agent.api_get",
            return_value=_resp(200, {"data": [_AGENT_ROW], "has_more": False, "next_cursor": None}),
        ):
            result = runner.invoke(app, ["agent", "show", "research"])
        assert result.exit_code == 0
        assert "access policy" not in result.output


class TestScopeSet:
    def test_scope_set_sends_put_with_items(self):
        with (
            patch(
                "cli.commands.agent.api_get",
                return_value=_resp(200, {"data": [_AGENT_ROW], "has_more": False, "next_cursor": None}),
            ),
            patch(
                "cli.commands.agent.api_put",
                return_value=_resp(200, {"items": [{"item_type": "plugin", "item_id": "p1"}]}),
            ) as m,
        ):
            result = runner.invoke(
                app,
                [
                    "agent",
                    "scope",
                    "set",
                    "research",
                    "--plugin",
                    "p1",
                    "--table",
                    "t1",
                    "--connection",
                    "c1",
                    "--memory-domain",
                    "d1",
                ],
            )
        assert result.exit_code == 0
        assert m.call_args.args[0] == "/api/v1/agents/ag_1/scope"
        items = m.call_args.kwargs["json"]["items"]
        assert {"item_type": "plugin", "item_id": "p1"} in items
        assert {"item_type": "table", "item_id": "t1"} in items
        assert {"item_type": "connection", "item_id": "c1"} in items
        assert {"item_type": "memory_domain", "item_id": "d1"} in items

    def test_scope_set_requires_at_least_one_item(self):
        result = runner.invoke(app, ["agent", "scope", "set", "research"])
        assert result.exit_code == 2


class TestToken:
    def test_token_prints_secret_once_with_warning(self):
        with (
            patch(
                "cli.commands.agent.api_get",
                return_value=_resp(200, {"data": [_AGENT_ROW], "has_more": False, "next_cursor": None}),
            ),
            patch(
                "cli.commands.agent.api_post",
                return_value=_resp(
                    200,
                    {
                        "id": "tok_1",
                        "name": "ci",
                        "prefix": "abcd1234",
                        "agent_id": "ag_1",
                        "token": "agent-pat-secret-value",
                        "expires_at": "2026-10-01",
                        "created_at": "2026-07-01",
                    },
                ),
            ) as m,
        ):
            result = runner.invoke(app, ["agent", "token", "research", "--name", "ci"])
        assert result.exit_code == 0
        assert m.call_args.args[0] == "/api/v1/agents/ag_1/tokens"
        assert m.call_args.kwargs["json"] == {"name": "ci", "expires_in_days": 90}
        assert "agent-pat-secret-value" in result.output
        assert "ONCE" in result.output

    def test_token_expires_days_zero_means_never(self):
        with (
            patch(
                "cli.commands.agent.api_get",
                return_value=_resp(200, {"data": [_AGENT_ROW], "has_more": False, "next_cursor": None}),
            ),
            patch(
                "cli.commands.agent.api_post",
                return_value=_resp(
                    200,
                    {"id": "tok_1", "name": "ci", "prefix": "x", "agent_id": "ag_1", "token": "t", "expires_at": None},
                ),
            ) as m,
        ):
            result = runner.invoke(app, ["agent", "token", "research", "--name", "ci", "--expires-days", "0"])
        assert result.exit_code == 0
        assert m.call_args.kwargs["json"] == {"name": "ci", "expires_in_days": None}

    def test_token_raw_prints_only_secret(self):
        with (
            patch(
                "cli.commands.agent.api_get",
                return_value=_resp(200, {"data": [_AGENT_ROW], "has_more": False, "next_cursor": None}),
            ),
            patch(
                "cli.commands.agent.api_post",
                return_value=_resp(
                    200,
                    {
                        "id": "tok_1",
                        "name": "ci",
                        "prefix": "abcd1234",
                        "agent_id": "ag_1",
                        "token": "agent-pat-secret-value",
                        "expires_at": "2026-10-01",
                        "created_at": "2026-07-01",
                    },
                ),
            ),
        ):
            result = runner.invoke(app, ["agent", "token", "research", "--name", "ci", "--raw"])
        assert result.exit_code == 0
        # stdout is the bare secret, nothing else
        assert result.stdout.strip() == "agent-pat-secret-value"
        # the one-time warning went to stderr, not stdout
        assert "agent-pat-secret-value" not in result.stderr
        assert "shown ONCE" in result.stderr

    def test_token_not_selected_mode_renders_detail_code(self):
        with (
            patch(
                "cli.commands.agent.api_get",
                return_value=_resp(200, {"data": [_AGENT_ROW], "has_more": False, "next_cursor": None}),
            ),
            patch(
                "cli.commands.agent.api_post",
                return_value=_resp(
                    403,
                    {
                        "detail": {
                            "code": "agent_not_selected_mode",
                            "message": "agent PATs require all four scope modes to be 'selected'",
                        }
                    },
                ),
            ),
        ):
            result = runner.invoke(app, ["agent", "token", "research", "--name", "ci"])
        assert result.exit_code == 1
        assert "agent_not_selected_mode" in result.output


class TestDelete:
    def test_delete_requires_confirm_without_yes(self):
        with (
            patch(
                "cli.commands.agent.api_get",
                return_value=_resp(200, {"data": [_AGENT_ROW], "has_more": False, "next_cursor": None}),
            ),
            patch("cli.commands.agent.api_delete") as m,
        ):
            result = runner.invoke(app, ["agent", "delete", "research"], input="n\n")
        m.assert_not_called()
        assert result.exit_code != 0

    def test_delete_with_yes_calls_delete(self):
        with (
            patch(
                "cli.commands.agent.api_get",
                return_value=_resp(200, {"data": [_AGENT_ROW], "has_more": False, "next_cursor": None}),
            ),
            patch("cli.commands.agent.api_delete", return_value=_resp(204)) as m,
        ):
            result = runner.invoke(app, ["agent", "delete", "research", "--yes"])
        assert result.exit_code == 0
        assert m.call_args.args[0] == "/api/v1/agents/ag_1"


class TestAsk:
    def test_ask_sync_answer(self):
        with patch(
            "cli.commands.agent.api_post",
            return_value=_resp(
                200,
                {
                    "answer": "The answer is 42.",
                    "session_id": "sess_1",
                    "response_id": "resp_1",
                    "usage": {},
                    "agent_config_hash": "abc",
                    "request_id": "req_1",
                },
            ),
        ) as m:
            result = runner.invoke(app, ["agent", "ask", "research", "what is the answer?"])
        assert result.exit_code == 0
        assert m.call_args.args[0] == "/api/v1/agents/research/responses"
        assert m.call_args.kwargs["json"]["input"] == "what is the answer?"
        assert "The answer is 42." in result.output

    def test_ask_sync_answer_json(self):
        with patch(
            "cli.commands.agent.api_post",
            return_value=_resp(200, {"answer": "42", "session_id": "s1"}),
        ):
            result = runner.invoke(app, ["agent", "ask", "research", "q", "--json"])
        data = json.loads(result.output)
        assert data["answer"] == "42"

    def test_ask_background_polls_until_completed(self):
        job_queued = {"id": "job_1", "status": "queued", "result": None, "error": None}
        job_done = {"id": "job_1", "status": "completed", "result": {"answer": "final answer"}, "error": None}
        with (
            patch(
                "cli.commands.agent.api_post",
                return_value=_resp(202, {"job_id": "job_1"}),
            ),
            patch(
                "cli.commands.agent.api_get",
                side_effect=[_resp(200, job_queued), _resp(200, job_done)],
            ),
            patch("cli.commands.agent.time.sleep"),
        ):
            result = runner.invoke(app, ["agent", "ask", "research", "q"])
        assert result.exit_code == 0
        assert "final answer" in result.output

    def test_ask_background_job_failed(self):
        job_failed = {
            "id": "job_1",
            "status": "failed",
            "result": None,
            "error": {"code": "concurrency_cap", "message": "too many"},
        }
        with (
            patch("cli.commands.agent.api_post", return_value=_resp(202, {"job_id": "job_1"})),
            patch("cli.commands.agent.api_get", return_value=_resp(200, job_failed)),
            patch("cli.commands.agent.time.sleep"),
        ):
            result = runner.invoke(app, ["agent", "ask", "research", "q"])
        assert result.exit_code == 1
        assert "concurrency_cap" in result.output

    def test_ask_poll_bounded_by_timeout(self):
        """Never-terminal job must not loop forever — bounded by --timeout."""
        job_running = {"id": "job_1", "status": "in_progress", "result": None, "error": None}
        with (
            patch("cli.commands.agent.api_post", return_value=_resp(202, {"job_id": "job_1"})),
            patch("cli.commands.agent.api_get", return_value=_resp(200, job_running)) as m_get,
            patch("cli.commands.agent.time.sleep"),
        ):
            result = runner.invoke(app, ["agent", "ask", "research", "q", "--timeout", "4"])
        assert result.exit_code == 1
        # bounded: a handful of polls, not unbounded
        assert m_get.call_count <= 10
        assert "Timed out" in result.output

    def test_ask_error_renders_detail_code(self):
        with patch(
            "cli.commands.agent.api_post",
            return_value=_resp(404, {"detail": {"code": "agent_not_found"}}),
        ):
            result = runner.invoke(app, ["agent", "ask", "nope", "q"])
        assert result.exit_code == 1
        assert "agent_not_found" in result.output

    def test_ask_poll_budget_shrinks_by_time_spent_on_sync_post(self):
        """The sync POST can itself consume part of `--timeout` before
        returning 202 — the poll that follows must get only what's left,
        not the full budget again (that would let total wall-clock time
        run to ~2x `--timeout`)."""
        from cli.commands import agent as agent_mod

        with (
            patch.object(agent_mod, "api_post", return_value=_resp(202, {"job_id": "job_1"})),
            # First call = `start` at top of ask(); second = elapsed check on
            # the 202 path. Simulates the sync POST taking 45s of a 100s
            # budget. Patching the module-level `time` name (not the global
            # stdlib module) so this can't be perturbed by unrelated
            # `time.monotonic()` calls elsewhere in the process.
            patch.object(agent_mod, "time") as m_time,
            patch.object(
                agent_mod,
                "_poll_job",
                return_value={"status": "completed", "result": {"answer": "ok"}},
            ) as m_poll,
        ):
            m_time.monotonic.side_effect = [0.0, 45.0]
            result = runner.invoke(app, ["agent", "ask", "research", "q", "--timeout", "100"])
        assert result.exit_code == 0
        assert m_poll.call_args.args[0] == "job_1"
        # 100 (total) - 45 (already spent) = 55, not the full 100 again.
        assert m_poll.call_args.args[1] == 55

    def test_ask_poll_budget_floors_at_one_when_sync_post_ate_the_whole_timeout(self):
        from cli.commands import agent as agent_mod

        with (
            patch.object(agent_mod, "api_post", return_value=_resp(202, {"job_id": "job_1"})),
            patch.object(agent_mod, "time") as m_time,
            patch.object(
                agent_mod,
                "_poll_job",
                return_value={"status": "completed", "result": {"answer": "ok"}},
            ) as m_poll,
        ):
            m_time.monotonic.side_effect = [0.0, 500.0]
            result = runner.invoke(app, ["agent", "ask", "research", "q", "--timeout", "10"])
        assert result.exit_code == 0
        assert m_poll.call_args.args[1] == 1  # max(1, ...) floor, never <= 0


class TestPollJobDeadline:
    def test_poll_job_stops_at_wall_clock_deadline_even_with_many_attempts_left(self):
        """`_poll_job` must bound by the remaining wall-clock deadline, not
        just by a naive attempt count carried over from the full timeout —
        this is what lets `ask` hand it a shrunk `remaining` budget and have
        it actually be honored."""
        from cli.commands import agent as agent_mod

        job_running = {"id": "job_1", "status": "in_progress", "result": None, "error": None}
        # monotonic(): first call establishes the deadline inside _poll_job,
        # every call thereafter reports the deadline already passed.
        with (
            patch.object(agent_mod, "api_get", return_value=_resp(200, job_running)) as m_get,
            patch.object(agent_mod, "time") as m_time,
        ):
            m_time.monotonic.side_effect = [0.0] + [100.0] * 10
            with pytest.raises(typer.Exit):
                agent_mod._poll_job("job_1", 4.0)
        # exactly one attempt: the deadline check after it fires immediately.
        assert m_get.call_count == 1
        m_time.sleep.assert_not_called()


class TestUsage:
    _USAGE_BODY = {
        "period": "2026-07",
        "agent_slug": "research",
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_tokens": 10,
        "cache_creation_tokens": 5,
        "total_tokens": 155,
        "budget_limit": 1000,
        "budget_remaining": 845,
    }

    def test_usage_text(self):
        with patch("cli.commands.agent.api_get", return_value=_resp(200, self._USAGE_BODY)) as m:
            result = runner.invoke(app, ["agent", "usage", "research"])
        assert result.exit_code == 0
        assert m.call_args.args[0] == "/api/v1/agents/research/usage"
        assert m.call_args.kwargs["params"] == {}
        assert "155" in result.output
        assert "2026-07" in result.output

    def test_usage_with_period_flag(self):
        with patch("cli.commands.agent.api_get", return_value=_resp(200, self._USAGE_BODY)) as m:
            result = runner.invoke(app, ["agent", "usage", "research", "--period", "2026-06"])
        assert result.exit_code == 0
        assert m.call_args.kwargs["params"] == {"period": "2026-06"}

    def test_usage_json(self):
        with patch("cli.commands.agent.api_get", return_value=_resp(200, self._USAGE_BODY)):
            result = runner.invoke(app, ["agent", "usage", "research", "--json"])
        data = json.loads(result.output)
        assert data["total_tokens"] == 155

    def test_usage_unbounded_agent_renders_unbounded(self):
        body = dict(self._USAGE_BODY)
        body["budget_limit"] = None
        body["budget_remaining"] = None
        with patch("cli.commands.agent.api_get", return_value=_resp(200, body)):
            result = runner.invoke(app, ["agent", "usage", "research"])
        assert result.exit_code == 0
        assert "(unbounded)" in result.output

    def test_usage_error_renders_detail_code(self):
        with patch(
            "cli.commands.agent.api_get",
            return_value=_resp(404, {"detail": {"code": "agent_not_found"}}),
        ):
            result = runner.invoke(app, ["agent", "usage", "nope"])
        assert result.exit_code == 1
        assert "agent_not_found" in result.output


class TestWebhooks:
    _WEBHOOK_ROW = {
        "id": "wh_1",
        "agent_id": "ag_1",
        "url": "https://hooks.example.com/incoming",
        "events": ["job.completed", "job.failed"],
        "active": True,
        "consecutive_failures": 0,
        "created_at": "2026-07-01",
    }

    def test_webhooks_list_text(self):
        with patch(
            "cli.commands.agent.api_get",
            return_value=_resp(200, {"data": [self._WEBHOOK_ROW], "has_more": False, "next_cursor": None}),
        ) as m:
            result = runner.invoke(app, ["agent", "webhooks", "list", "research"])
        assert result.exit_code == 0
        assert m.call_args.args[0] == "/api/v1/agents/research/webhooks"
        assert "wh_1" in result.output
        assert "hooks.example.com" in result.output

    def test_webhooks_list_json(self):
        with patch(
            "cli.commands.agent.api_get",
            return_value=_resp(200, {"data": [self._WEBHOOK_ROW], "has_more": False, "next_cursor": None}),
        ):
            result = runner.invoke(app, ["agent", "webhooks", "list", "research", "--json"])
        data = json.loads(result.output)
        assert data[0]["id"] == "wh_1"

    def test_webhooks_list_empty_hints_add(self):
        with patch(
            "cli.commands.agent.api_get",
            return_value=_resp(200, {"data": [], "has_more": False, "next_cursor": None}),
        ):
            result = runner.invoke(app, ["agent", "webhooks", "list", "research"])
        assert result.exit_code == 0
        assert "agnes agent webhooks add" in result.output

    def test_webhooks_add_sends_url_and_prints_secret_once(self):
        created = dict(self._WEBHOOK_ROW)
        created["secret"] = "a" * 64
        with patch("cli.commands.agent.api_post", return_value=_resp(201, created)) as m:
            result = runner.invoke(
                app,
                ["agent", "webhooks", "add", "research", "--url", "https://hooks.example.com/incoming"],
            )
        assert result.exit_code == 0
        assert m.call_args.args[0] == "/api/v1/agents/research/webhooks"
        assert m.call_args.kwargs["json"] == {"url": "https://hooks.example.com/incoming"}
        assert "a" * 64 in result.output
        assert "ONCE" in result.output

    def test_webhooks_add_with_events(self):
        created = dict(self._WEBHOOK_ROW)
        created["secret"] = "s" * 64
        with patch("cli.commands.agent.api_post", return_value=_resp(201, created)) as m:
            result = runner.invoke(
                app,
                [
                    "agent",
                    "webhooks",
                    "add",
                    "research",
                    "--url",
                    "https://hooks.example.com/incoming",
                    "--event",
                    "job.completed",
                ],
            )
        assert result.exit_code == 0
        assert m.call_args.kwargs["json"] == {
            "url": "https://hooks.example.com/incoming",
            "events": ["job.completed"],
        }

    def test_webhooks_add_json(self):
        created = dict(self._WEBHOOK_ROW)
        created["secret"] = "s" * 64
        with patch("cli.commands.agent.api_post", return_value=_resp(201, created)):
            result = runner.invoke(
                app,
                ["agent", "webhooks", "add", "research", "--url", "https://hooks.example.com/x", "--json"],
            )
        data = json.loads(result.output)
        assert data["secret"] == "s" * 64

    def test_webhooks_add_error_renders_detail_code(self):
        with patch(
            "cli.commands.agent.api_post",
            return_value=_resp(400, {"detail": {"code": "webhook_url_forbidden"}}),
        ):
            result = runner.invoke(
                app,
                ["agent", "webhooks", "add", "research", "--url", "http://127.0.0.1/x"],
            )
        assert result.exit_code == 1
        assert "webhook_url_forbidden" in result.output

    def test_webhooks_delete_requires_confirm_without_yes(self):
        with patch("cli.commands.agent.api_delete") as m:
            result = runner.invoke(app, ["agent", "webhooks", "delete", "research", "wh_1"], input="n\n")
        m.assert_not_called()
        assert result.exit_code != 0

    def test_webhooks_delete_with_yes_calls_delete(self):
        with patch("cli.commands.agent.api_delete", return_value=_resp(204)) as m:
            result = runner.invoke(app, ["agent", "webhooks", "delete", "research", "wh_1", "--yes"])
        assert result.exit_code == 0
        assert m.call_args.args[0] == "/api/v1/agents/research/webhooks/wh_1"

    def test_webhooks_delete_not_found_renders_detail_code(self):
        with patch(
            "cli.commands.agent.api_delete",
            return_value=_resp(404, {"detail": {"code": "webhook_not_found"}}),
        ):
            result = runner.invoke(app, ["agent", "webhooks", "delete", "research", "wh_1", "--yes"])
        assert result.exit_code == 1
        assert "webhook_not_found" in result.output


class TestMemory:
    """`agnes agent memory list|approve|archive|delete` — CLI surface for the
    owner-facing memory-management API (`/api/v1/agents/{id}/memories[/{id}]`,
    agent-api V1c Task 5/7). Every subcommand resolves the slug to an agent
    id first (one `api_get` round trip on `/api/v1/agents`), same as every
    other slug-addressed subcommand."""

    _PENDING_ROW = {
        "id": "mem_1",
        "agent_id": "ag_1",
        "content": "The user prefers concise answers.",
        "status": "pending",
        "source_session_id": "sess_1",
        "created_at": "2026-07-20",
        "activated_at": None,
        "archived_at": None,
    }
    _ACTIVE_IN_BUDGET_ROW = {
        "id": "mem_2",
        "agent_id": "ag_1",
        "content": "The user's timezone is UTC+2.",
        "status": "active",
        "source_session_id": "sess_2",
        "created_at": "2026-07-21",
        "activated_at": "2026-07-21",
        "archived_at": None,
        "in_budget": True,
    }
    _ACTIVE_SHADOWED_ROW = {
        "id": "mem_3",
        "agent_id": "ag_1",
        "content": "An old fact that no longer fits the materialize budget.",
        "status": "active",
        "source_session_id": "sess_3",
        "created_at": "2026-06-01",
        "activated_at": "2026-06-01",
        "archived_at": None,
        "in_budget": False,
    }

    def _agents_resp(self):
        return _resp(200, {"data": [_AGENT_ROW], "has_more": False, "next_cursor": None})

    # -- list --------------------------------------------------------------

    def test_memory_list_text_shows_status_and_in_budget_marker(self):
        with patch(
            "cli.commands.agent.api_get",
            side_effect=[
                self._agents_resp(),
                _resp(
                    200,
                    {
                        "data": [self._PENDING_ROW, self._ACTIVE_IN_BUDGET_ROW, self._ACTIVE_SHADOWED_ROW],
                        "has_more": False,
                        "next_cursor": None,
                    },
                ),
            ],
        ) as m:
            result = runner.invoke(app, ["agent", "memory", "list", "research"])
        assert result.exit_code == 0
        assert m.call_args_list[0].args[0] == "/api/v1/agents"
        assert m.call_args_list[1].args[0] == "/api/v1/agents/ag_1/memories"
        assert m.call_args_list[1].kwargs["params"] == {}
        assert "mem_1" in result.output
        assert "pending" in result.output
        assert "mem_2" in result.output
        assert "in effect" in result.output
        assert "mem_3" in result.output
        assert "shadowed" in result.output

    def test_memory_list_json(self):
        with patch(
            "cli.commands.agent.api_get",
            side_effect=[
                self._agents_resp(),
                _resp(200, {"data": [self._ACTIVE_IN_BUDGET_ROW], "has_more": False, "next_cursor": None}),
            ],
        ):
            result = runner.invoke(app, ["agent", "memory", "list", "research", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data[0]["id"] == "mem_2"
        assert data[0]["in_budget"] is True

    def test_memory_list_with_status_filter(self):
        with patch(
            "cli.commands.agent.api_get",
            side_effect=[
                self._agents_resp(),
                _resp(200, {"data": [], "has_more": False, "next_cursor": None}),
            ],
        ) as m:
            result = runner.invoke(app, ["agent", "memory", "list", "research", "--status", "pending"])
        assert result.exit_code == 0
        assert m.call_args_list[1].kwargs["params"] == {"status": "pending"}

    def test_memory_list_empty(self):
        with patch(
            "cli.commands.agent.api_get",
            side_effect=[
                self._agents_resp(),
                _resp(200, {"data": [], "has_more": False, "next_cursor": None}),
            ],
        ):
            result = runner.invoke(app, ["agent", "memory", "list", "research"])
        assert result.exit_code == 0
        assert "No memories" in result.output

    def test_memory_list_error_renders_detail_code(self):
        with patch(
            "cli.commands.agent.api_get",
            side_effect=[
                self._agents_resp(),
                _resp(404, {"detail": {"code": "agent_not_found"}}),
            ],
        ):
            result = runner.invoke(app, ["agent", "memory", "list", "research"])
        assert result.exit_code == 1
        assert "agent_not_found" in result.output

    # -- approve -------------------------------------------------------------

    def test_memory_approve_sends_patch_with_action(self):
        approved = dict(self._PENDING_ROW)
        approved["status"] = "active"
        with (
            patch("cli.commands.agent.api_get", return_value=self._agents_resp()),
            patch("cli.commands.agent.api_patch", return_value=_resp(200, approved)) as m,
        ):
            result = runner.invoke(app, ["agent", "memory", "approve", "research", "mem_1"])
        assert result.exit_code == 0
        assert m.call_args.args[0] == "/api/v1/agents/ag_1/memories/mem_1"
        assert m.call_args.kwargs["json"] == {"action": "approve"}
        assert "mem_1" in result.output
        assert "approved" in result.output.lower()

    def test_memory_approve_error_renders_detail_code(self):
        with (
            patch("cli.commands.agent.api_get", return_value=self._agents_resp()),
            patch(
                "cli.commands.agent.api_patch",
                return_value=_resp(404, {"detail": {"code": "memory_not_found"}}),
            ),
        ):
            result = runner.invoke(app, ["agent", "memory", "approve", "research", "mem_nope"])
        assert result.exit_code == 1
        assert "memory_not_found" in result.output

    # -- archive ---------------------------------------------------------

    def test_memory_archive_sends_patch_with_action(self):
        archived = dict(self._ACTIVE_IN_BUDGET_ROW)
        archived["status"] = "archived"
        with (
            patch("cli.commands.agent.api_get", return_value=self._agents_resp()),
            patch("cli.commands.agent.api_patch", return_value=_resp(200, archived)) as m,
        ):
            result = runner.invoke(app, ["agent", "memory", "archive", "research", "mem_2"])
        assert result.exit_code == 0
        assert m.call_args.args[0] == "/api/v1/agents/ag_1/memories/mem_2"
        assert m.call_args.kwargs["json"] == {"action": "archive"}
        assert "mem_2" in result.output
        assert "archived" in result.output.lower()

    def test_memory_archive_error_renders_detail_code(self):
        with (
            patch("cli.commands.agent.api_get", return_value=self._agents_resp()),
            patch(
                "cli.commands.agent.api_patch",
                return_value=_resp(400, {"detail": {"code": "invalid_action"}}),
            ),
        ):
            result = runner.invoke(app, ["agent", "memory", "archive", "research", "mem_2"])
        assert result.exit_code == 1
        assert "invalid_action" in result.output

    # -- delete ------------------------------------------------------------

    def test_memory_delete_requires_confirm_without_yes(self):
        with (
            patch("cli.commands.agent.api_get", return_value=self._agents_resp()),
            patch("cli.commands.agent.api_delete") as m,
        ):
            result = runner.invoke(app, ["agent", "memory", "delete", "research", "mem_1"], input="n\n")
        m.assert_not_called()
        assert result.exit_code != 0

    def test_memory_delete_with_yes_calls_delete(self):
        with (
            patch("cli.commands.agent.api_get", return_value=self._agents_resp()),
            patch("cli.commands.agent.api_delete", return_value=_resp(204)) as m,
        ):
            result = runner.invoke(app, ["agent", "memory", "delete", "research", "mem_1", "--yes"])
        assert result.exit_code == 0
        assert m.call_args.args[0] == "/api/v1/agents/ag_1/memories/mem_1"

    def test_memory_delete_not_found_renders_detail_code(self):
        with (
            patch("cli.commands.agent.api_get", return_value=self._agents_resp()),
            patch(
                "cli.commands.agent.api_delete",
                return_value=_resp(404, {"detail": {"code": "memory_not_found"}}),
            ),
        ):
            result = runner.invoke(app, ["agent", "memory", "delete", "research", "mem_1", "--yes"])
        assert result.exit_code == 1
        assert "memory_not_found" in result.output


class TestSchedule:
    """`agnes agent schedule list|add|remove|enable|disable` — CLI surface
    for `/api/v1/agents/{slug}/schedules[/{schedule_id}]` (agent-schedules
    design). Slug-keyed like webhooks; `remove`/`enable`/`disable` resolve a
    schedule name to id via one `list` round trip first."""

    _SCHEDULE_ROW = {
        "id": "sched_1",
        "agent_id": "ag_1",
        "name": "morning-briefing",
        "schedule": "cron 0 7 * * 1-5",
        "prompt": "invoke the briefing skill",
        "enabled": True,
        "last_run_at": None,
        "last_status": None,
        "last_job_id": None,
        "created_at": "2026-08-17",
        "updated_at": "2026-08-17",
    }

    def test_schedule_list_text(self):
        with patch(
            "cli.commands.agent.api_get",
            return_value=_resp(200, {"data": [self._SCHEDULE_ROW], "has_more": False, "next_cursor": None}),
        ) as m:
            result = runner.invoke(app, ["agent", "schedule", "list", "research"])
        assert result.exit_code == 0
        assert m.call_args.args[0] == "/api/v1/agents/research/schedules"
        assert "morning-briefing" in result.output
        assert "cron 0 7 * * 1-5" in result.output

    def test_schedule_list_json(self):
        with patch(
            "cli.commands.agent.api_get",
            return_value=_resp(200, {"data": [self._SCHEDULE_ROW], "has_more": False, "next_cursor": None}),
        ):
            result = runner.invoke(app, ["agent", "schedule", "list", "research", "--json"])
        data = json.loads(result.output)
        assert data[0]["id"] == "sched_1"

    def test_schedule_list_empty_hints_add(self):
        with patch(
            "cli.commands.agent.api_get",
            return_value=_resp(200, {"data": [], "has_more": False, "next_cursor": None}),
        ):
            result = runner.invoke(app, ["agent", "schedule", "list", "research"])
        assert result.exit_code == 0
        assert "agnes agent schedule add" in result.output

    def test_schedule_add_sends_payload(self):
        with patch("cli.commands.agent.api_post", return_value=_resp(201, dict(self._SCHEDULE_ROW))) as m:
            result = runner.invoke(
                app,
                [
                    "agent",
                    "schedule",
                    "add",
                    "research",
                    "--name",
                    "morning-briefing",
                    "--schedule",
                    "cron 0 7 * * 1-5",
                    "--prompt",
                    "invoke the briefing skill",
                ],
            )
        assert result.exit_code == 0
        assert m.call_args.args[0] == "/api/v1/agents/research/schedules"
        assert m.call_args.kwargs["json"] == {
            "name": "morning-briefing",
            "schedule": "cron 0 7 * * 1-5",
            "prompt": "invoke the briefing skill",
            "enabled": True,
        }
        assert "sched_1" in result.output

    def test_schedule_add_disabled_flag(self):
        with patch("cli.commands.agent.api_post", return_value=_resp(201, dict(self._SCHEDULE_ROW))) as m:
            result = runner.invoke(
                app,
                [
                    "agent",
                    "schedule",
                    "add",
                    "research",
                    "--name",
                    "morning-briefing",
                    "--schedule",
                    "cron 0 7 * * 1-5",
                    "--prompt",
                    "p",
                    "--disabled",
                ],
            )
        assert result.exit_code == 0
        assert m.call_args.kwargs["json"]["enabled"] is False

    _SKILLS_RESP = {
        "skills": [
            {
                "marketplace_id": "mp1",
                "plugin_name": "scout",
                "skill_name": "scout-briefing",
                "name": "Scout Briefing",
                "description": "Prepare the morning briefing.",
                "invocation": None,
                "body": "...",
            },
            {
                "marketplace_id": "mp1",
                "plugin_name": "housekeeping",
                "skill_name": "cleanup",
                "name": "Cleanup",
                "description": "",
                "invocation": None,
                "body": "...",
            },
            {
                "marketplace_id": "mp2",
                "plugin_name": "other-plugin",
                "skill_name": "cleanup",
                "name": "Cleanup",
                "description": "Different plugin, same skill name.",
                "invocation": None,
                "body": "...",
            },
        ]
    }

    def test_schedule_add_skill_templates_prompt(self):
        """--skill resolves against /api/v2/marketplace/skills and sends the
        same templated prompt the builder UI's Skill dropdown produces —
        client-side sugar only, the payload stays a plain prompt string."""
        with (
            patch("cli.commands.agent.api_get", return_value=_resp(200, self._SKILLS_RESP)) as g,
            patch("cli.commands.agent.api_post", return_value=_resp(201, dict(self._SCHEDULE_ROW))) as m,
        ):
            result = runner.invoke(
                app,
                [
                    "agent",
                    "schedule",
                    "add",
                    "research",
                    "--name",
                    "morning-briefing",
                    "--schedule",
                    "cron 0 7 * * 1-5",
                    "--skill",
                    "Scout Briefing",
                ],
            )
        assert result.exit_code == 0
        assert g.call_args.args[0] == "/api/v2/marketplace/skills"
        assert m.call_args.kwargs["json"]["prompt"] == "Run the Scout Briefing skill: Prepare the morning briefing."

    def test_schedule_add_skill_without_description_ends_with_period(self):
        with (
            patch("cli.commands.agent.api_get", return_value=_resp(200, self._SKILLS_RESP)),
            patch("cli.commands.agent.api_post", return_value=_resp(201, dict(self._SCHEDULE_ROW))) as m,
        ):
            result = runner.invoke(
                app,
                [
                    "agent",
                    "schedule",
                    "add",
                    "research",
                    "--name",
                    "n",
                    "--schedule",
                    "every 1h",
                    "--skill",
                    "housekeeping:cleanup",
                ],
            )
        assert result.exit_code == 0
        assert m.call_args.kwargs["json"]["prompt"] == "Run the Cleanup skill."

    def test_schedule_add_skill_ambiguous_lists_qualified_names(self):
        """Two plugins ship a skill named 'cleanup' — the bare name must not
        silently pick one."""
        with patch("cli.commands.agent.api_get", return_value=_resp(200, self._SKILLS_RESP)):
            result = runner.invoke(
                app,
                ["agent", "schedule", "add", "research", "--name", "n", "--schedule", "every 1h", "--skill", "cleanup"],
            )
        assert result.exit_code == 1
        assert "ambiguous" in result.output
        assert "housekeeping:cleanup" in result.output
        assert "other-plugin:cleanup" in result.output

    def test_schedule_add_skill_not_found_lists_available(self):
        with patch("cli.commands.agent.api_get", return_value=_resp(200, self._SKILLS_RESP)):
            result = runner.invoke(
                app,
                ["agent", "schedule", "add", "research", "--name", "n", "--schedule", "every 1h", "--skill", "nope"],
            )
        assert result.exit_code == 1
        assert "Skill not found: nope" in result.output
        assert "scout:scout-briefing" in result.output

    def test_schedule_add_requires_exactly_one_of_prompt_and_skill(self):
        for extra in ([], ["--prompt", "p", "--skill", "cleanup"]):
            result = runner.invoke(
                app,
                ["agent", "schedule", "add", "research", "--name", "n", "--schedule", "every 1h"] + extra,
            )
            assert result.exit_code == 1
            assert "--prompt or --skill" in result.output

    def test_schedule_add_invalid_schedule_renders_detail_code(self):
        with patch(
            "cli.commands.agent.api_post",
            return_value=_resp(
                400,
                {"detail": {"code": "invalid_schedule", "message": "schedule must be ... note the literal 'cron '"}},
            ),
        ):
            result = runner.invoke(
                app,
                [
                    "agent",
                    "schedule",
                    "add",
                    "research",
                    "--name",
                    "x",
                    "--schedule",
                    "weekly",
                    "--prompt",
                    "p",
                ],
            )
        assert result.exit_code == 1
        assert "invalid_schedule" in result.output

    def test_schedule_remove_requires_confirm_without_yes(self):
        with (
            patch(
                "cli.commands.agent.api_get",
                return_value=_resp(200, {"data": [self._SCHEDULE_ROW], "has_more": False, "next_cursor": None}),
            ),
            patch("cli.commands.agent.api_delete") as m,
        ):
            result = runner.invoke(app, ["agent", "schedule", "remove", "research", "morning-briefing"], input="n\n")
        m.assert_not_called()
        assert result.exit_code != 0

    def test_schedule_remove_with_yes_calls_delete(self):
        with (
            patch(
                "cli.commands.agent.api_get",
                return_value=_resp(200, {"data": [self._SCHEDULE_ROW], "has_more": False, "next_cursor": None}),
            ),
            patch("cli.commands.agent.api_delete", return_value=_resp(204)) as m,
        ):
            result = runner.invoke(app, ["agent", "schedule", "remove", "research", "morning-briefing", "--yes"])
        assert result.exit_code == 0
        assert m.call_args.args[0] == "/api/v1/agents/research/schedules/sched_1"

    def test_schedule_remove_not_found_hints_list(self):
        with patch(
            "cli.commands.agent.api_get",
            return_value=_resp(200, {"data": [], "has_more": False, "next_cursor": None}),
        ):
            result = runner.invoke(app, ["agent", "schedule", "remove", "research", "nope", "--yes"])
        assert result.exit_code == 1
        assert "agnes agent schedule list research" in result.output

    def test_schedule_enable_sends_patch(self):
        with (
            patch(
                "cli.commands.agent.api_get",
                return_value=_resp(
                    200,
                    {
                        "data": [{**self._SCHEDULE_ROW, "enabled": False}],
                        "has_more": False,
                        "next_cursor": None,
                    },
                ),
            ),
            patch(
                "cli.commands.agent.api_patch",
                return_value=_resp(200, {**self._SCHEDULE_ROW, "enabled": True}),
            ) as m,
        ):
            result = runner.invoke(app, ["agent", "schedule", "enable", "research", "morning-briefing"])
        assert result.exit_code == 0
        assert m.call_args.args[0] == "/api/v1/agents/research/schedules/sched_1"
        assert m.call_args.kwargs["json"] == {"enabled": True}
        assert "enabled" in result.output

    def test_schedule_disable_sends_patch(self):
        with (
            patch(
                "cli.commands.agent.api_get",
                return_value=_resp(200, {"data": [self._SCHEDULE_ROW], "has_more": False, "next_cursor": None}),
            ),
            patch(
                "cli.commands.agent.api_patch",
                return_value=_resp(200, {**self._SCHEDULE_ROW, "enabled": False}),
            ) as m,
        ):
            result = runner.invoke(app, ["agent", "schedule", "disable", "research", "morning-briefing"])
        assert result.exit_code == 0
        assert m.call_args.kwargs["json"] == {"enabled": False}
        assert "disabled" in result.output


class TestTimeoutDriftGuard:
    def test_cli_ask_timeout_constants_match_server_defaults(self):
        """The CLI's `_DEFAULT_ASK_TIMEOUT_S`/`_MAX_ASK_TIMEOUT_S` are kept in
        manual sync with `app.api.agent_runtime`'s `_DEFAULT_TIMEOUT_S`/
        `_MAX_TIMEOUT_S` (per the module docstring in `cli/commands/agent.py`)
        since the CLI has no import-time dependency on the FastAPI router.
        This test is the tripwire for that manual sync drifting."""
        from app.api import agent_runtime
        from cli.commands import agent as agent_mod

        assert agent_mod._DEFAULT_ASK_TIMEOUT_S == agent_runtime._DEFAULT_TIMEOUT_S
        assert agent_mod._MAX_ASK_TIMEOUT_S == agent_runtime._MAX_TIMEOUT_S
