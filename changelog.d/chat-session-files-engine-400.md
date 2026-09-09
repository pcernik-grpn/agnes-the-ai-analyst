### Fixed
- **The chat session Files panel could fail with an "engine unavailable"
  error, spamming a full traceback on every poll, for a conversation that
  predates the instance's switch to the `kai-agent` chat provider.** Such a
  session's older `chat_<hex>` id cannot key the engine's uuid-typed chat
  table, so the engine answers `400` rather than the `404` an
  unknown-but-well-formed id gets — previously read as an outage. A `400`
  from the engine now degrades the same way a `404` already did (an honest
  "no files here", falling back to any harvested copies), and a genuine
  engine outage logs its full traceback only once per session instead of on
  every poll.

### Internal
- The `kai-agent` development stub now mirrors the real engine's `400` for a
  malformed chat id on its sandbox file routes, so this class of drift is
  caught by the stub-paired end-to-end tests rather than only live.
