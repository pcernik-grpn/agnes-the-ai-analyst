"""CLI tests for `agnes issue …` and `agnes admin issue …` (issue reporting,
step 1, Task 3).

Mocks the HTTP layer the same way ``tests/test_cli_facts.py`` /
``tests/test_cli_agent.py`` do: patch ``cli.commands.issue.api_{get,post,put}``
and ``cli.commands.admin_issue.api_{get,post}`` with a small dict-routed fake
(no live TestClient) — these are CLI-shape tests (right payload to the right
path, right output/exit code for a given API response). Server-side behaviour
is covered by ``tests/test_issues_endpoint.py`` / ``tests/db_pg/
test_issues_api_pg.py`` (Task 2).
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from cli.main import app


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    yield tmp_path


_NOT_FOUND = (404, {"detail": {"error": "issue_not_found", "message": "nope"}})


class _FakeResp:
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json = json_data
        self.text = "" if json_data is None else json.dumps(json_data, default=str)

    def json(self):
        return self._json


class _FakeApi:
    """Dict-routed fake for the `api_get`/`api_post`/`api_put` module
    functions — keyed by the exact path string the CLI command calls with.
    A miss answers a generic 404 `issue_not_found`, matching what the real
    API would answer for an unknown id."""

    def __init__(self):
        self.get: dict[str, tuple[int, dict | None]] = {}
        self.post: dict[str, tuple[int, dict | None]] = {}
        self.put: dict[str, tuple[int, dict | None]] = {}
        self.last_post_json = None
        self.last_put_bytes = None

    def do_get(self, path, **kwargs):
        status, data = self.get.get(path, _NOT_FOUND)
        return _FakeResp(status, data)

    def do_post(self, path, **kwargs):
        self.last_post_json = kwargs.get("json")
        status, data = self.post.get(path, _NOT_FOUND)
        return _FakeResp(status, data)

    def do_put(self, path, **kwargs):
        self.last_put_bytes = kwargs.get("content")
        status, data = self.put.get(path, _NOT_FOUND)
        return _FakeResp(status, data)


@pytest.fixture
def fake_api(monkeypatch):
    api = _FakeApi()
    monkeypatch.setattr("cli.commands.issue.api_get", api.do_get)
    monkeypatch.setattr("cli.commands.issue.api_post", api.do_post)
    monkeypatch.setattr("cli.commands.issue.api_put", api.do_put)
    monkeypatch.setattr("cli.commands.admin_issue.api_get", api.do_get)
    monkeypatch.setattr("cli.commands.admin_issue.api_post", api.do_post)
    return api


# ---------------------------------------------------------------------------
# agnes issue report
# ---------------------------------------------------------------------------


def test_report_prints_number_and_id(runner, fake_api):
    fake_api.post["/api/issues"] = (201, {"id": "iss_abc", "number": 42, "webhook_delivered_at": None})
    r = runner.invoke(
        app,
        ["issue", "report", "Tables render raw", "-m", "while streaming", "--kind", "bug", "--url", "/chat?session=x"],
    )
    assert r.exit_code == 0, r.stderr
    assert "Filed #42 (iss_abc)" in r.stdout
    assert fake_api.last_post_json["kind"] == "bug"
    assert fake_api.last_post_json["page_url"] == "/chat?session=x"
    assert "kept in this instance" in r.stdout  # webhook not delivered → say so


def test_report_json(runner, fake_api):
    fake_api.post["/api/issues"] = (201, {"id": "iss_abc", "number": 42})
    r = runner.invoke(app, ["issue", "report", "x", "--json"])
    assert r.exit_code == 0, r.stderr
    assert json.loads(r.stdout)["number"] == 42


def test_report_attach_doctor_embeds_client_section(runner, fake_api, monkeypatch):
    monkeypatch.setattr("cli.commands.issue._doctor_client_section", lambda: {"cli_version": "0.98.3"})
    fake_api.post["/api/issues"] = (201, {"id": "iss_abc", "number": 1})
    r = runner.invoke(app, ["issue", "report", "x", "--attach-doctor"])
    assert r.exit_code == 0, r.stderr
    assert fake_api.last_post_json["context"]["doctor"] == {"cli_version": "0.98.3"}


def test_report_screenshot_puts_png(runner, fake_api, tmp_path):
    png = tmp_path / "s.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 8)
    fake_api.post["/api/issues"] = (201, {"id": "iss_abc", "number": 1})
    fake_api.put["/api/issues/iss_abc/screenshot"] = (204, None)
    r = runner.invoke(app, ["issue", "report", "x", "--screenshot", str(png)])
    assert r.exit_code == 0, r.stderr
    assert fake_api.last_put_bytes.startswith(b"\x89PNG")


def test_report_invalid_kind_exits_before_any_call(runner, fake_api):
    r = runner.invoke(app, ["issue", "report", "x", "--kind", "nope"])
    assert r.exit_code == 2
    assert "--kind must be one of" in r.stderr
    assert fake_api.last_post_json is None


def test_report_501_renders_one_sentence(runner, fake_api):
    fake_api.post["/api/issues"] = (501, {"error": "requires_postgres_backend", "feature": "issue_reports"})
    r = runner.invoke(app, ["issue", "report", "x"])
    assert r.exit_code == 1
    assert "Postgres" in r.stderr


# ---------------------------------------------------------------------------
# agnes issue list
# ---------------------------------------------------------------------------


def test_list_table_and_truncation(runner, fake_api):
    fake_api.get["/api/issues/mine"] = (
        200,
        {
            "data": [
                {
                    "number": 42,
                    "kind": "bug",
                    "status": "open",
                    "comment_count": 1,
                    "created_at": "2026-09-09T10:00:00Z",
                    "title": "Tables render raw",
                }
            ],
            "count": 1,
            "truncated": {"limit": 1, "total": 3},
        },
    )
    r = runner.invoke(app, ["issue", "list", "--limit", "1"])
    assert r.exit_code == 0, r.stderr
    assert "#42" in r.stdout
    assert "bug" in r.stdout
    assert "1 reply" in r.stdout
    assert "showing 1 of 3" in r.stdout

    j = runner.invoke(app, ["issue", "list", "--limit", "1", "--json"])
    assert json.loads(j.stdout)["truncated"] == {"limit": 1, "total": 3}


def test_list_empty_points_forward(runner, fake_api):
    fake_api.get["/api/issues/mine"] = (200, {"data": [], "count": 0, "truncated": None})
    r = runner.invoke(app, ["issue", "list"])
    assert r.exit_code == 0, r.stderr
    assert "agnes issue report" in r.stdout


# ---------------------------------------------------------------------------
# agnes issue show / comment
# ---------------------------------------------------------------------------


def test_show_404_hints(runner, fake_api):
    fake_api.get["/api/issues/99"] = (404, {"detail": {"error": "issue_not_found", "message": "nope"}})
    r = runner.invoke(app, ["issue", "show", "99"])
    assert r.exit_code == 1
    assert "agnes issue list" in r.stderr


def test_show_renders_header_body_and_comments(runner, fake_api):
    fake_api.get["/api/issues/42"] = (
        200,
        {
            "number": 42,
            "kind": "bug",
            "status": "open",
            "created_at": "2026-09-09T10:00:00Z",
            "created_by": "u1",
            "created_by_email": "a@example.com",
            "body": "while streaming",
            "comments": [
                {
                    "author_kind": "admin",
                    "author_email": "ops@example.com",
                    "created_at": "2026-09-09T11:00:00Z",
                    "body": "on it",
                }
            ],
        },
    )
    r = runner.invoke(app, ["issue", "show", "42"])
    assert r.exit_code == 0, r.stderr
    assert "#42" in r.stdout
    assert "while streaming" in r.stdout
    assert "on it" in r.stdout


def test_comment_posts_body_and_confirms(runner, fake_api):
    fake_api.post["/api/issues/42/comments"] = (
        201,
        {"id": "isc_1", "issue_id": "iss_abc", "author_kind": "reporter", "body": "more detail"},
    )
    r = runner.invoke(app, ["issue", "comment", "42", "more detail"])
    assert r.exit_code == 0, r.stderr
    assert fake_api.last_post_json == {"body": "more detail"}
    assert "Comment added to #42" in r.stdout


# ---------------------------------------------------------------------------
# agnes admin issue …
# ---------------------------------------------------------------------------


def test_admin_list_shows_reporter_column(runner, fake_api):
    fake_api.get["/api/admin/issues"] = (
        200,
        {
            "data": [
                {
                    "number": 7,
                    "kind": "request",
                    "status": "open",
                    "created_by_email": "a@example.com",
                    "title": "Add a filter",
                }
            ],
            "count": 1,
            "truncated": None,
        },
    )
    r = runner.invoke(app, ["admin", "issue", "list"])
    assert r.exit_code == 0, r.stderr
    assert "#7" in r.stdout
    assert "a@example.com" in r.stdout


def test_admin_reply_posts_and_confirms(runner, fake_api):
    fake_api.post["/api/issues/7/comments"] = (
        201,
        {"id": "isc_2", "issue_id": "iss_x", "author_kind": "admin", "body": "on it"},
    )
    r = runner.invoke(app, ["admin", "issue", "reply", "7", "on it"])
    assert r.exit_code == 0, r.stderr
    assert fake_api.last_post_json == {"body": "on it"}
    assert "Reply added to #7" in r.stdout


def test_admin_resolve_success(runner, fake_api):
    fake_api.post["/api/admin/issues/42/resolve"] = (
        200,
        {"number": 42, "status": "resolved", "resolved_by": "ops@example.com"},
    )
    r = runner.invoke(app, ["admin", "issue", "resolve", "42", "--note", "fixed"])
    assert r.exit_code == 0, r.stderr
    assert fake_api.last_post_json == {"resolution_note": "fixed"}
    assert "Resolved: #42 by ops@example.com" in r.stdout


def test_admin_resolve_409(runner, fake_api):
    fake_api.post["/api/admin/issues/42/resolve"] = (
        409,
        {"detail": {"error": "already_resolved", "message": "#42 was already resolved by ops at T"}},
    )
    r = runner.invoke(app, ["admin", "issue", "resolve", "42", "--note", "fixed"])
    assert r.exit_code == 1
    assert "already resolved" in r.stderr


def test_admin_resolve_not_found(runner, fake_api):
    r = runner.invoke(app, ["admin", "issue", "resolve", "999"])
    assert r.exit_code == 1
    assert "agnes admin issue list" in r.stderr
