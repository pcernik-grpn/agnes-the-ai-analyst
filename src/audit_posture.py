"""Declared audit posture per mutating HTTP route (F1 — audit-full-coverage
plan, Task 2; contract rewritten by Wave 2 — Task 1: declarative action
emission).

``tests/test_audit_route_posture.py`` is a ratchet: every ``POST``/``PUT``/
``PATCH``/``DELETE`` route registered on the app must have an entry here, and
every entry must correspond to a route that still exists. A new mutating
route that doesn't declare itself fails the build — this is the mechanism
that stops the coverage gap this plan closes from silently growing back.

A posture entry is no longer documentation of what a handler happens to do —
it IS the route's declared action. Two kinds of value:

- **A real, cataloged action name** (``src.audit_events.is_cataloged``).
  Either the route's handler (or an intra-module ``_audit``/helper wrapper it
  calls) already writes its own row under this action on at least one code
  path — in which case declaring it here is a cross-check, since
  ``AuditFallbackMiddleware`` only fires when ``audit_written_count() == 0``
  regardless of what this dict says — OR the handler writes nothing and
  ``AuditFallbackMiddleware`` (``app/middleware/audit_fallback.py``) emits
  THIS action on the route's behalf via ``declared_action()``. Either way the
  row a reader sees is the semantically real action, never a generic
  ``http.request`` placeholder. The literal ``"fallback"`` is retired from
  this vocabulary entirely (Wave 2 — Task 1) — every mutating route names its
  real domain effect now.
- ``"exempt:<reason>"`` — deliberately never audited, and
  ``declared_action()`` returns ``None`` for it, so ``AuditFallback
  Middleware`` skips the route outright rather than writing anything.
  Reserve this for genuinely non-attributable/noise routes (health probes, a
  dry-run diagnostic that writes nothing by design) — a route that performs a
  real, attributable effect always gets a cataloged action, never
  ``"exempt:..."``.

When a handler emits more than one action on different branches (a
create-or-error shape, a multi-step admin flow), the entry below names the
primary/success action — the OTHER action(s) are still real cataloged rows on
their own error/alternate branch, just not repeated here. A handful of
routes name a SHARED action their handler reaches through an unrelated
dependency (e.g. a proxy/relay route sharing one "traffic reached this
surface" action across several HTTP verbs) rather than minting one action per
verb — that's a deliberate volume/semantics call, not an oversight.

``declared_action(method, path_template) -> str | None`` (below) is the read
API onto this dict: the cataloged action, or ``None`` when the route is
exempt or simply undeclared (an undeclared mutating route is a ratchet
failure, not a runtime concern — see ``tests/test_audit_route_posture.py``).

CONTRACT: append-only per route key, like ``src.audit_events.CATALOG`` —
grouped by the handler's module (comment header) so two tasks touching
different domains rarely collide on the same block.
"""

from __future__ import annotations

MUTATING = ("POST", "PUT", "PATCH", "DELETE")

POSTURE: dict[str, str] = {
    # -- app.api.access --------------------------------------------------------
    "DELETE /api/admin/grants/{grant_id}": "resource_grant.deleted",
    "DELETE /api/admin/groups/{group_id}": "user_group.deleted",
    "DELETE /api/admin/groups/{group_id}/members/{user_id}": "user_group.member_removed",
    "DELETE /api/admin/users/{user_id}/memberships/{group_id}": "user_group.member_removed",
    "PATCH /api/admin/groups/{group_id}": "user_group.updated",
    "POST /api/admin/grants": "resource_grant.created",
    "POST /api/admin/groups": "user_group.created",
    "POST /api/admin/groups/{group_id}/members": "user_group.member_added",
    "POST /api/admin/users/{user_id}/memberships": "user_group.member_added",
    "PUT /api/admin/grants/{grant_id}": "resource_grant.requirement_updated",
    # -- app.api.admin ---------------------------------------------------------
    "DELETE /api/admin/registry/{table_id}": "unregister_table",
    "DELETE /api/admin/store/submissions/{submission_id}": "store.submission.deleted",
    "PATCH /api/admin/registry/{table_id}/docs": "table_registry.docs_update",
    "POST /api/admin/configure": "instance.configure",
    "POST /api/admin/discover-and-register": "table_registry.discover_and_register",
    "POST /api/admin/register-table": "register_table",
    "POST /api/admin/register-table/precheck": "table_registry.register_precheck",
    "POST /api/admin/registry/rebuild": "rebuild_registry",
    "POST /api/admin/registry/{table_id}/policy/compile": "access_policy.compile",
    "POST /api/admin/registry/{table_id}/policy/preview": "access_policy.preview",
    "POST /api/admin/run-audit-prune": "run_audit_prune",
    # Landed on `integration` in parallel with this wave.
    "POST /api/admin/upgrade-freeze": "upgrade_freeze.set",
    "DELETE /api/admin/upgrade-freeze": "upgrade_freeze.lift",
    "POST /api/admin/run-blocked-purge": "run_blocked_purge",
    "POST /api/admin/run-corporate-memory": "run_corporate_memory",
    "POST /api/admin/run-jira-consistency-check": "run_jira_consistency_check",
    "POST /api/admin/run-jira-sla-poll": "run_jira_sla_poll",
    "POST /api/admin/run-knowledge-digests": "run_knowledge_digests",
    "POST /api/admin/run-knowledge-migration": "run_knowledge_migration",
    "POST /api/admin/run-knowledge-packaging": "run_knowledge_packaging",
    "POST /api/admin/run-reap-stuck-reviews": "run_reap_stuck_reviews",
    "POST /api/admin/run-retention-prune": "run_retention_prune",
    "POST /api/admin/run-session-collector": "run_session_collector",
    "POST /api/admin/run-session-processor": "run_session_processor:dynamic",
    "POST /api/admin/server-config": "instance_config.update",
    "POST /api/admin/store/submissions/{submission_id}/override": "store.submission.overridden",
    "POST /api/admin/store/submissions/{submission_id}/rescan": "store.submission.rescan",
    "POST /api/admin/store/submissions/{submission_id}/retry": "store.submission.retry",
    "PUT /api/admin/registry/{table_id}": "update_table",
    # -- app.api.admin_analytics -----------------------------------------------
    "POST /api/admin/analytics/migrate": "analytics.migrate",
    # -- app.api.admin_bigquery_test -------------------------------------------
    "POST /api/admin/bigquery/test-connection": "data_source.bigquery_connection_test",
    # -- app.api.admin_chat ----------------------------------------------------
    "DELETE /admin/chat/{chat_id}": "chat.session.admin_kill",
    "POST /admin/chat/secrets": "chat.secrets.update",
    "POST /admin/chat/secrets/test": "chat.secrets.test",
    "POST /admin/chat/{chat_id}/tail-ticket": "chat.session.tail_ticket_issue",
    # -- app.api.admin_contributed_skills --------------------------------------
    "DELETE /api/admin/contributed-skills/{name}": "contributed_skill.delete",
    "POST /api/admin/contributed-skills": "contributed_skill.create",
    # -- app.api.admin_datasource_secrets --------------------------------------
    "DELETE /api/admin/datasource-secrets/{name}": "datasource.secret.clear",
    "POST /api/admin/validate-gws-credentials": "datasource.gws_credentials_validate",
    "PUT /api/admin/datasource-secrets/{name}": "datasource.secret.set",
    # -- app.api.admin_doctor --------------------------------------------------
    "POST /api/admin/doctor/new-instance": "diagnostics.new_instance_check",
    # -- app.api.admin_keboola_test --------------------------------------------
    "POST /api/admin/keboola/test-connection": "data_source.keboola_connection_test",
    # -- app.api.admin_mcp -----------------------------------------------------
    "DELETE /api/admin/mcp-sources/{source_id}": "mcp_source.delete",
    "DELETE /api/admin/mcp-sources/{source_id}/grants/{group_id}": "mcp_source.grant.remove",
    "DELETE /api/admin/mcp-sources/{source_id}/secret": "mcp_source.secret.delete",
    "DELETE /api/admin/mcp-tools/{tool_id}": "mcp_tool.delete",
    "DELETE /api/admin/mcp-tools/{tool_id}/grants/{group_id}": "mcp_tool.grant.remove",
    "POST /api/admin/mcp-sources": "mcp_source.create",
    "POST /api/admin/mcp-sources/preview-introspect": "mcp_source.preview_introspect",
    "POST /api/admin/mcp-sources/{source_id}/classify": "mcp_source.classify",
    "POST /api/admin/mcp-sources/{source_id}/grants": "mcp_source.grant.add",
    "POST /api/admin/mcp-sources/{source_id}/introspect": "mcp_source.introspect",
    "POST /api/admin/mcp-sources/{source_id}/materialize": "mcp_source.materialize",
    "POST /api/admin/mcp-sources/{source_id}/oauth/register": "mcp_source.oauth_register",
    "POST /api/admin/mcp-sources/{source_id}/test": "mcp_source.test",
    "POST /api/admin/mcp-tools": "mcp_tool.create",
    "POST /api/admin/mcp-tools/{tool_id}/grants": "mcp_tool.grant.add",
    "PUT /api/admin/mcp-sources/{source_id}": "mcp_source.update",
    "PUT /api/admin/mcp-sources/{source_id}/oauth/client": "mcp_source.oauth_client_update",
    "PUT /api/admin/mcp-sources/{source_id}/secret": "mcp_source.secret.set",
    "PUT /api/admin/mcp-tools/{tool_id}": "mcp_tool.update",
    "PUT /api/admin/mcp-tools/{tool_id}/projection-map": "mcp_tool.projection_map",
    # -- app.api.admin_sharepoint ----------------------------------------------
    "DELETE /api/admin/sharepoint/connections/{connection_id}/scopes": "sharepoint_connection.scope_remove",
    "POST /api/admin/sharepoint/connections/{connection_id}/scopes": "sharepoint_connection.scope_confirm",
    # -- app.api.admin_slack_secrets -------------------------------------------
    "DELETE /api/admin/slack-secrets/{name}": "slack.secret.clear",
    "PUT /api/admin/slack-secrets/{name}": "slack.secret.set",
    # -- app.api.admin_source_connections --------------------------------------
    "DELETE /api/admin/source-connections/{connection_id}": "source_connection.delete",
    "DELETE /api/admin/source-connections/{connection_id}/chat-tools": "source_connection.chat_tools_disable",
    "DELETE /api/admin/source-connections/{connection_id}/secret": "source_connection.secret.clear",
    "POST /api/admin/source-connections": "source_connection.create",
    "POST /api/admin/source-connections/{connection_id}/chat-tools": "source_connection.chat_tools_enable",
    "POST /api/admin/source-connections/{connection_id}/test": "source_connection.test",
    "PUT /api/admin/source-connections/{connection_id}": "source_connection.update",
    "PUT /api/admin/source-connections/{connection_id}/secret": "source_connection.secret.set",
    # -- app.api.admin_sso -----------------------------------------------------
    "DELETE /api/admin/sso/client-secret": "sso.client_secret_clear",
    "DELETE /api/admin/sso/config": "sso.config_delete",
    "DELETE /api/admin/sso/identities/{user_id}": "sso.identity_unlink",
    "POST /api/admin/sso/test-config": "sso.test_config",
    "PUT /api/admin/sso/client-secret": "sso.client_secret_set",
    "PUT /api/admin/sso/config": "sso.config_update",
    # -- app.api.admin_usage ---------------------------------------------------
    "POST /api/admin/telemetry/ask": "usage.ask",
    "POST /api/admin/telemetry/prune": "usage.prune",
    "POST /api/admin/telemetry/reprocess": "usage.reprocess",
    # -- app.api.agent_builder -------------------------------------------------
    "POST /api/agents/{agent_id}/builder/turn": "agent.builder_turn",
    # -- app.api.agent_memory --------------------------------------------------
    "POST /api/v1/sessions/{session_id}/memories": "agent.memory.write",
    # -- app.api.agent_runtime -------------------------------------------------
    "POST /api/v1/agents/{slug}/responses": "agent.invoke",
    # Landed on `integration` in parallel with this wave.
    "POST /api/v1/agents/{slug}/delegate": "chat.delegation",
    # -- app.api.agent_schedules -----------------------------------------------
    "DELETE /api/v1/agents/{slug}/schedules/{schedule_id}": "agent_schedules.delete",
    "PATCH /api/v1/agents/{slug}/schedules/{schedule_id}": "agent_schedules.update",
    "POST /api/v1/agents/run-due": "agent_schedules.run_due.tick",
    "POST /api/v1/agents/{slug}/schedules": "agent_schedules.create",
    # -- app.api.agent_sessions ------------------------------------------------
    "DELETE /api/v1/sessions/{session_id}": "agent.session.delete",
    "POST /api/v1/agents/{slug}/sessions": "agent.session.create",
    "POST /api/v1/sessions/{session_id}/cancel": "agent.session.cancel",
    "POST /api/v1/sessions/{session_id}/messages": "agent.session.message",
    # -- app.api.agent_webhooks ------------------------------------------------
    "DELETE /api/v1/agents/{slug}/webhooks/{webhook_id}": "agent.webhook.delete",
    "POST /api/v1/agents/{slug}/webhooks": "agent.webhook.create",
    # -- app.api.agents_admin --------------------------------------------------
    "DELETE /api/v1/agents/{agent_id}": "agent.delete",
    "DELETE /api/v1/agents/{agent_id}/memories/{memory_id}": "agent.memory.delete",
    "PATCH /api/v1/agents/{agent_id}/memories/{memory_id}": "agent.memory.dynamic",
    "POST /api/v1/agents": "agent.create",
    "POST /api/v1/agents/{agent_id}/tokens": "agent.token_create",
    "PUT /api/v1/agents/{agent_id}": "agent.update",
    "PUT /api/v1/agents/{agent_id}/scope": "agent.scope_update",
    # -- app.api.authoring_suggestions -----------------------------------------
    "POST /api/admin/authoring-suggestions/{sid}/approve": "authoring_suggestion.approved",
    "POST /api/admin/authoring-suggestions/{sid}/reject": "authoring_suggestion.dynamic",
    "POST /api/studio/suggestions": "authoring_suggestion.submit",
    # -- app.api.bq_metadata_refresh -------------------------------------------
    "POST /api/admin/run-bq-metadata-refresh": "run_bq_metadata_refresh",
    "POST /api/v2/metadata-cache/refresh": "metadata_cache.refresh_table",
    # -- app.api.broker --------------------------------------------------------
    "POST /api/broker/agnes-api": "broker_admin_route_rejected",
    "POST /api/broker/agnes-mcp": "broker_admin_route_rejected",
    "POST /api/broker/anthropic": "broker_llm_auth_failure",
    "POST /api/broker/anthropic/{subpath:path}": "broker_llm_auth_failure",
    "POST /api/broker/data-apps": "broker_admin_route_rejected",
    "POST /api/broker/data-apps.git/{slug}/{path:path}": "broker_data_apps_git_rejected",
    # -- app.api.cache_warmup --------------------------------------------------
    "POST /api/admin/cache-warmup/run": "cache_warmup.run",
    # -- app.api.catalog -------------------------------------------------------
    "POST /api/catalog/profile/{table_name}/refresh": "catalog.profile_refresh",
    # -- app.api.chat ----------------------------------------------------------
    "DELETE /api/chat/sessions/{chat_id}": "chat.session.archive",
    "DELETE /api/chat/sessions/{chat_id}/permanent": "chat.session.delete",
    "POST /api/chat/sessions": "chat.session.create",
    "POST /api/chat/sessions/{chat_id}/ticket": "chat.session.ticket",
    "PUT /api/chat/journey": "chat.journey_update",
    "PUT /api/chat/sessions/{chat_id}/archived": "chat.session.archive",
    "PUT /api/chat/sessions/{chat_id}/pin": "chat.session.pin_set",
    "PUT /api/chat/sessions/{chat_id}/title": "chat.session.title_update",
    # -- app.api.chat_copresence -----------------------------------------------
    "POST /api/chat/{session_id}/fork": "chat.copresence.fork",
    "POST /api/chat/{session_id}/invite": "chat.copresence.invite",
    "POST /api/chat/{session_id}/join-ticket": "chat.copresence.join",
    "POST /api/chat/{session_id}/leave": "chat.copresence.leave",
    # -- app.api.chat_session_files --------------------------------------------
    "POST /api/chat/sessions/{chat_id}/files/save-artefact": "chat.session_file.save_artefact",
    # -- app.api.chat_uploads --------------------------------------------------
    "POST /api/chat/uploads": "chat.upload",
    # -- app.api.claude_md -----------------------------------------------------
    "DELETE /api/admin/workspace-prompt-template": "workspace_prompt_template.reset",
    "POST /api/admin/workspace-prompt-template/preview": "workspace_prompt_template.preview",
    "PUT /api/admin/workspace-prompt-template": "workspace_prompt_template.update",
    # -- app.api.cli_auth ------------------------------------------------------
    "POST /cli/auth/exchange": "cli_auth.token_minted",
    "POST /cli/auth/rescope-surface": "cli_auth.surface_rescoped",
    "POST /cli/auth/start": "cli_auth.code_issued",
    # -- app.api.collections ---------------------------------------------------
    "DELETE /api/collections/{collection_id}": "collection.delete",
    "DELETE /api/collections/{collection_id}/files/{file_id}": "collection.file_delete",
    "POST /api/collections": "collection.create",
    "POST /api/collections/{collection_id}/files": "collection.file_add",
    "POST /api/collections/{collection_id}/files/{file_id}/move": "collection.file_move",
    "POST /api/collections/{collection_id}/files/{file_id}/reingest": "collection.file_reingest",
    # -- app.api.cowork_bundle -------------------------------------------------
    "DELETE /api/user/setup-tokens/{token_id}": "setup_token.revoke",
    "POST /api/auth/exchange-setup-token": "setup_token.exchange",
    "POST /api/user/cowork-bundle": "cowork_bundle.generate",
    # -- app.api.data_apps -----------------------------------------------------
    "DELETE /api/data-apps/{slug}": "data_app.delete",
    "DELETE /api/data-apps/{slug}/drafts/{draft_slug}": "data_app.draft_delete",
    "PATCH /api/data-apps/{slug}": "data_app.set_description",
    "POST /api/data-apps": "data_app.create",
    "POST /api/data-apps/reap-idle": "data_app.reap_idle",
    "POST /api/data-apps/{slug}/deploy": "data_app.deploy",
    "POST /api/data-apps/{slug}/drafts": "data_app.draft_create",
    "POST /api/data-apps/{slug}/git-credential": "data_app.git_credential",
    "POST /api/data-apps/{slug}/preview-grant": "data_app.preview_grant",
    "POST /api/data-apps/{slug}/stop": "data_app.stop",
    "PUT /api/data-apps/{slug}/secrets": "data_app.secrets_update",
    # -- app.api.data_apps_git -------------------------------------------------
    # Same fetch-vs-push shape as app.marketplace_server.git_router above,
    # applied to one app's own repo instead of the aggregated marketplace.
    "POST /data-apps.git/{slug}/{path:path}": "data_app.git_fetch",
    # -- app.api.data_apps_proxy -----------------------------------------------
    # The legacy path-based app proxy forwards arbitrary non-GET calls into a
    # user's own running container; one shared action across all four verbs
    # ("traffic reached this app") — the GET-side / windowed-dedup "who used
    # which data app" concept belongs to app.data_apps_subdomain, not here.
    "DELETE /apps/{slug}/{path:path}": "data_app.proxy_mutation",
    "PATCH /apps/{slug}/{path:path}": "data_app.proxy_mutation",
    "POST /apps/{slug}/{path:path}": "data_app.proxy_mutation",
    "PUT /apps/{slug}/{path:path}": "data_app.proxy_mutation",
    # -- app.api.data_packages -------------------------------------------------
    "DELETE /api/admin/data-packages/{pkg_id}": "data_package.delete",
    "DELETE /api/admin/data-packages/{pkg_id}/tables/{table_id}": "data_package.remove_table",
    "DELETE /api/admin/data-packages/{pkg_id}/tools/{tool_id}": "data_package.remove_tool",
    "POST /api/admin/data-packages": "data_package.create",
    "POST /api/admin/data-packages/{pkg_id}/restore": "data_package.restore",
    "POST /api/admin/data-packages/{pkg_id}/tables": "data_package.add_table",
    "POST /api/admin/data-packages/{pkg_id}/tools": "data_package.add_tool",
    "PUT /api/admin/data-packages/{pkg_id}": "data_package.update",
    # -- app.api.databricks_semantic_layer_refresh -----------------------------
    "POST /api/admin/run-databricks-semantic-layer-refresh": "run_databricks_semantic_layer_refresh",
    # -- app.api.db_state ------------------------------------------------------
    "POST /api/admin/db/cancel/{job_id}": "db_migration.cancel",
    "POST /api/admin/db/migrate": "db_migration.start",
    # -- app.api.entity_builder ------------------------------------------------
    "POST /api/store/entities/builder/preview-agent": "store.entity_builder_preview_agent",
    "POST /api/store/entities/builder/turn": "store.entity_builder_turn",
    # -- app.api.facts ---------------------------------------------------------
    "DELETE /api/facts/corrections/{subject_kind}/{subject_id}": "facts.correction.delete",
    "POST /api/facts/ingest": "facts.ingest",
    "POST /api/facts/neighbors": "facts.neighbors",
    "POST /api/facts/search": "facts.search",
    "PUT /api/facts/corrections/{subject_kind}/{subject_id}": "facts.correction.upsert",
    # -- app.api.initial_workspace ---------------------------------------------
    "DELETE /api/admin/initial-workspace": "initial_workspace.delete",
    "POST /api/admin/initial-workspace": "initial_workspace.register",
    "POST /api/admin/initial-workspace/sync": "initial_workspace.sync",
    # Shares `_do_sync` (and its `initial_workspace.sync` action) with the
    # manual route above; the only path where this middleware actually fires
    # is the "not configured, short-circuit" skip, which performs no sync at
    # all but is still a real, attributable no-op reachable by this route.
    "POST /api/admin/initial-workspace/sync-if-configured": "initial_workspace.sync",
    "POST /api/initial-workspace/applied": "initial_workspace.applied",
    # -- app.api.jira_webhooks -------------------------------------------------
    "POST /webhooks/jira": "webhook.jira_received",
    # -- app.api.jobs ----------------------------------------------------------
    "POST /api/jobs": "job.enqueue",
    # -- app.api.kai -----------------------------------------------------------
    "POST /api/kai/mcp": "broker_ticket_scope_mismatch",
    "POST /api/kai/sessions": "kai.session_create",
    "POST /api/kai/tickets": "kai.tickets_issue",
    # -- app.api.keboola_login_projects ----------------------------------------
    "POST /api/auth/keboola/projects": "keboola.projects_import",
    # -- app.api.keboola_semantic_layer_refresh --------------------------------
    "POST /api/admin/run-keboola-semantic-layer-refresh": "run_keboola_semantic_layer_refresh",
    # -- app.api.knowledge_digests ---------------------------------------------
    "DELETE /api/admin/knowledge-digests/{digest_id}": "knowledge_digest.delete",
    "POST /api/admin/knowledge-digests": "knowledge_digest.create",
    "PUT /api/admin/knowledge-digests/{digest_id}": "knowledge_digest.update",
    # -- app.api.marketplace ---------------------------------------------------
    "DELETE /api/marketplace/curated/{marketplace_id}/{plugin_name}/install": "marketplace.curated.uninstall",
    "POST /api/marketplace/curated/{marketplace_id}/{plugin_name}/install": "marketplace.curated.install",
    # -- app.api.marketplaces --------------------------------------------------
    "DELETE /api/marketplaces/{marketplace_id}": "marketplace.delete",
    "DELETE /api/marketplaces/{marketplace_id}/plugins/{plugin_name}/system": "marketplace.plugin.unmark_system",
    "PATCH /api/marketplaces/{marketplace_id}": "marketplace.update",
    "POST /api/marketplaces": "marketplace.create",
    "POST /api/marketplaces/sync-all": "marketplace.sync_all",
    "POST /api/marketplaces/{marketplace_id}/plugins/{plugin_name}/disable": "marketplace.plugin.disable",
    "POST /api/marketplaces/{marketplace_id}/plugins/{plugin_name}/enable": "marketplace.plugin.enable",
    "POST /api/marketplaces/{marketplace_id}/plugins/{plugin_name}/system": "marketplace.plugin.mark_system",
    "POST /api/marketplaces/{marketplace_id}/sync": "marketplace.sync",
    # -- app.api.mcp_builder ---------------------------------------------------
    "POST /api/admin/mcp-sources/builder/turn": "mcp_source.builder_turn",
    # -- app.api.mcp_connect ---------------------------------------------------
    "POST /api/mcp-connect/token": "token.create",
    # -- app.api.mcp_oauth_connect ---------------------------------------------
    "DELETE /api/mcp/sources/{source_id}/oauth/connection": "mcp_source.oauth_disconnect",
    # -- app.api.mcp_passthrough -----------------------------------------------
    "POST /api/mcp/passthrough/tools/{tool_id}/call": "mcp.passthrough_call",
    # -- app.api.mcp_per_table -------------------------------------------------
    "POST /api/mcp/query-table/{table_id}": "query.table_scoped",
    # -- app.api.mcp_streamable ------------------------------------------------
    "DELETE /api/mcp/http": "mcp_streamable.session_delete",
    "POST /api/mcp/http": "mcp_streamable.request",
    # -- app.api.mcp_user_secrets ----------------------------------------------
    "DELETE /api/mcp/sources/{source_id}/my-secret": "mcp_user_secret.clear",
    "POST /api/mcp/sources/{source_id}/my-secret/test": "mcp_user_secret.test",
    "PUT /api/mcp/sources/{source_id}/my-secret": "mcp_user_secret.set",
    # -- app.api.me ------------------------------------------------------------
    "PATCH /api/me/display-name": "user_display_name_updated",
    "POST /api/me/elevation": "admin_elevation_paused",
    "POST /api/me/onboarded": "user_onboarded",
    # -- app.api.memory --------------------------------------------------------
    "DELETE /api/memory/{item_id}/dismiss": "corporate_memory.undismiss",
    "PATCH /api/memory/admin/{item_id}": "corporate_memory.dynamic",
    "POST /api/memory": "corporate_memory.create",
    "POST /api/memory/admin/approve": "corporate_memory.dynamic",
    "POST /api/memory/admin/batch": "corporate_memory.dynamic",
    "POST /api/memory/admin/bulk-update": "corporate_memory.dynamic",
    "POST /api/memory/admin/contradictions": "corporate_memory.contradiction_create",
    "POST /api/memory/admin/contradictions/{contradiction_id}/resolve": "corporate_memory.dynamic",
    "POST /api/memory/admin/duplicate-candidates/resolve": "corporate_memory.dynamic",
    "POST /api/memory/admin/edit": "corporate_memory.dynamic",
    "POST /api/memory/admin/mandate": "memory_item.set_required",
    "POST /api/memory/admin/reject": "corporate_memory.dynamic",
    "POST /api/memory/admin/revoke": "corporate_memory.dynamic",
    "POST /api/memory/items/{item_id}/mark-mandatory": "memory_item.set_required",
    "POST /api/memory/items/{item_id}/mark-unmandatory": "memory_item.set_required",
    "POST /api/memory/{item_id}/dismiss": "corporate_memory.dismiss",
    "POST /api/memory/{item_id}/personal": "corporate_memory.toggle_personal",
    "POST /api/memory/{item_id}/vote": "corporate_memory.vote",
    # -- app.api.memory_domain_suggestions -------------------------------------
    "POST /api/admin/memory-domain-suggestions/{sid}/approve": "memory_domain_suggestion.approve",
    "POST /api/admin/memory-domain-suggestions/{sid}/reject": "memory_domain_suggestion.reject",
    "POST /api/memory-domain-suggestions": "memory_domain_suggestion.create",
    # -- app.api.memory_domains ------------------------------------------------
    "DELETE /api/admin/memory-domains/{domain_id}": "memory_domain.delete",
    "DELETE /api/admin/memory-domains/{domain_id}/items/{item_id}": "memory_domain.remove_item",
    "POST /api/admin/memory-domains": "memory_domain.create",
    "POST /api/admin/memory-domains/{domain_id}/items": "memory_domain.add_item",
    "POST /api/admin/memory-domains/{domain_id}/restore": "memory_domain.restore",
    "PUT /api/admin/memory-domains/{domain_id}": "memory_domain.update",
    # -- app.api.memory_mining -------------------------------------------------
    "POST /api/admin/memory-mining/run": "memory_mining.run",
    "POST /api/studio/memory-mining/consent": "memory_mining.consent_set",
    # -- app.api.metadata ------------------------------------------------------
    "POST /api/admin/metadata/{table_id}": "table_metadata.save",
    "POST /api/admin/metadata/{table_id}/push": "table_metadata.push",
    # -- app.api.metrics -------------------------------------------------------
    "DELETE /api/admin/metrics/{metric_id:path}": "metric_definition.delete",
    "POST /api/admin/metrics": "metric_definition.upsert",
    "POST /api/admin/metrics/import": "metric_definition.import",
    # -- app.api.my_stack ------------------------------------------------------
    "PUT /api/my-stack/curated/{marketplace_id}/{plugin_name}": "my_stack.curated_toggle",
    # -- app.api.news ----------------------------------------------------------
    "POST /api/admin/news/preview": "news_previewed",
    "POST /api/admin/news/publish": "news_published",
    "POST /api/admin/news/unpublish/{version}": "news_unpublished",
    "PUT /api/admin/news/draft": "news_draft_saved",
    # -- app.api.observability -------------------------------------------------
    "DELETE /api/admin/observability/views/{view_id}": "observability_view.delete",
    "POST /api/admin/observability/views": "observability_view.create",
    # -- app.api.ontology ------------------------------------------------------
    "DELETE /api/admin/ontology/drafts/{draft_id}": "ontology_draft.delete",
    "POST /api/admin/ontology/drafts": "ontology_draft.create",
    "POST /api/admin/ontology/drafts/{draft_id}/import": "ontology_draft.import",
    "POST /api/admin/ontology/drafts/{draft_id}/save": "ontology_draft.save",
    "POST /api/admin/ontology/dry-run": "ontology_draft.dry_run",
    "PUT /api/admin/ontology/drafts/{draft_id}": "ontology_draft.update",
    # -- app.api.package_builder -----------------------------------------------
    "POST /api/admin/data-packages/builder/turn": "data_package.builder_turn",
    # -- app.api.prompts -------------------------------------------------------
    "DELETE /api/admin/prompts/{kind}": "prompt.delete",
    "POST /api/admin/prompts/{kind}/bind-git": "prompt.bind_git",
    "POST /api/admin/prompts/{kind}/preview": "prompt.preview",
    "POST /api/admin/prompts/{kind}/source": "prompt.source_set",
    "PUT /api/admin/prompts/{kind}": "prompt.update",
    # -- app.api.query ---------------------------------------------------------
    "POST /api/query": "query.local",
    # -- app.api.query_hybrid --------------------------------------------------
    "POST /api/query/hybrid": "query.hybrid",
    # -- app.api.recipes -------------------------------------------------------
    "DELETE /api/admin/recipes/{recipe_id}": "recipe.delete",
    "POST /api/admin/recipes": "recipe.create",
    "POST /api/admin/recipes/{recipe_id}/restore": "recipe.restore",
    "PUT /api/admin/recipes/{recipe_id}": "recipe.update",
    # -- app.api.scripts -------------------------------------------------------
    "DELETE /api/scripts/{script_id}": "script.delete",
    "POST /api/scripts/deploy": "script.deploy",
    "POST /api/scripts/run": "script.run",
    "POST /api/scripts/run-due": "script_runner.tick",
    "POST /api/scripts/{script_id}/run": "script.run",
    # -- app.api.semantic_models -----------------------------------------------
    "DELETE /api/admin/semantic-models/{model_id:path}": "semantic_model.delete",
    "DELETE /api/admin/semantic-sources/{source_id}": "semantic_source.delete",
    "POST /api/admin/semantic-models": "semantic_model.create",
    "POST /api/admin/semantic-sources": "semantic_source.create",
    "POST /api/admin/semantic-sources/{source_id}/sync": "semantic_source.sync",
    "POST /api/semantic-models/apply": "authoring_suggestion.submit",
    "POST /api/semantic-models/validate-query": "semantic_model.validate_query",
    "PUT /api/admin/semantic-models/{model_id:path}": "semantic_model.update",
    "PUT /api/admin/semantic-sources/{source_id}": "semantic_source.update",
    # -- app.api.settings ------------------------------------------------------
    "PUT /api/settings/dataset": "settings.dataset_update",
    # -- app.api.share_requests_admin ------------------------------------------
    "PATCH /api/admin/share-requests/{request_id}": "share_request.decide",
    # -- app.api.sharing -------------------------------------------------------
    "PUT /api/sharing/{resource_type}/{resource_id}": "sharing.state_update",
    # -- app.api.slack ---------------------------------------------------------
    "POST /api/slack/bind": "slack.bind",
    "POST /api/slack/commands": "slack.command_received",
    "POST /api/slack/events": "slack.event_received",
    "POST /api/slack/interactivity": "slack_share",
    # -- app.api.stack ---------------------------------------------------------
    "DELETE /api/stack/artefacts/{corpus_id}": "stack.artefact_remove",
    "DELETE /api/stack/subscription/{resource_type}/{resource_id}": "stack.unsubscribe",
    "POST /api/stack/artefacts/{corpus_id}": "stack.artefact_add",
    "POST /api/stack/subscribe": "stack.subscribe",
    # -- app.api.store ---------------------------------------------------------
    "DELETE /api/store/entities/{entity_id}": "store.entity.archive",
    "DELETE /api/store/entities/{entity_id}/install": "store.entity.uninstall",
    "POST /api/store/entities": "store.entity.create",
    "POST /api/store/entities/dryrun": "store.entity_dryrun",
    "POST /api/store/entities/from-components": "store.entity.create",
    "POST /api/store/entities/from-markdown": "store.entity.create",
    "POST /api/store/entities/preview": "store.entity_preview",
    "POST /api/store/entities/{entity_id}/install": "store.entity.install",
    "POST /api/store/entities/{entity_id}/rate": "store.entity.rate",
    "POST /api/store/entities/{entity_id}/verification/request": "store.entity.verification.request",
    "POST /api/store/entities/{entity_id}/versions/{version_no}/restore": "store.entity.restore",
    "POST /api/store/import-bundle": "store.bundle.import",
    "PUT /api/store/entities/{entity_id}": "store.entity.update",
    "PUT /api/store/entities/{entity_id}/publisher": "store.entity.publisher",
    "PUT /api/store/entities/{entity_id}/verification": "store.entity.verification",
    # -- app.api.store_lint_admin ----------------------------------------------
    "POST /api/admin/store/lint-audit": "run_store_lint_audit",
    "POST /api/admin/store/lint-dismiss": "dismiss_store_lint_finding",
    # -- app.api.sync ----------------------------------------------------------
    "POST /api/sync/pull-confirm": "sync.pull_confirmed",
    "POST /api/sync/settings": "sync.settings_update",
    "POST /api/sync/table-subscriptions": "sync.subscriptions_update",
    "POST /api/sync/trigger": "sync.trigger",
    # -- app.api.telegram ------------------------------------------------------
    "POST /api/telegram/unlink": "telegram.unlink",
    "POST /api/telegram/verify": "telegram.bind",
    # -- app.api.tokens --------------------------------------------------------
    "DELETE /auth/admin/tokens/{token_id}": "token.admin_revoke",
    "DELETE /auth/tokens/{token_id}": "token.revoke",
    "POST /auth/tokens": "token.create",
    # -- app.api.upload --------------------------------------------------------
    "POST /api/upload/artifacts": "artifact.upload",
    "POST /api/upload/audit-events": "audit_events.upload",
    "POST /api/upload/local-md": "local_md.upload",
    "POST /api/upload/sessions": "session.upload",
    # -- app.api.uploads -------------------------------------------------------
    "POST /api/admin/uploads/cover-image": "cover_image.upload",
    # -- app.api.users ---------------------------------------------------------
    "DELETE /api/users/{user_id}": "user.delete",
    "PATCH /api/users/{user_id}": "user.update",
    "POST /api/users": "user.create",
    "POST /api/users/{user_id}/activate": "user.update",
    "POST /api/users/{user_id}/deactivate": "user.update",
    "POST /api/users/{user_id}/reset-password": "user.reset_password",
    "POST /api/users/{user_id}/set-password": "user.set_password",
    # -- app.api.v2_scan -------------------------------------------------------
    "POST /api/v2/scan": "snapshot.create",
    "POST /api/v2/scan/estimate": "snapshot.estimate",
    # -- app.api.welcome -------------------------------------------------------
    "DELETE /api/admin/welcome-template": "welcome_template.reset",
    "POST /api/admin/welcome-template/preview": "welcome_template.preview",
    "PUT /api/admin/welcome-template": "welcome_template.update",
    # -- app.auth.mcp_oauth ----------------------------------------------------
    "POST /api/mcp/oauth/consent": "mcp_oauth.consent_decision",
    # -- app.auth.providers.email ----------------------------------------------
    "POST /auth/email/send-link": "auth.magic_link_sent",
    "POST /auth/email/send-link/web": "auth.magic_link_sent",
    "POST /auth/email/verify": "login_success",
    # -- app.auth.providers.password -------------------------------------------
    "POST /auth/password/change": "password_changed",
    "POST /auth/password/login": "login_success",
    "POST /auth/password/login/web": "login_success",
    "POST /auth/password/reset": "password_reset_requested",
    "POST /auth/password/reset/confirm": "login_success",
    "POST /auth/password/setup": "account_activated",
    "POST /auth/password/setup/confirm": "account_activated",
    "POST /auth/password/setup/request": "setup_link_requested",
    # -- app.auth.router -------------------------------------------------------
    "POST /auth/bootstrap": "bootstrap_completed",
    "POST /auth/refresh-groups": "auth.refresh_groups",
    "POST /auth/token": "token_created",
    # -- app.marketplace_server.git_router -------------------------------------
    # POST serves both git-upload-pack (fetch negotiation) and git-receive-pack
    # (push) through the same handler; fetch is the overwhelmingly common case
    # for this read-mostly bare repo — see marketplace.git_push for the other
    # branch, still a real cataloged row on its own path.
    "POST /marketplace.git/{path:path}": "marketplace.git_fetch",
    # -- app.web.router --------------------------------------------------------
    "POST /admin/contribute-skill": "contributed_skill.create",
    "POST /admin/contribute-skill/{name}/delete": "contributed_skill.delete",
    "POST /auth/logout": "logout",
    "POST /me/profile/refetch-groups": "exempt:debug_dry_run_no_write",
    "POST /slack/bind": "slack.bind",
}


def declared_action(method: str, path_template: str) -> str | None:
    """The cataloged action a route declares, or ``None``.

    ``None`` covers two cases the caller doesn't need to distinguish: the
    route is declared ``"exempt:<reason>"`` (deliberately never audited), or
    the route simply isn't in ``POSTURE`` at all (undeclared — a ratchet
    failure surfaced by ``tests/test_audit_route_posture.py``, not something
    this function raises on). Used by ``app.middleware.audit_fallback`` for
    mutating routes; read-route callers (Task 2) reuse this same function
    against their own posture dict.
    """
    value = POSTURE.get(f"{method} {path_template}")
    if value is None or value.startswith("exempt:"):
        return None
    return value
