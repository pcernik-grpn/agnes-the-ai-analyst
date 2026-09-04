"""``agnes_extraction_runs`` / ``agnes_facts_ingest_runs`` — the internal
projections of ``extraction_runs`` and ``facts_ingest_runs`` — are
Postgres-only, ADMIN-ONLY internal tables in the seeded ``agnes-usage``
package.

Design: the extraction/fact-ingest pipelines' own operational history has no
per-user owner column — a run is keyed on a data-source connection or a set
of collections, never on a person — so unlike ``agnes_sessions`` /
``agnes_telemetry`` / ``agnes_audit`` / ``agnes_turns`` there is no "your own
rows" model to apply. ``filter_kind='admin_only'`` makes every non-admin
caller see zero rows regardless of the package grant; the grant still
decides whether the table is visible at all (same package, same mechanism as
every other internal table).

Both source tables arrived after the A3 freeze (Alembic ``0094_extraction_
runs`` / ``0078_facts_ingest_runs``, no ``src/db.py`` ladder step), so —
exactly like ``agnes_turns`` — their internal projections must not be
registered on a DuckDB-backed instance: a ``table_registry`` row there would
point at a table that does not exist. Everything backend-independent (the
declaration, the SQL scanner, the admin-only filter clause) plus the whole
DuckDB-backend "never registered" story lives here; the Postgres end-to-end
proof (rows, zero-rows-for-non-admin, admin sees all) needs a real PG server
and lives in
``tests/db_pg/test_agnes_extraction_usage_tables_pg.py``.

Callers are NON-ADMIN throughout except where a test's own subject is the
admin view — Admin is a god-mode short-circuit on every access check, so a
visibility test driven by an admin asserts nothing.
"""

from __future__ import annotations

import pytest

from connectors.internal.access import (
    INTERNAL_TABLES_BY_ID,
    build_filter_clause,
    find_internal_refs,
    is_internal_table,
)
from connectors.internal.registry import (
    USAGE_PACKAGE_SLUG,
    ensure_internal_package_seeded,
    ensure_internal_tables_registered,
)
from src.db import get_system_db
from src.repositories import data_packages_repo, table_registry_repo

EXTRACTION_ID = "agnes_extraction_runs"
INGEST_ID = "agnes_facts_ingest_runs"
ADMIN_ONLY_IDS = (EXTRACTION_ID, INGEST_ID)
ANALYST_ID = "analyst1"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _boot() -> None:
    """One startup pass — exactly what ``app/main.py`` does."""
    newly_registered = ensure_internal_tables_registered()
    ensure_internal_package_seeded(newly_registered=newly_registered)


def _member_ids(pkg_id: str) -> set[str]:
    return {t["id"] for t in data_packages_repo().list_tables(pkg_id)}


def _grant_usage_package(conn, user_id: str, *, group_name: str = "extraction-usage-pkg-testers") -> str:
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


@pytest.mark.parametrize(
    "registry_id,source_table",
    [(EXTRACTION_ID, "extraction_runs"), (INGEST_ID, "facts_ingest_runs")],
)
def test_declared_in_internal_tables(registry_id, source_table):
    table = INTERNAL_TABLES_BY_ID[registry_id]
    assert table.source_table == source_table
    # No per-row owner column exists on either physical table.
    assert table.filter_column is None
    assert table.filter_kind == "admin_only"
    assert table.legacy_username_column is None
    assert "admin" in table.description.lower()
    assert "your own" not in table.description.lower()
    assert "Postgres" in table.description
    assert is_internal_table(registry_id)


def test_sql_scanner_recognises_both_ids_on_every_backend():
    """``_TABLE_REF_RE`` keeps its import-time build, so both ids route into
    the internal branch on both backends and a DuckDB instance resolves them
    as unavailable — rather than the scanner ignoring them and the statement
    quietly reaching the analytics database."""
    assert find_internal_refs("SELECT COUNT(*) FROM agnes_extraction_runs") == [EXTRACTION_ID]
    assert find_internal_refs("SELECT COUNT(*) FROM agnes_facts_ingest_runs") == [INGEST_ID]


# ---------------------------------------------------------------------------
# The admin-only row filter — backend-independent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("registry_id", ADMIN_ONLY_IDS)
def test_admin_gets_unscoped_view(registry_id):
    table = INTERNAL_TABLES_BY_ID[registry_id]
    assert build_filter_clause(table, {"email": "admin@x", "id": "admin-uuid"}, True) == ""


@pytest.mark.parametrize("registry_id", ADMIN_ONLY_IDS)
def test_non_admin_gets_where_false_regardless_of_identity(registry_id):
    """No value the caller carries can widen this — an admin_only table's
    non-admin clause is unconditional, not merely "matches nothing today"."""
    table = INTERNAL_TABLES_BY_ID[registry_id]
    clause = build_filter_clause(table, {"email": "alice@example.com", "id": "alice-uuid"}, False)
    assert clause == "WHERE FALSE"


@pytest.mark.parametrize("registry_id", ADMIN_ONLY_IDS)
def test_non_admin_where_false_even_with_an_unsafe_identity(registry_id):
    """The admin_only short-circuit happens BEFORE `_filter_value` runs, so
    an identity that would fail the username/user_id safety regex never even
    reaches that check — there is nothing to interpolate."""
    table = INTERNAL_TABLES_BY_ID[registry_id]
    clause = build_filter_clause(table, {"email": "x@example.com", "id": "'; DROP TABLE--"}, False)
    assert clause == "WHERE FALSE"


# ---------------------------------------------------------------------------
# DuckDB backend — registered nowhere, never a broken row
# ---------------------------------------------------------------------------


def test_duckdb_boot_does_not_register_either_table(booted):
    repo = table_registry_repo()
    assert repo.get(EXTRACTION_ID) is None, "extraction_runs does not exist on the DuckDB app-state backend"
    assert repo.get(INGEST_ID) is None, "facts_ingest_runs does not exist on the DuckDB app-state backend"
    # The pre-A3 internal tables are unaffected.
    assert repo.get("agnes_sessions") is not None
    assert repo.get("agnes_telemetry") is not None
    assert repo.get("agnes_audit") is not None


def test_duckdb_boot_leaves_both_out_of_the_package(booted):
    pkg = data_packages_repo().get_by_slug(USAGE_PACKAGE_SLUG)
    assert pkg is not None
    members = _member_ids(pkg["id"])
    assert EXTRACTION_ID not in members
    assert INGEST_ID not in members
    assert "agnes_sessions" in members


@pytest.mark.parametrize(
    "registry_id,source_table", [(EXTRACTION_ID, "extraction_runs"), (INGEST_ID, "facts_ingest_runs")]
)
def test_duckdb_boot_prunes_a_pre_existing_row(seeded_app, registry_id, source_table):
    """Never a broken row: an id registered while the instance ran on
    Postgres (or by a hand-edit) is evicted on the next DuckDB boot instead of
    lingering in /catalog pointing at a table that is not there."""
    table_registry_repo().register(
        id=registry_id,
        name=INTERNAL_TABLES_BY_ID[registry_id].display_name,
        description="stale",
        source_type="internal",
        bucket="Agnes Internal",
        source_table=source_table,
        query_mode="internal",
        profile_after_sync=False,
        registered_by="test",
    )
    assert table_registry_repo().get(registry_id) is not None

    _boot()

    assert table_registry_repo().get(registry_id) is None


def test_duckdb_catalog_omits_both_even_with_the_package(booted):
    conn = get_system_db()
    try:
        _grant_usage_package(conn, ANALYST_ID)
    finally:
        conn.close()

    resp = booted["client"].get("/api/v2/catalog", headers=_auth(booted["analyst_token"]))
    assert resp.status_code == 200, resp.text
    ids = {t["id"] for t in resp.json()["tables"]}
    assert "agnes_sessions" in ids, "the grant must still surface the tables that DO exist here"
    assert EXTRACTION_ID not in ids
    assert INGEST_ID not in ids


@pytest.mark.parametrize("registry_id", ADMIN_ONLY_IDS)
def test_duckdb_query_fails_clean_without_the_package(booted, registry_id):
    """The unregistered id must produce the ordinary table-not-available
    error, never an unhandled 500 from a CTE over a missing source table."""
    resp = booted["client"].post(
        "/api/query",
        json={"sql": f"SELECT COUNT(*) FROM {registry_id}"},
        headers=_auth(booted["analyst_token"]),
    )
    assert resp.status_code != 500, resp.text
    assert resp.status_code == 403, resp.text
    assert registry_id in resp.json()["detail"]


@pytest.mark.parametrize("registry_id", ADMIN_ONLY_IDS)
def test_duckdb_query_denial_does_not_promise_a_package_that_cannot_help(booted, registry_id):
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
        json={"sql": f"SELECT COUNT(*) FROM {registry_id}"},
        headers=_auth(booted["analyst_token"]),
    )
    assert resp.status_code != 500, resp.text
    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    assert registry_id in detail
    assert USAGE_PACKAGE_SLUG not in detail, f"the package cannot carry {registry_id} on this backend: {detail}"
    assert "Postgres" in detail


@pytest.mark.parametrize("registry_id", ADMIN_ONLY_IDS)
def test_denial_message_helper_says_unavailable_not_ungranted(registry_id):
    """Pinned at the helper too, since every table gate funnels through it
    (`/api/data/*/download`, `/api/v2/sample`, `/api/v2/schema`), not only
    the `/api/query` route the tests above drive."""
    from src.rbac import table_not_in_stack_message

    msg = table_not_in_stack_message(registry_id)
    assert registry_id in msg
    assert "Postgres" in msg
    assert USAGE_PACKAGE_SLUG not in msg


# ---------------------------------------------------------------------------
# The gate keys on the ACTIVE backend, not on a build-time constant
# ---------------------------------------------------------------------------


def test_registration_follows_use_pg(seeded_app, monkeypatch):
    """The registrable set is decided by ``use_pg()`` at boot.

    Driven here against the DuckDB store with ``use_pg`` forced True, which
    isolates the decision itself; the real Postgres proof (rows, package
    membership, RBAC-scoped ``/api/query``) is
    ``tests/db_pg/test_agnes_extraction_usage_tables_pg.py``.
    """
    monkeypatch.setattr("connectors.internal.registry.use_pg", lambda: True)

    _boot()

    assert table_registry_repo().get(EXTRACTION_ID) is not None
    assert table_registry_repo().get(INGEST_ID) is not None
    pkg = data_packages_repo().get_by_slug(USAGE_PACKAGE_SLUG)
    members = _member_ids(pkg["id"])
    assert EXTRACTION_ID in members
    assert INGEST_ID in members


# ---------------------------------------------------------------------------
# agnes pull must never see these tables — verified, not assumed
# ---------------------------------------------------------------------------


def test_manifest_never_lists_either_table_for_agnes_pull(seeded_app, monkeypatch):
    """Forces the backend decision True (see `test_registration_follows_use_pg`
    above) so both ids exist in `table_registry` and are package members —
    the interesting case for "does the manifest builder skip them", since an
    unregistered id is trivially absent. `_build_data_packages_section`
    explicitly excludes every `is_internal_table` id from the typed
    `data_packages[].tables[]` section it builds (the section `agnes pull`
    reads to decide what to fetch); the flat legacy `tables` dict has no row
    for either id at all, because neither table ever gets a `sync_state` row
    (internal tables are never processed by `SyncOrchestrator`)."""
    monkeypatch.setattr("connectors.internal.registry.use_pg", lambda: True)
    _boot()

    conn = get_system_db()
    try:
        pkg_id = _grant_usage_package(conn, ANALYST_ID)
    finally:
        conn.close()
    assert {EXTRACTION_ID, INGEST_ID} <= _member_ids(pkg_id)

    resp = seeded_app["client"].get("/api/sync/manifest", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200, resp.text
    manifest = resp.json()

    assert EXTRACTION_ID not in manifest.get("tables", {})
    assert INGEST_ID not in manifest.get("tables", {})
    for pkg in manifest.get("data_packages", []):
        listed_ids = {t.get("id") for t in pkg.get("tables", [])}
        assert EXTRACTION_ID not in listed_ids
        assert INGEST_ID not in listed_ids
