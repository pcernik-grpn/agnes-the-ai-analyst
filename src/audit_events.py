"""The audit action catalog (F0 — audit-full-coverage plan, Task 1).

Every action string an audit writer (``log_safe``, ``write_audit``, or a
direct call to the repo layer's own ``log`` method) can emit belongs
here, exactly once. ``tests/test_audit_catalog.py`` scans the codebase for
``action="..."`` literals and fails the build if one isn't registered — the
catalog is the single source of truth for "what can Agnes write to
audit_log", not a description of intent.

Two escape hatches for entries that don't fit a fixed literal:

- ``LEGACY_ALIASES`` — read-side only. A renamed action keeps its old
  writers unmigrated (no history rewrite); the alias lets read-side
  classification (Activity Center, `agnes catalog --metrics`-style lookups)
  treat the old and new names as one action without touching a single call
  site.
- ``DYNAMIC_ACTION_PREFIXES`` — f-string writers whose suffix varies per
  call (``f"mcp_tool.{tool_name}"``, ``f"user.{event}"``, …). Enumerating
  every possible suffix would fight every future addition; the guard
  instead accepts any action starting with one of these prefixes.

CONTRACT (binding for every later task in the audit-full-coverage plan):
this dict literal is APPEND-ONLY. Do not rename or remove an existing key —
a rename is a silent history rewrite for every row already written under the
old name (see ``LEGACY_ALIASES`` above for how to actually rename one). Each
later task adds its own new entries at the bottom, under its own labeled
section, keeping merge conflicts to "two tasks appended near the same line"
rather than "two tasks touched the same entry".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CATEGORY = Literal["auth", "mutation", "read", "system"]


@dataclass(frozen=True)
class AuditEvent:
    action: str
    category: str
    description: str = ""


# ---------------------------------------------------------------------------
# CATALOG — one entry per action name currently emitted somewhere in the
# codebase (seeded by scanning app/, src/, services/, cli/ for action
# literals passed to log_safe / write_audit / the repo layer's log method,
# 2026-08-28, integration base commit 4cdf2cc8f), plus the handful of
# auth actions emitted via a positional `action` argument (login_audit.py,
# app/auth/router.py, app/auth/providers/password.py) that the literal-scan
# regex can't see but that are genuinely part of the live action space.
#
# Append-only past this point — see module docstring.
# ---------------------------------------------------------------------------
CATALOG: dict[str, AuditEvent] = {
    # -- auth: login/session/token/bootstrap/cli-auth/identity-linking -----
    "login_success": AuditEvent("login_success", "auth", "A sign-in completed (any provider)."),
    "login_failed": AuditEvent("login_failed", "auth", "A sign-in attempt was rejected."),
    "account_activated": AuditEvent("account_activated", "auth", "An invited account set its own password."),
    "setup_link_requested": AuditEvent(
        "setup_link_requested", "auth", "A self-service account-setup link was minted and mailed."
    ),
    "token_created": AuditEvent("token_created", "auth", "A personal access token was minted."),
    "token.create": AuditEvent("token.create", "auth", "A personal access token was minted (PAT/MCP-connect path)."),
    "token.revoke": AuditEvent("token.revoke", "auth", "A personal access token was revoked."),
    "token.first_use_new_ip": AuditEvent(
        "token.first_use_new_ip", "auth", "A token's first use came from an IP not seen for it before."
    ),
    "password_changed": AuditEvent("password_changed", "auth", "A user changed their own password."),
    "password_change_failed": AuditEvent("password_change_failed", "auth", "A password-change attempt was rejected."),
    "bootstrap_activated_seed": AuditEvent(
        "bootstrap_activated_seed", "auth", "The seed admin account was activated during bootstrap."
    ),
    "bootstrap_completed": AuditEvent("bootstrap_completed", "auth", "Initial-admin bootstrap finished."),
    "cli_auth.code_issued": AuditEvent("cli_auth.code_issued", "auth", "A CLI device-auth code was issued."),
    "cli_auth.token_minted": AuditEvent("cli_auth.token_minted", "auth", "A CLI auth token was minted."),
    "cli_auth.surface_rescoped": AuditEvent(
        "cli_auth.surface_rescoped", "auth", "A CLI credential's data-read surface was rescoped."
    ),
    "sso.identity.linked": AuditEvent("sso.identity.linked", "auth", "An external SSO identity was linked."),
    "slack.bind": AuditEvent("slack.bind", "auth", "A Slack account was bound to an Agnes user."),
    # -- read: catalog/query/data-access/downloads/observability-self-read -
    "activity.read": AuditEvent("activity.read", "read", "The Activity Center timeline/facets/KPIs were read."),
    "admin.user_activity_read": AuditEvent(
        "admin.user_activity_read", "read", "An admin read another user's activity feed."
    ),
    "access_policy.preview": AuditEvent(
        "access_policy.preview", "read", "An admin previewed a table access policy's effect."
    ),
    "attachment.download": AuditEvent("attachment.download", "read", "A chat attachment was downloaded."),
    "catalog.list": AuditEvent("catalog.list", "read", "The table/dataset catalog was listed."),
    "catalog.sample": AuditEvent("catalog.sample", "read", "A table sample was fetched via the catalog."),
    "catalog.schema": AuditEvent("catalog.schema", "read", "A table schema was fetched via the catalog."),
    "data.access_check": AuditEvent("data.access_check", "read", "An RBAC access check against a table ran."),
    "data.download": AuditEvent("data.download", "read", "A data export/download ran."),
    "knowledge.artifact_download": AuditEvent(
        "knowledge.artifact_download", "read", "A corporate-memory knowledge artifact was downloaded."
    ),
    "knowledge.digest_download": AuditEvent(
        "knowledge.digest_download", "read", "A knowledge digest bundle was downloaded."
    ),
    "manifest.fetch": AuditEvent("manifest.fetch", "read", "An `agnes pull` manifest was fetched."),
    "query.hybrid": AuditEvent("query.hybrid", "read", "A hybrid (BigQuery + local) query ran."),
    "query.internal": AuditEvent("query.internal", "read", "An internal/server-side query ran."),
    "query.local": AuditEvent("query.local", "read", "A local-scope query ran."),
    "query.remote": AuditEvent("query.remote", "read", "A remote-scope (BigQuery/Databricks) query ran."),
    "session.transcript_view": AuditEvent("session.transcript_view", "read", "An admin viewed a session transcript."),
    "session_bulk_download": AuditEvent("session_bulk_download", "read", "Multiple sessions were bulk-downloaded."),
    "session_download": AuditEvent("session_download", "read", "A single session file was downloaded."),
    "snapshot.estimate": AuditEvent("snapshot.estimate", "read", "A snapshot's cost/row estimate was computed."),
    "store.submission.bundle_downloaded": AuditEvent(
        "store.submission.bundle_downloaded", "read", "A marketplace store submission's bundle was downloaded."
    ),
    "usage.ask": AuditEvent("usage.ask", "read", "A natural-language usage question was answered."),
    "usage.export": AuditEvent("usage.export", "read", "Usage data was exported."),
    "usage.summary": AuditEvent("usage.summary", "read", "A usage summary was read."),
    # -- system: scheduler ticks, startup, broker guardrails, review internals
    "admin_elevation_paused": AuditEvent(
        "admin_elevation_paused", "system", "A sensitive admin action was paused pending elevation."
    ),
    "agent_schedules.run_due.tick": AuditEvent(
        "agent_schedules.run_due.tick", "system", "The due-agent-schedules scheduler tick ran."
    ),
    "broker_admin_read_replayed": AuditEvent(
        "broker_admin_read_replayed", "system", "The secret broker replayed a cached admin read."
    ),
    "broker_admin_route_rejected": AuditEvent(
        "broker_admin_route_rejected", "system", "The secret broker rejected an admin route request."
    ),
    "broker_data_apps_git_rejected": AuditEvent(
        "broker_data_apps_git_rejected", "system", "The secret broker rejected a data-app git request."
    ),
    "broker_data_apps_path_rejected": AuditEvent(
        "broker_data_apps_path_rejected", "system", "The secret broker rejected a data-app path request."
    ),
    "broker_llm_auth_failure": AuditEvent(
        "broker_llm_auth_failure", "system", "The secret broker's LLM credential auth failed."
    ),
    "broker_path_rejected": AuditEvent("broker_path_rejected", "system", "The secret broker rejected a path."),
    "broker_ticket_scope_mismatch": AuditEvent(
        "broker_ticket_scope_mismatch", "system", "A broker ticket's scope didn't match the request."
    ),
    "broker_vertex_path_rejected": AuditEvent(
        "broker_vertex_path_rejected", "system", "The secret broker rejected a Vertex AI path."
    ),
    "broker_vertex_target_rejected": AuditEvent(
        "broker_vertex_target_rejected", "system", "The secret broker rejected a Vertex AI target."
    ),
    "broker_vertex_token_failed": AuditEvent(
        "broker_vertex_token_failed", "system", "The secret broker failed to mint a Vertex AI token."
    ),
    "broker_wif_exchange_failed": AuditEvent(
        "broker_wif_exchange_failed", "system", "Workload-identity-federation token exchange failed."
    ),
    "kai_credential_scope_mismatch": AuditEvent(
        "kai_credential_scope_mismatch", "system", "A Kai credential's scope didn't match the request."
    ),
    "marketplace.sync_all": AuditEvent("marketplace.sync_all", "system", "The scheduled full marketplace sync ran."),
    "reports.marketplace_digest": AuditEvent(
        "reports.marketplace_digest", "system", "The scheduled marketplace digest report ran."
    ),
    "run_audit_prune": AuditEvent("run_audit_prune", "system", "The scheduled audit-log retention prune ran."),
    "run_blocked_purge": AuditEvent("run_blocked_purge", "system", "The scheduled blocked-content purge ran."),
    "run_corporate_memory": AuditEvent(
        "run_corporate_memory", "system", "The scheduled corporate-memory maintenance tick ran."
    ),
    "run_jira_consistency_check": AuditEvent(
        "run_jira_consistency_check", "system", "The scheduled Jira consistency check ran."
    ),
    "run_jira_sla_poll": AuditEvent("run_jira_sla_poll", "system", "The scheduled Jira SLA poll ran."),
    "run_knowledge_digests": AuditEvent(
        "run_knowledge_digests", "system", "The scheduled knowledge-digest generation ran."
    ),
    "run_knowledge_migration": AuditEvent(
        "run_knowledge_migration", "system", "The scheduled knowledge-migration tick ran."
    ),
    "run_knowledge_packaging": AuditEvent(
        "run_knowledge_packaging", "system", "The scheduled knowledge-packaging tick ran."
    ),
    "run_reap_stuck_reviews": AuditEvent("run_reap_stuck_reviews", "system", "The scheduled stuck-review reaper ran."),
    "run_retention_prune": AuditEvent("run_retention_prune", "system", "The scheduled retention prune ran."),
    "run_session_collector": AuditEvent("run_session_collector", "system", "The scheduled session-file collector ran."),
    "script_runner.tick": AuditEvent("script_runner.tick", "system", "The scheduled script-runner tick ran."),
    "startup.seed_admin_failed": AuditEvent(
        "startup.seed_admin_failed", "system", "Seeding the initial admin account failed at startup."
    ),
    "store.submission.bg_verdict_skipped": AuditEvent(
        "store.submission.bg_verdict_skipped", "system", "A background lint verdict was skipped for a submission."
    ),
    "store.submission.review_error": AuditEvent(
        "store.submission.review_error", "system", "A store submission review pass errored."
    ),
    # -- mutation: everything else — writes, admin config, chat lifecycle --
    "agent.memory.write": AuditEvent("agent.memory.write", "mutation", "An agent's memory notebook was written."),
    "analytics.migrate": AuditEvent("analytics.migrate", "mutation", "The analytics DuckDB schema was migrated."),
    "authoring_suggestion.approved": AuditEvent(
        "authoring_suggestion.approved", "mutation", "A semantic-layer authoring suggestion was approved."
    ),
    "authoring_suggestion.submit": AuditEvent(
        "authoring_suggestion.submit", "mutation", "A semantic-layer authoring suggestion was submitted."
    ),
    "auth.refresh_groups": AuditEvent(
        "auth.refresh_groups", "mutation", "A user's Google Workspace group memberships were refreshed."
    ),
    "chat.approval_decision": AuditEvent(
        "chat.approval_decision", "mutation", "A chat tool-call approval was decided."
    ),
    "chat.question_answer": AuditEvent("chat.question_answer", "mutation", "A chat Q&A turn completed."),
    "chat.secrets.update": AuditEvent("chat.secrets.update", "mutation", "A chat sandbox's secrets were updated."),
    "chat.session_killed": AuditEvent("chat.session_killed", "mutation", "A chat session was force-killed."),
    "chat.tool_call": AuditEvent("chat.tool_call", "mutation", "A chat sandbox tool call ran."),
    "co_session_fork": AuditEvent("co_session_fork", "mutation", "A co-presence chat session was forked."),
    "facts.correction.delete": AuditEvent(
        "facts.correction.delete", "mutation", "A fact-graph correction was deleted."
    ),
    "facts.correction.upsert": AuditEvent(
        "facts.correction.upsert", "mutation", "A fact-graph correction was created/updated."
    ),
    "facts.merge": AuditEvent("facts.merge", "mutation", "Two fact-graph nodes were merged."),
    "facts.split": AuditEvent("facts.split", "mutation", "A fact-graph node was split."),
    "initial_workspace.applied": AuditEvent(
        "initial_workspace.applied", "mutation", "An initial-workspace override was applied to a client."
    ),
    "initial_workspace.delete": AuditEvent(
        "initial_workspace.delete", "mutation", "An initial-workspace override registration was deleted."
    ),
    "initial_workspace.fetch_started": AuditEvent(
        "initial_workspace.fetch_started", "mutation", "An initial-workspace override fetch started."
    ),
    "initial_workspace.register": AuditEvent(
        "initial_workspace.register", "mutation", "An initial-workspace override was registered."
    ),
    "initial_workspace.sync": AuditEvent(
        "initial_workspace.sync", "mutation", "An initial-workspace override was synced."
    ),
    "initial_workspace.sync_failed": AuditEvent(
        "initial_workspace.sync_failed", "mutation", "An initial-workspace override sync failed."
    ),
    "instance_config.update": AuditEvent("instance_config.update", "mutation", "Instance configuration was updated."),
    "memory_domain_suggestion.approve": AuditEvent(
        "memory_domain_suggestion.approve", "mutation", "A memory-domain suggestion was approved."
    ),
    "memory_domain_suggestion.create": AuditEvent(
        "memory_domain_suggestion.create", "mutation", "A memory-domain suggestion was created."
    ),
    "memory_domain_suggestion.reject": AuditEvent(
        "memory_domain_suggestion.reject", "mutation", "A memory-domain suggestion was rejected."
    ),
    "memory_item.set_required": AuditEvent(
        "memory_item.set_required", "mutation", "A corporate-memory item's mandatory flag was toggled."
    ),
    "metrics.prune": AuditEvent(
        "metrics.prune", "mutation", "Admin-triggered pruning of yaml-imported metric definitions."
    ),
    "news_draft_saved": AuditEvent("news_draft_saved", "mutation", "An admin news post draft was saved."),
    "news_published": AuditEvent("news_published", "mutation", "An admin news post was published."),
    "news_unpublished": AuditEvent("news_unpublished", "mutation", "An admin news post was unpublished."),
    "rebuild_registry": AuditEvent("rebuild_registry", "mutation", "The table registry was rebuilt."),
    "register_table": AuditEvent("register_table", "mutation", "A table was registered."),
    "session.upload": AuditEvent("session.upload", "mutation", "A session file was uploaded."),
    "slack_share": AuditEvent("slack_share", "mutation", "Content was shared to a Slack channel."),
    "snapshot.create": AuditEvent("snapshot.create", "mutation", "A local snapshot was materialized."),
    "store.submission.approved": AuditEvent(
        "store.submission.approved", "mutation", "A marketplace store submission was approved."
    ),
    "store.submission.blocked_llm": AuditEvent(
        "store.submission.blocked_llm", "mutation", "A store submission was blocked by the LLM content scan."
    ),
    "store.submission.deleted": AuditEvent(
        "store.submission.deleted", "mutation", "A marketplace store submission was deleted."
    ),
    "store.submission.overridden": AuditEvent(
        "store.submission.overridden", "mutation", "A marketplace store submission's verdict was overridden."
    ),
    "store.submission.rescan": AuditEvent(
        "store.submission.rescan", "mutation", "A marketplace store submission was rescanned."
    ),
    "store.submission.retry": AuditEvent(
        "store.submission.retry", "mutation", "A marketplace store submission review was retried."
    ),
    "store.upload.security_blocked": AuditEvent(
        "store.upload.security_blocked", "mutation", "A store upload was blocked by a security check."
    ),
    "sync.trigger": AuditEvent("sync.trigger", "mutation", "A manual sync was triggered."),
    "unregister_table": AuditEvent("unregister_table", "mutation", "A table was unregistered."),
    "update_table": AuditEvent("update_table", "mutation", "A table's registration was updated."),
    "usage.prune": AuditEvent("usage.prune", "mutation", "Admin-triggered pruning of usage session data."),
    "usage.reprocess": AuditEvent("usage.reprocess", "mutation", "Admin-triggered reprocessing of session usage."),
    "user_display_name_updated": AuditEvent(
        "user_display_name_updated", "mutation", "A user's display name was updated."
    ),
    "user_onboarded": AuditEvent("user_onboarded", "mutation", "A new user completed onboarding."),
    # --- entries below appended by later audit-coverage tasks ---
    # Task 2 (F1 — fallback middleware + route-posture ratchet)
    "http.request": AuditEvent(
        "http.request",
        "mutation",
        "Generic fallback row written by AuditFallbackMiddleware for a mutating "
        "request whose handler wrote no audit row of its own.",
    ),
    # Task 7 (F2e — secrets, admin config, distribution channels, ingress)
    "source_connection.create": AuditEvent(
        "source_connection.create", "mutation", "A named data-source connection was created."
    ),
    "source_connection.update": AuditEvent(
        "source_connection.update", "mutation", "A named data-source connection was updated."
    ),
    "source_connection.delete": AuditEvent(
        "source_connection.delete", "mutation", "A named data-source connection was deleted."
    ),
    "source_connection.test": AuditEvent(
        "source_connection.test", "mutation", "A named data-source connection's connectivity was tested."
    ),
    "source_connection.secret.set": AuditEvent(
        "source_connection.secret.set", "mutation", "A source connection's vault-stored token was set/rotated."
    ),
    "source_connection.secret.clear": AuditEvent(
        "source_connection.secret.clear", "mutation", "A source connection's vault-stored token was cleared."
    ),
    "mcp_user_secret.set": AuditEvent(
        "mcp_user_secret.set", "mutation", "An analyst's own per-user MCP source credential was set/rotated."
    ),
    "mcp_user_secret.clear": AuditEvent(
        "mcp_user_secret.clear", "mutation", "An analyst's own per-user MCP source credential was cleared."
    ),
    "mcp_user_secret.test": AuditEvent(
        "mcp_user_secret.test", "mutation", "An analyst's own per-user MCP source credential was test-connected."
    ),
    "mcp_user_secret.read": AuditEvent(
        "mcp_user_secret.read", "read", "An analyst read their own per-user MCP source credential status."
    ),
    "datasource.secret.set": AuditEvent(
        "datasource.secret.set", "mutation", "A server-wide datasource vault secret was set/rotated."
    ),
    "datasource.secret.clear": AuditEvent(
        "datasource.secret.clear", "mutation", "A server-wide datasource vault secret was cleared."
    ),
    "datasource.secret.read": AuditEvent(
        "datasource.secret.read", "read", "The server-wide datasource secrets' presence/source status was read."
    ),
    "slack.secret.set": AuditEvent(
        "slack.secret.set", "mutation", "A server-wide Slack bot vault secret was set/rotated."
    ),
    "slack.secret.clear": AuditEvent(
        "slack.secret.clear", "mutation", "A server-wide Slack bot vault secret was cleared."
    ),
    "slack.secret.read": AuditEvent(
        "slack.secret.read", "read", "The server-wide Slack bot secrets' presence/source status was read."
    ),
    "server_config.read": AuditEvent(
        "server_config.read", "read", "The admin server-config editor's current instance.yaml view was read."
    ),
    "instance.configure": AuditEvent(
        "instance.configure",
        "mutation",
        "POST /api/admin/configure wrote data-source/instance settings to instance.yaml.",
    ),
    "script.deploy": AuditEvent("script.deploy", "mutation", "An admin-authored server script was deployed."),
    "script.run": AuditEvent("script.run", "mutation", "An admin-authored server script was run (ad-hoc or deployed)."),
    "script.delete": AuditEvent("script.delete", "mutation", "A deployed server script was undeployed."),
    "marketplace.bundle_download": AuditEvent(
        "marketplace.bundle_download",
        "read",
        "The aggregated marketplace zip (or a single-plugin repackage) was downloaded.",
    ),
    "marketplace.git_fetch": AuditEvent(
        "marketplace.git_fetch", "read", "The per-user marketplace bare repo was fetched over git smart-HTTP."
    ),
    "marketplace.git_push": AuditEvent(
        "marketplace.git_push", "mutation", "A push was attempted against the per-user marketplace bare repo."
    ),
    "store.bundle_download": AuditEvent(
        "store.bundle_download", "read", "A ZIP export of Store entities was downloaded."
    ),
    "memory.bundle_download": AuditEvent(
        "memory.bundle_download", "read", "A token-budgeted (or per-domain markdown) corporate-memory bundle was read."
    ),
    "webhook.jira_received": AuditEvent(
        "webhook.jira_received", "system", "A Jira webhook event passed signature verification."
    ),
    "webhook.jira_rejected": AuditEvent(
        "webhook.jira_rejected", "system", "A Jira webhook event was rejected (bad/missing signature)."
    ),
    "artifact.upload": AuditEvent(
        "artifact.upload", "mutation", "A user artifact (HTML report, chart, ...) was uploaded."
    ),
    "local_md.upload": AuditEvent(
        "local_md.upload", "mutation", "A CLAUDE.local.md was uploaded for corporate-memory processing."
    ),
    "sync.pull_confirmed": AuditEvent(
        "sync.pull_confirmed", "mutation", "The CLI reported completion of an `agnes pull`."
    ),
    "sync.settings_update": AuditEvent(
        "sync.settings_update", "mutation", "A user's dataset sync settings were updated."
    ),
    "sync.subscriptions_update": AuditEvent(
        "sync.subscriptions_update", "mutation", "A user's per-table subscription settings were updated."
    ),
    # Task 5 (F2c — MCP surface: tool calls, passthrough, per-table, facts)
    "mcp.tool_call": AuditEvent("mcp.tool_call", "read", "An MCP tool was invoked (SSE or Streamable-HTTP transport)."),
    "mcp.passthrough_call": AuditEvent(
        "mcp.passthrough_call", "read", "A passthrough MCP tool call was forwarded to its upstream source."
    ),
    "mcp.passthrough_denied": AuditEvent(
        "mcp.passthrough_denied", "system", "A passthrough MCP tool call was denied by grant/mutating/rate-limit gate."
    ),
    "query.table_scoped": AuditEvent(
        "query.table_scoped", "read", "A per-table outbound MCP tool query ran (POST /api/mcp/query-table/{id})."
    ),
    "facts.search": AuditEvent("facts.search", "read", "The fact graph was searched by type/attribute filters."),
    "facts.neighbors": AuditEvent("facts.neighbors", "read", "The fact graph was traversed from one subject."),
    "facts.claims": AuditEvent("facts.claims", "read", "A fact/edge subject's readable evidence was read."),
    "facts.ingest": AuditEvent("facts.ingest", "mutation", "A fact-graph producer batch was ingested."),
    # Task 6 (F2d — messaging surfaces: Telegram, Slack inbound, chat lifecycle)
    "telegram.bind": AuditEvent("telegram.bind", "auth", "A Telegram account was linked to an Agnes user."),
    "telegram.message": AuditEvent(
        "telegram.message", "read", "The Telegram bot received and processed a message from a linked user."
    ),
    "telegram.script_run": AuditEvent(
        "telegram.script_run",
        "mutation",
        "A Telegram button press ran a user notification script via the sudo path.",
    ),
    "slack.message": AuditEvent(
        "slack.message", "read", "The Slack bot accepted an inbound DM or channel mention from a bound user."
    ),
    "slack.command": AuditEvent("slack.command", "mutation", "A Slack slash command was dispatched."),
    "chat.session.create": AuditEvent("chat.session.create", "mutation", "A chat session was created."),
    "chat.session.delete": AuditEvent("chat.session.delete", "mutation", "A chat session was permanently deleted."),
    "chat.session.archive": AuditEvent("chat.session.archive", "mutation", "A chat session was archived or restored."),
    "chat.session.ticket": AuditEvent(
        "chat.session.ticket", "mutation", "A fresh WebSocket ticket was issued for an existing chat session."
    ),
    "chat.user_message": AuditEvent(
        "chat.user_message", "read", "A user message reached the chat manager's delivery ingress."
    ),
    "chat.copresence.invite": AuditEvent(
        "chat.copresence.invite", "mutation", "A co-presence chat session was created via invite."
    ),
    "chat.copresence.join": AuditEvent(
        "chat.copresence.join", "mutation", "A live participant obtained a co-presence join ticket."
    ),
    "chat.copresence.leave": AuditEvent(
        "chat.copresence.leave", "mutation", "A participant left a co-presence chat session."
    ),
    # Task 4 (F2b — agent invocation + worker/job execution + silent scheduler jobs)
    "agent.invoke": AuditEvent(
        "agent.invoke", "mutation", "An agent was invoked via POST /api/v1/agents/{slug}/responses."
    ),
    "agent.session.create": AuditEvent(
        "agent.session.create", "mutation", "A multi-turn agent-API session was created."
    ),
    "agent.session.message": AuditEvent(
        "agent.session.message", "mutation", "A message was sent on a multi-turn agent-API session."
    ),
    "agent.session.cancel": AuditEvent(
        "agent.session.cancel", "mutation", "A multi-turn agent-API session's in-flight turn was cancelled."
    ),
    "agent.session.delete": AuditEvent(
        "agent.session.delete", "mutation", "A multi-turn agent-API session was deleted (archived)."
    ),
    "agent.webhook.create": AuditEvent("agent.webhook.create", "mutation", "An outbound agent webhook was registered."),
    "agent.webhook.delete": AuditEvent("agent.webhook.delete", "mutation", "An outbound agent webhook was deleted."),
    "job.run": AuditEvent(
        "job.run",
        "system",
        "The worker dispatched and ran one claimed job (any kind) — one row per "
        "job run, regardless of kind, written by the single dispatch-level wrapper.",
    ),
    "run_bq_metadata_refresh": AuditEvent(
        "run_bq_metadata_refresh", "system", "The scheduled BigQuery metadata-cache refresh ran."
    ),
    "run_semantic_sources_refresh": AuditEvent(
        "run_semantic_sources_refresh", "system", "The scheduled semantic-sources refresh sweep ran."
    ),
    # No live writer since #1707 Block 3 step 4 retired the two per-connector
    # refresh triggers in favour of `run_semantic_sources_refresh` above. Kept
    # per this module's append-only contract: rows written under these names
    # before the migration are still in every existing instance's audit_log,
    # and removing the key would turn them into unknown actions on the read
    # side.
    "run_keboola_semantic_layer_refresh": AuditEvent(
        "run_keboola_semantic_layer_refresh", "system", "The scheduled Keboola semantic-layer refresh ran."
    ),
    "run_databricks_semantic_layer_refresh": AuditEvent(
        "run_databricks_semantic_layer_refresh", "system", "The scheduled Databricks semantic-layer refresh ran."
    ),
    "run_store_lint_audit": AuditEvent(
        "run_store_lint_audit", "system", "The scheduled marketplace-store skill-lint audit ran."
    ),
    # Task 3 (F2a — auth remainder: token reads, project import, posture flips)
    "token.list": AuditEvent("token.list", "read", "A personal access token list/detail was read (own/one/admin_all)."),
    "keboola.projects_import": AuditEvent(
        "keboola.projects_import",
        "mutation",
        "Selected discovered Keboola projects were connected (select-mode multi-project login).",
    ),
    # Task 8 (F4 — chat sessions bridge into the analyst-sessions store)
    "chat.session_exported": AuditEvent(
        "chat.session_exported",
        "system",
        "A web-chat session was materialized as a session jsonl under SESSION_DATA_DIR "
        "(admin transcript viewer + usage rollups now cover it, same as any CLI session).",
    ),
    # Task 9 (F3 — client-reported CLI audit events)
    "query.local_offline": AuditEvent(
        "query.local_offline",
        "read",
        "`agnes query` ran against the local DuckDB with no server round-trip; "
        "client-reported after the fact via POST /api/upload/audit-events.",
    ),
    "explore.local_offline": AuditEvent(
        "explore.local_offline",
        "read",
        "`agnes explore` ran against the local DuckDB with no server round-trip; "
        "client-reported after the fact via POST /api/upload/audit-events.",
    ),
    "audit_events.upload": AuditEvent(
        "audit_events.upload",
        "system",
        "A CLI's batch of client-reported audit events was ingested "
        "(one row per batch; params carry accepted/rejected counts).",
    ),
    # Landed on `integration` in parallel with this wave; registered here so
    # the catalog stays the complete list rather than growing an exemption.
    "chat.delegation": AuditEvent(
        "chat.delegation",
        "mutation",
        "A chat session delegated work to an agent profile.",
    ),
    "chat.delegation_denied": AuditEvent(
        "chat.delegation_denied",
        "system",
        "A delegation request was refused (scope or availability).",
    ),
    "upgrade_freeze.set": AuditEvent(
        "upgrade_freeze.set",
        "mutation",
        "An admin froze automatic upgrades for this instance.",
    ),
    "upgrade_freeze.lift": AuditEvent(
        "upgrade_freeze.lift",
        "mutation",
        "An admin lifted the automatic-upgrade freeze.",
    ),
    # Built on `mf/semantic-layer-v0` in parallel with this wave and brought in
    # by the landing-plan step-0 merge; registered here for the same reason as
    # the block above — the catalog stays the complete list rather than growing
    # an exemption.
    "semantic_auto_draft_sweep": AuditEvent(
        "semantic_auto_draft_sweep",
        "mutation",
        "The headless semantic-model auto-draft sweep ran (one row per sweep, "
        "params carry queued/skipped/errored counts).",
    ),
    "semantic_model.detach": AuditEvent(
        "semantic_model.detach",
        "mutation",
        "An admin detached a semantic model from its source, freezing it against further syncs.",
    ),
    "semantic_model.reattach": AuditEvent(
        "semantic_model.reattach",
        "mutation",
        "An admin re-attached a detached semantic model to its source.",
    ),
    # The `agnes pull` semantic-cache delivery channel. Minted here because
    # the wave-2 read-posture ratchet (#1802) reached this branch on the
    # merge and a bundle read is data content leaving the server — the same
    # call `memory.bundle_download` and `store.bundle_download` already make.
    "semantic_model.bundle_download": AuditEvent(
        "semantic_model.bundle_download",
        "read",
        "Someone pulled the RBAC-scoped bundle of readable semantic models "
        "(the local `<workspace>/semantic/` cache `agnes pull` renders).",
    ),
    "semantic_model.link_package": AuditEvent(
        "semantic_model.link_package",
        "mutation",
        "An admin linked a semantic model to a Data Package, granting read access "
        "to everyone with a grant on that package.",
    ),
    "semantic_model.unlink_package": AuditEvent(
        "semantic_model.unlink_package",
        "mutation",
        "An admin unlinked a semantic model from a Data Package.",
    ),
    # The coverage-tag pair writes no row of its own; these two exist so the
    # fallback middleware emits a real domain action for it, since Wave 2 —
    # Task 1 retired the "fallback" posture literal this branch declared.
    "semantic_coverage_tag.create": AuditEvent(
        "semantic_coverage_tag.create",
        "mutation",
        "An admin tagged a semantic-layer scope for the cross-domain coverage report.",
    ),
    "semantic_coverage_tag.delete": AuditEvent(
        "semantic_coverage_tag.delete",
        "mutation",
        "An admin removed a semantic-layer coverage tag.",
    ),
    "semantic_health_mute.create": AuditEvent(
        "semantic_health_mute.create",
        "mutation",
        "An admin silenced a semantic-layer health check for a scope.",
    ),
    "semantic_health_mute.delete": AuditEvent(
        "semantic_health_mute.delete",
        "mutation",
        "An admin lifted a semantic-layer health-check mute.",
    ),
    "semantic_feedback.submit": AuditEvent(
        "semantic_feedback.submit",
        "mutation",
        "Someone (person or agent) flagged an answer as wrong against the semantic layer.",
    ),
    "semantic_feedback.resolved": AuditEvent(
        "semantic_feedback.resolved",
        "mutation",
        "An admin resolved a semantic-layer feedback report.",
    ),
    # -- Wave 2 -- Task 1: declarative action emission -----------------------
    # 127 actions minted while flipping the 135 mutating routes that were
    # declared "fallback" in src/audit_posture.py into their real domain
    # action; the other 8 reuse an action already cataloged above (one shared
    # pre-existing action, "initial_workspace.sync", plus 7 duplicate mappings
    # of a newly-minted action onto more than one route -- see
    # src/audit_posture.py for the full route -> action table.
    "table_registry.docs_update": AuditEvent(
        "table_registry.docs_update", "mutation", "A registered table's admin-authored docs were updated."
    ),
    "table_registry.discover_and_register": AuditEvent(
        "table_registry.discover_and_register",
        "mutation",
        "Auto-discovery registered a batch of matching tables at once.",
    ),
    "table_registry.register_precheck": AuditEvent(
        "table_registry.register_precheck",
        "read",
        "An admin dry-ran the register-table validation without registering anything.",
    ),
    "access_policy.compile": AuditEvent(
        "access_policy.compile", "mutation", "An admin compiled a table access policy's SQL from its builder form."
    ),
    "data_source.bigquery_connection_test": AuditEvent(
        "data_source.bigquery_connection_test",
        "read",
        "An admin ran the configured BigQuery connection's health probe.",
    ),
    "chat.session.admin_kill": AuditEvent(
        "chat.session.admin_kill", "mutation", "An admin force-terminated another user's live chat session."
    ),
    "chat.secrets.test": AuditEvent(
        "chat.secrets.test", "read", "An admin tested the configured chat-engine secrets' connectivity."
    ),
    "chat.session.tail_ticket_issue": AuditEvent(
        "chat.session.tail_ticket_issue",
        "mutation",
        "An admin issued a short-lived ticket to tail a live chat session's debug stream.",
    ),
    "contributed_skill.delete": AuditEvent(
        "contributed_skill.delete",
        "mutation",
        "A contributed-skill plugin was removed from the contributed marketplace.",
    ),
    "contributed_skill.create": AuditEvent(
        "contributed_skill.create", "mutation", "A skill was published to the contributed marketplace."
    ),
    "datasource.gws_credentials_validate": AuditEvent(
        "datasource.gws_credentials_validate",
        "read",
        "An admin validated a Google Workspace service-account credential before saving it.",
    ),
    "diagnostics.new_instance_check": AuditEvent(
        "diagnostics.new_instance_check", "system", "An admin ran the new-instance deployment-gate doctor checks."
    ),
    "data_source.keboola_connection_test": AuditEvent(
        "data_source.keboola_connection_test", "read", "An admin ran the configured Keboola connection's health probe."
    ),
    "mcp_source.oauth_register": AuditEvent(
        "mcp_source.oauth_register",
        "mutation",
        "An MCP source's OAuth client was dynamically registered with its authorization server.",
    ),
    "mcp_source.oauth_client_update": AuditEvent(
        "mcp_source.oauth_client_update", "mutation", "An MCP source's OAuth client credentials were updated."
    ),
    "sharepoint_connection.scope_remove": AuditEvent(
        "sharepoint_connection.scope_remove",
        "mutation",
        "A confirmed SharePoint scope (site/library/folder) was removed from a connection.",
    ),
    "sharepoint_connection.scope_confirm": AuditEvent(
        "sharepoint_connection.scope_confirm",
        "mutation",
        "An admin confirmed a SharePoint site/library/folder as an ingested scope.",
    ),
    "source_connection.chat_tools_disable": AuditEvent(
        "source_connection.chat_tools_disable",
        "mutation",
        "A source connection's derived chat tools were disabled and removed.",
    ),
    "source_connection.chat_tools_enable": AuditEvent(
        "source_connection.chat_tools_enable",
        "mutation",
        "A source connection's tables were registered as derived chat tools.",
    ),
    "sso.client_secret_clear": AuditEvent("sso.client_secret_clear", "mutation", "The SSO client secret was cleared."),
    "sso.config_delete": AuditEvent("sso.config_delete", "mutation", "The SSO configuration was deleted."),
    "sso.identity_unlink": AuditEvent(
        "sso.identity_unlink", "mutation", "An admin unlinked a user's external SSO identity."
    ),
    "sso.test_config": AuditEvent(
        "sso.test_config", "read", "An admin tested the SSO configuration's discovery document."
    ),
    "sso.client_secret_set": AuditEvent("sso.client_secret_set", "mutation", "The SSO client secret was set."),
    "sso.config_update": AuditEvent("sso.config_update", "mutation", "The SSO configuration was updated."),
    "agent.builder_turn": AuditEvent(
        "agent.builder_turn", "mutation", "An admin exchanged one turn with the agent-profile builder assistant."
    ),
    "agent_schedules.delete": AuditEvent(
        "agent_schedules.delete", "mutation", "A scheduled agent invocation was deleted."
    ),
    "agent_schedules.update": AuditEvent(
        "agent_schedules.update", "mutation", "A scheduled agent invocation was updated."
    ),
    "agent_schedules.create": AuditEvent(
        "agent_schedules.create", "mutation", "A scheduled agent invocation was created."
    ),
    "agent.delete": AuditEvent("agent.delete", "mutation", "An agent profile was deleted."),
    "agent.create": AuditEvent("agent.create", "mutation", "An agent profile was created."),
    "agent.token_create": AuditEvent("agent.token_create", "mutation", "A PAT scoped to an agent profile was minted."),
    "agent.update": AuditEvent("agent.update", "mutation", "An agent profile's configuration was updated."),
    "agent.scope_update": AuditEvent(
        "agent.scope_update", "mutation", "An agent profile's authority scope was updated."
    ),
    "metadata_cache.refresh_table": AuditEvent(
        "metadata_cache.refresh_table",
        "mutation",
        "A single remote table's metadata cache entry was refreshed on demand.",
    ),
    "cache_warmup.run": AuditEvent("cache_warmup.run", "mutation", "An admin triggered a catalog cache-warmup run."),
    "catalog.profile_refresh": AuditEvent(
        "catalog.profile_refresh", "mutation", "A table's data profile was recomputed on demand."
    ),
    "chat.journey_update": AuditEvent(
        "chat.journey_update", "mutation", "A caller's onboarding/journey progress was updated."
    ),
    "chat.session.pin_set": AuditEvent("chat.session.pin_set", "mutation", "A chat session's pinned flag was changed."),
    "chat.session.title_update": AuditEvent(
        "chat.session.title_update", "mutation", "A chat session's title was changed."
    ),
    "chat.copresence.fork": AuditEvent(
        "chat.copresence.fork", "mutation", "A shared chat session was forked into the caller's own copy."
    ),
    "chat.session_file.save_artefact": AuditEvent(
        "chat.session_file.save_artefact",
        "mutation",
        "A chat session's engine-side file was saved as a permanent chat artefact.",
    ),
    "chat.upload": AuditEvent(
        "chat.upload", "mutation", "A file was uploaded to chat and registered as a workspace table."
    ),
    "workspace_prompt_template.reset": AuditEvent(
        "workspace_prompt_template.reset",
        "mutation",
        "The analyst-workspace CLAUDE.md prompt template was reset to its default.",
    ),
    "workspace_prompt_template.preview": AuditEvent(
        "workspace_prompt_template.preview",
        "read",
        "An admin previewed the rendered analyst-workspace CLAUDE.md prompt template.",
    ),
    "workspace_prompt_template.update": AuditEvent(
        "workspace_prompt_template.update", "mutation", "The analyst-workspace CLAUDE.md prompt template was updated."
    ),
    "collection.delete": AuditEvent("collection.delete", "mutation", "A knowledge collection was deleted."),
    "collection.file_delete": AuditEvent(
        "collection.file_delete", "mutation", "A file was deleted from a knowledge collection."
    ),
    "collection.create": AuditEvent("collection.create", "mutation", "A knowledge collection was created."),
    "collection.file_add": AuditEvent(
        "collection.file_add", "mutation", "A file was uploaded into a knowledge collection."
    ),
    "collection.file_move": AuditEvent(
        "collection.file_move", "mutation", "A file was moved between knowledge collections."
    ),
    "collection.file_reingest": AuditEvent(
        "collection.file_reingest", "mutation", "A knowledge collection file was queued for re-ingestion."
    ),
    "setup_token.revoke": AuditEvent(
        "setup_token.revoke", "mutation", "A self-service Cowork setup token was revoked."
    ),
    "setup_token.exchange": AuditEvent(
        "setup_token.exchange", "auth", "A self-service Cowork setup token was exchanged for a session."
    ),
    "cowork_bundle.generate": AuditEvent(
        "cowork_bundle.generate",
        "mutation",
        "A personal Cowork onboarding bundle (credentials + skills) was generated.",
    ),
    "data_app.git_fetch": AuditEvent(
        "data_app.git_fetch", "read", "A data app's own git repo was fetched (clone/pull) over smart-HTTP."
    ),
    "data_app.proxy_mutation": AuditEvent(
        "data_app.proxy_mutation",
        "mutation",
        "A non-GET request was proxied into a running data app's own container via the legacy path-based proxy.",
    ),
    "db_migration.cancel": AuditEvent(
        "db_migration.cancel", "mutation", "An admin cancelled an in-progress app-state database migration job."
    ),
    "db_migration.start": AuditEvent(
        "db_migration.start", "mutation", "An admin started an app-state database migration job."
    ),
    "store.entity_builder_preview_agent": AuditEvent(
        "store.entity_builder_preview_agent",
        "mutation",
        "An admin previewed the review-agent's response while building a store entity.",
    ),
    "store.entity_builder_turn": AuditEvent(
        "store.entity_builder_turn", "mutation", "An admin exchanged one turn with the store-entity builder assistant."
    ),
    "job.enqueue": AuditEvent("job.enqueue", "mutation", "A durable worker-runtime job was enqueued."),
    "kai.session_create": AuditEvent("kai.session_create", "mutation", "A Kai chat engine session was created."),
    "kai.tickets_issue": AuditEvent(
        "kai.tickets_issue", "mutation", "Short-lived Kai session egress tickets were (re)issued."
    ),
    "mcp_source.builder_turn": AuditEvent(
        "mcp_source.builder_turn", "mutation", "An admin exchanged one turn with the MCP-source builder assistant."
    ),
    "mcp_source.oauth_disconnect": AuditEvent(
        "mcp_source.oauth_disconnect", "mutation", "An MCP source's stored OAuth connection was disconnected."
    ),
    "mcp_streamable.session_delete": AuditEvent(
        "mcp_streamable.session_delete", "mutation", "An MCP streamable-HTTP session was terminated."
    ),
    "mcp_streamable.request": AuditEvent(
        "mcp_streamable.request", "system", "A JSON-RPC request was sent over the MCP streamable-HTTP transport."
    ),
    "corporate_memory.undismiss": AuditEvent(
        "corporate_memory.undismiss", "mutation", "A caller un-dismissed a previously dismissed corporate-memory item."
    ),
    "corporate_memory.create": AuditEvent(
        "corporate_memory.create", "mutation", "A corporate-memory knowledge item was submitted."
    ),
    "corporate_memory.contradiction_create": AuditEvent(
        "corporate_memory.contradiction_create",
        "mutation",
        "An admin manually recorded a corporate-memory contradiction.",
    ),
    "corporate_memory.dismiss": AuditEvent(
        "corporate_memory.dismiss", "mutation", "A caller dismissed a corporate-memory item from their own feed."
    ),
    "corporate_memory.toggle_personal": AuditEvent(
        "corporate_memory.toggle_personal", "mutation", "A caller toggled a corporate-memory item's personal-only flag."
    ),
    "corporate_memory.vote": AuditEvent(
        "corporate_memory.vote", "mutation", "A caller voted on a corporate-memory item."
    ),
    "memory_mining.run": AuditEvent(
        "memory_mining.run", "mutation", "An admin triggered an ad-hoc corporate-memory mining run."
    ),
    "memory_mining.consent_set": AuditEvent(
        "memory_mining.consent_set", "mutation", "A user set their corporate-memory mining consent preference."
    ),
    "table_metadata.save": AuditEvent(
        "table_metadata.save", "mutation", "An admin saved hand-authored metadata for a registered table."
    ),
    "table_metadata.push": AuditEvent(
        "table_metadata.push", "mutation", "A table's metadata was pushed back to its upstream source system."
    ),
    "metric_definition.delete": AuditEvent(
        "metric_definition.delete", "mutation", "A business metric definition was deleted."
    ),
    "metric_definition.upsert": AuditEvent(
        "metric_definition.upsert", "mutation", "A business metric definition was created or updated."
    ),
    "metric_definition.import": AuditEvent(
        "metric_definition.import", "mutation", "Business metric definitions were bulk-imported from a directory."
    ),
    "my_stack.curated_toggle": AuditEvent(
        "my_stack.curated_toggle",
        "mutation",
        "A caller enabled or disabled a curated-marketplace plugin in their own stack.",
    ),
    "news_previewed": AuditEvent("news_previewed", "read", "An admin previewed a news banner draft's rendered output."),
    "observability_view.delete": AuditEvent(
        "observability_view.delete", "mutation", "A saved observability view was deleted."
    ),
    "observability_view.create": AuditEvent(
        "observability_view.create", "mutation", "A caller saved a new observability view."
    ),
    "ontology_draft.delete": AuditEvent("ontology_draft.delete", "mutation", "An ontology draft was deleted."),
    "ontology_draft.create": AuditEvent("ontology_draft.create", "mutation", "An ontology draft was created."),
    "ontology_draft.import": AuditEvent(
        "ontology_draft.import", "mutation", "Text was imported into an ontology draft via the extraction pipeline."
    ),
    "ontology_draft.save": AuditEvent("ontology_draft.save", "mutation", "An ontology draft was saved/promoted."),
    "ontology_draft.dry_run": AuditEvent(
        "ontology_draft.dry_run", "read", "An admin dry-ran ontology extraction without persisting a draft."
    ),
    "ontology_draft.update": AuditEvent("ontology_draft.update", "mutation", "An ontology draft was updated."),
    "data_package.builder_turn": AuditEvent(
        "data_package.builder_turn", "mutation", "An admin exchanged one turn with the data-package builder assistant."
    ),
    "prompt.delete": AuditEvent(
        "prompt.delete", "mutation", "An admin-authored system prompt override was reset to its default."
    ),
    "prompt.bind_git": AuditEvent(
        "prompt.bind_git", "mutation", "A system prompt was bound to a git-tracked source file."
    ),
    "prompt.preview": AuditEvent("prompt.preview", "read", "An admin previewed a system prompt's rendered output."),
    "prompt.source_set": AuditEvent(
        "prompt.source_set", "mutation", "A system prompt's authoring source was changed (inline vs. git-bound)."
    ),
    "prompt.update": AuditEvent("prompt.update", "mutation", "An admin-authored system prompt override was updated."),
    "semantic_model.delete": AuditEvent("semantic_model.delete", "mutation", "A semantic model document was deleted."),
    "semantic_source.delete": AuditEvent("semantic_source.delete", "mutation", "A semantic-layer source was deleted."),
    "semantic_model.create": AuditEvent("semantic_model.create", "mutation", "A semantic model document was created."),
    "semantic_source.create": AuditEvent(
        "semantic_source.create", "mutation", "A semantic-layer source was registered."
    ),
    "semantic_source.sync": AuditEvent(
        "semantic_source.sync", "mutation", "A semantic-layer source was manually synced."
    ),
    "semantic_model.validate_query": AuditEvent(
        "semantic_model.validate_query",
        "read",
        "A SQL query was validated against a semantic model's constraints without executing it.",
    ),
    "semantic_model.update": AuditEvent("semantic_model.update", "mutation", "A semantic model document was updated."),
    "semantic_source.update": AuditEvent(
        "semantic_source.update", "mutation", "A semantic-layer source's configuration was updated."
    ),
    "settings.dataset_update": AuditEvent(
        "settings.dataset_update", "mutation", "A caller's default dataset preference was updated."
    ),
    "share_request.decide": AuditEvent(
        "share_request.decide", "mutation", "An admin approved or denied a pending share request."
    ),
    "sharing.state_update": AuditEvent(
        "sharing.state_update", "mutation", "A resource's sharing state (which groups can see it) was updated."
    ),
    "slack.command_received": AuditEvent(
        "slack.command_received", "system", "A Slack slash command was received and dispatched."
    ),
    "slack.event_received": AuditEvent(
        "slack.event_received", "system", "A Slack Events API callback was received and dispatched."
    ),
    "stack.artefact_remove": AuditEvent(
        "stack.artefact_remove", "mutation", "A knowledge artefact was removed from a caller's stack."
    ),
    "stack.unsubscribe": AuditEvent(
        "stack.unsubscribe", "mutation", "A caller unsubscribed from a resource in their stack."
    ),
    "stack.artefact_add": AuditEvent(
        "stack.artefact_add", "mutation", "A knowledge artefact was added to a caller's stack."
    ),
    "stack.subscribe": AuditEvent(
        "stack.subscribe", "mutation", "A caller subscribed to a resource (e.g. a data package) in their stack."
    ),
    "store.entity_dryrun": AuditEvent(
        "store.entity_dryrun", "read", "A store entity submission was dry-run validated without being created."
    ),
    "store.entity_preview": AuditEvent(
        "store.entity_preview", "read", "A store entity submission was previewed without being created."
    ),
    "dismiss_store_lint_finding": AuditEvent(
        "dismiss_store_lint_finding", "mutation", "An admin dismissed a store-lint finding."
    ),
    "telegram.unlink": AuditEvent("telegram.unlink", "mutation", "A caller unlinked their Telegram account."),
    "token.admin_revoke": AuditEvent(
        "token.admin_revoke", "mutation", "An admin revoked another user's personal access token."
    ),
    "cover_image.upload": AuditEvent(
        "cover_image.upload", "mutation", "An admin uploaded a cover image for a data package or memory domain."
    ),
    "welcome_template.reset": AuditEvent(
        "welcome_template.reset", "mutation", "The welcome-banner template was reset to its default."
    ),
    "welcome_template.preview": AuditEvent(
        "welcome_template.preview", "read", "An admin previewed the welcome banner's rendered output."
    ),
    "welcome_template.update": AuditEvent(
        "welcome_template.update", "mutation", "The welcome-banner template was updated."
    ),
    "mcp_oauth.consent_decision": AuditEvent(
        "mcp_oauth.consent_decision", "auth", "A user approved or denied an MCP OAuth client's consent request."
    ),
    "auth.magic_link_sent": AuditEvent("auth.magic_link_sent", "auth", "A sign-in magic link was minted and mailed."),
    "password_reset_requested": AuditEvent("password_reset_requested", "auth", "A password-reset link was requested."),
    "logout": AuditEvent("logout", "auth", "A user signed out."),
    # -- Wave 2 -- Task 2: reads and WebSocket routes join the ratchet --------
    # New actions for sensitive GET routes that were still completely
    # unaudited (READ_POSTURE, src/audit_posture.py) -- everything else
    # sensitive reuses an action already cataloged above. Two of these
    # (adoption.kpis, adoption.user_kpis) plus mcp_oauth.connect were ALREADY
    # being written by live code before this task (via a positional `action`
    # argument the catalog literal-scan can't see), just never registered
    # here -- a pre-existing catalog gap this task closes in passing rather
    # than filing a follow-up for a one-line fix.
    "adoption.kpis": AuditEvent(
        "adoption.kpis", "read", "An admin viewed the adoption dashboard's global KPI summary."
    ),
    "adoption.user_kpis": AuditEvent("adoption.user_kpis", "read", "An admin viewed one user's adoption KPI summary."),
    "admin.sessions_browse": AuditEvent(
        "admin.sessions_browse", "read", "An admin browsed the cross-user session browser's data grid."
    ),
    "admin.user_sessions_read": AuditEvent(
        "admin.user_sessions_read", "read", "An admin read a specific user's session list."
    ),
    "agent.session.artifact_download": AuditEvent(
        "agent.session.artifact_download", "read", "An agent-API session's harvested artifact was downloaded."
    ),
    "chat.session.admin_list": AuditEvent(
        "chat.session.admin_list", "read", "An admin listed every currently-live chat session across all users."
    ),
    "chat.session_file.download": AuditEvent(
        "chat.session_file.download", "read", "A file inside a chat sandbox session was downloaded."
    ),
    "collection.file_download": AuditEvent(
        "collection.file_download", "read", "A collection file's raw bytes were downloaded."
    ),
    "collection.file_preview": AuditEvent("collection.file_preview", "read", "A collection file was previewed."),
    "collection.search": AuditEvent("collection.search", "read", "The caller's accessible collections were searched."),
    "knowledge.search": AuditEvent(
        "knowledge.search",
        "read",
        "A combined search ran across documents, the knowledge base, and the table catalog.",
    ),
    "mcp_oauth.connect": AuditEvent(
        "mcp_oauth.connect", "auth", "A user completed an MCP source's OAuth authorization-code exchange."
    ),
    "memory.admin_audit_read": AuditEvent(
        "memory.admin_audit_read", "read", "An admin read the corporate-memory governance audit trail."
    ),
    "sharepoint_connection.certificate_read": AuditEvent(
        "sharepoint_connection.certificate_read",
        "read",
        "An admin read a SharePoint connection's certificate metadata (never the private key).",
    ),
    "sharepoint_connection.corpus_map_read": AuditEvent(
        "sharepoint_connection.corpus_map_read",
        "read",
        "The SharePoint connection's scope-to-collection corpus map was read.",
    ),
    "sharepoint_connection.scopes_read": AuditEvent(
        "sharepoint_connection.scopes_read", "read", "An admin read a SharePoint connection's configured scopes."
    ),
    "sharepoint_connection.tree_browse": AuditEvent(
        "sharepoint_connection.tree_browse",
        "read",
        "An admin browsed a SharePoint connection's site/drive/folder tree.",
    ),
    "sharepoint_connection.tree_search": AuditEvent(
        "sharepoint_connection.tree_search", "read", "An admin searched a SharePoint connection's folder tree."
    ),
    "source_connection.tables_discover": AuditEvent(
        "source_connection.tables_discover",
        "read",
        "An admin browsed a connected source's discoverable tables before registering any.",
    ),
    "sso.config_read": AuditEvent(
        "sso.config_read", "read", "An admin read the SSO configuration's status (never the client secret)."
    ),
    "sso.identities_list": AuditEvent(
        "sso.identities_list", "read", "An admin listed every linked external identity across all users."
    ),
    "table_registry.discover_preview": AuditEvent(
        "table_registry.discover_preview",
        "read",
        "An admin previewed auto-discoverable tables without registering any.",
    ),
    # Wave 2 — Task 3 (the three surfaces that wrote nothing: apps-runner
    # container lifecycle report-back, the data-app subdomain proxy's
    # windowed access log, and the notifications WS connect/reject events).
    "data_app.container_up": AuditEvent(
        "data_app.container_up",
        "mutation",
        "apps-runner started (or replaced) a data app's container. Reported "
        "best-effort by the runner sidecar, which holds no database "
        "access of its own — see services/apps_runner/audit_report.py.",
    ),
    "data_app.container_stop": AuditEvent(
        "data_app.container_stop",
        "mutation",
        "apps-runner stopped (paused or removed) a data app's container. Reported best-effort by the runner sidecar.",
    ),
    "data_app.container_resume": AuditEvent(
        "data_app.container_resume",
        "mutation",
        "apps-runner unpaused a data app's container. Reported best-effort "
        "by the runner sidecar — this event had no control-plane "
        "counterpart at all before this task, which is why it was invisible.",
    ),
    "data_app.access": AuditEvent(
        "data_app.access",
        "read",
        "An end user's traffic reached a deployed data app through the "
        "subdomain proxy. One row per (user, app) per 15-minute window, "
        "not per request — see app/data_apps_subdomain.py.",
    ),
    "notifications.ws_connect": AuditEvent(
        "notifications.ws_connect",
        "system",
        "A desktop/browser notifications WebSocket connection authenticated and was registered.",
    ),
    "notifications.ws_rejected": AuditEvent(
        "notifications.ws_rejected",
        "system",
        "A notifications WebSocket connection attempt was refused "
        "(bad/expired token, malformed handshake, or over the per-user cap).",
    ),
    # -- Wave 2 — admin chat tail (the WS gap Task 2 flagged rather than
    # folding into "noise"; closed here) --------------------------------------
    "chat.session.tail_view": AuditEvent(
        "chat.session.tail_view",
        "read",
        "An admin started streaming another user's live chat log. Distinct "
        "from chat.session.tail_ticket_issue, which records only that "
        "permission was granted — this row records that it was used.",
    ),
    "chat.session.tail_rejected": AuditEvent(
        "chat.session.tail_rejected",
        "system",
        "A live-chat-log stream was refused (invalid or expired ticket).",
    ),
    # -- Landed on `integration` in parallel with this wave; declared here
    # because the "fallback" posture value they arrived with no longer exists.
    "sharepoint_connection.extract": AuditEvent(
        "sharepoint_connection.extract",
        "mutation",
        "A document extraction was started for one SharePoint connection.",
    ),
    "run_sharepoint_extraction": AuditEvent(
        "run_sharepoint_extraction",
        "system",
        "The scheduler's SharePoint extraction sweep ran.",
    ),
    # -- 2026-08-30 plan, Task 4: sharepoint-acl-sync worker job — per-scope
    # SharePoint permission mirroring (connectors/sharepoint/acl_sync.py).
    # `sync_triggered` is emitted by Task 5's admin route, registered here
    # per the plan's instruction (declared alongside its sibling actions).
    "sharepoint_acl.sync_completed": AuditEvent(
        "sharepoint_acl.sync_completed",
        "mutation",
        "A SharePoint ACL-mirroring sync completed for one connection.",
    ),
    "sharepoint_acl.sync_failed": AuditEvent(
        "sharepoint_acl.sync_failed",
        "mutation",
        "A SharePoint ACL-mirroring sync failed for one connection.",
    ),
    "sharepoint_acl.grant_added": AuditEvent(
        "sharepoint_acl.grant_added",
        "mutation",
        "A SharePoint ACL sync granted a mirrored group access to a collection.",
    ),
    "sharepoint_acl.grant_removed": AuditEvent(
        "sharepoint_acl.grant_removed",
        "mutation",
        "A SharePoint ACL sync revoked a mirrored group's access to a collection (source-side revocation).",
    ),
    "sharepoint_acl.membership_replaced": AuditEvent(
        "sharepoint_acl.membership_replaced",
        "mutation",
        "A SharePoint ACL sync replaced a mirrored group's membership from the source directory.",
    ),
    "sharepoint_acl.principal_unmatched": AuditEvent(
        "sharepoint_acl.principal_unmatched",
        "mutation",
        "A SharePoint ACL sync could not resolve one or more source principals to an Agnes account "
        "(one row per run, with counts — never per-user).",
    ),
    "sharepoint_acl.grants_suspended": AuditEvent(
        "sharepoint_acl.grants_suspended",
        "mutation",
        "A SharePoint connection's mirrored grants were suspended after exceeding "
        "acl_sync.max_stale_hours (must_not guarantee mode).",
    ),
    "sharepoint_acl.sync_triggered": AuditEvent(
        "sharepoint_acl.sync_triggered",
        "mutation",
        "An admin manually triggered a SharePoint ACL sync for one connection (POST .../acl-sync).",
    ),
    # -- 2026-08-30 plan, Task 7: broken-inheritance subtree sweep. The
    # sweep job itself (connectors/sharepoint/acl_sync.py::run_subtree_sweep)
    # writes no audit row of its own (state persisted into the connection's
    # config, no log_safe call) -- only the admin-facing per-subtree
    # "include anyway" override gets one, since it is a deliberate,
    # security-relevant decision (spec §3(b)'s should_not-only escape hatch).
    "sharepoint_acl.subtree_override": AuditEvent(
        "sharepoint_acl.subtree_override",
        "mutation",
        "An admin overrode a detected broken-inheritance subtree exclusion "
        "('include anyway') on one SharePoint scope — should_not guarantee mode only.",
    ),
}


# ---------------------------------------------------------------------------
# LEGACY_ALIASES — read-side only mapping from a retired action name to its
# current name. No writer in this codebase currently emits "co_session_fork"
# any more (Task 6 — F2d — renamed the one write site to "chat.copresence.
# invite" for naming symmetry with the sibling join/leave actions added in
# the same task); the "co_session_fork" key stays in CATALOG above, unrenamed
# and unremoved, so historical rows written under it remain a known action —
# this dict is what actually resolves them to their current name for
# read-side classification. `is_cataloged` does NOT consult this dict — an
# alias key is by definition a name nothing should still be emitting.
# ---------------------------------------------------------------------------
LEGACY_ALIASES: dict[str, str] = {
    "co_session_fork": "chat.copresence.invite",
}


# ---------------------------------------------------------------------------
# DYNAMIC_ACTION_PREFIXES — f-string writers whose suffix varies per call
# (e.g. `action=f"mcp_tool.{tool_name}"`). Exact list from the Task 1
# Interfaces contract; later tasks may rely on this exact tuple, so treat it
# as append-only from the end, never reorder or remove an existing prefix.
# ---------------------------------------------------------------------------
DYNAMIC_ACTION_PREFIXES: tuple[str, ...] = (
    "run_session_processor:",
    "corporate_memory.",
    "authoring_suggestion.",
    "agent.memory.",
    "store.",
    "mcp_source.",
    "mcp_tool.",
    "data_app.",
    "marketplace.",
    "user_group.",
    "resource_grant.",
    "recipe.",
    "data_package.",
    "memory_domain.",
    "knowledge_digest.",
    "user.",
    "initial_workspace.",
)


def is_cataloged(action: str) -> bool:
    """True when *action* is a known, classified audit action.

    Either an exact key in :data:`CATALOG`, or it starts with one of
    :data:`DYNAMIC_ACTION_PREFIXES`. Legacy aliases are intentionally NOT
    consulted here — they are a read-side classification aid, not a
    write-side allowlist.
    """
    if action in CATALOG:
        return True
    return action.startswith(DYNAMIC_ACTION_PREFIXES)
