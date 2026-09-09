### Added
- **One record per LLM call, priced once, from every call site.** A new Postgres-only
  `llm_calls` ledger holds who / for what / in which turn / at what price for every
  brokered chat completion AND every server-side generation (the five builder
  endpoints, document fact extraction including batch results, OCR, vision
  captioning, the NER anonymizer, auto-title, chat readiness probes, corporate
  memory, knowledge digests, table autodoc, ontology, session verification, the
  store guardrails, and the admin usage question assistant under its own
  `admin_ask` workload), with the rates stored beside
  the figure (`priced_as`). Read it with `GET /api/admin/telemetry/llm-cost`
  (`agnes admin usage llm-cost`, grouped by workload / agent / user / model /
  purpose), `GET /api/admin/telemetry/llm-calls` (`agnes admin usage llm-calls`,
  the rows of one session / turn / job / user), the new "LLM cost" section on
  `/admin/telemetry`, and the `agnes_llm_calls` table in the `agnes-usage`
  package. `retention.llm_calls_days` prunes it (default 0 = forever). See
  `docs/observability.md` → *LLM call ledger*.
- **A real trace per chat turn.** ChatManager mints a `turn_id` per user message,
  opens an `agnes.chat.turn` span with one `agnes.chat.tool <tool>` child per tool
  call, and the broker parents its completion spans under it via a coordination
  record — no engine-side propagation needed. The same `turn_id` rides every
  frame, `chat_messages.turn_id` (on the user row and the assistant row, so a
  question and its answer pair up — `GET /api/chat/sessions/{id}/messages` rows
  carry it too), `usage_turns.turn_uuid` and `llm_calls.turn_id`.
- **Thumbs on chat answers.** `POST /api/chat/sessions/{id}/feedback` (`up`/`down`
  plus an optional comment, one row per turn and user; audited as `chat.feedback`),
  rendered on every completed assistant bubble in the web chat; admins read the
  queue with `GET /api/admin/telemetry/feedback` (`agnes admin usage feedback`).
  Agent memories now record the turn and message that wrote them
  (`agent_memories.source_turn_id`/`source_message_id`).
- **A queryable corpus of whole conversations, for evaluation.**
  `GET /api/admin/conversations/corpus` (`agnes admin conversations export`)
  pulls one complete record per chat session — every surface, transcript, tool
  calls, feedback and memory writes joined in, never truncated — under the same
  content-export policy as the OTel export. Postgres-only; a pull is refused
  with `403 content_export_disabled` when the policy is off, has no recorded
  basis, or excludes the `chat` workload.
- **The corpus can also be pushed on a schedule.**
  `observability.conversation_export` (`endpoint`, `headers_secret_env`,
  `interval_minutes`, `surfaces`) turns on a `conversation-export` worker job
  that delivers newline-delimited JSON batches to the endpoint (at most 200
  records or 8 MiB each; three retries with backoff on a 5xx or a connection
  error, never on a 4xx), advancing a Postgres-persisted keyset watermark
  (`last_message_at`, session id) only after a 2xx, under the same
  content-export policy as the pull. A run that fails mid-walk is audited as
  failed, never raised into the worker. Migration `0115_export_watermarks`.
- Generation spans now carry prompt-cache tokens, `agnes.cost_usd`,
  `agnes.workload`, `agnes.purpose`, `agnes.turn_id`, `agnes.job_id` and
  `agnes.subject_id`; the `llm_generation` log line gains `workload`, `purpose`
  and `cost_usd`.

### Changed
- **BREAKING** Prompt and completion text now leaves the instance only under a recorded
  policy: `observability.content_export` in `instance.yaml` (`mode: off | pseudonymized |
  full`, `placement`, `basis`, `approved_by`, `approved_at`, `workloads`).
  `AGNES_OTEL_CAPTURE_CONTENT` is now a deprecated alias that no longer enables
  content export on its own — a deployment that relied on it must add the policy
  record to keep exporting content. A mode without a recorded basis is treated as
  `off` and logged at startup; the effective policy is written to the audit log as
  `observability.content_export`. `pseudonymized` runs every exported text through
  the instance anonymizer, and the `workloads` allowlist lets an operator open
  `builder`/`corporate_memory` content for quality work while keeping `chat` at
  `off`. The embedded engine's OTLP relay obeys the same policy: under `off` it
  strips `gen_ai.prompt` / `gen_ai.completion` / `gen_ai.input.messages` /
  `gen_ai.output.messages` (flagging `agnes.content_stripped`) and drops log
  bodies, under `pseudonymized` it rewrites them, and an undecodable batch is
  refused (`400 otlp_batch_undecodable`) rather than forwarded blind.
- `agnes.user_email` is no longer exported on any span (`agnes.user_id` stays),
  and the broker no longer reads the session row for the native sandbox's `main`
  scope on the span path.

### Fixed
- Usage is now recovered for a streamed completion that overflows the broker's
  usage mirror: tokens, cost, model and stop reason are parsed from bounded
  head/tail buffers and still land on the span and in `llm_calls`
  (`response_truncated: true`), instead of the row and the span both losing
  usage entirely past the mirror's cap.

### Internal
- Every LLM call site is now traced with `trace_generation`, including ones that
  previously bypassed it entirely (document fact extraction, OCR, vision, the
  NER anonymizer, auto-title, the admin usage assistant) — closed by a static
  coverage guard (`tests/test_llm_coverage_guard.py`) that scans every module
  the design names plus the LLM providers, requires every `extract_json` call to
  set an `llm_context`, and fails CI on a new bypass. Extraction rows carry the
  document's id as `subject_id`; Vertex-hosted generations are labelled
  `gcp.vertex_ai` everywhere, matching the chat broker.
- The worker runtime binds `job_id` into the LLM call context so every
  generation inside a background job is attributed without per-handler code.
- New Alembic migration `0114_llm_observability` (`llm_calls`,
  `chat_message_feedback`, `chat_messages.turn_id`,
  `agent_memories.source_turn_id`/`source_message_id`) — Postgres-only, A3
  ratchet; the frozen DuckDB app-state backend keeps answering a typed `501`
  for the new read routes and silently no-ops the ledger writes.
