### Fixed
- **Several MCP foundation tools could exceed the agent sandbox's tool
  output cap even with an explicit `limit`/`k`** — `semantic_model_search`
  (returned every matched model's full document, blowing up on an
  ordinary single-word query), `skills` (every RBAC-visible SKILL.md body,
  uncapped), `documentation_api` (the whole API reference in one call),
  `collection_get` (a page of files could still carry oversized
  `processing_detail` error text), and `fact_edges`/`fact_neighbors`/
  `fact_claims` (a well-connected node's attrs/quotes routinely outgrew
  `limit`'s row cap). Each now shortens or pages its response to fit,
  with an honest `truncated`/`truncated_note` saying what was cut and the
  next step (a smaller `limit`, an `offset` to continue, or a follow-up
  tool call) — never returned whole over budget, never silently short.
  `semantic_model_get` and `documentation_api` gained an `offset` parameter
  to page a long document like `collection_file_read` already does.
- `collection_get`'s response gained `files_next_offset` — the exact
  offset to request next, `null` once nothing remains. It accounts for a
  page compacted for size the same way it accounts for more files existing
  server-side, so a caller following it can never loop on the same files
  forever (the previous compacted-page hint told a caller to retry the
  same `offset`, which returned exactly the files it already had).
- `fact_neighbors`' queried root now always survives output-budget
  compaction as its own node, even when it carries no visible edges (a
  hub's own `attrs` can be large enough on its own to force compaction) —
  previously `compact_graph_result` derived every kept node from a
  surviving edge endpoint, so a root with no surviving edge vanished from
  its own traversal result.
- `paginate_text_response` (`semantic_model_get`, `documentation_api`) no
  longer returns a still-oversized page when JSON-escaping a page densely
  packed with expensive characters (backslashes, control characters) makes
  the escaped page larger than its raw length allowed for — it now retries
  from a smaller floor instead of giving up on the first overshoot.

### Internal
- `src/mcp_tooling.py` gained `compact_listing` (the search-tool
  shorten-then-drop algorithm, generalized to any list-shaped tool
  response), `paginate_text`/`paginate_text_response` (page one long
  string, verified against the actual JSON-escaped wire size), and
  `compact_graph_result` (the `fact_edges`/`fact_neighbors` node/edge
  shape — shortens inline claim quotes first, then drops edges from the
  tail and prunes `nodes` to what a surviving edge still references, so a
  caller never sees a dangling reference). `compact_graph_result` also
  takes an optional `required_node_ids` to pin a node (`fact_neighbors`'
  root) regardless of edge survival.

### Changed
- **BREAKING: `documentation_api` now returns a `dict`
  (`{"content", "offset", "next_offset", "total_chars", "truncated"}`)
  instead of a plain `str`.** The guide is one long document, always well
  over the tool output budget on its own, so it is now paged the same way
  `collection_file_read` already is rather than returned whole. An
  external MCP client that parsed the old bare-string result must read
  `content` instead, and follow `next_offset` for a document over ~20k
  characters.
- **BREAKING: `semantic_model_get`'s `document` field is now a PAGE, not
  always the complete document.** A model document over ~20k characters
  used to always come back whole in one call; it is now paginated like
  `collection_file_read`, with `total_chars`/`next_offset`/`truncated`
  describing the rest. A caller that assumed `document` was always
  complete must check `truncated` and follow `next_offset` to read past
  the first page.
