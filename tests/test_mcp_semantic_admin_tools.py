"""The three semantic-layer admin families over MCP (#1707).

Sources (`add/list/sync/remove`), detach/reattach, and the Data Package link
pair had REST and CLI and no MCP tool at all. Nothing justified the gap —
CONTRIBUTING.md's only standing MCP exemptions are credential-provisioning
writes and security-posture diagnostics, and "an admin does it rarely" is
neither — so these eight tools mirror the endpoints the CLI already calls.

Each is a thin wrapper: the REST endpoint keeps its own admin gate, its own
validation and (for detach/reattach) its own Postgres-only refusal. What
these tests pin is that the wrapper calls the right endpoint with the right
payload, that the two confirmation flags cannot be defaulted away, and that a
refusal arrives as a refusal instead of a crash.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


def _run(coro):
    return asyncio.run(coro)


def _mock_resp(data: Any, status: int = 200) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json.return_value = data
    r.reason_phrase = "OK" if status < 400 else "Error"
    r.text = ""
    r.request = MagicMock()
    r.request.url = "http://testserver"
    r.raise_for_status = MagicMock()
    return r


def _mod():
    pytest.importorskip("mcp", reason="mcp package not installed")
    import app.api.mcp_http as mod

    return mod


class TestSemanticSourceTools:
    def test_add_posts_the_source_definition(self):
        mod = _mod()
        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            post = AsyncMock(return_value=_mock_resp({"id": "ss_1", "kind": "git"}, status=201))
            MC.return_value.__aenter__.return_value.post = post
            result = _run(
                mod.semantic_source_add(
                    name="Retail models",
                    kind="git",
                    adapter="native",
                    config={"repo_url": "https://example.com/models.git"},
                )
            )

        assert result["id"] == "ss_1"
        assert "/api/admin/semantic-sources" in post.call_args[0][0]
        assert post.call_args[1]["json"] == {
            "name": "Retail models",
            "kind": "git",
            "adapter": "native",
            "config": {"repo_url": "https://example.com/models.git"},
            "enabled": True,
        }

    def test_list_forwards_enabled_only_and_omits_it_otherwise(self):
        mod = _mod()
        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            get = AsyncMock(return_value=_mock_resp([{"id": "ss_1"}]))
            MC.return_value.__aenter__.return_value.get = get
            result = _run(mod.semantic_source_list())
            assert get.call_args[1]["params"] is None

            _run(mod.semantic_source_list(enabled_only=True))
            assert get.call_args[1]["params"] == {"enabled_only": "true"}

        # A bare list is not a valid MCP tool result — the rows are wrapped.
        assert result["sources"] == [{"id": "ss_1"}]

    def test_sync_posts_to_the_sources_sync_path(self):
        mod = _mod()
        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            post = AsyncMock(return_value=_mock_resp({"imported": 3, "pruned": 0}))
            MC.return_value.__aenter__.return_value.post = post
            result = _run(mod.semantic_source_sync("ss_1"))

        assert result["imported"] == 3
        assert "/api/admin/semantic-sources/ss_1/sync" in post.call_args[0][0]

    def test_remove_deletes_and_reports_what_it_removed(self):
        mod = _mod()
        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            delete = AsyncMock(return_value=_mock_resp(None, status=204))
            MC.return_value.__aenter__.return_value.delete = delete
            result = _run(mod.semantic_source_remove("ss_1"))

        assert result == {"deleted": "ss_1"}
        assert "/api/admin/semantic-sources/ss_1" in delete.call_args[0][0]


class TestDetachReattachTools:
    def test_confirmation_is_a_required_argument_not_a_default(self):
        """The REST endpoints refuse without it, which is the point: the model
        has to be TOLD to confirm by whoever is calling. A tool defaulting the
        flag to True would confirm on the user's behalf every time and turn a
        deliberate danger flow into a silent one."""
        mod = _mod()
        for fn, arg in (
            (mod.semantic_model_detach, "confirm_detach"),
            (mod.semantic_model_reattach, "confirm_reattach"),
        ):
            param = inspect.signature(fn).parameters[arg]
            assert param.default is inspect.Parameter.empty, f"{arg} must not carry a default"

    def test_detach_forwards_the_confirmation(self):
        mod = _mod()
        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            post = AsyncMock(return_value=_mock_resp({"slug": "retail", "sync_mode": "detached"}))
            MC.return_value.__aenter__.return_value.post = post
            result = _run(mod.semantic_model_detach("retail", True))

        assert result["sync_mode"] == "detached"
        assert "/api/admin/semantic-models/retail/detach" in post.call_args[0][0]
        assert post.call_args[1]["json"] == {"confirm_detach": True}

    def test_an_unconfirmed_detach_is_forwarded_unconfirmed(self):
        """`confirm_detach=False` must reach the endpoint as False so the
        endpoint's own `confirm_required` refusal is what answers — the tool
        never decides on its own that a caller "meant" yes."""
        mod = _mod()
        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            post = AsyncMock(
                return_value=_mock_resp(
                    {"detail": {"code": "confirm_required", "message": "detach requires confirm_detach=true"}},
                    status=400,
                )
            )
            MC.return_value.__aenter__.return_value.post = post
            with pytest.raises(httpx.HTTPStatusError) as exc:
                _run(mod.semantic_model_detach("retail", False))

        assert post.call_args[1]["json"] == {"confirm_detach": False}
        assert "confirm_required" in str(exc.value)

    def test_reattach_forwards_the_confirmation(self):
        mod = _mod()
        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            post = AsyncMock(return_value=_mock_resp({"slug": "retail", "sync_mode": "synced"}))
            MC.return_value.__aenter__.return_value.post = post
            result = _run(mod.semantic_model_reattach("retail", True))

        assert result["sync_mode"] == "synced"
        assert "/api/admin/semantic-models/retail/reattach" in post.call_args[0][0]
        assert post.call_args[1]["json"] == {"confirm_reattach": True}

    @pytest.mark.parametrize("tool_name", ["semantic_model_detach", "semantic_model_reattach"])
    def test_a_duckdb_instance_gets_the_typed_501_not_a_crash(self, tool_name):
        """Both endpoints are Postgres-only (A3 ratchet) and answer a typed
        `501` on the frozen DuckDB app-state backend. The tool must pass that
        through as an error the model can explain — not swallow it, and not
        fail on something else first.

        The mocked body is the REAL one `app/main.py`'s handler emits, built
        from the real exception rather than hand-written: `detail` is prose
        and `error`/`feature` are its SIBLINGS, so a test that nested them
        under `detail` would assert against a response shape that never
        occurs — and would hide that `_raise_for_status_with_detail` forwards
        `detail` only. What actually reaches the model is the prose, which is
        why the assertions are on the prose.
        """
        from src.repository_errors import RequiresPostgresBackend

        mod = _mod()
        exc_obj = RequiresPostgresBackend(tool_name)
        body = {
            "detail": str(exc_obj),
            "error": "requires_postgres_backend",
            "feature": exc_obj.feature,
        }
        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            MC.return_value.__aenter__.return_value.post = AsyncMock(return_value=_mock_resp(body, status=501))
            with pytest.raises(httpx.HTTPStatusError) as exc:
                _run(getattr(mod, tool_name)("retail", True))

        message = str(exc.value)
        assert "501" in message
        assert "Postgres app-state backend" in message, "the reason must survive into the tool error"
        assert tool_name in message, "the model must be able to name the feature that refused"


class TestPackageLinkTools:
    def test_link_posts_the_package_id(self):
        mod = _mod()
        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            post = AsyncMock(return_value=_mock_resp({"package_ids": ["pkg_a"]}))
            MC.return_value.__aenter__.return_value.post = post
            result = _run(mod.semantic_model_link_package("retail", "pkg_a"))

        assert result["package_ids"] == ["pkg_a"]
        assert "/api/admin/semantic-models/retail/packages" in post.call_args[0][0]
        assert post.call_args[1]["json"] == {"package_id": "pkg_a"}

    def test_unlink_deletes_the_pair(self):
        mod = _mod()
        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            delete = AsyncMock(return_value=_mock_resp({"package_ids": []}))
            MC.return_value.__aenter__.return_value.delete = delete
            result = _run(mod.semantic_model_unlink_package("retail", "pkg_a"))

        assert result["package_ids"] == []
        assert "/api/admin/semantic-models/retail/packages/pkg_a" in delete.call_args[0][0]

    def test_a_missing_package_surfaces_the_endpoints_own_detail(self):
        """`_raise_for_status_with_detail`, not a bare `raise_for_status` — the
        model needs `data_package_not_found` to correct itself."""
        mod = _mod()
        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            MC.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=_mock_resp({"detail": "data_package_not_found"}, status=404)
            )
            with pytest.raises(httpx.HTTPStatusError) as exc:
                _run(mod.semantic_model_link_package("retail", "nope"))

        assert "data_package_not_found" in str(exc.value)
