"""``agnes_llm_calls`` — the internal projection of ``llm_calls`` — is a
Postgres-only internal table.

Plan: ``docs/superpowers/plans/2026-09-08-llm-observability.md`` Task 6;
design: ``docs/superpowers/specs/2026-09-08-llm-observability-design.md``
§3.4, §3.7. Modelled on ``tests/test_agnes_turns_internal_table.py`` (same
pattern established for ``agnes_turns``).

``llm_calls`` arrived after the A3 freeze, so it exists on the Postgres
app-state backend only (Alembic revision ``0115_llm_observability``, no
``src/db.py`` ladder step). Its internal projection therefore must not be
registered on a DuckDB-backed instance: a ``table_registry`` row there would
point at a table that does not exist — the "broken row" this file pins
against. Everything backend-independent (the declaration, the SQL scanner)
plus the whole DuckDB-backend behaviour lives here.

Callers are NON-ADMIN throughout — Admin is a god-mode short-circuit on every
access check, so a visibility test driven by an admin asserts nothing.
"""

from __future__ import annotations

import pytest

from connectors.internal.access import INTERNAL_TABLES_BY_ID, find_internal_refs, is_internal_table
from connectors.internal.registry import (
    USAGE_PACKAGE_SLUG,
    ensure_internal_package_seeded,
    ensure_internal_tables_registered,
)
from src.db import get_system_db
from src.repositories import data_packages_repo, table_registry_repo

LLM_CALLS_ID = "agnes_llm_calls"
ANALYST_ID = "analyst1"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _boot() -> None:
    """One startup pass — exactly what ``app/main.py`` does."""
    newly_registered = ensure_internal_tables_registered()
    ensure_internal_package_seeded(newly_registered=newly_registered)


def _member_ids(pkg_id: str) -> set[str]:
    return {t["id"] for t in data_packages_repo().list_tables(pkg_id)}


def _grant_usage_package(conn, user_id: str, *, group_name: str = "llmcalls-pkg-testers") -> str:
    """Put the seeded ``agnes-usage`` package in *user_id*'s stack — what an
    admin does at /admin/access."""
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    pkg = data_packages_repo().get_by_slug(USAGE_PACKAGE_SLUG)
    assert pkg is not None, "the agnes-usage package must be seeded first"

    groups = UserGroupsRepository(conn)
    grp = groups.get_by_name(group_name) or groups.create(name=group_name, description="test", created_by="test")
    members = UserGroupMembersRepository(conn)
    if not members.has_membership(user_id, grp["id"]):
        members.add_member(user_id, grp["id"], source="admin", added_by="test")
    grants = ResourceGrantsRepository(conn)
    if not grants.has_grant([grp["id"]], "data_package", pkg["id"]):
        grants.create(
            group_id=grp["id"],
            resource_type="data_package",
            resource_id=pkg["id"],
            assigned_by="test",
            requirement="required",
        )
    return pkg["id"]


@pytest.fixture
def booted(seeded_app):
    """Seeded app + one boot pass of the internal seed.

    ``seeded_app``'s TestClient never runs the ASGI lifespan, so the boot-time
    seeding ``app/main.py`` performs is replayed explicitly.
    """
    _boot()
    return seeded_app


# ---------------------------------------------------------------------------
# Declaration — backend-independent
# ---------------------------------------------------------------------------


def test_agnes_llm_calls_is_declared_in_internal_tables():
    table = INTERNAL_TABLES_BY_ID[LLM_CALLS_ID]
    assert table.source_table == "llm_calls"
    assert table.filter_column == "user_id"
    assert table.filter_kind == "user_id"
    assert table.display_name == "Agnes LLM calls"
    # `llm_calls` has no `username` column at all (migration 0115), so there
    # is no pre-v45 legacy identity to fall back on — the OR fallback the
    # older internal tables carry would be a Binder error here.
    assert table.legacy_username_column is None
    assert "Postgres" in table.description
    assert "your own" in table.description.lower() or "own rows" in table.description.lower()
    assert is_internal_table(LLM_CALLS_ID)


def test_sql_scanner_recognises_the_id_on_every_backend():
    """``_TABLE_REF_RE`` keeps its import-time build, so the id routes into
    the internal branch on both backends and a DuckDB instance resolves it
    as unavailable — rather than the scanner ignoring it and the statement
    quietly reaching the analytics database."""
    assert find_internal_refs("SELECT COUNT(*) FROM agnes_llm_calls") == [LLM_CALLS_ID]


# ---------------------------------------------------------------------------
# DuckDB backend — registered nowhere, never a broken row
# ---------------------------------------------------------------------------


def test_duckdb_boot_does_not_register_agnes_llm_calls(booted):
    repo = table_registry_repo()
    assert repo.get(LLM_CALLS_ID) is None, "llm_calls does not exist on the DuckDB app-state backend"
    # The three pre-A3 internal tables are unaffected.
    assert repo.get("agnes_sessions") is not None
    assert repo.get("agnes_telemetry") is not None
    assert repo.get("agnes_audit") is not None


def test_duckdb_boot_leaves_agnes_llm_calls_out_of_the_package(booted):
    pkg = data_packages_repo().get_by_slug(USAGE_PACKAGE_SLUG)
    assert pkg is not None
    members = _member_ids(pkg["id"])
    assert LLM_CALLS_ID not in members
    assert "agnes_sessions" in members


def test_duckdb_boot_prunes_a_pre_existing_agnes_llm_calls_row(seeded_app):
    """Never a broken row: an id registered while the instance ran on
    Postgres (or by a hand-edit) is evicted on the next DuckDB boot instead of
    lingering in /catalog pointing at a table that is not there."""
    table_registry_repo().register(
        id=LLM_CALLS_ID,
        name="Agnes LLM calls",
        description="stale",
        source_type="internal",
        bucket="Agnes Internal",
        source_table="llm_calls",
        query_mode="internal",
        profile_after_sync=False,
        registered_by="test",
    )
    assert table_registry_repo().get(LLM_CALLS_ID) is not None

    _boot()

    assert table_registry_repo().get(LLM_CALLS_ID) is None


def test_duckdb_catalog_omits_agnes_llm_calls_even_with_the_package(booted):
    conn = get_system_db()
    try:
        _grant_usage_package(conn, ANALYST_ID)
    finally:
        conn.close()

    resp = booted["client"].get("/api/v2/catalog", headers=_auth(booted["analyst_token"]))
    assert resp.status_code == 200, resp.text
    ids = {t["id"] for t in resp.json()["tables"]}
    assert "agnes_sessions" in ids, "the grant must still surface the tables that DO exist here"
    assert LLM_CALLS_ID not in ids


def test_duckdb_query_fails_clean_without_the_package(booted):
    """The unregistered id must produce the ordinary table-not-available
    error, never an unhandled 500 from a CTE over a missing source table."""
    resp = booted["client"].post(
        "/api/query",
        json={"sql": "SELECT COUNT(*) FROM agnes_llm_calls"},
        headers=_auth(booted["analyst_token"]),
    )
    assert resp.status_code != 500, resp.text
    assert resp.status_code == 403, resp.text
    assert LLM_CALLS_ID in resp.json()["detail"]


def test_duckdb_query_denial_does_not_promise_a_package_that_cannot_help(booted):
    """With the package already granted the generic stack denial ("ask an
    admin to grant you 'agnes-usage'") would name a step the analyst has
    already taken and that could never work here — the package does not carry
    this table on a DuckDB instance. Say it is unavailable instead."""
    conn = get_system_db()
    try:
        _grant_usage_package(conn, ANALYST_ID)
    finally:
        conn.close()

    resp = booted["client"].post(
        "/api/query",
        json={"sql": "SELECT COUNT(*) FROM agnes_llm_calls"},
        headers=_auth(booted["analyst_token"]),
    )
    assert resp.status_code != 500, resp.text
    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    assert LLM_CALLS_ID in detail
    assert USAGE_PACKAGE_SLUG not in detail, f"the package cannot carry {LLM_CALLS_ID} on this backend: {detail}"
    assert "Postgres" in detail


def test_denial_message_helper_says_unavailable_not_ungranted():
    """Pinned at the helper too, since every table gate funnels through it
    (`/api/data/*/download`, `/api/v2/sample`, `/api/v2/schema`), not only
    the `/api/query` route the tests above drive."""
    from src.rbac import table_not_in_stack_message

    msg = table_not_in_stack_message(LLM_CALLS_ID)
    assert LLM_CALLS_ID in msg
    assert "Postgres" in msg
    assert USAGE_PACKAGE_SLUG not in msg
    # The tables that DO exist here keep the package wording.
    assert USAGE_PACKAGE_SLUG in table_not_in_stack_message("agnes_sessions")


# ---------------------------------------------------------------------------
# The gate keys on the ACTIVE backend, not on a build-time constant
# ---------------------------------------------------------------------------


def test_registration_follows_use_pg(seeded_app, monkeypatch):
    """The registrable set is decided by ``use_pg()`` at boot.

    Driven here against the DuckDB store with ``use_pg`` forced True, which
    isolates the decision itself."""
    monkeypatch.setattr("connectors.internal.registry.use_pg", lambda: True)

    _boot()

    assert table_registry_repo().get(LLM_CALLS_ID) is not None
    pkg = data_packages_repo().get_by_slug(USAGE_PACKAGE_SLUG)
    assert LLM_CALLS_ID in _member_ids(pkg["id"])
