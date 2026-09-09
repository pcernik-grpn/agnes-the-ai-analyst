### Fixed
- **`fact_search` / `POST /api/facts/search` / `agnes facts search` no longer
  time out on a short name lookup over a large fact graph.** On a graph of
  ~830k facts a three-letter `q` hit the 5 s statement timeout (8 of 35 tool
  calls in one sample) even though only ~120 aliases matched: the planner
  had no selectivity estimate for the alias `ILIKE` inside `EXISTS`, sized
  the candidate set at ~416k rows and scanned all of `claims` and
  `fact_aliases` instead of probing two indexes. Candidates are now selected
  as a ranked, `MATERIALIZED`, `LIMIT`-ed CTE (500 readable name matches per
  call, ordered by the same exact/prefix/substring tiers the result uses),
  which gives the planner a hard cardinality ceiling — 5.3 s → ~0.3 s on
  the same query. Hitting the cap is disclosed with the additive
  `candidates_capped: true` (REST/MCP; the CLI prints a note), so a page is
  never presented as the complete match set; `limit_applied` keeps its own
  meaning. A `q` under four characters must now start a name token
  (`llr` finds `llr-corp` and `acme-llr`, not `fullrange`) instead of
  matching anywhere — a 2–3 character substring matches a large share of any
  real alias set. A search that still outlives the statement timeout answers
  a typed, hinted error — `504 {"reason": "facts_search_timeout", "hint":
  …}` on REST, the same hint as the MCP tool error — instead of the raw
  `psycopg.errors.QueryCanceled` text.
