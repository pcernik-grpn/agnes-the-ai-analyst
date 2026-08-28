"""Repository factory — backend selection lives here, not at the callsites.

Each ``<name>_repo()`` function returns a ready-to-use repository instance:

* ``DATABASE_URL`` (or legacy ``AGNES_DB_URL``) unset  → DuckDB ``system.duckdb`` repos
* ``DATABASE_URL`` (or legacy ``AGNES_DB_URL``) set    → Postgres-backed ``*_pg`` repos

Postgres is the canonical, only-growing app-state backend. The DuckDB
backend is frozen (PG-first ratchet, A3 — see ``CLAUDE.md`` -> "Dual-backend
discipline"): existing pairs below stay registered on both backends, but a
NEW repo key registers Postgres-only. Resolving a Postgres-only repo while
the active backend is DuckDB raises :class:`RequiresPostgresBackend` instead
of constructing anything.

Callsites import factory functions instead of repository classes:

    # Before
    from src.repositories.users import UserRepository
    repo = UserRepository(conn)

    # After
    from src.repositories import users_repo
    repo = users_repo()

The choice is computed per-call, so a process that flips the env var in
tests (via ``monkeypatch.setenv``) immediately routes to the new
backend. DuckDB repos accept a fresh cursor on the singleton system DB;
PG repos accept the singleton engine.

We DO NOT mix backends within a request: every factory consults the
same env var, so within one request all repos resolve to the same
side. Cross-repo transactional guarantees are then identical to the
pre-existing single-conn behaviour.

Backend dispatch is a *declarative registry*, not a hand-written
two-way ``if`` per repo. :data:`_REGISTRY` maps each repo key to a
``{backend: (module_path, class_name)}`` table, and :func:`_build`
resolves the active backend, imports the class lazily, and constructs
it with that backend's connection argument (see :data:`_ARG_PROVIDERS`).

Adding a new backend (e.g. ``duckdb_quack``, see
``src/db_state_machine.py``) is therefore localised:

  1. teach :func:`use_pg` / a future ``active_backend()`` to return the
     new backend key,
  2. add the new key to :data:`_ARG_PROVIDERS` (how to obtain its
     connection/engine),
  3. fill the new column in :data:`_REGISTRY` (one ``(module, class)``
     per repo).

The dispatch logic in :func:`_build` is backend-count-agnostic — no
per-repo function changes — and ``tests/test_repository_registry.py``
verifies each entry is either fully symmetric or Postgres-only (see
:class:`RequiresPostgresBackend`, below — the DuckDB app-state backend is
frozen post-A3, so a NEW entry never carries a DuckDB backend without one).
"""

from __future__ import annotations

import os
from importlib import import_module
from typing import Any

# Re-exports of the legacy DuckDB connection helpers — many callers
# still ``from src.repositories import get_system_db``. Keep them here
# until those imports are migrated to call the factory directly.
from src.db import get_analytics_db, get_system_db

# Re-exported, not defined here: exception handling is keyed on class identity
# and several test harnesses ``importlib.reload`` this module, which would mint
# a new class and silently unbind the app-wide 501 handler. See
# ``src/repository_errors.py``.
from src.repository_errors import RequiresPostgresBackend

__all__ = [
    "get_system_db",
    "get_analytics_db",
    "use_pg",
    "RequiresPostgresBackend",
    # Core user / RBAC cluster
    "users_repo",
    "user_groups_repo",
    "user_group_members_repo",
    "resource_grants_repo",
    "audit_repo",
    # Ops cluster
    "table_registry_repo",
    "sync_state_repo",
    # Config / templates / tokens
    "metric_repo",
    "glossary_repo",
    "semantic_model_repo",
    "semantic_source_repo",
    "claude_md_template_repo",
    "welcome_template_repo",
    "news_template_repo",
    "access_token_repo",
    "profile_repo",
    # Lookup / cache
    "view_ownership_repo",
    "column_metadata_repo",
    "bq_metadata_cache_repo",
    "sync_settings_repo",
    "notifications_telegram_repo",
    "notifications_pending_code_repo",
    "notifications_script_repo",
    # Telemetry
    "session_processor_state_repo",
    "observability_views_repo",
    "usage_repo",
    "reports_repo",
    # Store / marketplace
    "marketplace_registry_repo",
    "marketplace_plugins_repo",
    "store_entities_repo",
    "store_entity_votes_repo",
    "user_store_installs_repo",
    "user_curated_subscriptions_repo",
    "store_submissions_repo",
    "store_lint_repo",
    # Knowledge
    "knowledge_repo",
    # Data packages / memory / recipes / subscriptions
    "data_packages_repo",
    "authoring_suggestions_repo",
    "memory_mining_consent_repo",
    "memory_domain_suggestions_repo",
    "memory_domains_repo",
    "recipes_repo",
    "user_stack_subscriptions_repo",
    "user_journey_repo",
    # MCP / Cowork
    "mcp_sources_repo",
    "per_user_secrets_repo",
    "shared_secrets_repo",
    "system_secrets_repo",
    "tool_registry_repo",
    "setup_tokens_repo",
    # Outbound MCP OAuth data layer (v109)
    "mcp_source_oauth_clients_repo",
    "mcp_user_oauth_tokens_repo",
    "mcp_oauth_flows_repo",
    # Source connections
    "source_connections_repo",
    "connection_secrets_repo",
    # Cloud chat
    "chat_session_repo",
    "chat_message_repo",
    "user_workdirs_repo",
    "chat_session_participants_repo",
    # OAuth 2.1 MCP connector
    "oauth_clients_repo",
    # Collections
    "file_corpora_repo",
    "corpus_files_repo",
    "corpus_chunks_repo",
    "corpus_file_sources_repo",
    # Fact graph over Collections
    "facts_repo",
    "ontology_drafts_repo",
    "facts_ingest_runs_repo",
    # Agent registry (v103) — the Library's agent items
    "agents_repo",
    # Maintained digests (K4, #799)
    "knowledge_digests_repo",
    # Chat sandbox secret broker tickets
    "ticket_repo",
    # Job queue (wave-2B worker runtime foundation)
    "jobs_repo",
    # Data apps (hosted user web apps registry)
    "data_apps_repo",
    # Agent profiles + agent-as-API (v100)
    "agents_repo",
    "llm_usage_repo",
    "idempotency_repo",
    # Agent webhooks + artifacts (v101, agent-api V1b)
    "agent_webhooks_repo",
    "agent_artifacts_repo",
    # Agent memories (v102, agent-api V1c)
    "agent_memories_repo",
    # Agent schedules (v119, agent schedules)
    "agent_schedules_repo",
    # Cross-domain semantic coverage (F4.1) — Postgres-only
    "resource_source_tags_repo",
    # Semantic-layer feedback queue (F4.5) — Postgres-only
    "semantic_feedback_repo",
    # Muted semantic-layer health checks (F4.3) — Postgres-only
    "semantic_health_mutes_repo",
]


def use_pg() -> bool:
    """Return True when the active backend is Postgres (side-car or cloud).

    Precedence:
      1. ``instance.yaml::database.backend`` — ONLY when EXPLICITLY declared
         (admin-controlled / migrator / first-boot seed).
      2. ``DATABASE_URL`` env var presence (12-factor convention).
      3. ``AGNES_DB_URL`` env var presence (legacy alias).
    """
    try:
        from src.db_state_machine import BackendState, is_backend_explicitly_declared, read_backend_state

        state, _ = read_backend_state()
        if state in (
            BackendState.SIDE_CAR,
            BackendState.CLOUD,
            BackendState.SIDE_CAR_IN_PROGRESS,
            BackendState.CLOUD_IN_PROGRESS,
        ):
            return True
        # Only treat DUCKDB as authoritative when the overlay EXPLICITLY
        # declares it (`database: {backend: duckdb}` present) — an overlay
        # that merely EXISTS (e.g. because an unrelated
        # /admin/server-config save touching only data_source/theme/etc.
        # created the file) with no `database` key at all is NOT a DuckDB
        # declaration. Checking `_OVERLAY_PATH.exists()` alone here used to
        # conflate the two and silently revert a DATABASE_URL-based Postgres
        # instance to an empty DuckDB backend on the next restart — see
        # CHANGELOG "PG-backend-revert" fix.
        if state == BackendState.DUCKDB and is_backend_explicitly_declared():
            return False
    except Exception:
        pass

    # Env-var fallback
    return bool(os.environ.get("DATABASE_URL") or os.environ.get("AGNES_DB_URL"))


def _pg_engine() -> Any:
    from src.db_pg import get_engine

    return get_engine()


# ---------------------------------------------------------------------------
# backend dispatch
# ---------------------------------------------------------------------------

#: Backend keys. New backends append here (and to the data structures below).
DUCKDB = "duckdb"
PG = "pg"


def _active_backend() -> str:
    """The backend key the current request/process resolves to."""
    return PG if use_pg() else DUCKDB


#: How to obtain the constructor argument for each backend. DuckDB repos take
#: a fresh cursor on the singleton system DB; PG repos take the singleton
#: engine. A new backend registers its connection/engine provider here.
#:
#: The providers resolve ``get_system_db`` / ``_pg_engine`` by NAME at call
#: time (the lambda looks them up in this module's globals when invoked), not
#: by capturing the function object at import. This preserves the pre-registry
#: behaviour where each factory called ``get_system_db()`` in its body — tests
#: that ``patch("src.repositories.get_system_db", ...)`` to redirect the system
#: DB must still take effect.
_ARG_PROVIDERS = {
    DUCKDB: lambda: get_system_db(),
    PG: lambda: _pg_engine(),
}

#: ``repo_key -> {backend: (module_path, class_name)}``. The single source of
#: truth for which class implements each repo on each backend. Lazy import by
#: dotted path keeps import cost identical to the old per-function local
#: imports and avoids import cycles (e.g. ``app.*`` repos).
_REGISTRY: dict[str, dict[str, tuple[str, str]]] = {
    # core user / RBAC
    "users": {
        DUCKDB: ("src.repositories.users", "UserRepository"),
        PG: ("src.repositories.users_pg", "UsersPgRepository"),
    },
    "user_groups": {
        DUCKDB: ("src.repositories.user_groups", "UserGroupsRepository"),
        PG: ("src.repositories.user_groups_pg", "UserGroupsPgRepository"),
    },
    "user_group_members": {
        DUCKDB: ("src.repositories.user_group_members", "UserGroupMembersRepository"),
        PG: ("src.repositories.user_group_members_pg", "UserGroupMembersPgRepository"),
    },
    "resource_grants": {
        DUCKDB: ("src.repositories.resource_grants", "ResourceGrantsRepository"),
        PG: ("src.repositories.resource_grants_pg", "ResourceGrantsPgRepository"),
    },
    "audit": {
        DUCKDB: ("src.repositories.audit", "AuditRepository"),
        PG: ("src.repositories.audit_pg", "AuditPgRepository"),
    },
    # ops triad
    "table_registry": {
        DUCKDB: ("src.repositories.table_registry", "TableRegistryRepository"),
        PG: ("src.repositories.table_registry_pg", "TableRegistryPgRepository"),
    },
    "sync_state": {
        DUCKDB: ("src.repositories.sync_state", "SyncStateRepository"),
        PG: ("src.repositories.sync_state_pg", "SyncStatePgRepository"),
    },
    # config / templates / tokens
    "metric": {
        DUCKDB: ("src.repositories.metrics", "MetricRepository"),
        PG: ("src.repositories.metrics_pg", "MetricPgRepository"),
    },
    "glossary": {
        DUCKDB: ("src.repositories.glossary", "GlossaryRepository"),
        PG: ("src.repositories.glossary_pg", "GlossaryPgRepository"),
    },
    "semantic_model": {
        DUCKDB: ("src.repositories.semantic_models", "SemanticModelsRepository"),
        PG: ("src.repositories.semantic_models_pg", "SemanticModelsPgRepository"),
    },
    "semantic_source": {
        DUCKDB: ("src.repositories.semantic_sources", "SemanticSourcesRepository"),
        PG: ("src.repositories.semantic_sources_pg", "SemanticSourcesPgRepository"),
    },
    "claude_md_template": {
        DUCKDB: ("src.repositories.claude_md_template", "ClaudeMdTemplateRepository"),
        PG: ("src.repositories.claude_md_template_pg", "ClaudeMdTemplatePgRepository"),
    },
    "welcome_template": {
        DUCKDB: ("src.repositories.welcome_template", "WelcomeTemplateRepository"),
        PG: ("src.repositories.welcome_template_pg", "WelcomeTemplatePgRepository"),
    },
    "news_template": {
        DUCKDB: ("src.repositories.news_template", "NewsTemplateRepository"),
        PG: ("src.repositories.news_template_pg", "NewsTemplatePgRepository"),
    },
    "access_token": {
        DUCKDB: ("src.repositories.access_tokens", "AccessTokenRepository"),
        PG: ("src.repositories.access_tokens_pg", "AccessTokenPgRepository"),
    },
    "profile": {
        DUCKDB: ("src.repositories.profiles", "ProfileRepository"),
        PG: ("src.repositories.profiles_pg", "ProfilePgRepository"),
    },
    # lookup / cache / settings
    "view_ownership": {
        DUCKDB: ("src.repositories.view_ownership", "ViewOwnershipRepository"),
        PG: ("src.repositories.view_ownership_pg", "ViewOwnershipPgRepository"),
    },
    "column_metadata": {
        DUCKDB: ("src.repositories.column_metadata", "ColumnMetadataRepository"),
        PG: ("src.repositories.column_metadata_pg", "ColumnMetadataPgRepository"),
    },
    "bq_metadata_cache": {
        DUCKDB: ("src.repositories.bq_metadata_cache", "BqMetadataCacheRepository"),
        PG: ("src.repositories.bq_metadata_cache_pg", "BqMetadataCachePgRepository"),
    },
    "sync_settings": {
        DUCKDB: ("src.repositories.sync_settings", "SyncSettingsRepository"),
        PG: ("src.repositories.sync_settings_pg", "SyncSettingsPgRepository"),
    },
    "notifications_telegram": {
        DUCKDB: ("src.repositories.notifications", "TelegramRepository"),
        PG: ("src.repositories.notifications_pg", "TelegramPgRepository"),
    },
    "notifications_pending_code": {
        DUCKDB: ("src.repositories.notifications", "PendingCodeRepository"),
        PG: ("src.repositories.notifications_pg", "PendingCodePgRepository"),
    },
    "notifications_script": {
        DUCKDB: ("src.repositories.notifications", "ScriptRepository"),
        PG: ("src.repositories.notifications_pg", "ScriptPgRepository"),
    },
    # telemetry
    "session_processor_state": {
        DUCKDB: ("src.repositories.session_processor_state", "SessionProcessorStateRepository"),
        PG: ("src.repositories.session_processor_state_pg", "SessionProcessorStatePgRepository"),
    },
    "observability_views": {
        DUCKDB: ("src.repositories.observability_views", "ObservabilityViewsRepository"),
        PG: ("src.repositories.observability_views_pg", "ObservabilityViewsPgRepository"),
    },
    "usage": {
        DUCKDB: ("src.repositories.usage", "UsageRepository"),
        PG: ("src.repositories.usage_pg", "UsagePgRepository"),
    },
    "reports": {
        DUCKDB: ("src.repositories.reports", "ReportsRepository"),
        PG: ("src.repositories.reports_pg", "ReportsPgRepository"),
    },
    # store / marketplace
    "marketplace_registry": {
        DUCKDB: ("src.repositories.marketplace_registry", "MarketplaceRegistryRepository"),
        PG: ("src.repositories.marketplace_registry_pg", "MarketplaceRegistryPgRepository"),
    },
    "marketplace_plugins": {
        DUCKDB: ("src.repositories.marketplace_plugins", "MarketplacePluginsRepository"),
        PG: ("src.repositories.marketplace_plugins_pg", "MarketplacePluginsPgRepository"),
    },
    "store_entities": {
        DUCKDB: ("src.repositories.store_entities", "StoreEntitiesRepository"),
        PG: ("src.repositories.store_entities_pg", "StoreEntitiesPgRepository"),
    },
    "store_entity_votes": {
        DUCKDB: ("src.repositories.store_entity_votes", "StoreEntityVotesRepository"),
        PG: ("src.repositories.store_entity_votes_pg", "StoreEntityVotesPgRepository"),
    },
    "user_store_installs": {
        DUCKDB: ("src.repositories.user_store_installs", "UserStoreInstallsRepository"),
        PG: ("src.repositories.user_store_installs_pg", "UserStoreInstallsPgRepository"),
    },
    "user_curated_subscriptions": {
        DUCKDB: ("src.repositories.user_curated_subscriptions", "UserCuratedSubscriptionsRepository"),
        PG: ("src.repositories.user_curated_subscriptions_pg", "UserCuratedSubscriptionsPgRepository"),
    },
    "store_submissions": {
        DUCKDB: ("src.repositories.store_submissions", "StoreSubmissionsRepository"),
        PG: ("src.repositories.store_submissions_pg", "StoreSubmissionsPgRepository"),
    },
    "store_lint": {
        DUCKDB: ("src.repositories.store_lint", "StoreLintRepository"),
        PG: ("src.repositories.store_lint_pg", "StoreLintPgRepository"),
    },
    # knowledge
    "knowledge": {
        DUCKDB: ("src.repositories.knowledge", "KnowledgeRepository"),
        PG: ("src.repositories.knowledge_pg", "KnowledgePgRepository"),
    },
    # data packages / memory / recipes / subscriptions
    "data_packages": {
        DUCKDB: ("src.repositories.data_packages", "DataPackagesRepository"),
        PG: ("src.repositories.data_packages_pg", "DataPackagesPgRepository"),
    },
    "memory_domains": {
        DUCKDB: ("src.repositories.memory_domains", "MemoryDomainsRepository"),
        PG: ("src.repositories.memory_domains_pg", "MemoryDomainsPgRepository"),
    },
    "memory_domain_suggestions": {
        DUCKDB: ("src.repositories.memory_domain_suggestions", "MemoryDomainSuggestionsRepository"),
        PG: ("src.repositories.memory_domain_suggestions_pg", "MemoryDomainSuggestionsPgRepository"),
    },
    "authoring_suggestions": {
        DUCKDB: ("src.repositories.authoring_suggestions", "AuthoringSuggestionsRepository"),
        PG: ("src.repositories.authoring_suggestions_pg", "AuthoringSuggestionsPgRepository"),
    },
    "memory_mining_consent": {
        DUCKDB: ("src.repositories.memory_mining_consent", "MemoryMiningConsentRepository"),
        PG: ("src.repositories.memory_mining_consent_pg", "MemoryMiningConsentPgRepository"),
    },
    "recipes": {
        DUCKDB: ("src.repositories.recipes", "RecipesRepository"),
        PG: ("src.repositories.recipes_pg", "RecipesPgRepository"),
    },
    "user_stack_subscriptions": {
        DUCKDB: ("src.repositories.user_stack_subscriptions", "UserStackSubscriptionsRepository"),
        PG: ("src.repositories.user_stack_subscriptions_pg", "UserStackSubscriptionsPgRepository"),
    },
    "user_journey": {
        DUCKDB: ("src.repositories.user_journey", "UserJourneyRepository"),
        PG: ("src.repositories.user_journey_pg", "UserJourneyPgRepository"),
    },
    # MCP / Cowork
    "mcp_sources": {
        DUCKDB: ("src.repositories.mcp_sources", "MCPSourceRepository"),
        PG: ("src.repositories.mcp_sources_pg", "MCPSourcePgRepository"),
    },
    "per_user_secrets": {
        DUCKDB: ("app.secrets_vault", "PerUserSecretsRepository"),
        PG: ("src.repositories.secrets_vault_pg", "PerUserSecretsPgRepository"),
    },
    "shared_secrets": {
        DUCKDB: ("app.secrets_vault", "SharedSecretsRepository"),
        PG: ("src.repositories.secrets_vault_pg", "SharedSecretsPgRepository"),
    },
    "system_secrets": {
        DUCKDB: ("app.secrets_vault", "SystemSecretsRepository"),
        PG: ("src.repositories.secrets_vault_pg", "SystemSecretsPgRepository"),
    },
    "tool_registry": {
        DUCKDB: ("src.repositories.tool_registry", "ToolRegistryRepository"),
        PG: ("src.repositories.tool_registry_pg", "ToolRegistryPgRepository"),
    },
    "setup_tokens": {
        DUCKDB: ("src.repositories.setup_tokens", "SetupTokenRepository"),
        PG: ("src.repositories.setup_tokens_pg", "SetupTokenPgRepository"),
    },
    # Outbound MCP OAuth data layer (v109)
    "mcp_source_oauth_clients": {
        DUCKDB: ("src.repositories.mcp_source_oauth_clients", "MCPSourceOAuthClientRepository"),
        PG: ("src.repositories.mcp_source_oauth_clients_pg", "MCPSourceOAuthClientPgRepository"),
    },
    "mcp_user_oauth_tokens": {
        DUCKDB: ("src.repositories.mcp_user_oauth_tokens", "MCPUserOAuthTokenRepository"),
        PG: ("src.repositories.mcp_user_oauth_tokens_pg", "MCPUserOAuthTokenPgRepository"),
    },
    "mcp_oauth_flows": {
        DUCKDB: ("src.repositories.mcp_oauth_flows", "MCPOAuthFlowRepository"),
        PG: ("src.repositories.mcp_oauth_flows_pg", "MCPOAuthFlowPgRepository"),
    },
    # source connections
    "source_connections": {
        DUCKDB: ("src.repositories.source_connections", "SourceConnectionsRepository"),
        PG: ("src.repositories.source_connections_pg", "SourceConnectionsPgRepository"),
    },
    "connection_secrets": {
        DUCKDB: ("app.secrets_vault", "ConnectionSecretsRepository"),
        PG: ("src.repositories.secrets_vault_pg", "ConnectionSecretsPgRepository"),
    },
    # cloud chat — the DuckDB side is a single ChatRepository covering all
    # chat tables; the PG side is split per table.
    "chat_session": {
        DUCKDB: ("app.chat.persistence", "ChatRepository"),
        PG: ("src.repositories.chat_sessions_pg", "ChatSessionPgRepository"),
    },
    "chat_message": {
        DUCKDB: ("app.chat.persistence", "ChatRepository"),
        PG: ("src.repositories.chat_messages_pg", "ChatMessagePgRepository"),
    },
    "user_workdirs": {
        DUCKDB: ("app.chat.persistence", "ChatRepository"),
        PG: ("src.repositories.user_workdirs_pg", "UserWorkdirPgRepository"),
    },
    "chat_session_participants": {
        DUCKDB: ("app.chat.persistence", "ChatRepository"),
        PG: ("src.repositories.chat_session_participants_pg", "ChatSessionParticipantPgRepository"),
    },
    "oauth_clients": {
        DUCKDB: ("src.repositories.oauth_clients", "OAuthClientsRepository"),
        PG: ("src.repositories.oauth_clients_pg", "OAuthClientsPgRepository"),
    },
    # collections
    "file_corpora": {
        DUCKDB: ("src.repositories.file_corpora", "FileCorporaRepository"),
        PG: ("src.repositories.file_corpora_pg", "FileCorporaPgRepository"),
    },
    "corpus_files": {
        DUCKDB: ("src.repositories.corpus_files", "CorpusFilesRepository"),
        PG: ("src.repositories.corpus_files_pg", "CorpusFilesPgRepository"),
    },
    "corpus_chunks": {
        DUCKDB: ("src.repositories.corpus_chunks", "CorpusChunksRepository"),
        PG: ("src.repositories.corpus_chunks_pg", "CorpusChunksPgRepository"),
    },
    # Crawler-anchor mapping (fact-graph-over-Collections §6 prerequisite) —
    # PG-only, A3 ratchet: no DuckDB backend.
    "corpus_file_sources": {
        PG: ("src.repositories.corpus_file_sources_pg", "CorpusFileSourcesPgRepository"),
    },
    # Fact graph over Collections (design doc §2 consequences) — PG-only,
    # A3 ratchet: no DuckDB backend.
    "facts": {
        PG: ("src.repositories.facts_pg", "FactsPgRepository"),
    },
    # Ontology builder draft state (fact-graph-over-Collections §13.2) —
    # PG-only, A3 ratchet: no DuckDB backend.
    "ontology_drafts": {
        PG: ("src.repositories.ontology_drafts_pg", "OntologyDraftsPgRepository"),
    },
    # Persisted ingest run reports (design doc §7.2/§13.2) — PG-only, A3
    # ratchet: no DuckDB backend. Separate repo/table from "facts" above so
    # a report-write failure is structurally never part of the ingest
    # transaction.
    "facts_ingest_runs": {
        PG: ("src.repositories.facts_ingest_runs_pg", "FactsIngestRunsPgRepository"),
    },
    # agent registry (v103)
    "agents": {
        DUCKDB: ("src.repositories.agents", "AgentsRepository"),
        PG: ("src.repositories.agents_pg", "AgentsPgRepository"),
    },
    # Maintained digests (K4, #799)
    "knowledge_digests": {
        DUCKDB: ("src.repositories.knowledge_digests", "KnowledgeDigestsRepository"),
        PG: ("src.repositories.knowledge_digests_pg", "KnowledgeDigestsPgRepository"),
    },
    # Chat sandbox secret broker tickets
    "ticket": {
        DUCKDB: ("src.repositories.ticket", "TicketRepository"),
        PG: ("src.repositories.ticket_pg", "TicketPgRepository"),
    },
    # Job queue (wave-2B worker runtime foundation)
    "jobs": {
        DUCKDB: ("src.repositories.jobs", "JobsRepository"),
        PG: ("src.repositories.jobs_pg", "JobsPgRepository"),
    },
    # Data apps (hosted user web apps registry)
    "data_apps": {
        DUCKDB: ("src.repositories.data_apps", "DataAppsRepository"),
        PG: ("src.repositories.data_apps_pg", "DataAppsPgRepository"),
    },
    # "agents" (agent registry v103 + agent profiles/agent-as-API v100 share
    # the same table/repo pair) is registered once above.
    "llm_usage": {
        DUCKDB: ("src.repositories.llm_usage", "LlmUsageRepository"),
        PG: ("src.repositories.llm_usage_pg", "LlmUsagePgRepository"),
    },
    "idempotency": {
        DUCKDB: ("src.repositories.idempotency", "IdempotencyRepository"),
        PG: ("src.repositories.idempotency_pg", "IdempotencyPgRepository"),
    },
    # Agent webhooks + artifacts (v101, agent-api V1b)
    "agent_webhooks": {
        DUCKDB: ("src.repositories.agent_webhooks", "AgentWebhooksRepository"),
        PG: ("src.repositories.agent_webhooks_pg", "AgentWebhooksPgRepository"),
    },
    "agent_artifacts": {
        DUCKDB: ("src.repositories.agent_artifacts", "AgentArtifactsRepository"),
        PG: ("src.repositories.agent_artifacts_pg", "AgentArtifactsPgRepository"),
    },
    # Agent memories (v102, agent-api V1c)
    "agent_memories": {
        DUCKDB: ("src.repositories.agent_memories", "AgentMemoriesRepository"),
        PG: ("src.repositories.agent_memories_pg", "AgentMemoriesPgRepository"),
    },
    # Agent schedules (v119, agent schedules)
    "agent_schedules": {
        DUCKDB: ("src.repositories.agent_schedules", "AgentSchedulesRepository"),
        PG: ("src.repositories.agent_schedules_pg", "AgentSchedulesPgRepository"),
    },
    # Cross-domain semantic coverage (F4.1) — POSTGRES-ONLY, the first entry
    # registered under the A3 PG-first ratchet. No DUCKDB key by design: the
    # DuckDB app-state backend is frozen, so resolving this on a DuckDB
    # instance raises RequiresPostgresBackend (translated to a typed 501 by
    # app/main.py) instead of silently reading a table that does not exist.
    "resource_source_tags": {
        PG: ("src.repositories.resource_source_tags_pg", "ResourceSourceTagsPgRepository"),
    },
    # Semantic-layer feedback queue (F4.5) — POSTGRES-ONLY, same reasoning as
    # the entry above.
    "semantic_feedback": {
        PG: ("src.repositories.semantic_feedback_pg", "SemanticFeedbackPgRepository"),
    },
    # Muted semantic-layer health checks (F4.3) — POSTGRES-ONLY, same
    # reasoning as the two entries above.
    "semantic_health_mutes": {
        PG: ("src.repositories.semantic_health_mutes_pg", "SemanticHealthMutesPgRepository"),
    },
}


def _build(key: str) -> Any:
    """Resolve + construct the repo for ``key`` on the active backend."""
    backend = _active_backend()
    entry = _REGISTRY.get(key, {})
    if backend not in entry:
        if backend == DUCKDB and PG in entry:
            # PG-only repo (post-A3): no DuckDB fallback exists by design —
            # raise the typed error instead of a bare KeyError so callers (and
            # the app-wide exception handler) can translate it into a clean
            # 4xx/501 instead of an unhandled crash.
            raise RequiresPostgresBackend(key)
        raise KeyError(f"no '{backend}' repository registered for '{key}' (known: {sorted(entry)})")
    module_path, class_name = entry[backend]
    klass = getattr(import_module(module_path), class_name)
    return klass(_ARG_PROVIDERS[backend]())


# ---------------------------------------------------------------------------
# public factory functions — thin delegates over the registry. Names + return
# contract are unchanged; callsites are unaffected.
# ---------------------------------------------------------------------------


# core user / RBAC
def users_repo() -> Any:
    return _build("users")


def user_groups_repo() -> Any:
    return _build("user_groups")


def user_group_members_repo() -> Any:
    return _build("user_group_members")


def resource_grants_repo() -> Any:
    return _build("resource_grants")


def audit_repo() -> Any:
    return _build("audit")


# ops triad
def table_registry_repo() -> Any:
    return _build("table_registry")


def sync_state_repo() -> Any:
    return _build("sync_state")


# config / templates / tokens
def metric_repo() -> Any:
    return _build("metric")


def glossary_repo() -> Any:
    return _build("glossary")


def semantic_model_repo() -> Any:
    return _build("semantic_model")


def semantic_source_repo() -> Any:
    return _build("semantic_source")


def claude_md_template_repo() -> Any:
    return _build("claude_md_template")


def welcome_template_repo() -> Any:
    return _build("welcome_template")


def news_template_repo() -> Any:
    return _build("news_template")


def access_token_repo() -> Any:
    return _build("access_token")


def profile_repo() -> Any:
    return _build("profile")


# lookup / cache / settings
def view_ownership_repo() -> Any:
    return _build("view_ownership")


def column_metadata_repo() -> Any:
    return _build("column_metadata")


def bq_metadata_cache_repo() -> Any:
    return _build("bq_metadata_cache")


def sync_settings_repo() -> Any:
    return _build("sync_settings")


def notifications_telegram_repo() -> Any:
    return _build("notifications_telegram")


def notifications_pending_code_repo() -> Any:
    return _build("notifications_pending_code")


def notifications_script_repo() -> Any:
    return _build("notifications_script")


# telemetry
def session_processor_state_repo() -> Any:
    return _build("session_processor_state")


def observability_views_repo() -> Any:
    return _build("observability_views")


def usage_repo() -> Any:
    return _build("usage")


def reports_repo() -> Any:
    return _build("reports")


# store / marketplace
def marketplace_registry_repo() -> Any:
    return _build("marketplace_registry")


def marketplace_plugins_repo() -> Any:
    return _build("marketplace_plugins")


def store_entities_repo() -> Any:
    return _build("store_entities")


def store_entity_votes_repo() -> Any:
    return _build("store_entity_votes")


def user_store_installs_repo() -> Any:
    return _build("user_store_installs")


def user_curated_subscriptions_repo() -> Any:
    return _build("user_curated_subscriptions")


def store_submissions_repo() -> Any:
    return _build("store_submissions")


def store_lint_repo() -> Any:
    return _build("store_lint")


# knowledge
def knowledge_repo() -> Any:
    return _build("knowledge")


# data packages / memory / recipes / subscriptions
def data_packages_repo() -> Any:
    return _build("data_packages")


def memory_domains_repo() -> Any:
    return _build("memory_domains")


def memory_domain_suggestions_repo() -> Any:
    return _build("memory_domain_suggestions")


def authoring_suggestions_repo() -> Any:
    return _build("authoring_suggestions")


def memory_mining_consent_repo() -> Any:
    return _build("memory_mining_consent")


def recipes_repo() -> Any:
    return _build("recipes")


def user_stack_subscriptions_repo() -> Any:
    return _build("user_stack_subscriptions")


def user_journey_repo() -> Any:
    return _build("user_journey")


# MCP / Cowork
def mcp_sources_repo() -> Any:
    return _build("mcp_sources")


def per_user_secrets_repo() -> Any:
    return _build("per_user_secrets")


def shared_secrets_repo() -> Any:
    return _build("shared_secrets")


def source_connections_repo() -> Any:
    return _build("source_connections")


def connection_secrets_repo() -> Any:
    return _build("connection_secrets")


def system_secrets_repo() -> Any:
    return _build("system_secrets")


def tool_registry_repo() -> Any:
    return _build("tool_registry")


def setup_tokens_repo() -> Any:
    return _build("setup_tokens")


# Outbound MCP OAuth data layer (v109)
def mcp_source_oauth_clients_repo() -> Any:
    return _build("mcp_source_oauth_clients")


def mcp_user_oauth_tokens_repo() -> Any:
    return _build("mcp_user_oauth_tokens")


def mcp_oauth_flows_repo() -> Any:
    return _build("mcp_oauth_flows")


# cloud chat
def chat_session_repo() -> Any:
    return _build("chat_session")


def chat_message_repo() -> Any:
    return _build("chat_message")


def user_workdirs_repo() -> Any:
    return _build("user_workdirs")


def chat_session_participants_repo() -> Any:
    return _build("chat_session_participants")


def oauth_clients_repo() -> Any:
    return _build("oauth_clients")


# collections
def file_corpora_repo() -> Any:
    return _build("file_corpora")


def corpus_files_repo() -> Any:
    return _build("corpus_files")


def agents_repo() -> Any:
    """Agent registry (v103) — server-side agent definitions; also backs
    agent profiles + agent-as-API (v100), which share the same table/repo."""
    return _build("agents")


def corpus_chunks_repo() -> Any:
    return _build("corpus_chunks")


def corpus_file_sources_repo() -> Any:
    """Crawler-anchor mapping (fact-graph-over-Collections §6). PG-only —
    raises ``RequiresPostgresBackend`` on a DuckDB-backed instance."""
    return _build("corpus_file_sources")


def facts_repo() -> Any:
    """Fact graph over Collections (facts/fact_aliases/edges/claims/
    corrections — design doc §2 consequences). PG-only — raises
    ``RequiresPostgresBackend`` on a DuckDB-backed instance."""
    return _build("facts")


def ontology_drafts_repo() -> Any:
    """Ontology builder draft state (design doc §13.2, "Ontology builder").
    PG-only — raises ``RequiresPostgresBackend`` on a DuckDB-backed
    instance."""
    return _build("ontology_drafts")


def facts_ingest_runs_repo() -> Any:
    """Persisted run reports for ``POST /api/facts/ingest`` (design doc
    §7.2/§13.2 source card). PG-only — raises ``RequiresPostgresBackend``
    on a DuckDB-backed instance."""
    return _build("facts_ingest_runs")


# Maintained digests (K4, #799)
def knowledge_digests_repo() -> Any:
    return _build("knowledge_digests")


# chat sandbox secret broker tickets
def ticket_repo() -> Any:
    return _build("ticket")


# job queue (wave-2B worker runtime foundation)
def jobs_repo() -> Any:
    return _build("jobs")


# data apps (hosted user web apps registry)
def data_apps_repo() -> Any:
    return _build("data_apps")


def llm_usage_repo() -> Any:
    return _build("llm_usage")


def idempotency_repo() -> Any:
    return _build("idempotency")


# Agent webhooks + artifacts (v101, agent-api V1b)
def agent_webhooks_repo() -> Any:
    return _build("agent_webhooks")


def agent_artifacts_repo() -> Any:
    return _build("agent_artifacts")


# Agent memories (v102, agent-api V1c)
def agent_memories_repo() -> Any:
    return _build("agent_memories")


# Agent schedules (v119, agent schedules)
def agent_schedules_repo() -> Any:
    return _build("agent_schedules")


# Cross-domain semantic coverage (F4.1) — POSTGRES-ONLY. Raises
# RequiresPostgresBackend on a DuckDB-backed instance; let it propagate.
def resource_source_tags_repo() -> Any:
    return _build("resource_source_tags")


# Semantic-layer feedback queue (F4.5) — POSTGRES-ONLY. Raises
# RequiresPostgresBackend on a DuckDB-backed instance; let it propagate.
def semantic_feedback_repo() -> Any:
    return _build("semantic_feedback")


# Muted semantic-layer health checks (F4.3) — POSTGRES-ONLY. Raises
# RequiresPostgresBackend on a DuckDB-backed instance; let it propagate.
def semantic_health_mutes_repo() -> Any:
    return _build("semantic_health_mutes")
