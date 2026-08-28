"""Tests for Agnes HTTP MCP server (app/api/mcp_http.py).

Verifies:
- Auth middleware: 401 without token, 401 with invalid token, pass with valid PAT
- SSE endpoint starts and emits an event stream
- Each tool makes the expected self-calls and returns the right shape
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.mcp_tooling import MCPOutputTooLarge


# ── helpers ─────────────────────────────────────────────────────────────────────


def _run(coro):
    """Run a coroutine synchronously (no pytest-asyncio dependency)."""
    return asyncio.run(coro)


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _mock_resp(data: Any, status: int = 200) -> MagicMock:
    """Build a mock httpx response."""
    r = MagicMock()
    r.status_code = status
    r.json.return_value = data
    r.raise_for_status = MagicMock()
    return r


def _import_mod():
    pytest.importorskip("mcp", reason="mcp package not installed")
    import app.api.mcp_http as mod

    return mod


# ── auth middleware ─────────────────────────────────────────────────────────────


class TestAuthMiddleware:
    def test_no_token_returns_401(self, seeded_app):
        r = seeded_app["client"].get("/api/mcp/sse")
        assert r.status_code == 401

    def test_bad_token_returns_401(self, seeded_app):
        r = seeded_app["client"].get("/api/mcp/sse", headers={"Authorization": "Bearer not-a-real-jwt"})
        assert r.status_code == 401

    def test_valid_token_passes_through_to_mcp(self, seeded_app):
        """_AuthMiddleware calls the underlying ASGI app when the token is valid."""
        import asyncio
        from app.api.mcp_http import _AuthMiddleware

        tok = seeded_app["analyst_token"]
        reached = []

        async def _inner_app(scope, receive, send):
            reached.append(True)

        middleware = _AuthMiddleware(_inner_app)
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/mcp/sse",
            "query_string": b"",
            "headers": [(b"authorization", f"Bearer {tok}".encode())],
        }
        asyncio.run(middleware(scope, None, None))
        assert reached, "Middleware did not call inner app with valid token"

    def test_sets_current_user_id_during_request(self, seeded_app):
        """_AuthMiddleware exposes the resolved caller id via _current_user_id
        for the duration of the request (read by passthrough closures so a
        per_user source forwards the caller's own credential), and resets it
        after."""
        import asyncio
        from app.api.mcp_http import _AuthMiddleware, _current_user_id

        tok = seeded_app["analyst_token"]
        seen = {}

        async def _inner_app(scope, receive, send):
            seen["uid"] = _current_user_id.get()

        middleware = _AuthMiddleware(_inner_app)
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/mcp/sse",
            "query_string": b"",
            "headers": [(b"authorization", f"Bearer {tok}".encode())],
        }
        asyncio.run(middleware(scope, None, None))
        assert seen["uid"] == "analyst1"
        # Reset after the request — no leak into the next context.
        assert _current_user_id.get() == ""

    def test_query_param_token_passes_through(self, seeded_app):
        """?token= fallback also reaches the inner app when valid."""
        import asyncio
        from app.api.mcp_http import _AuthMiddleware

        tok = seeded_app["analyst_token"]
        reached = []

        async def _inner_app(scope, receive, send):
            reached.append(True)

        middleware = _AuthMiddleware(_inner_app)
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/mcp/sse",
            "query_string": f"token={tok}".encode(),
            "headers": [],
        }
        asyncio.run(middleware(scope, None, None))
        assert reached, "?token= param did not reach inner app"

    def test_query_param_token_still_yields_an_authorization_header(self, seeded_app):
        """Every foundation tool gets its credential from `headers_fn()`, and
        the facts tools additionally resolve a caller out of it
        (`_facts_caller`). Both would break for a `?token=`-authenticated SSE
        session if the middleware left that path without an Authorization
        header — so pin that it normalizes the query param into the same
        `_current_token` the header path sets, which is what `_headers()`
        synthesizes from (raised as a question in review on #1652)."""
        import asyncio

        from app.api.mcp.foundation_tools import _facts_caller
        from app.api.mcp_http import _AuthMiddleware, _headers

        tok = seeded_app["analyst_token"]
        seen: dict = {}

        async def _inner_app(scope, receive, send):
            seen["headers"] = _headers()
            seen["caller_id"] = (_facts_caller(_headers) or {}).get("id")

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/mcp/sse",
            "query_string": f"token={tok}".encode(),
            "headers": [],
        }
        asyncio.run(_AuthMiddleware(_inner_app)(scope, None, None))

        assert seen["headers"]["Authorization"] == f"Bearer {tok}"
        assert seen["caller_id"] == "analyst1", "facts tools must authenticate a ?token= session"

    def test_query_param_token_can_be_disabled(self, seeded_app, monkeypatch):
        """`mcp.allow_query_param_token=false` turns the fallback off (401).

        F-3, 2026-08-05 audit: a token in the query string lands in every
        request log (CWE-598). The fallback stays ON by default so no existing
        SSE client breaks, but an operator whose clients all send the header
        can eliminate the exposure outright rather than relying on proxy log
        redaction.
        """
        import asyncio

        from app.api.mcp_http import _AuthMiddleware

        monkeypatch.setenv("AGNES_MCP_ALLOW_QUERY_PARAM_TOKEN", "false")

        tok = seeded_app["analyst_token"]
        reached = []
        sent = []

        async def _inner_app(scope, receive, send):
            reached.append(True)

        async def _send(msg):
            sent.append(msg)

        middleware = _AuthMiddleware(_inner_app)
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/mcp/sse",
            "query_string": f"token={tok}".encode(),
            "headers": [],
        }
        asyncio.run(middleware(scope, None, _send))

        assert not reached, "?token= reached the inner app with the flag off"
        assert any(m.get("type") == "http.response.start" and m.get("status") == 401 for m in sent)

    def test_header_auth_still_works_with_query_param_disabled(self, seeded_app, monkeypatch):
        """Turning the fallback off must not touch the Authorization header path."""
        import asyncio

        from app.api.mcp_http import _AuthMiddleware

        monkeypatch.setenv("AGNES_MCP_ALLOW_QUERY_PARAM_TOKEN", "false")

        tok = seeded_app["analyst_token"]
        reached = []

        async def _inner_app(scope, receive, send):
            reached.append(True)

        middleware = _AuthMiddleware(_inner_app)
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/mcp/sse",
            "query_string": b"",
            "headers": [(b"authorization", f"Bearer {tok}".encode())],
        }
        asyncio.run(middleware(scope, None, None))

        assert reached, "header auth broke when the query-param fallback was disabled"


# ── tool registration ────────────────────────────────────────────────────────────


class TestToolRegistration:
    def test_exact_server_side_tool_set(self):
        # Reload to a pristine module: the static @mcp.tool() set only. Dynamic
        # passthrough tools are registered onto the module-level ``mcp`` singleton
        # by ``create_app()`` (via register_passthrough_tools), so a prior test
        # that built the app would otherwise pollute this set. Reloading re-runs
        # the module body (fresh FastMCP + static decorators, no passthrough).
        import importlib

        import app.api.mcp_http as mod

        mod = importlib.reload(mod)
        tools = {t.name for t in mod.mcp._tool_manager.list_tools()}
        assert tools == {
            "server_info",
            "catalog",
            "schema",
            "describe",
            "query",
            "skills",
            # Triple-surface coverage for /documentation/api — agents read
            # the curated REST guide without leaving the chat. See
            # tests/test_documentation_api_triple_surface.py for the policy.
            "documentation_api",
            # On-demand full tool docs (progressive descriptions) — wire
            # descriptions carry only the first docstring paragraph.
            "tool_docs",
            # Stack discovery + subscription (issue #621) — an analyst's
            # Claude can browse available resources and subscribe without
            # leaving the chat.
            "stack_browse",
            "stack_subscribe",
            "stack_unsubscribe",
            # Store thumbs up/down ratings (issue #398) — an analyst's Claude
            # can rate a store entity without leaving the chat. See
            # tests/test_documentation_api_triple_surface.py for the policy.
            "store_rate",
            # Owner-facing review-pipeline status — pairs with
            # `agnes store status` and GET /api/store/entities/{id}/status.
            "store_status",
            # Full agent/skill lifecycle parity (REST × CLI × MCP) — discover,
            # inspect, install/remove marketplace items, and edit/delete owned
            # store entities from the chat. Pairs with `agnes marketplace
            # search/detail/add/remove` and `agnes store update/delete`.
            "marketplace_search",
            "marketplace_detail",
            "marketplace_add",
            "marketplace_remove",
            "store_update",
            "store_delete",
            # Collections read surfaces (Slice 2) — list collections and read
            # one collection's files. Upload/delete are CLI-only (multipart /
            # mutation). See tests/test_documentation_api_triple_surface.py.
            "collections_list",
            "collection_get",
            "collections_search",
            # Read ONE named file's extracted text, for the question search
            # cannot serve ("what is in this file?" has no keywords). Thin
            # wrapper over the existing preview endpoint, so its ~20k-char
            # cap and `truncated` flag are the context guard. Triple-surface
            # with GET /api/collections/{cid}/files/{fid}/preview +
            # `agnes collections cat`.
            "collection_file_read",
            # Unified knowledge search (K2, #797) — one query across
            # Collections chunks + knowledge items + table catalog cards.
            # Triple-surface with GET /api/knowledge/search + `agnes search`.
            "knowledge_search",
            # Keboola glossary import (2026-07-17 design) — relevance-ranked
            # (BM25) search over Keboola-imported business-term definitions.
            # Triple-surface with GET /api/glossary/search + `agnes glossary
            # search`.
            "glossary_search",
            # Open semantic-layer contract (Task 12) — read-only search + get
            # over canonical Ossie semantic models, RBAC-filtered on the
            # linked Data Package's grant. Triple-surface with GET
            # /api/semantic-models/search + GET /api/semantic-models/{slug}.yaml
            # + `agnes admin semantic-model list/export`.
            "semantic_model_search",
            "semantic_model_get",
            # Query-validation engine wiring (wave 3). Triple-surface with
            # POST /api/semantic-models/validate-query + `agnes semantic-model
            # validate-query`.
            "validate_semantic_query",
            # Agent read-parity tools (wave 4) — typed context lookup + JSON
            # Schema introspection over the caller's accessible semantic
            # models. Triple-surface with GET /api/semantic-models/context +
            # GET /api/semantic-models/schema + `agnes semantic-model
            # context/schema`.
            "get_semantic_context",
            "get_semantic_schema",
            # Chat-first authoring (spec 2026-08-24) — the one semantic-layer
            # write surface with outcome branching (admin → applied,
            # non-admin → moderation queue). Triple-surface with POST
            # /api/semantic-models/apply + `agnes semantic-model apply`.
            "apply_semantic_model",
            # Re-run ingestion for one stuck file (needs_review/rejected) —
            # status-honesty follow-up (spec 2026-07-08). Triple-surface with
            # POST /api/collections/{cid}/files/{fid}/reingest +
            # `agnes collections reingest`.
            "collections_reingest",
            # Fact graph over Collections — query surface (build order step
            # 6). Triple-surface with POST /api/facts/search, POST
            # /api/facts/neighbors, GET /api/facts/{subject_id}/claims, and
            # `agnes facts search/neighbors/claims`.
            "fact_search",
            "fact_neighbors",
            "fact_claims",
            # Config-surface introspection — an operator's Claude reads this
            # instance's live configurable surface (knobs + sources, registered
            # IWT, marketplaces, infra_repo_url). Triple-surface with
            # GET /api/admin/config-surface + `agnes admin config-surface`.
            "admin_config_surface",
            # Multi-project Keboola: list named source connections (#731).
            # Triple-surface with GET /api/admin/source-connections +
            # `agnes admin connection list`.
            "admin_source_connections_list",
            # Register a table from an upstream source. Triple-surface with
            # POST /api/admin/register-table + `agnes admin register-table`.
            "admin_register_table",
            # Why a connected project's metrics are (or aren't) landing.
            # Triple-surface with GET /api/admin/semantic-layer/coverage +
            # `agnes admin semantic-layer coverage`.
            "admin_semantic_layer_coverage",
            # Source-agnostic zero-coverage check — which registered tables
            # have NO valid semantic model at all, across every source.
            # Triple-surface with GET /api/admin/semantic-coverage +
            # `agnes semantic-model coverage tables`.
            "admin_semantic_coverage",
            # What each connected source still lacks across all six domains
            # (F4.1), and the tags the report cannot derive. Triple-surface
            # with /api/admin/semantic-model/coverage* + `agnes semantic-model
            # coverage[ tag| untag]`.
            "semantic_model_coverage",
            "semantic_model_coverage_tag",
            "semantic_model_coverage_untag",
            # Muting a semantic-layer health check (F4.3) — "I know, it is
            # deliberate". Triple-surface with /api/admin/semantic-layer/mutes*
            # + `agnes semantic-model mute|unmute|mutes`.
            "semantic_mutes_list",
            "mute_semantic_check",
            "unmute_semantic_check",
            # Is the layer trustworthy right now (F4.2) — sync failures,
            # disconnected models, invalid documents, static document-quality
            # checks, F4.1's coverage roll-up, and F4.3's active mutes.
            # Triple-surface with GET /api/admin/semantic-layer/health +
            # `agnes semantic-model health`.
            "semantic_layer_health",
            # "That answer looked wrong" (F4.5). `flag_semantic_issue` is the
            # one write here an ordinary caller may make — an agent that cannot
            # ground its answer is the intended reporter; the other two are the
            # admin side of the same queue. Triple-surface with
            # /api/semantic-feedback + /api/admin/semantic-feedback* + `agnes
            # semantic-model feedback submit|list|resolve`.
            "flag_semantic_issue",
            "semantic_feedback_list",
            "semantic_feedback_resolve",
            # Job management for scheduler — list, get, enqueue tasks.
            # Triple-surface with GET /api/jobs + GET /api/jobs/{job_id} +
            # POST /api/jobs + `agnes admin jobs`.
            "admin_jobs_list",
            "admin_job_get",
            "admin_job_enqueue",
            # DuckLake analytics-backend migration (wave-2G Task 6). Triple-
            # surface with POST /api/admin/analytics/migrate + `agnes admin
            # analytics migrate`.
            "admin_analytics_migrate",
            # Contributed-skill triple-surface — admin can list, publish, and
            # delete skills in the Agnes Contributed marketplace without leaving
            # the chat. Mirrors REST + `agnes admin skill` CLI surface.
            "list_contributed_skills",
            "contribute_skill",
            "delete_contributed_skill",
            # Web chat composer slash-menu catalog (issue #780). Triple-surface
            # with GET /api/chat/skills + `agnes chat skills`.
            "chat_skills",
            # Chat composer "+" upload (#966) — upload a file into the chat
            # workspace without leaving the conversation. Triple-surface with
            # POST /api/chat/uploads + `agnes chat upload`. The server-hosted
            # variant refuses by-path reads (client-side stdio does the actual
            # file read); see app/api/mcp/foundation_tools.py.
            "chat_upload_file",
            # Markdown-first skill publish (studio Skill Builder, issue #688).
            # Triple-surface with POST /api/store/entities/from-markdown +
            # `agnes store publish-md`.
            "store_publish_markdown",
            # Maintained digests (K4, #799) — admin CRUD over LLM-regenerated
            # digest documents. Triple-surface with the
            # /api/admin/knowledge-digests* REST surface + `agnes admin digest`.
            "admin_knowledge_digests_list",
            "admin_knowledge_digest_get",
            "admin_knowledge_digest_create",
            "admin_knowledge_digest_update",
            "admin_knowledge_digest_delete",
            # Skill-linter admin moderation surface (v89, #687) — findings
            # list, manual full-corpus audit, per-finding dismiss. Triple-
            # surface with /api/admin/store/lint-* + `agnes admin store lint-*`.
            "admin_store_lint_findings",
            "admin_store_lint_audit",
            "admin_store_lint_dismiss",
            # Per-user MCP credential connectivity check — an analyst's Claude
            # verifies their own stored token. Triple-surface with POST
            # /api/mcp/sources/{id}/my-secret/test + `agnes mcp my-secret test`.
            "my_secret_test",
            # Agent profiles (agent-api V1a, Task 12) — list your own agent
            # profiles and one-shot ask an agent without leaving the chat.
            # Triple-surface with GET /api/v1/agents + POST
            # /api/v1/agents/{slug}/responses + `agnes agent list`/`ask`.
            "agent_list",
            "agent_ask",
            # Agent-as-API monthly usage (agent-api V1b, Task 8). Triple-
            # surface with GET /api/v1/agents/{slug}/usage +
            # `agnes agent usage`.
            "agent_usage",
            # Hosted data apps (data-apps platform plan, Task 11) — list/get
            # for any authenticated user with view access, deploy/logs for
            # app owner or Admin. Triple-surface with /api/data-apps* +
            # `agnes app list/show/deploy/logs`.
            "data_apps_list",
            # `create` completes the family: a draft is a sibling of an
            # EXISTING app, so without it an agent building a new app from
            # chat had no first step and got 404 data_app_not_found.
            "data_app_create",
            "data_app_get",
            "data_app_deploy",
            "data_app_logs",
            # "Add artefacts to My Stack" — triple-surface with
            # /api/stack/artefacts* + `agnes stack artefacts list/add/remove`.
            "stack_artefacts_candidates",
            "stack_artefact_add",
            "stack_artefact_remove",
            # Wave 3B draft-iteration model (Task 8) — create/delete a draft
            # copy of a prod app on an iteration branch, and mint a fresh git
            # push credential. Triple-surface with /api/data-apps/{slug}/drafts*
            # and /git-credential + `agnes app draft create/delete` +
            # `agnes app git-credential`.
            "data_app_create_draft",
            "data_app_delete_draft",
            "data_app_git_credential",
            # Linked (externally-hosted) apps (v108) — admin description override
            # on a managed/linked app. Triple-surface with PATCH
            # /api/data-apps/{slug} + `agnes app set-description`.
            "data_app_set_description",
            # Wave 3C in-chat preview loop (Task 4/5) — chat-surface-only
            # render directives for the split-pane preview iframe (spec
            # §7/§9): no REST/CLI analogue. `agnes_data_app_preview`'s
            # live-URL call mints a short-TTL `data-app-preview:<slug>`
            # scoped grant via POST /api/data-apps/{slug}/preview-grant.
            "agnes_data_app_preview",
            "agnes_data_app_refresh",
            "agnes_data_app_close",
            "agnes_data_app_credentials",
        }

    def test_no_client_only_tools(self):
        """query_local and pull require a local analyst filesystem — excluded here."""
        mod = _import_mod()
        tools = {t.name for t in mod.mcp._tool_manager.list_tools()}
        assert "query_local" not in tools
        assert "pull" not in tools


# ── catalog tool ────────────────────────────────────────────────────────────────


class TestCatalogTool:
    def test_returns_table_list(self):
        mod = _import_mod()
        data = {"tables": [{"id": "orders", "name": "Orders", "query_mode": "local"}]}

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            MC.return_value.__aenter__.return_value.get = AsyncMock(return_value=_mock_resp(data))
            result = _run(mod.catalog())

        assert result["tables"][0]["id"] == "orders"

    def test_url_contains_v2_catalog(self):
        mod = _import_mod()

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_get = AsyncMock(return_value=_mock_resp({}))
            MC.return_value.__aenter__.return_value.get = mock_get
            _run(mod.catalog())

        called_url = mock_get.call_args[0][0]
        assert "/api/v2/catalog" in called_url


# ── schema tool ─────────────────────────────────────────────────────────────────


class TestSchemaTool:
    def test_passes_table_id_in_url(self):
        mod = _import_mod()
        data = {"table_id": "orders", "columns": [{"name": "id", "type": "VARCHAR"}]}

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_get = AsyncMock(return_value=_mock_resp(data))
            MC.return_value.__aenter__.return_value.get = mock_get
            result = _run(mod.schema("orders"))

        assert result["columns"][0]["name"] == "id"
        assert "orders" in mock_get.call_args[0][0]


# ── describe tool ───────────────────────────────────────────────────────────────


class TestDescribeTool:
    def test_returns_schema_and_sample(self):
        mod = _import_mod()
        schema_data = {"columns": []}
        sample_data = {"rows": []}

        def _side(url, **kw):
            return _mock_resp(schema_data if "schema" in url else sample_data)

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            MC.return_value.__aenter__.return_value.get = AsyncMock(side_effect=_side)
            result = _run(mod.describe("orders"))

        assert "schema" in result
        assert "sample" in result

    def test_clamps_rows_to_50(self):
        mod = _import_mod()
        calls = []

        def _side(url, **kw):
            calls.append((url, kw))
            return _mock_resp({})

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            MC.return_value.__aenter__.return_value.get = AsyncMock(side_effect=_side)
            _run(mod.describe("orders", rows=9999))

        sample = next((c for c in calls if "sample" in c[0]), None)
        assert sample is not None
        assert sample[1].get("params", {}).get("n", 0) <= 50


# ── query tool ──────────────────────────────────────────────────────────────────


class TestQueryTool:
    def test_posts_sql_and_limit(self):
        mod = _import_mod()
        resp_data = {"columns": ["x"], "rows": [[1]], "truncated": False}

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_post = AsyncMock(return_value=_mock_resp(resp_data))
            MC.return_value.__aenter__.return_value.post = mock_post
            result = _run(mod.query("SELECT x FROM t", limit=5))

        assert result["columns"] == ["x"]
        posted = mock_post.call_args[1]["json"]
        assert posted["sql"] == "SELECT x FROM t"
        assert posted["limit"] == 5

    def test_default_limit_is_1000(self):
        mod = _import_mod()

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_post = AsyncMock(return_value=_mock_resp({}))
            MC.return_value.__aenter__.return_value.post = mock_post
            _run(mod.query("SELECT 1"))

        assert mock_post.call_args[1]["json"]["limit"] == 1000


# ── stack tools (issue #621) ──────────────────────────────────────────────────────


class TestStackTools:
    def test_browse_passes_type_param(self):
        mod = _import_mod()
        data = {"items": [{"id": "pkg_a", "name": "A", "in_stack": False}]}

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_get = AsyncMock(return_value=_mock_resp(data))
            MC.return_value.__aenter__.return_value.get = mock_get
            result = _run(mod.stack_browse("data_package"))

        assert result["items"][0]["id"] == "pkg_a"
        called_url = mock_get.call_args[0][0]
        assert "/api/stack/browse" in called_url
        assert mock_get.call_args[1]["params"] == {"type": "data_package"}

    def test_subscribe_posts_payload_and_adds_hint(self):
        mod = _import_mod()

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_post = AsyncMock(return_value=_mock_resp({"subscribed": True}))
            MC.return_value.__aenter__.return_value.post = mock_post
            result = _run(mod.stack_subscribe("data_package", "pkg_a"))

        assert result["subscribed"] is True
        # Post-subscribe hint tells the model what to run next.
        assert "agnes pull" in result["next_step"]
        called_url = mock_post.call_args[0][0]
        assert "/api/stack/subscribe" in called_url
        posted = mock_post.call_args[1]["json"]
        assert posted == {"resource_type": "data_package", "resource_id": "pkg_a"}

    def test_unsubscribe_calls_subscription_endpoint(self):
        mod = _import_mod()

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_delete = AsyncMock(return_value=_mock_resp({}, status=204))
            MC.return_value.__aenter__.return_value.delete = mock_delete
            result = _run(mod.stack_unsubscribe("data_package", "pkg_a"))

        assert result["unsubscribed"] is True
        called_url = mock_delete.call_args[0][0]
        assert "/api/stack/subscription/data_package/pkg_a" in called_url


class TestStorePublishMarkdownTool:
    def test_posts_full_payload(self):
        mod = _import_mod()
        data = {"id": "ent_1", "name": "my-skill", "version": 1, "visibility_status": "pending"}

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_post = AsyncMock(return_value=_mock_resp(data, status=201))
            MC.return_value.__aenter__.return_value.post = mock_post
            result = _run(
                mod.store_publish_markdown(
                    "my-skill",
                    "# My Skill\n\nBody text.",
                    description="Use when doing X",
                    category="analytics",
                )
            )

        assert result == data
        called_url = mock_post.call_args[0][0]
        assert "/api/store/entities/from-markdown" in called_url
        posted = mock_post.call_args[1]["json"]
        assert posted == {
            "type": "skill",
            "name": "my-skill",
            "skill_md": "# My Skill\n\nBody text.",
            "description": "Use when doing X",
            "category": "analytics",
        }

    def test_omits_optional_fields_when_absent(self):
        mod = _import_mod()
        data = {"id": "ent_2", "name": "bare-skill", "version": 1, "visibility_status": "approved"}

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_post = AsyncMock(return_value=_mock_resp(data, status=201))
            MC.return_value.__aenter__.return_value.post = mock_post
            result = _run(mod.store_publish_markdown("bare-skill", "# Bare Skill"))

        assert result == data
        posted = mock_post.call_args[1]["json"]
        assert posted == {"type": "skill", "name": "bare-skill", "skill_md": "# Bare Skill"}

    def test_accepts_agent_type(self):
        """type="agent" (#865) — MCP callers can now publish an agent, not just a skill."""
        mod = _import_mod()
        data = {"id": "ent_3", "name": "my-agent", "version": 1, "visibility_status": "pending"}

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_post = AsyncMock(return_value=_mock_resp(data, status=201))
            MC.return_value.__aenter__.return_value.post = mock_post
            result = _run(mod.store_publish_markdown("my-agent", "# My Agent\n\nBody text.", type="agent"))

        assert result == data
        posted = mock_post.call_args[1]["json"]
        assert posted == {"type": "agent", "name": "my-agent", "skill_md": "# My Agent\n\nBody text."}


class TestValidateSemanticQueryTool:
    """Query-validation engine wiring (wave 3) — same request/response shape
    as ``POST /api/semantic-models/validate-query`` and
    ``agnes semantic-model validate-query``."""

    def test_posts_sql_and_returns_result(self):
        mod = _import_mod()
        data = {"available": True, "valid": True, "used_datasets": ["orders"], "violations": []}

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_post = AsyncMock(return_value=_mock_resp(data))
            MC.return_value.__aenter__.return_value.post = mock_post
            result = _run(mod.validate_semantic_query("SELECT SUM(revenue) FROM orders"))

        assert result == data
        called_url = mock_post.call_args[0][0]
        assert "/api/semantic-models/validate-query" in called_url
        posted = mock_post.call_args[1]["json"]
        assert posted == {"sql": "SELECT SUM(revenue) FROM orders", "target_engine": "duckdb"}

    def test_expected_and_target_engine_are_forwarded(self):
        mod = _import_mod()
        data = {"available": True, "valid": True}
        expected = [{"type": "metric", "name": "revenue"}]

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_post = AsyncMock(return_value=_mock_resp(data))
            MC.return_value.__aenter__.return_value.post = mock_post
            _run(mod.validate_semantic_query("SELECT 1", expected=expected, target_engine="bigquery"))

        posted = mock_post.call_args[1]["json"]
        assert posted == {"sql": "SELECT 1", "target_engine": "bigquery", "expected": expected}

    def test_no_semantic_model_returns_unavailable_shape(self):
        """Fail-closed gating: the tool surfaces the server's `available:
        false` payload as-is, never a misleading all-clear."""
        mod = _import_mod()
        data = {"available": False, "error": "no_semantic_model", "message": "No semantic model is available."}

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_post = AsyncMock(return_value=_mock_resp(data))
            MC.return_value.__aenter__.return_value.post = mock_post
            result = _run(mod.validate_semantic_query("SELECT 1"))

        assert result["available"] is False
        assert "valid" not in result


class TestSemanticContextAndSchemaTools:
    """Agent read-parity tools (wave 4) — same request/response shape as
    ``GET /api/semantic-models/context`` / ``GET /api/semantic-models/schema``
    and `agnes semantic-model context` / `schema`."""

    def test_context_builds_a_single_element_selections_list(self):
        import json

        mod = _import_mod()
        data = {
            "results": [{"semantic_type": "dataset", "mode": "compact", "objects": []}],
            "unknown_types": [],
        }

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_get = AsyncMock(return_value=_mock_resp(data))
            MC.return_value.__aenter__.return_value.get = mock_get
            result = _run(mod.get_semantic_context("dataset"))

        assert result == data
        called_url = mock_get.call_args[0][0]
        assert "/api/semantic-models/context" in called_url
        params = mock_get.call_args[1]["params"]
        assert json.loads(params["selections"]) == [{"semantic_type": "dataset", "ids": None}]
        assert "model_ids" not in params

    def test_context_forwards_ids_and_model_ids(self):
        import json

        mod = _import_mod()
        data = {"results": [], "unknown_types": []}

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_get = AsyncMock(return_value=_mock_resp(data))
            MC.return_value.__aenter__.return_value.get = mock_get
            _run(mod.get_semantic_context("metric", ids=["revenue"], model_ids=["retail"]))

        params = mock_get.call_args[1]["params"]
        assert json.loads(params["selections"]) == [{"semantic_type": "metric", "ids": ["revenue"]}]
        assert params["model_ids"] == ["retail"]

    def test_schema_forwards_semantic_types(self):
        mod = _import_mod()
        data = {"$defs": {"Dataset": {}}, "types": {"dataset": {"$ref": "#/$defs/Dataset"}}, "unknown_types": []}

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_get = AsyncMock(return_value=_mock_resp(data))
            MC.return_value.__aenter__.return_value.get = mock_get
            result = _run(mod.get_semantic_schema(["dataset"]))

        assert result == data
        called_url = mock_get.call_args[0][0]
        assert "/api/semantic-models/schema" in called_url
        assert mock_get.call_args[1]["params"] == {"semantic_types": ["dataset"]}


# ── marketplace lifecycle tools (agent-management triple-surface parity) ────────


class TestMarketplaceLifecycleTools:
    """MCP mirrors of `agnes marketplace search/detail/add/remove` and
    `agnes store update/delete` — full agent/skill lifecycle without a CLI."""

    def test_search_defaults_to_both_tabs_with_labels(self):
        mod = _import_mod()
        curated_item = {"id": "eng/reviewer", "source": "curated", "type": "agent"}
        flea_item = {"id": "ent_9", "source": "flea", "type": "agent"}

        def _side(url, **kw):
            tab = kw["params"]["tab"]
            return _mock_resp({"items": [curated_item if tab == "curated" else flea_item]})

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_get = AsyncMock(side_effect=_side)
            MC.return_value.__aenter__.return_value.get = mock_get
            result = _run(mod.marketplace_search(query="review", type="agent"))

        assert result == {"items": [curated_item, flea_item], "total": 2}
        assert mock_get.call_count == 2
        tabs = [c[1]["params"]["tab"] for c in mock_get.call_args_list]
        assert tabs == ["curated", "flea"]
        for c in mock_get.call_args_list:
            assert "/api/marketplace/items" in c[0][0]
            assert c[1]["params"]["q"] == "review"
            assert c[1]["params"]["type"] == "agent"

    def test_search_single_source_hits_one_tab(self):
        mod = _import_mod()

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_get = AsyncMock(return_value=_mock_resp({"items": []}))
            MC.return_value.__aenter__.return_value.get = mock_get
            result = _run(mod.marketplace_search(source="flea"))

        assert result == {"items": [], "total": 0}
        assert mock_get.call_count == 1
        assert mock_get.call_args[1]["params"]["tab"] == "flea"

    def test_detail_parses_curated_and_flea_ids(self):
        mod = _import_mod()

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_get = AsyncMock(return_value=_mock_resp({"type": "agent"}))
            MC.return_value.__aenter__.return_value.get = mock_get
            _run(mod.marketplace_detail("eng/reviewer"))
            _run(mod.marketplace_detail("ent_9"))

        urls = [c[0][0] for c in mock_get.call_args_list]
        assert "/api/marketplace/curated/eng/reviewer" in urls[0]
        assert "/api/marketplace/flea/ent_9/detail" in urls[1]

    def test_tools_accept_tab_prefixed_search_ids(self):
        """Ids exactly as `/api/marketplace/items` prints them — `curated-<mid>/<plugin>`,
        `flea-<uuid>` — must route to the bare-form REST paths (Devin Review on #982)."""
        mod = _import_mod()

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_get = AsyncMock(return_value=_mock_resp({"type": "agent"}))
            mock_post = AsyncMock(return_value=_mock_resp({"installed": True}))
            MC.return_value.__aenter__.return_value.get = mock_get
            MC.return_value.__aenter__.return_value.post = mock_post
            _run(mod.marketplace_detail("curated-eng/reviewer"))
            _run(mod.marketplace_detail("flea-ent_9"))
            _run(mod.marketplace_add("curated-eng/reviewer"))
            _run(mod.marketplace_add("flea-ent_9"))

        get_urls = [c[0][0] for c in mock_get.call_args_list]
        assert "/api/marketplace/curated/eng/reviewer" in get_urls[0]
        assert "/api/marketplace/flea/ent_9/detail" in get_urls[1]
        post_urls = [c[0][0] for c in mock_post.call_args_list]
        assert "/api/marketplace/curated/eng/reviewer/install" in post_urls[0]
        assert "/api/store/entities/ent_9/install" in post_urls[1]

    def test_add_routes_by_id_shape_and_hints_refresh(self):
        mod = _import_mod()

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_post = AsyncMock(return_value=_mock_resp({"installed": True}))
            MC.return_value.__aenter__.return_value.post = mock_post
            flea = _run(mod.marketplace_add("ent_9"))
            curated = _run(mod.marketplace_add("eng/reviewer"))

        assert flea["installed"] is True and "update-agnes-plugins" in flea["next_step"]
        assert curated["installed"] is True
        urls = [c[0][0] for c in mock_post.call_args_list]
        assert "/api/store/entities/ent_9/install" in urls[0]
        assert "/api/marketplace/curated/eng/reviewer/install" in urls[1]

    def test_remove_routes_by_id_shape(self):
        mod = _import_mod()

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_delete = AsyncMock(return_value=_mock_resp({}, status=204))
            MC.return_value.__aenter__.return_value.delete = mock_delete
            flea = _run(mod.marketplace_remove("ent_9"))
            curated = _run(mod.marketplace_remove("eng/reviewer"))

        assert flea["removed"] is True and curated["removed"] is True
        urls = [c[0][0] for c in mock_delete.call_args_list]
        assert "/api/store/entities/ent_9/install" in urls[0]
        assert "/api/marketplace/curated/eng/reviewer/install" in urls[1]

    def test_store_update_sends_only_provided_fields(self):
        mod = _import_mod()
        data = {"id": "ent_9", "version": 1}

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_put = AsyncMock(return_value=_mock_resp(data))
            MC.return_value.__aenter__.return_value.put = mock_put
            result = _run(mod.store_update("ent_9", description="Better trigger line"))

        assert result == data
        assert "/api/store/entities/ent_9" in mock_put.call_args[0][0]
        assert mock_put.call_args[1]["data"] == {"description": "Better trigger line"}

    def test_store_update_refuses_empty_edit_without_http_call(self):
        mod = _import_mod()

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_put = AsyncMock()
            MC.return_value.__aenter__.return_value.put = mock_put
            result = _run(mod.store_update("ent_9"))

        assert result["error"] == "nothing_to_update"
        mock_put.assert_not_called()

    def test_store_delete_calls_entity_endpoint(self):
        mod = _import_mod()

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            mock_delete = AsyncMock(return_value=_mock_resp({}, status=204))
            MC.return_value.__aenter__.return_value.delete = mock_delete
            result = _run(mod.store_delete("ent_9"))

        assert result == {"deleted": True, "entity_id": "ent_9"}
        assert "/api/store/entities/ent_9" in mock_delete.call_args[0][0]


# ── server_info tool ────────────────────────────────────────────────────────────


class TestServerInfoTool:
    def test_returns_health_and_email(self):
        mod = _import_mod()

        def _side(url, **kw):
            if "/api/health" in url:
                return _mock_resp({"status": "ok"})
            if "/api/me" in url:
                return _mock_resp({"email": "analyst@test.com"})
            return _mock_resp({})

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            MC.return_value.__aenter__.return_value.get = AsyncMock(side_effect=_side)
            result = _run(mod.server_info())

        assert result["authenticated"] is True
        assert result["health"] == {"status": "ok"}
        assert result["user_email"] == "analyst@test.com"

    def test_health_unreachable_doesnt_crash(self):
        mod = _import_mod()

        def _side(url, **kw):
            if "/api/health" in url:
                raise ConnectionError("refused")
            return _mock_resp({"email": "x@y.com"})

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            MC.return_value.__aenter__.return_value.get = AsyncMock(side_effect=_side)
            result = _run(mod.server_info())

        assert result["health"] == "unreachable"
        assert result["authenticated"] is True


# ── my_secret_test tool ──────────────────────────────────────────────────────


class TestMySecretTestTool:
    def test_success_passthrough(self):
        mod = _import_mod()
        data = {"ok": True, "tool_count": 3, "message": "ok"}

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            MC.return_value.__aenter__.return_value.post = AsyncMock(return_value=_mock_resp(data))
            result = _run(mod.my_secret_test("src_test"))

        assert result == data

    def test_403_remedy_reaches_the_model_instead_of_raising(self):
        """raise_for_status() would discard the response body and surface only
        a generic 'Forbidden' — the connect-here remedy in `detail` must reach
        the caller instead (audit finding on PR #919)."""
        mod = _import_mod()
        remedy = "not connected — visit /me/connections?source=src_test to add your token"

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            resp = _mock_resp({"detail": remedy}, status=403)
            MC.return_value.__aenter__.return_value.post = AsyncMock(return_value=resp)
            result = _run(mod.my_secret_test("src_test"))

        assert result == {"ok": False, "tool_count": None, "message": remedy}
        resp.raise_for_status.assert_not_called()

    def test_other_4xx_also_returns_detail_without_raising(self):
        mod = _import_mod()

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            resp = _mock_resp({"detail": "not_granted"}, status=429)
            MC.return_value.__aenter__.return_value.post = AsyncMock(return_value=resp)
            result = _run(mod.my_secret_test("src_test"))

        assert result["ok"] is False
        assert result["message"] == "not_granted"

    def test_5xx_still_raises(self):
        mod = _import_mod()

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            resp = _mock_resp({}, status=500)
            resp.raise_for_status.side_effect = RuntimeError("boom")
            MC.return_value.__aenter__.return_value.post = AsyncMock(return_value=resp)
            with pytest.raises(RuntimeError):
                _run(mod.my_secret_test("src_test"))


# ── tool_docs + progressive descriptions ────────────────────────────────────────


class TestToolDocs:
    def test_returns_full_docstring(self):
        mod = _import_mod()
        result = _run(mod.tool_docs("query"))
        assert result["tool"] == "query"
        assert "Args:" in result["docs"]

    def test_unknown_tool_lists_valid_names(self):
        mod = _import_mod()
        with pytest.raises(ValueError, match="Valid tool names"):
            _run(mod.tool_docs("nope"))


class TestWireDescriptions:
    def _pristine(self):
        import importlib

        import app.api.mcp_http as mod

        return importlib.reload(mod)

    def test_all_descriptions_stay_short(self):
        # Ratchet: tools/list must never re-bloat. 500 chars per description.
        mod = self._pristine()
        for t in mod.mcp._tool_manager.list_tools():
            assert t.description, f"{t.name} has no description"
            assert len(t.description) <= 500, (
                f"{t.name}: {len(t.description)} chars (>500) — trim the docstring's first paragraph"
            )

    def test_query_description_points_to_tool_docs(self):
        mod = self._pristine()
        t = mod.mcp._tool_manager.get_tool("query")
        assert "tool_docs('query')" in t.description
        assert "Args:" not in t.description  # detail moved off the wire


# ── output-size guard ───────────────────────────────────────────────────────────


class TestOutputGuard:
    def _query(self, mod, resp_data):
        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            MC.return_value.__aenter__.return_value.post = AsyncMock(return_value=_mock_resp(resp_data))
            return _run(mod.query("SELECT x FROM t"))

    def test_query_over_cap_raises_with_guidance(self, monkeypatch):
        mod = _import_mod()
        monkeypatch.setenv("AGNES_MCP_MAX_OUTPUT_CHARS", "1000")
        big = {"columns": ["x"], "rows": [["y" * 5000]], "truncated": False}
        with pytest.raises(MCPOutputTooLarge, match="output cap"):
            self._query(mod, big)

    def test_query_under_cap_passes(self, monkeypatch):
        mod = _import_mod()
        monkeypatch.setenv("AGNES_MCP_MAX_OUTPUT_CHARS", "1000")
        small = {"columns": ["x"], "rows": [[1]], "truncated": False}
        assert self._query(mod, small) == small

    def test_env_zero_disables_guard(self, monkeypatch):
        mod = _import_mod()
        monkeypatch.setenv("AGNES_MCP_MAX_OUTPUT_CHARS", "0")
        big = {"columns": ["x"], "rows": [["y" * 500_000]], "truncated": False}
        assert self._query(mod, big) == big

    def test_describe_over_cap_mentions_rows_hint(self, monkeypatch):
        mod = _import_mod()
        monkeypatch.setenv("AGNES_MCP_MAX_OUTPUT_CHARS", "500")
        wide = {"columns": [{"name": "x", "type": "VARCHAR", "blob": "z" * 5000}]}
        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            MC.return_value.__aenter__.return_value.get = AsyncMock(return_value=_mock_resp(wide))
            with pytest.raises(MCPOutputTooLarge, match="rows"):
                _run(mod.describe("t1"))
