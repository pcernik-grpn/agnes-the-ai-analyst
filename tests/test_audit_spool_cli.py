"""Client-side spool for offline CLI audit events (F3 — audit-full-coverage
plan, Task 9): `cli.lib.audit_spool` (record/drain/commit) and its two
producers, `agnes query` (local path) and `agnes explore` (local path).
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from cli.config import set_workspace_root
from cli.main import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# cli.lib.audit_spool — unit tests
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A configured `workspace_root` — the `.claude/` state dir the spool
    (and the push ledger) both live under."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "config"))
    ws = tmp_path / "workspace"
    ws.mkdir()
    set_workspace_root(str(ws))
    return ws


class TestRecordLocalEvent:
    def test_appends_one_jsonl_line(self, workspace):
        from cli.lib.audit_spool import record_local_event

        record_local_event("query.local_offline", {"tables": ["orders"], "sql_hash": "abc123", "rows": 3})
        spool = workspace / ".claude" / "audit_spool.jsonl"
        assert spool.exists()
        lines = spool.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        event = json.loads(lines[0])
        assert event["action"] == "query.local_offline"
        assert event["params"]["tables"] == ["orders"]
        assert event["params"]["sql_hash"] == "abc123"
        assert "observed_at" in event

    def test_multiple_events_append(self, workspace):
        from cli.lib.audit_spool import record_local_event

        record_local_event("query.local_offline", {"rows": 1})
        record_local_event("explore.local_offline", {"rows": 2})
        spool = workspace / ".claude" / "audit_spool.jsonl"
        lines = spool.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2

    def test_noop_without_workspace_root(self, tmp_path, monkeypatch):
        """No workspace_root configured -> silently does nothing, never raises."""
        monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "config"))
        from cli.lib.audit_spool import record_local_event

        record_local_event("query.local_offline", {"rows": 1})  # must not raise
        assert not (tmp_path / "workspace" / ".claude" / "audit_spool.jsonl").exists()

    def test_never_writes_sql_text(self, workspace):
        """Contract check: the CLI callers only ever pass `sql_hash`, never a
        `sql` key — this test pins that nothing in this module itself adds
        one."""
        from cli.lib.audit_spool import record_local_event

        record_local_event("query.local_offline", {"sql_hash": "deadbeef", "tables": ["t"]})
        spool = workspace / ".claude" / "audit_spool.jsonl"
        raw = spool.read_text(encoding="utf-8")
        assert "SELECT" not in raw.upper()
        event = json.loads(raw.splitlines()[0])
        assert "sql" not in event["params"]


class TestDrainAndCommit:
    def test_drain_returns_without_removing(self, workspace):
        from cli.lib.audit_spool import drain_spool, record_local_event

        record_local_event("query.local_offline", {"rows": 1})
        events = drain_spool()
        assert len(events) == 1
        spool = workspace / ".claude" / "audit_spool.jsonl"
        # Still on disk — drain is non-destructive.
        assert len(spool.read_text(encoding="utf-8").splitlines()) == 1

    def test_commit_removes_exactly_the_drained_events(self, workspace):
        from cli.lib.audit_spool import commit_drain, drain_spool, record_local_event

        record_local_event("query.local_offline", {"rows": 1})
        record_local_event("query.local_offline", {"rows": 2})
        events = drain_spool()
        assert len(events) == 2
        commit_drain()
        spool = workspace / ".claude" / "audit_spool.jsonl"
        assert not spool.exists() or spool.read_text(encoding="utf-8").strip() == ""

    def test_commit_preserves_events_appended_after_drain(self, workspace):
        """A `record_local_event` call between `drain_spool()` and
        `commit_drain()` (e.g. a concurrent query) must survive the commit —
        it appended AFTER the drain snapshot."""
        from cli.lib.audit_spool import commit_drain, drain_spool, record_local_event

        record_local_event("query.local_offline", {"rows": 1})
        drained = drain_spool()
        assert len(drained) == 1
        record_local_event("query.local_offline", {"rows": 2})  # arrives after drain
        commit_drain()
        spool = workspace / ".claude" / "audit_spool.jsonl"
        remaining = spool.read_text(encoding="utf-8").splitlines()
        assert len(remaining) == 1
        assert json.loads(remaining[0])["params"]["rows"] == 2

    def test_commit_without_prior_drain_is_noop(self, workspace):
        from cli.lib.audit_spool import commit_drain, record_local_event

        record_local_event("query.local_offline", {"rows": 1})
        commit_drain()  # no preceding drain_spool() call
        spool = workspace / ".claude" / "audit_spool.jsonl"
        assert len(spool.read_text(encoding="utf-8").splitlines()) == 1

    def test_drain_empty_when_no_spool_file(self, workspace):
        from cli.lib.audit_spool import drain_spool

        assert drain_spool() == []

    def test_drain_respects_max_events(self, workspace):
        from cli.lib.audit_spool import drain_spool, record_local_event

        for i in range(10):
            record_local_event("query.local_offline", {"rows": i})
        events = drain_spool(max_events=3)
        assert len(events) == 3


# ---------------------------------------------------------------------------
# `agnes query` (local path) hooks into the spool
# ---------------------------------------------------------------------------


@pytest.fixture
def local_db(tmp_path, monkeypatch, workspace):
    """A local DuckDB with one table, wired up via AGNES_LOCAL_DIR (the
    `resolve_data_workspace()` anchor `_run_local` reads from) — independent
    of `workspace` (the `workspace_root` anchor the spool reads from)."""
    import duckdb

    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path / "local"))
    db_dir = tmp_path / "local" / "user" / "duckdb"
    db_dir.mkdir(parents=True)
    conn = duckdb.connect(str(db_dir / "analytics.duckdb"))
    conn.execute("CREATE TABLE nums (n INTEGER)")
    conn.execute("INSERT INTO nums VALUES (1), (2), (3)")
    conn.close()
    return workspace


class TestQueryLocalSpoolsEvent:
    def test_success_writes_query_local_offline_event(self, local_db):
        result = runner.invoke(app, ["query", "SELECT SUM(n) AS total FROM nums", "--scope", "local", "--json"])
        assert result.exit_code == 0, result.output

        spool = local_db / ".claude" / "audit_spool.jsonl"
        assert spool.exists()
        lines = spool.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        event = json.loads(lines[0])
        assert event["action"] == "query.local_offline"
        assert event["params"]["tables"] == ["nums"]
        assert event["params"]["rows"] == 1
        assert "duration_ms" in event["params"]
        assert "sql_hash" in event["params"]
        # NEVER the SQL text.
        assert "SELECT" not in json.dumps(event["params"]).upper()

    def test_error_path_also_writes_event(self, local_db):
        result = runner.invoke(app, ["query", "SELECT * FROM does_not_exist_table", "--scope", "local"])
        assert result.exit_code == 1

        spool = local_db / ".claude" / "audit_spool.jsonl"
        lines = spool.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        event = json.loads(lines[0])
        assert event["action"] == "query.local_offline"
        assert event["params"]["rows"] == 0


# ---------------------------------------------------------------------------
# `agnes explore` (local path) hooks into the spool
# ---------------------------------------------------------------------------


class TestExploreLocalSpoolsEvent:
    def test_success_writes_explore_local_offline_event(self, local_db):
        result = runner.invoke(app, ["explore", "--scope", "local", "--json", "nums"])
        assert result.exit_code == 0, result.output

        spool = local_db / ".claude" / "audit_spool.jsonl"
        lines = spool.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        event = json.loads(lines[0])
        assert event["action"] == "explore.local_offline"
        assert event["params"]["tables"] == ["nums"]
        assert event["params"]["rows"] == 3

    def test_table_miss_writes_event(self, local_db):
        result = runner.invoke(app, ["explore", "--scope", "local", "does_not_exist"])
        assert result.exit_code == 1

        spool = local_db / ".claude" / "audit_spool.jsonl"
        lines = spool.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        event = json.loads(lines[0])
        assert event["action"] == "explore.local_offline"
        assert event["params"]["rows"] == 0


# ---------------------------------------------------------------------------
# `agnes push` uploads the spool, best-effort
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code


class TestPushSpoolUpload:
    def _stub_push(self, monkeypatch, workspace):
        monkeypatch.setattr("cli.commands.push.get_server_url", lambda: "http://x")
        monkeypatch.setattr("cli.commands.push.get_token", lambda: "test-pat")
        monkeypatch.setattr("cli.commands.push.get_workspace_root", lambda: str(workspace))
        monkeypatch.setattr("cli.commands.push.api_get", lambda p, **kw: _FakeResp(200))
        monkeypatch.setattr("cli.commands.push.list_session_files", lambda _ws: [])

    def test_successful_upload_drains_spool(self, workspace, monkeypatch):
        from cli.commands.push import push_app
        from cli.lib.audit_spool import record_local_event

        self._stub_push(monkeypatch, workspace)
        record_local_event("query.local_offline", {"rows": 1})

        calls = []

        def _fake_post(endpoint, **kwargs):
            calls.append((endpoint, kwargs))
            return _FakeResp(200)

        monkeypatch.setattr("cli.commands.push.api_post", _fake_post)

        result = runner.invoke(push_app, ["--quiet"])
        assert result.exit_code == 0, result.output

        audit_calls = [c for c in calls if c[0] == "/api/upload/audit-events"]
        assert len(audit_calls) == 1
        assert len(audit_calls[0][1]["json"]["events"]) == 1

        spool = workspace / ".claude" / "audit_spool.jsonl"
        assert not spool.exists() or spool.read_text(encoding="utf-8").strip() == ""

    def test_upload_failure_keeps_spool_intact(self, workspace, monkeypatch):
        """A server outage (or any non-200) during the spool upload must
        NOT fail `agnes push`, and must leave the spool untouched for the
        next push to retry."""
        from cli.commands.push import push_app
        from cli.lib.audit_spool import record_local_event

        self._stub_push(monkeypatch, workspace)
        record_local_event("query.local_offline", {"rows": 1})

        def _fake_post_fails(endpoint, **kwargs):
            if endpoint == "/api/upload/audit-events":
                return _FakeResp(503)
            return _FakeResp(200)

        monkeypatch.setattr("cli.commands.push.api_post", _fake_post_fails)

        result = runner.invoke(push_app, ["--quiet"])
        assert result.exit_code == 0, result.output

        spool = workspace / ".claude" / "audit_spool.jsonl"
        assert spool.exists()
        assert len(spool.read_text(encoding="utf-8").splitlines()) == 1

    def test_upload_exception_keeps_spool_intact_and_push_still_succeeds(self, workspace, monkeypatch):
        """A network exception talking to /api/upload/audit-events must not
        propagate out of `agnes push` — same best-effort philosophy as
        every other upload in this command."""
        from cli.commands.push import push_app
        from cli.lib.audit_spool import record_local_event

        self._stub_push(monkeypatch, workspace)
        record_local_event("query.local_offline", {"rows": 1})

        def _fake_post_raises(endpoint, **kwargs):
            if endpoint == "/api/upload/audit-events":
                raise ConnectionError("server unreachable")
            return _FakeResp(200)

        monkeypatch.setattr("cli.commands.push.api_post", _fake_post_raises)

        result = runner.invoke(push_app, ["--quiet"])
        assert result.exit_code == 0, result.output

        spool = workspace / ".claude" / "audit_spool.jsonl"
        assert spool.exists()
        assert len(spool.read_text(encoding="utf-8").splitlines()) == 1

    def test_no_events_no_upload_call(self, workspace, monkeypatch):
        """An empty spool must not trigger an audit-events POST at all —
        keeps a no-op push from making an extra network round-trip."""
        from cli.commands.push import push_app

        self._stub_push(monkeypatch, workspace)

        calls = []

        def _fake_post(endpoint, **kwargs):
            calls.append(endpoint)
            return _FakeResp(200)

        monkeypatch.setattr("cli.commands.push.api_post", _fake_post)

        result = runner.invoke(push_app, ["--quiet"])
        assert result.exit_code == 0, result.output
        assert "/api/upload/audit-events" not in calls
