"""A3 PG-first ratchet — filesystem-level guard.

The registry-level ratchet (``tests/test_repository_registry_pg_first_ratchet.py``)
only catches a repo that got wired into ``src.repositories._REGISTRY``. A new
plain DuckDB repo module dropped under ``src/repositories/`` but never
registered (against the "reach repos through the factory" convention, but not
otherwise caught by any existing guard — ``tests/test_backend_split_guard.py``'s
direct-instantiation scan only flags a class that already has a ``_pg.py``
sibling) would slip past it entirely.

This guard scans the directory itself: no NEW plain (non-``_pg.py``) module
may appear, regardless of whether it ever makes it into the registry. New
app-state repos are Postgres-only — a bare ``<name>_pg.py`` file with no
plain sibling — see ``docs/migrations.md`` -> "Adding a PG-only feature".
"""

from __future__ import annotations

from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[2] / "src" / "repositories"

# Infra/mixin modules that are not themselves a DuckDB repo implementation.
_NON_REPO_INFRA = {"__init__.py", "audit_protocol.py", "_orchestration_mixins.py"}

# ---------------------------------------------------------------------------
# the frozen list — every plain (non-`_pg.py`) module stem under
# src/repositories/ the day the A3 ratchet flipped (main, 2026-08-26). Shrink
# only, as A4 deletes a module outright; never grow.
#
# `cli_auth_codes` is pre-existing DuckDB-only debt (no PG sibling at all,
# tracked for the A4 cleanup per the remediation-program plan) — it predates
# the ratchet, so it is grandfathered here rather than treated as new
# surface.
# ---------------------------------------------------------------------------
_FROZEN_DUCKDB_REPO_MODULES: frozenset[str] = frozenset(
    {
        "access_tokens",
        "agent_artifacts",
        "agent_memories",
        "agent_schedules",
        "agent_webhooks",
        "agents",
        "audit",
        "authoring_suggestions",
        "bq_metadata_cache",
        "claude_md_template",
        "cli_auth_codes",
        "column_metadata",
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
        "metrics",
        "news_template",
        "notifications",
        "oauth_clients",
        "observability_views",
        "profiles",
        "recipes",
        "reports",
        "resource_grants",
        "semantic_models",
        "semantic_sources",
        "session_processor_state",
        "setup_tokens",
        "source_connections",
        "store_entities",
        "store_entity_votes",
        "store_lint",
        "store_submissions",
        "sync_settings",
        "sync_state",
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
        "users",
        "view_ownership",
        "welcome_template",
    }
)


def _duckdb_repo_module_stems() -> set[str]:
    return {p.stem for p in REPO_DIR.glob("*.py") if p.name not in _NON_REPO_INFRA and not p.name.endswith("_pg.py")}


def test_no_new_duckdb_repo_module():
    """No NEW plain ``src/repositories/<name>.py`` module may appear — DuckDB
    app-state is frozen (CLAUDE.md -> "Dual-backend discipline")."""
    new = sorted(_duckdb_repo_module_stems() - _FROZEN_DUCKDB_REPO_MODULES)
    assert not new, (
        "new DuckDB repository module(s) under src/repositories/ -- DuckDB "
        "app-state is frozen; add a Postgres-only `<name>_pg.py` module "
        "(registered PG-only in src.repositories._REGISTRY) instead -- see "
        "docs/migrations.md -> 'Adding a PG-only feature':\n  " + "\n  ".join(new)
    )


def test_frozen_duckdb_repo_modules_has_no_stale_entries():
    """Every frozen stem must still exist as a plain module — once a module
    is deleted outright (A4), delete its entry here too."""
    stale = sorted(_FROZEN_DUCKDB_REPO_MODULES - _duckdb_repo_module_stems())
    assert not stale, (
        "stale frozen-DuckDB-module entries -- delete them from _FROZEN_DUCKDB_REPO_MODULES:\n  " + "\n  ".join(stale)
    )


def test_frozen_list_matches_the_directory_exactly_today():
    """Snapshot check: the frozen list equals today's directory contents
    exactly, so drift in either direction is a single unambiguous failure."""
    assert _FROZEN_DUCKDB_REPO_MODULES == _duckdb_repo_module_stems()


# ---------------------------------------------------------------------------
# meta-tests (negative controls, both directions)
# ---------------------------------------------------------------------------


def test_meta_planted_new_duckdb_module_is_flagged():
    """A branch that drops a brand-new plain repo module under
    src/repositories/ must be caught."""
    stems = _duckdb_repo_module_stems() | {"_planted_widget"}
    new = stems - _FROZEN_DUCKDB_REPO_MODULES
    assert new == {"_planted_widget"}


def test_meta_pg_only_module_addition_passes():
    """A branch that drops ONLY `<name>_pg.py` (no plain sibling) must NOT be
    caught — the scan filters out `_pg.py` files by construction, so a
    PG-only addition never contributes a new stem in the first place."""
    stems = _duckdb_repo_module_stems()
    assert "_planted_widget" not in stems
