# LLM Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One record per LLM call from every call site (who / for what / in which turn / at what price), written to the on-instance `llm_calls` ledger and to the opt-in OTLP export, with a real trace per chat turn, a thumbs feedback signal on chat, provenance on agent memory, and one content-export policy that governs every export path including the engine's relay.

**Architecture:** A `contextvars`-backed `LlmCallContext` labels every generation with workload/purpose/identity; `LlmCallRecord` is the one shape both sinks consume; the broker and `trace_generation` are the two producers. ChatManager mints a `turn_id` per user message, publishes the turn's span context under a coordination key so the broker can parent its completion spans without engine propagation, and stamps `turn_id` onto every frame. Content export is decided by a policy record in `instance.yaml` (mode + placement + consent) that the app's own spans AND the relay obey; everything new in app-state is Postgres-only (A3 ratchet), one Alembic revision.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy/Alembic (Postgres), DuckDB (frozen app-state sibling), OpenTelemetry SDK + `opentelemetry-proto` (already in `[server]` extras), Typer CLI, Jinja2 + vanilla JS web UI, pytest.

**Spec:** `docs/superpowers/specs/2026-09-08-llm-observability-design.md` — read it in full before any task. Section 4 lists decisions already taken; do not re-open them.

## Global Constraints

- **Postgres-first ratchet (A3):** every new app-state table/column is an Alembic revision + `src/models/*` model + `src/repositories/<name>_pg.py` registered `PG`-only in `src/repositories/__init__.py` `_REGISTRY`. No new `src/db.py` step, no new DuckDB repo module, `SCHEMA_VERSION` does not move. Existing DuckDB↔PG pairs (`agent_memories`, chat messages via `app/chat/persistence.py`) stay mirrored: the DuckDB side accepts-and-drops the new PG-only kwargs (the `chat_messages` cache-token precedent, migration `0092`).
- **A PG-only feature fails clean on DuckDB:** routes resolve the PG-only repo as a FastAPI dependency so `RequiresPostgresBackend` surfaces before body validation and `app/main.py` answers the typed `501 requires_postgres_backend`; ledger writers swallow it silently.
- **Instrumentation never fails the call it observes** (spec 3.9): every producer and both sinks catch their own exceptions and log at DEBUG.
- **Audit:** every new route declares its posture in `src/audit_posture.py` (`POSTURE` for POST, `READ_POSTURE` for GET); every new action string is in `src/audit_events.py` `CATALOG`; writes go through `src.audit_helpers.log_safe` or `app.chat.audit.write_audit`; content (prompts, comments) never enters `params`.
- **Seven places for a new `/api/*` route** (`.claude/skills/agnes-conventions/references/audit.md`): posture, catalog, `tests/test_documentation_api_triple_surface.py` (`_EXEMPT` with reason), `docs/api-reference.md`, `tests/test_route_auth_guard.py` (only if no auth dep), `tests/db_pg/test_endpoints_smoke.py` `KNOWN_UNTESTED`, `tests/snapshots/openapi.json` (regenerated ONCE by the integrator: `make update-openapi-snapshot`).
- **Vendor-agnostic:** no customer names, hostnames, project ids, private-repo references in code, comments, docs, commits.
- **CHANGELOG:** one fragment `changelog.d/llm-observability.md` written by the integrator (Task 9). Builders never edit `CHANGELOG.md` or `CLAUDE.md`; they report their bullet in their final message.
- **No AI attribution** in commit messages or the PR.
- **Design system:** the admin page section uses `--ds-*` tokens, no raw hex, no `var(--primary)`; the page already extends `base_admin_page.html`.
- **Command-UX standard** for the new CLI commands: `--limit`, `--json`; no new boolean scope flag; a "nothing found" answer says what to do next.
- **Tests:** builders run ONLY their own test files plus the specific guards named in their task. Never a lane, never the full suite (one host, many parallel builders). The integrator runs `--lane impacted` once; CI runs everything on the push.
- **Worktree hygiene:** a builder in an isolated worktree must NOT create a venv; symlink the main checkout's `.venv` into the worktree first (`ln -sfn <main-checkout>/.venv .venv`) and use `.venv/bin/pytest`.
- **Postgres tests** (`tests/db_pg/`) run locally through the bundled `pixeltable_pgserver` (default `AGNES_TEST_PG_BACKEND=pgserver`); they are slow to boot (one shared server per run) — run them once per task, not per edit.
- Spec vocabulary, verbatim: workloads `chat`, `agent_api`, `builder`, `extraction`, `corporate_memory`, `knowledge`, `semantic_layer`, `anonymization`, `ocr`, `vision`, `auto_title`, `readiness`, `store_guardrails`, `verification`, `admin_ask`; content modes `off | pseudonymized | full`; placements `operator | third_party`; coordination key `chat:turn:{session_id}` with a 24 h TTL; span names `agnes.chat.turn`, `agnes.chat.tool <tool>`, `agnes.chat.feedback`; span event `agnes.feedback`; audit actions `chat.feedback`, `observability.content_export`; migration `0115_llm_observability`; config keys `observability.content_export.{mode,placement,basis,approved_by,approved_at}` and `retention.llm_calls_days`.

---

## File map (who owns what)

| File | Task | Responsibility |
|---|---|---|
| `src/observability/llm_context.py` (new) | 1 | `LlmCallContext` + `llm_context()` / `bind_llm_context()` |
| `src/observability/llm_record.py` (new) | 1 | `LlmCallRecord`, `build_record()`, `priced_as_for()`, `span_ids()` |
| `src/observability/llm_ledger.py` (new) | 1 | `record_call()` — the direct ledger sink (no-op until the repo exists / on DuckDB) |
| `src/llm_pricing.py` | 1 | `resolve_price_key()` |
| `src/observability/otel.py` | 1, 4, 5 | identity minimisation, context attrs, cost, cache tokens on generation spans, parent context, turn/tool spans, policy-gated content |
| `src/observability/llm_tracing.py` | 1, 5 | `purpose`, four token kinds (both shapes), pricing, record emission, `record_generation()`, prompt/completion text capture |
| `src/observability/__init__.py` | 1 | re-exports |
| `app/api/broker_agent_policy.py` | 1 | `UsageAccumulator.add_call()` + `llm_calls` flush |
| `app/api/broker.py` | 2, 4, 5 | record at both usage sites; no `user_email`; no session read on the span path; child-of-turn linkage; relay gating |
| `app/chat/turn_context.py` (new) | 4 | `TurnRecord`, `publish_turn()`, `read_turn()` over `chat:turn:{session_id}` |
| `app/chat/manager.py` | 4, 6 | turn id + spans + frame stamping; `turn_id` to `append_message` |
| `app/worker/runtime.py` | 3 | bind `job_id` into the LLM context |
| call sites listed in spec 3.3 | 3 | `trace_generation(purpose=…)` wraps + `llm_context(...)` at entry points |
| `tests/test_llm_coverage_guard.py` (new) | 3 | static scan: every `messages.create(` in the 3.3 modules sits inside `trace_generation` |
| `src/observability/content_policy.py` (new) | 5 | policy record, `content_export_mode()`, `export_text()`, startup announce + audit |
| `src/observability/otlp_scrub.py` (new) | 5 | protobuf strip/pseudonymise for traces and logs, gzip |
| `config/instance.yaml.example` | 5, 6 | `observability.content_export` block; `retention.llm_calls_days` |
| `migrations/versions/0115_llm_observability.py` (new) | 6 | `llm_calls`, `chat_message_feedback`, `chat_messages.turn_id`, `agent_memories.source_turn_id/source_message_id` |
| `src/models/llm_observability.py` (new), `src/models/chat.py`, `src/models/agents.py`, `src/models/__init__.py` | 6 | models |
| `src/repositories/llm_calls_pg.py`, `src/repositories/chat_message_feedback_pg.py` (new), `src/repositories/__init__.py` | 6 | PG-only repos + registry |
| `src/repositories/chat_messages_pg.py`, `app/chat/persistence.py`, `app/chat/types.py` | 6 | `turn_id` column (PG writes, DuckDB drops) |
| `src/repositories/agent_memories.py`, `agent_memories_pg.py`, `app/api/agent_memory.py` | 6 | memory provenance |
| `connectors/internal/access.py`, `connectors/internal/registry.py` | 6 | `agnes_llm_calls` internal table |
| `src/audit_retention.py`, `app/instance_config.py`, `app/api/admin.py` | 6 | `llm_calls` retention trail |
| `app/api/admin_usage.py`, `cli/commands/admin_usage.py`, `app/web/templates/admin_usage.html` | 7 | `llm-cost`, `llm-calls`, `feedback` reads + CLI + page section |
| `app/api/chat.py`, `app/web/static/js/chat.js`, `app/web/static/css/chat.css` | 8 | feedback endpoint + thumbs UI |
| `src/audit_posture.py`, `src/audit_events.py`, `tests/test_documentation_api_triple_surface.py`, `tests/db_pg/test_endpoints_smoke.py`, `tests/db_pg/test_get_status_parity_sweep.py`, `docs/api-reference.md` | 5, 7, 8 | the route bookkeeping (append-only edits) |
| `docs/observability.md`, `changelog.d/llm-observability.md`, `CLAUDE.md`, `tests/snapshots/openapi.json` | 9 | docs, fragment, snapshot |

## Execution notes for the orchestrator

- Rollout order (spec 3.11) = task order. Tasks 1→2 sequential (2 consumes 1's API). Tasks 3, 4, 5 run in PARALLEL after Task 2 (disjoint regions: 3 touches call sites + `app/worker/runtime.py`; 4 touches `app/chat/*` + the broker's span-open path + new `otel.py` functions appended at the end; 5 touches `capture_content_enabled` / the event-emission blocks in `otel.py`, `llm_tracing.py`'s `_Capture` text fields, and the broker's `otlp_proxy`). Task 6 after 3–5 (the migration lands last). Tasks 7 and 8 in PARALLEL after 6 (both append to the posture/catalog/exempt/smoke/api-docs lists — append-only, integrator keeps both). Task 9 last.
- Every task's builder commits in its own worktree with a clean message (no AI attribution) and reports: branch, commit(s), files, tests run with output, CHANGELOG bullet hint.
- The integrator regenerates `tests/snapshots/openapi.json` once after Tasks 7 and 8 are merged, and runs `python3 scripts/verify_syncmap.py` + `--lane impacted`.

---

### Task 1: Foundation — call context, call record, pricing key, ledger sink, span attributes, `trace_generation` upgrades

**Files:**
- Create: `src/observability/llm_context.py`
- Create: `src/observability/llm_record.py`
- Create: `src/observability/llm_ledger.py`
- Modify: `src/llm_pricing.py` (`resolve_price` → add `resolve_price_key`)
- Modify: `src/observability/otel.py` (`start_completion_span`, `_open_span`, `start_generation_span`, `end_generation_span`, new `span_ids`, `remote_parent_context`)
- Modify: `src/observability/llm_tracing.py` (`_Capture`, `trace_generation`, new `record_generation`)
- Modify: `src/observability/__init__.py` (re-exports)
- Modify: `app/api/broker_agent_policy.py` (`UsageAccumulator.add_call`, flush of call rows)
- Test: `tests/test_llm_context.py` (new), `tests/test_llm_record.py` (new), `tests/test_llm_tracing.py` (extend), `tests/test_otel_export.py` (adjust: `user_email` gone, cache tokens + cost on generation spans), `tests/test_broker_agent_policy.py` (extend), `tests/test_llm_pricing.py` (extend if it exists, else create `tests/test_llm_pricing_key.py`)

**Interfaces:**
- Produces `src.observability.llm_context`:
  - `WORKLOADS: frozenset[str]` (the 14 spec values)
  - `@dataclass(frozen=True) class LlmCallContext: workload, purpose, session_id, turn_id, user_id, agent_id, job_id, subject_id: str | None = None` with `merged(**overrides) -> LlmCallContext` (a `None` override never clears an inherited value) and `span_attributes() -> dict[str, str]` (keys `agnes.workload`, `agnes.purpose`, `agnes.session_id`, `agnes.turn_id`, `agnes.user_id`, `agnes.agent_id`, `agnes.job_id`, `agnes.subject_id`; unset fields omitted).
  - `current_llm_context() -> LlmCallContext` (empty context when nothing is bound)
  - `@contextmanager llm_context(**fields) -> Iterator[LlmCallContext]` (pushes `current.merged(**fields)`, restores on exit)
  - `bind_llm_context(**fields) -> contextvars.Token`, `unbind_llm_context(token) -> None`
- Produces `src.observability.llm_record`:
  - `@dataclass(frozen=True) class LlmCallRecord` with fields exactly: `id, created_at, kind, workload, purpose, session_id, turn_id, user_id, agent_id, job_id, subject_id, trace_id, span_id, provider, upstream, model_requested, model_response, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, cost_usd, priced_as, latency_ms, status, error_type, http_status, prompt_chars, completion_chars, stop_reason, stream_complete` and methods `to_row() -> dict[str, Any]` (column names = field names; `created_at` a tz-aware datetime; `priced_as` a dict) and `span_attributes() -> dict` (`agnes.cost_usd` + the context attributes).
  - `priced_as_for(model: str | None, *, batch: bool = False) -> dict[str, float | str]` → `{"price_key": <known key or "default">, "input_per_mtok", "output_per_mtok", "cache_read_per_mtok", "cache_write_per_mtok", "batch_multiplier": 1.0 | 0.5}`
  - `build_record(*, kind, context, provider, upstream, model_requested, model_response, usage, latency_ms, status, error_type=None, http_status=None, prompt_chars=None, completion_chars=None, stop_reason=None, stream_complete=None, trace_id=None, span_id=None, batch=False, created_at=None) -> LlmCallRecord` where `usage` is `parse_usage`'s dict shape (`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, optional `model`) or `None`.
  - `usage_from_anthropic(usage_obj) -> dict` and `usage_from_openai(usage_obj) -> dict` (the four kinds, ints, zeros when absent; OpenAI reads `prompt_tokens`, `completion_tokens`, `prompt_tokens_details.cached_tokens` → `cache_read_tokens`, and `input_tokens = prompt_tokens - cached_tokens`).
- Produces `src.observability.llm_ledger.record_call(record: LlmCallRecord) -> None` — returns immediately when `src.repositories.use_pg()` is false; otherwise `llm_calls_repo().insert_batch([record.to_row()])` inside `try/except Exception` logged at DEBUG (`ImportError`/`AttributeError` included: the repo lands in Task 6).
- Produces `src.llm_pricing.resolve_price_key(model) -> str | None` (the `PRICES` key that `resolve_price` would use; `None` when it falls back to `DEFAULT_PRICE`).
- Produces in `src.observability.otel`:
  - `start_completion_span(*, upstream, model, stream, session_id, ticket_scope, user_id=None, agent_id=None, context: LlmCallContext | None = None, parent_context: Any = None)` — **no `user_email` parameter**; sets the context's span attributes.
  - `start_generation_span(*, provider, model, context: LlmCallContext | None = None)`
  - `end_generation_span(span, *, input_tokens=None, output_tokens=None, cache_read_tokens=None, cache_creation_tokens=None, cost_usd=None, prompt_chars=None, completion_chars=None, error_type=None, user_id=None)` — sets `gen_ai.usage.cache_read_input_tokens`, `gen_ai.usage.cache_creation_input_tokens`, `agnes.cost_usd`.
  - `end_completion_span(span, *, ..., cost_usd: float | None = None)` — sets `agnes.cost_usd` when given (everything else unchanged in this task).
  - `span_ids(span) -> tuple[str | None, str | None]` — `(trace_id_hex32, span_id_hex16)` for a recording span, `(None, None)` otherwise.
  - `remote_parent_context(trace_id_hex: str, span_id_hex: str) -> Any | None` — an OTel `Context` carrying a `NonRecordingSpan(SpanContext(..., is_remote=True, trace_flags=SAMPLED))`; `None` on bad input or when the API is absent.
  - `_open_span(name, attrs, *, kind=None, parent_context=None)` (private; passes `context=parent_context` to `start_span`).
- Produces in `src.observability.llm_tracing`:
  - `trace_generation(*, provider, model, distinct_id=None, purpose=None, subject_id=None, batch=False)` — `purpose` falls back to the context's; `distinct_id` still maps to `user_id` and is merged into the context for this call.
  - `_Capture.set_output_from_anthropic(response)` reads the four token kinds via `usage_from_anthropic`; `set_output_from_openai` via `usage_from_openai`; new attributes `cache_read_tokens`, `cache_creation_tokens`, `model_response` (from `response.model` when present), `stop_reason` (`response.stop_reason` / `choices[0].finish_reason`).
  - `record_generation(*, provider, model, purpose, usage, latency_ms=None, prompt_chars=None, completion_chars=None, subject_id=None, batch=False, error_type=None) -> None` — emits log + span + ledger for a call that was not timed in-process (a batch result).
- Produces in `app.api.broker_agent_policy.UsageAccumulator`: `add_call(row: dict) -> None` buffering `llm_calls` rows (skipped when `use_pg()` is false), flushed by `flush()` after the `llm_usage` rows through `llm_calls_repo().insert_batch(rows)`; an `ImportError`/`AttributeError`/`RequiresPostgresBackend` drops the rows at DEBUG, any other failure logs at ERROR and drops.

- [ ] **Step 1: Write the failing context tests**

`tests/test_llm_context.py`:

```python
"""The LLM call context — a contextvar every producer reads so a span and a
ledger row can say who ran the call, for what, in which turn."""

from __future__ import annotations

import asyncio

from src.observability.llm_context import (
    WORKLOADS,
    LlmCallContext,
    bind_llm_context,
    current_llm_context,
    llm_context,
    unbind_llm_context,
)


def test_the_default_context_is_empty():
    ctx = current_llm_context()
    assert ctx == LlmCallContext()
    assert ctx.span_attributes() == {}


def test_nested_contexts_merge_and_inner_wins():
    with llm_context(workload="builder", purpose="entity_builder_turn", user_id="u1"):
        with llm_context(purpose="inner", subject_id="ent_1"):
            ctx = current_llm_context()
            assert ctx.workload == "builder"          # inherited
            assert ctx.purpose == "inner"             # inner wins
            assert ctx.user_id == "u1"
            assert ctx.subject_id == "ent_1"
        assert current_llm_context().purpose == "entity_builder_turn"  # restored
    assert current_llm_context() == LlmCallContext()


def test_a_none_override_never_clears_an_inherited_value():
    with llm_context(workload="chat", user_id="u1"):
        with llm_context(user_id=None):
            assert current_llm_context().user_id == "u1"


def test_span_attributes_carry_only_set_fields():
    ctx = LlmCallContext(workload="ocr", purpose="scan_ocr", job_id="job_1")
    assert ctx.span_attributes() == {
        "agnes.workload": "ocr",
        "agnes.purpose": "scan_ocr",
        "agnes.job_id": "job_1",
    }


def test_bind_and_unbind_token():
    token = bind_llm_context(job_id="job_9")
    assert current_llm_context().job_id == "job_9"
    unbind_llm_context(token)
    assert current_llm_context().job_id is None


def test_context_is_task_local():
    async def _child():
        return current_llm_context().workload

    async def _main():
        with llm_context(workload="extraction"):
            return await asyncio.create_task(_child())

    assert asyncio.run(_main()) == "extraction"
    assert current_llm_context().workload is None


def test_workload_vocabulary_is_the_spec_list():
    assert WORKLOADS == frozenset(
        {
            "chat", "agent_api", "builder", "extraction", "corporate_memory", "knowledge",
            "semantic_layer", "anonymization", "ocr", "vision", "auto_title", "readiness",
            "store_guardrails", "verification",
        }
    )
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_llm_context.py -q`
Expected: FAIL with `ModuleNotFoundError: src.observability.llm_context`

- [ ] **Step 3: Implement `src/observability/llm_context.py`**

```python
"""The LLM call context — who is running a model call, for what, in which turn.

A ``contextvars.ContextVar`` holding an immutable :class:`LlmCallContext`.
A call site pushes one with :func:`llm_context` (``with llm_context(
workload="builder", purpose="entity_builder_turn", user_id=...)``); nested
pushes MERGE — inner values win, unset fields inherit — so an entry point can
label the workload once and a deeper helper can add the purpose. Every
producer (the chat broker, ``trace_generation``) reads
:func:`current_llm_context` and copies the fields onto the span and the
ledger row, which is what lets a builder turn, a corporate-memory extraction
and an auto-title stop looking identical.

``bind_llm_context`` / ``unbind_llm_context`` are the token form for a
runtime that cannot use a ``with`` block around the whole unit of work (the
worker binds ``job_id`` where it already binds ``request_id``).
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass, fields, replace
from typing import Any, Iterator

WORKLOADS: frozenset[str] = frozenset(
    {
        "chat",
        "agent_api",
        "builder",
        "extraction",
        "corporate_memory",
        "knowledge",
        "semantic_layer",
        "anonymization",
        "ocr",
        "vision",
        "auto_title",
        "readiness",
        "store_guardrails",
        "verification",
    }
)


@dataclass(frozen=True)
class LlmCallContext:
    workload: str | None = None
    purpose: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    user_id: str | None = None
    agent_id: str | None = None
    job_id: str | None = None
    subject_id: str | None = None

    def merged(self, **overrides: Any) -> "LlmCallContext":
        """A copy with ``overrides`` applied; a ``None`` override keeps the
        inherited value (nesting can only add or replace, never clear)."""
        clean = {k: v for k, v in overrides.items() if v is not None}
        unknown = set(clean) - {f.name for f in fields(self)}
        if unknown:
            raise TypeError(f"unknown llm context field(s): {sorted(unknown)}")
        return replace(self, **clean)

    def span_attributes(self) -> dict[str, str]:
        return {f"agnes.{f.name}": str(getattr(self, f.name)) for f in fields(self) if getattr(self, f.name)}


_current: contextvars.ContextVar[LlmCallContext | None] = contextvars.ContextVar("agnes_llm_context", default=None)


def current_llm_context() -> LlmCallContext:
    return _current.get() or LlmCallContext()


def bind_llm_context(**fields_: Any) -> contextvars.Token:
    return _current.set(current_llm_context().merged(**fields_))


def unbind_llm_context(token: contextvars.Token) -> None:
    _current.reset(token)


@contextmanager
def llm_context(**fields_: Any) -> Iterator[LlmCallContext]:
    token = bind_llm_context(**fields_)
    try:
        yield current_llm_context()
    finally:
        unbind_llm_context(token)


__all__ = [
    "WORKLOADS",
    "LlmCallContext",
    "bind_llm_context",
    "current_llm_context",
    "llm_context",
    "unbind_llm_context",
]
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_llm_context.py -q` → PASS

- [ ] **Step 5: Add `resolve_price_key` to `src/llm_pricing.py`**

Refactor so both functions share one resolver:

```python
def _resolve(model: str | None) -> tuple[str | None, ModelPrice]:
    if not model:
        return None, DEFAULT_PRICE
    key = model.strip().lower()
    if key in PRICES:
        return key, PRICES[key]
    key = key.removeprefix("anthropic.")
    for known in sorted(PRICES, key=len, reverse=True):
        if key.startswith(known):
            return known, PRICES[known]
    return None, DEFAULT_PRICE


def resolve_price(model: str | None) -> ModelPrice:
    """(docstring unchanged)"""
    return _resolve(model)[1]


def resolve_price_key(model: str | None) -> str | None:
    """The ``PRICES`` key ``model`` resolved to, or ``None`` when it priced at
    :data:`DEFAULT_PRICE` — so a stored ``priced_as`` can say "this figure is
    a guess at the most expensive general-purpose tier", not pass as a
    measurement."""
    return _resolve(model)[0]
```

Test (append to `tests/test_llm_pricing.py` if it exists, else create `tests/test_llm_pricing_key.py`):

```python
from src.llm_pricing import DEFAULT_PRICE, resolve_price, resolve_price_key


def test_resolve_price_key_names_the_table_row_or_none():
    assert resolve_price_key("claude-sonnet-5-20260101") == "claude-sonnet-5"
    assert resolve_price_key("anthropic.claude-opus-5") == "claude-opus-5"
    assert resolve_price_key("totally-unknown") is None
    assert resolve_price("totally-unknown") is DEFAULT_PRICE
```

Run: `.venv/bin/pytest tests/test_llm_pricing*.py -q` → PASS

- [ ] **Step 6: Write the failing record tests**

`tests/test_llm_record.py`:

```python
"""``LlmCallRecord`` — the one shape both sinks (span, ledger) consume."""

from __future__ import annotations

from datetime import datetime, timezone

from src.llm_pricing import DEFAULT_PRICE, cost_usd
from src.observability.llm_context import LlmCallContext
from src.observability.llm_record import (
    LlmCallRecord,
    build_record,
    priced_as_for,
    usage_from_anthropic,
    usage_from_openai,
)

_USAGE = {"input_tokens": 1000, "output_tokens": 100, "cache_read_tokens": 5000, "cache_creation_tokens": 200}


def test_build_record_prices_the_four_token_kinds_and_stores_the_rates():
    ctx = LlmCallContext(workload="chat", purpose="completion", session_id="s1", turn_id="t1", user_id="u1")
    rec = build_record(
        kind="completion", context=ctx, provider="anthropic", upstream="anthropic",
        model_requested="claude-sonnet-5", model_response="claude-sonnet-5-20260101",
        usage=_USAGE, latency_ms=812, status="ok", http_status=200,
        prompt_chars=40, completion_chars=12, stop_reason="end_turn", stream_complete=True,
        trace_id="a" * 32, span_id="b" * 16,
    )
    assert isinstance(rec, LlmCallRecord)
    assert rec.cost_usd == round(cost_usd(model="claude-sonnet-5-20260101", **_USAGE), 6)
    assert rec.priced_as["price_key"] == "claude-sonnet-5"
    assert rec.priced_as["input_per_mtok"] == 3.0
    assert rec.priced_as["cache_read_per_mtok"] == round(3.0 * 0.1, 6)
    assert rec.priced_as["cache_write_per_mtok"] == round(3.0 * 1.25, 6)
    assert rec.priced_as["batch_multiplier"] == 1.0
    assert rec.turn_id == "t1" and rec.user_id == "u1" and rec.workload == "chat"
    assert rec.trace_id == "a" * 32 and rec.span_id == "b" * 16
    assert rec.id and rec.created_at.tzinfo is not None
    row = rec.to_row()
    assert row["cost_usd"] == rec.cost_usd and row["priced_as"] == rec.priced_as and row["kind"] == "completion"
    assert set(row) == {
        "id", "created_at", "kind", "workload", "purpose", "session_id", "turn_id", "user_id", "agent_id",
        "job_id", "subject_id", "trace_id", "span_id", "provider", "upstream", "model_requested",
        "model_response", "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens",
        "cost_usd", "priced_as", "latency_ms", "status", "error_type", "http_status", "prompt_chars",
        "completion_chars", "stop_reason", "stream_complete",
    }


def test_an_unknown_model_prices_at_the_default_and_says_so():
    rec = build_record(
        kind="generation", context=LlmCallContext(), provider="openai_compat", upstream="openai_compat",
        model_requested="mystery-9", model_response=None, usage=_USAGE, latency_ms=1, status="ok",
    )
    assert rec.priced_as["price_key"] == "default"
    assert rec.priced_as["input_per_mtok"] == DEFAULT_PRICE.input_per_mtok


def test_batch_pricing_halves_every_term():
    rec = build_record(
        kind="generation", context=LlmCallContext(), provider="anthropic", upstream="anthropic",
        model_requested="claude-haiku-4-5", model_response=None, usage=_USAGE, latency_ms=None,
        status="ok", batch=True,
    )
    assert rec.priced_as["batch_multiplier"] == 0.5
    assert rec.cost_usd == round(cost_usd(model="claude-haiku-4-5", batch=True, **_USAGE), 6)


def test_a_failed_call_without_usage_is_a_zero_cost_error_row():
    rec = build_record(
        kind="completion", context=LlmCallContext(), provider="anthropic", upstream="vertex",
        model_requested="claude-opus-5", model_response=None, usage=None, latency_ms=5,
        status="error", error_type="529", http_status=529,
    )
    assert rec.cost_usd == 0.0 and rec.input_tokens == 0 and rec.status == "error"


def test_span_attributes_carry_cost_and_context():
    ctx = LlmCallContext(workload="ocr", purpose="scan_ocr", job_id="j1")
    rec = build_record(kind="generation", context=ctx, provider="anthropic", upstream="anthropic",
                       model_requested="claude-haiku-4-5", model_response=None, usage=_USAGE,
                       latency_ms=3, status="ok")
    attrs = rec.span_attributes()
    assert attrs["agnes.cost_usd"] == rec.cost_usd
    assert attrs["agnes.workload"] == "ocr" and attrs["agnes.job_id"] == "j1"


def test_usage_from_anthropic_reads_the_cache_fields():
    class _U:
        input_tokens = 10
        output_tokens = 3
        cache_read_input_tokens = 70
        cache_creation_input_tokens = 4

    assert usage_from_anthropic(_U()) == {
        "input_tokens": 10, "output_tokens": 3, "cache_read_tokens": 70, "cache_creation_tokens": 4,
    }
    assert usage_from_anthropic(None) == {
        "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_creation_tokens": 0,
    }


def test_usage_from_openai_splits_cached_prompt_tokens():
    class _Details:
        cached_tokens = 30

    class _U:
        prompt_tokens = 100
        completion_tokens = 9
        prompt_tokens_details = _Details()

    assert usage_from_openai(_U()) == {
        "input_tokens": 70, "output_tokens": 9, "cache_read_tokens": 30, "cache_creation_tokens": 0,
    }

    class _Plain:
        prompt_tokens = 5
        completion_tokens = 1

    assert usage_from_openai(_Plain())["input_tokens"] == 5


def test_priced_as_for_is_stable_and_serialisable():
    p = priced_as_for("claude-opus-5")
    assert p == {
        "price_key": "claude-opus-5", "input_per_mtok": 5.0, "output_per_mtok": 25.0,
        "cache_read_per_mtok": 0.5, "cache_write_per_mtok": 6.25, "batch_multiplier": 1.0,
    }
    assert datetime.now(timezone.utc)  # sanity: tz-aware datetimes are what created_at holds
```

- [ ] **Step 7: Run to verify failure**

Run: `.venv/bin/pytest tests/test_llm_record.py -q` → FAIL (module missing)

- [ ] **Step 8: Implement `src/observability/llm_record.py`**

```python
"""``LlmCallRecord`` — one LLM call, in the one shape both sinks consume.

The chat broker (``app/api/broker.py``) and ``trace_generation``
(``src/observability/llm_tracing.py``) each build one of these per call and
hand it to two sinks: the span (``src/observability/otel.py``) and the
on-instance ledger (``src/observability/llm_ledger.py`` → ``llm_calls``).
Both carry the same ids, so a row and a span describe the same call.

Pricing happens HERE, once, at write time (``src.llm_pricing.cost_usd``),
with the rates stored beside the figure (``priced_as``) so any row can be
re-derived — and an unknown model, priced at the most expensive
general-purpose tier like every other surface, says so
(``priced_as["price_key"] == "default"``) instead of passing as a
measurement.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional
from uuid import uuid4

from src.llm_pricing import BATCH_PRICE_MULTIPLIER, cost_usd, resolve_price, resolve_price_key
from src.observability.llm_context import LlmCallContext

TOKEN_KINDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")


def _int(value: Any) -> int:
    try:
        if value is None or isinstance(value, bool):
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def usage_from_anthropic(usage: Any) -> dict[str, int]:
    """The four token kinds off an Anthropic ``usage`` object (or a dict)."""
    get = (lambda k: usage.get(k)) if isinstance(usage, Mapping) else (lambda k: getattr(usage, k, None))
    if usage is None:
        return {k: 0 for k in TOKEN_KINDS}
    return {
        "input_tokens": _int(get("input_tokens")),
        "output_tokens": _int(get("output_tokens")),
        "cache_read_tokens": _int(get("cache_read_input_tokens")),
        "cache_creation_tokens": _int(get("cache_creation_input_tokens")),
    }


def usage_from_openai(usage: Any) -> dict[str, int]:
    """The four token kinds off an OpenAI-shaped ``usage``. ``prompt_tokens``
    INCLUDES the cached prefix there, so the uncached input is the difference."""
    if usage is None:
        return {k: 0 for k in TOKEN_KINDS}
    get = (lambda o, k: o.get(k)) if isinstance(usage, Mapping) else (lambda o, k: getattr(o, k, None))
    prompt = _int(get(usage, "prompt_tokens"))
    details = get(usage, "prompt_tokens_details")
    cached = _int(get(details, "cached_tokens")) if details is not None else 0
    return {
        "input_tokens": max(prompt - cached, 0),
        "output_tokens": _int(get(usage, "completion_tokens")),
        "cache_read_tokens": cached,
        "cache_creation_tokens": 0,
    }


def priced_as_for(model: Optional[str], *, batch: bool = False) -> dict[str, Any]:
    price = resolve_price(model)
    return {
        "price_key": resolve_price_key(model) or "default",
        "input_per_mtok": price.input_per_mtok,
        "output_per_mtok": price.output_per_mtok,
        "cache_read_per_mtok": round(price.cache_read_per_mtok, 6),
        "cache_write_per_mtok": round(price.cache_write_per_mtok, 6),
        "batch_multiplier": BATCH_PRICE_MULTIPLIER if batch else 1.0,
    }


@dataclass(frozen=True)
class LlmCallRecord:
    id: str
    created_at: datetime
    kind: str
    workload: Optional[str]
    purpose: Optional[str]
    session_id: Optional[str]
    turn_id: Optional[str]
    user_id: Optional[str]
    agent_id: Optional[str]
    job_id: Optional[str]
    subject_id: Optional[str]
    trace_id: Optional[str]
    span_id: Optional[str]
    provider: str
    upstream: str
    model_requested: Optional[str]
    model_response: Optional[str]
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float
    priced_as: dict[str, Any]
    latency_ms: Optional[int]
    status: str
    error_type: Optional[str]
    http_status: Optional[int]
    prompt_chars: Optional[int]
    completion_chars: Optional[int]
    stop_reason: Optional[str]
    stream_complete: Optional[bool]

    def to_row(self) -> dict[str, Any]:
        return asdict(self)

    def context(self) -> LlmCallContext:
        return LlmCallContext(
            workload=self.workload, purpose=self.purpose, session_id=self.session_id, turn_id=self.turn_id,
            user_id=self.user_id, agent_id=self.agent_id, job_id=self.job_id, subject_id=self.subject_id,
        )

    def span_attributes(self) -> dict[str, Any]:
        return {**self.context().span_attributes(), "agnes.cost_usd": self.cost_usd}


def build_record(
    *,
    kind: str,
    context: LlmCallContext,
    provider: str,
    upstream: str,
    model_requested: Optional[str],
    model_response: Optional[str],
    usage: Optional[Mapping[str, Any]],
    latency_ms: Optional[int],
    status: str,
    error_type: Optional[str] = None,
    http_status: Optional[int] = None,
    prompt_chars: Optional[int] = None,
    completion_chars: Optional[int] = None,
    stop_reason: Optional[str] = None,
    stream_complete: Optional[bool] = None,
    trace_id: Optional[str] = None,
    span_id: Optional[str] = None,
    batch: bool = False,
    created_at: Optional[datetime] = None,
) -> LlmCallRecord:
    tokens = {k: _int((usage or {}).get(k)) for k in TOKEN_KINDS}
    priced_model = model_response or model_requested
    cost = round(cost_usd(model=priced_model, batch=batch, **tokens), 6)
    return LlmCallRecord(
        id=str(uuid4()),
        created_at=created_at or datetime.now(timezone.utc),
        kind=kind,
        workload=context.workload,
        purpose=context.purpose,
        session_id=context.session_id,
        turn_id=context.turn_id,
        user_id=context.user_id,
        agent_id=context.agent_id,
        job_id=context.job_id,
        subject_id=context.subject_id,
        trace_id=trace_id,
        span_id=span_id,
        provider=provider,
        upstream=upstream,
        model_requested=model_requested,
        model_response=model_response,
        cost_usd=cost,
        priced_as=priced_as_for(priced_model, batch=batch),
        latency_ms=latency_ms,
        status=status,
        error_type=error_type,
        http_status=http_status,
        prompt_chars=prompt_chars,
        completion_chars=completion_chars,
        stop_reason=stop_reason,
        stream_complete=stream_complete,
        **tokens,
    )


__all__ = ["LlmCallRecord", "TOKEN_KINDS", "build_record", "priced_as_for", "usage_from_anthropic", "usage_from_openai"]
```

- [ ] **Step 9: Run to verify pass**

Run: `.venv/bin/pytest tests/test_llm_record.py -q` → PASS

- [ ] **Step 10: Implement `src/observability/llm_ledger.py` with its test**

Test (append to `tests/test_llm_record.py`):

```python
def test_record_call_is_a_silent_noop_on_duckdb(monkeypatch):
    from src.observability import llm_ledger

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AGNES_DB_URL", raising=False)
    rec = build_record(kind="generation", context=LlmCallContext(), provider="anthropic", upstream="anthropic",
                       model_requested="m", model_response=None, usage=_USAGE, latency_ms=1, status="ok")
    llm_ledger.record_call(rec)  # no repo, no backend: never raises


def test_record_call_inserts_one_row_when_the_repo_exists(monkeypatch):
    import src.repositories as repos
    from src.observability import llm_ledger

    captured: list[list[dict]] = []

    class _Repo:
        def insert_batch(self, rows):
            captured.append([dict(r) for r in rows])
            return len(captured[-1])

    monkeypatch.setattr(repos, "use_pg", lambda: True)
    monkeypatch.setattr(repos, "llm_calls_repo", lambda: _Repo(), raising=False)
    rec = build_record(kind="generation", context=LlmCallContext(purpose="p"), provider="anthropic",
                       upstream="anthropic", model_requested="m", model_response=None, usage=_USAGE,
                       latency_ms=1, status="ok")
    llm_ledger.record_call(rec)
    assert captured == [[rec.to_row()]]


def test_record_call_swallows_a_failing_repo(monkeypatch):
    import src.repositories as repos
    from src.observability import llm_ledger

    class _Boom:
        def insert_batch(self, rows):
            raise RuntimeError("db down")

    monkeypatch.setattr(repos, "use_pg", lambda: True)
    monkeypatch.setattr(repos, "llm_calls_repo", lambda: _Boom(), raising=False)
    rec = build_record(kind="generation", context=LlmCallContext(), provider="anthropic", upstream="anthropic",
                       model_requested="m", model_response=None, usage=None, latency_ms=1, status="ok")
    llm_ledger.record_call(rec)  # logged at debug, never raised
```

Implementation:

```python
"""The ledger sink — one ``llm_calls`` row per :class:`LlmCallRecord`.

Postgres-only by construction (A3): the table has no DuckDB sibling, so on a
DuckDB-backed instance this is a silent no-op. A measurement never costs
the call it measures: every failure is logged at DEBUG and swallowed.
"""

from __future__ import annotations

import logging

from src.observability.llm_record import LlmCallRecord

logger = logging.getLogger(__name__)


def ledger_available() -> bool:
    try:
        from src.repositories import use_pg

        return bool(use_pg())
    except Exception:  # noqa: BLE001
        return False


def record_call(record: LlmCallRecord) -> None:
    if not ledger_available():
        return
    try:
        import src.repositories as repos

        repos.llm_calls_repo().insert_batch([record.to_row()])
    except Exception:  # noqa: BLE001 - see the module docstring
        logger.debug("llm ledger: could not record call %s", record.id, exc_info=True)


__all__ = ["ledger_available", "record_call"]
```

Run: `.venv/bin/pytest tests/test_llm_record.py -q` → PASS

- [ ] **Step 11: `otel.py` — identity minimisation, context attributes, cost, cache tokens, parent context, span ids**

Edits in `src/observability/otel.py`:

1. Import the context type: `from src.observability.llm_context import LlmCallContext` (top-level; the module has no OTel dependency).
2. `start_completion_span`: remove `user_email` from the signature and from `attrs`; add `context: Optional[LlmCallContext] = None, parent_context: Any = None`; merge `**(context.span_attributes() if context else {})` into `attrs` BEFORE `_clean` (explicit `session_id`/`user_id`/`agent_id` arguments win over the context's when both are given); call `_open_span(name, attrs, parent_context=parent_context)`.
3. `_open_span(name, attrs, *, kind=None, parent_context=None)`: `tracer().start_span(name, kind=kind or SpanKind.CLIENT, attributes=dict(attrs), context=parent_context)`.
4. `start_generation_span(*, provider, model, context=None)`: merge `context.span_attributes()` into attrs.
5. `end_generation_span(...)`: add keyword-only `cache_read_tokens=None, cache_creation_tokens=None, cost_usd=None`; extend the attribute tuple with `("gen_ai.usage.cache_read_input_tokens", cache_read_tokens)`, `("gen_ai.usage.cache_creation_input_tokens", cache_creation_tokens)`; after the loop: `if isinstance(cost_usd, (int, float)) and not isinstance(cost_usd, bool): span.set_attribute("agnes.cost_usd", float(cost_usd))`.
6. `end_completion_span(...)`: add keyword-only `cost_usd: Optional[float] = None`; set `agnes.cost_usd` the same way, right after `set_usage_attributes(span, usage)`.
7. New functions (append after `end_completion_span`):

```python
def span_ids(span: Any) -> tuple[Optional[str], Optional[str]]:
    """``(trace_id, span_id)`` as lowercase hex for a recording span — the
    ids a ledger row stores so it can be joined to the exported span.
    ``(None, None)`` for a non-recording span (export off) or any failure."""
    try:
        if not span.is_recording():
            return None, None
        sc = span.get_span_context()
        return format(sc.trace_id, "032x"), format(sc.span_id, "016x")
    except Exception:  # noqa: BLE001
        return None, None


def remote_parent_context(trace_id_hex: Optional[str], span_id_hex: Optional[str]) -> Any:
    """An OTel ``Context`` whose current span is a remote, sampled
    ``NonRecordingSpan`` — the parent a completion span opens under when the
    turn span lives in another process (spec 3.2). ``None`` on bad input."""
    if not _OTEL_API or not trace_id_hex or not span_id_hex:
        return None
    try:
        from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

        sc = SpanContext(
            trace_id=int(trace_id_hex, 16),
            span_id=int(span_id_hex, 16),
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
        return trace.set_span_in_context(NonRecordingSpan(sc))
    except Exception:  # noqa: BLE001
        return None
```

8. Add `"span_ids"`, `"remote_parent_context"` to `__all__`. Update the module docstring: identity = `agnes.user_id`, never the email.

- [ ] **Step 12: `llm_tracing.py` — purpose, four token kinds, pricing, record emission, `record_generation`**

Replace `_Capture` and `trace_generation` with:

```python
from src.observability.llm_context import current_llm_context
from src.observability.llm_ledger import record_call
from src.observability.llm_record import build_record, usage_from_anthropic, usage_from_openai


class _Capture:
    def __init__(self) -> None:
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self.cache_read_tokens: int | None = None
        self.cache_creation_tokens: int | None = None
        self.prompt_chars: int | None = None
        self.completion_chars: int | None = None
        self.model_response: str | None = None
        self.stop_reason: str | None = None
        self.extra: dict[str, Any] = {}

    # _size / set_input / set_output unchanged

    def set_tokens(self, input_tokens, output_tokens, *, cache_read_tokens=None, cache_creation_tokens=None) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_tokens = cache_read_tokens
        self.cache_creation_tokens = cache_creation_tokens

    def set_output_from_anthropic(self, response: Any) -> None:
        try:
            usage = getattr(response, "usage", None)
            if usage is not None:
                u = usage_from_anthropic(usage)
                self.input_tokens, self.output_tokens = u["input_tokens"], u["output_tokens"]
                self.cache_read_tokens, self.cache_creation_tokens = u["cache_read_tokens"], u["cache_creation_tokens"]
            self.model_response = getattr(response, "model", None) or None
            self.stop_reason = getattr(response, "stop_reason", None) or None
            texts = [...]  # unchanged
            if texts:
                self.completion_chars = sum(len(t) for t in texts)
        except Exception:  # noqa: BLE001
            logger.debug("llm tracing: unreadable anthropic response shape", exc_info=True)

    def set_output_from_openai(self, response: Any) -> None:
        try:
            usage = getattr(response, "usage", None)
            if usage is not None:
                u = usage_from_openai(usage)
                self.input_tokens, self.output_tokens = u["input_tokens"], u["output_tokens"]
                self.cache_read_tokens, self.cache_creation_tokens = u["cache_read_tokens"], u["cache_creation_tokens"]
            self.model_response = getattr(response, "model", None) or None
            choices = getattr(response, "choices", None) or []
            if choices:
                message = getattr(choices[0], "message", None)
                self.completion_chars = self._size(getattr(message, "content", None))
                self.stop_reason = getattr(choices[0], "finish_reason", None) or None
        except Exception:  # noqa: BLE001
            logger.debug("llm tracing: unreadable openai response shape", exc_info=True)

    def usage(self) -> dict[str, int]:
        return {
            "input_tokens": int(self.input_tokens or 0),
            "output_tokens": int(self.output_tokens or 0),
            "cache_read_tokens": int(self.cache_read_tokens or 0),
            "cache_creation_tokens": int(self.cache_creation_tokens or 0),
        }
```

```python
@contextmanager
def trace_generation(
    *,
    provider: str,
    model: str,
    distinct_id: str | None = None,
    purpose: str | None = None,
    subject_id: str | None = None,
    batch: bool = False,
) -> Iterator[_Capture]:
    """Time one LLM call and emit its record to the log, the span and the
    ledger. Re-raises whatever the call raises."""
    capture = _Capture()
    context = current_llm_context().merged(purpose=purpose, user_id=distinct_id, subject_id=subject_id)
    started = time.monotonic()
    error_type: str | None = None
    span = _otel.start_generation_span(provider=provider, model=model, context=context)
    try:
        yield capture
    except BaseException as exc:
        error_type = type(exc).__name__
        raise
    finally:
        latency_ms = int((time.monotonic() - started) * 1000)
        trace_id, span_id = _otel.span_ids(span)
        try:
            record = build_record(
                kind="generation", context=context, provider=provider, upstream=provider,
                model_requested=model, model_response=capture.model_response, usage=capture.usage(),
                latency_ms=latency_ms, status="error" if error_type else "ok", error_type=error_type,
                prompt_chars=capture.prompt_chars, completion_chars=capture.completion_chars,
                stop_reason=capture.stop_reason, trace_id=trace_id, span_id=span_id, batch=batch,
            )
        except Exception:  # noqa: BLE001 - instrumentation never fails the call
            logger.debug("llm tracing: could not build the call record", exc_info=True)
            record = None
        fields: dict[str, Any] = {
            "event": "llm_generation",
            "provider": provider,
            "model": model,
            "latency_ms": latency_ms,
            "input_tokens": capture.input_tokens,
            "output_tokens": capture.output_tokens,
            "cache_read_tokens": capture.cache_read_tokens,
            "cache_creation_tokens": capture.cache_creation_tokens,
            "prompt_chars": capture.prompt_chars,
            "completion_chars": capture.completion_chars,
            "is_error": error_type is not None,
            "workload": context.workload,
            "purpose": context.purpose,
            "cost_usd": record.cost_usd if record else None,
            **capture.extra,
        }
        if error_type is not None:
            fields["error_type"] = error_type
        if context.user_id:
            fields["user_id"] = context.user_id
        try:
            logger.info("llm generation", extra=fields)
        except Exception:  # noqa: BLE001
            logger.debug("llm tracing: could not emit the generation record", exc_info=True)
        _otel.end_generation_span(
            span,
            input_tokens=capture.input_tokens,
            output_tokens=capture.output_tokens,
            cache_read_tokens=capture.cache_read_tokens,
            cache_creation_tokens=capture.cache_creation_tokens,
            cost_usd=record.cost_usd if record else None,
            prompt_chars=capture.prompt_chars,
            completion_chars=capture.completion_chars,
            error_type=error_type,
            user_id=context.user_id,
        )
        if record is not None:
            record_call(record)


def record_generation(
    *,
    provider: str,
    model: str,
    purpose: str,
    usage: Any,
    latency_ms: int | None = None,
    prompt_chars: int | None = None,
    completion_chars: int | None = None,
    subject_id: str | None = None,
    batch: bool = False,
    error_type: str | None = None,
    model_response: str | None = None,
    stop_reason: str | None = None,
) -> None:
    """Emit the record for a generation that was NOT timed in this process —
    a Batches-API result collected later. ``usage`` is an Anthropic usage
    object or an already-normalized dict. Never raises."""
    try:
        with trace_generation(provider=provider, model=model, purpose=purpose, subject_id=subject_id, batch=batch) as cap:
            u = usage if isinstance(usage, dict) and "cache_read_tokens" in usage else usage_from_anthropic(usage)
            cap.set_tokens(u["input_tokens"], u["output_tokens"],
                           cache_read_tokens=u["cache_read_tokens"], cache_creation_tokens=u["cache_creation_tokens"])
            cap.prompt_chars, cap.completion_chars = prompt_chars, completion_chars
            cap.model_response, cap.stop_reason = model_response, stop_reason
            if latency_ms is not None:
                cap.extra["reported_latency_ms"] = latency_ms
            if error_type:
                raise _RecordedError(error_type)
    except _RecordedError:
        pass
    except Exception:  # noqa: BLE001
        logger.debug("llm tracing: record_generation failed", exc_info=True)


class _RecordedError(Exception):
    """Carries a batch result's error type through ``trace_generation`` so the
    record is marked ``error`` without a live exception."""
```

(When `_RecordedError` propagates, `trace_generation` records `error_type="_RecordedError"` — set the real name instead: in the `except BaseException` branch use `getattr(exc, "recorded_type", None) or type(exc).__name__`, and give `_RecordedError.__init__` a `recorded_type` attribute.)

Update `src/observability/__init__.py` to also export `record_generation`, `llm_context`, `current_llm_context`, `LlmCallContext`.

Tests to add to `tests/test_llm_tracing.py`:

```python
def test_cache_tokens_and_cost_are_read_off_an_anthropic_response(caplog):
    class _Usage:
        input_tokens = 1000
        output_tokens = 100
        cache_read_input_tokens = 5000
        cache_creation_input_tokens = 200

    class _Response:
        usage = _Usage()
        model = "claude-sonnet-5-20260101"
        stop_reason = "end_turn"
        content = [type("Block", (), {"type": "text", "text": "hi"})()]

    with caplog.at_level(logging.INFO):
        with trace_generation(provider="anthropic", model="claude-sonnet-5", purpose="unit") as trace:
            trace.set_output_from_anthropic(_Response())

    (record,) = _records(caplog)
    assert record.cache_read_tokens == 5000 and record.cache_creation_tokens == 200
    assert record.purpose == "unit"
    from src.llm_pricing import cost_usd
    assert record.cost_usd == round(cost_usd(model="claude-sonnet-5-20260101", input_tokens=1000,
                                             output_tokens=100, cache_read_tokens=5000, cache_creation_tokens=200), 6)


def test_openai_cached_prompt_tokens_are_split_out(caplog):
    class _Details:
        cached_tokens = 40

    class _Usage:
        prompt_tokens = 100
        completion_tokens = 9
        prompt_tokens_details = _Details()

    class _Response:
        usage = _Usage()
        model = "gpt-x"
        choices: list = []

    with caplog.at_level(logging.INFO):
        with trace_generation(provider="openai_compat", model="gpt-x") as trace:
            trace.set_output_from_openai(_Response())

    (record,) = _records(caplog)
    assert (record.input_tokens, record.cache_read_tokens) == (60, 40)


def test_purpose_and_workload_come_from_the_context_when_not_given(caplog):
    from src.observability.llm_context import llm_context

    with caplog.at_level(logging.INFO):
        with llm_context(workload="builder", purpose="entity_builder_turn", user_id="u9"):
            with trace_generation(provider="anthropic", model="m"):
                pass

    (record,) = _records(caplog)
    assert (record.workload, record.purpose, record.user_id) == ("builder", "entity_builder_turn", "u9")


def test_the_record_reaches_the_ledger(monkeypatch, caplog):
    import src.repositories as repos

    rows: list[dict] = []

    class _Repo:
        def insert_batch(self, batch):
            rows.extend(dict(r) for r in batch)
            return len(batch)

    monkeypatch.setattr(repos, "use_pg", lambda: True)
    monkeypatch.setattr(repos, "llm_calls_repo", lambda: _Repo(), raising=False)
    with trace_generation(provider="anthropic", model="claude-haiku-4-5", purpose="p") as trace:
        trace.set_tokens(10, 2)
    (row,) = rows
    assert row["kind"] == "generation" and row["purpose"] == "p" and row["input_tokens"] == 10
    assert row["trace_id"] is None  # export off: no span ids, row still written


def test_record_generation_marks_a_batch_result(caplog):
    from src.observability.llm_tracing import record_generation

    class _Usage:
        input_tokens = 10
        output_tokens = 5
        cache_read_input_tokens = 0
        cache_creation_input_tokens = 0

    with caplog.at_level(logging.INFO):
        record_generation(provider="anthropic", model="claude-haiku-4-5", purpose="facts_batch",
                          usage=_Usage(), subject_id="file_1", batch=True)
    (record,) = _records(caplog)
    assert record.purpose == "facts_batch" and record.input_tokens == 10 and record.is_error is False
```

Tests to add/adjust in `tests/test_otel_export.py`:
- `test_trace_generation_emits_a_span`: also assert `attrs["gen_ai.usage.cache_read_input_tokens"]` / `cache_creation_input_tokens` when `cap.set_tokens(10, 5, cache_read_tokens=7, cache_creation_tokens=1)` and `attrs["agnes.cost_usd"] > 0`, `attrs["agnes.purpose"]` when `purpose="unit"` is passed.
- New `test_span_ids_and_remote_parent_context(otel_exporter)`: open a completion span, `tid, sid = otel.span_ids(span)`; assert 32/16 hex chars; `ctx = otel.remote_parent_context(tid, sid)`; open a second span with `start_completion_span(..., parent_context=ctx)`; end both; the second finished span's `parent.span_id == int(sid, 16)` and `context.trace_id == int(tid, 16)`. Also `otel.span_ids(otel._NoopSpan()) == (None, None)` and `otel.remote_parent_context("zz", "yy") is None`.
- Grep the file for `user_email` and remove any assertion expecting `agnes.user_email` (Task 2 asserts absence on the broker path).

Run: `.venv/bin/pytest tests/test_llm_tracing.py tests/test_otel_export.py -q -k "not broker"` → PASS

- [ ] **Step 13: `UsageAccumulator.add_call` + flush**

In `app/api/broker_agent_policy.py`:

```python
from src.repositories import RequiresPostgresBackend, llm_usage_repo, use_pg
```

In `UsageAccumulator.__init__`: `self._call_rows: List[Dict[str, Any]] = []`.

```python
    def add_call(self, row: Dict[str, Any]) -> None:
        """Buffer one ``llm_calls`` ledger row (spec 3.1) — the per-call record
        the broker builds beside the ``llm_usage`` budget row. Same flush
        cadence, same shutdown flush. Skipped outright on the frozen DuckDB
        app-state backend: the table is Postgres-only."""
        if not use_pg():
            return
        with self._lock:
            self._call_rows.append(row)
        self.maybe_flush()
```

`maybe_flush` counts `len(self._rows) + len(self._call_rows)` for the size threshold and `bool(self._rows or self._call_rows)` for age. `flush()`:

```python
        with self._lock:
            rows, self._rows = self._rows, []
            call_rows, self._call_rows = self._call_rows, []
            self._last_flush = self._clock()
        if rows:
            try:
                llm_usage_repo().insert_batch(rows)
            except Exception:
                logger.exception("llm_usage batch flush failed; %d usage rows dropped", len(rows))
        if call_rows:
            try:
                import src.repositories as repos

                repos.llm_calls_repo().insert_batch(call_rows)
            except (ImportError, AttributeError, RequiresPostgresBackend):
                logger.debug("llm_calls ledger unavailable; %d call rows dropped", len(call_rows))
            except Exception:
                logger.exception("llm_calls batch flush failed; %d call rows dropped", len(call_rows))
```

Tests (append to `tests/test_broker_agent_policy.py`):

```python
def test_accumulator_add_call_is_skipped_on_duckdb(monkeypatch):
    monkeypatch.setattr(pol, "use_pg", lambda: False)
    acc = pol.UsageAccumulator(flush_size=1, flush_interval_s=3600)
    acc.add_call({"id": "c1"})
    assert acc._call_rows == []


def test_accumulator_flushes_call_rows_to_the_ledger(monkeypatch):
    import src.repositories as repos

    flushed: list[list[dict]] = []

    class _Repo:
        def insert_batch(self, rows):
            flushed.append(list(rows))
            return len(rows)

    monkeypatch.setattr(pol, "use_pg", lambda: True)
    monkeypatch.setattr(repos, "llm_calls_repo", lambda: _Repo(), raising=False)
    monkeypatch.setattr(pol, "llm_usage_repo", lambda: _Repo())
    acc = pol.UsageAccumulator(flush_size=2, flush_interval_s=3600)
    acc.add_call({"id": "c1"})
    assert flushed == []
    acc.add_call({"id": "c2"})
    assert flushed == [[{"id": "c1"}, {"id": "c2"}]]


def test_accumulator_drops_call_rows_when_the_ledger_is_missing(monkeypatch):
    import src.repositories as repos

    monkeypatch.setattr(pol, "use_pg", lambda: True)
    monkeypatch.delattr(repos, "llm_calls_repo", raising=False)
    acc = pol.UsageAccumulator(flush_size=1, flush_interval_s=3600)
    acc.add_call({"id": "c1"})  # flushes immediately; AttributeError swallowed
    assert acc._call_rows == []
```

Run: `.venv/bin/pytest tests/test_broker_agent_policy.py -q` → PASS

- [ ] **Step 14: Guards + commit**

Run: `.venv/bin/pytest tests/test_llm_context.py tests/test_llm_record.py tests/test_llm_tracing.py tests/test_otel_export.py tests/test_broker_agent_policy.py tests/test_llm_pricing*.py -q`. Fix the two provider modules if their calls to `set_tokens` broke (signature is backward compatible: two positionals). Then:

```bash
git add src/observability/llm_context.py src/observability/llm_record.py src/observability/llm_ledger.py \
  src/observability/otel.py src/observability/llm_tracing.py src/observability/__init__.py src/llm_pricing.py \
  app/api/broker_agent_policy.py tests/test_llm_context.py tests/test_llm_record.py tests/test_llm_tracing.py \
  tests/test_otel_export.py tests/test_broker_agent_policy.py tests/test_llm_pricing*.py
git commit -m "observability: LLM call context, call record, priced ledger sink, cache tokens and cost on spans"
```

---

### Task 2: The broker builds the call record at both usage sites; identity minimisation; no session read on the span path

**Files:**
- Modify: `app/api/broker.py` (`_start_otel_completion_span` ~882–906; the `otel_session`/span-open block ~1040–1056 and ~1313–1328; the SSE `finally` ~1470–1512; the buffered path ~1545–1575)
- Modify: `src/observability/otel.py` (`describe_completion` + `CompletionSummary`; `end_completion_span(summary=...)`)
- Test: `tests/test_otel_export.py` (broker tests), `tests/test_broker_llm_calls.py` (new)

**Interfaces:**
- Consumes (Task 1): `LlmCallContext`, `build_record`, `otel.span_ids`, `otel.start_completion_span(context=...)`, `otel.end_completion_span(cost_usd=...)`, `usage_accumulator.add_call(row)`.
- Produces in `src.observability.otel`:
  - `@dataclass class CompletionSummary: model, stop_reason, prompt_chars, completion_chars, prompt_json, completion_json, stream_complete: bool | None, response_bytes: int | None`
  - `describe_completion(*, request_body: bytes | None, response_body: bytes | None, content_type: str, response_truncated: bool = False) -> CompletionSummary` — pure; the parsing `end_completion_span` used to do inline. Never raises.
  - `end_completion_span(span, *, status_code=None, usage=None, request_body=None, response_body=None, content_type="", error=None, response_truncated=False, cost_usd=None, summary: CompletionSummary | None = None)` — when `summary` is given it is used instead of re-parsing.
- Produces in `app.api.broker`: `_completion_context(row, *, agent_row, caller_user_id) -> LlmCallContext` (in this task: `workload="chat"`, `purpose="completion"`, `session_id=row["session_id"]`, `user_id=caller_user_id`, `agent_id=agent_row["id"] if agent_row else None`; Task 4 refines `workload`/`user_id`/`agent_id`/`turn_id` from the turn record) and `_record_completion(*, context, span, upstream, model_requested, usage, status_code, latency_ms, summary, error=None) -> LlmCallRecord | None` — builds the record (`kind="completion"`, `provider="gcp.vertex_ai" if upstream == "vertex" else "anthropic"`, `model_response=usage.get("model") or summary.model`, `status="error"` when `error` or `status_code >= 400`, `error_type=type(error).__name__` or `str(status_code)`, `http_status`, `prompt_chars`/`completion_chars`/`stop_reason`/`stream_complete` from the summary, trace/span ids via `span_ids(span)` when `span` is not None), hands `record.to_row()` to `usage_accumulator.add_call`, returns the record; never raises (wrapped, DEBUG).

- [ ] **Step 1: Failing broker tests**

`tests/test_broker_llm_calls.py`:

```python
"""Every brokered completion becomes one ``llm_calls`` row and one span that
carry the same ids and the same price (spec 3.1) — for agent-less sessions
too, and with ``agnes.user_email`` gone from the span (spec 3.6)."""

from __future__ import annotations

import json

import pytest

from src.repositories import ticket_repo
from tests.test_otel_export import _FakeUpstream, _post, _sse, otel_broker, otel_exporter  # noqa: F401


@pytest.fixture
def ledger(monkeypatch):
    import src.repositories as repos
    from app.api.broker_agent_policy import usage_accumulator

    rows: list[dict] = []

    class _Repo:
        def insert_batch(self, batch):
            rows.extend(dict(r) for r in batch)
            return len(batch)

    monkeypatch.setattr(repos, "use_pg", lambda: True)
    monkeypatch.setattr(repos, "llm_calls_repo", lambda: _Repo(), raising=False)
    monkeypatch.setattr("app.api.broker_agent_policy.use_pg", lambda: True)
    usage_accumulator.flush()
    yield rows
    usage_accumulator.flush()
    rows.clear()


def _json_body():
    return json.dumps({
        "id": "msg_1", "model": "claude-sonnet-5-20260101", "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "answer"}],
        "usage": {"input_tokens": 1000, "output_tokens": 100,
                  "cache_read_input_tokens": 5000, "cache_creation_input_tokens": 200},
    }).encode()


def test_buffered_completion_writes_a_priced_row_matching_the_span(otel_broker, otel_exporter, ledger):
    from app.api.broker_agent_policy import usage_accumulator
    from src.llm_pricing import cost_usd

    _FakeUpstream.body = _json_body()
    tok = ticket_repo().mint("chat_ledger_json", "main", ttl_seconds=60)
    r = _post(otel_broker, tok, "/api/broker/anthropic/v1/messages",
              {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text
    usage_accumulator.flush()

    (span,) = otel_exporter.get_finished_spans()
    attrs = dict(span.attributes)
    (row,) = ledger
    expected = round(cost_usd(model="claude-sonnet-5-20260101", input_tokens=1000, output_tokens=100,
                              cache_read_tokens=5000, cache_creation_tokens=200), 6)
    assert row["cost_usd"] == expected and attrs["agnes.cost_usd"] == expected
    assert row["trace_id"] == format(span.context.trace_id, "032x")
    assert row["span_id"] == format(span.context.span_id, "016x")
    assert row["kind"] == "completion" and row["session_id"] == "chat_ledger_json"
    assert row["workload"] == "chat" and row["purpose"] == "completion"
    assert row["model_requested"] == "claude-sonnet-5" and row["model_response"] == "claude-sonnet-5-20260101"
    assert row["status"] == "ok" and row["http_status"] == 200 and row["stop_reason"] == "end_turn"
    assert row["priced_as"]["price_key"] == "claude-sonnet-5"
    assert row["agent_id"] is None  # agent-less session: recorded all the same
    assert "agnes.user_email" not in attrs
    assert attrs["agnes.workload"] == "chat" and attrs["agnes.purpose"] == "completion"


def test_streamed_completion_writes_a_row_with_stream_completeness(otel_broker, otel_exporter, ledger):
    from app.api.broker_agent_policy import usage_accumulator

    _FakeUpstream.content_type = "text/event-stream"
    _FakeUpstream.sse_chunks = _sse([
        ("message_start", {"type": "message_start", "message": {"model": "claude-stream",
                                                                  "usage": {"input_tokens": 20, "output_tokens": 1}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": "Hello"}}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                           "usage": {"output_tokens": 4}}),
    ])
    tok = ticket_repo().mint("chat_ledger_sse", "main", ttl_seconds=60)
    r = _post(otel_broker, tok, "/api/broker/anthropic/v1/messages",
              {"model": "claude-stream", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    usage_accumulator.flush()
    (row,) = ledger
    assert row["stream_complete"] is True and row["stop_reason"] == "end_turn"
    assert row["input_tokens"] == 20 and row["output_tokens"] == 4
    assert row["latency_ms"] is not None and row["latency_ms"] >= 0


def test_upstream_error_is_an_error_row(otel_broker, otel_exporter, ledger):
    from app.api.broker_agent_policy import usage_accumulator

    _FakeUpstream.status_code = 529
    _FakeUpstream.body = b'{"error": {"type": "overloaded_error", "message": "busy"}}'
    tok = ticket_repo().mint("chat_ledger_err", "main", ttl_seconds=60)
    r = _post(otel_broker, tok, "/api/broker/anthropic/v1/messages",
              {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 529
    usage_accumulator.flush()
    (row,) = ledger
    assert row["status"] == "error" and row["http_status"] == 529 and row["error_type"] == "529"
    assert row["cost_usd"] == 0.0


def test_ledger_rows_are_written_even_when_export_is_off(e2e_env, shared_app, monkeypatch, ledger):
    from src.observability import otel
    from app.api.broker_agent_policy import usage_accumulator
    import app.api.broker as broker_mod

    otel.shutdown_otel()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _FakeUpstream)
    _FakeUpstream.status_code = 200
    _FakeUpstream.content_type = "application/json"
    _FakeUpstream.body = _json_body()
    _FakeUpstream.sse_chunks = []
    tok = ticket_repo().mint("chat_ledger_noexport", "main", ttl_seconds=60)
    r = _post(shared_app, tok, "/api/broker/anthropic/v1/messages",
              {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    usage_accumulator.flush()
    (row,) = ledger
    assert row["trace_id"] is None and row["span_id"] is None and row["cost_usd"] > 0
```

Also in `tests/test_otel_export.py::test_broker_streamed_completion_with_content_and_identity` replace any `agnes.user_email` assertion with `assert "agnes.user_email" not in attrs` (that test creates a real session with no agent bound, so `caller_user_id` is None today — assert only the absence of the email).

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_broker_llm_calls.py -q` → FAIL (no rows / `user_email` present)

- [ ] **Step 3: `describe_completion` in `otel.py`**

Move the parsing out of `end_completion_span` into:

```python
@dataclass
class CompletionSummary:
    model: Optional[str] = None
    stop_reason: Optional[str] = None
    prompt_chars: Optional[int] = None
    completion_chars: Optional[int] = None
    prompt_json: Optional[str] = None
    completion_json: Optional[str] = None
    stream_complete: Optional[bool] = None
    response_bytes: Optional[int] = None


def describe_completion(*, request_body, response_body, content_type, response_truncated=False) -> CompletionSummary:
    out = CompletionSummary()
    try:
        if response_body is not None:
            out.response_bytes = len(response_body)
        if response_body is not None and not response_truncated:
            summary = summarize_completion(response_body, content_type)
            out.model = summary.get("model")
            out.stop_reason = summary.get("stop_reason")
            if "text/event-stream" in (content_type or "").lower():
                out.stream_complete = bool(summary.get("stop_reason"))
            if summary.get("blocks") is not None:
                out.completion_json = json.dumps(
                    [{"role": "assistant", "parts": _parts_from_content(summary["blocks"])}], ensure_ascii=False
                )
                out.completion_chars = len(out.completion_json)
        if request_body is not None:
            out.prompt_json = json.dumps(input_messages_from_request(request_body), ensure_ascii=False)
            out.prompt_chars = len(out.prompt_json)
    except Exception:  # noqa: BLE001
        logger.debug("otel: could not describe the completion", exc_info=True)
    return out
```

`end_completion_span` then computes `summary = summary or describe_completion(...)` (after the `is_recording()` early return — a non-recording span must stay free) and sets the same attributes from it (`agnes.response_bytes`, `agnes.stream_complete`, `gen_ai.response.model` fallback, `gen_ai.response.finish_reasons`, `agnes.prompt_chars`, `agnes.completion_chars`); the content-event block reads `summary.prompt_json` / `summary.completion_json`. Existing `tests/test_otel_export.py` content tests must stay green.

- [ ] **Step 4: Broker changes**

1. `_start_otel_completion_span`: drop the `session` parameter and the `chat_session_repo().get_session` read; add `context: LlmCallContext` and `parent_context: Any = None` parameters; call `_otel.start_completion_span(upstream=..., model=..., stream=..., session_id=row.get("session_id"), ticket_scope=row.get("scope"), user_id=context.user_id, agent_id=context.agent_id, context=context, parent_context=parent_context)`. The `otel_session` read at ~1051 stays ONLY for the `llm`-scope `ticket_session_gone` check — stop passing it to the span opener; rename the variable to `llm_scope_session` so the name no longer claims a tracing purpose.
2. Add near `_completion_request_hints`:

```python
def _completion_context(row, *, agent_row, caller_user_id) -> LlmCallContext:
    return LlmCallContext(
        workload="chat",
        purpose="completion",
        session_id=row.get("session_id"),
        user_id=caller_user_id,
        agent_id=agent_row.get("id") if agent_row else None,
    )


def _record_completion(*, context, span, upstream, model_requested, usage, status_code, latency_ms, summary, error=None):
    try:
        trace_id, span_id = _otel.span_ids(span) if span is not None else (None, None)
        failed = error is not None or (status_code is not None and status_code >= 400)
        record = build_record(
            kind="completion", context=context,
            provider="gcp.vertex_ai" if upstream == "vertex" else "anthropic", upstream=upstream,
            model_requested=model_requested, model_response=(usage or {}).get("model") or summary.model,
            usage=usage, latency_ms=latency_ms, status="error" if failed else "ok",
            error_type=(type(error).__name__ if error is not None else (str(status_code) if failed else None)),
            http_status=status_code, prompt_chars=summary.prompt_chars, completion_chars=summary.completion_chars,
            stop_reason=summary.stop_reason, stream_complete=summary.stream_complete,
            trace_id=trace_id, span_id=span_id,
        )
        usage_accumulator.add_call(record.to_row())
        return record
    except Exception:  # noqa: BLE001 - a measurement never costs a forward
        logger.debug("broker: could not record the completion", exc_info=True)
        return None
```

3. Where `otel_span` is opened (~1313): compute `completion_context = _completion_context(row, agent_row=agent_row, caller_user_id=caller_user_id)` for every `is_completion` request (not only when export is on), hoist `upstream_label = "dispatcher" if use_dispatcher else ("vertex" if vertex_mode else "anthropic")` and `requested_model, _ = _completion_request_hints(raw_body, vertex_target)` into locals, set `forward_started = time.monotonic()` just before the upstream call, and pass `context=completion_context` to `_start_otel_completion_span`.
4. SSE `finally` (~1504): compute `usage` ONCE (`None if overflow else parse_usage(...)`), `summary = _otel.describe_completion(request_body=raw_body, response_body=bytes(collected), content_type=ctype, response_truncated=state["overflow"])`, then `record = _record_completion(context=completion_context, span=otel_span, upstream=upstream_label, model_requested=requested_model, usage=usage, status_code=resp.status_code, latency_ms=int((time.monotonic() - forward_started) * 1000), summary=summary)` when `is_completion`, then `if otel_span is not None: _otel.end_completion_span(otel_span, status_code=..., usage=usage, request_body=raw_body, response_body=bytes(collected), content_type=ctype, response_truncated=state["overflow"], summary=summary, cost_usd=record.cost_usd if record else None)`.
5. Buffered path (~1566): same sequence with `resp.content`; `usage = parse_usage(...) if resp.status_code == 200 else None`.
6. The two early `end_completion_span(otel_span, error=_exc)` sites (~1393, ~1413 — upstream exceptions) also record: `_record_completion(context=completion_context, span=otel_span, upstream=upstream_label, model_requested=requested_model, usage=None, status_code=None, latency_ms=..., summary=_otel.CompletionSummary(), error=_exc)` when `is_completion`.

- [ ] **Step 5: Run to verify pass**

Run: `.venv/bin/pytest tests/test_broker_llm_calls.py tests/test_otel_export.py tests/test_broker_otlp.py -q` → PASS. Also `.venv/bin/pytest tests/test_broker_routes.py tests/test_broker_agent_policy.py -q` → PASS.

- [ ] **Step 6: Commit**

```bash
git add app/api/broker.py src/observability/otel.py tests/test_broker_llm_calls.py tests/test_otel_export.py
git commit -m "broker: one priced call record per completion, user_email off the span, no session read on the span path"
```

---

### Task 3: Coverage — every call site traced, worker job id bound, builders labelled, static guard

**Files:**
- Modify: `connectors/sharepoint/facts_extraction.py` (`_create` ~2462, `_sync_retry` ~4868, `_collect_batch` ~4966 where a `succeeded` result's `result.message.usage` is folded)
- Modify: `src/ingest/scan_ocr.py` (~1126, ~1519), `src/ingest/vision.py` (~98), `src/anonymization_ner.py` (~1010/~1020 — wrap the whole try/except block once), `app/chat/auto_title.py` (~341), `app/chat/readiness.py` (~266, ~314, ~363)
- Modify: `app/api/entity_builder.py` (~408–421), `app/api/agent_builder.py` (~458–475), `app/api/mcp_builder.py` (~350–364), `app/api/package_builder.py` (~375–389), `app/api/semantic_model_builder.py` (~615–629) — each `_llm_turn` helper gains `*, user_id: str | None = None, subject_id: str | None = None` and wraps `extract_json` in `llm_context(workload="builder", purpose="<kind>_builder_turn", user_id=user_id, subject_id=subject_id)`; each route passes `user_id=user["id"]` and the entity/agent/package/model id when the route has one (`agent_id` path param for agents; a draft's `id` field when the payload carries one; else `None`).
- Modify: `services/corporate_memory/collector.py` (`check_sensitivity` → `purpose="sensitivity_check"`; the catalog refresh → `purpose="catalog_refresh"`; both `workload="corporate_memory"`), `services/corporate_memory/tagger.py` (`tagger`), `services/corporate_memory/contradiction.py` (`contradiction`), `src/knowledge_digests.py` (`workload="knowledge"`, `purpose="digest"`), `src/table_autodoc.py` (`semantic_layer`/`table_autodoc`), `app/api/ontology.py` (`semantic_layer`/`ontology_draft`, `user_id` = caller), `app/services/memory_curator_profile.py` (`corporate_memory`/`curator_profile`), `services/session_processors/verification.py` (`verification`/`session_verification`), `services/verification_detector/detector.py` (`verification`/`detector`), `src/store_guardrails/craft_review.py` (`store_guardrails`/`craft_review`), `src/store_guardrails/llm_review.py` (`store_guardrails`/`llm_review`) — each: `with llm_context(workload=..., purpose=...):` around the `extract_json` call (the provider's own `trace_generation` picks it up).
- Modify: `app/worker/runtime.py` (~655 and ~773)
- Modify: `src/observability/llm_tracing.py` (add `provider_label(provider) -> str`)
- Create: `tests/test_llm_coverage_guard.py`, `tests/test_worker_llm_context.py`
- Test: the two new files plus the touched modules' existing tests (`git grep -l "auto_title\|readiness\|scan_ocr\|vision\|anonymization_ner\|facts_extraction\|entity_builder\|agent_builder\|mcp_builder\|package_builder\|semantic_model_builder\|knowledge_digests\|table_autodoc\|corporate_memory\|store_guardrails\|verification_detector" tests connectors | grep test_` — run whichever exist)

**Interfaces:**
- Consumes (Task 1): `trace_generation(provider, model, purpose=, subject_id=)`, `record_generation(...)`, `llm_context(...)`, `bind_llm_context(job_id=)`, `unbind_llm_context(token)`.
- Produces: `src.observability.llm_tracing.provider_label(provider: str) -> str` (`"gcp.vertex_ai"` for `"vertex"`, else `"anthropic"`); `tests/test_llm_coverage_guard.py::COVERED_MODULES` — the list later tasks extend.

- [ ] **Step 1: Write the static coverage guard first (it fails today)**

`tests/test_llm_coverage_guard.py`:

```python
"""Every ``messages.create(`` in the modules the design names (spec 3.3) sits
inside ``trace_generation`` — a static scan, so a new bypass fails CI."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

COVERED_MODULES = [
    "connectors/sharepoint/facts_extraction.py",
    "src/ingest/scan_ocr.py",
    "src/ingest/vision.py",
    "src/anonymization_ner.py",
    "app/chat/auto_title.py",
    "app/chat/readiness.py",
    "connectors/llm/anthropic_provider.py",
]


def _is_messages_create(call: ast.Call) -> bool:
    f = call.func
    return (
        isinstance(f, ast.Attribute) and f.attr == "create"
        and isinstance(f.value, ast.Attribute) and f.value.attr == "messages"
    )


def _with_calls_trace_generation(node: ast.With) -> bool:
    for item in node.items:
        ctx = item.context_expr
        if isinstance(ctx, ast.Call):
            name = ctx.func.attr if isinstance(ctx.func, ast.Attribute) else getattr(ctx.func, "id", "")
            if name == "trace_generation":
                return True
    return False


def _uncovered(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    withs = [n for n in ast.walk(tree) if isinstance(n, ast.With) and _with_calls_trace_generation(n)]
    missing = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_messages_create(node):
            if not any(w.lineno <= node.lineno <= (w.end_lineno or w.lineno) for w in withs):
                missing.append(node.lineno)
    return missing


@pytest.mark.parametrize("module", COVERED_MODULES)
def test_every_messages_create_is_traced(module):
    missing = _uncovered(REPO / module)
    assert not missing, f"{module}: messages.create( at line(s) {missing} is not inside trace_generation(...)"


def test_the_guard_sees_a_bypass(tmp_path):
    src = "def f(client):\n    return client.messages.create(model='m')\n"
    p = tmp_path / "m.py"
    p.write_text(src)
    assert _uncovered(p) == [2]
    covered = (
        "from src.observability import trace_generation\n"
        "def f(client):\n    with trace_generation(provider='anthropic', model='m') as cap:\n"
        "        return client.messages.create(model='m')\n"
    )
    p.write_text(covered)
    assert _uncovered(p) == []
```

Run: `.venv/bin/pytest tests/test_llm_coverage_guard.py -q` → FAIL on six modules.

- [ ] **Step 2: Wrap each call site**

Pattern (facts extractor `_create`; provider is `self.provider`):

```python
    def _create(self, user_message: str) -> Any:
        from src.observability import trace_generation
        from src.observability.llm_tracing import provider_label

        client, model = self._ensure_client()
        with trace_generation(provider=provider_label(self.provider), model=model, purpose="facts_extraction") as cap:
            cap.set_input(user_message)
            response = client.messages.create(...)  # unchanged kwargs
            cap.set_output_from_anthropic(response)
        return response
```

`_sync_retry` → `purpose="facts_retry"`. Batch results: in `_collect_batch`, where a `succeeded` result's usage is recorded (`_record_usage("batch", ...)`), add `record_generation(provider=provider_label(provider), model=resolved_model, purpose="facts_batch", usage=getattr(result.message, "usage", None), subject_id=file_id, batch=True, model_response=getattr(result.message, "model", None), stop_reason=getattr(result.message, "stop_reason", None))` — one record per document result. The extraction entry point (the function the worker's `corpus-extraction` job calls for facts, or `run_facts_extraction`'s body) wraps its body in `with llm_context(workload="extraction"):` so `workload` is set for every record.

OCR: `purpose="scan_ocr"` (page transcription) and `"scan_ocr_retry"` (the triage call; keep the outer try/except — the `with` goes INSIDE the try). Vision: `with llm_context(workload="vision"): with trace_generation(..., purpose="image_caption")`. NER: `llm_context(workload="anonymization")` + `purpose="ner_detect"` around the whole try/retry block (both `create` calls inside one `with`). Auto-title: `llm_context(workload="auto_title", subject_id=<chat id — thread it into the sync helper as a keyword>)` + `purpose="auto_title"`. Readiness: three probes, `llm_context(workload="readiness")` + `purpose="probe"`. OCR entry: `llm_context(workload="ocr")` at the method that loops a document's pages.

- [ ] **Step 3: Worker binds `job_id`**

`app/worker/runtime.py` next to `rid_token = bind_request_id(job.get("payload_json"))`:

```python
    from src.observability.llm_context import bind_llm_context, unbind_llm_context

    llm_token = bind_llm_context(job_id=str(job.get("id") or "") or None)
```

and in the same `finally` that calls `unbind_request_id(rid_token)`: `unbind_llm_context(llm_token)`.

`tests/test_worker_llm_context.py` — find how `tests/test_worker_runtime*.py` drives one job through the runtime with a fake handler (reuse its fixture); register a handler that captures `current_llm_context().job_id`, assert it equals the claimed job's id, and that after the run `current_llm_context().job_id is None`.

- [ ] **Step 4: Builders + the remaining entry points**

`app/api/entity_builder.py`:

```python
def _llm_turn(prompt, schema, *, user_id=None, subject_id=None):
    from src.observability.llm_context import llm_context
    ...
    with llm_context(workload="builder", purpose="entity_builder_turn", user_id=user_id, subject_id=subject_id):
        return extractor.extract_json(...)
```

Route: `_llm_turn(prompt, schema, user_id=user["id"], subject_id=<id or None>)`. Same for the other four (`agent_builder_turn` with `subject_id=agent_id`; `mcp_builder_turn`; `package_builder_turn` with the package id when the route has it; `semantic_model_builder_turn` with the model id when present). Add one test per builder module in its existing test file: monkeypatch `create_extractor_from_env_or_config` to return a fake whose `extract_json` records `current_llm_context()`, assert `workload == "builder"`, `purpose == "<kind>_builder_turn"`, `user_id == <test user id>`.

For the non-builder modules: wrap each `extract_json` call in `with llm_context(workload=..., purpose=...)` per the file list above, and add ONE test per module that asserts the context seen by a fake extractor (same recording-fake pattern).

- [ ] **Step 5: Run and commit**

Run the two new files plus the touched modules' existing tests → PASS.

```bash
git add <every touched file> tests/test_llm_coverage_guard.py tests/test_worker_llm_context.py
git commit -m "observability: trace every LLM call site, bind job id in the worker, label builder and service workloads"
```

---

### Task 4: Turn structure — turn id, turn/tool spans, frame stamping, broker child linkage

**Files:**
- Create: `app/chat/turn_context.py`
- Modify: `app/chat/manager.py` (`LiveSession` fields ~395–440; `_deliver_local_user_message` ~3138–3175; `_pump_subprocess_to_ws` ~2547–2740 — the `assistant_message`/`done`/`error`/`tool_call`/`tool_result` branches; `_broadcast` ~2891; `_record_turn_usage` ~804–860 (`turn_uuid`); `send_user_message` ~4097–4120 (thread the user message id))
- Modify: `src/observability/otel.py` (append `child_context`, `start_turn_span`, `end_turn_span`, `start_tool_span`, `end_tool_span`)
- Modify: `app/api/broker.py` (`_completion_context(turn=...)`, `_turn_for_row`, the span-open block)
- Test: `tests/test_chat_turn_context.py` (new — the module), `tests/test_chat_turn_spans.py` (new — ChatManager), `tests/test_broker_turn_linkage.py` (new)

**Interfaces:**
- Consumes (Task 1/2): `otel.remote_parent_context`, `otel.span_ids`, `otel._open_span(kind=, parent_context=)`, `_completion_context`, `_start_otel_completion_span(parent_context=)`, `tests/test_broker_llm_calls.py::ledger` fixture + `_json_body`.
- Produces `app.chat.turn_context`:
  - `TURN_TTL_SECONDS = 24 * 3600`, `turn_key(session_id) -> str` (`chat:turn:{session_id}`)
  - `@dataclass(frozen=True) class TurnRecord: turn_id: str; trace_id: str | None; span_id: str | None; started_at: str; user_id: str | None; agent_id: str | None; surface: str | None; workload: str; message_id: str | None = None` with `to_json()` / `from_json(text) -> TurnRecord | None`.
  - `publish_turn(session_id, record) -> None` — `coordination().kv_set(turn_key(session_id), record.to_json(), ttl_s=TURN_TTL_SECONDS)`; never raises.
  - `read_turn(session_id) -> TurnRecord | None` — `kv_get`; never raises.
  - `workload_for_surface(surface) -> str` — `"agent_api"` when the surface value is `api`, else `"chat"`.
- Produces in `src.observability.otel`:
  - `child_context(span) -> Any` — `trace.set_span_in_context(span)` when the API is present and `span` is recording, else `None`.
  - `start_turn_span(*, session_id, turn_id, user_id, agent_id, surface, workload) -> span` — name `agnes.chat.turn`, kind `INTERNAL`, attrs `agnes.kind="turn"`, `agnes.session_id`, `agnes.turn_id`, `agnes.user_id`, `agnes.agent_id`, `agnes.surface`, `agnes.workload`. No content.
  - `end_turn_span(span, *, tool_calls: int, usage: Mapping | None = None, cost_usd: float | None = None, error_kind: str | None = None) -> None` — sets `agnes.tool_calls`, the four `gen_ai.usage.*` via `set_usage_attributes`, `agnes.cost_usd`; `error.type` + ERROR status when `error_kind`, else OK; ends the span.
  - `start_tool_span(*, tool: str, args_hash: str | None, parent: Any) -> span` — name `agnes.chat.tool <tool>`, kind `INTERNAL`, `parent_context=child_context(parent)`, attrs `agnes.kind="tool"`, `agnes.tool`, `agnes.args_hash`. Never arguments or results.
  - `end_tool_span(span, *, is_error: bool) -> None` — `agnes.is_error`, status, end.
- Produces in `app.chat.manager.LiveSession`: `turn_id: str | None = None`, `turn_span: Any = None`, `turn_tool_calls: int = 0`, `turn_tool_spans: dict[str, Any] = field(default_factory=dict)`, `turn_error_kind: str | None = None`; module constant `_TURN_FRAME_TYPES`.
- Produces in `app.api.broker`: `_completion_context(row, *, agent_row, caller_user_id, turn: TurnRecord | None = None)` — with a turn: `workload=turn.workload`, `turn_id=turn.turn_id`, `user_id=caller_user_id or turn.user_id`, `agent_id=(agent_row["id"] if agent_row else turn.agent_id)`; `_turn_for_row(row) -> TurnRecord | None` (own try/except; `read_turn(row["session_id"])` when a session id is present).

- [ ] **Step 1: Failing tests for the turn-context module**

`tests/test_chat_turn_context.py`:

```python
import pytest

from app.chat.turn_context import TURN_TTL_SECONDS, TurnRecord, publish_turn, read_turn, turn_key, workload_for_surface
from app.coordination.factory import reset_coordination_for_tests


@pytest.fixture(autouse=True)
def _reset():
    reset_coordination_for_tests()
    yield
    reset_coordination_for_tests()


def _rec(**kw):
    base = dict(turn_id="t1", trace_id="a" * 32, span_id="b" * 16, started_at="2026-09-08T10:00:00+00:00",
                user_id="u1", agent_id=None, surface="web", workload="chat", message_id="msg_1")
    base.update(kw)
    return TurnRecord(**base)


def test_key_and_ttl_follow_the_spec():
    assert turn_key("s1") == "chat:turn:s1"
    assert TURN_TTL_SECONDS == 24 * 3600


def test_publish_then_read_round_trips_and_the_next_turn_overwrites():
    publish_turn("s1", _rec())
    assert read_turn("s1") == _rec()
    publish_turn("s1", _rec(turn_id="t2"))
    assert read_turn("s1").turn_id == "t2"


def test_read_of_an_unknown_session_is_none():
    assert read_turn("nope") is None


def test_malformed_value_reads_as_none():
    from app.coordination.factory import coordination

    coordination().kv_set(turn_key("s1"), "{not json", ttl_s=10)
    assert read_turn("s1") is None


def test_coordination_outage_never_raises(monkeypatch):
    import app.chat.turn_context as tc
    from app.coordination.base import CoordinationUnavailable

    def _boom():
        raise CoordinationUnavailable("down")

    monkeypatch.setattr(tc, "coordination", _boom)
    publish_turn("s1", _rec())
    assert read_turn("s1") is None


def test_workload_for_surface():
    assert workload_for_surface("api") == "agent_api"
    assert workload_for_surface("web") == "chat"
    assert workload_for_surface(None) == "chat"
```

- [ ] **Step 2: Implement `app/chat/turn_context.py`**

```python
"""The turn record — what the broker needs to parent a completion span under
the chat turn that caused it, without the engine propagating anything.

ChatManager mints a ``turn_id`` per delivered user message, opens the
``agnes.chat.turn`` span and publishes ``{turn_id, trace_id, span_id, …}``
under ``chat:turn:{session_id}`` (24 h TTL, the coordination backend
``app/chat/turn_usage.py`` already uses). The broker reads it on every
completion and opens its span as a child of that context — the two may be
different replicas; the collector stitches on ``trace_id``. The key is
overwritten by the next turn and never deleted at turn end, so a completion
that lands after the assistant frame still attributes to the turn that
caused it. Coordination unavailable → no linkage, everything else recorded.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from typing import Optional

from app.coordination.base import CoordinationUnavailable
from app.coordination.factory import coordination

logger = logging.getLogger(__name__)

TURN_TTL_SECONDS = 24 * 3600


def turn_key(session_id: str) -> str:
    return f"chat:turn:{session_id}"


def workload_for_surface(surface: Optional[str]) -> str:
    return "agent_api" if str(surface or "") == "api" else "chat"


@dataclass(frozen=True)
class TurnRecord:
    turn_id: str
    trace_id: Optional[str]
    span_id: Optional[str]
    started_at: str
    user_id: Optional[str]
    agent_id: Optional[str]
    surface: Optional[str]
    workload: str
    message_id: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: Optional[str]) -> Optional["TurnRecord"]:
        if not text:
            return None
        try:
            data = json.loads(text)
            if not isinstance(data, dict) or not data.get("turn_id"):
                return None
            return cls(**{k: data.get(k) for k in cls.__dataclass_fields__})
        except (ValueError, TypeError):
            return None


def publish_turn(session_id: str, record: TurnRecord) -> None:
    try:
        coordination().kv_set(turn_key(session_id), record.to_json(), ttl_s=TURN_TTL_SECONDS)
    except CoordinationUnavailable:
        logger.warning("turn record not published for session %s: coordination unavailable", session_id)
    except Exception:  # noqa: BLE001
        logger.warning("turn record publish failed for session %s", session_id, exc_info=True)


def read_turn(session_id: str) -> Optional[TurnRecord]:
    try:
        return TurnRecord.from_json(coordination().kv_get(turn_key(session_id)))
    except CoordinationUnavailable:
        return None
    except Exception:  # noqa: BLE001
        logger.debug("turn record read failed for session %s", session_id, exc_info=True)
        return None
```

Run: `.venv/bin/pytest tests/test_chat_turn_context.py -q` → PASS

- [ ] **Step 3: `otel.py` turn/tool span helpers** (append after `remote_parent_context`; each wrapped like the existing helpers, never raising)

```python
def child_context(span: Any) -> Any:
    if not _OTEL_API:
        return None
    try:
        return trace.set_span_in_context(span) if span.is_recording() else None
    except Exception:  # noqa: BLE001
        return None


def start_turn_span(*, session_id, turn_id, user_id, agent_id, surface, workload) -> Any:
    attrs = _clean({
        "agnes.kind": "turn", "agnes.session_id": session_id, "agnes.turn_id": turn_id,
        "agnes.user_id": user_id, "agnes.agent_id": agent_id, "agnes.surface": surface,
        "agnes.workload": workload,
    })
    return _open_span("agnes.chat.turn", attrs, kind=SpanKind.INTERNAL if _OTEL_API else None)


def end_turn_span(span, *, tool_calls, usage=None, cost_usd=None, error_kind=None) -> None:
    try:
        if not span.is_recording():
            return
        span.set_attribute("agnes.tool_calls", int(tool_calls))
        set_usage_attributes(span, usage)
        if isinstance(cost_usd, (int, float)) and not isinstance(cost_usd, bool):
            span.set_attribute("agnes.cost_usd", float(cost_usd))
        if error_kind:
            span.set_attribute("error.type", str(error_kind))
            span.set_status(StatusCode.ERROR, str(error_kind)[:200])
        else:
            span.set_status(StatusCode.OK)
    except Exception:  # noqa: BLE001
        logger.debug("otel: could not finish the turn span", exc_info=True)
    finally:
        try:
            span.end()
        except Exception:  # noqa: BLE001
            pass


def start_tool_span(*, tool, args_hash, parent) -> Any:
    attrs = _clean({"agnes.kind": "tool", "agnes.tool": tool, "agnes.args_hash": args_hash})
    return _open_span(f"agnes.chat.tool {tool}", attrs, kind=SpanKind.INTERNAL if _OTEL_API else None,
                      parent_context=child_context(parent))


def end_tool_span(span, *, is_error: bool) -> None:
    try:
        if not span.is_recording():
            return
        span.set_attribute("agnes.is_error", bool(is_error))
        span.set_status(StatusCode.ERROR if is_error else StatusCode.OK)
    except Exception:  # noqa: BLE001
        logger.debug("otel: could not finish the tool span", exc_info=True)
    finally:
        try:
            span.end()
        except Exception:  # noqa: BLE001
            pass
```

Add all five to `__all__`.

- [ ] **Step 4: ChatManager**

1. `LiveSession` fields (see Interfaces).
2. `_deliver_local_user_message(self, live, text, *, message_id: str | None = None)`: after `live.turn_in_flight = True` add:

```python
        live.turn_id = str(uuid4())
        live.turn_tool_calls = 0
        live.turn_tool_spans = {}
        live.turn_error_kind = None
        self._open_turn(live, message_id=message_id)
```

with

```python
    def _open_turn(self, live: LiveSession, *, message_id: str | None) -> None:
        """Open the turn span and publish the turn record — never raises."""
        try:
            user_id = None
            with contextlib.suppress(Exception):
                user_id = (users_repo().get_by_email(live.user_email) or {}).get("id")
            agent_id = None
            with contextlib.suppress(Exception):
                agent_id = getattr(self._repo.get_session(live.chat_id), "agent_id", None)
            workload = workload_for_surface(live.surface)
            live.turn_span = _otel.start_turn_span(
                session_id=live.chat_id, turn_id=live.turn_id, user_id=user_id,
                agent_id=agent_id, surface=str(live.surface or ""), workload=workload,
            )
            trace_id, span_id = _otel.span_ids(live.turn_span)
            publish_turn(live.chat_id, TurnRecord(
                turn_id=live.turn_id, trace_id=trace_id, span_id=span_id,
                started_at=datetime.now(UTC).isoformat(), user_id=user_id, agent_id=agent_id,
                surface=str(live.surface or ""), workload=workload, message_id=message_id,
            ))
        except Exception:
            logger.debug("turn open failed for %s (non-fatal)", live.chat_id, exc_info=True)
```

(`live.surface` may be an enum — use `getattr(live.surface, "value", live.surface)`.) The two lookups are one DB read each per turn — the same reads `_record_turn_usage` and the agent gate already pay; acceptable.

3. `send_user_message` (~4097): capture `msg = self._repo.append_message(...)` and pass `message_id=msg.id` to `_deliver_local_user_message`. The inbound consumer path (~3916) and the restart replay (~2292) pass nothing (the id is not in the stream entry).
4. `_broadcast`: first line of the locked section, before `stamp_frame`: `if live.turn_id and frame.get("type") in _TURN_FRAME_TYPES: frame.setdefault("turn_id", live.turn_id)` with `_TURN_FRAME_TYPES = frozenset({"assistant_message", "tool_call", "tool_result", "error", "done", "token", "cancelled", "confirmation_required"})`.
5. Pump branches:
   - `tool_call`: after the audit write, `self._open_tool_span(live, frame)` → `live.turn_tool_calls += 1`; `span = _otel.start_tool_span(tool=str(frame.get("tool")), args_hash=hash_args(frame.get("args", {})), parent=live.turn_span)`; `live.turn_tool_spans[_tool_key(frame, live)] = span` where `_tool_key` returns `str(frame.get("tool_use_id"))` when present, else `f"#{live.turn_tool_calls}"`.
   - `tool_result`: `span = live.turn_tool_spans.pop(<same key>, None)`; if found `_otel.end_tool_span(span, is_error=bool(frame.get("is_error")))`.
   - `error`: `live.turn_error_kind = str(frame.get("kind") or "error")`.
   - `assistant_message` (after `_record_turn_usage`) and `done`: `self._close_turn(live, frame)`:

```python
    def _close_turn(self, live: LiveSession, frame: dict) -> None:
        try:
            span, live.turn_span = live.turn_span, None
            if span is None:
                return
            for tool_span in live.turn_tool_spans.values():
                _otel.end_tool_span(tool_span, is_error=False)
            live.turn_tool_spans = {}
            usage = None
            cost = None
            if frame.get("type") == "assistant_message":
                usage = {
                    "model": frame.get("model"),
                    "input_tokens": int(frame.get("tokens_in") or 0),
                    "output_tokens": int(frame.get("tokens_out") or 0),
                    "cache_read_tokens": int(frame.get("cache_read_tokens") or 0),
                    "cache_creation_tokens": int(frame.get("cache_creation_tokens") or 0),
                }
                cost = round(cost_usd(model=frame.get("model"), input_tokens=usage["input_tokens"],
                                      output_tokens=usage["output_tokens"], cache_read_tokens=usage["cache_read_tokens"],
                                      cache_creation_tokens=usage["cache_creation_tokens"]), 6)
            _otel.end_turn_span(span, tool_calls=live.turn_tool_calls, usage=usage, cost_usd=cost,
                                error_kind=live.turn_error_kind)
        except Exception:
            logger.debug("turn close failed for %s (non-fatal)", live.chat_id, exc_info=True)
```

   Every span call in the pump is inside try/except — a span must never break the pump.
6. `_record_turn_usage`: `"turn_uuid": live.turn_id or str(uuid4())`.
7. Wherever `turn_in_flight = False` is set outside the pump (grep — the partial-save on kill ~4334), also call `self._close_turn(live, {"type": "done"})`.

- [ ] **Step 5: ChatManager tests**

`tests/test_chat_turn_spans.py` — reuse the `manager` fixture and `_attach_fake_live_with_fake_handle` from `tests/test_chat_manager.py`, `FakeHandle`/`FakeWS`/`_wait_until` from `tests/chat_fakes.py`, and `otel_exporter` from `tests/test_otel_export.py` (read those files first and adapt the exact calls):

```python
def test_turn_span_wraps_tool_spans_and_completions(manager, otel_exporter):
    """user_msg → tool_call → tool_result → assistant_message: one INTERNAL turn span,
    one nested tool span, turn_id on every frame, the record under chat:turn:{id}."""
    live, handle, ws = ...  # attach a fake live session per the helper
    asyncio.run(manager._deliver_local_user_message(live, "hello", message_id="msg_u1"))
    rec = read_turn(live.chat_id)
    assert rec is not None and rec.turn_id == live.turn_id and rec.message_id == "msg_u1"
    assert rec.trace_id and rec.span_id
    handle.feed({"type": "tool_call", "tool": "Bash", "tool_use_id": "tu1", "args": {"cmd": "ls"}})
    handle.feed({"type": "tool_result", "tool_use_id": "tu1", "result": "ok", "is_error": False})
    handle.feed({"type": "assistant_message", "content": "done", "tokens_in": 10, "tokens_out": 5,
                 "model": "claude-sonnet-5"})
    _wait_until(lambda: not live.turn_in_flight)
    frames = ws.sent
    assert all(f.get("turn_id") == rec.turn_id for f in frames
               if f.get("type") in ("tool_call", "tool_result", "assistant_message"))
    spans = {s.name: s for s in otel_exporter.get_finished_spans()}
    turn, tool = spans["agnes.chat.turn"], spans["agnes.chat.tool Bash"]
    assert tool.parent.span_id == turn.context.span_id
    assert turn.kind.name == "INTERNAL"
    a = dict(turn.attributes)
    assert a["agnes.tool_calls"] == 1 and a["agnes.turn_id"] == rec.turn_id and a["agnes.cost_usd"] > 0
    assert a["gen_ai.usage.input_tokens"] == 10
    assert "ls" not in json.dumps(dict(tool.attributes)) and dict(tool.attributes)["agnes.args_hash"]


def test_turn_key_survives_turn_end_and_next_turn_overwrites(manager):
    ...  # deliver, close via assistant_message, read_turn still returns it; deliver again → new id


def test_usage_turns_row_uses_the_turn_id(manager, monkeypatch):
    ...  # reuse tests/test_chat_usage_turns.py's _RecordingTurnsRepo; rows[0]["turn_uuid"] == live.turn_id


def test_spans_never_break_the_pump(manager, monkeypatch):
    def _boom(**kw):
        raise RuntimeError("otel down")

    monkeypatch.setattr("app.chat.manager._otel.start_turn_span", _boom)
    ...  # deliver + assistant_message still persists the message and clears turn_in_flight
```

- [ ] **Step 6: Broker child linkage**

In `app/api/broker.py`:

```python
from app.chat.turn_context import TurnRecord, read_turn


def _turn_for_row(row: Dict[str, Any]) -> Optional[TurnRecord]:
    """The live turn for the ticket's session — ``None`` when there is none
    or the coordination backend is unavailable (degrade, never fail)."""
    try:
        session_id = row.get("session_id")
        return read_turn(session_id) if session_id else None
    except Exception:  # noqa: BLE001
        return None
```

`_completion_context(row, *, agent_row, caller_user_id, turn=None)` per the Interfaces block. At the span-open block: `turn = _turn_for_row(row) if is_completion else None`; `completion_context = _completion_context(row, agent_row=agent_row, caller_user_id=caller_user_id, turn=turn)`; `parent_context = _otel.remote_parent_context(turn.trace_id, turn.span_id) if turn else None`; pass `parent_context=parent_context` to `_start_otel_completion_span`. Leave a one-line comment that a `traceparent` request header, if the engine ever sends one, would be preferred here.

`tests/test_broker_turn_linkage.py`:

```python
from tests.test_broker_llm_calls import _json_body, ledger  # noqa: F401
from tests.test_otel_export import _FakeUpstream, _post, otel_broker, otel_exporter  # noqa: F401


def test_completion_span_is_a_child_of_the_published_turn(otel_broker, otel_exporter, ledger):
    from app.api.broker_agent_policy import usage_accumulator
    from app.chat.turn_context import TurnRecord, publish_turn
    from src.repositories import ticket_repo

    publish_turn("chat_link_1", TurnRecord(turn_id="t-1", trace_id="c" * 32, span_id="d" * 16,
                 started_at="2026-09-08T00:00:00+00:00", user_id="u-1", agent_id="ag-1",
                 surface="api", workload="agent_api"))
    _FakeUpstream.body = _json_body()
    tok = ticket_repo().mint("chat_link_1", "main", ttl_seconds=60)
    r = _post(otel_broker, tok, "/api/broker/anthropic/v1/messages",
              {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    usage_accumulator.flush()
    (span,) = otel_exporter.get_finished_spans()
    assert format(span.context.trace_id, "032x") == "c" * 32
    assert format(span.parent.span_id, "016x") == "d" * 16
    attrs = dict(span.attributes)
    assert attrs["agnes.turn_id"] == "t-1" and attrs["agnes.user_id"] == "u-1"
    assert attrs["agnes.agent_id"] == "ag-1" and attrs["agnes.workload"] == "agent_api"
    (row,) = ledger
    assert row["turn_id"] == "t-1" and row["trace_id"] == "c" * 32 and row["workload"] == "agent_api"


def test_without_a_turn_record_the_span_is_a_root_and_turn_id_is_null(otel_broker, otel_exporter, ledger):
    ...  # span.parent is None; row["turn_id"] is None; everything else recorded


def test_coordination_outage_degrades_to_a_root_span(otel_broker, otel_exporter, ledger, monkeypatch):
    import app.api.broker as broker_mod

    def _boom(session_id):
        raise RuntimeError("down")

    monkeypatch.setattr(broker_mod, "read_turn", _boom)
    ...  # request still 200; span exported as a root; row written
```

- [ ] **Step 7: Run + commit**

Run: `.venv/bin/pytest tests/test_chat_turn_context.py tests/test_chat_turn_spans.py tests/test_broker_turn_linkage.py tests/test_chat_manager.py tests/test_chat_usage_turns.py tests/test_chat_multisink.py tests/test_broker_llm_calls.py tests/test_otel_export.py -q` → PASS

```bash
git add app/chat/turn_context.py app/chat/manager.py src/observability/otel.py app/api/broker.py \
  tests/test_chat_turn_context.py tests/test_chat_turn_spans.py tests/test_broker_turn_linkage.py
git commit -m "chat: turn id, turn and tool spans, turn_id on frames, broker completions parented under the turn"
```

---

### Task 5: Content policy — placement and consent, pseudonymised mode, the relay under the same policy

**Files:**
- Create: `src/observability/content_policy.py`
- Create: `src/observability/otlp_scrub.py`
- Modify: `src/observability/otel.py` (`capture_content_enabled`, the content-event block of `end_completion_span`, `end_generation_span(prompt_text=, completion_text=)`)
- Modify: `src/observability/llm_tracing.py` (`_Capture.prompt_text` / `completion_text`, passed to `end_generation_span`)
- Modify: `app/api/broker.py` (`otlp_proxy`)
- Modify: `app/main.py` lifespan (one call after `configure_otel(...)`: `announce_content_export_policy()`)
- Modify: `src/audit_events.py` (`observability.content_export`, category `system`)
- Modify: `config/instance.yaml.example` (the `observability:` block)
- Test: `tests/test_content_policy.py` (new), `tests/test_otlp_scrub.py` (new), `tests/test_broker_otlp.py` (extend), `tests/test_otel_export.py` (content tests now enable content via the policy, not the env var)

**Interfaces:**
- Produces `src.observability.content_policy`:
  - `MODES = ("off", "pseudonymized", "full")`, `PLACEMENTS = ("operator", "third_party")`, `DEPRECATED_ENV_VAR = "AGNES_OTEL_CAPTURE_CONTENT"`
  - `@dataclass(frozen=True) class ContentExportPolicy: mode: str; placement: str; basis: str; approved_by: str; approved_at: str; requested_mode: str; warnings: tuple[str, ...]` — `mode` is the EFFECTIVE mode (`off` unless the record is complete), `requested_mode` what the config asked for.
  - `load_content_export_policy(config: Mapping | None = None) -> ContentExportPolicy` — reads `observability.content_export` from `config` (or `app.instance_config.get_value("observability", "content_export")`); an unknown `mode`/`placement` → `off` with a warning; `mode != off` with any of `basis`/`approved_by`/`placement` blank → `off` with the warning text `content export requested without a recorded basis; exporting sizes only`; the env var set while the block is absent/`off` → ignored, same warning plus `AGNES_OTEL_CAPTURE_CONTENT is deprecated`.
  - `content_export_mode() -> str` — the effective mode, read per call (no cache).
  - `export_text(text: str) -> str` — `full` → unchanged; `pseudonymized` → `anonymize_markdown(text, key=resolve_or_provision_key(), rules=rules_from_config()).text`; on any failure (no key, anonymizer error) returns `"[content withheld: pseudonymisation unavailable]"` and logs at WARNING once per process (fail closed, never the raw text); `off` → `""`.
  - `announce_content_export_policy() -> None` — logs the effective policy once at INFO (`mode`, `placement`, `approved_by`; the warnings at WARNING) and writes ONE audit row via `log_safe(user_id=None, action="observability.content_export", resource="observability:content_export", params={"mode", "requested_mode", "placement", "approved_by", "approved_at", "basis_recorded": bool}, result="success", client_kind="system")`; idempotent within a process (module flag) so a re-run of the lifespan does not double-write.
- Produces `src.observability.otlp_scrub`:
  - `CONTENT_ATTRIBUTE_KEYS = frozenset({"gen_ai.prompt", "gen_ai.completion", "gen_ai.input.messages", "gen_ai.output.messages"})`
  - `class OtlpBatchUndecodable(ValueError)`
  - `decode_body(body: bytes, content_encoding: str | None) -> bytes` (gunzip when `gzip`; raises `OtlpBatchUndecodable`)
  - `scrub_traces(body: bytes, *, mode: str, content_encoding: str | None = None) -> bytes` — parses `ExportTraceServiceRequest`; for every span and span event: `off` → remove the content attributes, add `agnes.content_stripped=true` (bool) when any was removed; `pseudonymized` → replace each content attribute's `string_value` with `export_text(value)`, a non-string value is removed and flagged the same way; `full` → returned unchanged (no decode). Serialises uncompressed. Raises `OtlpBatchUndecodable` on a parse failure.
  - `scrub_logs(body: bytes, *, mode: str, content_encoding: str | None = None) -> bytes | None` — `off` → `None` (drop); `pseudonymized` → each `LogRecord.body.string_value` and each content attribute rewritten through `export_text`; `full` → unchanged.
  - `empty_logs_response() -> bytes` — a serialised `ExportLogsServiceResponse()`.
- Produces in `src.observability.otel`: `capture_content_enabled() -> bool` is `content_export_mode() != "off"`; both event emitters run each text through `export_text` before `add_event`; `end_generation_span(..., prompt_text: str | None = None, completion_text: str | None = None)` emits `gen_ai.content.prompt` / `gen_ai.content.completion` events when enabled (same cap + `agnes.content_truncated`).
- Produces in `src.observability.llm_tracing._Capture`: `prompt_text: str | None` (set by `set_input` when the prompt is a `str`), `completion_text: str | None` (set by `set_output_from_anthropic` from the text blocks / `set_output_from_openai` from `choices[0].message.content` / `set_output(str)`).

- [ ] **Step 1: Failing policy tests**

`tests/test_content_policy.py`:

```python
import logging

import pytest

from src.observability import content_policy as cp


def _cfg(**block):
    return {"observability": {"content_export": block}}


def test_off_when_absent(monkeypatch):
    monkeypatch.delenv(cp.DEPRECATED_ENV_VAR, raising=False)
    p = cp.load_content_export_policy({})
    assert p.mode == "off" and p.warnings == ()


@pytest.mark.parametrize("mode", ["pseudonymized", "full"])
def test_a_complete_record_enables_the_requested_mode(mode):
    p = cp.load_content_export_policy(_cfg(mode=mode, placement="operator", basis="DPA §4", approved_by="A. Person",
                                           approved_at="2026-09-01"))
    assert p.mode == mode and p.placement == "operator" and p.approved_by == "A. Person"


@pytest.mark.parametrize("missing", ["basis", "approved_by", "placement"])
def test_a_mode_without_a_basis_is_off_loudly(missing, caplog):
    block = dict(mode="full", placement="third_party", basis="contract", approved_by="x", approved_at="2026-09-01")
    block[missing] = ""
    p = cp.load_content_export_policy(_cfg(**block))
    assert p.mode == "off" and p.requested_mode == "full"
    assert any("without a recorded basis" in w for w in p.warnings)


def test_unknown_mode_or_placement_is_off():
    assert cp.load_content_export_policy(_cfg(mode="everything", placement="operator", basis="b", approved_by="a")).mode == "off"
    assert cp.load_content_export_policy(_cfg(mode="full", placement="cloud", basis="b", approved_by="a")).mode == "off"


def test_the_env_var_is_a_deprecated_alias_that_never_enables_content(monkeypatch):
    monkeypatch.setenv(cp.DEPRECATED_ENV_VAR, "1")
    p = cp.load_content_export_policy({})
    assert p.mode == "off"
    assert any("deprecated" in w for w in p.warnings)


def test_export_text_by_mode(monkeypatch):
    monkeypatch.setattr(cp, "content_export_mode", lambda: "full")
    assert cp.export_text("Jane <jane@example.com>") == "Jane <jane@example.com>"
    monkeypatch.setattr(cp, "content_export_mode", lambda: "off")
    assert cp.export_text("x") == ""


def test_pseudonymized_export_runs_the_anonymizer(monkeypatch):
    monkeypatch.setattr(cp, "content_export_mode", lambda: "pseudonymized")
    monkeypatch.setattr(cp, "_pseudonym_key", lambda: b"unit-test-key")
    out = cp.export_text("mail jane@example.com now")
    assert "jane@example.com" not in out and "EMAIL_" in out


def test_pseudonymized_export_fails_closed_without_a_key(monkeypatch):
    from src.anonymization_key import AnonymizationKeyError

    def _no_key():
        raise AnonymizationKeyError("none")

    monkeypatch.setattr(cp, "content_export_mode", lambda: "pseudonymized")
    monkeypatch.setattr(cp, "_pseudonym_key", _no_key)
    assert cp.export_text("jane@example.com") == "[content withheld: pseudonymisation unavailable]"


def test_announce_logs_once_and_audits(monkeypatch, caplog):
    rows = []
    monkeypatch.setattr(cp, "log_safe", lambda **kw: rows.append(kw))
    monkeypatch.setattr(cp, "load_content_export_policy", lambda config=None: cp.ContentExportPolicy(
        mode="full", placement="operator", basis="b", approved_by="a", approved_at="2026-09-01",
        requested_mode="full", warnings=()))
    cp._announced = False
    with caplog.at_level(logging.INFO):
        cp.announce_content_export_policy()
        cp.announce_content_export_policy()
    assert len(rows) == 1 and rows[0]["action"] == "observability.content_export"
    assert rows[0]["params"]["mode"] == "full" and rows[0]["client_kind"] == "system"
    assert "b" not in str(rows[0]["params"].get("basis", ""))  # basis_recorded is a bool, the text stays in config
```

- [ ] **Step 2: Implement `src/observability/content_policy.py`** per the Interfaces block (use `from src.anonymization import anonymize_markdown, rules_from_config`; `_pseudonym_key()` wraps `src.anonymization_key.resolve_or_provision_key()`; `_announced = False` module flag; import `log_safe` from `src.audit_helpers` at module level so the test can monkeypatch it). Add the CATALOG entry:

```python
    # -- 2026-09-08 LLM observability design §3.6 — the effective content-export
    # policy is written once at startup so the decision is in the trail.
    "observability.content_export": AuditEvent(
        "observability.content_export",
        "system",
        "The instance's effective content-export policy (mode, placement, approver) at startup.",
    ),
```

Run: `.venv/bin/pytest tests/test_content_policy.py tests/test_audit_catalog.py -q` → PASS

- [ ] **Step 3: `otel.py` + `llm_tracing.py` under the policy**

- `capture_content_enabled()`:

```python
def capture_content_enabled() -> bool:
    """Content leaves the instance only under a recorded policy
    (``observability.content_export`` — src/observability/content_policy.py).
    ``AGNES_OTEL_CAPTURE_CONTENT`` is a deprecated alias that no longer enables
    anything on its own."""
    from src.observability.content_policy import content_export_mode

    return content_export_mode() != "off"
```

- In `end_completion_span`'s content block: `text, cut = truncate_content(export_text(prompt_json))` (import `export_text` lazily inside the block), same for the completion.
- `end_generation_span(..., prompt_text=None, completion_text=None)`: after the numeric attributes, `if capture_content_enabled(): ...` emit `PROMPT_EVENT` with `{"gen_ai.prompt": text}` where `text, cut = truncate_content(export_text(json.dumps([{"role": "user", "parts": [{"type": "text", "content": prompt_text}]}], ensure_ascii=False)))` when `prompt_text` is a str, and `COMPLETION_EVENT` with the assistant shape for `completion_text`; set `agnes.content_truncated` on a cut.
- `_Capture`: `set_input` keeps `prompt_text = prompt if isinstance(prompt, str) else None`; `set_output(output)` keeps `completion_text` when a str; `set_output_from_anthropic` joins the text blocks into `completion_text`; `set_output_from_openai` keeps `message.content` when a str. `trace_generation` passes both to `end_generation_span`.
- `tests/test_otel_export.py`: every test that set `otel.CAPTURE_CONTENT_VAR` now enables content through the policy — add a fixture `content_policy_full(monkeypatch)` that monkeypatches `src.observability.content_policy.content_export_mode` to `lambda: "full"` (and `content_policy_pseudonymized` → `"pseudonymized"` with `_pseudonym_key` → `b"k"`), and a new test `test_the_env_var_alone_exports_no_content` (env var set, no policy → `span.events == ()`), plus `test_generation_span_carries_content_events_under_policy` (a `trace_generation` with `set_input("Hello jane@example.com")` / `set_output("fine")` under `pseudonymized` → two events, the email replaced by an `EMAIL_` token, sizes unchanged).

Run: `.venv/bin/pytest tests/test_otel_export.py tests/test_llm_tracing.py -q` → PASS

- [ ] **Step 4: The relay scrubber with a real protobuf batch**

`tests/test_otlp_scrub.py` builds batches with the proto classes:

```python
import gzip

import pytest
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
from opentelemetry.proto.common.v1 import common_pb2
from opentelemetry.proto.logs.v1 import logs_pb2
from opentelemetry.proto.trace.v1 import trace_pb2

from src.observability import content_policy as cp
from src.observability import otlp_scrub as sc


def _kv(key, value):
    return common_pb2.KeyValue(key=key, value=common_pb2.AnyValue(string_value=value))


def _trace_batch():
    span = trace_pb2.Span(name="turn", trace_id=b"\x01" * 16, span_id=b"\x02" * 8)
    span.attributes.extend([_kv("gen_ai.prompt", "hello jane@example.com"), _kv("agnes.turn_id", "t1")])
    ev = span.events.add(name="gen_ai.content.completion")
    ev.attributes.extend([_kv("gen_ai.completion", "call 777 123 456"), _kv("kept", "yes")])
    req = trace_service_pb2.ExportTraceServiceRequest()
    req.resource_spans.add().scope_spans.add().spans.append(span)
    return req.SerializeToString()


def _span(body: bytes):
    req = trace_service_pb2.ExportTraceServiceRequest()
    req.ParseFromString(body)
    return req.resource_spans[0].scope_spans[0].spans[0]


def _attrs(items):
    return {kv.key: kv.value for kv in items}


def test_off_strips_content_from_spans_and_events_and_keeps_structure():
    out = sc.scrub_traces(_trace_batch(), mode="off")
    span = _span(out)
    a = _attrs(span.attributes)
    assert "gen_ai.prompt" not in a and a["agnes.turn_id"].string_value == "t1"
    assert a["agnes.content_stripped"].bool_value is True
    e = _attrs(span.events[0].attributes)
    assert "gen_ai.completion" not in e and e["kept"].string_value == "yes"
    assert e["agnes.content_stripped"].bool_value is True


def test_pseudonymized_rewrites_content(monkeypatch):
    monkeypatch.setattr(cp, "content_export_mode", lambda: "pseudonymized")
    monkeypatch.setattr(cp, "_pseudonym_key", lambda: b"k")
    span = _span(sc.scrub_traces(_trace_batch(), mode="pseudonymized"))
    assert "jane@example.com" not in _attrs(span.attributes)["gen_ai.prompt"].string_value
    assert "EMAIL_" in _attrs(span.attributes)["gen_ai.prompt"].string_value


def test_full_forwards_bytes_unchanged():
    body = _trace_batch()
    assert sc.scrub_traces(body, mode="full") is body


def test_gzip_is_decoded_and_the_result_is_uncompressed():
    body = gzip.compress(_trace_batch())
    out = sc.scrub_traces(body, mode="off", content_encoding="gzip")
    assert _span(out).name == "turn"


def test_undecodable_batch_is_refused():
    with pytest.raises(sc.OtlpBatchUndecodable):
        sc.scrub_traces(b"\xff\xfe not a batch", mode="off")
    with pytest.raises(sc.OtlpBatchUndecodable):
        sc.scrub_traces(b"nope", mode="off", content_encoding="gzip")


def test_logs_are_dropped_rewritten_or_forwarded(monkeypatch):
    req = logs_service_pb2.ExportLogsServiceRequest()
    rec = req.resource_logs.add().scope_logs.add().log_records.add()
    rec.body.string_value = "user jane@example.com asked"
    body = req.SerializeToString()
    assert sc.scrub_logs(body, mode="off") is None
    monkeypatch.setattr(cp, "content_export_mode", lambda: "pseudonymized")
    monkeypatch.setattr(cp, "_pseudonym_key", lambda: b"k")
    out = logs_service_pb2.ExportLogsServiceRequest()
    out.ParseFromString(sc.scrub_logs(body, mode="pseudonymized"))
    assert "jane@example.com" not in out.resource_logs[0].scope_logs[0].log_records[0].body.string_value
    assert sc.scrub_logs(body, mode="full") is body
    assert isinstance(sc.empty_logs_response(), bytes)
```

Implement `src/observability/otlp_scrub.py` accordingly (helper `_scrub_attributes(repeated_kv, mode) -> None` used for spans, events and log records; `content_encoding` compared case-insensitively; `export_text` imported from `content_policy`).

- [ ] **Step 5: `otlp_proxy` under the policy**

In the route, after the size checks and `body = await request.body()`:

```python
    mode = content_export_mode()
    encoding = request.headers.get("content-encoding")
    try:
        if signal == "traces" and mode != "full":
            body = scrub_traces(body, mode=mode, content_encoding=encoding)
            encoding = None
        elif signal == "logs":
            if mode == "off":
                return Response(content=empty_logs_response(), status_code=200, media_type="application/x-protobuf")
            if mode == "pseudonymized":
                body = scrub_logs(body, mode=mode, content_encoding=encoding) or b""
                encoding = None
    except OtlpBatchUndecodable:
        raise HTTPException(status_code=400, detail={"code": "otlp_batch_undecodable"})
```

and build the forwarded headers from `encoding` (drop `content-encoding` when the batch was re-serialised). Metrics are untouched. Docstring: the relay fails closed on content.

`tests/test_broker_otlp.py` additions (reuse `otlp_broker`, `_post`, `_FakeCollectorClient.captured`):
- `test_traces_are_stripped_under_off`: post the `_trace_batch()` bytes from `tests/test_otlp_scrub.py`; the captured forwarded content parses and has no `gen_ai.prompt`, has `agnes.content_stripped`.
- `test_traces_forward_unchanged_under_full` (monkeypatch mode → full): captured content == posted bytes, `content-encoding` header preserved when sent.
- `test_logs_are_accepted_and_dropped_under_off`: 200, `_FakeCollectorClient.captured == {}`.
- `test_undecodable_traces_batch_is_400`: `otlp_batch_undecodable`, nothing forwarded.
- `test_gzip_batch_is_forwarded_uncompressed`: post gzip with `content-encoding: gzip` under `off`; captured headers carry no `content-encoding`.

- [ ] **Step 6: Startup announce + config example**

`app/main.py` lifespan, right after `configure_otel(...)`:

```python
    from src.observability.content_policy import announce_content_export_policy

    announce_content_export_policy()
```

`config/instance.yaml.example` — add before the `# --- Per-trail retention` block:

```yaml
# --- Observability: content export policy (LLM observability design, 2026-09-08) ---
# Prompt and completion TEXT leave this instance (on OTLP spans, and through the
# embedded engine's telemetry relay) only under a recorded decision. Sizes and
# token counts are always exported; text never is unless `mode` is set AND the
# consent fields below are filled in — a mode without a basis is treated as
# `off` and logged at startup. `AGNES_OTEL_CAPTURE_CONTENT` is deprecated and
# no longer enables content on its own.
# observability:
#   content_export:
#     mode: off             # off | pseudonymized | full
#     placement: operator   # operator | third_party — who runs the collector (T1 vs T2 in docs/observability.md)
#     basis: ""             # the contract clause / DPA reference / "internal dev instance" that permits it
#     approved_by: ""       # a person, never blank unless mode is off
#     approved_at: ""       # ISO date
```

- [ ] **Step 7: Run + commit**

Run: `.venv/bin/pytest tests/test_content_policy.py tests/test_otlp_scrub.py tests/test_broker_otlp.py tests/test_otel_export.py tests/test_llm_tracing.py tests/test_audit_catalog.py -q` → PASS

```bash
git add src/observability/content_policy.py src/observability/otlp_scrub.py src/observability/otel.py \
  src/observability/llm_tracing.py app/api/broker.py app/main.py src/audit_events.py config/instance.yaml.example \
  tests/test_content_policy.py tests/test_otlp_scrub.py tests/test_broker_otlp.py tests/test_otel_export.py tests/test_llm_tracing.py
git commit -m "observability: content export policy with placement and consent, pseudonymised mode, relay strips or rewrites content"
```

---

### Task 6: Schema, repositories, ledger end-to-end, memory provenance, `agnes_llm_calls`, retention

**Files:**
- Create: `migrations/versions/0115_llm_observability.py`
- Create: `src/models/llm_observability.py`; Modify: `src/models/chat.py` (`ChatMessage.turn_id`), `src/models/agents.py` (`AgentMemory.source_turn_id`, `source_message_id`), `src/models/__init__.py`
- Create: `src/repositories/llm_calls_pg.py`, `src/repositories/chat_message_feedback_pg.py`; Modify: `src/repositories/__init__.py` (`__all__`, `_REGISTRY`, factories `llm_calls_repo`, `chat_message_feedback_repo`)
- Modify: `src/repositories/chat_messages_pg.py` (`append_message(turn_id=)`, `list_messages`/`list_recent_messages` SELECT + `ChatMessage(turn_id=)`), `app/chat/persistence.py` (`append_message(turn_id=)` — PG passes it, DuckDB drops it), `app/chat/types.py` (`ChatMessage.turn_id: Optional[str] = None`), `app/chat/manager.py` (`append_message(..., turn_id=live.turn_id)` at the assistant persist ~2685)
- Modify: `src/repositories/agent_memories.py`, `src/repositories/agent_memories_pg.py` (`create(..., source_turn_id=None, source_message_id=None)`), `app/api/agent_memory.py` (`remember` fills them from `read_turn`; audit row carries `turn_id`)
- Modify: `connectors/internal/access.py` (`agnes_llm_calls`), `connectors/internal/registry.py` (`PG_ONLY_INTERNAL_TABLE_IDS`, package description text)
- Modify: `src/audit_retention.py` (`_prune_llm_calls`), `app/instance_config.py` (`get_llm_calls_retention_days`), `app/api/admin.py` (`run_retention_prune` windows), `config/instance.yaml.example` (`retention.llm_calls_days`)
- Modify: `scripts/migrate_duckdb_to_pg/__init__.py` — nothing (both new tables use a plain `id` PK)
- Test: `tests/db_pg/test_llm_calls_pg.py` (new), `tests/db_pg/test_chat_message_feedback_pg.py` (new), `tests/db_pg/test_agent_memories_contract.py` (extend), `tests/db_pg/test_chat_messages_turn_id_pg.py` (new), `tests/test_chat_usage_turns.py`/`tests/test_chat_manager.py` (turn_id passed), `tests/test_agent_memory_provenance.py` (new), `tests/test_agnes_llm_calls_internal_table.py` (new, modelled on `tests/test_agnes_turns_internal_table.py`), `tests/test_audit_retention.py` (extend), `tests/db_pg/test_alembic_roundtrip.py`, `tests/test_repository_registry.py`, `tests/test_repository_registry_pg_first_ratchet.py`, `tests/db_pg/test_repo_module_pg_first_ratchet.py`, `tests/db_pg/test_repo_method_parity.py`, `tests/test_backend_split_guard.py`, `tests/test_db_schema_version_frozen.py`

**Interfaces:**
- Consumes (Tasks 1, 4): `LlmCallRecord.to_row()` column set, `read_turn(session_id) -> TurnRecord`, `LiveSession.turn_id`.
- Produces `src.repositories.llm_calls_pg.LlmCallsPgRepository(engine)`:
  - `insert_batch(rows: Iterable[dict]) -> int` — columns = the record's `to_row()` keys; `priced_as` written as JSONB; `ON CONFLICT (id) DO NOTHING`; chunked 500.
  - `list_calls(*, session_id=None, turn_id=None, job_id=None, user_id=None, limit=100, before: datetime | None = None) -> list[dict]` — newest first by `created_at`, `before` is the cursor (`created_at < :before`), timestamps ISO strings, `priced_as` a dict.
  - `cost_summary(*, since: datetime | None, by: str) -> list[dict]` — `by` ∈ `{"workload", "agent", "user", "model", "purpose"}` (anything else → `ValueError`); each row `{key, calls, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, cost_usd (float), priced_models (sorted list of distinct COALESCE(model_response, model_requested))}` ordered by `cost_usd DESC`; the `key` is the group value (may be `None`).
  - `prune_older_than(days: int) -> int`, `count() -> int`.
- Produces `src.repositories.chat_message_feedback_pg.ChatMessageFeedbackPgRepository(engine)`:
  - `upsert(*, session_id, turn_id, user_id, verdict, comment=None, message_id=None) -> dict` — `INSERT ... ON CONFLICT (turn_id, user_id) DO UPDATE SET verdict, comment, message_id, updated_at`; returns the row.
  - `list_feedback(*, since: datetime | None = None, verdict: str | None = None, limit: int = 100) -> list[dict]` newest first.
  - `get(turn_id, user_id) -> dict | None`, `prune_older_than(days) -> int` (not wired to retention; exists for symmetry — skip if it adds nothing).
- Produces factories `src.repositories.llm_calls_repo()`, `src.repositories.chat_message_feedback_repo()` (PG-only entries `"llm_calls"`, `"chat_message_feedback"`).
- Produces `connectors.internal.access.INTERNAL_TABLES` entry `agnes_llm_calls` (`source_table="llm_calls"`, `filter_column="user_id"`, `filter_kind="user_id"`, column descriptions for every column) and `PG_ONLY_INTERNAL_TABLE_IDS |= {"agnes_llm_calls"}`.
- Produces `app.instance_config.get_llm_calls_retention_days() -> int` and the `"llm_calls"` trail in `src.audit_retention._TRAIL_PRUNERS` (a `RequiresPostgresBackend` inside the pruner returns 0).

- [ ] **Step 1: Models + migration**

`src/models/llm_observability.py`:

```python
"""``llm_calls`` and ``chat_message_feedback`` — the LLM observability ledger
and the chat quality signal (design 2026-09-08, §3.7).

PG-ONLY (A3 PG-first ratchet): both tables landed after the DuckDB app-state
backend was frozen — no ``src/db.py`` step, no DuckDB repository sibling.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class LlmCall(Base):
    """One LLM call, from every call site: who / for what / in which turn / at
    what price. Written by the chat broker (``kind='completion'``) and by
    ``trace_generation`` (``kind='generation'``); ``trace_id``/``span_id``
    join it to the exported span when export is on."""

    __tablename__ = "llm_calls"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    workload: Mapped[str | None] = mapped_column(String, nullable=True)
    purpose: Mapped[str | None] = mapped_column(String, nullable=True)
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    turn_id: Mapped[str | None] = mapped_column(String, nullable=True)
    user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String, nullable=True)
    job_id: Mapped[str | None] = mapped_column(String, nullable=True)
    subject_id: Mapped[str | None] = mapped_column(String, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String, nullable=True)
    span_id: Mapped[str | None] = mapped_column(String, nullable=True)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    upstream: Mapped[str] = mapped_column(String, nullable=False)
    model_requested: Mapped[str | None] = mapped_column(String, nullable=True)
    model_response: Mapped[str | None] = mapped_column(String, nullable=True)
    input_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"), nullable=False)
    output_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"), nullable=False)
    cache_read_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"), nullable=False)
    cache_creation_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"), nullable=False)
    cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), server_default=text("0"), nullable=False)
    priced_as: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    error_type: Mapped[str | None] = mapped_column(String, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_chars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_chars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    stream_complete: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    __table_args__ = (
        Index("idx_llm_calls_created", "created_at"),
        Index("idx_llm_calls_session_time", "session_id", "created_at"),
        Index("idx_llm_calls_turn", "turn_id"),
        Index("idx_llm_calls_user_time", "user_id", "created_at"),
        Index("idx_llm_calls_agent_time", "agent_id", "created_at"),
        Index("idx_llm_calls_workload_time", "workload", "created_at"),
    )


class ChatMessageFeedback(Base):
    """Thumbs up/down (plus an optional comment) on one chat turn, one row per
    ``(turn_id, user_id)`` — a second submit updates it."""

    __tablename__ = "chat_message_feedback"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    turn_id: Mapped[str] = mapped_column(String, nullable=False)
    message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    verdict: Mapped[str] = mapped_column(String, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"))

    __table_args__ = (
        UniqueConstraint("turn_id", "user_id", name="uq_chat_message_feedback_turn_user"),
        Index("idx_chat_message_feedback_session", "session_id"),
        Index("idx_chat_message_feedback_created", "created_at"),
    )
```

`src/models/chat.py` `ChatMessage`: add `turn_id: Mapped[str | None] = mapped_column(String, nullable=True)` (comment: the chat turn that produced the row — the same id `usage_turns.turn_uuid` and `llm_calls.turn_id` carry; PG-only, migration 0115) and `Index("idx_chat_messages_turn", "turn_id")` in `__table_args__`. `src/models/agents.py` `AgentMemory`: `source_turn_id`, `source_message_id` (both `String`, nullable). `src/models/__init__.py`: import `LlmCall, ChatMessageFeedback` and add to `__all__`.

`migrations/versions/0115_llm_observability.py` (`down_revision = "0114_corpus_chunks_tsv"`): `op.create_table("llm_calls", ...)` + the six indexes; `op.create_table("chat_message_feedback", ...)` + unique + two indexes; `op.add_column("chat_messages", sa.Column("turn_id", sa.String(), nullable=True))` + `op.create_index("idx_chat_messages_turn", "chat_messages", ["turn_id"])`; `op.add_column("agent_memories", sa.Column("source_turn_id", ...))`, `op.add_column("agent_memories", sa.Column("source_message_id", ...))`. `downgrade()` is the exact inverse. Docstring names the design and the PG-only rule (mirror `0092`'s wording).

Run: `.venv/bin/pytest tests/db_pg/test_alembic_roundtrip.py tests/db_pg/test_alembic_skeleton.py tests/test_db_schema_version_frozen.py -q` → PASS (drift test empty).

- [ ] **Step 2: PG repos + registry, TDD**

`tests/db_pg/test_llm_calls_pg.py` (fixture like `tests/db_pg/test_semantic_feedback_pg.py::repo` — alembic upgrade head on `pg_engine`, then `LlmCallsPgRepository(pg_engine)`):

```python
from datetime import datetime, timedelta, timezone

from src.observability.llm_context import LlmCallContext
from src.observability.llm_record import build_record

_USAGE = {"input_tokens": 100, "output_tokens": 10, "cache_read_tokens": 400, "cache_creation_tokens": 5}


def _row(**kw):
    ctx = LlmCallContext(**{k: kw.pop(k) for k in list(kw) if k in LlmCallContext.__dataclass_fields__})
    return build_record(kind=kw.pop("kind", "generation"), context=ctx, provider="anthropic", upstream="anthropic",
                        model_requested=kw.pop("model", "claude-haiku-4-5"), model_response=None, usage=_USAGE,
                        latency_ms=7, status="ok", **kw).to_row()


def test_insert_batch_is_idempotent_on_id(repo):
    row = _row(workload="chat", session_id="s1", turn_id="t1", user_id="u1")
    assert repo.insert_batch([row]) == 1
    assert repo.insert_batch([row]) == 0
    assert repo.count() == 1


def test_list_calls_filters_and_pages_newest_first(repo):
    now = datetime.now(timezone.utc)
    rows = [_row(workload="chat", session_id="s1", turn_id="t1", created_at=now - timedelta(minutes=i)) for i in range(3)]
    rows.append(_row(workload="extraction", job_id="j1", created_at=now))
    repo.insert_batch(rows)
    got = repo.list_calls(session_id="s1", limit=2)
    assert [r["turn_id"] for r in got] == ["t1", "t1"] and got[0]["created_at"] > got[1]["created_at"]
    older = repo.list_calls(session_id="s1", before=datetime.fromisoformat(got[-1]["created_at"]))
    assert len(older) == 1
    assert [r["job_id"] for r in repo.list_calls(job_id="j1")] == ["j1"]
    assert repo.list_calls(turn_id="t1")[0]["priced_as"]["price_key"] == "claude-haiku-4-5"


def test_cost_summary_groups_and_sums(repo):
    repo.insert_batch([
        _row(workload="chat", user_id="u1", model="claude-haiku-4-5"),
        _row(workload="chat", user_id="u2", model="claude-sonnet-5"),
        _row(workload="builder", user_id="u1", purpose="entity_builder_turn"),
    ])
    by_workload = {r["key"]: r for r in repo.cost_summary(since=None, by="workload")}
    assert by_workload["chat"]["calls"] == 2 and by_workload["builder"]["calls"] == 1
    assert by_workload["chat"]["cache_read_tokens"] == 800
    assert sorted(by_workload["chat"]["priced_models"]) == ["claude-haiku-4-5", "claude-sonnet-5"]
    assert isinstance(by_workload["chat"]["cost_usd"], float) and by_workload["chat"]["cost_usd"] > 0
    assert {r["key"] for r in repo.cost_summary(since=None, by="user")} == {"u1", "u2"}
    assert {r["key"] for r in repo.cost_summary(since=None, by="model")} == {"claude-haiku-4-5", "claude-sonnet-5"}
    assert {r["key"] for r in repo.cost_summary(since=None, by="purpose")} >= {"entity_builder_turn"}
    since = datetime.now(timezone.utc) + timedelta(minutes=1)
    assert repo.cost_summary(since=since, by="workload") == []
    import pytest
    with pytest.raises(ValueError):
        repo.cost_summary(since=None, by="user_id; DROP TABLE llm_calls")


def test_prune_older_than(repo):
    old = datetime.now(timezone.utc) - timedelta(days=40)
    repo.insert_batch([_row(created_at=old), _row()])
    assert repo.prune_older_than(30) == 1 and repo.count() == 1
```

`tests/db_pg/test_chat_message_feedback_pg.py`:

```python
def test_upsert_is_one_row_per_turn_and_user(repo):
    a = repo.upsert(session_id="s1", turn_id="t1", user_id="u1", verdict="down", comment="wrong number")
    b = repo.upsert(session_id="s1", turn_id="t1", user_id="u1", verdict="up", comment=None)
    assert a["id"] == b["id"] and b["verdict"] == "up" and b["comment"] is None
    assert b["updated_at"] >= a["created_at"]
    repo.upsert(session_id="s1", turn_id="t1", user_id="u2", verdict="down")
    assert len(repo.list_feedback()) == 2
    assert [r["user_id"] for r in repo.list_feedback(verdict="down")] == ["u2"]
    assert repo.get("t1", "u1")["verdict"] == "up" and repo.get("t1", "nobody") is None
```

Implement both repos (style: `sa.text` with named binds, reads `engine.connect()`, writes `engine.begin()`; `priced_as` via `CAST(:priced_as AS JSONB)` with `json.dumps`; `cost_summary`'s group expression from a dict `_GROUP_EXPR = {"workload": "workload", "agent": "agent_id", "user": "user_id", "model": "COALESCE(model_response, model_requested)", "purpose": "purpose"}`; `priced_models` via `array_agg(DISTINCT COALESCE(model_response, model_requested))` filtered of NULLs; `Decimal` → `float`). Registry:

```python
    "llm_calls": {PG: ("src.repositories.llm_calls_pg", "LlmCallsPgRepository")},
    "chat_message_feedback": {PG: ("src.repositories.chat_message_feedback_pg", "ChatMessageFeedbackPgRepository")},
```

plus `__all__` entries and `def llm_calls_repo() -> Any: return _build("llm_calls")` / `chat_message_feedback_repo()`.

Run: `.venv/bin/pytest tests/db_pg/test_llm_calls_pg.py tests/db_pg/test_chat_message_feedback_pg.py tests/test_repository_registry.py tests/test_repository_registry_pg_first_ratchet.py tests/db_pg/test_repo_module_pg_first_ratchet.py tests/test_backend_split_guard.py -q` → PASS

- [ ] **Step 3: `chat_messages.turn_id` end to end**

- `app/chat/types.py`: `ChatMessage.turn_id: Optional[str] = None` (after `cache_creation_tokens`, documented like it).
- `src/repositories/chat_messages_pg.py`: `append_message(..., turn_id: Optional[str] = None)` → INSERT column + bind + returned dataclass; `list_messages` / `list_recent_messages` SELECT `turn_id` and pass it to `ChatMessage`.
- `app/chat/persistence.py::append_message(..., turn_id=None)`: forward to `_messages_pg` when present; on the DuckDB path do not write it (comment: PG-only column, migration 0115 — the same accept-and-drop as the cache columns).
- `app/chat/manager.py` ~2685: `turn_id=live.turn_id`.
- `tests/db_pg/test_chat_messages_turn_id_pg.py`: through `ChatMessagePgRepository(pg_engine)` after `alembic upgrade head`, `append_message(..., turn_id="t1")` then `list_messages(...)[0].turn_id == "t1"`; and the DuckDB `ChatRepository` (as in `tests/test_chat_api.py::_make_app`) accepts `turn_id="t1"` and returns a message whose `turn_id is None`.
- `tests/test_chat_usage_turns.py` or `tests/test_chat_turn_spans.py`: assert the persisted assistant row's `turn_id` equals `live.turn_id` when the repo is PG-backed (a recording fake for `_messages_pg` is enough: `monkeypatch.setattr(manager._repo, "_messages_pg", _Recording())`).

- [ ] **Step 4: Memory provenance**

- Both `agent_memories` repos: `create(..., source_turn_id: Optional[str] = None, source_message_id: Optional[str] = None)`; PG writes the two columns; DuckDB ignores them (comment). Extend `tests/db_pg/test_agent_memories_contract.py`:

```python
def test_create_accepts_turn_provenance_on_both_backends(repo):
    repo.create(id="m1", agent_id="a1", owner_user_id="u1", content="x", source_session_id="c1",
                source_turn_id="t1", source_message_id="msg_1")
    row = repo.get("m1")
    assert row["source_session_id"] == "c1"
    # PG stores the provenance; the frozen DuckDB side accepts and drops it.
    assert row.get("source_turn_id") in ("t1", None) and row.get("source_message_id") in ("msg_1", None)


def test_pg_stores_turn_provenance(repo):
    if not hasattr(repo, "_engine"):
        pytest.skip("PG side only")
    repo.create(id="m2", agent_id="a1", owner_user_id="u1", content="x", source_session_id="c1",
                source_turn_id="t1", source_message_id="msg_1")
    assert repo.get("m2")["source_turn_id"] == "t1"
```

- `app/api/agent_memory.py::remember`: `turn = read_turn(session_id)`; `repo.create(..., source_turn_id=turn.turn_id if turn else None, source_message_id=turn.message_id if turn else None)`; the success `_audit(...)` `extra` gains `"turn_id": turn.turn_id if turn else None`. `tests/test_agent_memory_provenance.py`: publish a `TurnRecord` for the session, call the endpoint the way `tests/test_agent_memory*.py` already does (reuse its fixtures), assert the repo received the two ids and the audit row's `params["turn_id"]`.

- [ ] **Step 5: `agnes_llm_calls` internal table**

`connectors/internal/access.py` — append an `InternalTable(registry_id="agnes_llm_calls", source_table="llm_calls", filter_column="user_id", filter_kind="user_id", display_name="Agnes LLM calls", description="One row per LLM call across every workload — chat, agent API, builders, extraction, corporate memory and the rest — with the four token kinds, the USD cost as priced at write time and the ids that join it to a chat turn, a worker job or an exported trace. Your own rows only (admins see all). Postgres-backed instances only. Server-side only; query with `agnes query`.", column_descriptions={...every column of the table, one line each...})`. `connectors/internal/registry.py`: add `"agnes_llm_calls"` to `PG_ONLY_INTERNAL_TABLE_IDS` and mention the table in `USAGE_PACKAGE_LONG_DESCRIPTION` ("seven tables" → count it) and `USAGE_PACKAGE_DESCRIPTION`. `tests/test_agnes_llm_calls_internal_table.py`: copy the DuckDB-backend half of `tests/test_agnes_turns_internal_table.py` (declaration present, `is_internal_table`, NOT registered on DuckDB, pruned if left behind, `find_internal_refs` sees it). Update `tests/test_internal_table_descriptions.py` if it pins the table count/list.

- [ ] **Step 6: Retention**

`app/instance_config.py`: `get_llm_calls_retention_days()` (copy of `get_llm_usage_retention_days`, key `retention.llm_calls_days`). `src/audit_retention.py`:

```python
def _prune_llm_calls(days: int, repo: Optional[Any] = None) -> int:
    if repo is None:
        from src.repositories import RequiresPostgresBackend, llm_calls_repo

        try:
            repo = llm_calls_repo()
        except RequiresPostgresBackend:
            return 0  # the ledger is Postgres-only; nothing to prune on DuckDB
    return repo.prune_older_than(days)
```

registered as `"llm_calls"` in `_TRAIL_PRUNERS`; `app/api/admin.py::run_retention_prune` adds `"llm_calls": get_llm_calls_retention_days()`; `config/instance.yaml.example` retention block gains `#   llm_calls_days: 0                 # llm_calls rows (the LLM call ledger), by created_at.` Extend `tests/test_audit_retention.py` (the trail appears in `run_retention_sweep`'s result, `0` skips, an injected repo is called with the days, DuckDB backend → `{"pruned": 0, "skipped": False}` without raising).

- [ ] **Step 7: Ledger end to end on Postgres**

`tests/db_pg/test_llm_calls_pg.py` gains one test that drives the real sink: with `AGNES_DB_URL` pointed at `pg_engine` (see `_make_pg_repo` in the contract tests for the env + `db_pg.dispose()` dance), `with trace_generation(provider="anthropic", model="claude-haiku-4-5", purpose="e2e") as cap: cap.set_tokens(5, 1)` → `llm_calls_repo().list_calls(limit=1)[0]["purpose"] == "e2e"`; and `usage_accumulator.add_call(build_record(...).to_row()); usage_accumulator.flush()` → the row is there.

- [ ] **Step 8: Run + commit**

Run: the files listed under **Test** → PASS; plus `tests/db_pg/test_repo_method_parity.py` and `tests/test_chat_api.py`.

```bash
git add migrations/versions/0115_llm_observability.py src/models/llm_observability.py src/models/chat.py src/models/agents.py \
  src/models/__init__.py src/repositories/llm_calls_pg.py src/repositories/chat_message_feedback_pg.py src/repositories/__init__.py \
  src/repositories/chat_messages_pg.py app/chat/persistence.py app/chat/types.py app/chat/manager.py \
  src/repositories/agent_memories.py src/repositories/agent_memories_pg.py app/api/agent_memory.py \
  connectors/internal/access.py connectors/internal/registry.py src/audit_retention.py app/instance_config.py app/api/admin.py \
  config/instance.yaml.example tests/db_pg/test_llm_calls_pg.py tests/db_pg/test_chat_message_feedback_pg.py \
  tests/db_pg/test_agent_memories_contract.py tests/db_pg/test_chat_messages_turn_id_pg.py tests/test_agent_memory_provenance.py \
  tests/test_agnes_llm_calls_internal_table.py tests/test_audit_retention.py <other touched tests>
git commit -m "observability: llm_calls and chat_message_feedback tables, PG-only repositories, turn provenance on messages and memories, agnes_llm_calls, retention"
```

---

### Task 7: Read surfaces — `llm-cost`, `llm-calls`, `feedback` endpoints, CLI, admin page section

**Files:**
- Modify: `app/api/admin_usage.py` (three GETs after `chat_cost`; `chat_cost` notes gain one line)
- Modify: `cli/commands/admin_usage.py` (`llm-cost`, `llm-calls`, `feedback`)
- Modify: `app/web/templates/admin_usage.html` ("LLM cost" section + JS)
- Modify: `src/audit_posture.py` (`READ_POSTURE` ×3), `src/audit_events.py` (`usage.llm_cost`, `usage.llm_calls`, `usage.feedback_list` — category `read`), `tests/test_documentation_api_triple_surface.py` (`_EXEMPT` ×3), `tests/db_pg/test_endpoints_smoke.py` (`KNOWN_UNTESTED` ×3), `tests/db_pg/test_get_status_parity_sweep.py` (`_PG_ONLY_ROUTE_EXEMPTIONS` ×3), `docs/api-reference.md` (three paths under `/api/admin/telemetry`), `app/initial_workspace_default/.claude/skills/agnes-web-guide/references/admin-pages.md` (the `/admin/telemetry` line mentions the LLM cost section)
- Test: `tests/test_admin_llm_cost_api.py` (new), `tests/db_pg/test_admin_llm_cost_pg.py` (new), `tests/test_cli_admin_usage_llm.py` (new), `tests/test_admin_usage_page_llm_cost.py` (new), and the guards: `tests/test_audit_read_posture.py`, `tests/test_audit_catalog.py`, `tests/test_documentation_api_triple_surface.py`, `tests/test_api_docs_coverage.py`, `tests/test_route_auth_guard.py`, `tests/test_design_system_contract.py`, `tests/test_web_guide_skill_sync.py`

**Interfaces:**
- Consumes (Task 6): `llm_calls_repo().cost_summary(since, by)`, `.list_calls(...)`, `chat_message_feedback_repo().list_feedback(...)`.
- Produces:
  - `GET /api/admin/telemetry/llm-cost?window=1d|7d|30d|all&by=workload|agent|user|model|purpose` → `{"window", "by", "groups": [{"key", "calls", "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens", "cost_usd", "cached_input_share", "priced_models"}], "totals": {same numeric keys + "cached_input_share"}, "notes": [str]}`; 400 on a bad `window`/`by`; 501 typed on DuckDB (repo resolved via `Depends(_llm_calls_repo)`).
  - `GET /api/admin/telemetry/llm-calls?session_id=&turn_id=&job_id=&user_id=&limit=100&before=<iso>` → `{"rows": [...ledger rows...], "next_before": <iso of the last row> | null, "notes": [...]}`; 400 when none of the three ids is given.
  - `GET /api/admin/telemetry/feedback?window=7d&verdict=up|down&limit=100` → `{"window", "verdict", "rows": [...]}`.
  - CLI: `agnes admin usage llm-cost [--window 7d] [--by workload] [--json]`, `agnes admin usage llm-calls (--session-id|--turn-id|--job-id) [--limit 50] [--json]`, `agnes admin usage feedback [--window 7d] [--verdict up|down] [--limit 50] [--json]` — each prints a table or the raw JSON, uses `_handle_error`, and on an empty result prints what to do next (`llm-calls`: "no calls recorded for that id — is this instance Postgres-backed? see docs/observability.md"; on a 501 body `requires_postgres_backend` print that hint).
  - Page: a `<section class="obs-table-wrap" id="llmcost-section" aria-label="LLM cost">` under the query-telemetry section with its own window `<select id="llmcost-window">` (1d/7d/30d/all) and a table (workload, calls, input, output, cache read, cache write, cached %, cost USD, priced as), totals row, and a note line; a 501 renders "LLM cost needs the Postgres app-state backend." in the empty row; loaded by `loadLlmCost()` on page load and on select change.

- [ ] **Step 1: Failing endpoint tests**

`tests/test_admin_llm_cost_api.py` (DuckDB app via `seeded_app` — admin gate + typed 501 + validation):

```python
def test_llm_cost_requires_admin(seeded_client_and_tokens):
    client, tokens = ...  # per the seeded fixture's shape
    r = client.get("/api/admin/telemetry/llm-cost", headers=_auth(tokens["analyst"]))
    assert r.status_code == 403


def test_llm_cost_answers_typed_501_on_duckdb(...):
    r = client.get("/api/admin/telemetry/llm-cost", headers=_auth(tokens["admin"]))
    assert r.status_code == 501 and r.json()["error"] == "requires_postgres_backend"


def test_llm_cost_rejects_bad_window_and_by(...):
    # validation happens BEFORE the repo dependency? No — the repo is a dependency, so on DuckDB a bad
    # window still 501s. Assert the 400s on the PG side instead (tests/db_pg/test_admin_llm_cost_pg.py).


def test_llm_calls_requires_one_id(...):  # PG side too
```

`tests/db_pg/test_admin_llm_cost_pg.py` (PG client via `build_seeded_client("pg", ...)` from `tests/db_pg/_parity_sweep_util.py`, or the pattern `tests/db_pg/test_semantic_feedback_pg.py` uses for endpoints): seed six `llm_calls` rows through `llm_calls_repo().insert_batch` (two workloads, two users, two models, one 40 days old), then:
- `by=workload` → two groups, sums match, `cached_input_share` = `cache_read / (input + cache_read + cache_creation)` rounded 4, `priced_models` lists the models, `totals.cost_usd` equals the sum, `window=30d` excludes the old row, `window=all` includes it;
- `by=nonsense` → 400; `window=2w` → 400;
- `llm-calls?session_id=s1&limit=2` → 2 rows newest first + `next_before`; `llm-calls?before=<next_before>&session_id=s1` → the rest; `llm-calls` with no id → 400;
- `feedback` after two `chat_message_feedback_repo().upsert(...)` → both rows; `verdict=down` → one; a non-admin → 403.

- [ ] **Step 2: Implement the endpoints**

```python
_LLM_COST_GROUPS = ("workload", "agent", "user", "model", "purpose")


def _llm_calls_repo() -> Any:
    """Resolved AS A DEPENDENCY so a DuckDB-backed instance answers the typed
    501 before any parameter is validated (the semantic-feedback pattern)."""
    from src.repositories import llm_calls_repo

    return llm_calls_repo()


def _feedback_repo() -> Any:
    from src.repositories import chat_message_feedback_repo

    return chat_message_feedback_repo()


@router.get("/llm-cost")
def llm_cost(
    window: str = Query("7d", description="1d|7d|30d|all"),
    by: str = Query("workload", description="workload|agent|user|model|purpose"),
    admin: dict = Depends(require_admin),
    repo: Any = Depends(_llm_calls_repo),
):
    """Cost of every LLM call this instance made, by workload / agent / user /
    model / purpose — measured on-instance from ``llm_calls`` (priced at write
    time, the rates stored beside each row), so the same figure the exported
    span carries. ``chat-cost`` stays the per-session chat view; this is the
    cross-workload one. Postgres-backed instances only (typed 501 otherwise).
    """
    if window not in _CHAT_COST_WINDOWS:
        raise HTTPException(status_code=400, detail=f"window must be one of {sorted(_CHAT_COST_WINDOWS)}")
    if by not in _LLM_COST_GROUPS:
        raise HTTPException(status_code=400, detail=f"by must be one of {list(_LLM_COST_GROUPS)}")
    days = _CHAT_COST_WINDOWS[window]
    since = datetime.now(timezone.utc) - timedelta(days=days) if days else None
    groups = []
    totals = {k: 0 for k in ("calls", "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")}
    totals["cost_usd"] = 0.0
    for g in repo.cost_summary(since=since, by=by):
        read_input = g["input_tokens"] + g["cache_read_tokens"] + g["cache_creation_tokens"]
        groups.append({**g, "cost_usd": round(float(g["cost_usd"]), 6),
                       "cached_input_share": round(g["cache_read_tokens"] / read_input, 4) if read_input else None})
        for k in totals:
            totals[k] += g[k] if k != "cost_usd" else float(g[k])
    totals["cost_usd"] = round(totals["cost_usd"], 6)
    read_input = totals["input_tokens"] + totals["cache_read_tokens"] + totals["cache_creation_tokens"]
    totals["cached_input_share"] = round(totals["cache_read_tokens"] / read_input, 4) if read_input else None
    notes = ["cost_usd is priced at write time with the rates stored on each row (priced_as); "
             "a group whose priced_models include an unknown model was priced at the default tier."]
    if not groups:
        notes.append("No LLM calls recorded in this window.")
    return {"window": window, "by": by, "groups": groups, "totals": totals, "notes": notes}
```

`llm_calls` and `feedback` follow the same shape (`before` parsed with `datetime.fromisoformat`, 400 on a bad value; `limit` `Query(100, ge=1, le=1000)`). `chat_cost`'s `notes` gains: `"Chat only. For every workload (builders, extraction, corporate memory, …) see GET /api/admin/telemetry/llm-cost."`.

Bookkeeping (append-only, one entry each):
- `READ_POSTURE`: `"GET /api/admin/telemetry/llm-cost": "usage.llm_cost"`, `"GET /api/admin/telemetry/llm-calls": "usage.llm_calls"`, `"GET /api/admin/telemetry/feedback": "usage.feedback_list"`.
- `CATALOG` (read): `usage.llm_cost` ("An admin read the cross-workload LLM cost summary."), `usage.llm_calls` ("An admin read LLM call ledger rows for a session, turn or job."), `usage.feedback_list` ("An admin read chat feedback rows.").
- `_EXEMPT` ×3 with the chat-cost reasoning (admin-only cost/feedback telemetry; CLI surface `agnes admin usage llm-cost|llm-calls|feedback`; no MCP tool, an agent has no business reading every user's spend or feedback).
- `KNOWN_UNTESTED` ×3 with the reason "admin-gated, PG-only; behaviourally covered in tests/db_pg/test_admin_llm_cost_pg.py (grouping, paging, 400s) and tests/test_admin_llm_cost_api.py (admin gate, typed 501)".
- `_PG_ONLY_ROUTE_EXEMPTIONS` ×3: "reads `llm_calls` / `chat_message_feedback`, PG-only tables (LLM observability design 2026-09-08)".
- `docs/api-reference.md`: add the three paths to the `/api/admin/telemetry` list.

- [ ] **Step 3: CLI**

In `cli/commands/admin_usage.py`, after `chat_cost`:

```python
@app.command("llm-cost")
def llm_cost(
    window: str = typer.Option("7d", "--window", help="1d|7d|30d|all"),
    by: str = typer.Option("workload", "--by", help="workload|agent|user|model|purpose"),
    json_out: bool = typer.Option(False, "--json", help="Emit raw JSON instead of a table."),
):
    """Measured LLM cost across every workload, priced at write time."""
    client = get_client(timeout=60)
    try:
        resp = client.get("/api/admin/telemetry/llm-cost", params={"window": window, "by": by})
    except Exception as e:
        typer.echo(f"[err] cannot reach server: {e}", err=True)
        raise typer.Exit(1)
    _handle_llm_error(resp, "llm-cost")
    data = resp.json()
    if json_out:
        typer.echo(json.dumps(data, indent=2, default=str))
        return
    t = data.get("totals") or {}
    typer.echo(f"LLM cost — window {data.get('window')} by {data.get('by')}: ${t.get('cost_usd', 0):.4f} over {t.get('calls', 0)} calls")
    groups = data.get("groups") or []
    if not groups:
        typer.echo("  (no LLM calls recorded in this window — the ledger fills as calls happen; see docs/observability.md)")
    else:
        typer.echo(f"  {'group':<28} {'calls':>6} {'in':>10} {'out':>8} {'cache rd':>10} {'cache wr':>9} {'cached%':>8} {'cost':>10}")
        for g in groups:
            share = g.get("cached_input_share")
            typer.echo(f"  {str(g.get('key') or '-')[:27]:<28} {g.get('calls', 0):>6} {g.get('input_tokens', 0):>10,} "
                       f"{g.get('output_tokens', 0):>8,} {g.get('cache_read_tokens', 0):>10,} {g.get('cache_creation_tokens', 0):>9,} "
                       f"{(f'{share:.1%}' if isinstance(share, (int, float)) else 'n/a'):>8} ${g.get('cost_usd', 0):>9.4f}")
    for note in data.get("notes") or []:
        typer.echo(f"  note: {note}")
```

with `_handle_llm_error(resp, context)` = `_handle_error` plus: a 501 whose body has `error == "requires_postgres_backend"` prints `[err] {context} needs the Postgres app-state backend (see docs/migrations.md)` and exits 1. `llm-calls` (`--session-id`, `--turn-id`, `--job-id`, `--user-id`, `--limit 50`, `--before`, `--json`; refuses with a hint when none of the ids is given; table columns time / kind / workload / purpose / model / in / out / cache rd / cost / status) and `feedback` (`--window`, `--verdict`, `--limit`, `--json`; columns time / session / turn / user / verdict / comment-length — never the comment text in the table, it is in `--json`). `tests/test_cli_admin_usage_llm.py`: use the CliRunner pattern from the existing CLI tests (e.g. `tests/test_cli_admin_usage*.py` or `tests/test_cli_api_parity.py`) with a fake client; assert the request path + params for each command and the 501 hint.

- [ ] **Step 4: Admin page section**

In `app/web/templates/admin_usage.html` after the query-telemetry `</section>`:

```html
  {# LLM cost (LLM observability design 2026-09-08 §3.4): every workload's
     spend from the llm_calls ledger — priced at write time, so this and the
     exported spans carry one figure. Postgres-backed instances only. #}
  <section class="obs-table-wrap" id="llmcost-section" style="margin-top:20px;" aria-label="LLM cost">
    <div class="obs-section-head">
      <strong>LLM cost — by workload</strong>
      <label class="obs-window">
        <span class="obs-label">Window</span>
        <select id="llmcost-window" class="obs-select ds-dropdown-native">
          <option value="1d">Last 24h</option>
          <option value="7d" selected>Last 7d</option>
          <option value="30d">Last 30d</option>
          <option value="all">All time</option>
        </select>
      </label>
      <span id="llmcost-totals" class="obs-row-count">—</span>
    </div>
    <table class="obs-table">
      <thead><tr>
        <th>Workload</th><th class="obs-num">Calls</th><th class="obs-num">Input</th><th class="obs-num">Output</th>
        <th class="obs-num">Cache read</th><th class="obs-num">Cache write</th><th class="obs-num">Cached %</th>
        <th class="obs-num">Cost (USD)</th><th>Priced as</th>
      </tr></thead>
      <tbody id="llmcost-rows"><tr><td colspan="9" class="obs-empty">Loading…</td></tr></tbody>
    </table>
    <p class="obs-subtitle" id="llmcost-note" style="padding:8px 14px 12px; margin:0;">Also: <code>agnes admin usage llm-cost</code>, and the <code>agnes_llm_calls</code> table in the <code>agnes-usage</code> package.</p>
  </section>
```

(`.obs-section-head` = the inline style the query-telemetry header uses, moved into the page's `<style>` block as a class so both headers share it — no raw hex: use `var(--border, …)` tokens exactly as the existing rules do.) JS in the page's IIFE:

```js
  async function loadLlmCost() {
    const win = document.getElementById('llmcost-window').value;
    const rows = document.getElementById('llmcost-rows');
    const totals = document.getElementById('llmcost-totals');
    const r = await fetch(`/api/admin/telemetry/llm-cost?window=${encodeURIComponent(win)}&by=workload`, { credentials: 'same-origin' });
    if (r.status === 501) {
      rows.innerHTML = '<tr><td colspan="9" class="obs-empty">LLM cost needs the Postgres app-state backend.</td></tr>';
      totals.textContent = '—';
      return;
    }
    if (!r.ok) { rows.innerHTML = `<tr><td colspan="9" class="obs-empty">Could not load (${r.status}).</td></tr>`; return; }
    const data = await r.json();
    const t = data.totals || {};
    totals.textContent = `$${(t.cost_usd || 0).toFixed(4)} over ${t.calls || 0} calls`;
    if (!(data.groups || []).length) { rows.innerHTML = '<tr><td colspan="9" class="obs-empty">No LLM calls in this window.</td></tr>'; return; }
    rows.innerHTML = data.groups.map(g => `<tr>
      <td>${escapeHtml(g.key || '—')}</td><td class="obs-num">${g.calls}</td>
      <td class="obs-num">${(g.input_tokens||0).toLocaleString()}</td><td class="obs-num">${(g.output_tokens||0).toLocaleString()}</td>
      <td class="obs-num">${(g.cache_read_tokens||0).toLocaleString()}</td><td class="obs-num">${(g.cache_creation_tokens||0).toLocaleString()}</td>
      <td class="obs-num">${g.cached_input_share == null ? 'n/a' : (g.cached_input_share*100).toFixed(1)+'%'}</td>
      <td class="obs-num">$${(g.cost_usd||0).toFixed(4)}</td><td>${escapeHtml((g.priced_models||[]).join(', '))}</td></tr>`).join('');
  }
  document.getElementById('llmcost-window').addEventListener('change', loadLlmCost);
  loadLlmCost();
```

(`escapeHtml` already exists in the page.) `tests/test_admin_usage_page_llm_cost.py`: render `/admin/telemetry` as admin (pattern from `tests/test_web_admin_nav.py` or any admin page test) and assert `id="llmcost-section"`, `llm-cost?window=` and the 501 copy are in the HTML. Web guide: `- \`/admin/telemetry\` — usage telemetry, query telemetry and the LLM cost table (every workload, priced at write time).`

- [ ] **Step 5: Run + commit**

Run: the four new test files + `tests/test_audit_read_posture.py tests/test_audit_catalog.py tests/test_documentation_api_triple_surface.py tests/test_api_docs_coverage.py tests/test_route_auth_guard.py tests/test_design_system_contract.py tests/test_web_guide_skill_sync.py tests/db_pg/test_get_status_parity_sweep.py tests/db_pg/test_endpoints_smoke.py -q` → PASS (the openapi snapshot test may fail until the integrator regenerates; do NOT regenerate in this task).

```bash
git add app/api/admin_usage.py cli/commands/admin_usage.py app/web/templates/admin_usage.html src/audit_posture.py src/audit_events.py \
  tests/test_documentation_api_triple_surface.py tests/db_pg/test_endpoints_smoke.py tests/db_pg/test_get_status_parity_sweep.py \
  docs/api-reference.md app/initial_workspace_default/.claude/skills/agnes-web-guide/references/admin-pages.md \
  tests/test_admin_llm_cost_api.py tests/db_pg/test_admin_llm_cost_pg.py tests/test_cli_admin_usage_llm.py tests/test_admin_usage_page_llm_cost.py
git commit -m "telemetry: llm-cost, llm-calls and feedback admin reads, CLI commands, LLM cost section on the telemetry page"
```

---

### Task 8: Feedback on a chat turn — endpoint, audit, log + span event, web thumbs

**Files:**
- Modify: `app/api/chat.py` (`POST /sessions/{chat_id}/feedback`; `list_messages` returns `turn_id`)
- Modify: `app/web/static/js/chat.js` (`finalizeAssistantMessage` stamps `article.dataset.turnId = frame.turn_id`; the history renderer stamps it from `m.turn_id`; `attachMessageActions` renders thumbs on assistant articles with a turn id; a POST helper)
- Modify: `app/web/static/css/chat.css` (`.msg-feedback`, `.msg-feedback.is-up/.is-down`, `.msg-feedback-comment`)
- Modify: `src/observability/otel.py` (`emit_feedback_span(*, session_id, turn_id, user_id, verdict, has_comment, parent_context) -> None`)
- Modify: `src/audit_posture.py` (`POSTURE`: `"POST /api/chat/sessions/{chat_id}/feedback": "chat.feedback"`), `src/audit_events.py` (`chat.feedback`, category `mutation`), `tests/test_documentation_api_triple_surface.py` (`_EXEMPT`), `tests/db_pg/test_endpoints_smoke.py` (`KNOWN_UNTESTED`), `tests/db_pg/test_mutation_status_parity_sweep.py` (`_PG_ONLY_ROUTE_EXEMPTIONS` if the sweep drives parameterised POST routes — check; otherwise nothing), `docs/api-reference.md` (`/api/chat/sessions/{chat_id}/feedback` under the chat section)
- Test: `tests/test_chat_feedback_api.py` (new — DuckDB app: gate, typed 501, validation), `tests/db_pg/test_chat_feedback_pg.py` (new — upsert through the endpoint, audit row, log record, span event), `tests/test_chat_feedback_ui.py` (new — static assertions on chat.js/chat.css), and the guards `tests/test_audit_route_posture.py`, `tests/test_audit_declared_actions.py`, `tests/test_audit_catalog.py`, `tests/test_documentation_api_triple_surface.py`, `tests/test_api_docs_coverage.py`, `tests/test_chat_api.py`

**Interfaces:**
- Consumes (Tasks 4, 6): `read_turn(session_id)`, `otel.remote_parent_context`, `chat_message_feedback_repo().upsert(...)`, `ChatMessage.turn_id`.
- Produces `POST /api/chat/sessions/{chat_id}/feedback` — body `{"turn_id": str (1–64 chars), "verdict": "up"|"down", "comment": str | null (≤ 2000 chars)}`; gate `require_chat_access` + `_reject_restricted_principal` + the session must be owned by the caller OR the caller must be a live participant (`repo.get_session_participants(chat_id)` with `left_at is None`), else 404 (never 403 — matches the sibling routes); repo via `Depends(_feedback_repo)` so DuckDB answers the typed 501 before validation; response `{"id", "turn_id", "verdict", "comment", "updated_at"}` (200 on update as well — one row per `(turn_id, user_id)`); writes `write_audit(user_email=user["email"], action="chat.feedback", details={"session_id", "turn_id", "verdict"})` — never the comment; logs `logger.info("chat feedback", extra={"event": "chat_feedback", "session_id", "turn_id", "verdict", "has_comment": bool})`; calls `otel.emit_feedback_span(...)` with `parent_context=remote_parent_context(turn.trace_id, turn.span_id)` when `read_turn(chat_id)` returns a record whose `turn_id` equals the body's, else `None`.
- Produces `otel.emit_feedback_span(...)`: a short span `agnes.chat.feedback` (kind `INTERNAL`, attrs `agnes.session_id`, `agnes.turn_id`, `agnes.user_id`, `agnes.verdict`) with one event `agnes.feedback` `{ "agnes.verdict": verdict, "agnes.has_comment": bool }`, ended immediately; no-op when export is off; never raises.
- Produces in the web client: every assistant `<article>` carries `data-turn-id` (live: from `frame.turn_id`; reload: from `m.turn_id`); `attachMessageActions` appends a `.msg-feedback` group (two `<button type="button" class="msg-feedback-btn" data-verdict="up|down" aria-label="Good answer"/"Bad answer">`) only when `article.dataset.turnId` is set; a click POSTs `{turn_id, verdict}` via `fetch(`/api/chat/sessions/${sessionId}/feedback`, {method: "POST", credentials: "same-origin", headers: {"Content-Type": "application/json", ...CSRF header the other POSTs in chat.js use}, body})`, marks the pressed button `is-selected`, and on `down` reveals a one-line `<input class="msg-feedback-comment" maxlength="2000" placeholder="What was wrong? (optional)">` whose Enter/blur re-POSTs with `comment`; a 501 shows the toast "Feedback needs the Postgres app-state backend."; any other failure shows "Couldn't send feedback."

- [ ] **Step 1: Failing endpoint tests (DuckDB side)**

`tests/test_chat_feedback_api.py` — build the app exactly like `tests/test_chat_api.py::_make_app` (import it), create a session via `POST /api/chat/sessions`, then:

```python
def test_feedback_is_501_on_duckdb(client):
    r = client.post(f"/api/chat/sessions/{chat_id}/feedback", json={"turn_id": "t1", "verdict": "up"})
    assert r.status_code == 501 and r.json()["error"] == "requires_postgres_backend"
```

and, with `app.dependency_overrides[_feedback_repo] = lambda: _RecordingRepo()` (a fake with `upsert` returning a row dict):

```python
def test_feedback_upserts_and_audits(client, monkeypatch):
    audits = []
    monkeypatch.setattr("app.api.chat.write_audit", lambda **kw: audits.append(kw))
    r = client.post(..., json={"turn_id": "t1", "verdict": "down", "comment": "wrong number"})
    assert r.status_code == 200 and r.json()["verdict"] == "down"
    assert fake.calls == [dict(session_id=chat_id, turn_id="t1", user_id="user1", verdict="down", comment="wrong number", message_id=None)]
    (row,) = audits
    assert row["action"] == "chat.feedback" and row["details"] == {"session_id": chat_id, "turn_id": "t1", "verdict": "down"}
    assert "wrong number" not in json.dumps(row)


def test_feedback_validates_the_body(client):
    assert client.post(..., json={"turn_id": "t1", "verdict": "meh"}).status_code == 422
    assert client.post(..., json={"turn_id": "", "verdict": "up"}).status_code == 422
    assert client.post(..., json={"turn_id": "t1", "verdict": "up", "comment": "x" * 2001}).status_code == 422


def test_feedback_is_404_for_a_stranger_and_ok_for_a_live_participant(client, ...):
    # switch TEST_USER (the test_chat_api pattern) to bob → 404; add bob as a participant via repo.add_session_participant → 200


def test_feedback_emits_a_log_record_and_a_span_event(client, caplog, otel_exporter):
    from app.chat.turn_context import TurnRecord, publish_turn
    publish_turn(chat_id, TurnRecord(turn_id="t1", trace_id="c" * 32, span_id="d" * 16, started_at="…",
                                     user_id="user1", agent_id=None, surface="web", workload="chat"))
    with caplog.at_level(logging.INFO):
        client.post(..., json={"turn_id": "t1", "verdict": "up"})
    rec = next(r for r in caplog.records if getattr(r, "event", None) == "chat_feedback")
    assert rec.turn_id == "t1" and rec.verdict == "up" and rec.has_comment is False
    (span,) = [s for s in otel_exporter.get_finished_spans() if s.name == "agnes.chat.feedback"]
    assert format(span.context.trace_id, "032x") == "c" * 32 and format(span.parent.span_id, "016x") == "d" * 16
    (event,) = span.events
    assert event.name == "agnes.feedback" and dict(event.attributes)["agnes.verdict"] == "up"
```

`tests/db_pg/test_chat_feedback_pg.py`: the real repo through the endpoint on a PG-backed client — two POSTs for one turn by one user → one row, second verdict wins; `GET /api/admin/telemetry/feedback` (Task 7) lists it for an admin.

- [ ] **Step 2: Implement the endpoint** (in `app/api/chat.py`, after `rename_session`)

```python
class FeedbackBody(BaseModel):
    turn_id: str = Field(min_length=1, max_length=64)
    verdict: Literal["up", "down"]
    comment: Optional[str] = Field(default=None, max_length=2000)


def _feedback_repo() -> Any:
    """Resolved as a dependency: a DuckDB-backed instance answers the typed
    501 before the body is validated (the semantic-feedback pattern)."""
    from src.repositories import chat_message_feedback_repo

    return chat_message_feedback_repo()


def _can_rate(repo, session, user: dict) -> bool:
    if session.user_email == user["email"]:
        return True
    try:
        return any(p.user_email == user["email"] and p.left_at is None for p in repo.get_session_participants(session.id))
    except Exception:
        return False


@router.post("/sessions/{chat_id}/feedback")
async def submit_feedback(
    chat_id: str,
    body: FeedbackBody,
    request: Request,
    user: dict = Depends(require_chat_access),
    feedback_repo: Any = Depends(_feedback_repo),
):
    """Thumbs up/down on one completed turn (LLM observability design §3.5).

    Keyed on the turn, not the message: the client learns ``turn_id`` from
    the frames it already receives, while the assistant row is written after
    the frame is broadcast. One row per ``(turn_id, user_id)`` — a second
    submit updates it. Audited as ``chat.feedback`` with the verdict, never
    the comment.
    """
    _reject_restricted_principal(user, "rate an answer")
    repo = _get_repo(request)
    s = repo.get_session(chat_id)
    if s is None or not _can_rate(repo, s, user):
        raise HTTPException(404)
    comment = (body.comment or "").strip() or None
    row = feedback_repo.upsert(session_id=chat_id, turn_id=body.turn_id, user_id=user["id"],
                               verdict=body.verdict, comment=comment, message_id=None)
    write_audit(user_email=user["email"], action="chat.feedback",
                details={"session_id": chat_id, "turn_id": body.turn_id, "verdict": body.verdict})
    logger.info("chat feedback", extra={"event": "chat_feedback", "session_id": chat_id,
                                        "turn_id": body.turn_id, "verdict": body.verdict,
                                        "has_comment": comment is not None})
    turn = read_turn(chat_id)
    parent = _otel.remote_parent_context(turn.trace_id, turn.span_id) if turn and turn.turn_id == body.turn_id else None
    _otel.emit_feedback_span(session_id=chat_id, turn_id=body.turn_id, user_id=user["id"],
                             verdict=body.verdict, has_comment=comment is not None, parent_context=parent)
    return {"id": row["id"], "turn_id": body.turn_id, "verdict": body.verdict, "comment": comment,
            "updated_at": row.get("updated_at")}
```

`list_messages` adds `"turn_id": getattr(m, "turn_id", None)` to each item. Bookkeeping: `POSTURE` entry, `CATALOG` entry `chat.feedback` ("A user rated one chat turn (thumbs up/down)."), `_EXEMPT` reason ("a web-chat UI affordance keyed on the live turn id the client learns from the WS frames; the admin read side is `GET /api/admin/telemetry/feedback` with `agnes admin usage feedback`; no analyst CLI/MCP analogue — an agent rating its own answers is not a signal"), `KNOWN_UNTESTED` reason, `docs/api-reference.md` path. `emit_feedback_span` in `otel.py` per the Interfaces block.

- [ ] **Step 3: Web client**

`chat.js`:
- In `finalizeAssistantMessage`, where the article is known (both branches), `if (frame && frame.turn_id) article.dataset.turnId = frame.turn_id;` BEFORE `attachMessageActions(...)`.
- In the history renderer (the function that builds an assistant article from a `GET /sessions/{id}/messages` item — grep `renderMessage`), `if (m.turn_id) article.dataset.turnId = m.turn_id;`.
- In `attachMessageActions`, after the copy button: `if (article.classList.contains("msg-assistant") && article.dataset.turnId) wrap.appendChild(buildFeedbackControls(article.dataset.turnId));` (check the assistant article's class name in the file — use whatever `finalizeAssistantMessage` creates).
- New `buildFeedbackControls(turnId)` + `submitFeedback(turnId, verdict, comment)` per the Interfaces block; the CSRF/credentials pattern copied from the nearest existing JSON POST in chat.js (grep `"Content-Type": "application/json"`); the current session id from the variable the file already uses for `/api/chat/sessions/${...}` calls.

`chat.css` (after `.msg-actions` rules):

```css
/* Thumbs on a completed assistant turn — same muted row as the copy button;
   the selected verdict stays visible without hover. */
.msg-feedback { display: inline-flex; align-items: center; gap: var(--space-1); margin-left: var(--space-2); }
.msg-feedback-btn { background: none; border: 0; padding: 2px; color: inherit; cursor: pointer;
  border-radius: var(--radius-sm, 4px); line-height: 0; }
.msg-feedback-btn:hover { color: var(--ds-text); }
.msg-feedback-btn.is-selected { color: var(--ds-primary); }
.msg:has(.msg-feedback-btn.is-selected) .msg-actions { opacity: 1; }
.msg-feedback-comment { font: inherit; font-size: var(--text-xs); padding: 2px 6px; border: 1px solid var(--ds-border);
  border-radius: var(--radius-sm, 4px); background: var(--ds-surface); color: var(--ds-text); min-width: 220px; }
```

(Use only tokens that exist in `app/web/static/css/*.css` — grep `--ds-border`, `--ds-surface`, `--ds-text`; substitute the file's actual names.)

`tests/test_chat_feedback_ui.py`: static assertions — chat.js contains `dataset.turnId`, `/feedback`, `msg-feedback-btn`, `data-verdict`; chat.css contains `.msg-feedback` and no raw hex in the new block; and `tests/test_design_system_contract.py` stays green.

- [ ] **Step 4: Run + commit**

Run: the three new test files + `tests/test_chat_api.py tests/test_audit_route_posture.py tests/test_audit_declared_actions.py tests/test_audit_catalog.py tests/test_documentation_api_triple_surface.py tests/test_api_docs_coverage.py tests/test_design_system_contract.py tests/db_pg/test_endpoints_smoke.py -q` → PASS

```bash
git add app/api/chat.py app/web/static/js/chat.js app/web/static/css/chat.css src/observability/otel.py src/audit_posture.py \
  src/audit_events.py tests/test_documentation_api_triple_surface.py tests/db_pg/test_endpoints_smoke.py docs/api-reference.md \
  tests/test_chat_feedback_api.py tests/db_pg/test_chat_feedback_pg.py tests/test_chat_feedback_ui.py
git commit -m "chat: thumbs feedback on a turn — endpoint, audit, log and span event, web controls"
```

---

### Task 9: Docs, CHANGELOG fragment, CLAUDE.md note, OpenAPI snapshot (integrator)

**Files:**
- Modify: `docs/observability.md` (the retention table; the `agnes-usage` package section; a new "LLM call ledger" section after "Chat cost"; the whole "OpenTelemetry export" section incl. the attribute table, "Content" → "Content policy", "The embedded engine's own spans" → adds the relay-under-policy paragraph; the "Chat cost" section gains one line pointing at `llm-cost`)
- Create: `changelog.d/llm-observability.md`
- Modify: `CLAUDE.md` (the "LLM cost accounting" paragraph gains two sentences)
- Regenerate: `tests/snapshots/openapi.json` (`make update-openapi-snapshot`)
- Test: `tests/test_changelog_integrity.py`, `tests/test_openapi_snapshot.py`, `python3 scripts/verify_syncmap.py`, `tests/test_api_docs_coverage.py`

- [ ] **Step 1: `docs/observability.md`**

Rewrite these sections (keep everything else):

1. Retention table: add `| \`llm_calls\` | \`retention.llm_calls_days\` | 0 = forever | daily \`retention-prune\` |`; the intro sentence "seven distinct records" → "eight" and name `llm_calls`.
2. `agnes-usage` package: add `agnes_llm_calls` to the table list (Postgres-backed instances only; own rows for members).
3. New section `## LLM call ledger — every call, priced once` after "Chat cost": what `llm_calls` holds (one row per call from the broker AND every server-side generation — builders, extraction incl. batch results, OCR, vision, NER, auto-title, readiness, corporate memory, digests, autodoc, ontology, verification, store guardrails), the context fields (workload/purpose/session/turn/user/agent/job/subject), pricing at write time with `priced_as` (and the `default` price key meaning), the three reads with the CLI commands and one example each, the `turn_id` that `chat_messages`, `usage_turns.turn_uuid` and `llm_calls` share, feedback (`POST /api/chat/sessions/{id}/feedback`, `chat_message_feedback`, `agnes admin usage feedback`), memory provenance (`agent_memories.source_turn_id`/`source_message_id`), DuckDB-backed instances (typed 501, no ledger), retention.
4. "OpenTelemetry export": drop `AGNES_OTEL_CAPTURE_CONTENT=1` from the env example (replace with a comment pointing at the policy block); the Terraform-module paragraph says the module's `otlp_capture_content` boolean no longer enables content on its own and that carrying the policy record is a separate module change. "What is exported" gains: the `agnes.chat.turn` span (INTERNAL, per delivered user message, attrs), `agnes.chat.tool <tool>` children (name, `agnes.args_hash`, `agnes.is_error`, never arguments/results), completion spans as CHILDREN of the turn via the `chat:turn:{session_id}` coordination record (different replicas; collector stitches on `trace_id`; coordination down → root span), the `agnes.chat.feedback` span + `agnes.feedback` event. Attribute table: remove `agnes.user_email` (with the VARIANT-NULL note for a collector-side consumer), add `agnes.workload`, `agnes.purpose`, `agnes.turn_id`, `agnes.job_id`, `agnes.subject_id`, `agnes.cost_usd` (both), the cache-token attributes now on generation spans too, `agnes.tool_calls` on the turn span.
5. "Content" → "### Content policy — placement and consent": the three tiers T0/T1/T2, the `instance.yaml` block, the rules (mode without basis = off + WARNING; the env var deprecated; startup log + `observability.content_export` audit row), `pseudonymized` = the instance anonymizer with its pseudonym key, the events/cap/truncation paragraph kept; a `**BREAKING**`-flavoured paragraph: a deployment that relied on `AGNES_OTEL_CAPTURE_CONTENT=1` must add the policy record to keep exporting content.
6. "The embedded engine's own spans": add "The relay under the same policy" — `off` strips `gen_ai.prompt`/`gen_ai.completion`/`gen_ai.input.messages`/`gen_ai.output.messages` and sets `agnes.content_stripped=true` (structural spans still flow); `pseudonymized` rewrites; `full` forwards; logs dropped/rewritten/forwarded; metrics always forwarded; gzip decoded and re-sent uncompressed; undecodable → `400 otlp_batch_undecodable` (fails closed).

- [ ] **Step 2: `changelog.d/llm-observability.md`**

```markdown
### Added
- **One record per LLM call, priced once, from every call site.** A new Postgres-only
  `llm_calls` ledger holds who / for what / in which turn / at what price for every
  brokered chat completion AND every server-side generation (builders, document
  extraction incl. batch results, OCR, vision, NER, auto-title, readiness probes,
  corporate memory, digests, autodoc, ontology, verification, store guardrails),
  with the rates stored beside the figure (`priced_as`). Read it with
  `GET /api/admin/telemetry/llm-cost` (`agnes admin usage llm-cost`, grouped by
  workload / agent / user / model / purpose), `GET /api/admin/telemetry/llm-calls`
  (`agnes admin usage llm-calls`, the rows of one session / turn / job), the new
  "LLM cost" table on `/admin/telemetry`, and the `agnes_llm_calls` table in the
  `agnes-usage` package. `retention.llm_calls_days` prunes it (default 0 = forever).
  See `docs/observability.md` → *LLM call ledger*.
- **A real trace per chat turn.** ChatManager mints a `turn_id` per user message,
  opens an `agnes.chat.turn` span with one `agnes.chat.tool <tool>` child per tool
  call, and the broker parents its completion spans under it — no engine-side
  propagation needed. The same `turn_id` rides every frame, `chat_messages.turn_id`,
  `usage_turns.turn_uuid` and `llm_calls.turn_id`.
- **Thumbs on chat answers.** `POST /api/chat/sessions/{id}/feedback` (`up`/`down` +
  optional comment, one row per turn and user; audited as `chat.feedback`), rendered
  on every completed assistant bubble in the web chat; admins read it with
  `GET /api/admin/telemetry/feedback` (`agnes admin usage feedback`). Agent memories
  now record the turn and message that wrote them (`source_turn_id`, `source_message_id`).
- Generation spans carry prompt-cache tokens, `agnes.cost_usd`, `agnes.workload`,
  `agnes.purpose`, `agnes.job_id` and `agnes.subject_id`; the `llm_generation` log
  line gains `workload`, `purpose`, `cost_usd`.

### Changed
- **BREAKING** Prompt and completion text now leaves the instance only under a recorded
  policy: `observability.content_export` in `instance.yaml` (`mode: off | pseudonymized |
  full`, `placement`, `basis`, `approved_by`, `approved_at`). `AGNES_OTEL_CAPTURE_CONTENT`
  is a deprecated alias that no longer enables content on its own — a deployment that
  relied on it must add the policy record to keep exporting content. A mode without a
  basis is treated as `off` and logged at startup; the effective policy is written to the
  audit log as `observability.content_export`. `pseudonymized` runs every exported text
  through the instance anonymizer. The embedded engine's OTLP relay obeys the same policy:
  under `off` it strips `gen_ai.prompt` / `gen_ai.completion` / `gen_ai.input.messages` /
  `gen_ai.output.messages` (flagging `agnes.content_stripped`) and drops log bodies,
  under `pseudonymized` it rewrites them, and an undecodable batch is refused
  (`400 otlp_batch_undecodable`) rather than forwarded blind.
- `agnes.user_email` is no longer exported on completion spans (`agnes.user_id` stays),
  and the broker no longer reads the session row for the native sandbox's `main` scope
  on the span path.
```

- [ ] **Step 3: `CLAUDE.md`** — in "LLM cost accounting", append: "Since the 2026-09-08 LLM observability design every call — brokered or server-side — also lands as one priced row in the Postgres-only `llm_calls` ledger (`src/observability/llm_record.py` prices it with the same table at write time; `GET /api/admin/telemetry/llm-cost` groups it by workload/agent/user/model/purpose), carrying the `turn_id` that `chat_messages`, `usage_turns` and the exported spans share. Content export (spans and the engine's relay) is governed by `observability.content_export` in `instance.yaml`, never by an environment variable — see `docs/observability.md` → *Content policy*."

- [ ] **Step 4: Snapshot + gates + commit**

```bash
make update-openapi-snapshot
python3 scripts/verify_syncmap.py
.venv/bin/pytest tests/test_changelog_integrity.py tests/test_openapi_snapshot.py tests/test_api_docs_coverage.py tests/test_web_guide_skill_sync.py -q
git add docs/observability.md changelog.d/llm-observability.md CLAUDE.md tests/snapshots/openapi.json
git commit -m "docs: LLM observability — call ledger, turn traces, content policy; changelog fragment; openapi snapshot"
```

---

## Self-review against the spec

| Spec | Task |
|---|---|
| 3.1 context (`llm_context.py`, merge, worker `job_id`) | 1, 3 |
| 3.1 record + two producers + two sinks, pricing with `priced_as`, unknown model → default and says so | 1, 2 |
| 3.1 `trace_generation(purpose=)`, four tokens both shapes, log line fields | 1 |
| 3.1 `agnes.user_email` removed; cache tokens + cost on generation spans; trace/span ids on the row | 1, 2 |
| 3.1 accumulator carries `llm_calls` rows, shutdown flush (existing `usage_accumulator.flush()` in `app/main.py` covers it) | 1 |
| 3.2 turn id, turn span, coordination key (24 h, never deleted), frame stamping, tool spans, turn-end totals + cost, broker child linkage, degrade on outage, `usage_turns.turn_uuid`/`chat_messages.turn_id` | 4, 6 |
| 3.3 every call site incl. batch results; five builders with subject + user; the service modules | 3 |
| 3.4 three reads + CLI + `agnes_llm_calls` + admin page section + chat-cost note + retention | 6, 7 |
| 3.5 feedback endpoint/table/audit/log/span event/web thumbs; memory provenance; builder content events under policy | 5, 6, 8 |
| 3.6 policy record, rules, deprecated env var, startup log + audit, pseudonymised mode, relay strip/rewrite/drop/gzip/undecodable, identity minimisation, module note in docs | 2, 5, 9 |
| 3.7 tables/columns/indexes/repos/registry/contract tests | 6 |
| 3.8 only the two config keys, no new env var | 5, 6 |
| 3.9 invariants | every producer/sink wrapped; 501 typed; relay fails closed |
| 3.10 tests | per task; coverage guard in 3; policy table in 5; protobuf incl. gzip in 5; OpenAPI + `KNOWN_UNTESTED` in 7/8/9 |
| 3.11 order | Tasks 1–5 no schema, 6 the migration, 7–8 after it |

---

## Addendum (2026-09-08, after Tasks 1–2 merged): spec sections 3.12, the 8 MiB usage hole, per-workload content classes

Spec delta landed in the same commit as this addendum. Three new tasks and one
follow-up; the orchestrator sequences them as noted. Global constraints above
apply unchanged.

### Task 2b: Usage survives an oversized stream (head + tail buffers)

**When:** after Task 4 has merged (both touch the broker's SSE `finally`).

**Files:**
- Modify: `app/api/broker.py` (the SSE mirror: `_SSE_USAGE_COLLECT_MAX_BYTES`, the `collected`/`state["overflow"]` block in `_passthrough`, and the `usage = None if state["overflow"] …` line)
- Modify: `app/api/broker_agent_policy.py` (`parse_usage` gains `parse_usage_from_edges(head: bytes, tail: bytes, content_type) -> dict | None`)
- Modify: `src/observability/llm_record.py` (`response_truncated: bool | None` on the record and row) — only if not already present
- Test: `tests/test_broker_llm_calls.py`, `tests/test_otel_export.py`

**Interfaces:**
- `_SSE_EDGE_BYTES = 64 * 1024`. The mirror keeps `head` (first 64 KiB, append-only until full) and `tail` (a `collections.deque` of chunks trimmed to the last 64 KiB) for EVERY streamed completion, in addition to the existing capped full mirror.
- On overflow: `usage = parse_usage_from_edges(bytes(head), b"".join(tail), ctype)`; the record/span get `usage` as normal, `response_truncated=True`, `stream_complete` from the tail's `message_delta` (`stop_reason` present). Content summary (`describe_completion`) runs on the head only and is flagged truncated. No overflow: behaviour unchanged.
- `parse_usage_from_edges` scans `message_start` in `head` and `message_delta` in `tail` with the same event parser `parse_usage` uses; a `message_start` missing from the head (pathological) yields `None` for input tokens but still returns output tokens/model from the tail.

- [ ] **Step 1: Failing tests** — a 9 MiB synthetic SSE body (one `message_start` with usage, many `content_block_delta`, a final `message_delta` with `stop_reason` + `output_tokens`): the span carries all four token kinds, `gen_ai.response.finish_reasons`, `agnes.response_truncated=True`, `agnes.cost_usd > 0`; the `llm_calls` row is written with the same numbers; a second test with the body under the cap asserts nothing changed.
- [ ] **Step 2: Implement** the edge buffers and `parse_usage_from_edges`; keep the existing warning log for the overflow but downgrade its wording ("usage recovered from stream edges; content summary truncated").
- [ ] **Step 3: Run** `tests/test_broker_llm_calls.py tests/test_otel_export.py tests/test_broker_agent_policy.py`; commit `broker: recover usage from stream edges when the mirror overflows`.

### Task 5b: Per-workload content classes (`workloads` allowlist)

**When:** after Task 5 has merged. Small.

**Files:**
- Modify: `src/observability/content_policy.py` (`workloads: tuple[str, ...]` on the record; `content_export_mode(workload: str | None = None)` returns `off` when the allowlist is non-empty and excludes `workload`), `src/observability/otel.py` (`capture_content_enabled(workload=None)`; the completion/generation event emission passes the record's workload), `src/observability/llm_tracing.py` (passes `purpose`'s workload), `app/api/broker.py` (`otlp_proxy` evaluates the policy for workload `chat`), `config/instance.yaml.example` (`workloads: []` line + the content-class table as comments), `docs/observability.md` (the policy section: the table from spec 3.6)
- Test: `tests/test_content_policy.py` (policy table gains the workload dimension), `tests/test_otel_export.py`, `tests/test_broker_otlp.py`

- [ ] **Step 1: Failing tests** — `mode: full, workloads: [builder]`: a broker completion span (workload `chat`) carries NO content events, a builder generation span does; the relay strips for `chat`. Empty `workloads` = every workload.
- [ ] **Step 2: Implement**; `announce_policy()` logs the allowlist. Commit `observability: content policy can allow content per workload`.

### Task 10: Conversation corpus export — pull endpoint, CLI, docs (spec 3.12)

**When:** after Task 6 (migration) has merged — the record reads `chat_message_feedback`, `agent_memories.source_turn_id`, `chat_messages.turn_id`, `llm_calls`. Urgent: build it right after 6, in parallel with 7 and 8 (disjoint files; append-only shared lists).

**Files:**
- Create: `src/conversation_export.py` — `build_conversation_record(session, messages, calls, feedback, memories, *, content_mode, anonymizer) -> dict`, `iter_conversations(repo_bundle, *, since, until, surface, agent_id, limit, cursor) -> (records, next_cursor)`, `serialize_jsonl(records) -> Iterator[bytes]`
- Create: `app/api/conversations_export.py` — router `GET /api/admin/conversations/corpus` (admin gate, PG-only repos resolved as dependencies → typed 501, policy check → `403 content_export_disabled` with `{"error": "content_export_disabled", "reason": "mode_off|no_basis|workload_excluded"}`, `StreamingResponse` for jsonl, JSON array for `format=json`, `X-Next-Cursor` header + `next_cursor` in JSON), registered in `app/main.py` next to the other admin routers
- Create: `cli/commands/admin_conversations.py` — `agnes admin conversations export --since --until --surface --agent-id --limit --out <file> [--json]`; streams pages through the cursor until exhausted; "nothing found" answers name the window and the policy state; wired into `cli/commands/admin.py` (or wherever `admin usage` is registered)
- Modify: `src/repositories/chat_sessions_pg.py` (`list_completed_between(since, until, *, surface, agent_id, limit, after: tuple[datetime, str] | None)` keyset on `(last_message_at, id)`), `src/repositories/chat_messages_pg.py` (`list_for_sessions(session_ids) -> dict[str, list[ChatMessage]]`), `src/repositories/llm_calls_pg.py` (`totals_for_sessions(session_ids) -> dict[str, dict]`, `statuses_for_sessions`), `src/repositories/chat_message_feedback_pg.py` (`list_for_sessions`), `src/repositories/agent_memories_pg.py` (`list_for_sessions`)
- Modify (append-only): `src/audit_posture.py` (`READ_POSTURE["GET /api/admin/conversations/corpus"] = "conversations.export"`), `src/audit_events.py` (`conversations.export`, kind `read`, "A conversation corpus export was read."), `tests/test_documentation_api_triple_surface.py` (`_EXEMPT` — CLI-covered), `docs/api-reference.md`, `tests/db_pg/test_endpoints_smoke.py` `KNOWN_UNTESTED`, `docs/observability.md` (new section "Conversation corpus export — for evaluation, under the content policy": shape table from spec 3.12, the policy gate, the pull example with a PAT, `content_mode`, the note that emails never appear)
- Test: `tests/db_pg/test_conversation_export_pg.py` (new; PG), `tests/test_conversation_export.py` (new; pure record builder), `tests/test_cli_admin_conversations.py` (new)

**Interfaces / rules:**
- A conversation is "completed" for the window when `chat_sessions.last_message_at` (or the last message's `created_at`) falls in `[since, until)`; default `until=now`, `since` required (400 when absent).
- `messages_json` items: `{"role", "content", "turn_id", "created_at", "parts": [...]}` — `parts` verbatim from `chat_messages.parts` (tool_use/tool_result with `args`/`result`), `content` the message text; `tool_calls_json` flattened from `parts` in message order with `started_at = message.created_at`.
- `content_mode`: `full` → verbatim; `pseudonymized` → `src.anonymization.anonymize_markdown` (instance key, `rules_from_config()`) applied ONCE to `messages_json` (each `content`, each tool `args`/`result` string leaf), `tool_calls_json` and `first_user_message`; never to ids or timestamps.
- `user_id` only; the record never carries an email (`sender_email` is dropped; a test asserts `"@"` does not appear in any `user_id` field and that `sender_email` is absent).
- Totals from `llm_calls` when at least one row exists for the session (`cost_status="ledger"`), else from `chat_messages` token columns priced with `src.llm_pricing.cost_usd` per message model (`cost_status="transcript"`), else `cost_status="unavailable"` with zeros — never a silent zero.
- Audit one row per request: `conversations.export` with `{since, until, surface, agent_id, count, content_mode, placement, delivery: "pull"}`.
- Policy evaluation uses `content_export_mode(workload="chat")` (Task 5b) — if 5b has not merged, use the global mode.

- [ ] **Step 1: Failing tests** — record builder (shape, ordering, tool_calls flattening, feedback/memory joins, pseudonymised leaves, no email, `cost_status` three ways); PG endpoint (admin gate 403 for non-admin, 501 on DuckDB via the parity sweep exemption, 403 `content_export_disabled` when policy off, jsonl streaming with cursor pagination across 3 pages of `limit=2`, `format=json`); CLI (`--out` file written, pages followed, `--json`).
- [ ] **Step 2: Implement** the repo helpers (keyset, bulk-by-session-ids, no N+1), the record builder, the router, the CLI.
- [ ] **Step 3: Bookkeeping** — posture, catalog, triple-surface exemption, api-reference, `KNOWN_UNTESTED`, docs section; the integrator regenerates the OpenAPI snapshot.
- [ ] **Step 4: Run** the three test files + `tests/test_audit_read_posture.py tests/test_audit_catalog.py tests/test_documentation_api_triple_surface.py`; commit `conversations: corpus export endpoint and CLI under the content policy`.

### Task 11: Conversation corpus export — push sink (spec 3.12, optional, last)

**When:** after Task 10 and Task 9; only if time remains before the PR leaves draft — otherwise leave a `TODO` in `docs/observability.md` naming this task.

**Files:**
- Create: `app/worker/kinds_conversation_export.py` (or extend `app/worker/kinds.py`): job kind `conversation-export`, LIGHT lane, idempotency key `conversation-export:<instance>`; reads the watermark, calls `iter_conversations`, POSTs newline-delimited JSON batches (≤ 200 records / ≤ 8 MiB) with headers from `os.environ[headers_secret_env]` parsed like `OTEL_EXPORTER_OTLP_HEADERS`, retries 3× with backoff on 5xx/connection errors, advances the watermark only after 2xx, audits `conversations.export` with `delivery: "push"`, `count`, `endpoint_host` (never headers)
- Create: migration `0116_export_watermarks.py` — table `export_watermarks (name text pk, watermark timestamptz, updated_at timestamptz)`; model; PG-only repo `export_watermarks_pg.py`; registry entry
- Modify: `services/scheduler/__main__.py` (an interval tick that enqueues the job when `observability.conversation_export.endpoint` is set; `interval_minutes` default 60), `config/instance.yaml.example` (`observability.conversation_export` block), `app/instance_config.py` (typed reader), `docs/observability.md`
- Test: `tests/db_pg/test_conversation_export_push_pg.py` (fake HTTP server via `httpx.MockTransport`; watermark advances only on 2xx; a 500 leaves it; batches split at 200), `tests/test_scheduler_conversation_export_tick.py`

- [ ] **Step 1: Failing tests**; **Step 2: implement**; **Step 3: docs**; commit `conversations: scheduled push of the corpus export`.

### File map additions

| File | Task | Responsibility |
|---|---|---|
| `app/api/broker.py` (SSE mirror edges), `app/api/broker_agent_policy.py` (`parse_usage_from_edges`) | 2b | usage survives an oversized stream |
| `src/observability/content_policy.py` (`workloads`), producers pass workload | 5b | per-workload content classes |
| `src/conversation_export.py`, `app/api/conversations_export.py`, `cli/commands/admin_conversations.py` (new); repo read helpers | 10 | corpus export, pull |
| `app/worker/kinds_conversation_export.py`, `migrations/versions/0116_export_watermarks.py`, `src/repositories/export_watermarks_pg.py` (new) | 11 | corpus export, push |

### Execution-notes additions

- Order: 3, 4, 5 (running) → integrate → **2b and 5b** in parallel (2b touches the broker's SSE path, 5b touches `content_policy.py`/`otel.py` event gating — disjoint) → 6 (migration) → **7, 8, 10** in parallel → 9 (integrator; the CHANGELOG fragment gains the corpus-export bullet and the `workloads` line) → 11 if time remains.
- The self-review table gains rows: 3.1 usage survives an oversized stream → 2b; 3.6 `workloads` allowlist + content-class table → 5b; 3.12 pull → 10; 3.12 push → 11; 3.5 extraction provenance → 3 (`subject_id`, `job_id` already required there).
