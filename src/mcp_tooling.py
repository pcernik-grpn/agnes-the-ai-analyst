"""Shared helpers for the Agnes MCP servers (HTTP foundation + CLI stdio).

Two concerns, both about keeping MCP tool traffic inside a model's context
budget:

- ``summarize_docstring`` / ``progressive_tool`` — ship only the first
  docstring paragraph as the wire description on ``tools/list``; the full
  docstring stays available on demand via each server's ``tool_docs`` tool.
- ``ensure_output_size`` — hard cap on serialized tool output; over the cap
  the tool raises with actionable narrowing guidance instead of returning a
  payload that would flood the model's context.
- ``ensure_query_output_size`` — the same cap for a ``/api/query`` response,
  except the optional ``semantic_validation`` advisory is shortened and then
  dropped BEFORE the rows are: an advisory must never fail a query that would
  otherwise have returned.
"""

from __future__ import annotations

import inspect
import json
import os
from collections.abc import Callable, MutableMapping
from typing import Any

DEFAULT_MAX_OUTPUT_CHARS = 100_000
MAX_OUTPUT_CHARS_ENV = "AGNES_MCP_MAX_OUTPUT_CHARS"

QUERY_NARROW_HINT = "select specific columns, add a WHERE filter, lower `limit`, or aggregate server-side"


class MCPOutputTooLarge(ValueError):
    """Serialized tool output exceeded the configured cap."""


def max_output_chars() -> int:
    """Resolve the output cap: env override, else default. ``0`` disables."""
    raw = os.environ.get(MAX_OUTPUT_CHARS_ENV, "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return DEFAULT_MAX_OUTPUT_CHARS


def ensure_output_size(
    payload: Any,
    tool_name: str,
    *,
    hint: str = QUERY_NARROW_HINT,
    cap: int | None = None,
) -> Any:
    """Return ``payload`` unless its serialized size exceeds the cap.

    Raises :class:`MCPOutputTooLarge` with an actionable message instead of
    returning an oversized payload — no partial data, so an agent never
    computes over a silently-incomplete result.
    """
    effective = max_output_chars() if cap is None else cap
    if effective <= 0:
        return payload
    size = len(json.dumps(payload, default=str))
    if size > effective:
        raise MCPOutputTooLarge(
            f"{tool_name} response is ~{size:,} chars, over the {effective:,}-char "
            f"output cap. Narrow the request: {hint}."
        )
    return payload


# How many advisory warnings survive the shrink in
# ``ensure_query_output_size``. Enough to see the shape of the problem; the
# full list is one ``validate_semantic_query`` call away, and that call is not
# competing with the caller's rows for budget.
ADVISORY_KEPT_WARNINGS = 3


def ensure_query_output_size(payload: Any) -> Any:
    """``ensure_output_size`` for a ``/api/query`` response — but never let the
    ``semantic_validation`` advisory be what fails the query.

    ``ensure_output_size`` RAISES rather than truncating (no partial data, so
    an agent never computes over a silently-incomplete result). That contract
    is right for rows and wrong for an advisory: bolting one onto a
    borderline-sized result would turn a query that succeeded yesterday into
    an error today — an advisory that blocks a delivered answer is exactly
    what "soft enforcement" is not.

    So the advisory gives way first: full payload, else a summary-sized
    advisory, else no advisory at all. Only when the rows alone still exceed
    the cap does the original guard fire — unchanged behaviour, and its
    narrowing hint is then genuinely about the result.

    Shared by both MCP query surfaces (HTTP foundation + CLI stdio) so a
    borderline result cannot fail on one transport and succeed on the other.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("semantic_validation"), dict):
        return ensure_output_size(payload, "query")
    advisory: dict = payload["semantic_validation"]
    try:
        return ensure_output_size(payload, "query")
    except MCPOutputTooLarge:
        pass

    warnings = list(advisory.get("warnings") or [])
    shrunk = {
        "valid": advisory.get("valid"),
        "warnings": warnings[:ADVISORY_KEPT_WARNINGS],
        "locally_executable": advisory.get("locally_executable"),
        # One small string — keep it even in the shrunk form. Dropping it
        # here would silently strip the "this is a heuristic, not proof"
        # disclosure (Issue #1707 finding A18) exactly when payloads are
        # largest and a caller is least likely to also call
        # `validate_semantic_query` to see it. Only the full advisory drop
        # below (rows are the big part) also drops this.
        "detection": advisory.get("detection"),
        # Say it was cut, so a caller never reads a short list as the whole
        # story.
        "truncated": True,
        "truncated_note": (
            f"advisory shortened to fit the tool output cap ({len(warnings)} warnings total) — "
            "run `validate_semantic_query` for the full result"
        ),
    }
    try:
        return ensure_output_size({**payload, "semantic_validation": shrunk}, "query")
    except MCPOutputTooLarge:
        pass
    # The rows are what is big. Drop the advisory entirely and let the
    # pre-existing guard speak about the result itself.
    return ensure_output_size({**payload, "semantic_validation": None}, "query")


def summarize_docstring(doc: str | None) -> tuple[str, bool]:
    """Return ``(first paragraph as one line, has_more_content)``."""
    cleaned = inspect.cleandoc(doc or "").strip()
    if not cleaned:
        return "", False
    first, _sep, rest = cleaned.partition("\n\n")
    summary = " ".join(line.strip() for line in first.splitlines())
    return summary, bool(rest.strip())


# Titles for the tools whose NAME carries no verb. Agnes names tools
# `resource_action` (`collections_list`, `stack_subscribe`), which reads well and
# sorts usefully, but a dozen are bare nouns — `catalog`, `schema`, `skills` —
# and a reader picking from a tool list sees a noun with no clue what calling it
# does. OpenAI's submission guidance asks for verb-based, action-describing tool
# identity; renaming the tools themselves would break every configured client,
# so the ACTION lives in the title, which is the string a directory reviewer and
# a tool picker actually render.
#
# Shared by both MCP surfaces (HTTP foundation + CLI stdio) so a tool cannot be
# titled two different ways depending on how you connected.
TITLE_OVERRIDES: dict[str, str] = {
    "catalog": "List Available Tables",
    "fact_type_map": "List Fact Types With Counts",
    "fact_facets": "List Filterable Entity Values",
    "schema": "Get Table Schema",
    "skills": "List Skills",
    "chat_skills": "List Chat Skills",
    "tool_docs": "Get Tool Documentation",
    "documentation_api": "Get API Documentation",
    "pull": "Sync Data To This Machine",
    "stack_artefacts_candidates": "List Artefact Candidates",
    "admin_config_surface": "Get Config Surface",
    "admin_register_table": "Register Source Table",
    "store_compose_plugin": "Publish A Plugin Bundling Existing Items",
    "store_edit_markdown": "Update A Published Skill Or Agent From Markdown",
    "admin_semantic_layer_coverage": "Get Semantic Layer Coverage",
    "admin_semantic_coverage": "List Uncovered Semantic Tables",
    "semantic_model_coverage": "Get Cross-Domain Coverage",
    "semantic_model_coverage_tag": "Add Resource Source Tag",
    "semantic_model_coverage_untag": "Remove Resource Source Tag",
    "semantic_mutes_list": "List Silenced Semantic Layer Checks",
    "mute_semantic_check": "Silence Semantic Layer Check",
    "unmute_semantic_check": "Restore Semantic Layer Check",
    "semantic_layer_health": "Get Semantic Layer Health",
    "flag_semantic_issue": "Open Semantic Layer Issue Report",
    "semantic_feedback_resolve": "Close Semantic Layer Issue Report",
    "apply_semantic_model": "Apply Semantic Model Document",
    "data_app_git_credential": "Get Data App Git Credential",
    "agnes_data_app_credentials": "Get Data App Credentials",
    "server_info": "Check Server Connection",
    "store_status": "Get Store Submission Status",
    "marketplace_detail": "Get Marketplace Plugin Detail",
    "admin_store_lint_findings": "List Store Lint Findings",
    "agent_usage": "Get Agent Usage",
    "data_app_logs": "Get Data App Logs",
    "fact_neighbors": "Get Fact Neighbors",
    "fact_claims": "Get Fact Claims",
    "activity": "Get Activity Timeline",
}


def title_from_name(name: str) -> str:
    """``admin_job_enqueue`` → ``Admin Job Enqueue`` — a human-readable title.

    A name in ``TITLE_OVERRIDES`` wins: those are the tools whose name is a bare
    noun, so the derived title would not say what calling it does.
    """
    override = TITLE_OVERRIDES.get(name)
    if override:
        return override
    return " ".join(part.capitalize() for part in name.split("_") if part)


def progressive_tool(
    mcp: Any, docs_registry: MutableMapping[str, str]
) -> Callable[..., Callable[[Callable], Callable]]:
    """Drop-in replacement factory for ``@mcp.tool()``.

    ``tool = progressive_tool(mcp, registry)`` then ``@tool(read_only=True)``
    registers the function with only its first docstring paragraph as the wire
    description (plus a ``tool_docs`` pointer when the docstring has more), and
    stores the full docstring in ``docs_registry`` for the on-demand
    ``tool_docs`` tool. The decorated function is returned unchanged, matching
    ``FastMCP.tool``.

    ``read_only`` is REQUIRED, on purpose. Both the Anthropic and OpenAI
    directory submissions check that every tool carries behaviour hints, and a
    client that auto-approves read-only calls needs the flag to be right rather
    than defaulted. Making it a required keyword means a new tool cannot be
    added without someone deciding.

    Args:
        read_only: True when the tool only reads state.
        destructive: True when a write is not trivially reversible (a delete,
            an unsubscribe). Ignored — and forced False — for read-only tools.
        idempotent: True when repeating the call with the same arguments has
            no additional effect.
        open_world: True when the tool reaches systems outside Agnes, so its
            effects cannot be enumerated up front.
        title: Override the auto-derived human-readable title.
    """

    def tool(
        *,
        read_only: bool,
        destructive: bool = False,
        idempotent: bool | None = None,
        open_world: bool = False,
        title: str | None = None,
    ) -> Callable[[Callable], Callable]:
        def decorate(fn: Callable) -> Callable:
            full = inspect.cleandoc(fn.__doc__ or "").strip()
            summary, has_more = summarize_docstring(full)
            description = summary or fn.__name__
            if has_more:
                description += f" Full contract: tool_docs('{fn.__name__}')."
            docs_registry[fn.__name__] = full

            display_title = title or title_from_name(fn.__name__)
            annotations: dict[str, Any] = {
                "title": display_title,
                "readOnlyHint": read_only,
                # A reader never destroys anything, whatever the call site says.
                "destructiveHint": False if read_only else destructive,
                "openWorldHint": open_world,
            }
            if idempotent is not None:
                annotations["idempotentHint"] = idempotent

            return mcp.tool(description=description, annotations=annotations)(fn)

        return decorate

    return tool
