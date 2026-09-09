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
  the rows of one session / turn / job / user, paged with a `before` +
  `before_id` keyset cursor so rows sharing a timestamp are never skipped;
  buffered rows reach the ledger within 30 s even on an idle instance, and a
  transient ledger write failure holds its rows for the next flush instead
  of dropping them; a
  streamed completion that never reached its stop reason is recorded as
  `incomplete` rather than `ok`, and the corpus's `has_error` counts it),
  the new "LLM cost" section on
  `/admin/telemetry`, and the `agnes_llm_calls` table in the `agnes-usage`
  package. `retention.llm_calls_days` prunes it (default 0 = forever). See
  `docs/observability.md` → *LLM call ledger*.
- A turn record now says when its turn closed, so a completion that finishes
  after the next turn started, and a memory written outside any turn, record
  no turn rather than the wrong one. An interrupted answer and a forwarded
  (role-split) turn carry the same `turn_id` and message id as the user
  message that started them, and so does a question redelivered after a
  restart. A conversation written before messages carried structured parts
  still reports its tool calls, read from the legacy column.
- **A real trace per chat turn.** ChatManager mints a `turn_id` per user message,
  opens an `agnes.chat.turn` span with one `agnes.chat.tool <tool>` child per tool
  call, and the broker parents its completion spans under it via a coordination
  record — no engine-side propagation needed. The same `turn_id` rides every
  frame, `chat_messages.turn_id` (on the user row and the assistant row, so a
  question and its answer pair up — `GET /api/chat/sessions/{id}/messages` rows
  carry it too), `usage_turns.turn_uuid` and `llm_calls.turn_id`.
- **Thumbs on chat answers.** `POST /api/chat/sessions/{id}/feedback` (`up`/`down`
  plus an optional comment, one row per turn and user, accepted only for a turn
  the session's own messages carry; audited as `chat.feedback`),
  rendered on every completed assistant bubble in the web chat; admins read the
  queue with `GET /api/admin/telemetry/feedback` (`agnes admin usage feedback`),
  which orders and filters by when a verdict last CHANGED, so a rating
  revised today shows up today and retention counts from the revision.
  Agent memories now record the turn and message that wrote them
  (`agent_memories.source_turn_id`/`source_message_id`).
- **A queryable corpus of whole conversations, for evaluation.**
  `GET /api/admin/conversations/corpus` (`agnes admin conversations export`)
  pulls one complete record per chat session — every surface, transcript, tool
  calls, feedback and memory writes joined in, never truncated — under the same
  content-export policy as the OTel export. Postgres-only; a pull is refused
  with `403 content_export_disabled` when the policy is off, has no recorded
  basis, or excludes the `chat` workload. Its default upper bound lags by the
  same five-minute settle window the push sink uses, so a pull never catches
  a turn mid-write; an explicit `until` is honoured as given.
- **The corpus can also be pushed on a schedule.**
  `observability.conversation_export` (`endpoint`, `headers_secret_env`,
  `interval_minutes`, `surfaces`) turns on a `conversation-export` worker job
  that delivers newline-delimited JSON batches to the endpoint (at most 200
  records or 8 MiB each; three retries with backoff on a 5xx or a connection
  error, never on a 4xx), advancing a Postgres-persisted keyset watermark
  (`last_message_at`, session id) only after a 2xx, under the same
  content-export policy as the pull. The watermark is keyed by the delivery
  configuration (`endpoint` + `surfaces`), so repointing the sink re-delivers
  the corpus from scratch and the destination must upsert by `thread_id`; a
  conversation is exported once its last message is five minutes old, so a
  turn in flight is never exported half-finished. The endpoint must be an
  `https://` URL to a named host (plain `http://` only to loopback) without
  credentials, and `AGNES_REMOTE_ATTACH_HOST_ALLOWLIST`, when set, applies to
  its host too. A single conversation whose own line exceeds the 8 MiB batch
  cap is still offered to the destination, and only a refusal is stepped
  over and counted, so an outsized transcript is neither dropped unasked nor
  able to block every conversation behind it. A thumbs verdict that lands
  after a conversation was delivered re-sends that record on the next tick
  (a second, coarser watermark tracks feedback changes; up to 500 sessions a
  run, counted as `refreshed`, narrowed by the same `surfaces` allowlist,
  resumable on a keyset so a burst wider than the cap is not lost, and held
  back while the conversation itself is still mid-turn), so the corpus's
  quality signal is not frozen at export time. A run that fails mid-walk is audited as
  failed, never raised into the worker. Migration `0118_export_watermarks`.
- Generation spans now carry prompt-cache tokens, `agnes.cost_usd`,
  `agnes.workload`, `agnes.purpose`, `agnes.turn_id`, `agnes.job_id` and
  `agnes.subject_id`; the `llm_generation` log line gains `workload`, `purpose`
  and `cost_usd`.

### Changed
- **BREAKING** Prompt and completion text now leaves the instance only under a recorded
  policy: `observability.content_export` in `instance.yaml` (`mode: off | pseudonymized |
  full`, `placement`, `basis`, `approved_by`, `approved_at`, `workloads`).
  All four consent fields are required: a mode with no basis, no approver, no
  placement or no usable `approved_at` date is treated as `off` and says so,
  because an undated approval cannot be told apart from one that predates the
  configuration it is supposed to cover.
  `AGNES_OTEL_CAPTURE_CONTENT` is now a deprecated alias that no longer enables
  content export on its own — a deployment that relied on it must add the policy
  record to keep exporting content. A mode without a recorded basis is treated as
  `off` and logged at startup; the effective policy is written to the audit log as
  `observability.content_export`. `pseudonymized` runs every exported text through
  the instance anonymizer (feedback comments included), and the `workloads`
  allowlist lets an operator open `builder`/`corporate_memory` content for
  quality work while keeping `chat` at `off` — a `workloads` value with no valid
  entry (a typo, a mapping) disables content export rather than widening it,
  and a bare string is read as a one-entry list. The embedded engine's OTLP
  relay obeys the same policy: under `off` it
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
- Deleting a conversation, or purging an account, now takes its feedback rows
  with it and strips the session, turn and user ids from the matching
  `llm_calls` rows — the spend record survives (deleting it would rewrite cost
  history) but stops naming anyone. `retention.chat_feedback_days` puts a
  clock on feedback comments through the daily retention sweep.
- A request a provider refuses outright — the anonymizer's temperature retry
  is the case in point — now records its own zero-cost error row beside the
  attempt that succeeded, so a rejection is visible in the call counts
  without being priced twice.
- Every batch extraction outcome now reaches the ledger: a result that
  errored, was canceled, expired or never came back writes a zero-cost
  `llm_calls` row, and an answer whose document vanished before it could be
  ingested writes a normal priced row, since those tokens were really spent.
  Previously only the succeeded-and-ingested path was recorded, so a failed
  batch left no trace in the call counts or the error summary at all.
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
- New Alembic migration `0117_llm_observability` (`llm_calls`,
  `chat_message_feedback`, `chat_messages.turn_id`,
  `agent_memories.source_turn_id`/`source_message_id`) — Postgres-only, A3
  ratchet; the frozen DuckDB app-state backend keeps answering a typed `501`
  for the new read routes and silently no-ops the ledger writes.
