"""CLI tests for the admin semantic surface — `agnes admin semantic` and its
`source` sub-group (open semantic-layer contract, Task 11; regrouped by #1707
Block 6).

The paths here are the NEW ones. The old spellings (`agnes admin
semantic-model …` / `agnes admin semantic-source …`) still work as hidden
deprecated aliases; that delegation is covered in
`tests/test_cli_semantic_consolidation.py`, not duplicated here.
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
        with patch("cli.commands.admin_semantic.api_get", return_value=_resp(200, rows)):
            result = runner.invoke(app, ["admin", "semantic", "list", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert isinstance(payload, list)
        assert payload[0]["slug"] == "retail"

    def test_list_filters_by_positional_term(self):
        rows = [
            {"id": "m1", "slug": "retail", "name": "Retail", "source": "manual", "description": None},
            {"id": "m2", "slug": "finance", "name": "Finance", "source": "manual", "description": None},
        ]
        with patch("cli.commands.admin_semantic.api_get", return_value=_resp(200, rows)):
            result = runner.invoke(app, ["admin", "semantic", "list", "fin", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert [m["slug"] for m in payload] == ["finance"]

    def test_list_respects_limit(self):
        rows = [{"id": f"m{i}", "slug": f"s{i}", "name": f"s{i}", "source": "manual"} for i in range(5)]
        with patch("cli.commands.admin_semantic.api_get", return_value=_resp(200, rows)):
            result = runner.invoke(app, ["admin", "semantic", "list", "--limit", "2", "--json"])
        assert len(json.loads(result.stdout)) == 2


class TestSemanticModelShow:
    def test_show_missing_model_hints_the_next_step(self):
        with patch(
            "cli.commands.admin_semantic.api_get",
            return_value=_resp(404, {"detail": "not found"}),
        ):
            result = runner.invoke(app, ["admin", "semantic", "show", "nope"])
        assert result.exit_code == 1
        # Error hints go to stderr (repo convention, `err=True`); CliRunner
        # merges both streams into `.output`, not `.stdout`.
        assert "agnes admin semantic list" in result.output

    def test_show_found(self):
        row = {"id": "m1", "slug": "retail", "name": "retail", "source": "manual", "description": None}
        with patch("cli.commands.admin_semantic.api_get", return_value=_resp(200, row)):
            result = runner.invoke(app, ["admin", "semantic", "show", "m1", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout)["slug"] == "retail"


# `export` and `validate` moved to the any-user `agnes semantic-model` group
# (#1707 Block 6) — neither was ever an admin operation. Their tests moved with
# them, to tests/test_cli_semantic_consolidation.py.


class TestSemanticModelImport:
    def test_import_posts_file_content(self, tmp_path):
        p = tmp_path / "m.yaml"
        p.write_text(DOC)
        with patch(
            "cli.commands.admin_semantic.api_post",
            return_value=_resp(201, {"id": "manual/_/retail", "slug": "retail"}),
        ) as m:
            result = runner.invoke(app, ["admin", "semantic", "import", str(p)])
        assert result.exit_code == 0
        assert m.call_args.kwargs["json"]["document"] == DOC
        assert "retail" in result.output

    def test_import_invalid_document_reports_errors(self, tmp_path):
        p = tmp_path / "m.yaml"
        p.write_text("semantic_model: [oops")
        with patch(
            "cli.commands.admin_semantic.api_post",
            return_value=_resp(422, {"detail": {"errors": ["YAML parse error: boom"]}}),
        ):
            result = runner.invoke(app, ["admin", "semantic", "import", str(p)])
        assert result.exit_code == 1
        assert "YAML parse error" in result.output


class TestSemanticModelPackageLink:
    def test_link_package_reports_the_updated_list(self):
        with patch(
            "cli.commands.admin_semantic.api_post",
            return_value=_resp(200, {"package_ids": ["pkg_1"]}),
        ) as m:
            result = runner.invoke(app, ["admin", "semantic", "link-package", "retail", "pkg_1"])
        assert result.exit_code == 0
        assert m.call_args.kwargs["json"] == {"package_id": "pkg_1"}
        assert "pkg_1" in result.output

    def test_link_package_json_output(self):
        with patch(
            "cli.commands.admin_semantic.api_post",
            return_value=_resp(200, {"package_ids": ["pkg_1"]}),
        ):
            result = runner.invoke(app, ["admin", "semantic", "link-package", "retail", "pkg_1", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout) == {"package_ids": ["pkg_1"]}

    def test_link_package_missing_model_hints_the_next_step(self):
        with patch(
            "cli.commands.admin_semantic.api_post",
            return_value=_resp(404, {"detail": "Semantic model 'nope' not found"}),
        ):
            result = runner.invoke(app, ["admin", "semantic", "link-package", "nope", "pkg_1"])
        assert result.exit_code == 1
        assert "agnes admin semantic list" in result.output

    def test_link_package_missing_package(self):
        with patch(
            "cli.commands.admin_semantic.api_post",
            return_value=_resp(404, {"detail": "data_package_not_found"}),
        ):
            result = runner.invoke(app, ["admin", "semantic", "link-package", "retail", "nope"])
        assert result.exit_code == 1
        assert "Data package not found" in result.output

    def test_unlink_package_reports_the_updated_list(self):
        with patch(
            "cli.commands.admin_semantic.api_delete",
            return_value=_resp(200, {"package_ids": []}),
        ) as m:
            result = runner.invoke(app, ["admin", "semantic", "unlink-package", "retail", "pkg_1"])
        assert result.exit_code == 0
        assert m.call_args.args[0] == "/api/admin/semantic-models/retail/packages/pkg_1"
        assert "retail" in result.output

    def test_unlink_package_missing_model_hints_the_next_step(self):
        with patch(
            "cli.commands.admin_semantic.api_delete",
            return_value=_resp(404, {"detail": "Semantic model 'nope' not found"}),
        ):
            result = runner.invoke(app, ["admin", "semantic", "unlink-package", "nope", "pkg_1"])
        assert result.exit_code == 1
        assert "agnes admin semantic list" in result.output


class TestSemanticSourceAdd:
    def test_add_git_source(self):
        with patch(
            "cli.commands.admin_semantic.api_post",
            return_value=_resp(201, {"id": "ss_1", "kind": "git", "name": "Finance models"}),
        ) as m:
            result = runner.invoke(
                app,
                [
                    "admin",
                    "semantic",
                    "source",
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
            "cli.commands.admin_semantic.api_post",
            return_value=_resp(201, {"id": "ss_2", "kind": "upload"}),
        ) as m:
            result = runner.invoke(
                app,
                [
                    "admin",
                    "semantic",
                    "source",
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
        with patch("cli.commands.admin_semantic.api_get", return_value=_resp(200, rows)):
            result = runner.invoke(app, ["admin", "semantic", "source", "list", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout) == rows

    def test_json_carries_the_owned_model_count_verbatim(self):
        """`--json` is the server payload, so the count rides along untouched."""
        rows = [{"id": "ss_1", "kind": "git", "name": "x", "enabled": True, "owned_model_count": 3}]
        with patch("cli.commands.admin_semantic.api_get", return_value=_resp(200, rows)):
            result = runner.invoke(app, ["admin", "semantic", "source", "list", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout)[0]["owned_model_count"] == 3

    def test_table_distinguishes_a_synced_source_that_owns_nothing(self):
        """#1707: both rows read `ok`; only the model count tells them apart."""
        rows = [
            {
                "id": "ss_full",
                "kind": "connection",
                "name": "Warehouse",
                "enabled": True,
                "last_sync_status": "ok",
                "owned_model_count": 4,
            },
            {
                "id": "ss_empty",
                "kind": "connection",
                "name": "Empty",
                "enabled": True,
                "last_sync_status": "ok",
                "owned_model_count": 0,
            },
        ]
        with patch("cli.commands.admin_semantic.api_get", return_value=_resp(200, rows)):
            result = runner.invoke(app, ["admin", "semantic", "source", "list"])
        assert result.exit_code == 0
        lines = {line.split()[0]: line for line in result.output.splitlines() if line.startswith("ss_")}
        assert "4 models" in lines["ss_full"]
        assert "0 models" in lines["ss_empty"]

    def test_a_synced_source_owning_nothing_gets_a_forward_hint(self):
        rows = [
            {
                "id": "ss_empty",
                "kind": "connection",
                "name": "Empty",
                "enabled": True,
                "last_sync_status": "ok",
                "owned_model_count": 0,
            }
        ]
        with patch("cli.commands.admin_semantic.api_get", return_value=_resp(200, rows)):
            result = runner.invoke(app, ["admin", "semantic", "source", "list"])
        assert result.exit_code == 0
        assert "synced but own no models" in result.output
        assert "agnes admin semantic source sync" in result.output

    def test_no_hint_when_every_synced_source_owns_something(self):
        rows = [
            {
                "id": "ss_full",
                "kind": "git",
                "name": "Full",
                "enabled": True,
                "last_sync_status": "ok",
                "owned_model_count": 1,
            }
        ]
        with patch("cli.commands.admin_semantic.api_get", return_value=_resp(200, rows)):
            result = runner.invoke(app, ["admin", "semantic", "source", "list"])
        assert "own no models" not in result.output

    def test_a_never_synced_source_owning_nothing_is_not_flagged(self):
        """Nothing has run yet — "0 models" there is expected, not a finding."""
        rows = [{"id": "ss_new", "kind": "git", "name": "New", "enabled": True, "owned_model_count": 0}]
        with patch("cli.commands.admin_semantic.api_get", return_value=_resp(200, rows)):
            result = runner.invoke(app, ["admin", "semantic", "source", "list"])
        assert "own no models" not in result.output

    def test_an_explicit_null_renders_the_placeholder_not_zero(self):
        """`null` is "cannot say" (the source's provenance could not be
        resolved), not "owns nothing" — and it must not be flagged as a
        silently-empty source either."""
        rows = [
            {
                "id": "ss_murky",
                "kind": "connection",
                "name": "Murky",
                "enabled": True,
                "last_sync_status": "ok",
                "owned_model_count": None,
            }
        ]
        with patch("cli.commands.admin_semantic.api_get", return_value=_resp(200, rows)):
            result = runner.invoke(app, ["admin", "semantic", "source", "list"])
        assert result.exit_code == 0
        assert "0 models" not in result.output
        assert "own no models" not in result.output

    def test_a_server_without_the_field_renders_a_placeholder(self):
        """Version skew: an older server omits the field. Print "-", never a
        confident "0 models" the server never claimed."""
        rows = [{"id": "ss_1", "kind": "git", "name": "x", "enabled": True, "last_sync_status": "ok"}]
        with patch("cli.commands.admin_semantic.api_get", return_value=_resp(200, rows)):
            result = runner.invoke(app, ["admin", "semantic", "source", "list"])
        assert result.exit_code == 0
        assert "0 models" not in result.output
        assert "own no models" not in result.output


class TestSemanticSourceListScanScope:
    """Finding A17 on #1707: "ok · 0 models" still cannot separate "there is
    nothing upstream" from "the role I connect as cannot see it". The row also
    names what was scanned."""

    @staticmethod
    def _row(**overrides) -> dict:
        row = {
            "id": "ss_sf",
            "kind": "connection",
            "name": "Warehouse",
            "enabled": True,
            "last_sync_status": "ok",
            "owned_model_count": 0,
            "scan_scope": "ESHOP_DEMO.RAW as ESHOP_DEMO_ROLE",
        }
        row.update(overrides)
        return row

    def _run(self, rows: list[dict]):
        with patch("cli.commands.admin_semantic.api_get", return_value=_resp(200, rows)):
            return runner.invoke(app, ["admin", "semantic", "source", "list"])

    def test_the_row_names_what_was_scanned(self):
        result = self._run([self._row()])
        assert result.exit_code == 0
        assert "scanned ESHOP_DEMO.RAW as ESHOP_DEMO_ROLE" in result.output

    def test_the_scope_sits_beside_the_model_count(self):
        """Both, not one instead of the other — the count says nothing came
        back, the scope says where it looked."""
        result = self._run([self._row()])
        line = next(line for line in result.output.splitlines() if line.startswith("ss_sf"))
        assert "0 models" in line
        assert "scanned ESHOP_DEMO.RAW as ESHOP_DEMO_ROLE" in line

    def test_a_source_without_a_derivable_scope_prints_none(self):
        result = self._run([self._row(scan_scope=None)])
        assert result.exit_code == 0
        assert "scanned" not in result.output

    def test_a_server_too_old_to_send_the_field_prints_none(self):
        rows = [self._row()]
        rows[0].pop("scan_scope")
        result = self._run(rows)
        assert result.exit_code == 0
        assert "scanned" not in result.output

    def test_json_carries_the_scope_verbatim(self):
        with patch("cli.commands.admin_semantic.api_get", return_value=_resp(200, [self._row()])):
            result = runner.invoke(app, ["admin", "semantic", "source", "list", "--json"])
        assert json.loads(result.stdout)[0]["scan_scope"] == "ESHOP_DEMO.RAW as ESHOP_DEMO_ROLE"


class TestSemanticSourceSync:
    def test_sync_reports_counts(self):
        report = {"models_written": 2, "models_unchanged": 1, "models_pruned": [], "invalid": []}
        with patch("cli.commands.admin_semantic.api_post", return_value=_resp(200, report)):
            result = runner.invoke(app, ["admin", "semantic", "source", "sync", "ss_1"])
        assert result.exit_code == 0
        assert "written 2" in result.output.lower() or "2" in result.output

    def test_sync_failure_reports_error(self):
        with patch(
            "cli.commands.admin_semantic.api_post",
            return_value=_resp(502, {"detail": "sync failed: clone failed: auth"}),
        ):
            result = runner.invoke(app, ["admin", "semantic", "source", "sync", "ss_1"])
        assert result.exit_code == 1
        assert "clone failed" in result.output
