"""Task 11 -- the disclosure chain: `row_scope` across API / CLI / MCP / pull
(table access policies design doc §10; plan Task 11).

`policied_table_ids` is already threaded through `execute_query` and
`run_remote_select_to_arrow` (Task 7) and through the `table_id`-shaped
surfaces via `PoliciedRelation.policied` (Task 8). This is the FIRST test
that turns that plumbing into something a caller can actually see: the
`row_scope` envelope on `/api/query` and `/api/v2/sample`, the
`X-Agnes-Row-Scope` header on `/api/v2/scan` (which has no JSON body to
carry it), the CLI's `[scope]` stderr note, and the `.claude/rules/` entry
`agnes pull` writes so an agent has the caveat in context BEFORE it writes
a query against a policied table -- the only link in the chain that reaches
an agent before the fact rather than after.
"""

from __future__ import annotations

import contextlib
import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner as ClickCliRunner
from typer.main import get_command

from cli.main import app as _cli_app
from src.access_policy import row_scope_payload

POLICY_SQL = "SELECT * EXCLUDE (secret) FROM orders WHERE list_contains($user_groups, unit)"

_click_app = get_command(_cli_app)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── row_scope_payload: the shared envelope builder every surface below binds
# against, so the wording never drifts between REST/CLI/header. ────────────


class TestRowScopePayload:
    def test_empty_list_returns_none(self):
        assert row_scope_payload([]) is None

    def test_none_input_returns_none(self):
        assert row_scope_payload(None) is None

    def test_single_table_note_matches_the_disclosure_wording(self):
        payload = row_scope_payload(["orders"])
        assert payload == {
            "policied_tables": ["orders"],
            "note": ("rows in 'orders' are filtered by an access policy — this is your slice, not the whole table"),
        }

    def test_dedupes_preserving_first_seen_order(self):
        payload = row_scope_payload(["orders", "invoices", "orders"])
        assert payload["policied_tables"] == ["orders", "invoices"]


# ── cli.query_hints.row_scope_note / row_scope_note_from_header: the ONE
# wording source `agnes query`, `agnes describe` and `agnes snapshot
# create`/`refresh` all render through. ────────────────────────────────────


class TestRowScopeNoteHelper:
    _ROW_SCOPE = {
        "policied_tables": ["orders"],
        "note": "rows in 'orders' are filtered by an access policy — this is your slice, not the whole table",
    }

    def test_renders_the_scope_prefix_and_note(self):
        from cli.query_hints import row_scope_note

        assert row_scope_note(self._ROW_SCOPE) == f"[scope] {self._ROW_SCOPE['note']}"

    def test_none_input_returns_none(self):
        from cli.query_hints import row_scope_note

        assert row_scope_note(None) is None

    def test_non_dict_input_returns_none(self):
        from cli.query_hints import row_scope_note

        assert row_scope_note("not a dict") is None

    def test_dict_without_note_key_returns_none(self):
        from cli.query_hints import row_scope_note

        assert row_scope_note({"policied_tables": ["orders"]}) is None


class TestRowScopeNoteFromHeaderHelper:
    _HEADER_JSON = json.dumps(
        {
            "policied_tables": ["orders"],
            "note": "rows in 'orders' are filtered by an access policy — this is your slice, not the whole table",
        }
    )

    def test_parses_the_json_header_and_renders_the_note(self):
        from cli.query_hints import row_scope_note_from_header

        assert row_scope_note_from_header(self._HEADER_JSON) == (
            "[scope] rows in 'orders' are filtered by an access policy — this is your slice, not the whole table"
        )

    def test_missing_header_returns_none(self):
        from cli.query_hints import row_scope_note_from_header

        assert row_scope_note_from_header(None) is None
        assert row_scope_note_from_header("") is None

    def test_malformed_json_returns_none_not_raises(self):
        from cli.query_hints import row_scope_note_from_header

        assert row_scope_note_from_header("{not json") is None

    def test_valid_json_but_wrong_shape_returns_none_not_raises(self):
        from cli.query_hints import row_scope_note_from_header

        assert row_scope_note_from_header("[1, 2, 3]") is None
        assert row_scope_note_from_header('"just a string"') is None


# ── shared fixture: one policied table + one untouched sibling ─────────────


@pytest.fixture
def policied_orders(seeded_app, mock_extract_factory, monkeypatch):
    """A ``server_only`` ``orders`` table carrying ``POLICY_SQL``, plus a
    sibling ``line_items`` table with no policy attached -- both granted to a
    non-admin user, plus the seeded admin. Mirrors the fixture in
    ``tests/test_access_policy_table_id_surfaces.py``."""
    from app.auth.jwt import create_access_token
    from src.db import get_system_db
    from src.orchestrator import SyncOrchestrator
    from src.repositories.table_registry import TableRegistryRepository
    from src.repositories.users import UserRepository
    from tests.conftest import grant_table_via_package

    monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "true")

    env = seeded_app["env"]
    mock_extract_factory(
        "keboola",
        [
            {
                "name": "orders",
                "data": [
                    {"id": "1", "unit": "TeamA", "secret": "s1", "amount": "100"},
                    {"id": "2", "unit": "TeamA", "secret": "s2", "amount": "150"},
                    {"id": "3", "unit": "TeamB", "secret": "s3", "amount": "300"},
                ],
            },
            {
                "name": "line_items",
                "data": [{"id": "1", "sku": "A1"}],
            },
        ],
    )
    SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

    conn = get_system_db()
    try:
        registry = TableRegistryRepository(conn)
        registry.register(
            id="orders",
            name="orders",
            source_type="keboola",
            query_mode="local",
            server_only=True,
        )
        registry.set_access_policy("orders", sql=POLICY_SQL, note="unit filter", updated_by="admin")

        # No access_policy_sql on this one -- the inert-case control.
        registry.register(
            id="line_items",
            name="line_items",
            source_type="keboola",
            query_mode="local",
        )

        users = UserRepository(conn)
        users.create(id="u_team_a", email="team-a@example.com", name="Team A")

        grant_table_via_package(conn, "orders", "u_team_a", group_name="TeamA")
        grant_table_via_package(conn, "line_items", "u_team_a", group_name="TeamA")
    finally:
        conn.close()

    return {
        **seeded_app,
        "team_a_token": create_access_token("u_team_a", "team-a@example.com"),
    }


# ── /api/query ────────────────────────────────────────────────────────────


class TestQueryEndpointRowScope:
    def test_row_scope_present_and_non_null_for_policied_table(self, policied_orders):
        c = policied_orders["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT * FROM orders"},
            headers=_auth(policied_orders["team_a_token"]),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["row_scope"] is not None
        assert body["row_scope"]["policied_tables"] == ["orders"]
        assert "orders" in body["row_scope"]["note"]

    def test_row_scope_is_null_for_non_policied_table(self, policied_orders):
        c = policied_orders["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT * FROM line_items"},
            headers=_auth(policied_orders["team_a_token"]),
        )
        assert r.status_code == 200, r.text
        assert r.json()["row_scope"] is None

    def test_row_scope_is_null_for_admin_bypass(self, policied_orders):
        """The admin credential surface reads the raw table (§12) -- nothing
        was narrowed, so there is nothing to disclose."""
        c = policied_orders["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT * FROM orders"},
            headers=_auth(policied_orders["admin_token"]),
        )
        assert r.status_code == 200, r.text
        assert r.json()["row_scope"] is None


class _FakeQuota:
    """Minimal quota double for direct ``run_remote_select_to_arrow`` calls
    (mirrors ``tests/test_access_policy_query_endpoint.py``)."""

    def check_daily_budget(self, user=None):
        pass

    def acquire(self, user=None):
        return contextlib.nullcontext()

    def record_bytes(self, user=None, n=0):
        pass


class TestRunRemoteSelectToArrowPolicyInfo:
    """``run_remote_select_to_arrow`` has no response envelope of its own (a
    bare ``pyarrow.Table``) -- it reports ``policied_table_ids`` back through
    an optional ``policy_info`` out-param instead, the same pattern
    ``_run_bq_scan`` already uses for BQ job metadata via ``job_info``
    (``app/api/v2_scan.py``). ``/api/v2/scan``'s ``from_query`` branch is the
    consumer that turns this into the ``X-Agnes-Row-Scope`` header."""

    @staticmethod
    def _call(user: dict, sql: str, **kwargs):
        from src.db import get_system_db

        from app.api.query import run_remote_select_to_arrow

        conn = get_system_db()
        try:
            return run_remote_select_to_arrow(conn, user, sql, bq=None, quota=_FakeQuota(), **kwargs)
        finally:
            conn.close()

    def test_policy_info_carries_the_policied_table_id(self, policied_orders):
        policy_info: dict = {}
        self._call(
            {"id": "u_team_a", "email": "team-a@example.com"},
            "SELECT * FROM orders",
            policy_info=policy_info,
        )
        assert policy_info["policied_table_ids"] == ["orders"]

    def test_policy_info_empty_for_a_non_policied_table(self, policied_orders):
        policy_info: dict = {}
        self._call(
            {"id": "u_team_a", "email": "team-a@example.com"},
            "SELECT * FROM line_items",
            policy_info=policy_info,
        )
        assert policy_info.get("policied_table_ids") == []

    def test_a_caller_that_omits_policy_info_is_unaffected(self, policied_orders):
        """Existing callers that don't pass ``policy_info`` at all (pre-Task-11
        call sites) must keep working exactly as before."""
        result = self._call(
            {"id": "u_team_a", "email": "team-a@example.com"},
            "SELECT * FROM orders",
        )
        rows = result.read_all() if hasattr(result, "read_all") else result
        assert rows.num_rows == 2


# ── /api/v2/sample ───────────────────────────────────────────────────────


class TestV2SampleRowScope:
    def test_row_scope_present_for_policied_table(self, policied_orders):
        c = policied_orders["client"]
        r = c.get("/api/v2/sample/orders?n=10", headers=_auth(policied_orders["team_a_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["row_scope"] is not None
        assert body["row_scope"]["policied_tables"] == ["orders"]

    def test_row_scope_absent_for_non_policied_table(self, policied_orders):
        c = policied_orders["client"]
        r = c.get("/api/v2/sample/line_items?n=10", headers=_auth(policied_orders["team_a_token"]))
        assert r.status_code == 200, r.text
        assert r.json().get("row_scope") is None

    def test_row_scope_absent_for_admin_bypass(self, policied_orders):
        c = policied_orders["client"]
        r = c.get("/api/v2/sample/orders?n=10", headers=_auth(policied_orders["admin_token"]))
        assert r.status_code == 200, r.text
        assert r.json().get("row_scope") is None


# ── /api/v2/scan (Arrow -- header, no JSON body to carry the field) ────────


class TestV2ScanRowScopeHeader:
    def test_header_present_for_policied_table(self, policied_orders):
        c = policied_orders["client"]
        r = c.post(
            "/api/v2/scan",
            json={"table_id": "orders"},
            headers=_auth(policied_orders["team_a_token"]),
        )
        assert r.status_code == 200, r.text
        header = r.headers.get("x-agnes-row-scope")
        assert header is not None
        payload = json.loads(header)
        assert payload["policied_tables"] == ["orders"]

    def test_header_absent_for_non_policied_table(self, policied_orders):
        c = policied_orders["client"]
        r = c.post(
            "/api/v2/scan",
            json={"table_id": "line_items"},
            headers=_auth(policied_orders["team_a_token"]),
        )
        assert r.status_code == 200, r.text
        assert "x-agnes-row-scope" not in r.headers

    def test_header_absent_for_admin_bypass(self, policied_orders):
        c = policied_orders["client"]
        r = c.post(
            "/api/v2/scan",
            json={"table_id": "orders"},
            headers=_auth(policied_orders["admin_token"]),
        )
        assert r.status_code == 200, r.text
        assert "x-agnes-row-scope" not in r.headers


# ── POST /api/mcp/query-table/{id} (N1, RLS review #1979) ──────────────────
# `app/api/mcp_per_table.py`'s `query_table` filters correctly through
# `policied_relation` but, before this test, never attached the same
# `row_scope` envelope every other read surface above carries -- an MCP
# client reading a policied table through this "fast path" had no way to
# know it got a slice. Mirrors `TestV2SampleRowScope`'s shape exactly.


class TestMcpQueryTableRowScope:
    def test_row_scope_present_for_policied_table(self, policied_orders):
        c = policied_orders["client"]
        r = c.post(
            "/api/mcp/query-table/orders",
            json={"filter": {}, "limit": 10},
            headers=_auth(policied_orders["team_a_token"]),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["row_scope"] is not None
        assert body["row_scope"]["policied_tables"] == ["orders"]
        assert "orders" in body["row_scope"]["note"]

    def test_row_scope_absent_for_non_policied_table(self, policied_orders):
        c = policied_orders["client"]
        r = c.post(
            "/api/mcp/query-table/line_items",
            json={"filter": {}, "limit": 10},
            headers=_auth(policied_orders["team_a_token"]),
        )
        assert r.status_code == 200, r.text
        assert r.json().get("row_scope") is None

    def test_row_scope_absent_for_admin_bypass(self, policied_orders):
        c = policied_orders["client"]
        r = c.post(
            "/api/mcp/query-table/orders",
            json={"filter": {}, "limit": 10},
            headers=_auth(policied_orders["admin_token"]),
        )
        assert r.status_code == 200, r.text
        assert r.json().get("row_scope") is None


# ── manifest carries the policied flag (consumed by `agnes pull` below) ────


class TestManifestAccessPolicyMarker:
    def test_data_package_table_entry_carries_access_policy_flag(self, policied_orders):
        c = policied_orders["client"]
        r = c.get("/api/sync/manifest", headers=_auth(policied_orders["team_a_token"]))
        assert r.status_code == 200, r.text
        packages = r.json()["data_packages"]
        entries = {t["id"]: t for pkg in packages for t in pkg["tables"]}
        assert entries["orders"]["access_policy"] is True
        assert entries["line_items"]["access_policy"] is False


# ── CLI: `[scope]` note on stderr, `--format json` stdout stays clean ──────


class TestCliQueryRowScopeStderr:
    @pytest.fixture(autouse=True)
    def _tmp_config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path / "local"))
        (tmp_path / "config").mkdir()
        (tmp_path / "local").mkdir()

    @staticmethod
    def _resp(status_code=200, json_data=None):
        r = MagicMock()
        r.status_code = status_code
        r.json.return_value = json_data if json_data is not None else {}
        return r

    _POLICIED_PAYLOAD = {
        "columns": ["id"],
        "rows": [[1]],
        "truncated": False,
        "row_scope": {
            "policied_tables": ["orders"],
            "note": "rows in 'orders' are filtered by an access policy — this is your slice, not the whole table",
        },
    }

    def test_scope_note_printed_to_stderr(self):
        with patch("cli.client.api_post", return_value=self._resp(200, self._POLICIED_PAYLOAD)):
            result = ClickCliRunner().invoke(_click_app, ["query", "SELECT * FROM orders", "--remote"])
        assert result.exit_code == 0
        assert "[scope]" in result.stderr
        assert "rows in 'orders' are filtered by an access policy" in result.stderr

    def test_no_scope_note_when_row_scope_absent(self):
        payload = {"columns": ["id"], "rows": [[1]], "truncated": False, "row_scope": None}
        with patch("cli.client.api_post", return_value=self._resp(200, payload)):
            result = ClickCliRunner().invoke(_click_app, ["query", "SELECT * FROM t", "--remote"])
        assert result.exit_code == 0
        assert "[scope]" not in result.stderr

    def test_scope_note_on_stderr_keeps_json_stdout_pure(self):
        with patch("cli.client.api_post", return_value=self._resp(200, self._POLICIED_PAYLOAD)):
            result = ClickCliRunner().invoke(
                _click_app, ["query", "SELECT * FROM orders", "--remote", "--format", "json"]
            )
        assert result.exit_code == 0
        parsed = json.loads(result.stdout.strip())
        assert parsed == [{"id": 1}]
        assert "[scope]" not in result.stdout
        assert "[scope]" in result.stderr


class TestCliDescribeRowScopeStderr:
    """`agnes describe`'s human render drops `sample.row_scope` entirely --
    `--json` dumps the full payload (row_scope included) but a human never
    sees the caveat. Mirrors `TestCliQueryRowScopeStderr` above."""

    @pytest.fixture(autouse=True)
    def _tmp_config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
        (tmp_path / "config").mkdir()
        (tmp_path / "data").mkdir()

    _SCHEMA = {
        "table_id": "orders",
        "columns": [{"name": "id", "type": "INTEGER"}],
    }
    _SAMPLE_WITH_SCOPE = {
        "table_id": "orders",
        "rows": [{"id": 1}],
        "row_scope": {
            "policied_tables": ["orders"],
            "note": "rows in 'orders' are filtered by an access policy — this is your slice, not the whole table",
        },
    }
    _SAMPLE_NO_SCOPE = {"table_id": "orders", "rows": [{"id": 1}]}

    @staticmethod
    def _fake_get(sample_payload):
        def _get(path, **kwargs):
            return TestCliDescribeRowScopeStderr._SCHEMA if "schema" in path else sample_payload

        return _get

    def test_scope_note_printed_when_row_scope_present(self):
        with patch("cli.commands.describe.api_get_json", side_effect=self._fake_get(self._SAMPLE_WITH_SCOPE)):
            result = ClickCliRunner().invoke(_click_app, ["describe", "orders"])
        assert result.exit_code == 0
        assert "[scope]" in result.stderr
        assert "rows in 'orders' are filtered by an access policy" in result.stderr

    def test_no_scope_note_when_row_scope_absent(self):
        with patch("cli.commands.describe.api_get_json", side_effect=self._fake_get(self._SAMPLE_NO_SCOPE)):
            result = ClickCliRunner().invoke(_click_app, ["describe", "orders"])
        assert result.exit_code == 0
        assert "[scope]" not in result.stderr

    def test_scope_note_on_stderr_keeps_json_stdout_pure(self):
        with patch("cli.commands.describe.api_get_json", side_effect=self._fake_get(self._SAMPLE_WITH_SCOPE)):
            result = ClickCliRunner().invoke(_click_app, ["describe", "orders", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["sample"]["row_scope"]["policied_tables"] == ["orders"]
        assert "[scope]" not in result.stdout
        assert "[scope]" in result.stderr


# ── agnes pull: `.claude/rules/access_policies.md` ──────────────────────────
#
# Deliberately NOT named `ka_<x>.md` / `km_<x>.md` -- those two namespaces
# are each swept by their OWN owner's prune loop on every pull
# (`_sync_knowledge_digests` deletes any `ka_*.md` not one of ITS digest
# slugs the moment the manifest carries a `knowledge_artifacts` key --
# `tests/test_lib_pull_digests.py::test_prunes_on_deauthorization` pins
# exactly this -- and `_fetch_and_write_rules` does the same over `km_*.md`).
# A same-prefix file would be deleted out from under this feature by an
# unrelated pull step.


class TestPullWritesAccessPolicyRules:
    @pytest.fixture(autouse=True)
    def _isolate_config_dir(self, tmp_path, monkeypatch):
        cfg = tmp_path / "_agnes_cfg"
        cfg.mkdir()
        monkeypatch.setenv("AGNES_CONFIG_DIR", str(cfg))

    @staticmethod
    def _manifest(tables_payload):
        if not tables_payload:
            return {"tables": {}}
        return {
            "tables": {},
            "data_packages": [
                {
                    "id": "pkg_1",
                    "slug": "pkg-1",
                    "name": "Pkg",
                    "tables": tables_payload,
                    "total_size_bytes": 0,
                }
            ],
        }

    @staticmethod
    def _stub_server(monkeypatch, manifest):
        def _api_get(path, *args, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status = lambda: None
            if path == "/api/sync/manifest":
                resp.json.return_value = manifest
            elif path == "/api/memory/bundle":
                resp.json.return_value = {"mandatory": [], "approved": []}
            else:
                resp.json.return_value = {}
            return resp

        def _stream_download(path, target_path, progress_callback=None):
            raise AssertionError(f"unexpected stream_download({path!r}) in a disclosure-only pull test")

        monkeypatch.setattr("cli.lib.pull.api_get", _api_get, raising=False)
        monkeypatch.setattr("cli.lib.pull.stream_download", _stream_download, raising=False)

    def test_writes_rules_file_naming_the_policied_table(self, tmp_path, monkeypatch):
        from cli.lib.pull import run_pull

        manifest = self._manifest(
            [
                {"id": "orders", "name": "orders", "access_policy": True, "hash": "", "query_mode": "local"},
                {"id": "line_items", "name": "line_items", "access_policy": False, "hash": "", "query_mode": "local"},
            ]
        )
        self._stub_server(monkeypatch, manifest)

        result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

        target = tmp_path / ".claude" / "rules" / "access_policies.md"
        assert target.exists()
        text = target.read_text()
        assert "orders" in text
        assert "line_items" not in text
        assert result.access_policy_tables == 1

    def test_no_policied_tables_writes_nothing(self, tmp_path, monkeypatch):
        from cli.lib.pull import run_pull

        manifest = self._manifest(
            [{"id": "line_items", "name": "line_items", "access_policy": False, "hash": "", "query_mode": "local"}]
        )
        self._stub_server(monkeypatch, manifest)

        run_pull(server_url="http://x", token="t", workspace=tmp_path)

        assert not (tmp_path / ".claude" / "rules" / "access_policies.md").exists()

    def test_missing_data_packages_key_is_a_no_op(self, tmp_path, monkeypatch):
        """Pre-this-feature server: no `data_packages` key at all -- must not
        crash `run_pull`, and must leave the workspace untouched."""
        from cli.lib.pull import run_pull

        manifest = self._manifest([])
        self._stub_server(monkeypatch, manifest)

        result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

        assert not (tmp_path / ".claude" / "rules" / "access_policies.md").exists()
        assert result.access_policy_tables == 0

    def test_table_dropping_off_the_stack_prunes_the_file(self, tmp_path, monkeypatch):
        from cli.lib.pull import run_pull

        with_policy = self._manifest(
            [{"id": "orders", "name": "orders", "access_policy": True, "hash": "", "query_mode": "local"}]
        )
        self._stub_server(monkeypatch, with_policy)
        run_pull(server_url="http://x", token="t", workspace=tmp_path)
        target = tmp_path / ".claude" / "rules" / "access_policies.md"
        assert target.exists()

        without_policy = self._manifest([])
        self._stub_server(monkeypatch, without_policy)
        run_pull(server_url="http://x", token="t", workspace=tmp_path)
        assert not target.exists()
