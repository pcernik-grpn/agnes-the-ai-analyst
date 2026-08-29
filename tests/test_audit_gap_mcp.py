"""F2c — MCP surface audit coverage (audit-full-coverage plan, Task 5).

Covers the four MCP-adjacent gaps this task closes:

* ``mcp.tool_call`` — the ONE dispatch wrapper (``app.api.mcp.tools_generator
  .install_tool_call_audit``) both the SSE and Streamable-HTTP transports
  install onto their ``FastMCP`` instance's low-level ``CallToolRequest``
  handler. Testing the wrapper directly (as built here) covers both
  transports, since they share the exact same function.
* ``mcp.passthrough_call`` / ``mcp.passthrough_denied`` —
  ``app/api/mcp_passthrough.py``.
* ``query.table_scoped`` — ``app/api/mcp_per_table.py``.
* ``facts.search`` / ``facts.neighbors`` / ``facts.claims`` /
  ``facts.ingest`` — ``app/api/facts.py``, with ``facts_repo()`` /
  ``facts_ingest_runs_repo()`` mocked so these run on the DuckDB test
  backend without needing a live Postgres (``facts_pg.py`` is PG-only, A3
  ratchet — see ``tests/test_api_facts.py`` for the un-mocked fail-clean
  501 behavior this deliberately bypasses).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("mcp", reason="mcp SDK not installed")

from src.db import close_analytics_db, get_analytics_db, get_system_db
from src.repositories.mcp_sources import MCPSourceRepository
from src.repositories.table_registry import TableRegistryRepository
from src.repositories.tool_registry import PASSTHROUGH, ToolRegistryRepository
from src.repositories.user_group_members import UserGroupMembersRepository
from src.repositories.user_groups import UserGroupsRepository


def _as_dict(v):
    """Normalize a ``params``/``params_before`` cell to a dict — DuckDB and
    Postgres don't agree on whether a JSON column comes back parsed or as
    text (see ``tests/db_pg/test_audit_contract.py::_as_dict``, same idea)."""
    if v is None:
        return None
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        return json.loads(v)
    return v


# ---------------------------------------------------------------------------
# mcp.tool_call — the shared dispatch wrapper
# ---------------------------------------------------------------------------


def test_tool_call_dispatch_wrapper_logs_one_row(seeded_app):
    from mcp.server.fastmcp import FastMCP

    from app.api.mcp.tools_generator import install_tool_call_audit
    from src import audit_context
    from src.repositories import audit_repo

    mcp = FastMCP("test-dispatch", instructions="t")
    seen: dict = {}

    @mcp.tool()
    async def echo(x: int) -> int:
        # Stamped by the wrapper BEFORE dispatch, in this same async task —
        # captured here (not after asyncio.run() returns) because a Task's
        # contextvar copy never propagates back to the caller.
        seen["client_kind"] = audit_context.auto_client_kind()
        return x

    wrapped = install_tool_call_audit(mcp, caller_id_fn=lambda: "audit-gap-u1")
    result = asyncio.run(wrapped("echo", {"x": 5}))
    assert result is not None
    assert seen["client_kind"] == "mcp"

    rows, _ = audit_repo().query(action="mcp.tool_call", user_id="audit-gap-u1", limit=5)
    assert rows, "expected an mcp.tool_call audit row"
    row = rows[0]
    assert row["resource"] == "mcp_tool:echo"
    params = _as_dict(row["params"])
    assert set(params.keys()) == {"tool", "args_hash"}
    assert params["tool"] == "echo"
    assert row["client_kind"] == "mcp"


def test_tool_call_dispatch_wrapper_skips_unresolved_caller(seeded_app):
    from mcp.server.fastmcp import FastMCP

    from app.api.mcp.tools_generator import install_tool_call_audit
    from src.repositories import audit_repo

    mcp = FastMCP("test-dispatch-2", instructions="t")

    @mcp.tool()
    async def echo(x: int) -> int:
        return x

    wrapped = install_tool_call_audit(mcp, caller_id_fn=lambda: None)
    asyncio.run(wrapped("echo", {"x": 1}))

    rows, _ = audit_repo().query(action="mcp.tool_call", resource="mcp_tool:echo", limit=5)
    assert not rows


# ---------------------------------------------------------------------------
# set_client_kind("mcp") — SSE session/auth resolution
# ---------------------------------------------------------------------------


def test_sse_auth_middleware_stamps_mcp_client_kind(seeded_app):
    from app.api.mcp_http import _AuthMiddleware
    from src import audit_context

    tok = seeded_app["analyst_token"]
    seen = {}

    async def _inner_app(scope, receive, send):
        seen["kind"] = audit_context.auto_client_kind()

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/mcp/sse",
        "query_string": b"",
        "headers": [(b"authorization", f"Bearer {tok}".encode())],
    }
    asyncio.run(_AuthMiddleware(_inner_app)(scope, None, None))
    assert seen["kind"] == "mcp"


# ---------------------------------------------------------------------------
# mcp.passthrough_call / mcp.passthrough_denied
# ---------------------------------------------------------------------------


def _seed_passthrough_tool(analyst_id: str = "analyst1") -> None:
    conn = get_system_db()
    sources = MCPSourceRepository(conn)
    tools = ToolRegistryRepository(conn)
    groups = UserGroupsRepository(conn)
    members = UserGroupMembersRepository(conn)

    sources.upsert(id="src_audit_gap_pt", name="audit-gap-upstream", transport="stdio", command="/bin/true", args=[])
    tools.upsert(
        tool_id="audit-gap-upstream.lookup",
        source_id="src_audit_gap_pt",
        original_name="lookup",
        exposed_name="lookup",
        mode=PASSTHROUGH,
        description="Audit gap coverage test tool.",
    )
    grp = groups.create(name="audit-gap-pt-grp", description=None)
    tools.add_grant("audit-gap-upstream.lookup", grp["id"])
    members.add_member(analyst_id, grp["id"], source="system_seed")
    conn.close()


def _patch_upstream_call(text="ok", is_error=False, data=None):
    from connectors.mcp.client import ToolCallResult

    return patch(
        "app.api.mcp_passthrough.call_tool_async",
        new=AsyncMock(return_value=ToolCallResult(text=text, data=data, is_error=is_error)),
    )


def test_passthrough_call_logs_audit_row(seeded_app):
    _seed_passthrough_tool()
    client = seeded_app["client"]
    with _patch_upstream_call(text="ok"):
        r = client.post(
            "/api/mcp/passthrough/tools/audit-gap-upstream.lookup/call",
            headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
            json={"arguments": {}},
        )
    assert r.status_code == 200, r.text

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="mcp.passthrough_call", limit=5)
    assert rows
    row = rows[0]
    assert row["resource"] == "mcp_source:src_audit_gap_pt"
    assert _as_dict(row["params"])["tool_id"] == "audit-gap-upstream.lookup"


def test_passthrough_denied_logs_audit_row(seeded_app):
    """A caller with no grant 403s; the deny is logged as mcp.passthrough_denied."""
    conn = get_system_db()
    sources = MCPSourceRepository(conn)
    tools = ToolRegistryRepository(conn)
    sources.upsert(
        id="src_audit_gap_deny", name="audit-gap-deny-upstream", transport="stdio", command="/bin/true", args=[]
    )
    tools.upsert(
        tool_id="audit-gap-deny-upstream.private",
        source_id="src_audit_gap_deny",
        original_name="private",
        exposed_name="private",
        mode=PASSTHROUGH,
    )
    conn.close()

    client = seeded_app["client"]
    r = client.post(
        "/api/mcp/passthrough/tools/audit-gap-deny-upstream.private/call",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
        json={"arguments": {}},
    )
    assert r.status_code == 403

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="mcp.passthrough_denied", limit=5)
    assert rows
    row = rows[0]
    assert row["result"] == "denied"
    assert row["resource"] == "mcp_tool:audit-gap-deny-upstream.private"


# ---------------------------------------------------------------------------
# query.table_scoped
# ---------------------------------------------------------------------------


def _seed_view_and_registry(rows: list[dict]) -> dict:
    table_id = f"tt_audit_{uuid.uuid4().hex[:8]}"
    a_conn = get_analytics_db()
    cols = sorted(rows[0].keys()) if rows else ["id"]
    select_parts = []
    for r in rows:
        vals = ", ".join((f"'{r[c]}'" if isinstance(r[c], str) else str(r[c])) + f' AS "{c}"' for c in cols)
        select_parts.append(f"SELECT {vals}")
    union_sql = " UNION ALL ".join(select_parts) if select_parts else "SELECT NULL AS id"
    a_conn.execute(f'CREATE OR REPLACE VIEW "{table_id}" AS {union_sql}')
    close_analytics_db()

    sys_conn = get_system_db()
    TableRegistryRepository(sys_conn).register(
        id=table_id, name=table_id, folder=None, sync_strategy="full_refresh", registered_by="system_seed"
    )
    sys_conn.close()
    return {"table_id": table_id}


def test_query_table_scoped_logs_audit_row(seeded_app):
    seed = _seed_view_and_registry([{"id": "1", "country": "CZ"}, {"id": "2", "country": "DE"}])
    r = seeded_app["client"].post(
        f"/api/mcp/query-table/{seed['table_id']}",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        json={"filter": {"country": "CZ"}, "limit": 10},
    )
    assert r.status_code == 200, r.text

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="query.table_scoped", limit=5)
    assert rows
    row = rows[0]
    assert row["resource"] == f"table:{seed['table_id']}"
    params = _as_dict(row["params"])
    assert params["row_count"] == 1
    assert params["filter_columns"] == ["country"]
    # Filter VALUES never logged, only column names.
    assert "CZ" not in str(params)


# ---------------------------------------------------------------------------
# facts.search / facts.neighbors / facts.claims / facts.ingest
# ---------------------------------------------------------------------------


def _facts_headers(app):
    return {"Authorization": f"Bearer {app['admin_token']}"}


def test_facts_search_logs_audit_row(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
    import app.api.facts as facts_mod

    class _FakeRepo:
        def search(self, user, type=None, filters=None, limit=20, **kwargs):
            return {"subjects": [{"id": "f1"}], "limit_applied": False}

    monkeypatch.setattr(facts_mod, "facts_repo", lambda: _FakeRepo())

    r = seeded_app["client"].post("/api/facts/search", json={}, headers=_facts_headers(seeded_app))
    assert r.status_code == 200, r.text

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="facts.search", limit=5)
    assert rows
    assert _as_dict(rows[0]["params"])["result_count"] == 1


def test_facts_neighbors_logs_audit_row(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
    import app.api.facts as facts_mod

    class _FakeRepo:
        def neighbors(self, user, subject_id, edge_types=None, depth=1, fanout=100, limit=500):
            return {"nodes": [{"id": subject_id}], "edges": [], "truncated": {}}

    monkeypatch.setattr(facts_mod, "facts_repo", lambda: _FakeRepo())

    r = seeded_app["client"].post("/api/facts/neighbors", json={"subject_id": "f1"}, headers=_facts_headers(seeded_app))
    assert r.status_code == 200, r.text

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="facts.neighbors", limit=5)
    assert rows
    assert rows[0]["resource"] == "fact:f1"
    assert _as_dict(rows[0]["params"])["node_count"] == 1


def test_facts_claims_logs_audit_row(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
    import app.api.facts as facts_mod

    class _FakeRepo:
        def claims(self, user, subject_id):
            return {"claims": [{"id": "c1"}], "revealed": False}

    monkeypatch.setattr(facts_mod, "facts_repo", lambda: _FakeRepo())

    r = seeded_app["client"].get("/api/facts/f1/claims", headers=_facts_headers(seeded_app))
    assert r.status_code == 200, r.text

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="facts.claims", limit=5)
    assert rows
    assert rows[0]["resource"] == "fact:f1"
    assert _as_dict(rows[0]["params"])["claim_count"] == 1


def test_facts_ingest_logs_audit_row(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
    import app.api.facts as facts_mod

    class _FakeRepo:
        def ingest_batch(self, documents, full_documents, nodes, edges):
            return {
                "claims_written": 3,
                "claims_rejected": [],
                "deferred": [],
                "subjects_created": 1,
                "subjects_deleted": 0,
                "review_items": [],
            }

    class _FakeRunsRepo:
        def create(self, **kwargs):
            return {"id": "run1"}

    monkeypatch.setattr(facts_mod, "facts_repo", lambda: _FakeRepo())
    monkeypatch.setattr(facts_mod, "facts_ingest_runs_repo", lambda: _FakeRunsRepo())

    r = seeded_app["client"].post(
        "/api/facts/ingest",
        json={"documents": [{"doc_id": "d1", "corpus_id": "c1"}]},
        headers=_facts_headers(seeded_app),
    )
    assert r.status_code == 200, r.text

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="facts.ingest", limit=5)
    assert rows
    assert _as_dict(rows[0]["params"])["claims_written"] == 3


# ---------------------------------------------------------------------------
# hash_args moved to src.audit_helpers, re-exported from app.chat.audit
# ---------------------------------------------------------------------------


def test_hash_args_reexported_from_chat_audit():
    from app.chat.audit import hash_args as chat_hash_args
    from src.audit_helpers import hash_args

    assert chat_hash_args is hash_args
    assert hash_args({"a": 1}) == hash_args({"a": 1})
    assert hash_args({"a": 1}) != hash_args({"a": 2})
