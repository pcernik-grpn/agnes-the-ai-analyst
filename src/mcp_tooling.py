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
- ``compact_search_results`` — the SEARCH tools' counterpart: a response that
  would not fit the budget is shortened (prose fields become marked prefixes,
  then lowest-ranked hits are dropped) instead of refused, because a search
  hit is an excerpt already and a shorter excerpt still answers "what matched
  and where"; the model is told exactly what was cut and how to read the rest.
"""

from __future__ import annotations

import inspect
import json
import os
from collections.abc import Callable, MutableMapping
from typing import Any

import pydantic_core

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


# ── search-result compaction ───────────────────────────────────────────────────

#: Budget for a SEARCH tool's serialized response. Deliberately separate from
#: ``DEFAULT_MAX_OUTPUT_CHARS``: that cap REFUSES an oversized ``query`` result
#: (rows are data an agent computes over, so a silently-incomplete set is worse
#: than none), while a search hit is already an excerpt — a shorter excerpt is
#: still a correct answer to "what matched, and where". 20k chars is the ceiling
#: ``collection_file_read`` already applies to one file's text, and sits well
#: inside the tool-result limit of the agent SDKs that consume these servers.
#: The incident behind it: ten 3.2k-char document chunks came back as a 52k-char
#: ``knowledge_search`` result that the chat engine refused outright and wrote
#: to a file the model could not read — a search that found the answer, and a
#: turn that could not use it.
DEFAULT_SEARCH_MAX_CHARS = 20_000
SEARCH_MAX_CHARS_ENV = "AGNES_MCP_SEARCH_MAX_CHARS"

#: Per-hit fields that hold prose and may be shortened. Everything else on a
#: hit — ids, names, scores, the pivot hint — is an identifier or a number a
#: follow-up call depends on, and is never touched.
SEARCH_TEXT_FIELDS: tuple[str, ...] = ("text", "snippet", "description", "definition", "content")

#: Top-level prose the search endpoints echo back — the caller's own query
#: (unbounded: a pasted paragraph is a legal query), and the empty-result
#: ``hint`` / offline ``note``. Capped alongside the hit fields, or a long
#: query alone could keep the response over budget after every hit was
#: dropped (Devin Review on #2046).
SEARCH_ENVELOPE_FIELDS: tuple[str, ...] = ("query", "hint", "note")

#: A prose field is never cut below this. Once every field is at the floor and
#: the response still does not fit, whole hits are dropped from the tail
#: instead — ten unreadable stubs are worth less than five readable prefixes.
SEARCH_TEXT_FLOOR = 200

#: Appended to every shortened field, so the cut is visible inline as well as
#: in the hit's ``truncated_fields``.
SEARCH_TRUNCATED_MARK = "…"


def search_max_chars() -> int:
    """Resolve the search-result budget: env override, else default. ``0`` disables."""
    raw = os.environ.get(SEARCH_MAX_CHARS_ENV, "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return DEFAULT_SEARCH_MAX_CHARS


def wire_size(payload: Any) -> int:
    """Characters FastMCP puts on the wire for a dict tool result.

    FastMCP serializes a non-string return value with
    ``pydantic_core.to_json(result, fallback=str, indent=2)`` — pretty-printed,
    unicode kept as-is — and that text is what an MCP client measures against
    its tool-result limit. This calls the same serializer with the same
    arguments rather than approximating it with ``json.dumps`` (the two differ
    on floats, datetimes and key ordering, and an approximation that ran
    short would let an oversized result through — Copilot review on #2046).
    ``ensure_output_size`` measures the compact ``json.dumps`` form; its cap
    is an order of magnitude looser, so the difference never mattered there.
    """
    return len(pydantic_core.to_json(payload, fallback=str, indent=2).decode("utf-8"))


def _shorten(value: str, cap: int) -> str:
    return value[:cap].rstrip() + SEARCH_TRUNCATED_MARK


def _apply_cap(hits: list[Any], cap: int) -> list[Any]:
    """Every prose field longer than ``cap`` becomes a marked prefix.

    Always derived from the ORIGINAL hits (never from a previous pass), so a
    shorter cap re-cuts the full text and the mark is appended exactly once.
    """
    out: list[Any] = []
    for hit in hits:
        if not isinstance(hit, dict):
            out.append(hit)
            continue
        fields = [f for f in SEARCH_TEXT_FIELDS if isinstance(hit.get(f), str) and len(hit[f]) > cap]
        if not fields:
            out.append(hit)
            continue
        shortened = {**hit, **{f: _shorten(hit[f], cap) for f in fields}}
        shortened["truncated"] = True
        shortened["truncated_fields"] = fields
        out.append(shortened)
    return out


def _cap_envelope(payload: dict, cap: int) -> dict:
    """The payload with its own prose fields (:data:`SEARCH_ENVELOPE_FIELDS`)
    cut to ``cap`` and listed in a top-level ``truncated_fields``."""
    fields = [f for f in SEARCH_ENVELOPE_FIELDS if isinstance(payload.get(f), str) and len(payload[f]) > cap]
    if not fields:
        return payload
    return {**payload, **{f: _shorten(payload[f], cap) for f in fields}, "truncated_fields": fields}


def _with_results(payload: dict, tool_name: str, hits: list[Any], *, total: int, budget: int) -> dict:
    shortened = sum(1 for h in hits if isinstance(h, dict) and h.get("truncated_fields"))
    dropped = total - len(hits)
    what: list[str] = []
    if shortened:
        what.append(
            f"{shortened} of {total} results carry shortened text (a PREFIX — see each hit's `truncated_fields`)"
        )
    if dropped:
        what.append(f"{dropped} lower-ranked result(s) of {total} were dropped")
    if payload.get("truncated_fields"):
        what.append(f"the response's own {', '.join(payload['truncated_fields'])} field(s) were shortened")
    note = (
        f"{tool_name}: {'; '.join(what)} to fit the {budget:,}-character tool output budget. "
        "To read the document a shortened hit came from, call "
        "collection_file_read(collection_id=<hit.corpus_id>, file_id=<hit.file_id>) "
        "and follow its next_offset past the first page; "
        "otherwise narrow the query or lower `k`."
    )
    return {**payload, "results": hits, "truncated": True, "truncated_note": note}


def compact_search_results(payload: Any, tool_name: str, *, budget: int | None = None) -> Any:
    """Fit a search response into the tool-output budget — shorten first, drop last.

    The counterpart of :func:`ensure_output_size` for the search tools, and
    the opposite contract on purpose: that guard RAISES so an agent never
    computes over silently-incomplete query rows; this one never raises,
    because a search hit is an excerpt already — a shorter excerpt is still a
    true answer to "what matched, and where", whereas an error that names a
    file the model cannot open (what an MCP client does with a result over
    its limit) is a search that found the answer and a turn that lost it.

    A response that already fits is returned untouched — the same object.
    Otherwise every prose field is cut to the LARGEST common cap at which the
    serialized response (:func:`wire_size` — what the client actually
    measures) fits — so the budget is spent on text, not left idle by a
    coarse cut. Size is monotonic in the cap only BETWEEN the distinct field
    lengths: the moment the cap reaches a field's length that field comes
    back whole and sheds its mark and its ``truncated_fields`` entry, so the
    size can step DOWN as the cap goes up (Copilot review on #2046). The
    search therefore walks those intervals from the longest down, and
    binary-searches only inside the first one whose start fits, where the
    monotonicity holds exactly. If even the floor does not fit, hits are
    dropped from the tail, i.e. lowest-ranked first (results arrive
    ranked). Nothing is cut silently: every shortened hit
    carries ``truncated: true`` and ``truncated_fields``, its text ends in
    ``…``, and the payload carries ``truncated: true`` plus a
    ``truncated_note`` saying what was cut and how to read the rest.

    Compacted: ``payload["results"]`` (a list) and the payload's own prose
    fields (:data:`SEARCH_ENVELOPE_FIELDS` — the echoed query, the
    empty-result ``hint``, the offline ``note``), which take part in the same
    cap search so a paragraph-long query cannot keep the response over
    budget on its own; anything else (``retrieval``, counts) is small by
    construction and passes through. When even an empty result list with
    every prose field at the floor does not fit, the budget is smaller than
    the response envelope itself — a misconfiguration — and the return is a
    minimal note saying so (naming the knob) rather than an oversized
    payload the client would refuse. A ``budget`` of ``0`` disables the
    compaction.
    """
    effective = search_max_chars() if budget is None else budget
    if effective <= 0 or not isinstance(payload, dict):
        return payload
    results = payload.get("results")
    if not isinstance(results, list) or wire_size(payload) <= effective:
        return payload

    hits = list(results)
    total = len(hits)

    def _candidate(kept: list[Any], cap: int) -> dict:
        return _with_results(
            _cap_envelope(payload, cap), tool_name, _apply_cap(kept, cap), total=total, budget=effective
        )

    def _fits(candidate: dict) -> bool:
        return wire_size(candidate) <= effective

    # Largest cap that fits. Within an interval between two consecutive
    # distinct field lengths the set of shortened fields is fixed, so the
    # size is monotone non-decreasing in the cap (a longer prefix, rstrip
    # included, is never shorter) and a binary search is exact. At a
    # boundary a field comes back whole and the size can drop, so the
    # intervals are tried from the longest down: the first whose START fits
    # contains the global maximum. The top interval — cap at or above the
    # longest field — is the untouched payload, already known not to fit.
    lengths = sorted(
        {len(h[f]) for h in hits if isinstance(h, dict) for f in SEARCH_TEXT_FIELDS if isinstance(h.get(f), str)}
        | {len(payload[f]) for f in SEARCH_ENVELOPE_FIELDS if isinstance(payload.get(f), str)}
    )
    bounds = [SEARCH_TEXT_FLOOR] + [n for n in lengths if n > SEARCH_TEXT_FLOOR]
    for j in range(len(bounds) - 2, -1, -1):
        lo, hi = bounds[j], bounds[j + 1] - 1
        if not _fits(_candidate(hits, lo)):
            continue
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if _fits(_candidate(hits, mid)):
                lo = mid
            else:
                hi = mid - 1
        return _candidate(hits, lo)

    # Even the floor does not fit: drop lowest-ranked hits until it does.
    kept = hits
    candidate = _candidate(kept, SEARCH_TEXT_FLOOR)
    while kept:
        kept = kept[:-1]
        candidate = _candidate(kept, SEARCH_TEXT_FLOOR)
        if _fits(candidate):
            return candidate
    # Even an empty result list with every prose field at the floor does not
    # fit: the budget is smaller than the response envelope itself. Returning
    # the oversized shape would recreate the client's refusal this helper
    # exists to prevent, so answer with the smallest honest thing — what
    # happened and which knob fixes it (Devin Review on #2046).
    return {
        "results": [],
        "truncated": True,
        "truncated_note": (
            f"{tool_name}: {total} result(s) withheld — the {effective:,}-character tool output budget "
            f"({SEARCH_MAX_CHARS_ENV}) is smaller than the response envelope itself; raise it."
        ),
    }


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
    "stack_artefacts_candidates": "List Artifact Candidates",
    "admin_config_surface": "Get Config Surface",
    "admin_register_table": "Register Source Table",
    "store_compose_plugin": "Publish A Plugin Bundling Existing Items",
    "store_edit_markdown": "Update A Published Skill Or Agent From Markdown",
    "admin_semantic_layer_coverage": "Get Semantic Layer Coverage",
    "admin_knowledge_packaging_status": "Get Knowledge Packaging Status",
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
    "fact_edges": "List Fact Edges Of One Type",
    "fact_claims": "Get Fact Claims",
    "activity": "Get Activity Timeline",
    "effective_access": "Check Table Access Policy",
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
