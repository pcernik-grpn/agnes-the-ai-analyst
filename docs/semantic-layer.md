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
agnes admin semantic-source add --kind git \
  --name "Finance models" \
  --repo-url https://example.com/semantics.git \
  --ref main --glob 'semantic/**/*.yaml'

agnes admin semantic-source sync <source-id>
```

A sync that cannot fetch **fails loudly and imports nothing**. This matters more
than it sounds: an empty document list legitimately means "upstream deleted
everything", which prunes. A failed clone must never be able to present itself
as an empty source, so the error is recorded on the source row and re-raised.

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

Three adapters ship today: `native` (the source already publishes Ossie),
`keboola_metastore` (composes a document from a Keboola project's metastore
objects), and `snowflake_semantic` (composes one document per Snowflake
semantic view).

An adapter name that nothing is registered under is refused at registration
(`400`, naming the adapters that do exist) rather than at the first sync.

### `snowflake_semantic`

Register it as a `connection`-kind source; the config carries only scope, never
credentials — those resolve from the instance's Snowflake connection like every
other Snowflake code path:

```bash
agnes admin semantic-source add --kind connection --name "Snowflake semantic views" \
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

One documented exception: `column_metadata` has no `source_ref` column, so column
descriptions prune at `(table_id, source)` granularity. Two sources sharing a
`source` value *and* describing the same physical table can prune each other's
column descriptions. Metrics and glossary terms are unaffected.

## Export

```bash
agnes admin semantic-model export retail > retail.yaml
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

Not to be confused with `agnes admin semantic-model validate <file>` below,
which schema-checks a *document*, offline, before it is ever stored.

## Coverage: what each source still lacks

`GET /api/admin/semantic-model/coverage` (admin, `/admin/semantic-layer` →
**Coverage**) asks one question of **every** connected data source, not just
the ones with a semantic model: is there a semantic model, are there metrics,
glossary terms, a skill, a specialized agent, a knowledge base? Each answer is
`ok` / `partial` / `missing` / `not_applicable`.

Three things about it are deliberate:

- **`not_applicable` is not a gap.** Only three adapters exist (see *Adapters*
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
- **CLI** — `agnes semantic-model feedback submit/list/resolve`.
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

```bash
agnes admin semantic-model list [--json] [--limit N]
agnes admin semantic-model show <slug>
agnes admin semantic-model import <file>
agnes admin semantic-model export <slug>
agnes admin semantic-model validate <file>   # offline: no server, no token

agnes admin semantic-source add ... | list | sync <id>

agnes semantic-model validate-query "<SQL>"  # see "Query validation" above

agnes semantic-model coverage [--source <id>] [--json]   # see "Coverage" above
agnes semantic-model coverage tag <type> <resource-id> <source-id>
agnes semantic-model coverage untag <tag-id>

agnes semantic-model feedback submit "<question>" [--sql ...] [--metric ...] [--comment ...]
agnes semantic-model feedback list [--status open] [--json]   # admin
agnes semantic-model feedback resolve <id> [--note "..."]     # admin
```

`validate` deliberately needs neither a server nor a token — someone fixing a
document should not need an instance to check their work.
