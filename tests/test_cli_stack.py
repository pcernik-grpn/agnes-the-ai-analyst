"""CLI tests for `agnes stack {list,add,remove}`.

Server side is fully mocked via ``cli.commands.stack.api_get/api_post/
api_delete`` — these tests verify the request shape (URL + body) and the
human-readable rendering of the typed error codes the API returns
(``already_required``, ``no_grant``, ``cannot_remove_required``).
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
    (tmp_path / "config").mkdir()
    yield tmp_path


def _resp(status_code=200, json_data=None, text=""):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data if json_data is not None else {}
    r.text = text
    return r


class TestStackList:
    def test_list_with_explicit_type(self):
        body = {
            "items": [
                {
                    "id": "pkg_sales",
                    "name": "Sales",
                    "description": "Orders + line items",
                    "requirement": "available",
                    "in_stack": True,
                }
            ]
        }
        with patch("cli.commands.stack.api_get", return_value=_resp(200, body)) as m:
            result = runner.invoke(app, ["stack", "list", "--type", "data_package"])
        assert result.exit_code == 0
        # URL contract — the call hits /api/stack?type=data_package
        args, kwargs = m.call_args
        assert args[0] == "/api/stack"
        assert kwargs["params"] == {"type": "data_package"}
        assert "Sales" in result.output
        assert "optional" in result.output

    def test_list_without_type_fetches_both(self):
        with patch(
            "cli.commands.stack.api_get",
            return_value=_resp(200, {"items": []}),
        ) as m:
            result = runner.invoke(app, ["stack", "list"])
        assert result.exit_code == 0
        # Two calls — one per supported type.
        calls = m.call_args_list
        types_called = sorted([c.kwargs["params"]["type"] for c in calls])
        assert types_called == ["data_package", "memory_domain"]
        assert "empty" in result.output.lower()

    def test_list_json_output(self):
        body = {"items": [{"id": "pkg_a", "name": "A", "requirement": "required", "in_stack": True}]}
        with patch("cli.commands.stack.api_get", return_value=_resp(200, body)):
            result = runner.invoke(app, ["stack", "list", "--type", "data_package", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data[0]["id"] == "pkg_a"
        assert data[0]["type"] == "data_package"

    def test_list_plugin_type_rejected(self):
        # Plugins keep the marketplace flow; --type plugin is out of scope.
        result = runner.invoke(app, ["stack", "list", "--type", "plugin"])
        assert result.exit_code == 2
        assert "marketplace" in result.output.lower()

    def test_list_unknown_type_rejected(self):
        result = runner.invoke(app, ["stack", "list", "--type", "garbage"])
        assert result.exit_code == 2


class TestStackBrowse:
    def test_browse_with_explicit_type(self):
        body = {
            "items": [
                {
                    "id": "pkg_sales",
                    "name": "Sales",
                    "description": "Orders + line items",
                    "requirement": "available",
                    "in_stack": False,
                },
                {
                    "id": "pkg_core",
                    "name": "Core",
                    "description": "Always on",
                    "requirement": "required",
                    "in_stack": True,
                },
            ]
        }
        with patch("cli.commands.stack.api_get", return_value=_resp(200, body)) as m:
            result = runner.invoke(app, ["stack", "browse", "--type", "data_package"])
        assert result.exit_code == 0
        # URL contract — hits /api/stack/browse?type=data_package
        args, kwargs = m.call_args
        assert args[0] == "/api/stack/browse"
        assert kwargs["params"] == {"type": "data_package"}
        # Table renders the IN STACK column + the ✓ for the required row.
        assert "IN STACK" in result.output
        assert "Sales" in result.output
        assert "✓" in result.output

    def test_browse_prints_the_id_that_add_requires(self):
        """`agnes stack add <type> <id>` takes an id the table never showed.

        Live symptom (analyst persona): browse printed NAME / TYPE /
        REQUIREMENT / IN STACK / DESCRIPTION, so the reader guessed the web
        slug, got "Access denied: your groups have no grant on
        data_package/northwind-orders. Ask an admin to grant it." — which was
        false; the grant was there and the id was wrong. The real id came from
        calling /api/stack/browse by hand. Names are display strings and the
        one on that instance contained an em dash, so it was not even
        retypeable.
        """
        body = {
            "items": [
                {
                    "id": "pkg_1050485f3d7e",
                    "name": "Sales — Northwind Orders",
                    "description": "1200 order lines",
                    "requirement": "available",
                    "in_stack": False,
                },
            ]
        }
        with patch("cli.commands.stack.api_get", return_value=_resp(200, body)):
            result = runner.invoke(app, ["stack", "browse", "--type", "data_package"])
        assert result.exit_code == 0
        assert "ID" in result.output
        assert "pkg_1050485f3d7e" in result.output, (
            "browse must print the id its sibling `stack add` demands"
        )

    def test_browse_without_type_fetches_both(self):
        with patch(
            "cli.commands.stack.api_get",
            return_value=_resp(200, {"items": []}),
        ) as m:
            result = runner.invoke(app, ["stack", "browse"])
        assert result.exit_code == 0
        types_called = sorted([c.kwargs["params"]["type"] for c in m.call_args_list])
        assert types_called == ["data_package", "memory_domain"]

    def test_browse_json_output(self):
        body = {"items": [{"id": "pkg_a", "name": "A", "requirement": "available", "in_stack": False}]}
        with patch("cli.commands.stack.api_get", return_value=_resp(200, body)):
            result = runner.invoke(app, ["stack", "browse", "--type", "data_package", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data[0]["id"] == "pkg_a"
        assert data[0]["type"] == "data_package"
        assert data[0]["in_stack"] is False

    def test_browse_plugin_type_rejected(self):
        result = runner.invoke(app, ["stack", "browse", "--type", "plugin"])
        assert result.exit_code == 2
        assert "marketplace" in result.output.lower()


class TestStackAdd:
    def test_add_calls_subscribe_endpoint(self):
        with patch("cli.commands.stack.api_post", return_value=_resp(200, {"subscribed": True})) as m:
            result = runner.invoke(app, ["stack", "add", "data_package", "pkg_sales"])
        assert result.exit_code == 0
        args, kwargs = m.call_args
        assert args[0] == "/api/stack/subscribe"
        assert kwargs["json"] == {
            "resource_type": "data_package",
            "resource_id": "pkg_sales",
        }
        # Mode-neutral success copy: classic (the default) joins the stack,
        # auto-membership only requests the local copy — the echo leads with
        # the membership action and points at `agnes pull` either way.
        assert "Added data_package/pkg_sales to your stack" in result.output
        assert "agnes pull" in result.output

    def test_add_memory_domain(self):
        with patch("cli.commands.stack.api_post", return_value=_resp(200, {"subscribed": True})) as m:
            result = runner.invoke(app, ["stack", "add", "memory_domain", "dom_x"])
        assert result.exit_code == 0
        assert m.call_args.kwargs["json"]["resource_type"] == "memory_domain"

    def test_add_already_required_is_soft_success(self):
        with patch(
            "cli.commands.stack.api_post",
            return_value=_resp(400, {"detail": "already_required"}),
        ):
            result = runner.invoke(app, ["stack", "add", "data_package", "pkg_sales"])
        # Already required = no-op, exit 0, message on stderr.
        assert result.exit_code == 0
        assert "already required" in result.output.lower()

    def test_add_no_grant_surfaces_hint(self):
        with patch(
            "cli.commands.stack.api_post",
            return_value=_resp(403, {"detail": "no_grant"}),
        ):
            result = runner.invoke(app, ["stack", "add", "data_package", "pkg_sales"])
        assert result.exit_code == 1
        assert "Access denied" in result.output
        assert "admin" in result.output.lower()

    def test_add_plugin_rejected(self):
        result = runner.invoke(app, ["stack", "add", "plugin", "p1"])
        assert result.exit_code == 2

    def test_add_unknown_type_rejected(self):
        result = runner.invoke(app, ["stack", "add", "spaceship", "x"])
        assert result.exit_code == 2


class TestStackRemove:
    def test_remove_calls_subscription_endpoint(self):
        with patch(
            "cli.commands.stack.api_delete",
            return_value=_resp(200, {"subscribed": False}),
        ) as m:
            result = runner.invoke(app, ["stack", "remove", "data_package", "pkg_sales"])
        assert result.exit_code == 0
        args, _ = m.call_args
        assert args[0] == "/api/stack/subscription/data_package/pkg_sales"
        assert "Removed" in result.output

    def test_remove_required_surfaces_hint(self):
        with patch(
            "cli.commands.stack.api_delete",
            return_value=_resp(400, {"detail": "cannot_remove_required"}),
        ):
            result = runner.invoke(app, ["stack", "remove", "data_package", "pkg_sales"])
        assert result.exit_code == 1
        assert "required" in result.output.lower()
        assert "admin" in result.output.lower()
