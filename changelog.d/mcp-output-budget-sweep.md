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

### Internal
- `src/mcp_tooling.py` gained `compact_listing` (the search-tool
  shorten-then-drop algorithm, generalized to any list-shaped tool
  response), `paginate_text`/`paginate_text_response` (page one long
  string, verified against the actual JSON-escaped wire size), and
  `compact_graph_result` (the `fact_edges`/`fact_neighbors` node/edge
  shape — shortens inline claim quotes first, then drops edges from the
  tail and prunes `nodes` to what a surviving edge still references, so a
  caller never sees a dangling reference).
