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

## Scope: only tables that never leave the server

A policy is only enforceable where Agnes evaluates the read. Once a parquet is on an analyst's laptop, the analyst holds unfiltered bytes with no server in the loop — so a policy may only be attached to a table that is **not distributed**:

```
query_mode = 'remote'   OR   server_only = TRUE
```

Both are equally undistributed; pick by table size and freshness, not by which one supports policies. `server_only` is documented in [`admin/query-modes.md`](admin/query-modes.md) — mark a table `server_only` (keeping `query_mode: local` or `materialized`) to make it queryable but never synced by `agnes pull`.

The server enforces this both ways, not just at attach time:

- **You cannot attach a policy** to a table that is neither `remote` nor `server_only` — the write is rejected with `access_policy_requires_undistributed`, naming the fix (set `server_only=true` first, or switch to `query_mode='remote'`).
- **You cannot make a policied table distributable again** — clearing `server_only`, or moving `query_mode` to `local`, is rejected the same way while a policy is attached. Clear the policy first if you really mean to distribute the table.
- **A second row with no policy of its own cannot point at the same physical source.** Registering (or editing) a row whose `source_query` / `(bucket, source_table)` / BigQuery FQN resolves to a *policied* table's underlying data is rejected (`access_policy_physical_source_conflict`) — otherwise that second registry id hands out exactly the unfiltered rows the policy exists to withhold. **Distributability does not decide whether this fires**, only the wording of the rejection: an earlier version exempted `server_only` / `remote` twins on the reasoning that `agnes pull` is how data escapes, and a live instance disproved it — `/api/query` resolves such a row by its own name server-side and returns the raw rows to anyone granted it, the same disclosure minus the parquet. The check runs in **both** directions: attaching a policy to a table that already has such a twin is refused the same way, naming the offending row, because nothing would ever write that twin again for the register/edit-time check to catch. Keboola bulk auto-discovery classifies such a source as `invalid` (visible in `discover-and-register`'s dry run) rather than registering it. If you genuinely need a second row over one source, give it **its own policy** in the same write — two policied rows over one source are legal, since every read still goes through a policy. To unwind such a pair, repoint one row first (its policy travels with it, so clearing it afterwards succeeds) or unregister one; clearing one policy while the other still covers the source is refused, because that is precisely the unpolicied-twin shape.

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

**A variable may only stand where a value stands.** `FROM $table`, `EXCLUDE ($col)`, or any table/column/alias-name position is rejected at save time — the admin fixes the query's *structure*; the caller only ever supplies *values*. Values bind as real DuckDB (and, on a remote table, BigQuery) named parameters — never string-interpolated — so a group literally named `Robert'); DROP TABLE users;--` is just an inert list element. The one value-position exception: identity variables are also rejected as the *pattern* side of `LIKE` / `ILIKE` / `SIMILAR TO` or a regex function (`owner LIKE $user_email` would let a group or user literally named `%` — no character class validates group names elsewhere in Agnes — silently widen the match to everyone).

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

For **pseudonymization** (keep a stable, joinable value without exposing the original), `md5(col) AS col` is available — this is the only hash function in v1's function allowlist. Be clear-eyed about what it buys you: `md5` is a **pseudonym, not a mask**. An unsalted hash over a low-entropy domain (an email, a short id) is reversible by dictionary in minutes, and the whole point of using it instead of `NULL` is that the hashed value still joins across tables — so treat it as "not shown in plaintext", not as "protected". There is no keyed-hash (HMAC) function in v1's allowlist; for genuine masking, redact with `CASE`/`NULL`/a literal instead of reaching for `md5()`.

The function and construct allowlist is intentionally narrow and closed (logical connectors, `CASE`/`IF`/`COALESCE`/`NULLIF`, `CAST`, `LOWER`, `UPPER`, `TRIM`, `LENGTH`, `CONCAT`, `SUBSTRING`, the regex family for literal patterns, `md5`, and the group-membership functions below) — anything else is rejected at save time, not silently ignored. A policy body is arbitrary SQL that runs on the server's analytics connection on every analyst request; the allowlist is what keeps that a bounded escalation instead of an open one.

**Don't re-derive a column `*` still emits.** `* EXCLUDE (national_id), md5(email) AS email` looks right but leaves `email` out of the `EXCLUDE` list, so the star still emits the original *and* the re-derived expression appends a second column with the same name — DuckDB accepts the duplicate silently, and every serializer either keeps the first (plaintext) occurrence under the plain name or renames the second one, putting the unmasked value exactly where a caller expects the masked one. Always exclude a column before re-deriving it under the same name (`EXCLUDE (national_id, email)`, as above) — Agnes rejects a policy whose output has a duplicate column name at save time (`policy_duplicate_output_column`).

### The group-membership idiom

Use `list_contains($user_groups, col)`, not `col IN (SELECT unnest($user_groups))`. Both execute correctly on DuckDB, but only the first survives the remote transpiles cleanly:

```
list_contains($g, unit)        →  BQ:  EXISTS(SELECT 1 FROM UNNEST(@g) AS _col WHERE _col = unit)
                                  DBX: ARRAY_CONTAINS(:g, unit)
unit IN (SELECT unnest($g))    →  a GENERATE_ARRAY/CROSS JOIN construct, ~10× longer
```

The `unnest` form isn't rejected — the save-time validator logs a server-side warning rather than blocking the save — but there's no reason to reach for it over `list_contains`.

### One authored body, three engines

You write the policy once, in DuckDB SQL. Each engine gets it in its own dialect, and — the part that matters — each engine's own **named-parameter** syntax, so the caller's identity is never text inside the statement:

| engine | marker | how values travel |
|---|---|---|
| DuckDB | `$user_email` | native named parameters |
| BigQuery | `@user_email` | `QueryParameter` on the job config |
| Databricks | `:user_email` | the Statement Execution API's `parameters` field |

Databricks needs one extra step for `$user_groups` alone: its API binds **scalars only**, so the array marker is rewritten into `ARRAY(:p0, :p1, …)` over generated scalar markers. The group names still travel as request fields — only how many of them there are becomes visible in the statement. A caller in no groups binds a typed empty array and matches nothing.

A policy that fails to transpile to *either* remote engine is rejected at save time (`policy_untranspilable`), not at read time — a policy that denies because it cannot be compiled is an outage wearing an access rule's clothes.

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

(There is no web-UI toggle for `policy_mapping` yet — CLI or a direct `PUT /api/admin/registry/{id}` with `{"policy_mapping": true}` is the only way to set it in v1.) Marking a table this way makes it referenceable from **any** table's policy, not just one specific consumer — and it does **not** itself grant analysts access to `user_access`; that table's own row-level visibility is unaffected.

**The empty-mapping trap.** If `user_access`'s sync fails, or it lands with zero rows, every policy that joins it returns zero rows for everyone — indistinguishable, from an analyst's side, from "you legitimately have no data". Both look like a healthy Agnes with an empty result. Sync the mapping table like any other registered table and watch its sync status; see [v1 limitations](#v1-limitations) below for how (and how not) this surfaces today.

## The admin bypass

An Admin-group member is unfiltered by every policy **only when their credential's surface is `all`** — the default for a browser session and for `agnes auth token create` (no `--surface` flag). A PAT minted with `--surface stack` is filtered exactly like an ordinary analyst, even though its holder is an Admin — that surface exists specifically to make a script or session behave like an analyst's own view, and policies follow it on purpose. This matters because **`agnes init`'s token exchange mints `surface='stack'` PATs** — so an admin's own `agnes query` from their initialized analyst workspace is filtered by any policy on a table they can otherwise see everything of in the browser. If a query looks unexpectedly filtered, check `agnes auth whoami` / the token's surface before assuming the policy is broken.

## Attaching a policy

### Web UI

`/admin/tables` → the **Access** column on each row:

- `—` (muted, "not available — distributed") on a table that is not yet `server_only`/`remote` — still clickable; the modal explains the fix inline.
- `—` (plain) on an eligible table with no policy — click to add one.
- A tinted **Policy** chip, with who/when underneath, once one is attached.

The modal is a plain SQL textarea plus a required note field ("why does this policy exist" — mandatory whenever a non-empty body is saved, so the next admin who finds forty lines of SQL knows whether it's a legal requirement or a hunch), an inline preview runner (persona = one user's email, or an ad-hoc comma-separated group list — see below), and recent edit history. A rejected save renders inline rather than as an auto-dismissing toast, on purpose — a security-invariant refusal has to stay legible while you re-read the SQL.

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

## Disclosure: the caller is told they got a slice

Silent row filtering is actively dangerous — an analyst (or an agent, with more confidence) who sums a policied table's own column and reports the total has no way to know it was never the whole table. Every enforcement point surfaces the fact of filtering, not just the filtered data:

- **`row_scope` in the API.** `POST /api/query` and `POST /api/v2/sample` return `row_scope: {policied_tables: [...], note: "..."}` when the query touched a policied table (`null` otherwise — never an empty-but-present envelope). `POST /api/v2/scan` has no JSON body, so it carries the same payload in an `X-Agnes-Row-Scope` response header instead.
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

### Errors

Every policy-related rejection is a structured `reason`-keyed detail (never a raw engine error — a failing policy's own message could quote literal values out of the policy body):

| `reason` | Where | Meaning |
|---|---|---|
| `policy_name_collision` | 400, query surfaces | your query's own CTE/subquery alias is spelled identically to a policied table — rename it |
| `policy_identity_unresolvable` | 403, query surfaces | no single identity to bind (a co-drive session with several participants) — open the table in a solo session |
| `policy_error` | 500, query surfaces | the policy failed to resolve or execute — never falls back to the unfiltered table |
| `access_policies_disabled` | 422, admin write | attaching a policy while `access_policies.enabled` is off |
| `access_policy_requires_undistributed` | 422, admin write | the table is not `remote`/`server_only` |
| `access_policy_physical_source_conflict` | 422, admin write | a second row with **no policy of its own** points at the same physical source as a policied table — any `query_mode`, `server_only` or not (either direction: registering/editing the twin, or attaching the policy). Also fires when *clearing* one policy of a legal policied pair, since that produces the same shape |
| `bq_path_policied` | 403, query surfaces | a direct `bq."dataset"."table"` path (or the full-backtick form) names the physical source of a policied table — query the registered name instead |
| `sf_path_policied` | 403, query surfaces | the same for `sf."SCHEMA"."TABLE"` |
| `dbx_path_policied` | 403, query surfaces | the same for `dbx."<catalog.schema>"."<table>"` and for a bare three-part Databricks path |
| `policy_note_required` | 422, admin write | `access_policy_sql` is set without `access_policy_note` |
| `policy_preview_unsafe_group_name` | 422, admin preview | an `as_groups` name carries a pattern metacharacter (`%`, `_`) |
| `policy_preview_unsafe_live_group_name` | 422, admin preview | the `as_user` persona's own live group name carries one — the resolver would refuse to bind it, so this policy could never be served to that user |

## Snapshots and staleness

`agnes snapshot create` deliberately materializes a filtered slice of a remote table onto the laptop. `POST /api/v2/scan` stamps an `X-Agnes-Policy-Fingerprint` header (a hash of the policy text plus the caller's group set at fetch time) plus an `X-Agnes-Policy-Table-Id` naming the policied table it belongs to; the CLI stores both on the snapshot's metadata, and `agnes pull` re-derives the current fingerprint for that table from the manifest and blocks the view (via the same `snapshot_views_blocked` mechanism used for a de-authorized or newly-`server_only` table) when they no longer match — so a snapshot taken before a policy tightened does not keep quietly answering with the old, wider slice.

The table id is what makes this work for `--from-query` snapshots (including every `agnes query --remote --auto-snapshot`), where the snapshot's stored `table_id` is the *name* the analyst chose, not a registry id. A snapshot whose source table the manifest does not describe at all is left resolvable rather than blocked: unknown is not stale. A snapshot created before this was recorded therefore compares against nothing — `agnes snapshot refresh <name>` re-stamps it and restores staleness tracking.

## v1 limitations

Three known gaps, all fail-closed (nothing here degrades to leaking unfiltered data — the failure mode in each case is "answers less than it should," not "answers more than it should"):

1. **`remote` policied tables are refused on the quick-preview surfaces.** `/api/v2/sample` ("Preview data" in the catalog) and `/api/v2/scan` (what `agnes snapshot create` calls) only wired policy enforcement into the AST-rewrite path used by `agnes query` / `POST /api/query`. Neither has a caller-authored statement to substitute a policy into — each *builds* one from `table_id` + `select` + `where` — so for a non-admin they fail closed rather than ever returning the raw table: `500 policy_error` for every `query_mode='remote'` row on `/api/v2/sample` — BigQuery, Snowflake, Keboola and Databricks-with-attach alike, since the sample endpoint gained a live branch for the non-BigQuery ones and applied the same fail-closed ratchet to it — and `400 policy_unsupported_on_scan_engine` for Databricks rows on `/api/v2/scan`, which names the two paths that do work. Note the two quick-preview surfaces have diverged: `/api/v2/sample` now serves the *unpolicied* remote rows live, while `/api/v2/scan` still refuses them outright. This is an **availability gap, not a leak** — the same data is reachable, filtered, via `agnes query --remote` or `POST /api/query`. Wiring these two surfaces properly is a planned follow-up.
2. **The admin preview is single-persona.** `table-policy preview` / the web modal's preview runner shows one chosen persona (a user, or an ad-hoc group set) at a time — not the full persona matrix (union coverage across every distinct group-set, pairwise overlap) that would catch a `CASE`-with-a-missing-branch bug automatically. Preview as more than one persona by hand before trusting a policy that branches on `$user_groups`; the full matrix view is a planned enhancement.
3. **An empty or stale mapping table fails closed silently on the query path.** A policy that joins a `policy_mapping` table with zero (or never-synced) rows currently returns an ordinary empty result on a live query — indistinguishable from "you legitimately have no data" unless you go check. `GET /api/me/effective-access` **does** distinguish this case explicitly (`reason: "mapping_empty"`, naming the mapping table and its last sync), and `table-policy preview`'s `0`-rows note points at the same possibility — but the live query surfaces themselves (`agnes query`, the sample/scan endpoints) do not yet raise a distinct error for it. An explicit error on the query path itself is a planned enhancement; until then, a suspiciously-empty result from a policied table is worth checking against effective-access before treating it as a real answer.

## See also

- [`RBAC.md`](RBAC.md) — the table-grain grant model this layer sits on top of.
- [`admin/query-modes.md`](admin/query-modes.md) — `query_mode` and `server_only`, the two states a policied table must be in.
- [`feature-flags.md`](feature-flags.md) — the `access_policies` flag row and the general feature-flag convention.
- [`superpowers/specs/2026-08-11-table-access-policies-design.md`](superpowers/specs/2026-08-11-table-access-policies-design.md) — full design: the resolver architecture, the BigQuery transpile, the enforcement ratchet, and the decisions behind each rule above.
