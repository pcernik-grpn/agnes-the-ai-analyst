# LLM observability — one call record, two sinks, one content policy

**Date:** 2026-09-08
**Status:** approved design, pre-implementation
**Related:** `2026-09-01-broker-turn-usage-attribution-design.md` (the
coordination-backed turn counters this design extends),
`2026-08-31-usage-package-and-per-turn-tokens-design.md` (the `agnes-usage`
package and `usage_turns`), `docs/observability.md` (what ships today).

## 1. Problem

Agnes cannot answer three questions about its own model usage in one place:

1. **What did the model do?** How many calls, which ones, in what order, how
   long each took — for one chat turn, one agent run, one extraction job.
2. **What did it cost?** Per instance, per agent, per user, per *kind of
   work* (chat vs. builder vs. extraction vs. corporate memory).
3. **Why was an answer bad?** And its cousins: why did an agent's memory
   notebook store nonsense, why did a builder propose the wrong skill.

Everything needed exists in fragments (verified against the code on
2026-09-08):

- The chat broker opens one OTLP span per forwarded completion
  (`app/api/broker.py`, `_start_otel_completion_span`) and
  `trace_generation` (`src/observability/llm_tracing.py`) opens one per
  server-side generation. Both are **flat**: no parent, no propagation, no
  turn boundary. `traceparent` is neither read by the broker nor sent by the
  embedded engine's sandbox (its relay copies the CLI's headers and adds only
  `authorization`; it ships no HTTP instrumentation). The sandbox's own spans
  and the broker's completion spans therefore have different trace ids and
  join only on the chat session id.
- Generation spans carry provider, model and two token kinds. They carry no
  cache tokens (`_Capture.set_output_from_anthropic` reads only
  `input_tokens`/`output_tokens`), no *purpose* (a builder turn, a
  corporate-memory extraction, a digest and an auto-title look identical),
  and no identity (`distinct_id` has no caller).
- A whole class of LLM calls bypasses `trace_generation` entirely: the
  document fact extractor (`connectors/sharepoint/facts_extraction.py`,
  `_create` and `_sync_retry`), OCR (`src/ingest/scan_ocr.py`), vision
  (`src/ingest/vision.py`), the NER anonymizer (`src/anonymization_ner.py`),
  auto-title (`app/chat/auto_title.py`). Only the two providers under
  `connectors/llm/` are traced.
- Cost lives in four disjoint ledgers with different coverage:
  `chat_messages` token columns (every chat surface; cache columns
  Postgres-only), `llm_usage` (broker, **agent-bound sessions only**),
  `usage_turns` (Postgres-only, per assistant turn), and the extraction
  ledgers (`extraction_runs.usage`, `facts_ingest_runs.llm_usage`).
  Server-side generations — builders, corporate memory, digests, auto-title,
  NER, OCR, vision — have **no ledger at all**, only a log line and (opt-in) a
  span. USD is computed only at read time and only over `chat_messages`
  (`GET /api/admin/telemetry/chat-cost`). A span carries tokens, never a
  price, so a collector has to re-implement the price table.
- Chat content is on-instance already (`chat_messages.content` + `parts` with
  tool arguments and results, exported to session JSONL). What is missing is
  the link from a bad answer to the completions that produced it, and any
  signal that it *was* bad: there is no thumbs-down on chat;
  `semantic_feedback` records a question and a SQL string with no session or
  message id. `agent_memories` carries only `source_session_id`, never the
  turn or the message that wrote it. Builder turns are stateless by design —
  the transcript lives in the page — so after the fact nothing server-side
  says what the model was asked or answered.
- Content export is a bare boolean environment variable
  (`AGNES_OTEL_CAPTURE_CONTENT`, `src/observability/otel.py`), set by
  whoever owns the deployment automation. The code cannot tell an
  operator-controlled collector from a vendor-controlled one, and records no
  basis for the decision. The embedded engine's sandbox exports prompt and
  completion text on its **own** spans (`gen_ai.input.messages`,
  `gen_ai.output.messages`, `gen_ai.prompt`, `gen_ai.completion`) with only
  a regex scrub (emails, phones, tokens), and the broker's OTLP relay
  (`otlp_proxy`) forwards those batches byte-for-byte — so once the engine's
  relay scope is enabled, content leaves the instance regardless of the Agnes
  flag.
- `agnes.user_email` rides every completion span. It is personal data with
  no join value off-instance (`agnes.user_id` is the stable key), and
  fetching it is the only reason the broker reads the session row for the
  native sandbox's `main` scope.

## 2. Goals and non-goals

**Goals**

- One record per LLM call, from every call site, carrying *who / for what /
  in which turn / at what price*, written to two sinks: the on-instance
  Postgres ledger and the opt-in OTLP export. Both sinks carry the same ids
  so a row and a span describe the same call.
- A real trace per chat turn — turn span, tool spans, completion spans as
  children — without depending on the engine propagating context.
- Cost answerable on-instance for every customer, by instance, agent, user,
  model and workload; the same figure exported on the span.
- A quality signal on chat (thumbs + comment) linked to the turn, and
  provenance on the two artifacts the model writes for later (agent memory,
  builder patches) linked to the same turn.
- One content policy that names **placement** (where content may go) and
  **consent** (who approved it, on what basis) separately, and that governs
  every export path including the engine's relay.

**Non-goals**

- No new telemetry vendor, no new transport. OTLP/HTTP to wherever the
  operator points it stays the only export.
- No change to the embedded engine (a separate codebase). Everything below
  is achievable in this repository; engine-side propagation remains a
  compatible future enhancement.
- No retirement of `llm_usage`, `usage_turns` or the extraction ledgers in
  this change. They keep their readers; the new ledger is the superset they
  can be folded into later.
- No evaluation framework. This design makes evals *possible* (content,
  structure, feedback, joinable ids); it does not build one.

## 3. Design

### 3.1 One call record, two sinks

`src/observability/llm_context.py` — a `contextvars.ContextVar` holding an
immutable `LlmCallContext`:

| field | meaning | set by |
|---|---|---|
| `workload` | coarse kind of work: `chat`, `agent_api`, `builder`, `extraction`, `corporate_memory`, `knowledge`, `semantic_layer`, `anonymization`, `ocr`, `vision`, `auto_title`, `readiness`, `store_guardrails`, `verification` | the call site |
| `purpose` | fine-grained call site label, e.g. `entity_builder_turn`, `facts_extraction`, `facts_retry`, `digest`, `tagger`, `contradiction`, `completion` | the call site |
| `session_id`, `turn_id` | chat session and turn (see 3.2) | ChatManager / broker |
| `user_id`, `agent_id` | identity — the user id, never the email | the call site |
| `job_id` | worker job id when running inside one | worker runtime |
| `subject_id` | what the call is *about*: an entity id for a builder, a document id for extraction, a session id for auto-title | the call site |

`with llm_context(workload=..., purpose=..., ...)` pushes a context; nested
calls merge (inner values win, unset inherit). The worker runtime binds
`job_id` where it already binds `request_id` (`app/worker/runtime.py`,
`bind_request_id`), so every generation inside a job inherits it with no
per-handler code.

`LlmCallRecord` (a dataclass in `src/observability/llm_record.py`) is the
one shape both sinks consume:

`kind` (`completion`|`generation`), the context fields above, `provider`,
`upstream`, `model_requested`, `model_response`, the four token kinds,
`cost_usd`, `priced_as` (the four rates used), `latency_ms`, `status`
(`ok`|`error`), `error_type`, `http_status`, `prompt_chars`,
`completion_chars`, `stop_reason`, `stream_complete`, `trace_id`, `span_id`,
`created_at`.

Two producers:

- **The broker**, at both places it already parses usage (the SSE
  `finally` mirror and the buffered non-stream path). It builds the record
  from `parse_usage` + the request hints + the turn linkage (3.2), prices it,
  finishes the span from it, and hands it to the accumulator.
- **`trace_generation`**, which gains keyword `purpose` (falls back to the
  context's), reads the four token kinds from Anthropic *and* OpenAI-shaped
  usage (`cache_read_input_tokens`/`cache_creation_input_tokens`;
  `prompt_tokens_details.cached_tokens`), prices the call, and emits the
  same record to the span and the ledger. Its `llm_generation` log line
  keeps every field it has and gains `workload`, `purpose`, `cost_usd`.

Two sinks:

- **Span.** `start_completion_span` / `start_generation_span` take the
  context and set `agnes.workload`, `agnes.purpose`, `agnes.turn_id`,
  `agnes.user_id`, `agnes.agent_id`, `agnes.job_id`, `agnes.subject_id`;
  the end functions set `agnes.cost_usd` and the cache-token attributes on
  generation spans too. `agnes.user_email` is **removed** (see 3.6). The
  record's `trace_id`/`span_id` are read back from the span so the ledger
  row carries them.
- **Ledger.** Table `llm_calls` (3.7), written through
  `UsageAccumulator` in the broker (extended to carry `llm_calls` rows next
  to the existing `llm_usage` rows, same flush cadence and same
  shutdown flush) and directly from `trace_generation` (one insert per call,
  wrapped; a `RequiresPostgresBackend` or any other failure is logged at
  debug and swallowed — a measurement never costs the call).

Pricing is `src/llm_pricing.cost_usd` at write time, with `priced_as`
stored beside the figure so any row can be re-derived. An unknown model
prices at `DEFAULT_PRICE` exactly as every other surface does, and the
`priced_as` rates say so.

### 3.2 Turn structure without engine propagation

ChatManager mints a `turn_id` (uuid4) when it delivers a user message
(`_deliver_local_user_message`, where `turn_in_flight` is set) and:

- opens an OTel span `agnes.chat.turn` (kind `INTERNAL`) with
  `agnes.session_id`, `agnes.turn_id`, `agnes.user_id`, `agnes.agent_id`,
  `agnes.surface`, `agnes.workload=chat|agent_api` — no content;
- stores `{turn_id, trace_id, span_id, started_at}` under the coordination
  key `chat:turn:{session_id}` (`coordination().kv_set`, 24 h TTL, the same
  backend `turn_usage.py` uses), overwritten by the next turn — never
  deleted at turn end, so a completion that lands after the assistant frame
  still attributes to the turn that caused it;
- stamps `turn_id` onto every frame it broadcasts for that turn
  (`assistant_message`, `tool_call`, `tool_result`, `error`, `done`) so the
  web client can reference the turn (3.5);
- opens one child span per `tool_call` frame, ended by the matching
  `tool_result` (name `agnes.chat.tool <tool>`, attributes: tool name,
  `agnes.args_hash` — the hash `chat.tool_call` audit already uses —
  `agnes.is_error`, duration); never arguments or results;
- ends the turn span at `assistant_message` or `done` (whichever closes the
  turn today), with `agnes.tool_calls`, the drained token totals and the
  turn's `agnes.cost_usd` summed from the broker's counters.

The broker, on every completion, reads `chat:turn:{session_id}` and, when
present, opens the completion span **as a child** of the stored span
context (`trace.set_span_in_context(NonRecordingSpan(SpanContext(...,
is_remote=True)))`), and copies `turn_id` into the record. The two
processes may be different replicas; the collector stitches on `trace_id`.
With the coordination backend unavailable the completion span is a root
span, `turn_id` is null, and everything else is recorded — degrade, never
fail.

`usage_turns.turn_uuid` and the PG-only `chat_messages.turn_id` column use
the same id, so the per-turn token table, the transcript and the call
ledger agree on what a turn is.

The engine's own sandbox spans keep their own trace id (out of scope); they
still join on the session id.

### 3.3 Coverage: every call site is traced

Wrap in `trace_generation` with an explicit `purpose` (and a context set at
the entry point):

| call site | workload / purpose |
|---|---|
| `connectors/sharepoint/facts_extraction.py` `_create`, `_sync_retry` | `extraction` / `facts_extraction`, `facts_retry` |
| the extractor's batch transport (usage recorded when the batch result is fetched) | `extraction` / `facts_batch` — one record per document result |
| `src/ingest/scan_ocr.py` (both call sites) | `ocr` / `scan_ocr`, `scan_ocr_retry` |
| `src/ingest/vision.py` | `vision` / `image_caption` |
| `src/anonymization_ner.py` | `anonymization` / `ner_detect` |
| `app/chat/auto_title.py` | `auto_title` / `auto_title` (subject = session id) |
| `app/chat/readiness.py` probes | `readiness` / `probe` |
| the five builder endpoints (`entity_builder`, `agent_builder`, `mcp_builder`, `package_builder`, `semantic_model_builder`) | `builder` / `<kind>_builder_turn`, subject = the entity/agent/package id when one exists, `user_id` = the caller |
| `services/corporate_memory/*`, `src/knowledge_digests.py`, `src/table_autodoc.py`, `app/api/ontology.py`, `app/services/memory_curator_profile.py`, `services/session_processors/verification.py`, `services/verification_detector/detector.py`, `src/store_guardrails/*` | one `workload`/`purpose` pair each, set at the entry point |

Call sites that already use `connectors/llm` providers only need the
context set; the provider's `trace_generation` picks it up.

### 3.4 Cost: measured on-instance, priced once

Read surfaces over `llm_calls` (all admin-gated, all Postgres-only with the
typed `501 requires_postgres_backend` on the frozen DuckDB backend):

- `GET /api/admin/telemetry/llm-cost?window=1d|7d|30d|all&by=workload|agent|user|model|purpose`
  — grouped totals: calls, the four token kinds, `cost_usd`,
  `cached_input_share`, plus `priced_models` so the reader knows what the
  figure was priced at.
- `GET /api/admin/telemetry/llm-calls?session_id=|turn_id=|job_id=|limit=`
  — the detail rows for one turn/session/job, newest first, cursor by
  `created_at`.
- `GET /api/admin/telemetry/feedback?window=&verdict=` — 3.5.
- CLI: `agnes admin usage llm-cost`, `agnes admin usage llm-calls`,
  `agnes admin usage feedback`, each mirroring one endpoint with `--json`
  and the window/filter flags above (command-UX standard).
- `agnes_llm_calls` joins the seeded `agnes-usage` data package
  (`connectors/internal/access.py`), own rows for a non-admin grantee
  (`user_id`), everything for an admin, absent on a DuckDB-backed instance
  like `agnes_turns`.
- `/admin/telemetry` gains one section, "LLM cost", a table by workload with
  a window selector, reading `llm-cost`. Page-shell conventions apply.

`GET /api/admin/telemetry/chat-cost` stays as is; its notes gain one line
pointing at `llm-cost` for the cross-workload view.

Retention: `retention.llm_calls_days` (default 0 = forever) handled by the
existing daily `retention-prune` job (`src/audit_retention.py`).

### 3.5 Quality: a signal, and provenance on what the model writes

**Feedback on a chat turn.** Table `chat_message_feedback` (3.7). Endpoint
`POST /api/chat/sessions/{chat_id}/feedback` with body
`{turn_id, verdict: "up"|"down", comment?}`, gated like the session's other
routes (owner or live participant), one row per `(turn_id, user_id)` —
a second submit updates it. Audit action `chat.feedback` (params: session,
turn, verdict; never the comment). The web chat renders thumbs on each
completed assistant bubble (the frame carries `turn_id`, 3.2) and a short
optional comment on thumbs-down. The same event is emitted as a structured
log record (`event: chat_feedback`, with `session_id`, `turn_id`,
`verdict`) and, when export is on, as a span event `agnes.feedback` on a
short `agnes.chat.feedback` span carrying the turn's trace context read
back from `chat:turn:{session_id}`, so a collector can join it to the
trace.

**Memory provenance.** `agent_memories` gains PG-only `source_turn_id`
and `source_message_id`; `remember` fills them from
`chat:turn:{session_id}` at write time, and the `agent.memory.write`
audit row carries `turn_id`. The DuckDB sibling accepts and drops them
(the same pattern as `chat_messages` cache columns).

**Builder provenance.** The five builder turns set the context (3.3), so
their `llm_calls` rows say which builder, for which subject, by whom. The
prompt and the reply are content and follow the content policy: generation
spans gain the same two content events completion spans have
(`gen_ai.content.prompt` / `gen_ai.content.completion`) when the policy
allows it — `trace_generation` receives the prompt via `set_input` already
and gets the reply text from the provider response. Storing builder prompts
in the database is deliberately **not** part of this design; the export
under policy is the first step, a table is a separate decision if the
export proves insufficient.

### 3.6 Content policy: placement and consent, separately

**Placement** — three tiers, named in the docs and in the policy record:

- T0, on-instance: `chat_messages`, session JSONL, `llm_calls`. Always.
  Under the customer's own contract.
- T1, operator-controlled collector: an OTLP endpoint the instance's owner
  runs.
- T2, third-party collector: an endpoint someone else runs (the vendor, a
  SaaS).

The code cannot verify which tier an endpoint is; the policy record makes
the operator say it.

**Consent** — `config/instance.yaml`:

```yaml
observability:
  content_export:
    mode: off            # off | pseudonymized | full
    placement: operator  # operator | third_party   (required unless off)
    basis: ""            # free text: contract clause, DPA reference, "internal dev instance"
    approved_by: ""      # a person, never blank unless off
    approved_at: ""      # ISO date
```

Rules, enforced in `src/observability/otel.py`:

- `capture_content_enabled()` returns true only when `mode != off` **and**
  `basis`, `approved_by`, `placement` are non-empty. A `mode` without a
  basis is logged at WARNING at startup ("content export requested without
  a recorded basis; exporting sizes only") and treated as `off`.
- `AGNES_OTEL_CAPTURE_CONTENT` becomes a deprecated alias: when set and no
  policy exists it is ignored with the same warning. **This is a behaviour
  change for a deployment relying on the variable** and is flagged
  `**BREAKING**` in the changelog fragment.
- The effective policy is logged once at startup (mode, placement,
  approved_by) and written to the audit log as
  `observability.content_export` (system action) so the decision is in
  the trail.
- `mode: pseudonymized` runs every exported text through the instance
  anonymizer (`src/anonymization.anonymize_markdown` with the instance's
  pseudonym key and `rules_from_config()`) before `add_event`; `full`
  exports as today. Both keep the existing per-event cap and
  `agnes.content_truncated`.

**The relay under the same policy.** `otlp_proxy` decodes the
`ExportTraceServiceRequest` it forwards (`opentelemetry-proto` ships with
the exporter dependency), and for every span and span event:

- `off` — removes the content-bearing attributes `gen_ai.prompt`,
  `gen_ai.completion`, `gen_ai.input.messages`, `gen_ai.output.messages`
  and adds `agnes.content_stripped=true`; the structural spans (turn,
  step, tool) still flow.
- `pseudonymized` — rewrites those attributes through the anonymizer.
- `full` — forwards unchanged.

Logs (`/v1/logs`) carry free-text bodies: under `off` the batch is
accepted and dropped (the exporter sees 2xx), under `pseudonymized` bodies
are rewritten, under `full` forwarded. Metrics are always forwarded. A
gzip-encoded batch is decompressed for processing and re-sent
uncompressed; an undecodable batch is refused with `400
otlp_batch_undecodable` rather than forwarded blind.

**Identity minimisation.** `agnes.user_email` is removed from completion
spans; `agnes.user_id` stays. The broker no longer reads the session row
for the `main` scope on the span path. Note for a collector-side consumer
that already references the attribute: a VARIANT access to a missing key
yields NULL, so the column empties rather than breaks.

The deployment module's `otlp_capture_content` boolean no longer enables
content on its own; the module change to carry the policy record is a
separate PR in the module and is out of scope here (docs say so).

### 3.7 Data model (Postgres-only, one Alembic revision)

`llm_calls`

| column | type | notes |
|---|---|---|
| `id` | text pk | uuid |
| `created_at` | timestamptz | call end, indexed |
| `kind` | text | `completion` / `generation` |
| `workload`, `purpose` | text | 3.1 |
| `session_id`, `turn_id`, `user_id`, `agent_id`, `job_id`, `subject_id` | text null | indexed: `(session_id, created_at)`, `(turn_id)`, `(user_id, created_at)`, `(agent_id, created_at)`, `(workload, created_at)` |
| `trace_id`, `span_id` | text null | hex, null when export is off |
| `provider`, `upstream` | text | |
| `model_requested`, `model_response` | text null | |
| `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens` | bigint default 0 | |
| `cost_usd` | numeric(12,6) | |
| `priced_as` | jsonb | the four rates |
| `latency_ms` | integer null | |
| `status` | text | `ok` / `error` |
| `error_type` | text null | |
| `http_status` | integer null | |
| `prompt_chars`, `completion_chars` | integer null | |
| `stop_reason` | text null | |
| `stream_complete` | boolean null | |

`chat_message_feedback`

| column | type |
|---|---|
| `id` text pk, `session_id` text, `turn_id` text, `message_id` text null, `user_id` text, `verdict` text (`up`/`down`), `comment` text null, `created_at`, `updated_at` timestamptz | unique `(turn_id, user_id)`; index `(session_id)`, `(created_at)` |

Columns added: `chat_messages.turn_id text null` (index),
`agent_memories.source_turn_id text null`, `agent_memories.source_message_id
text null`.

Repositories: `src/repositories/llm_calls_pg.py`,
`src/repositories/chat_message_feedback_pg.py`, registered PG-only in
`_REGISTRY`; `chat_messages_pg.py` and `agent_memories` PG repo gain the
new columns, their DuckDB siblings accept-and-drop, contract tests extended
in the same change.

### 3.8 Configuration

- `observability.content_export` (3.6) — the only new instance.yaml key.
- `retention.llm_calls_days` — one more row in the existing retention block.
- No new environment variables. `AGNES_OTEL_CAPTURE_CONTENT` is deprecated
  in place.

### 3.9 Error handling and invariants

- Instrumentation never fails the call it observes: every producer and
  both sinks swallow their own exceptions (log at debug) — the invariant
  `otel.py` and `llm_tracing.py` already hold.
- Coordination unavailable: no turn linkage, everything else recorded.
- DuckDB app-state backend: ledger writes are silent no-ops; the three new
  read routes answer the typed `501`; spans unaffected.
- The relay fails **closed** on content: a batch it cannot decode is
  refused, never forwarded unstripped.
- Content policy without a basis is `off`, loudly.

### 3.10 Testing

- Unit: context merge/inheritance; record pricing and `priced_as`; token
  extraction for both usage shapes; policy evaluation table (mode × basis ×
  placement); relay stripping and pseudonymisation over a real protobuf
  batch incl. gzip; anonymizer pass on event text.
- Broker (existing `otel_broker` fixture in `tests/test_otel_export.py`):
  child-of-turn linkage via the coordination key, `agnes.user_email`
  absent, `agnes.cost_usd` present, ledger row written with matching
  `trace_id`/`span_id`, agent-less sessions recorded.
- ChatManager: turn span opened/closed, tool spans nested, `turn_id` on
  frames and on `chat_messages`/`usage_turns`.
- Endpoints: admin gate, `501` on DuckDB, grouping correctness on seeded
  rows; feedback upsert and audit; OpenAPI snapshot regenerated; the three
  admin GETs listed in the PG smoke `KNOWN_UNTESTED` with reasons.
- Coverage guard: a test that imports every module in 3.3 and asserts each
  `messages.create(` call site sits inside `trace_generation` (a static
  scan, so a new bypass fails CI).
- Docs: `docs/observability.md` rewritten sections (attribute table,
  content policy, relay), `config/instance.yaml.example`, CHANGELOG
  fragment with the `**BREAKING**` line.

### 3.11 Rollout

Order of independent pieces (each mergeable alone; the migration last):

1. Identity minimisation + generation cache tokens + coverage wraps
   (no schema).
2. Context + record + span attributes + cost on spans (no schema).
3. Turn id, turn/tool spans, broker child linkage, `turn_id` on frames
   (no schema; `chat_messages.turn_id` lands with the migration).
4. Content policy + pseudonymised mode + relay gating (no schema).
5. Migration + repositories + ledger writes + read endpoints + CLI +
   package table + admin page section + feedback endpoint/UI + memory
   provenance + retention.

A deployment that relied on `AGNES_OTEL_CAPTURE_CONTENT=1` must add the
policy record to keep exporting content; the changelog and
`docs/observability.md` say exactly that.

## 4. Decisions taken (and why)

- **New `llm_calls` table rather than widening `llm_usage`.** `llm_usage`
  is a frozen DuckDB↔PG pair; widening it means a DuckDB schema step the
  A3 ratchet forbids, and its semantics (agent budget) are narrower than a
  call ledger. The new table is Postgres-only by construction.
- **Turn linkage through coordination, not `traceparent`.** The engine
  does not propagate and this repository cannot change that; the
  coordination key gives real parent-child spans today and stays correct
  if the engine ever starts sending `traceparent` (the broker would then
  prefer the header).
- **Policy in `instance.yaml`, not an environment variable.** The variable
  is a switch; the question is who agreed to what. A record with a basis
  and an approver is the enforcement of that question, not a tuning knob.
- **Relay strips rather than refuses.** Refusing the `kai_otlp` scope would
  lose the structural spans that answer question 1; stripping keeps them
  and removes only the content.
- **Feedback keyed on the turn, not the message.** The client learns the
  turn id from the frames it already receives; the message id is not on the
  wire and the assistant row is written after the frame is broadcast.
- **Builder prompts exported under policy, not stored.** Storing them is a
  new retention surface for admin-authored content; the export answers the
  question where a collector exists, and the decision to store can be
  taken later with evidence.
