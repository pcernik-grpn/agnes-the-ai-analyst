# API Reference

> Maintained alongside the code — CI checks that every public endpoint is listed here
> (`tests/test_api_docs_coverage.py`). For the always-current interactive reference, see
> [Swagger UI](/docs) and [ReDoc](/redoc) (login required).

> **Three surfaces, one source.** This guide is reachable from
> [`/documentation/api`](/documentation/api) (web), `agnes docs api` (CLI), and the
> `documentation_api` MCP tool (agent / Claude Desktop). All three render the same
> `docs/api-reference.md` so a public endpoint is documented in lockstep across
> the surfaces an analyst or agent might reach for.

---

## Contents

1. Authentication
2. Environments
3. Tables — `/api/admin/registry`
4. Data Packages — `/api/admin/data-packages`
5. Server config — `/api/admin/server-config`
6. Gotchas
7. End-to-end recipes
8. OpenAPI / Swagger
9. Endpoint inventory

---

## 1. Authentication

All admin endpoints require a Personal Access Token (PAT) sent as a Bearer header.
PATs are **per-instance** — a token issued on one deployment returns `HTTP 401 "User not found"`
on any other instance.

```bash
PAT=<your-personal-access-token>
```

Example using curl:

```bash
curl -s -X GET "https://{your-instance}/api/admin/registry" \
  -H "Authorization: Bearer $PAT"
```

---

## 2. Environments

Agnes is typically deployed as two instances: a development instance and a production
instance. Both expose the **same API surface**. Schema migrations may roll to dev first.

| Environment | Base URL | Notes |
|---|---|---|
| Dev | `https://dev.{your-instance}` | Schema migrations land here first |
| Prod | `https://{your-instance}` | Stable; catalog state may be wiped on redeploy (see Gotcha #16) |

Tokens are per-instance and are not interchangeable across dev and prod.

---

## 3. Tables — `/api/admin/registry`

A **table** is a single physical (BigQuery, Keboola, local parquet, etc.) or virtual
asset that the server knows how to query. Tables are the unit of data access; packages
are the unit of curation and user-facing discovery.

### 3.1 Endpoints

| Method | Path | Body | Purpose |
|---|---|---|---|
| `GET` | `/api/admin/registry` | — | List all registered tables (includes extended-doc + column fields) |
| `GET` | `/api/v2/catalog` | — | Public-facing catalog (same data, no admin fields) |
| `POST` | `/api/admin/register-table` | see §3.3 | Register a new table |
| `POST` | `/api/admin/register-table/precheck` | see §3.3 | Validate a registration payload without committing |
| `POST` | `/api/admin/registry/rebuild` | — | Rebuild the extract + master views once (companion to `register-table` `defer_rebuild` for bulk onboarding) |
| `PUT` | `/api/admin/registry/{table_id}` | see §3.2 | Update **operational** fields (idempotent partial) |
| `PATCH` | `/api/admin/registry/{table_id}/docs` | see §3.5 | Update **extended LLM-facing docs** (grain, gotchas, …) |
| `DELETE` | `/api/admin/registry/{table_id}` | — | Unregister |
| `POST` | `/api/admin/registry/{table_id}/policy/preview` | see §3.7 | Preview a stored or candidate access policy as a chosen persona |
| `GET` | `/api/admin/registry/{table_id}/policy/columns` | — | No-SQL policy builder: real column schema + sample values (see §3.8) |
| `POST` | `/api/admin/registry/{table_id}/policy/compile` | see §3.8 | No-SQL policy builder: structured spec → validated SQL (never persisted) |
| `GET` | `/api/admin/metadata/{table_id}` | — | Get per-column metadata (see §3.6) |
| `POST` | `/api/admin/metadata/{table_id}` | see §3.6 | Save per-column metadata |
| `POST` | `/api/admin/metadata/{table_id}/push` | — | Push saved column metadata downstream (no body) |
| `POST` | `/api/admin/run-bq-metadata-refresh` | — | Refresh column metadata from BigQuery (no body) |

### 3.2 Editable fields (PUT)

| Field | Type | Notes |
|---|---|---|
| `name` | string | Display name. **Editable in-place via PUT — does NOT change the registry `id`** (the id is fixed at register-time; see §3.4 and Gotcha #11). Use this to normalize casing or rename the display name without re-registering. |
| `description` | string | Free-form blurb; LLM-facing |
| `bucket` | string | **Display-only** for BigQuery `query_mode=remote` tables. Renaming does NOT affect SQL path resolution. |
| `source_table` | string | **BARE physical table name** (e.g. `orders_daily`) — see the standard below |
| `query_mode` | enum | `remote`, `local`, `materialized` |
| `sync_strategy` | string | For local/materialized tables |
| `primary_key` | string or string[] | Accepts a bare string (coerced to `[string]`) or a list for composite keys |
| `sync_schedule` | string | cron expression |
| `profile_after_sync` | bool | |

> **`source_table` standard: BARE table name, `bucket` = dataset.**
> The server resolves the physical path as `{server-config default project}.{bucket}.{source_table}`,
> so `source_table` carries ONLY the table name (e.g. `orders_daily`) and `bucket` carries
> the dataset (e.g. `analytics`). Do NOT write the full `project.dataset.table` path —
> the full-path form is non-standard and may not resolve correctly on all builds.

> **PUT handles operational fields only.** The extended LLM-facing doc fields
> (`grain`, `things_to_know`, `gotchas`, `pairs_well_with`, `sample_questions`,
> `platforms`, `partition_col`, `history`) are returned by `GET /api/admin/registry`
> but are **not** in the `UpdateTableRequest` schema — `PUT` silently ignores them.
> Write them via `PATCH /api/admin/registry/{table_id}/docs` instead (see §3.5).
> Per-column descriptions are a separate layer (see §3.6).

### 3.3 Example — update description + bucket

```bash
curl -s -X PUT \
  "https://{your-instance}/api/admin/registry/orders_daily" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -d '{
    "description": "One row per order, partitioned by order date.",
    "bucket": "analytics"
  }'
# {"id":"orders_daily","updated":["description","bucket"]}
```

> **Renaming a table's display name in place (no re-register).** A `PUT` carrying just
> `{"name": "…"}` updates the `name` field **without changing the `id`, the docs, or package
> membership** — the id is fixed at register-time and is decoupled from later name edits.
> The id-derivation in Gotcha #11 fires at register (POST) ONLY, not on subsequent PUTs.

### 3.4 Example — register a new BigQuery table

```bash
curl -s -X POST \
  "https://{your-instance}/api/admin/register-table" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "sales_orders",
    "source_type": "bigquery",
    "source_table": "sales_orders",
    "bucket": "analytics",
    "query_mode": "remote",
    "description": "Short, LLM-facing blurb (1–3 sentences)."
  }'
# response: {"id": "sales_orders", ...}
```

**Four registration rules:**

1. **The registry `id` is DERIVED from `name`** (lower-cased). A passed `id` field is **ignored**.
   To get id `sales_orders`, set `name: "sales_orders"`.
2. **`name` must be a DuckDB-safe identifier** — `^[a-zA-Z_][a-zA-Z0-9_]{0,63}$`. **No hyphens
   or special characters** → **HTTP 422** (generic check, fires first). For BigQuery remote tables,
   a space in `name` is not coerced — the BQ raw-name check rejects it with **HTTP 400**.
   Put friendly text in `description`.
3. **BigQuery remote tables require `bucket`** (the BigQuery dataset) — omitting it → `bigquery: 'bucket' is required`.
4. **`source_table` is the BARE table name** (§3.2 standard) — the dataset goes in
   `bucket`, the project comes from server config. Not the full `project.dataset.table` path.

To validate a payload without committing, POST the same body to
`/api/admin/register-table/precheck` first.

### 3.5 Extended table docs — `PATCH /api/admin/registry/{table_id}/docs`

The single `description` field is the short blurb. **Rich, LLM-facing table
documentation** lives behind a dedicated `PATCH` endpoint with the
`TableDocsRequest` schema. These are the fields returned by `GET /api/admin/registry`
that are not writable via `PUT` (see the note under §3.2).

| Field | Type | Notes |
|---|---|---|
| `grain` | string | One-line grain statement, e.g. `"1 row per order"` |
| `things_to_know` | string | Extended free-text writeup — quality filters, conventions, caveats |
| `gotchas` | object[] | Array of `{"body": "...", "key": false}` — `body` (string) required, `key` (bool) optional. **Plain strings are rejected** with `model_attributes_type`. Max 8 entries. |
| `pairs_well_with` | string[] | Related table **IDs** for cross-table analysis hints |
| `sample_questions` | string[] | Prompt seeds (table-level equivalent of a package's `example_questions`) |
| `platforms` | string[] | Applicable platforms, e.g. `["web","app"]`. Max 8 entries. |
| `partition_col` | string | Partition column name, e.g. `"event_date"` |
| `history` | string | Retention / history note |

```bash
curl -s -X PATCH \
  "https://{your-instance}/api/admin/registry/sales_orders/docs" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -d '{
    "grain": "1 row per order",
    "things_to_know": "Standard quality filters apply. Join on order_id across all order-grain tables.",
    "gotchas": [
      {"body": "Use DATE(order_created_at) for revenue-period filters, not event_date"},
      {"body": "Refund rows share the same order_id — deduplicate on event_type when counting orders"}
    ],
    "pairs_well_with": ["order_items", "customer_segments"],
    "sample_questions": ["How many orders were placed last week?"],
    "platforms": ["web", "app"],
    "partition_col": "event_date"
  }'
```

Notes:

- Verb is **`PATCH`** (not `PUT`) and the path carries a `/docs` suffix.
- Partial update — only the keys you send are changed; omit a field to leave it untouched.
- **`gotchas` items are objects, not strings** — `{"body": "...", "key": false}`. Sending plain
  strings returns HTTP 422 `model_attributes_type`. `pairs_well_with`, `sample_questions`,
  and `platforms` ARE plain string arrays.

### 3.6 Per-column metadata — `/api/admin/metadata/{table_id}`

A separate layer holds **per-column descriptions** (the `ColumnMetadataSave` /
`ColumnMetadataItem` schema). Distinct from the table-level docs in §3.5.

| Endpoint | Purpose |
|---|---|
| `GET /api/admin/metadata/{table_id}` | Returns `{"table_id": "...", "columns": [...]}` |
| `POST /api/admin/metadata/{table_id}` | Save the `columns` array (replaces existing) |
| `POST /api/admin/metadata/{table_id}/push` | Publish saved metadata downstream (no body) |
| `POST /api/admin/run-bq-metadata-refresh` | Re-pull column metadata from BigQuery (no body) |

Each column item:

| Field | Type | Notes |
|---|---|---|
| `column_name` | string | required |
| `basetype` | string | data type (nullable) |
| `description` | string | LLM-facing column description (nullable) |
| `confidence` | string | provenance/quality marker for the description |

```bash
curl -s -X POST \
  "https://{your-instance}/api/admin/metadata/sales_orders" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -d '{
    "columns": [
      {"column_name": "order_id",     "basetype": "STRING", "description": "Unique order identifier; primary join key.", "confidence": "high"},
      {"column_name": "event_date",   "basetype": "DATE",   "description": "Partition column — always filter on this.", "confidence": "high"},
      {"column_name": "order_status", "basetype": "STRING", "description": "Current status of the order lifecycle.", "confidence": "high"}
    ]
  }'
```

### 3.7 Access-policy preview — `POST /api/admin/registry/{table_id}/policy/preview`

Runs a table's stored access policy — or a **candidate** policy, checked before it is
ever saved — as a chosen persona, and reports what that persona would see. A policy is
attached/replaced/cleared via `PUT /api/admin/registry/{table_id}` (`access_policy_sql`
+ mandatory `access_policy_note`, `policy_mapping`); this endpoint never writes anything.
Every call is recorded to the audit log (`access_policy.preview`) — it shows one admin
another person's data slice.

| Field | Type | Notes |
|---|---|---|
| `sql` | string, optional | A candidate policy body to preview before saving. Omit to preview the table's **currently stored** `access_policy_sql`. Validated the same way a `PUT` would validate it. |
| `as_user` | string, optional | Preview as an existing user's real identity (id or email) — binds their **live** group membership. |
| `as_groups` | string[], optional | Preview as an ad-hoc, hypothetical group set with no real user behind it. |

Exactly one of `as_user` / `as_groups` is required — a request naming both, or neither, returns `422`.

```bash
curl -s -X POST \
  "https://{your-instance}/api/admin/registry/orders_daily/policy/preview" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -d '{"as_groups": ["Finance"]}'
# {"columns": [{"name": "id", "hidden": false}, {"name": "secret", "hidden": true}, ...],
#  "sample_rows": [...], "rows_visible": 42, "rows_total": 4200}
```

`columns` marks every base column `hidden: true` if the policy's `EXCLUDE`/rewrite drops
it for that persona; `rows_visible` is the count through the policy, `rows_total` the
unfiltered count (admin bypass).

`base_sample_rows` is the same bounded window of RAW rows, for a before/after view, and
`base_sample_comparable` says whether the two samples are guaranteed to cover the same
source rows. It is `false` when the policy's reads of its own table could not be bounded
to the raw sample's window (e.g. it names the table with a schema qualifier) and the raw
sample is not provably the whole table — in that case the two lists must **not** be
diffed row-by-row, only read on their own.

### 3.8 No-SQL policy builder — `GET .../policy/columns`, `POST .../policy/compile`

Lets an admin author a policy by picking columns and masks instead of writing SQL by
hand. `GET /api/admin/registry/{table_id}/policy/columns` returns the table's real
schema plus sample values (from the stored profile, if one exists) so the builder UI
never has to know the table's structure up front:

```bash
curl -s "https://{your-instance}/api/admin/registry/orders_daily/policy/columns" \
  -H "Authorization: Bearer $PAT"
# {"columns": [{"name": "email", "type": "VARCHAR", "samples": ["a@x.com"], "distinct": 42, "pii": true}, ...],
#  "mapping_tables": ["cost_centers"], "eligible": true}
```

`eligible` mirrors the distribution interlock (§3.7's PUT gate): a policy can only be
attached to a `query_mode='remote'` or `server_only=true` table — the builder shows
this so the UI can nudge toward `server_only` first rather than fail silently later.

`POST /api/admin/registry/{table_id}/policy/compile` turns a structured spec into the
same canonical SQL the resolver runs — the anti-leak invariant (every output column is
named explicitly; masked columns are emitted once and new source columns added after the
policy is saved are omitted) is enforced in `src/access_policy_compile.py`, not
duplicated here:

| Field | Type | Notes |
|---|---|---|
| `row_rules` | array, optional | `[{"column", "op", "value"}]` — `op` is one of `in_caller_groups`, `eq_caller_email`, `eq_caller_id`, `eq`, `in` |
| `row_combine` | string, optional | `"and"` (default) or `"or"` |
| `column_masks` | object, optional | `{column: "show"\|"hide"\|"nullify"\|"hash"\|"unmask"}` — `"unmask"` takes `{"choice": "unmask", "groups": ["..."]}` (single-group `"group"` is still accepted) |

```bash
curl -s -X POST \
  "https://{your-instance}/api/admin/registry/orders_daily/policy/compile" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -d '{
    "row_rules": [{"column": "cost_center", "op": "in_caller_groups"}],
    "column_masks": {"email": "hash", "national_id": "hide"}
  }'
# {"sql": "SELECT md5(\"email\") AS \"email\", \"cost_center\" FROM \"orders_daily\" WHERE list_contains($user_groups, \"cost_center\")",
#  "warnings": []}
```

`warnings` carries what the compiler had to say about the spec — a column it did not
recognize and dropped, or a spec that filters and masks nothing at all. A spec it cannot
understand (an unknown `op` or mask) returns `422 policy_compile_invalid_spec`.

This endpoint never persists anything — it only returns SQL text. Save it the same way
as any hand-written policy: `PUT /api/admin/registry/{table_id}` with the returned `sql`
as `access_policy_sql` (plus the mandatory `access_policy_note`).

Both builder routes are admin-only and, like `.../policy/preview`, available regardless
of `access_policies.enabled`: they neither read nor write a stored policy. That flag
gates attaching one (`PUT` with a non-null `access_policy_sql`) and applying one on a
read.

---

## 4. Data Packages — `/api/admin/data-packages`

A **data package** is a thematic bundle of tables exposed to end users and LLMs as
a single browsable entity, with rich metadata — short description, long description,
guardrail bullets, example questions, icon, color, and a cover image.

### 4.1 Endpoints

| Method | Path | Body | Purpose |
|---|---|---|---|
| `GET` | `/api/admin/data-packages` | — | List all packages (flat array). Accepts `?include_table_ids=true` to embed table id arrays. |
| `POST` | `/api/admin/data-packages` | see §4.3 | Create — `name` + `slug` required |
| `GET` | `/api/admin/data-packages/{pkg_id}` | — | Get one — includes `tables` array and `related_tools` |
| `PUT` | `/api/admin/data-packages/{pkg_id}` | see §4.3 | Update (idempotent partial) |
| `DELETE` | `/api/admin/data-packages/{pkg_id}` | — | Soft-delete (reversible via /restore) |
| `POST` | `/api/admin/data-packages/{pkg_id}/restore` | — | Undo a soft-delete |
| `POST` | `/api/admin/data-packages/{pkg_id}/tables` | `{"table_id": "..."}` | Attach table to package |
| `DELETE` | `/api/admin/data-packages/{pkg_id}/tables/{table_id}` | — | Detach table |
| `POST` | `/api/admin/data-packages/{pkg_id}/tools` | `{"tool_id": "..."}` | Attach MCP tool to package |
| `DELETE` | `/api/admin/data-packages/{pkg_id}/tools/{tool_id}` | — | Detach MCP tool |
| `GET` | `/api/data-packages/{slug}` | — | Public-facing view (no admin) |
| `POST` | `/api/admin/uploads/cover-image` | multipart `file` | Upload a cover image → `{"url": "/uploads/covers/<sha256>.<ext>", "content_type": "...", "size": <bytes>}`. Extension mirrors the uploaded file type (not always `.png`). Storage is content-addressed — identical bytes always produce the same path. Set the returned `url` on a package's `cover_image_url`. |

### 4.2 Editable fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes (on create) | Human-readable name |
| `slug` | string | yes (on create) | URL-safe slug; immutable after create (see Gotcha #9) |
| `description` | string | — | Short blurb; ~210 chars / two sentences works well |
| `long_description` | string | — | Extended writeup; max 4000 chars |
| `icon` | string | — | Single emoji glyph (e.g. `💰`, `🔍`) |
| `color` | string | — | 6-digit hex value (e.g. `#10b981`) — other formats return 422 |
| `cover_image_url` | string | — | URL or **data URI**. Send `""` (empty string) to clear the cover image. |
| `status` | string | — | One of `prod`, `poc`, `coming-soon`, `draft`. `coming-soon` hides the package from non-admin users. |
| `category` | string | — | Free-text category label. Send `""` to clear. |
| `owner_name` | string | — | |
| `owner_team` | string | — | |
| `tags` | string[] | — | Max 8 entries, 30 chars each |
| `when_to_use` | string[] | — | Guardrail bullets shown to LLM users; max 8, 200 chars each |
| `when_not_to_use` | string[] | — | Guardrail bullets; max 8, 200 chars each |
| `example_questions` | string[] | — | Rendered in the UI as example questions; max 12, 200 chars each |

### 4.3 Example — create a package

```bash
curl -s -X POST \
  "https://{your-instance}/api/admin/data-packages" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Core Analytics",
    "slug": "core-analytics",
    "description": "Order and revenue data for the entire platform.",
    "icon": "💰",
    "color": "#10b981"
  }'
```

### 4.4 Example — update a package

```bash
curl -s -X PUT \
  "https://{your-instance}/api/admin/data-packages/pkg_xxxxxxxxxxxxxxxx" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -d '{
    "description": "Every order the platform has ever processed.",
    "icon": "💰",
    "color": "#0284c7",
    "when_to_use": [
      "Revenue, margin, refund, or order-volume questions",
      "Anything that requires audit-grade per-order numbers"
    ],
    "when_not_to_use": [
      "Session-level traffic analysis — use the Traffic package instead"
    ],
    "example_questions": [
      "What was total revenue last month?",
      "How many orders were placed in Q1?"
    ]
  }'
```

The full updated package object is returned.

### 4.5 Example — attach a table to a package

```bash
curl -s -X POST \
  "https://{your-instance}/api/admin/data-packages/pkg_xxxxxxxxxxxxxxxx/tables" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -d '{"table_id": "sales_orders"}'
```

The table must already be registered via `/api/admin/registry`.
Response: `{"added": true}` (idempotent — `{"added": false}` if already attached).

### 4.6 Generating an SVG cover image (data URI)

The `cover_image_url` field accepts data URIs, allowing self-contained inline covers
with no external hosting requirement.

```python
import urllib.parse

def build_cover(name: str, color_dark: str, color_light: str) -> str:
    # IMPORTANT: XML-escape `&`, `<`, `>` in the visible name (see Gotcha #1)
    safe = (name.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="274" height="120" viewBox="0 0 274 120">'
        '<defs>'
        f'<linearGradient id="g" x1="0" y1="0" x2="274" y2="120" gradientUnits="userSpaceOnUse">'
        f'<stop offset="0" stop-color="{color_dark}"/>'
        f'<stop offset="1" stop-color="{color_light}"/>'
        '</linearGradient>'
        '</defs>'
        '<rect width="274" height="120" fill="url(#g)"/>'
        f'<text x="14" y="70" font-family="Inter, sans-serif" font-size="24" '
        f'font-weight="700" fill="#ffffff">{safe}</text>'
        '</svg>'
    )
    return "data:image/svg+xml;utf8," + urllib.parse.quote(svg, safe="")
```

Then PUT it as a string field:

```python
import json, subprocess
cover = build_cover("Core Analytics", "#064e3b", "#10b981")
subprocess.run([
    "curl", "-s", "-X", "PUT",
    f"https://{{your-instance}}/api/admin/data-packages/{{pkg_id}}",
    "-H", f"Authorization: Bearer {PAT}",
    "-H", "Content-Type: application/json",
    "-d", json.dumps({"cover_image_url": cover}),
])
```

---

## 5. Server config — `/api/admin/server-config`

Platform-wide settings live here, including the data source connection configuration.

### 5.1 Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/admin/server-config` | Return current config + `known_fields` self-documentation |
| `POST` | `/api/admin/server-config` | **Partial-patch** (preferred) — only the sections you send are changed |
| `GET` | `/api/admin/server-config/overlay` | Raw, editable-section-only instance.yaml overlay (unresolved, secrets stripped) — the `agnes admin config export`/`apply` round-trip projection |
| `POST` | `/api/admin/configure` | Full wizard-style setup; missing fields get nulled. Prefer the partial-patch above. |

`POST /api/admin/server-config` accepts a `sections` object keyed by section name
(`instance`, `data_source`, `email`, `telegram`, `jira`, `theme`, `server`, `auth`,
`ai`, `desktop`, `corporate_memory`, `materialize`, `guardrails`,
`marketplace`). Sections outside this allowlist are rejected with 400.

Sections `auth` and `server` are "danger zones" — mutating them requires sending
`confirm_danger: true` in the request body, since incorrect values can lock
administrators out of the instance.

### 5.2 Export/apply the overlay as reviewable YAML

`GET /api/admin/server-config/overlay` returns the raw on-disk overlay
(`${STATE_DIR}/instance.yaml`) filtered to the editable sections — not the
merged, env-resolved config `GET /api/admin/server-config` serves. An
unresolved `${VAR}` reference or an env-var NAME field (`token_env`) passes
through unchanged; a literal never leaves the server in two cases: (a) the
`connectors` section is free-form and admin-typed (per-connector keys with
no static schema, e.g. a Slack webhook URL) so every literal there is
omitted regardless of key name, and (b) a value that is unambiguously
credential-shaped (a JWT, a PEM block, a URL carrying userinfo or a long
opaque token segment) is omitted everywhere else too. The response's
`omitted_keys` lists every dropped path — nothing is silently discarded.
`agnes admin config export`/`apply` wrap this endpoint and the partial-patch
POST above into a round-trip: export the overlay to a file (the CLI prints
a comment header + stderr note listing anything `omitted_keys` reported),
review/edit it, commit it, and `apply` it to a new or existing instance
through the exact same validated path an admin's form save would use. This
is the "onboard a new client via a reviewed PR" building block — REST+CLI
only, never MCP-exposed (same reasoning as the sections above: it is a
one-call dump of the instance's editable config surface).

### 5.3 BigQuery config shape

```json
{
  "sections": {
    "data_source": {
      "type": "bigquery",
      "bigquery": {
        "project":                   "your-gcp-data-project",
        "billing_project":           "your-gcp-billing-project",
        "location":                  "us-central1",
        "bq_max_scan_bytes":         5368709120,
        "max_bytes_per_materialize": 10737418240,
        "query_timeout_ms":          600000
      }
    }
  }
}
```

`billing_project` is a separate explicit field. When the service account can read from
the data project but must bill against a different project, set both. Mismatched
project/billing pair → `USER_PROJECT_DENIED` on every BigQuery call.

---

## 6. Gotchas

| # | Gotcha | Fix |
|---|---|---|
| 1 | `&`, `<`, `>` in SVG cover names break the XML parser — text truncates silently | XML-escape before URL-encoding: `&` → `&amp;`, `<` → `&lt;`, `>` → `&gt;` |
| 2 | `PUT` with `"cover_image_url": null` does NOT clear the field | Treated as no-change. Send `""` (empty string) to clear. |
| 3 | PATs are per-instance | Using a token from one instance against another → `HTTP 401 "User not found"` |
| 4 | `bucket` on a BigQuery `remote` table is display-only | Renaming `bucket` does not affect SQL path resolution; safe to rebrand freely |
| 5 | `restart_required` in the server-config response is **per-save, not constant** — it used to be a hardcoded `true` | The same response carries `sections_effect`, a `{section: "live" / "restart" / "deploy"}` map: `restart_required` is `true` iff some patched section is not `live`. Read `sections_effect` to see which one forced it. Sections resolved per request (`instance`, `theme`, `ai`, `mcp`, …) report `live` and need no bounce; sections built once at boot (`auth`, `chat`, `server`, `email`) or read by a separate process (`telegram`, `data_source` — the scheduler and workers keep the pre-save coordinates) report `restart`. Registry PUTs (description/bucket) are a different endpoint and always take effect immediately. |
| 6 | OpenAPI spec lives at `/openapi.json`, NOT `/api/openapi.json` | The latter returns 404 |
| 7 | **Package IDs are per-instance** (server-generated `pkg_*`). The same slug may have different IDs on dev vs prod. | Always look up the destination package by **slug**, never reuse a source-instance ID. Table IDs ARE stable across instances. |
| 8 | `POST /api/admin/data-packages` create response may omit fields that were persisted (`icon`, `color`, `cover_image_url` returned as `null` even though saved). | Don't trust the POST echo — `GET /api/admin/data-packages/{pkg_id}` to verify. |
| 9 | `slug` is immutable after create — sending it on PUT is at best a no-op, at worst rejected. | Drop `slug` from PUT payloads. Only include it on POST create. |
| 10 | **Registry GET exposes more fields than PUT accepts.** `grain`, `things_to_know`, `gotchas`, `pairs_well_with`, `sample_questions`, `platforms`, `partition_col`, `history` come back in `GET /api/admin/registry` but are NOT in `UpdateTableRequest` — a `PUT` carrying them silently drops them. | Write extended docs via `PATCH /api/admin/registry/{id}/docs` (§3.5); write per-column docs via `POST /api/admin/metadata/{id}` (§3.6). |
| 11 | **`register-table` derives the registry `id` from `name`** (lower-cased) — a passed `id` is ignored. This derivation fires at register (POST) ONLY. A later `PUT {"name":…}` renames the display name **in place without re-keying the id**. | Set `name` to the identifier you want as the id (e.g. `name: "Sales_Orders"` → id `sales_orders`). To fix casing afterward, `PUT` a lowercase `name` — id stays put. |
| 12 | **`name` must be a DuckDB-safe identifier** `^[a-zA-Z_][a-zA-Z0-9_]{0,63}$`. Hyphens or special characters → **HTTP 422** (generic check, fires first). For BigQuery remote tables, a space in `name` is not coerced — the BQ raw-name check rejects it with **HTTP 400**. BigQuery remote tables also require `bucket` (the dataset) — omitting it → HTTP 422. | Use an underscore identifier for `name`; pass `bucket` = BQ dataset on register. |
| 13 | **`DELETE /api/admin/registry/{id}` can return HTTP 500** for a table whose package was deleted out from under it (dangling membership). | Detach from packages first, or remove via the admin UI. |
| 14 | **Some builds echo list-type doc fields as JSON-encoded strings.** After a `PATCH .../docs`, `GET` may return `platforms` as `'["web"]'` (a string) instead of `["web"]` (a list). | Parse the field with `json.loads` when it comes back as a string before comparing. |
| 15 | **Transient 5xx (502/503/504) are routine**, especially under concurrent publishes. A single failed call is NOT a signal that the content is wrong. | Retry with exponential backoff (e.g. 4 attempts at 1/2/4s). |
| 16 | **Prod redeploys may wipe all admin catalog state** — packages, extended docs, covers, memberships are re-seeded from the bundled default. Dev deploys typically persist state; prod deploys on some configurations don't. | All content should live in a version-controlled presentation layer and be re-applied via a publish pipeline after each deploy. Anything published only by hand is lost on the next prod redeploy. |
| 17 | **`status` has four allowed values**, not two. | `status` accepts four values: `prod`, `poc`, `coming-soon`, `draft`. |

---

## 7. End-to-end recipes

### 7.1 Onboard a new BigQuery table into an existing package

```bash
PAT=<your-personal-access-token>
BASE="https://{your-instance}"

# 1. Register the physical table
curl -s -X POST "$BASE/api/admin/register-table" \
  -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
  -d '{
    "name": "new_table",
    "source_type": "bigquery",
    "source_table": "new_table",
    "bucket": "analytics",
    "query_mode": "remote",
    "description": "What it is + when to use it."
  }'
# id derives from `name`; source_table is BARE + bucket=dataset (§3.2/§3.4 rules)

# 2. Attach it to a package (look up pkg_id by slug first — see Gotcha #7)
curl -s -X POST "$BASE/api/admin/data-packages/pkg_xxxxxxxxxxxxxxxx/tables" \
  -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
  -d '{"table_id": "new_table"}'
```

### 7.2 Mirror packages between instances (slug-keyed, idempotent upsert)

Because package IDs are per-instance (see Gotcha #7), you cannot copy
`pkg_*` IDs across environments. The reliable pattern is:

1. Read the source list and the destination list.
2. Index the destination by `slug`.
3. For each source package: if its slug exists on the destination → `PUT` (update),
   otherwise → `POST` (create).
4. Mirror table memberships separately by calling
   `POST /api/admin/data-packages/{dest_pkg_id}/tables` with the same `table_id`s
   (table IDs ARE stable across instances).

Direction-agnostic recipe:

```python
import json, subprocess

PAT_SRC  = "<source-instance-token>"
PAT_DST  = "<destination-instance-token>"
SRC_BASE = "https://dev.{your-instance}"
DST_BASE = "https://{your-instance}"

COPY_FIELDS = [
    "name", "description", "long_description", "icon", "color",
    "cover_image_url", "status", "category", "owner_name", "owner_team",
    "tags", "when_to_use", "when_not_to_use", "example_questions",
]   # NOTE: `slug` deliberately excluded — it's set on create only (Gotcha #9)

def call(method, url, pat, body=None):
    cmd = ["curl", "-s", "-X", method, url,
           "-H", f"Authorization: Bearer {pat}"]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    return json.loads(subprocess.check_output(cmd, text=True))

src_pkgs = call("GET", f"{SRC_BASE}/api/admin/data-packages", PAT_SRC)
dst_pkgs = call("GET", f"{DST_BASE}/api/admin/data-packages", PAT_DST)
dst_by_slug = {p["slug"]: p["id"] for p in dst_pkgs}

for src_pkg in src_pkgs:
    slug = src_pkg["slug"]
    # Always GET the full source object — list endpoint may omit some fields
    full = call("GET", f"{SRC_BASE}/api/admin/data-packages/{src_pkg['id']}", PAT_SRC)

    if slug in dst_by_slug:
        dst_id = dst_by_slug[slug]
        payload = {f: full.get(f) for f in COPY_FIELDS}
        call("PUT", f"{DST_BASE}/api/admin/data-packages/{dst_id}", PAT_DST, payload)
    else:
        payload = {f: full.get(f) for f in COPY_FIELDS}
        payload["slug"] = slug
        resp = call("POST", f"{DST_BASE}/api/admin/data-packages", PAT_DST, payload)
        dst_id = resp["id"]
        # Gotcha #8: POST echo may be incomplete — GET to verify if needed.

    # Mirror table membership (table IDs are stable cross-instance)
    src_tables = [t["id"] for t in full.get("tables", [])]
    dst_after  = call("GET", f"{DST_BASE}/api/admin/data-packages/{dst_id}", PAT_DST)
    already    = {t["id"] for t in dst_after.get("tables", [])}
    for tid in src_tables:
        if tid in already:
            continue
        call("POST", f"{DST_BASE}/api/admin/data-packages/{dst_id}/tables",
             PAT_DST, {"table_id": tid})
```

### 7.2.1 Mirror table registry (descriptions + buckets)

Table IDs are the same across instances, so this is simpler — no slug indirection:

```python
for t in call("GET", f"{SRC_BASE}/api/admin/registry", PAT_SRC)["tables"]:
    if t.get("source_type") != "bigquery":
        continue
    call("PUT", f"{DST_BASE}/api/admin/registry/{t['id']}", PAT_DST, {
        "description": t.get("description") or "",
        "bucket":      t.get("bucket"),
    })
```

---

## 8. OpenAPI / Swagger

| Path | Returns |
|---|---|
| `/openapi.json` | Full OpenAPI 3 spec |
| `/docs` | Swagger UI (HTML) |
| `/redoc` | ReDoc (HTML) |
| `/api/openapi.json` | **404 — common mistake** |

Grep the OpenAPI spec for new endpoints:

```bash
curl -s "https://{your-instance}/openapi.json" \
  -H "Authorization: Bearer $PAT" | \
  python3 -c "
import json, sys
spec = json.load(sys.stdin)
for path, methods in spec['paths'].items():
    for m in methods:
        if m in ('get','post','put','delete','patch'):
            print(f'{m.upper():6s} {path}')"
```

---

## 9. Endpoint inventory

Generated from `app.openapi()['paths']` at build time. Every `/api/*` path in the
running application appears here exactly once. This is the list `tests/test_api_docs_coverage.py`
checks against.

### `/api/admin/registry` — Table registry

- /api/admin/registry
- /api/admin/registry/rebuild
- /api/admin/registry/{table_id}
- /api/admin/registry/{table_id}/docs
- /api/admin/registry/{table_id}/policy/preview
- /api/admin/registry/{table_id}/policy/columns
- /api/admin/registry/{table_id}/policy/compile

### `/api/admin/register-table` — Table registration

- /api/admin/register-table
- /api/admin/register-table/precheck

### `/api/admin/metadata` — Per-column metadata

- /api/admin/metadata/{table_id}
- /api/admin/metadata/{table_id}/push

### `/api/admin/data-packages` — Data packages

`POST /api/admin/data-packages/builder/turn` (admin) runs one turn of the
package drawer's conversation and returns `{reply, patch, suggestions}`. It
**writes nothing, and has no `apply` flag at all** — creating a package writes
grants, so a turn only ever proposes into the open drawer and the admin
presses Create having seen the access matrix they are about to write. Unlike
the two builder-turn endpoints under `/api/store` and `/api/agents`, the
candidate lists are fetched server-side rather than accepted from the caller:
the worst case here is a group, so the set of grantable groups is the
server's answer. Proposed table and group ids are validated against it, and a
fabricated one is dropped rather than corrected.

- /api/admin/data-packages
- /api/admin/data-packages/builder/turn
- /api/admin/data-packages/{pkg_id}
- /api/admin/data-packages/{pkg_id}/restore
- /api/admin/data-packages/{pkg_id}/tables
- /api/admin/data-packages/{pkg_id}/tables/{table_id}
- /api/admin/data-packages/{pkg_id}/tools
- /api/admin/data-packages/{pkg_id}/tools/{tool_id}

### `/api/admin/server-config` and `/api/admin/configure` — Instance configuration

- /api/admin/server-config
- /api/admin/server-config/overlay
- /api/admin/configure

### `/api/admin/uploads` — File uploads

- /api/admin/uploads/cover-image

### `/api/admin/discover-tables` and `/api/admin/discover-and-register` — Table discovery

- /api/admin/discover-tables
- /api/admin/discover-and-register

### `/api/admin/users` — User management

- /api/admin/users/{user_id}/activity
- /api/admin/users/{user_id}/effective-access
- /api/admin/users/{user_id}/library-preview
- /api/admin/users/{user_id}/memberships
- /api/admin/users/{user_id}/memberships/{group_id}
- /api/admin/users/{user_id}/sessions
- /api/admin/users/{user_id}/sessions/download-all
- /api/admin/users/{user_id}/sessions/{session_file}/download

### `/api/admin/groups` — User groups

- /api/admin/groups
- /api/admin/groups/{group_id}
- /api/admin/groups/{group_id}/members
- /api/admin/groups/{group_id}/members/{user_id}

### `/api/admin/grants` — Resource grants

- /api/admin/grants
- /api/admin/grants/{grant_id}

### `/api/admin/access-overview` — Access overview

- /api/admin/access-overview

### `/api/admin/resource-types` — Resource type registry

- /api/admin/resource-types

### `/api/admin/knowledge-digests` — Maintained digests CRUD (admin, K4)

- /api/admin/knowledge-digests
- /api/admin/knowledge-digests/{digest_id}

### `/api/jobs` — Job queue (wave-2B worker runtime)

- /api/jobs
- /api/jobs/{job_id}

Enqueue (`POST /api/jobs`), fetch (`GET /api/jobs/{job_id}`), and list
(`GET /api/jobs?status=&kind=&limit=`) jobs on the durable job queue
(`src/repositories/jobs.py`). `kind` must be registered in the server's
`JOB_KINDS` registry (`app/worker/registry.py`, populated by
`register_all_kinds()` at startup) — an unrecognized kind 400s with the
list of currently-registered kinds. Gated by `require_admin`, which also
accepts the scheduler's shared-secret bearer token
(`app/auth/scheduler_token.py`) since that token resolves to a synthetic
user in the `Admin` group. CLI: `agnes admin jobs enqueue|show|list`. MCP:
`admin_job_enqueue`, `admin_job_get`, `admin_jobs_list`. One kind,
`agent_response`, is registered CONDITIONALLY (only on a process with a
live chat manager) — background agent runs (`POST
/api/v1/agents/{slug}/responses`) therefore execute on gateway-colocated
workers, not on a worker-only replica in a role-split deployment.

### `/api/admin/analytics/migrate` — DuckLake analytics-backend migration (wave-2G)

- /api/admin/analytics/migrate

`POST` with `{"to": "ducklake"}` or `{"to": "legacy"}`. Validates
prerequisites (`to="ducklake"` only — the DuckLake extension is loadable
and the catalog is reachable, auto-repairing a missing catalog database
on an existing Postgres volume) and enqueues an `analytics-migrate` job
(`app/worker/kinds.py`, HEAVY lane) that rebuilds the named target
backend from the on-disk extracts tree, via
`SyncOrchestrator.migrate_to_backend`. Returns 202 with a `job_id` to
poll via `GET /api/jobs/{job_id}`; 400 with the full list of unmet
prerequisites; 409 if a migration is already in flight. Never flips
`analytics.backend` in config — see `docs/DEPLOYMENT.md`'s DuckLake
section for the full operator flow. CLI: `agnes admin analytics migrate
--to <target>`. MCP: `admin_analytics_migrate`.

### `/api/admin/mcp-sources` — MCP source management

- /api/admin/mcp-sources
- /api/admin/mcp-sources/builder/turn
- /api/admin/mcp-sources/preview-introspect
- /api/admin/mcp-sources/{source_id}
- /api/admin/mcp-sources/{source_id}/classify
- /api/admin/mcp-sources/{source_id}/introspect
- /api/admin/mcp-sources/{source_id}/materialize
- /api/admin/mcp-sources/{source_id}/oauth/client
- /api/admin/mcp-sources/{source_id}/oauth/register
- /api/admin/mcp-sources/{source_id}/secret
- /api/admin/mcp-sources/{source_id}/test

### `/api/admin/mcp-tools` — MCP tool management

- /api/admin/mcp-tools
- /api/admin/mcp-tools/{tool_id}
- /api/admin/mcp-tools/{tool_id}/grants
- /api/admin/mcp-tools/{tool_id}/grants/{group_id}
- /api/admin/mcp-tools/{tool_id}/projection-map
- /api/admin/mcp-sources/{source_id}/grants
- /api/admin/mcp-sources/{source_id}/grants/{group_id}

`PUT …/mcp-tools/{tool_id}/projection-map` records which of a lister tool's
columns carry an app's id, URL and name; an empty body clears it and restores
the built-in guesses. It is separate from `PUT …/mcp-tools/{tool_id}` because
the choice is only makeable *after* a fetch has shown what the tool emits, and
because clearing has to be expressible. A named column is authoritative even
when its value is empty — falling back to a guess would make the mapping look
applied while a different column supplied the value.

`POST …/mcp-sources/{source_id}/grants` grants a group **every** tool registered
under one source, and `DELETE …/grants/{group_id}` revokes the set. Per-tool
grants suit an upstream curated a few tools at a time; a connected Keboola
project registers around forty at once, and granting those one page at a time is
the friction the chat-tools switch exists to remove. Idempotent per tool, refuses
(409 `no_tools_registered`) rather than reporting success over a source with no
tools, and returns `granted` / `already_granted` / `total` separately — "granted
0 of 37" and "granted 37 of 37" are different news. CLI: `agnes admin mcp source
grant <src> --group <id> [--revoke]`. Not MCP-exposed: a tool an agent can call
that widens which tools a group may call is a privilege-escalation seam.

### `/api/admin/memory-domains` — Knowledge domain management (admin)

- /api/admin/memory-domains
- /api/admin/memory-domains/{domain_id}
- /api/admin/memory-domains/{domain_id}/items
- /api/admin/memory-domains/{domain_id}/items/{item_id}
- /api/admin/memory-domains/{domain_id}/restore

### `/api/admin/memory-domain-suggestions` — Domain suggestion review (admin)

- /api/admin/memory-domain-suggestions
- /api/admin/memory-domain-suggestions/count-pending
- /api/admin/memory-domain-suggestions/{sid}/approve
- /api/admin/memory-domain-suggestions/{sid}/reject

### `/api/admin/authoring-suggestions` — Authoring studio suggestion review (admin)

Generic non-admin suggestion queue for the authoring studio (data-package / mcp /
marketplace / corporate-memory). Non-admins submit a proposed create payload from
the `/admin/studio/{domain}` builder; admins approve/reject (guarded state
transitions — turning an approved suggestion into the real resource is a deferred
follow-up that must re-validate through the domain endpoint, never replay).

The Studio is **hidden by default** since the admin cleanup (`studio.enabled` /
`AGNES_STUDIO_ENABLED`), and these endpoints answer `403` while it is off. See
[feature-flags.md](feature-flags.md).

- /api/studio/suggestions
- /api/studio/suggestions/mine
- /api/admin/authoring-suggestions
- /api/admin/authoring-suggestions/{sid}/approve
- /api/admin/authoring-suggestions/{sid}/reject

### `/api/studio/memory-mining` — Corporate-memory mining (privacy-gated)

Opt-in (per design spec §4.4): a user consents to having their session
transcripts mined into shared corporate memory; an admin triggers a run that
PII-scans candidates, tags provenance, and routes them through the
authoring-suggestions queue (never an admin-direct write).

- /api/studio/memory-mining/consent
- /api/admin/memory-mining/run

### `/api/admin/metrics` — Metric definitions (admin)

- /api/admin/metrics
- /api/admin/metrics/import
- /api/admin/metrics/{metric_id}

### `/api/admin/recipes` — Recipe management (admin)

- /api/admin/recipes
- /api/admin/recipes/{recipe_id}
- /api/admin/recipes/{recipe_id}/restore

### `/api/admin/observability` — Observability views

- /api/admin/observability/facets
- /api/admin/observability/kpis
- /api/admin/observability/views
- /api/admin/observability/views/{view_id}

### `/api/admin/adoption` — Adoption dashboard (admin)

- /api/admin/adoption/kpis
- /api/admin/adoption/series
- /api/admin/adoption/top-skills
- /api/admin/adoption/top-users
- /api/admin/adoption/users/{user_id}/kpis
- /api/admin/adoption/users/{user_id}/series
- /api/admin/adoption/users/{user_id}/top-skills
- /api/admin/adoption/users/{user_id}/top-tools

### `/api/admin/dashboard` — Admin dashboard signals (admin)

- /api/admin/dashboard/signals

  The "Needs fixing" zone of the `/admin` dashboard — failed syncs, broken
  marketplace syncs, and tools erroring above threshold. Fetched by the page
  after first paint rather than rendered inline, because these read the
  unbounded `sync_history` / `usage_events` tables;
  memoised behind a short process-local TTL. Clear signals are OMITTED rather
  than returned at `count: 0`, so an empty `signals` array is the healthy
  state. A signal whose resolver raised comes back with `failed: true` so a
  broken check never reads as all-clear. Admin-only.

### `/api/admin/reports` — Marketplace usage digest (admin)

- /api/admin/reports/marketplace-digest

  One consolidated, report-shaped JSON payload for an external rendering
  pipeline (e.g. an n8n workflow). `?period=daily|weekly[&date=YYYY-MM-DD]`.
  Returns headline KPIs (with prior-period deltas), a per-day trend series,
  usage by source, top items, rising/falling movers, failures,
  installs/adoption, zero-usage curated plugins, and per-marketplace sync
  health. Admin-only; PAT-gated for headless callers.

### `/api/admin/telemetry` — Query telemetry

- /api/admin/telemetry/ask
- /api/admin/telemetry/export
- /api/admin/telemetry/facets
- /api/admin/telemetry/kpis
- /api/admin/telemetry/prune
- /api/admin/telemetry/query
- /api/admin/telemetry/reprocess
- /api/admin/telemetry/summary

### `/api/admin/sessions` — Session management (admin)

- /api/admin/sessions/facets
- /api/admin/sessions/kpis
- /api/admin/sessions/list
- /api/admin/sessions/{username}/{session_file}/download
- /api/admin/sessions/{username}/{session_file}/transcript

### `/api/admin/activity` — Activity feed

- /api/admin/activity
- /api/admin/activity/health
- /api/admin/activity/sync

### `/api/admin/news` — News / announcements

- /api/admin/news/current
- /api/admin/news/draft
- /api/admin/news/preview
- /api/admin/news/publish
- /api/admin/news/unpublish/{version}
- /api/admin/news/versions
- /api/admin/news/versions/{version}

### `/api/admin/initial-workspace` — Initial workspace template

Admin-only (web UI at `/admin/initial-workspace`; no analyst CLI/MCP analogue).
`/sync` is the manual "Sync now" action (errors loudly when no repo is
registered). `/sync-if-configured` is the nightly-scheduler wrapper: it always
returns 200, short-circuiting to `{"skipped": true, "reason": "not_configured"}`
when no IWT repo is registered, so the nightly job is a no-op on instances
without one. Cadence is configurable via `SCHEDULER_INITIAL_WORKSPACE_SCHEDULE`
or `instance.yaml` `initial_workspace.sync_schedule` (default `daily 03:30`).

- /api/admin/initial-workspace
- /api/admin/initial-workspace/sync
- /api/admin/initial-workspace/sync-if-configured

### `/api/admin/welcome-template` — Welcome message template

- /api/admin/welcome-template
- /api/admin/welcome-template/preview

### `/api/admin/workspace-prompt-template` — Workspace prompt template

- /api/admin/workspace-prompt-template
- /api/admin/workspace-prompt-template/preview

### `/api/admin/prompts` — Managed prompts (admin, #622)

Unified admin surface for the install + workspace prompts (`kind ∈
install|workspace`), each with an explicit Git ⇄ Editor `source_mode` toggle.
Editor mode keeps the DB override editable; Git mode binds the prompt to a file
in the Initial Workspace Template clone. Backs the `/admin/prompts` page.
`iwt-files` (read-only) lists the repo-root-relative bindable files in the
synced IWT clone for the bind-git file picker.

- /api/admin/prompts/iwt-files
- /api/admin/prompts/{kind}
- /api/admin/prompts/{kind}/source
- /api/admin/prompts/{kind}/bind-git
- /api/admin/prompts/{kind}/preview

### `/api/admin/bigquery` — BigQuery diagnostics

- /api/admin/bigquery/test-connection

### `/api/admin/doctor` — deployment-gate & support diagnostics

`POST /api/admin/doctor/new-instance` (admin-only) runs the new-instance
deployment checks — `login-door`, `email-delivery`, `chat-grant`,
`agent-scope`, `branding` — and returns
`{status, checks: [{name, status, audience, detail}]}` using the
`agnes diagnose` status vocabulary (`ok`/`warning`/`error`/`info`).
Optional body `{"email_to": "..."}` makes the email-delivery check send a
real test message through the same send path the login flows use. CLI:
`agnes admin doctor --new-instance`; the host-side siblings live in
`scripts/ops/post-deploy-smoke-test.sh`.

`GET /api/admin/doctor/support` (admin-only) collects the redacted
support-bundle snapshot that feeds the server section of `agnes doctor`:
`build` (version/channel/image tag/commit), `schema` (backend + migration
verdict), `retrieval` (`hybrid`/`lexical_only`, the latter a loud
`warning`), `sync` (per-source rollup with the most recent failures),
`disk`, `process`, and `secrets` (env-var **presence booleans only —
never values**). Each section is collected in isolation, so a crashing
collector reports itself instead of failing the request.

- /api/admin/doctor/new-instance
- /api/admin/doctor/support

### `/api/admin/keboola` — Keboola diagnostics

- /api/admin/keboola/test-connection

### `/api/admin/data-sources` — Source catalog discovery

Admin-only, read-only browse of a configured source's catalog, for the
"Add data source" wizard's table picker. Keyed on `source_type` rather than a
connection id, because the sources that need it have no connection record — their
coordinates live in `data_source.<name>` (instance.yaml / `/admin/server-config`).
Snowflake today; Keboola keeps its per-connection listing below.

- /api/admin/data-sources/{source_type}/tables

`GET …/{source_type}/tables` attaches, reads `information_schema.tables` and
detaches — no extract is written and no registry row touched (registration stays
`POST /api/admin/register-table`). Optional `?schema=` narrows to one schema.
Returns `{source_type, database, schemas: [{name, tables: [{name, table_type}]}]}`.
400 when the source type is not browsable, when the source is not configured, or
when the resolved host is outside `AGNES_REMOTE_ATTACH_HOST_ALLOWLIST`; 502 when
the driver or catalog query fails — never an empty listing, which would read as
"the account has no tables".

### `/api/admin/source-connections` — Named source connections (multi-project Keboola, #731)

Admin-only CRUD for named data-source connections. Enables multiple Keboola projects
per Agnes instance. Each connection stores a `stack_url` and a vault-backed token.
Tables in `table_registry` can be pinned to a specific connection via `connection_id`.
`GET …/{connection_id}/tables` lists the project's buckets with nested tables (admin-UI
discovery helper for the /admin/data-sources add-project wizard, #755).

- /api/admin/source-connections
- /api/admin/source-connections/{connection_id}
- /api/admin/source-connections/{connection_id}/secret
- /api/admin/source-connections/{connection_id}/test
- /api/admin/source-connections/{connection_id}/tables
- /api/admin/source-connections/{connection_id}/chat-tools

`POST …/{connection_id}/chat-tools` lends the chat agent the connected project's
own upstream MCP tools (SQL, buckets/tables, search, semantic context): it derives
an `mcp_sources` stdio row from the connection, copies the connection's storage
token into the MCP vault, then **introspects the upstream and registers its tools**
as `tool_registry` passthrough rows — the agent's passthrough surface is built from
that table, so a source alone would give it nothing to call. `mutating` comes from
each tool's `readOnlyHint`; a tool the upstream does not annotate is recorded as
mutating rather than assumed safe. Exposed names are prefixed per connection, so
two projects' identically-named tools stay apart, and capped at 64 characters —
the tool-name limit model APIs enforce. Returns `tools_registered` plus
`tools_admin_only`, the number of registered tools recorded as mutating: the
passthrough policy gate refuses those unless the caller is an admin or one of
the caller's groups holds a grant with `allow_mutating=true` on that specific
tool (`POST /api/admin/mcp-tools/{tool_id}/grants` — schema v121; agent
profiles ride their owner's groups, still narrowed by connection scope), and
on an upstream that annotates nothing that is all of them — the caller needs
to see that before promising analysts anything. A registration failure returns 502
and rolls back the previous chat-tools state; a failed local config write
propagates instead of being dressed up as an upstream problem.
A connection carrying `config.workspace_schema` passes it through as
`KBC_WORKSPACE_SCHEMA`, which is what makes a non-master (read-only) token able
to run `query_data` — with a master token Keboola creates the workspace itself,
so it stays absent.
`DELETE` removes both (idempotent), and deleting the connection itself does the
same. Keboola-only; 400 without a resolvable token — a source that connected
anonymously would fail every call at the far end instead. Enabling is idempotent
and re-running is how a rotated token reaches the derived source. The derived
source lands with **no** `tool_grants`, so nothing is exposed until an admin
grants the tools to a group. CLI: `agnes admin connection chat-tools [--disable]`.
Deliberately not MCP-exposed (credential-provisioning exemption, `CONTRIBUTING.md`).

### `/api/admin/sharepoint/connections/{connection_id}` — SharePoint connect wizard (spec 2026-08-27 §13.2)

Admin-only surface behind the "connect → scope → share" file-source wizard on
`/admin/data-sources`. The SharePoint connection itself is an ordinary
`source_type=sharepoint` row through `/api/admin/source-connections` (tenant/
client id, certificate via vault secret or `config.cert_private_key_env`);
these three routes are the wizard's own steps 2/3.

- /api/admin/sharepoint/connections/{connection_id}/tree
- /api/admin/sharepoint/connections/{connection_id}/tree/search
- /api/admin/sharepoint/connections/{connection_id}/scopes
- /api/admin/sharepoint/connections/{connection_id}/corpus-map
- /api/admin/sharepoint/connections/{connection_id}/certificate

`GET …/tree` browses the live Microsoft Graph folder tree one level per call
(no `site_id`/`drive_id` → sites; `site_id` alone → that site's document
libraries; `drive_id` → the drive's root children; `drive_id` + `item_id` →
that folder's own children, at any depth — TCRD-240) using the connection's
resolved certificate. `item_id` is structurally validated before it ever
reaches a Graph URL path segment, and is rejected (`422
item_id_requires_drive_id`) without a `drive_id`. A missing/unresolvable
certificate is a typed `409 sharepoint_cert_unresolved` (surface absence
rather than fail the crawl); a rejected/failed Graph call is a typed `502
sharepoint_graph_error`.

`GET …/tree/search` (TCRD-240) is a bounded breadth-first folder search over
the same live tree — Graph's own `/search` is known to silently under-return
under app-only auth, so this module never calls it. Params: `q` (required,
`min_length=2`), `mode` (`prefix` default, `contains`, or `glob` —
`fnmatch` syntax, case/composition-insensitive; a malformed glob, defined as
unbalanced `[`/`]`, is a typed `422 invalid_search_pattern`), an optional
`drive_id` + `item_id` subtree root (neither given searches every drive of
every reachable site; `item_id` without `drive_id` is `422`), and
`max_depth`/`max_visited` (defaults `5`/`500`, CLAMPED to caps `10`/`2000`
rather than rejected). Response: `{matches: [{item_id, drive_id,
display_path}], visited, truncated}` — `truncated` is `true` whenever a cap
is what stopped the walk, never a silently partial result.

`GET/POST/DELETE …/scopes` manage the wizard's scope rows — each a selected
site/library/folder, stored as `{source_scope_id, display_path, anonymize,
collection_id}` inside the connection's own `config.scopes` (no new table).
`POST` confirms a scope: creates its collection on first confirmation and
reuses the same collection on every re-confirmation of the same
`source_scope_id` (idempotent — a rename/move in the source updates
`display_path` in place rather than forking a second collection), and
optionally applies group grants (ordinary `resource_grants` rows on the
collection — never duplicated onto the scope row itself). The response's
`no_group_warning` flags a collection with no granted group ("indexed but
invisible"). `DELETE` (`?source_scope_id=`) unselects a scope — an explicit
exclusion — without touching its already-created collection.

`GET …/corpus-map` is the producer handoff: the flat `{source_scope_id:
collection_id}` mapping `ship_to_agnes.py --corpus-map` consumes until
crawling moves inside Agnes.

`GET …/certificate` returns read-only certificate metadata — the thumbprint
the client actually presents (`thumbprint_x5t`, the JWT assertion's `x5t`
header value) plus the conventional uppercase-hex SHA-1 fingerprint
(`thumbprint_sha1_hex`), `subject`/`issuer`, `not_before`/`not_after`, and a
derived `expires_in_days` (may be negative) / `status`
(`ok`/`expiring_soon` at ≤30 days/`expired`) — derived at request time from
the connection's already-stored PEM, no new schema. Catches two real
failure modes: a registered certificate that doesn't match what the
connection presents (opaque provider auth error), and a certificate
expiring silently. Never returns the private key. No certificate configured
or an unparseable one is a typed absence — `{"certificate": null, "reason":
"..."}` — not an error status.

Admin-only wizard bookkeeping with no analyst CLI/MCP analogue; the eventual
document surface is `agnes facts …`.

### `/api/admin/ontology` — Ontology builder (spec 2026-08-27 §13.2)

Admin-only, behind the `facts` feature flag. The builder shell on
`/admin/ontology` authors the entity/relationship types the fact graph
extracts against. Everything fills an **unsaved draft**; only `save`
materializes it — conversation and import never apply on their own.

- /api/admin/ontology/drafts
- /api/admin/ontology/drafts/{draft_id}
- /api/admin/ontology/drafts/{draft_id}/import
- /api/admin/ontology/drafts/{draft_id}/save
- /api/admin/ontology/dry-run

`POST/GET /drafts` create and list drafts; `GET/PUT/DELETE /drafts/{id}` read,
edit and discard one. `POST …/import` translates a pasted or uploaded
ontology (the producer's YAML) into the draft, reporting the leftovers the
translator could not place structurally (mirrors the allowlisted
`/api/admin/metrics/import`). `POST …/save` validates the frozen draft against
the vendored Ossie schema and materializes it into a semantic model through
the same path `agnes admin semantic-model import` uses. `POST /dry-run` runs
the draft's current types over one selected document through the server-side
LLM and returns proposed facts/edges plus a **not-captured** block; it is a
typed `501` when no LLM provider is configured. Admin-only authoring with no
analyst CLI/MCP analogue (the ontology is consumed as a semantic model, which
has its own surface).

### `/api/admin/contributed-skills` — Contributed skill management

Admin-only CRUD for the Agnes Contributed marketplace. `POST` wraps a pasted `SKILL.md` in a one-skill plugin and publishes it; `GET` lists contributed plugins with their granted group; `DELETE` removes a plugin and clears its grants. Mirrors the `/admin/contribute-skill` web form, `agnes admin skill list/contribute/delete` CLI, and `list_contributed_skills`/`contribute_skill`/`delete_contributed_skill` MCP tools.

These endpoints are NOT gated by `features.contribute_skill_enabled` — that flag hides the `/admin/contribute-skill` WEB PAGE only (off by default since the admin cleanup; the Library's skill builder is the supported path). The API, CLI and MCP surfaces keep working, so automation that publishes contributed skills is unaffected.

- /api/admin/contributed-skills
- /api/admin/contributed-skills/{name}

### `/api/admin/datasource-secrets` — Datasource credential management

Admin-only, write-only vault for datasource secrets (`KEBOOLA_STORAGE_TOKEN`, `BIGQUERY_SERVICE_ACCOUNT_JSON`). Values are encrypted via `AGNES_VAULT_KEY`; the GET endpoint returns presence/source status only, never the value.

- /api/admin/datasource-secrets
- /api/admin/datasource-secrets/{name}

`POST /api/admin/validate-gws-credentials` format-checks a Google Workspace OAuth `client_id` (no network call, no persistence) for the UI "Test" button; returns `{"valid": bool}`.

- /api/admin/validate-gws-credentials

### `/api/admin/slack-secrets` — Slack secret management

- /api/admin/slack-secrets
- /api/admin/slack-secrets/{name}

### `/api/admin/sso` — External SSO login (runtime-configured Entra ID OIDC)

Singleton runtime config for the optional external-identity login (`sso`
provider slot): tenant/client IDs, a write-only Fernet-encrypted client
secret, the mandatory email-domain allowlist, button label and enable flag,
plus the captured external-identity bindings. `PUT`/`DELETE` on the config
and secret are guarded by the last-login-door rule (422 `last_login_door`
when the operation would leave no usable sign-in method). Postgres app-state
backend required (typed 501 on DuckDB). See `docs/auth-sso-entra.md`.

- /api/admin/sso/config
- /api/admin/sso/client-secret
- /api/admin/sso/test-config
- /api/admin/sso/identities
- /api/admin/sso/identities/{user_id}

### `/api/admin/db` — Database state and migration

- /api/admin/db/cancel/{job_id}
- /api/admin/db/job/{job_id}
- /api/admin/db/migrate
- /api/admin/db/state

### `/api/admin/cache-warmup` — Cache warmup

- /api/admin/cache-warmup/run
- /api/admin/cache-warmup/status
- /api/admin/cache-warmup/stream

### `/api/admin/store` — Marketplace store submissions (admin)

- /api/admin/store/lint-audit
- /api/admin/store/lint-dismiss
- /api/admin/store/lint-findings
- /api/admin/store/submissions
- /api/admin/store/submissions/{submission_id}
- /api/admin/store/submissions/{submission_id}/bundle.zip
- /api/admin/store/submissions/{submission_id}/override
- /api/admin/store/submissions/{submission_id}/rescan
- /api/admin/store/submissions/{submission_id}/retry

### `/api/admin/semantic-layer` — Keboola semantic-layer import status

- /api/admin/semantic-layer/coverage

`GET /api/admin/semantic-layer/coverage` (admin) reports, per Keboola
connection holding a master token, how much of that project's semantic layer
actually reaches `metric_definitions`: `metrics.upstream` vs
`metrics.importable`, the glossary count, metrics `blocked` by their own
definition (with the skip reason), and the datasets that have no registered
table. Computed live against the Metastore and `table_registry` — it reads no
stored sync counters, so it is accurate immediately after a restart.

`warnings[]` carries the two conditions worth acting on:
`token_project_mismatch` (the connection's storage and master tokens resolve to
different projects, so tables sync from one project while the semantic layer is
read from another — no metric can bind) and `no_metrics_bound` (the project
publishes metrics but none bind to a registered table). Datasets with no
registered table are reported as a plain count, never as pending work — a
semantic layer routinely describes more of a project than an instance registers.

CLI: `agnes admin semantic-layer coverage [--json]`. MCP:
`admin_semantic_layer_coverage`.

### `/api/admin/semantic-models` and `/api/semantic-models` — Open semantic-layer contract

Admin CRUD over canonical Apache Ossie semantic-model documents, plus a
public, resource-gated export and search surface. The stored `document` is
the source of truth — export returns it byte-for-byte, never re-serialized,
so comments and key order survive.

- /api/admin/semantic-models
- /api/admin/semantic-models/{model_id}
- /api/admin/semantic-sources
- /api/admin/semantic-sources/{source_id}
- /api/admin/semantic-sources/{source_id}/sync
- /api/semantic-models/search
- /api/semantic-models/{slug}.yaml
- /api/semantic-models/validate-query
- /api/semantic-models/context
- /api/semantic-models/schema
- /api/semantic-models/apply

`POST /api/admin/semantic-models` validates the pasted document against the
vendored Ossie schema (422 with the schema errors on failure) and stores it
as a hand-authored (`source='manual'`) model, keyed by the document's own
model name. A model whose `source` is anything else (imported by a
registered `semantic-source`) refuses edits through `PUT` with 409
`source_owned`, naming the source to edit it at instead — the next sync
would otherwise silently revert the change. `POST
.../semantic-sources/{id}/sync` fetches and imports one source now; a
failed fetch imports nothing and is recorded on the source, never mistaken
for "upstream went empty".

`GET /api/semantic-models/{slug}.yaml` (export) and `GET
/api/semantic-models/search` are any-authenticated-user, gated instead on
the linked Data Package's grant (`data_package_semantic_models`) — a model
rides the same visibility as the package(s) it belongs to; admins always
see everything. A model with no linked package is admin-only until an
admin links it.

CLI: `agnes admin semantic-model list/show/import/export/validate` (the
last runs entirely offline — no server, no token) and `agnes admin
semantic-source add/list/sync`. MCP: `semantic_model_search`,
`semantic_model_get`.

`POST /api/semantic-models/apply` is the one non-admin-reachable write
surface (chat-first authoring): any authenticated caller submits an Ossie
document, and the outcome branches on authority — an admin's document is
validated, stored as `source='manual'`, and projected (`outcome: applied`);
anyone else's is queued as an `authoring_suggestions` row (domain
`semantic-layer`) for admin moderation (`outcome: submitted_for_review`) and
never touches `semantic_models` before approval. Shared guards for both
roles: schema-invalid 422; a slug owned by an imported source 409
`source_owned` (stronger than the raw admin POST — apply refuses to shadow
an imported model even for admins); a stale `expected_content_hash` 409
`stale_document` (the optimistic lock for read → modify → re-apply). The
non-admin branch also 409s `duplicate_pending` while an earlier proposal for
the same slug awaits review, and 403s `studio_disabled` when the Studio
toggle is off. CLI: `agnes semantic-model apply`. MCP:
`apply_semantic_model`.

`POST /api/semantic-models/validate-query` validates a SQL statement against
the caller's accessible `status='valid'` models (same RBAC tier as
search/export) via the pure `src.semantic_validation.validate_query` engine:
an `error`-severity constraint violation sets `valid: false`; a rule that
cannot be checked statically degrades to `post_execution_checks`, never a
guessed violation; a used metric whose only expressions target another
engine sets `locally_executable: false`. With zero accessible valid models
the response is `{"available": false, "error": "no_semantic_model", ...}`
rather than a misleading all-clear. CLI: `agnes semantic-model
validate-query "<SQL>" [--expect JSON] [--target-engine duckdb] [--json]`
(distinct from `agnes admin semantic-model validate`, which schema-checks a
document, not a query). MCP: `validate_semantic_query`.

`GET /api/semantic-models/context` and `GET /api/semantic-models/schema` are
the agent read-parity tools. `context` uses the same RBAC tier as
search/export/validate-query (a Data Package or direct model grant, not
admin-only); `schema` is authentication-only — it reflects no model-specific
data, so any authenticated user may read it.
`context` takes a JSON-encoded `selections` query param — a list of
`{"semantic_type": "dataset"|"metric"|"relationship", "ids": [...]?}` objects
— plus an optional repeatable `model_ids` to restrict which accessible
models are searched; absent/empty `ids` returns every object of that type
COMPACTLY (name + a short summary), explicit `ids` return the FULL object.
`schema` takes a repeatable `semantic_types` query param and returns the
matching slice of the vendored Apache Ossie JSON Schema (`$defs` + a
`$ref`-keyed `types` map) — never a hand-written copy, and not gated on any
model existing (it reflects the schema every model is validated against).
CLI: `agnes semantic-model context <type> [--id ...] [--model ...] [--json]`
and `agnes semantic-model schema <type> [<type> ...] [--json]`. MCP:
`get_semantic_context`, `get_semantic_schema`.

### `/api/admin/run-*` — Background job triggers

- /api/admin/run-audit-prune
- /api/admin/run-blocked-purge
- /api/admin/run-bq-metadata-refresh
- /api/admin/run-corporate-memory
- /api/admin/run-databricks-semantic-layer-refresh
- /api/admin/run-jira-consistency-check
- /api/admin/run-jira-sla-poll
- /api/admin/run-keboola-semantic-layer-refresh
- /api/admin/run-knowledge-digests
- /api/admin/run-knowledge-migration
- /api/admin/run-knowledge-packaging
- /api/admin/run-reap-stuck-reviews
- /api/admin/run-retention-prune
- /api/admin/upgrade-freeze — per-instance auto-upgrade freeze (GET status, POST set for 1–72 h, DELETE lift); writes the state-disk marker the VM's upgrade tick honors
- /api/admin/run-session-collector
- /api/admin/run-session-processor

### `/api/auth` — Authentication

- /api/auth/exchange-setup-token

### `/api/auth/keboola` — Keboola multi-project login (select mode)

A `multi_project_mode: select` Keboola sign-in stashes the discovered
projects (vault-encrypted, 15-minute TTL) for a user-driven import; these
endpoints serve and act on the caller's OWN stash (session/JWT auth).
`GET /projects` lists the discovery with an `imported` flag per project;
`POST /projects` (POST-to-collection — connect these discovered projects)
provisions the selected ids through the same core
the `auto` mode runs at login (PAT mint + vault, connection, chat tools,
`kbc-<project>-<role>` membership). REST-only by design — see the standing
credential-provisioning exemption in CONTRIBUTING.md.

- /api/auth/keboola/projects

### `/api/catalog` — Public catalog

- /api/catalog/metrics/{metric_path}
- /api/catalog/profile/{table_name}
- /api/catalog/profile/{table_name}/refresh
- /api/catalog/tables

### `/api/chat` — Chat sessions

- /api/chat/journey
- /api/chat/sessions
- /api/chat/sessions/{chat_id}
- /api/chat/sessions/{chat_id}/archived
- /api/chat/sessions/{chat_id}/files
- /api/chat/sessions/{chat_id}/files/download
- /api/chat/sessions/{chat_id}/files/save-artefact
- /api/chat/sessions/{chat_id}/messages
- /api/chat/sessions/{chat_id}/permanent
- /api/chat/sessions/{chat_id}/pin
- /api/chat/sessions/{chat_id}/ticket
- /api/chat/sessions/{chat_id}/title
- /api/chat/skills
- /api/chat/uploads
- /api/chat/{session_id}/fork
- /api/chat/{session_id}/invite
- /api/chat/{session_id}/join-ticket
- /api/chat/{session_id}/leave
- /api/chat/{session_id}/messages

### `/api/agents/{agent_id}/builder/turn` — Agent-builder assistant

The `/api/agents` CRUD this section used to document is gone: `/api/v1/agents*`
absorbed every operation it served and the router was deleted (remediation
Track C, Task C1.2). One route survives under this prefix — the builder's
conversational turn.

`POST /api/agents/{agent_id}/builder/turn` (owner only) runs one turn of the
builder's assistant: it takes the owner's message plus the transcript so far
and the caller's own plugin candidates, and asks the configured LLM for a
configuration patch. When the patch is applied it goes through the same
`PUT /api/v1/agents/{agent_id}` path a hand edit uses — so the
builder-declaration → enforced-scope derivation is identical either way.

The model's proposal is filtered before anything is written: unknown fields,
knowledge/plugin ids outside the caller's own candidate lists, and tones
outside the four the UI offers are dropped, and neither `status` nor the
`*_mode` scope columns are writable from a conversation.

Returns `{reply, patch, agent, suggestions}`. Two optional request fields
serve the builder page's unsaved working copy: `apply` (default `true`) writes
the patch as described above — pass `false` to get the sanitized patch back
**without** writing, in which case `agent` is `null`; and `config`, the
caller's unsaved copy, narrowed to the patchable keys and used only to build
the prompt. With no AI credential configured the endpoint answers
`503 builder_llm_unavailable` and the form stays fully usable by hand.

- /api/agents/{agent_id}/builder/turn
### `/api/sharing` — Owner-initiated sharing of Library items

The owner-scoped counterpart to `/api/access` (which is admin-only): the creator
of a Library item may share it with groups they belong to, plus `Everyone`
(workspace-wide). Writes the same `resource_grants` rows as the admin layer.
Shareable resource types are `collection` and `agent` — skills are excluded
because an approved store entity is already readable by every authenticated
user.

**Track C6 — agent-sharing needs admin approval (Postgres-backed instances).**
A user may build agents freely, but when a NON-ADMIN actor shares an `agent`
with a group it has not already reached, the grant is not written
immediately: it is queued in `share_requests` and `PUT /api/sharing/agent/{id}`
answers `202` (not `200`), with `pending_group_ids` naming what's awaiting a
decision. An admin actor (regardless of who owns the agent) and any un-share
(revoking a group) both stay instant and answer `200`, matching every other
resource type. `GET /api/sharing/agent/{id}` always echoes the current
`pending_group_ids` so a page reload still shows "pending approval". The
queue (`share_requests`) is Postgres-only (A3 ratchet) — see
`/api/admin/share-requests` below — but sharing itself never regresses: on a
DuckDB-backed instance the approval step simply isn't active, so a
non-admin's agent share falls back to the pre-C6 instant grant instead of a
`501`. Only the admin queue endpoints answer `501` there.

- /api/sharing/groups
- /api/sharing/{resource_type}/{resource_id}

### `/api/admin/share-requests` — Agent-sharing approval queue (Track C6, PG-only)

These endpoints are NOT gated by `features.store_moderation_enabled` — that flag
hides the `/admin/store` WEB PAGE (off by default), which is the only UI that
renders this queue. While it is hidden, a queued request is still listed and
decided here. See [feature-flags.md](feature-flags.md).

Every route requires admin. `GET` lists queued requests, optionally filtered by
comma-separated `status` (`pending`/`approved`/`rejected`; omitted returns every
decision, newest first — the queue doubles as its own audit trail). Each row
carries resolved display fields (`resource_name`, `requested_group_name`,
`requested_by_email`) alongside the raw ids. `PATCH /api/admin/share-requests/{id}`
takes `{"decision": "approve" | "reject"}` — the decision rides in the body
rather than a verb path segment, the same shape as
`PATCH /api/v1/agents/{agent_id}/memories/{memory_id}`'s `{"action": ...}`
(`tests/test_api_design_rules.py::test_no_new_verbs_in_path` forbids a new
verb segment in a path). `decision: "approve"` writes the grant via the same
`resource_grants_repo().ensure_grant` the admin-curated `/admin/access` layer
uses — an approved share reaches the grantee through the identical mechanism
the shared-agent runtime already honors — and marks the request `approved`
with `decided_by`/`decided_at`. `decision: "reject"` leaves no grant and marks
it `rejected`. An unrecognized `decision` is `400`. The PATCH is a clean `404`
on an unknown id OR a request that was already decided (an atomic
`WHERE status = 'pending'` guard — a double-click can never double-write the
grant or flip an already-decided verdict). Every decision writes an
`audit_log` row (`share_request.approved` / `share_request.rejected`).
PG-only (A3 ratchet): on a DuckDB-backed instance every route here answers
`501 requires_postgres_backend`. Web-only by design — see the triple-surface
ratchet's `_SHARE_REQUESTS_ADMIN_REASON` for why no CLI/MCP vocabulary was
added.

- /api/admin/share-requests
- /api/admin/share-requests/{request_id}

### `/api/collections` — File collections (bring-your-files)

The two **preview** endpoints back the Library's file-preview modal.
`…/preview` answers "what should the viewer show?" — `kind` is `image` /
`pdf` (fetch `raw_url`), `text` (source for textual uploads, otherwise the
text ingestion extracted, capped to a glance and flagged `truncated`), or
`none` with a `reason`. `…/raw` streams the bytes for a **closed** set of
browser-renderable types (PNG/JPEG/GIF/WebP/PDF) with the media type pinned
server-side plus `nosniff`; anything else — notably an uploaded `.html` — is
`415` and previews as source text instead, so an upload can never execute
inline on this origin. Both read-gate on the parent collection's access OR a
grant on the `corpus_file` itself, so a file shared out of a folder stays
viewable by the person it was shared with.

Uploading (`POST .../files`) returns one `{file_id, filename, path,
processing_status, …, claims_purged}` per file. `claims_purged` (spec §8) is
the count of fact-graph claims dropped for that file because its content
changed in place (§6) — 0 for a brand-new file, an unchanged-content resync
or rename, or when the `facts` flag is off — so a producer's ingest
idempotence can tell "content changed, re-ingest is genuinely needed" apart
from "already shipped".

- /api/collections
- /api/collections/search
- /api/collections/{collection_id}
- /api/collections/{collection_id}/files
- /api/collections/{collection_id}/files/{file_id}
- /api/collections/{collection_id}/files/{file_id}/move
- /api/collections/{collection_id}/files/{file_id}/preview
- /api/collections/{collection_id}/files/{file_id}/raw
- /api/collections/{collection_id}/files/{file_id}/reingest

### `/api/facts` — Fact graph over Collections

Typed subjects (facts/edges) extracted from Collections documents, each
claim carrying its evidencing document, a verbatim quote and a date. Behind
the `facts` feature flag (off by default; `404` on the whole router when
disabled) and Postgres-only (A3 ratchet — a DuckDB-backed instance answers a
typed `501`). See
`docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md`.

**Read surface** (build order steps 2+3+6) — any authenticated caller, no
admin gate; visibility is enforced entirely server-side, per caller, from
readable collection grants (§4/§5). `search` and `neighbors` project
attributes and traverse edges from readable claims only; `claims` returns
the caller's readable evidence for one subject, `404` (never `403`) when it
does not exist or has no readable claim. `search` also accepts an OPTIONAL
`q` — a free-text name lookup matched against `fact_aliases.natural_key`
ONLY (never a claim's quote or attrs, so it cannot reopen the §5 attribute
oracle): the query is normalized (casefolded, spaces -> hyphens) and matched
as a substring, filtering candidates before `limit` applies, then ranked —
an exact match on the alias's slug first, a prefix match second, any other
substring match last (no `pg_trgm`/extension similarity ranking; this
schema does not enable one). All three request models are `extra="forbid"`
— an unrecognized field `422`s rather than being silently ignored. Triple-
surface: `agnes facts search|neighbors|claims` (CLI; `search` takes an
optional second positional `[query]` for `q`) and `fact_search`/
`fact_neighbors`/`fact_claims` (MCP foundation tools) call the same
repository directly — facts have no local scope, so every result is labeled
`[server]` on the CLI's stderr, a deliberate deviation from the `--scope
auto|local|server` convention (spec §12).

**Write surface** (build order step 4) — scheduler token or admin PAT, no
CLI/MCP by design (a producer contract, not an analyst command). `ingest`
is the §7.2 protocol: batch caps (≤500 documents, ≤5000 claims/request,
`413`; a single document's evidence alone over the claim cap is a `422`
`document_exceeds_claim_cap`, never split), the verbatim gate (§8, a quote
must be a substring of one chunk of the evidencing document's extracted
text OR of the document's own SERVER-STORED `filename`/`path` — never a
producer-supplied identity string off the wire, which would let a producer
self-certify an invented quote), union vs `full_documents` replace mode,
alias/edge resolution, a `documents[]` entry's OPTIONAL `source_url` (§8/O7,
e.g. the crawler's Graph `webUrl`) persisted onto `corpus_file_sources` for
the citation's "Open in source" link — validated https-only/length-capped,
dropped (never rejects the surrounding claim) when absent or invalid; a
SENT-but-invalid value is itemized on the response's `source_urls_rejected:
[{doc_id, reason}]` (never an absent one), same shape as `claims_rejected`,
`wrong`-correction re-attachment across a subject's delete-then-recreate,
and a post-ingest orphan sweep (zero-claim subjects deleted and counted).
`review_items` mixes two self-describing shapes (a `kind` discriminator on
each) — `possible_duplicate_of` entity-resolution candidates, and (§7.3)
`single_valued_conflict`: a functionally single-valued edge type
(`facts.single_valued_edges` in instance.yaml, default `owned_by`/
`for_client`) whose src carries >1 distinct dst with a live claim — pre-
existing edges included, nothing persisted, so it clears the moment a
dst's claims are gone. Same detection re-runs at read time in
`collection_facts_summary` (surfaced on the collection detail page),
caller-scoped: a dst the caller cannot independently read (its own claim
AND the edge's own claim both readable, the same discipline
`possible_duplicate_of` review items get) never appears. Response is the
run report: `{claims_written, claims_accepted_via_identity,
claims_rejected: [{row, reason}], deferred: [...], subjects_created,
subjects_deleted, corrections_active: [...], review_items: [...]}`.
`claims_accepted_via_identity` is the subset of `claims_written` whose
quote passed the gate ONLY via the document's filename/path — surfaced so
an operator can see how much evidence is filename- rather than
content-grounded (weaker evidence still, per §8's own honesty note that the
gate validates the quote, not the fact). `documents` may be
omitted only when every evidence `doc_id` already resolves through a prior
upload's `corpus_file_sources` mapping — otherwise `400` with the
unresolved ids itemized. Corrections management
(`PUT`/`DELETE /api/facts/corrections/{subject_kind}/{subject_id}`,
`wrong`/`restricted`/`revealed`, each reasoned and audit-logged) and the
producer export (`GET /api/facts/corrections` — every `wrong` subject's
natural keys, spec §7.4) round out the write surface.

Every successful ingest batch also persists a copy of its run report to
`facts_ingest_runs` — written AFTER the ingest transaction commits, so a
report-write failure never rolls back or fails the ingest itself (see
`app/api/facts.py::facts_ingest`). `GET /api/facts/ingest-runs?limit=`
(admin, default `20`, max `200`) lists them newest-first: this is what the
`/admin/data-sources` source card (spec §13.2) reads for its pipeline-strip
counts and per-category error badges — an admin-only, UI-internal surface,
not an analyst query (no CLI/MCP analogue).

- /api/facts/search
- /api/facts/neighbors
- /api/facts/{subject_id}/claims
- /api/facts/ingest
- /api/facts/ingest-runs
- /api/facts/corrections
- /api/facts/corrections/{subject_kind}/{subject_id}

### `/api/connectors` — Connector manifest

- /api/connectors/manifest
- /api/connectors/params
- /api/connectors/{slug}/prompt

`/api/connectors/{slug}/prompt` returns one connector's full setup prompt
(the post-frontmatter SKILL.md body, brand-substituted) for slugs the
manifest lists. Consumed by `agnes connectors show <slug>` so the install
prompt can reference connector setup by name instead of inlining every
body.

`/api/connectors/params` serves per-tenant connector params (the
`connectors:` overlay of `instance.yaml`, filtered against the seed
manifest) as plain string values; `agnes init`/`agnes update` write them
to the workspace's `.claude/agnes/.env`. When the operator provisioned a
shared Google Workspace OAuth client, the response backfills
`connector-gws` params including the client-secret value (a Desktop-app
OAuth client secret is an app identifier, not a user credential); the
legacy `*_ENV` pointer key is still emitted alongside the value for
backward compatibility with seed skills that read the pointer shape.
Overlay keys win over server-resolved ones.

### `/api/data-apps` — Hosted data apps control plane

Control-plane REST for hosted user web apps (`data_apps` registry, v96) — a
user-owned app (internal template or external git repo) deployed to a
runtime container and put to sleep after an idle timeout. RBAC: owner,
Admin, or a group holding a `resource_grants` row on `(data_app, <slug>)`
may view; only owner or Admin may mutate. Gated behind
`data_apps.enabled` in `instance.yaml` (404 `data_apps_disabled` when off).
CLI: `agnes app list/show/create/deploy/stop/delete/logs/git-credential`
plus `agnes app draft create/delete`. MCP tools (list/show/deploy/logs plus
the wave 3B AI-authoring flow, matching the view-vs-mutate RBAC split
above): `data_apps_list`, `data_app_get`, `data_app_deploy`,
`data_app_logs`, `data_app_create_draft`, `data_app_delete_draft`,
`data_app_git_credential` — no MCP analogue for
create/stop/delete/secrets/reap-idle.

**Linked (externally-hosted) apps (v108):** a `repo_mode='linked'` row points at
an app running elsewhere (e.g. a Keboola-platform data app ingested via an MCP
source) via an `external_url`, with no git repo/runtime — Agnes only catalogs +
grants + links it. `GET /api/data-apps?kind=hosted|linked` filters the list;
each entry carries `kind` + `effective_description` (an admin `description`
override wins over the synced one). `PATCH /api/data-apps/{slug}` sets that
override on a managed (linked) app (owner/Admin only; `409 not_managed` for a
hosted app). Triple-surface: `agnes app list --linked` / `agnes app
set-description` + MCP `data_app_set_description` and the `kind` arg on
`data_apps_list`. Soft-deleted linked rows (upstream app gone) 404 on every
by-slug surface but keep their grants for a lossless re-link.

- /api/data-apps
- /api/data-apps/reap-idle
- /api/data-apps/{slug}
- /api/data-apps/{slug}/deploy
- /api/data-apps/{slug}/drafts
- /api/data-apps/{slug}/drafts/{draft_slug}
- /api/data-apps/{slug}/git-credential
- /api/data-apps/{slug}/logs
- /api/data-apps/{slug}/preview-grant
- /api/data-apps/{slug}/readiness
- /api/data-apps/{slug}/secrets
- /api/data-apps/{slug}/stop

`POST /{slug}/deploy` fast-forwards the app's internal git repo's
`agnes-live` branch, mints a fresh PAT scoped to `data-app:<slug>` (revoking
the previous one), decrypts the app's stored secrets, builds the runtime
`config.json` + container spec, and hands both to the `apps-runner` sidecar.
A failed runner call sets the app's state to `error` (with the runner's own
message in `state_detail`, which the detail response carries) and returns 502.
The two causes are reported apart, because they send an operator to different
places: `runner_unavailable` means the sidecar never answered — it is down,
unreachable, or slower than the client timeout — while `runner_error: <code>`
means it answered and named the problem (`image_not_found`,
`image_not_allowed`, `bad_runner_token`, `docker_error: …`). Collapsing the
second into the first blames a healthy, responding process; `GET /{slug}/logs`
reports the same pair the same way. `POST /reap-idle` is `require_admin`-gated (the
scheduler's shared-secret token resolves to a synthetic Admin user) and
stops any `running` app idle longer than its own `idle_timeout_s`.

`POST /{slug}/git-credential` mints a 24h-scoped PAT (`data-app-git:<slug>`)
for an AI-authoring session and returns it embedded in a `git+https` clone
URL, so an agent can push to the app's internal repo without a standing
credential. `POST /{slug}/drafts` (wave 3B AI-authoring flow) creates a
draft copy of a prod app on a new git branch — `ensure_branch` creates the
branch on the *parent's* repo (a draft has no repo of its own, just a
`data_apps` row with `is_draft=True` and `parent_app_id` set), and the
returned `git_clone_url` is minted against that same parent repo. Drafts
are excluded from `GET /api/data-apps`'s default listing and instead
surface inlined as `"drafts": [...]` on the parent's `GET /{slug}` detail
response (empty/omitted for a draft's own detail — drafts can't themselves
be drafted from, 400 `parent_is_draft`). `DELETE /{slug}/drafts/{draft_slug}`
tears one down — owner/Admin of the *parent* — via the same teardown as
`DELETE /{slug}` (container stop, token revoke, registry row delete) plus
deleting the draft branch on the parent's repo; 400 `not_a_draft` if
`draft_slug` isn't a draft of `slug`. Deleting a prod app with `DELETE
/{slug}` cascades: any live drafts are torn down first, so a parent delete
never leaves orphaned draft rows/branches/containers behind.

`POST /{slug}/preview-grant` (wave 3C in-chat preview loop) mints a
short-TTL (30 min) `data-app-preview:<slug>` scoped token in the same
`access_tokens` table (no new schema) and returns it as a `preview_cookie`
Set-Cookie string scoped `Path=/apps/<slug>/; SameSite=Lax; HttpOnly`. Any
caller who can already *view* the app (owner, Admin, or a group grant — the
same predicate as `GET /{slug}`) may request one — unlike `git-credential`,
this is not owner/Admin-only. The ingress proxy's serving path
(`/apps/<slug>/...`) accepts a valid, unexpired preview token pinned to that
exact slug in place of a normal session/PAT; the token is rejected outright
on this JSON control-plane API. Chat-only MCP tools
(`agnes_data_app_preview`/`_refresh`/`_close`/`_credentials`, no REST/CLI
analogue) drive the in-chat split-pane preview iframe on top of this grant.

### `/api/data-packages` — Public data packages

- /api/data-packages/{slug}

### `/api/data` — Table data access

- /api/data/{table_id}/check-access
- /api/data/{table_id}/download

### `/api/attachments` — Connector-catalogued attachment binaries

- /api/attachments/{source}/{attachment_id}/download — streams one attachment
  file a connector stored on the server, looked up by id in the source's
  declared catalogue table (`src/attachment_sources.py`; `jira` is the first
  source: the `attachments` catalogue's `local_path`). RBAC is table-level — read access to
  the catalogue table via `can_access_table`, the same gate as the parquet
  download; a catalogue table marked `server_only` refuses with 403
  `attachment_table_server_only`, keeping its binaries on the server just as
  it keeps its parquet. Misses stay distinguishable from denials: 404
  `attachment_not_found` (no such row) / `attachment_not_stored` (row exists,
  no bytes on the server — over-size skip, transform-time miss, or removed
  since; fall back to the upstream system for these) vs the RBAC 403 — and
  both stay distinguishable from malfunctions: a catalogued file the server
  cannot OPEN (permissions/I-O) answers 503 `attachment_unreadable`, never a
  404 that would send callers upstream while the outage looks normal. Every
  fetch, granted or denied, is audited as `attachment.download`. Consumed by
  `agnes attachment get <source> <id>`; no MCP analogue (binary byte-stream,
  mirrors the `/api/data/{table_id}/download` channel).

### `/api/debug` — Debug utilities

- /api/debug/throw

### `/api/glossary` — Keboola-imported business-term glossary (user-facing)

Read/search over `glossary_terms`, populated by the Keboola semantic-layer
importer (`keboola-semantic-layer-refresh` job) — see
`docs/superpowers/specs/2026-07-17-keboola-glossary-import-design.md`.
Relevance-ranked search uses DuckDB FTS BM25 with an ILIKE fallback.

- /api/glossary
- /api/glossary/search
- /api/glossary/{glossary_id}

### `/api/health` — Health checks

- /api/health
- /api/health/detailed

### `/api/initial-workspace` — Initial workspace (user-facing)

- /api/initial-workspace
- /api/initial-workspace.zip
- /api/initial-workspace/applied

### `/api/kai` — Embedded kai-agent turn engine (host wiring)

Host-side wiring for an embedded `kai-agent` turn engine (`app/api/kai.py`).
Enabled only when `KAI_HOST_JWT_SECRET` is set — every route below `503`s with
`kai_integration_not_configured` otherwise, so an instance that does not embed
the engine exposes nothing.

- /api/kai/sessions — `POST`, ordinary user auth, **no request body**. Creates
  the `chat_sessions` row and returns `{chat_id, token, expires_at}`. `token`
  is the short-lived HS256 session JWT the engine verifies; every identity
  claim comes from the resolved caller, never from the request, so a caller can
  only ever mint a token for themselves. `chat_id` is Agnes's — the caller
  passes it to the engine as `body.id` so the engine's chat row and this
  session share one key (no cross-database join). Signed with
  `KAI_HOST_JWT_SECRET` and stamped with `KAI_HOST_JWT_ISSUER` /
  `KAI_HOST_JWT_AUDIENCE` / `KAI_TENANT_ID`, which must match the engine's
  `HOST_JWT_*` config. Lifetime is 12 h, inside the engine's 24 h `exp` ceiling.
- /api/kai/tickets — `POST`, authenticated by the opaque
  `downstream_credential` carried in that JWT (**not** user auth, and **not** a
  broker ticket — presenting a `main`/`mcp` ticket here is `401
  kai_credential_scope_mismatch`, so a sandbox that captured one turn's ticket
  cannot refresh itself). Called once per turn by the engine's server, never by
  the sandbox. Returns `{"llm": "<ticket>"}`, plus `"mcp"` when
  `KAI_BROKER_MCP_ENABLED` is set. Minting retires the session's previous
  *egress* tickets only — one live set per chat, while the session credential
  in its own scope survives (the engine has no way to be handed a replacement).

- /api/kai/mcp — `POST`, authenticated by a **`kai_mcp`-scoped broker ticket**
  (an `llm` ticket is rejected, and so is the native sandbox's `mcp` — see the
  scope split below). Note the asymmetry with the response key: `/api/kai/tickets`
  returns this ticket under `"mcp"`, which is the engine's wire name for it, while
  the broker scope it carries is `kai_mcp`. Forwards the sandbox's verbatim
  Streamable-HTTP MCP request to Agnes's own MCP server under the ticket's
  real identity, and streams the response back chunk by chunk over a real
  HTTP self-call to `AGNES_MCP_INTERNAL_URL` — an in-process ASGI dispatch
  buffers the whole reply and applies no timeout, which would trip the
  engine relay's time-to-headers bound on any slow tool (a Streamable-HTTP
  server may
  answer as SSE). The brokered identity is a short-lived `mcp-oauth` access
  token minted for the ticket's user, so the engine reaches exactly the tools
  and RBAC a Claude Desktop connector would — the broker adds no authority.
  A session bound to a scope-limited agent (or an agent whose session user is
  not its owner) instead gets a registered `agent_session` token: the
  resolver rebuilds owner-grants ∩ agent-scope live per request
  (`AgentPrincipal`), the same narrowed identity the native broker replay
  mints. A co-session is refused (`403 mcp_not_available_to_co_session`)
  rather than resolved to its stored owner. Point the engine's
  `HOST_BROKER_MCP_URL` here and set `KAI_BROKER_MCP_ENABLED`.
- /api/kai/workspace — `GET`, authenticated by the session credential (the
  engine's *server* calls it once per SDK process spawn; the sandbox never
  sees it). Returns `200` with a gzipped tar of the caller's workspace tree,
  or `204` for "no workspace" — the engine treats any other status as a
  failed turn. The tree is the admin-registered Initial Workspace Template
  when one is synced, else the bundled default: `CLAUDE.md`, the org
  `PreToolUse` safety hook, and `.claude/skills/*`. Agnes's own sandbox-image
  build assets (`docker-sandbox/`) are excluded — they
  describe how to build a sandbox, not how to work in one.

  `CLAUDE.md` is the **rendered** Workspace Prompt, not the template's static
  copy — the same document `WorkdirManager` writes over that file when it
  prepares a native chat sandbox, and the same one `agnes init` fetches from
  `GET /api/welcome`, RBAC-filtered for the caller. Two exceptions, both
  inherited from `run_init` rather than specific to the engine: in
  git-template override mode the registered template's `CLAUDE.md` wins
  verbatim (the git override and the admin Workspace Prompt are mutually
  exclusive by design — see
  [initial-workspace-override.md](initial-workspace-override.md)), and a
  co-session gets the un-filtered bundled text, because the rendered document
  describes one identity's reachable tables and skills and a co-session has
  no single one. A session bound to an agent additionally carries the agent
  overlay — the persona `CLAUDE.md` (which replaces the rendered prompt,
  native parity with `WorkdirManager._materialize_profile`), the identity
  skill, and the active memories at `.claude/agent-memory.md`; its flattened
  marketplace components are intersection-filtered for a scope-limited
  agent. The payload is therefore per-session, but stays
  byte-stable for a given session and configuration, which is what the
  engine's re-fetch on every SDK respawn relies on. Per *session* rather than
  per caller because the rendered document carries a date (`{{ today }}` in
  the shipped template), so its clock is pinned to the session's `started_at`
  — otherwise a conversation straddling midnight would rewrite the whole
  sandbox tree over a date string.

The LLM upstream needs no new route: the engine's in-sandbox relay speaks plain
pass-through, which is exactly what `/api/broker/anthropic/{subpath}` already
is. Point the engine's `HOST_BROKER_LLM_URL` at it and the `llm` ticket
authenticates there in its own dedicated `llm` broker scope — **not** the
native sandbox's `main`, which also authenticates `/api/broker/agnes-api` and
would expose the caller's whole non-admin `/api/*` replay surface to the
sandbox. The real credential is injected there, model-gated, budgeted and
metered server-side.

### `/api/knowledge` — Unified knowledge search

- /api/knowledge/search — one query fanned out across document Collections
  (hybrid lexical+vector), corporate-memory knowledge items (fulltext), and
  table catalog cards; typed results (`chunk | knowledge | table`) with
  citations, RBAC fail-closed per source. Params: `q` (required), `k` (1–50,
  default 10). Triple-surface: `agnes search` + MCP tool `knowledge_search`.
- /api/knowledge/artifacts/{corpus_id}/download — streams the per-collection
  `knowledge.duckdb` artifact (chunks + embeddings) built by the K3 local
  packaging pass; listed in the sync manifest's `knowledge_artifacts` array
  and fetched by `agnes pull`. ETag/304 support. RBAC = collection grants via
  `require_resource_access`: ungranted analyst on a known collection → 403;
  unknown corpus or a not-yet-built artifact → 404. REST-only (no CLI/MCP
  analogue — mirrors `/api/data/{table_id}/download`).
- /api/knowledge/digests/{digest_id}/content — serves one maintained
  digest's markdown (K4, #799): `{id, slug, title, output_md, status,
  status_reason, generated_at}`. Listed in the sync manifest's
  `knowledge_artifacts` array as `kind: "digest"` entries (co-existing with
  the K3 `kind: "chunks"` entries) and fetched by `agnes pull` into
  `.claude/rules/ka_<slug>.md`. RBAC = `ResourceType.KNOWLEDGE_DIGEST`
  grants via `require_resource_access` — same house style as the artifact
  download above: ungranted analyst on a known digest → 403; unknown id or
  a digest that has never generated (`pending`, empty `output_md`) → 404.
  REST-only (no CLI/MCP analogue — pull-consumed, mirrors the artifact
  download and `/api/memory/bundle` channels).

### `/api/marketplace` and `/api/marketplaces` — Marketplace

- /api/marketplace/categories
- /api/marketplace/curated/{marketplace_id}/{plugin_name}
- /api/marketplace/curated/{marketplace_id}/{plugin_name}/agent/{agent_name}
- /api/marketplace/curated/{marketplace_id}/{plugin_name}/asset/{path}
- /api/marketplace/curated/{marketplace_id}/{plugin_name}/doc/{path}
- /api/marketplace/curated/{marketplace_id}/{plugin_name}/install
- /api/marketplace/curated/{marketplace_id}/{plugin_name}/mirrored/{key}
- /api/marketplace/curated/{marketplace_id}/{plugin_name}/skill/{skill_name}
- /api/marketplace/flea/{entity_id}/agent/{agent_name}
- /api/marketplace/flea/{entity_id}/detail
- /api/marketplace/flea/{entity_id}/skill/{skill_name}
- /api/marketplace/items
- /api/marketplaces
- /api/marketplaces/sync-all
- /api/marketplaces/{marketplace_id}
- /api/marketplaces/{marketplace_id}/plugins
- /api/marketplaces/{marketplace_id}/plugins/{plugin_name}/system
- /api/marketplaces/{marketplace_id}/sync

### `/api/mcp` — MCP passthrough and per-table query

Outbound MCP OAuth connect flow (2026-07-30 spec §3, PR 2): `GET
.../oauth/authorize` (human-only, grant-gated) 302s the caller's browser to
the upstream authorization server; `GET /api/mcp/oauth-client/callback`
(deliberately outside the `/api/mcp/sources/*` prefix — see the spec's
routing note) redeems the code and redirects to `/me/connections`; `DELETE
.../oauth/connection` drops the caller's stored token. `agnes mcp connect` /
`agnes mcp disconnect` are the CLI equivalents.

- /api/mcp/oauth-client/callback
- /api/mcp/passthrough/tools
- /api/mcp/passthrough/tools/{tool_id}/call
- /api/mcp/query-table/{table_id}
- /api/mcp/sources/{source_id}/my-secret
- /api/mcp/sources/{source_id}/my-secret/test
- /api/mcp/sources/{source_id}/oauth/authorize
- /api/mcp/sources/{source_id}/oauth/connection

### `/api/mcp-connect` — Headless MCP client setup

Issues a PAT for headless AI editors (Cursor, GitHub Copilot) that cannot complete the
interactive OAuth browser flow. The token is returned once and must be saved by the caller.

- /api/mcp-connect/token

### `/api/me` — Current user self-service

- /api/me/display-name
- /api/me/effective-access
- /api/me/elevation
- /api/me/external-identity
- /api/me/home-stats
- /api/me/onboarded
- /api/me/stats/queries
- /api/me/stats/sessions
- /api/me/stats/sync
- /api/me/stats/tokens

### `/api/memory` — Corporate memory (knowledge base)

- /api/memory
- /api/memory-domain-suggestions
- /api/memory-domain-suggestions/mine
- /api/memory/admin/approve
- /api/memory/admin/audit
- /api/memory/admin/batch
- /api/memory/admin/bulk-update
- /api/memory/admin/contradictions
- /api/memory/admin/contradictions/{contradiction_id}/resolve
- /api/memory/admin/duplicate-candidates
- /api/memory/admin/duplicate-candidates/resolve
- /api/memory/admin/edit
- /api/memory/admin/mandate
- /api/memory/admin/pending
- /api/memory/admin/reject
- /api/memory/admin/revoke
- /api/memory/admin/{item_id}
- /api/memory/bundle
- /api/memory/domains
- /api/memory/domains/{slug}
- /api/memory/items/{item_id}/mark-mandatory
- /api/memory/items/{item_id}/mark-unmandatory
- /api/memory/my-contributions
- /api/memory/my-votes
- /api/memory/stats
- /api/memory/tree
- /api/memory/{item_id}/dismiss
- /api/memory/{item_id}/personal
- /api/memory/{item_id}/provenance
- /api/memory/{item_id}/vote

### `/api/metrics` — Metric catalog (user-facing)

- /api/metrics
- /api/metrics/{metric_id}

### `/api/my-stack` — User stack subscriptions

- /api/my-stack
- /api/my-stack/curated/{marketplace_id}/{plugin_name}

### `/api/query` — Data queries

- /api/query
- /api/query/hybrid

### `/api/recipes` — Recipes (user-facing)

- /api/recipes
- /api/recipes/{slug}

### `/api/scripts` — Scheduled scripts

- /api/scripts
- /api/scripts/deploy
- /api/scripts/run
- /api/scripts/run-due
- /api/scripts/{script_id}
- /api/scripts/{script_id}/run

### `/api/settings` — User settings

- /api/settings
- /api/settings/dataset

### `/api/slack` — Slack integration

- /api/slack/bind
- /api/slack/commands
- /api/slack/events
- /api/slack/interactivity

### `/api/stack` — Stack subscriptions

- /api/stack
- /api/stack/browse
- /api/stack/subscribe
- /api/stack/subscription/{resource_type}/{resource_id}
- /api/stack/artefacts/candidates
- /api/stack/artefacts/{corpus_id}

### `/api/store` — Marketplace flea-market store

`POST /api/store/entities/builder/turn` runs one turn of the `/skills`
builder's conversation. It takes `{type, message, history, draft}` and returns
`{reply, patch, suggestions}` — and it writes **nothing**: a Library entity has
no row until the author saves it, so the draft lives in their browser and the
patch is merged there for them to review. The model's output is untrusted:
only the fields that type allows survive (a `plugin` patch can never carry a
`body` — its contents are an uploaded archive), a category must be one the
server actually offers, and everything is length-capped. With no AI credential
configured it answers `503 builder_llm_unavailable` and the form stays fully
usable by hand.

`POST /api/chat/sessions` accepts an optional `preview_skill` (`{name, body}`)
that backs the `/skills` builder's Preview for SKILLS. The skills catalog
reports what is on disk in a session's project scope, so previewing a skill
that exists nowhere but the author's browser means writing it there: the draft
is materialized into that one session's own `.claude/skills/`, which is forced
to be a copy so it can never reach the author's shared workspace. Both fields
are untrusted — the name becomes a directory name and is *replaced* rather
than sanitized, and the body is length-capped. Nothing is persisted beyond the
session. Both delivery paths carry it: native providers mount the session
directory, and the kai-agent provider packs the same bytes into its workspace
tarball, so the preview cannot work on one provider and silently do nothing on
the other.

`POST /api/store/entities/builder/preview-agent` backs that builder's Preview
tab for agent TEMPLATES. A template is a system prompt, so trying one means
running an agent with it — which needs a row, because a chat session runs as
an agent id. This points the caller's single scratch agent (fixed slug
`template-preview`, `status='scratch'`) at the draft and returns its slug.
Those rows are filtered out of every agent listing, so the author never sees
machinery they did not create; fetch-by-slug still resolves, which is how the
session binds. Idempotent per user — one row however many templates they try
— so a browser that dies mid-preview leaves at most one invisible row behind.
The scratch agent inherits none of the author's own knowledge or plugins: a
template carries no data access, and a preview that quietly ran with theirs
would flatter it.

`POST /api/store/entities/from-components` composes a **plugin** out of store
entities the caller can already see, instead of out of an uploaded `.zip`.
Every entity is baked into a one-plugin tree on save, so a published skill is
already served to Claude Code as a single-skill plugin — this endpoint exists
for the case that shape cannot express: one install handing someone several
skills and agent templates at once.

Body: `{name, description?, category?, components: [entity_id, …], access?,
publisher_kind?, dry_run?}`. Each component's baked subtree is merged (minus
its own `.claude-plugin/`, since the composite gets one synthesized manifest),
zipped in memory, and handed to the same `POST /entities` path — so a composed
plugin is indistinguishable downstream from an uploaded one and pays the same
guardrail review. Component directory names keep their `-by-<username>`
suffix: it is what the component's own frontmatter says, and it is what lets
two owners' same-named skills coexist in one composite.

`dry_run: true` returns the `PreviewResponse` shape (200) that the `.zip`
route's `POST /entities/preview` returns, and writes nothing.

Refusals, all typed under `detail.code`: `no_components`,
`too_many_components` (cap: `MAX_COMPONENTS`), `duplicate_component`,
`component_not_found` (**404 for an entity the caller cannot see, never 403** —
a composite must not be a probe for someone's private item),
`component_type_unsupported` (a plugin cannot contain a plugin — that would
mean merging two manifests), `component_bundle_missing`,
`component_path_conflict`, `components_too_large`.

`POST /api/admin/mcp-sources/builder/turn` is the fourth builder-turn
endpoint (after the agent, entity and package builders) and backs
`/admin/mcp-sources/new`. It proposes into the panel and writes nothing.

Two refusals in its sanitizer are specific to what it configures. A `url` the
admin has not already typed is dropped — the model may not choose which host
the instance dials, and "correcting" a URL is the same act as choosing one. An
`auth_secret_env` that is not shaped like an environment-variable name is
dropped, which is what stops a pasted token being written into a field that is
stored and displayed.

`POST /api/admin/mcp-sources/preview-introspect` dials a connection the admin
has typed and returns its tool list, writing nothing — the same relationship to
`POST /mcp-sources` that `/entities/preview` has to `POST /entities`. The
builder needs it because you cannot sensibly choose which tools to grant, or
name the source, without seeing what it exposes; the alternative was registering
a disabled row first and introspecting that, which puts a source in the list the
admin never agreed to create. It builds a row-shaped dict and runs the SAME url
guard (`_check_source_url_or_400`) and the SAME introspection the registered
path does, because a probe dials with a credential attached whether or not a row
exists. The secret is never in the payload: `auth_secret_env` names a variable
and the credential resolves out of the vault exactly as it does for a registered
source, so a connection whose secret is not stored yet fails here with that
reason.

- /api/store/bundle.zip
- /api/store/categories
- /api/store/entities
- /api/store/entities/builder/turn
- /api/store/entities/builder/preview-agent
- /api/store/entities/dryrun
- /api/store/entities/from-components
- /api/store/entities/from-markdown
- /api/store/entities/preview
- /api/store/entities/{entity_id}
- /api/store/entities/{entity_id}/docs/{filename}
- /api/store/entities/{entity_id}/files
- /api/store/entities/{entity_id}/install
- /api/store/entities/{entity_id}/photo
- /api/store/entities/{entity_id}/publisher
- /api/store/entities/{entity_id}/rate
- /api/store/entities/{entity_id}/status
- /api/store/entities/{entity_id}/verification
- /api/store/entities/{entity_id}/verification/request
- /api/store/entities/{entity_id}/versions/{version_no}/restore
- /api/store/import-bundle
- /api/store/owners

#### Publisher & verification (the card's trust line)

Two orthogonal axes on a store entity, both separate from `visibility_status`:

| Endpoint | Who | What |
|---|---|---|
| `PUT /api/store/entities/{id}/publisher` | admin | Sets `publisher_kind` to `organization` or `user`. `organization` makes the item speak for the org (label: *Your organization*) and clears any verification state. |
| `PUT /api/store/entities/{id}/verification` | admin | Sets `verified`, `changes_requested` (with an optional `note` sent to the author), or `none`. `409 publisher_is_organization` on an org-published item. |
| `POST /api/store/entities/{id}/verification/request` | owner | Asks the org to review. `409 not_discoverable` while nobody else can see the item. |

Both verification endpoints return `404 verification_disabled` unless
`store.verification_enabled` is true (**default false** — upgrade parity: an
existing instance does not grow a verification workflow out of a routine
upgrade). Enable it together with `library.show_unverified_trust`; with it off
no user-authored item can ever leave the Community trust level, since there is
no admin action to verify one.

`publisher_kind` is **stored**, never derived from the owner's Admin-group
membership: group membership is mutable and re-synced from the identity
provider, so a derived value would silently reclassify already-published skills
when an author changes groups.

**Verification never gates a read.** It feeds the card chip and the
`?verification=verified|unverified` filter only; an unverified item is fully
readable by anyone otherwise entitled to see it. `?publisher=organization|me|
other_users` filters the listing the same way (`me` / `other_users` split
user-published items against the caller).

Marking a `store_entity` grant `requirement='required'` ("In stack, locked")
requires `publisher_kind='organization'` — otherwise `422`. Required items are
fanned out into group members' installs and cannot be uninstalled
(`409 entity_required`).

### `/api/sync` — Data sync (CLI)

- /api/sync/manifest
- /api/sync/pull-confirm
- /api/sync/settings
- /api/sync/status
- /api/sync/table-subscriptions
- /api/sync/trigger

### `/api/telegram` — Telegram integration

- /api/telegram/status
- /api/telegram/unlink
- /api/telegram/verify

### `/api/upload` — Session and artifact upload

- /api/upload/artifacts
- /api/upload/local-md
- /api/upload/sessions

### `/api/user` — User setup tokens

- /api/user/cowork-bundle
- /api/user/setup-tokens
- /api/user/setup-tokens/{token_id}

### `/api/users` — User administration

- /api/users
- /api/users/{user_id}
- /api/users/{user_id}/activate
- /api/users/{user_id}/deactivate
- /api/users/{user_id}/reset-password
- /api/users/{user_id}/set-password

### `/api/v1/agents` — Agent management (owner-scoped CRUD, scope, agent PATs)

This is also the Agent builder's own CRUD surface (`/agents`, client-rendered
against this API) — `knowledge`/`plugins`/`surfaces`/`role`/`tone`/`greeting`/
`status`/`template_entity_id` are the builder's wire fields, accepted here
directly, and `slug` is optional on create (auto-derived from `name` when
omitted). A dedicated `/api/agents` adapter router served the same shape
until the remediation-program's "one agent model" Track C1 folded it into
this API (Task C1.1) and deleted the router (Task C1.2). `GET /api/v1/agents`
and `GET /api/v1/agents/{agent_id}` are grant-aware (owner ∪ shared into one
of the caller's groups via `/api/sharing/agent/{id}`); a grant conveys *use*
only — mutations and token issuance stay owner-or-admin.

`DELETE /api/v1/agents/{agent_id}` cascades: every PAT minted for the agent is revoked, every outbound webhook registration (`/api/v1/agents/{slug}/webhooks`) is removed, and every harvested sandbox artifact row + its object-store blob (`/api/v1/sessions/{id}/artifacts`) is deleted. The object-store blob deletes are best-effort — a single failed delete is logged and skipped rather than blocking the agent delete (an orphaned blob under a deleted agent's `agent-artifacts/` prefix is a cheap, non-sensitive leak).

`PUT /api/v1/agents/{agent_id}/scope` — replace an agent's resource-grant set. Each of `plugins_mode`/`connections_mode`/`tables_mode`/`memory_mode` is `'all'` (no narrowing on that axis — the agent's authority passes through as the owner's set) or `'selected'` (narrowed to the accompanying `agent_scope` rows for that axis, e.g. specific table/plugin/connection/memory-domain ids). **This is live-enforced, not advisory**: a `'selected'`-scoped agent's brokered requests are authorized against `(owner grants ∩ agent scope)` via a restricted `AgentPrincipal`, never the owner's full grants — see `docs/superpowers/specs/2026-07-25-agent-scope-live-enforcement-design.md`. An agent PAT is issuable only once every mode is `'selected'` (`403 agent_not_selected_mode` otherwise), so an issuable PAT is always a real restriction of its owner, never a copy of the owner's full authority. One item type is routing rather than authority: `('slack_channel', <channel_id>)` binds the channel's @mentions to this agent (the Slack surface creates the thread session with this agent's id, prefixes the first turn with a `[slack context: …]` header, and acks the mention with an 👀 reaction) — at most one non-deleted agent may hold a given channel (`409 slack_channel_taken`), and the binding grants no plugin/table/connection reach. **Trust model:** a binding is a deliberately shared surface — any channel member who passes the Slack gates (admin channel allowlist, identity binding, CHAT grant) invokes the agent, and the routed session is created AS THE AGENT'S OWNER end to end: session row, sandbox workspace/rails/personal override, and brokered authority (owner grants ∩ agent scope) all resolve from the owner, exactly like the agent's API runs — never from the mentioning user, whose identity only gates participation and rides along as sender attribution. Any gated member may continue a routed thread. A binding requires a non-passthrough agent (at least one scope mode 'selected'; `400 binding_requires_selected_scope` otherwise, and widening a bound agent to all-'all' is refused with `409 agent_has_slack_binding`) so routed turns always carry the enforced AgentPrincipal — never the owner's plain identity. Bind only channels where that is intended, and narrow the agent's scope accordingly.

- /api/v1/agents
- /api/v1/agents/{agent_id}
- /api/v1/agents/{agent_id}/scope
- /api/v1/agents/{agent_id}/tokens

### `/api/v1/agents/{agent_id}/memories` — memory management (V1c Task 5)

Owner-facing inspect/approve/archive/delete over an agent's private memory notebook — the management counterpart to the "remember" tool (`POST /api/v1/sessions/{id}/memories`, above). Same auth matrix as the rest of `agents_admin.py`: `GET` allows admin read (`require_owner=False`, mirrors `GET /api/v1/agents/{id}`); `PATCH`/`DELETE` require ownership (403 `agent_not_owned` for an admin on someone else's agent, 404 for anyone else). Every route 404s `agent_not_found` for a non-owner/non-admin caller, and `memory_not_found` for a memory id that doesn't exist or belongs to a different agent than the path.

**C4 — "active" ≠ "in effect".** `materialize_memories` packs an agent's active memories (newest-first) into a spawned session's workdir up to a ~6000-token budget (`app.chat.agent_profile._MEMORY_BUDGET_CHARS`); with enough active memories, older ones — including a just-approved one sitting behind newer content — never actually materialize. `GET` marks every `active` row with `in_budget: bool`, computed via the same `select_in_budget` split `materialize_memories` uses at spawn time, so this list can never drift from what a live spawn would actually see. The key is omitted entirely for `pending`/`archived` rows (neither ever materializes, budget or not).

`GET /api/v1/agents/{agent_id}/memories?status=` — `200 {data: [{id, agent_id, content, status, source_session_id, created_at, activated_at, archived_at, in_budget?}], has_more, next_cursor}`. `status` (optional) filters to one value (`pending`/`active`/`archived`); omitted returns all statuses, newest-first.

`PATCH /api/v1/agents/{agent_id}/memories/{memory_id}` — `{action: "approve" | "archive"}` → `200` (the updated memory, same shape as a `GET` row). `approve` flips a `pending` row to `active` (no-op if it isn't currently `pending` — mirrors `agent_memories_repo().approve`'s semantics); `archive` moves any row to `archived`. An unrecognized `action` is `400 {"code": "invalid_action"}`.

`DELETE /api/v1/agents/{agent_id}/memories/{memory_id}` — `204`.

Mirrored by `agnes agent memory list [--status pending|active|archived] [--json]`, `agnes agent memory approve/archive <slug> <memory_id>`, and `agnes agent memory delete <slug> <memory_id> [--yes]` (V1c Task 7). No MCP analogue, permanently — see `tests/test_documentation_api_triple_surface.py`'s `_AGENT_MEMORY_ADMIN_REASON`.

- /api/v1/agents/{agent_id}/memories
- /api/v1/agents/{agent_id}/memories/{memory_id}

### `/api/v1/agents/{slug}/responses` and `/api/v1/jobs/{job_id}` — Agent-as-API runtime (one-shot)

`POST /api/v1/agents/{slug}/responses` — one-shot request/response over an owner's agent. `{input: str (required), background?: bool, timeout_s?: int = 120 (clamped 1..600), metadata?: dict}` → `200 {answer, session_id, response_id, usage, agent_config_hash, request_id}` when the turn completes within `timeout_s`, or `202 {job_id}` when `background: true` was requested OR the sync wait outran `timeout_s` (the run itself is never killed — only the wait is bounded; a timed-out sync call degrades to a background job that resumes waiting on the SAME session instead of re-sending the prompt). Callable with either an interactive session token or an agent PAT scoped to this exact agent (403 `agent_pat_wrong_agent` otherwise); requires the same `ResourceType.CHAT` grant the web chat UI does. Supports an `Idempotency-Key` header (scoped to the caller+agent): a replay with an identical request body returns the original response verbatim; a replay with a different body under the same key is `409 idempotency_key_reuse`.

`GET /api/v1/jobs/{job_id}` — owner-scoped read of a background/degraded job (`404` unless the caller owns it). Maps internal job status to `queued|in_progress|completed|failed`; a `completed` job's `result` carries the same `{answer, session_id, usage}` shape the synchronous 200 response does.

- /api/v1/agents/{slug}/responses
- /api/v1/jobs/{job_id}

### `/api/v1/agents/{slug}/usage` — Agent-as-API monthly token usage (V1b Task 8)

`GET /api/v1/agents/{slug}/usage?period=YYYY-MM` — per-agent monthly token usage against its budget, plus a per-caller breakdown for a SHARED agent (C2.3) run by multiple users (remediation Track C, C2.4). Auth (`require_agent_usage_principal`) is owner/runnable-grantee/agent-PAT like `/responses`, PLUS an admin inspection fallback the other runtime routes deliberately lack. `period` defaults to the current UTC month; an explicitly passed value that isn't `YYYY-MM` is `400 {"code": "invalid_period"}`. Returns `{period, agent_slug, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, total_tokens, budget_limit, budget_remaining, by_caller}` — the usage-shaped fields mirror Anthropic's own usage object. `total_tokens` is `input + output + cache_creation`, deliberately EXCLUDING `cache_read_tokens` (informational only) — the same quantity the broker's `check_budget` compares against `token_budget_monthly`, so `budget_remaining` (floored at `0`) lines up with when a call against this agent would actually start 429ing with `budget_exhausted`. `budget_limit`/`budget_remaining` are `null` for an agent with no configured budget. `by_caller` is a list of `{caller_user_id, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, total_tokens}`, one row per distinct caller who ran the agent that month (`caller_user_id` is `null` for an unattributed row — pre-C2.4 usage, or the DuckDB backend, which has no column to distinguish callers) — visible ONLY to the agent's owner or an admin; a plain runnable grantee gets `by_caller: null` (aggregate totals only, never other callers' usage). Mirrors `agnes agent usage` and the `agent_usage` MCP tool.

- /api/v1/agents/{slug}/usage

### `/api/v1/agents/{slug}/sessions` and `/api/v1/sessions/{id}` — Agent-as-API multi-turn sessions, SSE (V1b Task 4)

Multi-turn counterpart to the one-shot runtime above: create a session bound to an agent, then stream one turn at a time as Server-Sent Events. Auth is owner/agent-PAT scoped exactly like `/responses` (`403 agent_pat_wrong_agent` on the create-session call; every `/api/v1/sessions/{id}/*` route instead collapses ANY mismatch — wrong owner, or an agent PAT bound to a different agent — to a uniform `404 session_not_found`, never leaking cross-owner existence).

`POST /api/v1/agents/{slug}/sessions` — `{}` → `201 {"session_id": "..."}`. Creates an API-surface (`Surface.API`) chat session bound to the agent; no prompt is sent yet. `429 {"code": "concurrency_cap"}` on the same per-user concurrency cap `/responses` and the web chat UI enforce.

`POST /api/v1/sessions/{id}/messages` — `{input: str (required), response_format?: dict}` → `200 text/event-stream`. Attaches a fresh sink for this one turn, sends `input`, and streams AG-UI events (`RUN_STARTED`, `TEXT_MESSAGE_CONTENT` deltas, `TOOL_CALL_START`/`TOOL_CALL_END`, then a terminal `RUN_FINISHED` or `RUN_ERROR`) until the turn ends. Each SSE record carries an `id: {session_id}:{seq}` line (monotonic per session). `response_format` is accepted on the wire but not yet enforced (full structured-output support lands in V1b Task 7). A second concurrent `POST .../messages` for the same session is rejected with `409 {"code": "turn_in_flight"}` — only one turn may be in flight per session. Disconnecting the SSE client does NOT cancel the turn — the run keeps going server-side (and burns budget) until it finishes or `POST .../cancel` is called explicitly; a turn that never emits a terminal frame is force-terminated with `RUN_ERROR{code: "idle_timeout"}` after a bounded idle window.

`GET /api/v1/sessions/{id}` — `{session_id, agent_id, state, messages: [...]}` — session state (`active`/`archived`) plus full message history.

`POST /api/v1/sessions/{id}/cancel` — `202 {}` — cancels the in-flight turn (if any); the session itself is preserved (contrast `DELETE`, which archives it).

`DELETE /api/v1/sessions/{id}` — `204` — kills the live runner (if any) and archives the session. Before killing, best-effort harvests any artifacts the session's sandbox produced (see below) — the handle is only reachable while the sandbox is still live, so this must happen first.

- /api/v1/agents/{slug}/sessions
- /api/v1/sessions/{session_id}
- /api/v1/sessions/{session_id}/messages
- /api/v1/sessions/{session_id}/cancel

### `/api/v1/sessions/{id}/artifacts` — sandbox artifact harvest + download (V1b Task 5)

The chat sandbox is a separate per-session environment (a container under the docker provider); files an agent writes under `/work/outputs` inside it are harvested into the object store + `agent_artifacts` registry at two points: when a one-shot `/responses` (or `/jobs`) turn completes, and when `DELETE /api/v1/sessions/{id}` tears the sandbox down. Harvest is best-effort — a store that isn't configured, a missing `outputs/` dir, or a single file's read/write failure are all logged and skipped, never surfaced as an error on the run/delete path they piggyback on. Filenames are agent-chosen (an injection surface) and are sanitized to a flat, CR/LF-free basename before use — both as the object-store key (`agent-artifacts/{session_id}/{safe_filename}`) and in the download response's `Content-Disposition` header. Per-session caps (`agent_api_artifact_max_bytes`, default 25 MiB per file; `agent_api_artifact_max_files`, default 20 per harvest call) bound how much a single run can push into the store. Auth on both routes is the same `require_session_principal` every `/api/v1/sessions/{id}/*` route uses (owner or an agent PAT bound to this exact session's agent; any mismatch is `404`, never `403`).

`GET /api/v1/sessions/{id}/artifacts` — `200 {data: [{id, filename, size_bytes, content_type, created_at}], has_more, next_cursor}` — every artifact harvested for this session so far.

`GET /api/v1/sessions/{id}/artifacts/{artifact_id}` — `200` streams the artifact's bytes (default; authenticated via this endpoint's own auth, `content-type` + `content-disposition: attachment; filename="..."`), or with `?redirect=true` a `307` redirect to a short-TTL (≤120s) presigned object-store URL when the configured store supports presigning. The redirect path is opt-in only — the presigned URL is usable by anyone who obtains it (proxy log, browser history) for the TTL window with no further Agnes auth check, so the default streams through this endpoint instead. `404` for both an unknown artifact id and one belonging to a different session.

- /api/v1/sessions/{session_id}/artifacts
- /api/v1/sessions/{session_id}/artifacts/{artifact_id}

### `POST /api/v1/sessions/{id}/memories` — the "remember" tool (V1c Task 4)

In-sandbox write side of the per-agent memory notebook (`app/api/agent_memory.py`); the read side is the pre-spawn materialization into `.claude/agent-memory.md` (`app.chat.agent_profile.materialize_memories`, V1c Task 3). `{content: str (required)}` → `201 {id, status}`.

Behavior is governed by the CALLING agent's `memory_write_mode`:

- `off` — `403 {"code": "memory_writes_disabled"}`. The remember tool is also simply not advertised in the agent's context skill when its mode is `off` (`app.chat.agent_profile._context_skill`), but the endpoint enforces this regardless of what the agent was told.
- `propose` — creates the row `status: "pending"` → `201 {"status": "pending"}`. Excluded from `list_active` (and therefore from what gets materialized into future spawns) until the owner approves it.
- `auto` — creates the row `status: "active"` (with `activated_at` stamped) → `201 {"status": "active"}`, immediately eligible for materialization into future spawns.

Guards, enforced in every mode: empty/whitespace-only `content` → `422` — this one is a Pydantic field validator on the request body, which FastAPI validates while resolving the request, before the handler body (and therefore the C2 session-mismatch check and the mode check) ever runs, so it fires even for an `off` agent or a mismatched session. The rest run in the handler body, after the mode check, so an `off` agent with valid content always gets `memory_writes_disabled` rather than a guard-specific status: `len(content) > agent_memory_max_chars` (default 2000) → `413 {"code": "memory_too_large"}`; `agent_memory_writes_per_hour` (default 20) rolling writes in the last hour → `429 {"code": "memory_rate_limited"}`; `agent_memory_max_pending` (default 100) total pending rows for the agent → `429 {"code": "memory_pending_full"}` — a cap independent of the hourly rate limit, since nothing else shrinks the pending backlog except the owner's own review. (Reaping/ignoring stale pending rows past `agent_memory_pending_ttl_days`, default 30, when counting toward this cap is a config knob landed for a future reaper — not enforced yet.)

**Auth binds to the CALLING session, never the path `{id}`.** The in-sandbox agent reaches this route through the secret broker (`app/api/broker.py`), which authenticates as the sandbox's real owner and mints a JWT carrying `chat_session_id` for the session the ticket was minted for. Because the broker replays whatever path the sandboxed agent describes, a prompt-injected agent could otherwise target a DIFFERENT session belonging to the SAME owner but a DIFFERENT agent (with a different, possibly `off`, `memory_write_mode`) — `require_session_principal`'s ownership check alone would allow it, since both sessions share an owner. So whenever a broker-minted `chat_session_id` claim is present, it must equal the path `{id}` or the request is `403 {"code": "session_mismatch"}`, regardless of ownership. An interactive owner session token or an agent PAT (neither goes through the broker) carries no such claim, so the path `{id}` — already ownership/PAT-verified by `require_session_principal` — is trusted as-is.

- /api/v1/sessions/{session_id}/memories

### `POST /api/v1/agents/{slug}/delegate` — @delegation between shared agents (Track C7 MVP)

Server-side handoff: a live, user-driven agent turn (agent A) hands ONE sub-request off to another agent the CALLER may run (agent B, named by `{slug}`), mid-turn, and gets B's answer back into A's turn. `{input: message: str (required)}` → `200 {status: "ok"|"denied"|"degraded", reason: str | null, agent_slug, answer: str | null, message: str | null}` — a denial or degrade is a normal `200` body, never an HTTP error: the caller (agent A's own in-process delegation tool) is expected to read `status`/`reason` and continue the turn on its own judgment.

Same auth binding as `/api/v1/sessions/{id}/memories` above: reached exclusively through the secret broker under A's OWN session-scoped ticket (never a client-supplied session id) — `require_delegating_session` resolves the caller's identity from whatever the broker's JWT minting produced (an `AgentPrincipal` for a restricted/shared agent, or a plain user dict with a stashed `chat_session_id` claim for the "passthrough" optimization on an unrestricted agent run by its own owner), never from a client-shaped field.

`{slug}` is resolved exactly like `require_agent_runtime_principal` resolves a runtime target (`agents_repo().get_runnable_by_slug`) — the CALLER's own runnable set (owned, or reachable via a `ResourceType.AGENT` grant), never A's owner's. THE SECURITY INVARIANT: B is spawned as a fresh CHILD session via `ChatManager.create_session(user_email=<the ORIGINAL caller>, agent_id=B)` — never A's owner, never B's owner — so B's row-level access policies (`src/access_policy.py`) bind to the caller, never a wider identity. Depth-1 only (a session spawned as a delegate target cannot itself delegate, `reason: "depth_exceeded"`) and one delegation per turn (`reason: "already_delegated_this_turn"`). An RBAC denial (`reason: "agent_not_runnable"`), an exhausted monthly budget on B (`status: "degraded"`, `reason: "budget_exhausted"`), the per-user concurrency cap (`reason: "concurrency_cap"`), or B simply not answering in time (`reason: "timeout"`) all degrade the result — none of them raise an HTTP error or crash A's turn.

Sandbox-internal RPC (standing exemption from the triple-surface CLI/MCP ratchet — see `app/api/agent_delegation.py`'s module docstring): its only real caller is agent A's own in-process delegation tool (`app/chat/runner.py`'s `_delegation_mcp_server`), not something an analyst calls directly from a terminal.

- /api/v1/agents/{slug}/delegate

### `/api/v1/agents/{slug}/webhooks` — outbound agent webhooks (V1b Task 6)

SSRF-hardened, HMAC-signed outbound notifications: register an HTTPS URL to be POSTed a small notification whenever a background `agent_response` job (see `/api/v1/agents/{slug}/responses` above) reaches `job.completed` or `job.failed`. Owner-scoped standing config — every route requires an interactive session token (`require_session_token` rejects both plain PATs and agent PATs, same posture as `/api/v1/agents/{id}/tokens`).

**SSRF guard.** `POST` validates the URL at create time (`app.chat.webhook_delivery.validate_and_resolve`): scheme must be `https`, and every IP the host resolves to must be public — any address that is private/loopback/link-local/reserved/multicast/the cloud metadata endpoint (`169.254.169.254`)/IPv6 ULA is denied with `400 {"code": "webhook_url_forbidden"}`. This is a courtesy check, not the actual guard: the SAME resolve-and-pin check re-runs on every delivery attempt (not just a re-validate — the connection goes to the freshly resolved IP directly, never the hostname), which is what actually closes the DNS-rebinding TOCTOU window between registration and send.

**Delivery payload is a notification, not the answer.** The POST body is exactly `{event, job_id, agent_slug, status, ts}` — never the agent's answer, prompt, or any other job data. A receiver that wants the actual result fetches it afterward via `GET /api/v1/jobs/{job_id}` (owner/agent-PAT authenticated). Every delivery carries an `x-agnes-signature: sha256=<hex hmac>` header (HMAC-SHA256 over the raw JSON body, keyed by the webhook's own secret) so the receiver can verify authenticity.

`GET /api/v1/agents/{slug}/webhooks` — `200 {data: [{id, agent_id, url, events, active, consecutive_failures, created_at}], has_more, next_cursor}`. The signing `secret` is never included here.

`POST /api/v1/agents/{slug}/webhooks` — `{url: str (required, https), events?: ["job.completed", "job.failed"] (default both)}` → `201 {id, agent_id, url, events, active, consecutive_failures, created_at, secret}`. `secret` (a 64-hex-char HMAC key) is returned exactly once, at creation — like an agent PAT, it cannot be retrieved again.

`DELETE /api/v1/agents/{slug}/webhooks/{webhook_id}` — `204`. `404` for an unknown id or one belonging to a different agent/owner.

A webhook is auto-disabled (`active: false`) after `agent_api_webhook_max_failures` (default 5, `instance.yaml`'s `chat:` block) consecutive delivery failures — a dead or hostile endpoint stops being retried forever rather than accumulating unbounded `webhook-deliver` job attempts.

CLI: `agnes agent webhooks list|add|delete <slug> ...` (`add` takes `--url` and repeatable `--event`, and prints the signing secret exactly once, like `agnes agent token`). No MCP tool by design — see `tests/test_documentation_api_triple_surface.py`'s `_AGENT_WEBHOOKS_REASON`.

- /api/v1/agents/{slug}/webhooks
- /api/v1/agents/{slug}/webhooks/{webhook_id}

### `/api/v1/agents/{slug}/schedules` and `/api/v1/agents/run-due` — scheduled agent runs

Design doc: `docs/superpowers/specs/2026-08-17-agent-schedules-design.md`. A schedule is a run-type label (`name`, unique per agent, ≤20 per agent) + a cadence in the product's shared schedule grammar (`every Nm`/`every Nh`, `daily HH:MM[,HH:MM]` UTC, or `cron <5-field expr>` UTC) + the `prompt` sent to the agent on each fire. Owner-scoped standing config — every CRUD route requires an interactive session token (`require_session_token` rejects both plain and agent PATs, same posture as `/api/v1/agents/{slug}/webhooks`): an agent must not be able to grant itself new unattended runs through its own tool call.

`GET /api/v1/agents/{slug}/schedules` — `200 {data: [{id, agent_id, name, schedule, prompt, enabled, last_run_at, last_status, last_job_id, created_at, updated_at}], has_more, next_cursor}`.

`POST /api/v1/agents/{slug}/schedules` — `{name: str, schedule: str, prompt: str, enabled?: bool = true}` → `201` (the created row). `400 {"code": "invalid_name"}` (must be a single path-ish segment: letters/digits/`-`/`_`, max 64 chars), `400 {"code": "invalid_schedule"}` (names the accepted grammar, including the literal `cron ` prefix footgun), `400 {"code": "invalid_prompt"}` (empty/whitespace-only), `400 {"code": "schedule_limit"}` (agent already has 20 schedules), or `409 {"code": "schedule_name_taken"}` (name already in use for this agent — names are scoped per agent, not global).

`PATCH /api/v1/agents/{slug}/schedules/{schedule_id}` — any subset of `{name, schedule, prompt, enabled}` → `200` (the updated row). Same validation as `POST`; renaming to a taken name (or your own current name) follows the same `schedule_name_taken`/no-op-success rule as any other unique-per-scope rename. `404 {"code": "schedule_not_found"}` for an unknown id or one belonging to a different agent/owner.

`DELETE /api/v1/agents/{slug}/schedules/{schedule_id}` — `204`.

`POST /api/v1/agents/run-due` — the admin/scheduler-driven sweep, gated like every other scheduler-driven sweep (`require_admin`; the scheduler's shared-secret token resolves to a synthetic Admin-group user). Walks every `enabled=TRUE` row across every owner, skips rows whose agent no longer exists or was soft-deleted, evaluates due-ness with the same `is_table_due` catch-up semantics as every other schedule in the product, atomically claims each due row (optimistic — a concurrent sweep tick that already won the claim is silently skipped, not an error), and enqueues the existing `agent_response` background job kind directly with the agent OWNER's identity (`mode: "fresh"`, `owner_user_id`, `owner_email`, `agent_id`, `prompt`) — never impersonated through the public `/responses` endpoint, since the scheduler owns no agents of its own. Enqueue is deduplicated per-minute via `idempotency_key = "agent-schedule:<schedule_id>:<floor(unix_now/60)>"`. Records `last_run_at`/`last_status` (`enqueued` or `failed_enqueue`)/`last_job_id` on the schedule row — terminal job outcomes live on the job (`GET /api/v1/jobs/{id}`) + the agent's existing `job.completed`/`job.failed` webhooks, not here. Per-row failures are logged and skipped; they never abort the sweep. Scheduler row: `agents:run-due` in `services/scheduler/__main__.py`, `every 1m`, gated on `SCHEDULER_AGENT_SCHEDULES` (default on).

CLI: `agnes agent schedule list|add|remove|enable|disable <slug> ...` (`add` takes `--name`/`--schedule`/`--prompt`/`--disabled`; `remove`/`enable`/`disable` take the schedule `name`, resolved to an id via one `list` round trip). No MCP tool by design, same reasoning as `_AGENT_WEBHOOKS_REASON` — see `tests/test_documentation_api_triple_surface.py`'s `_AGENT_SCHEDULES_REASON`. `run-due` has no CLI/MCP analogue — it is a scheduler-internal sweep trigger, mirroring `/api/scripts/run-due`.

- /api/v1/agents/{slug}/schedules
- /api/v1/agents/{slug}/schedules/{schedule_id}
- /api/v1/agents/run-due

### `/api/v2` — v2 catalog and query APIs

- /api/v2/catalog
- /api/v2/marketplace/skills
- /api/v2/metadata-cache/refresh
- /api/v2/metadata-cache/status
- /api/v2/sample/{table_id}
- /api/v2/scan
- /api/v2/scan/estimate
- /api/v2/schema/{table_id}

### `/api/version` and `/api/welcome` — Instance info

- /api/version
- /api/welcome

### Config surface & marketplace plugin controls (admin)

- /api/admin/config-surface — read this instance's complete configurable surface: every config knob with its resolved value + source (env/yaml/default), the registered Initial Workspace Template, the registered marketplaces, and `infra_repo_url`. Also exposed as `agnes admin config-surface` and an MCP tool.
- /api/marketplaces/{marketplace_id}/plugins — admin-only: list a marketplace's plugins. Each row includes `admin_disabled`, which drives the `/admin/marketplaces` Details-modal switch and the DISABLED pill.
- /api/marketplaces/{marketplace_id}/plugins/{plugin_name}/disable — admin-only: disable any registered plugin (not just built-ins) instance-wide. The plugin is then hidden from every served and admin surface for all callers — served feed, browse page, my-stack, synthetic served marketplace, the group Access tab's grant UI, and v2 `/skills` — except the Details modal, where it can be re-enabled. Disabling also clears `is_system`.
- /api/marketplaces/{marketplace_id}/plugins/{plugin_name}/enable — admin-only: re-enable a previously disabled plugin. Does **not** restore a previously-cleared `is_system`. The disabled state persists across restarts / sync re-seed until explicitly re-enabled.
