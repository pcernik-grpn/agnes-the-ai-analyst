"""Tests for `agnes admin activity` subcommands.

Pattern mirrors tests/test_cli_admin_news.py: monkey-patch cli.client api_*
helpers to route through a FastAPI TestClient with an admin session, then
invoke the Typer app via CliRunner.

Covers:
- timeline (default callback): success, --json, --since, --action filter, admin-only enforcement
- health: success, --json
- sync: success, --json, --since
"""

from __future__ import annotations

import json
import tempfile
import uuid

import pytest
from typer.testing import CliRunner

_ANSI_RE = __import__("re").compile(r"\x1b\[[0-9;]*m")


def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        yield tmp


def _make_admin_test_client():
    from fastapi.testclient import TestClient

    from app.auth.jwt import create_access_token
    from app.main import app
    from src.db import close_system_db, get_system_db
    from src.repositories.users import UserRepository

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="admin@activity.test", name="admin")
        admin_group = conn.execute("SELECT id FROM user_groups WHERE name = 'Admin'").fetchone()
        conn.execute(
            "INSERT INTO user_group_members (user_id, group_id, source, added_by) VALUES (?, ?, 'admin', 'test')",
            [uid, admin_group[0]],
        )
        token = create_access_token(user_id=uid, email="admin@activity.test")
    finally:
        conn.close()
        close_system_db()

    c = TestClient(app)
    c.cookies.set("access_token", token)
    return c


def _make_non_admin_test_client():
    from fastapi.testclient import TestClient

    from app.auth.jwt import create_access_token
    from app.main import app
    from src.db import close_system_db, get_system_db
    from src.repositories.users import UserRepository

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="analyst@activity.test", name="analyst")
        token = create_access_token(user_id=uid, email="analyst@activity.test")
    finally:
        conn.close()
        close_system_db()

    c = TestClient(app)
    c.cookies.set("access_token", token)
    return c


@pytest.fixture
def cli_admin(monkeypatch, fresh_db):
    """CliRunner + activity_app wired to an admin TestClient."""
    test_client = _make_admin_test_client()

    def _get(path, **kw):
        params = kw.get("params") or {}
        return test_client.get(path, params=params)

    import cli.client

    monkeypatch.setattr(cli.client, "api_get", _get)

    import cli.commands.admin_activity as mod

    monkeypatch.setattr(mod, "api_get", _get)

    return CliRunner(), mod.activity_app


@pytest.fixture
def cli_non_admin(monkeypatch, fresh_db):
    """CliRunner + activity_app wired to a non-admin TestClient."""
    test_client = _make_non_admin_test_client()

    def _get(path, **kw):
        params = kw.get("params") or {}
        return test_client.get(path, params=params)

    import cli.client

    monkeypatch.setattr(cli.client, "api_get", _get)

    import cli.commands.admin_activity as mod

    monkeypatch.setattr(mod, "api_get", _get)

    return CliRunner(), mod.activity_app


# ---------------------------------------------------------------------------
# Timeline tests
# ---------------------------------------------------------------------------


class TestTimeline:
    def test_timeline_success_table_output(self, cli_admin):
        runner, app = cli_admin
        r = runner.invoke(app, [])
        assert r.exit_code == 0, _clean(r.output)
        # Should print column headers
        out = _clean(r.output)
        assert "TIME" in out or "ACTION" in out or "rows" in out.lower() or "No activity" in out

    def test_timeline_json_is_valid(self, cli_admin):
        runner, app = cli_admin
        r = runner.invoke(app, ["--json"])
        assert r.exit_code == 0, _clean(r.output)
        data = json.loads(r.output)
        assert "rows" in data

    def test_timeline_since_filter(self, cli_admin):
        runner, app = cli_admin
        r = runner.invoke(app, ["--since", "1h", "--json"])
        assert r.exit_code == 0, _clean(r.output)
        data = json.loads(r.output)
        # since=1h → since_minutes=60; server accepts and returns rows key
        assert "rows" in data

    def test_timeline_action_prefix_filter(self, cli_admin):
        runner, app = cli_admin
        r = runner.invoke(app, ["--action", "sync.", "--json"])
        assert r.exit_code == 0, _clean(r.output)
        data = json.loads(r.output)
        assert "rows" in data

    def test_timeline_result_class_and_source_filters(self, cli_admin):
        runner, app = cli_admin
        r = runner.invoke(app, ["--result-class", "success", "--source", "cli", "--json"])
        assert r.exit_code == 0, _clean(r.output)
        data = json.loads(r.output)
        assert data["filter"]["result_class"] == "success"
        assert data["filter"]["source"] == "cli"
        # self-reads hidden by default; flag flips it
        assert data["filter"]["include_self_reads"] is False
        r2 = runner.invoke(app, ["--include-self-reads", "--json"])
        assert r2.exit_code == 0, _clean(r2.output)
        assert json.loads(r2.output)["filter"]["include_self_reads"] is True

    def test_timeline_admin_only(self, cli_non_admin):
        runner, app = cli_non_admin
        r = runner.invoke(app, ["--json"])
        assert r.exit_code != 0
        out = _clean(r.output)
        assert "auth" in out.lower() or "403" in out or "401" in out or "forbidden" in out.lower()

    def test_timeline_since_7d(self, cli_admin):
        runner, app = cli_admin
        r = runner.invoke(app, ["--since", "7d", "--json"])
        assert r.exit_code == 0, _clean(r.output)
        data = json.loads(r.output)
        assert "rows" in data


# ---------------------------------------------------------------------------
# Cursor pagination tests — the endpoint has always paged (app/api/activity.py
# cursor_ts/cursor_id), but nothing let a CLI caller hand the cursor back.
# ---------------------------------------------------------------------------


def _seed_two_cursor_rows():
    from src.db import get_system_db
    from src.repositories.audit import AuditRepository

    conn = get_system_db()
    repo = AuditRepository(conn)
    repo.log(action="test.cursor.first", result="success")
    repo.log(action="test.cursor.second", result="success")
    conn.close()


class TestCursorPagination:
    def test_cursor_flag_advances_to_the_next_page(self, cli_admin):
        runner, app = cli_admin
        _seed_two_cursor_rows()

        r1 = runner.invoke(app, ["--action", "test.cursor.", "--limit", "1", "--json"])
        assert r1.exit_code == 0, _clean(r1.output)
        page1 = json.loads(r1.output)
        assert len(page1["rows"]) == 1
        assert page1["rows"][0]["action"] == "test.cursor.second"
        assert page1["next_cursor"] is not None

        cursor_arg = json.dumps(page1["next_cursor"])
        r2 = runner.invoke(app, ["--action", "test.cursor.", "--limit", "1", "--cursor", cursor_arg, "--json"])
        assert r2.exit_code == 0, _clean(r2.output)
        page2 = json.loads(r2.output)
        assert len(page2["rows"]) == 1
        # Second page must surface a row the first page did not.
        assert page2["rows"][0]["action"] == "test.cursor.first"
        assert page2["rows"][0]["action"] != page1["rows"][0]["action"]

    def test_cursor_forwards_ts_and_id_to_the_server(self, cli_admin, monkeypatch):
        runner, app = cli_admin
        captured: dict = {}

        import cli.commands.admin_activity as mod

        real_get = mod.api_get

        def _spy(path, **kw):
            captured["params"] = kw.get("params") or {}
            return real_get(path, **kw)

        monkeypatch.setattr(mod, "api_get", _spy)

        cursor_arg = json.dumps({"ts": "2026-09-09T10:00:00+00:00", "id": "abc-123"})
        r = runner.invoke(app, ["--cursor", cursor_arg, "--json"])
        assert r.exit_code == 0, _clean(r.output)
        assert captured["params"]["cursor_ts"] == "2026-09-09T10:00:00+00:00"
        assert captured["params"]["cursor_id"] == "abc-123"

    def test_malformed_cursor_is_a_clean_error(self, cli_admin):
        runner, app = cli_admin
        r = runner.invoke(app, ["--cursor", "not-json", "--json"])
        assert r.exit_code != 0
        assert "--cursor" in _clean(r.output)

    def test_more_rows_hint_names_the_cursor_flag(self, cli_admin):
        """The old hint ('pass --limit higher') was unachievable — --limit
        caps at 200 and narrowing --since cannot reach row 201 either. The
        fixed hint must name a flag that actually exists."""
        runner, app = cli_admin
        _seed_two_cursor_rows()

        r = runner.invoke(app, ["--action", "test.cursor.", "--limit", "1"])
        assert r.exit_code == 0, _clean(r.output)
        out = _clean(r.output)
        assert "--cursor" in out
        assert "--limit higher" not in out


# ---------------------------------------------------------------------------
# Health tests
# ---------------------------------------------------------------------------


class TestHealth:
    def test_health_success_table_output(self, cli_admin):
        runner, app = cli_admin
        r = runner.invoke(app, ["health"])
        assert r.exit_code == 0, _clean(r.output)
        out = _clean(r.output)
        # Should include at least the status or one field key
        assert any(k in out for k in ("green", "yellow", "red", "scheduler", "sync_24h", "STATUS"))

    def test_health_json_is_valid(self, cli_admin):
        runner, app = cli_admin
        r = runner.invoke(app, ["health", "--json"])
        assert r.exit_code == 0, _clean(r.output)
        data = json.loads(r.output)
        assert "status" in data
        assert data["status"] in ("green", "yellow", "red")
        assert "fields" in data
        assert "sentence" in data

    def test_health_admin_only(self, cli_non_admin):
        runner, app = cli_non_admin
        r = runner.invoke(app, ["health"])
        assert r.exit_code != 0


# ---------------------------------------------------------------------------
# Sync tests
# ---------------------------------------------------------------------------


class TestSync:
    def test_sync_success_table_output(self, cli_admin):
        runner, app = cli_admin
        r = runner.invoke(app, ["sync"])
        assert r.exit_code == 0, _clean(r.output)
        out = _clean(r.output)
        assert "TABLE" in out or "No sync" in out or "rows" in out.lower()

    def test_sync_json_is_valid(self, cli_admin):
        runner, app = cli_admin
        r = runner.invoke(app, ["sync", "--json"])
        assert r.exit_code == 0, _clean(r.output)
        data = json.loads(r.output)
        assert "rows" in data

    def test_sync_since_filter(self, cli_admin):
        runner, app = cli_admin
        r = runner.invoke(app, ["sync", "--since", "7d", "--json"])
        assert r.exit_code == 0, _clean(r.output)
        data = json.loads(r.output)
        assert "rows" in data

    def test_sync_admin_only(self, cli_non_admin):
        runner, app = cli_non_admin
        r = runner.invoke(app, ["sync"])
        assert r.exit_code != 0
