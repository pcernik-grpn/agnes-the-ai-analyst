"""A3 PG-first ratchet — registry-level guard.

CLAUDE.md -> "Dual-backend discipline" freezes the DuckDB app-state backend:
existing `src/repositories/__init__.py` `_REGISTRY` entries that carry a
DuckDB backend stay maintained (bugfixes, contract tests), but no NEW entry
may add one. A brand-new app-state repo registers Postgres-only
(`_REGISTRY[key] = {PG: (...)}`, no `DUCKDB` key) — see `docs/migrations.md`
-> "Adding a PG-only feature".

This complements, rather than replaces, `tests/test_repository_registry.py`,
whose `test_registry_backends_are_symmetric` now accepts a PG-only shape as
legitimate alongside the frozen full pairs. That test catches a *malformed*
entry (DuckDB with no Postgres side at all); this one catches a
*well-formed* entry that shouldn't exist yet — a brand-new full pair, which
is symmetric and would otherwise sail through unnoticed.
"""

from __future__ import annotations

import src.repositories as factory

# ---------------------------------------------------------------------------
# the frozen list — every `_REGISTRY` key that carried a DuckDB backend the
# day the A3 ratchet flipped (main, 2026-08-26, SCHEMA_VERSION 124). Shrink
# only, as A4 deletes a pair's DuckDB half; never grow.
# ---------------------------------------------------------------------------
_FROZEN_DUCKDB_REGISTRY_KEYS: frozenset[str] = frozenset(
    {
        "access_token",
        "agent_artifacts",
        "agent_memories",
        "agent_schedules",
        "agent_webhooks",
        "agents",
        "audit",
        "authoring_suggestions",
        "bq_metadata_cache",
        "chat_message",
        "chat_session",
        "chat_session_participants",
        "claude_md_template",
        "column_metadata",
        "connection_secrets",
        "corpus_chunks",
        "corpus_files",
        "data_apps",
        "data_packages",
        "file_corpora",
        "glossary",
        "idempotency",
        "jobs",
        "knowledge",
        "knowledge_digests",
        "llm_usage",
        "marketplace_plugins",
        "marketplace_registry",
        "mcp_oauth_flows",
        "mcp_source_oauth_clients",
        "mcp_sources",
        "mcp_user_oauth_tokens",
        "memory_domain_suggestions",
        "memory_domains",
        "memory_mining_consent",
        "metric",
        "news_template",
        "notifications_pending_code",
        "notifications_script",
        "notifications_telegram",
        "oauth_clients",
        "observability_views",
        "per_user_secrets",
        "profile",
        "recipes",
        "reports",
        "resource_grants",
        "semantic_model",
        "semantic_source",
        "session_processor_state",
        "setup_tokens",
        "shared_secrets",
        "source_connections",
        "store_entities",
        "store_entity_votes",
        "store_lint",
        "store_submissions",
        "sync_settings",
        "sync_state",
        "system_secrets",
        "table_registry",
        "ticket",
        "tool_registry",
        "usage",
        "user_curated_subscriptions",
        "user_group_members",
        "user_groups",
        "user_journey",
        "user_stack_subscriptions",
        "user_store_installs",
        "user_workdirs",
        "users",
        "view_ownership",
        "welcome_template",
    }
)


def _duckdb_backed_keys() -> set[str]:
    return {k for k, v in factory._REGISTRY.items() if factory.DUCKDB in v}


def test_no_new_duckdb_backend_registry_key():
    """No NEW `_REGISTRY` key may register a DuckDB backend. New app-state
    repos are Postgres-only — see `docs/migrations.md` -> "Adding a PG-only
    feature"."""
    new = sorted(_duckdb_backed_keys() - _FROZEN_DUCKDB_REGISTRY_KEYS)
    assert not new, (
        "new _REGISTRY key(s) with a DuckDB backend -- DuckDB app-state is "
        "frozen (CLAUDE.md -> 'Dual-backend discipline'); register new "
        "app-state repos Postgres-only:\n  " + "\n  ".join(new)
    )


def test_frozen_duckdb_registry_keys_has_no_stale_entries():
    """Every frozen key must still carry a DuckDB backend — once A4 deletes
    a pair's DuckDB half, delete its key here too so the ratchet stays
    honest."""
    stale = sorted(_FROZEN_DUCKDB_REGISTRY_KEYS - _duckdb_backed_keys())
    assert not stale, (
        "stale frozen-DuckDB-registry-key entries -- these repos no longer "
        "register a DuckDB backend; delete them from "
        "_FROZEN_DUCKDB_REGISTRY_KEYS:\n  " + "\n  ".join(stale)
    )


def test_frozen_list_matches_the_registry_exactly_today():
    """The frozen list is a snapshot, not a derived value — this pins it
    equal to today's registry so drift in either direction (a key silently
    added AND recorded here, or a key removed but not deleted here) is
    visible as a single, unambiguous failure."""
    assert _FROZEN_DUCKDB_REGISTRY_KEYS == _duckdb_backed_keys()


# ---------------------------------------------------------------------------
# meta-tests (negative controls, both directions) — prove the ratchet logic
# actually distinguishes the sanctioned post-A3 shape from the frozen one.
# ---------------------------------------------------------------------------


def test_meta_planted_new_duckdb_backend_key_is_flagged():
    """A branch that adds a brand-new `_REGISTRY` key with a DuckDB backend
    (even a fully symmetric, well-formed pair) must be caught."""
    planted = dict(factory._REGISTRY)
    planted["_planted_widget"] = {
        factory.DUCKDB: ("src.repositories.widgets", "WidgetRepository"),
        factory.PG: ("src.repositories.widgets_pg", "WidgetPgRepository"),
    }
    has_duckdb = {k for k, v in planted.items() if factory.DUCKDB in v}
    new = has_duckdb - _FROZEN_DUCKDB_REGISTRY_KEYS
    assert new == {"_planted_widget"}


def test_meta_planted_pg_only_key_passes():
    """A branch that adds a brand-new `_REGISTRY` key with ONLY a Postgres
    backend must NOT be caught — this is the sanctioned post-A3 shape."""
    planted = dict(factory._REGISTRY)
    planted["_planted_widget"] = {
        factory.PG: ("src.repositories.widgets_pg", "WidgetPgRepository"),
    }
    has_duckdb = {k for k, v in planted.items() if factory.DUCKDB in v}
    assert "_planted_widget" not in (has_duckdb - _FROZEN_DUCKDB_REGISTRY_KEYS)
