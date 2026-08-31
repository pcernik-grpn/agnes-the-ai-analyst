"""``agnes_turns`` end-to-end on a real Postgres backend.

Companion of ``tests/test_agnes_turns_internal_table.py`` (declaration + the
whole DuckDB-backend story, which needs no PG server). Everything here needs
rows in an actual ``usage_turns`` table, so it lives beside the other
backend-parametrized suites: ``seeded_app_both`` builds the app AFTER the
backend env is set, and runs each test once per backend.

What this pins:

* the id is registered and joins the seeded ``agnes-usage`` package on
  Postgres — and on Postgres only (the ``duck`` param is the negative half of
  the same assertion);
* the generic internal-query materializer serves the new table with NO
  per-table code: the source read goes through the PG engine, because
  ``usage_turns`` exists nowhere else;
* the RBAC story is the same as for every other internal table — package
  membership decides visibility, the row filter decides scope, and the two
  never trade places.

Callers are NON-ADMIN except in the one test whose subject is the admin's
unscoped view.
"""

from __future__ import annotations

import pytest

from connectors.internal.registry import (
    USAGE_PACKAGE_SLUG,
    ensure_internal_package_seeded,
    ensure_internal_tables_registered,
)
from src.repositories import (
    data_packages_repo,
    resource_grants_repo,
    table_registry_repo,
    user_group_members_repo,
    user_groups_repo,
    usage_turns_repo,
)

TURNS_ID = "agnes_turns"
ANALYST_ID = "analyst1"
OTHER_ID = "colleague1"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _boot() -> None:
    """One startup pass — ``seeded_app_both``'s client never runs the ASGI
    lifespan, so what ``app/main.py`` does at boot is replayed here."""
    ensure_internal_package_seeded(newly_registered=ensure_internal_tables_registered())


def _member_ids(pkg_id: str) -> set[str]:
    return {t["id"] for t in data_packages_repo().list_tables(pkg_id)}


def _seed_turns() -> None:
    """Two turns for the analyst, one for a colleague.

    The colleague's row is what makes own-rows-only assertable: a caller who
    can reach the table must still not see someone else's tokens.
    """
    usage_turns_repo().insert_batch(
        [
            {
                "session_file": f"{ANALYST_ID}/s1.jsonl",
                "session_id": "s1",
                "user_id": ANALYST_ID,
                "turn_uuid": "t1",
                "model": "model-a",
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_read_tokens": 30,
                "cache_creation_tokens": 4,
            },
            {
                "session_file": f"{ANALYST_ID}/s1.jsonl",
                "session_id": "s1",
                "user_id": ANALYST_ID,
                "turn_uuid": "t2",
                "model": "model-a",
                "input_tokens": 1,
                "output_tokens": 2,
            },
            {
                "session_file": f"{OTHER_ID}/s9.jsonl",
                "session_id": "s9",
                "user_id": OTHER_ID,
                "turn_uuid": "t9",
                "model": "model-b",
                "input_tokens": 500,
                "output_tokens": 500,
            },
        ]
    )


def _grant_usage_package(user_id: str, *, group_name: str = "turns-pkg-testers") -> str:
    """Put the seeded ``agnes-usage`` package in *user_id*'s stack, through
    the repository factory so the helper works on either backend."""
    pkg = data_packages_repo().get_by_slug(USAGE_PACKAGE_SLUG)
    assert pkg is not None, "the agnes-usage package must be seeded first"

    groups = user_groups_repo()
    grp = groups.get_by_name(group_name) or groups.create(name=group_name, description="test", created_by="test")
    members = user_group_members_repo()
    if not members.has_membership(user_id, grp["id"]):
        members.add_member(user_id, grp["id"], source="admin", added_by="test")
    grants = resource_grants_repo()
    if not grants.has_grant([grp["id"]], "data_package", pkg["id"]):
        grants.create(
            group_id=grp["id"],
            resource_type="data_package",
            resource_id=pkg["id"],
            assigned_by="test",
            requirement="required",
        )
    return pkg["id"]


def _query(app, token: str, sql: str):
    return app["client"].post("/api/query", json={"sql": sql}, headers=_auth(token))


# ---------------------------------------------------------------------------
# Registration + package membership — both backends, opposite outcomes
# ---------------------------------------------------------------------------


def test_registered_on_pg_only(seeded_app_both, state_backend):
    _boot()
    row = table_registry_repo().get(TURNS_ID)
    if state_backend == "pg":
        assert row is not None, "usage_turns exists here, so the projection must be registered"
        assert row["source_type"] == "internal"
        assert row["source_table"] == "usage_turns"
        assert row["query_mode"] == "internal"
    else:
        assert row is None, "a registry row would advertise a table this backend does not have"


def test_joins_the_usage_package_on_pg_only(seeded_app_both, state_backend):
    """Task 7's add-once reconciliation keys off first-time registry
    insertion, so a PG boot picks the new id up exactly once."""
    _boot()
    pkg = data_packages_repo().get_by_slug(USAGE_PACKAGE_SLUG)
    assert pkg is not None
    members = _member_ids(pkg["id"])
    # The pre-A3 tables are members on both backends — this is a narrowing
    # of the new id only, not of the package.
    assert "agnes_sessions" in members
    assert (TURNS_ID in members) is (state_backend == "pg")


def test_upgrade_boot_adds_it_to_an_existing_package(seeded_app_both, state_backend, monkeypatch):
    """The realistic path: the package already exists from an earlier release
    (or from this instance's DuckDB past), and ``agnes_turns`` shows up for
    the first time on a Postgres boot. Task 7's add-once rule keys on the
    boot that first inserts the ``table_registry`` row, so it must join then
    — exactly once, without disturbing the existing members."""
    if state_backend != "pg":
        pytest.skip("the id is never registered on DuckDB, so there is no upgrade boot")
    from connectors.internal.access import INTERNAL_TABLES

    older_release = tuple(t for t in INTERNAL_TABLES if t.registry_id != TURNS_ID)
    monkeypatch.setattr("connectors.internal.registry.INTERNAL_TABLES", older_release)
    _boot()
    pkg_id = data_packages_repo().get_by_slug(USAGE_PACKAGE_SLUG)["id"]
    assert TURNS_ID not in _member_ids(pkg_id)

    # Restore by re-patching, never `monkeypatch.undo()`: this monkeypatch
    # instance also holds `seeded_app_both`'s env setup (DATA_DIR,
    # AGNES_DB_URL), and undoing that mid-test would move the app off
    # Postgres.
    monkeypatch.setattr("connectors.internal.registry.INTERNAL_TABLES", INTERNAL_TABLES)
    _boot()

    assert TURNS_ID in _member_ids(pkg_id)
    assert {"agnes_sessions", "agnes_telemetry", "agnes_audit"} <= _member_ids(pkg_id)


# ---------------------------------------------------------------------------
# /api/query — visibility from the package, scope from the row filter
# ---------------------------------------------------------------------------


def test_non_admin_without_the_package_is_denied(seeded_app_both, state_backend):
    if state_backend != "pg":
        pytest.skip("the DuckDB half lives in tests/test_agnes_turns_internal_table.py")
    _boot()
    _seed_turns()

    resp = _query(seeded_app_both, seeded_app_both["analyst_token"], f"SELECT COUNT(*) FROM {TURNS_ID}")

    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    assert TURNS_ID in detail
    assert USAGE_PACKAGE_SLUG in detail, "the denial must name the package that would help"


def test_non_admin_with_the_package_sees_own_turns_only(seeded_app_both, state_backend):
    if state_backend != "pg":
        pytest.skip("usage_turns exists on Postgres only")
    _boot()
    _seed_turns()
    _grant_usage_package(ANALYST_ID)

    resp = _query(
        seeded_app_both,
        seeded_app_both["analyst_token"],
        f"SELECT turn_uuid FROM {TURNS_ID} ORDER BY turn_uuid",
    )

    assert resp.status_code == 200, resp.text
    assert [r[0] for r in resp.json()["rows"]] == ["t1", "t2"], "the colleague's turn must not leak"


def test_package_grant_does_not_widen_the_token_totals(seeded_app_both, state_backend):
    """The numbers a grantee reads are their own: 11 input / 22 output, not
    the instance-wide 511 / 522."""
    if state_backend != "pg":
        pytest.skip("usage_turns exists on Postgres only")
    _boot()
    _seed_turns()
    _grant_usage_package(ANALYST_ID)

    resp = _query(
        seeded_app_both,
        seeded_app_both["analyst_token"],
        f"SELECT SUM(input_tokens), SUM(output_tokens), SUM(cache_read_tokens) FROM {TURNS_ID}",
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["rows"][0][:3] == [11, 22, 30]


def test_admin_sees_every_turn_unscoped(seeded_app_both, state_backend):
    """The one admin-driven case: god-mode needs no package and the row
    filter stays empty."""
    if state_backend != "pg":
        pytest.skip("usage_turns exists on Postgres only")
    _boot()
    _seed_turns()

    resp = _query(seeded_app_both, seeded_app_both["admin_token"], f"SELECT COUNT(*) FROM {TURNS_ID}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["rows"][0][0] == 3


def test_catalog_lists_it_for_a_grantee(seeded_app_both, state_backend):
    if state_backend != "pg":
        pytest.skip("usage_turns exists on Postgres only")
    _boot()
    _grant_usage_package(ANALYST_ID)

    resp = seeded_app_both["client"].get("/api/v2/catalog", headers=_auth(seeded_app_both["analyst_token"]))

    assert resp.status_code == 200, resp.text
    assert TURNS_ID in {t["id"] for t in resp.json()["tables"]}


# ---------------------------------------------------------------------------
# The generic materializer needs no per-table code
# ---------------------------------------------------------------------------


def test_materializer_reads_usage_turns_through_the_pg_engine(seeded_app_both, state_backend):
    """``usage_turns`` exists ONLY in Postgres, so a non-admin query over it
    is proof that the source read runs on the PG engine — a DuckDB-only
    source-read branch would come back empty (or raise a Catalog error)
    rather than with the caller's two turns."""
    if state_backend != "pg":
        pytest.skip("the assertion is about the Postgres source read")
    from connectors.internal.access import execute_internal_query

    _seed_turns()

    _cols, rows, _truncated = execute_internal_query(
        system_db_path="",
        user={"id": ANALYST_ID, "email": "analyst@test.com"},
        is_admin=False,
        sql=f"SELECT COUNT(*) AS n FROM {TURNS_ID}",
        limit=10,
    )

    assert rows[0][0] == 2


def test_non_admin_cannot_reach_the_base_table(seeded_app_both, state_backend):
    """The alias is the only door: naming ``usage_turns`` directly is caught
    by the same denylist that protects the other internal tables — nothing
    about the new table opens a bypass."""
    if state_backend != "pg":
        pytest.skip("usage_turns exists on Postgres only")
    from connectors.internal.access import InternalAccessError, execute_internal_query

    _seed_turns()

    with pytest.raises(InternalAccessError) as excinfo:
        execute_internal_query(
            system_db_path="",
            user={"id": ANALYST_ID, "email": "analyst@test.com"},
            is_admin=False,
            sql=f"SELECT * FROM {TURNS_ID} WHERE session_id IN (SELECT session_id FROM usage_turns)",
            limit=10,
        )
    # The denylist rejection, not "no internal-table references in SQL" — the
    # latter would also be an InternalAccessError and would pass this test
    # even if the alias were unknown.
    assert "usage_turns" in str(excinfo.value)
