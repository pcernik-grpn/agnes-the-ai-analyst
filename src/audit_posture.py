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
    # Landed on `integration` in parallel with this wave, declared "fallback"
    # under the old contract — given real actions here, since "fallback" no
    # longer exists. Neither handler audits itself, so the middleware emits
    # these; `run_` keeps the run-due sweep inside SCHEDULER_ACTION_SQL's
    # liveness predicate, same reasoning as the other scheduler endpoints.
    "POST /api/admin/sharepoint/connections/{connection_id}/extract": "sharepoint_connection.extract",
    "POST /api/admin/sharepoint/connections/{connection_id}/scopes": "sharepoint_connection.scope_confirm",
    "POST /api/admin/sharepoint/extraction/run-due": "run_sharepoint_extraction",
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
    # apps-runner's own report-back channel (Wave 2 — Task 3): a single route
    # that emits whichever of the three container-lifecycle actions the
    # runner reports (up/stop/resume) — see app/api/data_apps.py's
    # `record_runner_event`. Named for the primary/most-common branch, same
    # multi-action convention documented above (e.g. user.activate/
    # deactivate both -> "user.update").
    "POST /api/data-apps/runner-events": "data_app.container_up",
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


# ---------------------------------------------------------------------------
# READ_POSTURE / WS_POSTURE -- Wave 2, Task 2: reads and WebSocket routes join
# the same declare-or-fail ratchet as MUTATING routes above. Same two-value
# vocabulary (a cataloged action, or "exempt:<reason>"), but the exempt reason
# must come from the CLOSED EXEMPT_REASONS vocabulary below -- this is what
# keeps "exempt" from becoming a junk drawer for "didn't think about it".
#
# Policy (decide once, apply mechanically): a read gets a real action when it
# returns data content (query results, samples, downloads, exports, bundles),
# secrets or tokens, another user's data (admin cross-user reads), or the audit
# trail itself. Everything else is exempt with a reason. Sensitive reads wave 1
# already audits reuse their EXISTING action; sensitive reads that were still
# unaudited get a new action in the "Wave 2 -- Task 2" CATALOG block.
# ---------------------------------------------------------------------------

EXEMPT_REASONS: frozenset[str] = frozenset(
    {
        "health",  # health/readiness/diagnostic probes -- no user data
        "self",  # caller reading their own data
        "ui_support",  # list/lookup backing a page, no sensitive content
        "static",  # bundled/served assets, docs, discovery documents
        "noise",  # high-frequency polling / transport plumbing, no security value
    }
)

READ_POSTURE: dict[str, str] = {
    # -- app.api.access --
    "GET /api/admin/access-overview": "exempt:ui_support",
    "GET /api/admin/grants": "exempt:ui_support",
    "GET /api/admin/groups": "exempt:ui_support",
    "GET /api/admin/groups/{group_id}": "exempt:ui_support",
    "GET /api/admin/groups/{group_id}/members": "exempt:ui_support",
    "GET /api/admin/resource-types": "exempt:ui_support",
    "GET /api/admin/users/{user_id}/effective-access": "exempt:ui_support",
    "GET /api/admin/users/{user_id}/library-preview": "exempt:ui_support",
    "GET /api/admin/users/{user_id}/memberships": "exempt:ui_support",
    "GET /api/me/effective-access": "exempt:self",
    # -- app.api.activity --
    "GET /api/admin/activity": "activity.read",
    "GET /api/admin/activity/health": "activity.read",
    "GET /api/admin/activity/sync": "activity.read",
    # -- app.api.admin --
    "GET /api/admin/discover-tables": "table_registry.discover_preview",
    "GET /api/admin/registry": "exempt:ui_support",
    "GET /api/admin/registry/{table_id}/policy/columns": "exempt:ui_support",
    "GET /api/admin/server-config": "server_config.read",
    "GET /api/admin/server-config/overlay": "server_config.read",
    "GET /api/admin/store/submissions": "exempt:ui_support",
    "GET /api/admin/store/submissions/{submission_id}": "exempt:ui_support",
    "GET /api/admin/store/submissions/{submission_id}/bundle.zip": "store.submission.bundle_downloaded",
    # -- app.api.admin_adoption --
    "GET /api/admin/adoption/kpis": "adoption.kpis",
    "GET /api/admin/adoption/series": "exempt:ui_support",
    "GET /api/admin/adoption/top-skills": "exempt:ui_support",
    "GET /api/admin/adoption/top-users": "exempt:ui_support",
    "GET /api/admin/adoption/users/{user_id}/kpis": "adoption.user_kpis",
    "GET /api/admin/adoption/users/{user_id}/series": "exempt:ui_support",
    "GET /api/admin/adoption/users/{user_id}/top-skills": "exempt:ui_support",
    "GET /api/admin/adoption/users/{user_id}/top-tools": "exempt:ui_support",
    # -- app.api.admin_chat --
    "GET /admin/chat": "chat.session.admin_list",
    "GET /admin/chat/readiness": "exempt:health",
    "GET /admin/chat/{chat_id}/debug": "exempt:noise",
    # -- app.api.admin_contributed_skills --
    "GET /api/admin/contributed-skills": "exempt:ui_support",
    # -- app.api.admin_dashboard --
    "GET /api/admin/dashboard/signals": "exempt:ui_support",
    # -- app.api.admin_datasource_secrets --
    "GET /api/admin/datasource-secrets": "datasource.secret.read",
    # -- app.api.admin_doctor --
    "GET /api/admin/doctor/support": "exempt:health",
    # -- app.api.admin_mcp --
    "GET /api/admin/mcp-sources": "exempt:ui_support",
    "GET /api/admin/mcp-sources/{source_id}": "exempt:ui_support",
    "GET /api/admin/mcp-tools": "exempt:ui_support",
    "GET /api/admin/mcp-tools/{tool_id}": "exempt:ui_support",
    # -- app.api.admin_reports --
    "GET /api/admin/reports/marketplace-digest": "reports.marketplace_digest",
    # -- app.api.admin_sessions --
    "GET /api/admin/sessions/facets": "exempt:ui_support",
    "GET /api/admin/sessions/kpis": "exempt:ui_support",
    "GET /api/admin/sessions/list": "admin.sessions_browse",
    "GET /api/admin/sessions/{username}/{session_file}/download": "session_download",
    "GET /api/admin/sessions/{username}/{session_file}/transcript": "session.transcript_view",
    # -- app.api.admin_sharepoint --
    "GET /api/admin/sharepoint/connections/{connection_id}/certificate": "sharepoint_connection.certificate_read",
    "GET /api/admin/sharepoint/connections/{connection_id}/corpus-map": "sharepoint_connection.corpus_map_read",
    "GET /api/admin/sharepoint/connections/{connection_id}/scopes": "sharepoint_connection.scopes_read",
    "GET /api/admin/sharepoint/connections/{connection_id}/tree": "sharepoint_connection.tree_browse",
    "GET /api/admin/sharepoint/connections/{connection_id}/tree/search": "sharepoint_connection.tree_search",
    # -- app.api.admin_slack_secrets --
    "GET /api/admin/slack-secrets": "slack.secret.read",
    # -- app.api.admin_source_connections --
    "GET /api/admin/source-connections": "exempt:ui_support",
    "GET /api/admin/source-connections/{connection_id}": "exempt:ui_support",
    "GET /api/admin/source-connections/{connection_id}/tables": "source_connection.tables_discover",
    # -- app.api.admin_source_discovery --
    "GET /api/admin/data-sources/{source_type}/tables": "source_connection.tables_discover",
    # -- app.api.admin_sso --
    "GET /api/admin/sso/config": "sso.config_read",
    "GET /api/admin/sso/identities": "sso.identities_list",
    # -- app.api.admin_upgrade_freeze --
    "GET /api/admin/upgrade-freeze": "exempt:ui_support",
    # -- app.api.admin_usage --
    "GET /api/admin/telemetry/export": "usage.export",
    # -- app.api.admin_usage_summary --
    "GET /api/admin/telemetry/facets": "exempt:ui_support",
    "GET /api/admin/telemetry/kpis": "exempt:ui_support",
    "GET /api/admin/telemetry/query": "exempt:ui_support",
    "GET /api/admin/telemetry/summary": "usage.summary",
    # -- app.api.admin_user_sessions --
    "GET /api/admin/users/{user_id}/activity": "admin.user_activity_read",
    "GET /api/admin/users/{user_id}/sessions": "admin.user_sessions_read",
    "GET /api/admin/users/{user_id}/sessions/download-all": "session_bulk_download",
    "GET /api/admin/users/{user_id}/sessions/{session_file:path}/download": "session_download",
    # -- app.api.agent_runtime --
    "GET /api/v1/agents/{slug}/usage": "exempt:self",
    "GET /api/v1/jobs/{job_id}": "exempt:noise",
    # -- app.api.agent_schedules --
    "GET /api/v1/agents/{slug}/schedules": "exempt:self",
    # -- app.api.agent_sessions --
    "GET /api/v1/sessions/{session_id}": "exempt:self",
    "GET /api/v1/sessions/{session_id}/artifacts": "exempt:self",
    "GET /api/v1/sessions/{session_id}/artifacts/{artifact_id}": "agent.session.artifact_download",
    # -- app.api.agent_webhooks --
    "GET /api/v1/agents/{slug}/webhooks": "exempt:self",
    # -- app.api.agents_admin --
    "GET /api/v1/agents": "exempt:self",
    "GET /api/v1/agents/{agent_id}": "exempt:self",
    "GET /api/v1/agents/{agent_id}/memories": "exempt:self",
    # -- app.api.attachments --
    "GET /api/attachments/{source}/{attachment_id}/download": "attachment.download",
    # -- app.api.authoring_suggestions --
    "GET /api/admin/authoring-suggestions": "exempt:ui_support",
    "GET /api/studio/suggestions/mine": "exempt:self",
    # -- app.api.bq_metadata_refresh --
    "GET /api/v2/metadata-cache/status": "exempt:noise",
    # -- app.api.broker --
    "GET /api/broker/data-apps.git/{slug}/{path:path}": "data_app.git_fetch",
    # -- app.api.cache_warmup --
    "GET /api/admin/cache-warmup/status": "exempt:noise",
    "GET /api/admin/cache-warmup/stream": "exempt:noise",
    # -- app.api.catalog --
    "GET /api/catalog/metrics/{metric_path:path}": "exempt:ui_support",
    "GET /api/catalog/profile/{table_name}": "exempt:ui_support",
    "GET /api/catalog/tables": "exempt:ui_support",
    # -- app.api.chat --
    "GET /api/chat/journey": "exempt:self",
    "GET /api/chat/sessions": "exempt:self",
    "GET /api/chat/sessions/{chat_id}/messages": "exempt:self",
    "GET /api/chat/skills": "exempt:ui_support",
    # -- app.api.chat_copresence --
    "GET /api/chat/{session_id}/messages": "exempt:self",
    # -- app.api.chat_session_files --
    "GET /api/chat/sessions/{chat_id}/files": "exempt:self",
    "GET /api/chat/sessions/{chat_id}/files/download": "chat.session_file.download",
    # -- app.api.claude_md --
    "GET /api/admin/workspace-prompt-template": "exempt:ui_support",
    "GET /api/welcome": "exempt:ui_support",
    # -- app.api.cli_artifacts --
    "GET /cli/download": "exempt:static",
    "GET /cli/install.sh": "exempt:static",
    "GET /cli/latest": "exempt:static",
    "GET /cli/wheel/{wheel_name}": "exempt:static",
    # -- app.api.cli_auth --
    "GET /cli/auth/start": "exempt:ui_support",
    # -- app.api.collections --
    "GET /api/collections": "exempt:ui_support",
    "GET /api/collections/search": "collection.search",
    "GET /api/collections/{collection_id}": "exempt:ui_support",
    "GET /api/collections/{collection_id}/files": "exempt:ui_support",
    "GET /api/collections/{collection_id}/files/{file_id}/preview": "collection.file_preview",
    "GET /api/collections/{collection_id}/files/{file_id}/raw": "collection.file_download",
    # -- app.api.config_surface --
    "GET /api/admin/config-surface": "exempt:ui_support",
    # -- app.api.connectors --
    "GET /api/connectors/manifest": "exempt:ui_support",
    "GET /api/connectors/params": "exempt:ui_support",
    "GET /api/connectors/{slug}/prompt": "exempt:ui_support",
    # -- app.api.cowork_bundle --
    "GET /api/user/setup-tokens": "exempt:self",
    # -- app.api.data --
    "GET /api/data/{table_id}/check-access": "data.access_check",
    "GET /api/data/{table_id}/download": "data.download",
    # -- app.api.data_apps --
    "GET /api/data-apps": "exempt:ui_support",
    "GET /api/data-apps/{slug}": "exempt:ui_support",
    "GET /api/data-apps/{slug}/logs": "data_app.logs_read",
    "GET /api/data-apps/{slug}/readiness": "exempt:health",
    # -- app.api.data_apps_git --
    "GET /data-apps.git/{slug}/{path:path}": "data_app.git_fetch",
    # -- app.api.data_apps_proxy --
    "GET /api/data-apps-tls-check": "exempt:health",
    "GET /apps/{slug}": "exempt:ui_support",
    # High-frequency asset-fetch catch-all — stays exempt on purpose, even
    # though a subdomain-routed request lands on this SAME route template
    # after `DataAppSubdomainMiddleware` rewrites the path. That middleware
    # already writes its own windowed `data_app.access` row directly (Wave 2
    # — Task 3, see app/data_apps_subdomain.py) BEFORE routing, independent
    # of this map. Declaring "data_app.access" here instead would make the
    # read-path fallback re-emit an UNWINDOWED row on every within-window
    # repeat and on every direct (non-subdomain) `/apps/<slug>/...` hit,
    # defeating the whole point of the TTL dedup.
    "GET /apps/{slug}/{path:path}": "exempt:noise",
    # -- app.api.data_packages --
    "GET /api/admin/data-packages": "exempt:ui_support",
    "GET /api/admin/data-packages/{pkg_id}": "exempt:ui_support",
    # -- app.api.db_state --
    "GET /api/admin/db/job/{job_id}": "exempt:noise",
    "GET /api/admin/db/state": "exempt:noise",
    # -- app.api.facts --
    "GET /api/facts/corrections": "exempt:ui_support",
    # Landed on `integration` in parallel with this wave. Both back the
    # Library's filter menu and type header: per-caller-visible labels and
    # counts, never claim text or document content — the content route below
    # (`/claims`) is the one that carries a real action.
    "GET /api/facts/facets": "exempt:ui_support",
    "GET /api/facts/ingest-runs": "exempt:ui_support",
    "GET /api/facts/type-map": "exempt:ui_support",
    "GET /api/facts/{subject_id}/claims": "facts.claims",
    # -- app.api.glossary --
    "GET /api/glossary": "exempt:ui_support",
    "GET /api/glossary/search": "exempt:ui_support",
    "GET /api/glossary/{glossary_id:path}": "exempt:ui_support",
    # -- app.api.health --
    "GET /api/debug/throw": "exempt:noise",
    "GET /api/health": "exempt:health",
    "GET /api/health/detailed": "exempt:health",
    "GET /api/version": "exempt:static",
    # -- app.api.health_probes --
    "GET /healthz": "exempt:health",
    "GET /readyz": "exempt:health",
    # -- app.api.initial_workspace --
    "GET /api/admin/initial-workspace": "exempt:ui_support",
    "GET /api/initial-workspace": "exempt:ui_support",
    "GET /api/initial-workspace.zip": "initial_workspace.fetch_started",
    # -- app.api.jira_webhooks --
    "GET /webhooks/jira/health": "exempt:health",
    # -- app.api.jobs --
    "GET /api/jobs": "exempt:noise",
    "GET /api/jobs/{job_id}": "exempt:noise",
    # -- app.api.kai --
    "GET /api/kai/workspace": "exempt:self",
    # -- app.api.keboola_login_projects --
    "GET /api/auth/keboola/projects": "exempt:self",
    # -- app.api.keboola_semantic_layer_refresh --
    "GET /api/admin/semantic-layer/coverage": "exempt:ui_support",
    # -- app.api.knowledge_digests --
    "GET /api/admin/knowledge-digests": "exempt:ui_support",
    "GET /api/admin/knowledge-digests/{digest_id}": "exempt:ui_support",
    # -- app.api.knowledge_search --
    "GET /api/knowledge/artifacts/{corpus_id}/download": "knowledge.artifact_download",
    # Landed on `integration` in parallel with this wave: the digest LIST
    # (titles/metadata for the UI); fetching a digest's CONTENT is the
    # audited route below.
    "GET /api/knowledge/digests": "exempt:ui_support",
    "GET /api/knowledge/digests/{digest_id}/content": "knowledge.digest_download",
    "GET /api/knowledge/search": "knowledge.search",
    # -- app.api.marketplace --
    "GET /api/marketplace/categories": "exempt:ui_support",
    "GET /api/marketplace/curated/{marketplace_id}/{plugin_name}": "exempt:static",
    "GET /api/marketplace/curated/{marketplace_id}/{plugin_name}/agent/{agent_name}": "exempt:static",
    "GET /api/marketplace/curated/{marketplace_id}/{plugin_name}/asset/{path:path}": "exempt:static",
    "GET /api/marketplace/curated/{marketplace_id}/{plugin_name}/doc/{path:path}": "exempt:static",
    "GET /api/marketplace/curated/{marketplace_id}/{plugin_name}/mirrored/{key:path}": "exempt:static",
    "GET /api/marketplace/curated/{marketplace_id}/{plugin_name}/skill/{skill_name}": "exempt:static",
    "GET /api/marketplace/flea/{entity_id}/agent/{agent_name}": "exempt:static",
    "GET /api/marketplace/flea/{entity_id}/detail": "exempt:ui_support",
    "GET /api/marketplace/flea/{entity_id}/skill/{skill_name}": "exempt:static",
    "GET /api/marketplace/items": "exempt:ui_support",
    # -- app.api.marketplaces --
    "GET /api/marketplaces": "exempt:ui_support",
    "GET /api/marketplaces/{marketplace_id}/plugins": "exempt:ui_support",
    # -- app.api.mcp_oauth_connect --
    "GET /api/mcp/oauth-client/callback": "mcp_oauth.connect",
    "GET /api/mcp/sources/{source_id}/oauth/authorize": "exempt:ui_support",
    # -- app.api.mcp_passthrough --
    "GET /api/mcp/passthrough/tools": "exempt:ui_support",
    # -- app.api.mcp_streamable --
    "GET /.well-known/oauth-authorization-server": "exempt:static",
    "GET /.well-known/oauth-authorization-server/api/mcp/http": "exempt:static",
    "GET /.well-known/oauth-protected-resource/api/mcp/http": "exempt:static",
    "GET /.well-known/openid-configuration": "exempt:static",
    "GET /.well-known/openid-configuration/api/mcp/http": "exempt:static",
    "GET /api/mcp/http": "exempt:noise",
    # -- app.api.mcp_user_secrets --
    "GET /api/mcp/sources/{source_id}/my-secret": "mcp_user_secret.read",
    # -- app.api.me --
    "GET /api/me/external-identity": "exempt:self",
    "GET /api/me/home-stats": "exempt:self",
    # -- app.api.me_stats --
    "GET /api/me/stats/queries": "exempt:self",
    "GET /api/me/stats/sessions": "exempt:self",
    "GET /api/me/stats/sync": "exempt:self",
    "GET /api/me/stats/tokens": "exempt:self",
    # -- app.api.memory --
    "GET /api/memory": "exempt:ui_support",
    "GET /api/memory/admin/audit": "memory.admin_audit_read",
    "GET /api/memory/admin/contradictions": "exempt:ui_support",
    "GET /api/memory/admin/duplicate-candidates": "exempt:ui_support",
    "GET /api/memory/admin/pending": "exempt:ui_support",
    "GET /api/memory/admin/{item_id}": "exempt:ui_support",
    "GET /api/memory/bundle": "memory.bundle_download",
    "GET /api/memory/domains": "exempt:ui_support",
    "GET /api/memory/my-contributions": "exempt:self",
    "GET /api/memory/my-votes": "exempt:self",
    "GET /api/memory/stats": "exempt:ui_support",
    "GET /api/memory/tree": "exempt:ui_support",
    "GET /api/memory/{item_id}/provenance": "exempt:ui_support",
    # -- app.api.memory_domain_suggestions --
    "GET /api/admin/memory-domain-suggestions": "exempt:ui_support",
    "GET /api/admin/memory-domain-suggestions/count-pending": "exempt:ui_support",
    "GET /api/memory-domain-suggestions/mine": "exempt:self",
    # -- app.api.memory_domains --
    "GET /api/admin/memory-domains": "exempt:ui_support",
    "GET /api/admin/memory-domains/{domain_id}": "exempt:ui_support",
    # -- app.api.memory_mining --
    "GET /api/studio/memory-mining/consent": "exempt:self",
    # -- app.api.metadata --
    "GET /api/admin/metadata/{table_id}": "exempt:ui_support",
    # -- app.api.metrics --
    "GET /api/metrics": "exempt:ui_support",
    "GET /api/metrics/{metric_id:path}": "exempt:ui_support",
    # -- app.api.my_stack --
    "GET /api/my-stack": "exempt:self",
    # -- app.api.news --
    "GET /api/admin/news/current": "exempt:ui_support",
    "GET /api/admin/news/draft": "exempt:ui_support",
    "GET /api/admin/news/versions": "exempt:ui_support",
    "GET /api/admin/news/versions/{version}": "exempt:ui_support",
    # -- app.api.observability --
    "GET /api/admin/observability/facets": "exempt:ui_support",
    "GET /api/admin/observability/kpis": "exempt:ui_support",
    "GET /api/admin/observability/views": "exempt:ui_support",
    # -- app.api.ontology --
    "GET /api/admin/ontology/drafts": "exempt:ui_support",
    "GET /api/admin/ontology/drafts/{draft_id}": "exempt:ui_support",
    # -- app.api.prompts --
    "GET /api/admin/prompts/iwt-files": "exempt:ui_support",
    "GET /api/admin/prompts/{kind}": "exempt:ui_support",
    # -- app.api.recipes --
    "GET /api/admin/recipes": "exempt:ui_support",
    "GET /api/admin/recipes/{recipe_id}": "exempt:ui_support",
    "GET /api/recipes": "exempt:ui_support",
    "GET /api/recipes/{slug}": "exempt:ui_support",
    # -- app.api.scripts --
    "GET /api/scripts": "exempt:ui_support",
    # -- app.api.semantic_models --
    "GET /api/admin/semantic-models": "exempt:ui_support",
    "GET /api/admin/semantic-models/{model_id:path}": "exempt:ui_support",
    "GET /api/admin/semantic-sources": "exempt:ui_support",
    "GET /api/admin/semantic-sources/{source_id}": "exempt:ui_support",
    "GET /api/semantic-models/context": "exempt:ui_support",
    "GET /api/semantic-models/schema": "exempt:ui_support",
    "GET /api/semantic-models/search": "exempt:ui_support",
    "GET /api/semantic-models/{slug}.yaml": "exempt:ui_support",
    # -- app.api.settings --
    "GET /api/settings": "exempt:self",
    # -- app.api.share_requests_admin --
    "GET /api/admin/share-requests": "exempt:ui_support",
    # -- app.api.sharing --
    "GET /api/sharing/groups": "exempt:ui_support",
    "GET /api/sharing/{resource_type}/{resource_id}": "exempt:ui_support",
    # -- app.api.stack --
    "GET /api/stack": "exempt:self",
    "GET /api/stack/artefacts/candidates": "exempt:self",
    "GET /api/stack/browse": "exempt:self",
    # -- app.api.stack_views --
    "GET /api/data-packages/{slug}": "exempt:ui_support",
    "GET /api/memory/domains/{slug}": "exempt:ui_support",
    # -- app.api.store --
    "GET /api/store/bundle.zip": "store.bundle_download",
    "GET /api/store/categories": "exempt:ui_support",
    "GET /api/store/entities": "exempt:ui_support",
    "GET /api/store/entities/{entity_id}": "exempt:ui_support",
    "GET /api/store/entities/{entity_id}/docs/{filename}": "exempt:ui_support",
    "GET /api/store/entities/{entity_id}/files": "exempt:ui_support",
    "GET /api/store/entities/{entity_id}/photo": "exempt:static",
    "GET /api/store/entities/{entity_id}/status": "exempt:ui_support",
    "GET /api/store/owners": "exempt:ui_support",
    # -- app.api.store_lint_admin --
    "GET /api/admin/store/lint-findings": "exempt:ui_support",
    # -- app.api.sync --
    "GET /api/sync/manifest": "manifest.fetch",
    "GET /api/sync/settings": "exempt:ui_support",
    "GET /api/sync/status": "exempt:noise",
    "GET /api/sync/table-subscriptions": "exempt:ui_support",
    # -- app.api.telegram --
    "GET /api/telegram/status": "exempt:self",
    # -- app.api.tokens --
    "GET /auth/admin/tokens": "token.list",
    "GET /auth/tokens": "token.list",
    "GET /auth/tokens/{token_id}": "token.list",
    # -- app.api.users --
    "GET /api/users": "exempt:ui_support",
    "GET /api/users/{user_id}": "exempt:ui_support",
    # -- app.api.v2_catalog --
    "GET /api/v2/catalog": "catalog.list",
    # -- app.api.v2_marketplace --
    "GET /api/v2/marketplace/skills": "exempt:ui_support",
    # -- app.api.v2_sample --
    "GET /api/v2/sample/{table_id}": "catalog.sample",
    # -- app.api.v2_schema --
    "GET /api/v2/schema/{table_id}": "catalog.schema",
    # -- app.api.welcome --
    "GET /api/admin/welcome-template": "exempt:ui_support",
    # -- app.auth.mcp_oauth --
    "GET /api/mcp/oauth/consent": "exempt:ui_support",
    # -- app.auth.providers.email --
    "GET /auth/email/verify": "exempt:ui_support",
    # -- app.auth.providers.google --
    "GET /auth/google/callback": "exempt:ui_support",
    "GET /auth/google/login": "exempt:ui_support",
    # -- app.auth.providers.keboola --
    "GET /auth/keboola/callback": "exempt:ui_support",
    "GET /auth/keboola/login": "exempt:ui_support",
    # -- app.auth.providers.microsoft --
    "GET /auth/microsoft/callback": "exempt:ui_support",
    "GET /auth/microsoft/login": "exempt:ui_support",
    # -- app.auth.providers.password --
    "GET /auth/password/change": "exempt:ui_support",
    "GET /auth/password/reset": "exempt:ui_support",
    "GET /auth/password/setup": "exempt:ui_support",
    # -- app.auth.providers.sso --
    "GET /auth/sso/callback": "exempt:ui_support",
    "GET /auth/sso/login": "exempt:ui_support",
    # -- app.main --
    "GET /docs": "exempt:static",
    "GET /openapi.json": "exempt:static",
    "GET /redoc": "exempt:static",
    # -- app.marketplace_server.git_router --
    "GET /marketplace.git/{path:path}": "marketplace.git_fetch",
    # -- app.marketplace_server.router --
    "GET /marketplace.zip": "marketplace.bundle_download",
    "GET /marketplace/cowork/{prefixed_name}.zip": "marketplace.bundle_download",
    "GET /marketplace/info": "exempt:ui_support",
    # -- app.observability.metrics --
    "GET /metrics": "exempt:health",
    # -- app.web.router --
    "GET /": "exempt:ui_support",
    "GET /_debug/throw/exc": "exempt:noise",
    "GET /_debug/throw/http/{code:int}": "exempt:noise",
    "GET /activity-center": "exempt:ui_support",
    "GET /admin": "exempt:ui_support",
    "GET /admin/access": "exempt:ui_support",
    "GET /admin/activity": "exempt:ui_support",
    "GET /admin/adoption": "exempt:ui_support",
    "GET /admin/adoption/users/{user_id}": "exempt:ui_support",
    "GET /admin/agent-prompt": "exempt:ui_support",
    "GET /admin/contribute-skill": "exempt:ui_support",
    "GET /admin/corporate-memory": "exempt:ui_support",
    "GET /admin/data-packages": "exempt:ui_support",
    "GET /admin/data-packages/new": "exempt:ui_support",
    "GET /admin/data-packages/{package_id}": "exempt:ui_support",
    "GET /admin/data-sources": "exempt:ui_support",
    "GET /admin/database": "exempt:ui_support",
    "GET /admin/datasource-credentials": "exempt:ui_support",
    "GET /admin/grants": "exempt:ui_support",
    "GET /admin/groups": "exempt:ui_support",
    "GET /admin/groups/{group_id}": "exempt:ui_support",
    "GET /admin/initial-workspace": "exempt:ui_support",
    "GET /admin/knowledge-digests": "exempt:ui_support",
    "GET /admin/linked-apps": "exempt:ui_support",
    "GET /admin/linked-apps/new": "exempt:ui_support",
    "GET /admin/marketplaces": "exempt:ui_support",
    "GET /admin/mcp-sources": "exempt:ui_support",
    "GET /admin/mcp-sources/new": "exempt:ui_support",
    "GET /admin/mcp-sources/{source_id}": "exempt:ui_support",
    "GET /admin/mcp-tools/{tool_id}/grants": "exempt:ui_support",
    "GET /admin/news": "exempt:ui_support",
    "GET /admin/ontology": "exempt:ui_support",
    "GET /admin/prompts": "exempt:ui_support",
    "GET /admin/scheduler-runs": "exempt:ui_support",
    "GET /admin/semantic-layer": "exempt:ui_support",
    "GET /admin/server-config": "exempt:ui_support",
    "GET /admin/sessions": "exempt:ui_support",
    "GET /admin/sessions/{username}/{session_file}": "exempt:ui_support",
    "GET /admin/store": "exempt:ui_support",
    "GET /admin/store/lint": "exempt:ui_support",
    "GET /admin/store/submissions": "exempt:ui_support",
    "GET /admin/store/submissions/{submission_id}": "exempt:ui_support",
    "GET /admin/studio": "exempt:ui_support",
    "GET /admin/studio/suggestions": "exempt:ui_support",
    "GET /admin/studio/{domain}": "exempt:ui_support",
    "GET /admin/sync": "exempt:ui_support",
    "GET /admin/tables": "exempt:ui_support",
    "GET /admin/telemetry": "exempt:ui_support",
    "GET /admin/tokens": "exempt:ui_support",
    "GET /admin/usage": "exempt:ui_support",
    "GET /admin/users": "exempt:ui_support",
    "GET /admin/users/{user_id}": "exempt:ui_support",
    "GET /admin/workspace-prompt": "exempt:ui_support",
    "GET /agents": "exempt:ui_support",
    "GET /apps": "exempt:ui_support",
    "GET /apps/detail/{slug}": "exempt:ui_support",
    "GET /artefacts": "exempt:ui_support",
    "GET /ask": "exempt:ui_support",
    "GET /auth/logout": "exempt:ui_support",
    "GET /catalog": "exempt:ui_support",
    "GET /catalog/p/{slug}": "exempt:ui_support",
    "GET /catalog/r/{slug}": "exempt:ui_support",
    "GET /catalog/semantics": "exempt:ui_support",
    "GET /catalog/t/{table_id}": "exempt:ui_support",
    "GET /chat": "exempt:ui_support",
    "GET /chats": "exempt:ui_support",
    "GET /corporate-memory": "exempt:ui_support",
    "GET /dashboard": "exempt:ui_support",
    "GET /documentation/api": "exempt:ui_support",
    "GET /first-time-setup": "exempt:ui_support",
    "GET /home": "exempt:ui_support",
    "GET /how-it-works": "exempt:ui_support",
    "GET /install": "exempt:ui_support",
    "GET /library": "exempt:ui_support",
    "GET /library/{slug}": "exempt:ui_support",
    "GET /library/{slug}/f/{file_id}": "exempt:ui_support",
    "GET /login": "exempt:ui_support",
    "GET /login/email": "exempt:ui_support",
    "GET /login/password": "exempt:ui_support",
    "GET /marketplace": "exempt:ui_support",
    "GET /marketplace/curated/{marketplace_id}/{plugin_name}": "exempt:ui_support",
    "GET /marketplace/curated/{marketplace_id}/{plugin_name}/agent/{agent_name}": "exempt:ui_support",
    "GET /marketplace/curated/{marketplace_id}/{plugin_name}/skill/{skill_name}": "exempt:ui_support",
    "GET /marketplace/flea/{entity_id}": "exempt:ui_support",
    "GET /marketplace/flea/{entity_id}/agent/{agent_name}": "exempt:ui_support",
    "GET /marketplace/flea/{entity_id}/edit": "exempt:ui_support",
    "GET /marketplace/flea/{entity_id}/skill/{skill_name}": "exempt:ui_support",
    "GET /marketplace/format-guide": "exempt:ui_support",
    "GET /marketplace/guide/curated": "exempt:ui_support",
    "GET /marketplace/guide/flea": "exempt:ui_support",
    "GET /mcp-connect": "exempt:ui_support",
    "GET /me/activity": "exempt:ui_support",
    "GET /me/ai-connector": "exempt:ui_support",
    "GET /me/connections": "exempt:ui_support",
    "GET /me/cowork": "exempt:ui_support",
    "GET /me/mcp": "exempt:ui_support",
    "GET /me/memory-mining": "exempt:ui_support",
    "GET /me/profile": "exempt:ui_support",
    "GET /me/stats": "exempt:ui_support",
    "GET /memory/d/{slug}": "exempt:ui_support",
    "GET /news": "exempt:ui_support",
    "GET /privacy": "exempt:ui_support",
    "GET /profile/sessions": "exempt:ui_support",
    "GET /profile/sessions/{filename}": "session_download",
    "GET /semantic-layer": "exempt:ui_support",
    "GET /semantic-layer/{slug}": "exempt:ui_support",
    "GET /semantic-layer/{slug}/{object_id:path}": "exempt:ui_support",
    "GET /setup": "exempt:ui_support",
    "GET /setup-advanced": "exempt:ui_support",
    "GET /skills": "exempt:ui_support",
    "GET /slack/bind": "exempt:ui_support",
    "GET /stack": "exempt:ui_support",
    "GET /store/examples": "exempt:ui_support",
    "GET /store/new": "exempt:ui_support",
    "GET /{full_path:path}": "exempt:ui_support",
}


# ---------------------------------------------------------------------------
# READ_SELF_AUDITING -- GET routes whose handler is a plain `def` (thread-
# offloaded by FastAPI/Starlette to the anyio worker pool) AND already writes
# its OWN row under the SAME action READ_POSTURE declares for it. Verified by
# grepping every declared-action read route's handler (and its intra-module
# helpers, transitively) for an audit write; see audit_fallback.py's module
# docstring for why a sync handler's write is invisible to this middleware's
# ContextVar counter. AuditFallbackMiddleware skips these routes UNCONDITIONALLY
# on the read path -- never checks the counter for them -- because the counter
# reading `0` is not ambiguous-but-possibly-wrong here, it is KNOWN wrong: the
# handler always writes, just on a thread this process can't see from the
# async side. Unlike the mutating path's `_already_covered_by_correlation_id`
# tie-break, this is a static allow-list, not a runtime DB query -- the read path
# must never pay a query per request (see module docstring, Task 2).
# ---------------------------------------------------------------------------
READ_SELF_AUDITING: frozenset[str] = frozenset(
    {
        "GET /api/admin/activity",
        "GET /api/admin/activity/health",
        "GET /api/admin/activity/sync",
        "GET /api/admin/adoption/kpis",
        "GET /api/admin/adoption/users/{user_id}/kpis",
        "GET /api/admin/reports/marketplace-digest",
        "GET /api/admin/sessions/{username}/{session_file}/download",
        "GET /api/admin/sessions/{username}/{session_file}/transcript",
        "GET /api/admin/telemetry/export",
        "GET /api/admin/telemetry/summary",
        "GET /api/admin/users/{user_id}/activity",
        "GET /api/admin/users/{user_id}/sessions/download-all",
        "GET /api/admin/users/{user_id}/sessions/{session_file:path}/download",
        "GET /api/attachments/{source}/{attachment_id}/download",
        "GET /api/facts/{subject_id}/claims",
        "GET /api/sync/manifest",
        "GET /api/v2/catalog",
        "GET /api/v2/sample/{table_id}",
        "GET /api/v2/schema/{table_id}",
        "GET /marketplace.zip",
        "GET /marketplace/cowork/{prefixed_name}.zip",
    }
)


# ---------------------------------------------------------------------------
# WS_POSTURE -- the WebSocket surface. Keyed "WS /path/template" using the SAME
# route-enumeration the ratchet test uses (`tests/test_audit_read_posture.py`):
# any route Starlette exposes with no `.methods` attribute, which is every true
# `WebSocketRoute` AND every plain ASGI `Mount` (StaticFiles, the MCP transport
# sub-apps) -- the two are indistinguishable from `app.routes` alone, so both
# kinds get a "WS ..." key here for the ratchet's sake.
#
# IMPORTANT -- declarative only, no emission wired up in this task. Unlike the
# GET branch, `AuditFallbackMiddleware` does not (yet) intercept ASGI
# `scope["type"] == "websocket"` connections -- this task's Files list is
# `audit_fallback.py` + `audit_posture.py` only, and actually auditing a live
# WebSocket would mean either wiring generic ASGI interception here (risky to
# get right for 5 disparate handlers sight-unseen) or editing each handler
# directly (out of this task's file list; Task 3 does exactly this for
# `notifications.ws_connect`/`ws_rejected` on `app/api/notifications_ws.py` --
# that entry below carries the real action since the handler emits it itself,
# independent of this map). Every OTHER entry is `exempt:<reason>` even where
# the underlying traffic would, by the policy above, deserve a real action --
# `/admin/chat/{chat_id}/tail` -- which streams ANOTHER user's live chat debug
# log to an admin -- carries a real action for the same reason: its handler
# emits `chat.session.tail_view` on accept and `chat.session.tail_rejected` on
# a refused ticket. The ticket ISSUANCE (a separate POST) is audited too, but
# it only proves permission was granted; these rows prove it was used.
# ---------------------------------------------------------------------------
WS_POSTURE: dict[str, str] = {
    # -- app.api.admin_chat -----------------------------------------------------
    # Another user's live chat content: the handler emits this itself (plus
    # `chat.session.tail_rejected` on a refused ticket), independent of this
    # map -- the middleware never intercepts websocket scopes.
    "WS /admin/chat/{chat_id}/tail": "chat.session.tail_view",
    # -- app.api.chat / app.api.chat_copresence ----------------------------------
    # Deliberately not auditing chat message CONTENT (policy, not a gap -- see the
    # wave 2 plan's self-review notes); both streams are owner/participant-scoped.
    "WS /api/chat/sessions/{chat_id}/stream": "exempt:self",
    "WS /api/chat/sessions/{session_id}/join": "exempt:self",
    # -- app.api.mcp_streamable / app.api.mcp_sse --------------------------------
    # Transport-level mounts; the actual tool invocations they carry are audited
    # per-call elsewhere (`mcp.tool_call`, `mcp.passthrough_call`).
    "WS /api/mcp": "exempt:noise",
    "WS /api/mcp/http": "exempt:noise",
    # -- app.api.notifications_ws -------------------------------------------------
    # Caller's own notification channel. Task 3 wired the real connect/reject
    # emission (`notifications.ws_connect` / `notifications.ws_rejected`) directly
    # in the handler, so this is a declared action, not an exemption.
    "WS /api/notifications/ws": "notifications.ws_connect",
    # -- app.api.data_apps_proxy --------------------------------------------------
    # Proxied data-app traffic bridge. Task 3's throttled `data_app.access` at the
    # SUBDOMAIN ingress (`app/data_apps_subdomain.py`) is the intended
    # instrumentation point for "who used which data app" -- auditing every frame
    # of this raw bridge too would both double-count and flood (no throttle here).
    "WS /apps/{slug}/{path:path}": "exempt:noise",
    # -- static asset mounts --------------------------------------------------------
    "WS /static": "exempt:static",
    "WS /uploads": "exempt:static",
}


def declared_read_action(method: str, path_template: str, *, posture: dict[str, str] | None = None) -> str | None:
    """The read-side sibling of `declared_action()`: the cataloged action a GET
    or WebSocket route declares, or `None` (exempt, or undeclared).

    Defaults to `READ_POSTURE`; pass `posture=WS_POSTURE` for a WebSocket lookup.
    """
    table = READ_POSTURE if posture is None else posture
    value = table.get(f"{method} {path_template}")
    if value is None or value.startswith("exempt:"):
        return None
    return value


# ---------------------------------------------------------------------------
# JOB_POSTURE / MCP_TOOL_POSTURE / BOT_COMMAND_POSTURE -- extending the
# declared-action ratchet past HTTP (Wave 2 -- non-HTTP surfaces task): a new
# worker job kind, MCP foundation tool, or bot slash-command/callback used to
# ship with NO audit posture at all and nothing would fail, because the two
# ratchets above only ever inspect ``app.routes`` -- there is no ASGI route
# for a `jobs.kind` value, an MCP tool name, or a Slack/Telegram command
# string. These three dicts close that gap the same way POSTURE/
# READ_POSTURE/WS_POSTURE do above: every entry in the surface's own
# enumerable registry must have a posture entry here, and vice versa (a
# stale entry -- the registry entry it names no longer exists -- fails too).
# The three registries: ``app.worker.registry.JOB_KINDS``,
# ``app.api.mcp.foundation_tools.FOUNDATION_TOOL_NAMES``, and the bot
# modules' own command sets -- ``services.slack_bot.commands.SLACK_COMMANDS``
# and ``services.telegram_bot.bot.TELEGRAM_COMMANDS`` (both extracted BY this
# task from what used to be a bare if/elif chain, so the dispatch code and
# the ratchet can never drift apart the way a hand-maintained duplicate list
# would).
#
# Design decision (read this before adding a new kind/tool/command): each of
# the three surfaces already writes ONE generic, non-differentiating audit
# row per invocation, independent of this map --
#
# - ``job.run``          (``app/worker/kinds.py::dispatch_job``)
# - ``mcp.tool_call``     (``app/api/mcp/tools_generator.py::install_tool_call_audit``)
# - ``slack.command``     (``services/slack_bot/commands.py::_audit_slash_command``)
# - ``telegram.message``  (``services/telegram_bot/bot.py::handle_message``)
#
# each carrying the kind/tool/command name in ``params`` so the row is
# already attributable and queryable per-name today -- just not DECLARED
# anywhere the way a route is, which is the actual gap this task closes.
# This task is declarative-only (same scope cut as WS_POSTURE above -- no
# new emission wiring, no middleware-equivalent for any of these three
# transports). Every entry below is therefore one of two shapes:
#
# - **Supplements** the already-firing generic action: the entry names that
#   SAME action, because nothing more specific fires for that name on this
#   path. The entry's only job is to make a future kind/tool/command that
#   forgets to register here fail the build -- catching exactly the
#   silent-gap risk this task closes -- not to add a second row.
# - **Names a more specific action** that a downstream call already writes
#   for real on that exact path -- an MCP tool that self-calls a REST route
#   over HTTP (which runs through that route's OWN declared posture, a
#   genuine second row) or `telegram.script_run` on the sudo-run callback.
#   The generic row above still ALSO fires in these cases (there is no
#   middleware dedup for any of these three transports) -- this is
#   deliberately not a "replaces" relationship, unlike the HTTP mutating
#   path's fallback-vs-self-write choice.
# ---------------------------------------------------------------------------

JOB_POSTURE: dict[str, str] = {
    # Every kind here reuses the generic `job.run` row `dispatch_job` already
    # writes for every execution (params carry `kind`) -- no handler below
    # writes a second, kind-specific row of its own, so there is nothing
    # more specific to name (see "Supplements" above).
    "data-refresh": "job.run",
    "marketplaces-sync": "job.run",
    "session-collector": "job.run",
    "corporate-memory": "job.run",
    "jira-refresh": "job.run",
    "jira-org-refresh": "job.run",
    "ducklake-maintenance": "job.run",
    "analytics-migrate": "job.run",
    "distribution-mirror": "job.run",
    "webhook-deliver": "job.run",
    "analytics-rebuild": "job.run",
    "collections-purge": "job.run",
    "corpus-extraction": "job.run",
    # Conditionally registered (only on a process hosting a live ChatManager
    # -- see register_all_kinds()'s docstring) but still a real, enumerable
    # kind name when it IS registered, so it still needs an entry here.
    "agent_response": "job.run",
}

# MCP foundation tools (app/api/mcp/foundation_tools.py::FOUNDATION_TOOL_NAMES,
# the SSE + Streamable-HTTP transports). Most tools self-call the matching
# REST endpoint over real HTTP (the same running app, so the call passes
# through that route's own POSTURE/READ_POSTURE-declared posture for real) --
# those entries below reuse that route's exact action/exempt-reason, per the
# "names a more specific action" shape above. A handful of tools never make
# that HTTP round trip -- the `fact_*` tools call `facts_repo()` directly
# in-process (see `_facts_caller`'s docstring), `chat_upload_file` always
# raises before any call on this server-hosted transport, and
# `agnes_data_app_refresh`/`agnes_data_app_close` are pure client-render
# directives with no server round-trip at all -- those reuse the generic
# `mcp.tool_call` action instead, per the "Supplements" shape.
MCP_TOOL_POSTURE: dict[str, str] = {
    "server_info": "exempt:health",  # primary call is GET /api/health
    "catalog": "catalog.list",
    "collections_list": "exempt:ui_support",
    "collection_get": "exempt:ui_support",
    "collections_search": "collection.search",
    "collection_file_read": "collection.file_preview",
    "knowledge_search": "knowledge.search",
    "glossary_search": "exempt:ui_support",
    "semantic_model_search": "exempt:ui_support",
    "semantic_model_get": "exempt:ui_support",
    "validate_semantic_query": "semantic_model.validate_query",
    "get_semantic_context": "exempt:ui_support",
    "get_semantic_schema": "exempt:ui_support",
    "apply_semantic_model": "authoring_suggestion.submit",
    "collections_reingest": "collection.file_reingest",
    # In-process facts_repo() calls, no HTTP self-call -- see module note.
    "fact_search": "mcp.tool_call",
    "fact_type_map": "mcp.tool_call",
    "fact_facets": "mcp.tool_call",
    "fact_neighbors": "mcp.tool_call",
    "fact_claims": "mcp.tool_call",
    "schema": "catalog.schema",
    "describe": "catalog.sample",  # calls schema then sample; sample is the substantive read
    "query": "query.local",
    "skills": "exempt:ui_support",
    "chat_skills": "exempt:ui_support",
    "stack_browse": "exempt:self",
    "stack_subscribe": "stack.subscribe",
    "stack_unsubscribe": "stack.unsubscribe",
    "stack_artefacts_candidates": "exempt:self",
    "stack_artefact_add": "stack.artefact_add",
    "stack_artefact_remove": "stack.artefact_remove",
    "store_rate": "store.entity.rate",
    "store_status": "exempt:ui_support",
    "store_publish_markdown": "store.entity.create",
    "store_compose_plugin": "store.entity.create",
    "marketplace_search": "exempt:ui_support",
    "marketplace_detail": "exempt:ui_support",
    # Branches on curated-vs-flea id shape (`_split_marketplace_id`); curated
    # named as primary per the multi-branch convention documented at the top
    # of POSTURE above -- the flea branch's own action (store.entity.install /
    # store.entity.uninstall) is real and cataloged too, just not repeated here.
    "marketplace_add": "marketplace.curated.install",
    "marketplace_remove": "marketplace.curated.uninstall",
    "store_update": "store.entity.update",
    "store_delete": "store.entity.archive",
    "admin_store_lint_findings": "exempt:ui_support",
    "admin_store_lint_audit": "run_store_lint_audit",
    "admin_store_lint_dismiss": "dismiss_store_lint_finding",
    "documentation_api": "exempt:static",  # reads a bundled markdown file, no self-call
    "tool_docs": "exempt:static",  # reads TOOL_DOCS in-process, no self-call
    "list_contributed_skills": "exempt:ui_support",
    "contribute_skill": "contributed_skill.create",
    "delete_contributed_skill": "contributed_skill.delete",
    "admin_config_surface": "exempt:ui_support",
    "admin_source_connections_list": "exempt:ui_support",
    "admin_register_table": "register_table",  # dry_run branch -> table_registry.register_precheck, also real
    "admin_semantic_layer_coverage": "exempt:ui_support",
    "admin_knowledge_digests_list": "exempt:ui_support",
    "admin_knowledge_digest_get": "exempt:ui_support",
    "admin_knowledge_digest_create": "knowledge_digest.create",
    "admin_knowledge_digest_update": "knowledge_digest.update",
    "admin_knowledge_digest_delete": "knowledge_digest.delete",
    # Always raises before any call on this server-hosted transport (reading
    # a caller-named path server-side would be an arbitrary-file-read) -- see
    # the tool's own docstring. Only the local stdio `agnes mcp` server (which
    # never reaches this process's audit log) can actually upload a file.
    "chat_upload_file": "mcp.tool_call",
    "my_secret_test": "mcp_user_secret.test",
    "admin_jobs_list": "exempt:noise",
    "admin_job_get": "exempt:noise",
    "admin_job_enqueue": "job.enqueue",
    "activity": "activity.read",
    "admin_analytics_migrate": "analytics.migrate",
    "agent_list": "exempt:self",
    "agent_ask": "agent.invoke",
    "agent_usage": "exempt:self",
    "data_apps_list": "exempt:ui_support",
    "data_app_get": "exempt:ui_support",
    "data_app_deploy": "data_app.deploy",
    "data_app_create": "data_app.create",
    "data_app_create_draft": "data_app.draft_create",
    "data_app_delete_draft": "data_app.draft_delete",
    "data_app_git_credential": "data_app.git_credential",
    "data_app_logs": "data_app.logs_read",
    "data_app_set_description": "data_app.set_description",
    # Placeholder call (empty `url`) makes no server request at all; the
    # live-URL call is the one that mints the preview grant -- named primary.
    "agnes_data_app_preview": "data_app.preview_grant",
    # Pure client-render directive, no server round-trip at all.
    "agnes_data_app_refresh": "mcp.tool_call",
    "agnes_data_app_close": "mcp.tool_call",
    "agnes_data_app_credentials": "exempt:ui_support",
}

# Slack (`/agnes`, `/agnes-new`, `/agnes-status`) and Telegram (`/start`,
# `/whoami`, `/status`, `/test`, `/help`, plus the inline "run script" button
# callback) bot commands -- keys namespaced by platform so the two command
# spaces (and Telegram's separate text-vs-callback dispatch) can't collide.
BOT_COMMAND_POSTURE: dict[str, str] = {
    # Every recognized slash command already gets `_audit_slash_command`'s
    # `slack.command` row (params carry `command`) -- see
    # services/slack_bot/commands.py.
    "slack:/agnes": "slack.command",
    "slack:/agnes-new": "slack.command",
    "slack:/agnes-status": "slack.command",
    # Every recognized text command already gets `handle_message`'s
    # `telegram.message` row for a LINKED account (params carry `command`) --
    # see services/telegram_bot/bot.py. `/start` for an UNLINKED account (the
    # common case -- it's the linking command) writes no row: there is no
    # `user_id` to attribute it to yet, the same unauthenticated-skip
    # reasoning `mcp.tool_call`'s own dispatch wrapper uses above.
    "telegram:/start": "telegram.message",
    "telegram:/whoami": "telegram.message",
    "telegram:/status": "telegram.message",
    "telegram:/test": "telegram.message",
    "telegram:/help": "telegram.message",
    # The sudo-run callback -- `handle_callback_query` already writes this
    # for real, independent of this map. Must NEVER be exempt: it is a
    # button press running an operator script as an arbitrary OS user via
    # `sudo -u` (services/telegram_bot/runner.py) -- the highest-value row
    # on this whole surface.
    "telegram:callback:run_script": "telegram.script_run",
}


def declared_nonhttp_action(posture: dict[str, str], key: str) -> str | None:
    """The read-side-shaped lookup for JOB_POSTURE / MCP_TOOL_POSTURE /
    BOT_COMMAND_POSTURE: the cataloged action *key* declares, or `None`
    (exempt, or undeclared). Same two-value contract as `declared_action()`/
    `declared_read_action()` above, generalized over the posture dict since
    none of these three surfaces has a single canonical lookup signature
    (job kind name, tool name, and namespaced bot command key all differ).
    """
    value = posture.get(key)
    if value is None or value.startswith("exempt:"):
        return None
    return value
