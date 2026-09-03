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


class TestFactsReset:
    """`agnes admin sharepoint facts reset --no-claims` — CLI counterpart to
    `POST /api/admin/sharepoint/connections/{connection_id}/facts/reset-no-claims`
    (TCRD-296 gap #62)."""

    def test_bare_call_requires_no_claims(self):
        result = runner.invoke(app, ["admin", "sharepoint", "facts", "reset", "conn1"])
        assert result.exit_code == 1
        assert "--no-claims" in result.output

    def test_no_claims_posts_dry_run_false_by_default(self):
        body = {
            "dry_run": False,
            "candidates": 3,
            "reset": ["cf_1"],
            "duplicates_recorded": {"cf_2": "cf_3"},
            "already_had_claims": 1,
            "unmapped": [],
        }
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(200, body)) as mock_post:
            result = runner.invoke(app, ["admin", "sharepoint", "facts", "reset", "conn1", "--no-claims"])
        assert result.exit_code == 0, result.output
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert args[0] == "/api/admin/sharepoint/connections/conn1/facts/reset-no-claims"
        assert kwargs["json"] == {"dry_run": False}
        assert "candidates=3" in result.output
        assert "reset=1" in result.output
        assert "duplicates_recorded=1" in result.output
        assert "already_had_claims=1" in result.output

    def test_dry_run_flag_rides_the_payload_and_is_labeled(self):
        body = {
            "dry_run": True,
            "candidates": 2,
            "reset": ["cf_1"],
            "duplicates_recorded": {},
            "already_had_claims": 1,
            "unmapped": [],
        }
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(200, body)) as mock_post:
            result = runner.invoke(app, ["admin", "sharepoint", "facts", "reset", "conn1", "--no-claims", "--dry-run"])
        assert result.exit_code == 0, result.output
        _, kwargs = mock_post.call_args
        assert kwargs["json"] == {"dry_run": True}
        assert "[dry run]" in result.output

    def test_json_output(self):
        body = {
            "dry_run": False,
            "candidates": 0,
            "reset": [],
            "duplicates_recorded": {},
            "already_had_claims": 0,
            "unmapped": [],
        }
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(200, body)):
            result = runner.invoke(app, ["admin", "sharepoint", "facts", "reset", "conn1", "--no-claims", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == body

    def test_a_typed_error_is_reported_and_exits_nonzero(self):
        detail = {"error": "facts_extraction_running", "message": "a facts pass is already running"}
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(409, {"detail": detail})):
            result = runner.invoke(app, ["admin", "sharepoint", "facts", "reset", "conn1", "--no-claims"])
        assert result.exit_code == 1
        assert "a facts pass is already running" in result.output


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


class TestSplitMergeCmd:
    """`agnes admin sharepoint split-merge` — CLI counterpart to
    `POST /api/admin/sharepoint/connections/{connection_id}/splits/merge`."""

    def _dry_run_body(self):
        return {
            "dry_run": True,
            "target": {"id": "col_target", "name": "Merged", "slug": "merged"},
            "siblings": [
                {
                    "connection_id": "sib1",
                    "name": "site — part 2/2",
                    "scopes_moved": 2,
                    "scopes_deduped": [],
                    "state": {
                        "crawl": {
                            "delta_links_carried": 1,
                            "ctags_carried": 5,
                            "failed_items_carried": 0,
                            "empty_items_carried": 0,
                            "conflicts": [],
                        },
                        "facts": {"docs_carried": 3, "conflicts": []},
                    },
                }
            ],
            "blocking": [],
        }

    def test_defaults_to_a_dry_run_with_explicit_siblings(self):
        with patch(
            "cli.commands.admin_sharepoint.api_post", return_value=_resp(200, self._dry_run_body())
        ) as mock_post:
            result = runner.invoke(
                app,
                [
                    "admin",
                    "sharepoint",
                    "split-merge",
                    "target1",
                    "--sibling",
                    "sib1",
                    "--target-collection-id",
                    "col_target",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "[dry run] would fold 1 sibling connection(s)" in result.output
        assert "delta_links=1 ctags=5" in result.output and "facts_docs=3" in result.output
        args, kwargs = mock_post.call_args
        assert args[0] == "/api/admin/sharepoint/connections/target1/splits/merge"
        assert kwargs["json"] == {
            "dry_run": True,
            "sibling_ids": ["sib1"],
            "target": {"collection_id": "col_target"},
        }

    def test_all_siblings_flag_rides_the_payload(self):
        body = {**self._dry_run_body(), "siblings": []}
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(200, body)) as mock_post:
            result = runner.invoke(
                app,
                [
                    "admin",
                    "sharepoint",
                    "split-merge",
                    "target1",
                    "--all-siblings",
                    "--target-name",
                    "Merged",
                ],
            )
        assert result.exit_code == 0, result.output
        _, kwargs = mock_post.call_args
        assert kwargs["json"] == {"dry_run": True, "all_split_siblings": True, "target": {"name": "Merged"}}

    def test_execute_flag_sends_dry_run_false_and_reports_conflicts_and_dupes(self):
        body = {
            "dry_run": False,
            "target": {"id": "col_target", "name": "Merged", "slug": "merged"},
            "siblings": [
                {
                    "connection_id": "sib1",
                    "name": "site — part 2/2",
                    "scopes_moved": 1,
                    "scopes_deduped": ["dup-scope"],
                    "state": {
                        "crawl": {
                            "delta_links_carried": 1,
                            "ctags_carried": 0,
                            "failed_items_carried": 0,
                            "empty_items_carried": 0,
                            "conflicts": [{"kind": "delta_links", "key": "drive:a", "resolution": "kept_target"}],
                        },
                        "facts": {"docs_carried": 0, "conflicts": []},
                    },
                    "runs_repointed": 2,
                }
            ],
        }
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(200, body)) as mock_post:
            result = runner.invoke(
                app,
                [
                    "admin",
                    "sharepoint",
                    "split-merge",
                    "target1",
                    "--sibling",
                    "sib1",
                    "--target-collection-id",
                    "col_target",
                    "--execute",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "folded 1 sibling connection(s)" in result.output
        assert "conflict: delta_links drive:a -> kept_target" in result.output
        assert "duplicate scope(s) dropped" in result.output and "dup-scope" in result.output
        _, kwargs = mock_post.call_args
        assert kwargs["json"] == {
            "dry_run": False,
            "sibling_ids": ["sib1"],
            "target": {"collection_id": "col_target"},
        }

    def test_neither_sibling_flag_is_a_usage_error(self):
        result = runner.invoke(app, ["admin", "sharepoint", "split-merge", "target1", "--target-name", "Merged"])
        assert result.exit_code == 1
        assert "exactly one" in result.output

    def test_both_sibling_flags_is_a_usage_error(self):
        result = runner.invoke(
            app,
            [
                "admin",
                "sharepoint",
                "split-merge",
                "target1",
                "--sibling",
                "sib1",
                "--all-siblings",
                "--target-name",
                "Merged",
            ],
        )
        assert result.exit_code == 1
        assert "exactly one" in result.output

    def test_neither_target_flag_is_a_usage_error(self):
        result = runner.invoke(app, ["admin", "sharepoint", "split-merge", "target1", "--sibling", "sib1"])
        assert result.exit_code == 1
        assert "exactly one" in result.output

    def test_both_target_flags_is_a_usage_error(self):
        result = runner.invoke(
            app,
            [
                "admin",
                "sharepoint",
                "split-merge",
                "target1",
                "--sibling",
                "sib1",
                "--target-collection-id",
                "col_x",
                "--target-name",
                "Y",
            ],
        )
        assert result.exit_code == 1
        assert "exactly one" in result.output

    def test_json_output(self):
        body = self._dry_run_body()
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(200, body)):
            result = runner.invoke(
                app,
                [
                    "admin",
                    "sharepoint",
                    "split-merge",
                    "target1",
                    "--sibling",
                    "sib1",
                    "--target-collection-id",
                    "col_target",
                    "--json",
                ],
            )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == body

    def test_a_typed_error_is_reported_and_exits_nonzero(self):
        detail = {"error": "crawl_or_facts_running", "jobs": {"sib1": ["job1"]}}
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(409, {"detail": detail})):
            result = runner.invoke(
                app,
                [
                    "admin",
                    "sharepoint",
                    "split-merge",
                    "target1",
                    "--sibling",
                    "sib1",
                    "--target-collection-id",
                    "col_target",
                    "--execute",
                ],
            )
        assert result.exit_code == 1
        assert "crawl_or_facts_running" in result.output


class TestShardPlanCmd:
    """`agnes admin sharepoint shard-plan` — CLI counterpart to
    `GET /api/admin/sharepoint/connections/{connection_id}/shard-plan`
    (2026-09-03 auto-parallel-crawl design §4.7)."""

    def _inline_body(self):
        return {"mode": "inline", "target_docs": 5000, "signal": "none", "shards": [], "loose_root_files": []}

    def _sharded_body(self):
        return {
            "mode": "sharded",
            "target_docs": 10,
            "signal": "search",
            "shards": [
                {"drive_id": "drv1", "index": 1, "label": "part 1/2", "expected": 400, "targets_count": 1},
                {"drive_id": "drv1", "index": 2, "label": "remainder", "expected": 0, "targets_count": 1},
            ],
            "loose_root_files": ["readme.txt"],
        }

    def test_inline_mode_prints_a_plain_sentence_not_a_table(self):
        with patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, self._inline_body())) as mock_get:
            result = runner.invoke(app, ["admin", "sharepoint", "shard-plan", "conn1"])
        assert result.exit_code == 0, result.output
        assert "single ordinary crawl" in result.output
        args, kwargs = mock_get.call_args
        assert args[0] == "/api/admin/sharepoint/connections/conn1/shard-plan"
        assert kwargs["params"] == {}

    def test_sharded_mode_prints_a_table_with_expected_and_loose_files(self):
        with patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, self._sharded_body())):
            result = runner.invoke(app, ["admin", "sharepoint", "shard-plan", "conn1"])
        assert result.exit_code == 0, result.output
        assert "part 1/2" in result.output
        assert "remainder" in result.output
        assert "readme.txt" in result.output

    def test_min_modified_rides_the_query(self):
        with patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, self._inline_body())) as mock_get:
            result = runner.invoke(app, ["admin", "sharepoint", "shard-plan", "conn1", "--min-modified", "2023-12-31"])
        assert result.exit_code == 0, result.output
        _, kwargs = mock_get.call_args
        assert kwargs["params"] == {"min_modified": "2023-12-31"}

    def test_json_output(self):
        body = self._sharded_body()
        with patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, body)):
            result = runner.invoke(app, ["admin", "sharepoint", "shard-plan", "conn1", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == body

    def test_a_typed_error_is_reported(self):
        detail = {"error": "sharepoint_cert_unresolved", "message": "no certificate configured"}
        with patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(409, {"detail": detail})):
            result = runner.invoke(app, ["admin", "sharepoint", "shard-plan", "conn1"])
        assert result.exit_code == 1
        assert "no certificate configured" in result.output


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


class TestCompletenessCmd:
    """`agnes admin sharepoint completeness` — CLI counterpart to
    `GET …/extraction/completeness`."""

    def _body(self, *, provisional=False):
        return {
            "connection_id": "conn1",
            "rows": [
                {
                    "kind": "scope",
                    "scope_id": "b!drive1",
                    "parent_scope_id": None,
                    "label": "Docs",
                    "collection_id": "col1",
                    "expected": 10,
                    "indexed": 7,
                    "rejected": 0,
                    "failed": 1,
                    "empty": 0,
                    "skipped_unsupported": 0,
                    "oversize": 0,
                    "gap": 2,
                    "status": "missing",
                },
                {
                    "kind": "folder",
                    "scope_id": "f1",
                    "parent_scope_id": "b!drive1",
                    "label": "Reports",
                    "collection_id": "col1",
                    "expected": 5,
                    "indexed": 5,
                    "rejected": 0,
                    "failed": 0,
                    "empty": 0,
                    "skipped_unsupported": 0,
                    "oversize": 0,
                    "gap": 0,
                    "status": "complete",
                },
            ],
            "total": {
                "kind": "total",
                "scope_id": None,
                "parent_scope_id": None,
                "label": "Total",
                "collection_id": None,
                "expected": 10,
                "indexed": 7,
                "rejected": 0,
                "failed": 1,
                "empty": 0,
                "skipped_unsupported": 0,
                "oversize": 0,
                "gap": 2,
                "status": "missing",
            },
            "caveats": [],
            "min_modified": {"value": None, "source": "none"},
            "cached": False,
            "provisional": provisional,
            "as_of": "2026-09-03T00:00:00+00:00",
        }

    def test_happy_path_prints_a_table(self):
        with patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, self._body())) as mock_get:
            result = runner.invoke(app, ["admin", "sharepoint", "completeness", "conn1"])
        assert result.exit_code == 0, result.output
        assert "Docs" in result.output
        assert "Reports" in result.output
        assert "missing" in result.output
        args, kwargs = mock_get.call_args
        assert args[0] == "/api/admin/sharepoint/connections/conn1/extraction/completeness"
        assert kwargs["params"] == {}

    def test_min_modified_and_refresh_ride_the_query(self):
        with patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, self._body())) as mock_get:
            result = runner.invoke(
                app,
                ["admin", "sharepoint", "completeness", "conn1", "--min-modified", "2023-12-31", "--refresh"],
            )
        assert result.exit_code == 0, result.output
        _, kwargs = mock_get.call_args
        assert kwargs["params"] == {"min_modified": "2023-12-31", "refresh": "true"}

    def test_provisional_is_flagged_in_the_title(self):
        with patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, self._body(provisional=True))):
            result = runner.invoke(app, ["admin", "sharepoint", "completeness", "conn1"])
        assert result.exit_code == 0, result.output
        assert "provisional" in result.output

    def test_json_output(self):
        body = self._body()
        with patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, body)):
            result = runner.invoke(app, ["admin", "sharepoint", "completeness", "conn1", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == body

    def test_a_typed_error_is_reported(self):
        detail = {"error": "sharepoint_cert_unresolved", "message": "no certificate configured"}
        with patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(409, {"detail": detail})):
            result = runner.invoke(app, ["admin", "sharepoint", "completeness", "conn1"])
        assert result.exit_code == 1
        assert "no certificate configured" in result.output


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


class TestFmtFactsBacklogSuffix:
    """`_fmt_facts` — TCRD-296 gap #61's two new fields
    (`facts_pending_documents`/`facts_pass_running`), rendered as a
    ` · N backlog, continuing/not running` suffix distinct from the
    existing "(N pending)" text — that number is what's left of the
    CURRENT run's own submitted batch, this one is the whole connection's
    outstanding backlog, which can be nonzero even with no run at all."""

    def test_no_backlog_field_renders_nothing_extra(self):
        from cli.commands.admin_sharepoint import _fmt_facts

        assert _fmt_facts({"docs_done": 5}) == "5 (0 pending)"

    def test_zero_backlog_renders_nothing_extra(self):
        from cli.commands.admin_sharepoint import _fmt_facts

        facts = {"docs_done": 5, "facts_pending_documents": 0, "facts_pass_running": False}
        assert _fmt_facts(facts) == "5 (0 pending)"

    def test_backlog_with_a_pass_running_says_continuing(self):
        from cli.commands.admin_sharepoint import _fmt_facts

        facts = {"docs_done": None, "facts_pending_documents": 12, "facts_pass_running": True}
        assert _fmt_facts(facts) == "— · 12 backlog, continuing"

    def test_backlog_with_no_pass_running_says_not_running(self):
        from cli.commands.admin_sharepoint import _fmt_facts

        facts = {"docs_done": None, "facts_pending_documents": 12, "facts_pass_running": False}
        assert _fmt_facts(facts) == "— · 12 backlog, not running"

    def test_none_facts_dict_is_unaffected(self):
        from cli.commands.admin_sharepoint import _fmt_facts

        assert _fmt_facts(None) == "—"


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

    def test_a_pending_backlog_with_no_pass_running_is_shown(self):
        connection = dict(_FLEET_BODY["connections"][0])
        connection["facts"] = {**connection["facts"], "facts_pending_documents": 42, "facts_pass_running": False}
        body = {**_FLEET_BODY, "connections": [connection]}
        with patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, body)):
            result = runner.invoke(app, ["admin", "sharepoint", "runs"])
        assert result.exit_code == 0, result.output
        assert "42 backlog, not running" in result.output

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


class TestRunsCancel:
    """`agnes admin sharepoint runs cancel <run_id>` — CLI counterpart to
    `POST /api/admin/sharepoint/extraction/runs/{run_id}/cancel`."""

    _CANCEL_BODY = {
        "connection_id": "conn-1",
        "id": "er_1",
        "outcome": "interrupted",
        "interrupted_reason": "cancelled",
        "resumable": True,
    }

    def test_cancel_posts_to_the_run_specific_route(self):
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(200, self._CANCEL_BODY)) as mock_post:
            result = runner.invoke(app, ["admin", "sharepoint", "runs", "cancel", "er_1"])
        assert result.exit_code == 0, result.output
        mock_post.assert_called_once_with("/api/admin/sharepoint/extraction/runs/er_1/cancel")
        assert "er_1" in result.output
        assert "interrupted" in result.output

    def test_cancel_json_output_is_the_raw_body(self):
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(200, self._CANCEL_BODY)):
            result = runner.invoke(app, ["admin", "sharepoint", "runs", "cancel", "er_1", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == self._CANCEL_BODY

    def test_cancel_404_is_reported_and_exits_nonzero(self):
        body = {"detail": "run_not_found"}
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(404, body)):
            result = runner.invoke(app, ["admin", "sharepoint", "runs", "cancel", "er_missing"])
        assert result.exit_code == 1
        assert "run_not_found" in result.output

    def test_cancel_409_is_reported_and_exits_nonzero(self):
        body = {"detail": {"error": "run_not_active", "message": "this run is already 'done'"}}
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(409, body)):
            result = runner.invoke(app, ["admin", "sharepoint", "runs", "cancel", "er_done"])
        assert result.exit_code == 1
        assert "already 'done'" in result.output

    def test_bare_runs_still_works_alongside_the_cancel_subcommand(self):
        """The sub-app conversion must not break the plain dashboard call."""
        with patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, _FLEET_BODY)) as mock_get:
            result = runner.invoke(app, ["admin", "sharepoint", "runs"])
        assert result.exit_code == 0, result.output
        mock_get.assert_called_once_with("/api/admin/sharepoint/extraction/runs?active=1")


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


class TestScopeSetMode:
    """`agnes admin sharepoint scope set-mode` — CLI counterpart to
    `PATCH /api/admin/sharepoint/connections/{connection_id}/scopes/bulk`."""

    def test_all_flag_sends_all_true(self):
        body = {"updated": ["s1", "s2"], "failed": []}
        with patch("cli.commands.admin_sharepoint.api_patch", return_value=_resp(200, body)) as mock_patch:
            result = runner.invoke(
                app, ["admin", "sharepoint", "scope", "set-mode", "conn1", "--all", "--mode", "mirrored"]
            )
        assert result.exit_code == 0, result.output
        assert "Updated 2 scope(s) to mirrored, failed 0" in result.output
        args, kwargs = mock_patch.call_args
        assert args[0] == "/api/admin/sharepoint/connections/conn1/scopes/bulk"
        assert kwargs["json"] == {"access_mode": "mirrored", "all": True}

    def test_repeated_scope_flags_send_source_scope_ids(self):
        body = {"updated": ["s1"], "failed": []}
        with patch("cli.commands.admin_sharepoint.api_patch", return_value=_resp(200, body)) as mock_patch:
            result = runner.invoke(
                app,
                [
                    "admin",
                    "sharepoint",
                    "scope",
                    "set-mode",
                    "conn1",
                    "--scope",
                    "s1",
                    "--scope",
                    "s2",
                    "--mode",
                    "manual",
                ],
            )
        assert result.exit_code == 0, result.output
        _, kwargs = mock_patch.call_args
        assert kwargs["json"] == {"access_mode": "manual", "source_scope_ids": ["s1", "s2"]}

    def test_both_scope_and_all_is_a_usage_error(self):
        result = runner.invoke(
            app,
            ["admin", "sharepoint", "scope", "set-mode", "conn1", "--scope", "s1", "--all", "--mode", "manual"],
        )
        assert result.exit_code == 1
        assert "mutually exclusive" in result.output

    def test_neither_scope_nor_all_is_a_usage_error(self):
        result = runner.invoke(app, ["admin", "sharepoint", "scope", "set-mode", "conn1", "--mode", "manual"])
        assert result.exit_code == 1
        assert "--scope" in result.output and "--all" in result.output

    def test_invalid_mode_is_a_usage_error(self):
        result = runner.invoke(
            app, ["admin", "sharepoint", "scope", "set-mode", "conn1", "--all", "--mode", "sometimes"]
        )
        assert result.exit_code == 1
        assert "manual or mirrored" in result.output

    def test_json_output(self):
        body = {"updated": ["s1"], "failed": [{"source_scope_id": "s2", "reason": "missing_drive_id"}]}
        with patch("cli.commands.admin_sharepoint.api_patch", return_value=_resp(200, body)):
            result = runner.invoke(
                app, ["admin", "sharepoint", "scope", "set-mode", "conn1", "--all", "--mode", "mirrored", "--json"]
            )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == body

    def test_failed_entries_are_printed(self):
        body = {"updated": [], "failed": [{"source_scope_id": "s1", "reason": "missing_drive_id"}]}
        with patch("cli.commands.admin_sharepoint.api_patch", return_value=_resp(200, body)):
            result = runner.invoke(
                app, ["admin", "sharepoint", "scope", "set-mode", "conn1", "--all", "--mode", "mirrored"]
            )
        assert result.exit_code == 0, result.output
        assert "s1" in result.output and "missing_drive_id" in result.output

    def test_a_400_is_reported(self):
        detail = {"error": "both_source_scope_ids_and_all", "message": "pick one"}
        with patch("cli.commands.admin_sharepoint.api_patch", return_value=_resp(400, {"detail": detail})):
            result = runner.invoke(
                app, ["admin", "sharepoint", "scope", "set-mode", "conn1", "--all", "--mode", "manual"]
            )
        assert result.exit_code == 1
        assert "pick one" in result.output


class TestAclMapSiteGroup:
    """`agnes admin sharepoint acl map-site-group` — CLI counterpart to
    `PATCH /api/admin/sharepoint/connections/{connection_id}/acl-site-group-map`.
    Read-modify-write: reads the connection's current map via `api_get`
    before PATCHing the merged whole."""

    def test_maps_a_site_group_preserving_other_entries(self):
        current = {"config": {"acl_site_group_map": {"Owners": ["grp-owners"]}}}
        result_body = {"acl_site_group_map": {"Owners": ["grp-owners"], "Members": ["grp-members"]}}
        with (
            patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, current)),
            patch("cli.commands.admin_sharepoint.api_patch", return_value=_resp(200, result_body)) as mock_patch,
        ):
            result = runner.invoke(
                app,
                [
                    "admin",
                    "sharepoint",
                    "acl",
                    "map-site-group",
                    "conn1",
                    "--site-group",
                    "Members",
                    "--group",
                    "grp-members",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "Members" in result.output and "grp-members" in result.output
        args, kwargs = mock_patch.call_args
        assert args[0] == "/api/admin/sharepoint/connections/conn1/acl-site-group-map"
        assert kwargs["json"] == {"mapping": {"Owners": ["grp-owners"], "Members": ["grp-members"]}}

    def test_multiple_groups_repeatable(self):
        current = {"config": {}}
        with (
            patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, current)),
            patch("cli.commands.admin_sharepoint.api_patch", return_value=_resp(200, {})) as mock_patch,
        ):
            result = runner.invoke(
                app,
                [
                    "admin",
                    "sharepoint",
                    "acl",
                    "map-site-group",
                    "conn1",
                    "--site-group",
                    "Members",
                    "--group",
                    "grp-a",
                    "--group",
                    "grp-b",
                ],
            )
        assert result.exit_code == 0, result.output
        _, kwargs = mock_patch.call_args
        assert kwargs["json"] == {"mapping": {"Members": ["grp-a", "grp-b"]}}

    def test_unmap_removes_the_entry(self):
        current = {"config": {"acl_site_group_map": {"Owners": ["grp-owners"], "Members": ["grp-members"]}}}
        with (
            patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, current)),
            patch("cli.commands.admin_sharepoint.api_patch", return_value=_resp(200, {})) as mock_patch,
        ):
            result = runner.invoke(
                app,
                ["admin", "sharepoint", "acl", "map-site-group", "conn1", "--site-group", "Members", "--unmap"],
            )
        assert result.exit_code == 0, result.output
        assert "Unmapped" in result.output
        _, kwargs = mock_patch.call_args
        assert kwargs["json"] == {"mapping": {"Owners": ["grp-owners"]}}

    def test_group_and_unmap_together_is_a_usage_error(self):
        result = runner.invoke(
            app,
            [
                "admin",
                "sharepoint",
                "acl",
                "map-site-group",
                "conn1",
                "--site-group",
                "Members",
                "--group",
                "grp-a",
                "--unmap",
            ],
        )
        assert result.exit_code == 1
        assert "mutually exclusive" in result.output

    def test_neither_group_nor_unmap_is_a_usage_error(self):
        result = runner.invoke(
            app, ["admin", "sharepoint", "acl", "map-site-group", "conn1", "--site-group", "Members"]
        )
        assert result.exit_code == 1
        assert "--group" in result.output and "--unmap" in result.output

    def test_json_output(self):
        current = {"config": {}}
        result_body = {"acl_site_group_map": {"Members": ["grp-a"]}}
        with (
            patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, current)),
            patch("cli.commands.admin_sharepoint.api_patch", return_value=_resp(200, result_body)),
        ):
            result = runner.invoke(
                app,
                [
                    "admin",
                    "sharepoint",
                    "acl",
                    "map-site-group",
                    "conn1",
                    "--site-group",
                    "Members",
                    "--group",
                    "grp-a",
                    "--json",
                ],
            )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == result_body

    def test_a_400_from_the_patch_is_reported(self):
        current = {"config": {}}
        detail = {"error": "invalid_group_id", "group_ids": ["ghost"]}
        with (
            patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, current)),
            patch("cli.commands.admin_sharepoint.api_patch", return_value=_resp(400, {"detail": detail})),
        ):
            result = runner.invoke(
                app,
                [
                    "admin",
                    "sharepoint",
                    "acl",
                    "map-site-group",
                    "conn1",
                    "--site-group",
                    "Members",
                    "--group",
                    "ghost",
                ],
            )
        assert result.exit_code == 1
        assert "invalid_group_id" in result.output

    def test_a_404_from_the_get_is_reported(self):
        with patch(
            "cli.commands.admin_sharepoint.api_get", return_value=_resp(404, {"detail": "connection_not_found"})
        ):
            result = runner.invoke(
                app,
                [
                    "admin",
                    "sharepoint",
                    "acl",
                    "map-site-group",
                    "does-not-exist",
                    "--site-group",
                    "Members",
                    "--group",
                    "grp-a",
                ],
            )
        assert result.exit_code == 1
        assert "connection_not_found" in result.output
