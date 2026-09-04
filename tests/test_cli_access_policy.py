"""CLI surface for table access policies (design doc §13.2, plan Task 16).

`agnes admin update-table --policy/--policy-note/--policy-mapping` attaches,
replaces, or clears the policy (mirrors the existing `--query` `@path/to.sql`
precedent, but --policy requires the file form -- policies are typically
multi-line). `agnes admin table-policy show|preview` is read-only inspection:
the stored policy, and a single-persona dry-run against
`POST /api/admin/registry/{id}/policy/preview` (design doc §13.1) -- the
surface Task 14's EXEMPT classification for that endpoint names.

Mirrors the mock-`api_*`-function harness in `tests/test_cli_admin.py`
(`TestUpdateTable`) rather than a live TestClient: these are CLI-shape tests
(right payload to the right path, right output/exit code for a given API
response). The server-side contract (validator rules, preview matrix,
mandatory-note enforcement) is covered by
`tests/test_admin_access_policy_api.py` and
`tests/test_journey_access_policy_interlock.py`.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
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


def _registry_resp(tables):
    return _resp(200, {"tables": tables, "count": len(tables)})


class TestUpdateTablePolicyFlag:
    def test_policy_at_file_sets_sql_and_note(self, tmp_path):
        sql_file = tmp_path / "policy.sql"
        sql_file.write_text("SELECT * FROM invoices WHERE list_contains($user_groups, unit)\n")
        captured = {}

        def fake_put(path, **kwargs):
            captured["path"] = path
            captured["json"] = kwargs.get("json")
            return _resp(200, {"id": "invoices", "updated": ["access_policy_sql", "access_policy_note"]})

        with patch("cli.commands.admin.api_put", side_effect=fake_put):
            result = runner.invoke(
                app,
                [
                    "admin",
                    "update-table",
                    "invoices",
                    "--policy",
                    f"@{sql_file}",
                    "--policy-note",
                    "restrict to the caller's unit",
                ],
            )
        assert result.exit_code == 0, result.output
        assert captured["path"] == "/api/admin/registry/invoices"
        assert captured["json"]["access_policy_sql"] == "SELECT * FROM invoices WHERE list_contains($user_groups, unit)"
        assert captured["json"]["access_policy_note"] == "restrict to the caller's unit"

    def test_policy_missing_file_errors_before_round_trip(self, tmp_path):
        with patch("cli.commands.admin.api_put") as mock_put:
            result = runner.invoke(
                app,
                ["admin", "update-table", "invoices", "--policy", f"@{tmp_path / 'nope.sql'}"],
            )
        assert result.exit_code == 2
        mock_put.assert_not_called()

    def test_policy_inline_sql_is_rejected(self):
        """Unlike --query, --policy requires @path/to.sql -- design doc
        §13.2: "nobody pastes multi-line SQL into a shell"."""
        with patch("cli.commands.admin.api_put") as mock_put:
            result = runner.invoke(
                app,
                ["admin", "update-table", "invoices", "--policy", "SELECT 1", "--policy-note", "x"],
            )
        assert result.exit_code == 2
        mock_put.assert_not_called()
        assert "@path/to.sql" in result.output

    def test_empty_policy_clears(self):
        captured = {}

        def fake_put(path, **kwargs):
            captured["json"] = kwargs.get("json")
            return _resp(200, {"id": "invoices", "updated": ["access_policy_sql"]})

        with patch("cli.commands.admin.api_put", side_effect=fake_put):
            result = runner.invoke(app, ["admin", "update-table", "invoices", "--policy="])
        assert result.exit_code == 0, result.output
        assert captured["json"] == {"access_policy_sql": None}

    def test_policy_without_note_surfaces_the_server_hint(self, tmp_path):
        sql_file = tmp_path / "policy.sql"
        sql_file.write_text("SELECT 1")
        with patch(
            "cli.commands.admin.api_put",
            return_value=_resp(
                422,
                {
                    "detail": (
                        "policy_note_required: access_policy_note is required whenever "
                        "access_policy_sql is set -- explain why this policy exists"
                    )
                },
                text="policy_note_required",
            ),
        ):
            result = runner.invoke(app, ["admin", "update-table", "invoices", "--policy", f"@{sql_file}"])
        assert result.exit_code == 1
        assert "policy_note_required" in result.output

    def test_policy_rejected_on_non_server_only_hints_server_only_flag(self, tmp_path):
        sql_file = tmp_path / "policy.sql"
        sql_file.write_text("SELECT 1")
        with patch(
            "cli.commands.admin.api_put",
            return_value=_resp(
                422,
                {"detail": "access_policy_requires_undistributed: set server_only=true first"},
                text="access_policy_requires_undistributed",
            ),
        ):
            result = runner.invoke(
                app,
                ["admin", "update-table", "invoices", "--policy", f"@{sql_file}", "--policy-note", "x"],
            )
        assert result.exit_code == 1
        assert "update-table invoices --server-only" in result.output

    def test_policy_mapping_flag_sets_true(self):
        captured = {}

        def fake_put(path, **kwargs):
            captured["json"] = kwargs.get("json")
            return _resp(200, {"id": "user_access", "updated": ["policy_mapping"]})

        with patch("cli.commands.admin.api_put", side_effect=fake_put):
            result = runner.invoke(app, ["admin", "update-table", "user_access", "--policy-mapping"])
        assert result.exit_code == 0, result.output
        assert captured["json"] == {"policy_mapping": True}

    def test_no_policy_mapping_flag_sets_false(self):
        captured = {}

        def fake_put(path, **kwargs):
            captured["json"] = kwargs.get("json")
            return _resp(200, {"id": "user_access", "updated": ["policy_mapping"]})

        with patch("cli.commands.admin.api_put", side_effect=fake_put):
            result = runner.invoke(app, ["admin", "update-table", "user_access", "--no-policy-mapping"])
        assert result.exit_code == 0, result.output
        assert captured["json"] == {"policy_mapping": False}

    def test_server_only_flag_reaches_payload(self):
        captured = {}

        def fake_put(path, **kwargs):
            captured["json"] = kwargs.get("json")
            return _resp(200, {"id": "invoices", "updated": ["server_only"]})

        with patch("cli.commands.admin.api_put", side_effect=fake_put):
            result = runner.invoke(app, ["admin", "update-table", "invoices", "--server-only"])
        assert result.exit_code == 0, result.output
        assert captured["json"] == {"server_only": True}

    def test_no_server_only_flag_sets_false(self):
        captured = {}

        def fake_put(path, **kwargs):
            captured["json"] = kwargs.get("json")
            return _resp(200, {"id": "invoices", "updated": ["server_only"]})

        with patch("cli.commands.admin.api_put", side_effect=fake_put):
            result = runner.invoke(app, ["admin", "update-table", "invoices", "--no-server-only"])
        assert result.exit_code == 0, result.output
        assert captured["json"] == {"server_only": False}

    def test_omitted_new_flags_leave_payload_unchanged(self):
        """None of this task's new fields go in the body unless the operator
        passes them -- same contract `TestUpdateTable::test_update_only_supplied_fields_sent`
        already pins for the pre-existing fields."""
        captured = {}

        def fake_put(path, **kwargs):
            captured["json"] = kwargs.get("json")
            return _resp(200, {"id": "invoices", "updated": ["bucket"]})

        with patch("cli.commands.admin.api_put", side_effect=fake_put):
            result = runner.invoke(app, ["admin", "update-table", "invoices", "--bucket", "x"])
        assert result.exit_code == 0, result.output
        assert captured["json"] == {"bucket": "x"}


class TestTablePolicyShow:
    def test_show_prints_policy_text(self):
        tables = [
            {
                "id": "invoices",
                "name": "invoices",
                "access_policy_sql": "SELECT * FROM invoices WHERE list_contains($user_groups, unit)",
                "access_policy_note": "restrict to unit",
                "access_policy_updated_by": "admin@x.com",
                "access_policy_updated_at": "2026-08-11T00:00:00",
                "policy_mapping": False,
            }
        ]
        with patch("cli.commands.admin.api_get", return_value=_registry_resp(tables)):
            result = runner.invoke(app, ["admin", "table-policy", "show", "invoices"])
        assert result.exit_code == 0, result.output
        assert "restrict to unit" in result.output
        assert "list_contains($user_groups, unit)" in result.output
        assert "admin@x.com" in result.output

    def test_show_json_is_clean_stdout(self):
        tables = [
            {
                "id": "invoices",
                "name": "invoices",
                "access_policy_sql": "SELECT 1",
                "access_policy_note": "note",
                "access_policy_updated_by": "admin@x.com",
                "access_policy_updated_at": "2026-08-11T00:00:00",
                "policy_mapping": True,
            }
        ]
        with patch("cli.commands.admin.api_get", return_value=_registry_resp(tables)):
            result = runner.invoke(app, ["admin", "table-policy", "show", "invoices", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)  # fails outright if anything else hit stdout
        assert data["access_policy_sql"] == "SELECT 1"
        assert data["policy_mapping"] is True

    def test_show_no_policy_attached(self):
        tables = [{"id": "orders", "name": "orders", "access_policy_sql": None, "policy_mapping": False}]
        with patch("cli.commands.admin.api_get", return_value=_registry_resp(tables)):
            result = runner.invoke(app, ["admin", "table-policy", "show", "orders"])
        assert result.exit_code == 0, result.output
        assert "no access policy" in result.output.lower()

    def test_show_unknown_table(self):
        with patch("cli.commands.admin.api_get", return_value=_registry_resp([])):
            result = runner.invoke(app, ["admin", "table-policy", "show", "nope"])
        assert result.exit_code == 1


class TestTablePolicyShowMappingStatus:
    """#2147: `policy_mapping_status` -- the read-only, per-mapping-table
    health `GET /api/admin/registry` now attaches to a policied row -- is
    rendered by `table-policy show`, both human and `--json`."""

    def test_broken_mapping_table_is_flagged(self):
        tables = [
            {
                "id": "invoices",
                "name": "invoices",
                "access_policy_sql": "SELECT * FROM invoices WHERE unit IN (SELECT unit FROM user_access)",
                "access_policy_note": "restrict to unit",
                "access_policy_updated_by": "admin@x.com",
                "access_policy_updated_at": "2026-08-11T00:00:00",
                "policy_mapping": False,
                "policy_mapping_status": [{"mapping_table": "user_access", "state": "never_synced", "last_sync": None}],
            }
        ]
        with patch("cli.commands.admin.api_get", return_value=_registry_resp(tables)):
            result = runner.invoke(app, ["admin", "table-policy", "show", "invoices"])
        assert result.exit_code == 0, result.output
        assert "mapping status" in result.output.lower()
        assert "user_access: never_synced" in result.output
        assert "last_sync: never" in result.output

    def test_healthy_mapping_table_is_shown_without_a_broken_flag(self):
        tables = [
            {
                "id": "invoices",
                "name": "invoices",
                "access_policy_sql": "SELECT * FROM invoices",
                "access_policy_note": "note",
                "access_policy_updated_by": "admin@x.com",
                "access_policy_updated_at": "2026-08-11T00:00:00",
                "policy_mapping": False,
                "policy_mapping_status": [
                    {"mapping_table": "user_access2", "state": "ok", "last_sync": "2026-08-11T00:00:00"}
                ],
            }
        ]
        with patch("cli.commands.admin.api_get", return_value=_registry_resp(tables)):
            result = runner.invoke(app, ["admin", "table-policy", "show", "invoices"])
        assert result.exit_code == 0, result.output
        assert "user_access2: ok" in result.output
        assert "broken" not in result.output.lower()

    def test_json_carries_the_field(self):
        tables = [
            {
                "id": "invoices",
                "name": "invoices",
                "access_policy_sql": "SELECT 1",
                "access_policy_note": "note",
                "access_policy_updated_by": "admin@x.com",
                "access_policy_updated_at": "2026-08-11T00:00:00",
                "policy_mapping": False,
                "policy_mapping_status": [{"mapping_table": "user_access", "state": "empty", "last_sync": None}],
            }
        ]
        with patch("cli.commands.admin.api_get", return_value=_registry_resp(tables)):
            result = runner.invoke(app, ["admin", "table-policy", "show", "invoices", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["policy_mapping_status"] == [{"mapping_table": "user_access", "state": "empty", "last_sync": None}]

    def test_no_status_field_when_the_table_has_no_dependency(self):
        """A policied row with no `policy_mapping` dependency at all carries
        no `policy_mapping_status` key (`GET /api/admin/registry` only adds
        it for a policied row)."""
        tables = [
            {
                "id": "invoices",
                "name": "invoices",
                "access_policy_sql": "SELECT * FROM invoices",
                "access_policy_note": "note",
                "access_policy_updated_by": "admin@x.com",
                "access_policy_updated_at": "2026-08-11T00:00:00",
                "policy_mapping": False,
            }
        ]
        with patch("cli.commands.admin.api_get", return_value=_registry_resp(tables)):
            result = runner.invoke(app, ["admin", "table-policy", "show", "invoices"])
        assert result.exit_code == 0, result.output
        assert "mapping status" not in result.output.lower()


class TestTablePolicyPreview:
    def test_preview_as_groups_prints_row_counts(self):
        captured = {}

        def fake_post(path, **kwargs):
            captured["path"] = path
            captured["json"] = kwargs.get("json")
            return _resp(
                200,
                {
                    "columns": [{"name": "id", "hidden": False}, {"name": "secret", "hidden": True}],
                    "sample_rows": [{"id": "1"}],
                    "rows_visible": 2,
                    "rows_total": 3,
                },
            )

        with patch("cli.commands.admin.api_post", side_effect=fake_post):
            result = runner.invoke(app, ["admin", "table-policy", "preview", "invoices", "--as-groups", "Finance,Ops"])
        assert result.exit_code == 0, result.output
        assert captured["path"] == "/api/admin/registry/invoices/policy/preview"
        assert captured["json"] == {"as_groups": ["Finance", "Ops"]}
        assert "2" in result.output
        assert "3" in result.output

    def test_preview_as_user(self):
        captured = {}

        def fake_post(path, **kwargs):
            captured["json"] = kwargs.get("json")
            return _resp(200, {"columns": [], "sample_rows": [], "rows_visible": 1, "rows_total": 1})

        with patch("cli.commands.admin.api_post", side_effect=fake_post):
            result = runner.invoke(app, ["admin", "table-policy", "preview", "invoices", "--as", "alice@x.com"])
        assert result.exit_code == 0, result.output
        assert captured["json"] == {"as_user": "alice@x.com"}

    def test_preview_requires_exactly_one_persona_flag_neither(self):
        with patch("cli.commands.admin.api_post") as mock_post:
            result = runner.invoke(app, ["admin", "table-policy", "preview", "invoices"])
        assert result.exit_code == 2
        mock_post.assert_not_called()

    def test_preview_requires_exactly_one_persona_flag_both(self):
        with patch("cli.commands.admin.api_post") as mock_post:
            result = runner.invoke(
                app,
                [
                    "admin",
                    "table-policy",
                    "preview",
                    "invoices",
                    "--as",
                    "alice@x.com",
                    "--as-groups",
                    "Finance",
                ],
            )
        assert result.exit_code == 2
        mock_post.assert_not_called()

    def test_preview_json_clean_stdout(self):
        body = {"columns": [], "sample_rows": [], "rows_visible": 1, "rows_total": 1}
        with patch("cli.commands.admin.api_post", return_value=_resp(200, body)):
            result = runner.invoke(
                app, ["admin", "table-policy", "preview", "invoices", "--as-groups", "Finance", "--json"]
            )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == body

    def test_preview_sql_candidate_at_file(self, tmp_path):
        sql_file = tmp_path / "candidate.sql"
        sql_file.write_text("SELECT 1")
        captured = {}

        def fake_post(path, **kwargs):
            captured["json"] = kwargs.get("json")
            return _resp(200, {"columns": [], "sample_rows": [], "rows_visible": 0, "rows_total": 0})

        with patch("cli.commands.admin.api_post", side_effect=fake_post):
            result = runner.invoke(
                app,
                [
                    "admin",
                    "table-policy",
                    "preview",
                    "invoices",
                    "--sql",
                    f"@{sql_file}",
                    "--as-groups",
                    "Finance",
                ],
            )
        assert result.exit_code == 0, result.output
        assert captured["json"]["sql"] == "SELECT 1"

    def test_preview_sql_inline_rejected(self):
        with patch("cli.commands.admin.api_post") as mock_post:
            result = runner.invoke(
                app,
                ["admin", "table-policy", "preview", "invoices", "--sql", "SELECT 1", "--as-groups", "Finance"],
            )
        assert result.exit_code == 2
        mock_post.assert_not_called()

    def test_zero_rows_visible_distinguishes_empty_slice_from_mapping(self):
        body = {"columns": [], "sample_rows": [], "rows_visible": 0, "rows_total": 3}
        with patch("cli.commands.admin.api_post", return_value=_resp(200, body)):
            result = runner.invoke(app, ["admin", "table-policy", "preview", "invoices", "--as-groups", "Nobody"])
        assert result.exit_code == 0, result.output
        assert "empty slice" in result.output
        assert "mapping" in result.output
        assert "unresolvable" in result.output.lower()

    def test_zero_rows_visible_no_note_when_table_itself_is_empty(self):
        body = {"columns": [], "sample_rows": [], "rows_visible": 0, "rows_total": 0}
        with patch("cli.commands.admin.api_post", return_value=_resp(200, body)):
            result = runner.invoke(app, ["admin", "table-policy", "preview", "invoices", "--as-groups", "Nobody"])
        assert result.exit_code == 0, result.output
        assert "empty slice" not in result.output

    def test_preview_api_error_surfaces_detail(self):
        with patch(
            "cli.commands.admin.api_post",
            return_value=_resp(
                422,
                {"detail": "policy_preview_no_policy: no stored or candidate policy"},
                text="policy_preview_no_policy",
            ),
        ):
            result = runner.invoke(app, ["admin", "table-policy", "preview", "orders", "--as-groups", "X"])
        assert result.exit_code == 1
        assert "policy_preview_no_policy" in result.output


class TestTablePolicyPreviewSurfaceRefusal:
    """F2 (#1979, security review): the preview endpoint is gated by
    ``require_admin_all_surface``, so an admin running it from an
    ``agnes init``-ed workspace (a ``surface='stack'`` PAT) gets a 403 whose
    detail names the fix. The CLI must show that detail rather than a bare
    "Failed" -- the credential, not the policy, is what needs changing."""

    def test_the_403_detail_reaches_the_terminal(self):
        detail = (
            "This endpoint requires an admin credential with the full "
            "('all') data-read surface — a surface='stack' PAT (the "
            "`agnes init` default) is filtered like an analyst here. "
            "Use a browser session, a regular PAT, or "
            "`agnes init --as-admin`."
        )

        with patch("cli.commands.admin.api_post", side_effect=lambda *a, **k: _resp(403, {"detail": detail})):
            result = runner.invoke(app, ["admin", "table-policy", "preview", "invoices", "--as-groups", "Finance"])

        assert result.exit_code == 1, result.output
        assert "agnes init --as-admin" in result.output
        assert "data-read surface" in result.output


class TestTablePolicyPreviewMatrix:
    """``agnes admin table-policy preview <id> --matrix`` (design doc §13.1,
    issue #2147) -- calls `POST .../policy/preview-matrix` instead of the
    single-persona endpoint. No persona flag is accepted: the matrix
    enumerates its own personas.
    """

    def _matrix_body(self):
        return {
            "rows_total": 3,
            "personas": [
                {
                    "kind": "group_set",
                    "label": "Finance",
                    "groups": ["Finance"],
                    "rows_visible": 2,
                    "rows_total": 3,
                    "hidden_columns": ["secret"],
                    "masked_columns": [],
                },
                {
                    "kind": "group_set",
                    "label": "Ops",
                    "groups": ["Ops"],
                    "rows_visible": 1,
                    "rows_total": 3,
                    "hidden_columns": ["secret"],
                    "masked_columns": [],
                },
            ],
            "union_coverage": 1.0,
            "no_op": False,
            "pairwise_overlap": [
                {"persona_a": "Finance", "persona_b": "Ops", "overlap_rows": 0, "overlap_fraction": 0.0}
            ],
            "identity_columns": ["id"],
            "truncated": False,
            "transpiled": None,
            "mapping_warning": None,
        }

    def test_matrix_calls_the_matrix_endpoint(self):
        captured = {}

        def fake_post(path, **kwargs):
            captured["path"] = path
            captured["json"] = kwargs.get("json")
            return _resp(200, self._matrix_body())

        with patch("cli.commands.admin.api_post", side_effect=fake_post):
            result = runner.invoke(app, ["admin", "table-policy", "preview", "invoices", "--matrix"])
        assert result.exit_code == 0, result.output
        assert captured["path"] == "/api/admin/registry/invoices/policy/preview-matrix"
        assert captured["json"] == {"personas": "both"}
        assert "Finance" in result.output
        assert "Ops" in result.output
        assert "union coverage: 100%" in result.output

    def test_matrix_json_clean_stdout(self):
        body = self._matrix_body()
        with patch("cli.commands.admin.api_post", return_value=_resp(200, body)):
            result = runner.invoke(app, ["admin", "table-policy", "preview", "invoices", "--matrix", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == body

    def test_matrix_personas_and_limit_forwarded(self):
        captured = {}

        def fake_post(path, **kwargs):
            captured["json"] = kwargs.get("json")
            return _resp(200, self._matrix_body())

        with patch("cli.commands.admin.api_post", side_effect=fake_post):
            result = runner.invoke(
                app,
                [
                    "admin",
                    "table-policy",
                    "preview",
                    "invoices",
                    "--matrix",
                    "--personas",
                    "policy_groups",
                    "--limit",
                    "10",
                ],
            )
        assert result.exit_code == 0, result.output
        assert captured["json"] == {"personas": "policy_groups", "limit": 10}

    def test_matrix_rejects_as_user(self):
        with patch("cli.commands.admin.api_post") as mock_post:
            result = runner.invoke(
                app, ["admin", "table-policy", "preview", "invoices", "--matrix", "--as", "alice@x.com"]
            )
        assert result.exit_code == 2
        mock_post.assert_not_called()

    def test_matrix_rejects_as_groups(self):
        with patch("cli.commands.admin.api_post") as mock_post:
            result = runner.invoke(
                app, ["admin", "table-policy", "preview", "invoices", "--matrix", "--as-groups", "Finance"]
            )
        assert result.exit_code == 2
        mock_post.assert_not_called()

    def test_matrix_rejects_an_unknown_personas_value(self):
        with patch("cli.commands.admin.api_post") as mock_post:
            result = runner.invoke(
                app, ["admin", "table-policy", "preview", "invoices", "--matrix", "--personas", "bogus"]
            )
        assert result.exit_code == 2
        mock_post.assert_not_called()

    def test_matrix_flags_a_no_op_policy(self):
        body = self._matrix_body()
        body["union_coverage"] = 1.0
        body["no_op"] = True
        for persona in body["personas"]:
            persona["rows_visible"] = persona["rows_total"]

        with patch("cli.commands.admin.api_post", return_value=_resp(200, body)):
            result = runner.invoke(app, ["admin", "table-policy", "preview", "invoices", "--matrix"])
        assert result.exit_code == 0, result.output
        assert "NO-OP" in result.output

    def test_matrix_mapping_warning_short_circuits_the_render(self):
        body = {
            "rows_total": None,
            "personas": [],
            "union_coverage": None,
            "no_op": None,
            "pairwise_overlap": [],
            "identity_columns": [],
            "truncated": False,
            "transpiled": None,
            "mapping_warning": "policy_mapping_empty: the mapping table has never synced",
        }
        with patch("cli.commands.admin.api_post", return_value=_resp(200, body)):
            result = runner.invoke(app, ["admin", "table-policy", "preview", "invoices", "--matrix"])
        assert result.exit_code == 0, result.output
        assert "policy_mapping_empty" in result.output

    def test_matrix_api_error_surfaces_detail(self):
        with patch(
            "cli.commands.admin.api_post",
            return_value=_resp(
                422,
                {"detail": "policy_preview_no_policy: no stored or candidate policy"},
                text="policy_preview_no_policy",
            ),
        ):
            result = runner.invoke(app, ["admin", "table-policy", "preview", "orders", "--matrix"])
        assert result.exit_code == 1
        assert "policy_preview_no_policy" in result.output
