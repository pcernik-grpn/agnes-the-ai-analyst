"""``agnes_issues`` / ``agnes_issue_comments`` end-to-end on a real Postgres
backend.

Companion of the DuckDB-side declaration story covered generically by
``tests/test_internal_table_descriptions.py`` — everything here needs rows
in an actual ``issue_reports``/``issue_comments`` table, so it lives beside
the other backend-parametrized suites, following
``tests/db_pg/test_agnes_turns_internal_table_pg.py``'s shape: both ids are
Postgres-only projections (A3 PG-first ratchet, ``issue_reports`` landed
after the freeze), so the DuckDB half of ``state_backend`` is the negative
control — neither id is registered, neither joins the seeded package.

What this pins:

* the ids are registered and join the seeded ``agnes-usage`` package on
  Postgres — and on Postgres only;
* a reporter sees only their own issues and the comments on them (including
  an admin's reply on THEIR issue); an admin sees every row.
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
    issue_reports_repo,
    resource_grants_repo,
    user_group_members_repo,
    user_groups_repo,
)

ISSUES_ID = "agnes_issues"
COMMENTS_ID = "agnes_issue_comments"
ANALYST_ID = "analyst1"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _boot() -> None:
    """One startup pass — ``seeded_app_both``'s client never runs the ASGI
    lifespan, so what ``app/main.py`` does at boot is replayed here."""
    ensure_internal_package_seeded(newly_registered=ensure_internal_tables_registered())


def _member_ids(pkg_id: str) -> set[str]:
    return {t["id"] for t in data_packages_repo().list_tables(pkg_id)}


def _grant_usage_package(user_id: str, *, group_name: str = "issues-pkg-testers") -> str:
    """Put the seeded ``agnes-usage`` package in *user_id*'s stack, through
    the repository factory so the helper works on either backend (mirrors
    ``tests/db_pg/test_agnes_turns_internal_table_pg.py``'s helper of the
    same name)."""
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


def test_registered_and_in_usage_package_on_pg_only(seeded_app_both, state_backend):
    _boot()
    pkg = data_packages_repo().get_by_slug(USAGE_PACKAGE_SLUG)
    assert pkg is not None
    members = _member_ids(pkg["id"])
    if state_backend == "pg":
        assert {ISSUES_ID, COMMENTS_ID} <= members
    else:
        assert not ({ISSUES_ID, COMMENTS_ID} & members)


def test_reporter_sees_only_own_rows_and_admin_replies(seeded_app_both, state_backend):
    if state_backend != "pg":
        pytest.skip("PG-only table")
    _boot()
    _grant_usage_package(ANALYST_ID)
    repo = issue_reports_repo()
    mine = repo.create(
        title="mine",
        body=None,
        kind="bug",
        created_by=ANALYST_ID,
        created_by_email=None,
        source_surface="web",
        page_url=None,
        context={"k": "v"},
    )
    repo.create(
        title="theirs",
        body=None,
        kind="bug",
        created_by="someone-else",
        created_by_email=None,
        source_surface="web",
        page_url=None,
        context=None,
    )
    repo.add_comment(mine["id"], author_id="admin1", author_email="ops@example.com", author_kind="admin", body="on it")

    resp = _query(
        seeded_app_both, seeded_app_both["analyst_token"], f"SELECT number, title FROM {ISSUES_ID} ORDER BY number"
    )
    assert resp.status_code == 200, resp.text
    assert [r[1] for r in resp.json()["rows"]] == ["mine"]

    comments = _query(seeded_app_both, seeded_app_both["analyst_token"], f"SELECT body FROM {COMMENTS_ID}")
    assert comments.status_code == 200, comments.text
    assert [r[0] for r in comments.json()["rows"]] == ["on it"]


def test_admin_sees_every_row(seeded_app_both, state_backend):
    if state_backend != "pg":
        pytest.skip("PG-only table")
    _boot()
    repo = issue_reports_repo()
    repo.create(
        title="a",
        body=None,
        kind="bug",
        created_by="u1",
        created_by_email=None,
        source_surface="web",
        page_url=None,
        context=None,
    )
    repo.create(
        title="b",
        body=None,
        kind="bug",
        created_by="u2",
        created_by_email=None,
        source_surface="web",
        page_url=None,
        context=None,
    )
    resp = _query(seeded_app_both, seeded_app_both["admin_token"], f"SELECT COUNT(*) FROM {ISSUES_ID}")
    assert resp.status_code == 200, resp.text
    assert int(resp.json()["rows"][0][0]) >= 2
