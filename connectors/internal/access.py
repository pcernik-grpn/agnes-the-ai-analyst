"""Per-request scoped query execution for the ``internal`` data source.

Architecture: open ``system.duckdb`` read-only, build a temporary view per
referenced internal table with RBAC applied as a WHERE clause, execute the
user's SQL inside that scope, return rows.

RBAC model:
- Admin (per ``is_user_admin``) sees the table unscoped — no WHERE clause.
- Everyone else gets a single-row-filter projection. The filter column is
  hard-coded per table (``INTERNAL_TABLES``) and the filter value comes
  from the auth-resolved user object — never from user-supplied SQL.

SQL-injection considerations:
- The temp-view DDL interpolates a literal string for the filter value.
- ``username`` for ``usage_*`` is the local-part of an email; we enforce
  the same regex used by the session-file path (alnums + ``._-``) before
  interpolation.
- ``user_id`` for ``audit_log`` is a UUID; we validate the regex before
  interpolation.
- The user's SELECT itself is gated by the same SELECT-only validator the
  ``/api/query`` endpoint uses (denylist of write/DDL keywords + file
  functions). That validator runs in the API layer.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from src.remote_engines import strip_one_trailing_semicolon
from src.sql_ident import quote_ident

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal-table registry — single source of truth
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InternalTable:
    """One internal table mapping.

    Fields:
        registry_id:    the value used in ``table_registry.id`` and what
                        analysts type in SQL (``SELECT * FROM <registry_id>``)
        source_table:   the underlying physical table in ``system.duckdb``
        filter_column:  the column on ``source_table`` carrying the per-row
                        owner. Used for the non-admin scoping clause. ``None``
                        when ``filter_kind='admin_only'`` — there is no
                        per-row owner column on that table at all.
        filter_kind:    how to resolve the filter value from the auth user
                        dict — ``'username'`` (email local-part), ``'user_id'``
                        (UUID), or ``'admin_only'``: the table carries no
                        per-row owner (it is operational/operator data, e.g.
                        an extraction run keyed on a connection or an ingest
                        batch keyed on a set of collections, never on a
                        person), so every non-admin caller gets zero rows —
                        see :func:`build_filter_clause`. Do not invent a
                        per-user filter a table's own columns cannot honestly
                        support; admin-only is the deliberate alternative.
        display_name:   human-readable name (also goes into ``table_registry.name``)
        description:    short blurb (catalog UI + ``agnes catalog`` output)
    """

    registry_id: str
    source_table: str
    filter_column: str | None
    filter_kind: str  # 'username' | 'user_id' | 'admin_only'
    display_name: str
    description: str
    legacy_username_column: str | None = None  # backward-compat OR fallback
    # Per-column descriptions served by /api/v2/schema (and therefore
    # `agnes schema`). This text is the ONLY column documentation an LLM
    # gets before writing SQL against the table — keep it factual and
    # disambiguating, not decorative.
    column_descriptions: dict[str, str] = field(default_factory=dict)


INTERNAL_TABLES: tuple[InternalTable, ...] = (
    InternalTable(
        registry_id="agnes_sessions",
        source_table="usage_session_summary",
        filter_column="user_id",
        filter_kind="user_id",
        display_name="Agnes sessions",
        description=(
            "One row per Claude Code or chat session with activity counters and "
            "summed token usage. You see your own sessions only (admins see all). "
            "Server-side only — query with `agnes query`; there is no local copy."
        ),
        legacy_username_column="username",
        column_descriptions={
            "session_file": "Storage key of the session transcript (`<dir>/<file>.jsonl`; chat sessions use `chat-<id>.jsonl`). Joins to agnes_turns.session_file by basename.",
            "session_id": "The session's UUID as assigned by Claude Code / the chat runtime.",
            "username": "Display identity — the user's full email (legacy rows may carry other key forms). For joins and filters prefer user_id.",
            "user_id": "Canonical owner key (users.id). The row-level filter and all dashboards key on this.",
            "started_at": "Timestamp of the first event in the session.",
            "ended_at": "Timestamp of the last event in the session.",
            "active_seconds": "Seconds with actual activity (gaps between events capped), i.e. hands-on time.",
            "wall_seconds": "ended_at minus started_at — elapsed wall-clock time including idle gaps.",
            "user_messages": "Count of user prompts in the session.",
            "assistant_messages": "Count of assistant turns.",
            "tool_calls": "Total tool invocations.",
            "tool_errors": "Tool invocations that returned an error.",
            "skill_invocations": "Skill (slash-command package) invocations.",
            "subagent_dispatches": "Sub-agent (Task tool) dispatches.",
            "mcp_calls": "MCP tool calls.",
            "slash_commands": "Slash-command invocations.",
            "distinct_tools": "Number of distinct tools used.",
            "distinct_skills": "Number of distinct skills used.",
            "primary_model": "Model that served most of the session's turns.",
            "input_tokens": "Summed uncached input tokens across assistant turns (excludes cache reads/writes).",
            "output_tokens": "Summed output tokens across assistant turns.",
            "cache_read_tokens": "Summed prompt-cache READ tokens (input served from cache; billed far below input_tokens).",
            "cache_creation_tokens": "Summed prompt-cache WRITE tokens (input written into cache; billed above input_tokens).",
            "processor_version": "Version of the usage processor that produced this row (bumps trigger reprocessing).",
            "extracted_at": "When the usage processor last (re)computed this row.",
            "uploaded_at": "When the session file arrived on the server (upload or collector).",
        },
    ),
    InternalTable(
        registry_id="agnes_telemetry",
        source_table="usage_events",
        filter_column="user_id",
        filter_kind="user_id",
        display_name="Agnes telemetry events",
        description=(
            "One row per tool/skill/sub-agent/MCP/slash-command event from your own "
            "sessions (admins see all users). No token columns — token usage lives "
            "in agnes_sessions (per session) and agnes_turns (per turn). Server-side "
            "only; query with `agnes query`."
        ),
        legacy_username_column="username",
        column_descriptions={
            "session_file": "Storage key of the originating session; join to agnes_sessions.session_file.",
            "session_id": "UUID of the originating session.",
            "username": "Display identity (full email). Prefer user_id for joins and filters.",
            "user_id": "Canonical owner key (users.id); the row-level filter keys on this.",
            "event_uuid": "UUID of the transcript event this row was extracted from.",
            "parent_uuid": "UUID of the parent transcript event (threading).",
            "event_type": "Kind of event: tool_use, slash_command, subagent, mcp_call, chat.message, …",
            "tool_name": "Tool invoked, when event_type is a tool use.",
            "skill_name": "Skill invoked, when applicable.",
            "subagent_type": "Sub-agent type, when the event is a Task dispatch.",
            "command_name": "Slash command, when applicable.",
            "is_error": "True when the invocation returned an error.",
            "source": "Where the invoked item came from: curated | flea | builtin | server.",
            "ref_id": "Marketplace/item reference for the invoked tool or skill, when known.",
            "model": "Model active at the time of the event, when known.",
            "cwd": "Working directory of the session at event time.",
            "occurred_at": "Event timestamp.",
            "friction_tags": "JSON list of detected friction signals for the event (e.g. retries), when any.",
            "processor_version": "Version of the usage processor that produced this row.",
            "extracted_at": "When the usage processor last (re)computed this row.",
        },
    ),
    InternalTable(
        registry_id="agnes_audit",
        source_table="audit_log",
        filter_column="user_id",
        filter_kind="user_id",
        display_name="Agnes audit log",
        description=(
            "Server-side audit trail of your own actions against this Agnes "
            "instance — API calls, queries, syncs, admin operations (admins see "
            "all users). Server-side only; query with `agnes query`."
        ),
        column_descriptions={
            "timestamp": "When the action happened.",
            "user_id": "Who performed it (users.id); the row-level filter keys on this.",
            "action": "Dotted action name from the audit catalog (e.g. query.local, session.upload).",
            "resource": "Entity the action touched (e.g. table:<id>, data_package:<id>), when any.",
            "params": "JSON parameters recorded for the action (sanitized).",
            "result": "Outcome: success, denied, error, …",
            "duration_ms": "Server-side duration of the action in milliseconds, when measured.",
            "params_before": "JSON snapshot of the previous state for mutating actions, when recorded.",
            "client_ip": "Caller IP as derived from trusted proxy hops.",
            "client_kind": "Kind of caller credential: web session, PAT, scheduler, …",
            "correlation_id": "Request correlation id for joining related audit rows.",
        },
    ),
    # Postgres-only: `usage_turns` landed after the A3 freeze (Alembic
    # revision 0094, no `src/db.py` ladder step), so it exists on the
    # Postgres app-state backend alone. The tuple is static — the derived
    # constants below are built at import time — and the BACKEND decides
    # whether the id is registered, in
    # `connectors.internal.registry.ensure_internal_tables_registered`. No
    # `legacy_username_column`: `usage_turns` has no `username` column, so
    # the OR fallback the older tables carry would be a Binder error here.
    InternalTable(
        registry_id="agnes_turns",
        source_table="usage_turns",
        filter_column="user_id",
        filter_kind="user_id",
        display_name="Agnes turns",
        description=(
            "One row per assistant turn with its exact token usage incl. prompt "
            "cache, across Claude Code and every chat surface — your own rows only "
            "(admins see all). The finest token granularity available; sums roll up "
            "to agnes_sessions. Postgres-backed instances only. Server-side only; "
            "query with `agnes query`."
        ),
        column_descriptions={
            "session_file": "Basename key of the owning session (`<file>.jsonl`; chat turns use `chat-<id>.jsonl`). Join to agnes_sessions.session_file by basename.",
            "session_id": "UUID of the owning session.",
            "user_id": "Canonical owner key (users.id); the row-level filter keys on this.",
            "surface": "Where the turn happened: claude_code, web, slack_dm, slack_thread, telegram, …",
            "turn_uuid": "Unique id of the turn within its session file (idempotency key with session_file).",
            "parent_uuid": "UUID of the parent transcript event, when known.",
            "model": "Model that served this turn.",
            "input_tokens": "Uncached input tokens for this turn.",
            "output_tokens": "Output tokens for this turn.",
            "cache_read_tokens": "Prompt-cache READ tokens (input served from cache; billed far below input_tokens).",
            "cache_creation_tokens": "Prompt-cache WRITE tokens (input written into cache; billed above input_tokens).",
            "occurred_at": "Turn timestamp.",
            "processor_version": "Usage-processor version for processor-written rows (0 for live chat writes).",
            "extracted_at": "When the row was written.",
        },
    ),
    # Postgres-only, same reasoning as `agnes_turns` above: `extraction_runs`
    # landed after the A3 freeze (Alembic revision 0094, no `src/db.py`
    # ladder step), so it exists on Postgres alone.
    #
    # `filter_kind='admin_only'`: this table has NO per-row owner column. A
    # run is keyed on a `connection_id` (a data-source connection, an
    # admin-managed entity with no per-user access model of its own — every
    # route that reads or writes it, e.g. `app/api/admin_extraction.py`, is
    # already admin-gated) — never on the person who happened to trigger it.
    # Treating it as own-rows-per-caller would be inventing a scoping
    # guarantee the columns cannot back up; treating it as admin/operator
    # data (every non-admin caller sees zero rows, regardless of package
    # grant) is the honest model and matches how every other surface over
    # this table already gates it.
    InternalTable(
        registry_id="agnes_extraction_runs",
        source_table="extraction_runs",
        filter_column=None,
        filter_kind="admin_only",
        display_name="Agnes extraction runs",
        description=(
            "One row per built-in extraction (crawl) run: status, phase, "
            "progress counters, the final run report and LLM token usage "
            "(`usage`) that run spent. Admin/operator data, not a per-user "
            "table — a run belongs to a data-source connection, never to a "
            "person, so there is no per-caller row scoping: non-admins see "
            "zero rows here even with the agnes-usage grant, admins see "
            "every run. `report`/`progress`/`skips` are JSON — for a wide scan "
            "prefer `json_extract(report, '$.key')` over pulling the whole "
            "blob for many rows at once. Postgres-backed instances only. "
            "Server-side only; query with `agnes query`."
        ),
        column_descriptions={
            "id": "Run id.",
            "connection_id": "The source_connections row this run crawled.",
            "job_id": "The owning jobs row's id, when the caller recorded one (nullable).",
            "status": "running | done | interrupted | failed. interrupted is its own outcome, not a flavour of failure.",
            "phase": "Last observed phase at checkpoint_at: crawl | convert | anonymize | ingest | plan | finalizing.",
            "started_at": "When the run began.",
            "finished_at": "When the run ended; NULL while running.",
            "checkpoint_at": "Last time this run wrote progress — the 'as of' timestamp, never re-derived at read time.",
            "files_seen": "Files enumerated so far.",
            "files_done": "Files processed so far.",
            "enumeration_done": "Whether delta enumeration has finished (files_seen can still rise until true). Never a fraction with files_done — absolute counters only.",
            "report": "Final run report (large JSON) — {} while the run is live. Prefer json_extract over SELECT * at scale.",
            "progress": "Live counters as of checkpoint_at — a subset of report's own shape.",
            "usage": "LLM token usage this run spent, when a detector used one: {model, calls, input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens}. {} means no tokens spent — a different claim from $0.00.",
            "skips": "Capped list of {path, reason, detail} plus a total, so a truncated list stays visibly truncated.",
            "error": "Error message, when the run failed.",
            "parent_run_id": "For a shard child: the planner run it was enqueued from. NULL for every non-sharded run.",
            "shard_key": "This shard's own delta-unit key (e.g. a drive id), when this run is a shard child.",
            "shard_label": "Human-facing folder path for this shard.",
            "shards_total": "Parent-only: how many children the plan enqueued. NULL on a non-parent row.",
            "shards_done": "Parent-only: how many children have finished so far.",
        },
    ),
    # Postgres-only, same reasoning as `agnes_turns`/`agnes_extraction_runs`
    # above: `facts_ingest_runs` landed after the A3 freeze (Alembic revision
    # 0078, no `src/db.py` ladder step).
    #
    # `filter_kind='admin_only'`, for the same reason as `agnes_extraction_
    # runs`: there is no per-row owner column. `caller` names who triggered
    # the batch, but `POST /api/facts/ingest` is itself admin-only
    # (`app/api/facts.py`), so every row's caller is already an admin —
    # scoping "your own" against it would filter admins against each other,
    # not protect analysts from each other. `corpus_ids` names the
    # collections a batch touched, not an owner; building a collection-
    # membership filter through this table's generic single-column-equality
    # mechanism would need one identical WHERE clause valid as both native
    # Postgres SQL over the JSONB column (source-table materialization) AND
    # DuckDB SQL over the same column already cast to text (the CTE
    # re-application over the materialized copy, see
    # `_select_list_with_json_as_text`) — no single expression satisfies
    # both cleanly, and a subtly-wrong array-containment filter here would
    # be a silent over- or under-disclosure of ingest history, not a loud
    # failure. Admin-only sidesteps that risk entirely: every non-admin
    # caller sees zero rows, admins see every run.
    InternalTable(
        registry_id="agnes_facts_ingest_runs",
        source_table="facts_ingest_runs",
        filter_column=None,
        filter_kind="admin_only",
        display_name="Agnes facts ingest runs",
        description=(
            "One row per POST /api/facts/ingest batch — the fact graph's "
            "ingest history, including the real LLM spend ledger "
            "(`llm_usage`). Admin/operator data, not a per-user table — "
            "ingest is admin-only and a run's corpus_ids name collections "
            "touched, not an owner, so there is no per-caller row scoping: "
            "non-admins see zero rows here even with the agnes-usage "
            "grant, admins see every run. `claims_rejected`/`review_items`/"
            "`deferred` are JSON — for a wide scan prefer "
            "`json_extract(claims_rejected, '$[0].reason')` over pulling "
            "the whole blob for many rows at once. Postgres-backed "
            "instances only. Server-side only; query with `agnes query`."
        ),
        column_descriptions={
            "id": "Run id.",
            "created_at": "When the batch was ingested.",
            "corpus_ids": "Collection ids this batch's documents belonged to — not an owner.",
            "caller": "Who triggered the ingest (email or id). Always an admin identity, since the route is admin-only.",
            "documents_seen": "Documents in the batch.",
            "claims_written": "Claims written by this batch.",
            "claims_rejected_count": "Claims rejected — the itemized list is claims_rejected.",
            "claims_rejected": "Itemized rejected claims (large JSON). Prefer json_extract at scale.",
            "deferred": "Claims deferred pending a subject that did not exist yet at ingest time.",
            "subjects_created": "New subjects created by this batch.",
            "subjects_deleted": "Subjects deleted (merged away) by this batch.",
            "review_items": "Itemized review-queue items this batch created (large JSON). Prefer json_extract at scale.",
            "anonymization": "Producer's declaration that (some of) this batch went through anonymize-in-front, when reported. {} means not declared.",
            "source_urls_rejected_count": "documents[].source_url values dropped as invalid — the claim itself still wrote, only its citation link is missing.",
            "source_urls_rejected": "Itemized {doc_id, reason} for source_urls_rejected_count.",
            "llm_usage": "Per-run LLM token/cost tally, when reported — the cost ledger for fact extraction: {models, input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens, ...}. NULL means this run reported no usage figure, never a fabricated zero.",
            "edges_skipped_missing_endpoint": "Edges not written because their src/dst fact was gone by INSERT time — a race between concurrent extraction passes, never a producer mistake.",
        },
    ),
    # Postgres-only, same reasoning as `agnes_turns`/`agnes_extraction_runs`/
    # `agnes_facts_ingest_runs` above: `llm_calls` landed after the A3 freeze
    # (Alembic revision 0114, no `src/db.py` ladder step), so it exists on
    # Postgres alone.
    InternalTable(
        registry_id="agnes_llm_calls",
        source_table="llm_calls",
        filter_column="user_id",
        filter_kind="user_id",
        display_name="Agnes LLM calls",
        description=(
            "One row per LLM call across every workload — chat, agent API, "
            "builders, extraction, corporate memory and the rest — with the "
            "four token kinds, the USD cost as priced at write time and the "
            "ids that join it to a chat turn, a worker job or an exported "
            "trace. Your own rows only (admins see all). Postgres-backed "
            "instances only. Server-side only; query with `agnes query`."
        ),
        column_descriptions={
            "id": "Call id (uuid).",
            "created_at": "When the call ended (when the row was written).",
            "kind": "completion (chat broker forward) or generation (server-side trace_generation call).",
            "workload": "Coarse kind of work: chat, agent_api, builder, extraction, corporate_memory, knowledge, semantic_layer, anonymization, ocr, vision, auto_title, readiness, store_guardrails, verification.",
            "purpose": "Fine-grained call-site label, e.g. entity_builder_turn, facts_extraction, digest, tagger.",
            "session_id": "Chat session id, when the call happened inside one.",
            "turn_id": "Chat turn id (same id chat_messages.turn_id and usage_turns.turn_uuid carry), when known.",
            "user_id": "Canonical caller id (users.id); the row-level filter keys on this. Never an email.",
            "agent_id": "Owning agent profile id, when the call ran under one.",
            "job_id": "Worker job id, when the call ran inside a background job.",
            "subject_id": "What the call is about: an entity id for a builder, a document id for extraction, a session id for auto-title.",
            "trace_id": "Exported span's trace id (hex), when OTLP export is on. NULL when export is off.",
            "span_id": "Exported span's own id (hex), when OTLP export is on. NULL when export is off.",
            "provider": "LLM SDK provider, e.g. anthropic, openai.",
            "upstream": "Upstream label the broker forwarded to, or the provider name for a server-side generation.",
            "model_requested": "Model the caller asked for.",
            "model_response": "Model the provider actually reported serving, when different from model_requested (e.g. an alias resolved).",
            "input_tokens": "Uncached input tokens for this call.",
            "output_tokens": "Output tokens for this call.",
            "cache_read_tokens": "Prompt-cache READ tokens (input served from cache; billed far below input_tokens).",
            "cache_creation_tokens": "Prompt-cache WRITE tokens (input written into cache; billed above input_tokens).",
            "cost_usd": "USD cost as priced at write time (src.llm_pricing), using priced_as's rates.",
            "priced_as": "The rates this row was priced with: {price_key, input_per_mtok, output_per_mtok, cache_read_per_mtok, cache_write_per_mtok, batch_multiplier}. price_key='default' means the model was unknown and priced at the most expensive general-purpose tier.",
            "latency_ms": "Call duration in milliseconds, when measured.",
            "status": "ok or error.",
            "error_type": "Error classification, when status=error.",
            "http_status": "Upstream HTTP status code, when the call went through the broker.",
            "prompt_chars": "Character length of the prompt content, when captured.",
            "completion_chars": "Character length of the completion content, when captured.",
            "stop_reason": "Model's own stop/finish reason, when reported.",
            "stream_complete": "Whether a streamed completion reached its terminal event, when known.",
            "response_truncated": "True when this row's usage was recovered from bounded head/tail buffers after an oversized stream overflowed the full-body mirror — tokens and cost are still real, only the content summary was cut short.",
        },
    ),
)

INTERNAL_TABLES_BY_ID: dict[str, InternalTable] = {t.registry_id: t for t in INTERNAL_TABLES}


def is_internal_table(table_id: str) -> bool:
    return table_id in INTERNAL_TABLES_BY_ID


# ---------------------------------------------------------------------------
# RBAC filter resolution
# ---------------------------------------------------------------------------

# `+` allowed in both regexes — RFC 5321 local-parts (e.g. alice+test@x)
# resolve to filesystem usernames with a `+`, and the session-data-dir
# layout already supports the same character class.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._+-]{1,200}$")
_USER_ID_RE = re.compile(r"^[A-Za-z0-9._@:+-]{1,200}$")


class InternalAccessError(Exception):
    """Raised when the caller cannot be safely resolved to a filter value
    or when an internal table is misconfigured."""


def _filter_value(user: dict[str, Any], kind: str) -> str:
    """Derive the per-row filter value from the authenticated user.

    For ``username`` we mirror the session-data-dir convention: the
    local-part of the email. This is the same value the UsageProcessor
    writes into ``usage_events.username`` / ``usage_session_summary``.

    For ``user_id`` we use the ``users.id`` UUID directly — same value
    audit_log writes carry.
    """
    if kind == "username":
        email = (user or {}).get("email", "") or ""
        username = email.split("@")[0] if "@" in email else email
        if not _USERNAME_RE.match(username):
            raise InternalAccessError(f"user email {email!r} does not yield a safe username for scoping")
        return username
    if kind == "user_id":
        uid = (user or {}).get("id", "") or ""
        if not _USER_ID_RE.match(uid):
            raise InternalAccessError(f"user_id {uid!r} fails the safe-identifier check")
        return uid
    raise InternalAccessError(f"unknown filter_kind: {kind!r}")


def build_filter_clause(table: InternalTable, user: dict[str, Any], is_admin: bool) -> str:
    """Return the WHERE clause for one internal table.

    Admins get an empty string (unscoped view). Everyone else get
    ``WHERE <col> = '<value>'`` where value has been regex-validated —
    except an ``admin_only`` table (``filter_kind='admin_only'``), which has
    no per-row owner column at all: every non-admin gets the unconditional
    ``WHERE FALSE`` (zero rows), regardless of what value ``_filter_value``
    could resolve for them. The package grant still decides whether the
    TABLE is visible at all (``agnes catalog`` / ``/api/query`` 403); this
    is the separate, independent decision of which ROWS a visible table
    yields, same division of labor as every own-rows table below.

    ``agnes_sessions`` and ``agnes_telemetry`` filter primarily on
    ``user_id`` (stable UUID) but include an OR fallback on
    ``username`` (email local-part) for rows that pre-date the v45
    backfill.  Once all rows carry a non-NULL ``user_id`` the
    ``legacy_username_column`` field can be removed.
    """
    if is_admin:
        return ""
    if table.filter_kind == "admin_only":
        return "WHERE FALSE"
    value = _filter_value(user, table.filter_kind)
    safe = value.replace("'", "''")

    if table.legacy_username_column:
        legacy = _filter_value(user, "username").replace("'", "''")
        return f"WHERE ({table.filter_column} = '{safe}' OR {table.legacy_username_column} = '{legacy}')"

    return f"WHERE {table.filter_column} = '{safe}'"


def sample_internal_rows(table: InternalTable, where_clause: str, n: int) -> list[dict[str, Any]]:
    """Read up to ``n`` rows from an internal table's physical source on the
    ACTIVE state backend (DuckDB or Postgres), applying the RBAC ``where_clause``.

    Internal-table rows live in the state backend; reading them off a raw
    DuckDB connection returns nothing on a Postgres instance (the catalog
    ``/sample`` preview then shows an empty table). Dispatching on ``use_pg()``
    keeps the preview correct on either backend.

    ``where_clause`` comes from :func:`build_filter_clause` — an ANSI
    ``WHERE col = '<escaped>'`` (single quotes doubled, value regex-validated),
    so the same statement runs unchanged on DuckDB and Postgres. The Postgres
    path uses ``exec_driver_sql`` (raw DBAPI) so SQLAlchemy never reinterprets a
    ``:token`` in the literal as a bind parameter.
    """
    n = max(1, int(n))
    sql = f"SELECT * FROM {table.source_table} {where_clause} LIMIT {n}"

    from src.repositories import use_pg

    if use_pg():
        from src.db_pg import get_engine

        with get_engine().connect() as conn:
            return [dict(r) for r in conn.exec_driver_sql(sql).mappings().all()]

    from src.db import get_system_db

    cur = get_system_db().cursor()
    try:
        return cur.execute(sql).fetchdf().to_dict(orient="records")
    finally:
        cur.close()


# ---------------------------------------------------------------------------
# Query execution
# ---------------------------------------------------------------------------

_TABLE_REF_RE = re.compile(
    r"\b(" + "|".join(re.escape(t.registry_id) for t in INTERNAL_TABLES) + r")\b",
    re.IGNORECASE,
)

# DuckDB dollar-quoted string literals: $$...$$ or $tag$...$tag$. Their body
# can contain single quotes, so they MUST be stripped before the single-quote
# scanner runs — otherwise a `'` inside a dollar-quoted block desyncs it,
# blanking the wrong span and hiding a state-table reference from the scanners
# below while DuckDB still executes it as live SQL.
_SQL_DOLLAR_QUOTE_RE = re.compile(r"\$(\w*)\$[\s\S]*?\$\1\$")

# DuckDB Postgres-style escape strings: E'...' / e'...' where a backslash
# escapes the next char (so `E'\''` is the literal `'`). The plain single-quote
# regex mis-lexes these (it treats the escaped `'` as a real close), so strip
# them explicitly too. Together with the dollar-quote form these are the known
# lexer-desync vectors against a regex string-stripper; the PRIMARY defense for
# non-admin queries is allowlist-by-construction (see
# _materialized_internal_duckdb_from_duckdb) — this keeps the routing scan and
# the denylist honest as defense-in-depth.
#
# ReDoS note (security audit F5): the body alternation MUST keep its branches
# mutually exclusive so the regex engine has exactly one way to tokenise every
# character. The earlier form `(?:\\.|''|[^'])*` let a lone backslash match
# BOTH `\\.` and `[^']`, so a long run of backslashes produced exponential
# backtracking and pinned a CPU (single-worker DoS). The `[^'\\]` first branch
# below can never match a backslash, so `\\.` is the only path for one — linear
# time. Keep the branches disjoint if you ever edit this.
_SQL_ESCAPE_STRING_RE = re.compile(r"(?<![A-Za-z0-9_])[eE]'(?:[^'\\]|\\.|'')*'")

# Single-quoted SQL string literals (with `''` escape handling). Stripped
# before reference detection so a non-admin can't trigger the internal
# privileged code path by smuggling the alias inside a literal.
_SQL_STRING_LITERAL_RE = re.compile(r"'(?:''|[^'])*'")

# SQL comments — block `/* … */` and `--` line forms. Stripped so a
# comment-wrapped table name (`/**/users/**/`) can't slip past the
# identifier scan downstream.
_SQL_BLOCK_COMMENT_RE = re.compile(r"/\*[\s\S]*?\*/")
_SQL_LINE_COMMENT_RE = re.compile(r"--[^\n]*")


def _strip_sql_noise(sql: str) -> str:
    """Strip string literals + block + line comments so the identifier
    scanners that follow see only structural SQL. Order matters: dollar-quoted
    and E'' escape strings first (their bodies can contain `'` and would desync
    the single-quote scanner), then single-quoted literals, then comments.
    String content is replaced with empty `''` to keep token spacing intact."""
    s = _SQL_DOLLAR_QUOTE_RE.sub("''", sql)
    s = _SQL_ESCAPE_STRING_RE.sub("''", s)
    s = _SQL_STRING_LITERAL_RE.sub("''", s)
    s = _SQL_BLOCK_COMMENT_RE.sub(" ", s)
    s = _SQL_LINE_COMMENT_RE.sub(" ", s)
    return s


_INTERNAL_ALIAS_NAMES: frozenset[str] = frozenset(t.registry_id.lower() for t in INTERNAL_TABLES)


def _state_table_denylist() -> list[str]:
    """Backend-aware set of state-table names a non-admin internal query must
    not reference directly (everything except the ``agnes_*`` aliases).

    On DuckDB the names come from ``information_schema.tables`` in the
    system.duckdb main schema. On Postgres the system DuckDB must never be
    opened, so the names come from the SQLAlchemy model metadata (the Alembic
    source of truth) — the same tables live in PG. Adding a new sensitive
    table in a future migration is automatically covered on both backends
    without re-editing this module.
    """
    from src.repositories import use_pg

    if use_pg():
        import src.models  # noqa: F401 — registers every model on Base.metadata
        from src.db_pg import Base

        return list(Base.metadata.tables.keys())
    from src.db import get_system_db

    # get_system_db() returns a fresh cursor on the shared singleton — store it
    # and close it in a finally so a non-admin internal query doesn't leak one
    # handle per call (matches the sibling get_schema() below).
    cursor = get_system_db()
    try:
        rows = cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'").fetchall()
    finally:
        cursor.close()
    return [name for (name,) in rows if name is not None]


def _sensitive_table_reference(stripped_sql: str, table_names) -> str | None:
    """Return the first non-allowlisted state table name that appears in
    ``stripped_sql``, or None if clean.

    Allowlist = the registered ``agnes_*`` internal-table IDs. ``table_names``
    is the backend-aware denylist (see :func:`_state_table_denylist`).

    ``stripped_sql`` MUST already have string literals and comments
    stripped (see ``_strip_sql_noise``). Identifier scan is
    case-insensitive word-boundary; schema-prefixed (`main.users`) and
    double-quoted (`"users"`) forms both match because the bare name
    still sits between word boundaries.
    """
    for name in table_names:
        if name is None:
            continue
        if name.lower() in _INTERNAL_ALIAS_NAMES:
            continue
        if re.search(rf"\b{re.escape(name)}\b", stripped_sql, re.IGNORECASE):
            return name
    return None


def find_internal_refs(sql: str) -> list[str]:
    """Word-boundary scan of `sql` for the registered internal table IDs.

    Returns the matched IDs (lowercase, deduped) in declaration order.
    String literals AND comments are stripped first so a literal /
    commented mention of `agnes_sessions` doesn't route the request
    into the privileged internal-query path (review #278 R2 / R3).
    """
    stripped = _strip_sql_noise(sql)
    found = {m.group(1).lower() for m in _TABLE_REF_RE.finditer(stripped)}
    # Preserve declaration order so reasoning about the resulting set is stable.
    return [t.registry_id for t in INTERNAL_TABLES if t.registry_id.lower() in found]


# Safety cap on how many rows a single internal source table may materialise
# into the ephemeral DuckDB on the Postgres path. Non-admin queries are
# RBAC-scoped to the caller's own rows (tiny); only an admin's unscoped query
# over a high-volume table (usage_events) can approach this. Exceeding it
# raises rather than risking an OOM — the one accepted behavioural divergence
# from DuckDB, and only at extreme scale.
_PG_MATERIALIZE_ROW_CAP = 1_000_000


def _select_list_with_json_as_text(source_table: str, physical_columns: "list[str] | None") -> str:
    """Column list for ``SELECT <list> FROM source_table`` that casts every
    JSON-family column (per the SQLAlchemy model's declared type) to text.

    Issue #1310: a heterogeneous JSON column — one row a dict, another a
    list, another a scalar or NULL — arrives from the DBAPI as a
    mixed-shape Python object column. The materialise step's
    ``CREATE TABLE AS SELECT`` over that then lets DuckDB infer the column
    type per result set: a ``STRUCT`` for uniformly-shaped dict rows, a
    ``MAP`` for differently-shaped ones, and otherwise a best-effort
    ``VARCHAR`` holding Python's ``repr()`` of the value (single-quoted —
    not valid JSON). Any of those makes ``agnes_audit`` unqueryable as JSON,
    and the exact outcome silently depends on which rows happen to be in a
    given caller's RBAC-filtered result set.

    Casting each JSON column to text in the SOURCE SELECT — on BOTH
    backends, so the fix does not quietly depend on DuckDB's own
    JSON-to-pandas conversion already stringifying it — makes the
    materialised column type always a deterministic VARCHAR holding the
    engine's own canonical JSON text (Postgres' ``jsonb::text`` / DuckDB's
    ``JSON`` cast), which is queryable with ``json_extract`` / ``->>`` on
    either backend exactly like before, just now unconditionally.

    The projection is built from ``physical_columns`` — the columns the
    source table ACTUALLY has, as introspected by the caller — in physical
    order, so it selects exactly what ``SELECT *`` would have. The model
    metadata (``Base.metadata`` — PG's Alembic source of truth, mirrored by
    the DuckDB ``_vN_to_v(N+1)`` ladder in ``src/db.py``) only decides
    *which* of those columns get the JSON→text cast, so a future JSON/JSONB
    column added to a model inherits the fix with no edit here. Deriving
    the column *list* itself from the model instead would turn any
    one-sided drift between the two schema ladders (or a system.duckdb
    that hasn't finished migrating) into a hard Binder error naming a
    column the analyst never referenced — the projection must degrade like
    ``SELECT *`` did, never fail harder than it. Falls back to ``"*"``
    when the caller has no introspected columns to offer.
    """
    import sqlalchemy as sa

    import src.models  # noqa: F401 — registers every model on Base.metadata
    from src.db_pg import Base

    if not physical_columns:
        return "*"
    table = Base.metadata.tables.get(source_table)
    # `sa.JSON` is the generic base of both the dialect-agnostic and the
    # Postgres-specific `JSON`/`JSONB` column types, so one isinstance check
    # covers every JSON-family column regardless of which subtype a model
    # uses.
    json_cols = {c.name for c in table.columns if isinstance(c.type, sa.JSON)} if table is not None else set()
    parts = []
    for name in physical_columns:
        ident = quote_ident(name)
        if name in json_cols:
            parts.append(f"CAST({ident} AS VARCHAR) AS {ident}")
        else:
            parts.append(ident)
    return ", ".join(parts)


def _materialized_internal_duckdb(refs, user, is_admin):
    """Build a fresh in-memory DuckDB holding ONLY the referenced internal
    source tables, each populated with the caller's RBAC-filtered rows read
    from Postgres. Returns ``(connection, close_callable)``.

    Security by construction: the postgres extension is NOT loaded and nothing
    is ATTACHed, so the postgres TVFs (``postgres_query`` / ``postgres_scan`` /
    ``postgres_execute``) that would bypass the string-stripping guards are
    simply unavailable. Only the (already-filtered) ``agnes_*`` source tables
    exist — base tables like ``users`` are absent (a bare reference errors), and
    because the filter is applied during materialisation, a user CTE that
    shadows an ``agnes_*`` alias still reads only the caller's rows.
    """
    import pandas as pd  # noqa: F401 — referenced by name in the DuckDB scan

    from src.db import _open_duckdb
    from src.db_pg import get_engine

    conn = _open_duckdb(":memory:")
    try:
        engine = get_engine()
        with engine.connect() as pg:
            for table_id in refs:
                table = INTERNAL_TABLES_BY_ID[table_id]
                where_clause = build_filter_clause(table, user, is_admin)
                # source_table values are trusted registry constants, never
                # user input — safe to interpolate into the catalog lookup.
                phys_cols = [
                    r[0]
                    for r in pg.exec_driver_sql(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = current_schema() "
                        f"AND table_name = '{table.source_table}' "
                        "ORDER BY ordinal_position"
                    ).fetchall()
                ]
                select_list = _select_list_with_json_as_text(table.source_table, phys_cols)
                q = (
                    f"SELECT {select_list} FROM {quote_ident(table.source_table)} "
                    f"{where_clause} LIMIT {_PG_MATERIALIZE_ROW_CAP + 1}"
                )
                result = pg.exec_driver_sql(q)
                col_names = list(result.keys())
                fetched = result.mappings().all()
                if len(fetched) > _PG_MATERIALIZE_ROW_CAP:
                    raise InternalAccessError(
                        f"internal query over {table.source_table!r} exceeds the "
                        f"{_PG_MATERIALIZE_ROW_CAP}-row materialisation cap on Postgres; "
                        f"add a more selective WHERE clause"
                    )
                # Empty result → keep the column names so the user SQL's column
                # references still resolve (COUNT/GROUP BY return empty).
                src_df = pd.DataFrame(list(fetched)) if fetched else pd.DataFrame(columns=col_names)
                conn.register("_pg_src_df", src_df)
                conn.execute(f"CREATE TABLE {quote_ident(table.source_table)} AS SELECT * FROM _pg_src_df")
                conn.unregister("_pg_src_df")
        return conn, conn.close
    except Exception:
        conn.close()
        raise


def _materialized_internal_duckdb_from_duckdb(refs, user, is_admin):
    """DuckDB-backend analog of :func:`_materialized_internal_duckdb`.

    Reads the caller's RBAC-filtered rows from the shared ``system.duckdb`` and
    copies them into a FRESH ``:memory:`` DuckDB holding ONLY the referenced
    ``agnes_*`` source tables. Returns ``(connection, close_callable)``.

    Security by construction (used for NON-admin queries): base state tables
    (``users``, ``personal_access_tokens``, ``audit_log``, ``resource_grants``,
    …) simply do not exist in this connection, so any reference to them fails to
    resolve — no string-stripping / denylist is load-bearing, and the SQL-lexer
    desync bypasses (dollar-quoted ``$$…$$`` or ``E'\\''`` escape strings that
    can smuggle a base-table reference past a regex scanner) are structurally
    impossible. Admins keep the shared read path (they are authorised to read
    the raw tables and their unscoped queries should not be row-capped here).

    We do NOT open a second (read-only) handle to the system.duckdb FILE: DuckDB
    serialises file handles process-wide, so a second handle/ATTACH to the
    already-open system.duckdb is rejected (see the handle-conflict history in
    ``execute_internal_query``'s docstring). Copying rows via the existing shared
    connection into an independent ``:memory:`` db sidesteps that entirely.
    """
    from src.db import _open_duckdb, get_system_db

    src = get_system_db()
    mem = _open_duckdb(":memory:")
    try:
        for table_id in refs:
            table = INTERNAL_TABLES_BY_ID[table_id]
            where_clause = build_filter_clause(table, user, is_admin)
            phys_cols = [
                r[0]
                for r in src.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = ? ORDER BY ordinal_position",
                    [table.source_table],
                ).fetchall()
            ]
            select_list = _select_list_with_json_as_text(table.source_table, phys_cols)
            q = f"SELECT {select_list} FROM {table.source_table} {where_clause} LIMIT {_PG_MATERIALIZE_ROW_CAP + 1}"
            src_df = src.execute(q).fetch_df()
            if len(src_df) > _PG_MATERIALIZE_ROW_CAP:
                raise InternalAccessError(
                    f"internal query over {table.source_table!r} exceeds the "
                    f"{_PG_MATERIALIZE_ROW_CAP}-row materialisation cap; "
                    f"add a more selective WHERE clause"
                )
            mem.register("_src_df", src_df)
            mem.execute(f"CREATE TABLE {quote_ident(table.source_table)} AS SELECT * FROM _src_df")
            mem.unregister("_src_df")
        return mem, mem.close
    except Exception:
        mem.close()
        raise
    finally:
        # The shared-singleton cursor is only needed for the copy above; close
        # it so a non-admin internal query doesn't leak one handle per call
        # (the returned :memory: connection stays open for the caller).
        src.close()


def execute_internal_query(
    system_db_path: str,
    user: dict[str, Any],
    is_admin: bool,
    sql: str,
    limit: int = 1000,
) -> tuple[list[str], list[tuple], bool]:
    """Run a SELECT against per-request-scoped internal views.

    Approach: wrap the user SQL in a CTE prefix that defines one
    ``agnes_*`` alias per referenced internal table, each scoped to the
    caller's rows (admin → unscoped). We then ``SELECT * FROM (<user_sql>)``
    inside the wrapper. DuckDB resolves the CTE aliases when it parses
    the user_sql; the caller never sees the real ``usage_*`` tables.

    Why a CTE wrapper instead of TEMP VIEW or ATTACH:

      - ``duckdb.connect(path, read_only=True)`` from a fresh handle is
        rejected when the app's main connection already holds
        ``system.duckdb`` open RW: "Can't open a connection to same
        database file with a different configuration than existing
        connections".
      - ``ATTACH '<path>' AS sys (READ_ONLY)`` from a :memory: handle is
        rejected with "Binder Error: Unique file handle conflict — the
        database file is already attached by …": DuckDB serialises file
        handles process-wide, so two connections can't reach the same
        ``.duckdb`` file even at different attach points.
      - ``TEMP VIEW`` on the shared singleton connection bleeds across
        concurrent requests (TEMP VIEWS are connection-scoped, and the
        request-handler pool reuses the same handle).

      CTE wrap leaves no residual state on the connection and isolates
      naturally per request. The SQL stays in SELECT space; existing
      keyword-denylist + sanitised-username defenses still apply.
    """
    # The SELECT-only guard upstream (_assert_select_only) tolerates one
    # trailing semicolon, but it would terminate the CTE-wrapped subquery
    # below early; strip the same single trailing semicolon here.
    sql = strip_one_trailing_semicolon(sql)

    refs = find_internal_refs(sql)
    if not refs:
        raise InternalAccessError("no internal-table references in SQL")

    # Lazy import to avoid a hard cycle (src.db imports go via repositories
    # which then end up importing access in some test paths).
    from src.db import get_system_db
    from src.repositories import use_pg

    # On a Postgres-backed instance the internal source tables live in PG, not
    # the DuckDB system file. To keep the analyst's arbitrary DuckDB SQL behaving
    # identically on both backends we still execute it in DuckDB, but over a
    # fresh in-memory handle into which the caller's RBAC-filtered rows have been
    # materialised from PG (see _materialized_internal_duckdb). The postgres
    # extension is deliberately NOT loaded / nothing is ATTACHed, so its
    # string-arg table functions can't be used to bypass the identifier guards.
    # ``pg`` selects that path.
    pg = use_pg()

    # Non-admins are NOT allowed to reference any state table outside the
    # registered agnes_* aliases. The CTE wrapper only scopes those aliases;
    # a direct FROM on the base table (`usage_session_summary`, `audit_log`,
    # `users`, `personal_access_tokens`, etc.) would bypass row-level RBAC and
    # leak other users' data. The denylist comes from `_state_table_denylist()`,
    # which is backend-aware — DuckDB `information_schema.tables` on DuckDB,
    # SQLAlchemy model metadata on Postgres (the system DuckDB is never opened
    # there) — so every state table that is NOT one of the agnes_* aliases is
    # sensitive. This is future-proof on both backends: new tables added by
    # later migrations are automatically covered without re-editing this module.
    #
    # Admin path is unaffected — admins have legitimate need to read
    # raw rows, and the filter clause is empty for them anyway.
    if not is_admin:
        stripped = _strip_sql_noise(sql)
        # Backend-aware denylist — never opens the system DuckDB on Postgres.
        sensitive = _sensitive_table_reference(stripped, _state_table_denylist())
        if sensitive is not None:
            raise InternalAccessError(
                f"non-admin SQL cannot reference table {sensitive!r}; query one of the agnes_* aliases instead"
            )
    cte_parts = []
    for table_id in refs:
        table = INTERNAL_TABLES_BY_ID[table_id]
        where_clause = build_filter_clause(table, user, is_admin)
        # Identical on both backends: the agnes_* alias selects from the source
        # table. On PG that table is a per-request DuckDB copy holding only the
        # caller's RBAC-filtered rows (built by _materialized_internal_duckdb),
        # so the boundary is the materialisation, not this CTE.
        cte_parts.append(f"{table.registry_id} AS (SELECT * FROM {table.source_table} {where_clause})")
    cte_prefix = "WITH " + ", ".join(cte_parts)
    wrapped = f"{cte_prefix} SELECT * FROM ({sql}) AS _agnes_user_query"

    if pg:
        conn, _close = _materialized_internal_duckdb(refs, user, is_admin)
    elif not is_admin:
        # DuckDB backend, non-admin: allowlist-by-construction. Materialise ONLY
        # the RBAC-filtered agnes_* source tables into a fresh :memory: DuckDB so
        # a reference to any non-agnes_* state table cannot resolve. This is the
        # PRIMARY defense against the SQL-lexer desync class (dollar-quoted / E''
        # escape strings smuggling a base-table reference past the regex
        # denylist above, which is kept only as defense-in-depth).
        conn, _close = _materialized_internal_duckdb_from_duckdb(refs, user, is_admin)
    else:
        # DuckDB backend, admin: read directly against the shared system.duckdb
        # singleton (authorised to read raw rows; no row-cap materialisation).
        # _close stays None so _cleanup never closes the shared connection.
        conn = get_system_db()
        _close = None
    cursor = conn.cursor()

    def _cleanup() -> None:
        try:
            cursor.close()
        finally:
            if _close is not None:
                _close()

    try:
        rows = cursor.execute(wrapped).fetchmany(limit + 1)
        cols = [d[0] for d in cursor.description] if cursor.description else []
        truncated = len(rows) > limit
        return cols, rows[:limit], truncated
    finally:
        try:
            _cleanup()
        except Exception:
            logger.exception("close() failed on internal-query cursor")


# ---------------------------------------------------------------------------
# Schema introspection — feeds /api/v2/schema/{id} for internal tables
# ---------------------------------------------------------------------------


def get_schema(system_db_path: str, table_id: str) -> list[dict]:
    """Return the underlying physical schema for an internal table.

    Used by ``/api/v2/schema/<id>`` so ``agnes schema <table>`` works
    against internal sources.

    Backend-aware: on DuckDB the columns come from the shared
    ``system.duckdb`` handle's ``information_schema`` (same rationale as
    ``execute_internal_query``: opening a parallel handle to the same file is
    process-wide blocked). On Postgres the system DuckDB must never be opened
    (``get_system_db()`` raises there), so the same physical tables are read
    from Postgres' ``information_schema.columns`` via the SQLAlchemy engine.
    Both paths return the same ``[{name, type, nullable}]`` shape and are
    read-only.

    ``system_db_path`` is kept in the signature for API symmetry with the
    earlier draft, but is unused — the singleton handle already knows the
    path.
    """
    if table_id not in INTERNAL_TABLES_BY_ID:
        return []
    table = INTERNAL_TABLES_BY_ID[table_id]
    from src.repositories import use_pg

    if use_pg():
        import sqlalchemy as sa

        from src.db_pg import get_engine

        with get_engine().connect() as pg_conn:
            rows = pg_conn.execute(
                sa.text(
                    "SELECT column_name, data_type, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = :t "
                    "ORDER BY ordinal_position"
                ),
                {"t": table.source_table},
            ).fetchall()
        return [{"name": r[0], "type": r[1], "nullable": r[2] == "YES"} for r in rows]

    from src.db import get_system_db

    cursor = get_system_db().cursor()
    try:
        rows = cursor.execute(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_name = ? ORDER BY ordinal_position",
            [table.source_table],
        ).fetchall()
        return [{"name": r[0], "type": r[1], "nullable": r[2] == "YES"} for r in rows]
    finally:
        try:
            cursor.close()
        except Exception:
            pass
