"""``agnes_llm_calls`` own-rows filtering on a real Postgres backend.

Companion of ``tests/test_agnes_llm_calls_internal_table.py`` (declaration +
the whole DuckDB-backend story, which needs no PG server) and modelled on
``tests/db_pg/test_agnes_turns_internal_table_pg.py`` — the RBAC story is the
same shape for every internal table: package membership decides visibility,
the row filter decides scope, and the two never trade places. This file pins
that ``agnes_llm_calls``'s ``filter_kind="user_id"`` actually narrows to the
caller on a real Postgres query, not merely that the declaration says so.

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
from src.observability.llm_context import LlmCallContext
from src.observability.llm_record import build_record
from src.repositories import (
    data_packages_repo,
    llm_calls_repo,
    resource_grants_repo,
    user_group_members_repo,
    user_groups_repo,
)

LLM_CALLS_ID = "agnes_llm_calls"
ANALYST_ID = "analyst1"
OTHER_ID = "colleague1"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _boot() -> None:
    """One startup pass — ``seeded_app_both``'s client never runs the ASGI
    lifespan, so what ``app/main.py`` does at boot is replayed here."""
    ensure_internal_package_seeded(newly_registered=ensure_internal_tables_registered())


def _call_row(*, user_id: str, turn_id: str, input_tokens: int, output_tokens: int) -> dict:
    record = build_record(
        kind="completion",
        context=LlmCallContext(user_id=user_id, workload="chat", turn_id=turn_id),
        provider="anthropic",
        upstream="anthropic",
        model_requested="claude-sonnet-5",
        model_response=None,
        usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
        latency_ms=100,
        status="success",
    )
    return record.to_row()


def _seed_calls() -> None:
    """Two calls for the analyst, one for a colleague.

    The colleague's row is what makes own-rows-only assertable: a caller who
    can reach the table must still not see someone else's calls.
    """
    llm_calls_repo().insert_batch(
        [
            _call_row(user_id=ANALYST_ID, turn_id="t1", input_tokens=10, output_tokens=20),
            _call_row(user_id=ANALYST_ID, turn_id="t2", input_tokens=1, output_tokens=2),
            _call_row(user_id=OTHER_ID, turn_id="t9", input_tokens=500, output_tokens=500),
        ]
    )


def _grant_usage_package(user_id: str, *, group_name: str = "llmcalls-pkg-testers") -> str:
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


def test_non_admin_without_the_package_is_denied(seeded_app_both, state_backend):
    if state_backend != "pg":
        pytest.skip("the DuckDB half lives in tests/test_agnes_llm_calls_internal_table.py")
    _boot()
    _seed_calls()

    resp = _query(seeded_app_both, seeded_app_both["analyst_token"], f"SELECT COUNT(*) FROM {LLM_CALLS_ID}")

    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    assert LLM_CALLS_ID in detail
    assert USAGE_PACKAGE_SLUG in detail, "the denial must name the package that would help"


def test_non_admin_with_the_package_sees_own_turns_only(seeded_app_both, state_backend):
    if state_backend != "pg":
        pytest.skip("llm_calls exists on Postgres only")
    _boot()
    _seed_calls()
    _grant_usage_package(ANALYST_ID)

    resp = _query(
        seeded_app_both,
        seeded_app_both["analyst_token"],
        f"SELECT turn_id FROM {LLM_CALLS_ID} ORDER BY turn_id",
    )

    assert resp.status_code == 200, resp.text
    assert [r[0] for r in resp.json()["rows"]] == ["t1", "t2"], "the colleague's call must not leak"


def test_admin_sees_every_turn_unscoped(seeded_app_both, state_backend):
    """The one admin-driven case: god-mode needs no package and the row
    filter stays empty."""
    if state_backend != "pg":
        pytest.skip("llm_calls exists on Postgres only")
    _boot()
    _seed_calls()

    resp = _query(seeded_app_both, seeded_app_both["admin_token"], f"SELECT COUNT(*) FROM {LLM_CALLS_ID}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["rows"][0][0] == 3
