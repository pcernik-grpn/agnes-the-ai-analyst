"""``agnes_extraction_runs`` / ``agnes_facts_ingest_runs`` end-to-end on a
real Postgres backend.

Companion of ``tests/test_agnes_extraction_usage_tables.py`` (declaration +
the whole DuckDB-backend story, which needs no PG server). Everything here
needs rows in the actual ``extraction_runs`` / ``facts_ingest_runs`` tables,
so it lives beside the other backend-parametrized suites: ``seeded_app_both``
builds the app AFTER the backend env is set, and runs each test once per
backend.

What this pins, on top of the ``agnes_turns`` precedent
(``test_agnes_turns_internal_table_pg.py``):

* the ids are registered and join the seeded ``agnes-usage`` package on
  Postgres — and on Postgres only;
* the ADMIN-ONLY row model — the point of this table pair: unlike every
  other internal table, granting the package does NOT let a non-admin see
  any row. A non-admin with the grant gets zero rows even though rows exist
  and even though the query succeeds (200, not 403 — the TABLE is visible,
  no ROW is);
* an admin sees every run unscoped, including the LLM-cost-ledger columns
  (``usage`` / ``llm_usage``) this table pair exists to make queryable;
* ``agnes pull`` never lists either table, on the real backend where they
  are registered and packaged.

Callers are NON-ADMIN except in the tests whose subject is the admin's
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
    extraction_runs_repo,
    facts_ingest_runs_repo,
    resource_grants_repo,
    table_registry_repo,
    user_group_members_repo,
    user_groups_repo,
)
from src.repositories.extraction_runs_pg import DONE

EXTRACTION_ID = "agnes_extraction_runs"
INGEST_ID = "agnes_facts_ingest_runs"
ADMIN_ONLY_IDS = (EXTRACTION_ID, INGEST_ID)
ANALYST_ID = "analyst1"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _boot() -> None:
    """One startup pass — ``seeded_app_both``'s client never runs the ASGI
    lifespan, so what ``app/main.py`` does at boot is replayed here."""
    ensure_internal_package_seeded(newly_registered=ensure_internal_tables_registered())


def _member_ids(pkg_id: str) -> set[str]:
    return {t["id"] for t in data_packages_repo().list_tables(pkg_id)}


def _seed_extraction_run(*, usage: dict | None = None) -> str:
    run_id = extraction_runs_repo().start(connection_id="conn-1", phase="crawl")
    extraction_runs_repo().finish(
        run_id,
        status=DONE,
        report={"total_files": 3},
        usage=usage or {"model": "haiku", "calls": 1, "input_tokens": 3_200_000_000, "output_tokens": 10_000},
        files_seen=3,
        files_done=3,
    )
    return run_id


def _seed_ingest_run(*, llm_usage: dict | None = None) -> str:
    return facts_ingest_runs_repo().create(
        corpus_ids=["corpus-a"],
        caller="admin@example.com",
        documents_seen=10,
        claims_written=25,
        claims_rejected=[],
        deferred=[],
        subjects_created=5,
        subjects_deleted=0,
        review_items=[],
        llm_usage=llm_usage or {"models": ["haiku"], "input_tokens": 3_200_000_000, "output_tokens": 500_000},
    )


def _grant_usage_package(user_id: str, *, group_name: str = "extraction-usage-pkg-testers") -> str:
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


@pytest.mark.parametrize(
    "registry_id,source_table", [(EXTRACTION_ID, "extraction_runs"), (INGEST_ID, "facts_ingest_runs")]
)
def test_registered_on_pg_only(seeded_app_both, state_backend, registry_id, source_table):
    _boot()
    row = table_registry_repo().get(registry_id)
    if state_backend == "pg":
        assert row is not None, f"{source_table} exists here, so the projection must be registered"
        assert row["source_type"] == "internal"
        assert row["source_table"] == source_table
        assert row["query_mode"] == "internal"
    else:
        assert row is None, "a registry row would advertise a table this backend does not have"


def test_joins_the_usage_package_on_pg_only(seeded_app_both, state_backend):
    _boot()
    pkg = data_packages_repo().get_by_slug(USAGE_PACKAGE_SLUG)
    assert pkg is not None
    members = _member_ids(pkg["id"])
    # The pre-A3 tables are members on both backends — this is a narrowing
    # of the two new ids only, not of the package.
    assert "agnes_sessions" in members
    assert (EXTRACTION_ID in members) is (state_backend == "pg")
    assert (INGEST_ID in members) is (state_backend == "pg")


# ---------------------------------------------------------------------------
# /api/query — visibility from the package, ADMIN-ONLY row scope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("registry_id", ADMIN_ONLY_IDS)
def test_non_admin_without_the_package_is_denied(seeded_app_both, state_backend, registry_id):
    if state_backend != "pg":
        pytest.skip("the DuckDB half lives in tests/test_agnes_extraction_usage_tables.py")
    _boot()

    resp = _query(seeded_app_both, seeded_app_both["analyst_token"], f"SELECT COUNT(*) FROM {registry_id}")

    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    assert registry_id in detail
    assert USAGE_PACKAGE_SLUG in detail, "the denial must name the package that would help"


def test_non_admin_with_the_package_sees_zero_extraction_runs(seeded_app_both, state_backend):
    """The defining behaviour of this table pair: the grant makes the TABLE
    visible, but no ROW is ever "the analyst's own" — unlike every other
    internal table, a successful (200) query returns an EMPTY result, not a
    403 and not someone else's row."""
    if state_backend != "pg":
        pytest.skip("extraction_runs exists on Postgres only")
    _boot()
    _seed_extraction_run()
    _grant_usage_package(ANALYST_ID)

    resp = _query(seeded_app_both, seeded_app_both["analyst_token"], f"SELECT COUNT(*) AS n FROM {EXTRACTION_ID}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["rows"][0][0] == 0


def test_non_admin_with_the_package_sees_zero_ingest_runs(seeded_app_both, state_backend):
    if state_backend != "pg":
        pytest.skip("facts_ingest_runs exists on Postgres only")
    _boot()
    _seed_ingest_run()
    _grant_usage_package(ANALYST_ID)

    resp = _query(seeded_app_both, seeded_app_both["analyst_token"], f"SELECT COUNT(*) AS n FROM {INGEST_ID}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["rows"][0][0] == 0


def test_admin_sees_every_extraction_run_unscoped_including_usage(seeded_app_both, state_backend):
    """The one admin-driven case: god-mode needs no package and the row
    filter stays empty — and the LLM-cost-ledger column this table exists
    to expose comes back readable."""
    if state_backend != "pg":
        pytest.skip("extraction_runs exists on Postgres only")
    _boot()
    _seed_extraction_run(usage={"model": "haiku", "calls": 1, "input_tokens": 3_200_000_000, "output_tokens": 10_000})

    resp = _query(
        seeded_app_both,
        seeded_app_both["admin_token"],
        f"SELECT json_extract(usage, '$.input_tokens') FROM {EXTRACTION_ID}",
    )

    assert resp.status_code == 200, resp.text
    # JSON round-trips through the internal materializer as text (every
    # JSON-family column is cast to VARCHAR on read) — the token figure must
    # still be present and correct, whatever the exact literal shape.
    value = str(resp.json()["rows"][0][0])
    assert "3200000000" in value


def test_admin_sees_every_ingest_run_unscoped_including_llm_usage(seeded_app_both, state_backend):
    """The live-verified motivation for this whole table pair: the real LLM
    spend ledger, sitting in ``llm_usage``, must be queryable directly —
    nobody has to trust one dashboard's arithmetic."""
    if state_backend != "pg":
        pytest.skip("facts_ingest_runs exists on Postgres only")
    _boot()
    _seed_ingest_run(llm_usage={"models": ["haiku"], "input_tokens": 3_200_000_000, "output_tokens": 500_000})

    resp = _query(
        seeded_app_both,
        seeded_app_both["admin_token"],
        f"SELECT json_extract(llm_usage, '$.input_tokens') FROM {INGEST_ID}",
    )

    assert resp.status_code == 200, resp.text
    value = str(resp.json()["rows"][0][0])
    assert "3200000000" in value


def test_catalog_lists_both_for_a_grantee(seeded_app_both, state_backend):
    if state_backend != "pg":
        pytest.skip("both tables exist on Postgres only")
    _boot()
    _grant_usage_package(ANALYST_ID)

    resp = seeded_app_both["client"].get("/api/v2/catalog", headers=_auth(seeded_app_both["analyst_token"]))

    assert resp.status_code == 200, resp.text
    ids = {t["id"] for t in resp.json()["tables"]}
    assert EXTRACTION_ID in ids
    assert INGEST_ID in ids


# ---------------------------------------------------------------------------
# The alias is the only door — same denylist as every other internal table
# ---------------------------------------------------------------------------


def test_non_admin_cannot_reach_extraction_runs_base_table(seeded_app_both, state_backend):
    if state_backend != "pg":
        pytest.skip("extraction_runs exists on Postgres only")
    from connectors.internal.access import InternalAccessError, execute_internal_query

    _boot()
    _seed_extraction_run()
    _grant_usage_package(ANALYST_ID)

    with pytest.raises(InternalAccessError) as excinfo:
        execute_internal_query(
            system_db_path="",
            user={"id": ANALYST_ID, "email": "analyst@test.com"},
            is_admin=False,
            sql=(f"SELECT * FROM {EXTRACTION_ID} WHERE connection_id IN (SELECT connection_id FROM extraction_runs)"),
            limit=10,
        )
    assert "extraction_runs" in str(excinfo.value)


# ---------------------------------------------------------------------------
# agnes pull must never see these tables — real backend, not monkeypatched
# ---------------------------------------------------------------------------


def test_manifest_never_lists_either_table(seeded_app_both, state_backend):
    if state_backend != "pg":
        pytest.skip("the interesting case is the backend that DOES register + package them")
    _boot()
    pkg_id = _grant_usage_package(ANALYST_ID)
    assert {EXTRACTION_ID, INGEST_ID} <= _member_ids(pkg_id)

    resp = seeded_app_both["client"].get("/api/sync/manifest", headers=_auth(seeded_app_both["analyst_token"]))

    assert resp.status_code == 200, resp.text
    manifest = resp.json()
    assert EXTRACTION_ID not in manifest.get("tables", {})
    assert INGEST_ID not in manifest.get("tables", {})
    for pkg in manifest.get("data_packages", []):
        listed_ids = {t.get("id") for t in pkg.get("tables", [])}
        assert EXTRACTION_ID not in listed_ids
        assert INGEST_ID not in listed_ids
