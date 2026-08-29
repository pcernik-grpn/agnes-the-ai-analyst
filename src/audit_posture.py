"""Declared audit posture per mutating HTTP route (F1 — audit-full-coverage
plan, Task 2).

``tests/test_audit_route_posture.py`` is a ratchet: every ``POST``/``PUT``/
``PATCH``/``DELETE`` route registered on the app must have an entry here, and
every entry must correspond to a route that still exists. A new mutating
route that doesn't declare itself fails the build — this is the mechanism
that stops the 155-route gap this plan closes from silently growing back.

Three kinds of value:

- **A real, cataloged action name** (``src.audit_events.is_cataloged``) — the
  route's handler (or an intra-module ``_audit``/helper wrapper it calls)
  already writes its own row under this action on at least one code path.
  Declaring it here is documentation, not enforcement: ``AuditFallback
  Middleware`` only fires when ``audit_written_count() == 0`` regardless of
  what this dict says, so a route can move to a real action name at any time
  without a matching middleware change.
- ``"fallback"`` — the shrinking debt list. The handler writes no audit row
  on its own; the generic ``AuditFallbackMiddleware`` (``app/middleware/
  audit_fallback.py``) is this route's ONLY coverage today, via the generic
  ``http.request`` action. Tasks 3-9 of the audit-full-coverage plan flip
  these to real action names domain by domain as they close each gap — flip
  the entry in the SAME change that adds the route's own ``log_safe`` call,
  never separately.
- ``"exempt:<reason>"`` — deliberately never audited, and the
  ``AuditFallbackMiddleware`` skips these routes outright (see its
  ``POSTURE.get(key, "").startswith("exempt:")`` check) rather than writing
  a generic row for them. Reserve this for genuinely non-attributable/noise
  routes (health probes, a dry-run diagnostic that writes nothing by
  design) — a route that already audits under an uncataloged action name,
  or one nobody has looked at yet, is ``"fallback"``, not ``"exempt:..."``.

A handful of routes below were mapped via a multi-hop reachability scan
(handler → intra-module helper → cross-module import) that occasionally
threaded through an unrelated SHARED dependency (e.g. the PAT resolver's
first-use-new-IP stamp) rather than the route's own action; those are kept
at ``"fallback"`` rather than credited with someone else's action. When a
handler emits more than one action on different branches (a create-or-error
shape, a multi-step admin flow), the entry below names the primary/success
action — the OTHER action(s) are still real cataloged rows on their own
error/alternate branch, just not repeated here.

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
    "PATCH /api/admin/registry/{table_id}/docs": "fallback",
    "POST /api/admin/configure": "instance.configure",
    "POST /api/admin/discover-and-register": "fallback",
    "POST /api/admin/register-table": "register_table",
    "POST /api/admin/register-table/precheck": "fallback",
    "POST /api/admin/registry/rebuild": "rebuild_registry",
    "POST /api/admin/registry/{table_id}/policy/compile": "fallback",
    "POST /api/admin/registry/{table_id}/policy/preview": "access_policy.preview",
    "POST /api/admin/run-audit-prune": "run_audit_prune",
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
    "POST /api/admin/bigquery/test-connection": "fallback",
    # -- app.api.admin_chat ----------------------------------------------------
    "DELETE /admin/chat/{chat_id}": "fallback",
    "POST /admin/chat/secrets": "chat.secrets.update",
    "POST /admin/chat/secrets/test": "fallback",
    "POST /admin/chat/{chat_id}/tail-ticket": "fallback",
    # -- app.api.admin_contributed_skills --------------------------------------
    "DELETE /api/admin/contributed-skills/{name}": "fallback",
    "POST /api/admin/contributed-skills": "fallback",
    # -- app.api.admin_datasource_secrets --------------------------------------
    "DELETE /api/admin/datasource-secrets/{name}": "datasource.secret.clear",
    "POST /api/admin/validate-gws-credentials": "fallback",
    "PUT /api/admin/datasource-secrets/{name}": "datasource.secret.set",
    # -- app.api.admin_doctor --------------------------------------------------
    "POST /api/admin/doctor/new-instance": "fallback",
    # -- app.api.admin_keboola_test --------------------------------------------
    "POST /api/admin/keboola/test-connection": "fallback",
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
    "POST /api/admin/mcp-sources/{source_id}/oauth/register": "fallback",
    "POST /api/admin/mcp-sources/{source_id}/test": "mcp_source.test",
    "POST /api/admin/mcp-tools": "mcp_tool.create",
    "POST /api/admin/mcp-tools/{tool_id}/grants": "mcp_tool.grant.add",
    "PUT /api/admin/mcp-sources/{source_id}": "mcp_source.update",
    "PUT /api/admin/mcp-sources/{source_id}/oauth/client": "fallback",
    "PUT /api/admin/mcp-sources/{source_id}/secret": "mcp_source.secret.set",
    "PUT /api/admin/mcp-tools/{tool_id}": "mcp_tool.update",
    "PUT /api/admin/mcp-tools/{tool_id}/projection-map": "mcp_tool.projection_map",
    # -- app.api.admin_sharepoint ----------------------------------------------
    "DELETE /api/admin/sharepoint/connections/{connection_id}/scopes": "fallback",
    "POST /api/admin/sharepoint/connections/{connection_id}/scopes": "fallback",
    # -- app.api.admin_slack_secrets -------------------------------------------
    "DELETE /api/admin/slack-secrets/{name}": "slack.secret.clear",
    "PUT /api/admin/slack-secrets/{name}": "slack.secret.set",
    # -- app.api.admin_source_connections --------------------------------------
    "DELETE /api/admin/source-connections/{connection_id}": "source_connection.delete",
    "DELETE /api/admin/source-connections/{connection_id}/chat-tools": "fallback",
    "DELETE /api/admin/source-connections/{connection_id}/secret": "source_connection.secret.clear",
    "POST /api/admin/source-connections": "source_connection.create",
    "POST /api/admin/source-connections/{connection_id}/chat-tools": "fallback",
    "POST /api/admin/source-connections/{connection_id}/test": "source_connection.test",
    "PUT /api/admin/source-connections/{connection_id}": "source_connection.update",
    "PUT /api/admin/source-connections/{connection_id}/secret": "source_connection.secret.set",
    # -- app.api.admin_sso -----------------------------------------------------
    "DELETE /api/admin/sso/client-secret": "fallback",
    "DELETE /api/admin/sso/config": "fallback",
    "DELETE /api/admin/sso/identities/{user_id}": "fallback",
    "POST /api/admin/sso/test-config": "fallback",
    "PUT /api/admin/sso/client-secret": "fallback",
    "PUT /api/admin/sso/config": "fallback",
    # -- app.api.admin_usage ---------------------------------------------------
    "POST /api/admin/telemetry/ask": "usage.ask",
    "POST /api/admin/telemetry/prune": "usage.prune",
    "POST /api/admin/telemetry/reprocess": "usage.reprocess",
    # -- app.api.agent_builder -------------------------------------------------
    "POST /api/agents/{agent_id}/builder/turn": "fallback",
    # -- app.api.agent_memory --------------------------------------------------
    "POST /api/v1/sessions/{session_id}/memories": "agent.memory.write",
    # -- app.api.agent_runtime -------------------------------------------------
    "POST /api/v1/agents/{slug}/responses": "agent.invoke",
    # -- app.api.agent_schedules -----------------------------------------------
    "DELETE /api/v1/agents/{slug}/schedules/{schedule_id}": "fallback",
    "PATCH /api/v1/agents/{slug}/schedules/{schedule_id}": "fallback",
    "POST /api/v1/agents/run-due": "agent_schedules.run_due.tick",
    "POST /api/v1/agents/{slug}/schedules": "fallback",
    # -- app.api.agent_sessions ------------------------------------------------
    "DELETE /api/v1/sessions/{session_id}": "agent.session.delete",
    "POST /api/v1/agents/{slug}/sessions": "agent.session.create",
    "POST /api/v1/sessions/{session_id}/cancel": "agent.session.cancel",
    "POST /api/v1/sessions/{session_id}/messages": "agent.session.message",
    # -- app.api.agent_webhooks ------------------------------------------------
    "DELETE /api/v1/agents/{slug}/webhooks/{webhook_id}": "agent.webhook.delete",
    "POST /api/v1/agents/{slug}/webhooks": "agent.webhook.create",
    # -- app.api.agents_admin --------------------------------------------------
    "DELETE /api/v1/agents/{agent_id}": "fallback",
    "DELETE /api/v1/agents/{agent_id}/memories/{memory_id}": "agent.memory.delete",
    "PATCH /api/v1/agents/{agent_id}/memories/{memory_id}": "agent.memory.dynamic",
    "POST /api/v1/agents": "fallback",
    "POST /api/v1/agents/{agent_id}/tokens": "fallback",
    "PUT /api/v1/agents/{agent_id}": "fallback",
    "PUT /api/v1/agents/{agent_id}/scope": "fallback",
    # -- app.api.authoring_suggestions -----------------------------------------
    "POST /api/admin/authoring-suggestions/{sid}/approve": "authoring_suggestion.approved",
    "POST /api/admin/authoring-suggestions/{sid}/reject": "authoring_suggestion.dynamic",
    "POST /api/studio/suggestions": "authoring_suggestion.submit",
    # -- app.api.bq_metadata_refresh -------------------------------------------
    "POST /api/admin/run-bq-metadata-refresh": "run_bq_metadata_refresh",
    "POST /api/v2/metadata-cache/refresh": "fallback",
    # -- app.api.broker --------------------------------------------------------
    "POST /api/broker/agnes-api": "broker_admin_route_rejected",
    "POST /api/broker/agnes-mcp": "broker_admin_route_rejected",
    "POST /api/broker/anthropic": "broker_llm_auth_failure",
    "POST /api/broker/anthropic/{subpath:path}": "broker_llm_auth_failure",
    "POST /api/broker/data-apps": "broker_admin_route_rejected",
    "POST /api/broker/data-apps.git/{slug}/{path:path}": "broker_data_apps_git_rejected",
    # -- app.api.cache_warmup --------------------------------------------------
    "POST /api/admin/cache-warmup/run": "fallback",
    # -- app.api.catalog -------------------------------------------------------
    "POST /api/catalog/profile/{table_name}/refresh": "fallback",
    # -- app.api.chat ----------------------------------------------------------
    "DELETE /api/chat/sessions/{chat_id}": "chat.session.archive",
    "DELETE /api/chat/sessions/{chat_id}/permanent": "chat.session.delete",
    "POST /api/chat/sessions": "chat.session.create",
    "POST /api/chat/sessions/{chat_id}/ticket": "chat.session.ticket",
    "PUT /api/chat/journey": "fallback",
    "PUT /api/chat/sessions/{chat_id}/archived": "chat.session.archive",
    "PUT /api/chat/sessions/{chat_id}/pin": "fallback",
    "PUT /api/chat/sessions/{chat_id}/title": "fallback",
    # -- app.api.chat_copresence -----------------------------------------------
    "POST /api/chat/{session_id}/fork": "fallback",
    "POST /api/chat/{session_id}/invite": "chat.copresence.invite",
    "POST /api/chat/{session_id}/join-ticket": "chat.copresence.join",
    "POST /api/chat/{session_id}/leave": "chat.copresence.leave",
    # -- app.api.chat_session_files --------------------------------------------
    "POST /api/chat/sessions/{chat_id}/files/save-artefact": "fallback",
    # -- app.api.chat_uploads --------------------------------------------------
    "POST /api/chat/uploads": "fallback",
    # -- app.api.claude_md -----------------------------------------------------
    "DELETE /api/admin/workspace-prompt-template": "fallback",
    "POST /api/admin/workspace-prompt-template/preview": "fallback",
    "PUT /api/admin/workspace-prompt-template": "fallback",
    # -- app.api.cli_auth ------------------------------------------------------
    "POST /cli/auth/exchange": "cli_auth.token_minted",
    "POST /cli/auth/rescope-surface": "cli_auth.surface_rescoped",
    "POST /cli/auth/start": "cli_auth.code_issued",
    # -- app.api.collections ---------------------------------------------------
    "DELETE /api/collections/{collection_id}": "fallback",
    "DELETE /api/collections/{collection_id}/files/{file_id}": "fallback",
    "POST /api/collections": "fallback",
    "POST /api/collections/{collection_id}/files": "fallback",
    "POST /api/collections/{collection_id}/files/{file_id}/move": "fallback",
    "POST /api/collections/{collection_id}/files/{file_id}/reingest": "fallback",
    # -- app.api.cowork_bundle -------------------------------------------------
    "DELETE /api/user/setup-tokens/{token_id}": "fallback",
    "POST /api/auth/exchange-setup-token": "fallback",
    "POST /api/user/cowork-bundle": "fallback",
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
    "POST /data-apps.git/{slug}/{path:path}": "fallback",
    # -- app.api.data_apps_proxy -----------------------------------------------
    "DELETE /apps/{slug}/{path:path}": "fallback",
    "PATCH /apps/{slug}/{path:path}": "fallback",
    "POST /apps/{slug}/{path:path}": "fallback",
    "PUT /apps/{slug}/{path:path}": "fallback",
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
    "POST /api/admin/db/cancel/{job_id}": "fallback",
    "POST /api/admin/db/migrate": "fallback",
    # -- app.api.entity_builder ------------------------------------------------
    "POST /api/store/entities/builder/preview-agent": "fallback",
    "POST /api/store/entities/builder/turn": "fallback",
    # -- app.api.facts ---------------------------------------------------------
    "DELETE /api/facts/corrections/{subject_kind}/{subject_id}": "facts.correction.delete",
    "POST /api/facts/ingest": "facts.ingest",
    "POST /api/facts/neighbors": "facts.neighbors",
    "POST /api/facts/search": "facts.search",
    "PUT /api/facts/corrections/{subject_kind}/{subject_id}": "facts.correction.upsert",
    # -- app.api.initial_workspace ---------------------------------------------
    "DELETE /api/admin/initial-workspace": "initial_workspace.delete",
    "POST /api/admin/initial-workspace": "initial_workspace.register",
    "POST /api/admin/initial-workspace/sync": "fallback",
    "POST /api/admin/initial-workspace/sync-if-configured": "fallback",
    "POST /api/initial-workspace/applied": "initial_workspace.applied",
    # -- app.api.jira_webhooks -------------------------------------------------
    "POST /webhooks/jira": "webhook.jira_received",
    # -- app.api.jobs ----------------------------------------------------------
    "POST /api/jobs": "fallback",
    # -- app.api.kai -----------------------------------------------------------
    "POST /api/kai/mcp": "broker_ticket_scope_mismatch",
    "POST /api/kai/sessions": "fallback",
    "POST /api/kai/tickets": "fallback",
    # -- app.api.keboola_login_projects ----------------------------------------
    "POST /api/auth/keboola/projects": "fallback",
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
    "POST /api/admin/mcp-sources/builder/turn": "fallback",
    # -- app.api.mcp_connect ---------------------------------------------------
    "POST /api/mcp-connect/token": "token.create",
    # -- app.api.mcp_oauth_connect ---------------------------------------------
    "DELETE /api/mcp/sources/{source_id}/oauth/connection": "fallback",
    # -- app.api.mcp_passthrough -----------------------------------------------
    "POST /api/mcp/passthrough/tools/{tool_id}/call": "mcp.passthrough_call",
    # -- app.api.mcp_per_table -------------------------------------------------
    "POST /api/mcp/query-table/{table_id}": "query.table_scoped",
    # -- app.api.mcp_streamable ------------------------------------------------
    "DELETE /api/mcp/http": "fallback",
    "POST /api/mcp/http": "fallback",
    # -- app.api.mcp_user_secrets ----------------------------------------------
    "DELETE /api/mcp/sources/{source_id}/my-secret": "mcp_user_secret.clear",
    "POST /api/mcp/sources/{source_id}/my-secret/test": "mcp_user_secret.test",
    "PUT /api/mcp/sources/{source_id}/my-secret": "mcp_user_secret.set",
    # -- app.api.me ------------------------------------------------------------
    "PATCH /api/me/display-name": "user_display_name_updated",
    "POST /api/me/elevation": "admin_elevation_paused",
    "POST /api/me/onboarded": "user_onboarded",
    # -- app.api.memory --------------------------------------------------------
    "DELETE /api/memory/{item_id}/dismiss": "fallback",
    "PATCH /api/memory/admin/{item_id}": "corporate_memory.dynamic",
    "POST /api/memory": "fallback",
    "POST /api/memory/admin/approve": "corporate_memory.dynamic",
    "POST /api/memory/admin/batch": "corporate_memory.dynamic",
    "POST /api/memory/admin/bulk-update": "corporate_memory.dynamic",
    "POST /api/memory/admin/contradictions": "fallback",
    "POST /api/memory/admin/contradictions/{contradiction_id}/resolve": "corporate_memory.dynamic",
    "POST /api/memory/admin/duplicate-candidates/resolve": "corporate_memory.dynamic",
    "POST /api/memory/admin/edit": "corporate_memory.dynamic",
    "POST /api/memory/admin/mandate": "memory_item.set_required",
    "POST /api/memory/admin/reject": "corporate_memory.dynamic",
    "POST /api/memory/admin/revoke": "corporate_memory.dynamic",
    "POST /api/memory/items/{item_id}/mark-mandatory": "memory_item.set_required",
    "POST /api/memory/items/{item_id}/mark-unmandatory": "memory_item.set_required",
    "POST /api/memory/{item_id}/dismiss": "fallback",
    "POST /api/memory/{item_id}/personal": "fallback",
    "POST /api/memory/{item_id}/vote": "fallback",
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
    "POST /api/admin/memory-mining/run": "fallback",
    "POST /api/studio/memory-mining/consent": "fallback",
    # -- app.api.metadata ------------------------------------------------------
    "POST /api/admin/metadata/{table_id}": "fallback",
    "POST /api/admin/metadata/{table_id}/push": "fallback",
    # -- app.api.metrics -------------------------------------------------------
    "DELETE /api/admin/metrics/{metric_id:path}": "fallback",
    "POST /api/admin/metrics": "fallback",
    "POST /api/admin/metrics/import": "fallback",
    # -- app.api.my_stack ------------------------------------------------------
    "PUT /api/my-stack/curated/{marketplace_id}/{plugin_name}": "fallback",
    # -- app.api.news ----------------------------------------------------------
    "POST /api/admin/news/preview": "fallback",
    "POST /api/admin/news/publish": "news_published",
    "POST /api/admin/news/unpublish/{version}": "news_unpublished",
    "PUT /api/admin/news/draft": "news_draft_saved",
    # -- app.api.observability -------------------------------------------------
    "DELETE /api/admin/observability/views/{view_id}": "fallback",
    "POST /api/admin/observability/views": "fallback",
    # -- app.api.ontology ------------------------------------------------------
    "DELETE /api/admin/ontology/drafts/{draft_id}": "fallback",
    "POST /api/admin/ontology/drafts": "fallback",
    "POST /api/admin/ontology/drafts/{draft_id}/import": "fallback",
    "POST /api/admin/ontology/drafts/{draft_id}/save": "fallback",
    "POST /api/admin/ontology/dry-run": "fallback",
    "PUT /api/admin/ontology/drafts/{draft_id}": "fallback",
    # -- app.api.package_builder -----------------------------------------------
    "POST /api/admin/data-packages/builder/turn": "fallback",
    # -- app.api.prompts -------------------------------------------------------
    "DELETE /api/admin/prompts/{kind}": "fallback",
    "POST /api/admin/prompts/{kind}/bind-git": "fallback",
    "POST /api/admin/prompts/{kind}/preview": "fallback",
    "POST /api/admin/prompts/{kind}/source": "fallback",
    "PUT /api/admin/prompts/{kind}": "fallback",
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
    "DELETE /api/admin/semantic-models/{model_id:path}": "fallback",
    "DELETE /api/admin/semantic-sources/{source_id}": "fallback",
    "POST /api/admin/semantic-models": "fallback",
    "POST /api/admin/semantic-sources": "fallback",
    "POST /api/admin/semantic-sources/{source_id}/sync": "fallback",
    "POST /api/semantic-models/apply": "authoring_suggestion.submit",
    "POST /api/semantic-models/validate-query": "fallback",
    "PUT /api/admin/semantic-models/{model_id:path}": "fallback",
    "PUT /api/admin/semantic-sources/{source_id}": "fallback",
    # -- app.api.settings ------------------------------------------------------
    "PUT /api/settings/dataset": "fallback",
    # -- app.api.share_requests_admin ------------------------------------------
    "PATCH /api/admin/share-requests/{request_id}": "fallback",
    # -- app.api.sharing -------------------------------------------------------
    "PUT /api/sharing/{resource_type}/{resource_id}": "fallback",
    # -- app.api.slack ---------------------------------------------------------
    "POST /api/slack/bind": "slack.bind",
    "POST /api/slack/commands": "fallback",
    "POST /api/slack/events": "fallback",
    "POST /api/slack/interactivity": "slack_share",
    # -- app.api.stack ---------------------------------------------------------
    "DELETE /api/stack/artefacts/{corpus_id}": "fallback",
    "DELETE /api/stack/subscription/{resource_type}/{resource_id}": "fallback",
    "POST /api/stack/artefacts/{corpus_id}": "fallback",
    "POST /api/stack/subscribe": "fallback",
    # -- app.api.store ---------------------------------------------------------
    "DELETE /api/store/entities/{entity_id}": "store.entity.archive",
    "DELETE /api/store/entities/{entity_id}/install": "store.entity.uninstall",
    "POST /api/store/entities": "store.entity.create",
    "POST /api/store/entities/dryrun": "fallback",
    "POST /api/store/entities/from-components": "store.entity.create",
    "POST /api/store/entities/from-markdown": "store.entity.create",
    "POST /api/store/entities/preview": "fallback",
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
    "POST /api/admin/store/lint-dismiss": "fallback",
    # -- app.api.sync ----------------------------------------------------------
    "POST /api/sync/pull-confirm": "sync.pull_confirmed",
    "POST /api/sync/settings": "sync.settings_update",
    "POST /api/sync/table-subscriptions": "sync.subscriptions_update",
    "POST /api/sync/trigger": "sync.trigger",
    # -- app.api.telegram ------------------------------------------------------
    "POST /api/telegram/unlink": "fallback",
    "POST /api/telegram/verify": "telegram.bind",
    # -- app.api.tokens --------------------------------------------------------
    "DELETE /auth/admin/tokens/{token_id}": "fallback",
    "DELETE /auth/tokens/{token_id}": "token.revoke",
    "POST /auth/tokens": "token.create",
    # -- app.api.upload --------------------------------------------------------
    "POST /api/upload/artifacts": "artifact.upload",
    "POST /api/upload/local-md": "local_md.upload",
    "POST /api/upload/sessions": "session.upload",
    # -- app.api.uploads -------------------------------------------------------
    "POST /api/admin/uploads/cover-image": "fallback",
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
    "DELETE /api/admin/welcome-template": "fallback",
    "POST /api/admin/welcome-template/preview": "fallback",
    "PUT /api/admin/welcome-template": "fallback",
    # -- app.auth.mcp_oauth ----------------------------------------------------
    "POST /api/mcp/oauth/consent": "fallback",
    # -- app.auth.providers.email ----------------------------------------------
    "POST /auth/email/send-link": "fallback",
    "POST /auth/email/send-link/web": "fallback",
    "POST /auth/email/verify": "login_success",
    # -- app.auth.providers.password -------------------------------------------
    "POST /auth/password/change": "password_changed",
    "POST /auth/password/login": "login_success",
    "POST /auth/password/login/web": "login_success",
    "POST /auth/password/reset": "fallback",
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
    "POST /admin/contribute-skill": "fallback",
    "POST /admin/contribute-skill/{name}/delete": "fallback",
    "POST /auth/logout": "fallback",
    "POST /me/profile/refetch-groups": "exempt:debug_dry_run_no_write",
    "POST /slack/bind": "slack.bind",
}
