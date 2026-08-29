"""Generate outbound MCP tools from ``tool_registry``.

For each passthrough-mode tool registered in ``tool_registry``, register a
FastMCP tool on the given server that forwards the call to the upstream MCP
source. The exposed name + JSON input schema from the registry are surfaced
verbatim to AI clients, so the upstream tool's contract reaches Claude
Desktop / Cursor / Cline unchanged.

Materialize-mode tools are NOT registered here — they live as DuckDB tables
in ``analytics.duckdb`` and are reachable via the existing generic ``query``
and ``catalog`` tools from @mf's foundation.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Iterable, List, Optional

from mcp.server.fastmcp import FastMCP

from connectors.mcp.client import call_tool_async
from src.repositories import mcp_sources_repo, tool_registry_repo
from src.repositories.tool_registry import PASSTHROUGH

logger = logging.getLogger(__name__)


_JSON_TYPE_TO_PY = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "array": "list",
    "object": "dict",
}


_PY_IDENT_RE = __import__("re").compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_ident(name: str) -> bool:
    """Reject anything that isn't a safe Python identifier — keeps exec() safe."""
    # Reserved words would shadow Python builtins (def, class, lambda, ...).
    return bool(_PY_IDENT_RE.match(name)) and not __import__("keyword").iskeyword(name)


async def _forward_with_gates(
    *,
    source: Dict[str, Any],
    original_name: str,
    arguments: Dict[str, Any],
    caller_user_id: Optional[str],
    tool_id: Optional[str],
) -> str:
    """Gate, forward, and redact a single passthrough call — the transport-side
    twin of ``app/api/mcp_passthrough.invoke_passthrough_tool``.

    When ``tool_id`` is set (every production registration), this re-fetches the
    live ``tool_registry`` row and runs the SAME authorization stack the REST
    endpoint runs — ``enforce_passthrough_access`` (grant → mutating → rate
    limit) — then applies the tool's ``pii_fields`` redaction to a successful
    result, so the SSE / Streamable-HTTP transports can't forward what REST
    would refuse. Re-fetching per call (not trusting the row captured at
    registration time) means an admin toggling ``mutating`` / ``rate_limit_pm``
    / grants / ``enabled`` takes effect immediately, matching REST.

    A gate denial surfaces as a ``RuntimeError`` — FastMCP renders it as a tool
    error to the MCP client. ``tool_id=None`` skips gating entirely; it exists
    only for the caller-less direct-synthesis unit tests.
    """
    pii_fields: Optional[List[str]] = None
    if tool_id is not None:
        from app.api.mcp_policy import (
            GrantDenied,
            MutatingNotAllowed,
            PerUserCredentialMissing,
            RateLimited,
            SourceUrlRefused,
            enforce_passthrough_access,
            enforce_per_user_credential,
            enforce_source_url_runtime_policy,
        )
        from src.repositories import mcp_sources_repo, tool_registry_repo
        from src.repositories.tool_registry import PASSTHROUGH

        fresh = tool_registry_repo().get(tool_id)
        if fresh is None or fresh.get("mode") != PASSTHROUGH or not fresh.get("enabled", True):
            raise RuntimeError(f"passthrough tool {tool_id!r} not found or disabled")
        try:
            enforce_passthrough_access(fresh, caller_user_id)
        except (GrantDenied, MutatingNotAllowed, RateLimited) as exc:
            raise RuntimeError(str(exc)) from exc
        # Re-fetch the source row fresh too (not the one captured at
        # registration time), so an admin disabling the source or flipping its
        # scope/credentials takes effect immediately — matching the REST
        # endpoint, which re-fetches per call. The closure-captured ``source``
        # is only a fallback for the caller-less unit-test path (tool_id=None).
        live_source = mcp_sources_repo().get(fresh["source_id"])
        if live_source is None or not live_source.get("enabled", True):
            raise RuntimeError(f"upstream MCP source for tool {tool_id!r} missing or disabled")
        source = live_source
        # Runtime twin of the admin-time url policy (#1216), behind
        # mcp.source_url_runtime_enforce (default off — see that switch's
        # description before turning it on). Refuses a credentialed dial to a
        # url the CURRENT policy would refuse, even on a row enabled before the
        # policy — or this switch — existed. Shared with the REST endpoint
        # (app/api/mcp_passthrough.py) via one helper so the two seams cannot
        # drift apart.
        try:
            enforce_source_url_runtime_policy(source)
        except SourceUrlRefused as exc:
            # Same split as the REST seam: the verdict (which embeds the
            # source's literal address) goes to the log, and the tool caller
            # — an MCP client, never an admin console — gets the friendly
            # sentence the analyst-reachable url-policy gate already uses
            # (Devin Review on PR #1301). An admin diagnosing this reads the
            # url_policy_verdict column, which flagged the row before this
            # switch was ever turned on.
            logger.warning(
                "mcp tool call refused for source %s: url failed the runtime policy (%s)",
                source.get("id"),
                exc.reason,
            )
            raise RuntimeError(f"{exc.source_name} is not configured correctly. Ask an admin to check it.") from exc
        # Same pre-forward per-user credential guard the REST endpoint runs, so a
        # per_user source without the caller's own credential fails closed with
        # an actionable message rather than an opaque upstream auth error.
        try:
            enforce_per_user_credential(source, caller_user_id)
        except PerUserCredentialMissing as exc:
            raise RuntimeError(str(exc)) from exc
        pii = fresh.get("pii_fields")
        pii_fields = pii if isinstance(pii, list) else None

    result = await call_tool_async(source, original_name, arguments=arguments, caller_user_id=caller_user_id)
    if result.is_error:
        raise RuntimeError(f"upstream tool {original_name!r} returned error: {result.text[:300]}")
    if pii_fields:
        from app.api.mcp_policy import redact_response

        text, _ = redact_response(text=result.text, data=result.data, pii_fields=pii_fields)
        return text
    return result.text


def _make_passthrough_callable(
    source: Dict[str, Any],
    original_name: str,
    input_schema: Optional[Dict[str, Any]],
    caller_id_fn: Optional[Callable[[], Optional[str]]] = None,
    tool_id: Optional[str] = None,
) -> Callable[..., Any]:
    """Build an async callable forwarding to the upstream MCP tool.

    FastMCP infers its validation Pydantic model from the function signature,
    so a single ``**kwargs`` wrapper makes every call fail validation. We
    materialize a real signature from ``input_schema.properties`` via exec()
    — required props are positional, optional get ``= None`` defaults.

    Property names from the upstream schema are validated against a strict
    identifier regex before being inserted into the synthesized source; any
    non-identifier prop name falls back to ``**kwargs`` for that tool. This
    keeps the exec() call free of upstream-controlled syntax.

    ``tool_id`` binds the closure to its ``tool_registry`` row so every call
    runs the shared RBAC + policy gate (``_forward_with_gates``); it is always
    set by ``register_passthrough_tools``. Omitting it (unit tests only) skips
    gating.
    """
    props: Dict[str, Dict[str, Any]] = (input_schema or {}).get("properties") or {}
    required = set((input_schema or {}).get("required") or [])

    safe_props = [name for name in props if _safe_ident(name)]
    fallback_kwargs = len(safe_props) != len(props)  # any unsafe key → use **kwargs

    # NOTE: only the genuine non-identifier case takes the **kwargs wrapper.
    # An EMPTY schema (no props) must fall through to the synthesized path
    # below, which emits a valid parameterless ``async def _passthrough():``.
    # Routing empty schemas here instead makes FastMCP render a *required*
    # ``kwargs`` field, so the only valid (empty) call 422s.
    if fallback_kwargs:

        async def _passthrough(**kwargs: Any) -> str:
            caller_user_id = caller_id_fn() if caller_id_fn else None
            return await _forward_with_gates(
                source=source,
                original_name=original_name,
                arguments=kwargs,
                caller_user_id=caller_user_id,
                tool_id=tool_id,
            )

        _passthrough.__name__ = original_name
        _passthrough.__doc__ = f"Passthrough to upstream MCP tool {original_name!r}."
        return _passthrough

    # Required params must precede optional ones in the synthesized
    # signature — Python rejects `def f(opt=None, req):` as
    # SyntaxError ("non-default argument follows default argument"),
    # and the SyntaxError lands at `exec()` below, OUTSIDE the per-tool
    # try/except that wraps `add_tool` — so a single upstream schema
    # listing an optional property before a required one would crash
    # the entire `register_passthrough_tools` loop and lose every
    # passthrough tool, not just the one with the bad property order.
    # Two-pass build keeps the synthesized callable parseable
    # regardless of upstream property ordering.
    # (Devin Review on #474)
    sig_parts: List[str] = []
    for name in safe_props:
        if name in required:
            sig_parts.append(name)
    for name in safe_props:
        if name not in required:
            sig_parts.append(f"{name}=None")
    sig_str = ", ".join(sig_parts)
    arg_dict_items = ", ".join(f'"{n}": {n}' for n in safe_props)

    # ``__forward`` runs the shared gate stack, forwards, and returns the final
    # (redacted) text — so the synthesized body just assembles the non-None
    # args and returns what the gate allows.
    src = (
        f"async def _passthrough({sig_str}):\n"
        f"    raw = {{{arg_dict_items}}}\n"
        f"    args = {{k: v for k, v in raw.items() if v is not None}}\n"
        f"    return await __forward(args)\n"
    )

    async def _forward(args: Dict[str, Any]) -> str:
        caller_user_id = caller_id_fn() if caller_id_fn else None
        return await _forward_with_gates(
            source=source,
            original_name=original_name,
            arguments=args,
            caller_user_id=caller_user_id,
            tool_id=tool_id,
        )

    namespace: Dict[str, Any] = {"__forward": _forward}
    exec(src, namespace)  # noqa: S102 - synthesized source only references vetted idents
    fn = namespace["_passthrough"]
    fn.__name__ = original_name
    fn.__doc__ = f"Passthrough to upstream MCP tool {original_name!r}."
    return fn


def register_passthrough_tools(
    mcp_instance: FastMCP,
    caller_id_fn: Optional[Callable[[], Optional[str]]] = None,
) -> List[str]:
    """Register every enabled passthrough tool from ``tool_registry`` on ``mcp_instance``.

    ``caller_id_fn`` resolves the current caller's user id at tool-call time.
    Each transport passes its own resolver so the synthesized closures —
    registered once at startup — can still forward caller identity into
    ``call_tool_async``. Scope differs by transport: Streamable-HTTP resolves
    per tool-call POST (off ``get_access_token()``), whereas SSE resolves the
    identity bound when the ``/sse`` connection was opened (the tool executes
    in that connection's task tree; harmless under the one-token-per-connection
    usage this serves). Without a resolver, a ``scope='per_user'`` source would
    see ``caller_user_id=None`` (the caller-less materialize signal) and resolve
    to the shared credential for every authenticated caller. Omit it only where
    no caller exists.

    Returns the list of exposed tool names that were registered (useful for
    logging and tests).
    """
    sources_repo = mcp_sources_repo()
    tools_repo = tool_registry_repo()

    registered: List[str] = []
    for tool in tools_repo.list_by_mode(PASSTHROUGH, enabled_only=True):
        source = sources_repo.get(tool["source_id"])
        if source is None or not source.get("enabled", True):
            logger.warning(
                "passthrough %s skipped — source %s missing/disabled", tool["exposed_name"], tool["source_id"]
            )
            continue

        input_schema = tool.get("input_schema") if isinstance(tool.get("input_schema"), dict) else None
        fn = _make_passthrough_callable(
            source, tool["original_name"], input_schema, caller_id_fn, tool_id=tool["tool_id"]
        )
        description = tool.get("description") or f"Passthrough to {source['name']}.{tool['original_name']}"

        try:
            mcp_instance.add_tool(fn, name=tool["exposed_name"], description=description)
        except Exception:
            logger.exception("failed to register passthrough tool %s", tool["exposed_name"])
            continue

        # Surface the upstream's JSON schema verbatim — covers cases where
        # the schema has descriptions/enums/constraints richer than what we
        # can express through plain Python parameter defaults.
        if input_schema:
            registered_tool = mcp_instance._tool_manager.get_tool(tool["exposed_name"])
            if registered_tool is not None:
                try:
                    registered_tool.parameters = input_schema
                except Exception:
                    logger.exception("could not patch parameters for %s", tool["exposed_name"])

        registered.append(tool["exposed_name"])

    if registered:
        logger.info("registered %d passthrough MCP tools: %s", len(registered), ", ".join(registered))
    return registered


def _allowed_passthrough_names(caller_user_id: Optional[str]) -> set[str]:
    """Exposed names of the passthrough tools ``caller_user_id`` may see —
    admin sees all enabled passthrough tools, everyone else the intersection
    of ``tool_grants`` with their groups, an unresolved caller sees none. This
    mirrors ``app/api/mcp_passthrough.py::_visible_passthrough_tools`` (the REST
    listing) so the transports and REST agree on visibility.

    A caller here is always a **bare user id** — the transports resolve one
    from their access token — which cannot express an agent-session caller
    ("the owner, minus this agent's ``connection`` scope"). Both transports
    therefore refuse a restricted principal outright (SSE:
    ``mcp_http._AuthMiddleware``; Streamable-HTTP: OAuth tokens only), and the
    sandbox reaches MCP through the principal-aware REST endpoints instead.
    If a transport ever starts resolving principals, it must pass the
    principal (not an id) into ``caller_authority`` here and in
    ``_forward_with_gates`` — otherwise the connection filter silently
    disappears on that path.
    """
    from app.auth.access import _user_group_ids, is_user_admin

    if not caller_user_id:
        return set()
    repo = tool_registry_repo()
    if is_user_admin(caller_user_id):
        return {t["exposed_name"] for t in repo.list_by_mode(PASSTHROUGH, enabled_only=True)}
    group_ids = list(_user_group_ids(caller_user_id))
    return {t["exposed_name"] for t in repo.list_passthrough_for_groups(group_ids)}


def install_grant_filtered_list_tools(
    mcp_instance: FastMCP,
    caller_id_fn: Optional[Callable[[], Optional[str]]] = None,
    passthrough_names: Optional[Iterable[str]] = None,
) -> Callable[[], Any]:
    """Replace the FastMCP ``tools/list`` handler with one that hides
    passthrough tools the current caller has no grant on.

    The transports register every enabled passthrough tool once at startup, so
    by default ``tools/list`` advertises all of them to any authenticated
    caller — leaking tool names / descriptions / input schemas regardless of
    ``tool_grants`` (invocation is already gated by ``enforce_passthrough_policy``;
    this closes the *visibility* gap so the SSE / Streamable listing matches the
    REST ``_visible_passthrough_tools`` intersection). Foundation tools are
    never passthrough-mode, so they always remain visible.

    The passthrough universe is fixed at install time — pass ``passthrough_names``
    (the ``register_passthrough_tools`` return value) so we don't re-query the
    registry on every ``tools/list``. This is also what makes the request-time
    path genuinely fail closed: the set of names to potentially hide is already
    known, so a runtime error resolving the caller's grants collapses ``allowed``
    to the empty set and the *entire* known universe is dropped (never the
    unfiltered list). If ``passthrough_names`` is omitted we snapshot it once
    here at install time.

    Re-registering on the low-level server replaces the handler
    (``request_handlers[ListToolsRequest] = handler``); call this AFTER
    ``register_passthrough_tools`` so the base handler already exists.
    """
    base_list_tools = mcp_instance.list_tools

    if passthrough_names is not None:
        universe = set(passthrough_names)
    else:
        try:
            universe = {t["exposed_name"] for t in tool_registry_repo().list_by_mode(PASSTHROUGH, enabled_only=True)}
        except Exception:
            # Can't determine the universe at install time → nothing to hide
            # yet, but log loudly: this is a security-visibility control.
            logger.exception("could not snapshot passthrough universe; tools/list filter disabled")
            universe = set()

    async def _filtered_list_tools():
        all_tools = await base_list_tools()
        if not universe:
            return all_tools
        try:
            caller_user_id = caller_id_fn() if caller_id_fn else None
            allowed = _allowed_passthrough_names(caller_user_id)
        except Exception:
            # Never let a filtering error expose the unfiltered list — fail
            # closed by dropping every passthrough tool in the known universe,
            # keeping foundation tools (which are never in the universe).
            logger.exception("passthrough tools/list filtering failed; hiding all passthrough tools")
            allowed = set()
        return [t for t in all_tools if t.name not in universe or t.name in allowed]

    mcp_instance._mcp_server.list_tools()(_filtered_list_tools)
    return _filtered_list_tools


def install_tool_call_audit(
    mcp_instance: FastMCP,
    caller_id_fn: Optional[Callable[[], Optional[str]]] = None,
) -> Callable[..., Any]:
    """Wrap every ``tools/call`` request with one ``mcp.tool_call`` audit
    row (F2c — audit-full-coverage plan, Task 5).

    Re-registers the low-level ``CallToolRequest`` handler exactly like
    ``install_grant_filtered_list_tools`` re-registers ``ListToolsRequest``
    above — the ONE dispatch point every tool invocation (foundation or
    passthrough) passes through on BOTH transports (SSE:
    ``app/api/mcp_http.py``, Streamable-HTTP: ``app/api/mcp_streamable.py``),
    so this is the single place a wrapper belongs instead of instrumenting
    ~80 individual tool bodies. Call once per transport, right after
    ``register_passthrough_tools``/``install_grant_filtered_list_tools`` —
    the exact call sites are each transport's ``_register_dynamic_tools``.

    Emits ``params={"tool": name, "args_hash": hash_args(arguments)}`` —
    never the raw arguments, which may carry SQL text, filter values, or
    upstream secrets. Also stamps ``audit_context.set_client_kind("mcp")``
    before dispatching: this wrapper IS the shared session/call-resolution
    point both transports install it from, so any audit row a tool writes
    directly in its OWN process — e.g. the ``fact_search``/``fact_neighbors``/
    ``fact_claims`` tools, which call ``facts_repo()`` in-process rather than
    self-calling over HTTP (see ``_facts_caller``'s docstring in
    ``foundation_tools.py``) — is correctly attributed to this MCP call
    instead of defaulting to ``client_kind="web"``.

    ``caller_id_fn`` mirrors ``register_passthrough_tools``'s resolver — an
    unresolvable caller (no id) skips the ``mcp.tool_call`` row rather than
    writing one under ``user_id=None``, matching the fallback middleware's
    own unauthenticated-skip rule (Task 2).
    """
    base_call_tool = mcp_instance.call_tool

    async def _audited_call_tool(name: str, arguments: Dict[str, Any]) -> Any:
        from src.audit_context import set_client_kind
        from src.audit_helpers import hash_args, log_safe

        set_client_kind("mcp")
        try:
            caller_user_id = caller_id_fn() if caller_id_fn else None
        except Exception:
            caller_user_id = None
        if caller_user_id:
            log_safe(
                user_id=caller_user_id,
                action="mcp.tool_call",
                resource=f"mcp_tool:{name}",
                params={"tool": name, "args_hash": hash_args(arguments)},
            )
        return await base_call_tool(name, arguments)

    mcp_instance._mcp_server.call_tool(validate_input=False)(_audited_call_tool)
    return _audited_call_tool
