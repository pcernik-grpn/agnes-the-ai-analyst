"""Pure helpers for UsageProcessor — event extraction from Claude Code session jsonls.

Session JSONL shape (as documented in dev_docs/session_explore.md and verified
against live samples):

Each line is a top-level event dict with:
  {
    "type": "user" | "assistant" | "progress" | "system" |
            "tool_use_result" | "summary" | "file-history-snapshot" |
            "queue-operation" | ...,
    "uuid": "event-uuid",
    "parentUuid": "parent-event-uuid",
    "sessionId": "session-uuid",
    "timestamp": "2026-05-12T07:30:00.000Z",
    "cwd": "/path/to/cwd",
    "message": {
      "role": "user" | "assistant",
      "model": "claude-...",       # present on assistant turns
      "content": [                 # array or plain string on user turns
        {"type": "text", "text": "..."},
        {"type": "tool_use", "id": "tu_123", "name": "Bash", "input": {...}},
        {"type": "tool_result", "tool_use_id": "tu_123", "is_error": false, "content": [...]}
      ]
    }
  }

Tool results appear as:
- Inline content items of type "tool_result" inside a user-role message, OR
- As top-level events of type "tool_use_result" (older Claude Code versions)

is_error correlation: build a map of {tool_use_id: True} from tool_result
items on the first pass, then apply to matching tool_use events.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

from src.repositories import marketplace_plugins_repo, store_entities_repo

# v10: per-assistant-turn token rows. The processor now emits one
# `usage_turns` row per assistant turn that reports a `message.usage` block
# (`iter_turn_usage` below), so tokens — including the two cache counters —
# are attributable to a turn and a model instead of only to a whole session.
# Bumping the version is the signal operators reprocess on
# (`agnes admin usage reprocess` / POST /api/admin/usage/reprocess), which
# clears the `usage` state rows and backfills turns for sessions already on
# disk. The write is idempotent — unique on (session_file, turn_uuid) — so a
# reprocess cannot double-count.
# (v9: phase-6 plugin-level rollup parity for flea. `_aggregate_events`
# now produces synthetic (source='flea', type='plugin', parent_plugin='',
# name=<plugin_synth>) rows aggregating nested skill/agent invocations,
# mirroring the curated path. Without this, flea plugin entity cards +
# detail telemetry chips read 0 from `_load_invocation_stats` (which
# filters `parent_plugin = ''` for flea) even though nested children
# had correct rollup rows. Bump forces a re-aggregation pass so historic
# nested-invocation data fills the new plugin-level rows.)
# (v8: phase-5 attribution keyspace fix + phase-4 bundle rename. Lookup
# tables key by `store_entities.synthetic_name` instead of `name`;
# `_attribute_event` gained the flea-plugin-nested branch so nested
# skills inside flea plugins flow into rollup tables.)
# (v5: v46 marketplace-telemetry refactor swapped AttributionLookup for
# MarketplaceItemLookup. Identifier prefix (`<plugin>:<local>`) now drives
# attribution and usage_events.source / ref_id are populated per-event from
# the live marketplace_plugins + store_entities tables.)
# (v4: #293 user_id column; v3: #303 <command-name> slash extraction.)
USAGE_PROCESSOR_VERSION = 10

# Claude Code wraps user-typed slash invocations as
# <command-name>/<name></command-name> inside the user message content
# (raw "/foo" plain text never reaches the jsonl). Tag may sit anywhere
# in the text — typically after a <command-message> sibling — so we
# search rather than anchor at start. Name pattern matches both flat
# commands (`clear`, `exit`) and plugin-prefixed ones (`plugin:name`).
COMMAND_NAME_RE = re.compile(r"<command-name>/([A-Za-z][\w:-]*)</command-name>")

# Event types to skip entirely
_SKIP_TYPES = frozenset(
    {
        "system",
        "summary",
        "file-history-snapshot",
        "queue-operation",
        "progress",
    }
)


@dataclass(frozen=True)
class ParsedEvent:
    event_uuid: str | None
    parent_uuid: str | None
    tool_id: str | None  # tool_use 'id' (tu_xxx) from message.content item; None for slash_command
    event_type: str  # 'tool_use' | 'slash_command' | 'subagent' | 'mcp_call'
    tool_name: str | None
    skill_name: str | None
    subagent_type: str | None
    command_name: str | None
    is_error: bool
    model: str | None
    cwd: str | None
    occurred_at: datetime


def _parse_ts(ts_str: str | None) -> datetime | None:
    """Parse ISO 8601 timestamp to aware datetime. Returns None on failure."""
    if not ts_str:
        return None
    try:
        ts_str = ts_str.replace("Z", "+00:00")
        return datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        return None


def _collect_error_map(turns: list[dict]) -> dict[str, bool]:
    """First-pass: collect tool_use_id → is_error from all tool_result items.

    Tool results appear in two places:
    1. As content items inside user-role messages (type='tool_result')
    2. As top-level events of type='tool_use_result'
    """
    errors: dict[str, bool] = {}
    for turn in turns:
        turn_type = turn.get("type", "")

        # Top-level tool_use_result events (older Claude Code)
        if turn_type == "tool_use_result":
            tu_id = turn.get("tool_use_id") or turn.get("toolUseId")
            if tu_id and turn.get("is_error"):
                errors[tu_id] = True

        # Inline tool_result content blocks inside user messages
        msg = turn.get("message", {}) or {}
        content = msg.get("content", [])
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "tool_result":
                    tu_id = item.get("tool_use_id")
                    if tu_id and item.get("is_error"):
                        errors[tu_id] = True

    return errors


def iter_events(turns: list[dict]) -> Iterator[ParsedEvent]:
    """Walk parsed JSONL turns and yield ParsedEvent for each observable event.

    Recognises:
    - Assistant tool_use blocks → event_type='tool_use' (or 'subagent'/'mcp_call')
    - Skill tool → also extracts skill_name
    - Task/Agent tools → event_type='subagent'
    - mcp__* tools → event_type='mcp_call'
    - User messages containing a <command-name>/foo</command-name> tag
      (Claude Code's wire format for user-typed slash invocations)
      → event_type='slash_command', command_name='foo'

    Skips: system, summary, file-history-snapshot, queue-operation, progress.
    """
    error_map = _collect_error_map(turns)

    for turn in turns:
        turn_type = turn.get("type", "")
        if turn_type in _SKIP_TYPES:
            continue

        ts = _parse_ts(turn.get("timestamp")) or datetime.now(timezone.utc)
        cwd = turn.get("cwd")
        event_uuid = turn.get("uuid")
        parent_uuid = turn.get("parentUuid")

        msg = turn.get("message", {}) or {}
        content = msg.get("content", [])
        model = msg.get("model")

        if turn_type == "assistant":
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") != "tool_use":
                        continue

                    tool_id = item.get("id", "")
                    tool_name = item.get("name") or ""
                    inp = item.get("input") or {}
                    is_error = error_map.get(tool_id, False)

                    # Classify event type
                    skill_name: str | None = None
                    subagent_type: str | None = None
                    command_name: str | None = None

                    if tool_name == "Skill":
                        event_type = "tool_use"
                        # Real Skill input shape varies; check both keys
                        skill_name = inp.get("skill") or inp.get("name") or None
                    elif tool_name in ("Task", "Agent"):
                        event_type = "subagent"
                        subagent_type = inp.get("subagent_type") or tool_name
                    elif tool_name.startswith("mcp__"):
                        event_type = "mcp_call"
                    else:
                        event_type = "tool_use"

                    yield ParsedEvent(
                        event_uuid=event_uuid,
                        parent_uuid=parent_uuid,
                        tool_id=tool_id or None,
                        event_type=event_type,
                        tool_name=tool_name or None,
                        skill_name=skill_name,
                        subagent_type=subagent_type,
                        command_name=command_name,
                        is_error=is_error,
                        model=model,
                        cwd=cwd,
                        occurred_at=ts,
                    )

        elif turn_type == "user":
            # Slash-invocation detection: scan user message content for
            # <command-name>/foo</command-name> tags. content is normally a
            # plain string on slash-invocation turns; tolerate the
            # list-of-blocks shape too in case Claude Code's wire format
            # shifts to structured content later.
            if isinstance(content, str):
                text_parts = [content]
            elif isinstance(content, list):
                text_parts = [
                    item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text"
                ]
            else:
                text_parts = []

            for text in text_parts:
                if not text:
                    continue
                for name in COMMAND_NAME_RE.findall(text):
                    yield ParsedEvent(
                        event_uuid=event_uuid,
                        parent_uuid=parent_uuid,
                        tool_id=None,
                        event_type="slash_command",
                        tool_name=None,
                        skill_name=None,
                        subagent_type=None,
                        command_name=name,
                        is_error=False,
                        model=None,
                        cwd=cwd,
                        occurred_at=ts,
                    )


# Synthetic plugin name Agnes uses to bundle flea-market store entities into
# a single Claude Code marketplace surface. Skill/agent/command identifiers
# from flea entities arrive as `flea:<synthetic_name>` in the JSONL.
FLEA_BUNDLE_PREFIX = "flea"


class MarketplaceItemLookup:
    """Preloads marketplace_plugins + store_entities into memory for O(1)
    per-event attribution.

    Claude Code writes plugin-defined skill/agent/command identifiers in the
    JSONL as ``<plugin_name>:<local_name>`` (e.g. ``grpn:design``). The prefix
    is the *plugin* name (curated plugin name, or the synthetic
    ``flea`` for flea entities); the local part is the
    skill/agent/command name relative to that plugin. Identifiers without
    a ``:`` are either built-in tools (``Bash``, ``Read``, …) or flat slash
    commands (``/exit``) — neither participates in marketplace telemetry.

    ``resolve()`` returns ``(source, parent_plugin, name, type)``:
    - ``source``: ``'curated'`` | ``'flea'`` | ``'builtin'``
    - ``parent_plugin``: the prefix when it matched a plugin, ``''`` otherwise
      (also ``''`` for flea entities, which are standalone — no parent)
    - ``name``: the local-part when source is curated/flea, ``None`` otherwise
    - ``type``: ``'skill'`` | ``'agent'`` — derived from the event_type
      (slash commands count as skill per product rule).
    """

    def __init__(self):
        # Routed through the src.repositories factory (not a raw conn read)
        # so both lookups hit the active backend — a raw DuckDB read here
        # silently returned zero rows on Postgres instances.
        self._curated_plugins: set[str] = set(marketplace_plugins_repo().list_distinct_names())
        # v49 phase-5: lookup table keyed by `synthetic_name` (the
        # `<name>-by-<owner>` slug baked into the served plugin tree). Claude
        # Code writes the local part of a flea invocation as that synthetic
        # name (`flea:xlsx-by-c-marustamyan`), so matching against `name`
        # (un-suffixed) never landed. Type comes along so the rollup writer
        # knows whether to record the invocation as skill / agent / plugin.
        self._flea_entities: dict[str, str] = store_entities_repo().list_approved_synthetic_types()
        # Flea PLUGIN entities can be matched as a prefix too — `<plugin>:<inner>`
        # invocations of a skill / agent that lives inside a flea plugin bundle
        # land here, mirroring the curated nested attribution path. Standalone
        # flea entities still flow through the FLEA_BUNDLE_PREFIX branch.
        # Set carries synthetic_names because that's the plugin slug Claude
        # Code resolves at install time (v49 phase-4: `data["name"] = suffixed`
        # in `_bake_plugin_tree` for type='plugin' entities).
        self._flea_plugins: set[str] = {
            synthetic for synthetic, ent_type in self._flea_entities.items() if ent_type == "plugin"
        }

    def resolve(self, event: ParsedEvent) -> tuple[str, str, str | None, str | None]:
        """Return ``(source, parent_plugin, name, type)``.

        Priority order — first identifier with a ``:`` prefix that matches a
        known plugin wins. Identifiers without ``:`` (built-in tools, flat
        slash commands) drop through to the builtin fallback.
        """
        # (identifier, default_type_when_matched)
        candidates = (
            (event.skill_name, "skill"),
            (event.subagent_type, "agent"),
            (event.command_name, "skill"),  # slash commands counted as skill
        )
        for ident, default_type in candidates:
            if not ident or ":" not in ident:
                continue
            prefix, local = ident.split(":", 1)
            if prefix == FLEA_BUNDLE_PREFIX:
                ent_type = self._flea_entities.get(local)
                if ent_type:
                    # For a flea entity bundle, the local-part *is* the
                    # entity name and its type comes from the registry.
                    # Standalone flea items have no parent plugin.
                    return ("flea", "", local, ent_type)
                # Bundle prefix but no matching entity — likely archived
                # since the event was written. Fall through to builtin.
                continue
            if prefix in self._curated_plugins:
                return ("curated", prefix, local, default_type)
            if prefix in self._flea_plugins:
                # Skill / agent nested inside a flea plugin bundle. Same
                # shape as curated: source='flea', parent_plugin=<plugin
                # name>, name=<inner local-part>.
                return ("flea", prefix, local, default_type)
            # Unknown plugin prefix — fall through to builtin (matches the
            # rebuild_rollups filter that excludes unattributed events).
        return ("builtin", "", None, None)


def compute_active_seconds(timestamps: list[datetime]) -> int:
    """Sum of intra-block durations. Gap >10 minutes = new block."""
    if not timestamps:
        return 0
    timestamps = sorted(timestamps)
    GAP = 600  # 10 minutes
    blocks = []
    block_start = timestamps[0]
    prev = timestamps[0]
    for ts in timestamps[1:]:
        gap = (ts - prev).total_seconds()
        if gap > GAP:
            blocks.append((block_start, prev))
            block_start = ts
        prev = ts
    blocks.append((block_start, prev))
    return int(sum((end - start).total_seconds() for start, end in blocks))


#: ``message.usage`` key -> the column every usage surface stores it under.
#: The jsonl names both cache counters ``*_input_tokens``; the storage schema
#: (``usage_session_summary``, ``usage_turns``) drops the "input" because a
#: cache write is not an input read.
_USAGE_TOKEN_COLUMNS = (
    ("input_tokens", "input_tokens"),
    ("output_tokens", "output_tokens"),
    ("cache_read_input_tokens", "cache_read_tokens"),
    ("cache_creation_input_tokens", "cache_creation_tokens"),
)


def _token_counts(msg: Any) -> dict[str, int] | None:
    """The four token counters from one assistant message's ``usage`` block.

    ``None`` when the message reports no usage at all: an unmeasured turn
    must not become a row of zeros claiming otherwise. Sessions older than
    prompt caching simply lack the ``cache_*`` keys, and non-int values
    (corrupt jsonl) count as zero — one bad turn can't poison a session.

    Single source of truth for reading the usage block, shared by
    :func:`compute_summary` (session grain) and :func:`iter_turn_usage`
    (turn grain) so the two readings of the same session cannot drift.
    """
    if not isinstance(msg, dict):
        return None
    usage = msg.get("usage")
    if not isinstance(usage, dict) or not usage:
        return None
    counts: dict[str, int] = {}
    for key, column in _USAGE_TOKEN_COLUMNS:
        value = usage.get(key, 0)
        counts[column] = value if isinstance(value, int) else 0
    return counts


def iter_turn_usage(turns: list[dict]) -> Iterator[dict]:
    """Yield one token record per assistant turn that reports usage.

    Keys are ``usage_turns`` column names, so a caller only adds the session
    identity (``session_file`` / ``session_id`` / ``user_id`` / ``surface``)
    before handing the dict to ``usage_turns_repo().insert_batch``.

    Skipped:

    * turns with no ``usage`` block — nothing was measured;
    * turns with no ``uuid`` — ``turn_uuid`` is half the idempotency key
      ``(session_file, turn_uuid)``, so a row without one could not be
      de-duplicated when the session is re-processed.

    ``occurred_at`` is the event's own timestamp and stays ``None`` when it
    cannot be parsed. Contrast :func:`iter_events`, which falls back to
    "now": there the value only orders events, while a turn carries tokens
    into time windows, and stamping an unparseable timestamp with "now"
    would drag a backfilled session's tokens into today's totals.
    """
    for turn in turns:
        if turn.get("type") != "assistant":
            continue
        event_uuid = turn.get("uuid")
        if not event_uuid:
            continue
        msg = turn.get("message") or {}
        counts = _token_counts(msg)
        if counts is None:
            continue
        yield {
            "turn_uuid": event_uuid,
            "parent_uuid": turn.get("parentUuid"),
            "model": msg.get("model"),
            **counts,
            "occurred_at": _parse_ts(turn.get("timestamp")),
        }


def compute_summary(turns: list[dict], events: list[dict]) -> dict:
    """Build the usage_session_summary row dict from parsed turns and event rows.

    Caller must fill in 'session_file' and 'username' after calling this.
    events is a list of dicts (as produced by UsageProcessor, not ParsedEvent).
    """
    # session_id: first turn with a sessionId field
    session_id = None
    for t in turns:
        sid = t.get("sessionId")
        if sid:
            session_id = sid
            break

    # Timestamps from all turns that have one
    timestamps: list[datetime] = []
    user_messages = 0
    assistant_messages = 0
    model_counter: Counter = Counter()
    input_tokens = 0
    output_tokens = 0
    cache_read_tokens = 0
    cache_creation_tokens = 0

    for t in turns:
        ts = _parse_ts(t.get("timestamp"))
        if ts:
            timestamps.append(ts)
        turn_type = t.get("type", "")
        if turn_type == "user":
            user_messages += 1
        elif turn_type == "assistant":
            assistant_messages += 1
            msg = t.get("message", {}) or {}
            m = msg.get("model")
            if m:
                model_counter[m] += 1
            # Anthropic API usage block on assistant turns, read through the
            # same helper `iter_turn_usage` uses so the session totals and the
            # per-turn rows can never disagree about the same session.
            counts = _token_counts(msg)
            if counts is not None:
                input_tokens += counts["input_tokens"]
                output_tokens += counts["output_tokens"]
                cache_read_tokens += counts["cache_read_tokens"]
                cache_creation_tokens += counts["cache_creation_tokens"]

    started_at = min(timestamps) if timestamps else None
    ended_at = max(timestamps) if timestamps else None
    wall_seconds = int((ended_at - started_at).total_seconds()) if started_at and ended_at else 0
    active_seconds = compute_active_seconds(timestamps)

    # Aggregate counts from events
    tool_calls = sum(1 for e in events if e["event_type"] == "tool_use")
    tool_errors = sum(1 for e in events if e.get("is_error"))
    skill_invocations = sum(1 for e in events if e.get("skill_name"))
    subagent_dispatches = sum(1 for e in events if e["event_type"] == "subagent")
    mcp_calls = sum(1 for e in events if e["event_type"] == "mcp_call")
    slash_commands = sum(1 for e in events if e["event_type"] == "slash_command")
    distinct_tools = len({e["tool_name"] for e in events if e.get("tool_name")})
    distinct_skills = len({e["skill_name"] for e in events if e.get("skill_name")})
    primary_model = model_counter.most_common(1)[0][0] if model_counter else None

    return {
        "session_id": session_id or "",
        "started_at": started_at,
        "ended_at": ended_at,
        "active_seconds": active_seconds,
        "wall_seconds": wall_seconds,
        "user_messages": user_messages,
        "assistant_messages": assistant_messages,
        "tool_calls": tool_calls,
        "tool_errors": tool_errors,
        "skill_invocations": skill_invocations,
        "subagent_dispatches": subagent_dispatches,
        "mcp_calls": mcp_calls,
        "slash_commands": slash_commands,
        "distinct_tools": distinct_tools,
        "distinct_skills": distinct_skills,
        "primary_model": primary_model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "processor_version": USAGE_PROCESSOR_VERSION,
    }


# Refresh interval for the 30-day window snapshot. The daily fact + 7d window
# refresh on every UsageProcessor tick (~10 min) because they're cheap and
# need to reflect recent activity. The 30d window is fuller, costs more to
# rebuild, and barely shifts between ticks — refresh hourly instead. Tracked
# in `session_processor_state` (processor_name='marketplace_rollup_30d').
WINDOW_30D_REFRESH_SECONDS = 3600
_MARKETPLACE_30D_TRACKER = "marketplace_rollup_30d"


def _identifier_split(skill_name, subagent_type, command_name, event_type):
    """Replicate MarketplaceItemLookup.resolve()'s prefix-split logic.

    Returns ``(prefix, local, default_type)`` or ``(None, None, None)`` if
    no identifier carries a plugin prefix. Used by the SQL rollup builder
    so the attribution logic stays in one place even though the loop runs
    in Python.
    """
    candidates = (
        (skill_name, "skill"),
        (subagent_type, "agent"),
        (command_name, "skill"),  # slash commands counted as skill (product rule)
    )
    for ident, default_type in candidates:
        if not ident or ":" not in ident:
            continue
        prefix, local = ident.split(":", 1)
        return prefix, local, default_type
    return None, None, None


def _attribute_event(
    curated_plugins: set[str],
    flea_entities: dict[str, str],
    flea_plugins: set[str],
    skill_name,
    subagent_type,
    command_name,
    event_type,
):
    """Resolve one event to (source, type, parent_plugin, name).

    Returns None when the event doesn't belong in marketplace rollups
    (built-in tool, flat slash command, unknown plugin prefix).

    Lookup tables (curated_plugins, flea_entities, flea_plugins) are passed
    in so the caller can preload once and reuse across thousands of events.
    Mirrors the four branches `MarketplaceItemLookup.resolve()` walks:

      1. ``flea:<synthetic>``        — standalone flea skill/agent/plugin
      2. ``<curated_plugin>:<inner>`` — nested skill/agent of a curated plugin
      3. ``<flea_plugin>:<inner>``    — nested skill/agent of a flea plugin
      4. anything else                — None (filtered out of rollups)
    """
    prefix, local, default_type = _identifier_split(skill_name, subagent_type, command_name, event_type)
    if prefix is None:
        return None
    if prefix == FLEA_BUNDLE_PREFIX:
        ent_type = flea_entities.get(local)
        if ent_type is None:
            return None
        return ("flea", ent_type, "", local)
    if prefix in curated_plugins:
        return ("curated", default_type, prefix, local)
    if prefix in flea_plugins:
        # v49 phase-5: nested skill/agent inside a flea plugin bundle.
        # Same shape as curated nested attribution (source='flea',
        # parent_plugin=<synthetic plugin name>, name=<inner frontmatter
        # name>). Without this branch the rollup builder silently dropped
        # inner-item invocations even though MarketplaceItemLookup.resolve()
        # — used by the live writer — handled them since v6.
        return ("flea", default_type, prefix, local)
    return None


def _aggregate_events(events_rows, curated_plugins, flea_entities, flea_plugins, *, group_by_day: bool):
    """Walk raw event rows and produce aggregated buckets.

    ``events_rows`` shape: (day, user_id, is_error, skill_name, subagent_type,
    command_name, event_type). When ``group_by_day=True`` returns rows keyed
    by (day, source, type, parent_plugin, name) — for daily fact. Else
    aggregates across the whole window (source, type, parent_plugin, name).

    Plugin-level aggregation (type='plugin' rows) is added by walking the
    child results once and grouping by parent.
    """
    # Bucket: key -> dict(count, users:set, errors)
    leaf: dict[tuple, dict] = {}
    for row in events_rows:
        day, uid, is_err, sk, sa, cm, etype = row
        attributed = _attribute_event(curated_plugins, flea_entities, flea_plugins, sk, sa, cm, etype)
        if attributed is None:
            continue
        source, type_, parent, name = attributed
        if group_by_day:
            key = (day, source, type_, parent, name)
        else:
            key = (source, type_, parent, name)
        b = leaf.setdefault(key, {"count": 0, "users": set(), "errors": 0})
        b["count"] += 1
        if uid:
            b["users"].add(uid)
        if is_err:
            b["errors"] += 1

    # Plugin-level rollup: curated AND flea invocations get a parent row,
    # summing the children. distinct_users at plugin level recomputed across
    # child users so a user counted in two skills of the same plugin doesn't
    # double-count. v49 phase-6: extended to flea (was curated-only); without
    # this, flea plugin entities never got an aggregated row, so the
    # parent_plugin='' filter in `_load_invocation_stats` returned no rows
    # for plugin cards / detail telemetry chips even though nested children
    # were attributed correctly.
    plugin_bucket: dict[tuple, dict] = {}
    for key, vals in leaf.items():
        if group_by_day:
            day, source, type_, parent, name = key
        else:
            day = None
            source, type_, parent, name = key
        if source not in ("curated", "flea") or not parent:
            continue
        if group_by_day:
            pkey = (day, source, "plugin", "", parent)
        else:
            pkey = (source, "plugin", "", parent)
        pb = plugin_bucket.setdefault(pkey, {"count": 0, "users": set(), "errors": 0})
        pb["count"] += vals["count"]
        pb["users"] |= vals["users"]
        pb["errors"] += vals["errors"]
    leaf.update(plugin_bucket)
    return leaf
