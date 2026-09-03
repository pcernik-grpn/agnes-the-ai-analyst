"""CLI tests for `agnes admin sharepoint` subcommands."""

import json
from unittest.mock import MagicMock, patch

import pytest
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


class TestFactsExtract:
    """`agnes admin sharepoint facts-extract` — CLI counterpart to
    `POST /api/admin/sharepoint/connections/{connection_id}/facts-extract`."""

    def test_bare_call_posts_no_body(self):
        with patch(
            "cli.commands.admin_sharepoint.api_post", return_value=_resp(202, {"job_id": "j1", "status": "queued"})
        ) as mock_post:
            result = runner.invoke(app, ["admin", "sharepoint", "facts-extract", "conn1"])
        assert result.exit_code == 0, result.output
        assert "j1" in result.output
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert args[0] == "/api/admin/sharepoint/connections/conn1/facts-extract"
        assert kwargs.get("json") is None

    def test_doc_id_is_repeatable_and_rides_the_payload(self):
        with patch(
            "cli.commands.admin_sharepoint.api_post", return_value=_resp(202, {"job_id": "j2", "status": "queued"})
        ) as mock_post:
            result = runner.invoke(
                app,
                [
                    "admin",
                    "sharepoint",
                    "facts-extract",
                    "conn1",
                    "--doc-id",
                    "d1",
                    "--doc-id",
                    "d2",
                ],
            )
        assert result.exit_code == 0, result.output
        _, kwargs = mock_post.call_args
        assert kwargs["json"] == {"doc_ids": ["d1", "d2"]}

    def test_timeout_s_rides_the_payload(self):
        with patch(
            "cli.commands.admin_sharepoint.api_post", return_value=_resp(202, {"job_id": "j3", "status": "queued"})
        ) as mock_post:
            result = runner.invoke(app, ["admin", "sharepoint", "facts-extract", "conn1", "--timeout-s", "120"])
        assert result.exit_code == 0, result.output
        _, kwargs = mock_post.call_args
        assert kwargs["json"] == {"timeout_s": 120}

    def test_json_output(self):
        body = {"job_id": "j4", "status": "queued"}
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(202, body)):
            result = runner.invoke(app, ["admin", "sharepoint", "facts-extract", "conn1", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == body

    def test_a_typed_error_is_reported_and_exits_nonzero(self):
        detail = {"error": "facts_extraction_disabled", "message": "extraction.facts.enabled is off"}
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(409, {"detail": detail})):
            result = runner.invoke(app, ["admin", "sharepoint", "facts-extract", "conn1"])
        assert result.exit_code == 1
        assert "extraction.facts.enabled is off" in result.output

    def test_a_plain_string_error_is_reported(self):
        with patch(
            "cli.commands.admin_sharepoint.api_post",
            return_value=_resp(404, {"detail": "connection_not_found"}),
        ):
            result = runner.invoke(app, ["admin", "sharepoint", "facts-extract", "does-not-exist"])
        assert result.exit_code == 1
        assert "connection_not_found" in result.output


class TestScopeBulkAdd:
    """`agnes admin sharepoint scope bulk-add` — CLI counterpart to
    `POST /api/admin/sharepoint/connections/{connection_id}/scopes/bulk`."""

    def test_repeatable_path_option_rides_the_payload(self):
        body = {"created": [{"path": "A"}, {"path": "B"}], "skipped": [], "failed": []}
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(200, body)) as mock_post:
            result = runner.invoke(
                app,
                ["admin", "sharepoint", "scope", "bulk-add", "conn1", "--path", "A", "--path", "B"],
            )
        assert result.exit_code == 0, result.output
        assert "Created 2, skipped 0, failed 0" in result.output
        args, kwargs = mock_post.call_args
        assert args[0] == "/api/admin/sharepoint/connections/conn1/scopes/bulk"
        assert kwargs["json"] == {"paths": ["A", "B"]}

    def test_drive_id_rides_the_payload(self):
        body = {"created": [{"path": "A"}], "skipped": [], "failed": []}
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(200, body)) as mock_post:
            result = runner.invoke(
                app,
                ["admin", "sharepoint", "scope", "bulk-add", "conn1", "--path", "A", "--drive-id", "drv1"],
            )
        assert result.exit_code == 0, result.output
        _, kwargs = mock_post.call_args
        assert kwargs["json"] == {"paths": ["A"], "drive_id": "drv1"}

    def test_collection_id_rides_the_payload(self):
        body = {"created": [{"path": "A"}], "skipped": [], "failed": []}
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(200, body)) as mock_post:
            result = runner.invoke(
                app,
                ["admin", "sharepoint", "scope", "bulk-add", "conn1", "--path", "A", "--collection-id", "col_shared"],
            )
        assert result.exit_code == 0, result.output
        _, kwargs = mock_post.call_args
        assert kwargs["json"] == {"paths": ["A"], "collection_id": "col_shared"}

    def test_collection_name_rides_the_payload(self):
        body = {"created": [{"path": "A"}], "skipped": [], "failed": []}
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(200, body)) as mock_post:
            result = runner.invoke(
                app,
                [
                    "admin",
                    "sharepoint",
                    "scope",
                    "bulk-add",
                    "conn1",
                    "--path",
                    "A",
                    "--collection-name",
                    "One Big Site",
                ],
            )
        assert result.exit_code == 0, result.output
        _, kwargs = mock_post.call_args
        assert kwargs["json"] == {"paths": ["A"], "collection": {"name": "One Big Site"}}

    def test_collection_id_and_collection_name_together_is_a_usage_error(self):
        result = runner.invoke(
            app,
            [
                "admin",
                "sharepoint",
                "scope",
                "bulk-add",
                "conn1",
                "--path",
                "A",
                "--collection-id",
                "col_x",
                "--collection-name",
                "Y",
            ],
        )
        assert result.exit_code == 1
        assert "mutually exclusive" in result.output

    def test_paths_file_is_read_and_combined_with_path_options(self, tmp_path):
        paths_file = tmp_path / "split.json"
        paths_file.write_text(json.dumps({"paths": ["From File A", "From File B"]}), encoding="utf-8")
        body = {"created": [], "skipped": [], "failed": []}
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(200, body)) as mock_post:
            result = runner.invoke(
                app,
                [
                    "admin",
                    "sharepoint",
                    "scope",
                    "bulk-add",
                    "conn1",
                    "--paths-file",
                    str(paths_file),
                    "--path",
                    "From Flag",
                ],
            )
        assert result.exit_code == 0, result.output
        _, kwargs = mock_post.call_args
        assert kwargs["json"] == {"paths": ["From Flag", "From File A", "From File B"]}

    def test_bare_json_list_paths_file(self, tmp_path):
        paths_file = tmp_path / "split.json"
        paths_file.write_text(json.dumps(["Only A"]), encoding="utf-8")
        body = {"created": [], "skipped": [], "failed": []}
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(200, body)) as mock_post:
            result = runner.invoke(
                app,
                ["admin", "sharepoint", "scope", "bulk-add", "conn1", "--paths-file", str(paths_file)],
            )
        assert result.exit_code == 0, result.output
        _, kwargs = mock_post.call_args
        assert kwargs["json"] == {"paths": ["Only A"]}

    def test_no_paths_at_all_is_a_clean_error(self):
        result = runner.invoke(app, ["admin", "sharepoint", "scope", "bulk-add", "conn1"])
        assert result.exit_code == 1
        assert "no paths given" in result.output

    def test_json_output_includes_the_full_breakdown(self):
        body = {
            "created": [{"path": "A"}],
            "skipped": [{"path": "B", "source_scope_id": "b1", "reason": "already_present"}],
            "failed": [{"path": "C", "reason": "not_found"}],
        }
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(200, body)):
            result = runner.invoke(
                app,
                ["admin", "sharepoint", "scope", "bulk-add", "conn1", "--path", "A", "--json"],
            )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == body

    def test_a_typed_error_is_reported_and_exits_nonzero(self):
        detail = {"error": "drive_id_required", "message": "drive_id was not supplied..."}
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(400, {"detail": detail})):
            result = runner.invoke(app, ["admin", "sharepoint", "scope", "bulk-add", "conn1", "--path", "A"])
        assert result.exit_code == 1
        assert "drive_id was not supplied" in result.output


class TestConnectionClone:
    """`agnes admin sharepoint connection clone` — CLI counterpart to
    `POST /api/admin/sharepoint/connections/{connection_id}/clone`."""

    def test_happy_path_reports_secret_copied(self):
        body = {"id": "conn2", "name": "clone-target", "secret_copied": True}
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(201, body)) as mock_post:
            result = runner.invoke(
                app, ["admin", "sharepoint", "connection", "clone", "conn1", "--name", "clone-target"]
            )
        assert result.exit_code == 0, result.output
        assert "conn1 -> conn2" in result.output
        assert "Vault secret copied" in result.output
        args, kwargs = mock_post.call_args
        assert args[0] == "/api/admin/sharepoint/connections/conn1/clone"
        assert kwargs["json"] == {"name": "clone-target"}

    def test_happy_path_reports_no_secret_to_copy(self):
        body = {"id": "conn2", "name": "clone-target", "secret_copied": False}
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(201, body)):
            result = runner.invoke(
                app, ["admin", "sharepoint", "connection", "clone", "conn1", "--name", "clone-target"]
            )
        assert result.exit_code == 0, result.output
        assert "No vault secret to copy" in result.output

    def test_json_output(self):
        body = {"id": "conn2", "name": "clone-target"}
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(201, body)):
            result = runner.invoke(
                app, ["admin", "sharepoint", "connection", "clone", "conn1", "--name", "clone-target", "--json"]
            )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == body

    def test_name_conflict_is_reported(self):
        with patch(
            "cli.commands.admin_sharepoint.api_post",
            return_value=_resp(409, {"detail": "connection_name_exists"}),
        ):
            result = runner.invoke(app, ["admin", "sharepoint", "connection", "clone", "conn1", "--name", "taken"])
        assert result.exit_code == 1
        assert "connection_name_exists" in result.output


class TestCollectionsConsolidate:
    """`agnes admin sharepoint collections consolidate` — CLI counterpart to
    `POST /api/admin/sharepoint/connections/{connection_id}/collections/consolidate`."""

    def test_defaults_to_a_dry_run(self):
        body = {
            "dry_run": True,
            "target": {"id": "col_target", "name": "Merged", "slug": "merged"},
            "sources": [{"id": "col_a", "name": "A", "slug": "a", "file_count": 3}],
            "blocking": [],
        }
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(200, body)) as mock_post:
            result = runner.invoke(
                app,
                ["admin", "sharepoint", "collections", "consolidate", "conn1", "--target-name", "Merged"],
            )
        assert result.exit_code == 0, result.output
        assert "[dry run]" in result.output
        assert "A (col_a) — 3 file(s)" in result.output
        args, kwargs = mock_post.call_args
        assert args[0] == "/api/admin/sharepoint/connections/conn1/collections/consolidate"
        assert kwargs["json"] == {"dry_run": True, "target": {"name": "Merged"}}

    def test_target_collection_id_rides_the_payload(self):
        body = {
            "dry_run": True,
            "target": {"id": "col_target", "name": "Existing", "slug": "existing"},
            "sources": [],
            "blocking": [],
        }
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(200, body)) as mock_post:
            result = runner.invoke(
                app,
                [
                    "admin",
                    "sharepoint",
                    "collections",
                    "consolidate",
                    "conn1",
                    "--target-collection-id",
                    "col_target",
                ],
            )
        assert result.exit_code == 0, result.output
        _, kwargs = mock_post.call_args
        assert kwargs["json"] == {"dry_run": True, "target_collection_id": "col_target"}

    def test_execute_flag_sends_dry_run_false_and_reports_the_summary(self):
        body = {
            "dry_run": False,
            "target": {"id": "col_target", "name": "Merged", "slug": "merged"},
            "sources": [{"id": "col_a", "name": "A", "slug": "a", "file_count": 3}],
            "files_moved": 3,
            "chunks_moved": 5,
            "sources_moved": 3,
            "events_moved": 1,
            "claims_moved": 2,
            "grants_merged": 1,
            "scopes_repointed": 1,
        }
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(200, body)) as mock_post:
            result = runner.invoke(
                app,
                [
                    "admin",
                    "sharepoint",
                    "collections",
                    "consolidate",
                    "conn1",
                    "--target-name",
                    "Merged",
                    "--execute",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "Folded 1 collection(s)" in result.output
        assert "files=3" in result.output and "scopes_repointed=1" in result.output
        _, kwargs = mock_post.call_args
        assert kwargs["json"] == {"dry_run": False, "target": {"name": "Merged"}}

    def test_neither_target_flag_is_a_usage_error(self):
        result = runner.invoke(app, ["admin", "sharepoint", "collections", "consolidate", "conn1"])
        assert result.exit_code == 1
        assert "exactly one" in result.output

    def test_both_target_flags_is_a_usage_error(self):
        result = runner.invoke(
            app,
            [
                "admin",
                "sharepoint",
                "collections",
                "consolidate",
                "conn1",
                "--target-collection-id",
                "col_x",
                "--target-name",
                "Y",
            ],
        )
        assert result.exit_code == 1
        assert "exactly one" in result.output

    def test_json_output(self):
        body = {"dry_run": True, "target": {"id": "t", "name": "T", "slug": "t"}, "sources": [], "blocking": []}
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(200, body)):
            result = runner.invoke(
                app,
                ["admin", "sharepoint", "collections", "consolidate", "conn1", "--target-name", "T", "--json"],
            )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == body

    def test_a_typed_error_is_reported_and_exits_nonzero(self):
        detail = {"error": "collection_referenced_by_other_connection", "blocking": [{"collection_id": "col_a"}]}
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(409, {"detail": detail})):
            result = runner.invoke(
                app,
                ["admin", "sharepoint", "collections", "consolidate", "conn1", "--target-name", "T", "--execute"],
            )
        assert result.exit_code == 1
        assert "collection_referenced_by_other_connection" in result.output

    def test_site_flag_rides_the_payload(self):
        body = {
            "dry_run": True,
            "target": {"id": None, "name": "Merged", "slug": None},
            "sources": [],
            "blocking": [],
            "connection_ids": ["conn1", "conn2"],
            "running": [],
        }
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(200, body)) as mock_post:
            result = runner.invoke(
                app,
                ["admin", "sharepoint", "collections", "consolidate", "conn1", "--target-name", "Merged", "--site"],
            )
        assert result.exit_code == 0, result.output
        _, kwargs = mock_post.call_args
        assert kwargs["json"] == {"dry_run": True, "include_split_siblings": True, "target": {"name": "Merged"}}

    def test_without_site_the_payload_omits_the_key(self):
        body = {"dry_run": True, "target": {"id": "t", "name": "T", "slug": "t"}, "sources": [], "blocking": []}
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(200, body)) as mock_post:
            runner.invoke(app, ["admin", "sharepoint", "collections", "consolidate", "conn1", "--target-name", "T"])
        _, kwargs = mock_post.call_args
        assert "include_split_siblings" not in kwargs["json"]

    def test_running_siblings_are_warned_about(self):
        body = {
            "dry_run": True,
            "target": {"id": None, "name": "Merged", "slug": None},
            "sources": [],
            "blocking": [],
            "connection_ids": ["conn1", "conn2"],
            "running": ["conn2"],
        }
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(200, body)):
            result = runner.invoke(
                app,
                ["admin", "sharepoint", "collections", "consolidate", "conn1", "--target-name", "Merged", "--site"],
            )
        assert result.exit_code == 0, result.output
        assert "conn2" in result.output
        assert "sibling_crawl_running" in result.output


class TestSplitPlanCmd:
    """`agnes admin sharepoint split-plan` — CLI counterpart to
    `GET /api/admin/sharepoint/connections/{connection_id}/split-plan`."""

    def _plan_body(self):
        return {
            "drive_id": "drv1",
            "folders": [{"name": "Big", "documents": 100}, {"name": "Small", "documents": 10}],
            "loose_root_files": ["readme.txt"],
            "groups": [
                {"name": "site — part 1/2", "folders": [{"name": "Big", "documents": 100}], "documents": 100},
                {"name": "site — part 2/2", "folders": [{"name": "Small", "documents": 10}], "documents": 10},
            ],
            "total_documents": 110,
            "collection": {"id": "col_1", "name": "site", "slug": "site"},
        }

    def test_happy_path_prints_a_table(self):
        with patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, self._plan_body())) as mock_get:
            result = runner.invoke(app, ["admin", "sharepoint", "split-plan", "conn1", "--n", "2"])
        assert result.exit_code == 0, result.output
        assert "site — part 1/2" in result.output
        assert "site — part 2/2" in result.output
        assert "readme.txt" in result.output
        args, kwargs = mock_get.call_args
        assert args[0] == "/api/admin/sharepoint/connections/conn1/split-plan"
        assert kwargs["params"] == {"n": 2}

    def test_min_modified_and_drive_id_ride_the_query(self):
        with patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, self._plan_body())) as mock_get:
            result = runner.invoke(
                app,
                [
                    "admin",
                    "sharepoint",
                    "split-plan",
                    "conn1",
                    "--n",
                    "2",
                    "--min-modified",
                    "2023-12-31",
                    "--drive-id",
                    "drv-x",
                ],
            )
        assert result.exit_code == 0, result.output
        _, kwargs = mock_get.call_args
        assert kwargs["params"] == {"n": 2, "min_modified": "2023-12-31", "drive_id": "drv-x"}

    def test_json_output(self):
        body = self._plan_body()
        with patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, body)):
            result = runner.invoke(app, ["admin", "sharepoint", "split-plan", "conn1", "--n", "2", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == body

    def test_a_typed_error_is_reported(self):
        detail = {"error": "drive_id_required", "message": "drive_id was not supplied"}
        with patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(400, {"detail": detail})):
            result = runner.invoke(app, ["admin", "sharepoint", "split-plan", "conn1", "--n", "2"])
        assert result.exit_code == 1
        assert "drive_id was not supplied" in result.output

    def test_shared_collection_is_printed(self):
        with patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, self._plan_body())):
            result = runner.invoke(app, ["admin", "sharepoint", "split-plan", "conn1", "--n", "2"])
        assert result.exit_code == 0, result.output
        assert "site (col_1)" in result.output

    def test_per_folder_collections_prints_the_old_default(self):
        body = self._plan_body()
        body["collection"] = None
        with patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, body)) as mock_get:
            result = runner.invoke(
                app, ["admin", "sharepoint", "split-plan", "conn1", "--n", "2", "--per-folder-collections"]
            )
        assert result.exit_code == 0, result.output
        assert "OWN collection" in result.output
        _, kwargs = mock_get.call_args
        assert kwargs["params"] == {"n": 2, "per_folder_collections": True}

    def test_collection_id_rides_the_query(self):
        with patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, self._plan_body())) as mock_get:
            result = runner.invoke(
                app, ["admin", "sharepoint", "split-plan", "conn1", "--n", "2", "--collection-id", "col_1"]
            )
        assert result.exit_code == 0, result.output
        _, kwargs = mock_get.call_args
        assert kwargs["params"] == {"n": 2, "target_collection_id": "col_1"}

    def test_collection_name_rides_the_query(self):
        with patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, self._plan_body())) as mock_get:
            result = runner.invoke(
                app, ["admin", "sharepoint", "split-plan", "conn1", "--n", "2", "--collection-name", "Whole Site"]
            )
        assert result.exit_code == 0, result.output
        _, kwargs = mock_get.call_args
        assert kwargs["params"] == {"n": 2, "target_name": "Whole Site"}

    def test_collection_id_and_collection_name_together_is_a_usage_error(self):
        result = runner.invoke(
            app,
            [
                "admin",
                "sharepoint",
                "split-plan",
                "conn1",
                "--n",
                "2",
                "--collection-id",
                "col_1",
                "--collection-name",
                "X",
            ],
        )
        assert result.exit_code == 1
        assert "mutually exclusive" in result.output

    def test_per_folder_collections_and_collection_id_together_is_a_usage_error(self):
        result = runner.invoke(
            app,
            [
                "admin",
                "sharepoint",
                "split-plan",
                "conn1",
                "--n",
                "2",
                "--per-folder-collections",
                "--collection-id",
                "col_1",
            ],
        )
        assert result.exit_code == 1
        assert "mutually exclusive" in result.output


class TestSplitCmd:
    """`agnes admin sharepoint split` — CLI counterpart to
    `POST /api/admin/sharepoint/connections/{connection_id}/splits`."""

    def _split_body(self):
        return {
            "connections": [
                {
                    "id": "c1",
                    "name": "site — part 1/2",
                    "folders": [{"name": "Big", "documents": 100}],
                    "documents": 100,
                },
                {
                    "id": "c2",
                    "name": "site — part 2/2",
                    "folders": [{"name": "Small", "documents": 10}],
                    "documents": 10,
                },
            ]
        }

    def test_happy_path(self):
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(201, self._split_body())) as mock_post:
            result = runner.invoke(app, ["admin", "sharepoint", "split", "conn1", "--n", "2"])
        assert result.exit_code == 0, result.output
        assert "Created 2 connection(s)" in result.output
        assert "c1" in result.output and "c2" in result.output
        args, kwargs = mock_post.call_args
        assert args[0] == "/api/admin/sharepoint/connections/conn1/splits"
        assert kwargs["json"] == {"n": 2, "start": False}

    def test_all_options_ride_the_payload(self):
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(201, self._split_body())) as mock_post:
            result = runner.invoke(
                app,
                [
                    "admin",
                    "sharepoint",
                    "split",
                    "conn1",
                    "--n",
                    "2",
                    "--min-modified",
                    "2023-12-31",
                    "--transport",
                    "batch",
                    "--retry-mode",
                    "off",
                    "--start",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "enqueued" in result.output.lower()
        _, kwargs = mock_post.call_args
        assert kwargs["json"] == {
            "n": 2,
            "start": True,
            "min_modified": "2023-12-31",
            "transport": "batch",
            "retry_mode": "off",
        }

    def test_json_output(self):
        body = self._split_body()
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(201, body)):
            result = runner.invoke(app, ["admin", "sharepoint", "split", "conn1", "--n", "2", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == body

    def test_invalid_transport_is_a_usage_error(self):
        result = runner.invoke(
            app, ["admin", "sharepoint", "split", "conn1", "--n", "2", "--transport", "carrier-pigeon"]
        )
        assert result.exit_code == 1
        assert "sync or batch" in result.output

    def test_invalid_retry_mode_is_a_usage_error(self):
        result = runner.invoke(app, ["admin", "sharepoint", "split", "conn1", "--n", "2", "--retry-mode", "nope"])
        assert result.exit_code == 1
        assert "must be one of" in result.output

    def test_split_exists_is_reported(self):
        detail = {"error": "split_exists", "message": "connections named like this split already exist"}
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(409, {"detail": detail})):
            result = runner.invoke(app, ["admin", "sharepoint", "split", "conn1", "--n", "2"])
        assert result.exit_code == 1
        assert "already exist" in result.output

    def test_collection_id_rides_the_payload_and_is_printed(self):
        body = self._split_body()
        body["collection"] = {"id": "col_1", "name": "site", "slug": "site"}
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(201, body)) as mock_post:
            result = runner.invoke(
                app, ["admin", "sharepoint", "split", "conn1", "--n", "2", "--collection-id", "col_1"]
            )
        assert result.exit_code == 0, result.output
        assert "Shared collection: site (col_1)" in result.output
        _, kwargs = mock_post.call_args
        assert kwargs["json"] == {"n": 2, "start": False, "target_collection_id": "col_1"}

    def test_collection_name_rides_the_payload(self):
        body = self._split_body()
        body["collection"] = {"id": "col_new", "name": "Whole Site", "slug": "whole-site"}
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(201, body)) as mock_post:
            result = runner.invoke(
                app, ["admin", "sharepoint", "split", "conn1", "--n", "2", "--collection-name", "Whole Site"]
            )
        assert result.exit_code == 0, result.output
        _, kwargs = mock_post.call_args
        assert kwargs["json"] == {"n": 2, "start": False, "target": {"name": "Whole Site"}}

    def test_per_folder_collections_rides_the_payload(self):
        body = self._split_body()
        body["collection"] = None
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(201, body)) as mock_post:
            result = runner.invoke(
                app, ["admin", "sharepoint", "split", "conn1", "--n", "2", "--per-folder-collections"]
            )
        assert result.exit_code == 0, result.output
        assert "Shared collection" not in result.output
        _, kwargs = mock_post.call_args
        assert kwargs["json"] == {"n": 2, "start": False, "per_folder_collections": True}

    def test_collection_id_and_collection_name_together_is_a_usage_error(self):
        result = runner.invoke(
            app,
            [
                "admin",
                "sharepoint",
                "split",
                "conn1",
                "--n",
                "2",
                "--collection-id",
                "col_1",
                "--collection-name",
                "X",
            ],
        )
        assert result.exit_code == 1
        assert "mutually exclusive" in result.output

    def test_per_folder_collections_and_collection_name_together_is_a_usage_error(self):
        result = runner.invoke(
            app,
            [
                "admin",
                "sharepoint",
                "split",
                "conn1",
                "--n",
                "2",
                "--per-folder-collections",
                "--collection-name",
                "X",
            ],
        )
        assert result.exit_code == 1
        assert "mutually exclusive" in result.output

    def test_unknown_collection_id_is_reported(self):
        with patch(
            "cli.commands.admin_sharepoint.api_post",
            return_value=_resp(404, {"detail": {"error": "collection_not_found"}}),
        ):
            result = runner.invoke(
                app, ["admin", "sharepoint", "split", "conn1", "--n", "2", "--collection-id", "nope"]
            )
        assert result.exit_code == 1
        assert "collection_not_found" in result.output


class TestFactsConfig:
    """`agnes admin sharepoint facts-config` — CLI counterpart to
    `PATCH /api/admin/sharepoint/connections/{connection_id}/extraction/facts-config`."""

    def test_retry_mode_patches_the_connection(self):
        body = {"connection_id": "conn1", "retry_mode": {"value": "always", "source": "connection"}}
        with patch("cli.commands.admin_sharepoint.api_patch", return_value=_resp(200, body)) as mock_patch:
            result = runner.invoke(app, ["admin", "sharepoint", "facts-config", "conn1", "--retry-mode", "always"])
        assert result.exit_code == 0, result.output
        assert "always" in result.output and "connection" in result.output
        args, kwargs = mock_patch.call_args
        assert args[0] == "/api/admin/sharepoint/connections/conn1/extraction/facts-config"
        assert kwargs["json"] == {"retry_mode": "always"}

    def test_clear_sends_a_null_retry_mode(self):
        body = {"connection_id": "conn1", "retry_mode": {"value": "on_gate_fail", "source": "instance"}}
        with patch("cli.commands.admin_sharepoint.api_patch", return_value=_resp(200, body)) as mock_patch:
            result = runner.invoke(app, ["admin", "sharepoint", "facts-config", "conn1", "--clear"])
        assert result.exit_code == 0, result.output
        assert "instance" in result.output
        _, kwargs = mock_patch.call_args
        assert kwargs["json"] == {"retry_mode": None}

    def test_neither_flag_is_a_usage_error(self):
        result = runner.invoke(app, ["admin", "sharepoint", "facts-config", "conn1"])
        assert result.exit_code == 1
        assert "--retry-mode or --clear" in result.output

    def test_both_flags_is_a_usage_error(self):
        result = runner.invoke(app, ["admin", "sharepoint", "facts-config", "conn1", "--retry-mode", "off", "--clear"])
        assert result.exit_code == 1
        assert "not both" in result.output

    def test_an_invalid_retry_mode_is_a_usage_error(self):
        result = runner.invoke(app, ["admin", "sharepoint", "facts-config", "conn1", "--retry-mode", "sometimes"])
        assert result.exit_code == 1
        assert "must be one of" in result.output

    def test_json_output(self):
        body = {"connection_id": "conn1", "retry_mode": {"value": "off", "source": "connection"}}
        with patch("cli.commands.admin_sharepoint.api_patch", return_value=_resp(200, body)):
            result = runner.invoke(
                app, ["admin", "sharepoint", "facts-config", "conn1", "--retry-mode", "off", "--json"]
            )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == body

    def test_a_server_side_422_is_reported(self):
        """The CLI validates locally against `_RETRY_MODES` before calling
        out, but the server's own 422 (a different value than the CLI
        would ever send today) must still surface cleanly rather than a
        bare traceback or a silent success."""
        detail = "retry_mode must be one of ['always', 'on_gate_fail', 'off'] or null (to clear the override)"
        with patch("cli.commands.admin_sharepoint.api_patch", return_value=_resp(422, {"detail": detail})):
            result = runner.invoke(app, ["admin", "sharepoint", "facts-config", "conn1", "--retry-mode", "off"])
        assert result.exit_code == 1
        assert "must be one of" in result.output

    def test_a_404_is_reported(self):
        with patch(
            "cli.commands.admin_sharepoint.api_patch",
            return_value=_resp(404, {"detail": "connection_not_found"}),
        ):
            result = runner.invoke(
                app, ["admin", "sharepoint", "facts-config", "does-not-exist", "--retry-mode", "off"]
            )
        assert result.exit_code == 1
        assert "connection_not_found" in result.output


class TestFactsConfigProvider:
    """`--provider` / `--clear-provider` — the same PRESENT-in-body
    convention `--transport` uses: a provider-only call re-sends the
    connection's CURRENT retry-mode override rather than wiping it, which is
    why it needs a mocked `api_get` (the CLI's own re-fetch) as well as
    `api_patch`."""

    def test_provider_only_patches_and_resends_the_current_retry_mode(self):
        body = {
            "connection_id": "conn1",
            "retry_mode": {"value": "always", "source": "connection"},
            "provider": {
                "value": "vertex",
                "source": "connection",
                "effective": "vertex",
                "effective_source": "connection",
            },
        }
        with (
            patch(
                "cli.commands.admin_sharepoint.api_get",
                return_value=_resp(200, {"config": {"extraction": {"facts": {"retry_mode": "always"}}}}),
            ) as mock_get,
            patch("cli.commands.admin_sharepoint.api_patch", return_value=_resp(200, body)) as mock_patch,
        ):
            result = runner.invoke(app, ["admin", "sharepoint", "facts-config", "conn1", "--provider", "vertex"])
        assert result.exit_code == 0, result.output
        mock_get.assert_called_once()
        args, kwargs = mock_patch.call_args
        assert args[0] == "/api/admin/sharepoint/connections/conn1/extraction/facts-config"
        assert kwargs["json"] == {"retry_mode": "always", "provider": "vertex"}
        assert "vertex" in result.output

    def test_clear_provider_sends_a_null_provider(self):
        body = {
            "connection_id": "conn1",
            "retry_mode": {"value": "on_gate_fail", "source": "instance"},
            "provider": {
                "value": "inherit",
                "source": "instance",
                "effective": "anthropic",
                "effective_source": "instance:inherit",
            },
        }
        with (
            patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, {"config": {}})),
            patch("cli.commands.admin_sharepoint.api_patch", return_value=_resp(200, body)) as mock_patch,
        ):
            result = runner.invoke(app, ["admin", "sharepoint", "facts-config", "conn1", "--clear-provider"])
        assert result.exit_code == 0, result.output
        _, kwargs = mock_patch.call_args
        assert kwargs["json"] == {"retry_mode": None, "provider": None}
        assert "inherit" in result.output
        assert "anthropic" in result.output

    def test_provider_and_clear_provider_together_is_a_usage_error(self):
        result = runner.invoke(
            app, ["admin", "sharepoint", "facts-config", "conn1", "--provider", "vertex", "--clear-provider"]
        )
        assert result.exit_code == 1
        assert "not both" in result.output

    def test_an_invalid_provider_is_a_usage_error(self):
        result = runner.invoke(app, ["admin", "sharepoint", "facts-config", "conn1", "--provider", "openai"])
        assert result.exit_code == 1
        assert "must be one of" in result.output

    def test_provider_alone_satisfies_the_required_flag_check(self):
        body = {
            "connection_id": "conn1",
            "provider": {
                "value": "anthropic",
                "source": "connection",
                "effective": "anthropic",
                "effective_source": "connection",
            },
        }
        with (
            patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, {"config": {}})),
            patch("cli.commands.admin_sharepoint.api_patch", return_value=_resp(200, body)),
        ):
            result = runner.invoke(app, ["admin", "sharepoint", "facts-config", "conn1", "--provider", "anthropic"])
        assert result.exit_code == 0, result.output


class TestFactsConfigVertexRegion:
    """`--vertex-region` / `--clear-vertex-region` — the same PRESENT-in-body
    convention `--transport`/`--provider` use: a vertex-region-only call
    re-sends the connection's CURRENT retry-mode override rather than
    wiping it, which is why it needs a mocked `api_get` (the CLI's own
    re-fetch) as well as `api_patch`."""

    def test_vertex_region_only_patches_and_resends_the_current_retry_mode(self):
        body = {
            "connection_id": "conn1",
            "retry_mode": {"value": "always", "source": "connection"},
            "vertex_region": {"value": "europe-west4", "source": "connection"},
        }
        with (
            patch(
                "cli.commands.admin_sharepoint.api_get",
                return_value=_resp(200, {"config": {"extraction": {"facts": {"retry_mode": "always"}}}}),
            ) as mock_get,
            patch("cli.commands.admin_sharepoint.api_patch", return_value=_resp(200, body)) as mock_patch,
        ):
            result = runner.invoke(
                app, ["admin", "sharepoint", "facts-config", "conn1", "--vertex-region", "europe-west4"]
            )
        assert result.exit_code == 0, result.output
        mock_get.assert_called_once()
        args, kwargs = mock_patch.call_args
        assert args[0] == "/api/admin/sharepoint/connections/conn1/extraction/facts-config"
        assert kwargs["json"] == {"retry_mode": "always", "vertex_region": "europe-west4"}
        assert "europe-west4" in result.output

    def test_clear_vertex_region_sends_a_null_vertex_region(self):
        body = {
            "connection_id": "conn1",
            "retry_mode": {"value": "on_gate_fail", "source": "instance"},
            "vertex_region": {"value": "us-central1", "source": "instance:ai.vertex"},
        }
        with (
            patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, {"config": {}})),
            patch("cli.commands.admin_sharepoint.api_patch", return_value=_resp(200, body)) as mock_patch,
        ):
            result = runner.invoke(app, ["admin", "sharepoint", "facts-config", "conn1", "--clear-vertex-region"])
        assert result.exit_code == 0, result.output
        _, kwargs = mock_patch.call_args
        assert kwargs["json"] == {"retry_mode": None, "vertex_region": None}
        assert "us-central1" in result.output

    def test_vertex_region_and_clear_vertex_region_together_is_a_usage_error(self):
        result = runner.invoke(
            app,
            [
                "admin",
                "sharepoint",
                "facts-config",
                "conn1",
                "--vertex-region",
                "europe-west4",
                "--clear-vertex-region",
            ],
        )
        assert result.exit_code == 1
        assert "not both" in result.output

    def test_an_invalid_vertex_region_is_a_usage_error(self):
        result = runner.invoke(
            app, ["admin", "sharepoint", "facts-config", "conn1", "--vertex-region", "not a region!"]
        )
        assert result.exit_code == 1
        assert "lowercase letters" in result.output

    def test_global_is_accepted(self):
        body = {"connection_id": "conn1", "vertex_region": {"value": "global", "source": "connection"}}
        with (
            patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, {"config": {}})),
            patch("cli.commands.admin_sharepoint.api_patch", return_value=_resp(200, body)),
        ):
            result = runner.invoke(app, ["admin", "sharepoint", "facts-config", "conn1", "--vertex-region", "global"])
        assert result.exit_code == 0, result.output

    def test_vertex_region_alone_satisfies_the_required_flag_check(self):
        body = {"connection_id": "conn1", "vertex_region": {"value": "us-east4", "source": "connection"}}
        with (
            patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, {"config": {}})),
            patch("cli.commands.admin_sharepoint.api_patch", return_value=_resp(200, body)),
        ):
            result = runner.invoke(app, ["admin", "sharepoint", "facts-config", "conn1", "--vertex-region", "us-east4"])
        assert result.exit_code == 0, result.output


class TestCrawlConfig:
    """`agnes admin sharepoint crawl-config` — CLI counterpart to
    `PATCH /api/admin/sharepoint/connections/{connection_id}/extraction/crawl-config`."""

    def test_min_modified_patches_the_connection(self):
        body = {"connection_id": "conn1", "min_modified": {"value": "2023-12-31", "source": "connection"}}
        with patch("cli.commands.admin_sharepoint.api_patch", return_value=_resp(200, body)) as mock_patch:
            result = runner.invoke(
                app, ["admin", "sharepoint", "crawl-config", "conn1", "--min-modified", "2023-12-31"]
            )
        assert result.exit_code == 0, result.output
        assert "2023-12-31" in result.output and "connection" in result.output
        args, kwargs = mock_patch.call_args
        assert args[0] == "/api/admin/sharepoint/connections/conn1/extraction/crawl-config"
        assert kwargs["json"] == {"min_modified": "2023-12-31"}

    def test_clear_sends_a_null_min_modified(self):
        body = {"connection_id": "conn1", "min_modified": {"value": None, "source": "none"}}
        with patch("cli.commands.admin_sharepoint.api_patch", return_value=_resp(200, body)) as mock_patch:
            result = runner.invoke(app, ["admin", "sharepoint", "crawl-config", "conn1", "--clear"])
        assert result.exit_code == 0, result.output
        assert "none" in result.output
        _, kwargs = mock_patch.call_args
        assert kwargs["json"] == {"min_modified": None}

    def test_neither_flag_is_a_usage_error(self):
        result = runner.invoke(app, ["admin", "sharepoint", "crawl-config", "conn1"])
        assert result.exit_code == 1
        assert "--min-modified or --clear" in result.output

    def test_both_flags_is_a_usage_error(self):
        result = runner.invoke(
            app, ["admin", "sharepoint", "crawl-config", "conn1", "--min-modified", "2023-12-31", "--clear"]
        )
        assert result.exit_code == 1
        assert "not both" in result.output

    def test_an_invalid_min_modified_is_a_usage_error(self):
        result = runner.invoke(app, ["admin", "sharepoint", "crawl-config", "conn1", "--min-modified", "not-a-date"])
        assert result.exit_code == 1
        assert "YYYY-MM-DD" in result.output

    def test_json_output(self):
        body = {"connection_id": "conn1", "min_modified": {"value": "2023-12-31", "source": "connection"}}
        with patch("cli.commands.admin_sharepoint.api_patch", return_value=_resp(200, body)):
            result = runner.invoke(
                app, ["admin", "sharepoint", "crawl-config", "conn1", "--min-modified", "2023-12-31", "--json"]
            )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == body

    def test_a_server_side_400_is_reported(self):
        with patch(
            "cli.commands.admin_sharepoint.api_patch",
            return_value=_resp(400, {"detail": "invalid_min_modified"}),
        ):
            result = runner.invoke(
                app, ["admin", "sharepoint", "crawl-config", "conn1", "--min-modified", "2023-12-31"]
            )
        assert result.exit_code == 1
        assert "invalid_min_modified" in result.output

    def test_a_404_is_reported(self):
        with patch(
            "cli.commands.admin_sharepoint.api_patch",
            return_value=_resp(404, {"detail": "connection_not_found"}),
        ):
            result = runner.invoke(
                app, ["admin", "sharepoint", "crawl-config", "does-not-exist", "--min-modified", "2023-12-31"]
            )
        assert result.exit_code == 1
        assert "connection_not_found" in result.output


_FLEET_BODY = {
    "connections": [
        {
            "connection_id": "conn_a",
            "connection_name": "corp-sharepoint",
            "run": {
                "outcome": "running",
                "phase": "facts",
                "files_done": 900,
                "files_seen": 900,
                "error": None,
                "usage": {"facts": {"input_tokens": 1000, "output_tokens": 200, "estimated_cost_usd": 0.5}},
            },
            "files_per_min": 12.5,
            "checkpoint_age_s": 45.0,
            "stuck": False,
            "facts": {"phase_active": True, "docs_done": 12, "docs_total": 340},
            "estimated_cost_usd": 0.5,
        }
    ],
    "totals": {
        "connections": 1,
        "active": 1,
        "stuck": 0,
        "files_done": 900,
        "files_seen": 900,
        "files_per_min": 12.5,
        "facts_docs_done": 12,
        "facts_docs_total": 340,
        "estimated_cost_usd": 0.5,
    },
    "jobs": {
        "corpus-extraction": {"queued": 3, "running": 1},
        "sharepoint-facts-extraction": {"queued": 0, "running": 0},
    },
    "as_of": "2026-09-02T12:00:00+00:00",
}


class TestRuns:
    """`agnes admin sharepoint runs` — CLI counterpart to
    `GET /api/admin/sharepoint/extraction/runs`."""

    def test_bare_call_uses_the_active_scope(self):
        with patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, _FLEET_BODY)) as mock_get:
            result = runner.invoke(app, ["admin", "sharepoint", "runs"])
        assert result.exit_code == 0, result.output
        mock_get.assert_called_once_with("/api/admin/sharepoint/extraction/runs?active=1")
        assert "corp-sharepoint" in result.output
        assert "Totals" in result.output

    def test_all_flag_broadens_the_scope(self):
        with patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, _FLEET_BODY)) as mock_get:
            result = runner.invoke(app, ["admin", "sharepoint", "runs", "--all"])
        assert result.exit_code == 0, result.output
        mock_get.assert_called_once_with("/api/admin/sharepoint/extraction/runs?all=1")

    def test_json_output_is_the_raw_body(self):
        with patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, _FLEET_BODY)):
            result = runner.invoke(app, ["admin", "sharepoint", "runs", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == _FLEET_BODY

    def test_a_typed_501_is_reported_and_exits_nonzero(self):
        body = {"detail": "extraction_runs requires postgres", "error": "requires_postgres_backend"}
        with patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(501, body)):
            result = runner.invoke(app, ["admin", "sharepoint", "runs"])
        assert result.exit_code == 1
        assert "requires postgres" in result.output

    def test_watch_stops_cleanly_on_keyboard_interrupt(self):
        """`--watch` loops until Ctrl-C — the second sleep() call raises to
        simulate the interrupt, and the command must exit 0, not crash."""
        with (
            patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, _FLEET_BODY)) as mock_get,
            patch("cli.commands.admin_sharepoint.time.sleep", side_effect=[None, KeyboardInterrupt()]),
        ):
            result = runner.invoke(app, ["admin", "sharepoint", "runs", "--watch"])
        assert result.exit_code == 0, result.output
        assert mock_get.call_count == 2

    def test_the_jobs_lane_strip_is_printed(self):
        """TCRD-296 synthesis: the queued-vs-running strip `/admin/
        extraction` shows is also readable from the terminal, without
        `--json`."""
        with patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, _FLEET_BODY)):
            result = runner.invoke(app, ["admin", "sharepoint", "runs"])
        assert result.exit_code == 0, result.output
        assert "Jobs" in result.output
        assert "corpus-extraction: 3 queued / 1 running" in result.output
        assert "sharepoint-facts-extraction: 0 queued / 0 running" in result.output

    def test_a_starved_lane_is_flagged(self):
        body = json.loads(json.dumps(_FLEET_BODY))
        body["jobs"] = {"corpus-extraction": {"queued": 5, "running": 0}}
        with patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, body)):
            result = runner.invoke(app, ["admin", "sharepoint", "runs"])
        assert result.exit_code == 0, result.output
        assert "starved" in result.output

    def test_a_body_with_no_jobs_key_prints_no_strip(self):
        """An older server that has not shipped `jobs` yet must not crash
        the command — the strip is simply absent."""
        body = json.loads(json.dumps(_FLEET_BODY))
        del body["jobs"]
        with patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, body)):
            result = runner.invoke(app, ["admin", "sharepoint", "runs"])
        assert result.exit_code == 0, result.output
        assert "Jobs" not in result.output


class TestExtract:
    """`agnes admin sharepoint extract` — CLI counterpart to
    `POST /api/admin/sharepoint/connections/{connection_id}/extract`, the
    manual crawl trigger with its per-run options. Until this command
    existed the run options (`resync`, `force_reprocess`) were reachable
    only from the source card or a hand-written curl."""

    def test_bare_call_posts_no_body(self):
        with patch(
            "cli.commands.admin_sharepoint.api_post", return_value=_resp(202, {"job_id": "e1", "status": "queued"})
        ) as mock_post:
            result = runner.invoke(app, ["admin", "sharepoint", "extract", "conn1"])
        assert result.exit_code == 0, result.output
        assert "e1" in result.output
        args, kwargs = mock_post.call_args
        assert args[0] == "/api/admin/sharepoint/connections/conn1/extract"
        assert kwargs.get("json") is None

    def test_concurrency_and_timeout_ride_the_payload(self):
        with patch(
            "cli.commands.admin_sharepoint.api_post", return_value=_resp(202, {"job_id": "e2", "status": "queued"})
        ) as mock_post:
            result = runner.invoke(
                app,
                ["admin", "sharepoint", "extract", "conn1", "--concurrency", "2", "--timeout-s", "600"],
            )
        assert result.exit_code == 0, result.output
        _, kwargs = mock_post.call_args
        assert kwargs["json"] == {"concurrency": 2, "timeout_s": 600}

    def test_force_reprocess_rides_the_payload_as_true(self):
        """The "re-read every file in scope, ignoring the change cursor"
        option — the same key the source card's checkbox sends, so the two
        surfaces can never drift on what the run does."""
        with patch(
            "cli.commands.admin_sharepoint.api_post", return_value=_resp(202, {"job_id": "e3", "status": "queued"})
        ) as mock_post:
            result = runner.invoke(app, ["admin", "sharepoint", "extract", "conn1", "--force-reprocess"])
        assert result.exit_code == 0, result.output
        _, kwargs = mock_post.call_args
        assert kwargs["json"] == {"force_reprocess": True}

    def test_resync_rides_the_payload_as_true(self):
        with patch(
            "cli.commands.admin_sharepoint.api_post", return_value=_resp(202, {"job_id": "e4", "status": "queued"})
        ) as mock_post:
            result = runner.invoke(app, ["admin", "sharepoint", "extract", "conn1", "--resync"])
        assert result.exit_code == 0, result.output
        _, kwargs = mock_post.call_args
        assert kwargs["json"] == {"resync": True}

    def test_retry_failed_rides_the_payload_as_true(self):
        """The targeted alternative to `--resync` — same key the source
        card's checkbox sends, so REST × CLI × UI never drift."""
        with patch(
            "cli.commands.admin_sharepoint.api_post", return_value=_resp(202, {"job_id": "e6", "status": "queued"})
        ) as mock_post:
            result = runner.invoke(app, ["admin", "sharepoint", "extract", "conn1", "--retry-failed"])
        assert result.exit_code == 0, result.output
        _, kwargs = mock_post.call_args
        assert kwargs["json"] == {"retry_failed": True}

    def test_retry_failed_response_prints_the_queued_count(self):
        """The toast the source card shows and this line must say the same
        thing — the server computes `queued_count` once, from the same
        persisted backlog, for both surfaces."""
        with patch(
            "cli.commands.admin_sharepoint.api_post",
            return_value=_resp(202, {"job_id": "e7", "status": "queued", "queued_count": 3}),
        ):
            result = runner.invoke(app, ["admin", "sharepoint", "extract", "conn1", "--retry-failed"])
        assert result.exit_code == 0, result.output
        assert "queued_count: 3" in result.output

    def test_a_plain_trigger_prints_no_queued_count(self):
        with patch(
            "cli.commands.admin_sharepoint.api_post", return_value=_resp(202, {"job_id": "e8", "status": "queued"})
        ):
            result = runner.invoke(app, ["admin", "sharepoint", "extract", "conn1"])
        assert result.exit_code == 0, result.output
        assert "queued_count" not in result.output

    def test_json_output(self):
        body = {"job_id": "e5", "status": "queued"}
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(202, body)):
            result = runner.invoke(app, ["admin", "sharepoint", "extract", "conn1", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == body

    def test_already_running_is_reported_and_exits_nonzero(self):
        detail = {"error": "extraction_already_running", "job_id": "e0"}
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(409, {"detail": detail})):
            result = runner.invoke(app, ["admin", "sharepoint", "extract", "conn1"])
        assert result.exit_code == 1
        assert "extraction_already_running" in result.output


class TestRetryEmpty:
    """`agnes admin sharepoint retry-empty` — CLI counterpart to
    `POST /api/admin/sharepoint/connections/{connection_id}/extraction/
    retry-empty`."""

    def test_bare_call_posts_to_the_retry_empty_route(self):
        with patch(
            "cli.commands.admin_sharepoint.api_post",
            return_value=_resp(202, {"job_id": "re1", "status": "queued", "queued_count": 3}),
        ) as mock_post:
            result = runner.invoke(app, ["admin", "sharepoint", "retry-empty", "conn1"])
        assert result.exit_code == 0, result.output
        assert "re1" in result.output
        assert "queued_count: 3" in result.output
        args, _ = mock_post.call_args
        assert args[0] == "/api/admin/sharepoint/connections/conn1/extraction/retry-empty"

    def test_json_output(self):
        body = {"job_id": "re2", "status": "queued", "queued_count": 0}
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(202, body)):
            result = runner.invoke(app, ["admin", "sharepoint", "retry-empty", "conn1", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == body

    def test_already_running_is_reported_and_exits_nonzero(self):
        detail = {"error": "extraction_already_running", "job_id": "re0"}
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(409, {"detail": detail})):
            result = runner.invoke(app, ["admin", "sharepoint", "retry-empty", "conn1"])
        assert result.exit_code == 1
        assert "extraction_already_running" in result.output

    def test_not_found_is_reported_and_exits_nonzero(self):
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(404, {"detail": "not found"})):
            result = runner.invoke(app, ["admin", "sharepoint", "retry-empty", "does-not-exist"])
        assert result.exit_code == 1
