### Fixed

- Sandbox LLM egress (`POST /api/broker/anthropic`) no longer serialises
  in-flight completions on the event loop. Before forwarding upstream, the
  broker resolved the ticket's session/agent/caller rows and read the monthly
  token budget with synchronous repository calls made directly on the single
  API worker's event loop, so every concurrent completion waited behind the
  others' database round-trips — added latency that scaled with in-flight LLM
  calls while the host stayed idle, and that a sandbox client with its own
  request deadline could abandon. Both reads now run in a worker thread, and an
  embedded-engine (`llm`-scope) completion reuses the session row already read
  for its scope check instead of fetching it twice.
