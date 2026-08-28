"""SQLAlchemy models for Agnes's Postgres-backed app state.

Importing this package registers every model on ``src.db_pg.Base.metadata``;
Alembic's autogenerate reads that metadata to detect drift.

Add a new model by creating ``src/models/<name>.py`` that imports ``Base``
from ``src.db_pg`` and adds it to the ``__all__`` re-export below.
"""

from __future__ import annotations

from src.models.agents import (
    Agent,
    AgentArtifact,
    AgentMemory,
    AgentSchedule,
    AgentScope,
    AgentScopeSnapshot,
    AgentWebhook,
    IdempotencyKey,
    LlmUsage,
)
from src.models.audit import AuditLog
from src.models.chat import ChatMessage, ChatSession, UserWorkdir
from src.models.chat_broker_tickets import ChatBrokerTicket
from src.models.collections import CorpusChunk, CorpusFile, CorpusFileSource, FileCorpus
from src.models.config import GlossaryTerm, InstanceTemplate, MetricDefinition, PersonalAccessToken
from src.models.connections import ConnectionSecret, SourceConnection
from src.models.data_apps import DataApp
from src.models.data_packages import DataPackage, DataPackageTable, DataPackageTool
from src.models.facts import Claim, Correction, Edge, Fact, FactAlias
from src.models.knowledge import (
    KnowledgeContradiction,
    KnowledgeItem,
    KnowledgeItemDomain,
    KnowledgeItemRelation,
    KnowledgeItemUserDismissed,
    KnowledgeVote,
    MemoryDomain,
    MemoryDomainSuggestion,
    VerificationEvidence,
)
from src.models.jobs import Job
from src.models.knowledge_digests import KnowledgeDigest
from src.models.lookup import (
    BqMetadataCache,
    ColumnMetadata,
    UserSyncSettings,
    ViewOwnership,
)
from src.models.misc import (
    NewsTemplate,
    PendingCode,
    ScriptRegistry,
    TableProfile,
    TelegramLink,
)
from src.models.ops import SyncHistory, SyncState, TableRegistry
from src.models.recipes import Recipe
from src.models.store import (
    MarketplacePlugin,
    MarketplaceRegistry,
    StoreEntity,
    StoreEntityVote,
    StoreLintDismissal,
    StoreLintEntityState,
    StoreLintFinding,
    StoreLintRun,
    StoreSubmission,
    UserPluginOptout,
    UserStackSubscription,
    UserStoreInstall,
)
from src.models.user_journey import UserJourneyState
from src.models.telemetry import (
    SessionProcessorState,
    UsageEvent,
    UsageMarketplaceItemDaily,
    UsageMarketplaceItemWindow,
    UsageSessionSummary,
    UsageToolDaily,
    UserObservabilityView,
)
from src.models.mcp import (
    MCPOAuthFlow,
    MCPSecret,
    MCPSource,
    MCPSourceOAuthClient,
    MCPUserOAuthToken,
    MCPUserSecret,
    SetupToken,
    ToolGrant,
    ToolRegistry,
)
from src.models.rbac import (
    ResourceGrant,
    User,
    UserGroup,
    UserGroupMember,
)
from src.models.oauth import OAuthAccessToken, OAuthAuthCode, OAuthClient, OAuthRefreshToken
from src.models.ontology_drafts import OntologyDraft
from src.models.semantic import DataPackageSemanticModel, SemanticModel, SemanticSource
from src.models.vault import SystemSecret


__all__ = [
    "Agent",
    "AgentArtifact",
    "AgentMemory",
    "AgentSchedule",
    "AgentScope",
    "AgentScopeSnapshot",
    "AgentWebhook",
    "AuditLog",
    "BqMetadataCache",
    "ChatBrokerTicket",
    "ChatMessage",
    "ChatSession",
    "Claim",
    "ColumnMetadata",
    "ConnectionSecret",
    "Correction",
    "CorpusChunk",
    "CorpusFile",
    "CorpusFileSource",
    "FileCorpus",
    "DataApp",
    "DataPackage",
    "GlossaryTerm",
    "DataPackageSemanticModel",
    "DataPackageTable",
    "DataPackageTool",
    "Edge",
    "Fact",
    "FactAlias",
    "IdempotencyKey",
    "InstanceTemplate",
    "Job",
    "KnowledgeContradiction",
    "KnowledgeDigest",
    "KnowledgeItem",
    "KnowledgeItemDomain",
    "KnowledgeItemRelation",
    "KnowledgeItemUserDismissed",
    "KnowledgeVote",
    "LlmUsage",
    "MCPOAuthFlow",
    "MCPSecret",
    "MCPSource",
    "MCPSourceOAuthClient",
    "MCPUserOAuthToken",
    "MCPUserSecret",
    "MarketplacePlugin",
    "MarketplaceRegistry",
    "MemoryDomain",
    "MemoryDomainSuggestion",
    "MetricDefinition",
    "NewsTemplate",
    "PendingCode",
    "PersonalAccessToken",
    "Recipe",
    "ResourceGrant",
    "SetupToken",
    "ScriptRegistry",
    "SemanticModel",
    "SemanticSource",
    "SessionProcessorState",
    "SourceConnection",
    "StoreEntity",
    "StoreEntityVote",
    "StoreLintDismissal",
    "StoreLintEntityState",
    "StoreLintFinding",
    "StoreLintRun",
    "StoreSubmission",
    "SyncHistory",
    "SyncState",
    "SystemSecret",
    "TableProfile",
    "TableRegistry",
    "TelegramLink",
    "VerificationEvidence",
    "UsageEvent",
    "UsageMarketplaceItemDaily",
    "UsageMarketplaceItemWindow",
    "UsageSessionSummary",
    "UsageToolDaily",
    "ToolGrant",
    "ToolRegistry",
    "User",
    "UserGroup",
    "UserGroupMember",
    "UserObservabilityView",
    "UserPluginOptout",
    "UserStackSubscription",
    "UserStoreInstall",
    "UserJourneyState",
    "UserSyncSettings",
    "UserWorkdir",
    "ViewOwnership",
    "OAuthAccessToken",
    "OAuthAuthCode",
    "OAuthClient",
    "OAuthRefreshToken",
    "OntologyDraft",
]
