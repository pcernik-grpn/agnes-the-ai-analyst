# Agnes — data access (available in every repository)

This machine has the Agnes CLI + MCP tools connected to the org's data
platform. When asked about org data, follow this protocol:

1. **Discover first** — `agnes catalog --json`, then `agnes schema <table>`
   and `agnes describe <table> -n 5`. Never `SELECT *` blindly.
2. **Check `query_mode`** per table: `local` → `agnes query "<SQL>"` runs
   on the laptop; `remote` → `agnes snapshot create … --estimate` first,
   or `agnes query --remote` for one-shot server-side execution;
   `server_only` → `agnes query --remote` only.
3. **Reuse snapshots** across questions; `agnes snapshot list` before
   fetching; drop with `agnes snapshot drop <name>` when done.
4. **Business metrics**: look up canonical definitions first —
   `agnes catalog --metrics` / `--show <id> --show <id2>` (repeatable, so
   several definitions cost one call). Never invent metric SQL.
5. **Semantic layer**, where the instance has one: read it in ONE pass —
   `agnes semantic-model context dataset metric relationship` — then work
   from what you read rather than re-fetching per object, and
   `agnes semantic-model validate-query "<SQL>"` before running.

Full protocol: `agnes skills show agnes-data-querying`. Data freshness is
maintained automatically (SessionStart hook); manual refresh: `agnes update`.
