"""Internal tables (``agnes_sessions`` / ``agnes_telemetry`` / ``agnes_audit``)
are stack-gated — visible only through a data package in the caller's stack.

Plan: ``docs/superpowers/plans/2026-08-31-usage-package-per-turn-tokens.md``
Task 8; design: ``docs/superpowers/specs/2026-08-31-usage-package-and-
per-turn-tokens-design.md`` §2 (**BREAKING**).

Before this change the tables were implicitly readable by every
authenticated user (own rows only) and no admin could say otherwise. They
are now members of the seeded ``agnes-usage`` package, so an admin grants
who may query usage data at all. What did NOT change: the row-level filter
(non-admin → own rows, admin → unscoped) and principal callers, whose
authority is owner-grants ∩ scope rather than a stack.

Every caller here is deliberately NON-ADMIN except where the admin path is
the thing under test — Admin is a god-mode short-circuit on every check, so
a visibility test driven by an admin asserts nothing.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from connectors.internal.registry import (
    USAGE_PACKAGE_SLUG,
    ensure_internal_package_seeded,
    ensure_internal_tables_registered,
)
from src.db import get_system_db
from src.repositories import data_packages_repo

ANALYST_ID = "analyst1"
VIEWER_ID = "viewer1"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_usage_rows(conn) -> None:
    """Two sessions for the analyst, one for another user.

    Own-rows-only is the assertion the third row makes possible: a caller
    who can see the table must still not see a colleague's session.
    """
    rows = [
        ("analyst/s1.jsonl", "s-an-1", "analyst", ANALYST_ID),
        ("analyst/s2.jsonl", "s-an-2", "analyst", ANALYST_ID),
        ("viewer/s1.jsonl", "s-vi-1", "viewer", VIEWER_ID),
    ]
    for session_file, session_id, username, user_id in rows:
        conn.execute(
            "INSERT INTO usage_session_summary "
            "(session_file, session_id, username, user_id, tool_calls, tool_errors, processor_version) "
            "VALUES (?, ?, ?, ?, 1, 0, 1)",
            [session_file, session_id, username, user_id],
        )


def _grant_usage_package(conn, user_id: str, *, group_name: str = "usage-pkg-testers") -> str:
    """Put the seeded ``agnes-usage`` package in *user_id*'s stack.

    Mirrors what an admin does at /admin/access: a group the user belongs
    to gets a ``resource_grants`` row of type ``data_package``. ``required``
    lands it in the stack without a subscribe step.
    """
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    pkg = data_packages_repo().get_by_slug(USAGE_PACKAGE_SLUG)
    assert pkg is not None, "the agnes-usage package must be seeded first"

    groups = UserGroupsRepository(conn)
    grp = groups.get_by_name(group_name)
    if not grp:
        grp = groups.create(name=group_name, description="test", created_by="test")
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
def gated(seeded_app):
    """Seeded app + one startup pass of the internal seed + usage rows.

    ``seeded_app``'s TestClient never runs the ASGI lifespan (see its
    docstring), so the boot-time seeding that ``app/main.py`` performs is
    replayed here explicitly.
    """
    newly_registered = ensure_internal_tables_registered()
    ensure_internal_package_seeded(newly_registered=newly_registered)
    conn = get_system_db()
    try:
        _seed_usage_rows(conn)
    finally:
        conn.close()
    return seeded_app


def _count(resp) -> int:
    body = resp.json()
    assert body["rows"], body
    return int(body["rows"][0][0])


# ---------------------------------------------------------------------------
# /api/query — the branch that does NOT inherit the rbac helpers
# ---------------------------------------------------------------------------


def test_query_denied_without_package(gated):
    """The internal short-circuit in ``/api/query`` runs before
    ``get_accessible_tables`` and ``_run_internal_query`` checks nothing but
    ``is_user_admin``. Without an explicit gate there, hiding the tables from
    /catalog would leave ``SELECT * FROM agnes_sessions`` open to everyone."""
    resp = gated["client"].post(
        "/api/query",
        json={"sql": "SELECT COUNT(*) FROM agnes_sessions"},
        headers=_auth(gated["analyst_token"]),
    )
    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"].lower()
    assert "data package" in detail
    assert "agnes_sessions" in detail


def test_query_denied_names_every_referenced_internal_table(gated):
    """A denial must name a table the caller actually asked for, so the
    "which package do I need" question has an answer."""
    resp = gated["client"].post(
        "/api/query",
        json={"sql": "SELECT COUNT(*) FROM agnes_audit"},
        headers=_auth(gated["analyst_token"]),
    )
    assert resp.status_code == 403, resp.text
    assert "agnes_audit" in resp.json()["detail"]


def test_query_denial_does_not_advertise_agnes_pull(gated):
    """Internal tables are never distributed to the laptop (`agnes pull`
    skips them), so the generic stack-denial hint's "then run `agnes pull`"
    tail would send the analyst down a path that cannot work."""
    resp = gated["client"].post(
        "/api/query",
        json={"sql": "SELECT COUNT(*) FROM agnes_sessions"},
        headers=_auth(gated["analyst_token"]),
    )
    assert resp.status_code == 403
    assert "agnes pull" not in resp.json()["detail"]


def test_query_allowed_with_package_grants_own_rows_only(gated):
    conn = get_system_db()
    try:
        _grant_usage_package(conn, ANALYST_ID)
    finally:
        conn.close()

    resp = gated["client"].post(
        "/api/query",
        json={"sql": "SELECT COUNT(*) FROM agnes_sessions"},
        headers=_auth(gated["analyst_token"]),
    )
    assert resp.status_code == 200, resp.text
    # Package membership decides visibility of the TABLE; the row filter is
    # unchanged, so the analyst still sees only their own two sessions.
    assert _count(resp) == 2


def test_package_grant_does_not_widen_row_scope(gated):
    """A grantee must not see a colleague's rows — there is no
    "grantee sees everyone" tier (spec §"Agreed semantics")."""
    conn = get_system_db()
    try:
        _grant_usage_package(conn, ANALYST_ID)
    finally:
        conn.close()

    resp = gated["client"].post(
        "/api/query",
        json={"sql": "SELECT session_id FROM agnes_sessions ORDER BY session_id"},
        headers=_auth(gated["analyst_token"]),
    )
    assert resp.status_code == 200, resp.text
    assert [r[0] for r in resp.json()["rows"]] == ["s-an-1", "s-an-2"]


def test_admin_unscoped_view_unchanged(gated):
    """Admin needs no package: god-mode short-circuits the table gate and the
    row filter stays unscoped."""
    resp = gated["client"].post(
        "/api/query",
        json={"sql": "SELECT COUNT(*) FROM agnes_sessions"},
        headers=_auth(gated["admin_token"]),
    )
    assert resp.status_code == 200, resp.text
    assert _count(resp) == 3


# ---------------------------------------------------------------------------
# Catalog / sample — these already route through the rbac helpers
# ---------------------------------------------------------------------------


def _catalog_ids(gated, token: str) -> set[str]:
    resp = gated["client"].get("/api/v2/catalog", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    return {t["id"] for t in resp.json()["tables"]}


def test_catalog_hides_internal_without_package(gated):
    ids = _catalog_ids(gated, gated["analyst_token"])
    assert "agnes_sessions" not in ids
    assert "agnes_telemetry" not in ids
    assert "agnes_audit" not in ids


def test_catalog_shows_internal_with_package(gated):
    conn = get_system_db()
    try:
        _grant_usage_package(conn, ANALYST_ID)
    finally:
        conn.close()
    ids = _catalog_ids(gated, gated["analyst_token"])
    assert {"agnes_sessions", "agnes_telemetry", "agnes_audit"} <= ids


def test_sample_denied_without_package(gated):
    """``/api/v2/sample`` gates through ``can_access_table``; the spec asks
    for the denial to be pinned on this surface too."""
    resp = gated["client"].get(
        "/api/v2/sample/agnes_sessions",
        headers=_auth(gated["analyst_token"]),
    )
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# MCP — the foundation `query` tool proxies to /api/query
# ---------------------------------------------------------------------------


def test_mcp_query_path_gated(gated, shared_app, monkeypatch):
    """The MCP tool self-calls ``/api/query`` over HTTP, so the gate above is
    what protects it — pinned here so a future in-process shortcut cannot
    quietly reopen the hole."""
    pytest.importorskip("mcp", reason="mcp package not installed")

    _RealAsyncClient = httpx.AsyncClient

    def _asgi_async_client(*args, **kwargs):
        return _RealAsyncClient(transport=httpx.ASGITransport(app=shared_app), base_url="http://t")

    monkeypatch.setattr(httpx, "AsyncClient", _asgi_async_client)

    import app.api.mcp_http as mcp_mod

    token = mcp_mod._current_token.set(gated["analyst_token"])
    try:
        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            asyncio.run(mcp_mod.query(sql="SELECT COUNT(*) FROM agnes_sessions"))
    finally:
        mcp_mod._current_token.reset(token)

    assert excinfo.value.response.status_code == 403
    assert "data package" in excinfo.value.response.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Principal carve-out (spec §2) — deliberate, not an oversight
# ---------------------------------------------------------------------------


def test_principal_keeps_internal_table_access(gated):
    """A co-session / agent principal has no stack, so "in the caller's
    stack" is undefined for it. Its authority is already bounded by owner
    grants ∩ scope and the row filter yields zero rows for a co-session, so
    internal tables stay reachable — see spec §2 and the three pinning tests
    (test_agent_scope_seams / test_copresence_datapath /
    test_query_internal_session_principal)."""
    from app.auth.session_principal import SessionPrincipal
    from src.rbac import can_access_table, get_accessible_tables

    principal = SessionPrincipal("chat_1", ["u1"], ["u1@example.com"], {"table": frozenset()})
    assert can_access_table(principal, "agnes_sessions") is True
    assert "agnes_sessions" in (get_accessible_tables(principal) or [])
    # No widening: a non-internal table outside the intersection stays denied.
    assert can_access_table(principal, "orders_daily") is False
