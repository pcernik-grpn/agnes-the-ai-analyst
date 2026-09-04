"""End-to-end POC verification for Universal MCP (RFC #461).

Exercises the full inbound + outbound pipeline against the local mock CRM
without requiring a running Agnes server or any auth/login flow. Intended
for: (a) quick local smoke after editing the connector, (b) CI integration
test once a tests/integration_mcp_universal.py is written.

Steps (see dev_docs/POC-mcp-universal.md for the full architecture):

  1. Spin up a temp DATA_DIR + fresh system.duckdb migrated to v61
  2. Register the mock CRM MCP server (stdio subprocess) as an mcp_source
  3. Introspect + classify upstream tools (3 expected — list/search/get)
  4. Persist the classifier's proposals into tool_registry
  5. Materialize the materialize-mode tool → extract.duckdb on disk
  6. Re-open the extract.duckdb and verify the table is queryable
  7. Build a fresh FastMCP, register passthrough tools from tool_registry
  8. Call each passthrough via the FastMCP API and verify it forwards live
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import duckdb
from mcp.server.fastmcp import FastMCP

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.api.mcp.tools_generator import _make_passthrough_callable, passthrough_annotations  # noqa: E402
from connectors.mcp.classifier import classify_all  # noqa: E402
from connectors.mcp.client import list_tools  # noqa: E402
from connectors.mcp.extractor import extract_source  # noqa: E402
from src.db import _ensure_schema  # noqa: E402
from src.repositories.mcp_sources import MCPSourceRepository  # noqa: E402
from src.repositories.tool_registry import (  # noqa: E402
    MATERIALIZE,
    PASSTHROUGH,
    ToolRegistryRepository,
)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="agnes-mcp-poc-e2e-"))
    os.environ["AGNES_DATA_DIR"] = str(tmp)
    print(f"[setup] tmp DATA_DIR = {tmp}")

    sys_db = tmp / "state" / "system.duckdb"
    sys_db.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(sys_db))
    _ensure_schema(conn)
    ver = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    print(f"[step 1] schema migrated to v{ver}")

    sources = MCPSourceRepository(conn)
    # Use the interpreter that's running this script — works whether the
    # caller invoked the worktree-local .venv or the main checkout's .venv
    # (worktrees don't carry their own .venv by default).
    sources.upsert(
        id="src_mock_crm",
        name="mock_crm",
        transport="stdio",
        command=sys.executable,
        args=[str(REPO_ROOT / "scripts" / "dev" / "mock_crm_mcp_server.py")],
    )
    src = sources.get("src_mock_crm")
    print(f"[step 2] registered source: {src['name']} ({src['transport']})")

    discovered = list_tools(src)
    if len(discovered) != 3:
        print(f"[step 3] FAIL: expected 3 upstream tools, got {len(discovered)}")
        return 1
    proposals = classify_all(discovered)
    by_name = {p.name: p for p in proposals}
    if by_name["listAccounts"].suggested_mode != MATERIALIZE:
        print(f"[step 3] FAIL: listAccounts should be materialize, got "
              f"{by_name['listAccounts'].suggested_mode}")
        return 1
    if by_name["searchContacts"].suggested_mode != PASSTHROUGH:
        print(f"[step 3] FAIL: searchContacts should be passthrough, got "
              f"{by_name['searchContacts'].suggested_mode}")
        return 1
    print("[step 3] classifier: listAccounts→materialize, search/get→passthrough  OK")

    tools_repo = ToolRegistryRepository(conn)
    upstream_by_name = {t.name: t for t in discovered}
    for proposal in proposals:
        info = upstream_by_name[proposal.name]
        if proposal.suggested_mode == MATERIALIZE:
            exposed = proposal.name.lower()
            schedule = "every 6h"
        elif proposal.suggested_mode == PASSTHROUGH:
            exposed = f"crm.{proposal.name}"
            schedule = None
        else:
            continue
        tools_repo.upsert(
            tool_id=f"src_mock_crm.{exposed}",
            source_id="src_mock_crm",
            original_name=proposal.name,
            exposed_name=exposed,
            mode=proposal.suggested_mode,
            input_schema=info.input_schema,
            description=info.description,
            schedule=schedule,
        )
    persisted = tools_repo.list_for_source("src_mock_crm")
    print(f"[step 4] persisted {len(persisted)} tools to tool_registry")

    result = extract_source(system_conn=conn, source_id="src_mock_crm")
    if result["errors"]:
        print(f"[step 5] FAIL: materialize errors: {result['errors']}")
        return 1
    extract_path = Path(result["extract_duckdb"])
    if not extract_path.exists():
        print(f"[step 5] FAIL: extract.duckdb not created at {extract_path}")
        return 1
    print(f"[step 5] materialized: {result['tables']}")

    ext = duckdb.connect(str(extract_path), read_only=True)
    try:
        meta_rows = ext.execute("SELECT table_name, rows FROM _meta").fetchall()
        if meta_rows != [("listaccounts", 15)]:
            print(f"[step 6] FAIL: unexpected _meta: {meta_rows}")
            return 1
        sample = ext.execute(
            "SELECT id, name, country FROM listaccounts WHERE country = 'DE'"
        ).fetchall()
        if len(sample) != 2:
            print(f"[step 6] FAIL: expected 2 DE rows, got {len(sample)}: {sample}")
            return 1
        print(f"[step 6] queryable: 15 rows total, 2 in DE — {sample[0][1]}, {sample[1][1]}")
    finally:
        ext.close()

    # register_passthrough_tools() now reads through the src.repositories
    # factory (no conn param) — this script deliberately keeps its own
    # isolated `conn`/`sources`/`tools_repo` for the rest of the pipeline,
    # so register the passthrough tools inline against those instead of
    # going through the (DATA_DIR-keyed) factory singleton.
    mcp = FastMCP("agnes-poc-e2e")
    registered: list[str] = []
    for tool in tools_repo.list_by_mode(PASSTHROUGH, enabled_only=True):
        upstream_source = sources.get(tool["source_id"])
        if upstream_source is None or not upstream_source.get("enabled", True):
            continue
        input_schema = tool.get("input_schema") if isinstance(tool.get("input_schema"), dict) else None
        fn = _make_passthrough_callable(upstream_source, tool["original_name"], input_schema)
        description = tool.get("description") or f"Passthrough to {upstream_source['name']}.{tool['original_name']}"
        mcp.add_tool(
            fn,
            name=tool["exposed_name"],
            description=description,
            annotations=passthrough_annotations(tool.get("mutating")),
        )
        registered.append(tool["exposed_name"])
    if set(registered) != {"crm.getAccount", "crm.searchContacts"}:
        print(f"[step 7] FAIL: unexpected registered set: {registered}")
        return 1
    print(f"[step 7] FastMCP knows about: {sorted(registered)}")

    async def _call() -> int:
        listed = await mcp.list_tools()
        listed_by_name = {t.name: t for t in listed}
        if "crm.searchContacts" not in listed_by_name:
            return 1
        sc = listed_by_name["crm.searchContacts"]
        req = (sc.inputSchema or {}).get("required", [])
        if req != ["query"]:
            print(f"[step 8] FAIL: searchContacts required wrong: {req}")
            return 1
        result = await mcp.call_tool("crm.searchContacts", {"query": "Tony"})
        if not result or "Tony Stark" not in str(result):
            print(f"[step 8] FAIL: searchContacts result missing 'Tony Stark': {result}")
            return 1
        print("[step 8] crm.searchContacts(query=Tony) forwarded to upstream  OK")
        result = await mcp.call_tool("crm.getAccount", {"account_id": "ACC-007"})
        if not result or "Stark Industries" not in str(result):
            print(f"[step 8] FAIL: getAccount result missing 'Stark Industries': {result}")
            return 1
        print("[step 8] crm.getAccount(account_id=ACC-007) forwarded to upstream  OK")
        return 0

    rc = asyncio.run(_call())
    conn.close()
    if rc == 0:
        print()
        print("===== POC end-to-end PASSED =====")
        print(f"     temp DATA_DIR (left for inspection): {tmp}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
