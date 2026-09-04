# Table access policies

Row filtering and column masking for registered tables, enforced server-side on every read. This is a **second, optional layer** on top of the RBAC model in [`RBAC.md`](RBAC.md): a grant (direct or via a data package) still decides whether a group can reach a table at all; a policy narrows *what* a caller sees once they're allowed to reach it. Design rationale and the full architecture (the resolver, the AST rewrite, the BigQuery transpile, the enforcement ratchet): [`superpowers/specs/2026-08-11-table-access-policies-design.md`](superpowers/specs/2026-08-11-table-access-policies-design.md).

## What a policy is

An admin attaches **one SQL `SELECT`** to a registered table. From that point on, every server-side read of the table — `agnes query`, the catalog's "Preview data" sample, `agnes snapshot create`, `agnes schema`, an MCP tool call, a chat agent's query — substitutes the policy body for the table, with the caller's identity available as bound variables:

```sql
SELECT * EXCLUDE (national_id, email), md5(email) AS email
FROM invoices
WHERE list_contains($user_groups, cost_center)
```

The policy is *data* — stored on the table, versioned via the audit log, one per table (there is no per-audience list; branch inside the SQL with `CASE` or `list_contains($user_groups, …)` instead). No policy means no behavior change: every path here short-circuits when a table's `access_policy_sql` is `NULL`.

**Attachment downloads are a row read too.** `GET /api/attachments/{source}/{id}/download` serves one row's file, so it first checks that the row is visible in the caller's policied view — through the policy's *projection*, exactly as any other read. A policy that masks or excludes the attachment identifier column therefore makes those rows unaddressable for that caller (404, indistinguishable from a missing row): a caller who cannot see an identifier through any policied surface has no legitimate way to hold it, and checking identity against the raw table instead would turn the download route into an oracle for confirming which hidden identifiers exist.

## Scope: only tables that never leave the server

A policy is only enforceable where Agnes evaluates the read. Once a parquet is on an analyst's laptop, the analyst holds unfiltered bytes with no server in the loop — so a policy may only be attached to a table that is **not distributed**:

```
query_mode = 'remote'   OR   server_only = TRUE
```

Both are equally undistributed; pick by table size and freshness, not by which one supports policies. `server_only` is documented in [`admin/query-modes.md`](admin/query-modes.md) — mark a table `server_only` (keeping `query_mode: local` or `materialized`) to make it queryable but never synced by `agnes pull`.

The server enforces this both ways, not just at attach time:

- **You cannot attach a policy** to a table that is neither `remote` nor `server_only` — the write is rejected with `access_policy_requires_undistributed`, naming the fix (set `server_only=true` first, or switch to `query_mode='remote'`).
- **You cannot make a policied table distributable again** — clearing `server_only`, or moving `query_mode` to `local`, is rejected the same way while a policy is attached. Clear the policy first if you really mean to distribute the table.
- **Nor can a re-registration do it behind the API's back.** The two rules above are enforced at the admin endpoints, but `table_registry.register()` is an upsert every non-admin writer also reaches — a connector's auto-discovery, the boot-time internal-table refresh, a collection file re-ingest — and none of them knows about policies. So the repository itself holds the same line on both backends: an upsert onto a policied row preserves `server_only` when the caller does not name it, and one that would leave the row distributable is refused (`PoliciedRowDistributionError`, surfaced as the same `422 access_policy_requires_undistributed`). Re-ingesting the file behind a policied **collection** table is refused one layer higher still, with `access_policy_protected_row` — see the errors table below.
- **A second row with no policy of its own cannot point at the same physical source.** Registering (or editing) a row whose `source_query` / `(bucket, source_table)` / BigQuery FQN resolves to a *policied* table's underlying data is rejected (`access_policy_physical_source_conflict`). The check resolves each row to a **canonical physical identifier** per engine rather than comparing the strings a row happens to spell its pointer with — BigQuery `(project, dataset, table)`, Databricks `(catalog, schema, table)`, Snowflake `(database, schema, table)`, Keboola `(connection, bucket, table)` — so a `bq_fqn` and a `bucket`+`source_table` row for one table, or a Databricks `sales` and `main.sales` bucket, are recognized as the same source; and for a `materialized` row it also parses the `source_query` in the engine's own dialect and resolves **every table the SQL reads**, so a materialization over a policied table is refused no matter how the reference is aliased, commented or quoted (a `source_query` that does not parse fails closed beside a policied row of the same engine, with the parse failure named) — otherwise that second registry id hands out exactly the unfiltered rows the policy exists to withhold. **Distributability does not decide whether this fires**, only the wording of the rejection: an earlier version exempted `server_only` / `remote` twins on the reasoning that `agnes pull` is how data escapes, and a live instance disproved it — `/api/query` resolves such a row by its own name server-side and returns the raw rows to anyone granted it, the same disclosure minus the parquet. The check runs in **both** directions: attaching a policy to a table that already has such a twin is refused the same way, naming the offending row, because nothing would ever write that twin again for the register/edit-time check to catch. Keboola bulk auto-discovery classifies such a source as `invalid` (visible in `discover-and-register`'s dry run) rather than registering it. **An omitted or blank `connection_id` counts as "any connection of that source type" for this check** — registering a second row over the same `(bucket, source_table)` with `connection_id` left unset does not escape it just because the policied row happens to pin one (or vice versa); only two rows that both pin *different*, non-empty `connection_id`s are treated as genuinely different projects. On a multi-connection instance, pin `connection_id` on both rows to disambiguate when they really are unrelated sources. If you genuinely need a second row over one source, give it **its own policy** in the same write — two policied rows over one source are legal, since every read still goes through a policy. To unwind such a pair, repoint one row first (its policy travels with it, so clearing it afterwards succeeds) or unregister one; clearing one policy while the other still covers the source is refused, because that is precisely the unpolicied-twin shape.

Two things a policy does **not** do, worth stating for the admin dialog as much as here:

- **It is not immediate for laptops that already hold a copy.** If the table was distributed before you attached a policy (or before you set `server_only`), the copy clears on the analyst's *next* `agnes pull` (which already prunes local copies of de-authorized/`server_only` tables) — not the instant you save.
- **It is not a recall.** Snapshots, notebooks, and exports already taken from the unfiltered table are out of reach by construction. Attaching a policy stops the next disclosure; it does not undo a previous one. (A `agnes snapshot create` slice taken *through* a policy does track staleness — see [Snapshots and staleness](#snapshots-and-staleness) below.)

## The feature flag

Attaching (writing) a policy is gated behind `access_policies.enabled` (env `AGNES_ACCESS_POLICIES_ENABLED`), **off by default**. Turn it on in `/admin/server-config` or via the env var before the Access column on `/admin/tables` will accept a save. See [`feature-flags.md`](feature-flags.md#current-flags) for the general convention.

The flag only gates *new* attachments. A table that already carries a policy stays protected — and the distribution interlocks above stay enforced — regardless of the flag's later state. Turning the flag off after policies exist does not strip them; it only stops an admin from attaching *new* ones until it's re-enabled. Clearing a policy (saving an empty body) always works, flag or no flag — it's a safety valve, not a new grant.

## Authoring a policy

### Variables

Three bound values, and only these three — Agnes reads identity from your identity provider and source systems, it does not invent new attributes:

| Variable | Type | Source |
|---|---|---|
| `$user_email` | `VARCHAR` | the authenticated caller's email |
| `$user_id` | `VARCHAR` | `users.id` |
| `$user_groups` | `LIST(VARCHAR)` | the caller's **live** group membership |

`$user_groups` is read through the same live path table-grain RBAC already uses (`get_accessible_tables` → `StackResolver`), so it never diverges from what that check just decided — group changes take effect on the next request, same as everywhere else in Agnes; there is no separate cache to invalidate.

**A variable may only stand where a value stands.** `FROM $table`, `EXCLUDE ($col)`, or any table/column/alias-name position is rejected at save time — the admin fixes the query's *structure*; the caller only ever supplies *values*. Values bind as real DuckDB (and, on a remote table, BigQuery) named parameters — never string-interpolated — so a group literally named `Robert'); DROP TABLE users;--` is just an inert list element. The one value-position exception: identity variables are also rejected as the *pattern* side of `LIKE` / `ILIKE` / `SIMILAR TO` or a regex function (`owner LIKE $user_email` would let a group or user literally named `%` — no character class validates group names elsewhere in Agnes — silently widen the match to everyone). The resolver re-derives that same check from the stored policy text on every read, so a body that reaches the registry without passing save-time validation (a hand-edited row) is refused live rather than bound; a `%`/`_` in a group name is otherwise harmless and binds as itself, because in every other position a bound parameter is a value, never a pattern — `sales_cz` and `CC_A` are ordinary group names.

### Row filtering

Add a `WHERE` that references `$user_email` / `$user_id` / `$user_groups`, or a column that a mapping table resolves them against (see below):

```sql
SELECT * FROM invoices WHERE list_contains($user_groups, cost_center)
```

For anything beyond a simple predicate, `CASE` is available — and is exactly where the dangerous bug hides. The example below reads defensively, but the risk is a stray `ELSE TRUE` (or `ELSE 1=1`) admitting every group you didn't enumerate:

```sql
WHERE CASE
    WHEN list_contains($user_groups, 'finance_admin') THEN TRUE
    WHEN list_contains($user_groups, 'finance_eu')     THEN region = 'EU'
    ELSE FALSE   -- the bug: writing `ELSE TRUE` here silently admits
                 -- every group not listed above
END
```

Always preview a policy like this as more than one persona before trusting it (see [Previewing before you trust it](#previewing-before-you-trust-it)).

### Column masking

`SELECT * EXCLUDE (col)` (or naming columns explicitly) drops a column outright — it disappears from `agnes schema`, the catalog, and every query result for a non-admin, not just from the row content.

For **redaction** (hide the value, keep the column), replace it with a literal or a `CASE`:

```sql
SELECT
    * EXCLUDE (national_id, email),
    CASE WHEN list_contains($user_groups, 'finance_admin')
         THEN email ELSE NULL END AS email
FROM invoices
WHERE list_contains($user_groups, cost_center)
```

For **pseudonymization** (keep a stable, joinable value without exposing the original), `md5(col) AS col` is available. Be clear-eyed about what it buys you: `md5` is a **pseudonym, not a mask**. An unsalted hash over a low-entropy domain (an email, a short id) is reversible by dictionary in minutes, and the whole point of using it instead of `NULL` is that the hashed value still joins across tables — so treat it as "not shown in plaintext", not as "protected".

**`agnes_hmac(col)` is the keyed version, and the one to reach for.** Builder mask: `pseudonymize_keyed`. It returns the hex HMAC-SHA256 of the value under this instance's own anonymization key — the same write-once key the anonymize-in-front extraction pipeline uses (`src/anonymization_key.py`: env var if an operator minted one, otherwise generated and stored encrypted in the vault the first time something needs it). It keeps everything `md5` offers — a stable value that still joins across tables *on this instance* — and takes the dictionary attack away: without the key, `alice@example.com` cannot be hashed and matched back. Two instances never produce the same pseudonym for the same person, so pseudonyms cannot correlate across tenants either. Three things to know:

- **DuckDB-only.** The key must never travel to a remote engine, so this works on tables that never leave the server (`server_only`, or a `materialized` row). A policy on a `query_mode='remote'` table that calls it is rejected at save time (`policy_function_duckdb_only`) — use `md5()` there, or make the table `server_only`.
- **Not reversible without the key, and not reversible with it either** — it is a one-way digest; holding the key only lets you re-derive the pseudonym for a value you already have. Callers cannot compute it themselves: `agnes_hmac()` is reserved for policy bodies and refused in caller-authored SQL (otherwise any analyst could hash a candidate value and match it against the masked column, which is the exact attack the key prevents).
- **Key rotation is not supported**, deliberately. A new key rewrites every pseudonym — the old and new digests of the same value are unrelated — so every join key already handed out silently stops matching. `src/anonymization_key.py`'s module docstring spells out why provisioning there is write-once and why there is no `rotate()` anywhere in this codebase.

For genuine masking — where no joinable token should survive at all — redact with `CASE`/`NULL`/a literal instead of reaching for either hash.

The no-SQL builder can also emit two **partial** masks — `last4` (`****6789`) and `email_partial` (`j*****@example.com`) — which keep the column's name and `VARCHAR` type, use a fixed-width asterisk run so the redaction never publishes the original's length, and redact a too-short value or an address without an `@` in full rather than half-revealing it. Both are text-only, and both are built from the existing allowlist (`CASE`/`LENGTH`/`CONCAT`/`SUBSTRING`/`REGEXP_REPLACE`/`LIKE`), so nothing was widened to add them.

The function and construct allowlist is intentionally narrow and closed (logical connectors, `CASE`/`IF`/`COALESCE`/`NULLIF`, `CAST`, `LOWER`, `UPPER`, `TRIM`, `LENGTH`, `CONCAT`, `SUBSTRING`, the regex family for literal patterns, `md5`, `agnes_hmac`, and the group-membership functions below) — anything else is rejected at save time, not silently ignored. A policy body is arbitrary SQL that runs on the server's analytics connection on every analyst request; the allowlist is what keeps that a bounded escalation instead of an open one.

**A masked column stays in the schema; a hidden one disappears.** `* EXCLUDE (national_id)` removes `national_id` from every schema surface — it is `hidden`. `md5(email) AS email` keeps `email` in the schema (same name, same reported type), but it is `masked`: the returned value is the transform's output, not the base column's raw value. `GET /api/v2/schema/{id}` marks a masked column `"masked": true` (alongside the existing `"hidden": true`/`false`), `agnes schema <table>` appends `(masked by access policy)` to its description in the human render (`--json` stays the raw payload), and the catalog table page badges it in the "Columns" section. The marker comes from a static read of the policy body's own `SELECT` list — a `md5(email) AS email` and a plain `email` pass-through DESCRIBE identically (same name, same `VARCHAR`), so there is no runtime signal to diff; only the SQL text itself says which one an admin wrote.

**Don't re-derive a column `*` still emits.** `* EXCLUDE (national_id), md5(email) AS email` looks right but leaves `email` out of the `EXCLUDE` list, so the star still emits the original *and* the re-derived expression appends a second column with the same name — DuckDB accepts the duplicate silently, and every serializer either keeps the first (plaintext) occurrence under the plain name or renames the second one, putting the unmasked value exactly where a caller expects the masked one. Always exclude a column before re-deriving it under the same name (`EXCLUDE (national_id, email)`, as above) — Agnes rejects a policy whose output has a duplicate column name at save time (`policy_duplicate_output_column`).

### The group-membership idiom

Use `list_contains($user_groups, col)`, not `col IN (SELECT unnest($user_groups))`. Both execute correctly on DuckDB, but only the first survives the remote transpiles cleanly:

```
list_contains($g, unit)        →  BQ:  EXISTS(SELECT 1 FROM UNNEST(@g) AS _col WHERE _col = unit)
                                  DBX: ARRAY_CONTAINS(:g, unit)
unit IN (SELECT unnest($g))    →  a GENERATE_ARRAY/CROSS JOIN construct, ~10× longer
```

The `unnest` form isn't rejected — the save-time validator logs a server-side warning rather than blocking the save — but there's no reason to reach for it over `list_contains`.

### One authored body, several engines

You write the policy once, in DuckDB SQL. Each engine gets it in its own dialect, and — the part that matters — each engine's own **named-parameter** syntax, so the caller's identity is never text inside the statement:

| engine | marker | how values travel |
|---|---|---|
| DuckDB | `$user_email` | native named parameters |
| BigQuery | `@user_email` | `QueryParameter` on the job config |
| Databricks | `:user_email` | the Statement Execution API's `parameters` field |
| Snowflake | `:user_email` | numbered (`:1`, `:2`, …) positional binds — see below |

Databricks needs one extra step for `$user_groups` alone: its API binds **scalars only**, so the array marker is rewritten into `ARRAY(:p0, :p1, …)` over generated scalar markers. The group names still travel as request fields — only how many of them there are becomes visible in the statement. A caller in no groups binds a typed empty array and matches nothing.

Snowflake's resolver transpile arm (`policied_relation(..., dialect="snowflake")`) exists for a genuinely Snowflake-native SQL text surface — a `snowflake_query()` pass-through, or a Snowflake semantic-view `MEASURE()` query that cannot parse as DuckDB SQL at all. A `query_mode='remote'` Snowflake row registered by name, the ordinary case, already reads correctly *today* through the plain `dialect="duckdb"` arm: the row is a DuckDB VIEW over the ATTACHed `sf` catalog (the `snowflake` DuckDB community extension), so the query text is DuckDB SQL bound with DuckDB's own native parameters, never Snowflake-dialect text. Snowflake has no named-bind-variable syntax an external driver resolves against request parameters (`:name` in Snowflake SQL is a *Snowflake Scripting* local-variable reference, valid only inside a stored procedure) — every real Snowflake execution path binds **positionally** (`qmark`/`numeric` paramstyle), so a caller of the transpile arm renumbers every marker via `connectors/snowflake/policy_params.py::bind_policy_parameters`, which also expands `$user_groups` into a bracket array literal (`[:3, :4, …]`) the same way the Databricks module expands its own `ARRAY(...)`.

One function is deliberately outside this: `agnes_hmac()` (the `pseudonymize_keyed` mask) exists only on Agnes's own DuckDB connection, and a remote-table policy that calls it is refused at save time (`policy_function_duckdb_only`). The transpile check below would *not* have caught it — sqlglot does not know the function and happily emits `AGNES_HMAC(...)` for any dialect, which on the warehouse means either nothing or, worse, somebody else's same-named function under a key Agnes does not control.

A policy that fails to transpile to *any* remote engine (BigQuery, Databricks, Snowflake) is rejected at save time (`policy_untranspilable`), not at read time — a policy that denies because it cannot be compiled is an outage wearing an access rule's clothes.

## Mapping tables

The common shape beyond a bare predicate is a join against a table maintained **upstream**, in the source system — a person → cost-centre mapping usually already exists there, and hand-copying it into Agnes creates a second copy that drifts:

```sql
SELECT * FROM invoices
WHERE cost_center IN (
    SELECT cost_center FROM user_access WHERE email = $user_email
)
```

`invoices`'s policy may only reference itself, plus tables explicitly marked referenceable. Mark `user_access` with `policy_mapping=true` first — a policy that joins an unmarked table is rejected at save time, naming the table:

```bash
agnes admin update-table user_access --policy-mapping
```

The same toggle also lives in the web UI: the Access modal on `/admin/tables` has a mapping switch that PUTs the same `policy_mapping` field — equivalent to the CLI flag above, just reachable without leaving the modal. Marking a table this way makes it referenceable from **any** table's policy, not just one specific consumer — and it does **not** itself grant analysts access to `user_access`; that table's own row-level visibility is unaffected.

**The empty-mapping trap.** If `user_access`'s sync fails, or it lands with zero rows, every policy that joins it returns zero rows for everyone — indistinguishable, from an analyst's side, from "you legitimately have no data". Both look like a healthy Agnes with an empty result. Sync the mapping table like any other registered table and watch its sync status; see [v1 limitations](#v1-limitations) below for how (and how not) this surfaces today. A protected table is never its own empty mapping dependency — a table that is both policied and itself marked `policy_mapping=true` reads normally while it holds zero rows, because a policy body's mandatory `FROM <its own table>` is excluded from this check; only a reference to a DIFFERENT empty referenceable table trips it. `GET /api/admin/registry` (and `agnes admin table-policy show <table>`) surfaces every policied row's mapping health up front as `policy_mapping_status` — one `{mapping_table, state, last_sync}` entry per `policy_mapping` dependency the policy joins, `state` being `ok` / `empty` / `never_synced` / `remote_unknown` — so this trap is visible on the registry list itself, not only after a suspiciously-empty live read.

**A mapping table with a policy of its own is joined unfiltered.** The substitution rewrite runs exactly once, non-recursively, over the *caller's* original SQL — a policy body's own `FROM`/joins are spliced in as literal text, never re-parsed and walked — so if `user_access` is itself a policied table, `invoices`'s policy reads it in full, unfiltered by `user_access`'s own policy; this is admin-authored and server-side by design, not a leak.

## The admin bypass

An Admin-group member is unfiltered by every policy **only when their credential's surface is `all`** — the default for a browser session and for `agnes auth token create` (no `--surface` flag). A PAT minted with `--surface stack` is filtered exactly like an ordinary analyst, even though its holder is an Admin — that surface exists specifically to make a script or session behave like an analyst's own view, and policies follow it on purpose. This matters because **`agnes init`'s token exchange mints `surface='stack'` PATs** — so an admin's own `agnes query` from their initialized analyst workspace is filtered by any policy on a table they can otherwise see everything of in the browser. If a query looks unexpectedly filtered, check `agnes auth whoami` / the token's surface before assuming the policy is broken.

## Grant narrowly, or the RBAC layer stays invisible

A policy is silent about *who can reach the table at all* — that's RBAC's job (the grant model in [`RBAC.md`](RBAC.md), referenced above). When you set up a policy — piloting it, demoing it, or just registering the table for the first time — put it in its **own data package granted only to the group(s) the policy branches on**. Do not reuse a broadly-granted package (one already granted to `Everyone`, or to any group outside the policy's `CASE`) just because it's convenient.

Why it matters: with a broad grant, a caller outside every group the policy enumerates still reaches the table — RBAC lets them in, and they land on the policy's own `ELSE FALSE` (or equivalent) branch, seeing an empty slice. That's a 200 with zero rows, not a refusal — indistinguishable, from the caller's side, from "the table happens to be empty." It also means the table-level RBAC gate never actually fires in your test or demo: every caller you tried already had a *table* grant, so only the *row*-level layer was ever exercised. A caller with no grant at all gets a different, earlier outcome — a `403` naming the table as "not in your stack," raised before the policy body ever runs (see [`RBAC.md`](RBAC.md)). Granting the wrapping package only to the policy's own groups is what makes both layers observable:

```bash
agnes admin data-package create --name "Sales, row-scoped" --slug sales-scoped
agnes admin data-package add-table sales-scoped orders
agnes admin grant create sales-cz data_package sales-scoped --requirement required
agnes admin grant create sales-de data_package sales-scoped --requirement required
```

A caller in `sales-cz`/`sales-de` gets their row-level slice; a caller in neither group gets the table-level 403, never the policy's empty slice. `tests/test_rls_pilot_e2e.py::TestTableLevelGateIsSeparateFromRowLevel` pins this end to end.

## Attaching a policy

### Web UI

`/admin/tables` → the **Access** column on each row:

- `—` (muted, "not available — distributed") on a table that is not yet `server_only`/`remote` — still clickable; the modal explains the fix inline.
- `—` (plain) on an eligible table with no policy — click to add one.
- A tinted **Policy** chip, with who/when underneath, once one is attached.

The modal is a plain SQL textarea plus a required note field ("why does this policy exist" — mandatory whenever a non-empty body is saved, so the next admin who finds forty lines of SQL knows whether it's a legal requirement or a hunch), an inline preview runner (persona = one user's email, or an ad-hoc comma-separated group list — see below), and recent edit history, each entry with a restore action and a collapsed line diff against the previous saved version (and, for the newest entry, against the currently stored policy) so an admin can see WHAT changed before deciding to restore it. A revision is recorded for every save that changes the policy body or its note (attach, edit, clear) and for a save that flips the "referenceable from other policies" (`policy_mapping`) switch — a no-op resend of unchanged values records nothing and leaves `access_policy_updated_at` / `access_policy_updated_by` untouched, which matters because the Edit modal round-trips every field on every save — and concurrent writes to one table are serialized server-side — one per-table lock covers a policy save, any other registry edit of that table, its deletion and its re-registration — so the newest revision is always the policy the table actually stores, and two admins editing different fields of the same table no longer overwrite each other's edit. Tables are independent: writes to different tables never wait on each other. A rejected save renders inline rather than as an auto-dismissing toast, on purpose — a security-invariant refusal has to stay legible while you re-read the SQL. Because table ids are derived from names, unregistering a table purges its revision history BEFORE the registry row itself is removed (a purge failure aborts the unregistration rather than orphaning the history), and registering a NEW table also purges any revisions left at its id, so a later table can never inherit — or offer for restore — an earlier table's policy bodies just because it reused the same name.

The editor has two tabs. **Builder** assembles row rules and column masks and compiles them server-side (`POST /api/admin/registry/{id}/policy/compile`) into the SQL that actually gets stored; **Advanced SQL** is that same body, editable by hand. The stored artifact is always SQL — there is no reverse-compiler from SQL back into rules — so when the body in the box is not what the Builder's rules describe (a policy written on the Advanced SQL tab, a restored revision, a hand edit), the Builder says so outright ("this policy was written as SQL and cannot be shown as rules") and points at the Advanced SQL tab, rather than showing its own empty rule set as if the table were unfiltered. Saving from that state stores the SQL unchanged; adding a rule there **replaces** the SQL policy with the rules, and asks for an explicit confirmation first.

In more detail: the Builder's row-rule repeater and per-column mask pickers are fed by `GET /api/admin/registry/{id}/policy/columns` (the real column list plus a PII hint per column) and compiled by `src/access_policy_compile.py`. The compiled output is always an explicit, fixed projection — never `SELECT *` — so a column added upstream after the policy is saved is hidden by omission rather than silently appearing. Row rules combine `in_caller_groups` (row's column is one of the caller's live groups), `eq_caller_email` / `eq_caller_id` (self-owned rows), and `eq` / `in` (literal match) with AND or OR. Column masks are `show`, `hide`, `nullify`, `hash` (an md5 pseudonym, same caveats as [Column masking](#column-masking) above), `pseudonymize_keyed` (the keyed HMAC pseudonym — text-only, and refused on a `remote` table), `last4`, `email_partial`, and `unmask` (visible only to an allowed group or groups, falling back type-aware for everyone else: `'*****'` for text-like columns, `NULL` otherwise). The picker disables `last4`/`email_partial`/`pseudonymize_keyed` on a non-text column and `pseudonymize_keyed` on a `query_mode='remote'` table, each with an inline reason so the admin never has to hit the save-time 422 to learn it; any value-producing mask (`show`/`hide` excepted) can also carry a `groups` allowlist that reveals the column verbatim to those groups and the mask to everyone else, and a `tiered` choice opens an ordered (groups, reveal) chain plus a default, both compiled server-side into the same fixed projection. The Builder's PII flag on a column is a name heuristic (the column name contains a substring like `email`, `phone`, `ssn`, `national_id`, `iban`, and a few others) or the profiler's own "unique" alert on a non-numeric column — a nudge toward masking it, never an authoritative classification (`_policy_builder_looks_like_pii` in `app/api/admin.py`).

### CLI

```bash
# 1. The table must already be undistributed
agnes admin update-table invoices --server-only
#    (or --query-mode remote, if it's already a BigQuery-remote table)

# 2. Attach — multi-line SQL must be a file, never inline
agnes admin update-table invoices \
    --policy @policy.sql \
    --policy-note "Cost-centre scoping per the 2026 finance access review"

# 3. Inspect what is stored
agnes admin table-policy show invoices [--json]

# 4. Clear it (empty --policy value)
agnes admin update-table invoices --policy=
```

### Previewing before you trust it

```bash
agnes admin table-policy preview invoices --as alice@example.com
agnes admin table-policy preview invoices --as-groups finance_eu,finance_admin
# --sql @candidate.sql previews a body BEFORE saving it
```

Prints rows-visible / rows-total, which columns are hidden, and a sample. `0` rows visible prints an explicit note that it may be a legitimate empty slice *or* an empty/stale mapping table (see [Mapping tables](#mapping-tables) above) rather than a bare `0` — an unresolvable persona fails the command outright instead of showing `0`.

This calls the same single-persona primitive the web modal uses (`POST /api/admin/registry/{id}/policy/preview`) — every call is audited, because "who looked at whose data, when" is the first question after an incident. See [v1 limitations](#v1-limitations) for what this preview does *not* yet do.

**The preview surfaces need a full-surface admin credential.** `.../policy/preview`, `.../policy/preview-groups`, `.../policy/preview-matrix`, `.../policy/columns` and `.../policy/revisions` hand back real content — raw unfiltered sample rows, per-group visibility, profiler sample values, historical policy bodies — with no per-table grant check and no policy rewrite behind them, so the same rule as the admin bypass above applies to *who may ask*: a `surface='stack'` PAT (the `agnes init` default) is refused with a `403` naming the fix, and a browser session, a regular PAT or `agnes init --as-admin` is not. `.../policy/compile` persists nothing and returns only generated SQL, so it stays on the plain admin gate.

**A failing preview never quotes the engine.** When the policy body cannot be executed, both previews answer `policy_preview_failed: … (<reason class>)` naming the table, and the per-group sweep puts that same message in the failing group's `error` — a raw DuckDB/BigQuery message can quote literals and column names straight out of the policy body, so it goes to the server log instead (same rule as the live `policy_error`).

**Transpiled form (#1979).** For a `query_mode='remote'` table on BigQuery or Databricks, a live read does not execute the DuckDB text above — it executes that body *transpiled* to the engine's own SQL (the BigQuery jobs-API path, the Databricks Statement Execution API path). The preview response carries a `transpiled: {dialect, relation_sql} | null` field so what you're checking is what would actually run: `null` for a `local`/`materialized` table (the DuckDB text *is* what executes) and, deliberately, also `null` for a `remote` Snowflake table — a registered Snowflake row is a plain DuckDB `VIEW` over the ATTACHed `sf` catalog, so its live reads stay on the ordinary DuckDB arm too, and showing the Snowflake transpile there would preview a body that never runs. `relation_sql` never carries a bound *value* — only the same `$name`/`@name`/`:name` markers the live resolver itself sends, exactly like the row/column preview above never shows a bound value inlined either. The web modal renders this, collapsed, as "Transpiled for `<dialect>`" under the row/column result. A policy body that doesn't transpile to the table's engine surfaces as `policy_preview_transpile_failed` (a stored body attached before the table became `query_mode='remote'` is the realistic case — see `policy_untranspilable` below for the save-time check that catches a *new* remote-table write).

**Matrix preview (§13.1, issue #2147).** A single-persona preview catches only
the extremes — 0 rows and all rows — and cannot see a `CASE`-on-`$user_groups`
with a missing branch. `POST /api/admin/registry/{id}/policy/preview-matrix`
(`agnes admin table-policy preview <id> --matrix`) runs the SAME single-persona
primitive once per PERSONA instead of once for a hand-picked one:

```bash
agnes admin table-policy preview invoices --matrix
agnes admin table-policy preview invoices --matrix --personas policy_groups
agnes admin table-policy preview invoices --matrix --limit 10 --json
```

Personas come from two families (`--personas group_sets | policy_groups |
both`, default `both`): **`group_sets`** — the distinct sets of live group
names held by real users who can actually reach the table (bounded by
group-SETS, not users — capped at 50 distinct sets, `truncated: true`
beyond); **`policy_groups`** — every group literal the policy body itself
compares `$user_groups` against (an `sqlglot` AST walk, never regex), plus
the empty group set. An admin persona never appears — the god-mode bypass
makes previewing "as" one meaningless. Each persona reports
`rows_visible`/`rows_total`/`hidden_columns`/`masked_columns`, plus two
numbers derived from the bounded sample every persona is checked against:
**`union_coverage`** (the fraction of sampled rows visible to at least one
persona — 100% *and* every persona individually at 100% flags `no_op: true`,
the policy does nothing) and **`pairwise_overlap`** (a non-zero overlap
between two personas that should partition the table is the permissive bug,
rendered directly rather than inferred from row counts alone). Row identity
for both is best-effort: no stable row key exists server-side yet, so it
falls back to the tuple of columns that survive from base to policied output
unchanged (never hidden, never masked) — two rows identical across every one
of those columns are indistinguishable to this preview, and a mask that
itself varies by persona can under-count overlap for a row that is genuinely
the same one. Audited (`access_policy.preview_matrix`), same admin gate as
the single-persona preview right above.

**Databricks `remote` tables cannot be previewed unless the Unity Catalog attach is on.** Both previews (single-persona and the all-groups sweep) execute the policy on the server's local analytics view, and a `query_mode='remote'` Databricks row only has one when `data_source.databricks.attach_enabled` (experimental) is enabled — otherwise both refuse with `policy_preview_remote_unsupported` instead of an opaque catalog error; live analyst reads are unaffected (they run natively on the SQL warehouse through the same transpiled policy), and a `materialized` copy of the table previews normally.

## Disclosure: the caller is told they got a slice

Silent row filtering is actively dangerous — an analyst (or an agent, with more confidence) who sums a policied table's own column and reports the total has no way to know it was never the whole table. Every enforcement point surfaces the fact of filtering, not just the filtered data:

- **`row_scope` in the API.** `POST /api/query`, `POST /api/v2/sample` and `POST /api/mcp/query-table/{id}` return `row_scope: {policied_tables: [...], note: "..."}` when the query touched a policied table (`null` otherwise — never an empty-but-present envelope). `POST /api/v2/scan` has no JSON body, so it carries the same payload in an `X-Agnes-Row-Scope` response header instead.
- **`[scope]` on the CLI.** `agnes query` prints `[scope] rows in 'invoices' are filtered by an access policy — this is your slice, not the whole table` to **stderr**, so `--format json` on stdout stays clean for a script or an agent parsing the result.
- **MCP tool docstrings** document the field and instruct the model: if `row_scope` is present, qualify the answer — never present a filtered aggregate as an organization-wide figure.
- **`.claude/rules/access_policies.md`.** `agnes pull` writes this file, naming every policied table currently in the analyst's stack, so an agent carries the caveat in context **before** it writes a query — the one link in the chain that reaches an agent ahead of the fact rather than after a response comes back with a note attached.

### Self-service diagnosis

`GET /api/me/effective-access` (and, for an admin looking at someone else, `GET /api/admin/users/{id}/effective-access`) reports a `policy` block per accessible table:

```json
{"table_id": "invoices",
 "policy": {"applies": true, "rows_visible": 42,
            "reason": "ok"}}
```

`reason` is one of `ok` / `empty_slice` / `mapping_empty` / `policy_error` / `identity_unresolvable`, each carrying a `note` explaining it (the mapping table's name and last-sync time for `mapping_empty`, for instance). This is the fastest way to answer "why does Agnes show me nothing on this table" without an admin hunting through table configuration.

An agent can ask the same question mid-conversation via the `effective_access` MCP tool (issue #2147) — a read-only proxy over `GET /api/me/effective-access` with an optional `table` filter (id or name). Its docstring tells the model to call it before reporting an unexpectedly empty or small result, and how to act on each `reason`. No admin variant is exposed over MCP; auditing someone else's access stays REST-only.
### Shared agents

Which identity `$user_email` / `$user_groups` bind to depends on *how* a caller reaches the agent — `src/access_policy.py::_resolve_identity`:

- **A surface that binds the OWNER.** A Slack channel bound to an agent, and a scheduled run, both run the turn AS THE AGENT'S OWNER end to end — session identity, sandbox workspace, and the row-policy binding all resolve from one identity, regardless of who mentioned the bot or which schedule fired. Everyone who can use the agent through that surface gets the **owner's** row slice.
- **A direct caller binds itself.** Agent-as-API (`POST /api/v1/agents/{slug}/responses`), a chat session run as a shared agent, and a delegated turn (`@delegate`) each carry the actual caller's own identity (`AgentPrincipal.caller_user_id` / `caller_email`), so each is filtered by its OWN slice — the owner's grant only bounds *which tables* the agent reaches, never whose rows come back.

The `/agents` builder page discloses this before it becomes a surprise: when a table reachable through the agent's declared Data & resources (a data package, expanded to its member tables) carries an access policy, the Boundaries panel shows a warning naming the table(s) and the **owner's** `rows_visible` / `reason` for each — the exact slice a Slack channel or scheduled run would return. The same fields ride on `GET /api/v1/agents` / `GET /api/v1/agents/{id}` as `policied_tables_in_scope` (an id list) and `policied_tables` (one `{table_id, name, policy}` entry per policied table, `policy` being the same shape as the effective-access block above), so `agnes agent show` prints the identical warning. This is disclosure only — it changes no enforcement; the owner-binding behaviour above is unchanged and, for a direct caller, was already correct.

### Errors

Every policy-related rejection is a structured `reason`-keyed detail (never a raw engine error — a failing policy's own message could quote literal values out of the policy body):

| `reason` | Where | Meaning |
|---|---|---|
| `policy_name_collision` | 400, query surfaces | your query's own CTE/subquery alias is spelled identically to a policied table — rename it |
| `policy_identity_unresolvable` | 403, query surfaces | no single identity to bind (a co-drive session with several participants) — open the table in a solo session |
| `policy_error` | 500, every read surface (`/api/v2/scan` included) | the policy failed to resolve or execute — never falls back to the unfiltered table, on any surface: a refusal that reached the SQL rewrite used to be swallowed as "not a registered table" and served the raw view with a 200 (#1979), which is why the resolver now signals "unknown table" with its own distinct type |
| `policy_mapping_empty` | 500, every read surface that executes a policied relation (`POST /api/query`, `agnes query --remote --auto-snapshot` / `--from-query` snapshots, `GET /api/v2/sample`, `POST /api/v2/scan`, `POST /api/mcp/query-table/{id}`) | a `policy_mapping` table the policy joins — another one, never the protected table's own mandatory self-reference — has zero (or never-synced) rows — named alongside the policied table (`table`, `mapping_table`, `note`, plus `last_sync`: the mapping table's last successful sync timestamp, `null` if it never synced), same underlying check and wording as `GET /api/me/effective-access`'s `reason: "mapping_empty"` (`src.access_policy.raise_if_policy_mapping_empty`, the one implementation every surface calls, via the shared HTTP shaping in `app.api.access_policy_http.assert_no_empty_policy_mapping`) |
| `access_policies_disabled` | 422, admin write | attaching a policy while `access_policies.enabled` is off |
| `access_policy_requires_undistributed` | 422, admin write | the table is not `remote`/`server_only` |
| `access_policy_physical_source_conflict` | 422, admin write | a second row with **no policy of its own** points at the same physical source as a policied table — any `query_mode`, `server_only` or not (either direction: registering/editing the twin, or attaching the policy). Also fires when *clearing* one policy of a legal policied pair, since that produces the same shape |
| `bq_path_policied` | 403, query surfaces | a direct `bq."dataset"."table"` path (or the full-backtick form) names the physical source of a policied table — query the registered name instead |
| `sf_path_policied` | 403, query surfaces | the same for `sf."SCHEMA"."TABLE"` |
| `dbx_path_policied` | 403, query surfaces | the same for `dbx."<catalog.schema>"."<table>"` and for a bare three-part Databricks path |
| `kbc_path_policied` | 403, query surfaces | the same for `kbc."<bucket>"."<table>"` (#1492; the prefix also gained the registry gate `kbc_path_not_registered` and the grant gate `kbc_path_access_denied` the other three already had) |
| `policy_function_duckdb_only` | 422, admin write / preview | the policy calls `agnes_hmac()` (the `pseudonymize_keyed` mask) on a `query_mode='remote'` table — it runs only on Agnes's own DuckDB connection, so use `md5()` or make the table `server_only` |
| `policy_note_required` | 422, admin write | `access_policy_sql` is set without `access_policy_note` |
| `access_policy_protected_row` | 409, collection file re-ingest / changed-content re-upload | the table derived from this file carries a policy, and a re-ingest would replace the table without it — an admin clears the policy first. The policy is not carried across, because the replacement file need not have the columns it references |
| `policy_var_in_pattern_position` | 422, admin write and preview | an identity variable stands on the *pattern* side of `LIKE` / `ILIKE` / `SIMILAR TO` or a regex function — rejected at save time, and refused again by the resolver (and so by the preview) if a stored body carries the shape anyway |
| `policy_preview_transpile_failed` | 422, preview only | the previewed body (stored or candidate) does not transpile to the table's engine — the realistic case is a body saved before the table became `query_mode='remote'`, so `policy_untranspilable`'s save-time check never ran against it (#1979) |
| `policy_preview_remote_unsupported` | 422, preview only | the table is a `query_mode='remote'` Databricks row and `data_source.databricks.attach_enabled` is off, so there is no local analytics view for the preview to execute the policy against — enable the attach, or preview against a `materialized` copy (#1979) |
| `policy_preview_matrix_limit_out_of_range` | 422, matrix preview only | `--limit`/`limit` on `.../policy/preview-matrix` was outside `1..50` (issue #2147) |

**The case of the table name is not a way out.** DuckDB's catalog is case-insensitive — `FROM ORDERS`, `FROM "Orders"` and `FROM orders` all read the same view — so the registry lookup behind the policy resolver folds case the same way, and every spelling is rewritten identically (#1979). If two registry rows carry names that differ only by case, the read is refused with `policy_error` instead: the catalog can hold only one of those views, so which row's policy governs it is unknowable, and guessing is the one thing a policy must never do.

## Snapshots and staleness

`agnes snapshot create` deliberately materializes a filtered slice of a remote table onto the laptop. `POST /api/v2/scan` stamps an `X-Agnes-Policy-Fingerprint` header (a hash of the policy text plus the caller's email and group set at fetch time) plus an `X-Agnes-Policy-Table-Id` naming the policied table it belongs to; the CLI stores both on the snapshot's metadata, and `agnes pull` re-derives the current fingerprint for that table from the manifest and blocks the view (via the same `snapshot_views_blocked` mechanism used for a de-authorized or newly-`server_only` table) when they no longer match — so a snapshot taken before a policy tightened, before the caller left a group, or before their account was renamed does not keep quietly answering with the old, wider slice.

The table id is what makes this work for `--from-query` snapshots (including every `agnes query --remote --auto-snapshot`), where the snapshot's stored `table_id` is the *name* the analyst chose, not a registry id. A snapshot whose source table the manifest does not describe at all is left resolvable rather than blocked: unknown is not stale. A snapshot created before this was recorded therefore compares against nothing — `agnes snapshot refresh <name>` re-stamps it and restores staleness tracking.

## v1 limitations

Two known gaps, both fail-closed (nothing here degrades to leaking unfiltered data — the failure mode in each case is "answers less than it should," not "answers more than it should"):

1. **`remote` policied tables are refused on the quick-preview surfaces.** `/api/v2/sample` ("Preview data" in the catalog) and `/api/v2/scan` (what `agnes snapshot create` calls) only wired policy enforcement into the AST-rewrite path used by `agnes query` / `POST /api/query`. Neither has a caller-authored statement to substitute a policy into — each *builds* one from `table_id` + `select` + `where` — so for a non-admin they fail closed rather than ever returning the raw table: `500 policy_error` for every `query_mode='remote'` row on `/api/v2/sample` — BigQuery, Snowflake, Keboola and Databricks-with-attach alike, since the sample endpoint gained a live branch for the non-BigQuery ones and applied the same fail-closed ratchet to it — and `400 policy_unsupported_on_scan_engine` for Databricks rows on `/api/v2/scan`, which names the two paths that do work. Note the two quick-preview surfaces have diverged: `/api/v2/sample` now serves the *unpolicied* remote rows live, while `/api/v2/scan` still refuses them outright. This is an **availability gap, not a leak** — the same data is reachable, filtered, via `agnes query --remote` or `POST /api/query`. Wiring these two surfaces properly is a planned follow-up.

(A former limitation 3 — the empty-mapping check being `POST /api/query`-only — is closed as of #2147: `GET /api/v2/sample`, `POST /api/v2/scan`'s local-parquet branch, and `POST /api/mcp/query-table/{id}` all raise the same `policy_mapping_empty` now, and `GET /api/admin/registry` surfaces the check's own read-only diagnosis up front via `policy_mapping_status` — see [Mapping tables](#mapping-tables) above.)
2. **The persona matrix samples, it does not scan.** `POST .../policy/preview-matrix` (see [Matrix preview](#previewing-before-you-trust-it) above) computes `union_coverage`/`pairwise_overlap` over the same bounded sample the before/after preview uses (`_POLICY_PREVIEW_SAMPLE_LIMIT` rows), not the whole table — a bug that only shows up past that window is invisible to it, same class of gap as the row-count-only `preview-groups` sweep it complements. Row identity across personas is also best-effort (documented above) rather than a real primary key.
3. **An empty or stale mapping table still fails closed silently on the sample/scan preview surfaces.** `agnes query` / `POST /api/query` now raise a distinct `500 policy_mapping_empty` (naming the policied table and the empty mapping table) the moment the caller's SQL touches a policy whose `policy_mapping` dependency has zero (or never-synced) rows — the exact same check `GET /api/me/effective-access` uses for its `reason: "mapping_empty"` diagnosis, so the two surfaces never disagree. `/api/v2/sample` and `/api/v2/scan` do not yet carry this check, so a suspiciously-empty result from either of those two is still worth checking against effective-access before treating it as a real answer; wiring them the same way is a planned follow-up (see limitation 1 above, the same two surfaces).

## See also

- [`RBAC.md`](RBAC.md) — the table-grain grant model this layer sits on top of.
- [`admin/query-modes.md`](admin/query-modes.md) — `query_mode` and `server_only`, the two states a policied table must be in.
- [`feature-flags.md`](feature-flags.md) — the `access_policies` flag row and the general feature-flag convention.
- [`superpowers/specs/2026-08-11-table-access-policies-design.md`](superpowers/specs/2026-08-11-table-access-policies-design.md) — full design: the resolver architecture, the BigQuery transpile, the enforcement ratchet, and the decisions behind each rule above.
