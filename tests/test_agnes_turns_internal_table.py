"""``agnes_turns`` — the internal projection of ``usage_turns`` — is a
Postgres-only internal table.

Plan: ``docs/superpowers/plans/2026-08-31-usage-package-per-turn-tokens.md``
Task 9; design: ``docs/superpowers/specs/2026-08-31-usage-package-and-
per-turn-tokens-design.md`` §3.

``usage_turns`` arrived after the A3 freeze, so it exists on the Postgres
app-state backend only (Alembic revision ``0094_usage_turns``, no
``src/db.py`` ladder step). Its internal projection therefore must not be
registered on a DuckDB-backed instance: a ``table_registry`` row there would
point at a table that does not exist — the "broken row" this file pins
against. Everything backend-independent (the declaration, the SQL scanner)
plus the whole DuckDB-backend behaviour lives here; the Postgres end-to-end
proof needs a real PG server and lives in
``tests/db_pg/test_agnes_turns_internal_table_pg.py``.

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

TURNS_ID = "agnes_turns"
ANALYST_ID = "analyst1"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _boot() -> None:
    """One startup pass — exactly what ``app/main.py`` does."""
    newly_registered = ensure_internal_tables_registered()
    ensure_internal_package_seeded(newly_registered=newly_registered)


def _member_ids(pkg_id: str) -> set[str]:
    return {t["id"] for t in data_packages_repo().list_tables(pkg_id)}


def _grant_usage_package(conn, user_id: str, *, group_name: str = "turns-pkg-testers") -> str:
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


def test_agnes_turns_is_declared_in_internal_tables():
    table = INTERNAL_TABLES_BY_ID[TURNS_ID]
    assert table.source_table == "usage_turns"
    assert table.filter_column == "user_id"
    assert table.filter_kind == "user_id"
    assert table.display_name == "Agnes turns"
    # `usage_turns` has no `username` column at all (see migration 0094), so
    # there is no pre-v45 legacy identity to fall back on — the OR fallback
    # the older internal tables carry would be a Binder error here.
    assert table.legacy_username_column is None
    assert "Postgres" in table.description
    assert is_internal_table(TURNS_ID)


def test_sql_scanner_recognises_the_id_on_every_backend():
    """``_TABLE_REF_RE`` keeps its import-time build (plan Task 9 step 2), so
    the id routes into the internal branch on both backends and a DuckDB
    instance resolves it as unavailable — rather than the scanner ignoring it
    and the statement quietly reaching the analytics database."""
    assert find_internal_refs("SELECT COUNT(*) FROM agnes_turns") == [TURNS_ID]


# ---------------------------------------------------------------------------
# DuckDB backend — registered nowhere, never a broken row
# ---------------------------------------------------------------------------


def test_duckdb_boot_does_not_register_agnes_turns(booted):
    repo = table_registry_repo()
    assert repo.get(TURNS_ID) is None, "usage_turns does not exist on the DuckDB app-state backend"
    # The three pre-A3 internal tables are unaffected.
    assert repo.get("agnes_sessions") is not None
    assert repo.get("agnes_telemetry") is not None
    assert repo.get("agnes_audit") is not None


def test_duckdb_boot_leaves_agnes_turns_out_of_the_package(booted):
    pkg = data_packages_repo().get_by_slug(USAGE_PACKAGE_SLUG)
    assert pkg is not None
    members = _member_ids(pkg["id"])
    assert TURNS_ID not in members
    assert "agnes_sessions" in members


def test_duckdb_boot_prunes_a_pre_existing_agnes_turns_row(seeded_app):
    """Never a broken row: an id registered while the instance ran on
    Postgres (or by a hand-edit) is evicted on the next DuckDB boot instead of
    lingering in /catalog pointing at a table that is not there."""
    table_registry_repo().register(
        id=TURNS_ID,
        name="Agnes turns",
        description="stale",
        source_type="internal",
        bucket="Agnes Internal",
        source_table="usage_turns",
        query_mode="internal",
        profile_after_sync=False,
        registered_by="test",
    )
    assert table_registry_repo().get(TURNS_ID) is not None

    _boot()

    assert table_registry_repo().get(TURNS_ID) is None


def test_duckdb_catalog_omits_agnes_turns_even_with_the_package(booted):
    conn = get_system_db()
    try:
        _grant_usage_package(conn, ANALYST_ID)
    finally:
        conn.close()

    resp = booted["client"].get("/api/v2/catalog", headers=_auth(booted["analyst_token"]))
    assert resp.status_code == 200, resp.text
    ids = {t["id"] for t in resp.json()["tables"]}
    assert "agnes_sessions" in ids, "the grant must still surface the tables that DO exist here"
    assert TURNS_ID not in ids


def test_duckdb_query_fails_clean_without_the_package(booted):
    """The unregistered id must produce the ordinary table-not-available
    error, never an unhandled 500 from a CTE over a missing source table."""
    resp = booted["client"].post(
        "/api/query",
        json={"sql": "SELECT COUNT(*) FROM agnes_turns"},
        headers=_auth(booted["analyst_token"]),
    )
    assert resp.status_code != 500, resp.text
    assert resp.status_code == 403, resp.text
    assert TURNS_ID in resp.json()["detail"]


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
        json={"sql": "SELECT COUNT(*) FROM agnes_turns"},
        headers=_auth(booted["analyst_token"]),
    )
    assert resp.status_code != 500, resp.text
    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    assert TURNS_ID in detail
    assert USAGE_PACKAGE_SLUG not in detail, f"the package cannot carry {TURNS_ID} on this backend: {detail}"
    assert "Postgres" in detail


# ---------------------------------------------------------------------------
# The gate keys on the ACTIVE backend, not on a build-time constant
# ---------------------------------------------------------------------------


def test_registration_follows_use_pg(seeded_app, monkeypatch):
    """The registrable set is decided by ``use_pg()`` at boot.

    Driven here against the DuckDB store with ``use_pg`` forced True, which
    isolates the decision itself; the real Postgres proof (rows, package
    membership, RBAC-scoped ``/api/query``) is
    ``tests/db_pg/test_agnes_turns_internal_table_pg.py``.
    """
    monkeypatch.setattr("connectors.internal.registry.use_pg", lambda: True)

    _boot()

    assert table_registry_repo().get(TURNS_ID) is not None
    pkg = data_packages_repo().get_by_slug(USAGE_PACKAGE_SLUG)
    assert TURNS_ID in _member_ids(pkg["id"])
