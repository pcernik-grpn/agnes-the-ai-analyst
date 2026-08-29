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
