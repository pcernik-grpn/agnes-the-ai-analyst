"""Agnes MCP server.

Runs as an stdio subprocess started by Claude Desktop.  All tools have
full network access to the Agnes server — unlike the Bash tool sandbox,
which blocks outbound HTTP.

Usage:
    agnes mcp                   # starts the MCP server (stdio transport)

Claude Desktop wires this via .claude/settings.json:
    {
      "mcpServers": {
        "agnes": {
          "command": "/path/to/agnes",
          "args": ["mcp"],
          "type": "stdio"
        }
      }
    }

The setup.py inside the Cowork bundle detects the agnes binary path at
install time and writes the mcpServers block with the correct absolute path.

Credentials are read from ~/.config/agnes/config.yaml (server URL) and
~/.config/agnes/token.json (PAT) — the same files written by setup.py.
"""

from __future__ import annotations

from typing import Literal

from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

from cli.client import api_get
from cli.config import get_server_url, get_token
from cli.query_hints import missing_table, remote_table_hint
from cli.v2_client import V2ClientError, api_delete, api_get_json, api_patch_json, api_post_json
from src.duckdb_conn import _open_duckdb
from src.mcp_tooling import ensure_output_size, progressive_tool
from src.remote_engines import strip_one_trailing_semicolon

mcp = FastMCP(
    "Agnes",
    instructions=(
        "Agnes is a self-hosted AI harness for the organization's data, skills, and memory. "
        "Use `catalog` first to discover available tables, then `schema` to understand "
        "columns, `describe` for sample rows, and `query` to run SQL. "
        "Run `pull` to sync the latest data before a session."
    ),
)

# Full docstrings by tool name — wire descriptions carry only the first
# paragraph; `tool_docs` serves the rest on demand.
TOOL_DOCS: dict[str, str] = {}
tool = progressive_tool(mcp, TOOL_DOCS)


# ── helpers ────────────────────────────────────────────────────────────────


def _mcp_error(context: str, exc: V2ClientError) -> str:
    """Turn a V2ClientError into a user-readable MCP error string."""
    return f"{context} failed (HTTP {exc.status_code}): {exc}"


# ── tools ──────────────────────────────────────────────────────────────────


@tool(read_only=True)
def server_info() -> dict:
    """Return the configured Agnes server URL and your account email.

    Useful as a quick connectivity check at the start of a session.
    """
    server_url = get_server_url()
    token = get_token()
    info: dict = {"server_url": server_url, "authenticated": bool(token)}
    try:
        resp = api_get("/api/health")
        if resp.status_code == 200:
            info["health"] = resp.json()
    except Exception:
        info["health"] = "unreachable"
    # Resolve email from /api/me if available
    try:
        me = api_get_json("/api/me")
        info["user_email"] = me.get("email", "")
    except Exception:
        pass
    return info


@tool(read_only=True)
def catalog() -> dict:
    """List all tables available to you (RBAC-filtered).

    Returns a dict with a ``tables`` list.  Each entry contains:
    - ``id``          — use this in schema / describe / query calls
    - ``name``        — human-readable label
    - ``description`` — what the table contains
    - ``source_type`` — e.g. keboola, bigquery, internal
    - ``query_mode``  — local | remote | materialized | internal
    - ``sql_flavor``  — duckdb or bigquery (affects SQL dialect in query)
    - ``rows``        — approximate row count (may be null)

    Always call this first so you know what data is available.
    """
    try:
        return api_get_json("/api/v2/catalog")
    except V2ClientError as exc:
        raise ValueError(_mcp_error("catalog", exc)) from exc


@tool(read_only=True)
def collections_list() -> dict:
    """List the file Collections you can access (RBAC-filtered).

    A Collection is a user-uploaded set of files Agnes has indexed. Returns a
    dict with an ``items`` list (``id``, ``name``, ``slug``, counts). Use
    ``collection_get`` for the files inside one collection.
    """
    try:
        return api_get_json("/api/collections")
    except V2ClientError as exc:
        raise ValueError(_mcp_error("collections_list", exc)) from exc


@tool(read_only=True)
def collection_get(collection_id: str) -> dict:
    """Show one Collection's detail plus its files and per-file status.

    Args:
        collection_id: Collection id from ``collections_list`` (``col_...``).
    """
    try:
        return api_get_json(f"/api/collections/{collection_id}")
    except V2ClientError as exc:
        raise ValueError(_mcp_error("collection_get", exc)) from exc


@tool(read_only=True)
def collections_search(query: str, k: int = 10, collection_id: str = "") -> dict:
    """Hybrid search across your accessible file Collections (RBAC-filtered). Matching is whole word, there is no wildcard, and file names are a fallback, consulted only when no passage explains the question better — so an empty result is a wording miss far more often than an access problem; read the response's ``hint`` before concluding anything from it.

    Returns ranked chunks with citations (``filename``, ``ordinal``, ``text``,
    ``score``). Optionally restrict to one collection via ``collection_id``.

    The response's ``retrieval`` field says how results were ranked:
    ``hybrid`` (lexical + semantic) or ``lexical_only`` — the degraded mode
    when the server has no embedding model installed.

    Behaviours that make a reasonable query miss — search the
    document's TEXT first:

    * **file names are a fallback, not an index.** The words inside a
      document are matched first; file names are tried only when no
      passage explains more of the question than a name does, and such a hit comes back with
      ``matched_on: "filename"`` and ``confidence: "low"``. Treat it as
      "this file is probably the one you mean", not as a quote.
    * **matching is whole word.** ``test`` does not find ``Testovaci``.
    * **there is no wildcard.** ``*`` and an empty query return nothing,
      not everything — there is no "list all chunks" query. Use
      ``collection_get`` to enumerate a collection's files.

    An empty ``results`` therefore does NOT mean you lack access. The
    response carries ``searched_collections`` and a ``hint`` saying which
    case you are in; read them before concluding anything about
    permissions.
    """
    params: dict = {"q": query, "k": k}
    if collection_id:
        params["corpus_id"] = collection_id
    try:
        return api_get_json("/api/collections/search", **params)
    except V2ClientError as exc:
        raise ValueError(_mcp_error("collections_search", exc)) from exc


@tool(read_only=True)
def knowledge_search(query: str, k: int = 10) -> dict:
    """One query across documents, the knowledge base, and the data catalog. Matching is whole word, there is no wildcard, and file names are a fallback, consulted only when no passage explains the question better — so an empty result is a wording miss far more often than an access problem; read the response's ``hint`` before concluding anything from it.

    Fans out server-side over Collections chunks (hybrid lexical+vector),
    corporate-memory knowledge items (fulltext), and table catalog cards —
    all RBAC-filtered. Results are typed ``chunk | knowledge | table``;
    a ``table`` hit means structured data: pivot to SQL via the ``query``
    tool with the hit's ``table_id`` instead of reading text chunks.

    The response's ``retrieval`` field labels the chunk engine's mode:
    ``hybrid`` (lexical + semantic) or ``lexical_only`` — the degraded mode
    when no embedding model is installed where the ranking ran.

    The chunk leg carries the same surprises as ``collections_search``:
    matching is whole word, there is no wildcard, and file names are a
    fallback, consulted only when no passage explains the question better. An empty result is not evidence that
    you lack access — check the ``hint`` before saying so.

    Offline fallback (K3, #798): if the server is unreachable (network/VPN
    down), falls back to `agnes pull`-shipped knowledge artifacts under
    `user/knowledge/` and runs the same hybrid ranking locally — documents
    (``chunk``) only, no ``knowledge``/``table`` hits. The response then
    carries ``source: "local"`` and a ``note`` explaining the degradation.
    An HTTP error from a reachable server (``V2ClientError``) is NOT a
    fallback trigger — the server answered, its error is the truth.
    """
    try:
        return api_get_json("/api/knowledge/search", q=query, k=k)
    except V2ClientError as exc:
        raise ValueError(_mcp_error("knowledge_search", exc)) from exc
    except httpx.TransportError as exc:
        # Server unreachable (offline laptop, VPN down) — fall back to the
        # artifacts `agnes pull` shipped. Same hybrid scoring, chunk source
        # only; HTTP errors above do NOT fall back (the server answered).
        from cli.config import get_workspace_root
        from src.search.local import local_search

        ws = get_workspace_root()
        if not ws:
            raise ValueError(
                f"knowledge_search failed: server unreachable ({exc}) and no "
                "local workspace configured — run `agnes init` + `agnes pull`."
            ) from exc
        from src.ingest.retrieval import retrieval_mode

        results = local_search(query, workspace=Path(ws), k=k)
        return {
            "query": query,
            "results": results,
            # Mode of the LOCAL ranking that just ran — the laptop may lack
            # the embeddings extra even when the server has it.
            "retrieval": retrieval_mode(),
            "source": "local",
            "note": "server unreachable — searched local knowledge artifacts (documents only)",
        }


@tool(read_only=True)
def collection_file_read(collection_id: str, file_id: str) -> dict:
    """Read one file's text straight, without guessing search terms.

    Use this when you know WHICH file you want — "what is in this
    document?", "summarise this upload". ``collections_search`` is for when
    you do not: it needs words that appear in the body, and it cannot
    enumerate a collection (see its own note).

    Returns ``kind`` plus, for readable files, ``text`` and ``truncated``.
    The server caps the text (~20k characters), so ``truncated: true`` means
    you are holding a PREFIX — do not summarise it as the whole document;
    fall back to ``collections_search`` with a distinctive term to reach the
    rest. Read ``text`` regardless of ``kind``: ``kind`` describes how a
    BROWSER would show the file (``text`` / ``pdf`` / ``image``), and a PDF
    comes back ``kind="pdf"`` while still carrying its ingested text. When
    ``text`` is empty, ``reason`` says why (still ingesting, rejected, or
    nothing extractable) — relay that rather than reporting an access error.

    Do not loop this over a whole collection: reading many files to answer
    one question is what retrieval is for.

    Args:
        collection_id: Collection id from ``collections_list`` (``col_...``).
        file_id: File id from ``collection_get`` (``cf_...``).
    """
    try:
        return api_get_json(f"/api/collections/{collection_id}/files/{file_id}/preview")
    except V2ClientError as exc:
        raise ValueError(_mcp_error("collection_file_read", exc)) from exc


@tool(read_only=False)
def collections_reingest(collection_id: str, file_id: str) -> dict:
    """Re-run ingestion for one file in a Collection (requires access to the collection).

    Use after the file or extraction config was fixed — e.g. a file stuck
    in ``needs_review`` (empty extraction) or ``rejected``. Returns the file
    row reset to ``pending``; ingestion runs server-side in the background.

    Args:
        collection_id: Collection id from ``collections_list`` (``col_...``).
        file_id: File id from ``collection_get`` (``cf_...``).
    """
    try:
        return api_post_json(f"/api/collections/{collection_id}/files/{file_id}/reingest", {})
    except V2ClientError as exc:
        raise ValueError(_mcp_error("collections_reingest", exc)) from exc


@tool(read_only=True)
def schema(table_id: str) -> dict:
    """Show column names, types, and SQL dialect hints for a table.

    Args:
        table_id: Table ID from the catalog (e.g. ``crm_accounts``).

    Returns column list with ``name``, ``type``, ``nullable``,
    ``description``.  Also returns ``sql_flavor``, ``partition_by``,
    ``clustered_by``, and ``where_dialect_hints`` where relevant.

    Call this before writing a query — knowing column types avoids
    casting errors and helps pick the right SQL dialect.
    """
    try:
        return api_get_json(f"/api/v2/schema/{table_id}")
    except V2ClientError as exc:
        raise ValueError(_mcp_error(f"schema({table_id})", exc)) from exc


@tool(read_only=True)
def describe(table_id: str, rows: int = 5) -> dict:
    """Show schema plus sample rows for a table.

    Args:
        table_id: Table ID from the catalog.
        rows:     How many sample rows to return (default 5, max 50).

    Returns ``{"schema": {...}, "sample": {"table_id": ..., "rows": [...],
    "source": ..., "row_scope": {"policied_tables": [...], "note": str} |
    None}}`` where ``sample.rows`` is a list of ``{column: value}`` objects
    (empty when the table has no rows — there is no ``columns`` key; column
    names come from ``schema.columns``), so you can see real values before
    writing a query. ``sample.row_scope`` is present when this table has an
    access policy applied — the rows are YOUR scoped slice, not the whole
    table. When present, state that qualification in your answer; never
    present a count or aggregate over the sample as an organisation-wide
    figure.
    """
    rows = min(max(1, rows), 50)
    try:
        sch = api_get_json(f"/api/v2/schema/{table_id}")
        sam = api_get_json(f"/api/v2/sample/{table_id}", n=rows)
    except V2ClientError as exc:
        raise ValueError(_mcp_error(f"describe({table_id})", exc)) from exc
    return ensure_output_size(
        {"schema": sch, "sample": sam},
        "describe",
        hint="lower `rows` or select specific columns with the query tool",
    )


@tool(read_only=True)
def query(sql: str, limit: int = 1000) -> dict:
    """Execute a SQL query against Agnes data.

    For ``query_mode=local`` and ``materialized`` tables the query runs
    against the server-side DuckDB view.  For ``query_mode=remote``
    (BigQuery) it passes through to BigQuery.

    Routes local/materialized vs remote tables automatically server-side;
    prefer this tool unless you specifically need offline access.

    Args:
        sql:   SQL statement to execute.  Use DuckDB dialect for local /
               materialized tables; BigQuery dialect for remote tables
               (check ``sql_flavor`` in the catalog entry).
        limit: Maximum rows to return (default 1000).

    Returns ``{"columns": [...], "rows": [[...], ...], "truncated": bool,
    "row_scope": {"policied_tables": [...], "note": str} | None}``.
    ``row_scope`` is present when a table this query touched has an access
    policy applied — the result is YOUR scoped slice, not the whole table.
    When present, state that qualification in your answer; never present an
    aggregate over the result as an organisation-wide figure.

    Tips:
    - Always run ``catalog()`` first to know what tables exist.
    - Run ``schema(table_id)`` before writing a query — column names
      and types are essential.
    - Prefer filtered queries over ``SELECT *`` — remote tables can be
      very large.
    """
    try:
        result = api_post_json("/api/query", {"sql": sql, "limit": limit})
    except V2ClientError as exc:
        raise ValueError(_mcp_error("query", exc)) from exc
    return ensure_output_size(result, "query")


@tool(read_only=True)
def query_local(sql: str, limit: int = 1000) -> dict:
    """Execute a SQL query directly against the local DuckDB cache.

    Use this for ``query_mode=local`` / ``materialized`` tables after
    ``pull()`` has synced data to disk.  Runs entirely offline — no
    server request is made.

    If a table is missing here it may be a ``query_mode='remote'`` or
    ``server_only`` table — use the ``query`` tool instead.

    Args:
        sql:   DuckDB-flavoured SQL.
        limit: Maximum rows to return (default 1000).

    Returns ``{"columns": [...], "rows": [[...], ...]}`` or raises if
    the local DuckDB file does not exist (run ``pull()`` first).

    The workspace is resolved from AGNES_LOCAL_DIR, else the current
    directory when it is a workspace, else the workspace anchored by
    ``agnes init`` — so this works regardless of where the MCP client
    spawned the process.
    """
    from cli.lib.workspace_resolve import resolve_data_workspace

    workspace = resolve_data_workspace()
    if workspace is None:
        raise FileNotFoundError(
            "No Agnes workspace found (checked AGNES_LOCAL_DIR, the current "
            "directory, and the anchored workspace_root). Run `agnes init` "
            "on this machine first."
        )
    db_path = workspace / "user" / "duckdb" / "analytics.duckdb"
    if not db_path.exists():
        raise FileNotFoundError(f"Local DuckDB not found at {db_path}. Run pull() first to sync data.")

    with _open_duckdb(str(db_path), read_only=True) as conn:
        # Apply LIMIT at the DuckDB level to protect against accidental
        # full-table scans on large cached parquets.
        # A trailing `;` is legal at the top level and fatal inside this wrap.
        # Same one-character rule the four server-side embed sites apply; the
        # shared helper is why this one no longer drifts from them.
        wrapped = f"SELECT * FROM ({strip_one_trailing_semicolon(sql)}) AS _q LIMIT {limit}"
        try:
            result = conn.execute(wrapped)
            columns = [d[0] for d in result.description]
            rows = result.fetchall()
        except Exception as exc:
            table = missing_table(str(exc))
            if table:
                raise ValueError(f"query_local failed: {exc}\n{remote_table_hint(table, surface='mcp')}") from exc
            raise

    return ensure_output_size(
        {
            "columns": columns,
            "rows": [list(r) for r in rows],
            "row_count": len(rows),
            "truncated": len(rows) == limit,
        },
        "query_local",
    )


@tool(read_only=False)
def chat_upload_file(
    file_path: str,
    kind: str = "data",
    register_as_table: bool = False,
    table_name: str = "",
) -> dict:
    """Push a file from THIS machine into your chat workspace (POST /api/chat/uploads).

    **Direction: this machine → workspace. Not a way to hand a file to the
    user.** An agent inside a chat sandbox that has produced a chart or a
    report cannot deliver it with this tool — the path it would name is the
    sandbox's, not the reader's, and this server is not running there anyway
    (it fails with "No Agnes token configured"). Put the result in the reply:
    inline ``<svg>`` for a chart, a ```mermaid fence for a diagram, a markdown
    table for figures.

    The file at ``file_path`` (local filesystem path) is read and posted to
    the Agnes server, landing in your per-user workspace ``uploads/`` folder
    so Claude can access it in the next chat sandbox session.

    For data files (CSV, parquet, XLSX) set ``register_as_table=True`` to
    register the file as a workspace-local queryable table so
    ``agnes query`` can reach it without an admin table-registry entry.

    Args:
        file_path: Local path to the file to upload.
        kind: One of ``data``, ``image``, ``document``. Default ``data``.
        register_as_table: When True (data files only), register the uploaded
            file as a workspace-local queryable table.
        table_name: Optional table name for registration. Derived from the
            filename stem when omitted.

    Returns the upload response with ``workspace_path``, ``filename``,
    ``size_bytes``, ``kind``, ``table_name`` (if registered), and ``hint``.

    Mirrors ``POST /api/chat/uploads`` and ``agnes chat upload``.
    """
    import mimetypes

    path = Path(file_path)
    if not path.exists():
        raise ValueError(f"File not found: '{file_path}'. Check the path and try again.")

    content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    data: dict[str, str] = {"kind": kind}
    if register_as_table:
        data["register_as_table"] = "true"
    if table_name:
        data["table_name"] = table_name

    server_url = get_server_url()
    token = get_token()
    if not token:
        raise ValueError("No Agnes token configured. Run setup.py from Terminal to authenticate.")

    import httpx

    with path.open("rb") as fh, httpx.Client() as c:
        r = c.post(
            f"{server_url}/api/chat/uploads",
            headers={"Authorization": f"Bearer {token}"},
            data=data,
            files={"file": (path.name, fh, content_type)},
            timeout=60,
        )
    r.raise_for_status()
    return r.json()


# Destructive because a pull PRUNES: tables the caller has lost access to are
# removed locally and their snapshot views blocked. Server-driven revocation
# rather than a requested delete, but the hint describes what the tool may do
# to local state, and this is not a purely additive update.
@tool(read_only=False, destructive=True, idempotent=True)
def pull(skip_materialize: bool = False) -> dict:
    """Sync the latest data from the Agnes server to local disk.

    Downloads parquets for all ``local`` and ``materialized`` tables
    visible to your account (RBAC-filtered), then rebuilds the local
    DuckDB view so ``query_local`` picks up the changes.

    Args:
        skip_materialize: Skip large materialized-mode (scheduled BQ
            export) tables — useful for a fast first sync when you only
            need remote-mode access.

    Returns a summary: ``{"tables_updated": N, "parquets_total": N,
    "errors": [...], "duration_s": N}``.

    Run at the start of a session to make sure local data is fresh.
    Equivalent to ``agnes pull`` on the command line.

    The workspace is resolved from AGNES_LOCAL_DIR, else the current
    directory when it is a workspace, else the workspace anchored by
    ``agnes init`` — so this works regardless of where the MCP client
    spawned the process.
    """
    # Imported inside the function (not at module scope) so tests can patch
    # ``cli.lib.pull.run_pull`` and have the patch take effect at call time.
    from cli.lib.pull import run_pull

    server_url = get_server_url()
    token = get_token()
    if not token:
        raise ValueError("No Agnes token configured. Run setup.py from Terminal to authenticate.")

    from cli.lib.workspace_resolve import resolve_data_workspace

    workspace = resolve_data_workspace()
    if workspace is None:
        raise FileNotFoundError(
            "No Agnes workspace found (checked AGNES_LOCAL_DIR, the current "
            "directory, and the anchored workspace_root). Run `agnes init` "
            "on this machine first."
        )
    result = run_pull(
        server_url,
        token,
        workspace,
        skip_materialize=skip_materialize,
        show_progress=False,
    )
    return {
        "tables_updated": result.tables_updated,
        # Surface prune counts so MCP clients can detect that tables were
        # removed from the workspace (security-relevant — revokes local
        # query access). Was missing in the original #594 (Devin Review).
        "tables_removed": result.tables_removed,
        "parquets_total": result.parquets_total,
        # Same reason as `tables_removed`: a withheld snapshot name silently
        # stops resolving, and an MCP caller has no other way to learn why
        # (#1129 review).
        "snapshot_views_blocked": list(getattr(result, "snapshot_views_blocked", []) or []),
        "errors": result.errors,
        # `PullResult.duration_s` is the wall-clock duration of the call.
        # Was historically referenced here as `result.elapsed_s` with a
        # `hasattr` guard that always returned False — every MCP `pull`
        # response returned `"elapsed_s": None` regardless of how long
        # the call took (Devin Review BUG_0001 on #594). Renamed the key
        # to `duration_s` to match `PullResult` + `--json` output.
        "duration_s": round(result.duration_s, 1),
    }


# ── hosted data apps ─────────────────────────────────────────────────────────
#
# The in-chat authoring agent connects through THIS stdio server (spawned as
# `agnes mcp` by app/chat/runner.py), not the HTTP foundation transports, so the
# whole data-app family — including the chat-surface render directives — must be
# registered here. The action tools mirror app/api/mcp/foundation_tools.py
# (same REST endpoints, same return shapes) via the sync CLI HTTP helpers; the
# render tools return the same fixed directive JSON the web chat frontend
# switches on. Parity is guarded by tests/test_mcp_tool_parity.py
# (DATA_APP_TOOL_NAMES ⊆ this server's tools).


def _data_apps_disabled_payload() -> dict:
    return {
        "error": "data_apps_disabled",
        "message": "Data apps are disabled on this instance.",
    }


def _is_data_apps_disabled(exc: V2ClientError) -> bool:
    """True when a V2ClientError is the server's ``data_apps_disabled`` 404."""
    if exc.status_code != 404:
        return False
    body = exc.body
    return isinstance(body, dict) and body.get("detail") == "data_apps_disabled"


@tool(read_only=True)
def data_apps_list(kind: Literal["", "hosted", "linked"] = "") -> dict:
    """List data apps you can see (RBAC-filtered).

    Visible to any authenticated user: apps you own, apps a group you're in has
    a ``resource_grants`` row for, or (Admin) all apps. Each entry has a
    ``kind`` — ``hosted`` (an app Agnes runs) or ``linked`` (an externally-hosted
    app, e.g. on Keboola, whose ``url`` opens the remote app directly). Returns a
    list of app summaries — ``slug``, ``name``, ``kind``, ``state``, ``url``,
    ``effective_description``, metadata; secrets are never included.

    Args:
        kind: Optional filter — ``"hosted"`` or ``"linked"``; empty (default)
              lists both.

    Mirrors ``GET /api/data-apps[?kind=]`` and ``agnes app list [--linked]``.
    """
    try:
        return api_get_json("/api/data-apps", **({"kind": kind} if kind else {}))
    except V2ClientError as exc:
        raise ValueError(_mcp_error("data_apps_list", exc)) from exc


@tool(read_only=True)
def data_app_get(slug: str) -> dict:
    """Show one hosted data app's detail.

    Any authenticated user with view access (owner, Admin, or a group granted
    access via ``resource_grants``) may call this. A prod app inlines its
    ``drafts`` for the owner.

    Args:
        slug: The app's slug (from ``data_apps_list``).

    Mirrors ``GET /api/data-apps/{slug}`` and ``agnes app show``.
    """
    try:
        return api_get_json(f"/api/data-apps/{slug}")
    except V2ClientError as exc:
        raise ValueError(_mcp_error(f"data_app_get({slug})", exc)) from exc


@tool(read_only=False, open_world=True)
def data_app_deploy(slug: str, sha: str = "", mode: Literal["", "dev"] = "") -> dict:
    """Deploy (or redeploy) a hosted data app — app owner or Admin only.

    Fast-forwards the app's ``agnes-live`` ref (to ``sha`` if given, else the
    tracked branch's latest), mints a fresh service token, and hands the build
    to the runner sidecar. ``mode="dev"`` deploys a draft on its pinned branch
    instead (a draft has no ``agnes-live`` ref, so ``sha`` is ignored there).

    Args:
        slug: The app's slug (a prod app's slug, or a draft's own slug when
              ``mode="dev"``).
        sha:  Optional commit sha. Empty (default) fast-forwards to the tracked
              branch's latest. Ignored for draft deploys.
        mode: ``"dev"`` deploys a draft's branch; empty (default) deploys prod.

    Returns ``{"state": "running", "deployed_sha": "..."}``. Mirrors
    ``POST /api/data-apps/{slug}/deploy`` and ``agnes app deploy``.
    """
    payload: dict = {}
    if sha:
        payload["sha"] = sha
    if mode:
        payload["mode"] = mode
    try:
        return api_post_json(f"/api/data-apps/{slug}/deploy", payload)
    except V2ClientError as exc:
        raise ValueError(_mcp_error(f"data_app_deploy({slug})", exc)) from exc


@tool(read_only=False)
def data_app_create(slug: str, name: str, description: str = "") -> dict:
    """Create a new hosted data app (the registry row plus its git repo).

    The FIRST step of building an app, and the one this surface was missing:
    ``data_app_create_draft`` needs an app to draft FROM, so an agent starting
    there got ``404 data_app_not_found`` with no way forward.

    The app is created empty. Seed it before deploying: clone with
    ``data_app_git_credential``, copy the baked scaffold from
    ``/work/scaffolds/nodejs-dashboard/``, push to ``main``, then
    ``data_app_deploy``. Deploying an empty repo fails with
    ``deploy_empty_repo``.

    Args:
        slug:        URL-safe id, unique per instance (``[a-z0-9-]``).
        name:        Human-readable title shown in the UI.
        description: Optional one-line summary.

    Returns ``{"id", "slug", "git_url"}``. Mirrors ``POST /api/data-apps`` and
    ``agnes app create``.
    """
    try:
        return api_post_json("/api/data-apps", {"slug": slug, "name": name, "description": description})
    except V2ClientError as exc:
        raise ValueError(_mcp_error(f"data_app_create({slug})", exc)) from exc


@tool(read_only=False)
def data_app_create_draft(slug: str, branch: str = "init") -> dict:
    """Create a draft of a prod data app on an iteration branch — owner/Admin only.

    The draft shares the prod app's git repo (no second repo, no copy): a
    registry sibling row pinned to ``branch`` on the parent's repo, deployable
    with ``data_app_deploy(draft_slug, mode="dev")``. Drafts are hidden from
    ``data_apps_list`` — reach them via the parent's ``drafts`` field in
    ``data_app_get``.

    Args:
        slug:   The PROD app's slug (must not itself be a draft).
        branch: Iteration branch name (default ``"init"``).

    Returns ``{"id", "slug", "branch", "git_clone_url"}`` — the draft's slug and
    a git push credential embedded in the clone URL. Mirrors
    ``POST /api/data-apps/{slug}/drafts`` and ``agnes app draft create``.
    """
    try:
        return api_post_json(f"/api/data-apps/{slug}/drafts", {"branch": branch})
    except V2ClientError as exc:
        raise ValueError(_mcp_error(f"data_app_create_draft({slug})", exc)) from exc


@tool(read_only=False, destructive=True)
def data_app_delete_draft(slug: str, draft_slug: str) -> dict:
    """Tear down a draft of a prod data app — app owner or Admin only.

    Stops the draft's container, revokes its service token, deletes the
    iteration branch on the parent's repo, and removes the draft's registry row.

    Args:
        slug:       The PROD app's slug (the draft's parent).
        draft_slug: The draft's own slug (from ``data_app_create_draft`` or the
                    parent's ``drafts`` field).

    Returns ``{"status": "deleted"}``. Mirrors
    ``DELETE /api/data-apps/{slug}/drafts/{draft_slug}`` and
    ``agnes app draft delete``.
    """
    try:
        api_delete(f"/api/data-apps/{slug}/drafts/{draft_slug}")
    except V2ClientError as exc:
        raise ValueError(_mcp_error(f"data_app_delete_draft({slug})", exc)) from exc
    return {"status": "deleted"}


@tool(read_only=False)
def data_app_git_credential(slug: str) -> dict:
    """Mint a fresh git push credential for a data app — app owner or Admin only.

    Args:
        slug: The app's slug (a prod app; drafts push through the same
              parent-repo credential).

    Returns ``{"git_clone_url": "..."}`` with an embedded, time-scoped push
    credential. Mirrors ``POST /api/data-apps/{slug}/git-credential`` and
    ``agnes app git-credential``.
    """
    try:
        return api_post_json(f"/api/data-apps/{slug}/git-credential", {})
    except V2ClientError as exc:
        raise ValueError(_mcp_error(f"data_app_git_credential({slug})", exc)) from exc


@tool(read_only=True)
def data_app_logs(slug: str, tail: int = 200) -> dict:
    """Show the last N lines of runner logs for a hosted data app — owner/Admin only.

    Args:
        slug: The app's slug.
        tail: Number of trailing log lines to return (default 200).

    Returns ``{"logs": "..."}``. Mirrors ``GET /api/data-apps/{slug}/logs`` and
    ``agnes app logs``.
    """
    try:
        return api_get_json(f"/api/data-apps/{slug}/logs", tail=tail)
    except V2ClientError as exc:
        raise ValueError(_mcp_error(f"data_app_logs({slug})", exc)) from exc


@tool(read_only=False, idempotent=True)
def data_app_set_description(slug: str, description: str) -> dict:
    """Set the admin description override on a managed (linked) data app.

    Linked apps are org resources whose ``description`` the ingest sync
    refreshes; this pins a human-authored description the sync won't clobber.
    Owner/Admin only; managed rows only (a 409 comes back for a hosted app).

    Args:
        slug:        The app's slug.
        description: The description to pin (empty string clears it).

    Returns the updated app dict. Mirrors ``PATCH /api/data-apps/{slug}`` and
    ``agnes app set-description``.
    """
    try:
        return api_patch_json(f"/api/data-apps/{slug}", {"description": description})
    except V2ClientError as exc:
        raise ValueError(_mcp_error(f"data_app_set_description({slug})", exc)) from exc


@tool(read_only=False)
def agnes_data_app_preview(slug: str, url: str = "") -> dict:
    """Open or refresh the in-chat split-pane preview of a hosted data app.

    Chat-surface render directive — the chat runner forwards this tool's return
    value verbatim into a directive the web chat frontend switches on (outside
    the web chat, e.g. a local terminal, the directive is inert).

    Call this TWICE per preview cycle: first with an empty ``url`` (the default)
    the moment a scaffold/dev deploy is kicked off — this opens a placeholder
    pane immediately, before the app is reachable. Once the dev deploy is
    healthy (poll ``data_app_get`` in short steps), call again with the real
    ``url`` (typically ``/apps/<slug>/``) to swap the pane to the live app — the
    second call mints a short-TTL scoped preview grant
    (``POST /api/data-apps/{slug}/preview-grant``) so the iframe loads without a
    cross-origin login.

    Args:
        slug: The (draft or prod) app's slug.
        url:  Empty (default) for the placeholder call; the app's URL (e.g.
              ``/apps/<slug>/``) to swap to the live pane.

    Returns ``{"render": "data_app_preview", "slug", "url"}`` — ``url`` is
    ``null`` on the placeholder call. The live-URL call mints the scoped preview
    cookie server-side (installed via the grant endpoint's ``Set-Cookie``
    header; the web chat re-fetches it same-origin), but the token value is
    deliberately NOT returned — a tool result is archived in the session
    transcript, and this is a live bearer credential. Returns a friendly
    ``data_apps_disabled`` payload (not an error) if data apps are disabled on
    this instance.
    """
    if not url:
        return {"render": "data_app_preview", "slug": slug, "url": None}
    try:
        # Validates view access (403 -> raises) and installs the scoped cookie
        # via the grant endpoint's Set-Cookie header. The cookie value is
        # intentionally discarded, not surfaced: the render directive the web
        # chat needs carries only slug + url, and the frontend lands the
        # HttpOnly cookie itself via a same-origin re-fetch of the endpoint.
        api_post_json(f"/api/data-apps/{slug}/preview-grant", {})
    except V2ClientError as exc:
        if _is_data_apps_disabled(exc):
            return _data_apps_disabled_payload()
        raise ValueError(_mcp_error(f"agnes_data_app_preview({slug})", exc)) from exc
    return {"render": "data_app_preview", "slug": slug, "url": url}


@tool(read_only=False, idempotent=True)
def agnes_data_app_refresh(slug: str) -> dict:
    """Force-reload the in-chat preview pane for a hosted data app.

    Chat-surface render directive — no server round-trip. Call after pushing a
    fresh commit to a draft's dev deploy so the iframe picks up the change
    without the user manually reloading.

    Args:
        slug: The app's slug the currently-open pane is showing.

    Returns ``{"render": "data_app_preview_refresh", "slug"}``.
    """
    return {"render": "data_app_preview_refresh", "slug": slug}


@tool(read_only=False, destructive=True)
def agnes_data_app_close(slug: str) -> dict:
    """Tear down the in-chat preview pane for a hosted data app.

    Chat-surface render directive — no server round-trip. Call this BEFORE
    ``data_app_delete_draft`` when abandoning or promoting a draft — closing the
    pane first avoids the iframe pointing at a container that's about to
    disappear.

    Args:
        slug: The app's slug the currently-open pane is showing.

    Returns ``{"render": "data_app_preview_close", "slug"}``.
    """
    return {"render": "data_app_preview_close", "slug": slug}


@tool(read_only=True)
def agnes_data_app_credentials(slug: str) -> dict:
    """Show the shareable URL for a hosted data app — a terminal render directive.

    Chat-surface render directive.

    Args:
        slug: The app's slug.

    Returns ``{"render": "data_app_credentials", "slug", "url", "password"}``.
    ``password`` is always ``null`` today — the control-plane detail endpoint
    this calls never returns the encrypted secrets blob (by design), so there is
    no shared basic-auth password to surface; hint at granting a group access
    via ``/admin/access`` instead. Returns a friendly ``data_apps_disabled``
    payload (not an error) if data apps are disabled on this instance.
    """
    try:
        detail = api_get_json(f"/api/data-apps/{slug}")
    except V2ClientError as exc:
        if _is_data_apps_disabled(exc):
            return _data_apps_disabled_payload()
        raise ValueError(_mcp_error(f"agnes_data_app_credentials({slug})", exc)) from exc
    return {
        "render": "data_app_credentials",
        "slug": slug,
        "url": detail.get("url"),
        "password": None,
    }


def _registered_tools() -> dict:
    """Tools FastMCP currently knows about, including dynamically added ones.

    FastMCP has moved this accessor around between versions, so probe rather
    than bind to one shape; an unexpected layout degrades to "no dynamic
    tools" instead of breaking tool_docs.
    """
    for attr in ("_tool_manager", "_tools"):
        holder = getattr(mcp, attr, None)
        if holder is None:
            continue
        tools = getattr(holder, "_tools", holder)
        if isinstance(tools, dict):
            return tools
    return {}


def _registered_tool_names() -> list:
    return list(_registered_tools())


def _registered_tool_doc(tool_name: str):
    tool = _registered_tools().get(tool_name)
    if tool is None:
        return None
    doc = getattr(tool, "description", None) or getattr(getattr(tool, "fn", None), "__doc__", None)
    return doc.strip() if isinstance(doc, str) and doc.strip() else None


@tool(read_only=True)
def tool_docs(tool_name: str) -> dict:
    """Return the full reference documentation (docstring) for one registered MCP tool — arguments, return shape, and usage tips beyond the short description shown in the tool list."""
    doc = TOOL_DOCS.get(tool_name)
    if doc is None:
        # Passthrough tools are registered at start-up from the server's
        # registry, so they are absent from the static TOOL_DOCS map. An
        # agent told to call tool_docs for any tool it sees in the listing
        # would otherwise be answered "Unknown tool" for exactly the ones
        # whose docs it cannot already read (review finding on #1144).
        doc = _registered_tool_doc(tool_name)
    if doc is None:
        known = ", ".join(sorted(set(TOOL_DOCS) | set(_registered_tool_names())))
        raise ValueError(f"Unknown tool {tool_name!r}. Valid tool names: {known}")
    return {"tool": tool_name, "docs": doc}


def run() -> None:
    """Entry point — start the MCP server (stdio transport).

    Before binding stdio we ask the configured Agnes server for the set of
    passthrough MCP tools the caller's groups can see, and dynamically
    register one FastMCP tool per entry that forwards through the server's
    ``/api/mcp/passthrough/tools/{tool_id}/call`` endpoint. Best-effort —
    a server outage or pre-Phase-2 image leaves the static tools above
    untouched (the dynamic helper logs to stderr and returns []).
    """
    try:
        # Local import keeps the module's top-level import surface light
        # for callers that only need the static tools (or import this
        # module for testing).
        from cli.mcp._dynamic_passthrough import register_passthrough_tools

        register_passthrough_tools(mcp)
    except Exception as exc:
        # Never let dynamic-registration explode the whole stdio surface.
        import sys as _sys

        print(f"[agnes mcp] dynamic passthrough registration skipped: {exc}", file=_sys.stderr)
    mcp.run()


if __name__ == "__main__":
    run()
