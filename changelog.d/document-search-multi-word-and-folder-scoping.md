### Fixed
- **A multi-word document search no longer comes back empty on a corpus that
  holds the answer.** Postgres candidate selection ran `plainto_tsquery`
  alone, whose semantics are AND — every term had to occur in the SAME chunk
  — so an ordinary natural-language question selected no candidates at all.
  Observed live: four of six searches in one chat session returned
  `results: []`, among them "Riveron AI Execution workstreams scope pods
  sprint"; no chunk carried all seven words, and the document the agent had
  been asked to write from was never read, so the reply was assembled from
  an unrelated notes file. The all-terms pass still runs first and unchanged
  (it is the most precise candidate set there is, and when it fills the limit
  nothing else runs), and only when it under-fills is it topped up by an
  any-term pass: one bounded index scan per term, `UNION ALL`-ed,
  de-duplicated, then ranked by `ts_rank_cd`. A window per term rather than
  one window over an OR'd match, because the corpus's commonest term
  otherwise fills it and crowds out the rare term that identifies the
  document. DuckDB-backed instances were never affected — that backend's
  prefilter has always been any-term.
- **Document search results are ranked again instead of all scoring exactly
  `1.0`.** The lexical score counted only whether a term was PRESENT, so
  candidates matching the same term set tied at an identical raw score, which
  min-max normalization's all-equal branch mapped to 1.0 — a result set that
  reported itself as uniformly perfect while its order was really the
  chunk-id tie-break. AND-semantics candidate selection (above) guaranteed
  that tie for every multi-word query. Scores now carry a saturating
  term-frequency factor (`tf/(tf+1)`, BM25's shape), so a passage that says
  more about the query outranks one that mentions it once, while rarity keeps
  its full weight — a chunk repeating a common word cannot out-rank one
  carrying the distinctive term.
- **`collection_get`'s `q` filter works on the chat sandbox's MCP server.**
  The stdio server sent `limit`/`offset`/`q` to `GET /api/collections/{id}`,
  which declares none of them, so FastAPI dropped all three without a word:
  `q="Riveron"` came back as that endpoint's own unfiltered 25-file preview,
  and an agent reading the result concluded the collection held no matching
  file. It now pages through `/files` like the HTTP foundation server always
  did. Signature parity had passed the whole time — both servers accepted the
  same arguments, one ignored them — so the parity suite gained a guard on
  the endpoint each server actually calls.
- **A document search whose candidate limit filled before it matched anything
  is refused instead of returning an empty list.** `results: []` is
  indistinguishable from "no such document exists", so an agent handed one
  stopped looking. It now answers `422 search_query_too_broad` with
  `reason: "capped_no_match"` and says the search read only part of the
  documents in scope. `reason: "no_usable_term"` names the pre-existing
  refusal, and `/api/knowledge/search`'s chunk leg no longer describes this
  case as a temporary outage the caller should "retry shortly" — it will fail
  identically until the scope or the words change.
- **A capped search no longer claims your query matched thousands of chunks
  when it matched none.** One `truncated` flag covered two events and the
  disclosure described only the first, so a search whose passage candidates
  numbered zero still reported "more than 5,000 chunks matching your query
  terms" — the cap had been filled by the FILENAME pass, where a term like
  "ai" matches a large share of any real corpus's names because that match is
  substring, not whole word. The response now carries `truncated_source`
  (`body` / `filename` / `both`) and a note that says which happened.

### Added
- **Folder scoping for document search: `path_prefix`.** A crawled bucket is
  routinely ONE collection holding every client's contracts, invoices and
  deal files, so `collection_id` could not express "only this engagement's
  folder" and one client's document had to out-rank thousands of chunks of
  everyone else's paperwork to be seen. Available on
  `GET /api/collections/search`, `collections_search` on both MCP servers,
  and `agnes collections search --path-prefix`; it narrows candidate
  selection on both the body and the filename path, and prefixes match
  literally (LIKE metacharacters escaped — `_` is ordinary in a folder name).
- **Agent guidance for follow-on work: find the folder before searching it.**
  A new "Documents — find the folder before you search it" section in the
  default Workspace Prompt, mirrored as ungated `DOCUMENT_SEARCH_RAILS` for
  persona'd agents (which replace that template wholesale): resolve the
  client's folder first — `collection_get(q=…)`, or the fact graph where the
  `facts` switch is on — then search inside it with `path_prefix`, and read
  the file whole rather than assembling an answer out of excerpts. It also
  says what an empty or capped result does and does not prove, and adds one
  rule the same session earned: **never offer scope you have not verified** —
  when the document that should define a piece of work cannot be found, ask
  for it, rather than turning four items pulled from an unapproved notes file
  into a menu of scope options.
