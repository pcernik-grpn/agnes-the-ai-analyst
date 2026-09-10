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
- **The Files panel no longer asks the engine about a conversation that has
  no sandbox yet.** It polls this route the instant a chat opens, well
  before the first turn can mint one — every one of those polls was landing
  a "chat not found" 404 on the engine's own log, for a well-formed id the
  engine simply hasn't seen. The listing now checks the session's own
  `sandbox_id` first and skips the round trip when it is unset, answering
  with the same "no files here yet" shape a 404 already produced.

### Internal
- The `kai-agent` development stub now mirrors the real engine's `400` for a
  malformed chat id on its sandbox file routes, so this class of drift is
  caught by the stub-paired end-to-end tests rather than only live.
