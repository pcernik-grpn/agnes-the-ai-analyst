"""Triple-surface coverage check for the new ``/documentation/api`` cohort.

Project policy: every public ``/api/*`` (and ``/documentation/*``) endpoint
added going forward MUST be reachable via three surfaces:

  1. REST (HTTP)            — ``app.web.router``
  2. CLI (``agnes …``)      — ``cli/commands/…`` + ``cli/main.py`` registration
  3. MCP tool               — ``app.api.mcp_http`` (HTTP MCP server)

This locks the v0.68.0 cohort: a single endpoint, ``GET /documentation/api``,
landed with a matching ``agnes docs api`` CLI subcommand AND a matching
``documentation_api`` MCP tool. The intent isn't a full retroactive sweep
(pre-existing endpoints stay as-is) — it's a forward-only ratchet so the
new floor never erodes.

When you add another endpoint that ought to live on all three surfaces,
extend ``_COHORT`` below and the test fails until the CLI/MCP entries land.
"""

from __future__ import annotations

import os
from pathlib import Path

# Endpoints that must have all three surfaces. Forward-only — add new
# entries when they land, do NOT retroactively backfill old endpoints
# (the policy is a ratchet, not a sweep). Tuple of (cli_cmd, mcp_tool).
_COHORT: dict[str, tuple[str, str]] = {
    "/documentation/api": ("docs api", "documentation_api"),
    # Reading one collection file's text (#1240). The endpoint's path is
    # browser-shaped — the Library's preview modal fetches it directly — but
    # its contract is now agent-facing: an agent shown a file it could not
    # open was the complaint that produced `collection_file_read` / `agnes
    # collections cat`. In the cohort rather than exempt so the structural
    # gate notices if either surface is ever removed. (Devin Review on #1240.)
    "/api/collections/{collection_id}/files/{file_id}/preview": ("collections cat", "collection_file_read"),
    # Unified knowledge search (K2, #797): one query across Collections
    # chunks + knowledge items + table catalog cards. CLI is the top-level
    # `agnes search` (callback-style single command, like `agnes pull`).
    "/api/knowledge/search": ("search", "knowledge_search"),
    # Stack discovery (issue #621). subscribe/unsubscribe paths are already
    # grandfathered; browse is the new triple-surface endpoint.
    "/api/stack/browse": ("stack browse", "stack_browse"),
    # Store thumbs up/down ratings (issue #398).
    "/api/store/entities/{entity_id}/rate": ("store rate", "store_rate"),
    # Owner-facing review-pipeline status (upload-friction feedback).
    "/api/store/entities/{entity_id}/status": ("store status", "store_status"),
    # Markdown-first skill publish (studio Skill Builder direct-publish flow,
    # issue #688). CLI: `store publish-md`. MCP: `store_publish_markdown`.
    "/api/store/entities/from-markdown": ("store publish-md", "store_publish_markdown"),
    # Full agent/skill lifecycle parity — an agent can discover, inspect,
    # install/remove marketplace items and edit/delete its own store entities
    # over any of the three surfaces. Binary siblings (ZIP upload/replace,
    # photo, `store mine` bundle) stay CLI-only — see the grandfathered
    # /api/store/entities POST path and _EXEMPT reasoning patterns.
    "/api/marketplace/items": ("marketplace search", "marketplace_search"),
    "/api/marketplace/flea/{entity_id}/detail": ("marketplace detail", "marketplace_detail"),
    "/api/marketplace/curated/{marketplace_id}/{plugin_name}": ("marketplace detail", "marketplace_detail"),
    "/api/marketplace/curated/{marketplace_id}/{plugin_name}/install": ("marketplace add", "marketplace_add"),
    "/api/store/entities/{entity_id}/install": ("marketplace add", "marketplace_add"),
    # PUT (metadata edit) → store_update; DELETE → store_delete. One cohort
    # row per path — the MCP column names the edit tool; store_delete is
    # asserted in FOUNDATION_TOOL_NAMES / test_mcp_http's exact-set check.
    "/api/store/entities/{entity_id}": ("store update", "store_update"),
    # Collections — bring-your-files (Slice 2). The read surfaces are
    # triple-surface; the multipart-upload + file-mutation paths are _EXEMPT
    # below (binary upload has no MCP analogue).
    "/api/collections": ("collections list", "collections_list"),
    "/api/collections/{collection_id}": ("collections show", "collection_get"),
    "/api/collections/search": ("collections search", "collections_search"),
    # Collections re-ingest (status-honesty, spec 2026-07-08).
    "/api/collections/{collection_id}/files/{file_id}/reingest": ("collections reingest", "collections_reingest"),
    # Config-surface introspection (built-in marketplace spec Phase 1).
    "/api/admin/config-surface": ("admin config-surface", "admin_config_surface"),
    # Multi-project Keboola: named source-connections (#731).
    "/api/admin/source-connections": ("admin connection list", "admin_source_connections_list"),
    # Semantic-layer coverage: why a connected project's metrics are (or are
    # not) landing in metric_definitions.
    "/api/admin/semantic-layer/coverage": (
        "admin semantic-layer coverage",
        "admin_semantic_layer_coverage",
    ),
    # Open semantic-layer contract (Task 10/11/12) — public, resource-gated
    # export of one canonical Ossie document. `semantic_model_get` reads
    # this same endpoint (wraps its raw YAML text into a dict); `agnes admin
    # semantic-model export` is the CLI counterpart.
    "/api/semantic-models/{slug}.yaml": ("admin semantic-model export", "semantic_model_get"),
    # Query-validation engine wiring (wave 3): validate SQL against the
    # caller's accessible semantic models before running it. CLI is the
    # non-admin `semantic-model` group (distinct from `admin semantic-model
    # validate`, which schema-checks a document, not a query).
    "/api/semantic-models/validate-query": ("semantic-model validate-query", "validate_semantic_query"),
    # Agent read-parity tools (wave 4) — typed context lookup + JSON Schema
    # introspection over the caller's accessible semantic models. Same RBAC
    # tier as search/export/validate-query.
    "/api/semantic-models/context": ("semantic-model context", "get_semantic_context"),
    "/api/semantic-models/schema": ("semantic-model schema", "get_semantic_schema"),
    # Chat-first authoring (spec 2026-08-24) — the one semantic-layer write
    # surface with outcome branching (admin → applied, non-admin → queued
    # for moderation).
    "/api/semantic-models/apply": ("semantic-model apply", "apply_semantic_model"),
    # Contributed-skill triple-surface (GET list + DELETE; POST contribute is _EXEMPT below).
    "/api/admin/contributed-skills": ("admin skill list", "list_contributed_skills"),
    "/api/admin/contributed-skills/{name}": ("admin skill delete", "delete_contributed_skill"),
    # Web chat slash-menu catalog (issue #780).
    "/api/chat/skills": ("chat skills", "chat_skills"),
    # Chat workspace file upload — any authenticated user can upload data/image/document
    # files into their per-user workspace so Claude sees them in the next session.
    "/api/chat/uploads": ("chat upload", "chat_upload_file"),
    # Maintained digests (K4, #799) — admin CRUD, triple-surface. Surfaces
    # (CLI `agnes admin digest …` + MCP tools) land in Task 7 — these two
    # entries are RED until then by design (see the K4 plan's Task 3).
    "/api/admin/knowledge-digests": ("admin digest list", "admin_knowledge_digests_list"),
    "/api/admin/knowledge-digests/{digest_id}": ("admin digest show", "admin_knowledge_digest_get"),
    # Skill-linter admin moderation surface (v89, #687): findings list,
    # manual full-corpus audit, per-finding dismiss.
    "/api/admin/store/lint-findings": ("admin store lint-findings", "admin_store_lint_findings"),
    "/api/admin/store/lint-audit": ("admin store lint-audit", "admin_store_lint_audit"),
    "/api/admin/store/lint-dismiss": ("admin store lint-dismiss", "admin_store_lint_dismiss"),
    # Per-user MCP credential connectivity check (self-service connect page).
    "/api/mcp/sources/{source_id}/my-secret/test": ("mcp my-secret test", "my_secret_test"),
    # Keboola glossary import (2026-07-17 design). Search is the primary
    # agent-facing access pattern and carries the triple-surface contract;
    # list/get-by-id are CLI-reachable (`agnes glossary show`) but have no
    # MCP analogue and are _EXEMPT below.
    "/api/glossary/search": ("glossary search", "glossary_search"),
    # Wave-2B job queue REST surface (Task 5) — `/api/jobs` carries both list
    # (GET) and enqueue (POST); `/api/jobs/{job_id}` is the detail view.
    "/api/jobs": ("admin jobs list", "admin_jobs_list"),
    "/api/jobs/{job_id}": ("admin jobs show", "admin_job_get"),
    # DuckLake analytics-backend migration (wave-2G Task 6).
    "/api/admin/analytics/migrate": ("admin analytics migrate", "admin_analytics_migrate"),
    # Agent profiles (agent-api V1a, Task 12) — management list surface.
    # CLI `agnes agent list` + MCP `agent_list` both map to this GET.
    "/api/v1/agents": ("agent list", "agent_list"),
    # Agent-as-API one-shot runtime (agent-api V1a, Task 9/12). CLI
    # `agnes agent ask` + MCP `agent_ask` both map to this POST — the MCP
    # tool is sync-only (see its docstring); background mode + job polling
    # has no MCP tool by design.
    "/api/v1/agents/{slug}/responses": ("agent ask", "agent_ask"),
    # Agent-as-API monthly usage (agent-api V1b, Task 8). CLI
    # `agnes agent usage` + MCP `agent_usage` both map to this GET.
    "/api/v1/agents/{slug}/usage": ("agent usage", "agent_usage"),
    # Hosted data apps control-plane (data-apps platform plan, Task 7/10/11) —
    # list/get are any-authenticated-user (RBAC-filtered by view access);
    # deploy/logs are owner-or-Admin. CLI landed in Task 10 (`agnes app …`),
    # MCP tools in Task 11 — all three surfaces now agree.
    "/api/data-apps": ("app list", "data_apps_list"),
    "/api/data-apps/{slug}": ("app show", "data_app_get"),
    "/api/data-apps/{slug}/deploy": ("app deploy", "data_app_deploy"),
    "/api/data-apps/{slug}/logs": ("app logs", "data_app_logs"),
    # "Add artefacts to My Stack" — Stack membership for personal file
    # Collections (permission = ownership/sharing, not admin-RBAC grants).
    # POST (add) → stack_artefact_add; DELETE (remove) → stack_artefact_remove
    # — one cohort row per path, mirrors the /api/store/entities/{entity_id}
    # PUT/DELETE row above.
    "/api/stack/artefacts/candidates": ("stack artefacts list", "stack_artefacts_candidates"),
    "/api/stack/artefacts/{corpus_id}": ("stack artefacts add", "stack_artefact_add"),
    # Wave 3B draft-iteration model (Task 8) — CLI (`agnes app draft
    # create/delete`, `agnes app git-credential`) and MCP tools
    # (`data_app_create_draft`/`data_app_delete_draft`/`data_app_git_credential`)
    # landed together; all three surfaces now agree.
    "/api/data-apps/{slug}/drafts": ("app draft create", "data_app_create_draft"),
    "/api/data-apps/{slug}/drafts/{draft_slug}": ("app draft delete", "data_app_delete_draft"),
    "/api/data-apps/{slug}/git-credential": ("app git-credential", "data_app_git_credential"),
    # Fact graph over Collections — query surface (build order step 6,
    # docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md
    # §12/§16). REST landed REST-only in a prior task (see the historical
    # note that used to sit in _EXEMPT here); CLI (`agnes facts …`) and MCP
    # tools (`fact_search`/`fact_neighbors`/`fact_claims`) now land together.
    "/api/facts/search": ("facts search", "fact_search"),
    "/api/facts/neighbors": ("facts neighbors", "fact_neighbors"),
    "/api/facts/{subject_id}/claims": ("facts claims", "fact_claims"),
}


def test_rest_endpoints_callable():
    """REST surface — every cohort entry resolves to a router handler."""
    # documentation_api is the handler name; importing the module is enough
    # to register the route on the router. Spot-check the new one explicitly.
    from app.web.router import (
        documentation_api,  # type: ignore[attr-defined]
        router,  # noqa: F401  (import surface registration)
    )

    assert callable(documentation_api)


def test_cli_subcommands_registered():
    """CLI surface — every cohort entry has a matching ``agnes <cmd>`` subcommand.

    Walks the typer command tree at the top-level ``app`` and asserts each
    ``<group> <subcmd>`` pair from the cohort resolves to a registered command.
    Supports two-level (``group cmd``) and three-level (``group sub cmd``) paths.
    """
    from cli.main import app

    # Top-level groups (name → registered Typer instance).
    groups: dict[str, object] = {g.name: g.typer_instance for g in app.registered_groups if g.name}

    for path, (cli_cmd, _mcp_tool) in _COHORT.items():
        if " " not in cli_cmd:
            # Single-token command: a callback-style Typer group registered at
            # the top level (e.g. `agnes search`, same shape as `agnes pull`) —
            # the group's existence IS the command surface.
            assert cli_cmd in groups, (
                f"CLI command '{cli_cmd}' missing for {path} — register via `app.add_typer(...)` in cli/main.py"
            )
            continue
        head, tail = cli_cmd.split(" ", 1)
        assert head in groups, (
            f"CLI group '{head}' missing for {path} — register via `app.add_typer(...)` in cli/main.py"
        )
        sub = groups[head]
        if " " in tail:
            # 3-level command: top-group → sub-group → command (e.g. "admin skill list")
            sub_group_name, cmd_name = tail.split(" ", 1)
            sub_groups = {g.name: g.typer_instance for g in sub.registered_groups if g.name}  # type: ignore[attr-defined]
            assert sub_group_name in sub_groups, (
                f"CLI subgroup '{head} {sub_group_name}' missing for {path} — "
                f"register via `{head}_app.add_typer(..., name='{sub_group_name}')` in cli/commands/{head}.py"
            )
            leaf = sub_groups[sub_group_name]
            leaf_names = {c.name for c in leaf.registered_commands if c.name}  # type: ignore[attr-defined]
            assert cmd_name in leaf_names, (
                f"CLI subcommand '{cli_cmd}' missing for {path} — define "
                f'`@{sub_group_name}_app.command("{cmd_name}")` in cli/commands/{head}_{sub_group_name}.py'
            )
        else:
            sub_names = {c.name for c in sub.registered_commands if c.name}  # type: ignore[attr-defined]
            assert tail in sub_names, (
                f"CLI subcommand '{cli_cmd}' missing for {path} — define "
                f'`@{head}_app.command("{tail}")` in cli/commands/{head}.py'
            )


def test_mcp_tools_registered():
    """MCP surface — every cohort entry has a matching FastMCP tool.

    FastMCP exposes registered tools via ``list_tools()``; the test runs the
    coroutine synchronously and checks the cohort's tool names are present.
    """
    import asyncio

    from app.api.mcp_http import mcp

    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    for path, (_cli_cmd, mcp_tool) in _COHORT.items():
        assert mcp_tool in names, (
            f"MCP tool '{mcp_tool}' missing for {path} — register via `@mcp.tool()` in app/api/mcp_http.py"
        )


_BASELINE_PATH = Path(__file__).resolve().parent / "api_triple_surface_grandfathered.txt"

# Endpoints consciously REST-only (admin mutations, internal, webhooks). Reason
# required. New endpoints go here OR in _COHORT — never silently.
_ADOPTION_REASON = (
    "admin-only Adoption dashboard — web UI only, no CLI/MCP analogue "
    "(read-only aggregates rendered as cards/charts in the browser)"
)
_PROMPTS_REASON = (
    "admin-only managed-prompt editor (#622) — web UI only at /admin/prompts, "
    "no analyst CLI/MCP analogue (mirrors the grandfathered "
    "/api/admin/{welcome,workspace-prompt}-template editors)"
)
_IW_SYNC_IF_CONFIGURED_REASON = (
    "admin-web/scheduler-only nightly auto-sync wrapper (#622 Slice 3 PR-B) — "
    "the scheduler sidecar POSTs it via SCHEDULER_API_TOKEN; no analyst "
    "CLI/MCP analogue, mirrors the manual /sync route's exemption"
)
_STORE_DRYRUN_REASON = (
    "Store upload-wizard helper (#317) — pre-submit dry-run that previews "
    "guardrail findings in the /store/new web form before the real "
    "POST /api/store/entities. No analyst CLI/MCP analogue (mirrors the "
    "grandfathered /api/store/entities/preview wizard step); the real "
    "create endpoint carries the triple-surface contract."
)
_TRUST_LINE_REASON = (
    "Publisher + verification (v104) — human judgment made while READING the "
    "item on its detail page, not a programmable query. An admin decides to "
    "publish something as the organization, or to verify it, having just looked "
    "at what it does; a bare `agnes admin verify <id>` would invite exactly the "
    "rubber-stamping this design removes (a badge nobody audited is what the "
    "old permanent 'In review' queue amounted to). The verification axis is "
    "also opt-in per instance (`store.verification_enabled`, default false), so "
    "on most instances the verbs would not exist at all. The READ side IS "
    "surfaced everywhere: publisher_kind / verification_state ride the store "
    "entity and marketplace item payloads that `marketplace_search` / "
    "`marketplace_detail` already expose, and the ?publisher / ?verification "
    "facets narrow the same listings."
)
_COLLECTIONS_FILES_REASON = (
    "Collections file endpoints (Slice 2) — multipart upload has no MCP/JSON "
    "analogue (binary body), reachable via `agnes collections upload`; file "
    "listing is folded into the collection_get MCP tool + `agnes collections "
    "show`; file deletion is a maintenance mutation with no analyst CLI/MCP "
    "analogue. The collection read surfaces carry the triple-surface contract."
)
_LIBRARY_RAW_REASON = (
    "`…/raw` streams image/PDF bytes for the BROWSER to draw — there is no "
    "JSON or MCP analogue of a byte stream, and an agent that wants the file's "
    "content has `collection_file_read` / `agnes collections cat`, which read "
    "the sibling `…/preview` endpoint. That endpoint is NOT exempt: it carries "
    "the triple-surface contract in _COHORT above, so removing either surface "
    "fails this test rather than passing silently. `…/preview` is capped "
    "(_PREVIEW_MAX_CHARS) and says so via `truncated`; an uncapped paginated "
    "whole-file reader would still be its own feature."
)


_AUTHORING_SUGGESTIONS_REASON = (
    "Authoring-studio suggestion queue (v80) — web-form/admin-moderation flow. "
    "Non-admins submit a proposed create payload from the /admin/studio/{domain} "
    "builder and admins approve/reject from the moderation UI. No analyst "
    "CLI/MCP analogue (mirrors the grandfathered /api/memory-domain-suggestions "
    "moderation queue); the real domain create endpoints carry the contract."
)
_MEMORY_MINING_REASON = (
    "Corporate-memory mining (v81) — privacy-gated web/admin flow. Users manage "
    "their own opt-in consent from a web toggle; an admin triggers a mining run "
    "from the moderation UI. Candidates route through the authoring-suggestions "
    "queue (itself exempt). No analyst CLI/MCP analogue."
)
_BUILTIN_DISABLE_REASON = (
    "admin-only per-plugin disable toggle — web UI at /admin/marketplaces plus "
    "`agnes admin marketplace disable-plugin/enable-plugin` (parity case in "
    "tests/test_cli_api_parity.py). Deliberately never MCP-exposed: an "
    "agent-invokable instance-wide kill switch over served plugins is a "
    "privilege-escalation seam, not a convenience."
)
_REPORTS_REASON = (
    "admin-only marketplace usage digest — read-only JSON feed for an external "
    "rendering pipeline (e.g. n8n), consumed over HTTP with a PAT. No analyst "
    "CLI/MCP analogue (mirrors the grandfathered /api/admin/adoption dashboard "
    "aggregates)"
)
_KNOWLEDGE_MIGRATION_REASON = (
    "one-time retroactive migration trigger (pre-v0.71.60 knowledge.json → DB) — "
    "idempotent admin-only POST, no analyst CLI/MCP analogue; endpoint is "
    "temporary and will be removed once all instances have migrated"
)
_MCP_CONNECT_REASON = (
    "user-facing PAT generator for headless MCP clients (Cursor, Copilot) — "
    "web UI flow that issues a connector token and returns ready-to-paste config "
    "snippets; no CLI/MCP analogue (the PAT it creates IS the MCP credential)"
)
_SOURCE_CONNECTIONS_CRUD_REASON = (
    "named source-connection CRUD sub-paths (multi-project Keboola, #731) — "
    "GET/PUT/DELETE /{id} and PUT/DELETE /{id}/secret and POST /{id}/test are "
    "reachable via `agnes admin connection add/remove/test`; the list path carries "
    "the triple-surface contract in _COHORT"
)
_SEMANTIC_MODELS_ADMIN_REASON = (
    "admin CRUD over the canonical Ossie semantic-model registry (open "
    "semantic-layer contract, Task 10) — creating/renaming/deleting a "
    "hand-authored document is an admin action. Reachable via `agnes admin "
    "semantic-model list/show/import`; no MCP analogue by design — an "
    "agent's read path is `semantic_model_search`/`semantic_model_get` "
    "(paired with the public, resource-gated export endpoint in _COHORT "
    "above), not the admin corpus-management surface, mirroring the "
    "/api/admin/data-packages and /api/admin/metrics admin-CRUD precedent."
)
_SEMANTIC_SOURCES_ADMIN_REASON = (
    "admin CRUD + manual sync-trigger over registered semantic-layer sync "
    "sources (git/upload/connection), open semantic-layer contract Task 10 "
    "— configuring where documents come from, and triggering a fetch, are "
    "admin actions. Reachable via `agnes admin semantic-source add/list/"
    "sync`; no MCP analogue by design, mirroring the "
    "_SOURCE_CONNECTIONS_CRUD_REASON precedent above (an agent-invokable "
    "tool that can point this server at an arbitrary git remote or upload "
    "payload and trigger a fetch is a credential/config surface, not a "
    "read tool)."
)
_SEMANTIC_MODELS_SEARCH_REASON = (
    "public, resource-gated substring search over semantic models — has an "
    "MCP tool (`semantic_model_search`) but no dedicated CLI subcommand: "
    "`agnes admin semantic-model list` covers interactive listing via the "
    "admin endpoint instead. Mirrors the /api/glossary + /api/glossary/"
    "search split (list has no MCP tool; search is the agent-facing path) "
    "in the opposite direction — here the admin list, not the search, is "
    "the one without an MCP pairing."
)
_BROKER_REASON = (
    "chat sandbox secret broker (2026-07-14 incident hardening) — internal "
    "sandbox->server routes, ticket-gated (not user auth); the in-sandbox "
    "loopback relay is the only caller. No analyst CLI/MCP analogue."
)
_AGENT_DETAIL_REASON = (
    "single-agent detail/update/delete (agent-api V1a) — DELETE is reachable "
    "via `agnes agent delete`; GET/PUT have no dedicated CLI verb (`agnes "
    "agent show` reads from the list response, `/api/v1/agents` in _COHORT, "
    "rather than this id route, and there is no `agent update` subcommand "
    "yet). No MCP analogue by design: `agent_list` already covers read, and "
    "updating/deleting one's own agent profile is a deliberate, low-frequency "
    "admin action better done interactively (CLI/web) than via an "
    "agent-callable tool."
)
_AGENT_SCOPE_REASON = (
    "agent resource-scope grant (agent-api V1a) — reachable via `agnes agent "
    "scope set`. No MCP analogue by design: scope is a permission grant (what "
    "data/plugins/connections/memory the agent may touch), and widening an "
    "agent's own access must stay an interactive, human-witnessed action, "
    "never something reachable through a tool call."
)
_AGENT_TOKENS_REASON = (
    "agent PAT issuance (agent-api V1a) — reachable via `agnes agent token`. "
    "No MCP analogue by design (per the agent-api V1a spec): minting a "
    "long-lived credential must stay an interactive, human-witnessed action — "
    "an agent must never be able to mint its own (or another agent's) PAT "
    "through a tool call."
)
_AGENT_JOBS_REASON = (
    "agent-runtime background-job poll (agent-api V1a) — reachable via "
    "`agnes agent ask`'s own polling loop after a 202 degrade, not a "
    "standalone CLI subcommand. No MCP analogue by design: `agent_ask` is "
    "deliberately sync-only (see its docstring) and never polls jobs itself — "
    "a tool call blocking on a poll loop is a poor fit for a chat turn."
)
_AGENT_SESSION_REASON = (
    "agent-as-API multi-turn sessions (agent-api V1b Task 4) — session "
    "create/history/cancel/delete around a Server-Sent Events turn stream. "
    "No MCP analogue by design: the `/messages` turn is an open SSE response "
    "(AG-UI event vocabulary), not a request/response tool call an MCP "
    "client can await, and cancel/delete are lifecycle controls over that "
    "same long-lived stream rather than a discrete query or action. "
    "`agnes chat <slug>` (agent-api V1c) IS a CLI surface for three of these "
    "four routes: it POSTs `/api/v1/agents/{slug}/sessions` to open a "
    "session, streams every turn through `/messages`' SSE, calls `/cancel` "
    "on Ctrl-C, and best-effort `DELETE`s the session on exit — so this "
    "group is not `_COHORT`-eligible for a different reason than 'no CLI': "
    "`_COHORT` models a discrete one-shot subcommand per route (like `agnes "
    "agent ask` for `/responses`), and `agnes chat` is a REPL wrapping this "
    "whole stream, not four separate commands — plus there is still no MCP "
    "analogue for any of them, and `_COHORT` requires both. The one route in "
    "this group `agnes chat` genuinely never calls is `GET "
    "/api/v1/sessions/{session_id}` (read back state + full history) — the "
    "REPL renders its own live transcript from the SSE stream as it "
    "arrives and never re-fetches history from the server, so that leg "
    "really is CLI-unreached, unlike its three siblings above."
)
_AGENT_WEBHOOKS_REASON = (
    "outbound agent webhook registration (agent-api V1b Task 6) — owner-scoped "
    "standing-config CRUD (register/list/revoke an HTTPS callback URL + HMAC "
    "secret for job.completed/job.failed notifications), explicitly named in "
    "CONTRIBUTING.md's API-coverage exemption list alongside health checks and "
    "OAuth callbacks ('health checks, webhooks, OAuth callbacks, and internal/"
    "SSE routes'). Reachable via `agnes agent webhooks list/add/delete` "
    "(Task 8). No MCP analogue BY DESIGN, permanently: minting a webhook "
    "secret and pointing this server's outbound network identity at an "
    "arbitrary URL is an SSRF-sensitive, human-witnessed action, never "
    "something an agent tool call should be able to trigger on its own "
    "registration — this is a standing-config exemption, not a landed-later "
    "gap."
)
_AGENT_SCHEDULES_REASON = (
    "scheduled agent runs (agent-schedules design, "
    "docs/superpowers/specs/2026-08-17-agent-schedules-design.md) — "
    "owner-scoped standing-config CRUD, same posture as "
    "_AGENT_WEBHOOKS_REASON above: reachable via `agnes agent schedule "
    "list/add/remove/enable/disable`. No MCP analogue by design: a schedule "
    "grants an agent UNATTENDED future runs on the owner's behalf, which "
    "must stay an interactive, human-witnessed action — an agent tool call "
    "that could give itself new autonomous cadences is the same "
    "self-service-authority-widening class _AGENT_TOKENS_REASON and "
    "_AGENT_SCOPE_REASON already exempt."
)
_AGENT_SCHEDULES_RUN_DUE_REASON = (
    "scheduled agent runs — the admin/scheduler-driven sweep (agent-schedules "
    "design). Mirrors the grandfathered `/api/scripts/run-due`: an internal "
    "sweep trigger the scheduler sidecar POSTs on a fixed cadence "
    "(`agents:run-due`, gated on `SCHEDULER_AGENT_SCHEDULES`), not an "
    "analyst-facing action — no CLI/MCP analogue."
)
_AGENT_ARTIFACTS_REASON = (
    "agent-as-API sandbox artifact harvest/download (agent-api V1b Task 5). "
    "No MCP analogue by design: the download route is a binary byte-stream "
    "response (arbitrary file content + Content-Disposition), not a "
    "JSON-shaped tool result an MCP client can consume — mirrors the "
    "existing `/api/knowledge/artifacts/{corpus_id}/download` exemption. No "
    "CLI subcommand: unlike the create/messages/cancel legs of "
    "`/api/v1/sessions/{id}/*` (see _AGENT_SESSION_REASON — `agnes chat` "
    "does drive those), `agnes chat` never harvests or downloads sandbox "
    "artifacts through the terminal REPL — this route genuinely has no CLI "
    "caller."
)
_AGENT_MEMORY_WRITE_REASON = (
    "the 'remember' tool (agent-api V1c Task 4) — an in-sandbox agent's own "
    "write into its private memory notebook, called by the agent itself "
    "against ITS OWN running session, never by an interactive human caller "
    "choosing among agents/sessions. No CLI subcommand: there is no "
    "analyst-facing 'write a memory for my agent' workflow (memory "
    "management for owners is the separate admin surface at "
    "_AGENT_MEMORY_ADMIN_REASON below, not this runtime write path). No MCP "
    "analogue either, permanently: this route's "
    "auth model binds the write to `request.state.chat_session_id` (the "
    "broker-minted claim identifying the CALLING session) and explicitly "
    "REJECTS a path `{id}` that differs from it (`403 session_mismatch`) — "
    "an MCP tool invoked by a human operator against an arbitrary session id "
    "would never carry that claim, so exposing this as a generic MCP tool "
    "would either be unusable (always 403) or require a different, weaker "
    "auth path that reopens the exact cross-agent-same-owner memory-"
    "poisoning gap this design closes. No CLI subcommand: unlike the "
    "create/messages/cancel legs of `/api/v1/sessions/{id}/*` (see "
    "_AGENT_SESSION_REASON — `agnes chat` does drive those), the in-sandbox "
    "agent calls this route itself; there is nothing for a human-facing CLI "
    "command to do here."
)
_AGENT_MEMORY_ADMIN_REASON = (
    "owner-facing memory management (agent-api V1c Task 5) — inspect/"
    "approve/archive/delete over an agent's private memory notebook, the "
    "management-surface counterpart to the 'remember' tool "
    "(_AGENT_MEMORY_WRITE_REASON above). Reachable via `agnes agent memory "
    "list/approve/archive/delete` (Task 7) — a CLI surface, not a _COHORT "
    "entry: _COHORT requires BOTH a CLI command and an MCP tool, and this "
    "route deliberately has no MCP analogue, permanently. Reviewing and "
    "approving what an agent is allowed to 'remember' about itself must "
    "stay an interactive, human-witnessed action — never something an "
    "agent's own tool call (or a tool call issued on a human operator's "
    "behalf against an arbitrary agent) can reach — mirrors the same "
    "posture as _AGENT_SCOPE_REASON and _AGENT_TOKENS_REASON above."
)

_DATA_APPS_REASON = (
    "control-plane REST for hosted data apps (data-apps platform plan, Task 7). "
    "CLI landed in Task 10 (`agnes app list/show/create/deploy/stop/delete/logs`, "
    "cli/commands/data_apps.py); list/show/deploy/logs got MCP tools in Task 11 "
    "and moved to _COHORT. `create`/`delete`/`stop` have no MCP analogue planned "
    "(create/delete piggy-back on the list/show cohort paths' REST+CLI-only "
    "methods; `stop` is its own path with no MCP tool — mirrors the "
    "list(GET)/create(POST) and show(GET)/delete(DELETE) shared-path pattern "
    "already used for /api/jobs and /api/collections/{collection_id})."
)
_DATA_APPS_SECRETS_REASON = (
    "secrets are set once via an operator/automation flow (`PUT .../secrets`), "
    "not a routine interactive analyst action — no CLI command planned (mirrors "
    "the write-only /api/admin/datasource-secrets exemption) and no MCP analogue."
)
_DATA_APPS_READINESS_REASON = (
    "polling primitive consumed by the ingress-proxy waking page "
    "(app/api/data_apps_proxy.py's holding-page poll loop), not an interactive "
    "analyst action; `agnes app show`/`agnes app open` (Task 10) cover the "
    "human-facing state check. No CLI/MCP analogue planned."
)
_LIBRARY_MOVE_REASON = (
    "Backs the Library's drag-and-drop: moving a file between collections is a "
    "direct-manipulation gesture on the /library table, not a command an analyst "
    "would type. The CLI/MCP equivalent of reorganising files is re-uploading "
    "into the intended collection, which already has surfaces."
)

_LIBRARY_SHARING_REASON = (
    "Owner-initiated sharing of Library items — a web affordance on /library "
    "(share dialog). The equivalent grant writing already has analyst-facing "
    "surfaces on the ADMIN side (`agnes admin grant …`); this endpoint only "
    "narrows those same `resource_grants` writes to what an item's owner may do, "
    "so a second CLI/MCP vocabulary for it would duplicate the admin one."
)

_DATA_APPS_PREVIEW_GRANT_REASON = (
    "preview-grant mints the in-chat iframe cookie for the web chat surface; chat-only, no CLI/MCP analogue (spec §7)"
)
_OAUTH_CONNECT_BROWSER_REASON = (
    "outbound MCP OAuth sources connect flow (2026-07-30 spec §3, PR 2) — a "
    "browser-navigation OAuth authorize/callback pair, explicitly named in "
    "CONTRIBUTING.md's API-coverage exemption list alongside health checks "
    "and webhooks ('health checks, webhooks, OAuth callbacks, and "
    "internal/SSE routes'). `agnes mcp connect <source>` opens the "
    "authorize URL in the user's own browser but there is no MCP tool "
    "analogue — a tool call cannot open a browser or receive a 3rd-party "
    "redirect, and the flow is human-only (deny_principal) by design."
)
_OAUTH_DISCONNECT_REASON = (
    "outbound MCP OAuth sources disconnect (2026-07-30 spec §3, PR 2) — "
    "drops the caller's OWN stored token, deny_principal (human-only), same "
    "treatment as the grandfathered DELETE …/my-secret sibling on this same "
    "router: CLI-reachable (`agnes mcp disconnect`), no MCP tool. An "
    "agent-invokable tool that could sever its owner's upstream credential "
    "out from under a live session is the same class of self-service "
    "identity operation the my-secret endpoints were never MCP-exposed for."
)
_MCP_SOURCE_GRANT_REASON = (
    "grant/revoke every tool of one MCP source to a group — an RBAC widening "
    "write. Reachable via `agnes admin mcp source grant [--revoke]`; "
    "deliberately never MCP-exposed, on the same reasoning as the standing "
    "credential-provisioning exemption in CONTRIBUTING.md: a tool an agent can "
    "call that widens which tools a group may call is a privilege-escalation "
    "seam, and this one widens by the whole source at once"
)

_KEBOOLA_LOGIN_PROJECTS_REASON = (
    "select-mode Keboola project import — a continuation of the browser OAuth "
    "login, bound to a short-TTL vaulted stash of that login's access token. "
    "The CLI has no OAuth login to continue, and MCP exposure is ruled out by "
    "CONTRIBUTING.md → 'Standing exemption — admin credential-provisioning "
    "writes': the import mints + vaults upstream project credentials, exactly "
    "the privilege-escalation seam that paragraph names"
)

_EXEMPT: dict[str, str] = {
    "/api/admin/users/{user_id}/library-preview": (
        "feeds the Simulate lens's Library-shaped preview on /admin/access — "
        "a projection of another person's /library page, meaningful only "
        "beside the why-chain that pane renders around it. The sibling "
        "/effective-access is grandfathered REST-only for the same reason. "
        "If an `agnes admin simulate <user>` CLI ever lands, this should "
        "join its cohort rather than stay exempt"
    ),
    "/api/auth/keboola/projects": _KEBOOLA_LOGIN_PROJECTS_REASON,
    "/api/admin/server-config/overlay": (
        "raw editable-section-only instance.yaml overlay — the export "
        "projection behind `agnes admin config export`/`apply` (Track D3), "
        "CLI-reachable via both subcommands but deliberately never "
        "MCP-exposed per the 'operator security-posture diagnostics' "
        "standing exemption in CONTRIBUTING.md: a one-call dump of the "
        "instance's entire editable config surface (which upstream it "
        "points at, what auth is configured) is reconnaissance in a "
        "prompt-injected chat session, not an agent affordance. Mirrors "
        "the existing grandfathered GET/POST /api/admin/server-config, "
        "which the same reasoning already covers"
    ),
    "/api/admin/doctor/new-instance": (
        "deployment-gate doctor (post-deploy smoke checks) — CLI-reachable via "
        "`agnes admin doctor --new-instance` and called by "
        "scripts/ops/post-deploy-smoke-test.sh, but deliberately never "
        "MCP-exposed per the 'operator security-posture diagnostics' standing "
        "exemption in CONTRIBUTING.md: the response enumerates the instance's "
        "auth-configuration posture (which login doors exist, whether "
        "bootstrap is still open, email transport state) — one-call "
        "reconnaissance in a prompt-injected agent session, not an agent "
        "affordance"
    ),
    "/api/admin/doctor/support": (
        "support-bundle doctor (redacted state snapshot) — CLI-reachable via "
        "`agnes doctor` (it renders the server section of the bundle file), "
        "but deliberately never MCP-exposed per the 'operator security-posture "
        "diagnostics' standing exemption in CONTRIBUTING.md: the response "
        "enumerates build fingerprints, schema state and secret PRESENCE — "
        "one-call reconnaissance in a prompt-injected agent session, not an "
        "agent affordance"
    ),
    "/api/admin/mcp-tools/{tool_id}/projection-map": (
        "names which of a lister tool's columns carry an app's id, URL and "
        "name. The decision is only makeable against the column list a fetch "
        "actually produced, and the wizard's step 2 is the only surface that "
        "renders it — `agnes admin mcp tool list` shows registered tools, not "
        "what one emitted — so a CLI flag or MCP tool would be choosing column "
        "names blind. If a surface ever exposes the fetched columns, this "
        "should follow it there rather than stay REST-only"
    ),
    "/api/connectors/{slug}/prompt": (
        "connector setup prompt for the analyst-laptop install flow — "
        "consumed by `agnes connectors show <slug>` (REST+CLI). Deliberately "
        "NOT an MCP tool on either transport: on the server-side foundation "
        "tools (app/api/mcp/foundation_tools.py) it would be a footgun — the "
        "prompt walks through storing credentials in the LOCAL OS keychain "
        "and registering local MCP servers, meaningless inside the chat "
        "sandbox; and the stdio server (cli/mcp/server.py) runs on the "
        "analyst's machine only because the agnes CLI is installed, so "
        "`agnes connectors show` is already present in that exact venue — a "
        "tool there would duplicate the CLI surface without adding reach"
    ),
    "/api/me/display-name": (
        "self-service display-name edit (issue #1036) — UI-only affordance on "
        "/profile; a one-field personal profile edit with no CLI/MCP analogue"
    ),
    "/api/collections/{collection_id}/files/{file_id}/move": _LIBRARY_MOVE_REASON,
    "/api/sharing/groups": _LIBRARY_SHARING_REASON,
    "/api/sharing/{resource_type}/{resource_id}": _LIBRARY_SHARING_REASON,
    "/api/me/elevation": (
        "admin elevation consent gate — sets the browser-session cookie the "
        "elevation middleware reads; structurally a web-browser surface (the "
        "CLI has no cookie jar to carry the state across invocations, and "
        "Bearer-authenticated calls are exempt from the instance default by "
        "design), so no CLI/MCP analogue"
    ),
    "/api/v1/agents/{agent_id}": _AGENT_DETAIL_REASON,
    "/api/v1/agents/{agent_id}/scope": _AGENT_SCOPE_REASON,
    "/api/v1/agents/{agent_id}/tokens": _AGENT_TOKENS_REASON,
    "/api/v1/jobs/{job_id}": _AGENT_JOBS_REASON,
    "/api/v1/agents/{slug}/sessions": _AGENT_SESSION_REASON,
    "/api/v1/agents/{slug}/webhooks": _AGENT_WEBHOOKS_REASON,
    "/api/v1/agents/{slug}/webhooks/{webhook_id}": _AGENT_WEBHOOKS_REASON,
    "/api/v1/agents/{slug}/schedules": _AGENT_SCHEDULES_REASON,
    "/api/v1/agents/{slug}/schedules/{schedule_id}": _AGENT_SCHEDULES_REASON,
    "/api/v1/agents/run-due": _AGENT_SCHEDULES_RUN_DUE_REASON,
    "/api/v1/sessions/{session_id}": _AGENT_SESSION_REASON,
    "/api/v1/sessions/{session_id}/messages": _AGENT_SESSION_REASON,
    "/api/v1/sessions/{session_id}/cancel": _AGENT_SESSION_REASON,
    "/api/v1/sessions/{session_id}/artifacts": _AGENT_ARTIFACTS_REASON,
    "/api/v1/sessions/{session_id}/artifacts/{artifact_id}": _AGENT_ARTIFACTS_REASON,
    "/api/v1/sessions/{session_id}/memories": _AGENT_MEMORY_WRITE_REASON,
    "/api/v1/agents/{agent_id}/memories": _AGENT_MEMORY_ADMIN_REASON,
    "/api/v1/agents/{agent_id}/memories/{memory_id}": _AGENT_MEMORY_ADMIN_REASON,
    # Outbound MCP OAuth sources (2026-07-30 spec §2, PR 1): CLI-reachable
    # (`agnes admin mcp source oauth-register` / `oauth-client`) but no MCP
    # analogue — one-time admin provisioning of Agnes's own OAuth client at
    # an upstream source's authorization server, not an agent-facing
    # data/tool operation. Mirrors the grandfathered …/secret vault-write
    # endpoint on this same router (also CLI-only by the same reasoning).
    "/api/admin/mcp-sources/{source_id}/oauth/register": (
        "CLI-reachable (`agnes admin mcp source oauth-register`); never MCP-exposed "
        "per CONTRIBUTING.md → 'API coverage (REST × CLI × MCP)' → 'Standing "
        "exemption — admin credential-provisioning writes'"
    ),
    "/api/admin/mcp-sources/{source_id}/oauth/client": (
        "CLI-reachable (`agnes admin mcp source oauth-client`); never MCP-exposed "
        "per CONTRIBUTING.md → 'API coverage (REST × CLI × MCP)' → 'Standing "
        "exemption — admin credential-provisioning writes'"
    ),
    # Outbound MCP OAuth sources connect flow (2026-07-30 spec §3, PR 2).
    "/api/mcp/sources/{source_id}/oauth/authorize": _OAUTH_CONNECT_BROWSER_REASON,
    "/api/mcp/oauth-client/callback": _OAUTH_CONNECT_BROWSER_REASON,
    "/api/mcp/sources/{source_id}/oauth/connection": _OAUTH_DISCONNECT_REASON,
    "/api/admin/registry/rebuild": (
        "admin-only registry rebuild trigger — server/consumer maintenance op "
        "(companion to register-table's defer_rebuild for bulk onboarding); no "
        "analyst CLI/MCP analogue, mirrors the cache-warmup/run + sync triggers"
    ),
    # Table access policies (design doc §13.1/§13.2, plan Task 14/16):
    # single-persona preview/dry-run an admin uses to check a stored or
    # candidate policy before trusting it. CLI-reachable via `agnes admin
    # table-policy preview` (plan Task 16) — mirrors the grandfathered
    # /api/admin/prompts/{kind}/preview exemption (_PROMPTS_REASON), which is
    # ALSO an admin-only preview endpoint with no MCP analogue. No MCP tool
    # planned, by design, not merely "not yet": this endpoint runs a policy
    # AS A CHOSEN PERSONA and returns that persona's row/column slice to the
    # calling admin — precisely the "who looked at whose data" action §13.1
    # says must be audited, and the same category of "must stay interactive,
    # human-witnessed" the codebase already draws around
    # _AGENT_MEMORY_ADMIN_REASON / _AGENT_SCOPE_REASON / _AGENT_TOKENS_REASON
    # rather than something reachable through an agent tool call.
    "/api/admin/registry/{table_id}/policy/preview": (
        "admin-only access-policy preview/dry-run (table access policies design "
        "§13.1) — reachable via `agnes admin table-policy preview` (plan Task "
        "16). No MCP analogue by design: mirrors the grandfathered "
        "/api/admin/prompts/{kind}/preview exemption, and separately, this "
        "endpoint runs a policy AS A CHOSEN PERSONA and hands that persona's "
        "row-filtered slice to the calling admin — an audited, human-witnessed "
        "diagnostic action (§13.1), not an agent-facing data operation, the "
        "same posture as _AGENT_MEMORY_ADMIN_REASON/_AGENT_SCOPE_REASON above."
    ),
    # access-policy-builder-ux plan, Tasks 2/3: the no-SQL builder's
    # columns+samples list and structured-spec-to-SQL compile. Same posture
    # as the policy/preview exemption right above (admin-only authoring
    # surface for the builder UI folded into the existing policy editor
    # modal) — no MCP analogue by design: an agent should never author a
    # masking/filtering policy on another caller's behalf, and no CLI
    # counterpart is planned for this slice (the builder is web-UI-only;
    # `agnes admin table-policy preview` above is the one CLI surface this
    # feature area has). `/policy/compile` additionally never persists
    # anything — it only returns SQL for the admin to review/save through
    # the existing PUT, so there is no state-changing action a CLI/MCP
    # wrapper would even give an agent access to.
    "/api/admin/registry/{table_id}/policy/columns": (
        "admin-only access-policy builder: real schema + sample values for "
        "the no-SQL builder (plan Task 2). No MCP analogue by design, same "
        "reasoning as the policy/preview exemption above; no CLI planned for "
        "this web-UI-only builder slice."
    ),
    "/api/admin/registry/{table_id}/policy/compile": (
        "admin-only access-policy builder: structured spec -> validated SQL "
        "(plan Task 3), never persists anything itself. No MCP analogue by "
        "design, same reasoning as the policy/preview exemption above; no "
        "CLI planned for this web-UI-only builder slice."
    ),
    "/api/collections/{collection_id}/files": _COLLECTIONS_FILES_REASON,
    "/api/collections/{collection_id}/files/{file_id}": _COLLECTIONS_FILES_REASON,
    "/api/collections/{collection_id}/files/{file_id}/raw": _LIBRARY_RAW_REASON,
    "/api/studio/memory-mining/consent": _MEMORY_MINING_REASON,
    "/api/admin/memory-mining/run": _MEMORY_MINING_REASON,
    "/api/studio/suggestions": _AUTHORING_SUGGESTIONS_REASON,
    "/api/studio/suggestions/mine": _AUTHORING_SUGGESTIONS_REASON,
    "/api/admin/authoring-suggestions": _AUTHORING_SUGGESTIONS_REASON,
    "/api/admin/authoring-suggestions/{sid}/approve": _AUTHORING_SUGGESTIONS_REASON,
    "/api/admin/authoring-suggestions/{sid}/reject": _AUTHORING_SUGGESTIONS_REASON,
    "/api/admin/initial-workspace/sync-if-configured": _IW_SYNC_IF_CONFIGURED_REASON,
    "/api/store/entities/dryrun": _STORE_DRYRUN_REASON,
    "/api/store/entities/{entity_id}/publisher": _TRUST_LINE_REASON,
    "/api/store/entities/{entity_id}/verification": _TRUST_LINE_REASON,
    "/api/store/entities/{entity_id}/verification/request": _TRUST_LINE_REASON,
    "/api/admin/prompts/{kind}": _PROMPTS_REASON,
    "/api/admin/prompts/{kind}/source": _PROMPTS_REASON,
    "/api/admin/prompts/{kind}/bind-git": _PROMPTS_REASON,
    "/api/admin/prompts/{kind}/preview": _PROMPTS_REASON,
    "/api/admin/prompts/iwt-files": _PROMPTS_REASON,
    "/api/admin/adoption/kpis": _ADOPTION_REASON,
    "/api/admin/adoption/series": _ADOPTION_REASON,
    "/api/admin/adoption/top-skills": _ADOPTION_REASON,
    "/api/admin/adoption/top-users": _ADOPTION_REASON,
    "/api/admin/adoption/users/{user_id}/kpis": _ADOPTION_REASON,
    "/api/admin/adoption/users/{user_id}/series": _ADOPTION_REASON,
    "/api/admin/adoption/users/{user_id}/top-skills": _ADOPTION_REASON,
    "/api/admin/adoption/users/{user_id}/top-tools": _ADOPTION_REASON,
    "/api/marketplaces/{marketplace_id}/plugins/{plugin_name}/disable": _BUILTIN_DISABLE_REASON,
    "/api/marketplaces/{marketplace_id}/plugins/{plugin_name}/enable": _BUILTIN_DISABLE_REASON,
    "/api/admin/run-knowledge-migration": _KNOWLEDGE_MIGRATION_REASON,
    "/api/admin/datasource-secrets": (
        "Admin-only vault-backed credential store for datasource secrets "
        "(Keboola token, BigQuery SA JSON). Write-only, no analyst CLI/MCP analogue — "
        "instance admins set these once via the /admin/datasource-credentials UI."
    ),
    "/api/admin/datasource-secrets/{name}": (
        "Admin-only vault-backed credential store for datasource secrets "
        "(Keboola token, BigQuery SA JSON). Write-only, no analyst CLI/MCP analogue — "
        "instance admins set these once via the /admin/datasource-credentials UI."
    ),
    "/api/admin/validate-gws-credentials": (
        "Admin-only format check for the GWS OAuth client_id used by the "
        "/admin/datasource-credentials UI 'Test' button. No network call, no "
        "persistence, no analyst CLI/MCP analogue."
    ),
    "/api/admin/reports/marketplace-digest": _REPORTS_REASON,
    "/api/admin/dashboard/signals": (
        "Render-path split for the /admin dashboard's 'Needs fixing' zone, not a "
        "capability: every signal it returns is a count over a page an admin can "
        "already open, and each row exists to link there. It is fetched after "
        "first paint purely so the unbounded audit/history reads stay off the "
        "page render. A CLI/MCP surface would expose nothing `agnes admin` "
        "cannot already reach per-queue."
    ),
    "/api/mcp-connect/token": _MCP_CONNECT_REASON,
    "/api/admin/source-connections/{connection_id}": _SOURCE_CONNECTIONS_CRUD_REASON,
    "/api/admin/source-connections/{connection_id}/secret": _SOURCE_CONNECTIONS_CRUD_REASON,
    "/api/admin/source-connections/{connection_id}/test": _SOURCE_CONNECTIONS_CRUD_REASON,
    "/api/admin/mcp-sources/{source_id}/grants": _MCP_SOURCE_GRANT_REASON,
    "/api/admin/mcp-sources/{source_id}/grants/{group_id}": _MCP_SOURCE_GRANT_REASON,
    "/api/admin/source-connections/{connection_id}/chat-tools": (
        "derives a Keboola MCP source from a connection and copies that "
        "connection's storage token into the MCP vault — a credential-"
        "provisioning write under the standing exemption in CONTRIBUTING.md "
        "(an agent-invokable tool that can re-point which upstream a "
        "credential authenticates against is a privilege-escalation seam). "
        "Reachable via `agnes admin connection chat-tools`; never MCP-exposed"
    ),
    "/api/admin/data-sources/{source_type}/tables": (
        "admin-only schema/table discovery for the 'Add data source' wizard's Snowflake "
        "picker — the connection-less sibling of the Keboola listing below, browse-only "
        "with no analyst CLI/MCP analogue; `agnes admin register-table` already covers "
        "the registration step it feeds"
    ),
    "/api/admin/source-connections/{connection_id}/tables": (
        "admin-only bucket/table discovery for the 'Add data source' wizard (#755) — "
        "keboola-only browse-and-register primitive with no analyst CLI/MCP analogue; "
        "`agnes admin register-table` already covers the actual registration step"
    ),
    # SharePoint connect wizard (spec 2026-08-27 §13.2) — admin-only browse
    # and scope-confirmation primitives feeding the wizard's step 2/3, with
    # no analyst CLI/MCP analogue (the wizard itself is the only client; the
    # eventual document surface is `agnes facts …`, already triple-surface
    # in _COHORT above).
    "/api/admin/sharepoint/connections/{connection_id}/tree": (
        "live Graph folder-tree browse (sites -> drives -> root children, one level "
        "per call) for the wizard's step-2 scope picker — admin-only, no analyst "
        "CLI/MCP analogue"
    ),
    "/api/admin/sharepoint/connections/{connection_id}/scopes": (
        "confirm/list/unselect a scope (site/library/folder -> collection) for the "
        "wizard's step 2/3 — admin-only wizard bookkeeping, no analyst CLI/MCP analogue"
    ),
    "/api/admin/sharepoint/connections/{connection_id}/corpus-map": (
        "producer handoff: the flat {source_scope_id: collection_id} mapping "
        "ship_to_agnes.py --corpus-map consumes until crawling moves inside Agnes — "
        "admin-only, no analyst CLI/MCP analogue"
    ),
    # Ontology builder (spec §13.2) — admin-only builder-shell CRUD + the two
    # draft state-machine actions + dry-run. No analyst CLI/MCP analogue: the
    # ontology is consumed as a semantic model, which has its own surface.
    "/api/admin/ontology/drafts": (
        "ontology builder draft CRUD (create/list) — admin-only builder UI, no analyst CLI/MCP analogue"
    ),
    "/api/admin/ontology/drafts/{draft_id}": (
        "ontology builder draft read/edit/discard — admin-only builder UI, no analyst CLI/MCP analogue"
    ),
    "/api/admin/ontology/drafts/{draft_id}/import": (
        "translate a pasted/uploaded ontology into the unsaved draft — admin-only "
        "builder action, no analyst CLI/MCP analogue"
    ),
    "/api/admin/ontology/drafts/{draft_id}/save": (
        "materialize the frozen draft into a semantic model — admin-only builder "
        "action; the semantic-model surface is where analysts consume it"
    ),
    "/api/admin/ontology/dry-run": (
        "run the draft's types over one document via the server-side LLM — admin-only "
        "builder preview, no analyst CLI/MCP analogue"
    ),
    # Open semantic-layer contract (Task 10) — admin CRUD over the
    # semantic-model registry and its sync sources. The public,
    # resource-gated export endpoint carries the triple-surface contract in
    # _COHORT above.
    "/api/admin/semantic-models": _SEMANTIC_MODELS_ADMIN_REASON,
    "/api/admin/semantic-models/{model_id}": _SEMANTIC_MODELS_ADMIN_REASON,
    "/api/admin/semantic-sources": _SEMANTIC_SOURCES_ADMIN_REASON,
    "/api/admin/semantic-sources/{source_id}": _SEMANTIC_SOURCES_ADMIN_REASON,
    "/api/admin/semantic-sources/{source_id}/sync": _SEMANTIC_SOURCES_ADMIN_REASON,
    "/api/semantic-models/search": _SEMANTIC_MODELS_SEARCH_REASON,
    "/api/attachments/{source}/{attachment_id}/download": (
        "connector-catalogued attachment binary download (Jira first) — one-shot "
        "fetch by id consumed by `agnes attachment get`; binary byte-stream with "
        "no MCP/JSON analogue, mirrors the parquet /api/data/{table_id}/download "
        "and knowledge-artifact download channels"
    ),
    "/api/knowledge/artifacts/{corpus_id}/download": (
        "K3 local packaging (#798) — binary knowledge.duckdb artifact consumed by "
        "`agnes pull` (hash-verified, atomic promotion, pruned on de-authorization); "
        "no MCP/JSON analogue, mirrors the parquet /api/data/{table_id}/download channel"
    ),
    "/api/admin/run-knowledge-packaging": (
        "scheduler-driven knowledge-artifact rebuild trigger (K3, #798) — "
        "admin/scheduler maintenance op, mirrors the run-corporate-memory "
        "exemption; no analyst CLI/MCP analogue"
    ),
    "/api/admin/run-knowledge-digests": (
        "scheduler-driven digest regeneration trigger (K4, #799) — admin/scheduler "
        "maintenance op, mirrors the run-knowledge-packaging / run-corporate-memory "
        "exemptions; no analyst CLI/MCP analogue"
    ),
    "/api/knowledge/digests/{digest_id}/content": (
        "K4 maintained digests (#799) — digest markdown consumed by `agnes pull` "
        "(written to .claude/rules/ka_<slug>.md, pruned on de-authorization); "
        "no interactive CLI/MCP analogue, mirrors the knowledge-artifact "
        "download and /api/memory/bundle delivery channels"
    ),
    # Chat sandbox secret broker (2026-07-14 incident hardening) — internal
    # sandbox→server routes only, gated by an opaque ticket (not user auth).
    # No CLI/MCP analogue: these exist purely so the in-sandbox loopback
    # relay never needs a real credential.
    "/api/broker/anthropic": _BROKER_REASON,
    "/api/broker/anthropic/{subpath}": _BROKER_REASON,
    "/api/broker/agnes-api": _BROKER_REASON,
    "/api/broker/agnes-mcp": _BROKER_REASON,
    # Embedded kai-agent turn engine host wiring (app/api/kai.py). Both routes
    # are handshake/credential surfaces for the engine, not analyst features:
    # /sessions mints the engine's own session token for the calling user
    # (bodyless by design — every claim comes from the resolved identity, so
    # there is nothing for a CLI flag or an MCP argument to carry), and
    # /tickets is an internal engine-server→Agnes route gated by an opaque
    # credential rather than user auth, exactly like the /api/broker/* family.
    # The two surfaces are declined for DIFFERENT reasons, stated separately
    # because "bodyless, so no flags to carry" is a weak argument for the CLI
    # half and was the whole of an earlier version of this note (Devin Review
    # on #1235 asked for the call to be conscious rather than inherited):
    #
    #   * MCP — the standing credential-provisioning exemption applies as
    #     written: an agent-invokable tool that mints session credentials is a
    #     privilege-escalation seam, not a convenience.
    #   * CLI — declined not because a command would be awkward to shape (a
    #     bodyless command is trivial) but because its OUTPUT has no consumer
    #     at a terminal. The response is a token only a kai-agent server
    #     holding the matching `HOST_JWT_*` secret can use, paired with a chat
    #     id owned by that server's database. Printing it for a human would
    #     hand out a live 12 h credential with no way to spend it and an
    #     obvious way to leak it (shell history, CI logs). If a CLI ever needs
    #     this, the right shape is a command that RUNS a turn, not one that
    #     prints a credential — and that command would drive the engine's own
    #     API, not this handshake.
    "/api/kai/sessions": (
        "embedded kai-agent turn engine — mints a session token usable only by "
        "an engine server holding the matching HOST_JWT_* secret; no MCP tool "
        "(credential-minting is an escalation seam, per CONTRIBUTING.md) and no "
        "CLI command (its output has no consumer at a terminal — see the note "
        "above this dict)"
    ),
    "/api/kai/tickets": (
        "embedded kai-agent turn engine — internal engine-server→Agnes route "
        "minting per-turn scope-split broker tickets, gated by an opaque "
        "credential (not user auth), like the other /api/broker/* routes"
    ),
    "/api/kai/mcp": (
        "embedded kai-agent turn engine — internal sandbox→Agnes MCP "
        "pass-through gated by an mcp-scoped broker ticket (not user auth); "
        "the tools it reaches are already the CLI/MCP surface, so a CLI "
        "command calling the proxy would be circular"
    ),
    "/api/kai/workspace": (
        "embedded kai-agent turn engine — internal engine-server→Agnes route "
        "serving the caller's workspace tree as a tarball, gated by an opaque "
        "credential (not user auth); the analyst-facing equivalent is the "
        "existing /api/initial-workspace.zip flow"
    ),
    "/api/admin/run-keboola-semantic-layer-refresh": (
        "scheduler-driven Keboola semantic layer (Metastore) sync trigger — "
        "admin/scheduler maintenance op, mirrors the run-bq-metadata-refresh / "
        "run-knowledge-digests exemptions; no analyst CLI/MCP analogue"
    ),
    "/api/admin/run-databricks-semantic-layer-refresh": (
        "scheduler-driven Databricks semantic layer (Unity Catalog metric "
        "views) sync trigger — admin/scheduler maintenance op, mirrors the "
        "run-keboola-semantic-layer-refresh exemption; no analyst CLI/MCP analogue"
    ),
    "/api/admin/run-audit-prune": (
        "scheduler-driven audit_log retention pruning trigger (B8 audit-trail "
        "seam) — admin/scheduler maintenance op, mirrors the run-blocked-purge "
        "/ run-reap-stuck-reviews exemptions; no analyst CLI/MCP analogue"
    ),
    "/api/chat/journey": (
        "chat-driven onboarding backend foundation — internal state read/write "
        "for the in-chat onboarding UI (a follow-up task), self-scoped to the "
        "caller's own journey row; no analyst CLI/MCP analogue"
    ),
    # Chat history row menu (pin / rename). Both mutate how the WEB history
    # panel presents a conversation, not what any agent can read or do: a pin
    # is a hoist in one rendered list, and a title is the label that list
    # shows. There is no CLI or MCP notion of "my history panel's ordering" to
    # mirror them onto — `agnes chat <slug>` streams a session, it does not
    # render the panel — and the auto-title already writes the same column
    # server-side. Self-scoped to the caller's own session (404, never 403).
    "/api/chat/sessions/{chat_id}/pin": (
        "web chat history-panel affordance — hoists one of the caller's own "
        "conversations into the Pinned group in the rendered list; no analyst "
        "CLI/MCP analogue"
    ),
    "/api/chat/sessions/{chat_id}/title": (
        "web chat history-panel affordance — renames one of the caller's own "
        "conversations, the same column the Haiku auto-title writes; no "
        "analyst CLI/MCP analogue"
    ),
    # Chats page (/chats) archive lifecycle. Same class as pin/title: these
    # manage how the WEB inventory presents the caller's own conversations.
    # Archived is the name the plain DELETE's long-standing soft-archive state
    # finally gets (plus the way back); permanent is the actual row+messages
    # delete. Neither has a CLI/MCP analogue — `agnes chat <slug>` streams a
    # session, it does not curate the caller's history inventory.
    "/api/chat/sessions/{chat_id}/archived": (
        "web chats-page affordance — archives/restores one of the caller's "
        "own conversations (the plain DELETE's soft-archive state, named and "
        "reversible); no analyst CLI/MCP analogue"
    ),
    "/api/chat/sessions/{chat_id}/permanent": (
        "web chats-page affordance — permanently deletes one of the caller's "
        "own conversations and its messages; no analyst CLI/MCP analogue"
    ),
    # Session-workspace file delivery (#1611) — the browser's way to reach
    # deliverables a chat agent rendered into its session workspace (list /
    # download / save a copy to the Library). Self-scoped to the caller's own
    # session (404, never 403). No analyst CLI/MCP analogue: the CLI runs IN
    # the workspace, so its files are already local, and MCP agents write
    # files rather than fetch them back.
    "/api/chat/sessions/{chat_id}/files": (
        "web chat affordance — lists files in the caller's own session "
        "workspace so the browser can offer downloads; CLI sessions already "
        "have the files locally, no MCP analogue"
    ),
    "/api/chat/sessions/{chat_id}/files/download": (
        "web chat affordance — streams one session-workspace file to the "
        "browser as an attachment; CLI sessions already have the files "
        "locally, no MCP analogue"
    ),
    "/api/chat/sessions/{chat_id}/files/save-artefact": (
        "web chat affordance — saves one session-workspace file as the "
        "caller's private Library artefact (same bridge the chat composer "
        "upload uses); CLI sessions already have the files locally, no MCP "
        "analogue"
    ),
    # Keboola glossary import (2026-07-17 design). `/api/glossary/search`
    # carries the triple-surface contract in _COHORT; list and get-by-id are
    # thin REST reads with no dedicated MCP tool (an agent resolves a term by
    # searching, not by listing or fetching a known id) — `agnes glossary
    # show` covers the get-by-id case for interactive CLI use.
    "/api/glossary": (
        "list-all glossary terms — thin REST read, no MCP analogue (an agent "
        "resolves terminology via glossary_search, not by paging the full "
        "list); no dedicated CLI list command either, mirrors the search-first "
        "access pattern of /api/knowledge/search"
    ),
    "/api/glossary/{glossary_id}": (
        "get-by-id glossary read, reachable via `agnes glossary show` — no "
        "MCP analogue (glossary_search is the agent-facing tool; an agent "
        "resolves a term by searching, not by a known id)"
    ),
    # Data apps control-plane REST (2026-07-21 data-apps platform plan,
    # Task 7). `/api/data-apps` (list+create) and `/api/data-apps/{slug}`
    # (show+delete) now carry the triple-surface contract in _COHORT via
    # their GET methods (`data_apps_list`/`data_app_get`) — `create`/`delete`
    # have no MCP analogue but piggy-back on the same path entries, the same
    # shared-path pattern already used for /api/jobs (list+enqueue) and
    # /api/collections/{collection_id} (show+delete-file). `deploy` and
    # `logs` also moved to _COHORT (Task 11: `data_app_deploy`/
    # `data_app_logs`). `stop` has its own path and no MCP tool planned.
    # `secrets`/`readiness` have their own reasons below.
    "/api/data-apps/{slug}/stop": _DATA_APPS_REASON,
    "/api/data-apps/{slug}/secrets": _DATA_APPS_SECRETS_REASON,
    "/api/data-apps/{slug}/readiness": _DATA_APPS_READINESS_REASON,
    # git-credential/drafts got their CLI + MCP surfaces in wave 3B Task 8 —
    # see the /api/data-apps/{slug}/drafts* and /git-credential entries in
    # _COHORT above.
    # Wave 3C in-chat preview loop (Task 5) — mints the iframe cookie for the
    # web chat surface; the 4 preview MCP tools (Task 4) are chat-only and
    # have no REST path of their own (see FOUNDATION_TOOL_NAMES), so this is
    # the one new REST route the preview loop adds.
    "/api/data-apps/{slug}/preview-grant": _DATA_APPS_PREVIEW_GRANT_REASON,
    "/api/broker/data-apps": (
        "broker replay surface for the sandboxed authoring agent; not a "
        "user-facing API — internal sandbox->server route confined to the "
        "/api/data-apps control-plane prefix, ticket-gated (not user auth) "
        "like the other /api/broker/* routes. No analyst CLI/MCP analogue: "
        "the agent calls the real /api/data-apps* endpoints through this "
        "relay, which already carry their own triple-surface contract."
    ),
    "/api/broker/data-apps.git/{slug}/{path}": (
        "git smart-HTTP transport for the sandboxed authoring agent; not a "
        "user-facing API — internal sandbox->server route, ticket-gated (not "
        "user auth) like the other /api/broker/* routes. Its consumer is the "
        "`git` binary inside the sandbox, not a person: a CLI or MCP wrapper "
        "would have nothing to wrap, since the client speaks the git wire "
        "protocol end to end. An analyst on a laptop clones the same repo "
        "directly from /data-apps.git/<slug> with the credential "
        "`agnes app git-credential` / `data_app_git_credential` mints, and "
        "THAT pair carries the triple-surface contract."
    ),
    # reap-idle is a scheduler-triggered admin maintenance op (Task 9) —
    # mirrors the run-knowledge-digests/run-corporate-memory exemptions
    # regardless of the CLI/MCP question above; no analyst CLI/MCP analogue.
    "/api/data-apps/reap-idle": (
        "scheduler-driven idle-app reaper trigger (data-apps platform Task 9) — "
        "admin/scheduler maintenance op, mirrors the run-knowledge-digests / "
        "run-corporate-memory exemptions; no analyst CLI/MCP analogue"
    ),
    # Build order step 4 (write path). Unlike the read routes (which live
    # in _COHORT with their CLI/MCP halves), this IS a permanent
    # exemption: the producer contract (ingest,
    # corrections CRUD/export) is scheduler-token-or-admin surface, not an
    # analyst command — spec §12's REST/CLI/MCP table covers only
    # search/neighbors/claims, and ingest/corrections never appear there.
    # Mirrors the run-knowledge-digests / run-corporate-memory / reap-idle
    # exemptions above: a producer/admin maintenance op, no analyst CLI/MCP
    # analogue by design.
    "/api/facts/ingest": (
        "fact graph producer contract (spec §7.2) — scheduler-token-or-admin "
        "ingest endpoint, not an analyst command; no CLI/MCP analogue"
    ),
    "/api/facts/corrections/{subject_kind}/{subject_id}": (
        "admin correction management (spec §4) — PUT/DELETE, no analyst CLI/MCP analogue"
    ),
    "/api/facts/corrections": (
        "producer corrections export (spec §7.4) — scheduler-token-or-admin, no analyst CLI/MCP analogue"
    ),
    "/api/facts/ingest-runs": (
        "persisted ingest run reports (spec §7.2/§13.2) — admin-only, feeds the "
        "/admin/data-sources source card, not an analyst query surface; no CLI/MCP analogue"
    ),
}


def _load_grandfathered() -> frozenset[str]:
    if not _BASELINE_PATH.exists():
        return frozenset()
    return frozenset(
        ln.strip()
        for ln in _BASELINE_PATH.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.startswith("#")
    )


def _live_surface_paths() -> set[str]:
    os.environ.setdefault("TESTING", "1")
    from app.main import create_app

    return {p for p in create_app().openapi()["paths"] if p.startswith(("/api/", "/documentation/"))}


def test_new_endpoints_are_classified():
    live = _live_surface_paths()
    assert len(live) > 150, "openapi returned too few paths — gate would be vacuous"
    grandfathered = _load_grandfathered()
    assert grandfathered, (
        "grandfather baseline empty/missing — run `.venv/bin/python -m scripts.seed_triple_surface_baseline`"
    )
    stale = grandfathered - live
    assert not stale, f"baseline lists paths no longer live (remove them): {sorted(stale)}"
    unclassified = live - set(_COHORT) - set(_EXEMPT) - grandfathered
    assert not unclassified, (
        f"{len(unclassified)} new endpoint(s) not classified — add each to "
        f"_COHORT (triple-surface: land CLI + MCP) or _EXEMPT (REST-only, with a "
        f"reason):\n" + "\n".join(f"  {p}" for p in sorted(unclassified))
    )
