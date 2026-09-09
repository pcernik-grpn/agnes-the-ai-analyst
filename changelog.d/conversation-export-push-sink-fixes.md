### Fixed
- **The conversation-corpus push sink no longer re-sends its trailing conversation.** The
  watermark now persists the exact `(last_message_at, id)` position of the last delivered
  session (`export_watermarks.cursor_id`), not a bare timestamp — closing a bug where the
  last-delivered conversation was re-POSTed on every scheduler tick, including every tick
  once a forked session's `last_message_at` diverged from its messages' own timestamps.
- **`observability.conversation_export.surfaces` now filters at the query, not after
  delivery.** An excluded surface is no longer fetched and re-discarded on every tick.
- Push-sink delivery is retried three times after the first attempt (four attempts total,
  exponential backoff: 1s/2s/4s) on a 5xx or connection error, matching the documented
  "retried three times"; a 4xx (including 429) is never retried within a run.
- A push-sink run that fails unexpectedly mid-walk is now caught, logged and audited with
  `result: "failed"` instead of propagating the exception out of the worker job.
