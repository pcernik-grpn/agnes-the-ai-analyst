"""Tests for agnes admin subcommands."""

import json
import pytest
from unittest.mock import patch, MagicMock

from typer.testing import CliRunner
from cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "config").mkdir()
    (tmp_path / "data").mkdir()
    yield tmp_path


def _resp(status_code=200, json_data=None, text=""):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data if json_data is not None else {}
    r.text = text
    return r


class TestListUsers:
    def test_list_users_text(self):
        users = [
            {"email": "alice@x.com", "role": "admin", "id": "aaa00001"},
            {"email": "bob@x.com", "role": "analyst", "id": "bbb00002"},
        ]
        with patch("cli.commands.admin.api_get", return_value=_resp(200, users)):
            result = runner.invoke(app, ["admin", "list-users"])
        assert result.exit_code == 0
        assert "alice@x.com" in result.output
        assert "bob@x.com" in result.output

    def test_list_users_json(self):
        users = [{"email": "alice@x.com", "role": "admin", "id": "aaa00001"}]
        with patch("cli.commands.admin.api_get", return_value=_resp(200, users)):
            result = runner.invoke(app, ["admin", "list-users", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data[0]["email"] == "alice@x.com"

    def test_list_users_api_error(self):
        with patch("cli.commands.admin.api_get", return_value=_resp(500, {"detail": "Server error"}, "Server error")):
            result = runner.invoke(app, ["admin", "list-users"])
        assert result.exit_code == 1


class TestAddUser:
    def test_add_user_success(self):
        created = {"email": "newuser@x.com", "id": "uid-1"}
        with patch("cli.commands.admin.api_post", return_value=_resp(201, created)):
            result = runner.invoke(app, ["admin", "add-user", "newuser@x.com"])
        assert result.exit_code == 0
        assert "newuser@x.com" in result.output

    def test_add_user_failure(self):
        with patch("cli.commands.admin.api_post", return_value=_resp(400, {"detail": "Already exists"})):
            result = runner.invoke(app, ["admin", "add-user", "dup@x.com"])
        assert result.exit_code == 1


class TestRemoveUser:
    def test_remove_user_success(self):
        with patch("cli.commands.admin.api_delete", return_value=_resp(204)):
            result = runner.invoke(app, ["admin", "remove-user", "uid-1"])
        assert result.exit_code == 0
        assert "removed" in result.output.lower()

    def test_remove_user_not_found(self):
        with patch("cli.commands.admin.api_delete", return_value=_resp(404, text="Not found")):
            result = runner.invoke(app, ["admin", "remove-user", "nonexistent"])
        assert result.exit_code == 1


class TestRegisterTable:
    def test_register_table_success(self):
        with patch("cli.commands.admin.api_post", return_value=_resp(201, {"id": "t1", "name": "orders"})):
            result = runner.invoke(
                app,
                [
                    "admin",
                    "register-table",
                    "orders",
                    "--source-type",
                    "keboola",
                    "--bucket",
                    "in.c-crm",
                    "--query-mode",
                    "local",
                ],
            )
        assert result.exit_code == 0
        assert "Registered: orders" in result.output

    def test_register_table_already_exists(self):
        with patch("cli.commands.admin.api_post", return_value=_resp(409, {"detail": "exists"})):
            result = runner.invoke(app, ["admin", "register-table", "orders"])
        assert result.exit_code == 0
        assert "Already exists" in result.output

    def test_register_table_failure(self):
        with patch("cli.commands.admin.api_post", return_value=_resp(500, {"detail": "error"})):
            result = runner.invoke(app, ["admin", "register-table", "bad_table"])
        assert result.exit_code == 1


class TestListTables:
    def test_list_tables_text(self):
        payload = {
            "count": 2,
            "tables": [
                {"name": "orders", "source_type": "keboola", "query_mode": "local", "bucket": "in.c-crm"},
                {"name": "customers", "source_type": "keboola", "query_mode": "local", "bucket": "in.c-crm"},
            ],
        }
        with patch("cli.commands.admin.api_get", return_value=_resp(200, payload)):
            result = runner.invoke(app, ["admin", "list-tables"])
        assert result.exit_code == 0
        assert "Registered tables: 2" in result.output
        assert "orders" in result.output

    def test_list_tables_json(self):
        payload = {"count": 1, "tables": [{"name": "orders"}]}
        with patch("cli.commands.admin.api_get", return_value=_resp(200, payload)):
            result = runner.invoke(app, ["admin", "list-tables", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["count"] == 1

    def test_list_tables_survives_null_columns(self):
        """A NULL `bucket` (or source_type / query_mode) must not crash the
        listing.

        `bucket` is nullable in `table_registry` — every non-Keboola row has
        none — and the row formatter passed the value straight into a `:20s`
        format spec, so `None` raised `TypeError: unsupported format string
        passed to NoneType.__format__` and took the whole command down after
        printing the header. `.get(key, default)` does not help: the key IS
        present, its value is null.
        """
        payload = {
            "count": 2,
            "tables": [
                {"name": "orders", "source_type": "keboola", "query_mode": "local", "bucket": None},
                {"name": "events", "source_type": None, "query_mode": None, "bucket": None},
            ],
        }
        with patch("cli.commands.admin.api_get", return_value=_resp(200, payload)):
            result = runner.invoke(app, ["admin", "list-tables"])
        assert result.exit_code == 0, result.output
        assert "orders" in result.output
        # The row after the null-bucket one still gets printed.
        assert "events" in result.output

    def test_list_tables_text_surfaces_sync_status_and_reason(self):
        """#754: `agnes admin list-tables` must surface WHY a table shows
        0 rows synced — the per-row line includes the sync status and,
        when the table errored or was skipped, the persisted reason."""
        payload = {
            "count": 2,
            "tables": [
                {
                    "name": "orders",
                    "source_type": "keboola",
                    "query_mode": "local",
                    "bucket": "in.c-crm",
                    "last_sync_status": "error",
                    "last_sync_error": "connection refused",
                },
                {
                    "name": "customers",
                    "source_type": "keboola",
                    "query_mode": "local",
                    "bucket": "in.c-crm",
                    "last_sync_status": "ok",
                    "last_sync_error": None,
                },
            ],
        }
        with patch("cli.commands.admin.api_get", return_value=_resp(200, payload)):
            result = runner.invoke(app, ["admin", "list-tables"])
        assert result.exit_code == 0
        assert "error" in result.output
        assert "connection refused" in result.output
        assert "ok" in result.output


class TestMetadataShow:
    def test_metadata_show_columns(self):
        payload = {
            "columns": [
                {"column_name": "id", "basetype": "INTEGER", "confidence": "high", "description": "PK"},
                {"column_name": "name", "basetype": "VARCHAR", "confidence": "high", "description": ""},
            ]
        }
        with patch("cli.commands.admin.api_get", return_value=_resp(200, payload)):
            result = runner.invoke(app, ["admin", "metadata-show", "orders"])
        assert result.exit_code == 0
        assert "id" in result.output
        assert "name" in result.output

    def test_metadata_show_json(self):
        payload = {"columns": [{"column_name": "id", "basetype": "INTEGER"}]}
        with patch("cli.commands.admin.api_get", return_value=_resp(200, payload)):
            result = runner.invoke(app, ["admin", "metadata-show", "orders", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "columns" in data

    def test_metadata_show_not_found(self):
        with patch("cli.commands.admin.api_get", return_value=_resp(404, {"detail": "Not found"})):
            result = runner.invoke(app, ["admin", "metadata-show", "nonexistent"])
        assert result.exit_code == 1


class TestUnregisterTable:
    """Issue #177: `agnes admin unregister-table` wraps DELETE
    /api/admin/registry/{id}. The server endpoint already does the
    parquet/sync_state cleanup; the CLI is a thin client."""

    def test_unregister_success(self):
        with patch("cli.commands.admin.api_delete", return_value=_resp(204)):
            result = runner.invoke(app, ["admin", "unregister-table", "orders", "--yes"])
        assert result.exit_code == 0, result.output
        assert "Unregistered: orders" in result.output

    def test_unregister_not_found(self):
        with patch(
            "cli.commands.admin.api_delete",
            return_value=_resp(404, {"detail": "Table not found"}),
        ):
            result = runner.invoke(app, ["admin", "unregister-table", "nope", "--yes"])
        assert result.exit_code == 1

    def test_unregister_prompts_without_yes(self):
        """Without --yes, the CLI confirms before destructive action."""
        with patch("cli.commands.admin.api_delete", return_value=_resp(204)) as d:
            # Simulate operator typing "n" at the prompt.
            result = runner.invoke(app, ["admin", "unregister-table", "orders"], input="n\n")
        # Either Aborted (exit 0) or refuses entirely; either way the
        # server must not have been called.
        d.assert_not_called()
        assert result.exit_code == 0


class TestUpdateTable:
    """Issue #177: `agnes admin update-table` wraps PUT
    /api/admin/registry/{id}. Only fields the operator passes go in the
    body — server-side merge keeps the rest unchanged."""

    def test_update_only_supplied_fields_sent(self):
        captured = {}

        def fake_put(path, **kwargs):
            captured["path"] = path
            captured["json"] = kwargs.get("json")
            return _resp(200, {"id": "orders", "updated": ["bucket"]})

        with patch("cli.commands.admin.api_put", side_effect=fake_put):
            result = runner.invoke(app, ["admin", "update-table", "orders", "--bucket", "out.c-prod"])
        assert result.exit_code == 0, result.output
        assert captured["path"] == "/api/admin/registry/orders"
        # description must NOT be in the body — operator didn't pass it.
        assert captured["json"] == {"bucket": "out.c-prod"}
        assert "Updated orders" in result.output

    def test_update_inline_query_for_materialized(self):
        captured = {}

        def fake_put(path, **kwargs):
            captured["json"] = kwargs.get("json")
            return _resp(200, {"id": "rev", "updated": ["query_mode", "source_query"]})

        with patch("cli.commands.admin.api_put", side_effect=fake_put):
            result = runner.invoke(
                app,
                [
                    "admin",
                    "update-table",
                    "rev",
                    "--query-mode",
                    "materialized",
                    "--query",
                    "SELECT 1",
                ],
            )
        assert result.exit_code == 0, result.output
        assert captured["json"]["query_mode"] == "materialized"
        assert captured["json"]["source_query"] == "SELECT 1"

    def test_update_query_at_file(self, tmp_path):
        sql_file = tmp_path / "q.sql"
        sql_file.write_text("SELECT * FROM orders\n")
        captured = {}

        def fake_put(path, **kwargs):
            captured["json"] = kwargs.get("json")
            return _resp(200, {"id": "rev", "updated": ["source_query"]})

        with patch("cli.commands.admin.api_put", side_effect=fake_put):
            result = runner.invoke(app, ["admin", "update-table", "rev", "--query", f"@{sql_file}"])
        assert result.exit_code == 0, result.output
        assert captured["json"]["source_query"] == "SELECT * FROM orders"

    def test_update_no_fields_supplied_errors(self):
        result = runner.invoke(app, ["admin", "update-table", "orders"])
        assert result.exit_code == 2
        assert "No fields supplied" in (result.output + (result.stderr or ""))

    def test_update_table_not_found(self):
        with patch(
            "cli.commands.admin.api_put",
            return_value=_resp(404, {"detail": "Table not found"}),
        ):
            result = runner.invoke(app, ["admin", "update-table", "nope", "--bucket", "x"])
        assert result.exit_code == 1


class TestRegisterTableHints:
    """The CLI prints helpful follow-up hints after a successful
    register-table call. v0.46 adds a third hint for query_mode=remote
    pointing at the IAM verify-your-SA smoke check."""

    def test_remote_register_emits_iam_verify_hint(self):
        with patch("cli.commands.admin.api_post", return_value=_resp(201, {"id": "t"})):
            result = runner.invoke(
                app,
                [
                    "admin",
                    "register-table",
                    "orders",
                    "--source-type",
                    "bigquery",
                    "--bucket",
                    "dwh_base",
                    "--source-table",
                    "orders",
                    "--query-mode",
                    "remote",
                ],
            )
        assert result.exit_code == 0
        assert "agnes query --remote" in result.output
        assert "query-modes.md" in result.output

    def test_local_register_does_not_emit_remote_hint(self):
        with patch("cli.commands.admin.api_post", return_value=_resp(201, {"id": "t"})):
            result = runner.invoke(
                app,
                [
                    "admin",
                    "register-table",
                    "users",
                    "--source-type",
                    "keboola",
                    "--bucket",
                    "in.c-crm",
                    "--source-table",
                    "users",
                    "--query-mode",
                    "local",
                ],
            )
        assert result.exit_code == 0
        assert "agnes query --remote" not in result.output


def test_admin_set_role_returns_hardfail():
    """v19: `agnes admin set-role` was removed. Calling it must hard-fail
    with a non-zero exit code and a message pointing at the replacement
    (group memberships)."""
    from cli.commands.admin import admin_app
    from typer.testing import CliRunner

    runner = CliRunner()
    result = runner.invoke(admin_app, ["set-role", "abc", "admin"])
    assert result.exit_code == 2
    out = (result.stderr or "") + (result.output or "")
    assert "removed" in out.lower()
    assert "group" in out.lower()


class TestMetadataApply:
    def test_metadata_apply_dry_run(self, tmp_path):
        proposal = {
            "tables": {
                "orders": {
                    "columns": {
                        "id": {"basetype": "INTEGER", "description": "Primary key"},
                    }
                }
            }
        }
        proposal_file = tmp_path / "proposal.json"
        proposal_file.write_text(json.dumps(proposal))
        result = runner.invoke(app, ["admin", "metadata-apply", str(proposal_file), "--dry-run"])
        assert result.exit_code == 0
        assert "DRY RUN" in result.output
        assert "orders.id" in result.output

    def test_metadata_apply_file_not_found(self):
        result = runner.invoke(app, ["admin", "metadata-apply", "/nonexistent/proposal.json"])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()


class TestResolveGrantId:
    """Pin the grant_list short_id -> grant_delete workflow contract.

    Operators read the 8-char ``short_id`` column from ``agnes admin grant list``
    and pass it to ``agnes admin grant delete``. The CLI resolves the prefix
    to a full UUID before hitting the API so the workflow doesn't 404.
    """

    def test_grant_delete_accepts_full_uuid(self):
        grants = [{"id": "aaaa1111-bbbb-cccc-dddd-eeeeeeeeeeee"}]
        with (
            patch("cli.commands.admin.api_get", return_value=_resp(200, grants)),
            patch("cli.commands.admin.api_delete", return_value=_resp(204)) as mock_del,
        ):
            result = runner.invoke(
                app,
                ["admin", "grant", "delete", "aaaa1111-bbbb-cccc-dddd-eeeeeeeeeeee"],
            )
        assert result.exit_code == 0, result.output
        mock_del.assert_called_once_with("/api/admin/grants/aaaa1111-bbbb-cccc-dddd-eeeeeeeeeeee")

    def test_grant_delete_accepts_8char_prefix(self):
        """Bug repro: pre-fix this would 404."""
        grants = [{"id": "aaaa1111-bbbb-cccc-dddd-eeeeeeeeeeee"}]
        with (
            patch("cli.commands.admin.api_get", return_value=_resp(200, grants)),
            patch("cli.commands.admin.api_delete", return_value=_resp(204)) as mock_del,
        ):
            result = runner.invoke(app, ["admin", "grant", "delete", "aaaa1111"])
        assert result.exit_code == 0, result.output
        # API received the FULL uuid, not the prefix
        mock_del.assert_called_once_with("/api/admin/grants/aaaa1111-bbbb-cccc-dddd-eeeeeeeeeeee")

    def test_grant_delete_ambiguous_prefix_aborts(self):
        grants = [
            {"id": "aaaa1111-bbbb-cccc-dddd-eeeeeeeeeeee"},
            {"id": "aaaa1111-zzzz-yyyy-xxxx-wwwwwwwwwwww"},  # same 8-char prefix
        ]
        with (
            patch("cli.commands.admin.api_get", return_value=_resp(200, grants)),
            patch("cli.commands.admin.api_delete") as mock_del,
        ):
            result = runner.invoke(app, ["admin", "grant", "delete", "aaaa1111"])
        assert result.exit_code == 1, result.output
        assert "Ambiguous" in result.output or "ambiguous" in result.output
        mock_del.assert_not_called()

    def test_grant_delete_unknown_ref_aborts(self):
        grants = [{"id": "aaaa1111-bbbb-cccc-dddd-eeeeeeeeeeee"}]
        with (
            patch("cli.commands.admin.api_get", return_value=_resp(200, grants)),
            patch("cli.commands.admin.api_delete") as mock_del,
        ):
            result = runner.invoke(app, ["admin", "grant", "delete", "deadbeef"])
        assert result.exit_code == 1, result.output
        assert "not found" in result.output.lower()
        mock_del.assert_not_called()
