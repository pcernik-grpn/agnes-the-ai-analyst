import pytest


@pytest.fixture
def rbac_conn(e2e_env):
    from src.db import get_system_db
    c = get_system_db()
    yield c
    c.close()


def test_can_access_table_with_session_principal(rbac_conn):
    from src.rbac import can_access_table
    from app.auth.session_principal import SessionPrincipal
    p = SessionPrincipal(
        session_id="chat_1",
        participant_user_ids=["ua", "ub"],
        participant_emails=["a@example.com", "b@example.com"],
        intersection={"table": frozenset({"t2"})},
    )
    assert can_access_table(p, "t2", rbac_conn) is True
    assert can_access_table(p, "t1", rbac_conn) is False


def test_can_access_table_principal_never_admin_short_circuits(rbac_conn, monkeypatch):
    from src.rbac import can_access_table
    from app.auth.session_principal import SessionPrincipal
    monkeypatch.setattr("app.auth.access.is_user_admin", lambda *a, **k: pytest.fail("admin"))
    p = SessionPrincipal("chat_1", ["ua"], ["a@example.com"], {"table": frozenset()})
    assert can_access_table(p, "t2", rbac_conn) is False


def test_get_accessible_tables_with_principal_returns_list_not_none(rbac_conn):
    from src.rbac import get_accessible_tables
    from app.auth.session_principal import SessionPrincipal
    p = SessionPrincipal("chat_1", ["ua"], ["a@example.com"], {"table": frozenset({"t2"})})
    result = get_accessible_tables(p, rbac_conn)
    assert result is not None  # never "all" for a principal
    assert "t2" in result
    from connectors.internal.access import INTERNAL_TABLES
    for t in INTERNAL_TABLES:
        assert t.registry_id in result


from fastapi.testclient import TestClient


def _seed_co_app_base(conn):
    """Seed two users, register tables, return (ua_id, ub_id, co_session_id)."""
    from src.db import SYSTEM_ADMIN_GROUP
    from src.repositories.users import UserRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.table_registry import TableRegistryRepository
    from app.chat.persistence import ChatRepository
    from app.chat.types import Surface

    UserRepository(conn).create(id="ua", email="a@example.com", name="A")
    UserRepository(conn).create(id="ub", email="b@example.com", name="B")

    # Register tables in table_registry
    reg = TableRegistryRepository(conn)
    reg.register(id="t1", name="t1", source_type="keboola")
    reg.register(id="t2", name="t2", source_type="keboola")

    # Create a co-session with both participants
    repo = ChatRepository(conn)
    s0 = repo.create_session(user_email="a@example.com", surface=Surface.WEB)
    s1 = repo.fork_session_as_co_session(
        s0.id,
        owner_email="a@example.com", owner_user_id="ua",
        invitee_email="b@example.com", invitee_user_id="ub",
    )
    return s1.id


def _grant_table_direct(conn, table_id, user_id, group_name):
    """Grant a direct table grant to a user via a new group."""
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.resource_grants import ResourceGrantsRepository

    groups = UserGroupsRepository(conn)
    grp = groups.get_by_name(group_name)
    if not grp:
        grp = groups.create(name=group_name, description="test", created_by="test")
    members = UserGroupMembersRepository(conn)
    if not members.has_membership(user_id, grp["id"]):
        members.add_member(user_id, grp["id"], source="admin", added_by="test")
    grants = ResourceGrantsRepository(conn)
    if not grants.has_grant([grp["id"]], "table", table_id):
        grants.create(
            group_id=grp["id"],
            resource_type="table",
            resource_id=table_id,
            assigned_by="test",
            requirement="required",
        )


@pytest.fixture
def co_app(e2e_env, shared_app):
    """App fixture: co-session where only ua holds table t1. t1 -> 403 for the co token."""
    from src.db import get_system_db
    conn = get_system_db()
    co_id = _seed_co_app_base(conn)
    # Only ua gets direct table grant for t1 -> t1 not in intersection
    _grant_table_direct(conn, "t1", "ua", "g-t1-only-a")
    conn.close()
    app = shared_app
    client = TestClient(app, raise_server_exceptions=False)
    yield client, co_id


@pytest.fixture
def co_app_shared(e2e_env, shared_app):
    """App fixture: co-session where BOTH ua and ub hold table t2. t2 -> 200."""
    from src.db import get_system_db
    conn = get_system_db()
    co_id = _seed_co_app_base(conn)
    # Both ua and ub get direct table grants for t2 -> t2 in intersection
    _grant_table_direct(conn, "t2", "ua", "g-t2-both-a")
    _grant_table_direct(conn, "t2", "ub", "g-t2-both-b")
    conn.close()
    app = shared_app
    client = TestClient(app, raise_server_exceptions=False)
    yield client, co_id


def test_co_token_403_on_single_participant_table(co_app):
    client, co_id = co_app  # app wired to a DuckDB where only A holds t1
    from app.auth.access import mint_co_session_jwt
    hdr = {"Authorization": f"Bearer {mint_co_session_jwt(co_id)}"}

    # /api/data check-access for t1 (A-only) -> 403
    assert client.get("/api/data/t1/check-access", headers=hdr).status_code == 403
    # /api/v2/scan for t1 -> 403 (ScanRequest: table_id only, no `sql`)
    assert client.post("/api/v2/scan", json={"table_id": "t1"}, headers=hdr).status_code == 403
    # /api/v2/sample + /api/v2/schema for t1 -> 403
    assert client.get("/api/v2/sample/t1", headers=hdr).status_code == 403
    assert client.get("/api/v2/schema/t1", headers=hdr).status_code == 403
    # /api/catalog/tables must NOT include t1
    cat = client.get("/api/catalog/tables", headers=hdr)
    assert cat.status_code == 200
    assert all(t.get("id") != "t1" for t in cat.json().get("tables", []))
    # /api/sync/manifest must NOT list t1
    man = client.get("/api/sync/manifest", headers=hdr)
    assert man.status_code == 200
    man_data = man.json()
    # Check direct_tables and data_packages sections
    all_table_ids = (
        [t.get("id") for t in man_data.get("direct_tables", [])]
        + [t.get("id") for pkg in man_data.get("data_packages", []) for t in pkg.get("tables", [])]
        + list(man_data.get("tables", {}).keys())
    )
    assert "t1" not in all_table_ids


def test_co_token_200_on_shared_table(co_app_shared):
    client, co_id = co_app_shared  # both A and B hold t2 via direct grants
    from app.auth.access import mint_co_session_jwt
    hdr = {"Authorization": f"Bearer {mint_co_session_jwt(co_id)}"}
    assert client.get("/api/data/t2/check-access", headers=hdr).status_code == 204


def test_stack_resolver_with_session_principal(rbac_conn):
    from app.services.stack_resolver import StackResolver
    from app.resource_types import ResourceType
    from app.auth.session_principal import SessionPrincipal
    # Seed a data package "pkgA" with required columns
    rbac_conn.execute(
        "INSERT INTO data_packages(id, slug, name, created_by) VALUES "
        "('pkgA', 'pkg-a', 'Pkg A', 'test')"
    )
    p = SessionPrincipal(
        session_id="chat_1",
        participant_user_ids=["ua", "ub"],
        participant_emails=["a@example.com", "b@example.com"],
        intersection={ResourceType.DATA_PACKAGE.value: frozenset({"pkgA"})},
    )
    entries = StackResolver(rbac_conn).stack(p, ResourceType.DATA_PACKAGE)
    assert {e.id for e in entries} == {"pkgA"}


# ---------------------------------------------------------------------------
# Success (200) datapath for a SessionPrincipal.
#
# Every co-session test above stops at a denial (403/404), and a denial never
# exercises the audit-log call sites — those sit inside a
# `try/except Exception: logger.exception(...)` that swallows the failure and
# re-raises the original HTTPException. That is why the `user.get("id")` crash
# on a frozen principal (fixed by `identity_for_audit`) went unnoticed: on
# /api/v2/scan it 500'd a request RBAC had ALLOWED, and on the other endpoints
# it silently dropped the audit row. These tests pin the allowed path.
# ---------------------------------------------------------------------------

_CO_TABLE = "co_local_tbl"


@pytest.fixture
def co_app_local(e2e_env, mock_extract_factory, shared_app):
    """Co-session where BOTH participants hold `co_local_tbl` — a real local
    table with a parquet on disk, so the v2 read endpoints reach a 200."""
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository

    mock_extract_factory("keboola", [{"name": _CO_TABLE, "data": [{"n": "1"}, {"n": "2"}]}])
    conn = get_system_db()
    co_id = _seed_co_app_base(conn)
    TableRegistryRepository(conn).register(id=_CO_TABLE, name=_CO_TABLE, source_type="keboola")
    _grant_table_direct(conn, _CO_TABLE, "ua", "g-co-local-a")
    _grant_table_direct(conn, _CO_TABLE, "ub", "g-co-local-b")
    conn.close()
    client = TestClient(shared_app, raise_server_exceptions=False)
    yield client, co_id


def _co_hdr(co_id):
    from app.auth.access import mint_co_session_jwt

    return {"Authorization": f"Bearer {mint_co_session_jwt(co_id)}"}


def _audit_rows(action: str) -> list:
    """(user_id, result) of every audit_log row for `action`, newest last."""
    from src.db import get_system_db

    c = get_system_db()
    try:
        return c.execute(
            "SELECT user_id, result FROM audit_log WHERE action = ? ORDER BY timestamp", [action]
        ).fetchall()
    finally:
        c.close()


def _assert_audited(action: str, expected_result: str) -> None:
    """A row was written, and the co-session is attributed to no participant.

    `identity_for_audit` reports no identity for a SessionPrincipal — several
    live participants, naming one would misattribute the others — so the row
    carries a NULL user_id. The row EXISTING is the regression guard: before
    the fix the write raised and was swallowed.
    """
    rows = _audit_rows(action)
    assert rows, f"no audit_log row for {action}"
    user_id, result = rows[-1]
    assert result == expected_result, rows[-1]
    assert user_id is None, f"co-session audit row must name no participant, got {user_id!r}"


def test_co_token_200_sample_writes_audit_row(co_app_local):
    client, co_id = co_app_local
    resp = client.get(f"/api/v2/sample/{_CO_TABLE}", headers=_co_hdr(co_id))
    assert resp.status_code == 200, resp.text
    assert resp.json()["table_id"] == _CO_TABLE
    _assert_audited("catalog.sample", "success")


def test_co_token_200_schema_writes_audit_row(co_app_local):
    client, co_id = co_app_local
    resp = client.get(f"/api/v2/schema/{_CO_TABLE}", headers=_co_hdr(co_id))
    assert resp.status_code == 200, resp.text
    assert [c["name"] for c in resp.json()["columns"]] == ["n"]
    _assert_audited("catalog.schema", "success")


def test_co_token_200_scan_writes_audit_row(co_app_local):
    """The path that used to 500: the quota-key derivation in `run_scan` sits
    outside every `except` clause in `scan_endpoint`, so the AttributeError
    escaped as a 500 on a request RBAC had allowed."""
    client, co_id = co_app_local
    resp = client.post("/api/v2/scan", json={"table_id": _CO_TABLE}, headers=_co_hdr(co_id))
    assert resp.status_code == 200, resp.text
    _assert_audited("snapshot.create", "success")


def test_co_token_200_scan_estimate_writes_audit_row(co_app_local):
    client, co_id = co_app_local
    resp = client.post("/api/v2/scan/estimate", json={"table_id": _CO_TABLE}, headers=_co_hdr(co_id))
    assert resp.status_code == 200, resp.text
    _assert_audited("snapshot.estimate", "success")


def test_co_token_200_data_download_writes_audit_row(co_app_local):
    client, co_id = co_app_local
    resp = client.get(f"/api/data/{_CO_TABLE}/download", headers=_co_hdr(co_id))
    assert resp.status_code == 200, resp.text
    _assert_audited("data.download", "success")


def test_co_token_204_check_access_writes_audit_row(co_app_local):
    client, co_id = co_app_local
    resp = client.get(f"/api/data/{_CO_TABLE}/check-access", headers=_co_hdr(co_id))
    assert resp.status_code == 204
    _assert_audited("data.access_check", "success")


def test_co_token_200_v2_catalog_writes_audit_row(co_app_local):
    client, co_id = co_app_local
    resp = client.get("/api/v2/catalog", headers=_co_hdr(co_id))
    assert resp.status_code == 200, resp.text
    assert any(t.get("id") == _CO_TABLE for t in resp.json().get("tables", []))
    _assert_audited("catalog.list", "success")


def test_co_token_403_still_writes_audit_row(co_app_local):
    """The denial path writes its row too — it was swallowed by the same
    `except Exception`, leaving no trace of a co-session's refused read."""
    client, co_id = co_app_local
    assert client.get("/api/v2/sample/t1", headers=_co_hdr(co_id)).status_code == 403
    _assert_audited("catalog.sample", "error.403")
