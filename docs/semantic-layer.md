# Semantic layer

A semantic model describes what your data *means* — which datasets exist, what
their columns are, how they join, and which metrics are computed from them.
Agnes stores one as a document in the [Apache Ossie](https://ossie.apache.org/)
format (incubating; the vendor-neutral successor to the Open Semantic
Interchange initiative), and derives its own flat tables from it.

> **The document is the owner.** `metric_definitions`, `glossary_terms` and
> `column_metadata` are projections of a stored document and can be regenerated
> from it. That is what lets fidelity improve later without a migration: an
> attribute Agnes does not surface yet is still *in* the document.

Design rationale:
[`superpowers/specs/2026-08-13-open-semantic-layer-contract-design.md`](superpowers/specs/2026-08-13-open-semantic-layer-contract-design.md).

---

## What a document looks like

```yaml
version: "0.2.0.dev0"
semantic_model:
  - name: retail
    ai_context:
      instructions: Use for order-level revenue questions.
      synonyms: [sales, orders]
    datasets:
      - name: orders
        source: "db.public.orders"
        primary_key: [order_id]
        fields:
          - name: order_date
            datatype: Date
            dimension: {is_time: true}
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: order_date
    relationships:
      - name: orders_to_customers
        from: orders
        to: customers
        from_columns: [customer_id]
        to_columns: [id]
    metrics:
      - name: revenue
        datatype: Decimal
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: SUM(amount)
```

Three shapes trip people up, all enforced by the schema:

- `datasets` is required and must be non-empty. A model with `datasets: []` is
  not a valid document.
- `custom_extensions[].data` is a **JSON-encoded string**, not a nested mapping.
  Write it with `json.dumps`, read it with `json.loads`.
- `expression` is required on a field. A plain column still needs a
  pass-through expression naming itself.

Agnes-specific attributes (registry ids, `query_mode`, constraint and glossary
payloads that core Ossie has no slot for) travel in `custom_extensions` under
the Agnes vendor name — never as new top-level keys, which the schema rejects.

## Sources

A source is where documents come from. Three kinds:

| Kind | Config | What happens |
|---|---|---|
| `git` | `repo_url`, `ref`, `glob`, optional `token_env` | Shallow-clone, read every file the glob matches, containment-checked against the clone root |
| `upload` | `documents` | Documents handed over directly (admin API / CLI) |
| `connection` | connector-specific | The adapter fetches from a configured data-source connection |

```bash
agnes admin semantic source add --kind git \
  --name "Finance models" \
  --repo-url https://example.com/semantics.git \
  --ref main --glob 'semantic/**/*.yaml'

agnes admin semantic source sync <source-id>
```

A sync that cannot fetch **fails loudly and imports nothing**. This matters more
than it sounds: an empty document list legitimately means "upstream deleted
everything", which prunes. A failed clone must never be able to present itself
as an empty source, so the error is recorded on the source row and re-raised.

### Scheduled refresh

Every registered source — regardless of kind — is also synced automatically by
`POST /api/admin/run-semantic-sources-refresh`, on a cadence set by
`SCHEDULER_SEMANTIC_SOURCES_REFRESH_INTERVAL` (default 6 h). One failing
source never stops the sweep over the rest; each source's own
`last_sync_at`/`last_sync_status`/`last_sync_error` (surfaced on `GET
/api/admin/semantic-sources` and in Health, below) still reflects only its own
outcome.

### Did the sync actually bring anything back?

`last_sync_status='ok'` answers "did the fetch work", never "did it import
anything" — a `connection` source scoped at a database with no semantic
content syncs green forever while owning zero models, which reads exactly
like a healthy source. Every surface that shows a source's sync state
therefore also shows **`owned_model_count`**: `GET
/api/admin/semantic-sources` (list, single-source `GET`, and the `POST`/`PUT`
responses, so every shape matches), the `sources` block of the health report,
the **Models** column on `/admin/semantic-sources`, and `agnes admin semantic
source list`.

A source that synced OK and still owns nothing is the finding, and it is
reported as one: amber on the page, a named line in the CLI listing, and its
own **"Sources that synced but imported nothing"** section in the health
report (page + CLI). Never with error styling — the fetch worked, so nothing
failed — and never for a source that has not synced at all: owning nothing
before the first run is "has not run", not "imported nothing".

The count is derived at read time, never stored: it counts `semantic_models`
rows stamped with the exact `(source, source_ref)` provenance this source's
imports write under (`src/semantic/ownership.py`). "Owns" means that and
nothing more — in particular it is **not** "its next sync could delete this
many": the prune is keyed on the same pair but is narrower (it skips
`sync_mode='detached'` rows on Postgres, and a `safe_prune` source skips the
prune entirely on a run that produced no valid document).

Invalid documents count as owned: the count answers "did this source bring
anything in", and whether what it brought in parses is a separate question
the health report's `invalid_models` already answers. A source whose
`config.provenance` override cannot be resolved reports `null` rather than a
confident `0` — the resolver validates against live state, so an
unresolvable override means "cannot say", not "owns nothing" (it is logged as
a warning server-side, renders as `—` on the page and `-` in the CLI).

### What did the sync actually look at?

A count of zero still leaves two very different explanations open: the
upstream really is empty, or its contents are invisible to the credentials
Agnes syncs with. Both read `ok · 0 models`. The second was observed live — a
Snowflake semantic view existed and the role the adapter connects as held no
privilege on it, so `SHOW SEMANTIC VIEWS` returned nothing — and it is a
misconfiguration an admin has to fix, not a state to accept.

The same four surfaces therefore also carry **`scan_scope`**: one line naming
what the source scans, derived from its own config at read time
(`src/semantic/scan_scope.py`), never stored — the scope is a property of the
row, not of a run. Per adapter:

| Source | `scan_scope` |
|---|---|
| Snowflake semantic views | `ESHOP_DEMO.RAW as ESHOP_DEMO_ROLE` — database, schema (or `ESHOP_DEMO (whole database)` when unscoped, plus `matching '<pattern>'` when a `like` narrows it) and the role that decides what is visible |
| Keboola Metastore | `Keboola project 4321 (Demo Project)` — the project the pinned connection is bound to |
| Databricks UC metric views | `Unity Catalog catalogs main, sales on <workspace host>` |
| git | `https://example.com/acme/semantics.git @ main matching '**/*.yaml'` — repository, ref (`default branch` when unset) and the glob, stated even when it is the default: a repo of `*.yml` documents against the default `*.yaml` pattern clones fine and matches nothing |
| upload / native | `uploaded documents (3)` |

`null` means no scope could be derived (an adapter with no resolver, or a
config too incomplete to name one) and every surface simply omits it — a new
adapter is additive here too. The string carries coordinates only: no token,
no credential env-var name, and any URL it echoes has its `user:pass@`
userinfo stripped first.

### Which sources the sweep runs

`enabled: false` (`--disabled` on `add`, or `PUT .../sources/{id}` with
`enabled: false`) excludes a source from **both** this scheduled sweep and the
manual `agnes admin semantic-source sync <id>` / `POST .../sources/{id}/sync`
— a disabled source's manual sync now answers `409 source_disabled` instead of
running. Re-enable it (`enabled: true`) to bring it back into rotation for
both paths.

This is the path for `git`/`upload`/`connection` sources alike, and since
#1707 Block 3 it is the ONLY scheduled semantic refresh. The Keboola and
Databricks connectors used to run their own, longer-standing ones (`POST
/api/admin/run-keboola-semantic-layer-refresh` /
`.../run-databricks-semantic-layer-refresh`); both endpoints and both
scheduler entries are gone.

Their sync logic is not: the same adapters compose the same documents and the
same central projector writes them. Only the trigger moved, and an operator
has nothing to do — every sweep first registers the sources those triggers
implied (`src/semantic/legacy_migration.py`):

| Legacy trigger | Auto-registered as | Provenance of the rows it writes |
|---|---|---|
| Keboola Metastore refresh | one `connection` source per Keboola connection holding a master token (`keboola_<connection id>`), or one for the legacy `KEBOOLA_STACK_URL`/`KEBOOLA_STORAGE_TOKEN` pair when no connection has one | unchanged: `source='keboola_metastore'`, `source_ref=<connection id>` — carried by a `config.provenance` override |
| Databricks metric-view refresh | the workspace, when one is configured (`databricks_default`) | unchanged: `source='ossie_connection'`, `source_ref='databricks_default'` |

The provenance override is why an upgrade is a no-op for what is *stored*:
every semantic model, metric, glossary term and column description is owned
by its `(source, source_ref)` pair, and re-importing the same upstream under
a new label would orphan every existing row and write a duplicate beside it.
A migrated Keboola source also carries `config.safe_prune: true`, the
full-wipe guard that sync has always used — an upstream answering with
nothing usable must not delete an installation's whole metric registry.

Registration is idempotent and never a get-or-*replace*: a source an admin
renamed, re-scoped or disabled stays exactly as they left it.

**`config.provenance` is Agnes-managed and not admin-writable.** It names the
`(source, source_ref)` pair a source's rows are written *and pruned* under, so
a source allowed to claim an arbitrary one could delete another connection's
models, metrics, glossary terms and column descriptions. `POST`/`PUT
/api/admin/semantic-sources` refuse a config carrying it (`400
provenance_not_settable`), and a stored override is validated on every sync:
the label must be a migrated legacy one, the source must run that label's
adapter, and the `source_ref` must be the source's own connection (or, for the
legacy env-credential row, the pair that path has ever stamped).

Two things the sweep skips rather than syncs, both carried over from guards
the retired triggers had built in:

| Skip | When | Where it shows |
|---|---|---|
| `skipped_running` | a Keboola source whose rows the login-triggered sync (`run_semantic_layer_refresh_background`) is writing right now — the two share one single-flight guard, so they can never overlap | the sweep's response only; the row keeps its last real sync state and the next sweep picks it up |
| `skipped_duplicate_project` | a second source resolving to the SAME upstream Keboola project as one already imported this sweep (two connections may point at one project) — importing both would write one project's rows under two refs that then delete each other's | the sweep's response, plus `last_sync_status='skipped'` with the reason in `last_sync_error` on the row |

## Adapters — adding a source format

An adapter turns one source's payload into Ossie documents and does nothing
else. It never writes to the database; validation and persistence happen once,
centrally.

```python
class MyAdapter:
    def extract(self, config: dict) -> list[str]:
        """Return Ossie documents as text."""
```

Register it in `src/semantic/adapters/__init__.py`. That is the whole contract —
which is the point: a new format is one function, not a new write path.

Return the document text **as produced**, never re-serialized through a YAML
dumper. Export hands that exact text back out, so a round-trip through
parse-and-dump would silently reorder keys and strip comments.

Four adapters ship today: `native` (the source already publishes Ossie),
`keboola_metastore` (composes a document from a Keboola project's metastore
objects), `snowflake_semantic` (composes one document per Snowflake semantic
view), and `databricks_metric_views` (composes one document per Unity Catalog
metric view).

Every `connection`-kind adapter must also be named by its `source_type` in
`SEMANTIC_ADAPTER_BY_SOURCE_TYPE` (`src/semantic/coverage.py`) in the same
change. That map is what the cross-domain coverage report reads, and a source
type absent from it reports `not_applicable` — "no adapter exists for this" —
which is a lie the moment one does.

An adapter name that nothing is registered under is refused at registration
(`400`, naming the adapters that do exist) rather than at the first sync.

### `keboola_metastore`

A `connection`-kind source whose config carries only scope — never
credentials. Either `connection_id` (a registered Keboola connection; its
master (owner) Storage token is read from that connection's own vault slot)
or `legacy_credentials: true` (the `KEBOOLA_STACK_URL` /
`KEBOOLA_STORAGE_TOKEN` pair). Registering one by hand is rarely needed: the
scheduled sweep registers one per master-token connection automatically (see
*Scheduled refresh* above).

Before each fetch the adapter runs the two preflight checks this sync has
always run — the token must be a master token (the Metastore rejects anything
else with an opaque error) and it must open the project its connection is
bound to. Either failing records the error on the source row and imports
nothing, rather than filing another project's semantic layer under this
connection's provenance.

### `snowflake_semantic`

Register it as a `connection`-kind source; the config carries only scope, never
credentials — those resolve from the instance's Snowflake connection like every
other Snowflake code path:

```bash
agnes admin semantic source add --kind connection --name "Snowflake semantic views" \
    --adapter snowflake_semantic
```

Optional scope keys in `config`: `database` (defaults to the connection's),
`schema` (defaults to every schema in the database), `like` (a SHOW pattern).

It reads `SHOW SEMANTIC VIEWS` and `DESCRIBE SEMANTIC VIEW` through the DuckDB
Snowflake extension's `snowflake_query()` pass-through — those are DDL commands,
not table scans, so they cannot go through the ATTACHed catalog the rest of the
connector uses. Credential egress is gated by the same host allowlist and SECRET
as every other Snowflake path.

Logical tables become datasets, dimensions and facts become fields, metrics
become metrics, and relationships map `FOREIGN_KEY` → `REF_KEY`. The model name
is the fully qualified `DB.SCHEMA.VIEW`, because the importer keys storage on
the model name and two same-named views in different schemas would otherwise
overwrite each other.

`DESCRIBE SEMANTIC VIEW` also emits an `EXTENSION` row that the SQL reference
does not document (name `CA`, Cortex Analyst). It is the only place the declared
time dimensions and every relationship's `join_type` appear at all, so it is
parsed for those two and carried whole in the model's `custom_extensions`. A
malformed payload costs its annotations and nothing else.

**Every expression is tagged `SNOWFLAKE`, which makes it readable but not
runnable here.** `src/semantic/dialect.py` prefers `DUCKDB` then `ANSI_SQL` and
reports anything else as unusable *with its reason* — so an imported Snowflake
metric will not be spliced into a local DuckDB query. That is the intended
outcome: importing gives you the catalog, the metric SQL, the lineage and
Snowflake's own AI instructions; it does not give you local execution. Facts and
metrics marked `PRIVATE` upstream carry that label in `custom_extensions` rather
than being presented as ordinary public surface.

### `databricks_metric_views`

Composes one document per Unity Catalog **metric view** — Databricks's
semantic layer — discovered via `information_schema.tables`
(`table_type='METRIC_VIEW'`) per configured catalog, with each view's YAML
definition read through `SHOW CREATE TABLE`. As with `snowflake_semantic`,
the config carries only scope — never credentials, which resolve from the
instance's Databricks connection (`resolve_databricks_settings()`) like every
other Databricks code path, so a semantic source row never becomes a second
place a workspace token is stored. Register it the same way as the Snowflake
adapter:

```bash
agnes admin semantic source add --kind connection --name "Databricks semantics" \
    --adapter databricks_metric_views
```

The scheduled refresh (`POST /api/admin/run-semantic-sources-refresh`,
see [`DATA_SOURCES.md`](DATA_SOURCES.md#semantic-layer-sync-unity-catalog-metric-views))
registers this source automatically under a fixed id (`databricks_default`)
if a workspace is configured and it does not already exist — manual
registration through the CLI, or the connect wizard's "Also sync semantic
views" opt-in, is only needed to pin a specific `connection_id` or a second,
differently-scoped source (e.g. a narrower `config.catalogs`).

Optional scope keys in `config`: `catalogs` (a list; or the single `catalog`,
defaulting to the connection's own catalog / `semantic_layer_catalogs`) and
`connection_id`, which pins WHICH Databricks connection this source reads —
the connect wizard writes the row it just saved. A pinned connection that no
longer exists, or that turns out to belong to another connector, fails the
sync by name rather than falling back to the default — which would import a
different workspace's metric views under this source's provenance.

`dimensions[]` become dataset fields, `measures[]` become metrics, and there
is no analogue to Keboola's relationships, constraints or glossary because
the YAML declares none. **Every measure expression is tagged `DATABRICKS`** —
the full runnable `SELECT MEASURE(...) FROM <metric view>`, not a bare
fragment, since a metric view is a warehouse-side object with nothing in
`table_registry` to bind against; the measure's own `expr` fragment is not
runnable anywhere on its own and rides along instead in `custom_extensions`.
`MEASURE()` is a Databricks-only aggregate, so the same
`src/semantic/dialect.py` rule as Snowflake's applies: readable here, refused
for local DuckDB execution, runnable through `agnes query --remote`. The
model name is the fully qualified `catalog.schema.view`, for the same
name-collision reason as Snowflake's `DB.SCHEMA.VIEW`.

## Ownership: imported models are read-only

A model that came from a registered source cannot be edited through the API —
`PUT` returns `409 source_owned` and names where to change it instead. Edit it
at the source: a commit in the git repo, or upstream for a connection-backed
source. A model created directly through the admin API stays editable.

This is not bureaucracy. A scheduled sync prunes what upstream no longer has, so
an edit made downstream would be reverted on the next run — silently, and at an
unpredictable time.

## Provenance and pruning

Every projected row is stamped with the model's `source` and `source_ref`, and a
sync prunes only within its own `(source, source_ref)`. Two sources can never
delete each other's rows.

One documented exception: column descriptions prune at `(table_id, source)`
granularity, so two writers sharing a `source` value *and* describing the same
physical table prune each other's column descriptions. Metrics and glossary terms
are unaffected.

This is not hypothetical, and it does not need an exotic setup: `source` is the
source *kind*, not the source. Two registered semantic sources of the same kind
(both `ossie_git`) or two Keboola connections (both `keboola_metastore`) already
share one `source` value, so whichever syncs last wins for any table both
describe. `column_metadata.source_ref` exists on Postgres and the projector
records it; the prune does not read it yet, because the frozen DuckDB app-state
schema has no such column and cannot gain one — closing the gap means accepting
a per-backend difference in what a sync deletes, which is a decision, not a
detail.

## Export

```bash
agnes semantic-model export retail > retail.yaml
```

Or over HTTP, gated on a grant for a Data Package the model is linked to (or a
direct grant on the model):

```
GET /api/semantic-models/retail.yaml
```

The bytes you get back are the bytes that were stored.

## Query validation

Before running a SQL statement, check it against the semantic layer: does it
trip a constraint, does it hit a metric declared only in another SQL dialect,
does it reference what you expect it to. `src/semantic_validation.py` is a
pure function over the document(s) — best-effort, case-insensitive text
matching against declared names, not SQL parsing — wrapped identically on
three surfaces:

```bash
POST /api/semantic-models/validate-query
agnes semantic-model validate-query "<SQL>" [--expect '[{"type":"metric","name":"mrr"}]'] [--target-engine duckdb] [--json]
validate_semantic_query   # MCP foundation tool
```

RBAC matches export/search — a Data Package grant or a direct grant on the
model, not admin-only — and every accessible `status='valid'` model is
consulted (a query may span more than one). An `error`-severity constraint
violation sets `valid: false`; a rule this module cannot check statically
(anything besides a `required_filter` presence check) degrades to
`post_execution_checks` rather than a guessed violation; a used metric whose
only declared expressions target another engine sets
`locally_executable: false` with a `mixed_dialect_warning`. With zero
accessible valid models the response is `{"available": false, "error":
"no_semantic_model", ...}` instead of the pure function's own empty-input
all-clear — do not read a missing `available` (or `available: true`) as
"no semantic layer configured".

Validation also happens **without being asked**. `POST /api/query` runs the
same check over the caller's readable models after a statement succeeds and
attaches `semantic_validation` to the response — but only when there is
something to say: an `error`-severity constraint violation, or a used metric
with no expression for the engine that actually ran the statement (DuckDB,
BigQuery, or a Databricks warehouse). A clean query, a caller who can read no
model, and an instance with no semantic layer all return `null`, so the field
appearing means something. Enforcement is **soft** by design — the rows are
untouched, the status stays `200`, and a failure of the check itself is
swallowed (logged, field omitted) rather than costing the caller their
result. `agnes query` prints each warning to stderr as `[semantic] …`, and the
MCP `query` tools (both transports) pass the field through — shortening, then
dropping, the advisory rather than letting it push a deliverable result over
the tool output cap, which raises rather than truncating. An advisory must
never fail a query.

Read it as a prompt to check, not as proof. Object detection is a
best-effort text match on declared names, not SQL parsing, so a column or
alias that happens to share a metric's name matches too; the payload says so
in `detection` and every warning line repeats it. Only the metrics that are
actually unexecutable are named (`not_executable_metrics`), never every
metric the statement mentioned. `post_execution_checks` — rules that cannot
be checked before running — ride along as **information**: this caller has
the rows but deliberately does not evaluate a business rule over them, and
they never raise the advisory on their own.

Constraints have no slot in core Ossie, so they ride `custom_extensions`
under the Agnes vendor name, and the key naming the rule kind is
`constraint_type` — the same key the Keboola adapter composes, the projector
copies into `metric_definitions.validation.rules[]`, and
`agnes catalog --metrics --show` renders:

```yaml
custom_extensions:
  - vendor_name: AGNES
    data: >-
      {"constraints": [{"name": "eu_only", "constraint_type": "required_filter",
       "rule": "region = 'EU'", "severity": "error", "metrics": ["revenue"]}]}
```

Not to be confused with `agnes semantic-model validate <file>` below,
which schema-checks a *document*, offline, before it is ever stored.

## Coverage: what each source still lacks

`GET /api/admin/semantic-model/coverage` (admin, `/admin/semantic-layer` →
**Coverage**) asks one question of **every** connected data source, not just
the ones with a semantic model: is there a semantic model, are there metrics,
glossary terms, a skill, a specialized agent, a knowledge base? Each answer is
`ok` / `partial` / `missing` / `not_applicable`.

Three things about it are deliberate:

- **`not_applicable` is not a gap.** Only four adapters exist (see *Adapters*
  above), so a BigQuery connection has no semantic-layer adapter at all.
  Reporting that as `missing`, next to a link into a create flow that does not
  exist for it, would invent work nobody can do.
- **Detail depth follows the connector, not the vendor.** Every cell has one
  `raw` slot in one place in the UI. Keboola's is fat because
  `connectors/keboola/semantic_layer.py::compute_semantic_coverage` computes a
  lot (token-identity mismatches, metrics blocked by their own definition,
  unregistered dataset tables); Snowflake's is thin because its adapter does
  not compute that yet. That report is a *provider* inside this one — it is
  neither replaced nor duplicated, and its own endpoint
  (`/api/admin/semantic-layer/coverage`) is unchanged.
- **Skills, agents and knowledge domains have to be told.** They live in their
  own registries with no notion of a data source, so the link is an explicit
  admin act — `resource_source_tags`, maintained via `… coverage tag/untag`.
  That table is **Postgres-only** (see `docs/migrations.md` → "Adding a PG-only
  feature"), so on the frozen DuckDB app-state backend these routes answer
  `501 requires_postgres_backend`.

## Health: is the layer trustworthy right now

Coverage answers "what exists"; `GET /api/admin/semantic-layer/health`
(admin, `/admin/semantic-layer` → **Health**) answers "is what exists broken,
stale, or internally inconsistent" — a different question, in one response:

- **`sources`** — every `semantic_sources` row's last sync outcome, verbatim,
  plus `owned_model_count` and `scan_scope` (see above): a source can sync
  `ok` and import nothing, and the status alone cannot say so — nor can the
  count say whether the upstream was empty or merely invisible. A dedicated
  **"Sources that synced but imported nothing"** section lists exactly those,
  naming what each one scanned, so a silently-empty source is an actionable
  finding rather than a number the reader has to notice — reported without
  error styling, because nothing failed.
- **`orphaned_models`** — non-`manual` models whose `source_ref` names no live
  source. Deleting a source (`DELETE /api/admin/semantic-sources/{id}`) does
  not cascade to the models it fed, so a project can vanish and leave its
  models silently pointing at nothing.
- **`orphaned_table_bindings`** — the same shape of gap, one hop over: a
  `metric_definitions` row bound by name (`table_name`/`tables[]`) or a
  `column_metadata` row bound by id (`table_id`) to a `table_registry` row
  that no longer exists — unregistering (or renaming) a table has no cascade
  either. Detection only (`src/semantic/orphans.py`); nothing here deletes
  the dangling metric/column rows, and a table delete is never blocked on it.
- **`invalid_models`** — documents with `status='invalid'`, and why.
- **Three static, document-only quality checks**, none of which touch live
  data: `metrics_missing_description` (a formula with no business decision
  written down — is "Revenue" gross or net?), `duplicate_metric_names` (the
  same name defined twice with a *different* formula — the "four sources of
  truth" anti-pattern), and `metrics_missing_relationships` (a metric whose
  SQL table-qualifies columns from two datasets with no declared relationship
  between them, read straight off the Ossie document — a substring heuristic
  over the expression text, advisory rather than authoritative, since exact
  parsing would need a grammar per dialect).
- **`coverage_summary`** — the missing/partial cell counts from Coverage,
  rolled up into two numbers.
- **`mutes`** — every currently active silence, so a finding already signed
  for by an admin is never reported as news a second time.

`semantic_health_mutes` is **Postgres-only**, and it is resolved *first* —
before any of the other, backend-agnostic checks run — so a DuckDB-backed
instance answers one clean `501 requires_postgres_backend` for the whole
report rather than a partial one that silently drops the one field muting
exists to keep visible.

## Muting: turning a check off is a signature

An admin who has read a finding, decided it is expected and put the work in a
plan should be able to stop it shouting. What they must not be able to do is
make it vanish without a trace — a check that simply stops appearing leaves the
next reader unable to tell "fixed" from "hidden". So Agnes has no "dismiss"
button; it has a mute that carries **who**, **when** and **why**, and hands all
three back on every read (`/admin/semantic-layer` → **Mute**).

A mute names a scope, in one of three widths:

| Scope | Silences |
|---|---|
| `domain:<domain>` | that domain across every source |
| `source:<source_id>` | that source, every domain |
| `source:<source_id>:domain:<domain>` | one cell of the coverage grid |

`__local__` is a valid source id — it is the report's synthetic bucket for
registered tables with no connection. A scope that parses to neither form is
refused (`400 invalid_scope`) rather than stored: a row that looks like a mute
but matches nothing is worse than either outcome. So is a scope naming a source
that does not exist (`404`), and re-muting something already muted (`409`, with
the existing mute's id — read the reason somebody already gave before adding a
second one).

`expires_at` is the honest middle option: say when you expect to have fixed it
and let the check come back on its own. Omit it and the mute stands until
somebody unmutes it. Expired mutes drop out of the default list but stay
readable with `?include_expired=true` / `--include-expired` — the silence ends
at the expiry, the record of who chose it does not.

Muting is admin-only on every surface (UI, `agnes admin semantic
mute/unmute/mutes`, MCP `mute_semantic_check` / `unmute_semantic_check` /
`semantic_mutes_list`, REST), and both mutations are audit-logged.
`semantic_health_mutes` is **Postgres-only** (see `docs/migrations.md` →
"Adding a PG-only feature") — on the frozen DuckDB app-state backend every one
of these surfaces answers `501 requires_postgres_backend` rather than
pretending the check was silenced.

## Feedback: "that answer looked wrong"

Coverage says what is undocumented and health says what is broken. Neither can
see the third failure: the layer looked complete and the **answer** was still
wrong — a number nothing supports, a metric that means something other than its
name, a concept nobody defined. Only whoever read the answer knows that, so the
report channel is open to **any signed-in caller** (`POST
/api/semantic-feedback`), while the queue behind it is admin-only
(`/admin/semantic-layer` → **Feedback**).

Four surfaces file the same report, on purpose:

- **UI** — the Feedback tab lists the queue and resolves with a note.
- **Chat / agent** — the MCP tool `flag_semantic_issue`, whose contract is to
  *offer* filing when it cannot support its own answer and file only once the
  user agrees: reporting silently on someone's behalf and waiting for the user
  to remember are both wrong. (The matching workspace-prompt sentence ships
  with the agent-grounding rules.)
- **CLI** — `agnes semantic-model feedback submit` for anyone signed in;
  `agnes admin semantic feedback list/resolve` for the queue.
- **REST** — the endpoints above; the only surface that also accepts
  `model_content_hash`, which pins the report to the document version that
  produced the answer.

Resolving is a guarded transition: the second admin to close the same report
gets `409`, so the record of who fixed it and how is never overwritten.
`semantic_feedback` is **Postgres-only** (see `docs/migrations.md` → "Adding a
PG-only feature") — on the frozen DuckDB app-state backend every one of these
surfaces answers `501 requires_postgres_backend` rather than pretending the
report was filed.

## Commands

Two groups, split by who may run them — not by which endpoint family they
happen to call.

**`agnes semantic-model` — anyone signed in.** RBAC is per model (a Data
Package grant or a direct model grant), so these show exactly what the caller
can already read.

```bash
agnes semantic-model search <term> [--limit N] [--json]   # find models you can read
agnes semantic-model show <slug> [--json]                 # provenance, status, content hash
agnes semantic-model export <slug> [-o FILE]              # the document, byte for byte

agnes semantic-model validate <file>          # offline: no server, no token
agnes semantic-model validate-query "<SQL>"   # see "Query validation" above
agnes semantic-model apply <file> [--description ...] [--expect-hash <hash>]

agnes semantic-model context dataset|metric|relationship [--id ...] [--model ...]
agnes semantic-model schema dataset metric relationship

agnes semantic-model feedback submit "<question>" [--sql ...] [--metric ...] [--comment ...]
```

`validate` deliberately needs neither a server nor a token — someone fixing a
document should not need an instance to check their work. It is **not**
`validate-query`: `validate` checks whether a *document file* is well-formed;
`validate-query` checks whether a *SQL statement* obeys the models you can
read, and needs a server.

`apply` is the write path for everyone: an admin's document goes live, anyone
else's is queued for admin moderation (the command labels which happened).

**`agnes admin semantic` — admin only.** Everything that changes what the
layer is, or reports on how healthy it is.

```bash
agnes admin semantic list [<term>] [--limit N] [--json]   # every model, any status
agnes admin semantic show <id|slug> [--json]
agnes admin semantic import <file>
agnes admin semantic delete <id|slug> [--yes]
agnes admin semantic detach|reattach <id|slug> [--yes]
agnes admin semantic link-package|unlink-package <slug> <package-id>

agnes admin semantic source add --kind git|upload|connection --name "..." [...]
agnes admin semantic source list [--enabled-only] [--json]
agnes admin semantic source sync <id>
agnes admin semantic source rm <id> [--yes]     # unregisters the source; keeps its models

agnes admin semantic coverage [--source <id>] [--json]   # PER SOURCE × domain grid
agnes admin semantic coverage tag <type> <resource-id> <source-id>
agnes admin semantic coverage untag <tag-id>
agnes admin semantic coverage tables [--limit N] [--json]  # PER TABLE: no model at all
agnes admin semantic keboola-import [--json] [--limit N]   # PER KEBOOLA PROJECT: what would import

agnes admin semantic health [--json]
agnes admin semantic mute <scope> [--reason "..."] [--expires <ISO8601>]
agnes admin semantic unmute <mute-id>
agnes admin semantic mutes [--include-expired] [--json]

agnes admin semantic feedback list [--status open] [--json]
agnes admin semantic feedback resolve <id> [--note "..."]
```

The three reports are three different questions, which is why
`keboola-import` no longer calls itself coverage:

| Command | Question | Endpoint |
|---|---|---|
| `coverage` | Per SOURCE: which of six domains is still empty? | `/api/admin/semantic-model/coverage` |
| `coverage tables` | Per TABLE: which registered tables no valid model describes | `/api/admin/semantic-coverage` |
| `keboola-import` | Per KEBOOLA PROJECT: how much of its published layer *would* import | `/api/admin/semantic-layer/coverage` |

Business terms projected out of a document are read through the glossary
surface: `agnes glossary search <term>` / `agnes glossary show <id>`.

### Renamed in this release

Five groups that all read as "the semantic layer" became the two above. Every
old spelling still runs for one release as a hidden alias that prints its new
path on stderr, then delegates:

| Old | New |
|---|---|
| `agnes admin semantic-model <cmd>` | `agnes admin semantic <cmd>` |
| `agnes admin semantic-model export\|validate` | `agnes semantic-model export\|validate` |
| `agnes admin semantic-source <cmd>` | `agnes admin semantic source <cmd>` |
| `agnes admin semantic-layer coverage` | `agnes admin semantic keboola-import` |
| `agnes semantic-model coverage\|health\|mute\|mutes\|unmute` | `agnes admin semantic <same>` |
| `agnes semantic-model feedback list\|resolve` | `agnes admin semantic feedback list\|resolve` |

`agnes admin data-semantics` was removed outright with no alias — it scaffolded
a pre-Ossie workspace pack that nothing reads.
