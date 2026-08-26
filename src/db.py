"""DuckDB connection management and schema versioning.

Provides get_system_db() for the system state database
and get_analytics_db() for the analytics database with parquet views.
"""

import logging
import math
import os
import re
import shutil
import time
from pathlib import Path

import duckdb

from connectors.bigquery.auth import get_metadata_token, BQMetadataAuthError
from src.analytics_backend import analytics_backend

logger = logging.getLogger(__name__)

# Dev-only DuckDB query capture. When DEBUG=1 in the environment, every
# connection returned from get_system_db / get_analytics_db /
# get_analytics_db_readonly is wrapped with an InstrumentedConnection that
# records `.execute()` calls into a contextvar buffer the debug toolbar reads
# at response time. In prod (DEBUG unset), `_maybe_instrument` is a no-op pass-
# through, so the wrapper is never even constructed on the hot path.


def _maybe_instrument(con, db_tag: str):
    """Wrap a duckdb connection with InstrumentedConnection when DEBUG=1, else return as-is.

    DEBUG is read on each call so tests can toggle it via monkeypatch.setenv
    without reloading this module. Connection creation is not a hot path.
    """
    if os.environ.get("DEBUG", "").lower() not in ("1", "true", "yes"):
        return con
    from app.debug.duckdb_panel import InstrumentedConnection

    return InstrumentedConnection(con, db_tag)


# Re-export the lightweight helper. The implementation lives in
# `src.duckdb_conn` so connectors / CLI / scripts can import it without
# pulling the heavy `connectors.bigquery.auth` dep that this module
# imports above.
from src.duckdb_conn import _open_duckdb  # noqa: F401, E402  (re-export)


_SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")

# Merge of main's ladder (→109: outbound MCP OAuth sources tables) with the
# paper-theme branch's schema additions restacked on top: 110
# file_corpora.origin, 111 store_entities trust columns, 112 agents builder
# superset columns, 113 chat_sessions.pinned_at, 114
# data_packages.publisher_kind (the stored trust axis that retired the
# render-time-derived `curated` badge), 115 one-time reclassification of
# pre-existing governance-created agents from `draft` to `ready` (see
# `_v114_to_v115`), 116 table_registry access-policy columns —
# access_policy_sql/note/updated_at/updated_by + policy_mapping, the storage
# for one SQL row/column-filtering policy per table (see `_v115_to_v116`),
# 117 semantic_models / semantic_sources / data_package_semantic_models —
# the canonical Ossie semantic-layer document store (see `_v116_to_v117`),
# 118 adds user_journey_state.agent_created (see `_v117_to_v118`),
# 119 adds tool_registry.projection_map — the admin's choice of which
# materialized columns carry a linked app's id / url / name, replacing a
# hardcoded alias list that only knew one upstream's column names (see
# `_v118_to_v119`), 120 adds agent_schedules — scheduled runs for agent
# profiles (see `_v119_to_v120`),
# 121 adds tool_grants.allow_mutating — per-group opt-in that lets a
# non-admin caller (including an agent riding its owner's groups) invoke a
# mutating passthrough tool, replacing the admin-or-bust mutating gate (see
# `_v120_to_v121`),
# 122 backfills enforced scope onto pre-existing `/agents` builder agents —
# their knowledge/plugins declaration becomes `agent_scope` rows and the four
# `*_mode` columns flip off the all-'all' passthrough shape, so the narrowing
# the builder UI showed is the narrowing the runtime applies (see
# `_v121_to_v122`),
# 123 adds chat_messages.parts — the assistant turn's ordered
# [{type:'text'|'tool', …}] shape, so prose and tool calls keep their
# interleaving across a reload and a replayed tool card can show its real
# outcome instead of only a name (see `_v122_to_v123`),
# 124 (remediation B1) backfills name-keyed `sync_state.table_id` /
# `sync_history.table_id` rows to the matching `table_registry.id`, data-only
# — writers now resolve the id themselves (see `src.sync_state_key`); this
# step only rewrites what an earlier binary already wrote (see
# `_v123_to_v124`).
SCHEMA_VERSION = 124

#: A3 PG-first ratchet (see CLAUDE.md -> "Dual-backend discipline"): the
#: DuckDB app-state migration ladder is frozen at this version. New schema
#: work lands as an Alembic-only revision (``migrations/versions/``), with no
#: matching ``_vN_to_v(N+1)`` step in this file — see ``docs/migrations.md``
#: -> "Adding a PG-only feature". ``SCHEMA_VERSION`` must never move past
#: this constant; ``tests/test_db_schema_version_frozen.py`` is the gate.
#: A4 deletes this whole ladder (and this constant) once the fleet has
#: migrated off the DuckDB app-state backend.
FROZEN_DUCKDB_SCHEMA_VERSION = 124

# v96: data_apps registry (hosted user web apps). Extracted as a shared
# module-level constant so the fresh-install DDL (appended to
# _SYSTEM_SCHEMA below) and the _v95_to_v96 upgrade step execute the
# identical CREATE TABLE — see _v95_to_v96 for the upgrade-path wiring.
_DATA_APPS_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS data_apps (
    id              VARCHAR PRIMARY KEY,
    slug            VARCHAR UNIQUE NOT NULL,
    name            VARCHAR NOT NULL,
    description     TEXT DEFAULT '',
    owner_user_id   VARCHAR NOT NULL,
    repo_mode       VARCHAR NOT NULL DEFAULT 'internal',
    repo_url        VARCHAR DEFAULT '',
    repo_branch     VARCHAR DEFAULT 'main',
    deployed_sha    VARCHAR DEFAULT '',
    runtime_tag     VARCHAR DEFAULT '',
    state           VARCHAR NOT NULL DEFAULT 'created',
    state_detail    TEXT DEFAULT '',
    secrets_enc     TEXT DEFAULT '',
    env             TEXT DEFAULT '{}',
    cpu_limit       VARCHAR DEFAULT '',
    mem_limit       VARCHAR DEFAULT '',
    idle_timeout_s  INTEGER DEFAULT 1800,
    sleep_mode      VARCHAR DEFAULT 'recreate',
    service_token_id VARCHAR DEFAULT '',
    parent_app_id   VARCHAR DEFAULT '',
    is_draft        BOOLEAN DEFAULT FALSE,
    draft_branch    VARCHAR DEFAULT '',
    -- Linked (externally-hosted) apps (v108): repo_mode='linked' rows carry an
    -- external deployment URL instead of a git repo/runtime; source_ref is the
    -- ingest provenance "<connection_id>:<external_app_id>"; managed=TRUE marks a
    -- sync-owned row whose description the admin may override without the sync
    -- clobbering it.
    external_url    VARCHAR,
    source_ref      VARCHAR,
    managed         BOOLEAN NOT NULL DEFAULT FALSE,
    description_override TEXT,
    last_request_at TIMESTAMP,
    last_deploy_at  TIMESTAMP,
    created_at      TIMESTAMP DEFAULT current_timestamp,
    updated_at      TIMESTAMP DEFAULT current_timestamp
);
"""

# v101: agent profiles + agent-as-API foundation (spec
# docs/superpowers/specs/2026-07-21-agent-profiles-and-agent-api-design.md).
#
# The paper-theme agent-builder's fields (role/tone/greeting/knowledge/plugins/
# surfaces/status) were merged as a SUPERSET onto this, main's canonical table
# (added on upgrade by _v111_to_v112); the builder maps created_by→owner_user_id
# and instructions→system_prompt, so those reuse the columns above rather than
# duplicating them.
#
# Shared constant (same reasoning as _DATA_APPS_CREATE_SQL) so the fresh-install
# DDL spliced into _SYSTEM_SCHEMA below and the rebuild in
# _heal_legacy_agents_table execute the identical CREATE TABLE — a heal that
# hand-copied the shape would drift the moment a column is added here.
_AGENTS_CREATE_SQL = """
-- No secondary indexes anywhere here — see the _v94_to_v95 ART-index
-- incident note; chat_sessions.agent_id especially must stay unindexed.
CREATE TABLE IF NOT EXISTS agents (
    id                   VARCHAR PRIMARY KEY,
    owner_user_id        VARCHAR NOT NULL,
    name                 VARCHAR NOT NULL,
    slug                 VARCHAR NOT NULL,
    description          TEXT,
    system_prompt        TEXT,
    model                VARCHAR,
    token_budget_monthly BIGINT,
    plugins_mode         VARCHAR NOT NULL DEFAULT 'all',
    connections_mode     VARCHAR NOT NULL DEFAULT 'all',
    tables_mode          VARCHAR NOT NULL DEFAULT 'all',
    memory_mode          VARCHAR NOT NULL DEFAULT 'all',
    memory_write_mode    VARCHAR NOT NULL DEFAULT 'propose',
    is_default           BOOLEAN NOT NULL DEFAULT FALSE,
    -- v110: paper-theme agent-builder superset. role/tone/greeting are authored
    -- profile fields; knowledge/plugins/surfaces are opaque id-list JSON the
    -- builder owns (never joined in SQL); status is the builder's draft|ready
    -- lifecycle.
    role                 VARCHAR,
    tone                 VARCHAR DEFAULT 'concise',
    greeting             TEXT,
    knowledge            TEXT DEFAULT '[]',
    plugins              TEXT DEFAULT '[]',
    surfaces             TEXT DEFAULT '{}',
    status               VARCHAR DEFAULT 'draft',
    created_at           TIMESTAMP DEFAULT current_timestamp,
    updated_at           TIMESTAMP DEFAULT current_timestamp,
    deleted_at           TIMESTAMP,
    UNIQUE (owner_user_id, slug)
);
"""

_SYSTEM_SCHEMA = (
    """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    applied_at TIMESTAMP DEFAULT current_timestamp
);

-- v13: authorization is now via user_groups + user_group_members + resource_grants.
-- v19: legacy `role` column physically dropped via _v18_to_v19_finalize table
-- rebuild (was a NULL artifact since v13 — ignored at runtime, but the column
-- shape persisted in DBs upgraded through v8→v18).
CREATE TABLE IF NOT EXISTS users (
    id VARCHAR PRIMARY KEY,
    email VARCHAR UNIQUE NOT NULL,
    name VARCHAR,
    password_hash VARCHAR,
    setup_token VARCHAR,
    setup_token_created TIMESTAMP,
    reset_token VARCHAR,
    reset_token_created TIMESTAMP,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    deactivated_at TIMESTAMP,
    deactivated_by VARCHAR,
    created_at TIMESTAMP DEFAULT current_timestamp,
    updated_at TIMESTAMP,
    -- v26: onboarded flag flipped by `agnes init` success path or by the
    -- self-mark "I've already set up Agnes locally" button on /home.
    -- Default FALSE; explicit signal required to flip (no PAT-heuristic
    -- auto-flip per the brainstorm decision §D).
    onboarded BOOLEAN NOT NULL DEFAULT FALSE,
    -- v44: per-user pull timestamp. Bumped on every GET /api/sync/manifest
    -- so `agnes pull` (and the SessionStart hook that wraps it) imprints
    -- the user's last sync time. Powers the /home status frame's "Last
    -- sync" card.
    last_pull_at TIMESTAMP,
    -- v71: Slack identity binding. NULL until the analyst redeems a
    -- /agnes verification code; maps a Slack user_id to this account so the
    -- Slack bot can resolve who is talking. Formalized in the schema (was a
    -- lazy ALTER in services/slack_bot/binding.py) so it lives in the active
    -- state backend — the binding broke on Postgres when it only existed in
    -- the DuckDB system file.
    slack_user_id VARCHAR,
    -- v77: forces a password change on first sign-in for accounts whose
    -- password was set BY SOMEONE ELSE — the seed admin created from
    -- SEED_ADMIN_PASSWORD (emailed in plaintext) and admin-set passwords.
    -- Cleared when the user sets their own password via reset/setup confirm.
    -- Irrelevant to SSO/magic-link accounts (they have no password).
    must_change_password BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS sync_state (
    table_id VARCHAR PRIMARY KEY,
    last_sync TIMESTAMP,
    rows BIGINT,
    file_size_bytes BIGINT,
    uncompressed_size_bytes BIGINT,
    columns INTEGER,
    hash VARCHAR,
    parts JSON,
    status VARCHAR DEFAULT 'ok',
    error TEXT
);

-- v10: view-name collision detection across connectors. The orchestrator
-- writes views into the master analytics.duckdb under a flat namespace; two
-- connectors with the same `_meta.table_name` would otherwise silently
-- overwrite each other (last-write-wins). This table records the FIRST
-- source to register a given view name; subsequent attempts from a different
-- source are refused with a `name_collision` log line until the operator
-- renames one side. Issue #81 Group C.
CREATE TABLE IF NOT EXISTS view_ownership (
    view_name     VARCHAR PRIMARY KEY,
    source_name   VARCHAR NOT NULL,
    registered_at TIMESTAMP NOT NULL DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS sync_history (
    id VARCHAR PRIMARY KEY,
    table_id VARCHAR NOT NULL,
    synced_at TIMESTAMP NOT NULL,
    rows BIGINT,
    duration_ms INTEGER,
    status VARCHAR,
    error TEXT
);

CREATE TABLE IF NOT EXISTS user_sync_settings (
    user_id VARCHAR NOT NULL,
    dataset VARCHAR NOT NULL,
    enabled BOOLEAN DEFAULT false,
    table_mode VARCHAR DEFAULT 'all',
    tables JSON,
    updated_at TIMESTAMP,
    PRIMARY KEY (user_id, dataset)
);

CREATE TABLE IF NOT EXISTS knowledge_items (
    id VARCHAR PRIMARY KEY,
    title VARCHAR NOT NULL,
    content TEXT,
    category VARCHAR,
    tags JSON,
    status VARCHAR DEFAULT 'pending',
    contributors JSON,
    source_user VARCHAR,
    audience VARCHAR,
    -- v15: context-engineering columns. Confidence is derived from verification
    -- evidence (see services/corporate_memory/confidence.py); valid_from/until
    -- carry the time-bounded validity for fact items; supersedes points to a
    -- prior id this row replaces; sensitivity gates which audiences can see
    -- the row; is_personal scopes the row to the contributor only (excluded
    -- from /bundle, listed only when the contributor is the caller).
    confidence DOUBLE,
    -- v49: the scalar ``domain`` column was replaced by the
    -- ``knowledge_item_domains`` M:N junction (see ``memory_domains``
    -- and the v49 backfill in ``_v51_to_v52``).
    entities JSON,
    source_type VARCHAR DEFAULT 'claude_local_md',
    source_ref VARCHAR,
    valid_from TIMESTAMP,
    valid_until TIMESTAMP,
    supersedes VARCHAR,
    sensitivity VARCHAR DEFAULT 'internal',
    is_personal BOOLEAN DEFAULT FALSE,
    -- v49: governance Required tier, split out of the v15-era
    -- status='mandatory' overload. status now tracks lifecycle only
    -- (pending/approved/rejected); is_required is the orthogonal
    -- "must appear in the bundle, cannot be dismissed" flag.
    is_required BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT current_timestamp,
    updated_at TIMESTAMP
);

-- v15: contradiction tracking — surfaced when two `mandatory`/`approved` items
-- assert conflicting facts on overlapping audiences. Detected by the
-- contradiction service; resolved by a curator (see app/api/memory.py).
CREATE TABLE IF NOT EXISTS knowledge_contradictions (
    id VARCHAR PRIMARY KEY,
    item_a_id VARCHAR NOT NULL,
    item_b_id VARCHAR NOT NULL,
    explanation TEXT,
    severity VARCHAR,
    suggested_resolution TEXT,
    resolved BOOLEAN DEFAULT FALSE,
    resolved_by VARCHAR,
    resolved_at TIMESTAMP,
    resolution VARCHAR,
    detected_at TIMESTAMP DEFAULT current_timestamp
);

-- v17: duplicate-candidate hints — one row per (item_a, item_b, relation_type)
-- pair where the verification detector identified two same-domain knowledge
-- items sharing >= MIN_ENTITY_OVERLAP entities (see issue #62 + ADR Decision 1).
-- The repository canonicalizes (a, b) to (min, max) so each unordered pair maps
-- to one row regardless of insertion order. ``score`` carries the Jaccard ratio
-- (|A ∩ B| / |A ∪ B|) at detection time. ``resolved`` flips to TRUE when an
-- admin marks the pair via /api/memory/admin/duplicate-candidates/resolve.
CREATE TABLE IF NOT EXISTS knowledge_item_relations (
    item_a_id VARCHAR NOT NULL,
    item_b_id VARCHAR NOT NULL,
    relation_type VARCHAR NOT NULL,
    score DOUBLE,
    resolved BOOLEAN DEFAULT FALSE,
    resolved_by VARCHAR,
    resolved_at TIMESTAMP,
    resolution VARCHAR,
    created_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (item_a_id, item_b_id, relation_type)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_item_relations_resolved
    ON knowledge_item_relations(resolved);

-- v15→v29: state tracking for any session-pipeline processor (verification,
-- usage, future extractors). Composite PK (processor_name, session_file) so
-- each processor has its own independent processed-set keyed by jsonl path.
-- file_hash invalidates state when a session jsonl grows (live append from
-- an active Claude Code session) so processors reprocess the new content.
CREATE TABLE IF NOT EXISTS session_processor_state (
    processor_name VARCHAR NOT NULL,
    session_file VARCHAR NOT NULL,
    username VARCHAR NOT NULL,
    processed_at TIMESTAMP DEFAULT current_timestamp,
    items_extracted INTEGER DEFAULT 0,
    file_hash VARCHAR,
    PRIMARY KEY (processor_name, session_file)
);

-- v16: per-detection evidence rows — one knowledge_item can accumulate
-- multiple evidence rows over time (each new analyst confirmation adds one).
-- Persisting user_quote + detection_type per row is what enables future
-- Bayesian re-calibration and "additional verifiers" boost computation.
CREATE TABLE IF NOT EXISTS verification_evidence (
    id VARCHAR PRIMARY KEY,
    item_id VARCHAR NOT NULL,
    source_user VARCHAR,
    source_ref VARCHAR,
    detection_type VARCHAR,
    user_quote TEXT,
    created_at TIMESTAMP DEFAULT current_timestamp
);
CREATE INDEX IF NOT EXISTS idx_verification_evidence_item ON verification_evidence(item_id);

CREATE TABLE IF NOT EXISTS knowledge_votes (
    item_id VARCHAR NOT NULL,
    user_id VARCHAR NOT NULL,
    vote INTEGER,
    voted_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (item_id, user_id)
);

-- v76: per-user thumbs up/down ratings on store / marketplace entities.
-- Mirrors knowledge_votes (one vote per (entity, user); ON CONFLICT upsert
-- flips the value; a missing row means "no vote"). Issue #398.
CREATE TABLE IF NOT EXISTS store_entity_votes (
    entity_id VARCHAR NOT NULL,
    user_id VARCHAR NOT NULL,
    vote INTEGER,
    voted_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (entity_id, user_id)
);

-- v46: per-user opt-out for knowledge items. A row here means the user has
-- dismissed an item from their personal AI bundle and (optionally) their
-- listing — but mandatory items can never be dismissed; the governance
-- hard rule is enforced API-side and reinforced by the SQL filter via
-- ``status != 'mandatory'`` in the EXISTS subquery in list_items/search/
-- count_items/bundle. Idempotent inserts (ON CONFLICT do nothing).
CREATE TABLE IF NOT EXISTS knowledge_item_user_dismissed (
    user_id VARCHAR NOT NULL,
    item_id VARCHAR NOT NULL,
    dismissed_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (user_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_item_user_dismissed_user
    ON knowledge_item_user_dismissed(user_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id VARCHAR PRIMARY KEY,
    timestamp TIMESTAMP NOT NULL DEFAULT current_timestamp,
    user_id VARCHAR,
    action VARCHAR NOT NULL,
    resource VARCHAR,
    params JSON,
    result VARCHAR,
    duration_ms INTEGER,
    params_before JSON,
    client_ip VARCHAR,
    client_kind VARCHAR,
    correlation_id VARCHAR
);

CREATE TABLE IF NOT EXISTS telegram_links (
    user_id VARCHAR PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    linked_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS pending_codes (
    code VARCHAR PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    created_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS script_registry (
    id VARCHAR PRIMARY KEY,
    name VARCHAR NOT NULL,
    owner VARCHAR,
    schedule VARCHAR,
    source TEXT NOT NULL,
    deployed_at TIMESTAMP DEFAULT current_timestamp,
    last_run TIMESTAMP,
    last_status VARCHAR
);

-- v19: `is_public` column removed. The bypass shortcut had no API/UI/CLI
-- surface to set it (only direct DB UPDATE worked) so RBAC enforcement was
-- de-facto inactive. Table access is now exclusively via resource_grants
-- (ResourceType.TABLE).
CREATE TABLE IF NOT EXISTS table_registry (
    id VARCHAR PRIMARY KEY,
    name VARCHAR NOT NULL,
    source_type VARCHAR,
    bucket VARCHAR,
    source_table VARCHAR,
    source_query TEXT,
    sync_strategy VARCHAR DEFAULT 'full_refresh',
    query_mode VARCHAR DEFAULT 'local',
    sync_schedule VARCHAR,
    profile_after_sync BOOLEAN DEFAULT true,
    primary_key VARCHAR,
    folder VARCHAR,
    description TEXT,
    registered_by VARCHAR,
    registered_at TIMESTAMP DEFAULT current_timestamp,
    -- v26: Keboola sync-strategy support columns. NULL on existing rows;
    -- meaningful only when sync_strategy ∈ {'incremental', 'partitioned'}
    -- (or any strategy + where_filters). API-layer validators enforce the
    -- per-strategy required-field rules.
    incremental_window_days INTEGER,
    max_history_days INTEGER,
    incremental_column VARCHAR,
    where_filters VARCHAR,
    partition_by VARCHAR,
    partition_granularity VARCHAR,
    initial_load_chunk_days INTEGER,
    -- v51: fully-qualified BigQuery path (`project.dataset.table`) for
    -- BigQuery rows. When set, decouples the UX/RBAC `bucket` label from
    -- the physical BQ dataset name; rows without it fall back to the
    -- legacy `<remote_attach.project>.<bucket>.<source_table>` path.
    -- Issue #343 (released on main as 0.54.29).
    bq_fqn VARCHAR,
    -- v55: per-table docs surface used by /catalog/t/<id>. All
    -- admin-authored, optional. sample_questions + pairs_well_with are
    -- JSON arrays so admins can edit lists without us cascading a new
    -- junction table; things_to_know is freeform notes (markdown-ish
    -- treated as plain text on render).
    sample_questions JSON,
    things_to_know   TEXT,
    pairs_well_with  JSON,
    -- v59: structured per-table documentation for the package-detail
    -- rewrite. ``grain`` (e.g. "1 row per session × event_date"),
    -- ``platforms`` (JSON list of platform names), ``partition_col``
    -- (single column name — distinct from the v33-era ``partition_by``
    -- which carries BigQuery partition-key SQL), ``history`` ("Full",
    -- "Rolling 15 months", "Nov 2025+"), ``gotchas`` (JSON list of
    -- ``{key: bool, body: str}`` — first ``key=true`` is rendered
    -- distinctly as the "Key gotcha"). All additive + NULLABLE.
    grain         VARCHAR,
    platforms     VARCHAR,
    partition_col VARCHAR,
    history       VARCHAR,
    gotchas       VARCHAR,
    -- v74: distribution flag, decoupled from query_mode. When true the
    -- table is kept server-side & queryable via `agnes query --remote`,
    -- but `agnes pull` does NOT download its parquet (the manifest still
    -- lists it for catalog discovery + RBAC). Only meaningful for
    -- query_mode IN ('local', 'materialized'); ignored for 'remote'
    -- (which has no server-stored parquet to suppress). Issue #607.
    server_only   BOOLEAN DEFAULT false,
    -- v79: nullable FK to source_connections.id. NULL = use the default
    -- connection for the row's source_type (backwards-compatible).
    connection_id VARCHAR,
    -- v116: table access policies. One SQL policy per table, substituted
    -- for the table on every server-side read when access_policies.enabled
    -- is on. access_policy_sql IS NULL = no policy, unchanged behavior.
    -- access_policy_note is the admin-facing "why" (mandatory at the API
    -- layer when a policy is set; not enforced by this DDL).
    -- access_policy_updated_at/_by are last-edit convenience columns —
    -- audit_log remains authoritative. policy_mapping marks this table as
    -- referenceable from another table's policy body (mapping tables).
    access_policy_sql VARCHAR,
    access_policy_note VARCHAR,
    access_policy_updated_at TIMESTAMP,
    access_policy_updated_by VARCHAR,
    policy_mapping BOOLEAN DEFAULT false
);

CREATE TABLE IF NOT EXISTS source_connections (
    id          VARCHAR PRIMARY KEY,
    name        VARCHAR NOT NULL UNIQUE,
    source_type VARCHAR NOT NULL,
    config      TEXT NOT NULL,
    token_env   VARCHAR,
    is_default  BOOLEAN DEFAULT FALSE,
    created_by  VARCHAR,
    created_at  TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS connection_secrets (
    connection_id VARCHAR PRIMARY KEY,
    ciphertext    TEXT NOT NULL,
    updated_at    TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS table_profiles (
    table_id VARCHAR PRIMARY KEY,
    profile JSON NOT NULL,
    profiled_at TIMESTAMP DEFAULT current_timestamp
);

-- v19: dataset_permissions and access_requests dropped. Replaced by
-- resource_grants (ResourceType.TABLE). Access requests flow removed —
-- users contact admin out-of-band; admin grants via /admin/access.

CREATE TABLE IF NOT EXISTS metric_definitions (
    id              VARCHAR PRIMARY KEY,
    name            VARCHAR NOT NULL,
    display_name    VARCHAR NOT NULL,
    category        VARCHAR NOT NULL,
    description     TEXT,
    type            VARCHAR DEFAULT 'sum',
    unit            VARCHAR,
    grain           VARCHAR DEFAULT 'monthly',
    table_name      VARCHAR,
    tables          VARCHAR[],
    expression      VARCHAR,
    time_column     VARCHAR,
    dimensions      VARCHAR[],
    filters         VARCHAR[],
    synonyms        VARCHAR[],
    notes           VARCHAR[],
    sql             TEXT NOT NULL,
    sql_variants    JSON,
    validation      JSON,
    source          VARCHAR DEFAULT 'manual',
    source_ref      VARCHAR,
    created_at      TIMESTAMP DEFAULT current_timestamp,
    updated_at      TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS column_metadata (
    table_id        VARCHAR NOT NULL,
    column_name     VARCHAR NOT NULL,
    basetype        VARCHAR,
    description     VARCHAR,
    confidence      VARCHAR DEFAULT 'manual',
    source          VARCHAR DEFAULT 'manual',
    updated_at      TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (table_id, column_name)
);

CREATE TABLE IF NOT EXISTS personal_access_tokens (
    id           VARCHAR PRIMARY KEY,
    user_id      VARCHAR NOT NULL,
    name         VARCHAR NOT NULL,
    token_hash   VARCHAR NOT NULL,
    prefix       VARCHAR NOT NULL,
    scopes       VARCHAR,
    created_at   TIMESTAMP NOT NULL DEFAULT current_timestamp,
    expires_at   TIMESTAMP,
    last_used_at TIMESTAMP,
    last_used_ip VARCHAR,
    revoked_at   TIMESTAMP,
    -- v101: agent-as-API — non-NULL when this PAT was minted for/by an
    -- agent (see docs/superpowers/specs/2026-07-21-agent-profiles-and-
    -- agent-api-design.md). Deliberately unindexed.
    agent_id     VARCHAR,
    -- v106: credential data-read surface — 'all' (legacy/admin opt-up) or
    -- 'stack' (catalog/query scoped to the owner's stack even for admins).
    -- Enforced in src/rbac.py via user["credential_surface"]; the schema
    -- DEFAULT backfills every pre-v106 row to 'all' (grandfather).
    surface      VARCHAR DEFAULT 'all'
);

-- v60: short-lived setup tokens for the Agnes Cowork one-click setup flow.
-- Generated by POST /api/user/cowork-bundle, consumed once by
-- POST /api/auth/exchange-setup-token which mints a regular PAT.
-- token_hash = SHA-256(raw "st_..." value) — plaintext never stored.
-- used_at NULL = token still valid; non-NULL = already consumed.
CREATE TABLE IF NOT EXISTS setup_tokens (
    id          VARCHAR PRIMARY KEY,
    user_id     VARCHAR NOT NULL,
    token_hash  VARCHAR NOT NULL,
    expires_at  TIMESTAMP NOT NULL,
    used_at     TIMESTAMP,
    created_at  TIMESTAMP NOT NULL DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS marketplace_registry (
    id              VARCHAR PRIMARY KEY,
    name            VARCHAR NOT NULL,
    url             VARCHAR NOT NULL,
    branch          VARCHAR,
    token_env       VARCHAR,
    description     TEXT,
    registered_by   VARCHAR,
    registered_at   TIMESTAMP DEFAULT current_timestamp,
    last_synced_at  TIMESTAMP,
    last_commit_sha VARCHAR,
    last_error      TEXT,
    -- v37: curator accountability — full name + email captured at registration
    -- and editable later. Surfaced on /marketplace cards and plugin detail in
    -- place of the historic `owner_todo` placeholder. Nullable so existing
    -- rows from pre-v37 instances survive migration; admin must fill via the
    -- /admin/marketplaces edit modal before the placeholder disappears.
    curator_name    VARCHAR,
    curator_email   VARCHAR,
    -- v78: built-in marketplace shipped with the wheel (not a git clone).
    -- TRUE for the single system-seeded row; FALSE for all admin-registered rows.
    -- The nightly git-sync path skips is_builtin=TRUE rows (nothing to fetch).
    is_builtin      BOOLEAN NOT NULL DEFAULT FALSE,
    -- v87: pin the marketplace to a fixed tag name or full 40-char commit
    -- SHA (issue #781). Mutually exclusive with `branch` — enforced at the
    -- admin API layer. NULL = float on `branch` (or remote HEAD) as before.
    ref             VARCHAR
);

CREATE TABLE IF NOT EXISTS marketplace_plugins (
    marketplace_id  VARCHAR NOT NULL,
    name            VARCHAR NOT NULL,
    description     TEXT,
    version         VARCHAR,
    author_name     VARCHAR,
    homepage        VARCHAR,
    category        VARCHAR,
    source_type     VARCHAR,
    source_spec     JSON,
    raw             JSON,
    created_at      TIMESTAMP DEFAULT current_timestamp,
    updated_at      TIMESTAMP DEFAULT current_timestamp,
    -- v37: enrichment from upstream `.claude-plugin/marketplace-metadata.json`.
    -- `cover_photo_url` and `video_url` are stored as already-resolved served
    -- URLs (internal asset endpoint, mirrored cache endpoint, or pass-through
    -- external URL). `doc_links` is a JSON array of `{name, url, kind}` where
    -- `kind ∈ {internal, mirrored, external}` so the frontend can pick the
    -- right icon without re-resolving. NULL = upstream marketplace shipped no
    -- marketplace-metadata.json (or shipped one without an entry for this plugin).
    cover_photo_url VARCHAR,
    video_url       VARCHAR,
    doc_links       JSON,
    -- v39: admin-managed mandatory tier. When TRUE, the plugin is
    -- materialized into resource_grants (for every group) and
    -- user_plugin_optouts (for every user) by the mark_system endpoint
    -- + creation hooks; UI then locks the controls so users cannot
    -- unsubscribe and admins cannot revoke per-group grants for it. The
    -- resolver itself is unchanged — system semantics are emergent from
    -- the materialized rows, not a new filter layer.
    is_system       BOOLEAN DEFAULT FALSE,
    -- v78: per-plugin admin disable flag for built-in plugins. When TRUE,
    -- the plugin is excluded from the served feed for all callers even
    -- when they hold a resource_grant for it. Distinct from the per-user
    -- user_plugin_optouts — this is an instance-wide admin decision.
    admin_disabled  BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (marketplace_id, name)
);

CREATE TABLE IF NOT EXISTS user_groups (
    id          VARCHAR PRIMARY KEY,
    name        VARCHAR NOT NULL UNIQUE,
    description TEXT,
    is_system   BOOLEAN DEFAULT FALSE,
    created_at  TIMESTAMP DEFAULT current_timestamp,
    created_by  VARCHAR
);

-- v13: per-user group membership. Replaces the v12 users.groups JSON cache.
-- The `source` column tracks who created the row so each source only mutates
-- its own rows — Google sync's nightly DELETE+INSERT does NOT clobber
-- admin-added members, and admin UI deletions don't fight the sync loop.
--
-- v14: group_id now FK→user_groups(id). DuckDB FK enforcement blocks the
-- parent DELETE while children exist, so the application must delete
-- members + resource_grants BEFORE the user_groups row (see
-- app/api/access.py:delete_group). DuckDB does NOT support ON DELETE
-- CASCADE, so we rely on explicit transactional cleanup at the call site
-- and let the FK serve as a defense-in-depth invariant.
CREATE TABLE IF NOT EXISTS user_group_members (
    user_id   VARCHAR NOT NULL,
    group_id  VARCHAR NOT NULL REFERENCES user_groups(id),
    source    VARCHAR NOT NULL,  -- 'admin' | 'google_sync' | 'system_seed'
    added_at  TIMESTAMP DEFAULT current_timestamp,
    added_by  VARCHAR,
    PRIMARY KEY (user_id, group_id)
);

-- v13: unified resource grants. Replaces both group_mappings (v8/v9) and
-- plugin_access (v11). resource_type is a string identifier from
-- app.resource_types.ResourceType enum (e.g. 'marketplace_plugin').
-- resource_id is a path string whose format is owned by the module that
-- registered the resource type (e.g. '<marketplace_slug>/<plugin_name>').
--
-- v14: group_id FK→user_groups(id), same rationale as user_group_members.
-- v49: ``requirement`` enum splits Required-tier semantics out of the
-- grant identity. ``available`` (default) — grantee can opt in via
-- ``user_stack_subscriptions``. ``required`` — auto-included in the
-- effective stack, opt-out blocked at the API. Applies to
-- ``data_package`` / ``memory_domain`` / ``memory_item`` grants;
-- ``marketplace_plugin`` Required-tier stays on
-- ``marketplace_plugins.is_system`` per D1.
CREATE TABLE IF NOT EXISTS resource_grants (
    id            VARCHAR PRIMARY KEY,
    group_id      VARCHAR NOT NULL REFERENCES user_groups(id),
    resource_type VARCHAR NOT NULL,
    resource_id   VARCHAR NOT NULL,
    requirement   VARCHAR DEFAULT 'available',
    assigned_at   TIMESTAMP DEFAULT current_timestamp,
    assigned_by   VARCHAR,
    -- v60: per-type FK columns (E.3). One of these is non-NULL for each
    -- of the 5 typed ResourceTypes; all NULL for marketplace_plugin.
    -- DuckDB has no FK/CHECK enforcement — application-layer validates.
    -- PG carries the real FKs + CHECK via migration 0013.
    resource_id_table          VARCHAR,
    resource_id_data_package   VARCHAR,
    resource_id_memory_domain  VARCHAR,
    resource_id_memory_item    VARCHAR,
    resource_id_recipe         VARCHAR,
    UNIQUE (group_id, resource_type, resource_id)
);

-- v49: Data Packages — admin-curated bundles of tables. A package is a
-- single Browse / Add-to-stack unit; effective TABLE set for RBAC =
-- direct TABLE grants ∪ tables in DATA_PACKAGE grants. See
-- ``docs/brainstorms/2026-05-15-unified-stack-design.md`` section 3.3.
CREATE TABLE IF NOT EXISTS data_packages (
    id              VARCHAR PRIMARY KEY,
    slug            VARCHAR UNIQUE NOT NULL,
    name            VARCHAR NOT NULL,
    description     TEXT,
    icon            VARCHAR,
    color           VARCHAR,
    -- v50: admin-uploaded cover image (served from /uploads/covers/<sha>.<ext>).
    -- Closes the visual gap with /marketplace cards which render real
    -- JPGs/PNGs; cards fall back to 2-letter initials when this is NULL.
    cover_image_url VARCHAR,
    -- v51: lifecycle + classification surface for /catalog cards. The
    -- card eyebrow renders ``category``; the cover-corner status pill
    -- renders ``status``. Hero filter checkboxes filter by status.
    -- ``status`` is a soft enum ('prod' default; 'poc'; 'coming-soon';
    -- 'draft' admin-only). ``category`` is free-form text for the
    -- eyebrow line — admins should keep it short and consistent
    -- (e.g. "Sessions & Traffic", "Customer Insights").
    status          VARCHAR DEFAULT 'prod',
    category        VARCHAR,
    -- v54: soft-delete column. DELETE handlers set this instead of
    -- removing the row, so junction tables + resource_grants survive
    -- for the undo flow. list/get filter ``deleted_at IS NULL``.
    deleted_at      TIMESTAMP,
    -- v56: extended content for the /catalog/p/<slug> detail-page
    -- rewrite (extended-descriptions admin spec). All additive + NULLABLE.
    --   owner_name / owner_team — render "Owned by X · Team" line
    --   tags                    — JSON list of category strings
    --   long_description        — markdown body for "What it is"
    --   when_to_use / when_not_to_use
    --                           — JSON bullet lists
    --   example_questions       — JSON list of analyst questions
    --                             surfaced as a package-level prompt
    --                             panel.
    -- v113: publisher_kind ('user' | 'organization') — the SAME stored
    -- trust axis store_entities carries, so a package and a skill make
    -- the same claim the same way and the Library / catalog / detail
    -- surfaces can render one shared marker for both.
    --
    -- This REPLACES the derived `curated` badge, which was computed at
    -- render time from "is the creator currently in the Admin group".
    -- That reading is not a property of the PACKAGE: an admin who leaves
    -- the Admin group silently un-curated everything they had ever
    -- created. It is the exact derivation ``store_entities.publisher_kind``
    -- was introduced to avoid (see the v104 columns below) — group
    -- membership is mutable and re-synced from the identity provider, so
    -- a derived trust claim reclassifies published content behind the
    -- admin's back. Stored, set explicitly, and migrated from whatever the
    -- derivation happened to say at upgrade time.
    --
    -- `new` REMAINS derived from created_at age — that one genuinely is a
    -- function of the clock and nothing else.
    publisher_kind  VARCHAR DEFAULT 'user',
    owner_name      VARCHAR,
    owner_team      VARCHAR,
    tags            VARCHAR,
    long_description TEXT,
    when_to_use     VARCHAR,
    when_not_to_use VARCHAR,
    example_questions VARCHAR,
    created_by      VARCHAR,
    created_at      TIMESTAMP DEFAULT current_timestamp,
    updated_at      TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS data_package_tables (
    package_id  VARCHAR NOT NULL REFERENCES data_packages(id),
    table_id    VARCHAR NOT NULL REFERENCES table_registry(id),
    added_at    TIMESTAMP DEFAULT current_timestamp,
    added_by    VARCHAR,
    PRIMARY KEY (package_id, table_id)
);
CREATE INDEX IF NOT EXISTS idx_data_package_tables_table
    ON data_package_tables(table_id);

-- v49: Memory Domains — first-class entities replacing the v15 scalar
-- ``knowledge_items.domain`` string. Junction allows an item to belong
-- to multiple domains; admin can create non-canonical domains beyond the
-- legacy ``VALID_DOMAINS`` six. See spec section 3.4.
CREATE TABLE IF NOT EXISTS memory_domains (
    id              VARCHAR PRIMARY KEY,
    slug            VARCHAR UNIQUE NOT NULL,
    name            VARCHAR NOT NULL,
    description     TEXT,
    icon            VARCHAR,
    color           VARCHAR,
    -- v50: admin-uploaded cover image — same path / fallback contract as
    -- data_packages.cover_image_url above.
    cover_image_url VARCHAR,
    -- v51: lifecycle ``status`` only ('prod' / 'poc' / 'coming-soon' /
    -- 'draft'). Memory Domains don't carry ``category`` because the
    -- domain itself IS the classification — adding a second-level
    -- category would be redundant.
    status          VARCHAR DEFAULT 'prod',
    -- v54: soft-delete (see data_packages.deleted_at).
    deleted_at      TIMESTAMP,
    created_by      VARCHAR,
    created_at      TIMESTAMP DEFAULT current_timestamp,
    updated_at      TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS knowledge_item_domains (
    item_id   VARCHAR NOT NULL REFERENCES knowledge_items(id),
    domain_id VARCHAR NOT NULL REFERENCES memory_domains(id),
    added_at  TIMESTAMP DEFAULT current_timestamp,
    added_by  VARCHAR,
    PRIMARY KEY (item_id, domain_id)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_item_domains_domain
    ON knowledge_item_domains(domain_id);

-- v55: ``memory_domain_suggestions`` — non-admin users can suggest a new
-- domain from /corporate-memory empty state. Admin queue surfaces them
-- with one-click approve (creates the real ``memory_domains`` row +
-- marks the suggestion ``status='approved'``) or reject. Open suggestions
-- have ``status='pending'``; resolved ones keep the row for audit so the
-- requester sees the disposition. No FK on ``created_by`` so a deleted
-- user doesn't cascade-nuke their suggestion history.
CREATE TABLE IF NOT EXISTS memory_domain_suggestions (
    id              VARCHAR PRIMARY KEY,
    name            VARCHAR NOT NULL,
    description     TEXT,
    rationale       TEXT,
    status          VARCHAR DEFAULT 'pending',  -- 'pending' / 'approved' / 'rejected'
    created_by      VARCHAR,
    created_at      TIMESTAMP DEFAULT current_timestamp,
    resolved_at     TIMESTAMP,
    resolved_by     VARCHAR,
    resolution_note TEXT,
    -- When approved, the resulting memory_domains.id so the admin queue
    -- can deep-link to the created domain.
    created_domain_id VARCHAR
);
CREATE INDEX IF NOT EXISTS idx_memory_domain_suggestions_status
    ON memory_domain_suggestions(status);

-- v77: ``authoring_suggestions`` — generic non-admin suggestion queue for the
-- authoring studio. A non-admin submits a proposed create payload (per studio
-- domain); an admin approves (replays the payload through the real endpoint)
-- or rejects. Generalizes ``memory_domain_suggestions`` across all domains.
CREATE TABLE IF NOT EXISTS authoring_suggestions (
    id                  VARCHAR PRIMARY KEY,
    domain              VARCHAR NOT NULL,
    payload             JSON,
    status              VARCHAR DEFAULT 'pending',  -- 'pending' / 'approved' / 'rejected'
    created_by          VARCHAR,
    created_at          TIMESTAMP DEFAULT current_timestamp,
    resolved_at         TIMESTAMP,
    resolved_by         VARCHAR,
    resolution_note     TEXT,
    created_resource_id VARCHAR
);
CREATE INDEX IF NOT EXISTS idx_authoring_suggestions_status
    ON authoring_suggestions(status);

-- v78: ``memory_mining_consent`` — per-user opt-IN to having their session
-- transcripts mined into shared corporate memory (privacy gate, spec §4.4).
CREATE TABLE IF NOT EXISTS memory_mining_consent (
    user_email   VARCHAR PRIMARY KEY,
    opted_in_at  TIMESTAMP,
    opted_out_at TIMESTAMP,
    updated_at   TIMESTAMP DEFAULT current_timestamp
);

-- v61: ``cli_auth_codes`` — short-lived, single-use exchange codes for the
-- browser-loopback `agnes auth login` flow (gh-style). The browser, holding
-- an authenticated session, confirms CLI authorization; the server mints a
-- code (hash stored here, bound to the user) and redirects it to the CLI's
-- localhost loopback. The CLI then POSTs the code to /cli/auth/exchange over
-- HTTPS and receives a real PAT — so the durable credential never travels
-- through the browser address bar / history. Codes expire in ~2 min and are
-- consumed exactly once (compare-and-swap on ``consumed_at``). Rows are left
-- after expiry/consumption for a short audit window; a cheap opportunistic
-- delete of expired rows runs on each create.
CREATE TABLE IF NOT EXISTS cli_auth_codes (
    code_hash   VARCHAR PRIMARY KEY,   -- sha256(raw code); raw code never stored
    user_id     VARCHAR NOT NULL,
    email       VARCHAR NOT NULL,
    created_at  TIMESTAMP NOT NULL DEFAULT current_timestamp,
    expires_at  TIMESTAMP NOT NULL,
    consumed_at TIMESTAMP
);

-- v53: Recipes are admin-curated, multi-table query templates analysts
-- copy + adapt. Sibling concept to Data Packages on /catalog (separate
-- "Recipes" tab). Not stack subscribable — analysts use a recipe, they
-- don't opt in to it. ``related_table_ids`` is a JSON array of
-- ``table_registry.id`` values so the recipe drilldown can render
-- per-table links without us cascading another junction table.
CREATE TABLE IF NOT EXISTS recipes (
    id              VARCHAR PRIMARY KEY,
    slug            VARCHAR UNIQUE NOT NULL,
    title           VARCHAR NOT NULL,
    description     TEXT,
    icon            VARCHAR,
    color           VARCHAR,
    sql_template    TEXT,
    related_table_ids JSON,
    status          VARCHAR DEFAULT 'prod',
    -- v54: soft-delete (see data_packages.deleted_at).
    deleted_at      TIMESTAMP,
    created_by      VARCHAR,
    created_at      TIMESTAMP DEFAULT current_timestamp,
    updated_at      TIMESTAMP DEFAULT current_timestamp
);

-- v49: generic per-user opt-in for resource_grants flagged
-- ``requirement='available'``. Currently scoped to ``data_package`` /
-- ``memory_domain`` resource types — Marketplace pluginy stay on the
-- existing ``user_plugin_optouts`` opt-out shape per D1.
CREATE TABLE IF NOT EXISTS user_stack_subscriptions (
    user_id       VARCHAR NOT NULL,
    resource_type VARCHAR NOT NULL,
    resource_id   VARCHAR NOT NULL,
    subscribed_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (user_id, resource_type, resource_id)
);

-- v22: reserved (formerly setup_banner — feature dropped, table kept for
-- forward compatibility with already-migrated instances).
CREATE TABLE IF NOT EXISTS setup_banner (
    id INTEGER PRIMARY KEY DEFAULT 1,
    content TEXT,
    updated_at TIMESTAMP,
    updated_by VARCHAR,
    CONSTRAINT singleton CHECK (id = 1)
);

-- v28: generic per-key instance-template storage. Consolidates the v21
-- welcome_template and v23 claude_md_template singletons into one shape
-- so future operator-customizable surfaces ship as a row insert + admin-UI
-- section, not a fresh schema bump. Pre-seeded keys: 'welcome', 'claude_md',
-- 'home'. NULL content means "use the OSS-shipped default"; an admin override
-- replaces the OSS default at render time.
CREATE TABLE IF NOT EXISTS instance_templates (
    key VARCHAR PRIMARY KEY,
    content TEXT,
    previous_content TEXT,
    updated_at TIMESTAMP,
    updated_by VARCHAR,
    -- v75 (#622): explicit Git⇄Editor source toggle for managed prompts,
    -- superseding the implicit seed_owns() read-only lock. 'editor' = the DB
    -- override (content) wins at render time; 'git' = bind to git_path in the
    -- Initial Workspace Template clone. base_sha is reserved for Slice 2
    -- divergence detection (written, not read in Slice 1).
    source_mode VARCHAR NOT NULL DEFAULT 'editor',
    git_path    VARCHAR,
    base_sha    VARCHAR
);

-- v29: news_template — single table holding every saved version of the
-- /home news perex + /news full body. `version` ↑ per save. `published`
-- distinguishes the active draft (FALSE) from public versions (TRUE).
-- Web reads `WHERE published = TRUE ORDER BY version DESC LIMIT 1`.
-- Admin can browse all rows. Invariant: at most one row with
-- `published = FALSE` at any time (the active draft). See
-- src/repositories/news_template.py.
CREATE TABLE IF NOT EXISTS news_template (
    id              VARCHAR PRIMARY KEY,
    version         INTEGER NOT NULL UNIQUE,
    intro           TEXT,
    content         TEXT,
    published       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMP NOT NULL DEFAULT current_timestamp,
    updated_at      TIMESTAMP NOT NULL DEFAULT current_timestamp,
    created_by      VARCHAR,
    published_at    TIMESTAMP,
    published_by    VARCHAR
);
CREATE INDEX IF NOT EXISTS ix_news_template_pub_ver
    ON news_template (published, version DESC);

-- v25: per-user marketplace composition layer on top of admin grants.
--   * store_entities       — community-uploaded skills/agents/plugins
--   * user_store_installs  — which entities each user has chosen to install
--   * user_plugin_optouts  — opt-out overlay on top of admin-granted plugins
--
-- The served Claude Code marketplace for a user is computed as:
--     (admin_granted ∖ opt_outs) ∪ store_installs
--
-- See src/marketplace_filter.py:resolve_user_marketplace.
-- FK refs to users(id) intentionally omitted (matches the
-- personal_access_tokens / marketplace_registry pattern). DuckDB blocks
-- ALTER on a referenced parent — past finalize steps RENAME / DROP COLUMN
-- on `users`, which would fail if these store tables held FK refs at the
-- time the ladder reaches them. App-level deletes already cascade
-- explicitly (see app/api/store.py + the resource_grant-deletion hook).
CREATE TABLE IF NOT EXISTS store_entities (
    id                VARCHAR PRIMARY KEY,
    owner_user_id     VARCHAR NOT NULL,
    owner_username    VARCHAR NOT NULL,
    type              VARCHAR NOT NULL CHECK (type IN ('skill','agent','plugin')),
    name              VARCHAR NOT NULL,
    description       TEXT,
    category          VARCHAR,
    version           VARCHAR NOT NULL,
    photo_path        VARCHAR,
    video_url         VARCHAR,
    doc_paths         JSON,
    file_size         BIGINT,
    install_count     BIGINT NOT NULL DEFAULT 0,
    -- v29: flea-market guardrails. Non-approved entities are hidden from
    -- non-admin browse + per-user marketplace composition until the LLM
    -- review (or an admin override) flips them to 'approved'. Existing
    -- v28 rows backfill to 'approved' so current uploads stay visible
    -- through the upgrade.
    -- v35: 'archived' added — owner soft-delete state. Hidden from
    -- every browse listing (including the owner's own My AI Stack
    -- "card" filter), but still served to existing user_store_installs
    -- so previously-installed users keep getting the bundle through
    -- marketplace.zip / .git. Hard delete remains admin-only via
    -- DELETE ?hard=true.
    visibility_status VARCHAR NOT NULL DEFAULT 'pending'
                      CHECK (visibility_status IN ('pending','approved','hidden','archived')),
    archived_at       TIMESTAMP,
    archived_by       VARCHAR,
    -- v37: flea-market edit feature. version_no tracks the current
    -- version index (1-based); version_history is an append-only JSON
    -- array of past version metadata. Bundle bytes for each version
    -- live on disk under ${DATA_DIR}/store/<id>/versions/v<N>/plugin/
    -- so rollback can copy them forward; the live `plugin/` dir is
    -- always a copy of the current version. See
    -- StoreEntitiesRepository.append_version + restore endpoint.
    version_no        INTEGER NOT NULL DEFAULT 1,
    version_history   JSON DEFAULT '[]',
    -- v49: phase-1 Flea refactor adds three user-facing metadata columns.
    -- `title` is a humanized display name (acronym-aware), shown on web
    -- surfaces instead of the kebab-case `name`. `tagline` is an optional
    -- 200-char short description for card UI (long-form lives in
    -- `description`). `synthetic_name` is the deterministic
    -- `<name>-by-<owner_username>` value baked into served bundles —
    -- stored as a column so attribution + uniqueness checks can target a
    -- single source of truth instead of recomputing the concat on every
    -- query. Phase 1 only populates these; downstream surfaces (cards,
    -- detail pages, Claude Code propagation) consume them in later phases.
    title             VARCHAR NOT NULL,
    tagline           VARCHAR,
    synthetic_name    VARCHAR NOT NULL,
    -- v104: publisher + verification — the card's trust line ("Skill · by
    -- Anna Nováková" / "Skill · Your organization").
    --
    -- `publisher_kind` answers "who stands behind this", and is deliberately
    -- STORED rather than derived from the owner's current Admin-group
    -- membership: groups are mutable and re-synced nightly from the identity
    -- provider, so a derived value would silently reclassify an author's
    -- published skills the moment they moved out of the Admin group. Only an
    -- explicit admin "publish as the organization" action writes
    -- 'organization'. Curated marketplace_plugins rows need no column — the
    -- admin act of registering the marketplace already makes every plugin in
    -- it organization-published, so the projection layer hard-codes it.
    --
    -- The verification columns are the org's advisory review of a
    -- USER-published item. Two invariants, both asserted in tests:
    --   1. publisher_kind='organization' ⇒ verification_state='none'. An org
    --      item carries the stronger claim; a checkmark on top of it would
    --      invent a fourth trust tier.
    --   2. Verification NEVER gates reads. It is a chip and a filter value,
    --      never a predicate in _enforce_visibility or the listing clause —
    --      the moment it gates, it is the old approval queue again.
    -- 'requested' / 'changes_requested' are author-visible workflow states;
    -- other users see only 'verified' (or nothing at all — there is no
    -- "Unverified" label, by design, since it would print on ~every card on
    -- an instance with no reviewer).
    publisher_kind    VARCHAR NOT NULL DEFAULT 'user'
                      CHECK (publisher_kind IN ('organization','user')),
    verification_state VARCHAR NOT NULL DEFAULT 'none'
                      CHECK (verification_state IN
                             ('none','requested','verified','changes_requested')),
    verified_at       TIMESTAMP,
    verified_by       VARCHAR,
    verification_note TEXT,
    created_at        TIMESTAMP DEFAULT current_timestamp,
    updated_at        TIMESTAMP DEFAULT current_timestamp,
    UNIQUE (owner_user_id, name)
);

CREATE TABLE IF NOT EXISTS user_store_installs (
    user_id      VARCHAR NOT NULL,
    entity_id    VARCHAR NOT NULL,
    installed_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (user_id, entity_id)
);

CREATE TABLE IF NOT EXISTS user_plugin_optouts (
    user_id        VARCHAR NOT NULL,
    marketplace_id VARCHAR NOT NULL,
    plugin_name    VARCHAR NOT NULL,
    opted_out_at   TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (user_id, marketplace_id, plugin_name)
);

-- v29: flea-market upload guardrails — every POST/PUT to /api/store/entities
-- writes a submissions row capturing the inline check verdicts, the async
-- LLM review outcome, and any admin override. Powers /admin/store/submissions.
--
-- Insert states (chosen by /api/store/entities POST):
--   pending_llm     → inline checks passed; LLM review enqueued; entity
--                     row created with visibility_status='pending'.
--   blocked_inline  → at least one inline check failed; entity row
--                     created with visibility_status='hidden' so admin
--                     can rescan / override / download. (Pre-v30 the
--                     entity row was rolled back; persisted now for
--                     forensics + the 30-day TTL bundle purge path.)
--
-- Background-task transitions (runner.py):
--   pending_llm → approved        — review concluded safe; entity flips
--                                   to visibility_status='approved'.
--   pending_llm → blocked_llm     — review flagged risk ≥ high; entity
--                                   stays at visibility_status='pending'.
--   pending_llm → review_error    — LLM call errored / timed out / missing
--                                   risk_level; admin Retry available.
--                                   Reaper sweeps stuck pending_llm rows
--                                   every 15 min into review_error.
--
-- Admin transitions:
--   blocked_* | review_error → overridden — force-publish; entity flipped
--                                   to visibility_status='approved'.
--
-- Lifecycle (terminal):
--   any → deleted — set by mark_deleted_for_entity after admin DELETE
--                   ?hard=true; entity row gone but tombstone entity_id
--                   preserved for activity-timeline correlation.
--
-- The legacy 'pending_inline' value exists in VALID_STATUSES for
-- forward-compat with future async-inline checks but is NOT written by
-- any current code path on insert.
CREATE TABLE IF NOT EXISTS store_submissions (
    id              VARCHAR PRIMARY KEY,
    entity_id       VARCHAR,
    submitter_id    VARCHAR NOT NULL,
    submitter_email VARCHAR,
    type            VARCHAR NOT NULL,
    name            VARCHAR NOT NULL,
    version         VARCHAR,
    status          VARCHAR NOT NULL,
    inline_checks   JSON,
    llm_findings    JSON,
    reviewed_by_model VARCHAR,
    override_by     VARCHAR,
    override_reason TEXT,
    -- v30: forensic columns. file_size + bundle_sha256 are populated at
    -- upload time and survive the TTL purge so admins can correlate
    -- repeat-payload attempts after the bundle bytes are gone.
    -- bundle_purged_at lets the detail UI render "Bundle purged on …"
    -- instead of an empty Download cell.
    file_size        BIGINT,
    bundle_sha256    VARCHAR,
    bundle_purged_at TIMESTAMP,
    created_at      TIMESTAMP DEFAULT current_timestamp,
    updated_at      TIMESTAMP DEFAULT current_timestamp
);

CREATE INDEX IF NOT EXISTS idx_store_submissions_status ON store_submissions(status);
CREATE INDEX IF NOT EXISTS idx_store_submissions_entity ON store_submissions(entity_id);
-- NOTE: the v50 UNIQUE INDEX on store_entities.synthetic_name is created
-- by ``_v49_to_v50_migrate``, not here. Reason: ``_v48_to_v49_migrate``
-- runs ``ALTER TABLE store_entities ALTER COLUMN … SET NOT NULL`` which
-- DuckDB blocks when an index already references the table. Fresh-install
-- ordering is therefore: CREATE TABLE (no index) → v49 migrate (no-op
-- ALTERs on empty table) → v50 migrate (CREATE UNIQUE INDEX).
-- NOTE: no created_at index. DuckDB 1.x has a bug where
-- `ORDER BY <indexed col> DESC LIMIT N` short-returns on small tables
-- (reproduced with N=2 against 3 rows during /admin/store/submissions
-- paging). Submissions table is admin-only and bounded by upload
-- volume, so the index buys little; dropping it sidesteps the bug.

-- v40: persistent metadata cache for remote sources (BigQuery initially).
-- Replaces the per-request, in-memory `_metadata_cache` in v2_catalog.py
-- that turned every cold-cache /api/v2/catalog into a sequence of N×3 BQ
-- jobs API calls (one TABLE_STORAGE + COLUMNS pair per remote row) — long
-- enough on view-backed or partitioned tables (>>30 s) to blow the CLI's
-- httpx 30 s read timeout. Now refresh is driven exclusively by the
-- scheduler (default every 4 h, `SCHEDULER_BQ_METADATA_REFRESH_INTERVAL`),
-- and the catalog endpoint just reads this table — no BQ at request time.
--
-- Columns:
--   table_id          — registry.id; PK and join key with table_registry.
--   rows / size_bytes / partition_by / clustered_by — last successful
--                       provider result. NULL when the table has never
--                       been fetched, or fetch failed before any success.
--                       clustered_by stored as JSON array of column names.
--   refreshed_at      — wall-clock of the last successful fetch. Used by
--                       the catalog response to compute metadata_freshness
--                       (`fresh` if < 2× scheduler interval old, `stale`
--                       otherwise, `never_fetched` if NULL).
--   error_at / error_msg — last failure timestamp + redacted message.
--                       NULL after the next successful refresh.
CREATE TABLE IF NOT EXISTS bq_metadata_cache (
    table_id        VARCHAR PRIMARY KEY,
    rows            BIGINT,
    size_bytes      BIGINT,
    partition_by    VARCHAR,
    clustered_by    JSON,
    -- BigQuery entity classification, surfaced in catalog so analyst Claude
    -- can decide query strategy. Values mirror INFORMATION_SCHEMA.TABLES.
    -- table_type: `BASE TABLE`, `VIEW`, `MATERIALIZED VIEW`, `EXTERNAL`,
    -- `SNAPSHOT`, `CLONE`. NULL until first successful refresh.
    entity_type     VARCHAR,
    -- Cache of known column names from the most recent successful refresh,
    -- as JSON array of strings. Used by /api/v2/catalog to filter generic
    -- where_examples against the table's actual schema — drops example
    -- predicates that reference columns the table doesn't have. Populated
    -- by bq_metadata_refresh.refresh_one from fetch_bq_columns_full, so
    -- there is no extra BQ roundtrip just for this.
    known_columns   JSON,
    refreshed_at    TIMESTAMP,
    error_at        TIMESTAMP,
    error_msg       VARCHAR
);
-- Self-heal for instances that already ran an earlier v40 incarnation
-- that lacked entity_type / known_columns. The CREATE TABLE above is
-- IF NOT EXISTS so it skips on already-existing tables; these ALTERs
-- close the column-set gap. Idempotent on fresh installs (no-op).
ALTER TABLE bq_metadata_cache ADD COLUMN IF NOT EXISTS entity_type VARCHAR;
ALTER TABLE bq_metadata_cache ADD COLUMN IF NOT EXISTS known_columns JSON;

-- v42 (was v41 pre-rebase): usage telemetry tables — per-event log,
-- per-session aggregate, daily rollups, and attribution tables for
-- skills/agents/commands.
CREATE TABLE IF NOT EXISTS usage_events (
    id                  VARCHAR PRIMARY KEY,
    session_id          VARCHAR NOT NULL,
    session_file        VARCHAR NOT NULL,
    username            VARCHAR NOT NULL,
    event_uuid          VARCHAR,
    parent_uuid         VARCHAR,
    event_type          VARCHAR NOT NULL,
    tool_name           VARCHAR,
    skill_name          VARCHAR,
    subagent_type       VARCHAR,
    command_name        VARCHAR,
    is_error            BOOLEAN DEFAULT FALSE,
    source              VARCHAR NOT NULL,
    ref_id              VARCHAR,
    model               VARCHAR,
    cwd                 VARCHAR,
    occurred_at         TIMESTAMP NOT NULL,
    processor_version   INTEGER NOT NULL,
    extracted_at        TIMESTAMP DEFAULT current_timestamp,
    friction_tags       JSON,
    user_id             VARCHAR
);
CREATE INDEX IF NOT EXISTS idx_usage_events_session ON usage_events(session_id);
CREATE INDEX IF NOT EXISTS idx_usage_events_user_time ON usage_events(username, occurred_at);
CREATE INDEX IF NOT EXISTS idx_usage_events_tool ON usage_events(tool_name);
CREATE INDEX IF NOT EXISTS idx_usage_events_skill ON usage_events(skill_name);
CREATE INDEX IF NOT EXISTS idx_usage_events_ref ON usage_events(source, ref_id);
-- idx_usage_events_user_id is created by _v44_to_v45, not here: _SYSTEM_SCHEMA
-- runs before the migration ladder, and CREATE TABLE IF NOT EXISTS won't add
-- user_id to a pre-v45 usage_events, so an index on it would fail to bind.
-- Same pattern as the v41 audit_log indices below.

CREATE TABLE IF NOT EXISTS usage_session_summary (
    session_file        VARCHAR PRIMARY KEY,
    session_id          VARCHAR NOT NULL,
    username            VARCHAR NOT NULL,
    started_at          TIMESTAMP,
    ended_at            TIMESTAMP,
    active_seconds      INTEGER,
    wall_seconds        INTEGER,
    user_messages       INTEGER DEFAULT 0,
    assistant_messages  INTEGER DEFAULT 0,
    tool_calls          INTEGER DEFAULT 0,
    tool_errors         INTEGER DEFAULT 0,
    skill_invocations   INTEGER DEFAULT 0,
    subagent_dispatches INTEGER DEFAULT 0,
    mcp_calls           INTEGER DEFAULT 0,
    slash_commands      INTEGER DEFAULT 0,
    distinct_tools      INTEGER DEFAULT 0,
    distinct_skills     INTEGER DEFAULT 0,
    primary_model       VARCHAR,
    processor_version   INTEGER NOT NULL,
    extracted_at        TIMESTAMP DEFAULT current_timestamp,
    -- v105: first-ingest arrival stamp; the sessions browser windows on
    -- this (anchor=uploaded) so late-uploaded sessions stay visible.
    -- NO secondary index — see the v95 ART-index incident note above.
    uploaded_at         TIMESTAMP,
    -- v44: per-session token counters summed from JSONL message.usage.*.
    -- BIGINT because cache tokens routinely exceed INT range over long
    -- sessions. Default 0 so existing rows backfill cleanly; the
    -- processor's reprocess loop (driven by USAGE_PROCESSOR_VERSION
    -- bump) overwrites with real values on next tick.
    input_tokens          BIGINT DEFAULT 0,
    output_tokens         BIGINT DEFAULT 0,
    cache_read_tokens     BIGINT DEFAULT 0,
    cache_creation_tokens BIGINT DEFAULT 0,
    user_id               VARCHAR
);
-- No secondary indexes here (deliberately, since v95): idx_usage_session_user
-- (username), idx_usage_session_started (started_at) and idx_usage_session_user_id
-- (user_id) used to be created here / by _v44_to_v45, but upsert_summary's
-- ON CONFLICT DO UPDATE rewrites all three columns on every re-process tick,
-- and a single corrupt ART entry on any of them turned that rewrite's
-- delete-old-entry step into a FATAL, connection-invalidating error
-- (INCIDENT 2026-07-20). _v94_to_v95 drops the indexes on upgrade; fresh
-- installs simply never create them. See _v94_to_v95 for the full incident
-- writeup.

-- usage_tool_daily: legacy rollup of tool invocations by day/source. Currently
-- only consumed by `src/usage_ask.py` SCHEMA_DIGEST + admin reprocess endpoint;
-- has no product-UI consumer. Marked as candidate for removal in v46; will be
-- evaluated for full drop in next telemetry refactor iteration.
CREATE TABLE IF NOT EXISTS usage_tool_daily (
    day                 DATE NOT NULL,
    tool_name           VARCHAR NOT NULL,
    source              VARCHAR NOT NULL,
    invocations         INTEGER DEFAULT 0,
    error_count         INTEGER DEFAULT 0,
    distinct_users      INTEGER DEFAULT 0,
    distinct_sessions   INTEGER DEFAULT 0,
    PRIMARY KEY (day, tool_name, source)
);

-- v46: marketplace item telemetry rollup. Per-day fact table; window snapshot
-- is sibling `usage_marketplace_item_window`.
-- `parent_plugin` is '' (empty string, not NULL) for type='plugin' rows and
-- standalone flea entities — keeps composite PK well-defined without NULL gymnastics.
CREATE TABLE IF NOT EXISTS usage_marketplace_item_daily (
    day            DATE    NOT NULL,
    source         VARCHAR NOT NULL,            -- 'curated' | 'flea' | 'builtin'
    type           VARCHAR NOT NULL,            -- 'plugin' | 'skill' | 'agent'
    parent_plugin  VARCHAR NOT NULL DEFAULT '', -- '' = no parent
    name           VARCHAR NOT NULL,
    count          INTEGER NOT NULL DEFAULT 0,
    distinct_users INTEGER NOT NULL DEFAULT 0, -- per-day COUNT(DISTINCT user_id)
    error_count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, source, type, parent_plugin, name)
);
CREATE INDEX IF NOT EXISTS idx_mid_lookup ON usage_marketplace_item_daily(source, type, parent_plugin, name);

-- v46: sliding-window snapshot for marketplace items. Refreshed by
-- `rebuild_rollups` — last_7d every UsageProcessor tick (~10 min),
-- last_30d hourly. `distinct_users` here is the TRUE distinct count
-- across the window (recomputed from usage_events at rebuild time),
-- not a sum of per-day distincts.
CREATE TABLE IF NOT EXISTS usage_marketplace_item_window (
    period_label   VARCHAR NOT NULL,            -- 'last_7d' | 'last_30d' (extensible)
    source         VARCHAR NOT NULL,
    type           VARCHAR NOT NULL,
    parent_plugin  VARCHAR NOT NULL DEFAULT '',
    name           VARCHAR NOT NULL,
    invocations    INTEGER NOT NULL DEFAULT 0,
    distinct_users INTEGER NOT NULL DEFAULT 0,  -- true sliding-window distinct
    refreshed_at   TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (period_label, source, type, parent_plugin, name)
);
CREATE INDEX IF NOT EXISTS idx_miw_lookup ON usage_marketplace_item_window(period_label, source, type);

-- v68: cloud chat — per-session transcript storage + per-user workdir markers.
-- DuckDB 1.5.x does NOT support ON DELETE CASCADE on foreign keys, so the
-- chat_messages FK is a plain reference (blocks deletes when children exist);
-- callers must delete messages before deleting a session, or use the
-- ChatRepository.delete_session() helper that does both.
-- DuckDB 1.5.x does NOT support partial (filtered) unique indexes, so the
-- per-surface uniqueness for slack_dm / slack_thread is enforced at the
-- application layer in ChatRepository (not by DB index).
CREATE TABLE IF NOT EXISTS chat_sessions (
    id               VARCHAR PRIMARY KEY,
    user_email       VARCHAR NOT NULL,
    surface          VARCHAR NOT NULL,
    slack_channel_id VARCHAR,
    slack_thread_ts  VARCHAR,
    title            VARCHAR,
    started_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- NOTE: last_message_at and message_count are NEVER written after
    -- INSERT. DuckDB 1.5.3 raises a false FK violation when UPDATE-ing
    -- last_message_at (it's part of idx_chat_sessions_user). Workaround:
    -- ChatRepository computes both via LEFT JOIN at read time. Code that
    -- reads these columns directly will see (NULL, 0) for every row.
    last_message_at  TIMESTAMP,
    message_count    INTEGER NOT NULL DEFAULT 0,
    archived         BOOLEAN NOT NULL DEFAULT FALSE,
    is_co_session    BOOLEAN NOT NULL DEFAULT FALSE,
    ephemeral        BOOLEAN NOT NULL DEFAULT FALSE,
    -- Sandbox pause/resume refs (un-indexed — DuckDB 1.5.3 FK+index bug).
    sandbox_id        VARCHAR,
    runner_pid        INTEGER,
    sandbox_paused_at TIMESTAMP,
    -- Relay protocol version of the runner sandbox_id/runner_pid point at
    -- (v98, Tier 1 restart-invariant reuse). NULL = unknown/legacy — see
    -- app.chat.types.RELAY_PROTOCOL_VERSION's docstring.
    relay_protocol_version INTEGER,
    -- v101: agent-as-API — which agent profile (if any) drove this session.
    -- Deliberately unindexed: this table already carries idx_chat_sessions_user
    -- and DuckDB's ART-index maintenance is the exact incident class
    -- _v94_to_v95 exists to fix, so no new secondary index goes on this column.
    agent_id          VARCHAR,
    -- v112: user-pinned conversations. NULL = not pinned; a timestamp records
    -- WHEN it was pinned so the history panel can order pins most-recent-first
    -- rather than by an arbitrary tiebreak. Deliberately unindexed for the same
    -- reason as agent_id above — and doubly so here, since this column IS
    -- UPDATEd after chat_messages rows exist (the DuckDB 1.5.3 FK+index bug
    -- would turn every pin click into a false FK violation).
    pinned_at         TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_user
    ON chat_sessions(user_email, last_message_at);

CREATE TABLE IF NOT EXISTS chat_messages (
    id          VARCHAR PRIMARY KEY,
    session_id  VARCHAR NOT NULL REFERENCES chat_sessions(id),
    role        VARCHAR NOT NULL,
    content     TEXT NOT NULL,
    tool_calls  JSON,
    -- Ordered [{type:'text'|'tool', …}] — the turn's SHAPE, so prose and tool
    -- calls keep their interleaving across a reload (#1504). `tool_calls`
    -- stays as its positionless projection for readers that predate this and
    -- for rows written before it existed; app/chat/message_parts.py owns both
    -- and derives one from the other. NULL on a pre-v123 row.
    parts       JSON,
    tokens_in   INTEGER,
    tokens_out  INTEGER,
    model       VARCHAR,
    sender_email VARCHAR,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_session
    ON chat_messages(session_id, created_at);

CREATE TABLE IF NOT EXISTS chat_session_participants (
    id          VARCHAR PRIMARY KEY,
    session_id  VARCHAR NOT NULL REFERENCES chat_sessions(id),
    user_email  VARCHAR NOT NULL,
    user_id     VARCHAR NOT NULL,
    role        VARCHAR NOT NULL,
    joined_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    left_at     TIMESTAMP,
    UNIQUE (session_id, user_email)
);
CREATE INDEX IF NOT EXISTS idx_chat_session_participants_user
    ON chat_session_participants(user_email, session_id);

CREATE TABLE IF NOT EXISTS user_workdirs (
    user_email             VARCHAR PRIMARY KEY,
    last_init_at           TIMESTAMP,
    marketplace_sha        VARCHAR,
    initial_workspace_sha  VARCHAR,
    agnes_version_at_init  VARCHAR
);

-- v83: OAuth 2.1 tables for the native MCP connector.
-- oauth_clients        — dynamic client registrations (RFC 7591)
-- oauth_auth_codes     — short-lived PKCE authorization codes
-- oauth_access_tokens  — issued access tokens (verify via load_access_token)
-- oauth_refresh_tokens — refresh tokens for token rotation
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id        VARCHAR PRIMARY KEY,
    client_secret    VARCHAR,
    redirect_uris    TEXT NOT NULL DEFAULT '[]',
    client_name      VARCHAR,
    client_metadata  TEXT NOT NULL DEFAULT '{}',
    created_at       TIMESTAMP NOT NULL DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS oauth_auth_codes (
    code                             VARCHAR PRIMARY KEY,
    client_id                        VARCHAR NOT NULL,
    scopes                           TEXT NOT NULL DEFAULT '[]',
    code_challenge                   VARCHAR NOT NULL,
    redirect_uri                     VARCHAR NOT NULL,
    redirect_uri_provided_explicitly BOOLEAN NOT NULL DEFAULT FALSE,
    expires_at                       DOUBLE NOT NULL,
    subject                          VARCHAR,
    resource                         VARCHAR,
    state                            VARCHAR
);

CREATE TABLE IF NOT EXISTS oauth_access_tokens (
    token      VARCHAR PRIMARY KEY,
    client_id  VARCHAR NOT NULL,
    scopes     TEXT NOT NULL DEFAULT '[]',
    expires_at BIGINT,
    subject    VARCHAR,
    resource   VARCHAR,
    revoked_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
    token      VARCHAR PRIMARY KEY,
    client_id  VARCHAR NOT NULL,
    scopes     TEXT NOT NULL DEFAULT '[]',
    expires_at BIGINT,
    subject    VARCHAR,
    resource   VARCHAR,
    revoked_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT current_timestamp
);

-- v82: Collections (bring-your-files) foundation.
-- file_corpora: a Collection -- self-service container of uploaded files.
CREATE TABLE IF NOT EXISTS file_corpora (
    id VARCHAR PRIMARY KEY,
    slug VARCHAR UNIQUE NOT NULL,
    name VARCHAR NOT NULL,
    description VARCHAR,
    created_by VARCHAR NOT NULL,
    origin VARCHAR NOT NULL DEFAULT 'uploaded',
    created_at TIMESTAMP DEFAULT current_timestamp,
    updated_at TIMESTAMP DEFAULT current_timestamp,
    deleted_at TIMESTAMP
);

-- corpus_files: one row per uploaded file + its processing lifecycle.
-- processing_status: pending | processing | indexed | needs_review | rejected
CREATE TABLE IF NOT EXISTS corpus_files (
    id VARCHAR PRIMARY KEY,
    corpus_id VARCHAR NOT NULL,
    filename VARCHAR NOT NULL,
    sha256 VARCHAR NOT NULL,
    file_type VARCHAR,
    size_bytes BIGINT,
    storage_path VARCHAR,
    parent_file_id VARCHAR,
    path VARCHAR,
    processing_status VARCHAR NOT NULL DEFAULT 'pending',
    processing_detail VARCHAR,
    created_at TIMESTAMP DEFAULT current_timestamp,
    updated_at TIMESTAMP DEFAULT current_timestamp
);

-- NOTE: the (corpus_id, path) UNIQUE INDEX that enforces the upsert invariant
-- is deliberately NOT declared here. _SYSTEM_SCHEMA runs *before* the migration
-- ladder (and, on split-brain future-version DBs, instead of it), so it must be
-- safe against every historical table shape. ``corpus_files`` predates ``path``
-- (table created v82, column added v97): on a legacy DB the CREATE TABLE above
-- is a no-op and an index over the not-yet-added ``path`` column raises
-- BinderException, aborting the whole schema pass before the ALTER can run.
-- _ensure_corpus_path_index() creates it after the ladder instead.

-- corpus_chunks: prose-document chunks + embedding vector.
-- embedding FLOAT[384]: fixed-size array for array_cosine_similarity.
-- Repo deferred to Retrieval slice; table created here so a single
-- migration covers all Collections schema.
CREATE TABLE IF NOT EXISTS corpus_chunks (
    id VARCHAR PRIMARY KEY,
    corpus_id VARCHAR NOT NULL,
    file_id VARCHAR NOT NULL,
    ordinal INTEGER,
    text VARCHAR,
    embedding FLOAT[384],
    section_path VARCHAR,
    page INTEGER,
    bbox VARCHAR,
    metadata VARCHAR,
    created_at TIMESTAMP DEFAULT current_timestamp
);

-- knowledge_digests: v89 (K4, #799). Admin-defined digest documents the
-- scheduler regenerates via LLM when their source corpora change.
-- status: pending (never generated) | fresh | stale (sources changed but
-- regeneration failed/deferred — output_md is the last good generation,
-- status_reason says why). source_corpus_ids is a JSON array stored as text.
CREATE TABLE IF NOT EXISTS knowledge_digests (
    id                 VARCHAR PRIMARY KEY,
    slug               VARCHAR NOT NULL UNIQUE,
    title              VARCHAR NOT NULL,
    instructions       TEXT NOT NULL,
    source_corpus_ids  VARCHAR,
    output_md          TEXT,
    source_fingerprint VARCHAR,
    generated_at       TIMESTAMP,
    model              VARCHAR,
    status             VARCHAR DEFAULT 'pending',
    status_reason      VARCHAR,
    created_by         VARCHAR,
    created_at         TIMESTAMP DEFAULT current_timestamp,
    updated_at         TIMESTAMP DEFAULT current_timestamp
);

-- chat_broker_tickets: v90. Opaque, short-lived tickets minted by the chat
-- sandbox secret broker (ticket_repo().mint) so a sandboxed chat agent never
-- holds the real ANTHROPIC_API_KEY / AGNES_TOKEN — only an opaque token the
-- broker resolves server-side. Indexed on session_id so revoke_session can
-- invalidate every outstanding ticket for a session in one statement.
CREATE TABLE IF NOT EXISTS chat_broker_tickets (
    token       VARCHAR PRIMARY KEY,
    session_id  VARCHAR NOT NULL,
    scope       VARCHAR NOT NULL,
    expires_at  TIMESTAMP NOT NULL,
    created_at  TIMESTAMP NOT NULL DEFAULT current_timestamp
);
CREATE INDEX IF NOT EXISTS idx_chat_broker_tickets_session_id ON chat_broker_tickets(session_id);

-- v91: skill lint (store guardrails). store_lint_runs tracks each lint pass
-- (scheduler | admin | publish trigger); store_lint_findings holds the
-- *current* generation of findings per entity (replace-on-relint, no
-- history retained here — dismissals below are the audit trail);
-- store_lint_dismissals is a per-(entity, rule) admin dismissal keyed to the
-- content_hash it was dismissed against, so a content change auto-resets it.
CREATE TABLE IF NOT EXISTS store_lint_runs (
    id               VARCHAR PRIMARY KEY,
    trigger          VARCHAR NOT NULL,  -- 'scheduler' | 'admin' | 'publish'
    started_at       TIMESTAMP NOT NULL,
    finished_at      TIMESTAMP,
    entities_linted  INTEGER NOT NULL DEFAULT 0,
    entities_skipped INTEGER NOT NULL DEFAULT 0,
    findings_count   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS store_lint_findings (
    id           VARCHAR PRIMARY KEY,
    run_id       VARCHAR NOT NULL,
    entity_id    VARCHAR NOT NULL,
    rule_id      VARCHAR NOT NULL,
    severity     VARCHAR NOT NULL,
    message      VARCHAR NOT NULL,
    evidence     VARCHAR DEFAULT '{}',
    doc_url      VARCHAR DEFAULT '',
    content_hash VARCHAR DEFAULT '',
    created_at   TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_store_lint_findings_entity ON store_lint_findings(entity_id);

CREATE TABLE IF NOT EXISTS store_lint_dismissals (
    entity_id     VARCHAR NOT NULL,
    rule_id       VARCHAR NOT NULL,
    dismissed_by  VARCHAR NOT NULL,
    dismissed_at  TIMESTAMP NOT NULL,
    content_hash  VARCHAR NOT NULL,
    PRIMARY KEY (entity_id, rule_id)
);

-- Tracks the last lint per entity even when it produced zero findings, so
-- the unchanged-content skip works for clean skills too (a findings-only
-- read would return nothing after a clean lint and force a re-lint).
CREATE TABLE IF NOT EXISTS store_lint_entity_state (
    entity_id    VARCHAR PRIMARY KEY,
    content_hash VARCHAR NOT NULL,
    run_id       VARCHAR NOT NULL,
    linted_at    TIMESTAMP NOT NULL
);

-- v97: user_journey_state — per-user onboarding "journey" progress
-- (backend foundation for chat-driven onboarding). One row per user;
-- absent row means the caller should treat the user as fresh (defaults
-- live in the repository, not the schema).
CREATE TABLE IF NOT EXISTS user_journey_state (
    user_id             VARCHAR PRIMARY KEY,
    first_asked         BOOLEAN NOT NULL DEFAULT FALSE,
    stack_setup_done    BOOLEAN NOT NULL DEFAULT FALSE,
    explored_stack      BOOLEAN NOT NULL DEFAULT FALSE,
    catalog_discovered  BOOLEAN NOT NULL DEFAULT FALSE,
    use_anywhere        BOOLEAN NOT NULL DEFAULT FALSE,
    agent_created       BOOLEAN NOT NULL DEFAULT FALSE,
    onboarded           BOOLEAN NOT NULL DEFAULT FALSE,
    successful_answers  INTEGER NOT NULL DEFAULT 0,
    updated_at          TIMESTAMP NOT NULL DEFAULT current_timestamp
);

-- glossary_terms: v93. Keboola semantic-glossary import destination
-- (docs/superpowers/specs/2026-07-17-keboola-glossary-import-design.md).
-- id = "keboola/{model_uuid}/{slug(term)}" for Keboola-sourced rows, or
-- admin-chosen for source='manual' rows. see_also is an opaque string
-- list (not resolved/validated against other Metastore types).
CREATE TABLE IF NOT EXISTS glossary_terms (
    id           VARCHAR PRIMARY KEY,
    term         VARCHAR NOT NULL,
    definition   TEXT NOT NULL,
    see_also     VARCHAR[],
    model_uuid   VARCHAR,
    source       VARCHAR NOT NULL DEFAULT 'manual',
    source_ref   VARCHAR,
    created_at   TIMESTAMP DEFAULT current_timestamp,
    updated_at   TIMESTAMP DEFAULT current_timestamp
);

-- v116: semantic_models / semantic_sources / data_package_semantic_models —
-- canonical Apache Ossie semantic-layer documents (see _v115_to_v116).
-- metric_definitions and glossary_terms above remain the flat projections
-- queries actually read; these tables own the document they were derived
-- from.
CREATE TABLE IF NOT EXISTS semantic_models (
    id                VARCHAR PRIMARY KEY,
    slug              VARCHAR NOT NULL,
    name              VARCHAR NOT NULL,
    description       TEXT,
    -- The document exactly as the adapter produced it. Never re-serialized:
    -- round-tripping through a YAML dumper would silently reorder keys and
    -- drop comments, and this column is what `export` hands back out.
    document          TEXT NOT NULL,
    document_json     JSON,
    spec_version      VARCHAR NOT NULL,
    content_hash      VARCHAR NOT NULL,
    source            VARCHAR NOT NULL DEFAULT 'manual',
    source_ref        VARCHAR,
    status            VARCHAR NOT NULL DEFAULT 'valid',
    validation_errors JSON,
    validated_at      TIMESTAMP,
    created_at        TIMESTAMP DEFAULT current_timestamp,
    updated_at        TIMESTAMP DEFAULT current_timestamp
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_semantic_models_origin
    ON semantic_models (source, source_ref, slug);

CREATE TABLE IF NOT EXISTS semantic_sources (
    id               VARCHAR PRIMARY KEY,
    kind             VARCHAR NOT NULL,     -- 'git' | 'upload' | 'connection'
    name             VARCHAR NOT NULL,
    adapter          VARCHAR NOT NULL,     -- 'native' | 'keboola_metastore'
    config           JSON NOT NULL,
    enabled          BOOLEAN DEFAULT TRUE,
    last_sync_at     TIMESTAMP,
    last_sync_status VARCHAR,
    last_sync_error  TEXT,
    created_at       TIMESTAMP DEFAULT current_timestamp,
    updated_at       TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS data_package_semantic_models (
    package_id VARCHAR NOT NULL,
    model_id   VARCHAR NOT NULL,
    PRIMARY KEY (package_id, model_id)
);

-- v94: jobs — durable job queue, the foundation of the worker runtime
-- (wave-2B). This table + JobsRepository/JobsPgRepository cover
-- enqueue/get/list + idempotency dedup only; the claim/lease lifecycle and
-- the worker loop are later tasks in the same wave.
--
-- idempotency_key dedup note: a *partial* unique index
-- (`... WHERE idempotency_key IS NOT NULL AND status IN ('queued',
-- 'running')`) would let a duplicate key be reused once the earlier job
-- leaves queued/running, but DuckDB does not support partial indexes
-- ("Not implemented Error: Creating partial indexes is not supported
-- currently"). Dedup is therefore enforced in JobsRepository.enqueue()
-- (guarded by an in-process lock — safe under DuckDB's single-writer
-- model) rather than at the DB level. `idx_jobs_idem` below is a plain
-- (non-unique) lookup index on this side.
--
-- The Postgres ladder (migrations/versions/0041_jobs_v94.py /
-- src/models/jobs.py) is asymmetric here: it DOES create `idx_jobs_idem`
-- as a partial unique index, because a plain SELECT-then-INSERT in
-- JobsPgRepository would race under READ COMMITTED (two concurrent
-- transactions can both miss each other's uncommitted row). The CONTRACT
-- shared by both backends is the dedup *behavior*, not the index shape.
--
-- `lease_token` is a fresh uuid4 minted by claim_next() on every claim
-- (including a same-worker reclaim of its own previously-abandoned lease).
-- heartbeat()/complete()/fail() guard on `lease_token = ? AND status =
-- 'running'` rather than `leased_by = ?`: all lane slots in one worker
-- process share the same `leased_by` (worker_id = hostname:pid), so a
-- worker_id-only guard cannot tell a stale slot's late call apart from a
-- same-process reclaim of the same job by a DIFFERENT slot — a
-- same-worker double-execution bug, empirically reproduced. `leased_by`
-- is kept for audit/logging only.
CREATE TABLE IF NOT EXISTS jobs (
    id                VARCHAR PRIMARY KEY,
    kind              VARCHAR NOT NULL,
    payload_json      VARCHAR NOT NULL DEFAULT '{}',
    status            VARCHAR NOT NULL DEFAULT 'queued',
    priority          INTEGER NOT NULL DEFAULT 0,
    run_after         TIMESTAMP,
    attempts          INTEGER NOT NULL DEFAULT 0,
    max_attempts      INTEGER NOT NULL DEFAULT 3,
    lease_expires_at  TIMESTAMP,
    leased_by         VARCHAR,
    lease_token       VARCHAR,
    idempotency_key   VARCHAR,
    error             VARCHAR,
    created_at        TIMESTAMP NOT NULL,
    started_at        TIMESTAMP,
    finished_at       TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(status, priority, run_after);
CREATE INDEX IF NOT EXISTS idx_jobs_idem ON jobs(idempotency_key);

"""
    + _AGENTS_CREATE_SQL
    + """
CREATE TABLE IF NOT EXISTS agent_scope (
    agent_id  VARCHAR NOT NULL,
    item_type VARCHAR NOT NULL,
    item_id   VARCHAR NOT NULL,
    PRIMARY KEY (agent_id, item_type, item_id)
);

CREATE TABLE IF NOT EXISTS llm_usage (
    id                    VARCHAR PRIMARY KEY,
    agent_id              VARCHAR,
    user_id               VARCHAR,
    session_id            VARCHAR,
    model                 VARCHAR,
    input_tokens          BIGINT DEFAULT 0,
    output_tokens         BIGINT DEFAULT 0,
    cache_read_tokens     BIGINT DEFAULT 0,
    cache_creation_tokens BIGINT DEFAULT 0,
    created_at            TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS agent_scope_snapshots (
    id              VARCHAR PRIMARY KEY,
    session_id      VARCHAR NOT NULL,
    agent_id        VARCHAR NOT NULL,
    effective_scope TEXT NOT NULL,
    created_at      TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key           VARCHAR NOT NULL,
    owner_user_id VARCHAR NOT NULL,
    agent_id      VARCHAR NOT NULL,
    request_hash  VARCHAR NOT NULL,
    response_body TEXT,
    status_code   INTEGER,
    created_at    TIMESTAMP DEFAULT current_timestamp,
    expires_at    TIMESTAMP,
    PRIMARY KEY (key, owner_user_id, agent_id)
);

-- v102: agent webhooks + artifacts (agent-api V1b). No secondary indexes
-- (ART-index incident — see _v94_to_v95).
CREATE TABLE IF NOT EXISTS agent_webhooks (
    id                   VARCHAR PRIMARY KEY,
    agent_id             VARCHAR NOT NULL,
    owner_user_id        VARCHAR NOT NULL,
    url                  VARCHAR NOT NULL,
    secret               VARCHAR NOT NULL,
    events               VARCHAR NOT NULL DEFAULT 'job.completed,job.failed',
    active               BOOLEAN NOT NULL DEFAULT TRUE,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    disabled_at          TIMESTAMP,
    created_at           TIMESTAMP DEFAULT current_timestamp,
    updated_at           TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS agent_artifacts (
    id            VARCHAR PRIMARY KEY,
    session_id    VARCHAR NOT NULL,
    agent_id      VARCHAR,
    owner_user_id VARCHAR NOT NULL,
    filename      VARCHAR NOT NULL,
    object_key    VARCHAR NOT NULL,
    size_bytes    BIGINT NOT NULL DEFAULT 0,
    content_type  VARCHAR,
    md5           VARCHAR,
    created_at    TIMESTAMP DEFAULT current_timestamp
);

-- v103: per-agent private memory notebook (agent-api V1c). No secondary
-- indexes (ART-index incident — see _v94_to_v95).
CREATE TABLE IF NOT EXISTS agent_memories (
    id                VARCHAR PRIMARY KEY,
    agent_id          VARCHAR NOT NULL,
    owner_user_id     VARCHAR NOT NULL,
    content           TEXT NOT NULL,
    source_session_id VARCHAR,
    status            VARCHAR NOT NULL DEFAULT 'pending',
    created_at        TIMESTAMP DEFAULT current_timestamp,
    activated_at      TIMESTAMP,
    archived_at       TIMESTAMP
);

-- v119: agent_schedules — scheduled runs for agent profiles (design doc
-- docs/superpowers/specs/2026-08-17-agent-schedules-design.md). Schedules
-- die with the agent (repo `delete_for_agent`, called from the agent-delete
-- cascade). No secondary indexes (ART-index incident — see _v94_to_v95).
CREATE TABLE IF NOT EXISTS agent_schedules (
    id          VARCHAR PRIMARY KEY,
    agent_id    VARCHAR NOT NULL,
    name        VARCHAR NOT NULL,
    schedule    VARCHAR NOT NULL,
    prompt      TEXT NOT NULL,
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    last_run_at TIMESTAMP,
    last_status VARCHAR,
    last_job_id VARCHAR,
    created_at  TIMESTAMP DEFAULT current_timestamp,
    updated_at  TIMESTAMP DEFAULT current_timestamp,
    UNIQUE (agent_id, name)
);

-- v109: outbound MCP OAuth sources (spec
-- docs/superpowers/specs/2026-07-30-mcp-oauth-sources-design.md). Same DDL as
-- _v108_to_v109 so the split-brain self-heal can recreate them; the migration
-- keeps IF NOT EXISTS so both paths coexist. No secondary indexes (ART-index
-- incident — see _v94_to_v95).
CREATE TABLE IF NOT EXISTS mcp_source_oauth_clients (
    source_id                     VARCHAR PRIMARY KEY,
    issuer                        VARCHAR NOT NULL,
    client_id                     VARCHAR NOT NULL,
    client_secret_enc             BLOB,
    registration_access_token_enc BLOB,
    authorization_endpoint        VARCHAR NOT NULL,
    token_endpoint                VARCHAR NOT NULL,
    scopes                        VARCHAR,
    created_at                    TIMESTAMP NOT NULL DEFAULT current_timestamp,
    updated_at                    TIMESTAMP NOT NULL DEFAULT current_timestamp
);
CREATE TABLE IF NOT EXISTS mcp_user_oauth_tokens (
    source_id         VARCHAR NOT NULL,
    user_id            VARCHAR NOT NULL,
    access_token_enc   BLOB NOT NULL,
    refresh_token_enc  BLOB,
    expires_at         TIMESTAMP,
    scopes             VARCHAR,
    created_at         TIMESTAMP NOT NULL DEFAULT current_timestamp,
    updated_at         TIMESTAMP NOT NULL DEFAULT current_timestamp,
    PRIMARY KEY (source_id, user_id)
);
CREATE TABLE IF NOT EXISTS mcp_oauth_flows (
    nonce              VARCHAR PRIMARY KEY,
    source_id          VARCHAR NOT NULL,
    user_id            VARCHAR NOT NULL,
    pkce_verifier_enc  BLOB NOT NULL,
    created_at         TIMESTAMP NOT NULL DEFAULT current_timestamp
);
"""
    + _DATA_APPS_CREATE_SQL
)


import threading  # noqa: E402

_system_db_lock = threading.Lock()
_system_db_conn: duckdb.DuckDBPyConnection | None = None
_system_db_path: str | None = None

# Mirror the system-DB singleton pattern for the analytics DB. Pre-#163,
# `get_analytics_db()` opened a fresh `duckdb.connect()` on every call —
# most callers don't `.close()` the returned handle, so each leaked
# connection held a WAL ref + FD until GC kicked in. Under load this
# manifested as "too many open files" or DuckDB lock contention on the
# analytics DB. Singleton + cursor-per-call (mirrors `get_system_db()`
# above) means callers that close the cursor only close the cursor —
# the underlying connection stays.
_analytics_db_lock = threading.Lock()
_analytics_db_conn: duckdb.DuckDBPyConnection | None = None
_analytics_db_path: str | None = None

# DuckDB per-connection memory budgets.
#
# DuckDB enforces ``memory_limit`` PER CONNECTION, not per process. In a
# memory-constrained container (e.g. a 4 GiB cgroup) the live connections
# must sum under the cap or the kernel OOM-kills the whole process. DuckDB
# 1.5 is cgroup-aware — a fresh connection defaults to ~80% of the cgroup
# limit — so a single *uncapped* connection can exceed the cap on its own.
# We give each connection an explicit conservative budget:
#
#   system (singleton)             1 GiB    metadata + telemetry aggregations
#   analytics (singleton)          1.5 GiB  working set over parquet views
#   analytics readonly (per req)   1 GiB    one analyst's heavy query
#
# Steady state — the two singletons plus one in-flight readonly query —
# is 3.5 GiB, under a 4 GiB cap with host headroom. The readonly path is
# per-request and unbounded in count (FastAPI threadpool), so a burst of
# concurrent analyst queries can momentarily exceed the cap; the
# ``temp_directory`` disk spill below is the backstop — an over-budget
# query spills to disk (or raises a clean DuckDB OOM) instead of growing
# process RSS. For very memory-constrained, high-concurrency deployments,
# tune AGNES_THREADPOOL_SIZE down too. (Bounding readonly connection
# concurrency directly is a possible follow-up — out of scope here.)
#
# See docs/superpowers/specs/2026-06-01-system-duckdb-resilience-design.md.
_SYSTEM_DB_MEMORY_LIMIT = "1GB"
_ANALYTICS_DB_MEMORY_LIMIT = "1500MB"
_ANALYTICS_RO_MEMORY_LIMIT = "1GB"
_DUCKDB_THREADS = 2
_DUCKDB_MAX_TEMP_DIR_SIZE = "10GB"


def _get_data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "./data"))


def _get_state_dir() -> Path:
    """Return path to writable state directory.

    Resolution order:
      1. STATE_DIR env var (explicit override).
      2. ${DATA_DIR}/state (default — current behavior).

    Use the explicit override when the deployer wants state on a
    separate disk mounted in parallel with /data rather than nested
    inside it. See docs/state-dir.md.
    """
    state = os.environ.get("STATE_DIR", "")
    if state:
        return Path(state)
    return _get_data_dir() / "state"


def _peek_schema_version(snapshot_path: Path) -> int:
    """Open a DuckDB snapshot read-only and return its
    ``MAX(schema_version.version)``.

    Read-only mode bypasses WAL replay entirely — even if the snapshot
    has its own stale WAL, the read-only handle ignores it. Any
    ``duckdb.Error`` (table missing, file corrupt, permission denied)
    is treated conservatively as version 0, so an unreadable snapshot
    fails the freshness check in :func:`_try_open_system_db` and ends
    in the refusal path. Defensive: never returns -1 / None / raises.
    """
    try:
        conn = _open_duckdb(str(snapshot_path), read_only=True)
        try:
            row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        finally:
            conn.close()
    except duckdb.Error:
        return 0


def _system_self_heal_enabled() -> bool:
    """Whether the ART-index self-heal on open is active (default: yes).

    ``AGNES_DB_SELF_HEAL=0`` (or ``false``/``no``) turns it off — e.g. to
    freeze a corrupt DB for forensics instead of auto-rebuilding it.
    """
    return os.environ.get("AGNES_DB_SELF_HEAL", "1").strip().lower() not in {"0", "false", "no"}


# The runtime signature DuckDB raises when an on-disk ART (PK/UNIQUE)
# index has diverged from its base table. Either substring means the
# file itself needs an EXPORT/IMPORT rebuild — a reopen cannot fix it.
_ART_CORRUPTION_MARKERS = (
    "Failed to delete all rows from index",
    "database has been invalidated",
)


def _probe_art_integrity(
    conn: duckdb.DuckDBPyConnection,
) -> tuple[bool, str | None]:
    """Canary for on-disk ART-index corruption.

    For each table backed by a PRIMARY KEY / UNIQUE (ART) index, delete one
    row *inside a transaction and roll it back*. The delete forces DuckDB to
    remove the row's key from the index — the exact operation that fails
    (``Failed to delete all rows from index``) when the index is corrupt —
    while the ROLLBACK guarantees no data changes. Empty and index-less
    tables are skipped (nothing to corrupt).

    Returns ``(True, None)`` when every probe succeeds. Returns
    ``(False, detail)`` when a probe raises the ART-corruption signature —
    at which point ``conn`` is invalidated and the caller must discard it
    and rebuild. Any *other* DuckDB error on a given table (e.g. a
    foreign-key ``ConstraintException`` from a referenced row) is caught,
    that table's probe transaction is rolled back, and the loop moves on
    to the next table — this canary's sole job is spotting the ART-
    corruption signature, so it must never itself abort the DB open or
    trigger a needless rebuild over an unrelated per-table quirk
    (Devin Review, PR #948 — this docstring previously said such errors
    are "re-raised", which never matched the implementation below).
    """
    # Schema-qualify: duckdb_constraints() spans every schema (the FTS
    # extension grows fts_main_* schemas with their own PK'd internal
    # tables), so resolving a bare table name via search_path is fragile.
    # DISTINCT (schema, table) + a qualified identifier probes each real
    # constraint-backed table exactly once, wherever it lives.
    indexed = conn.execute(
        "SELECT DISTINCT schema_name, table_name FROM duckdb_constraints() "
        "WHERE constraint_type IN ('PRIMARY KEY', 'UNIQUE')"
    ).fetchall()
    for schema, table in indexed:
        ident = '"' + str(schema).replace('"', '""') + '"."' + str(table).replace('"', '""') + '"'
        try:
            row = conn.execute(f"SELECT rowid FROM {ident} LIMIT 1").fetchone()
            if row is None:
                # Empty table: no index entries to delete, so the canary
                # can't exercise (and thus can't detect) a torn index here.
                # Known coverage gap — corruption localized to a currently
                # empty PK/UNIQUE table surfaces on its next real write.
                continue
            conn.execute("BEGIN")
            conn.execute(f"DELETE FROM {ident} WHERE rowid = {int(row[0])}")
            conn.execute("ROLLBACK")
        except duckdb.Error as e:
            if any(m in str(e) for m in _ART_CORRUPTION_MARKERS):
                return (False, str(e))
            # Anything else — a foreign-key ConstraintException on a
            # referenced row, or any other engine quirk — is NOT evidence of
            # index corruption. The probe must never break the DB open nor
            # trigger a needless rebuild: roll back this table's txn and move
            # on. Its sole job is spotting the corruption signature.
            try:
                conn.execute("ROLLBACK")
            except duckdb.Error:
                pass
            continue
    return (True, None)


def _probe_and_heal_art_index(conn: duckdb.DuckDBPyConnection, db_path: str) -> duckdb.DuckDBPyConnection:
    """Run :func:`_probe_art_integrity` on an already-open connection and
    transparently rebuild via :func:`_rebuild_system_db` if it's corrupt.

    Shared by every successful-open path in :func:`_try_open_system_db` —
    the clean open AND both WAL-recovery paths (STEP A salvage, STEP B
    snapshot restore). The same abrupt termination that leaves a dirty WAL
    can also tear the on-disk ART index; a DB that needed WAL recovery is
    not evidence the index survived (Devin Review, PR #948). Returns the
    connection, possibly rebuilt.
    """
    if not _system_self_heal_enabled():
        return conn
    healthy, detail = _probe_art_integrity(conn)
    if healthy:
        return conn
    logger.critical(
        "system.duckdb ART-index corruption detected on open (%s). A "
        "restart cannot fix on-disk index corruption; rebuilding via "
        "EXPORT/IMPORT (original preserved as .broken.<ts>).",
        (detail or "").split("\n", 1)[0][:200],
    )
    try:
        conn.close()
    except Exception:  # noqa: BLE001 - the handle is already invalidated
        pass
    _rebuild_system_db(db_path)
    conn = _open_duckdb(db_path)
    healthy_again, detail_again = _probe_art_integrity(conn)
    if not healthy_again:
        logger.critical(
            "system.duckdb STILL reports index corruption after rebuild (%s) — manual recovery required.",
            (detail_again or "").split("\n", 1)[0][:200],
        )
    return conn


def _rebuild_system_db(db_path: str) -> Path:
    """Rebuild ``system.duckdb`` in place via EXPORT/IMPORT, healing a
    corrupt on-disk ART index. Returns the ``.broken.<ts>`` path the
    corrupt original was quarantined to.

    The base-table data is fully readable (only the index is broken), so
    ``EXPORT DATABASE`` (full table scans, no index use) dumps every row to
    parquet, and ``IMPORT DATABASE`` into a fresh file re-creates every
    table and rebuilds every index from scratch. The rebuilt file is
    assembled completely *before* the original is touched. The corrupt
    original is then preserved as ``.broken.<ts>`` (a copy, chmod ``0o600``)
    and the rebuilt file swapped in with an atomic ``os.replace`` — so
    ``db_path`` is never momentarily absent for a concurrent opener. On any
    failure before the swap the original is left untouched.

    Sibling files (``.export``/``.rebuild`` scratch) live next to
    ``db_path`` so the final swap is a same-filesystem rename.
    """
    export_dir = f"{db_path}.export.{os.getpid()}"
    rebuilt = f"{db_path}.rebuild.{os.getpid()}"
    for stale in (export_dir, rebuilt):
        if os.path.isdir(stale):
            shutil.rmtree(stale, ignore_errors=True)
        elif os.path.exists(stale):
            os.remove(stale)

    # 1. Export the readable data from the (index-corrupt) live file. A
    # normal (read-write) open, not read-only: read-only bypasses WAL
    # replay entirely (see _peek_schema_version's docstring), so any
    # transactions committed since the last checkpoint but still only in
    # ``db_path + ".wal"`` would silently be missing from the export —
    # the opposite of this function's "data preserved" contract (Devin
    # Review, PR #948). A normal open is safe here: the whole premise of
    # this self-heal path is that the file OPENS fine on a plain
    # read-write connection — only an explicit write against the corrupt
    # index fails — and EXPORT DATABASE itself only does full table
    # scans, no index writes, so it can't re-trigger the corruption.
    src = _open_duckdb(db_path)
    try:
        src.execute(f"EXPORT DATABASE '{export_dir}' (FORMAT PARQUET)")
    finally:
        src.close()

    # 2. Import into a fresh file — this rebuilds all indexes.
    dst = _open_duckdb(rebuilt)
    try:
        dst.execute(f"IMPORT DATABASE '{export_dir}'")
        dst.execute("CHECKPOINT")
    finally:
        dst.close()

    # 3. Swap atomically. Preserve the corrupt original as .broken.<ts> with
    #    a COPY (so db_path is never momentarily absent), then os.replace()
    #    the rebuilt file over it — an atomic same-filesystem rename. A
    #    concurrent opener (a second app instance mid-rolling-restart, or an
    #    operator running `agnes admin db repair`) therefore always sees
    #    either the old or the new file, never a missing path — which
    #    duckdb.connect() would silently re-create as an empty DB whose writes
    #    the rename then orphans. (This is why the rebuild path does NOT use
    #    _move_to_broken, which moves db_path aside and reopens that window;
    #    the WAL-recovery branches can, as they replace the file wholesale.)
    wal_path = Path(db_path + ".wal")
    broken = Path(db_path + f".broken.{int(time.time())}")
    n = 1  # 1s timestamp isn't unique across back-to-back rebuilds
    while broken.exists():
        broken = Path(db_path + f".broken.{int(time.time())}.{n}")
        n += 1
    shutil.copy2(db_path, broken)
    try:
        os.chmod(broken, 0o600)  # holds password hashes / PAT rows / audit log
    except OSError:
        pass
    if wal_path.exists():
        broken_wal = str(broken) + ".wal"
        shutil.copy2(str(wal_path), broken_wal)
        try:
            os.chmod(broken_wal, 0o600)
        except OSError:
            pass
        # Drop the corrupt file's stale WAL so it can't replay onto the
        # rebuilt DB (already CHECKPOINTed above; it needs no WAL).
        wal_path.unlink()
    os.replace(rebuilt, db_path)
    try:
        os.chmod(db_path, 0o600)
    except OSError:
        pass
    shutil.rmtree(export_dir, ignore_errors=True)
    logger.warning(
        "system.duckdb rebuilt via EXPORT/IMPORT; corrupt original preserved at %s",
        broken,
    )
    return broken


def _try_open_system_db(db_path: str) -> duckdb.DuckDBPyConnection:
    """Open ``system.duckdb``. If DuckDB's WAL replay raises an
    ``INTERNAL Error`` from ``ReplayAlter`` (a known failure mode when a
    container is killed mid-migration window with an unflushed
    ``ALTER TABLE … ADD COLUMN`` op in the WAL), fall back to the
    ``system.duckdb.pre-migrate`` snapshot taken at the start of the
    most recent migration. The migration ladder is idempotent, so the
    second start re-runs it and ends up at the same SCHEMA_VERSION
    cleanly. Without this fallback, an operator hits an unhealthy
    instance after every mid-migration crash and has to restore the
    snapshot by hand — even though the snapshot is right there.

    Only fires on the specific WAL-replay error class to avoid masking
    legitimate corruption (operator-edited DB, disk failure, etc.).

    Layered on top: once ANY path above lands an open connection — clean
    open, STEP A salvage, or STEP B snapshot restore — an ART-index
    integrity probe (:func:`_probe_art_integrity`, via the shared
    :func:`_probe_and_heal_art_index`) runs on it. On-disk ART (PRIMARY
    KEY / UNIQUE) index corruption — caused by a termination that bypasses
    the graceful CHECKPOINT-and-close path (OOM SIGKILL, VM ``-replace``
    destroy, host crash) — lets the file OPEN cleanly but makes the first
    index write fail with ``Failed to delete all rows from index`` and
    invalidate the whole connection. A restart cannot heal it; only an
    EXPORT/IMPORT rebuild can. The same abrupt termination that dirties
    the WAL can also tear the ART index, so a WAL-recovered DB gets the
    same probe, not just a cleanly-opened one (Devin Review, PR #948).
    When the probe detects that signature the DB is transparently rebuilt
    (:func:`_rebuild_system_db`) and reopened. Disable with
    ``AGNES_DB_SELF_HEAL=0``.
    """
    try:
        conn = _open_duckdb(db_path)
    except duckdb.Error as e:
        msg = str(e)
        is_wal_replay = (
            "Failure while replaying WAL" in msg
            or "ReplayAlter" in msg
            or "GetDefaultDatabase with no default database set" in msg
        )
        if not is_wal_replay:
            raise
        wal_path = Path(db_path + ".wal")

        # STEP A — salvage the live file. Its last checkpoint is almost
        # always newer than any pre-migrate snapshot (checkpoints run
        # continuously; the snapshot is captured only at migrations).
        # Discard ONLY the unreplayable WAL — DuckDB couldn't apply it
        # anyway — and reopen the file at its last checkpoint. This loses
        # at most the transactions written since that checkpoint, never
        # the days of admin state a stale-snapshot rollback would drop.
        # It also subsumes the mid-migration case: an uncommitted ALTER
        # lives in the discarded WAL, so the file is at the pre-migration
        # version and the idempotent ladder re-runs forward on this start.
        salvaged = _salvage_discard_wal(db_path, wal_path, original_error=e)
        if salvaged is not None:
            return _probe_and_heal_art_index(salvaged, db_path)

        # STEP B — the live file itself won't open. Fall back to the
        # pre-migrate snapshot (with the #379 version guard below). The
        # WAL was already moved aside by Step A, so _move_to_broken here
        # just relocates the unreadable DB file.
        snapshot = Path(db_path).parent / "system.duckdb.pre-migrate"
        if not snapshot.exists():
            logger.error(
                "WAL replay failed, live file unreadable, and no pre-migrate "
                "snapshot at %s — manual recovery required.",
                snapshot,
            )
            raise

        # #379: refuse auto-recovery if the snapshot version doesn't
        # match SCHEMA_VERSION exactly. The migration ladder is
        # idempotent for schema but not for data; re-running it against
        # a stale snapshot (peek < SCHEMA_VERSION) silently drops every
        # row added since the snapshot was captured. The mirror case
        # (peek > SCHEMA_VERSION) is just as bad: an operator rolled
        # the code back, but the snapshot was captured at a later
        # migration transition — auto-recovery would copy the future
        # snapshot in and the next start's _ensure_schema would land
        # in the split-brain "current > target" branch. Both directions
        # mean data corruption; the only safe move is to refuse and
        # surface the version mismatch loudly. The broken DB + WAL are
        # preserved either way for forensics.
        snapshot_version = _peek_schema_version(snapshot)
        if snapshot_version != SCHEMA_VERSION:
            broken = _move_to_broken(db_path, wal_path)
            direction = "stale" if snapshot_version < SCHEMA_VERSION else "future"
            risk = (
                f"would re-run the migration ladder and silently drop all rows added since v{snapshot_version}"
                if snapshot_version < SCHEMA_VERSION
                else (f"would land the DB at v{snapshot_version} under a v{SCHEMA_VERSION} binary (split-brain)")
            )
            logger.critical(
                "REFUSING auto-recovery: pre-migrate snapshot %s "
                "(snapshot v%d, target v%d). Auto-recovery %s. Broken "
                "DB preserved at %s; broken WAL at %s.wal if it existed. "
                "To accept the snapshot anyway and discard the broken "
                "files, manually run: cp %s %s",
                direction,
                snapshot_version,
                SCHEMA_VERSION,
                risk,
                broken,
                broken,
                snapshot,
                db_path,
                exc_info=e,
            )
            raise RuntimeError(
                f"pre-migrate snapshot {direction} "
                f"(v{snapshot_version} vs target v{SCHEMA_VERSION}); "
                f"auto-recovery refused. Broken DB at {broken}. "
                f"Manual recovery: cp {snapshot} {db_path}"
            )

        logger.warning(
            "WAL replay failed (%s) — auto-restoring from pre-migrate "
            "snapshot %s. The migration ladder will re-run on this start.",
            msg.split("\n", 1)[0][:200],
            snapshot,
        )
        # Move (not copy) the broken DB aside so an operator can post-
        # mortem if needed. The pre-migrate snapshot becomes the new
        # main DB; the WAL is dropped (its content is what failed to
        # replay).
        _move_to_broken(db_path, wal_path)
        shutil.copy2(str(snapshot), db_path)
        # Re-open. If THIS also fails, propagate — auto-recovery has
        # exhausted its options. Same ART-index probe as the other paths:
        # the WAL-replay failure that forced this fallback can co-occur
        # with on-disk index corruption from the same abrupt termination.
        return _probe_and_heal_art_index(_open_duckdb(db_path), db_path)
    else:
        # File opened cleanly. Guard against on-disk ART-index corruption
        # (a different failure mode from WAL replay: the file opens, then
        # the first index write fails at runtime and invalidates the
        # connection — a restart cannot heal it).
        return _probe_and_heal_art_index(conn, db_path)


def _salvage_discard_wal(
    db_path: str, wal_path: Path, *, original_error: Exception
) -> duckdb.DuckDBPyConnection | None:
    """Discard an unreplayable WAL and reopen the DB at its last checkpoint.

    Returns the open connection on success, or ``None`` if the database
    file itself won't open (the caller then falls back to the pre-migrate
    snapshot). The discarded WAL is moved to ``<db>.wal.discarded.<ts>``
    (chmod ``0o600`` — it can hold uncommitted password/PAT writes) and
    preserved for forensics: its content is exactly what DuckDB failed to
    replay.
    """
    if wal_path.exists():
        discarded = Path(str(db_path) + f".wal.discarded.{int(time.time())}")
        try:
            shutil.move(str(wal_path), str(discarded))
            try:
                os.chmod(discarded, 0o600)
            except OSError:
                pass  # best-effort; preservation matters more than mode
        except OSError as move_err:
            logger.error(
                "WAL salvage: could not move WAL aside (%s); cannot reopen",
                move_err,
            )
            return None
    try:
        # Route through `_open_duckdb` so the salvage reopen inherits the
        # same `SET GLOBAL TimeZone='UTC'` pin every other connection gets
        # (frontend timezone fix, #473) — otherwise the WAL-salvage path
        # would silently drop back to the host's local zone.
        conn = _open_duckdb(db_path)
    except duckdb.Error as reopen_err:
        logger.warning(
            "WAL salvage reopen failed (%s); falling back to pre-migrate snapshot",
            reopen_err,
        )
        return None
    logger.warning(
        "WAL replay failed (%s) — discarded the unreplayable WAL and reopened "
        "system.duckdb at its last checkpoint. Transactions written since that "
        "checkpoint are lost; admin state up to the checkpoint is intact.",
        str(original_error).split("\n", 1)[0][:200],
    )
    return conn


def _move_to_broken(db_path: str, wal_path: Path) -> Path:
    """Move the broken DB (+ WAL if present) aside to ``.broken.<ts>``.

    Shared by both branches of :func:`_try_open_system_db` (refusal and
    happy-path recovery), and can now fire twice in one call chain: STEP
    B quarantines the WAL-replay-failed file, then — if the restored
    pre-migrate snapshot ALSO fails the ART probe — ``_rebuild_system_db``
    quarantines it too (Devin Review, PR #948). The preserved files are
    chmod'd to ``0o600`` because ``system.duckdb`` holds argon2 password
    hashes, personal-access-token rows, and the audit log — ``shutil.move``
    inherits the source mode (typically ``0o644`` under default umask), so
    a stale ``.broken.*`` would be world-readable on its way out. The
    containing ``state/`` directory is usually ``0o700``, but defense
    in depth matters: backups, container volumes, and tab-completion
    mistakes can all surface the file. Returns the chosen broken path.
    """
    broken = Path(db_path + f".broken.{int(time.time())}")
    # Collision guard: the 1-second timestamp resolution isn't unique
    # across two calls milliseconds apart in the same synchronous chain
    # (STEP B → failed re-probe → rebuild, above). shutil.move silently
    # overwrites an existing destination file, which would clobber an
    # earlier quarantine and destroy forensics data — append a counter
    # instead of overwriting.
    n = 1
    while broken.exists():
        broken = Path(db_path + f".broken.{int(time.time())}.{n}")
        n += 1
    shutil.move(db_path, str(broken))
    try:
        os.chmod(broken, 0o600)
    except OSError:
        pass  # best-effort; preservation is more important than mode
    if wal_path.exists():
        broken_wal = str(broken) + ".wal"
        shutil.move(str(wal_path), broken_wal)
        try:
            os.chmod(broken_wal, 0o600)
        except OSError:
            pass
    return broken


def _apply_memory_caps(conn: duckdb.DuckDBPyConnection, memory_limit: str, *, label: str) -> None:
    """Apply defensive memory caps + disk-spill settings to *conn*.

    DuckDB ``memory_limit`` is per-connection; this keeps any single
    connection from growing the process past the container cgroup cap
    (see the ``_*_MEMORY_LIMIT`` constants). Best-effort: a failing PRAGMA
    (read-only DB, in-memory DB, older DuckDB) is logged and skipped so
    the connection stays usable on defaults. The essential caps
    (memory_limit/threads) are applied in their own try so an optional
    temp-spill failure can't undo them.
    """
    try:
        conn.execute(f"SET memory_limit='{memory_limit}'")
        conn.execute(f"SET threads={_DUCKDB_THREADS}")
        conn.execute("SET preserve_insertion_order=false")
    except Exception as e:
        logger.warning("%s: SET memory/threads failed (%s); defaults remain", label, e)
    # Disk spill: a query that exceeds its memory budget spills to disk
    # (or raises a clean DuckDB error) instead of OOM-killing the process.
    try:
        tmp = _get_state_dir() / "duckdb-tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        conn.execute(f"SET temp_directory='{tmp}'")
        conn.execute(f"SET max_temp_directory_size='{_DUCKDB_MAX_TEMP_DIR_SIZE}'")
    except Exception as e:
        logger.debug("%s: temp_directory spill setup failed (%s)", label, e)


def cleanup_orphaned_temp_files(temp_dir=None, *, min_age_s: float = 300.0) -> tuple[int, int]:
    """Remove orphaned DuckDB spill files from the temp_directory.

    DuckDB does not remove ``duckdb_temp_storage_*`` files when the
    process dies hard (SIGKILL, crash, container stop timeout); they
    accumulate as multi-GB dead weight across incidents. Only this
    process ever opens DuckDB against ``{STATE_DIR}/duckdb-tmp`` (the
    scheduler sidecar is a pure HTTP clock — see
    ``services/scheduler/__main__.py``), so at process startup every
    matching file is an orphan of a dead process. ``min_age_s`` is the
    safety margin that makes the call-site ordering irrelevant: a file
    the booting process itself just spilled is seconds old and is left
    alone, while incident leftovers are hours to days old.

    Best-effort: an unremovable file is skipped, never raised. Returns
    ``(files_removed, bytes_freed)``.
    """
    d = Path(temp_dir) if temp_dir is not None else _get_state_dir() / "duckdb-tmp"
    removed = 0
    freed = 0
    if not d.is_dir():
        return (0, 0)
    cutoff = time.time() - max(min_age_s, 0)
    for f in d.glob("duckdb_temp_storage_*"):
        try:
            st = f.stat()
            if st.st_mtime > cutoff:
                continue
            size = st.st_size
            f.unlink()
        except OSError:
            continue
        removed += 1
        freed += size
    if removed:
        logger.info(
            "cleaned %d orphaned DuckDB spill file(s), %d bytes freed, from %s",
            removed,
            freed,
            d,
        )
    return (removed, freed)


def get_system_db() -> duckdb.DuckDBPyConnection:
    """Get a connection to the system state database.

    Uses a single shared connection per DATA_DIR to avoid DuckDB lock
    conflicts between the main app and background tasks. Returns a cursor
    so callers can safely close() it without closing the underlying connection.

    HARD INVARIANT: on a Postgres instance (``use_pg()``) the system state
    lives in Postgres, not the system DuckDB — opening it would create a stale
    ``state/system.duckdb`` file and read/write the wrong backend. This raises
    ``RuntimeError`` there. Safe because the only sanctioned callers gate on
    ``not use_pg()``: the DUCKDB arm of the repository factory
    (``src/repositories/__init__.py``), the ``else`` branch in
    ``connectors/internal/access.py``, and all CLI / scripts / tests. Every
    request/startup opener has been moved behind a ``use_pg()`` guard or
    rerouted through the repository factory.
    """
    from src.repositories import use_pg

    if use_pg():
        raise RuntimeError(
            "system DuckDB must not be opened on a Postgres instance — "
            "route system-state reads through the src.repositories factory "
            "(use_pg() is true)"
        )

    global _system_db_conn, _system_db_path
    db_path = str(_get_state_dir() / "system.duckdb")

    with _system_db_lock:
        if _system_db_conn is None or _system_db_path != db_path:
            # Close old connection if DATA_DIR changed (e.g., in tests)
            if _system_db_conn is not None:
                try:
                    _system_db_conn.close()
                except Exception:
                    pass
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            _system_db_conn = _try_open_system_db(db_path)
            # Cap BEFORE _ensure_schema so migrations + the on-demand FTS
            # index rebuild (src/fts.py) run under the budget too. The
            # system DB was missed by the analytics-only cap in PR #434 —
            # an uncapped singleton was the dominant allocator behind the
            # 4 GiB-cgroup OOM loop.
            _apply_memory_caps(_system_db_conn, _SYSTEM_DB_MEMORY_LIMIT, label="get_system_db")
            _system_db_path = db_path
            _ensure_schema(_system_db_conn)
        return _maybe_instrument(_system_db_conn.cursor(), "system")


_operational_db_conn: duckdb.DuckDBPyConnection | None = None
_operational_db_path: str | None = None

# DuckDB-local operational tables that are deliberately NOT part of the
# dual-backend repository layer — transient state with a short TTL and no
# Postgres mirror. They live in the dedicated operational DuckDB (never the
# system DuckDB, which must not be opened on a Postgres instance).
_OPERATIONAL_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS cli_auth_codes (
    code_hash   VARCHAR PRIMARY KEY,
    user_id     VARCHAR NOT NULL,
    email       VARCHAR NOT NULL,
    created_at  TIMESTAMP NOT NULL DEFAULT current_timestamp,
    expires_at  TIMESTAMP NOT NULL,
    consumed_at TIMESTAMP
);
"""


def get_operational_db() -> duckdb.DuckDBPyConnection:
    """Get a connection to the ephemeral DuckDB-local operational DB.

    Some operational tables are transient state with a short TTL and NO
    Postgres mirror — deliberately DuckDB-only on both backends:

      * ``cli_auth_codes`` — single-use browser-loopback CLI-login exchange
        codes (``app/api/cli_auth.py``), ~2-minute TTL.
      * ``slack_binding_codes`` / ``slack_binding_issue_log`` /
        ``slack_binding_redeem_log`` — Slack identity-binding verification
        codes (``services/slack_bot/binding.py``), ~10-minute TTL, created
        lazily by that module's ``_ensure_table``.

    These cannot live in the system DuckDB, which must never be opened on a
    Postgres instance (``get_system_db()`` raises there). They get their own
    file at ``{DATA_DIR}/state/operational.duckdb``. This accessor is
    backend-agnostic (valid on DuckDB and Postgres alike). Cursor-per-call on a
    shared singleton, mirroring ``get_system_db()`` so callers can ``.close()``
    without dropping the underlying connection.
    """
    global _operational_db_conn, _operational_db_path
    db_path = str(_get_state_dir() / "operational.duckdb")

    with _system_db_lock:
        if _operational_db_conn is None or _operational_db_path != db_path:
            if _operational_db_conn is not None:
                try:
                    _operational_db_conn.close()
                except Exception:
                    pass
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            _operational_db_conn = _open_duckdb(db_path)
            _apply_memory_caps(_operational_db_conn, _SYSTEM_DB_MEMORY_LIMIT, label="get_operational_db")
            _operational_db_conn.execute(_OPERATIONAL_SCHEMA_DDL)
            _operational_db_path = db_path
        return _maybe_instrument(_operational_db_conn.cursor(), "operational")


def get_analytics_db() -> duckdb.DuckDBPyConnection:
    """Get a connection to the analytics database (parquet views).

    Singleton — mirrors `get_system_db()` above. Returns a cursor on the
    shared connection so callers can `.close()` the handle without
    closing the underlying connection. Re-opens transparently when
    `DATA_DIR` changes (test fixtures that swap data dirs across cases).

    Pre-#163 this opened a fresh connection on every call and most
    callers leaked it; see the rationale block at the module-level
    `_analytics_db_*` globals. `get_analytics_db_readonly()` deliberately
    stays per-call because each invocation re-ATTACHes extract.duckdb
    files into a fresh read-only context.

    Do NOT call this from a request path. The connection it hands back
    stays open for the life of the process (nothing but
    `close_analytics_db()`/`close_singleton_connections()` at shutdown ever
    closes it), and DuckDB refuses to open a *second* connection to the
    same file with a different configuration while that read-write handle
    is alive — so every subsequent `get_analytics_db_readonly()` call in
    the process raises "Can't open a connection to same database file with
    a different configuration than existing connections" until restart.
    This is exactly the outage `POST /api/mcp/query-table/{table_id}`
    (`app/api/mcp_per_table.py`) caused before it was moved onto
    `get_analytics_db_readonly()` — see the regression tests
    in `tests/test_analytics_db_singleton.py::TestReadonlyOnFreshDataDir`.
    As of that fix, nothing under `app/`, `cli/`, `services/`, or
    `connectors/` calls this function; keep it that way. A static guard
    (`tests/test_analytics_rw_singleton_guard.py`) fails the build if a new
    call site appears outside `src/db.py` itself. If you genuinely need a
    long-lived read-write handle for maintenance work (bulk rebuild,
    profiling, catalog export), open your own connection rather than
    reintroducing a caller here — this singleton is reserved for
    infrastructure that owns the analytics DB's write lifecycle.
    """
    global _analytics_db_conn, _analytics_db_path
    db_path = str(_get_data_dir() / "analytics" / "server.duckdb")

    with _analytics_db_lock:
        if _analytics_db_conn is None or _analytics_db_path != db_path:
            # Close stale connection if DATA_DIR changed (test fixtures)
            if _analytics_db_conn is not None:
                try:
                    _analytics_db_conn.close()
                except Exception:
                    pass
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            # Route through ``_open_duckdb`` so the connection inherits the
            # ``SET GLOBAL TimeZone='UTC'`` pin (frontend timezone fix from
            # #473), then apply the memory cap (OOM resilience from #479).
            _analytics_db_conn = _open_duckdb(db_path)
            # Defensive memory cap (budgeted with the system + readonly
            # connections to sum under a 4 GiB cgroup — see the
            # ``_*_MEMORY_LIMIT`` constants). Analyst-facing queries that
            # hit the cap spill to disk or surface a clear DuckDB OOM
            # exception, rather than a silent process-wide OOM-kill.
            _apply_memory_caps(
                _analytics_db_conn,
                _ANALYTICS_DB_MEMORY_LIMIT,
                label="get_analytics_db",
            )
            _analytics_db_path = db_path
        return _maybe_instrument(_analytics_db_conn.cursor(), "analytics")


def close_singleton_connections() -> None:
    """Close the shared DuckDB connections so a subprocess can take the lock.

    Called from the DB-backend state-machine migrator-spawn path
    (`app.api.db_state.start_migration`) right before launching the migrator
    subprocess. DuckDB ≥1.5 holds an exclusive per-process file lock on each
    open database file; without releasing it here the subprocess raises
    ``IOException: Conflicting lock is held in /usr/local/bin/python3.13``.

    Also closes the DuckLake reader/writer singletons (``src.ducklake_session
    .close_ducklake_sessions()``) — when ``analytics.backend=ducklake`` is
    active, an open DuckLake attach holds the same kind of exclusive lock on
    a file-catalog target (or a live libpq connection on a Postgres catalog
    target) that would otherwise survive into the subprocess handoff and
    conflict with it, same as the DuckDB singletons above. Imported locally
    to avoid a module-level circular import (``src.ducklake_session`` itself
    imports from this module); safe to call unconditionally regardless of
    which analytics backend is configured — ``close_ducklake_sessions()`` is
    a no-op when no DuckLake session has ever been opened.

    Idempotent. The next call to ``get_system_db()`` / ``get_analytics_db()``
    will lazily re-open if the file is still on disk; if the migration
    flipped the backend to Postgres, the app process will be recreated by
    the host applier and these globals never need to re-open.
    """
    global _system_db_conn, _analytics_db_conn, _operational_db_conn

    # Same handshake as close_system_db() (#1294): this path also closes
    # _system_db_conn, so an in-flight rolling-snapshot EXPORT on a child
    # cursor must be interrupted and drained first, not closed out from under.
    if not interrupt_rolling_snapshot_export(
        _ROLLING_SNAPSHOT_INTERRUPT_TIMEOUT_S, caller="close_singleton_connections"
    ):
        logger.warning(
            "close_singleton_connections: rolling-snapshot export still running after "
            "being interrupted; closing system.duckdb anyway"
        )

    with _system_db_lock:
        if _system_db_conn is not None:
            try:
                _system_db_conn.close()
            except Exception:
                pass
            _system_db_conn = None
        # The operational DuckDB (cli_auth_codes / Slack binding codes) is a
        # third long-lived singleton, guarded by the same _system_db_lock. On a
        # Postgres instance it is the ONLY written DuckDB file, so its exclusive
        # file lock must also be released before a migrator subprocess spawns.
        if _operational_db_conn is not None:
            try:
                _operational_db_conn.close()
            except Exception:
                pass
            _operational_db_conn = None
    with _analytics_db_lock:
        if _analytics_db_conn is not None:
            try:
                _analytics_db_conn.close()
            except Exception:
                pass
            _analytics_db_conn = None

    from src.ducklake_session import close_ducklake_sessions

    close_ducklake_sessions()


def _reattach_remote_extensions(conn: duckdb.DuckDBPyConnection, extracts_dir: Path) -> None:
    """Re-LOAD DuckDB extensions listed in _remote_attach tables of each extract.duckdb.

    Called from get_analytics_db_readonly() after ATTACHing extract.duckdb files so
    that remote views (e.g. BigQuery) resolve correctly.  Uses LOAD only — no INSTALL —
    to avoid touching the network in read-only query paths.
    """
    if not extracts_dir.exists():
        return

    try:
        attached_dbs = {r[0] for r in conn.execute("SELECT database_name FROM duckdb_databases()").fetchall()}
    except Exception:
        return

    for ext_dir in sorted(extracts_dir.iterdir()):
        if not ext_dir.is_dir():
            continue
        if not _SAFE_IDENTIFIER.match(ext_dir.name):
            continue
        db_file = ext_dir / "extract.duckdb"
        if not db_file.exists():
            continue
        # Only process sources that were successfully attached
        if ext_dir.name not in attached_dbs:
            continue

        # Check whether this extract has a _remote_attach table
        try:
            has_table = conn.execute(
                "SELECT 1 FROM information_schema.tables "
                f"WHERE table_catalog='{ext_dir.name}' AND table_name='_remote_attach'"
            ).fetchone()
            if not has_table:
                continue
        except Exception:
            continue

        try:
            rows = conn.execute(
                f"SELECT alias, extension, url, token_env FROM {ext_dir.name}._remote_attach"
            ).fetchall()
        except Exception as e:
            logger.debug("Could not read _remote_attach from %s: %s", ext_dir.name, e)
            continue

        # Refresh attached list before processing each source's rows
        try:
            attached_dbs = {r[0] for r in conn.execute("SELECT database_name FROM duckdb_databases()").fetchall()}
        except Exception:
            pass

        # Issue #81 Group A — apply the same allowlist policy on the
        # query path that the orchestrator's rebuild path uses. Without
        # this, a malicious connector's _remote_attach row exfiltrates
        # JWT_SECRET_KEY / SESSION_SECRET / OPENAI_API_KEY on every
        # query, defeating the rebuild-path hardening entirely.
        from connectors.databricks.attach import UC_EXTENSION, attach_unity_catalog
        from connectors.snowflake.attach import (
            SF_EXTENSION,
            attach_snowflake,
            install_snowflake_adbc_driver,
        )
        from src.orchestrator_security import (
            attach_host_allowlist_configured,
            escape_sql_string_literal,
            is_attach_host_allowed,
            is_extension_allowed,
            is_token_env_allowed,
            resolve_remote_attach_token,
        )

        for alias, extension, url, token_env in rows:
            if not _SAFE_IDENTIFIER.match(alias or ""):
                logger.debug("Skipping unsafe remote_attach alias: %r", alias)
                continue
            if not _SAFE_IDENTIFIER.match(extension or ""):
                logger.debug("Skipping unsafe remote_attach extension: %r", extension)
                continue
            if not is_extension_allowed(extension):
                logger.error(
                    "query-path remote_attach: extension %r not in allowlist; "
                    "refusing to LOAD/ATTACH for source %s. Override via "
                    "AGNES_REMOTE_ATTACH_EXTENSIONS if intended.",
                    extension,
                    alias,
                )
                continue
            if token_env and not is_token_env_allowed(token_env):
                logger.error(
                    "query-path remote_attach: token_env %r not in allowlist; "
                    "refusing for source %s. Override via "
                    "AGNES_REMOTE_ATTACH_TOKEN_ENVS if intended.",
                    token_env,
                    alias,
                )
                continue
            already_attached = alias in attached_dbs
            # Non-BQ remote extensions carry no expiring credential — the
            # existing ATTACH (from an earlier call on this same
            # connection) is still good, nothing to refresh. BQ is
            # different: its ACCESS_TOKEN secret is a short-lived GCE
            # metadata token (or TOKEN literal), and this function is
            # called from a long-lived connection under the DuckLake
            # backend (`src.ducklake_session.get_ducklake_read`'s
            # process-wide reader singleton) as well as the legacy
            # per-request path. On the legacy path `already_attached` is
            # never True (each call gets a brand-new connection with
            # nothing attached yet), so this branch is a no-op there;
            # on the long-lived DuckLake reader, skipping the refresh
            # here would leave the BQ secret to expire after its TTL and
            # start failing every remote query for the rest of the
            # process's life — see the wave-2G task-4 report.
            if already_attached and extension != "bigquery":
                logger.debug("Remote source %s already attached, skipping", alias)
                continue
            try:
                # LOAD only on the read-only query path — no INSTALL.
                # Per the function docstring, this path runs on every
                # query request and must not touch the network. The
                # rebuild path (orchestrator) is responsible for INSTALL;
                # by the time a query lands here, any community extension
                # we'll see is already on disk. If LOAD fails because the
                # extension isn't installed, log + skip (caller will see
                # missing remote views and the operator will trigger a
                # rebuild). The Snowflake extension needs the ADBC driver
                # shared library on disk even for LOAD, so ensure it first.
                if extension == SF_EXTENSION:
                    install_snowflake_adbc_driver()
                conn.execute(f"LOAD {extension};")
                token = resolve_remote_attach_token(token_env)
                safe_url = escape_sql_string_literal(url)

                # BQ-specific: refresh token from GCE metadata, create session-scoped
                # secret before ATTACH. Empty token_env (set by the BQ extractor)
                # is the contract that signals "use built-in metadata path". The
                # secret is created here on every readonly-connection open because
                # secrets are session-scoped and don't persist with analytics.duckdb.
                # DuckDB resolves a secret by (TYPE, name) match at query time, not
                # at ATTACH time, so replacing `bq_secret_{alias}` refreshes
                # credentials for an ATTACH that already exists — no re-ATTACH
                # needed (and re-ATTACHing an already-attached alias would error).
                if extension == "bigquery":
                    try:
                        bq_token = get_metadata_token()
                    except BQMetadataAuthError as e:
                        logger.error(
                            "Failed to fetch BQ metadata token for %s: %s — skipping ATTACH",
                            alias,
                            e,
                        )
                        continue
                    escaped = escape_sql_string_literal(bq_token)
                    secret_name = f"bq_secret_{alias}"
                    conn.execute(f"CREATE OR REPLACE SECRET {secret_name} (TYPE bigquery, ACCESS_TOKEN '{escaped}')")
                    from connectors.bigquery.access import apply_bq_session_settings

                    apply_bq_session_settings(conn)
                    if not already_attached:
                        conn.execute(f"ATTACH '{safe_url}' AS {alias} (TYPE {extension}, READ_ONLY)")
                elif extension == UC_EXTENSION:
                    # Unity Catalog: PAT + workspace endpoint together, which
                    # the generic TOKEN branch cannot express. Mirrors the
                    # orchestrator's rebuild-path branch exactly (same helper),
                    # including the credential-egress host allowlist.
                    if not is_attach_host_allowed(url):
                        logger.error(
                            "Re-attach %s: url host %r not in AGNES_REMOTE_ATTACH_HOST_ALLOWLIST; "
                            "refusing to send credential from %s.",
                            alias,
                            url,
                            token_env,
                        )
                        continue
                    if not attach_host_allowlist_configured():
                        logger.warning(
                            "Re-attach %s: sending credential (%s) to connector-chosen url %r "
                            "with no AGNES_REMOTE_ATTACH_HOST_ALLOWLIST configured — pin "
                            "allowed hosts in production.",
                            alias,
                            token_env,
                            url,
                        )
                    attach_unity_catalog(conn, alias=alias, url=url, token=token)
                elif extension == SF_EXTENSION:
                    if token_env and not token:
                        # Mirror the rebuild path (src/orchestrator.py), which
                        # skips an unresolvable token_env with this warning. This
                        # branch is reached BEFORE the `elif token:` guard below,
                        # so without it an ATTACH goes out with `PASSWORD ''` and
                        # fails at Snowflake — the operator sees an
                        # authentication error instead of the real cause, a name
                        # nothing resolves. Reachable in normal operation: an
                        # `auth_type` flip in /admin/server-config changes which
                        # env name the credential lives under, while the extract's
                        # `_remote_attach.token_env` keeps the old one until
                        # something rebuilds it.
                        logger.warning(
                            "Re-attach %s: token_env %s not resolvable, skipping",
                            alias,
                            token_env,
                        )
                        continue
                    if not is_attach_host_allowed(url):
                        logger.error(
                            "Re-attach %s: url host %r not in AGNES_REMOTE_ATTACH_HOST_ALLOWLIST; "
                            "refusing to send credential from %s.",
                            alias,
                            url,
                            token_env,
                        )
                        continue
                    if not attach_host_allowlist_configured():
                        logger.warning(
                            "Re-attach %s: sending credential (%s) to connector-chosen url %r "
                            "with no AGNES_REMOTE_ATTACH_HOST_ALLOWLIST configured — pin "
                            "allowed hosts in production.",
                            alias,
                            token_env,
                            url,
                        )
                    from connectors.snowflake.settings import resolve_snowflake_passphrase_for_token

                    passphrase = resolve_snowflake_passphrase_for_token(token_env)
                    attach_snowflake(conn, alias=alias, url=url, token=token, passphrase=passphrase)
                elif token:
                    # #F11 — never ship a real credential to a connector-chosen
                    # host the operator has not approved (mirrors the rebuild
                    # path in src/orchestrator.py).
                    if not is_attach_host_allowed(url):
                        logger.error(
                            "Re-attach %s: url host %r not in AGNES_REMOTE_ATTACH_HOST_ALLOWLIST; "
                            "refusing to send credential from %s.",
                            alias,
                            url,
                            token_env,
                        )
                        continue
                    if not attach_host_allowlist_configured():
                        logger.warning(
                            "Re-attach %s: sending credential (%s) to connector-chosen url %r "
                            "with no AGNES_REMOTE_ATTACH_HOST_ALLOWLIST configured — pin "
                            "allowed hosts in production.",
                            alias,
                            token_env,
                            url,
                        )
                    escaped_token = escape_sql_string_literal(token)
                    conn.execute(f"ATTACH '{safe_url}' AS {alias} (TYPE {extension}, TOKEN '{escaped_token}')")
                    # Apply BQ session settings on every BQ-extension attach,
                    # not only the metadata-token branch above. Previously the
                    # token-based branch fell through without setting
                    # bq_query_timeout_ms, leaving the 90 s extension default
                    # in place and causing "remote query timeout" surprises.
                    if extension == "bigquery":
                        from connectors.bigquery.access import apply_bq_session_settings

                        apply_bq_session_settings(conn)
                else:
                    conn.execute(f"ATTACH '{safe_url}' AS {alias} (TYPE {extension}, READ_ONLY)")
                    if extension == "bigquery":
                        from connectors.bigquery.access import apply_bq_session_settings

                        apply_bq_session_settings(conn)
                attached_dbs.add(alias)
                logger.debug("Re-attached remote source %s via %s extension", alias, extension)
            except Exception as e:
                logger.debug("Could not re-attach remote source %s: %s", alias, e)


def get_analytics_db_readonly() -> duckdb.DuckDBPyConnection:
    """Read-only connection to analytics DB. Blocks writes and external access.

    ATTACHes extract.duckdb files so views that reference them work.

    Backend dispatch (wave-2G, DuckLake): when ``analytics.backend`` /
    ``AGNES_ANALYTICS_BACKEND`` resolves to ``"ducklake"``, this delegates
    to ``src.ducklake_session.get_ducklake_read()`` instead of the
    open-file-and-re-ATTACH-every-request path below.

    **Connection-model choice (documented per the wave-2G task-4 brief):**
    the legacy path below opens a brand-new connection on every call — a
    cheap operation for a local DuckDB file, so per-request open/close is
    fine. DuckLake is different: when the catalog target is a Postgres
    DSN, every ``ATTACH`` opens its own libpq connection (see
    ``src/ducklake_session.py``'s module docstring and the W2G-2 POC
    finding — "one PG connection per ATTACH, no extra per query"), so a
    naive per-request open would churn one new Postgres connection per
    API request. Instead, ``get_ducklake_read()`` keeps ONE long-lived
    attach per process and hands back a ``.cursor()`` per caller —
    mirroring the ``get_analytics_db()`` singleton+cursor pattern in this
    module rather than this function's per-call-open pattern. A cursor
    still gives snapshot-consistent reads (DuckLake's MVCC — a cursor
    opened before a concurrent writer commits keeps seeing the
    pre-commit snapshot) and callers here already only ever call
    ``.close()`` on the returned handle, which is exactly the
    cursor-close contract ``get_ducklake_read()`` documents.
    """
    if analytics_backend() == "ducklake":
        from src.ducklake_session import get_ducklake_read

        return get_ducklake_read()

    db_path = _get_data_dir() / "analytics" / "server.duckdb"
    # Serialize the existence-check + materialization + the following
    # read-only open under the same lock the singleton accessors use
    # (`get_analytics_db()` / `close_singleton_connections()` above).
    #
    # Without this, two threads racing the very first call on a fresh
    # install can interleave: thread A sees the file missing and opens it
    # read-write to materialize it (closing that handle right after), while
    # thread B — having observed the file already exists — reaches the
    # read-only open below *before* A's read-write handle is closed.
    # DuckDB refuses to open a file read-only while a read-write connection
    # to it is alive in the same process ("Can't open a connection to same
    # database file with a different configuration than existing
    # connections"), so B's request 500s. Holding the lock across both the
    # materialization and the read-only open guarantees every thread's
    # open happens strictly after any other thread's transient read-write
    # handle has been closed. Once the file exists permanently (after the
    # very first successful call), this section is a cheap exists() check
    # plus a normal read-only open — no meaningful serialization cost.
    with _analytics_db_lock:
        if not db_path.exists():
            # Fresh instance: materialize an empty database file so the
            # read-only open below has something to attach to, then drop the
            # read-write handle IMMEDIATELY.
            #
            # DuckDB refuses to open a file read-only while a read-write
            # connection to it is alive in the same process ("Can't open a
            # connection to same database file with a different configuration
            # than existing connections"). This branch used to *return* that
            # read-write connection instead of a read-only one. Its callers do
            # close it (`app/api/query.py` and `app/api/query_hybrid.py` each
            # close in a `finally`), so the damage was narrower than a leak:
            # the caller got a handle that was writable at the engine level,
            # on a path whose whole point is that it is not, and it had
            # skipped this function's `extracts` ATTACH loop and
            # `_reattach_remote_extensions` — so the query ran against a
            # database missing its views. A second request arriving inside
            # that window also failed outright, since the read-write handle
            # was still alive.
            db_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                _open_duckdb(str(db_path), read_only=False).close()
            except Exception:
                logger.exception("Failed to initialize empty analytics DB at %s", db_path)
        conn = _open_duckdb(str(db_path), read_only=True)
    # Memory cap (see get_analytics_db rationale). Read-only conns can
    # still buffer significant memory for analyst queries that hit
    # ``CREATE TEMP TABLE`` over read_parquet — capping keeps a single
    # analyst's heavy query from process-wide OOM-killing all other
    # in-flight requests.
    _apply_memory_caps(conn, _ANALYTICS_RO_MEMORY_LIMIT, label="analytics_ro")
    # ATTACH extract.duckdb files FIRST so views referencing them work
    extracts_dir = _get_data_dir() / "extracts"
    if extracts_dir.exists():
        for ext_dir in sorted(extracts_dir.iterdir()):
            db_file = ext_dir / "extract.duckdb"
            if db_file.exists() and ext_dir.is_dir():
                if not _SAFE_IDENTIFIER.match(ext_dir.name):
                    continue
                try:
                    conn.execute(f"ATTACH '{db_file}' AS {ext_dir.name} (READ_ONLY)")
                except Exception:
                    pass
    # Re-attach remote extensions so BigQuery / other remote views resolve.
    _reattach_remote_extensions(conn, extracts_dir)
    # Note: external_access stays enabled because views use read_parquet() on local files.
    # File-path-based attacks are blocked by the SQL blocklist in app/api/query.py.
    return _maybe_instrument(conn, "analytics_ro")


_V1_TO_V2_MIGRATIONS = [
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS source_type VARCHAR",
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS bucket VARCHAR",
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS source_table VARCHAR",
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS query_mode VARCHAR DEFAULT 'local'",
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS sync_schedule VARCHAR",
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS profile_after_sync BOOLEAN DEFAULT true",
]

_V2_TO_V3_MIGRATIONS = [
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS is_public BOOLEAN DEFAULT true",
]

_V4_TO_V5_MIGRATIONS = [
    # DuckDB doesn't allow ALTER TABLE ADD COLUMN with NOT NULL constraint,
    # so we add the column with a DEFAULT, backfill, then the app-level
    # code enforces non-null semantics (never inserts NULL for `active`).
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS active BOOLEAN DEFAULT TRUE",
    "UPDATE users SET active = TRUE WHERE active IS NULL",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS deactivated_at TIMESTAMP",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS deactivated_by VARCHAR",
]

_V5_TO_V6_MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS personal_access_tokens (
        id           VARCHAR PRIMARY KEY,
        user_id      VARCHAR NOT NULL,
        name         VARCHAR NOT NULL,
        token_hash   VARCHAR NOT NULL,
        prefix       VARCHAR NOT NULL,
        scopes       VARCHAR,
        created_at   TIMESTAMP NOT NULL DEFAULT current_timestamp,
        expires_at   TIMESTAMP,
        last_used_at TIMESTAMP,
        revoked_at   TIMESTAMP
    )
    """,
]

_V6_TO_V7_MIGRATIONS = [
    "ALTER TABLE personal_access_tokens ADD COLUMN IF NOT EXISTS last_used_ip VARCHAR",
]

_V7_TO_V8_MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS internal_roles (
        id           VARCHAR PRIMARY KEY,
        key          VARCHAR UNIQUE NOT NULL,
        display_name VARCHAR NOT NULL,
        description  TEXT,
        owner_module VARCHAR,
        created_at   TIMESTAMP NOT NULL DEFAULT current_timestamp,
        updated_at   TIMESTAMP NOT NULL DEFAULT current_timestamp
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS group_mappings (
        id                VARCHAR PRIMARY KEY,
        external_group_id VARCHAR NOT NULL,
        internal_role_id  VARCHAR NOT NULL REFERENCES internal_roles(id),
        assigned_at       TIMESTAMP NOT NULL DEFAULT current_timestamp,
        assigned_by       VARCHAR,
        UNIQUE (external_group_id, internal_role_id)
    )
    """,
]

# v9 migration is multi-stage: ALTER + new table → seed core.* rows → backfill
# existing users.role values into user_role_grants → DROP users.role. The
# latter three steps run as Python helpers (_seed_core_roles +
# _backfill_users_role_to_grants) called from _ensure_schema, not raw SQL —
# they need DuckDB ConstraintException handling and per-user-role lookups
# that don't translate cleanly to a static SQL list.
_V8_TO_V9_MIGRATIONS = [
    "ALTER TABLE internal_roles ADD COLUMN IF NOT EXISTS implies VARCHAR DEFAULT '[]'",
    "ALTER TABLE internal_roles ADD COLUMN IF NOT EXISTS is_core BOOLEAN DEFAULT false",
    """
    CREATE TABLE IF NOT EXISTS user_role_grants (
        id                VARCHAR PRIMARY KEY,
        user_id           VARCHAR NOT NULL REFERENCES users(id),
        internal_role_id  VARCHAR NOT NULL REFERENCES internal_roles(id),
        granted_at        TIMESTAMP NOT NULL DEFAULT current_timestamp,
        granted_by        VARCHAR,
        source            VARCHAR DEFAULT 'direct',
        UNIQUE (user_id, internal_role_id)
    )
    """,
]

# v10: view-name collision detection across connectors (issue #81 Group C).
# The system schema above already CREATEs view_ownership; this migration is
# the ALTER path for installs predating the bump.
_V9_TO_V10_MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS view_ownership (
        view_name     VARCHAR PRIMARY KEY,
        source_name   VARCHAR NOT NULL,
        registered_at TIMESTAMP NOT NULL DEFAULT current_timestamp
    )
    """,
]

# v11: marketplace registry + plugin listing + group access mapping. Was
# plugin-mapping's v7→v8 + v8→v9 before PR #73 took the v9 slot for role
# management and #81 Group C took v10 for view_ownership; shifted up to v11.
_V10_TO_V11_MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS marketplace_registry (
        id              VARCHAR PRIMARY KEY,
        name            VARCHAR NOT NULL,
        url             VARCHAR NOT NULL,
        branch          VARCHAR,
        token_env       VARCHAR,
        description     TEXT,
        registered_by   VARCHAR,
        registered_at   TIMESTAMP DEFAULT current_timestamp,
        last_synced_at  TIMESTAMP,
        last_commit_sha VARCHAR,
        last_error      TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS marketplace_plugins (
        marketplace_id  VARCHAR NOT NULL,
        name            VARCHAR NOT NULL,
        description     TEXT,
        version         VARCHAR,
        author_name     VARCHAR,
        homepage        VARCHAR,
        category        VARCHAR,
        source_type     VARCHAR,
        source_spec     JSON,
        raw             JSON,
        created_at      TIMESTAMP DEFAULT current_timestamp,
        updated_at      TIMESTAMP DEFAULT current_timestamp,
        PRIMARY KEY (marketplace_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_groups (
        id          VARCHAR PRIMARY KEY,
        name        VARCHAR NOT NULL UNIQUE,
        description TEXT,
        created_at  TIMESTAMP DEFAULT current_timestamp,
        created_by  VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS plugin_access (
        group_id       VARCHAR NOT NULL,
        marketplace_id VARCHAR NOT NULL,
        plugin_name    VARCHAR NOT NULL,
        granted_at     TIMESTAMP DEFAULT current_timestamp,
        granted_by     VARCHAR,
        PRIMARY KEY (group_id, marketplace_id, plugin_name)
    )
    """,
]

# v12: users.groups + user_groups.is_system. Was plugin-mapping's v9→v10
# (then v10→v11); shifted up to v12 after #81 Group C took v10.
_V11_TO_V12_MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS groups JSON",
    "ALTER TABLE user_groups ADD COLUMN IF NOT EXISTS is_system BOOLEAN DEFAULT FALSE",
]

# v13: replace internal_roles + group_mappings + user_role_grants + plugin_access
# with a single (group, resource_type, resource_id) grant model and add
# user_group_members to materialize membership (was users.groups JSON cache).
# Schema-only steps here; backfill + drops are in _v12_to_v13_finalize so we
# can run Python logic over the transitional state.
_V12_TO_V13_MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS user_group_members (
        user_id   VARCHAR NOT NULL,
        group_id  VARCHAR NOT NULL,
        source    VARCHAR NOT NULL,
        added_at  TIMESTAMP DEFAULT current_timestamp,
        added_by  VARCHAR,
        PRIMARY KEY (user_id, group_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS resource_grants (
        id            VARCHAR PRIMARY KEY,
        group_id      VARCHAR NOT NULL,
        resource_type VARCHAR NOT NULL,
        resource_id   VARCHAR NOT NULL,
        assigned_at   TIMESTAMP DEFAULT current_timestamp,
        assigned_by   VARCHAR,
        UNIQUE (group_id, resource_type, resource_id)
    )
    """,
]


# v15: corporate-memory context-engineering columns + contradiction tracking +
# session-extraction state. The columns rename `knowledge_items.audience`'s
# original semantics into a richer model: confidence + domain + entities +
# source_type/ref + valid window + supersedes lineage + sensitivity tier +
# is_personal flag. Pavel's branch had this as v9→v10 against a v9-era main;
# the bump to v15 sequences after main's v14 (FK-on-grants).
_V14_TO_V15_MIGRATIONS = [
    "ALTER TABLE knowledge_items ADD COLUMN IF NOT EXISTS confidence DOUBLE",
    "ALTER TABLE knowledge_items ADD COLUMN IF NOT EXISTS domain VARCHAR",
    "ALTER TABLE knowledge_items ADD COLUMN IF NOT EXISTS entities JSON",
    "ALTER TABLE knowledge_items ADD COLUMN IF NOT EXISTS source_type VARCHAR DEFAULT 'claude_local_md'",
    "ALTER TABLE knowledge_items ADD COLUMN IF NOT EXISTS source_ref VARCHAR",
    "ALTER TABLE knowledge_items ADD COLUMN IF NOT EXISTS valid_from TIMESTAMP",
    "ALTER TABLE knowledge_items ADD COLUMN IF NOT EXISTS valid_until TIMESTAMP",
    "ALTER TABLE knowledge_items ADD COLUMN IF NOT EXISTS supersedes VARCHAR",
    "ALTER TABLE knowledge_items ADD COLUMN IF NOT EXISTS sensitivity VARCHAR DEFAULT 'internal'",
    "ALTER TABLE knowledge_items ADD COLUMN IF NOT EXISTS is_personal BOOLEAN DEFAULT FALSE",
    "UPDATE knowledge_items SET source_type = 'claude_local_md' WHERE source_type IS NULL",
    """
    CREATE TABLE IF NOT EXISTS knowledge_contradictions (
        id VARCHAR PRIMARY KEY,
        item_a_id VARCHAR NOT NULL,
        item_b_id VARCHAR NOT NULL,
        explanation TEXT,
        severity VARCHAR,
        suggested_resolution TEXT,
        resolved BOOLEAN DEFAULT FALSE,
        resolved_by VARCHAR,
        resolved_at TIMESTAMP,
        resolution VARCHAR,
        detected_at TIMESTAMP DEFAULT current_timestamp
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_extraction_state (
        session_file VARCHAR PRIMARY KEY,
        username VARCHAR NOT NULL,
        processed_at TIMESTAMP DEFAULT current_timestamp,
        items_extracted INTEGER DEFAULT 0,
        file_hash VARCHAR
    )
    """,
]

# v16: per-detection evidence rows — many-to-one against knowledge_items.
# Future Bayesian re-calibration uses (detection_type, user_quote, source_user)
# triples; for now confidence.py walks them to compute "additional verifiers"
# boosts. Index on item_id keeps the per-item walk O(evidence-per-item).
_V15_TO_V16_MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS verification_evidence (
        id VARCHAR PRIMARY KEY,
        item_id VARCHAR NOT NULL,
        source_user VARCHAR,
        source_ref VARCHAR,
        detection_type VARCHAR,
        user_quote TEXT,
        created_at TIMESTAMP DEFAULT current_timestamp
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_verification_evidence_item ON verification_evidence(item_id)",
]


# v16 -> v17: knowledge_item_relations table for duplicate-candidate hints
# (see issue #62). Same DDL as in _SYSTEM_SCHEMA so fresh installs and
# upgrades converge.
_V16_TO_V17_MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS knowledge_item_relations (
        item_a_id VARCHAR NOT NULL,
        item_b_id VARCHAR NOT NULL,
        relation_type VARCHAR NOT NULL,
        score DOUBLE,
        resolved BOOLEAN DEFAULT FALSE,
        resolved_by VARCHAR,
        resolved_at TIMESTAMP,
        resolution VARCHAR,
        created_at TIMESTAMP DEFAULT current_timestamp,
        PRIMARY KEY (item_a_id, item_b_id, relation_type)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_knowledge_item_relations_resolved ON knowledge_item_relations(resolved)",
]


# v17 -> v18: see _v17_to_v18_finalize. Env-conditional, so kept as a Python
# helper rather than a flat SQL list (the migrate-ladder calls it directly).


# v19 -> v20: source_query column backs query_mode='materialized' for BigQuery.
# Admin-registered SQL stored verbatim; scheduler runs it through the DuckDB BQ
# extension (via BqAccess) and writes the result to
# /data/extracts/bigquery/data/<id>.parquet so the existing manifest + agnes pull
# flow distributes it to analysts. NULL on existing rows.
_V19_TO_V20_MIGRATIONS = [
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS source_query TEXT",
]


# Core role seed data — single source of truth. Used by both _seed_core_roles
# (idempotent insert) and the v8→v9 backfill. Order matters: lowest privilege
# first so implies references resolve cleanly when expand_implies does BFS.
_CORE_ROLES_SEED = [
    # (key, display_name, description, implies)
    ("core.viewer", "Viewer", "Read-only access to permitted datasets.", []),
    ("core.analyst", "Analyst", "Default user role; query data, run analyses.", ["core.viewer"]),
    (
        "core.km_admin",
        "Knowledge-management admin",
        "Manages metric definitions and column metadata.",
        ["core.analyst"],
    ),
    ("core.admin", "Administrator", "Full system access; bypasses dataset_permissions.", ["core.km_admin"]),
]

# Maps the legacy users.role string values onto core.* keys for the v8→v9
# backfill. Anything unrecognized falls back to core.viewer — safest default
# for existing rows that somehow held a value outside the documented enum.
_LEGACY_ROLE_TO_CORE_KEY = {
    "viewer": "core.viewer",
    "analyst": "core.analyst",
    "km_admin": "core.km_admin",
    "admin": "core.admin",
}


SYSTEM_ADMIN_GROUP = "Admin"
SYSTEM_EVERYONE_GROUP = "Everyone"

# Seed copy for the two hardcoded system groups. Names are referenced from
# app.auth.access (admin short-circuit) and the OAuth callback (default
# Everyone membership for new users); changing them is a breaking change.
_SYSTEM_GROUPS_SEED = [
    (SYSTEM_ADMIN_GROUP, "System: full access to all data and admin actions"),
    (SYSTEM_EVERYONE_GROUP, "System: default group every user is implicitly a member of"),
]

# Canonical memory-domain seed (the legacy ``VALID_DOMAINS`` six from
# ``app/api/memory.py``, v15 era). Deterministic ``md_<slug>`` ids —
# downstream code and tests rely on the naming convention. Seeded by the
# v52 ladder step below on the DuckDB backend, and by the app lifespan
# through the repository factory (``memory_domains_repo().ensure_seed``)
# on the active backend — the PG side has no Alembic seed for these.
# Tuple shape: (id, slug, name, icon, color).
_CANONICAL_MEMORY_DOMAINS_SEED = [
    ("md_finance", "finance", "Finance", "💰", "#dcfce7"),
    ("md_engineering", "engineering", "Engineering", "⚙️", "#dbeafe"),
    ("md_product", "product", "Product", "📦", "#fef3c7"),
    ("md_data", "data", "Data", "📊", "#f3e8ff"),
    ("md_operations", "operations", "Operations", "🔧", "#fff7ed"),
    ("md_infrastructure", "infrastructure", "Infrastructure", "🏗️", "#fef2f2"),
]


def _seed_system_groups(conn: duckdb.DuckDBPyConnection) -> None:
    """Idempotently insert/promote the Admin and Everyone system groups.

    Replaces the v9-era _seed_core_roles tail call. Runs on every connect
    once the DB is on a version this binary understands, so a manually-
    deleted system group reappears next start. Promotes a manually-created
    same-named group to is_system=TRUE without rewriting its description
    (admin's description wins; we only set our default when creating).
    """
    import uuid as _uuid

    for name, description in _SYSTEM_GROUPS_SEED:
        existing = conn.execute("SELECT id, is_system FROM user_groups WHERE name = ?", [name]).fetchone()
        if existing is None:
            conn.execute(
                """INSERT INTO user_groups (id, name, description, is_system, created_by)
                   VALUES (?, ?, ?, TRUE, 'system:seed')""",
                [str(_uuid.uuid4()), name, description],
            )
        elif not existing[1]:
            # Promote pre-existing manual group to system without touching desc.
            conn.execute(
                "UPDATE user_groups SET is_system = TRUE WHERE id = ?",
                [existing[0]],
            )


def _state_backend_is_pg() -> bool:
    """True when app-state lives in Postgres (``use_pg()``).

    Lazy import — ``src.repositories`` imports this module at its top
    level, so the dependency must stay function-local. Any failure to
    probe the backend falls back to False (the DuckDB fresh-install
    default), so a broken probe never suppresses the seed on a plain
    DuckDB instance.
    """
    try:
        from src.repositories import use_pg

        return use_pg()
    except Exception:
        return False


def _v12_to_v13_finalize(conn: duckdb.DuckDBPyConnection) -> None:
    """Backfill user_group_members + resource_grants, then drop legacy tables.

    Runs after _V12_TO_V13_MIGRATIONS created the new tables. Order matters:

    1. Seed Admin/Everyone in user_groups so backfill targets exist.
    2. Backfill user_group_members from users.groups JSON via name lookup
       (source='google_sync' — Google was the v12 origin of those entries).
    3. Backfill admin membership from user_role_grants.core.admin grants.
    4. Add Everyone membership to every user (source='system_seed').
    5. Backfill resource_grants from plugin_access.
    6. DROP legacy tables in FK-correct order.
    7. ALTER users DROP COLUMN groups (DuckDB ≥ 0.8 supports it).

    Wrapped in an explicit transaction so an unhandled mid-flight failure
    rolls the DB back to a clean v12 state. Per-step soft-fails on DROP
    TABLE / ALTER (already caught and logged inline) do NOT abort the
    transaction — only an unexpected exception from a backfill SELECT or
    INSERT does. The outer caller in _ensure_schema then skips the
    schema_version bump and the next start retries the whole step.
    """
    import uuid as _uuid

    conn.execute("BEGIN TRANSACTION")
    try:
        _seed_system_groups(conn)

        admin_group_id = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()[0]
        everyone_group_id = conn.execute(
            "SELECT id FROM user_groups WHERE name = ?", [SYSTEM_EVERYONE_GROUP]
        ).fetchone()[0]

        # 2. users.groups JSON → user_group_members (google_sync). Tolerant of the
        # column having been physically dropped already (re-run safety) and of
        # malformed JSON (caught row-by-row, skipped silently).
        has_groups_col = conn.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name = 'users' AND column_name = 'groups'"
        ).fetchone()
        if has_groups_col:
            rows = conn.execute("SELECT id, groups FROM users WHERE groups IS NOT NULL").fetchall()
            for user_id, groups_json in rows:
                try:
                    import json as _json

                    names = _json.loads(groups_json) if isinstance(groups_json, str) else (groups_json or [])
                except (ValueError, TypeError):
                    names = []
                if not isinstance(names, list):
                    continue
                for name in names:
                    if not isinstance(name, str) or not name.strip():
                        continue
                    group_row = conn.execute(
                        "SELECT id FROM user_groups WHERE name = ?",
                        [name],
                    ).fetchone()
                    if not group_row:
                        continue
                    try:
                        conn.execute(
                            """INSERT INTO user_group_members
                               (user_id, group_id, source, added_by)
                               VALUES (?, ?, 'google_sync', 'system:v13-backfill')""",
                            [user_id, group_row[0]],
                        )
                    except duckdb.ConstraintException:
                        logger.debug(
                            "v13 backfill step 2 (google_sync): skipped insert for user=%s group=%s — already present",
                            user_id,
                            name,
                        )

        # 3. core.admin grants → Admin membership. Tolerant of either table being
        # absent (e.g. fresh install path that skipped v8→v9).
        has_internal_roles = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'internal_roles'"
        ).fetchone()
        has_user_role_grants = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'user_role_grants'"
        ).fetchone()
        if has_internal_roles and has_user_role_grants:
            admin_users = conn.execute(
                """SELECT DISTINCT g.user_id
                   FROM user_role_grants g
                   JOIN internal_roles r ON r.id = g.internal_role_id
                   WHERE r.key = 'core.admin'"""
            ).fetchall()
            for (user_id,) in admin_users:
                try:
                    conn.execute(
                        """INSERT INTO user_group_members
                           (user_id, group_id, source, added_by)
                           VALUES (?, ?, 'system_seed', 'system:v13-backfill')""",
                        [user_id, admin_group_id],
                    )
                except duckdb.ConstraintException:
                    logger.debug(
                        "v13 backfill step 3 (admin system_seed): skipped "
                        "insert for user=%s — already in Admin group "
                        "(possibly from step 2 google_sync of 'Admin' "
                        "Workspace group; system_seed intent is dropped)",
                        user_id,
                    )

        # 4. Everyone for every user (idempotent via UNIQUE PK).
        user_rows = conn.execute("SELECT id FROM users").fetchall()
        for (user_id,) in user_rows:
            try:
                conn.execute(
                    """INSERT INTO user_group_members
                       (user_id, group_id, source, added_by)
                       VALUES (?, ?, 'system_seed', 'system:v13-backfill')""",
                    [user_id, everyone_group_id],
                )
            except duckdb.ConstraintException:
                logger.debug(
                    "v13 backfill step 4 (everyone system_seed): skipped "
                    "insert for user=%s — already in Everyone group "
                    "(possibly from step 2 google_sync of 'Everyone' "
                    "Workspace group; system_seed intent is dropped)",
                    user_id,
                )

        # 5. plugin_access → resource_grants
        has_plugin_access = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'plugin_access'"
        ).fetchone()
        if has_plugin_access:
            pa_rows = conn.execute(
                """SELECT group_id, marketplace_id, plugin_name, granted_at, granted_by
                   FROM plugin_access"""
            ).fetchall()
            for group_id, marketplace_id, plugin_name, granted_at, granted_by in pa_rows:
                resource_id = f"{marketplace_id}/{plugin_name}"
                try:
                    conn.execute(
                        """INSERT INTO resource_grants
                           (id, group_id, resource_type, resource_id, assigned_at, assigned_by)
                           VALUES (?, ?, 'marketplace_plugin', ?, ?, ?)""",
                        [str(_uuid.uuid4()), group_id, resource_id, granted_at, granted_by],
                    )
                except duckdb.ConstraintException:
                    logger.debug(
                        "v13 backfill step 5 (resource_grants): skipped "
                        "insert for group=%s resource=%s — already migrated",
                        group_id,
                        resource_id,
                    )

        # Audit: log any non-core capability grants before dropping the
        # legacy tables. No production caller in this repo ever registered
        # non-core roles via register_internal_role (verified across git
        # history) — this is a safety net for forked installs that may
        # have added custom rows. Operators see a warning naming each
        # affected role + count, so they can re-issue the equivalent
        # grants in the v13 group-based model.
        if has_internal_roles and has_user_role_grants:
            non_core_rows = conn.execute(
                """SELECT r.key, COUNT(*) AS cnt
                   FROM user_role_grants g
                   JOIN internal_roles r ON r.id = g.internal_role_id
                   WHERE r.key NOT LIKE 'core.%'
                   GROUP BY r.key"""
            ).fetchall()
            for role_key, cnt in non_core_rows:
                logger.warning(
                    "v13 migration: dropping %d grant(s) for non-core role "
                    "'%s' (no equivalent in the v13 group-based model). "
                    "If this role was registered via register_internal_role(), "
                    "the affected users need to be re-added to an "
                    "appropriate user_group post-upgrade.",
                    cnt,
                    role_key,
                )

        # 6. Drop legacy tables in FK-correct order: dependent tables first.
        for stmt in [
            "DROP TABLE IF EXISTS plugin_access",
            "DROP TABLE IF EXISTS user_role_grants",
            "DROP TABLE IF EXISTS group_mappings",
            "DROP TABLE IF EXISTS internal_roles",
        ]:
            try:
                conn.execute(stmt)
            except Exception as e:
                logger.warning("v13 drop failed (%s): %s", stmt, e)

        # 7. Drop users.groups column. DuckDB supports DROP COLUMN; silently no-op
        # if it's already gone (fresh-install path or partial re-run).
        if has_groups_col:
            try:
                conn.execute("ALTER TABLE users DROP COLUMN groups")
            except Exception as e:
                logger.warning("v13 ALTER users DROP COLUMN groups failed: %s", e)

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _v13_to_v14_finalize(conn: duckdb.DuckDBPyConnection) -> None:
    """Add FOREIGN KEY (group_id) → user_groups(id) on user_group_members
    and resource_grants.

    DuckDB does not support ALTER TABLE ADD CONSTRAINT for foreign keys, so
    the migration recreates each table:

        1. Pre-clean orphan rows (group_id no longer in user_groups).
           These should not exist on a clean v13 DB but the app-layer
           cascade was best-effort before this PR (see #3).
        2. RENAME old table to *_v13_pre.
        3. CREATE TABLE with the FK (matches the v14 _SYSTEM_SCHEMA).
        4. INSERT … SELECT from *_v13_pre.
        5. DROP *_v13_pre.

    Wrapped in BEGIN TRANSACTION so a mid-flight failure rolls back to
    a clean v13 state and the outer caller skips the schema_version bump.
    DuckDB does NOT support ON DELETE CASCADE — see _SYSTEM_SCHEMA above
    and app/api/access.py:delete_group for the explicit cascade.
    """
    orphan_members = conn.execute(
        """SELECT COUNT(*) FROM user_group_members
           WHERE group_id NOT IN (SELECT id FROM user_groups)"""
    ).fetchone()[0]
    orphan_grants = conn.execute(
        """SELECT COUNT(*) FROM resource_grants
           WHERE group_id NOT IN (SELECT id FROM user_groups)"""
    ).fetchone()[0]
    if orphan_members:
        logger.warning(
            "v14 migration: dropping %d orphan user_group_members rows (group_id pointed at a deleted user_groups.id)",
            orphan_members,
        )
    if orphan_grants:
        logger.warning(
            "v14 migration: dropping %d orphan resource_grants rows",
            orphan_grants,
        )

    conn.execute("BEGIN TRANSACTION")
    try:
        # Orphan cleanup must happen inside the transaction so it rolls
        # back together with the table swap on any failure.
        conn.execute(
            """DELETE FROM user_group_members
               WHERE group_id NOT IN (SELECT id FROM user_groups)"""
        )
        conn.execute(
            """DELETE FROM resource_grants
               WHERE group_id NOT IN (SELECT id FROM user_groups)"""
        )

        # user_group_members rebuild
        conn.execute("ALTER TABLE user_group_members RENAME TO user_group_members_v13_pre")
        conn.execute(
            """CREATE TABLE user_group_members (
                user_id   VARCHAR NOT NULL,
                group_id  VARCHAR NOT NULL REFERENCES user_groups(id),
                source    VARCHAR NOT NULL,
                added_at  TIMESTAMP DEFAULT current_timestamp,
                added_by  VARCHAR,
                PRIMARY KEY (user_id, group_id)
            )"""
        )
        conn.execute(
            """INSERT INTO user_group_members
               (user_id, group_id, source, added_at, added_by)
               SELECT user_id, group_id, source, added_at, added_by
               FROM user_group_members_v13_pre"""
        )
        conn.execute("DROP TABLE user_group_members_v13_pre")

        # resource_grants rebuild
        conn.execute("ALTER TABLE resource_grants RENAME TO resource_grants_v13_pre")
        conn.execute(
            """CREATE TABLE resource_grants (
                id            VARCHAR PRIMARY KEY,
                group_id      VARCHAR NOT NULL REFERENCES user_groups(id),
                resource_type VARCHAR NOT NULL,
                resource_id   VARCHAR NOT NULL,
                assigned_at   TIMESTAMP DEFAULT current_timestamp,
                assigned_by   VARCHAR,
                UNIQUE (group_id, resource_type, resource_id)
            )"""
        )
        conn.execute(
            """INSERT INTO resource_grants
               (id, group_id, resource_type, resource_id, assigned_at, assigned_by)
               SELECT id, group_id, resource_type, resource_id, assigned_at, assigned_by
               FROM resource_grants_v13_pre"""
        )
        conn.execute("DROP TABLE resource_grants_v13_pre")

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _v17_to_v18_finalize(conn: duckdb.DuckDBPyConnection) -> None:
    """Drop stranded non-google memberships from google-managed groups.

    Two classes of cruft:

    1. Auto-created google_sync groups (``created_by='system:google-sync'``)
       only exist because Google sync materialized them on a Workspace claim.
       Anyone in such a group whose membership is NOT ``source='google_sync'``
       got there by an obsolete code path; drop them unconditionally — the
       Workspace state is the source of truth for these rows.

    2. Seeded ``Admin`` / ``Everyone`` rows are env-conditional. When
       ``AGNES_GROUP_ADMIN_EMAIL`` / ``AGNES_GROUP_EVERYONE_EMAIL`` is set
       the row mirrors a Workspace group exclusively, and v13's
       ``system:v13-backfill`` writes (one row per existing user into
       Everyone, one per ``core.admin``-grantee into Admin) are stranded
       cruft that ``_is_sso_user`` mis-classifies as SSO membership. Drop
       those ``system_seed`` rows. The bootstrap admin's Admin membership
       is preserved by the ``added_by`` allow-list — it must survive so
       the operator never loses console access.

       When the env mapping is absent, those system rows are LOCAL groups,
       and ``system:v13-backfill`` rows are legitimate (the user's
       core.admin grant was migrated into Admin-group membership, and
       every user is auto-broadcast into Everyone). Touching them would
       remove admin privileges or empty Everyone — so the env-conditional
       branches are skipped.

    Env vars are read at migration time via os.environ — operators
    flipping the mapping later don't need a fresh migration.
    """
    # Non-google memberships in auto-created google_sync groups: always cruft.
    conn.execute(
        """DELETE FROM user_group_members
           WHERE source != 'google_sync'
             AND group_id IN (
                 SELECT id FROM user_groups
                 WHERE created_by = 'system:google-sync'
             )"""
    )

    if os.environ.get("AGNES_GROUP_EVERYONE_EMAIL", "").strip():
        conn.execute(
            """DELETE FROM user_group_members
               WHERE source = 'system_seed'
                 AND group_id IN (
                     SELECT id FROM user_groups
                     WHERE name = 'Everyone' AND is_system
                 )"""
        )

    if os.environ.get("AGNES_GROUP_ADMIN_EMAIL", "").strip():
        conn.execute(
            """DELETE FROM user_group_members
               WHERE source = 'system_seed'
                 AND added_by NOT IN ('app.main:seed_admin', 'auth.bootstrap')
                 AND group_id IN (
                     SELECT id FROM user_groups
                     WHERE name = 'Admin' AND is_system
                 )"""
        )


def _v18_to_v19_finalize(conn: duckdb.DuckDBPyConnection) -> None:
    """Drop legacy data-RBAC tables + dead columns.

    Removes:
      - ``dataset_permissions`` table (per-user grants — replaced by per-group
        ``resource_grants(resource_type='table')``)
      - ``access_requests`` table (self-service request/approve flow — removed,
        users contact admin out-of-band)
      - ``users.role`` column (NULL artifact since v13 — auth derives from
        ``user_group_members`` via ``is_user_admin``)
      - ``table_registry.is_public`` column (bypass shortcut with no
        API/UI/CLI surface — every table now requires explicit
        ``resource_grants`` row, admin override aside)

    DuckDB ALTER TABLE DROP COLUMN can be blocked by historic FK
    constraints, so the column drops use a table-rebuild idiom (rename →
    create new → INSERT … SELECT → drop old). The INSERT picks the
    intersection of the legacy and v19 column sets so test fixtures that
    hand-craft minimal pre-v19 schemas (e.g. without `sync_strategy` /
    `primary_key`) still migrate cleanly. Wrapped in BEGIN/COMMIT;
    on error ROLLBACK and the outer caller skips the schema_version bump.
    """

    def _existing_cols(table: str) -> set[str]:
        return {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
                [table],
            ).fetchall()
        }

    conn.execute("BEGIN TRANSACTION")
    try:
        # 1 + 2: legacy table drops. IF EXISTS guards against fresh installs
        # where _SYSTEM_SCHEMA never created them (v19+ shape).
        conn.execute("DROP TABLE IF EXISTS dataset_permissions")
        conn.execute("DROP TABLE IF EXISTS access_requests")

        # 3: rebuild users without `role` column. Skip when the column
        # never existed (fresh install on v19+ schema or test fixtures
        # that hand-crafted a minimal users table without it).
        if "role" in _existing_cols("users"):
            conn.execute("ALTER TABLE users RENAME TO users_v18_pre")
            conn.execute(
                """CREATE TABLE users (
                    id VARCHAR PRIMARY KEY,
                    email VARCHAR UNIQUE NOT NULL,
                    name VARCHAR,
                    password_hash VARCHAR,
                    setup_token VARCHAR,
                    setup_token_created TIMESTAMP,
                    reset_token VARCHAR,
                    reset_token_created TIMESTAMP,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    deactivated_at TIMESTAMP,
                    deactivated_by VARCHAR,
                    created_at TIMESTAMP DEFAULT current_timestamp,
                    updated_at TIMESTAMP
                )"""
            )
            users_target_cols = [
                "id",
                "email",
                "name",
                "password_hash",
                "setup_token",
                "setup_token_created",
                "reset_token",
                "reset_token_created",
                "active",
                "deactivated_at",
                "deactivated_by",
                "created_at",
                "updated_at",
            ]
            old_users_cols = _existing_cols("users_v18_pre")
            common = [c for c in users_target_cols if c in old_users_cols]
            col_list = ", ".join(common)
            conn.execute(f"INSERT INTO users ({col_list}) SELECT {col_list} FROM users_v18_pre")
            conn.execute("DROP TABLE users_v18_pre")

        # 4: rebuild table_registry without `is_public` column.
        if "is_public" in _existing_cols("table_registry"):
            # v49: _SYSTEM_SCHEMA runs before the migration ladder and
            # creates ``data_package_tables`` with a FK pointing at
            # table_registry(id). DuckDB blocks the RENAME until the
            # dependent is dropped — drop it and recreate after the swap.
            # The v49 finalize re-establishes data_package_tables, and the
            # body of this migration's INSERT … SELECT preserves all
            # registry rows so the recreated FK won't dangle. Saved-rows
            # in data_package_tables also stay valid (they reference
            # table_registry.id which is preserved verbatim).
            data_pkg_existed = False
            pkg_rows = []
            try:
                pkg_rows = conn.execute(
                    "SELECT package_id, table_id, added_at, added_by FROM data_package_tables"
                ).fetchall()
                data_pkg_existed = True
                conn.execute("DROP TABLE data_package_tables")
            except duckdb.Error:
                # Table doesn't exist (pre-v49 DB or hand-crafted fixture);
                # the v49 migration body will create it later.
                pass

            conn.execute("ALTER TABLE table_registry RENAME TO table_registry_v18_pre")
            conn.execute(
                """CREATE TABLE table_registry (
                    id VARCHAR PRIMARY KEY,
                    name VARCHAR NOT NULL,
                    source_type VARCHAR,
                    bucket VARCHAR,
                    source_table VARCHAR,
                    sync_strategy VARCHAR DEFAULT 'full_refresh',
                    query_mode VARCHAR DEFAULT 'local',
                    sync_schedule VARCHAR,
                    profile_after_sync BOOLEAN DEFAULT true,
                    primary_key VARCHAR,
                    folder VARCHAR,
                    description TEXT,
                    registered_by VARCHAR,
                    registered_at TIMESTAMP DEFAULT current_timestamp
                )"""
            )
            registry_target_cols = [
                "id",
                "name",
                "source_type",
                "bucket",
                "source_table",
                "sync_strategy",
                "query_mode",
                "sync_schedule",
                "profile_after_sync",
                "primary_key",
                "folder",
                "description",
                "registered_by",
                "registered_at",
            ]
            old_registry_cols = _existing_cols("table_registry_v18_pre")
            common = [c for c in registry_target_cols if c in old_registry_cols]
            col_list = ", ".join(common)
            conn.execute(f"INSERT INTO table_registry ({col_list}) SELECT {col_list} FROM table_registry_v18_pre")
            conn.execute("DROP TABLE table_registry_v18_pre")

            # Recreate the v49 junction + restore any rows we captured.
            if data_pkg_existed:
                conn.execute(
                    """CREATE TABLE data_package_tables (
                        package_id  VARCHAR NOT NULL REFERENCES data_packages(id),
                        table_id    VARCHAR NOT NULL REFERENCES table_registry(id),
                        added_at    TIMESTAMP DEFAULT current_timestamp,
                        added_by    VARCHAR,
                        PRIMARY KEY (package_id, table_id)
                    )"""
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_data_package_tables_table ON data_package_tables(table_id)"
                )
                for row in pkg_rows:
                    conn.execute(
                        "INSERT INTO data_package_tables(package_id, table_id, added_at, added_by) VALUES (?, ?, ?, ?)",
                        list(row),
                    )

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _seed_core_roles(conn: duckdb.DuckDBPyConnection) -> None:
    """Idempotently insert/refresh the four core.* hierarchy roles.

    Called from _ensure_schema on every system-DB connect (the unconditional
    tail call below the migration guard) — fresh installs need the rows to
    exist before any user_role_grants can reference them, and existing DBs
    benefit from the safety net if a deployment somehow loses a row
    (e.g. accidental admin DELETE). Implies field is rewritten on every call
    to keep the hierarchy in sync with code; display_name + description are
    rewritten too so a doc tweak deploys without manual SQL.
    """
    import json as _json
    import uuid as _uuid

    for key, display_name, description, implies in _CORE_ROLES_SEED:
        existing = conn.execute("SELECT id FROM internal_roles WHERE key = ?", [key]).fetchone()
        implies_json = _json.dumps(implies)
        if existing:
            conn.execute(
                """UPDATE internal_roles
                   SET display_name = ?, description = ?, implies = ?,
                       is_core = true, owner_module = 'core',
                       updated_at = current_timestamp
                   WHERE id = ?""",
                [display_name, description, implies_json, existing[0]],
            )
        else:
            conn.execute(
                """INSERT INTO internal_roles
                   (id, key, display_name, description, owner_module, implies, is_core)
                   VALUES (?, ?, ?, ?, 'core', ?, true)""",
                [str(_uuid.uuid4()), key, display_name, description, implies_json],
            )


def _backfill_users_role_to_grants(conn: duckdb.DuckDBPyConnection) -> None:
    """One-shot: convert legacy users.role values into user_role_grants rows.

    Runs as part of the v8→v9 migration, after _seed_core_roles populated the
    target internal_roles rows and before users.role is dropped. Idempotent
    via the (user_id, internal_role_id) UNIQUE constraint — re-run is safe.
    """
    import uuid as _uuid

    # Verify users.role column still exists (we may be re-running after a
    # half-applied migration); skip silently if it's already gone.
    has_role_col = conn.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name = 'users' AND column_name = 'role'"
    ).fetchone()
    if not has_role_col:
        return

    rows = conn.execute("SELECT id, role FROM users WHERE role IS NOT NULL").fetchall()
    backfilled = 0
    for user_id, role_str in rows:
        role_key = _LEGACY_ROLE_TO_CORE_KEY.get(role_str, "core.viewer")
        role_row = conn.execute("SELECT id FROM internal_roles WHERE key = ?", [role_key]).fetchone()
        if not role_row:
            logger.warning(
                "v9 backfill: core role %s missing — skipping user %s",
                role_key,
                user_id,
            )
            continue
        try:
            conn.execute(
                """INSERT INTO user_role_grants
                   (id, user_id, internal_role_id, granted_by, source)
                   VALUES (?, ?, ?, 'system:v9-backfill', 'auto-seed')""",
                [str(_uuid.uuid4()), user_id, role_row[0]],
            )
            backfilled += 1
        except duckdb.ConstraintException:
            pass  # already granted (idempotent re-run)
    if backfilled:
        logger.info(
            "v9 backfill: seeded user_role_grants for %d existing user(s)",
            backfilled,
        )


_V3_TO_V4_MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS metric_definitions (
        id              VARCHAR PRIMARY KEY,
        name            VARCHAR NOT NULL,
        display_name    VARCHAR NOT NULL,
        category        VARCHAR NOT NULL,
        description     TEXT,
        type            VARCHAR DEFAULT 'sum',
        unit            VARCHAR,
        grain           VARCHAR DEFAULT 'monthly',
        table_name      VARCHAR,
        tables          VARCHAR[],
        expression      VARCHAR,
        time_column     VARCHAR,
        dimensions      VARCHAR[],
        filters         VARCHAR[],
        synonyms        VARCHAR[],
        notes           VARCHAR[],
        sql             TEXT NOT NULL,
        sql_variants    JSON,
        validation      JSON,
        source          VARCHAR DEFAULT 'manual',
        source_ref      VARCHAR,
        created_at      TIMESTAMP DEFAULT current_timestamp,
        updated_at      TIMESTAMP DEFAULT current_timestamp
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS column_metadata (
        table_id        VARCHAR NOT NULL,
        column_name     VARCHAR NOT NULL,
        basetype        VARCHAR,
        description     VARCHAR,
        confidence      VARCHAR DEFAULT 'manual',
        source          VARCHAR DEFAULT 'manual',
        updated_at      TIMESTAMP DEFAULT current_timestamp,
        PRIMARY KEY (table_id, column_name)
    )
    """,
]

_V20_TO_V21_MIGRATIONS = [
    """CREATE TABLE IF NOT EXISTS welcome_template (
        id INTEGER PRIMARY KEY DEFAULT 1,
        content TEXT,
        updated_at TIMESTAMP,
        updated_by VARCHAR,
        CONSTRAINT singleton CHECK (id = 1)
    )""",
    "INSERT INTO welcome_template (id, content) VALUES (1, NULL) ON CONFLICT (id) DO NOTHING",
]

_V21_TO_V22_MIGRATIONS = [
    """CREATE TABLE IF NOT EXISTS setup_banner (
        id INTEGER PRIMARY KEY DEFAULT 1,
        content TEXT,
        updated_at TIMESTAMP,
        updated_by VARCHAR,
        CONSTRAINT singleton CHECK (id = 1)
    )""",
    "INSERT INTO setup_banner (id, content) VALUES (1, NULL) ON CONFLICT (id) DO NOTHING",
]

_V22_TO_V23_MIGRATIONS = [
    """CREATE TABLE IF NOT EXISTS claude_md_template (
        id INTEGER PRIMARY KEY DEFAULT 1,
        content TEXT,
        updated_at TIMESTAMP,
        updated_by VARCHAR,
        CONSTRAINT singleton CHECK (id = 1)
    )""",
    "INSERT INTO claude_md_template (id, content) VALUES (1, NULL) ON CONFLICT (id) DO NOTHING",
]

# v25: store + opt-out tables backing the flea-market and my-stack views
# (now served at /marketplace?tab=flea + /marketplace?tab=my; the v25-era
# standalone /store and /my-ai-stack page routes were dropped post-v25).
_V24_TO_V25_MIGRATIONS = [
    # FK refs deliberately omitted — see the matching note in _SYSTEM_SCHEMA.
    """
    CREATE TABLE IF NOT EXISTS store_entities (
        id              VARCHAR PRIMARY KEY,
        owner_user_id   VARCHAR NOT NULL,
        owner_username  VARCHAR NOT NULL,
        type            VARCHAR NOT NULL CHECK (type IN ('skill','agent','plugin')),
        name            VARCHAR NOT NULL,
        description     TEXT,
        category        VARCHAR,
        version         VARCHAR NOT NULL,
        photo_path      VARCHAR,
        video_url       VARCHAR,
        doc_paths       JSON,
        file_size       BIGINT,
        install_count   BIGINT NOT NULL DEFAULT 0,
        created_at      TIMESTAMP DEFAULT current_timestamp,
        updated_at      TIMESTAMP DEFAULT current_timestamp,
        UNIQUE (owner_user_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_store_installs (
        user_id      VARCHAR NOT NULL,
        entity_id    VARCHAR NOT NULL,
        installed_at TIMESTAMP DEFAULT current_timestamp,
        PRIMARY KEY (user_id, entity_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_plugin_optouts (
        user_id        VARCHAR NOT NULL,
        marketplace_id VARCHAR NOT NULL,
        plugin_name    VARCHAR NOT NULL,
        opted_out_at   TIMESTAMP DEFAULT current_timestamp,
        PRIMARY KEY (user_id, marketplace_id, plugin_name)
    )
    """,
]


# v26: unify Keboola query_mode='local' rows into 'materialized'.
#
# The old `local` flow ran the DuckDB Keboola extension's COPY through
# QueryService — which is unreliable on linked-bucket projects (and was
# wholly broken pre-v0.1.6 of the extension). The new `materialized`
# flow uses the Storage API export-async path directly:
#   POST /v2/storage/tables/<id>/export-async
#   GET  /v2/storage/jobs/<id>  (poll)
#   GET  /v2/storage/files/<id>?federationToken=1  (signed URL)
#   download → CSV → parquet
# That works regardless of project flags, and a NULL `source_query`
# means "full table export" — same effective behavior the `local` mode
# previously gave.
#
# Existing Keboola rows registered as `query_mode='local'` are flipped
# to 'materialized'; their source_query stays NULL (full table). Jira
# and BigQuery 'local' rows are untouched (this connector still uses
# its own path).
_V25_TO_V26_MIGRATIONS = [
    """
    UPDATE table_registry
    SET query_mode = 'materialized'
    WHERE source_type = 'keboola' AND query_mode = 'local'
    """,
]


# v27: Keboola sync-strategy support columns on table_registry.
#
# Layered on top of v26's local→materialized unification. Admins can opt
# specific Keboola tables back to `query_mode='local'` (via the Direct
# extract Edit-modal radio) to enable the new sync_strategy dispatcher.
# The existing `sync_strategy` column (default 'full_refresh') drives one
# of {'full_refresh', 'incremental', 'partitioned'} from v27 onward. The
# seven columns added here are the per-strategy knobs:
#   - incremental_window_days: backtrack window applied to last_sync (default 7)
#   - max_history_days: cap on first-sync history depth
#   - incremental_column: reserved for future use when changedSince's
#     lastChangeDate isn't the right mutation column for a table
#   - where_filters: JSON array of {column, operator, values} filter entries
#     resolved at sync time (date placeholders like {{last_3_months}})
#   - partition_by: column whose value drives the partition key
#   - partition_granularity: 'day' | 'month' | 'year'
#   - initial_load_chunk_days: chunked initial-load step size (default 30)
#
# All NULL on existing rows → no behavior change for tables that don't
# opt in. v26's local→materialized flip preserves the migration-correct
# behavior for the default case; new-or-edited rows that pick Direct
# extract land at `query_mode='local'` again with sync_strategy in play.
# API-layer validators enforce per-strategy required-field combinations
# (e.g. partitioned ⇒ partition_by required) and reject conflicting combos
# (e.g. incremental + where_filters → 422).
_V26_TO_V27_MIGRATIONS = [
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS incremental_window_days INTEGER",
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS max_history_days INTEGER",
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS incremental_column VARCHAR",
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS where_filters VARCHAR",
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS partition_by VARCHAR",
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS partition_granularity VARCHAR",
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS initial_load_chunk_days INTEGER",
]


# v28 (upstream): explicit-install (Model B) for curated marketplace plugins.
#
# Pre-v28 the served set was (rbac ∖ user_plugin_optouts) — a curated plugin
# the admin granted appeared in the user's marketplace until the user opted
# out via the my-stack view. From v28 the served set is (rbac ∩ subscriptions)
# — users explicitly install each curated plugin from /marketplace.
#
# We keep the table+column names (`user_plugin_optouts.opted_out_at`) to
# avoid DDL churn on running operator instances. Row PRESENCE flips meaning
# from "excluded" to "subscribed", so we wipe rows so the inverted reading
# starts from a clean baseline. Users will re-install via /marketplace.
#
# Also adds marketplace_plugins.created_at (per-plugin "newest first" sort
# on /marketplace). Backfilled from parent marketplace_registry.registered_at
# so existing plugins get a sensible date until the next sync overwrites
# with CURRENT_TIMESTAMP.
_V27_TO_V28_MIGRATIONS = [
    "DELETE FROM user_plugin_optouts",
    # IF NOT EXISTS guard: `_SYSTEM_SCHEMA` runs before the migration ladder
    # and creates `marketplace_plugins` with the full current-version
    # column set (including `created_at`) on fresh installs that come up
    # at any pre-v28 version via test fixtures. The ALTER would then trip
    # on an existing column. Same idiom as upstream `_V26_TO_V27_MIGRATIONS`.
    "ALTER TABLE marketplace_plugins ADD COLUMN IF NOT EXISTS created_at TIMESTAMP",
    """
    UPDATE marketplace_plugins
       SET created_at = (
           SELECT registered_at FROM marketplace_registry
            WHERE marketplace_registry.id = marketplace_plugins.marketplace_id
       )
     WHERE created_at IS NULL
    """,
]


# v29: /home page rollout. Two changes bundled because they ship together.
# (Originally drafted as v26 / v28 across rebases; landed at v29 after
# upstream's marketplace v28.)
#
#   1. instance_templates(key, content, ...) consolidates the v21
#      welcome_template + v23 claude_md_template singletons into one shape so
#      future operator-customizable surfaces ship as a row insert + admin-UI
#      section, not a fresh schema bump.
#
#      Migration semantics: CREATE the new table, INSERT existing rows from the
#      legacy tables (preserving content + updated_at + updated_by), DROP the
#      legacy tables. The CREATE+seed pure-SQL portion lives in this list;
#      the conditional INSERT-from-legacy + DROP lives in
#      _v28_to_v29_finalize() below because it needs information_schema
#      lookups to handle both fresh-install (no legacy tables) and existing
#      paths cleanly.
#
#   2. users.onboarded BOOLEAN NOT NULL DEFAULT FALSE — feeds the /home
#      state-aware landing. Default FALSE for everyone on migration; explicit
#      signal (`POST /api/me/onboarded` from `agnes init` success or the
#      self-mark button on the not-onboarded view) flips it to TRUE.
_V28_TO_V29_MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS instance_templates (
        key VARCHAR PRIMARY KEY,
        content TEXT,
        previous_content TEXT,
        updated_at TIMESTAMP,
        updated_by VARCHAR
    )
    """,
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarded BOOLEAN DEFAULT FALSE",
    # Backfill any pre-existing NULL onboarded values to FALSE so the column
    # carries the documented "default FALSE" semantics for legacy users
    # (DuckDB ADD COLUMN with DEFAULT applies to new INSERTs but leaves
    # existing rows NULL — UPDATE here closes that gap).
    "UPDATE users SET onboarded = FALSE WHERE onboarded IS NULL",
]


def _v28_to_v29_finalize(conn) -> None:
    """Migrate legacy welcome_template + claude_md_template rows into
    instance_templates, then drop the legacy tables.

    Runs after _V28_TO_V29_MIGRATIONS creates the new table. Idempotent:
    re-running on an already-v29 DB is a no-op because the legacy tables
    are gone after the first run and the seed INSERTs use ON CONFLICT.
    """
    has_welcome = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema = 'main' AND table_name = 'welcome_template'"
    ).fetchone()
    if has_welcome:
        conn.execute(
            "INSERT INTO instance_templates (key, content, updated_at, updated_by) "
            "SELECT 'welcome', content, updated_at, updated_by FROM welcome_template "
            "ON CONFLICT (key) DO NOTHING"
        )
        conn.execute("DROP TABLE welcome_template")

    has_claude_md = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema = 'main' AND table_name = 'claude_md_template'"
    ).fetchone()
    if has_claude_md:
        conn.execute(
            "INSERT INTO instance_templates (key, content, updated_at, updated_by) "
            "SELECT 'claude_md', content, updated_at, updated_by FROM claude_md_template "
            "ON CONFLICT (key) DO NOTHING"
        )
        conn.execute("DROP TABLE claude_md_template")

    # Seed the canonical key set with NULL content. The INSERTs are no-ops if
    # the keys already landed via the legacy migration above (existing
    # operators) or via a prior migration run (idempotent re-execution).
    for key in ("welcome", "claude_md", "home"):
        conn.execute(
            "INSERT INTO instance_templates (key, content) VALUES (?, NULL) ON CONFLICT (key) DO NOTHING",
            [key],
        )


_V29_TO_V30_MIGRATIONS = [
    # news_template: single table holding every saved version of the /home
    # news perex + /news full body. `version` monotonically increases per
    # save. `published` distinguishes the active draft (FALSE) from public
    # versions (TRUE). Web reads `WHERE published = TRUE ORDER BY version
    # DESC LIMIT 1`. Admin can browse all rows.
    """
    CREATE TABLE IF NOT EXISTS news_template (
        id              VARCHAR PRIMARY KEY,
        version         INTEGER NOT NULL UNIQUE,
        intro           TEXT,
        content         TEXT,
        published       BOOLEAN NOT NULL DEFAULT FALSE,
        created_at      TIMESTAMP NOT NULL DEFAULT current_timestamp,
        updated_at      TIMESTAMP NOT NULL DEFAULT current_timestamp,
        created_by      VARCHAR,
        published_at    TIMESTAMP,
        published_by    VARCHAR
    )
    """,
    # Composite index supports both `WHERE published = TRUE ORDER BY version
    # DESC LIMIT 1` (the hot read path on every /home + /news request) and
    # full-table version listing in the admin UI.
    """
    CREATE INDEX IF NOT EXISTS ix_news_template_pub_ver
        ON news_template (published, version DESC)
    """,
]


# v32: flea-market upload guardrails — create store_submissions table +
# add visibility_status to store_entities. (Originally drafted as v29
# but renumbered to v32 after rebase onto upstream's v29/v30/v31.)
#
#   * `store_entities.visibility_status` (default 'pending'). Existing
#     rows backfill to 'approved' so live uploads survive the upgrade —
#     the guardrail pipeline only gates NEW submissions.
#   * `store_submissions` table holds the per-upload audit trail
#     powering /admin/store/submissions. The CREATE lives in
#     _SYSTEM_SCHEMA; this migration only adds the column + backfill.
#
# IF NOT EXISTS guard on the ALTER mirrors v27/v28 — fresh installs at
# pre-v32 (test fixtures) come up with the column already present via
# _SYSTEM_SCHEMA.
_V31_TO_V32_MIGRATIONS = [
    "ALTER TABLE store_entities ADD COLUMN IF NOT EXISTS visibility_status VARCHAR",
    "UPDATE store_entities SET visibility_status = 'approved' WHERE visibility_status IS NULL",
]


# v33: forensic columns on store_submissions — file_size, bundle_sha256,
# bundle_purged_at. Underpins persist-blocked-bundle behavior: blocked
# uploads keep the bundle on disk so admins can Rescan / Override /
# Download. The 30-day TTL purge then clears bytes while leaving the
# row + sha intact for forensic correlation. file_size on existing rows
# is backfilled from the linked entity row (when present);
# bundle_sha256 stays NULL on legacy rows since we no longer have the
# bytes to hash. Renumbered from v30 → v33 after rebase onto upstream's
# v29/v30/v31 sequence.
_V32_TO_V33_MIGRATIONS = [
    "ALTER TABLE store_submissions ADD COLUMN IF NOT EXISTS file_size BIGINT",
    "ALTER TABLE store_submissions ADD COLUMN IF NOT EXISTS bundle_sha256 VARCHAR",
    "ALTER TABLE store_submissions ADD COLUMN IF NOT EXISTS bundle_purged_at TIMESTAMP",
    """
    UPDATE store_submissions
       SET file_size = (
           SELECT file_size FROM store_entities
            WHERE store_entities.id = store_submissions.entity_id
       )
     WHERE file_size IS NULL AND entity_id IS NOT NULL
    """,
]


# v34: drop store_submissions.retry_count. Counter mixed two unrelated
# things (LLM error count + admin rescan count), was asymmetric (Retry
# LLM didn't bump but Rescan did), and is fully redundant with the
# audit_log timeline now rendered on the detail page — every rescan /
# retry / review_error is a row there with timestamp + actor. SELECT
# COUNT(*) FROM audit_log WHERE resource = 'store_submission:<id>' AND
# action IN (…) gives the same number when an admin actually wants it.
# v35: store_entities gains 'archived' as a fourth visibility state +
# audit columns (archived_at, archived_by). Owner soft-delete writes
# this state instead of dropping the row; existing user_store_installs
# keep serving the bundle through marketplace.zip / .git so already-
# installed users don't lose the plugin. Hard delete (admin only via
# DELETE ?hard=true) remains the path for legal / privacy removals.
#
# DuckDB doesn't support ALTER COLUMN ADD CHECK in-place; the existing
# CHECK constraint allows {pending, approved, hidden}. Workaround:
# rebuild via column-rebuild — but DuckDB DROP COLUMN can fail on
# indexed tables (we hit this in v34). Easier: drop the CHECK constraint
# implicitly by not relying on it (the application validates via
# StoreEntitiesRepository.set_visibility), and just add the new
# columns. The CHECK still rejects 'archived' on inserts via DuckDB
# but DuckDB's CHECK constraint is informational on existing tables
# under ALTER — verify in migration testing.
#
# Concretely: drop and re-add the visibility_status column rebuilt
# without the CHECK, OR use ALTER TABLE … DROP CONSTRAINT. DuckDB
# supports neither cleanly on the indexed `store_entities`. Workaround:
# rename to a temp column, copy values, drop original, rename back.
# Two-step: first add the new audit columns (always safe); then
# rebuild visibility_status without the CHECK so 'archived' becomes a
# valid value.
def _v34_to_v35_migrate(conn: duckdb.DuckDBPyConnection) -> None:
    """Add the ``archived`` visibility state + audit columns to ``store_entities``.

    Replaces the old list-form ``_V34_TO_V35_MIGRATIONS`` so the migration is
    safe to re-run after a partial failure. The original sequence was

        ADD _vis_v35 → UPDATE _vis_v35 = visibility_status →
        DROP visibility_status → RENAME _vis_v35 TO visibility_status

    which left a half-rebuilt DB stranded if step 4 (RENAME) failed after
    step 3 (DROP) succeeded: ``visibility_status`` was gone, ``_vis_v35``
    held the values, and ``schema_version`` never got bumped because the
    UPDATE at the bottom of the migration ladder never ran. Restarting
    the binary then hit step 3 again with no IF EXISTS guard and looped
    on the same DROP error.

    The new implementation inspects ``store_entities``'s columns up front
    and picks the right recovery path:

    * **clean v34 shape** (``visibility_status`` present, ``_vis_v35``
      absent) — full rebuild via copy → drop → rename, as before
    * **partial v35** (``_vis_v35`` present, ``visibility_status`` absent)
      — rebuild aborted mid-way; finish the RENAME only
    * **both columns present** (rare; aborted rebuild that didn't reach
      the DROP) — drop the temp ``_vis_v35`` and keep ``visibility_status``

    The audit columns (``archived_at``, ``archived_by``) ship first
    behind ``IF NOT EXISTS`` so they're safe in all three states.
    """
    conn.execute("ALTER TABLE store_entities ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP")
    conn.execute("ALTER TABLE store_entities ADD COLUMN IF NOT EXISTS archived_by VARCHAR")

    # If the table is already at a post-v49 shape (synthetic_name column
    # present from phase-1 Flea refactor), the v35 visibility_status rebuild
    # has effectively been done long ago AND the v50 UNIQUE INDEX on
    # synthetic_name now blocks `DROP COLUMN visibility_status` (DuckDB
    # forbids dropping a column when an index references a column after it
    # positionally). Short-circuit so re-runs of the ladder on a fully
    # migrated DB (e.g. a test that resets schema_version backwards) stay
    # idempotent.
    post_v49 = conn.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'store_entities' AND column_name = 'synthetic_name'"
    ).fetchone()
    if post_v49:
        return

    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'store_entities' "
            "  AND column_name IN ('visibility_status', '_vis_v35')"
        ).fetchall()
    }
    has_vis = "visibility_status" in cols
    has_temp = "_vis_v35" in cols

    if has_vis and not has_temp:
        # Clean v34 shape — full rebuild. NOT NULL + DEFAULT are re-applied
        # in v36 (DuckDB ALTER COLUMN supports SET NOT NULL / SET DEFAULT
        # but not ADD CHECK on an existing column). Value-list enforcement
        # is application-side via VALID_VISIBILITY in StoreEntitiesRepository.
        conn.execute("ALTER TABLE store_entities ADD COLUMN _vis_v35 VARCHAR")
        conn.execute("UPDATE store_entities SET _vis_v35 = visibility_status")
        conn.execute("ALTER TABLE store_entities DROP COLUMN visibility_status")
        conn.execute("ALTER TABLE store_entities RENAME COLUMN _vis_v35 TO visibility_status")
    elif has_temp and not has_vis:
        # Partial-rebuild recovery — prior attempt dropped visibility_status
        # but the RENAME never landed. Data is already in _vis_v35 from
        # the prior UPDATE; finish the rename.
        logger.warning(
            "v34→v35 detected partial-rebuild state (visibility_status "
            "missing, _vis_v35 present); recovering via RENAME"
        )
        conn.execute("ALTER TABLE store_entities RENAME COLUMN _vis_v35 TO visibility_status")
    elif has_vis and has_temp:
        # Both present — earlier rebuild aborted before the DROP.
        # visibility_status holds the canonical values; drop the temp.
        logger.warning(
            "v34→v35 detected partial-rebuild state (both visibility_status and _vis_v35 present); dropping the temp"
        )
        conn.execute("ALTER TABLE store_entities DROP COLUMN _vis_v35")
    # else: neither column is present, which means store_entities itself
    # is at a shape ahead of v34. _SYSTEM_SCHEMA above already created
    # the post-v35 shape; nothing to do here.


# v35→v36: re-apply NOT NULL + DEFAULT 'pending' on
# store_entities.visibility_status. Lost in v34→v35 because the column
# rebuild via ADD/UPDATE/DROP/RENAME stripped both invariants. Without
# them an INSERT that omits visibility_status lands NULL → repo
# subsequently reads None → undefined behavior in the visibility gates.
# Idempotent: SET NOT NULL is a no-op when already NOT NULL; SET DEFAULT
# replaces whatever default was set. The defensive UPDATE handles the
# theoretical case where a row got NULL between v35 and v36.
#
# Also: defensively re-applies the v28→v29 users.onboarded ADD COLUMN
# for DBs where that step was silently skipped. We've observed DBs
# whose schema_version row says 36 but whose users table is missing
# `onboarded` — the only consequence-free recovery is an idempotent
# ADD IF NOT EXISTS at the v36 step.
def _v35_to_v36_migrate(conn: duckdb.DuckDBPyConnection) -> None:
    """Idempotent v35→v36. Gates the ALTER COLUMN steps on current
    nullability — once v50 creates the UNIQUE INDEX on store_entities,
    DuckDB blocks ALTER COLUMN against the table (the index references
    a column "after" visibility_status positionally), so a redundant
    SET NOT NULL on an already-NOT-NULL column would explode."""
    conn.execute("UPDATE store_entities SET visibility_status = 'pending' WHERE visibility_status IS NULL")
    nullable = conn.execute(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name = 'store_entities' AND column_name = 'visibility_status'"
    ).fetchone()
    if nullable and nullable[0] == "YES":
        conn.execute("ALTER TABLE store_entities ALTER COLUMN visibility_status SET NOT NULL")
        conn.execute("ALTER TABLE store_entities ALTER COLUMN visibility_status SET DEFAULT 'pending'")
    conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarded BOOLEAN DEFAULT FALSE")
    conn.execute("UPDATE users SET onboarded = FALSE WHERE onboarded IS NULL")


# v37→v38: flea-market entity edit feature with version history.
#
# (Originally drafted as v37; renumbered after rebase onto main where
# v37 is taken by the curated marketplace enrichment migration.)
#
# Adds two columns to store_entities so an owner editing their plugin
# accumulates an append-only history rather than overwriting the prior
# bundle:
#
#   * version_no INTEGER  — current version index (1-based). Bumps on
#     every approved bundle update; metadata-only edits don't bump.
#   * version_history JSON — array of past version metadata entries:
#       [{"n", "hash", "sha256", "size", "submission_id",
#         "created_at", "created_by"}, …]
#     Each row's bundle bytes live on disk under
#     ``${DATA_DIR}/store/<eid>/versions/v<N>/plugin/`` so rollback can
#     copy them forward.
#
# Backfill: existing rows get version_no=1 and a single-entry
# version_history populated from the row's current ``version`` (hash)
# + ``file_size`` so post-migration entities surface as v1 in the UI.
# created_at backfilled from the entity row; submission_id is best-
# effort (we look up the most recent submission_id for the entity_id
# if any exists, else NULL).
def _v37_to_v38_migrate(conn: duckdb.DuckDBPyConnection) -> None:
    """Idempotent v37→v38. Gates the ``version_no SET NOT NULL`` step on
    current nullability — see ``_v35_to_v36_migrate`` for why."""
    # Defensive: minimal partial-state DBs from earlier migrations may
    # be missing columns the backfill UPDATE below references. Add
    # them idempotently first. Real post-v29 DBs already have these;
    # this is a no-op there. Keeps the recovery path through
    # `tests/test_db_schema_version.py::test_v32_db_with_partial_v35_recovers_through_full_ladder`
    # intact when walking from v32 fixture forward.
    conn.execute("ALTER TABLE store_entities ADD COLUMN IF NOT EXISTS version VARCHAR")
    conn.execute("ALTER TABLE store_entities ADD COLUMN IF NOT EXISTS file_size BIGINT")
    conn.execute("ALTER TABLE store_entities ADD COLUMN IF NOT EXISTS created_at TIMESTAMP")
    # DuckDB ALTER doesn't accept "NOT NULL DEFAULT" together — split:
    # ADD nullable + DEFAULT, backfill nulls, then SET NOT NULL.
    conn.execute("ALTER TABLE store_entities ADD COLUMN IF NOT EXISTS version_no INTEGER DEFAULT 1")
    conn.execute("UPDATE store_entities SET version_no = 1 WHERE version_no IS NULL")
    nullable = conn.execute(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name = 'store_entities' AND column_name = 'version_no'"
    ).fetchone()
    if nullable and nullable[0] == "YES":
        conn.execute("ALTER TABLE store_entities ALTER COLUMN version_no SET NOT NULL")
    conn.execute("ALTER TABLE store_entities ADD COLUMN IF NOT EXISTS version_history JSON DEFAULT '[]'")
    # Backfill: synthesize a v1 entry from existing columns when the
    # history is empty. Idempotent — re-running on a populated row is
    # a no-op because the WHERE filters on empty/NULL history.
    conn.execute(
        """
        UPDATE store_entities SET version_history = json_array(
            json_object(
                'n',  1,
                'hash', version,
                'sha256', NULL,
                'size', file_size,
                'submission_id', (
                    SELECT id FROM store_submissions
                     WHERE entity_id = store_entities.id
                     ORDER BY created_at DESC
                     LIMIT 1
                ),
                'created_at', CAST(created_at AS VARCHAR),
                'created_by', owner_user_id
            )
        )
        WHERE version_history IS NULL
           OR version_history = '[]'
           OR json_array_length(version_history) = 0
        """
    )


# v39: marketplace_plugins.is_system flag backing the "system plugin"
# admin tier. Plugins flipped TRUE are materialized into resource_grants
# (per group) and user_plugin_optouts (per user) by the mark_system
# endpoint; UI then locks the corresponding controls. NULL backfill
# kept defensive — DEFAULT FALSE on the column already covers fresh rows
# but the explicit UPDATE catches any pre-existing nullable column from
# partial-state DBs.
_V38_TO_V39_MIGRATIONS = [
    "ALTER TABLE marketplace_plugins ADD COLUMN IF NOT EXISTS is_system BOOLEAN DEFAULT FALSE",
    "UPDATE marketplace_plugins SET is_system = FALSE WHERE is_system IS NULL",
]


# v40: bq_metadata_cache table. Existing DBs get an empty table; the next
# scheduler tick (or app startup warmup) populates it. The catalog endpoint
# treats absence-of-row as `metadata_freshness: never_fetched` and returns
# NULL for the optional fields rather than failing — analyst tooling already
# tolerates NULL rows / size_bytes from the pre-0.47 contract.
_V39_TO_V40_MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS bq_metadata_cache (
        table_id      VARCHAR PRIMARY KEY,
        rows          BIGINT,
        size_bytes    BIGINT,
        partition_by  VARCHAR,
        clustered_by  JSON,
        entity_type   VARCHAR,
        known_columns JSON,
        refreshed_at  TIMESTAMP,
        error_at      TIMESTAMP,
        error_msg     VARCHAR
    )
    """,
    # entity_type + known_columns may be absent on instances that picked
    # up the early v40 (`bq_metadata_cache` without these columns) before
    # the field was added. IF NOT EXISTS makes the ALTERs idempotent for
    # the fresh-create path above and additive for the upgrade path.
    "ALTER TABLE bq_metadata_cache ADD COLUMN IF NOT EXISTS entity_type VARCHAR",
    "ALTER TABLE bq_metadata_cache ADD COLUMN IF NOT EXISTS known_columns JSON",
]


def _v40_to_v41(conn: duckdb.DuckDBPyConnection) -> None:
    """v41 (was v40 pre-rebase): audit_log gains params_before (JSON), client_ip
    (VARCHAR), client_kind (VARCHAR, 'cli'|'web'|'agent'|'scheduler'|'external'),
    and correlation_id (VARCHAR, groups multi-step operations).

    Three indices added on (timestamp), (user_id, timestamp), (action, timestamp)
    to keep Activity Center timeline queries under 100ms even at 100k+ rows.

    NOTE: DuckDB does not honor DESC in CREATE INDEX; the planner is free to
    scan either direction. On a populated audit_log (~100k+ rows), each
    CREATE INDEX is single-threaded and may take 10–30s.
    """
    conn.execute("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS timestamp TIMESTAMP DEFAULT current_timestamp")
    conn.execute("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS user_id VARCHAR")
    conn.execute("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS action VARCHAR")
    conn.execute("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS resource VARCHAR")
    conn.execute("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS params JSON")
    conn.execute("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS result VARCHAR")
    conn.execute("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS duration_ms INTEGER")
    conn.execute("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS params_before JSON")
    conn.execute("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS client_ip VARCHAR")
    conn.execute("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS client_kind VARCHAR")
    conn.execute("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS correlation_id VARCHAR")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_timestamp_desc ON audit_log(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_user_time ON audit_log(user_id, timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_action_time ON audit_log(action, timestamp)")


def _v41_to_v42(conn: duckdb.DuckDBPyConnection) -> None:
    """v42 (was v41 pre-rebase): 7 new usage_* tables for platform telemetry.

    - usage_events: per-event log (tool_use, slash_command, subagent, mcp_call)
      extracted from session JSONLs.
    - usage_session_summary: per-session aggregate keyed by session_file.
      session_id is NOT NULL — the processor always extracts a session_id from
      JSONL; orphan sessions are skipped before this row is written.
    - usage_tool_daily / usage_plugin_daily: daily rollups for fast marketplace
      queries.
    - usage_attribution_skills / _agents / _commands: skill/agent/command
      attribution exploded from plugin manifests; composite PKs allow the same
      name to appear in two different plugins.

    All CREATE TABLE/INDEX statements are IF NOT EXISTS — safe to re-run.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_events (
            id                  VARCHAR PRIMARY KEY,
            session_id          VARCHAR NOT NULL,
            session_file        VARCHAR NOT NULL,
            username            VARCHAR NOT NULL,
            event_uuid          VARCHAR,
            parent_uuid         VARCHAR,
            event_type          VARCHAR NOT NULL,
            tool_name           VARCHAR,
            skill_name          VARCHAR,
            subagent_type       VARCHAR,
            command_name        VARCHAR,
            is_error            BOOLEAN DEFAULT FALSE,
            source              VARCHAR NOT NULL,
            ref_id              VARCHAR,
            model               VARCHAR,
            cwd                 VARCHAR,
            occurred_at         TIMESTAMP NOT NULL,
            processor_version   INTEGER NOT NULL,
            extracted_at        TIMESTAMP DEFAULT current_timestamp,
            friction_tags       JSON
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_session_summary (
            session_file        VARCHAR PRIMARY KEY,
            session_id          VARCHAR NOT NULL,
            username            VARCHAR NOT NULL,
            started_at          TIMESTAMP,
            ended_at            TIMESTAMP,
            active_seconds      INTEGER,
            wall_seconds        INTEGER,
            user_messages       INTEGER DEFAULT 0,
            assistant_messages  INTEGER DEFAULT 0,
            tool_calls          INTEGER DEFAULT 0,
            tool_errors         INTEGER DEFAULT 0,
            skill_invocations   INTEGER DEFAULT 0,
            subagent_dispatches INTEGER DEFAULT 0,
            mcp_calls           INTEGER DEFAULT 0,
            slash_commands      INTEGER DEFAULT 0,
            distinct_tools      INTEGER DEFAULT 0,
            distinct_skills     INTEGER DEFAULT 0,
            primary_model       VARCHAR,
            processor_version   INTEGER NOT NULL,
            extracted_at        TIMESTAMP DEFAULT current_timestamp
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_tool_daily (
            day                 DATE NOT NULL,
            tool_name           VARCHAR NOT NULL,
            source              VARCHAR NOT NULL,
            invocations         INTEGER DEFAULT 0,
            error_count         INTEGER DEFAULT 0,
            distinct_users      INTEGER DEFAULT 0,
            distinct_sessions   INTEGER DEFAULT 0,
            PRIMARY KEY (day, tool_name, source)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_plugin_daily (
            day                 DATE NOT NULL,
            source              VARCHAR NOT NULL,
            ref_id              VARCHAR NOT NULL,
            invocations         INTEGER DEFAULT 0,
            distinct_users      INTEGER DEFAULT 0,
            distinct_sessions   INTEGER DEFAULT 0,
            PRIMARY KEY (day, source, ref_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_attribution_skills (
            source       VARCHAR NOT NULL,
            ref_id       VARCHAR NOT NULL,
            skill_name   VARCHAR NOT NULL,
            PRIMARY KEY (source, ref_id, skill_name)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_attribution_agents (
            source       VARCHAR NOT NULL,
            ref_id       VARCHAR NOT NULL,
            agent_name   VARCHAR NOT NULL,
            PRIMARY KEY (source, ref_id, agent_name)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_attribution_commands (
            source       VARCHAR NOT NULL,
            ref_id       VARCHAR NOT NULL,
            command_name VARCHAR NOT NULL,
            PRIMARY KEY (source, ref_id, command_name)
        )
    """)
    # Indices — created after all tables so the batch can be re-run safely.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_events_session ON usage_events(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_events_user_time ON usage_events(username, occurred_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_events_tool ON usage_events(tool_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_events_skill ON usage_events(skill_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_events_ref ON usage_events(source, ref_id)")
    # No idx_usage_session_user / idx_usage_session_started here (removed in
    # v95 — see the comment on usage_session_summary in _SYSTEM_SCHEMA and
    # _v94_to_v95's incident writeup).
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_attr_skill_lookup ON usage_attribution_skills(skill_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_attr_agent_lookup ON usage_attribution_agents(agent_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_attr_command_lookup ON usage_attribution_commands(command_name)")


def _v42_to_v43(conn: duckdb.DuckDBPyConnection) -> None:
    """v43: user_observability_views — per-user saved filter combinations for
    the new unified /admin/activity page.

    Saved view payload (`query_json`) is the full UI state needed to reproduce
    a page render: `{window, lens, filters: {user_id, action_prefix, source,
    result_pattern}, search, sort}`. The schema is intentionally JSON not
    columns — the UI evolves faster than DB migrations.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_observability_views (
            id          VARCHAR PRIMARY KEY,
            user_id     VARCHAR NOT NULL,
            name        VARCHAR NOT NULL,
            query_json  JSON NOT NULL,
            created_at  TIMESTAMP DEFAULT current_timestamp,
            UNIQUE (user_id, name)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_obs_views_user ON user_observability_views(user_id, created_at)")


def _v43_to_v44(conn: duckdb.DuckDBPyConnection) -> None:
    """v44: homepage status frame backing columns.

    Adds ``users.last_pull_at`` (per-user manifest fetch timestamp) and
    four BIGINT token counters on ``usage_session_summary``
    (``input_tokens``, ``output_tokens``, ``cache_read_tokens``,
    ``cache_creation_tokens``). All idempotent ALTERs — fresh installs
    receive the columns from ``_SYSTEM_SCHEMA`` and this is a no-op for
    them; upgrade path picks them up.

    Token columns default to 0; existing summary rows backfill on the
    next UsageProcessor tick because ``USAGE_PROCESSOR_VERSION`` bumps
    from 1 → 2 in the same release, which the session-pipeline
    reprocess loop uses to invalidate stale summaries.
    """
    conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_pull_at TIMESTAMP")
    for col in (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
    ):
        conn.execute(f"ALTER TABLE usage_session_summary ADD COLUMN IF NOT EXISTS {col} BIGINT DEFAULT 0")


def _v44_to_v45(conn: duckdb.DuckDBPyConnection) -> None:
    """v45: add user_id column to usage tables for stable RBAC filtering.

    The ``username`` column in ``usage_session_summary`` / ``usage_events``
    stores the directory name from the session-data path, which is either
    an email local-part (session collector) or a UUID (upload API). Email
    local-parts are unstable — they change when users rename. ``user_id``
    is the stable identity and becomes the authoritative RBAC filter
    column for the ``agnes_sessions`` / ``agnes_telemetry`` aliases.

    Backfill: the UsageProcessor populates ``user_id`` on every
    (re)process run. Existing rows get backfilled when
    ``USAGE_PROCESSOR_VERSION`` bumps, which triggers the session-pipeline
    reprocess loop.

    No index on ``usage_session_summary.user_id`` here (removed in v95 —
    see _v94_to_v95): it used to be created in this step, but it is one of
    the three secondary indexes ``upsert_summary``'s ON CONFLICT DO UPDATE
    kept rewriting, which is what made a corrupt entry fatal.
    """
    conn.execute("ALTER TABLE usage_session_summary ADD COLUMN IF NOT EXISTS user_id VARCHAR")
    conn.execute("ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS user_id VARCHAR")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_events_user_id ON usage_events(user_id)")


def _v46_to_v47(conn: duckdb.DuckDBPyConnection) -> None:
    """v47: DuckDB FTS BM25 index over knowledge_items(title, content).

    Replaces ``ILIKE '%q%'`` ranking-by-insertion-order in
    ``KnowledgeRepository.search`` with BM25 relevance scoring (#121).

    The migration is *soft* — and soft against *any* exception, not just
    the ``duckdb.Error`` that ``ensure_knowledge_fts_index`` already
    handles. A non-DuckDB exception escaping from the inner helper (for
    example an ``OSError`` from an extension fetch that bypasses
    DuckDB's wrapping in a sandboxed environment, or a future DuckDB
    version surfacing a non-``Error`` subclass) would otherwise leave
    the DB stuck at v46 forever. ``KnowledgeRepository.search`` falls
    back to ILIKE on a missing index, so a soft-fail here is always
    recoverable later (boot-time lifespan rebuild + per-mutation
    ``create_fts_index(overwrite=1)`` both retry on every restart and
    every write).

    DuckDB FTS indexes are static snapshots — they don't track
    base-table mutations automatically. The lifespan in ``app/main.py``
    rebuilds once at boot as a safety net; ``create`` and title-or-
    content ``update`` in the repo rebuild on every relevant mutation
    via the same ``overwrite=1`` PRAGMA. At corpus sizes <few-thousand
    rows this is sub-100ms.
    """
    try:
        from src.fts import ensure_knowledge_fts_index

        ensure_knowledge_fts_index(conn)
    except Exception:  # noqa: BLE001 — best-effort migration, see docstring
        # Logged at the call site (``ensure_knowledge_fts_index`` already
        # WARNs on duckdb.Error); only surfaces here for non-DuckDB
        # escapes. Schema bump must proceed regardless.
        logger = logging.getLogger(__name__)
        logger.warning(
            "v47 FTS index creation raised non-duckdb exception during migration; "
            "schema bumped to 47 anyway, search will fall back to ILIKE until "
            "the next boot-time / per-mutation rebuild succeeds",
            exc_info=True,
        )


def _v45_to_v46(conn: duckdb.DuckDBPyConnection) -> None:
    """v46: per-user opt-out (dismiss) for knowledge items.

    Adds ``knowledge_item_user_dismissed`` (user_id, item_id, dismissed_at)
    with composite PK and an index on ``user_id`` to support the EXISTS
    subquery used by list_items / search / count_items / bundle to filter
    out items the caller has dismissed. Mandatory items are excluded from
    that filter at the SQL layer (``status != 'mandatory'``); the API
    further refuses POSTs against mandatory items so the row is never
    written in the first place.

    Idempotent: ``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT
    EXISTS`` are safe to re-run. Fresh installs receive the table via
    ``_SYSTEM_SCHEMA``; the upgrade path picks it up here.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS knowledge_item_user_dismissed (
            user_id VARCHAR NOT NULL,
            item_id VARCHAR NOT NULL,
            dismissed_at TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY (user_id, item_id)
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_item_user_dismissed_user ON knowledge_item_user_dismissed(user_id)"
    )


def _v47_to_v48(conn: duckdb.DuckDBPyConnection) -> None:
    """v48: marketplace telemetry refactor.

    The v42 attribution layer (``usage_attribution_skills``, ``_agents``,
    ``_commands``) lookups on ``skill_name`` *without* the plugin prefix,
    while Claude Code writes identifiers as ``<plugin_name>:<local_name>``
    in JSONL — so the lookup never matched and every event was attributed
    to ``('builtin', None)``. The downstream ``usage_plugin_daily`` rollup
    was filtered ``WHERE source IN ('curated','flea')`` and therefore
    always empty.

    The fix: prefix-split + live lookup against ``marketplace_plugins`` /
    ``store_entities`` makes the attribution layer redundant. The new
    schema replaces all four tables with two purpose-built rollups:

    - ``usage_marketplace_item_daily``: per-day fact with count +
      per-day distinct_users + error_count, primary granularity for
      sparkline charts and incremental refresh.
    - ``usage_marketplace_item_window``: sliding-window snapshot with
      true distinct_users per window (recomputed from usage_events at
      rebuild time, can't be summed from daily distincts). Two labels
      shipped: ``last_7d`` (refreshed every tick), ``last_30d``
      (refreshed hourly).

    Drop targets verified empty / derivable on production-shape data:
    - ``usage_plugin_daily``: 0 rows (always — the attribution bug
      meant the WHERE clause never matched).
    - ``usage_attribution_*``: mapping tables, derivable from plugin
      tree on disk if ever needed again.
    """
    conn.execute("DROP TABLE IF EXISTS usage_attribution_skills")
    conn.execute("DROP TABLE IF EXISTS usage_attribution_agents")
    conn.execute("DROP TABLE IF EXISTS usage_attribution_commands")
    conn.execute("DROP TABLE IF EXISTS usage_plugin_daily")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_marketplace_item_daily (
            day            DATE    NOT NULL,
            source         VARCHAR NOT NULL,
            type           VARCHAR NOT NULL,
            parent_plugin  VARCHAR NOT NULL DEFAULT '',
            name           VARCHAR NOT NULL,
            count          INTEGER NOT NULL DEFAULT 0,
            distinct_users INTEGER NOT NULL DEFAULT 0,
            error_count    INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (day, source, type, parent_plugin, name)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_marketplace_item_window (
            period_label   VARCHAR NOT NULL,
            source         VARCHAR NOT NULL,
            type           VARCHAR NOT NULL,
            parent_plugin  VARCHAR NOT NULL DEFAULT '',
            name           VARCHAR NOT NULL,
            invocations    INTEGER NOT NULL DEFAULT 0,
            distinct_users INTEGER NOT NULL DEFAULT 0,
            refreshed_at   TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY (period_label, source, type, parent_plugin, name)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mid_lookup ON usage_marketplace_item_daily(source, type, parent_plugin, name)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_miw_lookup ON usage_marketplace_item_window(period_label, source, type)"
    )


def _v48_to_v49_migrate(conn: duckdb.DuckDBPyConnection) -> None:
    """v49: phase-1 Flea refactor — add ``title``, ``tagline``, ``synthetic_name``.

    Python function (not a SQL list) because the backfill needs Python-side
    humanize logic (acronym dict + Title Case) which has no clean SQL
    equivalent. Pattern mirrors ``_v34_to_v35_migrate``.

    Steps:
      1. Add columns nullable + default NULL so the ALTER works on a populated
         table.
      2. Iterate rows: compute ``title = humanize_name(strip_archive_suffix(name))``
         and ``synthetic_name = f"{name}-by-{owner_username}"``. ``tagline``
         stays NULL.
      3. SET NOT NULL on ``title`` and ``synthetic_name``. ``tagline`` stays
         nullable by design (optional short description).

    Idempotent: re-runs are safe — ADD COLUMN IF NOT EXISTS is a no-op;
    UPDATEs overwrite with the same values; ALTER ... SET NOT NULL is a
    no-op when already NOT NULL.
    """
    from src.store_naming import humanize_name, strip_archive_suffix

    conn.execute("ALTER TABLE store_entities ADD COLUMN IF NOT EXISTS title VARCHAR")
    conn.execute("ALTER TABLE store_entities ADD COLUMN IF NOT EXISTS tagline VARCHAR")
    conn.execute("ALTER TABLE store_entities ADD COLUMN IF NOT EXISTS synthetic_name VARCHAR")

    rows = conn.execute("SELECT id, name, owner_username FROM store_entities").fetchall()
    for row_id, name, owner_username in rows:
        display_base = strip_archive_suffix(name or "")
        title = humanize_name(display_base) or display_base or "Untitled"
        synthetic = f"{name}-by-{owner_username}"
        conn.execute(
            "UPDATE store_entities SET title = ?, synthetic_name = ? WHERE id = ?",
            [title, synthetic, row_id],
        )

    # Gate ALTER … SET NOT NULL on current nullability. DuckDB blocks
    # ALTER COLUMN once an index references the table (which happens after
    # v50 creates the UNIQUE INDEX on synthetic_name), so an unconditional
    # re-run on a fully-migrated DB would explode. Idempotent path: skip
    # the ALTER when the column is already NOT NULL.
    nullable = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'store_entities' "
            "AND column_name IN ('title', 'synthetic_name')"
        ).fetchall()
    }
    if nullable.get("title") == "YES":
        conn.execute("ALTER TABLE store_entities ALTER COLUMN title SET NOT NULL")
    if nullable.get("synthetic_name") == "YES":
        conn.execute("ALTER TABLE store_entities ALTER COLUMN synthetic_name SET NOT NULL")


def _v49_to_v50_migrate(conn: duckdb.DuckDBPyConnection) -> None:
    """v50: enforce DB-level uniqueness on ``store_entities.synthetic_name``.

    v49 introduced the column as NOT NULL but without uniqueness. App-level
    ``_suffixed_already_taken`` only fires at upload/rename; any other write
    path (admin DB hand-fix, future migration drift) could silently insert a
    duplicate, and ``WHERE synthetic_name = ?`` would then non-deterministically
    return one of the matching rows. With ``synthetic_name`` now the canonical
    attribution key (rollup tables, marketplace bundle naming, JSONL invocation
    prefix), uniqueness must be enforced at the DB level.

    DuckDB has no ``ALTER TABLE ADD CONSTRAINT UNIQUE`` for existing tables,
    but ``CREATE UNIQUE INDEX`` is functionally equivalent (rejects duplicate
    inserts). The archive rewrite path
    (``StoreEntitiesRepository.archive``) renames synthetic_name alongside
    name, so archived rows cannot collide with live ones — a full-table
    UNIQUE index is correct.

    Steps:
      1. Pre-flight: scan for existing duplicates. If any are found, abort
         with ``RuntimeError`` listing them — the index creation would fail
         anyway, but a structured error gives the operator a clear diagnostic
         instead of a raw DuckDB constraint-violation message.
      2. Create the UNIQUE index (idempotent via IF NOT EXISTS).

    Idempotent: a re-run finds the index already present and skips both
    the duplicate scan (which would still pass) and the CREATE.
    """
    # Pre-flight duplicate detection. List the actual conflicting slugs +
    # row counts so the operator can resolve manually (typically by
    # archiving one of the colliding rows, which rewrites its
    # synthetic_name to the __archived__<epoch>-suffixed form).
    dupes = conn.execute(
        """SELECT synthetic_name, COUNT(*) AS n
             FROM store_entities
            GROUP BY synthetic_name
           HAVING COUNT(*) > 1
            ORDER BY n DESC, synthetic_name"""
    ).fetchall()
    if dupes:
        summary = ", ".join(f"{name!r} x{n}" for name, n in dupes)
        raise RuntimeError(
            "v49→v50 migration blocked: duplicate synthetic_name values "
            f"present in store_entities ({summary}). Resolve manually "
            "(archive or rename the colliding rows) and re-run."
        )

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_store_entities_synthetic_name ON store_entities(synthetic_name)"
    )


def _v51_to_v52(conn: duckdb.DuckDBPyConnection) -> None:
    """v49: unified stack for Data Packages + Memory.

    Single migration entry point for the v49 cutover. See
    ``docs/brainstorms/2026-05-15-unified-stack-design.md`` section 8.1
    for the full step list. Idempotent (``ALTER ... ADD COLUMN IF NOT
    EXISTS``, ``CREATE TABLE IF NOT EXISTS``) so re-running is safe.

    Steps 6 + 9b (junction populate + recreate) are conditional on the
    legacy ``knowledge_items.domain`` column actually existing — fresh
    installs come through ``_SYSTEM_SCHEMA`` which already creates the
    post-v49 table shape (no ``domain`` column, ``knowledge_item_domains``
    junction in place), so those steps no-op.
    """
    has_legacy_domain_col = (
        conn.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name = 'knowledge_items' AND column_name = 'domain'"
        ).fetchone()
        is not None
    )

    # 1) resource_grants.requirement — per-group 'available' | 'required'
    # enum. Default 'available' preserves pre-v49 semantics. Required-tier
    # applies to data_package / memory_domain / memory_item grants;
    # marketplace_plugin Required-tier stays on
    # marketplace_plugins.is_system per D1.
    conn.execute("ALTER TABLE resource_grants ADD COLUMN IF NOT EXISTS requirement VARCHAR DEFAULT 'available'")

    # 2) knowledge_items.is_required — splits the v15-era status='mandatory'
    # overload into an orthogonal boolean. Items can now be 'approved' and
    # also Required (governance tier), or 'pending' without affecting the
    # Required state. Existing 'mandatory' rows migrate to
    # is_required=TRUE, status='approved'.
    conn.execute("ALTER TABLE knowledge_items ADD COLUMN IF NOT EXISTS is_required BOOLEAN DEFAULT FALSE")
    # Skip the backfill UPDATE on hand-crafted v1 fixtures where
    # ``status`` was never added — the ladder upgrade from v1 reaches v15
    # before v49 anyway, but the migration body sees the pre-v15 shape
    # only on those test paths. Guard so the migration stays no-op-safe.
    has_status = conn.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name = 'knowledge_items' AND column_name = 'status'"
    ).fetchone()
    if has_status:
        conn.execute(
            "UPDATE knowledge_items    SET is_required = TRUE, status = 'approved'  WHERE status = 'mandatory'"
        )

    # 3) Data Packages — admin-curated bundles of tables. A package is a
    # browse / add-to-stack unit; the tables it contains flow into the
    # caller's effective table set via DATA_PACKAGE grants. See spec
    # section 3.3.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS data_packages (
            id              VARCHAR PRIMARY KEY,
            slug            VARCHAR UNIQUE NOT NULL,
            name            VARCHAR NOT NULL,
            description     TEXT,
            icon            VARCHAR,
            color           VARCHAR,
            cover_image_url VARCHAR,
            created_by      VARCHAR,
            created_at      TIMESTAMP DEFAULT current_timestamp,
            updated_at      TIMESTAMP DEFAULT current_timestamp
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS data_package_tables (
            package_id  VARCHAR NOT NULL REFERENCES data_packages(id),
            table_id    VARCHAR NOT NULL REFERENCES table_registry(id),
            added_at    TIMESTAMP DEFAULT current_timestamp,
            added_by    VARCHAR,
            PRIMARY KEY (package_id, table_id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_data_package_tables_table ON data_package_tables(table_id)")

    # 4) Memory Domains — first-class entities replacing the scalar
    # ``knowledge_items.domain`` string. The ``memory_domains`` parent
    # table is created here; ``knowledge_item_domains`` is deferred to
    # after the legacy column is dropped (step 9) because DuckDB blocks
    # ``ALTER TABLE knowledge_items DROP COLUMN`` while a child table
    # holds an FK reference. See spec section 3.4.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_domains (
            id              VARCHAR PRIMARY KEY,
            slug            VARCHAR UNIQUE NOT NULL,
            name            VARCHAR NOT NULL,
            description     TEXT,
            icon            VARCHAR,
            color           VARCHAR,
            cover_image_url VARCHAR,
            created_by      VARCHAR,
            created_at      TIMESTAMP DEFAULT current_timestamp,
            updated_at      TIMESTAMP DEFAULT current_timestamp
        )
        """
    )

    # 5) Seed canonical memory_domains (the module-level
    # ``_CANONICAL_MEMORY_DOMAINS_SEED`` — shared with the app-lifespan
    # ensure). Deterministic ``md_<slug>`` ids so downstream Phase-2+
    # refactors can rely on the naming convention without re-querying.
    # Icons / colors are part of the canonical seed so the Browse UI
    # renders consistently across instances.
    #
    # Skipped when app-state lives in Postgres — memory_domains is then
    # read from PG, and seeding here would leak rows into the local DuckDB
    # (the inactive backend). Only the fresh-install path can hit this
    # with PG active: the legacy-upgrade branches below predate the PG
    # backend entirely, so their JOINs against these rows are unaffected.
    if not _state_backend_is_pg():
        for did, slug, name, icon, color in _CANONICAL_MEMORY_DOMAINS_SEED:
            conn.execute(
                "INSERT INTO memory_domains (id, slug, name, icon, color, created_at) "
                "VALUES (?, ?, ?, ?, ?, current_timestamp) "
                "ON CONFLICT (slug) DO NOTHING",
                [did, slug, name, icon, color],
            )

    # Plus one row per non-canonical ``knowledge_items.domain`` value found
    # in the existing data (defensive — instances may have hand-set domains
    # outside the six). Slug normalization mirrors the junction populate
    # query below so the join in task 1.6 matches deterministically. Only
    # runs on an upgrade path where the legacy column still exists; fresh
    # installs skip this since ``_SYSTEM_SCHEMA`` ships the post-v49 shape.
    if has_legacy_domain_col:
        conn.execute(
            """
            INSERT INTO memory_domains(id, slug, name, created_at)
            SELECT
                'md_' || lower(regexp_replace(domain, '[^a-z0-9]+', '_', 'g')),
                lower(regexp_replace(domain, '[^a-z0-9]+', '-', 'g')),
                domain,
                current_timestamp
              FROM (SELECT DISTINCT domain FROM knowledge_items
                     WHERE domain IS NOT NULL AND domain <> ''
                       AND domain NOT IN ('finance','engineering','product','data','operations','infrastructure'))
            ON CONFLICT (slug) DO NOTHING
            """
        )

    # 6) Stash the legacy (item_id, domain_id) pairs in a temporary table
    # so we can recreate the relation after dropping the scalar column.
    # DuckDB blocks the DROP COLUMN as long as a child table FK-references
    # ``knowledge_items``, so the junction itself is created in step 9b
    # below — after the column is gone. Skipped on fresh installs where
    # the legacy column has never existed.
    if has_legacy_domain_col:
        conn.execute("DROP TABLE IF EXISTS _v49_item_domain_pairs")
        conn.execute(
            """
            CREATE TEMP TABLE _v49_item_domain_pairs AS
            SELECT ki.id AS item_id, md.id AS domain_id
              FROM knowledge_items ki
              JOIN memory_domains  md
                ON md.slug = lower(regexp_replace(ki.domain, '[^a-z0-9]+', '-', 'g'))
             WHERE ki.domain IS NOT NULL AND ki.domain <> ''
            """
        )

    # 7) Re-point ``MEMORY_DOMAIN`` grants — pre-v49 stored the domain slug
    # directly in ``resource_grants.resource_id``; v49+ stores
    # ``memory_domains.id``. Orphan grants (resource_id is a slug with no
    # matching ``memory_domains`` row) are left untouched per spec D14 so
    # an admin can decide whether to delete or re-create the domain.
    conn.execute(
        """
        UPDATE resource_grants
           SET resource_id = (
               SELECT id FROM memory_domains
                WHERE memory_domains.slug = resource_grants.resource_id
           )
         WHERE resource_type = 'memory_domain'
           AND EXISTS (
               SELECT 1 FROM memory_domains
                WHERE memory_domains.slug = resource_grants.resource_id
           )
        """
    )

    # 8) ``user_stack_subscriptions`` — generic per-user opt-in for
    # ``data_package`` and ``memory_domain`` grants flagged
    # ``requirement='available'``. Composite PK (user_id, resource_type,
    # resource_id) makes the insert idempotent. Marketplace pluginy stay
    # on the existing ``user_plugin_optouts`` opt-out shape per D1.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_stack_subscriptions (
            user_id       VARCHAR NOT NULL,
            resource_type VARCHAR NOT NULL,
            resource_id   VARCHAR NOT NULL,
            subscribed_at TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY (user_id, resource_type, resource_id)
        )
        """
    )

    # 9a) Drop the legacy ``knowledge_items.domain`` scalar. Single-PR
    # cutover per D14 — the temp-stashed pairs from step 6 + memory_domains
    # seeded in step 5 are the new sources of truth.
    #
    # DuckDB blocks ``ALTER TABLE … DROP COLUMN`` while any FK references
    # the same table (DependencyException). On the fresh-install +
    # upgrade-from-v1 paths, ``_SYSTEM_SCHEMA`` runs BEFORE the migration
    # ladder and creates ``knowledge_item_domains`` with the FK already
    # in place. We DROP that dependent (stashing its rows), perform the
    # column drop, and recreate the junction immediately after.
    junction_existed = False
    stashed_rows: list = []
    try:
        stashed_rows = conn.execute(
            "SELECT item_id, domain_id, added_at, added_by FROM knowledge_item_domains"
        ).fetchall()
        junction_existed = True
        conn.execute("DROP TABLE knowledge_item_domains")
    except duckdb.Error:
        # Junction didn't exist (genuine v48 upgrade path); fall through.
        pass

    conn.execute("ALTER TABLE knowledge_items DROP COLUMN IF EXISTS domain")

    # 9b) (Re)create the M:N junction and replay any rows we stashed.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_item_domains (
            item_id   VARCHAR NOT NULL REFERENCES knowledge_items(id),
            domain_id VARCHAR NOT NULL REFERENCES memory_domains(id),
            added_at  TIMESTAMP DEFAULT current_timestamp,
            added_by  VARCHAR,
            PRIMARY KEY (item_id, domain_id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_item_domains_domain ON knowledge_item_domains(domain_id)")
    if junction_existed and stashed_rows:
        for row in stashed_rows:
            conn.execute(
                "INSERT INTO knowledge_item_domains"
                "(item_id, domain_id, added_at, added_by) "
                "VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                list(row),
            )
    # Replay the stashed pairs. The temp table exists only when the
    # upgrade path ran step 6 — fresh installs skip both step 6 and
    # this replay since ``_v49_item_domain_pairs`` was never created.
    has_pairs = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = '_v49_item_domain_pairs'"
    ).fetchone()
    if has_pairs:
        conn.execute(
            """
            INSERT INTO knowledge_item_domains(item_id, domain_id, added_at)
            SELECT item_id, domain_id, current_timestamp
              FROM _v49_item_domain_pairs
            ON CONFLICT DO NOTHING
            """
        )
        conn.execute("DROP TABLE _v49_item_domain_pairs")

    # 10) bump schema_version row. Matches the pattern used by every
    # prior in-function migration (e.g. _v30_to_v31_migrate, _v34_to_v35_migrate)
    # — the per-step migrations declared as SQL lists rely on the
    # outer ``UPDATE schema_version`` at the end of ``_ensure_schema``,
    # but the ladder-internal function pattern keeps the bump local so a
    # mid-ladder failure doesn't leave the version stale.
    conn.execute("UPDATE schema_version SET version = 52")


_V50_TO_V51_MIGRATIONS = [
    # ``bq_fqn`` carries the fully-qualified BigQuery path
    # (``project.dataset.table``) for a registered remote table when set,
    # so the orchestrator's rebuild path no longer has to reconstruct it
    # from the globally-attached ``_remote_attach`` project + the dual-
    # purpose ``bucket`` field (which is also a UX/RBAC label).
    # Nullable for backwards compat — rows without it keep using the
    # legacy ``<remote_attach.project>.<bucket>.<source_table>`` fallback.
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS bq_fqn VARCHAR",
]


_V33_TO_V34_MIGRATIONS = [
    # DuckDB blocks DROP COLUMN while indexes reference the table
    # ("Dependency Error: Cannot alter entry … because there are entries
    # that depend on it"), even when the index doesn't reference the
    # dropped column. Drop both indexes, drop the column, then re-create
    # the indexes from _SYSTEM_SCHEMA's CREATE INDEX IF NOT EXISTS
    # statements (which already ran above this block — but DROP+CREATE
    # is idempotent here too).
    "DROP INDEX IF EXISTS idx_store_submissions_status",
    "DROP INDEX IF EXISTS idx_store_submissions_entity",
    "ALTER TABLE store_submissions DROP COLUMN IF EXISTS retry_count",
    "CREATE INDEX IF NOT EXISTS idx_store_submissions_status ON store_submissions(status)",
    "CREATE INDEX IF NOT EXISTS idx_store_submissions_entity ON store_submissions(entity_id)",
]


# v31: rename session_extraction_state → session_processor_state with composite
# PK (processor_name, session_file). The session pipeline framework
# (services/session_pipeline/) lets multiple processors track their own
# processed-set independently; each gets its own row keyed by name. Existing
# rows belong to the verification detector, so they're copied across with
# processor_name='verification'. The old single-PK table is dropped — its only
# caller (services/verification_detector/detector.py) is rewritten in the same
# PR to use the new repository.
#
# (Originally drafted as v29 but renumbered to v31 after rebase onto upstream's
# v29 instance_templates + v30 news_template work.)
#
# Implemented as a function rather than a SQL list because the INSERT-from-old
# step depends on whether `session_extraction_state` actually exists. Fresh
# installs at a pre-v31 schema_version (test fixtures hand-rolling a v19/v20
# DB) come through `_SYSTEM_SCHEMA` which already creates
# `session_processor_state` at the new shape — but does NOT create the old
# `session_extraction_state` (we removed that). So the migration must skip
# the copy + drop when the old table is missing rather than 500 on
# CatalogException.
_V30_TO_V31_CREATE_NEW_TABLE = """
    CREATE TABLE IF NOT EXISTS session_processor_state (
        processor_name VARCHAR NOT NULL,
        session_file VARCHAR NOT NULL,
        username VARCHAR NOT NULL,
        processed_at TIMESTAMP DEFAULT current_timestamp,
        items_extracted INTEGER DEFAULT 0,
        file_hash VARCHAR,
        PRIMARY KEY (processor_name, session_file)
    )
"""


def _v30_to_v31_migrate(conn: duckdb.DuckDBPyConnection) -> None:
    """Run the v31 migration steps with conditional copy from the legacy table."""
    conn.execute(_V30_TO_V31_CREATE_NEW_TABLE)

    # Skip the copy + drop when the legacy table doesn't exist (fresh
    # install or upgrade path that started at >= v31). Otherwise migrate
    # rows over with processor_name='verification' (the only writer of the
    # legacy table).
    has_legacy = conn.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'main' AND table_name = 'session_extraction_state'"
    ).fetchone()
    if not has_legacy:
        return

    # INSERT OR IGNORE on the (processor_name, session_file) PK so a
    # re-run idempotently no-ops if a verification row was already
    # written at the new shape.
    conn.execute(
        """
        INSERT OR IGNORE INTO session_processor_state
            (processor_name, session_file, username, processed_at, items_extracted, file_hash)
        SELECT 'verification', session_file, username, processed_at, items_extracted, file_hash
          FROM session_extraction_state
        """
    )
    conn.execute("DROP TABLE session_extraction_state")


# v37: curated marketplace enrichment from `.claude-plugin/marketplace-metadata.json`
# plus mandatory curator identity on `marketplace_registry`. See the file-level
# `_SYSTEM_SCHEMA` block for the column-level commentary; the migration is
# pure ADD COLUMN IF NOT EXISTS so it is idempotent against a fresh install
# whose schema_version row was hand-rolled below 37 by test fixtures (the
# IF NOT EXISTS guard then no-ops because `_SYSTEM_SCHEMA` already created
# the columns at the new shape). Same idiom as `_V27_TO_V28_MIGRATIONS`'s
# `marketplace_plugins.created_at` ALTER.
#
# Originally drafted as v32 but renumbered after rebase onto upstream's
# v32→v36 sequence (flea-market upload guardrails + soft delete).
_V36_TO_V37_MIGRATIONS = [
    "ALTER TABLE marketplace_registry ADD COLUMN IF NOT EXISTS curator_name VARCHAR",
    "ALTER TABLE marketplace_registry ADD COLUMN IF NOT EXISTS curator_email VARCHAR",
    "ALTER TABLE marketplace_plugins ADD COLUMN IF NOT EXISTS cover_photo_url VARCHAR",
    "ALTER TABLE marketplace_plugins ADD COLUMN IF NOT EXISTS video_url VARCHAR",
    "ALTER TABLE marketplace_plugins ADD COLUMN IF NOT EXISTS doc_links JSON",
]


# v24: rewrite materialized BQ source_query from DuckDB-flavor
# (bq."<dataset>"."<table>") to BigQuery-native (`<project>.<dataset>.<table>`)
# so the new connectors.bigquery.extractor.materialize_query wrapping
# path (which routes through bigquery_query() / BQ jobs API) accepts
# them. Pre-v24, materialize used Storage Read API for the bq.<ds>.<tbl>
# form, which fails for views — see PR for full motivation.
#
# This migration is implemented in Python (not pure SQL) because the
# rewrite is a regex-and-replace per row: the project_id comes from
# instance_config (file/env), not the DB. SQL alone can't pull the
# project_id and substitute it. If the project isn't configured at
# migration time, log a warning per affected row and leave them — the
# operator must configure data_source.bigquery.project, restart, and
# the migration will fire on next start (idempotent).
def _replace_for_v24(project_id: str):
    """Build a re.sub replacement function (not a string) so backslash
    sequences in `project_id` aren't interpreted as group references.
    GCP project IDs can't actually contain backslashes, but using a
    function-form replacement is the defensive idiom — it makes the
    intent explicit and removes the dependency on re.sub's replacement-
    string escaping rules."""

    def _repl(m):
        return f"`{project_id}.{m.group(1)}.{m.group(2)}`"

    return _repl


def _v23_to_v24_finalize(conn: duckdb.DuckDBPyConnection) -> None:
    import re as _re

    try:
        from app.instance_config import get_value

        project_id = get_value("data_source", "bigquery", "project", default="") or ""
    except Exception:
        project_id = ""

    pattern = _re.compile(r'bq\."([^"]+)"\."([^"]+)"')

    rows = conn.execute(
        "SELECT id, source_query FROM table_registry "
        "WHERE query_mode = 'materialized' "
        "AND source_query LIKE '%bq.\"%' "
        "AND source_type = 'bigquery'"
    ).fetchall()

    if not rows:
        return  # Nothing to migrate; skip the transaction.

    # If we have rows to migrate AND project_id isn't configured, we cannot
    # rewrite their source_query. Raise BEFORE the schema_version bump so
    # the migration re-runs on the NEXT startup (after the operator
    # configures the project). Pre-fix the function logged a warning per
    # row and returned normally — the schema_version then bumped to 24
    # unconditionally, the `if current < 24:` gate skipped this function
    # forever after, and rows stayed in DuckDB-flavor SQL. The new
    # `_wrap_admin_sql_for_jobs_api` wrapping path then rejected those
    # rows at materialize time as unparseable BQ SQL with no automatic
    # recovery (Devin Review on db.py:1757). Side effect: a BQ-using
    # deployment that hasn't set the project blocks startup until they
    # do — that's the right call for a config error that would otherwise
    # silently break materialized tables.
    if not project_id:
        raise RuntimeError(
            f"v24 migration cannot complete: {len(rows)} materialized "
            f"BigQuery row(s) need their source_query rewritten from "
            f'DuckDB-flavor `bq."ds"."tbl"` to BQ-native '
            f"`<project>.ds.tbl`, but `data_source.bigquery.project` is "
            f"not configured. Set it via /admin/server-config (or "
            f"`instance.yaml: data_source.bigquery.project`) and restart "
            f"the app to retry the migration. The schema version is NOT "
            f"bumped to 24 until this completes; pre-migration DB "
            f"snapshot is at `{_get_state_dir()}/system.duckdb.pre-migrate`."
        )

    conn.execute("BEGIN TRANSACTION")
    try:
        for row_id, sq in rows:
            if sq is None:
                continue
            new_sq = pattern.sub(_replace_for_v24(project_id), sq)
            if new_sq != sq:
                conn.execute(
                    "UPDATE table_registry SET source_query = ? WHERE id = ?",
                    [new_sq, row_id],
                )
                logger.info(
                    "v24 migration: rewrote source_query for row %r",
                    row_id,
                )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _v59_to_v60(conn: duckdb.DuckDBPyConnection) -> None:
    """Backfill ``usage_events.username`` / ``usage_session_summary.username``
    from ``users.email`` where the row has a resolved ``user_id``.

    Pre-v60 the ``username`` column was written by three writers with
    conflicting semantics:

    * REST event emitters (``sync.py``, ``stack.py``, ``memory.py``,
      ``web/router.py``) → full email (``user.get('email')``) or
      ``user['id']`` UUID when email empty.
    * Session pipeline via ``/data/user_sessions/<dir>/`` → directory
      name. From the session collector the directory is the OS
      username (typically the email local-part); from the upload API
      it is the user's UUID.

    Result: a single user surfaces in the admin telemetry dropdown
    under up to three different ``username`` values. The runner now
    normalises new writes to ``users.email``; this migration cleans up
    the historical rows so the dropdown is one row per user
    immediately.

    Only rows with a non-null ``user_id`` are touched — orphaned
    sessions (deleted users, never-matched directories) keep whatever
    label they had so the data isn't silently lost.
    """

    # Skip backfill on stub schemas (e.g. the v1→vN end-to-end test
    # seeds ``users`` with only an ``id`` column). The required
    # ``users.email`` plus ``usage_*.username`` / ``usage_*.user_id``
    # columns all come from earlier migrations on every real install;
    # if any of them is missing here, this is a synthetic fixture.
    def _cols(table: str) -> set[str]:
        return {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE lower(table_name) = lower(?)",
                [table],
            ).fetchall()
        }

    users_cols = _cols("users")
    if "email" not in users_cols:
        conn.execute("UPDATE schema_version SET version = 60")
        return

    for table, key_col in (
        ("usage_events", "user_id"),
        ("usage_session_summary", "user_id"),
    ):
        tcols = _cols(table)
        if {"username", key_col} - tcols:
            continue
        conn.execute(
            f"""
            UPDATE {table}
               SET username = u.email
              FROM users u
             WHERE {table}.{key_col} = u.id
               AND u.email IS NOT NULL
               AND u.email != ''
               AND {table}.username IS DISTINCT FROM u.email
            """
        )
    conn.execute("UPDATE schema_version SET version = 60")


def _v61_to_v62(conn: duckdb.DuckDBPyConnection) -> None:
    """v62: per-type FK columns on ``resource_grants`` (E.3).

    Adds five NULLable columns to mirror the PG per-type FK design
    (alembic migration 0013):

      resource_id_table          — set when resource_type='table'
      resource_id_data_package   — set when resource_type='data_package'
      resource_id_memory_domain  — set when resource_type='memory_domain'
      resource_id_memory_item    — set when resource_type='memory_item'
      resource_id_recipe         — set when resource_type='recipe'

    DuckDB has limited FK and CHECK constraint support, so neither is
    enforced at the DB layer here — application code is the source of
    truth for both backends. The PG migration (0013) carries the real
    FK + CHECK; this step merely keeps the column set in sync so the
    DB-state migrator can copy every column when moving DuckDB → PG.

    All ALTERs use ADD COLUMN IF NOT EXISTS — idempotent.

    Renumbered from v61 to v62 on the second merge with main (which
    shipped ``cli_auth_codes`` as v61 via PR #475). The first renumber
    (v60→v61) was for main's telemetry username collapse (PR #458).
    """
    for col_sql in (
        "ALTER TABLE resource_grants ADD COLUMN IF NOT EXISTS resource_id_table VARCHAR",
        "ALTER TABLE resource_grants ADD COLUMN IF NOT EXISTS resource_id_data_package VARCHAR",
        "ALTER TABLE resource_grants ADD COLUMN IF NOT EXISTS resource_id_memory_domain VARCHAR",
        "ALTER TABLE resource_grants ADD COLUMN IF NOT EXISTS resource_id_memory_item VARCHAR",
        "ALTER TABLE resource_grants ADD COLUMN IF NOT EXISTS resource_id_recipe VARCHAR",
    ):
        conn.execute(col_sql)
    # Backfill: copy resource_id into the per-type column for existing rows.
    # marketplace_plugin rows are left with all per-type columns NULL.
    for rtype, col in (
        ("table", "resource_id_table"),
        ("data_package", "resource_id_data_package"),
        ("memory_domain", "resource_id_memory_domain"),
        ("memory_item", "resource_id_memory_item"),
        ("recipe", "resource_id_recipe"),
    ):
        conn.execute(
            f"UPDATE resource_grants SET {col} = resource_id WHERE resource_type = ?",
            [rtype],
        )
    conn.execute("UPDATE schema_version SET version = 62")


def _v58_to_v59(conn: duckdb.DuckDBPyConnection) -> None:
    """v56: extended-content columns on ``data_packages`` + structured
    per-table doc columns on ``table_registry``.

    Backs the ``/catalog/p/<slug>`` rewrite per the extended-descriptions
    admin spec — owner attribution, curated tags,
    long-form description, use/skip arrays, package-level example
    questions on the package side; grain / platforms / partition /
    history / gotchas on the per-table side.

    All ALTERs are ADD COLUMN IF NOT EXISTS — idempotent + safe to
    re-run.
    """
    for col_sql in (
        "ALTER TABLE data_packages ADD COLUMN IF NOT EXISTS owner_name VARCHAR",
        "ALTER TABLE data_packages ADD COLUMN IF NOT EXISTS owner_team VARCHAR",
        "ALTER TABLE data_packages ADD COLUMN IF NOT EXISTS tags VARCHAR",
        "ALTER TABLE data_packages ADD COLUMN IF NOT EXISTS long_description TEXT",
        "ALTER TABLE data_packages ADD COLUMN IF NOT EXISTS when_to_use VARCHAR",
        "ALTER TABLE data_packages ADD COLUMN IF NOT EXISTS when_not_to_use VARCHAR",
        "ALTER TABLE data_packages ADD COLUMN IF NOT EXISTS example_questions VARCHAR",
        "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS grain VARCHAR",
        "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS platforms VARCHAR",
        "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS partition_col VARCHAR",
        "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS history VARCHAR",
        "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS gotchas VARCHAR",
    ):
        conn.execute(col_sql)
    conn.execute("UPDATE schema_version SET version = 59")


def _v60_to_v61(conn: duckdb.DuckDBPyConnection) -> None:
    """v61: ``cli_auth_codes`` table — single-use exchange codes for the
    browser-loopback ``agnes auth login`` flow.

    Idempotent CREATE TABLE IF NOT EXISTS. Fresh installs already get the
    table from ``_SYSTEM_SCHEMA``; this migration covers the sequential
    upgrade path from a v60 instance.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cli_auth_codes (
            code_hash   VARCHAR PRIMARY KEY,
            user_id     VARCHAR NOT NULL,
            email       VARCHAR NOT NULL,
            created_at  TIMESTAMP NOT NULL DEFAULT current_timestamp,
            expires_at  TIMESTAMP NOT NULL,
            consumed_at TIMESTAMP
        )
    """)
    conn.execute("UPDATE schema_version SET version = 61")


def _v62_to_v63(conn: duckdb.DuckDBPyConnection) -> None:
    """v63: ``setup_tokens`` table for the Agnes Cowork one-click setup flow.

    Short-lived tokens (24 h) generated by ``POST /api/user/cowork-bundle``
    and consumed once by ``POST /api/auth/exchange-setup-token``, which mints
    a regular PAT without requiring the analyst to log in interactively.

    Renumbered from v62 to v63: main PR #455 shipped per-type FK columns on
    resource_grants as v62 before this branch merged.

    CREATE TABLE IF NOT EXISTS is idempotent — safe on fresh installs where
    the table already exists courtesy of ``_SYSTEM_SCHEMA``.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS setup_tokens (
            id          VARCHAR PRIMARY KEY,
            user_id     VARCHAR NOT NULL,
            token_hash  VARCHAR NOT NULL,
            expires_at  TIMESTAMP NOT NULL,
            used_at     TIMESTAMP,
            created_at  TIMESTAMP NOT NULL DEFAULT current_timestamp
        )
    """)
    conn.execute("UPDATE schema_version SET version = 63")


def _v63_to_v64(conn: duckdb.DuckDBPyConnection) -> None:
    """v64: Universal MCP — ``mcp_sources``, ``tool_registry``, ``tool_grants``.

    Tables for the inbound MCP connector (RFC #461). A row in ``mcp_sources``
    describes an external MCP server we ingest from (stdio command or
    HTTP/SSE URL). Each curated tool from that source becomes one row in
    ``tool_registry`` with a ``mode`` of ``materialize`` (scheduled extract
    into a parquet → analytics.duckdb table) or ``passthrough`` (live call
    forwarded to the upstream MCP at query time). ``tool_grants`` is the
    per-group ACL for passthrough tools, parallel to ``resource_grants``.

    Renumbered from v63 to v64: main PR #455 shipped per-type FK columns on
    resource_grants as v62 before this branch merged.

    CREATE TABLE IF NOT EXISTS is idempotent — safe on fresh installs.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mcp_sources (
            id            VARCHAR PRIMARY KEY,
            name          VARCHAR NOT NULL UNIQUE,
            transport     VARCHAR NOT NULL,       -- stdio | http | sse
            command       VARCHAR,                -- stdio: executable path
            args          JSON,                   -- stdio: arg array
            url           VARCHAR,                -- http/sse: endpoint URL
            auth_method   VARCHAR,                -- none | bearer | basic
            auth_secret_env VARCHAR,              -- name of env var holding the secret (POC: no vault yet)
            enabled       BOOLEAN NOT NULL DEFAULT true,
            created_at    TIMESTAMP NOT NULL DEFAULT current_timestamp,
            updated_at    TIMESTAMP NOT NULL DEFAULT current_timestamp
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tool_registry (
            tool_id          VARCHAR PRIMARY KEY,        -- "<source_name>.<exposed_name>"
            source_id        VARCHAR NOT NULL,
            original_name    VARCHAR NOT NULL,
            exposed_name     VARCHAR NOT NULL,
            mode             VARCHAR NOT NULL,           -- materialize | passthrough
            table_id         VARCHAR,                    -- FK to table_registry (materialize mode only)
            input_schema     JSON,                       -- MCP inputSchema
            description      VARCHAR,
            mutating         BOOLEAN NOT NULL DEFAULT false,
            pii_fields       JSON,                       -- array of column names to redact on output
            rate_limit_pm    INTEGER,                    -- per-minute, per-user (NULL = unlimited)
            schedule         VARCHAR,                    -- materialize only, e.g. 'every 6h'
            projection_map   JSON,                       -- {"id": col, "url": col, "name": col} for the linked-apps projection
            enabled          BOOLEAN NOT NULL DEFAULT true,
            created_at       TIMESTAMP NOT NULL DEFAULT current_timestamp,
            updated_at       TIMESTAMP NOT NULL DEFAULT current_timestamp
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tool_grants (
            tool_id   VARCHAR NOT NULL,
            group_id  VARCHAR NOT NULL,
            PRIMARY KEY (tool_id, group_id)
        )
    """)
    conn.execute("UPDATE schema_version SET version = 64")


def _v64_to_v65(conn: duckdb.DuckDBPyConnection) -> None:
    """v65: ``mcp_secrets`` table — server-wide vault for MCP source auth.

    RFC #461 §4. One row per ``mcp_sources.id`` holds the Fernet-
    ciphertext of the upstream auth token. Replaces the legacy
    ``mcp_sources.auth_secret_env`` env-var pattern for HTTP/SSE
    sources — connectors/mcp/client.py first consults this table, then
    falls back to the env-var path so old registrations keep working.

    Per-user secrets (analyst-scoped OAuth tokens for upstream MCP) land
    in a follow-up migration as ``mcp_user_secrets``.

    Renumbered from v64 to v65: main PR #455 shipped per-type FK columns
    on resource_grants as v62 before this branch merged.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mcp_secrets (
            source_id        VARCHAR PRIMARY KEY,
            secret_value_enc BLOB NOT NULL,
            created_at       TIMESTAMP NOT NULL DEFAULT current_timestamp,
            updated_at       TIMESTAMP NOT NULL DEFAULT current_timestamp
        )
    """)
    conn.execute("UPDATE schema_version SET version = 65")


def _v65_to_v66(conn: duckdb.DuckDBPyConnection) -> None:
    """v66: per-user MCP source secrets + ``scope`` column on ``mcp_sources``.

    RFC #461 §4 phase B. ``mcp_user_secrets(source_id, user_id, ...)``
    holds each analyst's own credential (their Notion/Slack/Linear OAuth
    token) for upstream MCP servers that authenticate per-caller. The
    new ``mcp_sources.scope`` column selects which lookup path
    ``connectors/mcp/client._lookup_secret_for_source`` follows:

      ``shared``    — default; use mcp_secrets (or auth_secret_env env var).
                      Materialize scheduled jobs always use this scope —
                      they don't have a calling user.
      ``per_user``  — REST invoke endpoint threads the caller's id;
                      look up mcp_user_secrets(source_id, user_id).
                      Falls through to shared if the analyst hasn't
                      stored their own credential yet (so the path stays
                      forgiving while operators bootstrap).

    ``CREATE TABLE IF NOT EXISTS`` + ``ADD COLUMN IF NOT EXISTS`` keep
    the migration idempotent on fresh and upgrade paths.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mcp_user_secrets (
            source_id        VARCHAR NOT NULL,
            user_id          VARCHAR NOT NULL,
            secret_value_enc BLOB NOT NULL,
            created_at       TIMESTAMP NOT NULL DEFAULT current_timestamp,
            updated_at       TIMESTAMP NOT NULL DEFAULT current_timestamp,
            PRIMARY KEY (source_id, user_id)
        )
    """)
    conn.execute("ALTER TABLE mcp_sources ADD COLUMN IF NOT EXISTS scope VARCHAR DEFAULT 'shared'")
    conn.execute("UPDATE schema_version SET version = 66")


def _v66_to_v67(conn: duckdb.DuckDBPyConnection) -> None:
    """v67: ``data_package_tools`` — junction linking data packages to MCP tools.

    RFC #461 §6. Mirrors ``data_package_tables`` so a package can surface
    both its analytical tables AND the MCP tools that fit its workflow
    (e.g. a "Customer Lifecycle" package lists the orders/sessions tables
    AND a passthrough ``crm.searchAccounts`` tool). The package detail
    response gains a ``related_tools`` array populated via this junction.

    ``CREATE TABLE IF NOT EXISTS`` for idempotency on both fresh and
    upgrade paths.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS data_package_tools (
            package_id  VARCHAR NOT NULL,
            tool_id     VARCHAR NOT NULL,
            added_at    TIMESTAMP NOT NULL DEFAULT current_timestamp,
            PRIMARY KEY (package_id, tool_id)
        )
    """)
    conn.execute("UPDATE schema_version SET version = 67")


def _v67_to_v68(conn: duckdb.DuckDBPyConnection) -> None:
    """v68: cloud chat — per-session transcript storage + per-user workdir markers.

    Creates three tables: chat_sessions, chat_messages, user_workdirs.
    Adds two regular indexes for common query patterns.

    DuckDB 1.5.x limitations (documented in _SYSTEM_SCHEMA comment above):
    - ON DELETE CASCADE is not supported; application code must delete
      child messages before deleting a session row.
    - Partial (WHERE-clause) unique indexes are not supported; per-surface
      uniqueness for slack_dm / slack_thread is enforced at the application
      layer in ChatRepository.
    - UPDATE on a column that is part of a secondary index on a parent
      table raises a false FK violation when child rows exist. The
      ``last_message_at`` column is part of ``idx_chat_sessions_user`` and
      therefore cannot be UPDATEd once any chat_messages row references
      the session. Consequently ``chat_sessions.last_message_at`` and
      ``chat_sessions.message_count`` are NEVER written after row
      creation — they stay (NULL, 0) at the SQL level. ChatRepository
      computes both via LEFT JOIN at read time; any code that reads these
      columns directly (bypassing ChatRepository) will see stale values.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id               VARCHAR PRIMARY KEY,
            user_email       VARCHAR NOT NULL,
            surface          VARCHAR NOT NULL,
            slack_channel_id VARCHAR,
            slack_thread_ts  VARCHAR,
            title            VARCHAR,
            started_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_message_at  TIMESTAMP,
            message_count    INTEGER NOT NULL DEFAULT 0,
            archived         BOOLEAN NOT NULL DEFAULT FALSE
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id          VARCHAR PRIMARY KEY,
            session_id  VARCHAR NOT NULL REFERENCES chat_sessions(id),
            role        VARCHAR NOT NULL,
            content     TEXT NOT NULL,
            tool_calls  JSON,
            tokens_in   INTEGER,
            tokens_out  INTEGER,
            model       VARCHAR,
            created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_workdirs (
            user_email             VARCHAR PRIMARY KEY,
            last_init_at           TIMESTAMP,
            marketplace_sha        VARCHAR,
            initial_workspace_sha  VARCHAR,
            agnes_version_at_init  VARCHAR
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_sessions_user ON chat_sessions(user_email, last_message_at)")
    conn.execute("UPDATE schema_version SET version = 68")


def _v68_to_v69(conn: duckdb.DuckDBPyConnection) -> None:
    """v69: per-source non-secret env vars for stdio MCP sources.

    Adds ``mcp_sources.env`` — a JSON object of ``{VAR: value}`` passed to
    the spawned stdio subprocess (the ``auth_secret_env`` secret overlays
    it). NULL on existing rows preserves the prior single-secret behavior.
    """
    conn.execute("ALTER TABLE mcp_sources ADD COLUMN IF NOT EXISTS env VARCHAR")
    conn.execute("UPDATE schema_version SET version = 69")


def _v69_to_v70(conn: duckdb.DuckDBPyConnection) -> None:
    """v70: live co-drive foundation — co-session flags + participants table.

    Additive-only and forward-safe on populated prod DBs:
    - chat_sessions.is_co_session / ephemeral (BOOLEAN NOT NULL DEFAULT FALSE)
    - chat_messages.sender_email (VARCHAR, nullable; backfilled to the
      session owner for existing role='user' rows — every pre-v70 session
      is single-principal)
    - chat_session_participants table + index

    Each ADD COLUMN is PRAGMA-guarded because this migration may re-run on a
    partially-migrated DB (the ladder is idempotent). DuckDB has no
    ON DELETE CASCADE, so ChatRepository.hard_delete_user_sessions deletes
    participant rows by hand (see Task 4).
    """
    sess_cols = {r[1] for r in conn.execute("PRAGMA table_info('chat_sessions')").fetchall()}
    # These are added NULLABLE (DEFAULT FALSE), even though _SYSTEM_SCHEMA
    # (fresh install) and the Alembic PG migration declare them NOT NULL.
    # DuckDB cannot promote them: `ALTER COLUMN ... SET NOT NULL` on
    # chat_sessions raises DependencyException because the table is referenced
    # by foreign keys (chat_messages, chat_session_participants); and the
    # combined `ADD COLUMN ... NOT NULL DEFAULT` form is unsupported too. The
    # DEFAULT FALSE materializes a concrete False for every existing row and
    # the backfill below makes that explicit, so NO NULL is ever observed and
    # every reader coerces via bool() — the only difference from PG/fresh is
    # nullability *metadata*, not behavior. Closing it would require a full
    # table rebuild (drop/recreate FKs), which is not worth the risk.
    if "is_co_session" not in sess_cols:
        conn.execute("ALTER TABLE chat_sessions ADD COLUMN is_co_session BOOLEAN DEFAULT FALSE")
        conn.execute("UPDATE chat_sessions SET is_co_session = FALSE WHERE is_co_session IS NULL")
    if "ephemeral" not in sess_cols:
        conn.execute("ALTER TABLE chat_sessions ADD COLUMN ephemeral BOOLEAN DEFAULT FALSE")
        conn.execute("UPDATE chat_sessions SET ephemeral = FALSE WHERE ephemeral IS NULL")
    msg_cols = {r[1] for r in conn.execute("PRAGMA table_info('chat_messages')").fetchall()}
    if "sender_email" not in msg_cols:
        conn.execute("ALTER TABLE chat_messages ADD COLUMN sender_email VARCHAR")
        # Backfill: pre-v70 user turns are owned by the session's user_email.
        conn.execute(
            "UPDATE chat_messages SET sender_email = ("
            "  SELECT s.user_email FROM chat_sessions s WHERE s.id = chat_messages.session_id"
            ") WHERE role = 'user' AND sender_email IS NULL"
        )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_session_participants (
            id          VARCHAR PRIMARY KEY,
            session_id  VARCHAR NOT NULL REFERENCES chat_sessions(id),
            user_email  VARCHAR NOT NULL,
            user_id     VARCHAR NOT NULL,
            role        VARCHAR NOT NULL,
            joined_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            left_at     TIMESTAMP,
            UNIQUE (session_id, user_email)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_session_participants_user "
        "ON chat_session_participants(user_email, session_id)"
    )
    conn.execute("UPDATE schema_version SET version = 70")


def _v70_to_v71(conn: duckdb.DuckDBPyConnection) -> None:
    """v71: formalize ``users.slack_user_id`` into the schema.

    Previously this column was lazily ``ALTER``-ed in
    ``services/slack_bot/binding.py`` only when the Slack bot ran — so it
    existed only in the DuckDB system file and never on a Postgres instance,
    which broke Slack identity binding on Postgres (the binding was written to
    a DuckDB ``users`` table the factory-backed reads never consult). Adding it
    to the schema lets the binding route through ``users_repo()`` on either
    backend. Additive + nullable; idempotent (the lazy ALTER may already have
    added it on existing DuckDB instances).
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "slack_user_id" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN slack_user_id VARCHAR")
    conn.execute("UPDATE schema_version SET version = 71")


def _v71_to_v72(conn: duckdb.DuckDBPyConnection) -> None:
    """v72: ``system_secrets`` table — server-wide vault for system-level
    secrets keyed by name (Slack bot tokens).

    Distinct from ``mcp_secrets`` (keyed by ``source_id``, MCP data sources):
    this scope holds server-wide secrets that are not tied to any MCP source,
    starting with the three Slack bot tokens. Fernet-encrypted at rest, read
    via ``env > vault`` by ``services/slack_bot/secrets.slack_secret``.

    Idempotent CREATE TABLE IF NOT EXISTS — safe on fresh and upgrade paths.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS system_secrets (
            name             VARCHAR PRIMARY KEY,
            secret_value_enc BLOB NOT NULL,
            created_at       TIMESTAMP NOT NULL DEFAULT current_timestamp,
            updated_at       TIMESTAMP NOT NULL DEFAULT current_timestamp
        )
    """)
    conn.execute("UPDATE schema_version SET version = 72")


def _v72_to_v73(conn: duckdb.DuckDBPyConnection) -> None:
    """v73: sandbox pause/resume refs on ``chat_sessions``.

    Three nullable columns tracking the provider sandbox ID, the runner PID,
    and the time the session was paused. Must stay un-indexed — DuckDB 1.5.3
    raises a false FK violation when UPDATE-ing indexed columns of
    ``chat_sessions`` after any ``chat_messages`` INSERT (see comment at the
    ``chat_sessions`` DDL in ``_SYSTEM_SCHEMA``).

    Idempotent ADD COLUMN IF NOT EXISTS — safe on fresh and upgrade paths.
    """
    for ddl in (
        "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS sandbox_id VARCHAR",
        "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS runner_pid INTEGER",
        "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS sandbox_paused_at TIMESTAMP",
    ):
        conn.execute(ddl)
    conn.execute("UPDATE schema_version SET version = 73")


def _v73_to_v74(conn: duckdb.DuckDBPyConnection) -> None:
    """v74: ``server_only`` distribution flag on ``table_registry``.

    Decoupled from ``query_mode``: a ``server_only=true`` row is kept
    server-side and stays queryable via ``agnes query --remote``, but
    ``agnes pull`` does NOT download its parquet (the manifest still lists
    it for catalog discovery + RBAC). Only meaningful for
    ``query_mode IN ('local', 'materialized')``; ignored for ``'remote'``
    rows (no server-stored parquet to suppress). Issue #607.

    Idempotent ADD COLUMN IF NOT EXISTS — safe on fresh and upgrade paths.
    Default ``false`` leaves every existing row unchanged.
    """
    conn.execute("ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS server_only BOOLEAN DEFAULT false")
    conn.execute("UPDATE schema_version SET version = 74")


def _v74_to_v75(conn: duckdb.DuckDBPyConnection) -> None:
    """v75: source_mode/git_path/base_sha on instance_templates (#622 Slice 1).

    Generalizes the per-key prompt store with an explicit Git⇄Editor source
    toggle, superseding the implicit seed_owns() read-only lock. Existing
    keys default to 'editor' (today's behavior: the DB override wins when set;
    no override → bundled default). base_sha is reserved for Slice 2
    divergence detection (written, never read in Slice 1).

    Idempotent ADD COLUMN IF NOT EXISTS — safe on fresh and upgrade paths.
    Note: DuckDB ``ADD COLUMN ... DEFAULT`` does NOT backfill existing rows,
    so the explicit ``UPDATE ... WHERE source_mode IS NULL`` is required to
    stamp pre-existing 'welcome'/'claude_md' rows as 'editor'.
    """
    conn.execute("ALTER TABLE instance_templates ADD COLUMN IF NOT EXISTS source_mode VARCHAR DEFAULT 'editor'")
    conn.execute("ALTER TABLE instance_templates ADD COLUMN IF NOT EXISTS git_path VARCHAR")
    conn.execute("ALTER TABLE instance_templates ADD COLUMN IF NOT EXISTS base_sha VARCHAR")
    conn.execute("UPDATE instance_templates SET source_mode = 'editor' WHERE source_mode IS NULL")
    conn.execute("UPDATE schema_version SET version = 75")


def _v75_to_v76(conn: duckdb.DuckDBPyConnection) -> None:
    """v76: ``store_entity_votes`` — per-user thumbs up/down on store entities.

    Mirrors ``knowledge_votes``: one row per (entity, user), the vote value
    flips on re-vote (ON CONFLICT upsert in the repo), and a clear deletes the
    row. Additive-only; ``_SYSTEM_SCHEMA`` already creates the table on fresh
    installs (no-op CREATE IF NOT EXISTS here). Issue #398.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS store_entity_votes (
            entity_id VARCHAR NOT NULL,
            user_id VARCHAR NOT NULL,
            vote INTEGER,
            voted_at TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY (entity_id, user_id)
        )
        """
    )
    conn.execute("UPDATE schema_version SET version = 76")


def _v76_to_v77(conn: duckdb.DuckDBPyConnection) -> None:
    """v77: ``users.must_change_password`` — force a password change on first
    sign-in for accounts whose password was set by someone else (seed admin
    from SEED_ADMIN_PASSWORD, admin-set passwords). Additive-only; cleared when
    the user sets their own password. Idempotent ADD COLUMN IF NOT EXISTS, so
    safe on fresh and upgrade paths; ``_SYSTEM_SCHEMA`` already creates the
    column on fresh installs. Issue: emailed-credential rotation.
    """
    conn.execute(
        # DuckDB ALTER can't add a NOT NULL column ("constraints not yet
        # supported"); DEFAULT FALSE backfills existing rows. Fresh installs
        # get NOT NULL from _SYSTEM_SCHEMA. New rows always set it via the repo.
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN DEFAULT FALSE"
    )
    conn.execute("UPDATE schema_version SET version = 77")


def _v77_to_v78(conn: duckdb.DuckDBPyConnection) -> None:
    """v78: built-in marketplace columns.

    Adds ``marketplace_registry.is_builtin`` (BOOLEAN NOT NULL DEFAULT FALSE) so
    the system-seeded built-in marketplace row is distinguishable from
    admin-registered rows. The nightly git-sync path skips is_builtin=TRUE rows.

    Adds ``marketplace_plugins.admin_disabled`` (BOOLEAN NOT NULL DEFAULT FALSE)
    so an admin can disable an individual built-in plugin instance-wide without
    revoking its RBAC grant. Disabled plugins are filtered from the served feed
    for all callers.

    Both columns default to FALSE so pre-existing rows are unaffected. Additive
    ADD COLUMN IF NOT EXISTS — idempotent on fresh and upgrade paths.
    """
    # DuckDB ALTER TABLE ADD COLUMN does NOT support inline constraints
    # (NOT NULL) — "Adding columns with constraints not yet supported". The
    # fresh-create path in CREATE TABLE keeps NOT NULL; the upgrade path adds a
    # nullable column with DEFAULT FALSE (every writer supplies a value, so the
    # nullable-vs-not-null divergence on upgraded DuckDB DBs is cosmetic). This
    # matches the established additive ADD COLUMN pattern elsewhere in this file.
    conn.execute("ALTER TABLE marketplace_registry ADD COLUMN IF NOT EXISTS is_builtin BOOLEAN DEFAULT FALSE")
    conn.execute("ALTER TABLE marketplace_plugins ADD COLUMN IF NOT EXISTS admin_disabled BOOLEAN DEFAULT FALSE")
    conn.execute("UPDATE schema_version SET version = 78")


def _v78_to_v79(conn: duckdb.DuckDBPyConnection) -> None:
    """Named source connections (spec 2026-06-12): generic connection
    registry + vault-backed secrets + table_registry.connection_id."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS source_connections (
            id          VARCHAR PRIMARY KEY,
            name        VARCHAR NOT NULL UNIQUE,
            source_type VARCHAR NOT NULL,
            config      TEXT NOT NULL,
            token_env   VARCHAR,
            is_default  BOOLEAN DEFAULT FALSE,
            created_by  VARCHAR,
            created_at  TIMESTAMP DEFAULT current_timestamp
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS connection_secrets (
            connection_id VARCHAR PRIMARY KEY,
            ciphertext    TEXT NOT NULL,
            updated_at    TIMESTAMP DEFAULT current_timestamp
        )
    """)
    conn.execute("ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS connection_id VARCHAR")
    conn.execute("UPDATE schema_version SET version = 79")


def _v79_to_v80(conn: duckdb.DuckDBPyConnection) -> None:
    """v80: ``authoring_suggestions`` — generic non-admin suggestion queue for
    the authoring studio (data-package / mcp / marketplace / corporate-memory).

    A non-admin submits a proposed create payload (``status='pending'``); an
    admin approves (re-validates + creates the resource, stamps
    ``created_resource_id``) or rejects. Additive-only; ``_SYSTEM_SCHEMA`` creates
    it on fresh installs (no-op CREATE IF NOT EXISTS here).
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS authoring_suggestions (
            id                  VARCHAR PRIMARY KEY,
            domain              VARCHAR NOT NULL,
            payload             JSON,
            status              VARCHAR DEFAULT 'pending',
            created_by          VARCHAR,
            created_at          TIMESTAMP DEFAULT current_timestamp,
            resolved_at         TIMESTAMP,
            resolved_by         VARCHAR,
            resolution_note     TEXT,
            created_resource_id VARCHAR
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_authoring_suggestions_status ON authoring_suggestions(status)")
    conn.execute("UPDATE schema_version SET version = 80")


def _v80_to_v81(conn: duckdb.DuckDBPyConnection) -> None:
    """v81: ``memory_mining_consent`` — per-user opt-IN to having their session
    transcripts mined into shared corporate memory (design spec §4.4). The miner
    only mines transcripts whose author positively opted in. Additive-only;
    ``_SYSTEM_SCHEMA`` creates it on fresh installs (no-op CREATE here).
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_mining_consent (
            user_email   VARCHAR PRIMARY KEY,
            opted_in_at  TIMESTAMP,
            opted_out_at TIMESTAMP,
            updated_at   TIMESTAMP DEFAULT current_timestamp
        )
        """
    )
    conn.execute("UPDATE schema_version SET version = 81")


def _v81_to_v82(conn: duckdb.DuckDBPyConnection) -> None:
    """v82: Collections (bring-your-files) foundation.

    Creates ``file_corpora`` (collection container), ``corpus_files`` (per-file
    row + processing lifecycle) and ``corpus_chunks`` (prose chunks + 384-dim
    embedding). Additive; ``_SYSTEM_SCHEMA`` already creates them on fresh
    installs via IF NOT EXISTS (no-op here). ``corpus_chunks.embedding`` is a
    DuckDB ``FLOAT[384]`` array, queried with ``array_cosine_similarity``.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS file_corpora (
            id VARCHAR PRIMARY KEY,
            slug VARCHAR UNIQUE NOT NULL,
            name VARCHAR NOT NULL,
            description VARCHAR,
            created_by VARCHAR NOT NULL,
            origin VARCHAR NOT NULL DEFAULT 'uploaded',
            created_at TIMESTAMP DEFAULT current_timestamp,
            updated_at TIMESTAMP DEFAULT current_timestamp,
            deleted_at TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS corpus_files (
            id VARCHAR PRIMARY KEY,
            corpus_id VARCHAR NOT NULL,
            filename VARCHAR NOT NULL,
            sha256 VARCHAR NOT NULL,
            file_type VARCHAR,
            size_bytes BIGINT,
            storage_path VARCHAR,
            parent_file_id VARCHAR,
            processing_status VARCHAR NOT NULL DEFAULT 'pending',
            processing_detail VARCHAR,
            created_at TIMESTAMP DEFAULT current_timestamp,
            updated_at TIMESTAMP DEFAULT current_timestamp
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS corpus_chunks (
            id VARCHAR PRIMARY KEY,
            corpus_id VARCHAR NOT NULL,
            file_id VARCHAR NOT NULL,
            ordinal INTEGER,
            text VARCHAR,
            embedding FLOAT[384],
            section_path VARCHAR,
            page INTEGER,
            bbox VARCHAR,
            metadata VARCHAR,
            created_at TIMESTAMP DEFAULT current_timestamp
        )
        """
    )
    conn.execute("UPDATE schema_version SET version = 82")


def _v82_to_v83(conn: duckdb.DuckDBPyConnection) -> None:
    """v83: OAuth 2.1 tables for the native MCP connector (RFC 7591 / RFC 7636).

    Four tables: oauth_clients, oauth_auth_codes, oauth_access_tokens,
    oauth_refresh_tokens. IF NOT EXISTS guards make this a no-op on fresh
    installs where _SYSTEM_SCHEMA already creates the tables.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS oauth_clients (
            client_id        VARCHAR PRIMARY KEY,
            client_secret    VARCHAR,
            redirect_uris    TEXT NOT NULL DEFAULT '[]',
            client_name      VARCHAR,
            client_metadata  TEXT NOT NULL DEFAULT '{}',
            created_at       TIMESTAMP NOT NULL DEFAULT current_timestamp
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS oauth_auth_codes (
            code                             VARCHAR PRIMARY KEY,
            client_id                        VARCHAR NOT NULL,
            scopes                           TEXT NOT NULL DEFAULT '[]',
            code_challenge                   VARCHAR NOT NULL,
            redirect_uri                     VARCHAR NOT NULL,
            redirect_uri_provided_explicitly BOOLEAN NOT NULL DEFAULT FALSE,
            expires_at                       DOUBLE NOT NULL,
            subject                          VARCHAR,
            resource                         VARCHAR,
            state                            VARCHAR
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS oauth_access_tokens (
            token      VARCHAR PRIMARY KEY,
            client_id  VARCHAR NOT NULL,
            scopes     TEXT NOT NULL DEFAULT '[]',
            expires_at BIGINT,
            subject    VARCHAR,
            resource   VARCHAR,
            revoked_at TIMESTAMP,
            created_at TIMESTAMP NOT NULL DEFAULT current_timestamp
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
            token      VARCHAR PRIMARY KEY,
            client_id  VARCHAR NOT NULL,
            scopes     TEXT NOT NULL DEFAULT '[]',
            expires_at BIGINT,
            subject    VARCHAR,
            resource   VARCHAR,
            revoked_at TIMESTAMP,
            created_at TIMESTAMP NOT NULL DEFAULT current_timestamp
        )
    """)
    conn.execute("UPDATE schema_version SET version = 83")


def _v83_to_v84(conn: duckdb.DuckDBPyConnection) -> None:
    """v84: add resource column to oauth_refresh_tokens.

    Refreshed access tokens must preserve the original token's `resource`
    binding (RFC 8707). The auth-code exchange already persists `resource`
    on the access token; the refresh path needs the same value carried on
    the refresh-token row so token rotation doesn't drop it. IF NOT EXISTS
    keeps this a no-op on fresh installs.
    """
    conn.execute("ALTER TABLE oauth_refresh_tokens ADD COLUMN IF NOT EXISTS resource VARCHAR")
    conn.execute("UPDATE schema_version SET version = 84")


def _v84_to_v85(conn: duckdb.DuckDBPyConnection) -> None:
    """v85: pre-seed the vscode-mcp public OAuth client.

    VS Code native MCP is a public client (token_endpoint_auth_method=none,
    no client_secret, PKCE required). Pre-seeding a well-known client_id
    lets users who see the manual-registration dialog simply enter
    'vscode-mcp' without running Dynamic Client Registration separately.

    INSERT OR IGNORE is idempotent — re-running on a DB that already has
    the row (e.g. from a previous manual registration or a re-applied
    migration) is a safe no-op.

    Backend-conditional: when app-state lives in Postgres, the client row
    is owned by the PG side (Alembic 0032) — seeding it here would leak a
    row into the local DuckDB (the inactive backend), so only the ladder
    version is bumped.
    """
    if _state_backend_is_pg():
        conn.execute("UPDATE schema_version SET version = 85")
        return

    conn.execute(
        """
        INSERT OR IGNORE INTO oauth_clients
            (client_id, client_secret, redirect_uris, client_name, client_metadata, created_at)
        VALUES (
            'vscode-mcp',
            NULL,
            '["https://vscode.dev/redirect"]',
            'VS Code (native MCP)',
            '{"token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}',
            CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute("UPDATE schema_version SET version = 85")


def _v85_to_v86(conn: duckdb.DuckDBPyConnection) -> None:
    """v86: backfill users missing an Everyone membership (issue #748).

    PR #131 removed the implicit Everyone grant at user creation; every
    creation call site now re-adds it going forward (see
    ``app.auth.group_sync.ensure_everyone_membership``), but this migration
    covers users created in the interim on already-deployed instances.

    Follows the v13 precedent (``_v12_to_v13_finalize`` step 4): seed the
    system groups first so the target row exists, then backfill via a
    single INSERT..SELECT of every user id lacking an Everyone row.

    Env-conditional like v18's stranded-membership cleanup: when
    ``AGNES_GROUP_EVERYONE_EMAIL`` is set, Everyone is Workspace-controlled
    (memberships come exclusively from google_sync via ``apply_user_groups``)
    and this backfill would inject stray local rows that ``_is_sso_user``
    would then have to distinguish from IdP-owned ones — so it no-ops,
    mirroring the same env-conditional branch in ``_v17_to_v18_finalize``.

    Backend-conditional: when app-state lives in Postgres, the matching
    backfill runs there via Alembic 0033 — seeding groups/memberships here
    would write state rows into the local DuckDB (the inactive backend),
    so only the ladder version is bumped.
    """
    if _state_backend_is_pg():
        conn.execute("UPDATE schema_version SET version = 86")
        return

    _seed_system_groups(conn)

    if os.environ.get("AGNES_GROUP_EVERYONE_EMAIL", "").strip():
        conn.execute("UPDATE schema_version SET version = 86")
        return

    everyone_group_id = conn.execute(
        "SELECT id FROM user_groups WHERE name = ? AND is_system",
        [SYSTEM_EVERYONE_GROUP],
    ).fetchone()[0]

    conn.execute(
        """INSERT INTO user_group_members (user_id, group_id, source, added_by)
           SELECT u.id, ?, 'system_seed', 'system:v86-backfill'
             FROM users u
            WHERE NOT EXISTS (
                SELECT 1 FROM user_group_members m
                 WHERE m.user_id = u.id AND m.group_id = ?
            )""",
        [everyone_group_id, everyone_group_id],
    )
    conn.execute("UPDATE schema_version SET version = 86")


def _v86_to_v87(conn: duckdb.DuckDBPyConnection) -> None:
    """v87: add `ref` (tag/commit pin) column to marketplace_registry (#781).

    Pins a registered marketplace to a fixed tag name or full 40-char commit
    SHA so nightly/manual syncs stop tracking `branch` (or remote HEAD) once
    set. NULL on every existing row — floating (branch/HEAD) behavior is
    unchanged until an admin opts in via the edit modal / API.
    """
    conn.execute("ALTER TABLE marketplace_registry ADD COLUMN IF NOT EXISTS ref VARCHAR")
    conn.execute("UPDATE schema_version SET version = 87")


def _v87_to_v88(conn: duckdb.DuckDBPyConnection) -> None:
    """v87→v88: ``corpus_files.parent_file_id`` — bundle (zip) child linkage (K1).

    Children extracted from an uploaded archive point at the archive's own
    ``corpus_files`` row; directly-uploaded files (and archive rows
    themselves) keep NULL. Guarded ALTER so upgrades from a fresh-install
    schema (which already carries the column) stay idempotent.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info('corpus_files')").fetchall()}
    if "parent_file_id" not in cols:
        conn.execute("ALTER TABLE corpus_files ADD COLUMN parent_file_id VARCHAR")
    conn.execute("UPDATE schema_version SET version = 88")


def _v88_to_v89(conn: duckdb.DuckDBPyConnection) -> None:
    """v88→v89: ``knowledge_digests`` — maintained digests (K4, #799).

    Idempotent CREATE TABLE IF NOT EXISTS; fresh installs already get the
    table from ``_SYSTEM_SCHEMA`` (no-op here). Sequential upgrades from a
    v88 instance create it now.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_digests (
            id                 VARCHAR PRIMARY KEY,
            slug               VARCHAR NOT NULL UNIQUE,
            title              VARCHAR NOT NULL,
            instructions       TEXT NOT NULL,
            source_corpus_ids  VARCHAR,
            output_md          TEXT,
            source_fingerprint VARCHAR,
            generated_at       TIMESTAMP,
            model              VARCHAR,
            status             VARCHAR DEFAULT 'pending',
            status_reason      VARCHAR,
            created_by         VARCHAR,
            created_at         TIMESTAMP DEFAULT current_timestamp,
            updated_at         TIMESTAMP DEFAULT current_timestamp
        )
    """)
    conn.execute("UPDATE schema_version SET version = 89")


def _v89_to_v90(conn: duckdb.DuckDBPyConnection) -> None:
    """v89→v90: ``chat_broker_tickets`` — chat sandbox secret broker tickets.

    Opaque, short-lived tickets minted by ``ticket_repo().mint`` so a
    sandboxed chat agent never holds the real ``ANTHROPIC_API_KEY`` /
    ``AGNES_TOKEN`` — only an opaque token the broker resolves server-side.

    Idempotent CREATE TABLE/INDEX IF NOT EXISTS; fresh installs already get
    the table from ``_SYSTEM_SCHEMA`` (no-op here). Sequential upgrades from
    a v89 instance create it now.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_broker_tickets (
            token       VARCHAR PRIMARY KEY,
            session_id  VARCHAR NOT NULL,
            scope       VARCHAR NOT NULL,
            expires_at  TIMESTAMP NOT NULL,
            created_at  TIMESTAMP NOT NULL DEFAULT current_timestamp
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_broker_tickets_session_id ON chat_broker_tickets(session_id)")
    conn.execute("UPDATE schema_version SET version = 90")


def _v90_to_v91(conn: duckdb.DuckDBPyConnection) -> None:
    """v90→v91: skill lint (store guardrails) — ``store_lint_runs``,
    ``store_lint_findings``, ``store_lint_dismissals``,
    ``store_lint_entity_state``.

    Additive-only; ``_SYSTEM_SCHEMA`` already creates all four tables on
    fresh installs (no-op ``CREATE IF NOT EXISTS`` here).
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS store_lint_runs (
            id               VARCHAR PRIMARY KEY,
            trigger          VARCHAR NOT NULL,
            started_at       TIMESTAMP NOT NULL,
            finished_at      TIMESTAMP,
            entities_linted  INTEGER NOT NULL DEFAULT 0,
            entities_skipped INTEGER NOT NULL DEFAULT 0,
            findings_count   INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS store_lint_findings (
            id           VARCHAR PRIMARY KEY,
            run_id       VARCHAR NOT NULL,
            entity_id    VARCHAR NOT NULL,
            rule_id      VARCHAR NOT NULL,
            severity     VARCHAR NOT NULL,
            message      VARCHAR NOT NULL,
            evidence     VARCHAR DEFAULT '{}',
            doc_url      VARCHAR DEFAULT '',
            content_hash VARCHAR DEFAULT '',
            created_at   TIMESTAMP NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_store_lint_findings_entity ON store_lint_findings(entity_id)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS store_lint_dismissals (
            entity_id    VARCHAR NOT NULL,
            rule_id      VARCHAR NOT NULL,
            dismissed_by VARCHAR NOT NULL,
            dismissed_at TIMESTAMP NOT NULL,
            content_hash VARCHAR NOT NULL,
            PRIMARY KEY (entity_id, rule_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS store_lint_entity_state (
            entity_id    VARCHAR PRIMARY KEY,
            content_hash VARCHAR NOT NULL,
            run_id       VARCHAR NOT NULL,
            linted_at    TIMESTAMP NOT NULL
        )
        """
    )
    conn.execute("UPDATE schema_version SET version = 91")


def _v91_to_v92(conn: duckdb.DuckDBPyConnection) -> None:
    """v92: mcp_sources.connect_hint — per-source, admin-authored instructions
    telling a user where to obtain their personal token for a per_user source.
    Rendered through app/markdown_render.render_safe on the connect page.

    Guarded on table existence: minimal-fixture migration tests replay the
    ladder from an intermediate version onto a DB that never created
    ``mcp_sources`` (it is created at v64). On a real ladder the table always
    exists by v92, so the guard only no-ops those partial replays."""
    exists = conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'mcp_sources'").fetchone()
    if exists:
        conn.execute("ALTER TABLE mcp_sources ADD COLUMN IF NOT EXISTS connect_hint VARCHAR")
    conn.execute("UPDATE schema_version SET version = 92")


def _v92_to_v93(conn: duckdb.DuckDBPyConnection) -> None:
    """v92→v93: ``glossary_terms`` — Keboola semantic-glossary import
    destination (docs/superpowers/specs/2026-07-17-keboola-glossary-import-design.md).

    Additive-only; ``_SYSTEM_SCHEMA`` already creates the table on fresh
    installs (no-op ``CREATE TABLE IF NOT EXISTS`` here).
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS glossary_terms (
            id           VARCHAR PRIMARY KEY,
            term         VARCHAR NOT NULL,
            definition   TEXT NOT NULL,
            see_also     VARCHAR[],
            model_uuid   VARCHAR,
            source       VARCHAR NOT NULL DEFAULT 'manual',
            source_ref   VARCHAR,
            created_at   TIMESTAMP DEFAULT current_timestamp,
            updated_at   TIMESTAMP DEFAULT current_timestamp
        )
        """
    )
    conn.execute("UPDATE schema_version SET version = 93")


def _v93_to_v94(conn: duckdb.DuckDBPyConnection) -> None:
    """v93→v94: ``jobs`` — durable job queue (wave-2B worker runtime
    foundation). This migration covers table + claim/lookup index only;
    ``_SYSTEM_SCHEMA`` already creates it on fresh installs (no-op
    ``CREATE IF NOT EXISTS`` here).

    See the ``jobs`` block in ``_SYSTEM_SCHEMA`` above for why
    ``idx_jobs_idem`` is a plain index rather than a partial unique index
    (DuckDB does not support partial indexes) — idempotency dedup is
    enforced in ``JobsRepository.enqueue()`` instead. The Postgres ladder
    uses a real partial unique index for this same column; see that
    docstring block for why the two ladders are intentionally asymmetric.

    ``lease_token`` is the same-worker double-execution guard — see the
    ``_SYSTEM_SCHEMA`` docstring block above for the full rationale.

    Renumbered from v93 to v94 after upstream's glossary_terms migration
    (#920) claimed schema v93 first.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id                VARCHAR PRIMARY KEY,
            kind              VARCHAR NOT NULL,
            payload_json      VARCHAR NOT NULL DEFAULT '{}',
            status            VARCHAR NOT NULL DEFAULT 'queued',
            priority          INTEGER NOT NULL DEFAULT 0,
            run_after         TIMESTAMP,
            attempts          INTEGER NOT NULL DEFAULT 0,
            max_attempts      INTEGER NOT NULL DEFAULT 3,
            lease_expires_at  TIMESTAMP,
            leased_by         VARCHAR,
            lease_token       VARCHAR,
            idempotency_key   VARCHAR,
            error             VARCHAR,
            created_at        TIMESTAMP NOT NULL,
            started_at        TIMESTAMP,
            finished_at       TIMESTAMP
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(status, priority, run_after)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_idem ON jobs(idempotency_key)")
    conn.execute("UPDATE schema_version SET version = 94")


def _v94_to_v95(conn: duckdb.DuckDBPyConnection) -> None:
    """v94→v95: drop the 3 secondary (non-unique) ART indexes on
    ``usage_session_summary`` — ``idx_usage_session_user`` (username),
    ``idx_usage_session_started`` (started_at), ``idx_usage_session_user_id``
    (user_id).

    INCIDENT 2026-07-20: the periodic usage session-processor calls
    ``UsageRepository.upsert_summary`` every ~10 minutes, which refreshes
    all three of these columns via ``ON CONFLICT (session_file) DO UPDATE
    SET ...`` on every re-process tick. Updating an ART-indexed column runs
    as delete-old-entry + insert-new-entry; a single corrupt secondary-
    index entry turned that delete into a FATAL ``Failed to delete all rows
    from index`` error, which invalidates the whole DuckDB connection
    ("database has been invalidated ... must be restarted") for every
    subsequent query on the process — including login — and recurred on
    every scheduler tick since nothing marked the session processed. This
    migration removes the indexes themselves — repairing the already-
    corrupt structure on a live instance and removing the index maintenance
    that made those column rewrites fatal. ``upsert_summary`` still
    refreshes the columns (safe now that they are unindexed: a plain
    in-place write with no ART maintenance), so late-resolution identity
    backfill is preserved; do NOT re-add secondary indexes on them.
    ``session_file`` (the PRIMARY KEY) is untouched.

    Plain ``DROP INDEX IF EXISTS`` rather than a CTAS table rebuild: it is
    a catalog-only structural operation that does not need to walk (and
    therefore does not need to trust) the corrupted index's contents, so
    it succeeds even against a broken ART.
    """
    conn.execute("DROP INDEX IF EXISTS idx_usage_session_user")
    conn.execute("DROP INDEX IF EXISTS idx_usage_session_started")
    conn.execute("DROP INDEX IF EXISTS idx_usage_session_user_id")
    conn.execute("UPDATE schema_version SET version = 95")


def _v95_to_v96(conn: duckdb.DuckDBPyConnection) -> None:
    """v95→v96: ``data_apps`` registry (hosted user web apps).

    ``_SYSTEM_SCHEMA`` already creates the table on fresh installs (it is
    appended via the shared ``_DATA_APPS_CREATE_SQL`` constant); this
    migration covers the sequential-upgrade path from a pre-v96 instance.
    """
    conn.execute(_DATA_APPS_CREATE_SQL)
    conn.execute("UPDATE schema_version SET version = 96")


def _ensure_corpus_path_index(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the ``corpus_files(corpus_id, path)`` UNIQUE INDEX if possible.

    Enforces the upsert invariant: at most one row per ``(corpus_id, path)``.
    Plain (not partial) UNIQUE INDEX — DuckDB has no partial indexes, but NULLs
    are distinct on both DuckDB and Postgres, so ``path=NULL`` (plain-insert
    files, bundle children) is exempt while set paths stay unique.

    Called unconditionally at the end of ``_ensure_schema`` rather than from
    ``_SYSTEM_SCHEMA``, because ``_SYSTEM_SCHEMA`` runs *before* the migration
    ladder: ``corpus_files`` exists since v82 but ``path`` is ALTER-added at
    v97, so an index declared in ``_SYSTEM_SCHEMA`` raises BinderException on
    every v82..v96 DB and aborts the schema pass before the ALTER can run.

    Guarded on the column actually existing so the split-brain self-heal path
    (future-version DB, ladder skipped) degrades to a no-op instead of raising.
    """
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info('corpus_files')").fetchall()}
    except Exception:
        # Table absent entirely (pre-v82 DB shape) — nothing to index.
        return
    if "path" not in cols:
        return
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_corpus_files_corpus_path ON corpus_files(corpus_id, path)")


def _v96_to_v97(conn: duckdb.DuckDBPyConnection) -> None:
    """v96→v97: ``corpus_files.path`` — logical path for upsert-on-upload.

    An optional caller-supplied identity (e.g. a repo-relative path) so
    re-uploading the same logical file REPLACES the existing row instead of
    inserting a duplicate (keyed on ``(corpus_id, path)``). NULL on every
    existing row and on uploads that omit it — behavior is unchanged (plain
    insert) until a caller opts in. Guarded ALTER so upgrades from a fresh-
    install schema (which already carries the column) stay idempotent.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info('corpus_files')").fetchall()}
    if "path" not in cols:
        conn.execute("ALTER TABLE corpus_files ADD COLUMN path VARCHAR")
    # Enforce at most one row per (corpus_id, path). Existing rows all have
    # path=NULL (just-added column), so the index build can't collide.
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_corpus_files_corpus_path ON corpus_files(corpus_id, path)")
    conn.execute("UPDATE schema_version SET version = 97")


def _v97_to_v98(conn: duckdb.DuckDBPyConnection) -> None:
    """v97→v98: ``chat_sessions.relay_protocol_version`` (restart-invariant
    sandbox reuse) **and** ``user_journey_state`` (chat-driven onboarding).

    Both landed as v98 — the relay column on main, the journey table on the
    paper-theme branch (which replaced this function's body rather than
    stacking after it). The merged step does BOTH so a ≤v97 database climbs
    into the same schema either lineage produced; each half is idempotent.
    Databases already past 98 that climbed the *branch* ladder are missing
    the relay column — ``_heal_stranded_ladder_columns`` repairs those — and
    ones that climbed *main's* ladder are missing the journey table, which
    ``_v113_to_v114`` re-asserts (every pre-merge database is < 114).

    Relay column: NULL (every existing row) means unknown/legacy, preserving
    conservative fresh-spawn behavior until ``set_sandbox_ref`` stamps it.
    Un-indexed, matching the other sandbox-ref columns (DuckDB 1.5.3
    FK+index bug — see the ``chat_sessions`` DDL comment in
    ``_SYSTEM_SCHEMA``). Fresh installs get both objects from
    ``_SYSTEM_SCHEMA`` (no-op here).
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info('chat_sessions')").fetchall()}
    if "relay_protocol_version" not in cols:
        conn.execute("ALTER TABLE chat_sessions ADD COLUMN relay_protocol_version INTEGER")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_journey_state (
            user_id             VARCHAR PRIMARY KEY,
            first_asked         BOOLEAN NOT NULL DEFAULT FALSE,
            stack_setup_done    BOOLEAN NOT NULL DEFAULT FALSE,
            explored_stack      BOOLEAN NOT NULL DEFAULT FALSE,
            catalog_discovered  BOOLEAN NOT NULL DEFAULT FALSE,
            use_anywhere        BOOLEAN NOT NULL DEFAULT FALSE,
            onboarded           BOOLEAN NOT NULL DEFAULT FALSE,
            successful_answers  INTEGER NOT NULL DEFAULT 0,
            updated_at          TIMESTAMP NOT NULL DEFAULT current_timestamp
        )
    """)
    conn.execute("UPDATE schema_version SET version = 98")


def _v109_to_v110(conn: duckdb.DuckDBPyConnection) -> None:
    """v109→v110: add ``file_corpora.origin`` (``uploaded`` | ``generated``).

    Provenance for the Artefacts toolbar's Source facet. Every existing
    artefact is user-uploaded, so the column defaults to ``'uploaded'``; the
    future agent-generated-artefact writer sets ``'generated'``. Idempotent
    ``ADD COLUMN IF NOT EXISTS`` guarded on table existence — a no-op on fresh
    installs (``_SYSTEM_SCHEMA`` already declares the column).

    Restacked twice from the paper-theme branch onto main's ladder (v107→v108
    after main's data_apps-linked step claimed v108, then v109→v110 after
    main's mcp-oauth step claimed v109).
    """
    exists = conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'file_corpora'").fetchone()
    if exists:
        conn.execute("ALTER TABLE file_corpora ADD COLUMN IF NOT EXISTS origin VARCHAR DEFAULT 'uploaded'")
    conn.execute("UPDATE schema_version SET version = 110")


def _v110_to_v111(conn: duckdb.DuckDBPyConnection) -> None:
    """v110→v111: ``store_entities`` publisher + verification columns.

    Backfill intent: every pre-v111 row is a user upload (the "publish as the
    organization" action ships with this version), so ``publisher_kind``
    defaults to ``'user'`` and ``verification_state`` to ``'none'`` — which is
    also the state that renders NO chip, so the upgrade is visually inert.

    Idempotent ``ADD COLUMN IF NOT EXISTS`` guarded on table existence; a no-op
    on fresh installs where ``_SYSTEM_SCHEMA`` already declares the columns.
    The DDL lives in :func:`_add_store_entity_trust_columns` so the heal path can
    reuse it **without** re-stamping the version — see
    :func:`_heal_store_entity_trust_columns`.

    Restacked twice from the paper-theme branch onto main's ladder (v108→v109
    after main's data_apps-linked step claimed v108, then v110→v111 after
    main's mcp-oauth step claimed v109).
    """
    _add_store_entity_trust_columns(conn)
    conn.execute("UPDATE schema_version SET version = 111")


def _v111_to_v112(conn: duckdb.DuckDBPyConnection) -> None:
    """v111→v112: agents builder superset columns.

    Combines the paper-theme agent-builder's authored fields onto main's
    ``agents`` table so both the agent-as-API backend (main) and the /agents
    builder (paper-theme) read one table: ``role``/``tone``/``greeting`` plus
    the ``knowledge``/``plugins``/``surfaces`` id-list payloads (JSON text) and
    ``status`` (draft | ready). ``created_by`` maps to main's ``owner_user_id``
    and ``instructions`` to ``system_prompt`` in the repository layer, so no
    duplicate columns are added for those.

    Idempotent ``ADD COLUMN IF NOT EXISTS`` guarded on table existence — a no-op
    on fresh installs where ``_SYSTEM_SCHEMA`` already declares them.

    Restacked twice from the paper-theme branch onto main's ladder (v109→v110
    after main's data_apps-linked step claimed v108, then v111→v112 after
    main's mcp-oauth step claimed v109).
    """
    exists = conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'agents'").fetchone()
    if exists:
        conn.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS role VARCHAR")
        conn.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS tone VARCHAR DEFAULT 'concise'")
        conn.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS greeting TEXT")
        conn.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS knowledge TEXT DEFAULT '[]'")
        conn.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS plugins TEXT DEFAULT '[]'")
        conn.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS surfaces TEXT DEFAULT '{}'")
        conn.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS status VARCHAR DEFAULT 'draft'")
    conn.execute("UPDATE schema_version SET version = 112")


def _v112_to_v113(conn: duckdb.DuckDBPyConnection) -> None:
    """v112→v113: add ``chat_sessions.pinned_at`` (user-pinned conversations).

    NULL = not pinned, which is every pre-v113 row — so the upgrade is visually
    inert (the history panel renders no Pinned group until the user pins
    something). A timestamp rather than a boolean so pins order
    most-recently-pinned-first.

    The column is deliberately NOT indexed: it is UPDATEd on every pin/unpin,
    which on DuckDB 1.5.3 happens long after ``chat_messages`` rows exist for
    the session — indexing it would trip the FK+index false-violation bug that
    already forces ``last_message_at`` / ``message_count`` to be derived at read
    time (see the ``chat_sessions`` DDL in ``_SYSTEM_SCHEMA``).

    Idempotent ``ADD COLUMN IF NOT EXISTS`` guarded on table existence — a no-op
    on fresh installs where ``_SYSTEM_SCHEMA`` already declares the column.
    """
    exists = conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'chat_sessions'").fetchone()
    if exists:
        conn.execute("ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS pinned_at TIMESTAMP")
    # 113, not 112: this step has been renumbered on every restack (originally
    # _v110_to_v111, then _v111_to_v112, now _v112_to_v113 after main's
    # mcp-oauth step claimed v109), and the stamp has to move with each rename.
    # The fresh-install branch runs the steps unguarded with no tail stamp of
    # its own, so whatever the LAST step writes is the version the database
    # ends on — a stale stamp here strands every DB one short of SCHEMA_VERSION
    # and re-runs the whole ladder on every boot.
    conn.execute("UPDATE schema_version SET version = 113")


def _add_data_package_publisher_column(conn: duckdb.DuckDBPyConnection) -> None:
    """The v114 column DDL + backfill, with no version stamp.

    Split from the versioned step for the same reason as
    :func:`_add_store_entity_trust_columns`: a stamp in here would downgrade
    any instance already past 114 if a repair ever called it.

    The backfill is the whole point of the step. Before v114 a package's trust
    claim was *derived* on every render from "is ``created_by`` currently in the
    Admin group", so the upgrade must freeze whatever that derivation said at
    this moment into the column — otherwise every existing admin-created package
    would silently drop from Organization to Community on upgrade, which is a
    visible downgrade of a claim nobody asked to change.

    It resolves Admin membership through the same repositories the API uses
    rather than a hand-written JOIN: ``created_by`` holds a user id on some rows
    and an email on others (the API looks up both — see ``_badges_for``), and the
    Admin group is seeded ``is_system`` with its membership possibly written by
    the Google sync, so a raw two-table JOIN gets this wrong on a Postgres
    instance exactly as it did before (that bug is why ``_badges_for`` routes
    through the factory today).
    """
    exists = conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'data_packages'").fetchone()
    if not exists:
        return
    conn.execute("ALTER TABLE data_packages ADD COLUMN IF NOT EXISTS publisher_kind VARCHAR DEFAULT 'user'")
    # Pre-existing rows read NULL, not the DEFAULT (which applies to inserts
    # only), so normalize first and let the backfill promote from there.
    conn.execute("UPDATE data_packages SET publisher_kind = 'user' WHERE publisher_kind IS NULL")

    # Backfill in PLAIN SQL, on this same connection.
    #
    # It must NOT go through the repositories, however much the request-time
    # equivalent (``_badges_for``) has to: this runs inside ``_ensure_schema``,
    # while the single DuckDB writer is already held by ``conn``. Calling
    # ``users_repo()`` here re-enters the connection layer and asks for that same
    # writer, and the process hangs before "Application startup complete" — no
    # error, no traceback, just a server that never comes up. Learned the hard
    # way; the v104 sibling below is pure SQL for the same reason.
    #
    # Matches the Alembic sibling's predicate, including both shapes of
    # ``created_by`` seen in the wild — a user id on some rows, an email on
    # others. Guarded on the RBAC tables: without them every row stays 'user'.
    have = {
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name IN ('users', 'user_groups', 'user_group_members')"
        ).fetchall()
    }
    if not {"users", "user_groups", "user_group_members"} <= have:
        return
    # ``users.email`` is guarded separately from the table itself. A database
    # climbing the ladder from a very old schema reaches this step with whatever
    # ``users`` looked like when it was created, which can be id-only — and
    # DuckDB reports the missing column as ``Referenced table "u" not found``,
    # naming the alias rather than the column, so the failure reads like a
    # malformed query instead of an absent column. Match on id alone in that
    # case: an id-only ``users`` cannot carry an email for ``created_by`` to
    # have been written from, so the arm is dead weight, not lost coverage.
    user_cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'users'"
        ).fetchall()
    }
    created_by_match = "u.id = data_packages.created_by"
    if "email" in user_cols:
        created_by_match = f"({created_by_match} OR u.email = data_packages.created_by)"
    conn.execute(
        f"""
        UPDATE data_packages
           SET publisher_kind = 'organization'
         WHERE created_by IS NOT NULL
           AND EXISTS (
                 SELECT 1
                   FROM users u
                   JOIN user_group_members m ON m.user_id = u.id
                   JOIN user_groups g        ON g.id = m.group_id
                  WHERE g.name = 'Admin'
                    AND {created_by_match}
               )
        """
    )


def _heal_data_package_publisher_column(conn: duckdb.DuckDBPyConnection) -> None:
    """Ensure ``data_packages.publisher_kind`` exists, whatever the stamp says.

    Same repair, same reason, as :func:`_heal_store_entity_trust_columns`:
    ``schema_version`` is not evidence that a versioned step ran. The ladder's
    tail stamps ``SCHEMA_VERSION`` unconditionally, so a database opened by a
    build where the constant had already been bumped to 114 but ``_v113_to_v114``
    did not yet exist (or was not yet wired into the ladder) gets marked 114
    **without** the column — and is then skipped forever, because
    ``current < 114`` is false.

    It fails at query time with ``Binder Error: Referenced column
    "publisher_kind" not found``, and only on the paths that name the column, so
    it looks intermittent: the catalog and the package detail page 500 while a
    bare ``SELECT *`` keeps working. This is not hypothetical — it happened
    during development of this step's original v113 numbering, in the window
    between bumping the constant and wiring the step.

    Deployed instances cannot hit it (constant and step ship in one commit), but
    development and multi-worktree checkouts can. Presence of the column is
    checked directly: one ``information_schema`` read per boot, authoritative,
    and immune to a wrong stamp in either direction.
    """
    exists = conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'data_packages'").fetchone()
    if not exists:
        return
    has_col = conn.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name = 'data_packages' AND column_name = 'publisher_kind'"
    ).fetchone()
    if has_col:
        return
    _add_data_package_publisher_column(conn)


def _v113_to_v114(conn: duckdb.DuckDBPyConnection) -> None:
    """v113→v114: add ``data_packages.publisher_kind`` and backfill it.

    Turns the render-time-derived `curated` badge into the same stored trust
    axis ``store_entities`` already carries, so every surface can render one
    shared Organization / Verified / Community marker instead of four different
    vocabularies for overlapping claims. See
    :func:`_add_data_package_publisher_column` for why the derivation had to go
    and what the backfill preserves.

    Idempotent: ``ADD COLUMN IF NOT EXISTS`` guarded on table existence, and a
    no-op on fresh installs where ``_SYSTEM_SCHEMA`` already declares the column
    (the backfill then finds no rows).

    Also re-asserts ``user_journey_state``: that table's ladder home is
    ``_v97_to_v98``, a step every pre-merge database is already stamped past —
    main's lineage never created it (the branch put it there), so the first
    step the *merged* ladder is guaranteed to run must carry it too. CREATE
    TABLE IF NOT EXISTS — a no-op wherever v98 or ``_SYSTEM_SCHEMA`` already
    made it.
    """
    _add_data_package_publisher_column(conn)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_journey_state (
            user_id             VARCHAR PRIMARY KEY,
            first_asked         BOOLEAN NOT NULL DEFAULT FALSE,
            stack_setup_done    BOOLEAN NOT NULL DEFAULT FALSE,
            explored_stack      BOOLEAN NOT NULL DEFAULT FALSE,
            catalog_discovered  BOOLEAN NOT NULL DEFAULT FALSE,
            use_anywhere        BOOLEAN NOT NULL DEFAULT FALSE,
            onboarded           BOOLEAN NOT NULL DEFAULT FALSE,
            successful_answers  INTEGER NOT NULL DEFAULT 0,
            updated_at          TIMESTAMP NOT NULL DEFAULT current_timestamp
        )
    """)
    conn.execute("UPDATE schema_version SET version = 114")


def _v114_to_v115(conn: duckdb.DuckDBPyConnection) -> None:
    """v114→v115: one-time reclassification of pre-existing governance-created
    agents from ``status='draft'`` to ``'ready'``.

    An agent created through the governance API (``POST /api/v1/agents``,
    ``app/api/agents_admin.py::create_agent``) always carries an explicit,
    caller-chosen slug, and that route refuses to change it afterwards
    (``PUT`` 400s ``slug_immutable``) — the agent is published by definition
    the moment it exists. Before this release the create route never set a
    ``status`` at all, and both repositories' ``create()`` COALESCE a missing
    one to ``'draft'`` — so a governance-created agent was indistinguishable
    from a ``/agents`` builder placeholder that was never named. The
    builder's draft-rename rule (``app/api/agents.py::_draft_slug_rename``)
    only freezes a slug once the agent is ``'ready'``, so a
    deliberately-chosen slug — the one a PAT may already be minted against —
    stayed renameable forever through the builder's PATCH. The create route
    now sets ``status='ready'`` going forward; this step closes the gap for
    every agent created before that fix shipped.

    The discriminator is the row's ``id`` PREFIX, not its slug. Every
    builder-created row — ``POST /api/agents`` (``app/api/agents.py``,
    ~line 346) — carries an ``agt_`` prefix regardless of whether the agent
    was ever named, because that route mints ``"agt_" + uuid4().hex`` before
    the caller supplies (or omits) a ``name``. A row created through the
    governance API (``POST /api/v1/agents``,
    ``app/api/agents_admin.py::create_agent``) or the seeded default
    (``AgentsRepository.get_or_create_default``) always gets a bare
    ``uuid4()`` — neither path ever applies the prefix. A slug-based check
    (matching only the unnamed placeholder lineage ``agent`` / ``agent-N``)
    was tried first and is WRONG: a builder draft that the user already
    named — e.g. ``finance-bot`` — is not yet published (``_draft_slug_rename``
    only freezes its slug once ``status`` reaches ``'ready'``), but its slug
    no longer matches the placeholder pattern, so the slug check promoted it
    anyway and permanently froze an address for an agent that was never
    marked ready. ``NOT (id LIKE 'agt\\_%' ESCAPE '\\')`` has no such gap: it
    is true for every governance/default row and false for every builder
    row, named or not, so it selects exactly the intended cohort regardless
    of naming state. The seeded default agent (``is_default``) is excluded
    on top of that, redundantly but explicitly: it is seeded with no status
    (COALESCEd to ``'draft'``) and is a PERMANENT draft by design — see
    ``_draft_slug_rename``'s ``is_default`` exemption — so promoting it here
    would freeze an address that must keep renaming freely.

    Naturally idempotent: an already-``'ready'`` row no longer matches the
    ``WHERE``, so re-running this (as a fresh install's ladder walk does) is
    a no-op.

    Guarded on ``agents`` existing with ``status``/``is_default`` — same
    style as ``_v111_to_v112``. A database built by the pre-merge
    paper-theme branch reaches this step still in that branch's own shape
    (``created_by``/``instructions``, no ``is_default`` — see
    :func:`_heal_legacy_agents_table`), stamped somewhere in the 10x range,
    so the ladder walk lands here with a table this ``UPDATE`` cannot bind
    against. ``_heal_legacy_agents_table`` is the only thing that knows how
    to rebuild that table, and it runs at the *bottom* of ``_ensure_schema``,
    after the migration ladder — so an unguarded ``UPDATE`` here raises a
    DuckDB ``Binder Error`` and aborts startup before the heal ever gets a
    chance to run. Skipping the reclassification on that shape is safe: the
    heal's own INSERT ... SELECT will still carry over whatever ``status``
    the row already had.
    """
    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'agents'"
        ).fetchall()
    }
    if {"status", "is_default"} <= cols:
        conn.execute(r"""
            UPDATE agents
            SET status = 'ready', updated_at = current_timestamp
            WHERE COALESCE(status, 'draft') = 'draft'
              AND NOT COALESCE(is_default, FALSE)
              AND NOT (id LIKE 'agt\_%' ESCAPE '\')
        """)
    conn.execute("UPDATE schema_version SET version = 115")


def _v115_to_v116(conn: duckdb.DuckDBPyConnection) -> None:
    """v115→v116: table access-policy columns on ``table_registry``.

    Five additive, nullable columns backing the table access policies
    feature
    (``docs/superpowers/specs/2026-08-11-table-access-policies-design.md``
    §4): one SQL policy per table, substituted for that table on every
    server-side read to filter rows and mask columns by the caller's
    identity. ``access_policy_sql IS NULL`` means "no policy" and every
    enforcement path short-circuits to today's unfiltered behavior — this
    step alone changes no runtime behavior.

    - ``access_policy_sql``: the policy ``SELECT``, DuckDB dialect.
    - ``access_policy_note``: admin-facing "why" (mandatory at the API
      layer when a policy is set; not enforced by this DDL).
    - ``access_policy_updated_at`` / ``access_policy_updated_by``: last-edit
      convenience columns; ``audit_log`` remains authoritative.
    - ``policy_mapping``: marks this table as referenceable from another
      table's policy body (mapping tables, e.g. a user→cost-center map).
      Defaults ``false`` so no existing table is retroactively eligible.

    Idempotent ADD COLUMN IF NOT EXISTS — safe on fresh and upgrade paths.
    """
    for ddl in (
        "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS access_policy_sql VARCHAR",
        "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS access_policy_note VARCHAR",
        "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS access_policy_updated_at TIMESTAMP",
        "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS access_policy_updated_by VARCHAR",
        "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS policy_mapping BOOLEAN DEFAULT false",
    ):
        conn.execute(ddl)
    conn.execute("UPDATE schema_version SET version = 116")


def _v116_to_v117(conn: duckdb.DuckDBPyConnection) -> None:
    """v116→v117: semantic_models + semantic_sources + the data-package junction.

    Pure additive DDL — no backfill. Existing metric_definitions and
    glossary_terms rows keep their provenance and are NOT retro-attached to a
    model: there is no document they came from, and inventing one would make
    `export` emit a document the instance never received.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS semantic_models (
            id                VARCHAR PRIMARY KEY,
            slug              VARCHAR NOT NULL,
            name              VARCHAR NOT NULL,
            description       TEXT,
            document          TEXT NOT NULL,
            document_json     JSON,
            spec_version      VARCHAR NOT NULL,
            content_hash      VARCHAR NOT NULL,
            source            VARCHAR NOT NULL DEFAULT 'manual',
            source_ref        VARCHAR,
            status            VARCHAR NOT NULL DEFAULT 'valid',
            validation_errors JSON,
            validated_at      TIMESTAMP,
            created_at        TIMESTAMP DEFAULT current_timestamp,
            updated_at        TIMESTAMP DEFAULT current_timestamp
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_semantic_models_origin
            ON semantic_models (source, source_ref, slug)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS semantic_sources (
            id               VARCHAR PRIMARY KEY,
            kind             VARCHAR NOT NULL,
            name             VARCHAR NOT NULL,
            adapter          VARCHAR NOT NULL,
            config           JSON NOT NULL,
            enabled          BOOLEAN DEFAULT TRUE,
            last_sync_at     TIMESTAMP,
            last_sync_status VARCHAR,
            last_sync_error  TEXT,
            created_at       TIMESTAMP DEFAULT current_timestamp,
            updated_at       TIMESTAMP DEFAULT current_timestamp
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS data_package_semantic_models (
            package_id VARCHAR NOT NULL,
            model_id   VARCHAR NOT NULL,
            PRIMARY KEY (package_id, model_id)
        )
    """)
    conn.execute("UPDATE schema_version SET version = 117")


def _v117_to_v118(conn: duckdb.DuckDBPyConnection) -> None:
    """v117→v118: add ``user_journey_state.agent_created`` boolean.

    The onboarding checklist gained a sixth step — "Create your first agent" —
    which had no backing column, so it could not be tracked at all.

    The BACKFILL is the point of this step, not the column. A journey flag that
    lands FALSE for everyone re-opens a checklist people had finished: their
    retired card comes back carrying one unticked row, which reads as the
    product losing their progress. (That objection is why the sixth row was
    turned down once already — see
    ``tests/test_tour_onboarding_steps.py::test_the_checklist_carries_the_agent_step``.)
    So the step is marked done for anyone it is already true of, or moot for:

      * users who own an agent — they did the thing, and
      * users already flagged ``onboarded`` — their journey is closed, and a
        step invented afterwards is not theirs to complete.

    Only a genuinely mid-onboarding user sees the new row, which is who it is
    for. DuckDB rejects NOT NULL in ADD COLUMN, so the column is nullable with
    a DEFAULT and existing rows are normalised in the same step.

    Idempotent: uses ``PRAGMA table_info`` to skip the ALTER when the column
    already exists (the same pattern used by all neighbouring steps).
    """
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info('user_journey_state')").fetchall()}
    if "agent_created" not in existing_cols:
        conn.execute("ALTER TABLE user_journey_state ADD COLUMN agent_created BOOLEAN DEFAULT FALSE")
        conn.execute("UPDATE user_journey_state SET agent_created = FALSE WHERE agent_created IS NULL")
        conn.execute("UPDATE user_journey_state SET agent_created = TRUE WHERE onboarded = TRUE")
        # Owning an agent is the milestone itself, whatever the journey says.
        try:
            conn.execute(
                "UPDATE user_journey_state SET agent_created = TRUE "
                "WHERE user_id IN (SELECT DISTINCT owner_user_id FROM agents)"
            )
        except duckdb.Error:
            # `agents` predates this step on every supported path, but a
            # backfill must never be the reason a migration cannot finish.
            logger.warning("v118: could not backfill agent_created from agents", exc_info=True)
    conn.execute("UPDATE schema_version SET version = 118")


def _v118_to_v119(conn: duckdb.DuckDBPyConnection) -> None:
    """v118→v119: add ``tool_registry.projection_map``.

    The linked-apps projection asked a hardcoded alias list which materialized
    column carried an app's id (``id``/``app_id``/``config_id``) and which its
    URL. That list was written against one upstream; the next MCP server names
    its columns differently and every row is silently skipped — the projection
    reports "0 new, 0 updated", which reads as "the upstream has nothing"
    rather than "nothing here is named what I expected". Live example: a
    Keboola data-app lister emits ``data_app_id`` + ``configuration_id``, so
    all six rows were dropped while the wizard said the fetch succeeded.

    The mapping is per-tool because it describes THAT tool's output shape, and
    it belongs to the admin who designated the tool as a lister. NULL keeps the
    old alias behaviour, so no instance changes until someone chooses.

    Idempotent: ``PRAGMA table_info`` skips the ALTER when the column already
    exists, matching the neighbouring steps.

    ``tool_registry`` may not exist yet when the ladder is replayed from an old
    stamp — a database created at v68 or v73 (as the chat-migration tests do)
    reaches this step before the CREATE that introduces the table. `PRAGMA
    table_info` raises rather than returning empty for a missing table, so the
    existence check comes first and the step degrades to a no-op; the version
    is still stamped, because a step that declined to run has still been
    applied and skipping the stamp would stall the ladder here forever.
    """
    table_exists = conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'tool_registry'").fetchone()
    if table_exists:
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info('tool_registry')").fetchall()}
        if "projection_map" not in existing_cols:
            conn.execute("ALTER TABLE tool_registry ADD COLUMN projection_map JSON")
    conn.execute("UPDATE schema_version SET version = 119")


def _v119_to_v120(conn: duckdb.DuckDBPyConnection) -> None:
    """v119→v120: ``agent_schedules`` — scheduled runs for agent profiles
    (design doc docs/superpowers/specs/2026-08-17-agent-schedules-design.md).

    No secondary indexes (ART-index incident — see _v94_to_v95)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_schedules (
            id          VARCHAR PRIMARY KEY,
            agent_id    VARCHAR NOT NULL,
            name        VARCHAR NOT NULL,
            schedule    VARCHAR NOT NULL,
            prompt      TEXT NOT NULL,
            enabled     BOOLEAN NOT NULL DEFAULT TRUE,
            last_run_at TIMESTAMP,
            last_status VARCHAR,
            last_job_id VARCHAR,
            created_at  TIMESTAMP DEFAULT current_timestamp,
            updated_at  TIMESTAMP DEFAULT current_timestamp,
            UNIQUE (agent_id, name)
        )
    """)
    conn.execute("UPDATE schema_version SET version = 120")


def _v120_to_v121(conn: duckdb.DuckDBPyConnection) -> None:
    """v120→v121: add ``tool_grants.allow_mutating``.

    ``check_mutating`` (``app/api/mcp_policy.py``) was admin-or-bust: a
    ``mutating=True`` passthrough tool was uninvokable by any non-admin,
    including every agent profile (``AgentPrincipal.is_admin`` is pinned
    False by design). The policy module reserved this exact evolution — "a
    separate ``mutating_grant`` row" — and this column is it: a grant row
    with ``allow_mutating=TRUE`` lets members of that group (and agents
    whose owner is a member, still narrowed by connection scope) invoke the
    tool. Default FALSE keeps every existing grant read-only, so behavior
    is unchanged until an admin opts a group in per tool.

    ``tool_grants`` may not exist yet when the ladder replays from a stamp
    older than v64 — same guard-then-stamp pattern as ``_v118_to_v119``.

    No NOT NULL on the ALTER: DuckDB rejects ADD COLUMN with constraints
    ("Adding columns with constraints not yet supported"); DEFAULT FALSE
    backfills existing rows, and the repo layer treats NULL as FALSE so the
    two backends cannot drift on a tri-state.

    (Renumbered from v119→v120 when the agent_schedules migration merged
    first with that number.)
    """
    table_exists = conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'tool_grants'").fetchone()
    if table_exists:
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info('tool_grants')").fetchall()}
        if "allow_mutating" not in existing_cols:
            conn.execute("ALTER TABLE tool_grants ADD COLUMN allow_mutating BOOLEAN DEFAULT FALSE")
    conn.execute("UPDATE schema_version SET version = 121")


def _v121_to_v122(conn: duckdb.DuckDBPyConnection) -> None:
    """v121→v122: make pre-existing builder agents enforce what their UI
    already showed.

    A ``/agents`` builder row (``agt_`` id prefix — see ``_v114_to_v115`` for
    why the prefix, not the slug, is the discriminator) recorded the user's
    picks in the ``knowledge``/``plugins`` JSON columns but left all four
    ``*_mode`` columns at the repository default ``'all'``. All-``'all'`` is
    the passthrough shape (``agent_is_passthrough``), so such an agent ran
    with its owner's ENTIRE stack regardless of what the page showed — the
    UI promised a narrowing the runtime never applied. ``POST``/``PATCH``
    ``/api/agents`` now derive ``agent_scope`` from those columns and set the
    modes to ``'selected'``; this step does the same for rows created before
    that fix.

    Two things happen per row, in one transaction:

    1. ``knowledge`` ids become ``agent_scope`` rows, typed by which registry
       the id resolves in (data package / memory domain / collection), and
       ``plugins`` ids become ``('plugin', id)`` rows. Ids that resolve
       nowhere are skipped: an enforced-scope row that can never resolve is
       indistinguishable from a typo and would only widen the diff an
       operator has to audit.
    2. The four modes flip to ``'selected'``.

    A row with an empty declaration therefore ends up enforcing an EMPTY
    scope. That is the honest reading of a builder agent showing
    "0 sources · 0 tools", and it is the fail-closed direction; the owner
    widens it by picking sources in the builder, which now writes scope.

    Excluded: ``is_default`` (the seeded per-owner agent web chat is
    attributed to — it is infrastructure and must keep passing the owner's
    own authority through), and any row whose modes are already not all
    ``'all'`` (a governance-API agent, or a builder agent already fixed by
    ``agnes agent scope set``) — touching those would overwrite a
    deliberately-set scope with a re-derivation from columns the governance
    surface never wrote.

    Idempotent: after the flip the ``all four modes = 'all'`` predicate no
    longer matches, so a re-run (or a fresh install's ladder walk) is a
    no-op. Guarded on the modern ``agents`` shape for the same reason
    ``_v114_to_v115`` is — a database still in the pre-merge paper-theme
    shape reaches this step before ``_heal_legacy_agents_table`` runs, and
    an unguarded statement would abort startup with a Binder Error.

    Wrapped in an explicit transaction, like the other multi-row data
    backfills (``_v12_to_v13_finalize``, ``_v13_to_v14_finalize``): DuckDB
    autocommits per statement, so without it a crash mid-loop would leave
    some agents flipped and others not, while the Postgres sibling — whose
    whole Alembic run is one transaction — would roll back. The step is
    retry-safe either way (an unflipped agent still matches the cohort
    predicate next boot), but the two backends should not differ in their
    crash-window guarantee.
    """
    import json as _json

    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'agents'"
        ).fetchall()
    }
    required = {"knowledge", "plugins", "is_default", "tables_mode", "plugins_mode", "memory_mode", "connections_mode"}
    tables_present = {r[0] for r in conn.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    # ``agent_scope`` mirrors the Alembic guard's table check. Inert today
    # (_SYSTEM_SCHEMA creates it unconditionally before this step runs), but
    # without it a reordering there would crash DuckDB where PG no-ops.
    if required <= cols and "agent_scope" in tables_present:
        rows = conn.execute(r"""
            SELECT id, knowledge, plugins
              FROM agents
             WHERE id LIKE 'agt\_%' ESCAPE '\'
               AND NOT COALESCE(is_default, FALSE)
               AND COALESCE(tables_mode, 'all') = 'all'
               AND COALESCE(plugins_mode, 'all') = 'all'
               AND COALESCE(connections_mode, 'all') = 'all'
               AND COALESCE(memory_mode, 'all') = 'all'
        """).fetchall()

        def _ids(raw) -> list:
            """The JSON id-list column as a clean list of non-blank strings."""
            try:
                val = _json.loads(raw) if isinstance(raw, str) else (raw or [])
            except (ValueError, TypeError):
                return []
            return [v.strip() for v in val if isinstance(v, str) and v.strip()] if isinstance(val, list) else []

        # Registries a knowledge id may resolve in, probed in this order —
        # the same three the builder's Knowledge section is populated from.
        registries = [
            (item_type, table)
            for item_type, table in (
                ("data_package", "data_packages"),
                ("memory_domain", "memory_domains"),
                ("collection", "file_corpora"),
            )
            if table in tables_present
        ]
        conn.execute("BEGIN TRANSACTION")
        try:
            _backfill_builder_scope_rows(conn, rows, registries, _ids)
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    conn.execute("UPDATE schema_version SET version = 122")


def _v122_to_v123(conn: duckdb.DuckDBPyConnection) -> None:
    """v122→v123: ``chat_messages.parts`` — the assistant turn's ordered shape.

    A turn is prose → tool → prose, and the row recorded ``content`` (one
    flattened string) plus ``tool_calls`` (a positionless list), so the
    interleaving existed only in the live frame order and was gone by the time
    anything read the row back. A reloaded conversation therefore showed every
    tool block appended under the whole answer, and a replayed tool card could
    show only a name — no outcome, no result — because the row evidenced
    neither (#1504).

    ``parts`` stores the sequence instead: ``[{type:'text'|'tool', …}]``, with
    a tool entry carrying its own ``state``/``result``/``is_error``. See
    ``app/chat/message_parts.py`` for the shape and why it mirrors the one
    ``apps/kai-agent`` persists.

    Additive and nullable, with NO backfill: the ordering a historical row
    lost cannot be recovered from ``content`` + ``tool_calls`` — the positions
    are simply not in the data, and guessing them would put tool cards in
    places they never ran. Pre-v123 rows keep rendering the old way (text,
    then the calls after it) via the ``tool_calls`` fallback the client
    retains; new turns get the real shape.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info('chat_messages')").fetchall()}
    if "parts" not in cols:
        conn.execute("ALTER TABLE chat_messages ADD COLUMN parts JSON")
    conn.execute("UPDATE schema_version SET version = 123")


def _v123_to_v124(conn: duckdb.DuckDBPyConnection) -> None:
    """v123→v124 (remediation B1): backfill name-keyed ``sync_state.table_id``
    / ``sync_history.table_id`` rows to the matching ``table_registry.id``.

    Every writer wrote these keyed by the table's *name* (`_meta.table_name`
    for connector syncs, `table_registry.name` for the materialized pass)
    while several admin-status readers (`/api/admin/registry`, the
    data-sources pipeline strip, the Tables lens' delivery map) joined
    `sync_state` against `table_registry` on `id`. The two agree only when a
    table's registry id happens to equal its display name (the common
    case); a table registered with a display name that isn't already a
    valid identifier (spaces, uppercase — e.g. `name="Web Sessions"`, id
    `web_sessions`) showed healthy sync status on one admin surface and
    "never synced" on another, from the exact same sync. The writers now
    resolve the id themselves going forward (`src.sync_state_key`); this
    step is the one-time catch-up for rows an earlier binary already wrote.

    Data-only — no column or table change, so `src/db_pg.py` needs no
    matching edit; the Alembic sibling (`0072_sync_state_id_backfill_v124`)
    does the same rewrite against Postgres.

    A `sync_state.table_id` value that:
      - matches NO `table_registry.name` is left unchanged (an unregistered
        or since-renamed table) — logged, never dropped, matching the
        writers' own fallback behavior;
      - matches a `table_registry.name` whose `id` a row ALREADY under
        (i.e. `id == name`, or a stray duplicate) is a no-op — nothing to
        rewrite;
      - would collide with a row that ALREADY exists under the target id
        (a pathological pre-existing state — two rows that would both want
        `table_id = <that id>`) is left unchanged and logged rather than
        silently dropping one row's history; `sync_state.table_id` is a
        PRIMARY KEY, so blindly renaming into an existing key would raise.

    `sync_history.table_id` shares the exact same keying convention (every
    `sync_state.update_sync()` call inserts both rows under the identical
    key — see `src.repositories.sync_state.SyncStateRepository.update_sync`)
    but carries no uniqueness constraint, so its rewrite has no collision
    case to guard.

    Idempotent: a re-run finds no more name-keyed rows to touch (they were
    already renamed, or never had a registry match and are unchanged either
    way).

    Column-defensive: a DB replaying the ladder from far enough back can
    have `sync_state` / `table_registry` / `sync_history` present as bare
    stub tables (e.g. a pre-`_SYSTEM_SCHEMA` install, or a test harness
    that only stubs `CREATE TABLE IF NOT EXISTS <name> (id VARCHAR PRIMARY
    KEY)` for tables it doesn't otherwise exercise) — `IF NOT EXISTS` in
    `_SYSTEM_SCHEMA`'s own `CREATE TABLE` means that stub survives
    untouched all the way to here, since no earlier `_vN_to_v(N+1)` step
    ever needed to reshape `sync_state`'s columns before this one. No
    other migration step queries these tables' columns directly (every
    other consumer goes through the repo layer at RUNTIME, not during the
    ladder walk), so this is the first step a shape gap like that would
    ever surface for. `PRAGMA table_info` is checked before any SELECT
    that names a specific column; a table missing what this step expects
    has nothing to backfill (no real sync_state row can exist keyed by a
    column that doesn't exist) and is skipped with a log line rather than
    raising a Binder Error.
    """
    tables_present = {r[0] for r in conn.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    if "sync_state" not in tables_present or "table_registry" not in tables_present:
        conn.execute("UPDATE schema_version SET version = 124")
        return

    def _cols(table_name: str) -> set:
        return {row[1] for row in conn.execute(f"PRAGMA table_info('{table_name}')").fetchall()}

    sync_state_cols = _cols("sync_state")
    registry_cols = _cols("table_registry")
    if "table_id" not in sync_state_cols or "id" not in registry_cols or "name" not in registry_cols:
        logger.warning(
            "sync_state backfill (v124): skipped — sync_state and/or table_registry is missing an "
            "expected column at this point in the migration ladder (sync_state has %s, table_registry "
            "has %s); nothing to backfill on a table shaped like this",
            sorted(sync_state_cols),
            sorted(registry_cols),
        )
        conn.execute("UPDATE schema_version SET version = 124")
        return

    sync_history_has_table_id = "sync_history" in tables_present and "table_id" in _cols("sync_history")
    if "sync_history" in tables_present and not sync_history_has_table_id:
        logger.warning(
            "sync_state backfill (v124): sync_history is missing the table_id column at this point "
            "in the migration ladder — its rows are left untouched"
        )

    name_to_id: dict[str, str] = {}
    for rid, name in conn.execute("SELECT id, name FROM table_registry").fetchall():
        if name:
            name_to_id[name] = rid

    existing_ids = {row[0] for row in conn.execute("SELECT table_id FROM sync_state").fetchall()}

    for (table_id,) in conn.execute("SELECT table_id FROM sync_state").fetchall():
        new_id = name_to_id.get(table_id)
        if not new_id or new_id == table_id:
            # No registry match (left unchanged — a writer already logged
            # this at write time), or already id-keyed (id == name, or a
            # prior run of this same step).
            continue
        if new_id in existing_ids:
            logger.warning(
                "sync_state backfill (v124): leaving %r name-keyed — a row already exists under the target id %r",
                table_id,
                new_id,
            )
            continue
        conn.execute("UPDATE sync_state SET table_id = ? WHERE table_id = ?", [new_id, table_id])
        if sync_history_has_table_id:
            conn.execute("UPDATE sync_history SET table_id = ? WHERE table_id = ?", [new_id, table_id])
        existing_ids.discard(table_id)
        existing_ids.add(new_id)

    conn.execute("UPDATE schema_version SET version = 124")


def _backfill_builder_scope_rows(conn, rows, registries, _ids) -> None:
    """The per-agent write half of :func:`_v121_to_v122`, extracted so the
    transaction wrapper there reads as one unit."""
    for agent_id, knowledge_json, plugins_json in rows:
        pairs: list = []
        for item_id in _ids(knowledge_json):
            for item_type, table in registries:
                if conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", [item_id]).fetchone():
                    pairs.append((item_type, item_id))
                    break
        pairs += [("plugin", p) for p in _ids(plugins_json)]
        for item_type, item_id in pairs:
            try:
                conn.execute(
                    "INSERT INTO agent_scope (agent_id, item_type, item_id) VALUES (?, ?, ?)",
                    [agent_id, item_type, item_id],
                )
            except duckdb.ConstraintException:
                pass  # already scoped — the composite PK, not an error
        conn.execute(
            """UPDATE agents
                  SET tables_mode = 'selected', plugins_mode = 'selected',
                      connections_mode = 'selected', memory_mode = 'selected',
                      updated_at = current_timestamp
                WHERE id = ?""",
            [agent_id],
        )


def _add_store_entity_trust_columns(conn: duckdb.DuckDBPyConnection) -> None:
    """The v111 column DDL on its own, with no version stamp.

    Shared by ``_v110_to_v111`` (the ladder step) and
    :func:`_heal_store_entity_trust_columns` (the stamp-independent repair).
    Keeping the stamp out of here matters: a heal that called the versioned step
    directly would write ``version = 111`` and silently DOWNGRADE the stamp on any
    instance already past 111.
    """
    exists = conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'store_entities'").fetchone()
    if not exists:
        return
    conn.execute("ALTER TABLE store_entities ADD COLUMN IF NOT EXISTS publisher_kind VARCHAR DEFAULT 'user'")
    conn.execute("ALTER TABLE store_entities ADD COLUMN IF NOT EXISTS verification_state VARCHAR DEFAULT 'none'")
    conn.execute("ALTER TABLE store_entities ADD COLUMN IF NOT EXISTS verified_at TIMESTAMP")
    conn.execute("ALTER TABLE store_entities ADD COLUMN IF NOT EXISTS verified_by VARCHAR")
    conn.execute("ALTER TABLE store_entities ADD COLUMN IF NOT EXISTS verification_note TEXT")
    # Backfill deliberately, even though it looks redundant. DuckDB (measured
    # on 1.5.2) DOES apply an ADD COLUMN default to pre-existing rows, so these
    # UPDATEs are no-ops today — this comment used to claim the opposite, and a
    # wrong belief about it is what makes the next heal author guess. Keeping
    # them costs one no-op statement and removes the guess: a column added with
    # no DEFAULT still reads NULL, and nothing here should depend on which
    # engine version normalises what (Devin Review on #1158).
    conn.execute("UPDATE store_entities SET publisher_kind = 'user' WHERE publisher_kind IS NULL")
    conn.execute("UPDATE store_entities SET verification_state = 'none' WHERE verification_state IS NULL")


def _heal_store_entity_trust_columns(conn: duckdb.DuckDBPyConnection) -> None:
    """Ensure ``store_entities``' v111 trust columns exist, whatever the stamp says.

    ``schema_version`` is not sufficient evidence that a versioned step ran. The
    ladder's tail stamps ``SCHEMA_VERSION`` unconditionally, so a database opened
    by a build where ``SCHEMA_VERSION`` had already been bumped but the matching
    ``_v110_to_v111`` step did not yet exist gets marked past 111 **without** the
    columns — and is then skipped forever, because ``current < 111`` is false. It
    then fails at query time with ``Binder Error: Referenced column
    "publisher_kind" not found``, but only on the code paths that name the column
    in a WHERE (a bare ``SELECT *`` keeps working, which is what makes it look
    intermittent).

    Deployed instances cannot hit this — constant and step ship in the same
    commit — but development and multi-worktree checkouts can, and the same class
    of stranding already required two heal steps (``_v99_to_v100``,
    ``_v100_to_v101``). Rather than add a third version bump, the presence of the
    columns is checked directly: cheap (one ``information_schema`` read per
    boot), authoritative, and immune to a wrong stamp in either direction.
    """
    exists = conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'store_entities'").fetchone()
    if not exists:
        return
    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'store_entities'"
        ).fetchall()
    }
    if "publisher_kind" in cols and "verification_state" in cols:
        return
    logger.warning(
        "store_entities is missing its v111 trust columns despite schema_version "
        "— healing (see _heal_store_entity_trust_columns)"
    )
    _add_store_entity_trust_columns(conn)


def _heal_stranded_ladder_columns(conn: duckdb.DuckDBPyConnection) -> None:
    """Add back the plain columns the paper-theme renumbering stranded.

    :func:`_heal_legacy_agents_table` repairs the ``agents`` *table* for a DB
    that reached the merge already stamped past 101 — but ``_v100_to_v101`` also
    adds two ``agent_id`` columns, and the same skip drops those on the floor.
    ``chat_sessions.agent_id`` is the one that hurts: every chat read and write
    names it, so the whole surface 500s with ``Binder Error: Table "s" does not
    have a column named "agent_id"`` while the instance otherwise looks healthy
    (``/api/health`` reports ``db_schema: ok`` — the stamp *is* at the head).

    The renumbering shifted more than one step underneath these DBs, so the same
    treatment is owed to every plain-column addition in the affected range:
    ``sync_state.parts`` (v100) and the ``data_apps`` draft (v99) + linked (v108)
    columns. Structural steps are already covered by the sibling heals; these are
    the ones that are pure ``ADD COLUMN``.

    Stamp-independent and idempotent, for the reasons in
    :func:`_heal_store_entity_trust_columns` — the stamp is exactly the evidence
    that is wrong here. Unlike ``DROP COLUMN``, DuckDB has no problem adding a
    column to a table carrying indexes or inbound foreign keys, so no dependent
    parking is needed.
    """
    # (table, column, DDL) — verbatim from the ladder steps that own them, so a
    # healed DB is indistinguishable from a cleanly-migrated one.
    stranded = [
        ("chat_sessions", "agent_id", "VARCHAR"),  # _v100_to_v101
        ("personal_access_tokens", "agent_id", "VARCHAR"),  # _v100_to_v101
        ("sync_state", "parts", "JSON"),  # _v99_to_v100
        ("data_apps", "parent_app_id", "VARCHAR DEFAULT ''"),  # _v98_to_v99
        ("data_apps", "is_draft", "BOOLEAN DEFAULT FALSE"),  # _v98_to_v99
        ("data_apps", "draft_branch", "VARCHAR DEFAULT ''"),  # _v98_to_v99
        ("data_apps", "external_url", "VARCHAR"),  # _v107_to_v108
        ("data_apps", "source_ref", "VARCHAR"),  # _v107_to_v108
        ("data_apps", "managed", "BOOLEAN DEFAULT FALSE"),  # _v107_to_v108
        ("data_apps", "description_override", "TEXT"),  # _v107_to_v108
        # _v105_to_v106. Without it every PAT mint fails — including the CLI
        # sign-in exchange — on exactly the databases this heal repairs, so
        # leaving it out would fix chat and leave the operator locked out of
        # the CLI (Devin Review on #1158).
        ("personal_access_tokens", "surface", "VARCHAR DEFAULT 'all'"),
        ("usage_session_summary", "uploaded_at", "TIMESTAMP"),  # _v104_to_v105
        ("file_corpora", "origin", "VARCHAR DEFAULT 'uploaded'"),  # _v109_to_v110
        ("chat_sessions", "pinned_at", "TIMESTAMP"),  # _v112_to_v113
        # _v97_to_v98's relay half. The paper-theme branch REPLACED that step's
        # body with the journey table, so a database that climbed the branch
        # ladder past 98 is stamped at the head yet missing main's column —
        # and every chat spawn/resume names it via set_sandbox_ref.
        ("chat_sessions", "relay_protocol_version", "INTEGER"),  # _v97_to_v98
    ]
    # The seven agents.* columns those steps also add are NOT listed here:
    # _heal_legacy_agents_table rebuilds that table from the canonical DDL,
    # which carries them. tests/test_db_schema_version.py derives this
    # comparison from the ladder and fails if a future step adds a column that
    # neither heal covers — this list drifted twice before that guard existed
    # (Devin Review on #1158).

    present: dict[str, set[str]] = {}
    for table, column, ddl in stranded:
        if table not in present:
            exists = conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name = ?", [table]).fetchone()
            present[table] = (
                {
                    r[0]
                    for r in conn.execute(
                        "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
                        [table],
                    ).fetchall()
                }
                if exists
                else set()
            )
        # Empty set means the table itself is absent — nothing to heal, and the
        # fresh-install path will declare the column from _SYSTEM_SCHEMA anyway.
        if not present[table] or column in present[table]:
            continue
        logger.warning(
            "%s is missing %s despite schema_version — healing (see _heal_stranded_ladder_columns)",
            table,
            column,
        )
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        present[table].add(column)

    # `managed` is NOT NULL in the fresh-install DDL, but DuckDB cannot ADD
    # COLUMN with a constraint, so the ladder normalizes instead — same as
    # _v107_to_v108. Rows predating the column read NULL, not the DEFAULT.
    if "managed" in present.get("data_apps", set()):
        conn.execute("UPDATE data_apps SET managed = FALSE WHERE managed IS NULL")


def _heal_legacy_agents_table(conn: duckdb.DuckDBPyConnection) -> None:
    """Rebuild a pre-merge paper-theme ``agents`` table into main's canonical shape.

    Stamp-independent, for the same reason as
    :func:`_heal_store_entity_trust_columns`: the damage is invisible to
    ``schema_version``. Before the two agent features were combined (PR #1113)
    the paper-theme branch owned its own ``agents`` table — ``created_by`` /
    ``instructions`` / globally-UNIQUE ``slug``, created at *its* v103. A
    database built by that branch therefore reaches the merge already stamped
    past 101, so ``_v100_to_v101`` (whose ``CREATE TABLE IF NOT EXISTS`` would
    otherwise have laid down main's shape) never runs, and ``_v109_to_v110``
    finds every superset column already present and adds nothing. The table
    stays in the old shape forever while the repository layer
    (``src/repositories/agents.py``) writes the canonical one — so every
    ``POST /api/agents`` dies with ``Binder Error: Table "agents" does not have
    a column with name "owner_user_id"`` and the /agents builder's "Build an
    agent" button 500s.

    No deployed instance can be in this state (main never shipped the old
    shape), but every checkout that ran the paper-theme branch before the merge
    is — which is what makes this a heal rather than a ladder step.

    The rebuild is a rename → recreate → copy → drop, not a column shuffle:
    ``created_by`` → ``owner_user_id`` and ``instructions`` → ``system_prompt``
    are the mappings the repository already assumes, the columns main adds
    (description/model/token_budget_monthly/…) take their declared defaults, and
    the old global ``UNIQUE (slug)`` is strictly stronger than the new
    ``UNIQUE (owner_user_id, slug)`` so no row can collide on the way in.
    """
    exists = conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'agents'").fetchone()
    if not exists:
        return
    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'agents'"
        ).fetchall()
    }
    # Canonical already (the overwhelmingly common case) — or something else
    # entirely, which this heal must not touch.
    if "owner_user_id" in cols or "created_by" not in cols:
        return
    logger.warning(
        "agents is in the pre-merge paper-theme shape despite schema_version "
        "— rebuilding onto the canonical shape (see _heal_legacy_agents_table)"
    )
    conn.execute("DROP TABLE IF EXISTS agents_legacy_heal")
    conn.execute("ALTER TABLE agents RENAME TO agents_legacy_heal")
    conn.execute(_AGENTS_CREATE_SQL)
    conn.execute("""
        INSERT INTO agents
        (id, owner_user_id, name, slug, system_prompt,
         role, tone, greeting, knowledge, plugins, surfaces, status,
         created_at, updated_at, deleted_at)
        SELECT id, created_by, name, slug, instructions,
               role, tone, greeting, knowledge, plugins, surfaces, status,
               created_at, updated_at, deleted_at
        FROM agents_legacy_heal
    """)
    conn.execute("DROP TABLE agents_legacy_heal")


def _v98_to_v99(conn: duckdb.DuckDBPyConnection) -> None:
    """v98→v99: data_apps draft model — parent_app_id, is_draft, draft_branch."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info('data_apps')").fetchall()}
    if "parent_app_id" not in cols:
        conn.execute("ALTER TABLE data_apps ADD COLUMN parent_app_id VARCHAR DEFAULT ''")
    if "is_draft" not in cols:
        conn.execute("ALTER TABLE data_apps ADD COLUMN is_draft BOOLEAN DEFAULT FALSE")
    if "draft_branch" not in cols:
        conn.execute("ALTER TABLE data_apps ADD COLUMN draft_branch VARCHAR DEFAULT ''")
    conn.execute("UPDATE schema_version SET version = 99")


def _v99_to_v100(conn: duckdb.DuckDBPyConnection) -> None:
    """v99→v100: sync_state.parts — per-partition manifest for partitioned
    tables (partitioned distribution). Holds a JSON list of
    ``{path, hash, size_bytes}`` per part; NULL means a single-file table
    (backward compatible — the manifest/pull treat NULL as single-file)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info('sync_state')").fetchall()}
    if "parts" not in cols:
        conn.execute("ALTER TABLE sync_state ADD COLUMN parts JSON")
    conn.execute("UPDATE schema_version SET version = 100")


def _v100_to_v101(conn: duckdb.DuckDBPyConnection) -> None:
    """v100→v101: agent profiles + agent-as-API foundation (spec
    docs/superpowers/specs/2026-07-21-agent-profiles-and-agent-api-design.md).
    No secondary indexes anywhere here — see the _v94_to_v95 ART-index
    incident note; chat_sessions.agent_id especially must stay unindexed."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agents (
            id                   VARCHAR PRIMARY KEY,
            owner_user_id        VARCHAR NOT NULL,
            name                 VARCHAR NOT NULL,
            slug                 VARCHAR NOT NULL,
            description          TEXT,
            system_prompt        TEXT,
            model                VARCHAR,
            token_budget_monthly BIGINT,
            plugins_mode         VARCHAR NOT NULL DEFAULT 'all',
            connections_mode     VARCHAR NOT NULL DEFAULT 'all',
            tables_mode          VARCHAR NOT NULL DEFAULT 'all',
            memory_mode          VARCHAR NOT NULL DEFAULT 'all',
            memory_write_mode    VARCHAR NOT NULL DEFAULT 'propose',
            is_default           BOOLEAN NOT NULL DEFAULT FALSE,
            created_at           TIMESTAMP DEFAULT current_timestamp,
            updated_at           TIMESTAMP DEFAULT current_timestamp,
            deleted_at           TIMESTAMP,
            UNIQUE (owner_user_id, slug)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_scope (
            agent_id  VARCHAR NOT NULL,
            item_type VARCHAR NOT NULL,
            item_id   VARCHAR NOT NULL,
            PRIMARY KEY (agent_id, item_type, item_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS llm_usage (
            id                    VARCHAR PRIMARY KEY,
            agent_id              VARCHAR,
            user_id               VARCHAR,
            session_id            VARCHAR,
            model                 VARCHAR,
            input_tokens          BIGINT DEFAULT 0,
            output_tokens         BIGINT DEFAULT 0,
            cache_read_tokens     BIGINT DEFAULT 0,
            cache_creation_tokens BIGINT DEFAULT 0,
            created_at            TIMESTAMP DEFAULT current_timestamp
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_scope_snapshots (
            id              VARCHAR PRIMARY KEY,
            session_id      VARCHAR NOT NULL,
            agent_id        VARCHAR NOT NULL,
            effective_scope TEXT NOT NULL,
            created_at      TIMESTAMP DEFAULT current_timestamp
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS idempotency_keys (
            key           VARCHAR NOT NULL,
            owner_user_id VARCHAR NOT NULL,
            agent_id      VARCHAR NOT NULL,
            request_hash  VARCHAR NOT NULL,
            response_body TEXT,
            status_code   INTEGER,
            created_at    TIMESTAMP DEFAULT current_timestamp,
            expires_at    TIMESTAMP,
            PRIMARY KEY (key, owner_user_id, agent_id)
        )
    """)
    conn.execute("ALTER TABLE personal_access_tokens ADD COLUMN IF NOT EXISTS agent_id VARCHAR")
    conn.execute("ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS agent_id VARCHAR")
    conn.execute("UPDATE schema_version SET version = 101")


def _v101_to_v102(conn: duckdb.DuckDBPyConnection) -> None:
    """v101→v102: agent webhooks + artifacts (agent-api V1b). No secondary
    indexes (ART-index incident — see _v94_to_v95)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_webhooks (
            id                   VARCHAR PRIMARY KEY,
            agent_id             VARCHAR NOT NULL,
            owner_user_id        VARCHAR NOT NULL,
            url                  VARCHAR NOT NULL,
            secret               VARCHAR NOT NULL,
            events               VARCHAR NOT NULL DEFAULT 'job.completed,job.failed',
            active               BOOLEAN NOT NULL DEFAULT TRUE,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            disabled_at          TIMESTAMP,
            created_at           TIMESTAMP DEFAULT current_timestamp,
            updated_at           TIMESTAMP DEFAULT current_timestamp
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_artifacts (
            id            VARCHAR PRIMARY KEY,
            session_id    VARCHAR NOT NULL,
            agent_id      VARCHAR,
            owner_user_id VARCHAR NOT NULL,
            filename      VARCHAR NOT NULL,
            object_key    VARCHAR NOT NULL,
            size_bytes    BIGINT NOT NULL DEFAULT 0,
            content_type  VARCHAR,
            md5           VARCHAR,
            created_at    TIMESTAMP DEFAULT current_timestamp
        )
    """)
    conn.execute("UPDATE schema_version SET version = 102")


def _v102_to_v103(conn: duckdb.DuckDBPyConnection) -> None:
    """v102→v103: per-agent private memory notebook (agent-api V1c). No
    secondary indexes (ART incident — see _v94_to_v95)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_memories (
            id                VARCHAR PRIMARY KEY,
            agent_id          VARCHAR NOT NULL,
            owner_user_id     VARCHAR NOT NULL,
            content           TEXT NOT NULL,
            source_session_id VARCHAR,
            status            VARCHAR NOT NULL DEFAULT 'pending',
            created_at        TIMESTAMP DEFAULT current_timestamp,
            activated_at      TIMESTAMP,
            archived_at       TIMESTAMP
        )
    """)
    conn.execute("UPDATE schema_version SET version = 103")


def _v103_to_v104(conn: duckdb.DuckDBPyConnection) -> None:
    """v103→v104: audit_log identity backfill — user_id values holding an
    email are rewritten to the matching users.id, but only when the email
    resolves to exactly one account (case-insensitive). Unresolvable or
    ambiguous emails stay as-is: a searchable email beats a dropped row.
    Fixes the Activity Center facet split where one person appeared as both
    an email row and a UUID row (chat/memory/authoring/slack writers).

    Guarded on ``users.email`` existing — legacy snapshots migrated through
    the whole ladder in one pass (and test fixtures) can reach this step
    with a minimal ``users`` shape; the backfill is then a no-op and the
    version still advances."""
    user_cols = {r[1] for r in conn.execute("PRAGMA table_info('users')").fetchall()}
    audit_cols = {r[1] for r in conn.execute("PRAGMA table_info('audit_log')").fetchall()}
    if "email" not in user_cols or "user_id" not in audit_cols:
        conn.execute("UPDATE schema_version SET version = 104")
        return
    conn.execute(
        """
        UPDATE audit_log SET user_id = (
            SELECT min(u.id) FROM users u
            WHERE lower(u.email) = lower(audit_log.user_id)
        )
        WHERE user_id LIKE '%@%'
          AND (
            SELECT COUNT(*) FROM users u
            WHERE lower(u.email) = lower(audit_log.user_id)
          ) = 1
        """
    )
    conn.execute("UPDATE schema_version SET version = 104")


def _v104_to_v105(conn: duckdb.DuckDBPyConnection) -> None:
    """v104→v105: ``usage_session_summary.uploaded_at`` — arrival anchor.

    Backfill: newest ``session.upload`` audit row whose params filename
    matches the summary's ``session_file`` basename (join on the FILE name,
    never on session_id — resumed/forked sessions carry a different
    content-derived id). Falls back to ``started_at``. No secondary index
    (v95 ART incident)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info('usage_session_summary')").fetchall()}
    if "uploaded_at" not in cols:
        conn.execute("ALTER TABLE usage_session_summary ADD COLUMN uploaded_at TIMESTAMP")
    audit_cols = {r[1] for r in conn.execute("PRAGMA table_info('audit_log')").fetchall()}
    if "params" in audit_cols:
        conn.execute(
            """
            UPDATE usage_session_summary SET uploaded_at = (
                SELECT max(a.timestamp) FROM audit_log a
                WHERE a.action = 'session.upload'
                  AND CAST(a.params AS VARCHAR) LIKE
                      '%' || substr(
                          usage_session_summary.session_file,
                          position('/' in usage_session_summary.session_file) + 1
                      ) || '%'
            ) WHERE uploaded_at IS NULL
            """
        )
    conn.execute("UPDATE usage_session_summary SET uploaded_at = COALESCE(uploaded_at, started_at, CURRENT_TIMESTAMP)")
    conn.execute("UPDATE schema_version SET version = 105")


def _v105_to_v106(conn: duckdb.DuckDBPyConnection) -> None:
    """v105→v106: ``personal_access_tokens.surface`` — credential data-read
    surface ('all' | 'stack').

    Schema-level DEFAULT 'all' backfills every existing row in the same
    ALTER, so legacy PATs keep today's behavior (admin god-mode) without an
    app-level NULL sentinel. New mint sites pass 'stack' explicitly where
    the analyst default applies (cli_auth exchange, cowork bundle,
    mcp_connect). Enforcement: src/rbac.py reads it via
    ``user["credential_surface"]`` stashed by ``resolve_token_to_user``.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info('personal_access_tokens')").fetchall()}
    if "surface" not in cols:
        conn.execute("ALTER TABLE personal_access_tokens ADD COLUMN surface VARCHAR DEFAULT 'all'")
    conn.execute("UPDATE personal_access_tokens SET surface = 'all' WHERE surface IS NULL")
    conn.execute("UPDATE schema_version SET version = 106")


def _v106_to_v107(conn: duckdb.DuckDBPyConnection) -> None:
    """v106→v107: nullable ``source_ref`` on metric_definitions +
    glossary_terms — per-connection provenance for the multi-project
    semantic-layer sync (2026-07-28 spec). No-op on fresh installs
    (snapshot DDL already declares the column).

    (Authored as v106 on the branch; renumbered to v107 after the PAT
    surface migration claimed v106 on main.)
    """
    for table in ("metric_definitions", "glossary_terms"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info('{table}')").fetchall()}
        if "source_ref" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN source_ref VARCHAR")
    conn.execute("UPDATE schema_version SET version = 107")


def _v107_to_v108(conn: duckdb.DuckDBPyConnection) -> None:
    """v107→v108: linked (externally-hosted) data apps. Adds ``external_url``,
    ``source_ref``, ``managed``, ``description_override`` to ``data_apps`` so a
    ``repo_mode='linked'`` row can point at an app hosted elsewhere (e.g. a
    Keboola-platform data app ingested via an MCP source) instead of a git repo.
    No-op on fresh installs (snapshot DDL already declares the columns).
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info('data_apps')").fetchall()}
    if "external_url" not in cols:
        conn.execute("ALTER TABLE data_apps ADD COLUMN external_url VARCHAR")
    if "source_ref" not in cols:
        conn.execute("ALTER TABLE data_apps ADD COLUMN source_ref VARCHAR")
    if "managed" not in cols:
        # DuckDB can't ADD COLUMN with a NOT NULL constraint ("Adding columns
        # with constraints not yet supported") — add with DEFAULT only and
        # backfill, same pattern as v106's `surface` column. Fresh installs
        # get the NOT NULL from the snapshot DDL; migrated DBs rely on the
        # DEFAULT + backfill (every writer passes an explicit value).
        conn.execute("ALTER TABLE data_apps ADD COLUMN managed BOOLEAN DEFAULT FALSE")
    conn.execute("UPDATE data_apps SET managed = FALSE WHERE managed IS NULL")
    if "description_override" not in cols:
        conn.execute("ALTER TABLE data_apps ADD COLUMN description_override TEXT")
    conn.execute("UPDATE schema_version SET version = 108")


def _v108_to_v109(conn: duckdb.DuckDBPyConnection) -> None:
    """v108→v109: outbound MCP OAuth data-layer foundation (2026-07-30 spec,
    PR 1 / phase 1 — no runtime behavior yet).

    Three new tables, all additive ``CREATE TABLE IF NOT EXISTS`` (no-op on
    fresh installs, safe on upgrade):

    - ``mcp_source_oauth_clients`` — one row per OAuth ``mcp_sources`` row:
      Agnes's own dynamic client registration (RFC 7591) at the upstream
      authorization server. Named distinctly from the inbound issuer's
      ``oauth_clients`` table (mirror-image concept, opposite direction).
    - ``mcp_user_oauth_tokens`` — per ``(source_id, user_id)`` access/refresh
      token pair. Kept separate from ``mcp_user_secrets``: different
      lifecycle (server-side refresh mutates rows) and deletion semantics
      (best-effort revoke-at-AS).
    - ``mcp_oauth_flows`` — in-flight authorize-flow state (PKCE verifier +
      nonce), DB-backed so multi-replica Postgres deployments need no sticky
      sessions.

    Every ``*_enc`` column is a Fernet ciphertext blob, same vault key as
    ``mcp_secrets``/``mcp_user_secrets`` (``app.secrets_vault``).
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mcp_source_oauth_clients (
            source_id                     VARCHAR PRIMARY KEY,
            issuer                        VARCHAR NOT NULL,
            client_id                     VARCHAR NOT NULL,
            client_secret_enc             BLOB,
            registration_access_token_enc BLOB,
            authorization_endpoint        VARCHAR NOT NULL,
            token_endpoint                VARCHAR NOT NULL,
            scopes                        VARCHAR,
            created_at                    TIMESTAMP NOT NULL DEFAULT current_timestamp,
            updated_at                    TIMESTAMP NOT NULL DEFAULT current_timestamp
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mcp_user_oauth_tokens (
            source_id         VARCHAR NOT NULL,
            user_id            VARCHAR NOT NULL,
            access_token_enc   BLOB NOT NULL,
            refresh_token_enc  BLOB,
            expires_at         TIMESTAMP,
            scopes             VARCHAR,
            created_at         TIMESTAMP NOT NULL DEFAULT current_timestamp,
            updated_at         TIMESTAMP NOT NULL DEFAULT current_timestamp,
            PRIMARY KEY (source_id, user_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mcp_oauth_flows (
            nonce              VARCHAR PRIMARY KEY,
            source_id          VARCHAR NOT NULL,
            user_id            VARCHAR NOT NULL,
            pkce_verifier_enc  BLOB NOT NULL,
            created_at         TIMESTAMP NOT NULL DEFAULT current_timestamp
        )
    """)
    conn.execute("UPDATE schema_version SET version = 109")


def _v57_to_v58(conn: duckdb.DuckDBPyConnection) -> None:
    """v55: ``memory_domain_suggestions`` table — non-admin "Suggest a
    domain" affordance + admin moderation queue.

    Idempotent CREATE TABLE IF NOT EXISTS. Fresh installs already get
    the table from ``_SYSTEM_SCHEMA``; this migration covers the
    sequential-upgrade path from a v54 instance.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_domain_suggestions (
            id                VARCHAR PRIMARY KEY,
            name              VARCHAR NOT NULL,
            description       TEXT,
            rationale         TEXT,
            status            VARCHAR DEFAULT 'pending',
            created_by        VARCHAR,
            created_at        TIMESTAMP DEFAULT current_timestamp,
            resolved_at       TIMESTAMP,
            resolved_by       VARCHAR,
            resolution_note   TEXT,
            created_domain_id VARCHAR
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_domain_suggestions_status ON memory_domain_suggestions(status)")
    conn.execute("UPDATE schema_version SET version = 58")


def _v56_to_v57(conn: duckdb.DuckDBPyConnection) -> None:
    """v54: soft-delete columns on data_packages / memory_domains / recipes.

    Powers the "Deleted. Undo (10s)" toast on admin pages — DELETE sets
    ``deleted_at`` instead of nuking the row, so the junction rows
    (data_package_tables, knowledge_item_domains) + any resource_grants
    referencing the resource id survive intact. The list/get endpoints
    filter ``deleted_at IS NULL`` so users never see soft-deleted rows.

    Idempotent ADD COLUMN IF NOT EXISTS.
    """
    for table in ("data_packages", "memory_domains", "recipes"):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP")
    conn.execute("UPDATE schema_version SET version = 57")


def _v55_to_v56(conn: duckdb.DuckDBPyConnection) -> None:
    """v53: ``recipes`` table — admin-curated query templates surfaced as
    a second tab on /catalog.

    Idempotent CREATE TABLE IF NOT EXISTS. Fresh installs already get
    the table from ``_SYSTEM_SCHEMA``; this migration covers the
    sequential-upgrade path from a v52 instance.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS recipes (
            id                VARCHAR PRIMARY KEY,
            slug              VARCHAR UNIQUE NOT NULL,
            title             VARCHAR NOT NULL,
            description       TEXT,
            icon              VARCHAR,
            color             VARCHAR,
            sql_template      TEXT,
            related_table_ids JSON,
            status            VARCHAR DEFAULT 'prod',
            created_by        VARCHAR,
            created_at        TIMESTAMP DEFAULT current_timestamp,
            updated_at        TIMESTAMP DEFAULT current_timestamp
        )
    """)
    conn.execute("UPDATE schema_version SET version = 56")


def _v54_to_v55(conn: duckdb.DuckDBPyConnection) -> None:
    """v52: per-table docs columns on table_registry.

    Adds three admin-authored fields read by the new /catalog/t/<id>
    detail page: sample_questions (JSON array of strings),
    things_to_know (freeform text), pairs_well_with (JSON array of
    table_registry ids). All optional / NULL on legacy rows.

    Idempotent ADD COLUMN IF NOT EXISTS; safe to re-run.
    """
    conn.execute("ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS sample_questions JSON")
    conn.execute("ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS things_to_know TEXT")
    conn.execute("ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS pairs_well_with JSON")
    conn.execute("UPDATE schema_version SET version = 55")


def _v53_to_v54(conn: duckdb.DuckDBPyConnection) -> None:
    """v51: lifecycle ``status`` + per-package ``category`` columns.

    Adds the surfaces the /catalog mockup audit identified as gaps:
    a small status pill on each card (driven by hero filter checkboxes)
    and an eyebrow line above the title (data_packages only — memory
    domains don't need a second-level category since the domain itself
    classifies its items).

    All ADD COLUMN IF NOT EXISTS — idempotent re-run is safe. The fresh
    install path picks the columns up from _SYSTEM_SCHEMA directly; this
    migration covers the sequential-upgrade path off an earlier version.
    """
    conn.execute("ALTER TABLE data_packages ADD COLUMN IF NOT EXISTS status VARCHAR DEFAULT 'prod'")
    conn.execute("ALTER TABLE data_packages ADD COLUMN IF NOT EXISTS category VARCHAR")
    conn.execute("ALTER TABLE memory_domains ADD COLUMN IF NOT EXISTS status VARCHAR DEFAULT 'prod'")
    conn.execute("UPDATE schema_version SET version = 54")


def _v52_to_v53(conn: duckdb.DuckDBPyConnection) -> None:
    """v50: ``cover_image_url`` on ``data_packages`` + ``memory_domains``.

    Closes the visual gap with /marketplace cards: marketplace items render
    real JPGs/PNGs from ``cover_photo_url`` while /catalog + /memory have
    been stuck with 2-letter initials. The upload endpoint at
    ``POST /api/admin/uploads/cover-image`` returns a relative URL that
    callers stash here; cards render ``<img>`` when set, fall back to the
    initials banner when NULL.

    Idempotent (``ADD COLUMN IF NOT EXISTS``) — re-running is safe. Bumps
    the version row locally so the fresh-install path (which calls every
    migration in sequence and relies on each step to stamp its own number
    — see _v51_to_v52 step 10) lands at 50 even if a future step in the
    same ladder fails before the outer driver gets to its UPDATE.
    """
    conn.execute("ALTER TABLE data_packages ADD COLUMN IF NOT EXISTS cover_image_url VARCHAR")
    conn.execute("ALTER TABLE memory_domains ADD COLUMN IF NOT EXISTS cover_image_url VARCHAR")
    conn.execute("UPDATE schema_version SET version = 53")


def _ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create tables if they don't exist. Apply migrations if schema version changed.

    Self-heal pass for split-brain DBs runs only when ``current >=
    SCHEMA_VERSION``. Scenario: a contributor's DB landed at
    ``schema_version=N`` from a partial migration (crash mid-DDL,
    parallel WIP branch with a different table set, etc.), but the
    on-disk file is missing tables this binary expects. Without this
    pass, the migration block below skips because we don't downgrade,
    and every runtime query against the missing table crashes.

    Because ``_SYSTEM_SCHEMA`` is all ``CREATE TABLE IF NOT EXISTS``,
    running it is idempotent: existing tables stay untouched (columns +
    data preserved), missing tables get created. Cost: dozens of no-op
    DDLs per process start.

    The self-heal explicitly does NOT run on the ``current <
    SCHEMA_VERSION`` path so the pre-migration snapshot taken inside
    that branch captures a true point-in-time state of the on-disk DB
    *before* any DDL runs — operators reading the snapshot for rollback
    debugging see exactly the tables the old schema had, not the
    binary's full table set with extras tacked on.
    """
    current = get_schema_version(conn)
    if current >= SCHEMA_VERSION:
        # Split-brain or same-version safety net: heal any tables this
        # binary expects that aren't on disk. Migration block skipped
        # because we don't downgrade — the version row is left at
        # ``current`` so a later binary that understands ``current``
        # picks up where the split-brain left off.
        conn.execute(_SYSTEM_SCHEMA)
    if current < SCHEMA_VERSION:
        # Snapshot before migration for rollback support
        if current > 0:
            try:
                db_path = _get_state_dir() / "system.duckdb"
                if db_path.exists():
                    # Flush WAL to main DB file before copying
                    try:
                        conn.execute("CHECKPOINT")
                    except Exception:
                        pass  # CHECKPOINT may fail on read-only or in-memory DBs
                    snapshot = db_path.parent / "system.duckdb.pre-migrate"
                    shutil.copy2(str(db_path), str(snapshot))
                    # Also copy WAL if it still exists (belt and suspenders)
                    wal_path = Path(str(db_path) + ".wal")
                    if wal_path.exists():
                        shutil.copy2(str(wal_path), str(snapshot) + ".wal")
                    logger.info("Pre-migration snapshot saved: %s", snapshot)
            except Exception as e:
                logger.warning("Could not create pre-migration snapshot: %s", e)
        conn.execute(_SYSTEM_SCHEMA)
        if current == 0:
            conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                [SCHEMA_VERSION],
            )
            # Row seeds are skipped when app-state lives in Postgres —
            # these tables are then read from PG (where the repos already
            # tolerate the rows' absence), and writing them into the local
            # DuckDB would leak state into the inactive backend (same
            # contract as the system-groups seed at the bottom of this
            # function).
            if not _state_backend_is_pg():
                # v22 setup_banner row (kept as compat per CLAUDE.md schema notes).
                conn.execute("INSERT INTO setup_banner (id, content) VALUES (1, NULL) ON CONFLICT (id) DO NOTHING")
                # v26 instance_templates seed — three canonical keys with NULL
                # content (operator override absent → render OSS default).
                for key in ("welcome", "claude_md", "home"):
                    conn.execute(
                        "INSERT INTO instance_templates (key, content) VALUES (?, NULL) ON CONFLICT (key) DO NOTHING",
                        [key],
                    )
            # v41 audit_log indices: _SYSTEM_SCHEMA omits CREATE INDEX to
            # avoid failures when pre-existing audit_log lacks timestamp
            # (migration tests). Create them here for fresh installs; the
            # upgrade path uses _v40_to_v41 below.
            _v40_to_v41(conn)
            # v42 usage_* tables + indices. _SYSTEM_SCHEMA already creates
            # them via IF NOT EXISTS, so this is a safe no-op for fresh
            # installs; mirrors the established pattern for the upgrade
            # path below.
            _v41_to_v42(conn)
            # v43 user_observability_views — saved-views for /admin/activity.
            _v42_to_v43(conn)
            # v44 homepage-stats columns. _SYSTEM_SCHEMA already declares
            # them on fresh installs (no-op ALTERs); kept here for the
            # ladder's chronological readability.
            _v43_to_v44(conn)
            # v45 user_id column on usage tables. _SYSTEM_SCHEMA declares
            # the columns for fresh installs; migration adds them for
            # existing DBs. No-op on fresh.
            _v44_to_v45(conn)
            # v46 knowledge_item_user_dismissed — per-user opt-out for
            # curated memory items. _SYSTEM_SCHEMA already creates the
            # table on fresh installs; this call is a no-op there.
            _v45_to_v46(conn)
            # v47 fts index over knowledge_items — best-effort, silent
            # fallback to ILIKE search if the fts extension can't load.
            _v46_to_v47(conn)
            # v48 marketplace telemetry refactor — drops 4 legacy tables
            # and creates 2 new rollups. _SYSTEM_SCHEMA already creates
            # the new tables on fresh installs; the DROPs are no-ops
            # there because the legacy tables aren't in _SYSTEM_SCHEMA
            # anymore. Kept here for ladder readability.
            _v47_to_v48(conn)
            # v49 phase-1 Flea refactor — title, tagline, synthetic_name
            # columns. _SYSTEM_SCHEMA already declares them on fresh
            # installs; this call is a no-op (table empty, ALTER IF NOT
            # EXISTS, no rows to backfill, SET NOT NULL idempotent).
            _v48_to_v49_migrate(conn)
            # v50 UNIQUE INDEX on synthetic_name. _SYSTEM_SCHEMA already
            # creates the index on fresh installs; this call is a no-op
            # (table empty so no duplicates possible, CREATE UNIQUE
            # INDEX IF NOT EXISTS is idempotent).
            _v49_to_v50_migrate(conn)
            # v49 unified stack — Data Packages + Memory Domains junction +
            # requirement enum + is_required + user_stack_subscriptions.
            # _SYSTEM_SCHEMA already creates the new tables on fresh
            # installs; the migration body is idempotent (CREATE TABLE
            # IF NOT EXISTS / ALTER ... ADD COLUMN IF NOT EXISTS), so
            # this call no-ops apart from seeding canonical
            # memory_domains and bumping the version row.
            _v51_to_v52(conn)
            # v50 cover_image_url on data_packages + memory_domains.
            # _SYSTEM_SCHEMA already includes the column on fresh installs;
            # the migration's IF NOT EXISTS ALTERs no-op there.
            _v52_to_v53(conn)
            # v51 status + category on data_packages, status on
            # memory_domains. Same fresh-install no-op pattern.
            _v53_to_v54(conn)
            # v52 per-table docs columns on table_registry.
            _v54_to_v55(conn)
            # v53 recipes table.
            _v55_to_v56(conn)
            # v54 deleted_at columns on data_packages, memory_domains, recipes.
            _v56_to_v57(conn)
            # v55 memory_domain_suggestions table.
            _v57_to_v58(conn)
            # v56 extended content columns on data_packages + structured
            # per-table doc columns on table_registry.
            _v58_to_v59(conn)
            # v59→v60 backfills ``username`` in usage_events /
            # usage_session_summary from users.email. Fresh installs
            # have empty usage_* tables, so the UPDATE is a no-op.
            _v59_to_v60(conn)
            # v60→v61 creates the ``cli_auth_codes`` table (browser-
            # loopback login exchange codes).
            _v60_to_v61(conn)
            # v61→v62 adds per-type FK columns on resource_grants (E.3,
            # PR #455). _SYSTEM_SCHEMA already declares the columns on
            # fresh installs (no-op ALTERs); the backfill UPDATE is a
            # no-op on empty rows.
            _v61_to_v62(conn)
            # v62→v63: setup_tokens table for Agnes Cowork one-click setup.
            _v62_to_v63(conn)
            # v63→v64: Universal MCP — mcp_sources, tool_registry, tool_grants.
            _v63_to_v64(conn)
            # v64→v65: mcp_secrets — shared vault for MCP source auth.
            _v64_to_v65(conn)
            # v65→v66: per-user MCP secrets + scope column on mcp_sources.
            _v65_to_v66(conn)
            # v66→v67: data_package_tools junction — links packages to MCP tools.
            _v66_to_v67(conn)
            # v67→v68: cloud chat tables — chat_sessions, chat_messages,
            # user_workdirs + indexes. _SYSTEM_SCHEMA already creates them on
            # fresh installs via CREATE TABLE/INDEX IF NOT EXISTS (no-op here).
            _v67_to_v68(conn)
            # v68→v69: mcp_sources.env — per-source non-secret env vars for
            # the spawned stdio subprocess. ADD COLUMN IF NOT EXISTS (no-op here).
            _v68_to_v69(conn)
            # v69→v70: live co-drive foundation — co-session flags +
            # chat_session_participants. Additive; _SYSTEM_SCHEMA builds it
            # on fresh installs (no-op here).
            _v69_to_v70(conn)
            # v70→v71: formalize users.slack_user_id. _SYSTEM_SCHEMA already
            # creates it on fresh installs (no-op here).
            _v70_to_v71(conn)
            # v71→v72: system_secrets — server-wide vault for Slack bot tokens.
            _v71_to_v72(conn)
            # v72→v73: sandbox pause/resume refs on chat_sessions (un-indexed).
            _v72_to_v73(conn)
            # v73→v74: server_only distribution flag on table_registry.
            # _SYSTEM_SCHEMA already declares the column on fresh installs
            # (no-op ALTER here). Issue #607.
            _v73_to_v74(conn)
            # v74→v75: source_mode/git_path/base_sha on instance_templates.
            # _SYSTEM_SCHEMA already declares the columns on fresh installs
            # (no-op ALTER here). Issue #622 Slice 1.
            _v74_to_v75(conn)
            # v75→v76: store_entity_votes (per-user thumbs up/down on store
            # entities). _SYSTEM_SCHEMA already creates the table on fresh
            # installs (no-op CREATE here). Issue #398.
            _v75_to_v76(conn)
            # v76→v77: users.must_change_password — forced rotation flag for
            # seeded/admin-set passwords. _SYSTEM_SCHEMA already creates the
            # column on fresh installs (no-op ALTER here).
            _v76_to_v77(conn)
            # v77→v78: built-in marketplace columns. is_builtin on
            # marketplace_registry; admin_disabled on marketplace_plugins.
            _v77_to_v78(conn)
            # v78→v79: named source_connections + vault-backed connection_secrets
            # + table_registry.connection_id (spec 2026-06-12).
            _v78_to_v79(conn)
            # v79→v80: authoring_suggestions (authoring-studio suggestion queue).
            _v79_to_v80(conn)
            # v80→v81: memory_mining_consent (per-user opt-in to session mining).
            _v80_to_v81(conn)
            # v81→v82: Collections foundation — file_corpora, corpus_files,
            # corpus_chunks. _SYSTEM_SCHEMA already creates them on fresh
            # installs (no-op CREATE IF NOT EXISTS here).
            _v81_to_v82(conn)
            # v82→v83: OAuth 2.1 tables for the native MCP connector
            # (oauth_clients, oauth_auth_codes, oauth_access_tokens,
            # oauth_refresh_tokens). IF NOT EXISTS — no-op on fresh installs.
            _v82_to_v83(conn)
            # v83→v84: resource column on oauth_refresh_tokens so refreshed
            # access tokens keep their RFC 8707 resource binding.
            _v83_to_v84(conn)
            # v84→v85: pre-seed vscode-mcp public OAuth client so VS Code
            # native MCP users can enter 'vscode-mcp' in the manual dialog.
            _v84_to_v85(conn)
            # v85→v86: backfill Everyone membership for users missing it
            # (issue #748). Fresh installs have zero users at this point,
            # so the INSERT..SELECT is a no-op; kept for ladder readability
            # and so a fresh-install-then-manual-user-insert-before-boot
            # scenario in tests still lands correctly.
            _v85_to_v86(conn)
            # v86→v87: ref (tag/commit pin) column on marketplace_registry.
            # _SYSTEM_SCHEMA already declares it on fresh installs (no-op
            # ALTER here). Issue #781.
            _v86_to_v87(conn)
            # v87→v88: corpus_files.parent_file_id (bundle children).
            # _SYSTEM_SCHEMA already carries the column on fresh installs;
            # the guarded ALTER is a no-op here.
            _v87_to_v88(conn)
            # v88→v89: knowledge_digests table (K4, #799). _SYSTEM_SCHEMA
            # already creates it on fresh installs (no-op CREATE IF NOT
            # EXISTS here).
            _v88_to_v89(conn)
            # v89→v90: chat_broker_tickets table (chat sandbox secret
            # broker). _SYSTEM_SCHEMA already creates it on fresh installs
            # (no-op CREATE IF NOT EXISTS here).
            _v89_to_v90(conn)
            # v90→v91: skill lint tables (store_lint_runs/findings/dismissals/entity_state).
            # _SYSTEM_SCHEMA already creates them on fresh installs (no-op
            # CREATE IF NOT EXISTS here).
            _v90_to_v91(conn)
            # v91→v92: mcp_sources.connect_hint column.
            _v91_to_v92(conn)
            # v92→v93: glossary_terms table (Keboola semantic-glossary
            # import). _SYSTEM_SCHEMA already creates it on fresh installs
            # (no-op CREATE IF NOT EXISTS here).
            _v92_to_v93(conn)
            # v93→v94: jobs table (durable job queue, wave-2B worker runtime
            # foundation). _SYSTEM_SCHEMA already creates it on fresh
            # installs (no-op CREATE IF NOT EXISTS here).
            _v93_to_v94(conn)
            # v94→v95: drop usage_session_summary's 3 secondary indexes
            # (index-corruption hotfix). No-op here — _SYSTEM_SCHEMA never
            # creates them on fresh installs.
            _v94_to_v95(conn)
            # v95→v96: data_apps table (hosted user web apps registry).
            # _SYSTEM_SCHEMA already creates it on fresh installs (no-op
            # CREATE IF NOT EXISTS here).
            _v95_to_v96(conn)
            # v96→v97: corpus_files.path (upsert-on-upload identity).
            # _SYSTEM_SCHEMA already declares it on fresh installs (no-op
            # ALTER here).
            _v96_to_v97(conn)
            # v97→v98: chat_sessions.relay_protocol_version (restart-invariant
            # sandbox reuse) + user_journey_state table (chat-driven onboarding
            # backend foundation). _SYSTEM_SCHEMA already declares both on
            # fresh installs (no-op here).
            _v97_to_v98(conn)
            # v98→v99: data_apps draft model (parent_app_id, is_draft,
            # draft_branch). _SYSTEM_SCHEMA already declares the columns on
            # fresh installs (no-op ALTER here).
            _v98_to_v99(conn)
            # v99→v100: sync_state.parts (per-partition manifest for
            # partitioned distribution). _SYSTEM_SCHEMA already declares the
            # column on fresh installs (no-op ALTER here).
            _v99_to_v100(conn)
            # v100→v101: agents / agent_scope / llm_usage / agent_scope_snapshots
            # / idempotency_keys tables + agent_id columns on
            # personal_access_tokens/chat_sessions (agent profiles +
            # agent-as-API foundation). _SYSTEM_SCHEMA already creates/
            # declares all of these on fresh installs (no-op here).
            _v100_to_v101(conn)
            # v101→v102: agent_webhooks / agent_artifacts tables (agent-api
            # V1b). _SYSTEM_SCHEMA already creates them on fresh installs
            # (no-op CREATE IF NOT EXISTS here).
            _v101_to_v102(conn)
            # v102→v103: agent_memories table (agent-api V1c). _SYSTEM_SCHEMA
            # already creates it on fresh installs (no-op CREATE IF NOT
            # EXISTS here).
            _v102_to_v103(conn)
            # v103→v104: audit_log identity backfill — no-op on a fresh
            # install (empty audit_log), kept for ladder chronology.
            _v103_to_v104(conn)
            # v104→v105: usage_session_summary.uploaded_at — declared in
            # _SYSTEM_SCHEMA on fresh installs; backfill is a no-op.
            _v104_to_v105(conn)
            # v105→v106: personal_access_tokens.surface — declared in
            # _SYSTEM_SCHEMA on fresh installs (no-op ALTER here).
            _v105_to_v106(conn)
            # v106→v107: metric_definitions/glossary_terms.source_ref —
            # declared in _SYSTEM_SCHEMA on fresh installs; no-op here.
            _v106_to_v107(conn)
            # v107→v108: data_apps linked columns (external_url/source_ref/
            # managed/description_override) — declared in _SYSTEM_SCHEMA on
            # fresh installs; no-op here.
            _v107_to_v108(conn)
            # v108→v109: outbound MCP OAuth data-layer foundation —
            # mcp_source_oauth_clients / mcp_user_oauth_tokens /
            # mcp_oauth_flows. CREATE TABLE IF NOT EXISTS — no-op here
            # since the tables aren't in _SYSTEM_SCHEMA (fresh installs get
            # them from this same call).
            _v108_to_v109(conn)
            # --- paper-theme branch schema, restacked on top of main's ladder ---
            # v109→v110: file_corpora.origin (uploaded | generated). No-op on
            # fresh installs — _SYSTEM_SCHEMA already declares the column.
            _v109_to_v110(conn)
            # v110→v111: store_entities publisher_kind + verification_state
            # (+ verified_at/by/note). No-op on fresh installs — _SYSTEM_SCHEMA
            # already declares the columns.
            _v110_to_v111(conn)
            # v111→v112: agents builder superset columns (role/tone/greeting/
            # knowledge/plugins/surfaces/status) — combines the paper-theme
            # agent-builder fields onto main's agents table. No-op on fresh
            # installs — _SYSTEM_SCHEMA already declares them.
            _v111_to_v112(conn)
            # v112→v113: chat_sessions.pinned_at (pinned conversations). No-op
            # on fresh installs — _SYSTEM_SCHEMA already declares the column.
            _v112_to_v113(conn)
            # v113→v114: data_packages.publisher_kind, replacing the derived
            # `curated` badge with the stored trust axis store_entities uses.
            # No-op on fresh installs — _SYSTEM_SCHEMA already declares the
            # column, and the backfill then finds no rows to promote.
            _v113_to_v114(conn)
            # v114→v115: reclassify pre-existing governance-created agents
            # from draft to ready. No-op on fresh installs — no agents exist
            # yet to reclassify.
            _v114_to_v115(conn)
            # v115→v116: table_registry access-policy columns. No-op on
            # fresh installs — _SYSTEM_SCHEMA already declares the columns.
            _v115_to_v116(conn)
            # v116→v117: semantic_models + semantic_sources + junction. No-op
            # on fresh installs — _SYSTEM_SCHEMA already declares them.
            _v116_to_v117(conn)
            # v117→v118: add user_journey_state.agent_created for the sixth
            # onboarding step ("Create your first agent"). No-op on fresh
            # installs — _SYSTEM_SCHEMA already declares the column.
            _v117_to_v118(conn)
            # v118→v119: tool_registry.projection_map. No-op on fresh
            # installs — _SYSTEM_SCHEMA already declares the column.
            _v118_to_v119(conn)
            # v119→v120: agent_schedules (scheduled agent runs). No-op on
            # fresh installs — _SYSTEM_SCHEMA already declares the table.
            _v119_to_v120(conn)
            # v120→v121: tool_grants.allow_mutating. tool_grants is created
            # by the ladder (_v63_to_v64), not _SYSTEM_SCHEMA, so this ALTER
            # does real work on fresh installs too.
            _v120_to_v121(conn)
            # v121→v122: builder-agent scope backfill. Selects nothing on a
            # fresh install (no agents yet) — called for its version stamp,
            # which on this branch is what leaves the DB at SCHEMA_VERSION.
            _v121_to_v122(conn)
            # v122→v123: chat_messages.parts. No-op on fresh installs —
            # _SYSTEM_SCHEMA already declares the column — so this is called
            # for its version stamp.
            _v122_to_v123(conn)
            # v123→v124: sync_state/sync_history id backfill (B1). No-op on
            # a fresh install — no sync_state rows exist yet; called for its
            # version stamp, which on this branch is what leaves the DB at
            # SCHEMA_VERSION.
            _v123_to_v124(conn)
            # Fresh-install seed is handled by the unconditional
            # _seed_core_roles call at the bottom of _ensure_schema —
            # left as a no-op branch here so the migration ladder still
            # reads chronologically.
        else:
            if current < 2:
                for sql in _V1_TO_V2_MIGRATIONS:
                    conn.execute(sql)
            if current < 3:
                for sql in _V2_TO_V3_MIGRATIONS:
                    conn.execute(sql)
            if current < 4:
                for sql in _V3_TO_V4_MIGRATIONS:
                    conn.execute(sql)
            if current < 5:
                for sql in _V4_TO_V5_MIGRATIONS:
                    conn.execute(sql)
            if current < 6:
                for sql in _V5_TO_V6_MIGRATIONS:
                    conn.execute(sql)
            if current < 7:
                for sql in _V6_TO_V7_MIGRATIONS:
                    conn.execute(sql)
            if current < 8:
                for sql in _V7_TO_V8_MIGRATIONS:
                    conn.execute(sql)
            if current < 9:
                for sql in _V8_TO_V9_MIGRATIONS:
                    conn.execute(sql)
                # v9 finalize: seed core.* roles, backfill grants from
                # legacy users.role, then drop the column. Order matters —
                # backfill needs the seed rows to exist; drop must be last.
                _seed_core_roles(conn)
                _backfill_users_role_to_grants(conn)
                # DuckDB rejects DROP COLUMN while user_role_grants FK
                # references users(id), so we NULL the legacy values instead
                # — UserRepository ignores the column going forward. Physical
                # drop is deferred to a future schema-rebuild migration.
                # Skip UPDATE if the column never existed (e.g. test fixtures
                # starting from v2/v3 with a hand-crafted minimal users table).
                has_role_col = conn.execute(
                    "SELECT 1 FROM information_schema.columns WHERE table_name = 'users' AND column_name = 'role'"
                ).fetchone()
                if has_role_col:
                    conn.execute("UPDATE users SET role = NULL")
            if current < 10:
                for sql in _V9_TO_V10_MIGRATIONS:
                    conn.execute(sql)
            if current < 11:
                for sql in _V10_TO_V11_MIGRATIONS:
                    conn.execute(sql)
            if current < 12:
                for sql in _V11_TO_V12_MIGRATIONS:
                    conn.execute(sql)
            if current < 13:
                for sql in _V12_TO_V13_MIGRATIONS:
                    conn.execute(sql)
                _v12_to_v13_finalize(conn)
            if current < 14:
                _v13_to_v14_finalize(conn)
            if current < 15:
                for sql in _V14_TO_V15_MIGRATIONS:
                    conn.execute(sql)
            if current < 16:
                for sql in _V15_TO_V16_MIGRATIONS:
                    conn.execute(sql)
            if current < 17:
                for sql in _V16_TO_V17_MIGRATIONS:
                    conn.execute(sql)
            if current < 18:
                _v17_to_v18_finalize(conn)
            if current < 19:
                _v18_to_v19_finalize(conn)
            if current < 20:
                for sql in _V19_TO_V20_MIGRATIONS:
                    conn.execute(sql)
            if current < 21:
                for sql in _V20_TO_V21_MIGRATIONS:
                    conn.execute(sql)
            if current < 22:
                for sql in _V21_TO_V22_MIGRATIONS:
                    conn.execute(sql)
            if current < 23:
                for sql in _V22_TO_V23_MIGRATIONS:
                    conn.execute(sql)
            if current < 24:
                _v23_to_v24_finalize(conn)
            if current < 25:
                for sql in _V24_TO_V25_MIGRATIONS:
                    conn.execute(sql)
            if current < 26:
                for sql in _V25_TO_V26_MIGRATIONS:
                    conn.execute(sql)
            if current < 27:
                for sql in _V26_TO_V27_MIGRATIONS:
                    conn.execute(sql)
            if current < 28:
                for sql in _V27_TO_V28_MIGRATIONS:
                    conn.execute(sql)
            if current < 29:
                for sql in _V28_TO_V29_MIGRATIONS:
                    conn.execute(sql)
                _v28_to_v29_finalize(conn)
            if current < 30:
                for sql in _V29_TO_V30_MIGRATIONS:
                    conn.execute(sql)
            if current < 31:
                _v30_to_v31_migrate(conn)
            if current < 32:
                for sql in _V31_TO_V32_MIGRATIONS:
                    conn.execute(sql)
            if current < 33:
                for sql in _V32_TO_V33_MIGRATIONS:
                    conn.execute(sql)
            if current < 34:
                for sql in _V33_TO_V34_MIGRATIONS:
                    conn.execute(sql)
            if current < 35:
                _v34_to_v35_migrate(conn)
            if current < 36:
                _v35_to_v36_migrate(conn)
            if current < 37:
                for sql in _V36_TO_V37_MIGRATIONS:
                    conn.execute(sql)
            if current < 38:
                _v37_to_v38_migrate(conn)
            if current < 39:
                for sql in _V38_TO_V39_MIGRATIONS:
                    conn.execute(sql)
            if current < 40:
                for sql in _V39_TO_V40_MIGRATIONS:
                    conn.execute(sql)
            if current < 41:
                _v40_to_v41(conn)
            if current < 42:
                _v41_to_v42(conn)
            if current < 43:
                _v42_to_v43(conn)
            if current < 44:
                _v43_to_v44(conn)
            if current < 45:
                _v44_to_v45(conn)
            if current < 46:
                _v45_to_v46(conn)
            if current < 47:
                _v46_to_v47(conn)
            if current < 48:
                _v47_to_v48(conn)
            if current < 49:
                _v48_to_v49_migrate(conn)
            if current < 50:
                _v49_to_v50_migrate(conn)
            if current < 51:
                for sql in _V50_TO_V51_MIGRATIONS:
                    conn.execute(sql)
            if current < 52:
                _v51_to_v52(conn)
            if current < 53:
                _v52_to_v53(conn)
            if current < 54:
                _v53_to_v54(conn)
            if current < 55:
                _v54_to_v55(conn)
            if current < 56:
                _v55_to_v56(conn)
            if current < 57:
                _v56_to_v57(conn)
            if current < 58:
                _v57_to_v58(conn)
            if current < 59:
                _v58_to_v59(conn)
            if current < 60:
                _v59_to_v60(conn)
            if current < 61:
                _v60_to_v61(conn)
            if current < 62:
                _v61_to_v62(conn)
            if current < 63:
                _v62_to_v63(conn)
            if current < 64:
                _v63_to_v64(conn)
            if current < 65:
                _v64_to_v65(conn)
            if current < 66:
                _v65_to_v66(conn)
            if current < 67:
                _v66_to_v67(conn)
            if current < 68:
                _v67_to_v68(conn)
            if current < 69:
                _v68_to_v69(conn)
            if current < 70:
                _v69_to_v70(conn)
            if current < 71:
                _v70_to_v71(conn)
            if current < 72:
                _v71_to_v72(conn)
            if current < 73:
                _v72_to_v73(conn)
            if current < 74:
                _v73_to_v74(conn)
            if current < 75:
                _v74_to_v75(conn)
            if current < 76:
                _v75_to_v76(conn)
            if current < 77:
                _v76_to_v77(conn)
            if current < 78:
                _v77_to_v78(conn)
            if current < 79:
                _v78_to_v79(conn)
            if current < 80:
                _v79_to_v80(conn)
            if current < 81:
                _v80_to_v81(conn)
            if current < 82:
                _v81_to_v82(conn)
            if current < 83:
                _v82_to_v83(conn)
            if current < 84:
                _v83_to_v84(conn)
            if current < 85:
                _v84_to_v85(conn)
            if current < 86:
                _v85_to_v86(conn)
            if current < 87:
                _v86_to_v87(conn)
            if current < 88:
                _v87_to_v88(conn)
            if current < 89:
                _v88_to_v89(conn)
            if current < 90:
                _v89_to_v90(conn)
            if current < 91:
                _v90_to_v91(conn)
            if current < 92:
                _v91_to_v92(conn)
            if current < 93:
                _v92_to_v93(conn)
            if current < 94:
                _v93_to_v94(conn)
            if current < 95:
                _v94_to_v95(conn)
            if current < 96:
                _v95_to_v96(conn)
            if current < 97:
                _v96_to_v97(conn)
            if current < 98:
                _v97_to_v98(conn)
            if current < 99:
                _v98_to_v99(conn)
            if current < 100:
                _v99_to_v100(conn)
            if current < 101:
                _v100_to_v101(conn)
            if current < 102:
                _v101_to_v102(conn)
            if current < 103:
                _v102_to_v103(conn)
            if current < 104:
                _v103_to_v104(conn)
            if current < 105:
                _v104_to_v105(conn)
            if current < 106:
                _v105_to_v106(conn)
            if current < 107:
                _v106_to_v107(conn)
            if current < 108:
                _v107_to_v108(conn)
            if current < 109:
                _v108_to_v109(conn)
            if current < 110:
                _v109_to_v110(conn)
            if current < 111:
                _v110_to_v111(conn)
            if current < 112:
                _v111_to_v112(conn)
            if current < 113:
                _v112_to_v113(conn)
            if current < 114:
                _v113_to_v114(conn)
            if current < 115:
                _v114_to_v115(conn)
            if current < 116:
                _v115_to_v116(conn)
            if current < 117:
                _v116_to_v117(conn)
            if current < 118:
                _v117_to_v118(conn)
            if current < 119:
                _v118_to_v119(conn)
            if current < 120:
                _v119_to_v120(conn)
            if current < 121:
                _v120_to_v121(conn)
            if current < 122:
                _v121_to_v122(conn)
            if current < 123:
                _v122_to_v123(conn)
            if current < 124:
                _v123_to_v124(conn)
            conn.execute(
                "UPDATE schema_version SET version = ?, applied_at = current_timestamp",
                [SCHEMA_VERSION],
            )
            # Force WAL → main DB consolidation immediately after the
            # migration ladder. Without this, the v27 `ALTER TABLE
            # table_registry ADD COLUMN` statements sit in
            # `system.duckdb.wal` until DuckDB's next implicit checkpoint;
            # if the container is killed in that window (e.g. by the
            # auto-upgrade cron's `docker compose up -d` mid-deploy),
            # the next start's WAL replay hits an `INTERNAL Error:
            # Calling DatabaseManager::GetDefaultDatabase with no default
            # database set` on the `ReplayAlter` path and the system
            # database becomes unrecoverable from the running binary —
            # the operator has to restore from the pre-migrate snapshot
            # by hand. This was reproduced on agnes-dev during PR #217
            # rollout: container restart 5s after the v27 migration
            # window left the DB in an unhealthy=db_schema=unreachable
            # state.
            #
            # CHECKPOINT flushes the WAL to the main DB file
            # synchronously. Best-effort: if it fails (read-only handle,
            # in-memory DB, or transient lock), log and continue —
            # exactly the same exposure as before this fix.
            try:
                conn.execute("CHECKPOINT")
            except Exception as e:
                logger.warning(
                    "Post-migration CHECKPOINT failed (%s); WAL may "
                    "contain unflushed ALTER ops. A clean shutdown of "
                    "this process before any container restart is the "
                    "safe path; otherwise, the next start may need to "
                    "restore from %s.",
                    e,
                    _get_state_dir() / "system.duckdb.pre-migrate",
                )

    # corpus_files(corpus_id, path) UNIQUE INDEX. Deliberately created here,
    # after the migration ladder, rather than in _SYSTEM_SCHEMA: the column is
    # ALTER-added at v97 onto a table that exists since v82, and _SYSTEM_SCHEMA
    # runs before the ladder, so declaring the index there crashes every upgrade
    # from a v82..v96 DB. Running it here covers all three paths — fresh install,
    # incremental upgrade, and the split-brain future-version self-heal.
    _ensure_corpus_path_index(conn)

    # Always run the system-groups seed when the DB is on a version this binary
    # understands — per-connect safety net so a manually-deleted Admin/Everyone
    # row reappears next start. Two UPSERTs; near-zero cost. Lives outside the
    # migration guard so:
    #   1. recovery: deleted system group reappears on next start;
    #   2. fresh installs: the (current == 0) branch above doesn't need its own
    #      seed — _SYSTEM_SCHEMA created the user_groups table empty.
    # Skip when current > SCHEMA_VERSION (future-version-noop rollback contract).
    # Skip when app-state lives in Postgres: the groups are seeded there
    # through the repository factory (app.main lifespan), and this local file
    # then only serves the deliberately DuckDB-local tables (cli_auth_codes) —
    # seeding here would side-channel state writes into the wrong backend.
    if get_schema_version(conn) <= SCHEMA_VERSION and not _state_backend_is_pg():
        _seed_system_groups(conn)

    # Same reasoning as the seed above — this has to live OUTSIDE the
    # `if current < SCHEMA_VERSION` migration guard. A DB already stamped at
    # SCHEMA_VERSION skips the entire ladder, so a stamp written by a build that
    # lacked the matching v104 step can never be repaired from inside it. Checks
    # for the columns directly instead of trusting the stamp; one
    # information_schema read per connect. See
    # _heal_store_entity_trust_columns.
    #
    # _heal_legacy_agents_table is the same class of repair one step further —
    # there the stamp is not merely ahead of a missing column but ahead of a
    # whole pre-merge table SHAPE (see its docstring), so it too checks the
    # columns directly rather than trusting the version.
    #
    # _heal_stranded_ladder_columns closes the rest of that same gap: the
    # renumbering skipped whole steps, not just the agents table, so the plain
    # ADD COLUMNs those steps carried (chat_sessions.agent_id above all) need
    # the same stamp-independent treatment.
    if not _state_backend_is_pg():
        _heal_store_entity_trust_columns(conn)
        _heal_data_package_publisher_column(conn)
        _heal_legacy_agents_table(conn)
        _heal_stranded_ladder_columns(conn)
        # Flush whatever the heals just wrote. The post-migration CHECKPOINT
        # above sits inside `if current < SCHEMA_VERSION`, and a stranded DB
        # is stamped AT the head — so on precisely the databases these heals
        # exist for, that checkpoint never runs and their ALTER TABLE ... ADD
        # COLUMN statements stay in the WAL. That is the exact shape the
        # migration checkpoint documents as able to leave system.duckdb
        # unrecoverable on a cross-version WAL replay after an abrupt restart
        # (Devin Review on #1158). Unconditional and best-effort: a no-op
        # checkpoint on an unchanged DB is cheap, and deciding whether any
        # heal altered anything would put the correctness of a corruption
        # guard behind four independent return values.
        try:
            conn.execute("CHECKPOINT")
        except Exception as e:
            logger.warning(
                "Post-heal CHECKPOINT failed (%s); repaired columns may sit in the WAL. "
                "Shut this process down cleanly before any container restart.",
                e,
            )


def get_schema_version(conn: duckdb.DuckDBPyConnection) -> int:
    """Get current schema version. Returns 0 if no schema exists."""
    try:
        result = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        return result[0] if result and result[0] else 0
    except duckdb.CatalogException:
        return 0


def close_system_db() -> None:
    """Close the shared system DB connection. Called on app shutdown.

    CHECKPOINT before close so the WAL flushes into ``system.duckdb`` and
    the file is left in a clean state. If we skip this and the process
    later gets SIGKILL'd (e.g. Docker's default 10s stop_grace_period
    expires during ``docker compose up -d`` recreate), DuckDB leaves a
    populated ``.wal`` that the next process must replay on open. When
    the next process is a different DuckDB version (image upgrade
    window), replay can hit internal assertions like
    ``Failure while replaying WAL ... GetDefaultDatabase with no default
    database set`` and the app 500s on every authed request.

    CHECKPOINT is best-effort: if it raises (locked, disk full, etc.)
    we still proceed to close — the recovery path in ``_try_open_system_db``
    plus the longer ``stop_grace_period`` in compose are the safety nets.

    **Rolling-snapshot handoff (#1294):** if ``refresh_rolling_snapshot`` is
    mid-``EXPORT DATABASE`` on a cursor derived from this connection —
    reachable when its ``to_thread_drain_on_cancel`` caller (the
    checkpoint/rolling-snapshot loop in ``app.main``) abandoned that thread
    because the export outlived the shared shutdown drain budget — closing
    the parent connection out from under that still-executing child cursor
    is the exact wedge the drain helper exists to prevent. So before doing
    anything else, interrupt that cursor and wait (bounded by
    ``_ROLLING_SNAPSHOT_INTERRUPT_TIMEOUT_S``) for it to actually finish.
    The wait is polled rather than a single ``interrupt()`` call because
    ``interrupt()`` only cancels whatever is *currently* executing on the
    cursor — if it lands in the gap between the export's own ``CHECKPOINT``
    and ``EXPORT DATABASE`` statements it is a no-op, so we keep re-issuing
    it until the export thread reports itself idle or the bound elapses.
    This never blocks indefinitely: once the bound is spent we log and
    proceed to close anyway, same fallback philosophy as the CHECKPOINT
    best-effort below.
    """
    global _system_db_conn, _system_db_path

    if not interrupt_rolling_snapshot_export(_ROLLING_SNAPSHOT_INTERRUPT_TIMEOUT_S, caller="close_system_db"):
        logger.warning(
            "close_system_db: rolling-snapshot export still running %.1fs after being "
            "interrupted; closing system.duckdb anyway",
            _ROLLING_SNAPSHOT_INTERRUPT_TIMEOUT_S,
        )

    if _system_db_conn:
        try:
            _system_db_conn.execute("CHECKPOINT")
            logger.debug("close_system_db: CHECKPOINT ok")
        except Exception as exc:
            # Log + proceed — CHECKPOINT failure is not fatal (recovery path
            # in _try_open_system_db handles a dirty WAL on next open), but
            # we want operators to see WHY the safety net was needed if a
            # WAL-replay failure does surface later.
            logger.warning("close_system_db: CHECKPOINT failed (%s); proceeding to close", exc)
        try:
            _system_db_conn.close()
        except Exception as exc:
            logger.debug("close_system_db: close raised (%s); ignoring", exc)
        _system_db_conn = None
        _system_db_path = None


def checkpoint_system_db() -> bool:
    """Best-effort CHECKPOINT of the open system DB singleton (#710).

    The app holds this connection for its whole lifetime, which makes
    DuckDB defer its automatic 16 MB-threshold checkpoint indefinitely —
    in steady state the WAL grows unbounded and days of state writes
    (users, PATs, grants) live only in the WAL. A non-graceful exit then
    leaves a dirty WAL, and replaying it under a different DuckDB version
    (image-upgrade window) can render system.duckdb unrecoverable. The
    periodic lifespan task in ``app.main`` calls this every few minutes
    so the WAL never sits unflushed for days.

    Returns True when a CHECKPOINT ran, False when skipped (no open
    singleton — never opens one implicitly) or when DuckDB refused (e.g.
    "there are other transactions active"); refusal is expected under
    load and simply means the next tick retries.

    Holds ``_system_db_lock`` for the read-and-execute, like every other
    accessor of the ``_system_db_conn`` global — without it a tick can
    race the DATA_DIR-reopen branch in ``get_system_db()`` and execute
    on an already-closed native connection.
    """
    with _system_db_lock:
        if _system_db_conn is None:
            return False
        try:
            _system_db_conn.execute("CHECKPOINT")
            logger.debug("checkpoint_system_db: CHECKPOINT ok")
            return True
        except Exception as exc:
            # Concurrent transactions make DuckDB refuse a plain CHECKPOINT;
            # deliberately NOT `FORCE CHECKPOINT`, which would abort them.
            logger.warning("checkpoint_system_db: CHECKPOINT failed (%s); will retry next tick", exc)
            return False


_ROLLING_SNAPSHOT_DIRNAME = "system.duckdb.rolling-snapshot"
_ROLLING_SNAPSHOT_DEFAULT_INTERVAL_HOURS = 6.0

#: Bounds how long close_system_db() waits for an in-flight rolling-snapshot
#: EXPORT to unwind after being interrupted (#1294 — see refresh_rolling_snapshot
#: and close_system_db below). A safety bound, not the expected wait: DuckDB's
#: interrupt() is checked at operator boundaries and returns in well under a
#: second even mid-EXPORT of a multi-million-row table (measured).
_ROLLING_SNAPSHOT_INTERRUPT_TIMEOUT_S = 5.0

# Tracks the cursor (if any) currently running the rolling-snapshot EXPORT, so
# close_system_db() can interrupt + wait for it instead of closing the parent
# connection out from under a still-executing child cursor (#1294) — the exact
# wedge `to_thread_drain_on_cancel` exists to prevent, reachable here because a
# full-DB EXPORT can outlive that helper's shared shutdown drain budget.
#
# A DEDICATED lock, not `_system_db_lock`: close_system_db() holds this only
# long enough to read the cursor reference, then waits on the Event *unlocked*.
# refresh_rolling_snapshot's own cleanup needs this same lock (briefly, in its
# `finally`) to clear the cursor and set the Event — holding it across the wait
# would deadlock the two against each other.
_rolling_snapshot_export_lock = threading.Lock()
_rolling_snapshot_export_cursor: duckdb.DuckDBPyConnection | None = None
_rolling_snapshot_export_idle = threading.Event()
_rolling_snapshot_export_idle.set()  # no export in flight by default

# Serializes whole refresh_rolling_snapshot() runs. The machinery above is
# single-slot (one published cursor, one fixed `.tmp` scratch path), so two
# overlapping refreshes would clear each other's cursor/idle state — the very
# state close_system_db() keys its close decision on — and rmtree each other's
# in-progress export (Devin on #1294). Today the only production caller is the
# checkpoint-loop tick, so overlap needs a second caller (a future CLI/force
# surface); guard it structurally rather than by convention.
_rolling_snapshot_refresh_lock = threading.Lock()


def interrupt_rolling_snapshot_export(wait_s: float = 0.0, *, caller: str = "") -> bool:
    """Interrupt an in-flight rolling-snapshot ``EXPORT DATABASE`` (#1294).

    ``interrupt()`` only cancels the statement *currently* executing on the
    cursor — landing in the gap between the refresh's ``CHECKPOINT`` and
    ``EXPORT DATABASE`` is a no-op — so it is re-issued on a short poll until
    the export thread reports itself idle or ``wait_s`` elapses. Callers that
    must not block (``begin_shutdown``) pass ``wait_s=0``: one interrupt shot,
    no wait; the refresh's own pre-EXPORT ``is_shutdown_started()`` gate
    covers the between-statements window for that caller.

    Returns True when no export is in flight (anymore), False when the bound
    elapsed with the export still running.
    """
    with _rolling_snapshot_export_lock:
        cur = _rolling_snapshot_export_cursor
    if cur is None:
        return True
    deadline = time.monotonic() + wait_s
    while not _rolling_snapshot_export_idle.is_set():
        try:
            cur.interrupt()
        except Exception as exc:
            logger.debug(
                "%s: interrupting in-flight rolling-snapshot export failed (%s)",
                caller or "interrupt_rolling_snapshot_export",
                exc,
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        _rolling_snapshot_export_idle.wait(timeout=min(remaining, 0.05))
    return _rolling_snapshot_export_idle.is_set()


def _rolling_snapshot_interval_hours() -> float:
    """Read the configured rolling-snapshot cadence, in hours.

    Operator override lives at instance.yaml
    ``backups.rolling_snapshot_interval_hours`` (see
    ``config/instance.yaml.example``). ``0`` disables the rolling refresh
    entirely. Default 6h bounds :func:`refresh_rolling_snapshot`'s
    ``EXPORT DATABASE`` overhead while still tightening the recovery point
    from "unbounded since the last migration" to "a few hours old" (#380).
    Any unreadable/unparsable config value falls back to the default rather
    than silently disabling the safeguard — including a parsable-but-non-
    finite one: YAML `.nan` defeats both the disabled-check and the freshness
    gate (a full export every tick), `.inf` silently disables the refresh
    forever. Same reasoning as ``_state_checkpoint_interval_s`` in
    ``app/main.py``, the sibling knob (Devin on #1294).
    """
    try:
        from app.instance_config import get_value

        v = get_value(
            "backups",
            "rolling_snapshot_interval_hours",
            default=_ROLLING_SNAPSHOT_DEFAULT_INTERVAL_HOURS,
        )
        if v is None:
            return _ROLLING_SNAPSHOT_DEFAULT_INTERVAL_HOURS
        parsed = float(v)
        if not math.isfinite(parsed):
            logger.warning(
                "backups.rolling_snapshot_interval_hours=%r is not finite; using default %sh",
                v,
                _ROLLING_SNAPSHOT_DEFAULT_INTERVAL_HOURS,
            )
            return _ROLLING_SNAPSHOT_DEFAULT_INTERVAL_HOURS
        return parsed
    except Exception:
        return _ROLLING_SNAPSHOT_DEFAULT_INTERVAL_HOURS


def _tighten_snapshot_modes(root: Path) -> None:
    """chmod a rolling-snapshot export to ``0o700`` dirs / ``0o600`` files.

    The export is a full logical copy of ``system.duckdb`` — argon2 password
    hashes, personal-access-token rows, the audit log, vault rows — so it gets
    the same treatment as every other derivative of that file in this module
    (:func:`_move_to_broken`, the discarded WAL). ``EXPORT DATABASE`` writes
    its parquet under the process umask (typically ``0o644``), which would
    make this the one world-readable copy.

    Best-effort, like the chmods it mirrors: a snapshot that exists with loose
    modes beats no snapshot. The containing ``state/`` directory is usually
    ``0o700`` already — this is defense in depth for backups, container
    volumes, and bind mounts.
    """
    for path in (root, *root.rglob("*")):
        try:
            os.chmod(path, 0o700 if path.is_dir() else 0o600)
        except OSError as exc:
            logger.debug("refresh_rolling_snapshot: chmod %s failed (%s)", path, exc)


def refresh_rolling_snapshot(*, force: bool = False) -> bool:
    """Refresh ``system.duckdb.rolling-snapshot/`` on a rolling cadence (#380).

    ``system.duckdb.pre-migrate`` is captured ONCE per migration transition
    and never refreshed, so as a recovery snapshot it goes stale within
    hours — every row written since the last migration is lost on a
    WAL-recovery restore (#379). This function adds a rolling-refreshed
    recovery aid that stays close to HEAD.

    **Why a separate artifact, not a refreshed ``pre-migrate``:**
    ``system.duckdb.pre-migrate`` is a plain DuckDB *file* — copied with
    ``shutil.copy2`` in :func:`_ensure_schema`, opened directly by
    :func:`_peek_schema_version`, and ``shutil.copy2``'d wholesale back onto
    ``system.duckdb`` by :func:`_try_open_system_db` during WAL recovery.
    Pattern A from #380 (the safe-under-concurrent-writes design chosen
    here) produces a logical export — a *directory* of Parquet files plus a
    ``schema.sql`` — via ``EXPORT DATABASE``. Writing that shape to the
    ``pre-migrate`` path would silently break both call sites and the
    WAL-recovery runbook (``docs/runbooks/wal-recovery.md``), which is a
    manual-recovery contract change, not something to ship unilaterally
    inside this fix. ``system.duckdb.rolling-snapshot/`` is therefore
    independent of the WAL-recovery auto-restore path: it is a manual,
    operator-driven recovery aid (``IMPORT DATABASE`` into a fresh file —
    see the runbook), not a new branch of ``_try_open_system_db``.

    **Locking discipline:** runs entirely over the app's own long-lived
    ``system.duckdb`` singleton (``_system_db_conn``), guarded by the same
    ``_system_db_lock`` every other accessor of that global uses (mirrors
    :func:`checkpoint_system_db`). Never opens a second connection to the
    file — DuckDB allows only one writer. If the singleton isn't open (a
    Postgres-state instance never opens it; a DuckDB-state process that
    hasn't touched ``system.duckdb`` yet), this is a silent no-op — the next
    tick after the singleton opens retries.

    **Atomicity:** ``EXPORT DATABASE`` writes into a fresh
    ``system.duckdb.rolling-snapshot.tmp`` directory next to the final one
    (same filesystem). Only once that export succeeds in full does the
    previous snapshot get swapped out — a failed/partial export never
    touches the existing snapshot, and any tmp scratch dir left behind by a
    crashed prior attempt is cleaned up unconditionally at the top of this
    function. The swap itself never destroys the last good copy: it is only
    deleted once a snapshot is confirmed present under the final name, so a
    swap that fails *and* whose restore fails too leaves it stranded at
    ``.prev``, which the next run reclaims (Devin on #1294).

    **Permissions:** the export is a full logical copy of ``system.duckdb``
    (argon2 password hashes, PAT rows, the audit log, vault rows), so the
    scratch dir is created ``0o700`` *before* the export and every exported
    file is chmod'd ``0o600`` after — matching :func:`_move_to_broken` and
    the discarded-WAL path rather than landing under the process umask.

    **Shutdown-awareness (#1294):** a full-DB ``EXPORT DATABASE`` can run for
    whole seconds — long enough to outlive the shared shutdown drain budget
    that ``app.main``'s checkpoint loop waits on (see
    ``to_thread_drain_on_cancel`` in ``app.api.health_probes``), which would
    abandon this function's thread mid-export and let the lifespan's
    :func:`close_system_db` close the connection out from under the still
    executing cursor — the exact wedge that drain helper exists to prevent.
    Two guards close that gap: (1) this function returns ``False``
    immediately, before doing any work, once
    :func:`app.api.health_probes.is_shutdown_started` reports the lifespan
    has begun tearing down — checked again right before the ``EXPORT``
    statement itself, in case shutdown starts mid-``CHECKPOINT``; and (2)
    while an export cursor is executing, it is published for
    :func:`close_system_db` to interrupt and wait (bounded) on, instead of
    racing it. Neither guard is skipped by ``force=True``.

    Args:
        force: skip the Postgres-backend guard's cadence check and the
            freshness gate — used by the CLI/tests to refresh unconditionally.
            The Postgres no-op guard and the shutdown guard (#1294) are both
            never skipped.

    Returns:
        True if a refresh actually ran, False if skipped (PG backend,
        shutdown under way, no open singleton, still fresh, cadence
        disabled) or if the export/swap failed (previous snapshot preserved
        either way).
    """
    if not _rolling_snapshot_refresh_lock.acquire(blocking=False):
        # See the lock's definition: the export machinery is single-slot, so
        # a second concurrent refresh must skip, never share state.
        logger.debug("refresh_rolling_snapshot: a refresh is already running; skipping")
        return False
    try:
        return _refresh_rolling_snapshot_locked(force=force)
    finally:
        _rolling_snapshot_refresh_lock.release()


def _refresh_rolling_snapshot_locked(*, force: bool) -> bool:
    """Body of :func:`refresh_rolling_snapshot`; caller holds the refresh lock."""
    global _rolling_snapshot_export_cursor

    from app.api.health_probes import is_shutdown_started

    if is_shutdown_started():
        # The lifespan is tearing down; close_system_db() may run at any
        # moment. Do not start a fresh multi-second EXPORT that could
        # outlive it — see the docstring's "Shutdown-awareness" section.
        return False

    if _state_backend_is_pg():
        return False

    interval_hours = _rolling_snapshot_interval_hours()
    if interval_hours <= 0 and not force:
        return False

    state_dir = _get_state_dir()
    final_dir = state_dir / _ROLLING_SNAPSHOT_DIRNAME
    prev_dir = state_dir / f"{_ROLLING_SNAPSHOT_DIRNAME}.prev"

    # A prior run whose swap AND restore both failed leaves the last good
    # snapshot stranded at `.prev` with no final_dir (see the swap block
    # below). It is still the only recovery artifact on disk, and neither the
    # operator nor `docs/runbooks/wal-recovery.md` knows that name — so put it
    # back under the documented one before anything else, including before the
    # freshness gate (its mtime IS the current recovery point) and before an
    # export that may itself fail.
    if prev_dir.is_dir() and not final_dir.exists():
        try:
            os.rename(prev_dir, final_dir)
            logger.warning(
                "refresh_rolling_snapshot: reclaimed stranded snapshot %s -> %s",
                prev_dir,
                final_dir,
            )
        except OSError as exc:
            logger.error(
                "refresh_rolling_snapshot: could not reclaim stranded snapshot %s (%s); "
                "it stays there and is NOT deleted",
                prev_dir,
                exc,
            )

    if not force and final_dir.is_dir():
        age_s = time.time() - final_dir.stat().st_mtime
        if age_s < interval_hours * 3600:
            return False  # still fresh; skip this tick

    tmp_dir = state_dir / f"{_ROLLING_SNAPSHOT_DIRNAME}.tmp"

    with _system_db_lock:
        conn = _system_db_conn
        if conn is None:
            # Never open a second connection to system.duckdb just to
            # snapshot it — DuckDB allows only one writer per file.
            return False
        # Take a dedicated cursor and run the export OUTSIDE the lock. The
        # lock guards the singleton GLOBAL's lifecycle (open/close/replace),
        # not query execution — every request already runs on its own cursor
        # without holding it, and DuckDB serializes cursors internally. A
        # full-DB EXPORT can take whole seconds on a grown system.duckdb;
        # holding the lock across it would stall get_system_db() — and with
        # it every authed request — for the export's entire duration (Devin
        # on #1294). The cursor is published below so close_system_db() can
        # interrupt + wait for it instead of closing the parent connection
        # out from under it (#1294 follow-up — see that function's docstring).
        try:
            cur = conn.cursor()
        except Exception as exc:
            # close_system_db() raced us and closed the singleton between
            # the None-check and here — same "no open singleton" outcome.
            logger.debug("refresh_rolling_snapshot: no usable cursor (%s)", exc)
            return False

    with _rolling_snapshot_export_lock:
        _rolling_snapshot_export_cursor = cur
        _rolling_snapshot_export_idle.clear()

    try:
        # A crashed prior attempt can leave a stale tmp export behind;
        # EXPORT DATABASE refuses to write into a non-empty directory.
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)

        # Pre-create the scratch dir 0o700 instead of chmod'ing after the
        # export: tightening afterwards would leave the parquet — password
        # hashes, PAT rows, the audit log — world-readable for the export's
        # entire duration. DuckDB accepts an existing EMPTY directory; only a
        # non-empty one is refused. chmod rather than makedirs(mode=...),
        # which is masked by the process umask.
        try:
            os.makedirs(tmp_dir, exist_ok=True)
            os.chmod(tmp_dir, 0o700)
        except OSError as exc:
            logger.warning(
                "refresh_rolling_snapshot: could not create export scratch dir %s (%s); previous snapshot kept",
                tmp_dir,
                exc,
            )
            return False

        try:
            cur.execute("CHECKPOINT")
        except Exception as exc:
            # Best-effort, like checkpoint_system_db(): a refused CHECKPOINT
            # (concurrent transactions) doesn't block the export — EXPORT
            # DATABASE reads through the connection's own view of committed
            # data regardless of what's flushed to the on-disk file.
            logger.debug("refresh_rolling_snapshot: CHECKPOINT failed (%s); exporting anyway", exc)

        if is_shutdown_started():
            # Shutdown began while we were mid-CHECKPOINT (interrupted by
            # close_system_db() or not) — do not start EXPORT DATABASE at
            # all. close_system_db() may already be interrupting/waiting on
            # this cursor (see its docstring); returning now, instead of
            # starting a fresh multi-second statement, is what lets that
            # wait resolve quickly.
            logger.debug("refresh_rolling_snapshot: shutdown started before EXPORT; aborting")
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return False

        try:
            cur.execute(f"EXPORT DATABASE '{tmp_dir}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        except Exception as exc:
            logger.warning(
                "refresh_rolling_snapshot: EXPORT DATABASE failed (%s); previous snapshot kept",
                exc,
            )
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return False

        # EXPORT DATABASE writes its files under the process umask (0o644)
        # regardless of the directory's mode — tighten them before the swap so
        # the artifact is never exposed under its final name.
        _tighten_snapshot_modes(tmp_dir)
    finally:
        # Unpublish the cursor and flip the idle event BEFORE closing it —
        # close_system_db()'s wait (#1294) is keyed on the event, not on
        # cur.close() completing, so this is what lets an interrupted wait
        # unblock as soon as we're done issuing statements on the cursor.
        with _rolling_snapshot_export_lock:
            _rolling_snapshot_export_cursor = None
            _rolling_snapshot_export_idle.set()
        try:
            cur.close()
        except Exception:
            pass

    # `.prev` is swap scratch ONLY while a snapshot exists under the final
    # name; otherwise it is a stranded last-good copy the reclaim above could
    # not move, and deleting it here would destroy the only artifact there is.
    if final_dir.exists():
        shutil.rmtree(prev_dir, ignore_errors=True)
    try:
        if final_dir.exists():
            os.rename(final_dir, prev_dir)
        os.rename(tmp_dir, final_dir)
    except OSError as exc:
        logger.warning("refresh_rolling_snapshot: swap failed (%s); previous snapshot kept", exc)
        # Best-effort restore of the previous snapshot if the swap
        # partially landed (final_dir moved aside but tmp_dir didn't
        # take its place). Guarded: if this rename fails too, the last good
        # snapshot is the copy at prev_dir, and an escaping OSError would both
        # break the documented `False` return and — via the `finally` below —
        # delete it. Keep it instead; the next run reclaims it.
        if prev_dir.exists() and not final_dir.exists():
            try:
                os.rename(prev_dir, final_dir)
            except OSError as restore_exc:
                logger.error(
                    "refresh_rolling_snapshot: could not restore the previous snapshot to %s "
                    "(%s); it is PRESERVED at %s and reclaimed on the next refresh",
                    final_dir,
                    restore_exc,
                    prev_dir,
                )
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return False
    finally:
        # Only once a snapshot is confirmed present under the final name is
        # the copy at prev_dir redundant.
        if final_dir.exists():
            shutil.rmtree(prev_dir, ignore_errors=True)

    logger.info("Rolling snapshot refreshed: %s", final_dir)
    return True


def close_analytics_db() -> None:
    """Close the shared analytics DB connection. Called on app shutdown.

    Mirrors `close_system_db()` above (best-effort CHECKPOINT then
    close, swallow exceptions). Analytics DB is the parquet-views layer;
    a dirty WAL on it is less consequential than on the system DB
    (read-only views can be rebuilt by the orchestrator on next start)
    but the CHECKPOINT keeps the file on-disk clean for any operator
    poking at it with the duckdb CLI.
    """
    global _analytics_db_conn, _analytics_db_path
    if _analytics_db_conn:
        try:
            _analytics_db_conn.execute("CHECKPOINT")
            logger.debug("close_analytics_db: CHECKPOINT ok")
        except Exception as exc:
            logger.warning("close_analytics_db: CHECKPOINT failed (%s); proceeding to close", exc)
        try:
            _analytics_db_conn.close()
        except Exception as exc:
            logger.debug("close_analytics_db: close raised (%s); ignoring", exc)
        _analytics_db_conn = None
        _analytics_db_path = None


def checkpoint_operational_db() -> bool:
    """Best-effort CHECKPOINT of the open operational DB singleton (#710).

    ``operational.duckdb`` is a second long-lived DuckDB singleton (CLI-auth +
    Slack-binding codes). Like ``get_system_db()`` the app holds the connection
    for its whole lifetime, so DuckDB defers its automatic checkpoint and the
    WAL never folds on its own — and on a Postgres-state instance this is the
    ONLY written DuckDB file, so the system-DB checkpoint loop would otherwise
    never touch it. Fold its WAL periodically so a non-graceful exit can't leave
    a dirty WAL (there is no salvage-reopen path for this file).

    Returns True when a CHECKPOINT ran, False when skipped (no open singleton —
    never opens one implicitly) or when DuckDB refused; refusal is expected
    under load and simply means the next tick retries. Mirrors
    ``checkpoint_system_db()``.
    """
    with _system_db_lock:
        if _operational_db_conn is None:
            return False
        try:
            _operational_db_conn.execute("CHECKPOINT")
            logger.debug("checkpoint_operational_db: CHECKPOINT ok")
            return True
        except Exception as exc:
            logger.warning("checkpoint_operational_db: CHECKPOINT failed (%s); will retry next tick", exc)
            return False


def close_operational_db() -> None:
    """Close the shared operational DB connection. Called on app shutdown.

    Mirrors ``close_system_db()`` (best-effort CHECKPOINT then close, swallow
    exceptions) so the ``operational.duckdb`` WAL is flushed into the main file
    and the file is left clean. Skipping this would leave a populated ``.wal``
    that the next process must replay on open — and unlike the system DuckDB
    there is no salvage-reopen recovery path here, so a failed replay would wedge
    CLI login + Slack binding until the file is deleted.
    """
    global _operational_db_conn, _operational_db_path
    if _operational_db_conn:
        try:
            _operational_db_conn.execute("CHECKPOINT")
            logger.debug("close_operational_db: CHECKPOINT ok")
        except Exception as exc:
            logger.warning("close_operational_db: CHECKPOINT failed (%s); proceeding to close", exc)
        try:
            _operational_db_conn.close()
        except Exception as exc:
            logger.debug("close_operational_db: close raised (%s); ignoring", exc)
        _operational_db_conn = None
        _operational_db_path = None
