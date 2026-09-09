### Fixed
- **`mcp.tool_call` and `chat.tool_call` audit rows now carry the tool's measured
  `duration_ms`.** Both rows were written before the tool ran, so the observability
  KPIs (`/api/admin/observability/kpis?action_prefix=mcp.tool_call`) reported the
  transport and auth overhead as the tool's latency — a p95 of a few hundred
  milliseconds for searches that take seconds. The MCP dispatch wrapper now times the
  call and writes the row after it returns, with `result` set to `success` or
  `error:<Exception>` and the arguments still never logged; the chat manager pairs each
  `tool_call` frame with its `tool_result` on `tool_use_id` and stamps the wall time
  between them (an approval wait included), with a failed result marked `error`. A call
  whose result never arrives is still recorded at turn end, without a duration.

### Added
- **Per-turn LLM latency, measured at the secret broker.** Every session-bound
  completion now records its wall time (request start → last upstream byte) and
  time-to-first-byte next to its token usage; the chat manager sums them per turn onto
  the assistant message as `llm_calls` / `llm_duration_ms` / `llm_ttfb_ms` (Postgres
  app-state only — Alembic revision `0115_chat_messages_llm_timing`, no DuckDB schema
  step). `GET /api/admin/telemetry/chat-cost` and `agnes admin usage chat-cost` expose
  them per session with per-call averages and a `timing_accounting` marker, so "how
  long does the model take per call" is read from the record rather than inferred from
  log timestamps. See `docs/observability.md` → *Chat cost*.
