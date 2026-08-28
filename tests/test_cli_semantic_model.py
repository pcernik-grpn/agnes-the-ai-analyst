"""CLI tests for `agnes admin semantic-model` and `agnes admin semantic-source`
(open semantic-layer contract, Task 11).
"""

from __future__ import annotations

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


# NOTE: a bare model without `datasets` (as the plan's own inline test
# snippets use) is schema-INVALID against the vendored Ossie schema
# (`datasets` is a required, non-empty property per model — see Task 7's
# `_stub_dataset()` note). Every "valid document" fixture needs one.
DOC = (
    "version: '0.2.0.dev0'\n"
    "semantic_model:\n"
    "  - name: retail\n"
    "    datasets:\n"
    "      - name: orders\n"
    "        source: db.public.orders\n"
    "        fields: []\n"
)


class TestSemanticModelList:
    def test_list_json_shape(self):
        rows = [{"id": "manual/_/retail", "slug": "retail", "name": "retail", "source": "manual"}]
        with patch("cli.commands.admin_semantic_model.api_get", return_value=_resp(200, rows)):
            result = runner.invoke(app, ["admin", "semantic-model", "list", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert isinstance(payload, list)
        assert payload[0]["slug"] == "retail"

    def test_list_filters_by_positional_term(self):
        rows = [
            {"id": "m1", "slug": "retail", "name": "Retail", "source": "manual", "description": None},
            {"id": "m2", "slug": "finance", "name": "Finance", "source": "manual", "description": None},
        ]
        with patch("cli.commands.admin_semantic_model.api_get", return_value=_resp(200, rows)):
            result = runner.invoke(app, ["admin", "semantic-model", "list", "fin", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert [m["slug"] for m in payload] == ["finance"]

    def test_list_respects_limit(self):
        rows = [{"id": f"m{i}", "slug": f"s{i}", "name": f"s{i}", "source": "manual"} for i in range(5)]
        with patch("cli.commands.admin_semantic_model.api_get", return_value=_resp(200, rows)):
            result = runner.invoke(app, ["admin", "semantic-model", "list", "--limit", "2", "--json"])
        assert len(json.loads(result.stdout)) == 2


class TestSemanticModelShow:
    def test_show_missing_model_hints_the_next_step(self):
        with patch(
            "cli.commands.admin_semantic_model.api_get",
            return_value=_resp(404, {"detail": "not found"}),
        ):
            result = runner.invoke(app, ["admin", "semantic-model", "show", "nope"])
        assert result.exit_code == 1
        # Error hints go to stderr (repo convention, `err=True`); CliRunner
        # merges both streams into `.output`, not `.stdout`.
        assert "agnes admin semantic-model list" in result.output

    def test_show_found(self):
        row = {"id": "m1", "slug": "retail", "name": "retail", "source": "manual", "description": None}
        with patch("cli.commands.admin_semantic_model.api_get", return_value=_resp(200, row)):
            result = runner.invoke(app, ["admin", "semantic-model", "show", "m1", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout)["slug"] == "retail"


class TestSemanticModelValidate:
    def test_validate_reads_a_local_file_without_touching_the_server(self, tmp_path):
        p = tmp_path / "m.yaml"
        p.write_text("semantic_model: [oops")
        with (
            patch("cli.commands.admin_semantic_model.api_get") as get,
            patch("cli.commands.admin_semantic_model.api_post") as post,
        ):
            result = runner.invoke(app, ["admin", "semantic-model", "validate", str(p)])
        get.assert_not_called()
        post.assert_not_called()
        assert result.exit_code == 1
        assert "YAML" in result.output

    def test_validate_valid_document(self, tmp_path):
        p = tmp_path / "m.yaml"
        p.write_text(DOC)
        result = runner.invoke(app, ["admin", "semantic-model", "validate", str(p)])
        assert result.exit_code == 0
        assert "OK" in result.stdout

    def test_validate_missing_path(self, tmp_path):
        result = runner.invoke(app, ["admin", "semantic-model", "validate", str(tmp_path / "nope.yaml")])
        assert result.exit_code == 1


class TestSemanticModelImportExport:
    def test_import_posts_file_content(self, tmp_path):
        p = tmp_path / "m.yaml"
        p.write_text(DOC)
        with patch(
            "cli.commands.admin_semantic_model.api_post",
            return_value=_resp(201, {"id": "manual/_/retail", "slug": "retail"}),
        ) as m:
            result = runner.invoke(app, ["admin", "semantic-model", "import", str(p)])
        assert result.exit_code == 0
        assert m.call_args.kwargs["json"]["document"] == DOC
        assert "retail" in result.output

    def test_import_invalid_document_reports_errors(self, tmp_path):
        p = tmp_path / "m.yaml"
        p.write_text("semantic_model: [oops")
        with patch(
            "cli.commands.admin_semantic_model.api_post",
            return_value=_resp(422, {"detail": {"errors": ["YAML parse error: boom"]}}),
        ):
            result = runner.invoke(app, ["admin", "semantic-model", "import", str(p)])
        assert result.exit_code == 1
        assert "YAML parse error" in result.output

    def test_export_prints_document_verbatim(self):
        with patch(
            "cli.commands.admin_semantic_model.api_get",
            return_value=_resp(200, text=DOC),
        ):
            result = runner.invoke(app, ["admin", "semantic-model", "export", "retail"])
        assert result.exit_code == 0
        assert result.output == DOC + "\n" or result.output == DOC

    def test_export_missing_hints_the_next_step(self):
        with patch(
            "cli.commands.admin_semantic_model.api_get",
            return_value=_resp(404, {"detail": "not found"}),
        ):
            result = runner.invoke(app, ["admin", "semantic-model", "export", "nope"])
        assert result.exit_code == 1
        assert "agnes admin semantic-model list" in result.output

    def test_export_writes_to_output_file(self, tmp_path):
        out = tmp_path / "out.yaml"
        with patch(
            "cli.commands.admin_semantic_model.api_get",
            return_value=_resp(200, text=DOC),
        ):
            result = runner.invoke(app, ["admin", "semantic-model", "export", "retail", "--output", str(out)])
        assert result.exit_code == 0
        assert out.read_text() == DOC


class TestSemanticModelPackageLink:
    def test_link_package_reports_the_updated_list(self):
        with patch(
            "cli.commands.admin_semantic_model.api_post",
            return_value=_resp(200, {"package_ids": ["pkg_1"]}),
        ) as m:
            result = runner.invoke(app, ["admin", "semantic-model", "link-package", "retail", "pkg_1"])
        assert result.exit_code == 0
        assert m.call_args.kwargs["json"] == {"package_id": "pkg_1"}
        assert "pkg_1" in result.output

    def test_link_package_json_output(self):
        with patch(
            "cli.commands.admin_semantic_model.api_post",
            return_value=_resp(200, {"package_ids": ["pkg_1"]}),
        ):
            result = runner.invoke(app, ["admin", "semantic-model", "link-package", "retail", "pkg_1", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout) == {"package_ids": ["pkg_1"]}

    def test_link_package_missing_model_hints_the_next_step(self):
        with patch(
            "cli.commands.admin_semantic_model.api_post",
            return_value=_resp(404, {"detail": "Semantic model 'nope' not found"}),
        ):
            result = runner.invoke(app, ["admin", "semantic-model", "link-package", "nope", "pkg_1"])
        assert result.exit_code == 1
        assert "agnes admin semantic-model list" in result.output

    def test_link_package_missing_package(self):
        with patch(
            "cli.commands.admin_semantic_model.api_post",
            return_value=_resp(404, {"detail": "data_package_not_found"}),
        ):
            result = runner.invoke(app, ["admin", "semantic-model", "link-package", "retail", "nope"])
        assert result.exit_code == 1
        assert "Data package not found" in result.output

    def test_unlink_package_reports_the_updated_list(self):
        with patch(
            "cli.commands.admin_semantic_model.api_delete",
            return_value=_resp(200, {"package_ids": []}),
        ) as m:
            result = runner.invoke(app, ["admin", "semantic-model", "unlink-package", "retail", "pkg_1"])
        assert result.exit_code == 0
        assert m.call_args.args[0] == "/api/admin/semantic-models/retail/packages/pkg_1"
        assert "retail" in result.output

    def test_unlink_package_missing_model_hints_the_next_step(self):
        with patch(
            "cli.commands.admin_semantic_model.api_delete",
            return_value=_resp(404, {"detail": "Semantic model 'nope' not found"}),
        ):
            result = runner.invoke(app, ["admin", "semantic-model", "unlink-package", "nope", "pkg_1"])
        assert result.exit_code == 1
        assert "agnes admin semantic-model list" in result.output


class TestSemanticSourceAdd:
    def test_add_git_source(self):
        with patch(
            "cli.commands.admin_semantic_source.api_post",
            return_value=_resp(201, {"id": "ss_1", "kind": "git", "name": "Finance models"}),
        ) as m:
            result = runner.invoke(
                app,
                [
                    "admin",
                    "semantic-source",
                    "add",
                    "--kind",
                    "git",
                    "--name",
                    "Finance models",
                    "--repo-url",
                    "https://example.com/x.git",
                    "--ref",
                    "main",
                    "--glob",
                    "semantic/**/*.yaml",
                ],
            )
        assert result.exit_code == 0
        body = m.call_args.kwargs["json"]
        assert body["kind"] == "git"
        assert body["config"]["repo_url"] == "https://example.com/x.git"
        assert body["config"]["glob"] == "semantic/**/*.yaml"
        assert "ss_1" in result.output

    def test_add_upload_source(self, tmp_path):
        p = tmp_path / "m.yaml"
        p.write_text(DOC)
        with patch(
            "cli.commands.admin_semantic_source.api_post",
            return_value=_resp(201, {"id": "ss_2", "kind": "upload"}),
        ) as m:
            result = runner.invoke(
                app,
                [
                    "admin",
                    "semantic-source",
                    "add",
                    "--kind",
                    "upload",
                    "--name",
                    "Manual bundle",
                    "--file",
                    str(p),
                ],
            )
        assert result.exit_code == 0
        assert m.call_args.kwargs["json"]["config"]["documents"] == [DOC]


class TestSemanticSourceList:
    def test_list_json(self):
        rows = [{"id": "ss_1", "kind": "git", "name": "x", "enabled": True}]
        with patch("cli.commands.admin_semantic_source.api_get", return_value=_resp(200, rows)):
            result = runner.invoke(app, ["admin", "semantic-source", "list", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout) == rows


class TestSemanticSourceSync:
    def test_sync_reports_counts(self):
        report = {"models_written": 2, "models_unchanged": 1, "models_pruned": [], "invalid": []}
        with patch("cli.commands.admin_semantic_source.api_post", return_value=_resp(200, report)):
            result = runner.invoke(app, ["admin", "semantic-source", "sync", "ss_1"])
        assert result.exit_code == 0
        assert "written 2" in result.output.lower() or "2" in result.output

    def test_sync_failure_reports_error(self):
        with patch(
            "cli.commands.admin_semantic_source.api_post",
            return_value=_resp(502, {"detail": "sync failed: clone failed: auth"}),
        ):
            result = runner.invoke(app, ["admin", "semantic-source", "sync", "ss_1"])
        assert result.exit_code == 1
        assert "clone failed" in result.output
