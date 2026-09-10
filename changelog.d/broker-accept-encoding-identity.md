### Fixed
- **Non-streaming chat completions are no longer billed as free.** The chat
  secret broker forwarded the in-sandbox SDK's `accept-encoding: br, gzip,
  deflate` verbatim, so the provider answered in Brotli — which this image's
  httpx cannot decode. Every non-streaming completion therefore reached
  `parse_usage` as opaque bytes and landed in the `llm_calls` ledger as 0
  tokens / $0.00 (eight such rows per chat turn, beside the two streamed ones
  that parsed because SSE comes back uncompressed), and its tokens never
  reached the per-turn counters on `chat_messages` or an agent's
  `token_budget_monthly`. The same undecoded body was also handed to the
  sandbox under a bare `application/json`. The broker now asks upstream for
  `identity`, as the MCP relay already does for the same reason.
