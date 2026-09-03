"""Dynamic registration of passthrough MCP tools on the stdio server.

Called once at ``run()`` start (see ``cli/mcp/server.py``). Behavior:

  1. ``GET /api/mcp/passthrough/tools`` to learn which tools the caller's
     groups have grants on.
  2. For each, synthesize a FastMCP tool whose body posts to
     ``/api/mcp/passthrough/tools/{tool_id}/call`` and returns the upstream
     text content.

Best-effort — any error (server unreachable, no PAT, /api/mcp/passthrough
not present on the server, schema not v61) leaves the static cowork
tools in place and logs to stderr. We never want a transient server
issue to break the stdio MCP for the analyst's session.

The synthesized signature mirrors ``app/api/mcp/tools_generator`` so AI
clients see typed parameters from the upstream JSON schema. The exec()
construction stays safe through the same strict identifier validation —
non-identifier prop names fall back to ``**kwargs`` for that tool.
"""
from __future__ import annotations

import keyword
import logging
import re
import sys
from typing import Any, Callable, Dict, List, Optional

from mcp.types import ToolAnnotations

from cli.v2_client import V2ClientError, api_get_json, api_post_json

logger = logging.getLogger(__name__)

_PY_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_ident(name: str) -> bool:
    return bool(_PY_IDENT_RE.match(name)) and not keyword.iskeyword(name)


def passthrough_annotations(mutating: Any) -> ToolAnnotations:
    """``tool_registry.mutating`` → MCP behaviour hints.

    A copy of ``app.api.mcp.tools_generator.passthrough_annotations`` — this
    module runs on the analyst's laptop and must not import the server side
    (duckdb, the repositories). ``tests/test_mcp_cli_dynamic_passthrough.py``
    pins the two to the same verdicts.
    """
    is_mutating = bool(mutating)
    return ToolAnnotations(readOnlyHint=not is_mutating, destructiveHint=is_mutating)


def _make_rest_passthrough_callable(
    tool_id: str,
    input_schema: Optional[Dict[str, Any]],
) -> Callable[..., Any]:
    """Return a callable that posts to ``/api/mcp/passthrough/tools/{tool_id}/call``.

    Mirrors ``tools_generator._make_passthrough_callable`` but forwards via
    HTTP REST rather than a direct ``connectors.mcp.client.call_tool_async``
    — the CLI runs on the analyst's laptop and has no DuckDB access.
    """
    props: Dict[str, Dict[str, Any]] = (input_schema or {}).get("properties") or {}
    required = set((input_schema or {}).get("required") or [])
    safe_props = [name for name in props if _safe_ident(name)]
    fallback_kwargs = len(safe_props) != len(props)

    def _forward(args: Dict[str, Any]) -> str:
        payload = {"arguments": {k: v for k, v in args.items() if v is not None}}
        resp = api_post_json(f"/api/mcp/passthrough/tools/{tool_id}/call", payload)
        if resp.get("is_error"):
            raise RuntimeError(f"upstream error: {str(resp.get('text', ''))[:300]}")
        return resp.get("text", "")

    # Empty schema (no props) must fall through to the synthesized
    # parameterless ``def _passthrough():`` below — only genuine
    # non-identifier prop names take the **kwargs wrapper. (FastMCP renders
    # **kwargs as a required field, which breaks no-arg calls.)
    if fallback_kwargs:
        def _wrap(**kwargs: Any) -> str:
            return _forward(kwargs)

        _wrap.__name__ = tool_id.replace(".", "_")
        _wrap.__doc__ = f"Passthrough to upstream MCP tool {tool_id!r}."
        return _wrap

    # Required params must precede optional ones in the synthesized
    # signature — Python rejects `def f(opt=None, req):` as
    # SyntaxError ("non-default argument follows default argument").
    # Same fix as `app/api/mcp/tools_generator.py` (Devin Review on
    # #474); single insertion-order loop here would have crashed the
    # stdio MCP server's dynamic-tool registration loop on any upstream
    # schema listing an optional property before a required one.
    sig_parts: List[str] = []
    for name in safe_props:
        if name in required:
            sig_parts.append(name)
    for name in safe_props:
        if name not in required:
            sig_parts.append(f"{name}=None")
    sig_str = ", ".join(sig_parts)
    arg_dict_items = ", ".join(f'"{n}": {n}' for n in safe_props)
    src = (
        f"def _passthrough({sig_str}):\n"
        f"    raw = {{{arg_dict_items}}}\n"
        f"    return __forward(raw)\n"
    )
    namespace: Dict[str, Any] = {"__forward": _forward}
    exec(src, namespace)  # noqa: S102 - synthesized source references only vetted idents
    fn = namespace["_passthrough"]
    fn.__name__ = tool_id.replace(".", "_")
    fn.__doc__ = f"Passthrough to upstream MCP tool {tool_id!r}."
    return fn


def register_passthrough_tools(mcp_instance) -> List[str]:
    """Best-effort registration of passthrough tools on the stdio MCP server.

    Returns the list of exposed names registered. Returns ``[]`` (silently)
    on any error so a transient server issue doesn't break the static tool
    surface for the analyst's session.
    """
    try:
        tools = api_get_json("/api/mcp/passthrough/tools")
    except V2ClientError as exc:
        # 404 = server doesn't have the passthrough router yet (pre-Phase 2
        # image), 401/403 = no/invalid PAT — both fine, fall through.
        logger.info("passthrough list unavailable: %s", exc)
        print(f"[agnes mcp] passthrough tools unavailable: {exc}", file=sys.stderr)
        return []
    except Exception as exc:
        logger.warning("passthrough list failed: %s", exc)
        print(f"[agnes mcp] passthrough tools unavailable: {exc}", file=sys.stderr)
        return []

    if not isinstance(tools, list):
        return []

    registered: List[str] = []
    for tool in tools:
        tool_id = tool.get("tool_id")
        exposed_name = tool.get("exposed_name")
        if not tool_id or not exposed_name:
            continue
        input_schema = tool.get("input_schema") if isinstance(tool.get("input_schema"), dict) else None
        description = tool.get("description") or (
            f"Passthrough to {tool.get('source_name', tool_id)}.{exposed_name}"
        )
        fn = _make_rest_passthrough_callable(tool_id, input_schema)
        # Namespace the exposed_name with source so two upstream servers
        # exposing identically-named tools (e.g. both have "search") don't
        # collide. mcp_http.py uses raw exposed_name; the cowork stdio
        # surface is a fresh process so we can be stricter here.
        client_exposed = f"{tool.get('source_name', 'src')}.{exposed_name}"
        try:
            mcp_instance.add_tool(
                fn,
                name=client_exposed,
                description=description,
                # Same readOnlyHint the server transports derive from the
                # row's `mutating` flag (issue #2161) — a client that
                # auto-approves reads needs it here too, and a server that
                # predates the `mutating` field in this listing yields a
                # tool that asks, never one that runs unasked.
                annotations=passthrough_annotations(tool.get("mutating", True)),
            )
        except Exception as exc:
            logger.warning("could not register passthrough tool %s: %s", client_exposed, exc)
            continue

        if input_schema:
            registered_tool = mcp_instance._tool_manager.get_tool(client_exposed)
            if registered_tool is not None:
                try:
                    registered_tool.parameters = input_schema
                except Exception:
                    pass

        registered.append(client_exposed)

    if registered:
        print(
            f"[agnes mcp] registered {len(registered)} passthrough tools: "
            f"{', '.join(registered)}",
            file=sys.stderr,
        )
    return registered
