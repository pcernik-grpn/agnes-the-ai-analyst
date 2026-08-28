"""Block 6 of #1707 — the semantic-layer CLI collapses to two groups.

Five groups that all read as "the semantic layer" (`agnes semantic-model`,
`agnes admin semantic-model`, `agnes admin semantic-source`, `agnes admin
semantic-layer`, `agnes admin data-semantics`) become two:

  * ``agnes semantic-model``  — anything any authenticated caller may do
  * ``agnes admin semantic``  — anything that needs an admin

Every old invocation keeps working for one release as a HIDDEN alias that
prints a one-line stderr notice naming the new path. This module is the
contract for both halves: the new commands, and the aliases that keep the
old ones alive.
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


DOC = (
    "version: '0.2.0.dev0'\n"
    "semantic_model:\n"
    "  - name: retail\n"
    "    datasets:\n"
    "      - name: orders\n"
    "        source: db.public.orders\n"
    "        fields: []\n"
)

_MODEL_ROW = {
    "id": "manual/_/retail",
    "slug": "retail",
    "name": "Retail",
    "description": "Orders and revenue",
    "source": "manual",
    "source_ref": None,
    "status": "valid",
    "spec_version": "0.2.0.dev0",
    "content_hash": "abc123",
}


def _tree() -> dict:
    """`{path: typer command/group}` for the whole registered CLI tree."""
    out: dict = {}

    def walk(node, prefix):
        for g in getattr(node, "registered_groups", []):
            if not g.name:
                continue
            out[f"{prefix}{g.name}"] = g
            walk(g.typer_instance, f"{prefix}{g.name} ")
        for c in getattr(node, "registered_commands", []):
            if c.name:
                out[f"{prefix}{c.name}"] = c

    walk(app, "")
    return out


# ---------------------------------------------------------------------------
# 1. `agnes semantic-model search` — the missing non-admin listing
# ---------------------------------------------------------------------------


class TestSemanticModelSearch:
    def test_search_calls_the_public_endpoint_with_term_and_limit(self):
        body = {"query": "ret", "models": [_MODEL_ROW], "count": 1}
        with patch("cli.commands.semantic_model.api_get", return_value=_resp(200, body)) as m:
            result = runner.invoke(app, ["semantic-model", "search", "ret", "--limit", "5"])
        assert result.exit_code == 0
        assert m.call_args.args[0] == "/api/semantic-models/search"
        assert m.call_args.kwargs["params"] == {"q": "ret", "limit": 5}
        assert "retail" in result.output

    def test_search_json_is_the_raw_response(self):
        body = {"query": "ret", "models": [_MODEL_ROW], "count": 1}
        with patch("cli.commands.semantic_model.api_get", return_value=_resp(200, body)):
            result = runner.invoke(app, ["semantic-model", "search", "ret", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout)["models"][0]["slug"] == "retail"

    def test_no_match_hints_the_next_step(self):
        with patch(
            "cli.commands.semantic_model.api_get",
            return_value=_resp(200, {"query": "zzz", "models": [], "count": 0}),
        ):
            result = runner.invoke(app, ["semantic-model", "search", "zzz"])
        # Not an error — an empty result set is an answer — but it must point
        # forward (command-UX standard: "not found" hints the next step).
        assert result.exit_code == 0
        assert "agnes semantic-model context dataset" in result.output

    def test_a_full_page_says_it_may_be_truncated(self):
        """Silent partial scope is forbidden by the command-UX standard."""
        rows = [dict(_MODEL_ROW, slug=f"m{i}") for i in range(3)]
        with patch(
            "cli.commands.semantic_model.api_get",
            return_value=_resp(200, {"query": "m", "models": rows, "count": 3}),
        ):
            result = runner.invoke(app, ["semantic-model", "search", "m", "--limit", "3"])
        assert "--limit" in result.output


# ---------------------------------------------------------------------------
# 2. `agnes semantic-model show` — non-admin metadata, via the search response
# ---------------------------------------------------------------------------


class TestSemanticModelShow:
    def test_show_resolves_through_search_and_needs_no_admin_endpoint(self):
        body = {"query": "retail", "models": [_MODEL_ROW], "count": 1}
        with patch("cli.commands.semantic_model.api_get", return_value=_resp(200, body)) as m:
            result = runner.invoke(app, ["semantic-model", "show", "retail"])
        assert result.exit_code == 0
        assert m.call_args.args[0] == "/api/semantic-models/search"
        assert "manual/_/retail" in result.output
        assert "abc123" in result.output

    def test_show_ignores_substring_matches_that_are_not_the_slug(self):
        other = dict(_MODEL_ROW, id="manual/_/retail-eu", slug="retail-eu")
        body = {"query": "retail", "models": [other, _MODEL_ROW], "count": 2}
        with patch("cli.commands.semantic_model.api_get", return_value=_resp(200, body)):
            result = runner.invoke(app, ["semantic-model", "show", "retail", "--json"])
        assert json.loads(result.stdout)["slug"] == "retail"

    def test_show_missing_hints_search(self):
        with patch(
            "cli.commands.semantic_model.api_get",
            return_value=_resp(200, {"query": "nope", "models": [], "count": 0}),
        ):
            result = runner.invoke(app, ["semantic-model", "show", "nope"])
        assert result.exit_code == 1
        assert "agnes semantic-model search" in result.output


# ---------------------------------------------------------------------------
# 3. `export` / `validate` moved to the user group (they are not admin ops)
# ---------------------------------------------------------------------------


class TestExportAndValidateAreUserCommands:
    def test_export_reads_the_public_resource_gated_endpoint(self):
        with patch("cli.commands.semantic_model.api_get", return_value=_resp(200, text=DOC)) as m:
            result = runner.invoke(app, ["semantic-model", "export", "retail"])
        assert result.exit_code == 0
        assert m.call_args.args[0] == "/api/semantic-models/retail.yaml"
        assert "semantic_model:" in result.output

    def test_export_writes_to_output_file(self, tmp_path):
        out = tmp_path / "out.yaml"
        with patch("cli.commands.semantic_model.api_get", return_value=_resp(200, text=DOC)):
            result = runner.invoke(app, ["semantic-model", "export", "retail", "--output", str(out)])
        assert result.exit_code == 0
        assert out.read_text() == DOC

    def test_export_missing_hints_search_not_the_admin_list(self):
        with patch(
            "cli.commands.semantic_model.api_get",
            return_value=_resp(404, {"detail": "not found"}),
        ):
            result = runner.invoke(app, ["semantic-model", "export", "nope"])
        assert result.exit_code == 1
        assert "agnes semantic-model search" in result.output

    def test_validate_is_offline(self, tmp_path):
        p = tmp_path / "m.yaml"
        p.write_text(DOC)
        with (
            patch("cli.commands.semantic_model.api_get") as get,
            patch("cli.commands.semantic_model.api_post") as post,
        ):
            result = runner.invoke(app, ["semantic-model", "validate", str(p)])
        get.assert_not_called()
        post.assert_not_called()
        assert result.exit_code == 0
        assert "OK" in result.stdout

    def test_validate_help_names_the_sibling_it_is_confused_with(self):
        result = runner.invoke(app, ["semantic-model", "validate", "--help"])
        assert "validate-query" in result.output

    def test_validate_query_help_names_the_sibling_it_is_confused_with(self):
        result = runner.invoke(app, ["semantic-model", "validate-query", "--help"])
        assert "semantic-model validate" in result.output


# ---------------------------------------------------------------------------
# 4. `agnes admin semantic` — one admin group replacing three
# ---------------------------------------------------------------------------


class TestAdminSemanticGroup:
    def test_the_three_old_admin_groups_are_hidden_aliases(self):
        tree = _tree()
        assert "admin semantic" in tree
        for old in ("admin semantic-model", "admin semantic-source", "admin semantic-layer"):
            assert old in tree, f"{old} must survive as an alias for one release"
            assert tree[old].hidden is True, f"{old} must be hidden from --help"

    def test_admin_semantic_carries_every_promised_command(self):
        tree = _tree()
        for path in (
            "admin semantic list",
            "admin semantic show",
            "admin semantic import",
            "admin semantic delete",
            "admin semantic source add",
            "admin semantic source list",
            "admin semantic source sync",
            "admin semantic source rm",
            "admin semantic coverage",
            "admin semantic coverage tables",
            "admin semantic keboola-import",
            "admin semantic health",
            "admin semantic mute",
            "admin semantic mutes",
            "admin semantic unmute",
        ):
            assert path in tree, f"missing {path}"

    def test_list(self):
        with patch("cli.commands.admin_semantic.api_get", return_value=_resp(200, [_MODEL_ROW])):
            result = runner.invoke(app, ["admin", "semantic", "list", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout)[0]["slug"] == "retail"

    def test_delete_asks_before_destroying_and_calls_the_admin_endpoint(self):
        with patch("cli.commands.admin_semantic.api_delete", return_value=_resp(204)) as m:
            result = runner.invoke(app, ["admin", "semantic", "delete", "retail", "--yes"])
        assert result.exit_code == 0
        assert m.call_args.args[0] == "/api/admin/semantic-models/retail"
        assert "retail" in result.output

    def test_delete_without_yes_aborts_on_a_no(self):
        with patch("cli.commands.admin_semantic.api_delete") as m:
            result = runner.invoke(app, ["admin", "semantic", "delete", "retail"], input="n\n")
        m.assert_not_called()
        assert result.exit_code != 0

    def test_delete_missing_hints_the_listing(self):
        with patch(
            "cli.commands.admin_semantic.api_delete",
            return_value=_resp(404, {"detail": "Semantic model 'nope' not found"}),
        ):
            result = runner.invoke(app, ["admin", "semantic", "delete", "nope", "--yes"])
        assert result.exit_code == 1
        assert "agnes admin semantic list" in result.output

    def test_delete_of_a_source_owned_model_names_the_escape_hatch(self):
        with patch(
            "cli.commands.admin_semantic.api_delete",
            return_value=_resp(409, {"detail": {"error": "source_owned"}}),
        ):
            result = runner.invoke(app, ["admin", "semantic", "delete", "retail", "--yes"])
        assert result.exit_code == 1
        assert "detach" in result.output

    def test_source_rm_calls_the_admin_endpoint(self):
        with patch("cli.commands.admin_semantic.api_delete", return_value=_resp(204)) as m:
            result = runner.invoke(app, ["admin", "semantic", "source", "rm", "ss_1", "--yes"])
        assert result.exit_code == 0
        assert m.call_args.args[0] == "/api/admin/semantic-sources/ss_1"

    def test_source_rm_missing_hints_the_listing(self):
        with patch(
            "cli.commands.admin_semantic.api_delete",
            return_value=_resp(404, {"detail": "Semantic source 'ss_9' not found"}),
        ):
            result = runner.invoke(app, ["admin", "semantic", "source", "rm", "ss_9", "--yes"])
        assert result.exit_code == 1
        assert "agnes admin semantic source list" in result.output

    def test_keboola_import_reads_the_keboola_only_endpoint(self):
        with patch("cli.commands.admin_semantic.api_get", return_value=_resp(200, {"sources": []})) as m:
            result = runner.invoke(app, ["admin", "semantic", "keboola-import"])
        assert result.exit_code == 0
        assert m.call_args.args[0] == "/api/admin/semantic-layer/coverage"

    def test_the_two_coverage_reports_explain_which_question_they_answer(self):
        """Both reviews flagged that `coverage` and `coverage tables` read as
        the same report. The helps must name the different question AND the
        different endpoint."""
        grid = runner.invoke(app, ["admin", "semantic", "coverage", "--help"]).output
        tables = runner.invoke(app, ["admin", "semantic", "coverage", "tables", "--help"]).output
        assert "/api/admin/semantic-model/coverage" in grid
        assert "/api/admin/semantic-coverage" in tables
        assert "keboola-import" in grid or "keboola-import" in tables

    def test_health_reads_the_admin_endpoint(self):
        with patch("cli.commands.admin_semantic.api_get", return_value=_resp(200, {})) as m:
            result = runner.invoke(app, ["admin", "semantic", "health"])
        assert result.exit_code == 0
        assert m.call_args.args[0] == "/api/admin/semantic-layer/health"


# ---------------------------------------------------------------------------
# 5. Backward compatibility — every old path still works, and says so
# ---------------------------------------------------------------------------


_DEPRECATED = "Deprecated"


class TestDeprecatedAliases:
    def test_admin_semantic_model_list_delegates_and_warns(self):
        with patch("cli.commands.admin_semantic.api_get", return_value=_resp(200, [_MODEL_ROW])):
            result = runner.invoke(app, ["admin", "semantic-model", "list", "--json"])
        assert result.exit_code == 0
        assert _DEPRECATED in result.output
        assert "agnes admin semantic" in result.output

    def test_admin_semantic_source_list_delegates_and_warns(self):
        with patch("cli.commands.admin_semantic.api_get", return_value=_resp(200, [])):
            result = runner.invoke(app, ["admin", "semantic-source", "list", "--json"])
        assert result.exit_code == 0
        assert _DEPRECATED in result.output
        assert "agnes admin semantic source" in result.output

    def test_admin_semantic_layer_coverage_delegates_and_warns(self):
        with patch("cli.commands.admin_semantic.api_get", return_value=_resp(200, {"sources": []})):
            result = runner.invoke(app, ["admin", "semantic-layer", "coverage"])
        assert result.exit_code == 0
        assert _DEPRECATED in result.output
        assert "agnes admin semantic keboola-import" in result.output

    def test_admin_semantic_model_export_points_at_the_user_group(self):
        with patch("cli.commands.semantic_model.api_get", return_value=_resp(200, text=DOC)):
            result = runner.invoke(app, ["admin", "semantic-model", "export", "retail"])
        assert result.exit_code == 0
        assert _DEPRECATED in result.output
        assert "agnes semantic-model export" in result.output
        # ONE notice, naming ONE destination. `export` did not follow its group
        # to `admin semantic`, so a generic group-level "the group moved" line
        # printed alongside would send the reader to the wrong place.
        assert result.output.count(_DEPRECATED) == 1
        assert "admin semantic export" not in result.output

    def test_each_alias_names_the_command_that_was_actually_run(self):
        """"The group moved" is not an instruction — the line has to name the
        command the user just typed and its replacement."""
        with patch("cli.commands.admin_semantic.api_post", return_value=_resp(200, {"package_ids": []})):
            result = runner.invoke(app, ["admin", "semantic-model", "link-package", "retail", "pkg_1"])
        assert result.exit_code == 0
        assert "agnes admin semantic-model link-package" in result.output
        assert "agnes admin semantic link-package" in result.output

    def test_admin_semantic_model_validate_points_at_the_user_group(self, tmp_path):
        p = tmp_path / "m.yaml"
        p.write_text(DOC)
        result = runner.invoke(app, ["admin", "semantic-model", "validate", str(p)])
        assert result.exit_code == 0
        assert _DEPRECATED in result.output
        assert "agnes semantic-model validate" in result.output

    def test_user_group_health_alias_delegates_and_warns(self):
        with patch("cli.commands.admin_semantic.api_get", return_value=_resp(200, {})):
            result = runner.invoke(app, ["semantic-model", "health"])
        assert result.exit_code == 0
        assert _DEPRECATED in result.output
        assert "agnes admin semantic health" in result.output

    def test_user_group_mutes_alias_delegates_and_warns(self):
        with patch("cli.commands.admin_semantic.api_get", return_value=_resp(200, {"items": []})):
            result = runner.invoke(app, ["semantic-model", "mutes"])
        assert result.exit_code == 0
        assert _DEPRECATED in result.output
        assert "agnes admin semantic mutes" in result.output

    def test_user_group_unmute_alias_delegates_and_warns(self):
        with patch("cli.commands.admin_semantic.api_delete", return_value=_resp(204)):
            result = runner.invoke(app, ["semantic-model", "unmute", "mute_1"])
        assert result.exit_code == 0
        assert _DEPRECATED in result.output
        assert "agnes admin semantic unmute" in result.output

    def test_user_group_bare_coverage_alias_delegates_and_warns(self):
        with patch("cli.commands.admin_semantic.api_get", return_value=_resp(200, {"sources": []})):
            result = runner.invoke(app, ["semantic-model", "coverage"])
        assert result.exit_code == 0
        assert _DEPRECATED in result.output
        assert "agnes admin semantic coverage" in result.output

    def test_user_group_coverage_tables_alias_delegates_and_warns(self):
        with patch("cli.commands.admin_semantic.api_get", return_value=_resp(200, {"tables": []})):
            result = runner.invoke(app, ["semantic-model", "coverage", "tables"])
        assert result.exit_code == 0
        assert _DEPRECATED in result.output
        assert "agnes admin semantic coverage tables" in result.output

    def test_every_alias_is_hidden_from_help(self):
        tree = _tree()
        for path in (
            "semantic-model coverage",
            "semantic-model health",
            "semantic-model mute",
            "semantic-model mutes",
            "semantic-model unmute",
        ):
            assert path in tree, f"{path} must survive as an alias for one release"
            assert tree[path].hidden is True, f"{path} must be hidden from --help"

    def test_the_notice_goes_to_stderr_so_json_stays_pipeable(self):
        """A rename must not break `… --json | jq`."""
        with patch("cli.commands.admin_semantic.api_get", return_value=_resp(200, [_MODEL_ROW])):
            result = runner.invoke(
                app, ["admin", "semantic-model", "list", "--json"], catch_exceptions=False
            )
        # CliRunner merges the streams into `.output`; `.stdout` is stdout only.
        assert _DEPRECATED not in result.stdout
        json.loads(result.stdout)


# ---------------------------------------------------------------------------
# 6. `agnes admin data-semantics` is gone outright — no alias
# ---------------------------------------------------------------------------


def test_data_semantics_group_is_removed_with_no_alias():
    """A pre-Ossie scaffolder whose output nothing reads. Removed, not aliased:
    an alias would keep teaching a path that leads nowhere."""
    assert "admin data-semantics" not in _tree()
    result = runner.invoke(app, ["admin", "data-semantics", "generate", "/tmp/x"])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# 7. The stdio MCP server stops being an advertised end-user surface
# ---------------------------------------------------------------------------


def test_agnes_mcp_is_hidden_but_still_registered():
    """Retired as a *documented surface*, not as a program: the hosted chat
    sandbox spawns `agnes mcp` per session (app/chat/runner.py) and
    `agnes global enable` wires it into Claude Code's user scope. Hiding it
    stops `agnes --help` teaching it as an end-user path; removing it would
    break the chat runner."""
    tree = _tree()
    assert "mcp" in tree
    assert tree["mcp"].hidden is True


def test_agnes_mcp_help_says_it_is_internal():
    result = runner.invoke(app, ["mcp", "--help"])
    assert result.exit_code == 0
    assert "internal" in result.output.lower()
