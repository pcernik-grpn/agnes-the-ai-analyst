# Extraction scope curation — index broadly, extract facts where it earns it

**Status:** design only, nothing implemented. Written against the built-in
SharePoint pipeline as of this branch (`connectors/sharepoint/crawler.py`,
`connectors/sharepoint/facts_extraction.py`, the "Manage scopes" wizard in
`app/web/templates/admin_data_sources.html`).
**Scope:** which documents the LLM fact-extraction stage reads. Not the
crawl, not conversion, not the ontology, not the verbatim gate.

Related: [fact-extraction cost levers](2026-09-02-fact-extraction-cost-levers.md)
(lever 1 — this document is that paragraph turned into a design),
[extraction observability](2026-08-31-extraction-observability-ui-design.md)
§5 (P7 "estimate first", P10 "every number carries its source"),
[fact-graph over Collections](2026-08-27-fact-graph-over-collections-design.md)
§13.2 ("state, not settings"), [unstructured corpus intake](2026-08-24-unstructured-corpus-intake-design.md)
§2 (the three-tier vocabulary this document adopts), and the
`ontology-building` skill (`.claude/skills/ontology-building/SKILL.md`).

No customer-specific content: the measurements below come from "a live
deployment"; every folder name in an example is invented.

---

## 1. Why this is the exponent, not a constant

Fact extraction is the only pipeline stage that spends model tokens per
document. Measured on a live deployment (cost-levers spec §1): **$0.054 per
document** on the fast model tier at list price, **~12 documents per minute**
in the single extraction lane. The shipped estimate in
`config/instance.yaml.example` says $0.011; the retry fix and the other
levers will pull the real figure down, but not by an order of magnitude.

Today `extraction.facts.enabled` is per instance. When it is on, every
indexed document in every confirmed scope of every SharePoint connection is
extracted. Scaled to an office SharePoint:

| corpus | at $0.011/doc (shipped note) | at $0.054/doc (measured) | wall clock at 12/min |
|---:|---:|---:|---:|
| 400,000 documents | $4,400 | $21,600 | ~23 days |
| 20,000 documents | $220 | $1,080 | ~28 hours |
| 2,000 documents | $22 | $108 | ~3 hours |

Most of a 400k-file estate is material nobody will ask a graph question
about: `Templates/`, `Archive/2019/`, `.url` shortcuts, `*.inspect.ndjson`
artefacts, weekly status decks that restate the same facts, drafts beside
their finals. Every other lever — retry repair, cache breakpoints, output
compaction, batch API — multiplies the bill by a constant. **Choosing which
20,000 documents to read divides it by twenty.** And it is the only lever
that also cuts wall clock and graph noise (a superseded draft's facts
conflicting with the final's) in the same stroke.

---

## 2. Ground truth — what exists today

Everything below is code on this branch. The design changes only what §6
and §10 name.

**A scope is a folder, library or site on a connection, mapped to exactly
one collection.** Rows live in `source_connections.config.scopes` — a JSON
list, no table (`app/api/admin_sharepoint.py:111-133`). Each row:
`{source_scope_id, display_path, anonymize, collection_id, access_mode,
drive_id, include_excluded_subtrees, audience_classes, excluded_subtrees}`.
`ConfirmScopeBody` (`:229-276`) is the write shape; `_scope_out` (`:581-657`)
the read shape. Two field contracts coexist and matter here:

- **"always persisted on confirm"** — `anonymize`, `access_mode`,
  `drive_id`, `include_excluded_subtrees`: omitting the field resets it. The
  wizard round-trips an existing scope's values through
  `spExistingScopeFields` (`admin_data_sources.html:7113-7121`) precisely
  because forgetting one silently blanked `drive_id` on every "Share &
  finish" (live report, 2026-09-01).
- **"omitted means unchanged"** — `group_ids`, `audience_classes`: `None`
  leaves the stored value alone, `[]` clears it.

**Exclusions are ACL-driven, not curation.** `excluded_subtrees` is written
by the `sharepoint-subtree-sweep` job for broken-inheritance subtrees, and
`include_excluded_subtrees` is the admin's `should_not`-mode override
(`crawler.py:1632-1720`, `_ExclusionIndex`). Folder-kind entries exclude
at-or-under by component-safe prefix (`_under_prefix`, `:1745`); file-kind
entries match one item exactly. The crawl never downloads an excluded item.
This machinery answers "may this audience see it", never "is it worth
reading" — the two must stay separate, and this design reuses only the
prefix-matching primitive.

**Zones split a scope's documents across collections.** A permission zone
(`acl_sync.zone_rows`, `{zone_item_id, parent_scope_id, rel_path,
collection_id, status}`) routes a subtree of a scope into its own
collection — `_route_collection` (`crawler.py:1802-1818`), deepest wins.

**The facts pass is all-or-nothing per instance.** `maybe_run_after_crawl`
(`facts_extraction.py:1530-1546`) checks `extraction.facts.enabled` and
`facts.enabled`, then `run_facts_extraction` walks
`collection_ids_for(connection)` (`:765-781`) — *only* each scope's own
`collection_id`. Its `_plan()` (`:1314-1390`) makes every cheap decision on
the main thread before a worker slot is taken: not source-anchored →
skip; tabular by extension → `skipped-tabular`; not `indexed` → skip;
unchanged under `(sha256, model, prompt fingerprint)` → skip; no text →
skip. **Two consequences the design inherits:**

1. Documents routed into a *zone* collection are never fact-extracted —
   `collection_ids_for` does not know zones exist. Pre-existing gap; §6.2
   closes it deliberately.
2. `_TABULAR_EXTENSIONS` (`:98`) is the only curation rule, hard-coded.

**Per-item metadata is already cheap and deterministic.** The crawl stores
`corpus_files.{path, filename, file_type, size_bytes, sha256,
processing_status}` (drive-relative path with the original extension —
`crawler.py:1872-1873`) and `corpus_file_sources.{source_doc_id,
source_stable_id, source_sha256, source_url}`. The text it sends the model
is exactly the stored chunks, joined (`_document_text`, `:803-816`), so the
*input* side of every document's cost is measurable from storage before a
single token is spent.

**For an anonymize-marked scope the stored path is pseudonymized per
segment** (`_anonymize_identity`, `crawler.py:2437-2462`): shape and
extension survive, folder *names* may not. §4.3 takes this into account.

**Cost is computed and persisted, per run, per stage.** `_Report.render`
prices the pass through `src.llm_pricing.cost_usd` into
`facts_usage.estimated_cost_usd` (`facts_extraction.py:859-873`); the crawl
promotes it to `extraction_runs.usage.facts` (`crawler.py:3947-3960`), and
the run line renders it when present (`admin_data_sources.html:7614-7625`).
What does not exist: any number *before* the spend, any attribution per
scope, and any spend for an interrupted pass (`stopped_usage` carries `ner`
and `ocr` only, `crawler.py:3915-3919` — cost-levers §7.1).

**Two operator controls already narrow a run without curating:** the
`corpus-extraction` payload's `scopes` list (`crawler.py:4039-4041`,
crawl-side only — the facts pass does not receive it) and
`run_facts_extraction(doc_ids=...)` (`facts_extraction.py:1203`, no
non-test caller).

---

## 3. Decisions

Each is argued in the section named; this table is the contract.

| # | Decision | Recommended | Where |
|---|---|---|---|
| D1 | Unit of curation | **Scope tier** + per-scope path prefix rules + instance-level extension denylist. No per-document heuristics. | §4 |
| D2 | Tier vocabulary | Three names (`catalog`, `search`, `graph`); **two implemented** (`search`, `graph`). `catalog` is reserved, refused with a typed 422 until the metadata-only table exists. | §5 |
| D3 | Storage | `tier` and `graph_paths` on the existing scope row in `source_connections.config.scopes` — JSON, **no migration**. | §6.1 |
| D4 | Default for rows without `tier` | **`search`** (facts off). A deployment that already had `extraction.facts.enabled: true` stops extracting after upgrade until scopes are ticked; the card says so loudly. | §6.4 |
| D5 | Zone collections | Inherit the parent scope's tier and rules. | §6.2 |
| D6 | Un-tick | **Keep** extracted facts. Pruning is a separate named action (`Remove facts`), audited, stamping `graph_pruned_at` so a later re-tick re-extracts. | §7.3 |
| D7 | Estimate basis | Input from stored chunk characters (measured); calls-per-document and output ratio from the last completed pass on this instance, else a **labelled** reference basis. Always shown with its basis; never enforced. | §8 |
| D8 | What curation may never do | Skip a document inside a graph-tier scope on any content or "looks boring" heuristic; touch the ontology builder's sample; apply to explicit `doc_ids`. | §9 |

---

## 4. The unit of curation

### 4.1 The cases, and what each needs

| Case | Smallest control that covers it |
|---|---|
| "Index this whole site for search, but only build the graph over `Engagements/Active/`" | The site is one scope, `tier: search`; **either** make `Engagements/Active` its own scope with `tier: graph` (works today — folder scopes at any depth), **or** keep one scope with `tier: graph` and `graph_paths.only_under: ["Engagements/Active"]`. |
| "Never extract facts from `Templates/`" | `graph_paths.never_under: ["Templates"]` on the scope — or, instance-wide, the template *formats* (`.dotx`, `.potx`, …) in the extension denylist, which catches templates wherever they live. |
| "`.url` shortcuts, `*.inspect.ndjson`, JSON/YAML dumps" | Instance-level `extraction.facts.skip_extensions`. Deterministic, by suffix, one list. |
| "Skip anything named `*status*` older than the newest three" | **Not covered in v1**, deliberately (§4.4). The sanctioned answer is `never_under: ["Weekly status"]` — status decks live in a folder in every estate this was observed in — and letting replace-mode ingest and the conflict row deal with the rest. |
| "Archived years" | `never_under: ["Archive"]` or, better, a separate `tier: search` scope for the archive so the card shows the split. |

Three controls. Each is evaluated on metadata the pipeline already stores,
in one place (`_plan()`), in the order cheapest-first (§6.3).

### 4.2 Why the scope, not the folder-within-scope, is the primary unit

A scope is already the unit of every other decision the wizard makes —
collection, anonymization, access mode, audience tiers, group grants. The
card lists scopes; the crawl narrows by scope; the estimate (§8) can be
attributed per scope because a collection is a scope's. Adding a second
axis ("folders within a scope") would give the operator two places to say
the same thing. Path rules exist only for the case a scope *cannot*
express: a graph subset that is not a clean subtree, or a site scope whose
libraries the operator does not want to split.

Nested scopes are not the answer: a folder scope inside a site scope would
be enumerated by both (`_scope_targets`, `crawler.py:1609-1622` builds a
`DriveTarget` per scope, a site fans out to every drive), ingesting each
document twice into two collections. The wizard does not prevent it today;
this design does not fix that, it just does not rely on it.

### 4.3 Path rules — semantics, precisely

`graph_paths: {"only_under": [...], "never_under": [...]}`, on the scope row.

- Paths are **drive-relative**, exactly what `corpus_files.path` holds:
  `"Engagements/Active"`, never a leading slash, never a drive or site name.
  For a site scope (several drives) a rule applies to every drive of the
  site — if two libraries share a folder name and only one is wanted, make
  the library its own scope.
- Matching is `_under_prefix`'s component-safe rule (`path == prefix or
  path.startswith(prefix + "/")`) — the same rule `excluded_subtrees`
  folder entries use, so an operator who has read one understands the
  other — **but casefolded on both sides**. SharePoint paths are
  case-insensitive; a rule that misses on case is a silent over-spend.
  (`excluded_subtrees` stays case-sensitive; it is fail-closed by
  construction and written by the sweep, never typed.)
- `only_under` non-empty means "graph tier applies only under these";
  `never_under` is subtracted afterwards. Both empty = the whole scope.
- **Refused on an anonymize-marked scope** (`422 graph_paths_need_real_paths`).
  The stored path there is pseudonymized per segment; a rule written in
  real folder names would match by accident or not at all. The honest
  option on such a scope is the boolean tier plus scope-splitting. (The
  alternative — evaluate rules at crawl time where the real path is known
  and persist a per-document `graph_eligible` flag — sees every path but
  turns every rule edit into a re-crawl. Named as the upgrade path in §13;
  not built.)
- Validation: at most 64 entries per list, each ≤ 512 chars, no empty
  component, no `..`, no leading `/`. Typed `422 invalid_graph_path` naming
  the offending entry.

### 4.4 Deliberately not a control in v1

- **Recency / series rules** ("newest N of `*status*`"). Needs a notion of a
  series (same folder + name pattern + date) and the source's
  `lastModifiedDateTime`, which `corpus_files` does not store. Every
  heuristic here removes exactly the half-filled, superseded, odd
  documents the ontology skill wants in a *sample* (§9), and gets one
  wrong in every estate. If it is ever built it is a per-scope rule with
  its own estimate line, never a default.
- **A triage model call** ("is this document worth extracting?", ~1k
  tokens in, ~$0.001). Fifty times cheaper than an extraction, so it is a
  real lever for the long tail — but it is *automatic* curation, and it
  would be measured against an operator-curated baseline that does not
  exist yet. Revisit after D1–D8 have run on a real estate.
- **Size caps for the graph tier.** `DEFAULT_MAX_DOC_CHARS = 120_000` already
  truncates and counts; a long document is more likely to carry facts, not
  less.

---

## 5. Tiers — three names, two implemented

The intake spec (§2) named three tiers for a file-store corpus: a metadata
map of everything, a curated content tier, and fetch-on-demand. The built-in
pipeline today has **two real tiers and one that is not a tier**:

| Tier | What exists | Cost | Queryable as |
|---|---|---|---|
| **`catalog`** — the document is known to exist: path, size, type, cTag, mtime | *Not a tier.* The crawl keeps a cTag per item in its state file (`crawler.py:431-444`) and a capped skip list per run; neither is a table an agent can query ("what exists under Archive/?"). | Graph API calls only | nothing |
| **`search`** — converted, chunked, indexed | Every non-excluded, convertible file in a confirmed scope (`processing_status = indexed`) | local CPU + storage; no model tokens unless the LLM anonymizer or scan OCR is on | full-text / hybrid search, citations |
| **`graph`** — fact-extracted with verbatim evidence | Every `search` document, when `extraction.facts.enabled` | **model tokens per document** | `fact_search` / `fact_neighbors` / `fact_claims` |

**Decision D2: the scope row carries `tier ∈ {search, graph}`; `catalog` is
reserved.** It is reserved rather than omitted because the third value is
already designed, and a boolean `facts` field would need renaming the day it
lands; it is not implemented because it needs a new table (the intake
spec's T2), a crawl mode that enumerates without downloading, and an agent
surface — none of which is on the cost path this document is about.
`confirm_scope` refuses `tier: catalog` with `422 tier_not_available` naming
this section.

The tiers are **monotone** — `graph ⊃ search ⊃ catalog`. A graph-tier
document is always searchable; there is no "facts but no search"
(the verbatim gate needs the chunks).

**What the operator sees on the card, per scope row** (extends the
`cell["scopes"]` rows `_sharepoint_pipeline_cell` already renders,
`app/web/router.py:9530-9562`):

```
Engagements/Active    → active          search + graph   1,240 docs · 3,981 facts · 2 path rules
Engagements/Archive   → archive         search           8,120 docs
Templates             → templates       search           310 docs · 412 facts remain  [Remove facts]
```

and the pipeline strip's facts cell reads `facts — over 2 of 3 scopes`
instead of a bare count. A scope with `tier: graph` on an instance whose
`extraction.facts.enabled` is off shows `graph (instance switch off)` in the
warn tone — the intent is recorded, nothing will be spent, and the card
says which switch to flip. **Requested ≠ declared** (P4) applies here as it
does to anonymization: `tier: graph` is the request; "N facts · last pass
<date>" is the observation; the two are never collapsed into one word.

---

## 6. Where the switch lives

### 6.1 Schema — two keys on the scope row, no migration

`source_connections.config` is a JSON column on both backends
(`migrations/versions/0026_source_connections_v79.py:22`; the module
docstring at `admin_sharepoint.py:111-113` relies on it). The scope row
gains:

```json
{
  "source_scope_id": "…",
  "display_path": "Engagements/Active",
  "collection_id": "…",
  "anonymize": false,
  "tier": "graph",
  "graph_paths": {"only_under": [], "never_under": ["Weekly status"]},
  "graph_pruned_at": null
}
```

- `tier`: `"search" | "graph"`; **absent reads as `search`** (D4, §6.4).
  Write contract: *always persisted on confirm*, like `anonymize` — and
  therefore **added to `spExistingScopeFields`** in the same change, or the
  anonymize checkbox silently resets every scope to `search` on the next
  toggle (the `drive_id` trap, `admin_data_sources.html:7100-7112`).
- `graph_paths`: *omitted means unchanged*, like `audience_classes`;
  `{"only_under": [], "never_under": []}` clears. Ignored (and refused on
  write, §4.3) when `anonymize` is true.
- `graph_pruned_at`: server-written only, by the prune action (§7.3). Never
  accepted from the body.

Both keys live inside `scopes`, which `SHAREPOINT_SERVER_WRITTEN_CONFIG_KEYS`
already lists (`admin_sharepoint.py:347-353`), so the generic connection
editor's carry-forward and its ratchet test
(`tests/test_sharepoint_config_carry_forward_ratchet.py`) are unaffected.

`ConfirmScopeBody` gains:

```python
tier: Literal["search", "graph"] = "search"
graph_paths: Optional[GraphPathsIn] = None   # {only_under: [str], never_under: [str]}
```

`_scope_out` projects `tier` (never null), `graph_paths` (always an object,
both lists present), `graph_pruned_at`, and — read-only — `graph_effective:
bool` = `tier == "graph" and extraction.facts.enabled and facts.enabled`,
so the card's "instance switch off" state is server-derived, not
client-guessed.

The instance-level extension rule is a companion key beside
`extraction.facts.concurrency`, read through `get_value`, not a registry
`Switch` (the registry has no list kind — `app/switches.py` kinds are
`bool`/`int`/`select`):

```yaml
extraction:
  facts:
    enabled: true
    # Source-file suffixes never sent to the model, whatever the scope tier.
    # Replaces the hard-coded spreadsheet rule; this IS the default list.
    skip_extensions:
      - xlsx, xlsm, xls, csv, tsv          # tabular — deterministic converters own these (unchanged)
      - json, yaml, yml, ndjson, xml       # structured dumps and inspection artefacts
      - dotx, dotm, potx, potm, xltx, xltm # Office TEMPLATE formats
      - url, lnk                           # shortcuts
```

Denylist, not allowlist, on purpose: an allowlist starves the graph of an
unusual-but-valuable type silently (§9); a denylist names what is known to
be worthless for a graph and nothing else. Matched on `corpus_files.path`'s
suffix (the source extension, preserved even under anonymization —
`crawler.py:2455-2461`), lower-cased, leading dot optional. An invalid value
(not a list) logs a warning and **uses the default**, mirroring
`resolve_concurrency`'s `invalid` posture.

### 6.2 Zones inherit

`collection_ids_for(connection)` is replaced by

```python
def graph_scopes_for(connection) -> List[Tuple[dict, List[str]]]:
    """(scope_row, [collection ids]) for every confirmed scope with tier=graph:
    the scope's own collection first, then every ACTIVE zone whose
    parent_scope_id is this scope (acl_sync.active_zone_rows)."""
```

A zone is a subtree of a scope that ACL routing put in its own collection;
its documents were selected by the same operator decision as their
siblings. Extracting them is safe by construction — facts visibility
derives from the evidencing collection's grants (spec §5), and a zone
collection carries exactly the mirrored grants its ACL demands. **This is a
behaviour change on instances that have zones**: documents that were never
extracted before will be, once their parent scope is `graph`. The estimate
(§8) counts them, the report names them (`curation.zone_collections`), and
the implementation PR's changelog bullet says so.

### 6.3 `_plan()` — the decision order

Every decision stays on the main thread, cheapest first, each counted under
exactly one reason. New counters in bold.

```
for scope, collection_ids in graph_scopes_for(connection):        # tier != graph → never visited;
    rules = compile_rules(scope, skip_extensions)                  #   counted once per scope in curation.search_only_scopes
    for collection_id in collection_ids:
        for file_row in files_repo.list_for_corpus(collection_id):
            doc_id = source mapping or continue                    # not source-anchored (unchanged)
            if wanted_doc_ids and doc_id not in wanted_doc_ids: continue
            if wanted_doc_ids is None:                             # explicit doc_ids BYPASS curation (§9)
                if not rules.path_ok(path):        docs_skipped_path += 1; continue         **new**
                if rules.suffix_skipped(path):     docs_skipped_type += 1; state=skipped-type **renamed**
            if processing_status != indexed:       docs_skipped_not_indexed += 1; continue
            if is_up_to_date(entry, …) and not pruned_after(entry, scope): docs_unchanged += 1; continue  **pruned_at rule**
            text …                                  docs_skipped_no_text (unchanged)
            yield _Work(…)
```

- `pruned_after(entry, scope)`: `entry["at"] < scope["graph_pruned_at"]`
  when the stamp exists. The state file is the worker's; the API process
  never edits it. The stamp is how an API-side prune (§7.3) reaches the
  planner without touching a file it cannot see.
- `skipped-type` supersedes `skipped-tabular` in the state file; the reader
  treats the old string as the new one (`is_up_to_date` already returns
  False for any non-`done` status, so nothing else keys on it).
- No state entry is written for a tier or path skip: both can flip with a
  config edit and must re-evaluate every pass at zero cost.
- `run_facts_extraction` gains `only_scope_ids: Optional[Sequence[str]]`,
  plumbed from the `corpus-extraction` payload's existing `scopes` list
  through `maybe_run_after_crawl`. "Run extraction now" for one scope then
  also extracts facts for one scope; today the crawl narrows and the facts
  pass does not.

The report (`_Report.render`) adds `docs_skipped_path`, `docs_skipped_type`
(with `docs_skipped_tabular` kept as an alias for one release — it is the
same number, since tabular is a subset of the type rule) and a `curation`
block, so a pass says what rules it ran under (P10):

```json
"curation": {
  "graph_scopes": 2, "search_only_scopes": 3,
  "zone_collections": 1,
  "path_rules": 3,
  "skip_extensions": ["xlsx", "…"],
  "skip_extensions_source": "default" | "config" | "invalid"
}
```

### 6.4 Rows without `tier` read as `search` — the upgrade consequence

Options:

- **(A) absent = `search`.** After upgrade, an instance with
  `extraction.facts.enabled: true` extracts nothing until an admin ticks
  scopes. The pass still runs, reports `graph_scopes: 0`, and the card's
  facts cell reads `facts — no scope opted in · tick "facts" on a scope`.
- **(B) one-time backfill.** On first read, if the instance switch is on
  and *no* scope row carries `tier`, stamp every row `graph` and log it.
  Preserves behaviour; nobody decides anything.

**Recommend (A).** `extraction.facts.enabled` is a cost switch whose
default is off; the one deployment class that has it on is a pilot that is
paying per document *today* and is the audience for this feature. (B)
writes an all-on default into every existing row forever, which is the
opposite of curation, and it makes the very first thing a curated instance
does after upgrade a full-corpus pass. The implementation PR carries a
`**BREAKING**`-prefixed changelog bullet and the upgrade note names the
click.

---

## 7. The UI

Zero new pages, zero new nav (§13.2). Three existing surfaces change.

### 7.1 Wizard step 2 — the tree row and the saved-scopes panel

```
[x] Engagements/Active     → active   [ ] anonymize  [x] facts   graph: ~1,240 docs · ~$67 one-time
[x] Engagements/Archive    → archive  [ ] anonymize  [ ] facts
[ ] Templates                                        [ ] facts (select first)
```

- A `facts` checkbox beside `anonymize`, same enable rule (disabled until
  the row is selected), same handler shape: `spConfirmScope` sends
  `tier: "graph" | "search"` and round-trips everything else via
  `spExistingScopeFields`. The saved-scopes panel
  (`spRenderSavedScopes`, `:6698-6717`) gets the identical checkbox — it is
  the only place a deep folder scope is visible once discovery is
  forbidden.
- On tick, the row calls the estimate (§8) for that one scope and renders
  the result inline under the row; until it returns, the row says
  `estimating…`, never a number.
- **Help text** (`title` on the label, and repeated once as the step-2
  footer note the anonymize column already has):

  > Extract facts for the knowledge graph from this scope. This is the one
  > pipeline stage that spends model tokens on every document — read the
  > estimate that appears when you tick it. Documents stay searchable
  > either way; only the graph is affected. A document is read again
  > whenever its content, the model or the extraction prompt changes.

  It states *where* the cost comes from and *what* changes; it does not
  quote a per-document price, because the shipped constant is already
  known to be wrong by 5× (cost-levers §1.1) and the estimate is the
  number.
- Path rules are **not** in the tree row. An expandable `paths…` affordance
  on a graph-tier row in the saved-scopes panel opens two textareas, one
  path per line, with the semantics of §4.3 as their placeholder text, and
  saves through the same confirm call with `graph_paths` set. Hidden
  entirely on an anonymize-marked scope, with the one-line reason.

### 7.2 Wizard step 3 — the share preview totals

Each row gains a `graph` badge beside its collection and anonymization
badges. Below the table, one block:

```
Fact extraction   2 scopes · ~2,410 documents · ~$130 one-time at list price, then per changed document
                  basis: last pass 2 Sep (1.9 calls/doc, 0.23 output/input), src/llm_pricing.py
                  excluded by your rules: 8,430 documents (3 search-only scopes, 2 path rules, 41 by type)
```

If `extraction.facts.enabled` is off: the same block in the warn tone,
prefixed `Nothing will be extracted until an admin turns on
extraction.facts.enabled in /admin/server-config.` The wizard never flips an
instance switch; it records intent.

### 7.3 Source card — the scope rows, and what un-ticking does

The per-scope rows from §5. Two verbs, both on the row, neither a toggle:

- `tier` is edited in the wizard ("Manage scopes"), not on the card — the
  card shows state (§13.2).
- **`Remove facts`** appears on a `search`-tier row that still has claims
  from a previous `graph` life (`N facts remain`). Decision D6:

  **Un-ticking keeps the facts.** Reasons: it matches `remove_scope`'s
  precedent (a collection with data is kept and the admin is told where to
  delete it deliberately, `admin_sharepoint.py:1440-1470`); the facts cost
  money to make and nothing to keep; the graph's "answered from N documents
  in M collections" footer already tells a user where an answer came from;
  and an operator un-ticking for *cost* — the common case — wants exactly
  this. An operator un-ticking for *quality* (template garbage in the
  graph) or *exposure* clicks the second verb.

  `Remove facts` → `POST …/scopes/{source_scope_id}/facts/prune` (§10):
  deletes every claim anchored to a `corpus_files` row in the scope's own
  and zone collections (one `DELETE … WHERE corpus_file_id IN (SELECT id
  FROM corpus_files WHERE corpus_id = ANY(:ids))` — new
  `facts_repo().delete_claims_for_corpora`), runs `sweep_orphans` (the same
  post-delete hygiene `app/api/collections.py:815-831` does), stamps
  `graph_pruned_at`, audits `sharepoint_facts.prune` with the counts, and
  returns `{claims_deleted, subjects_deleted}`. The card confirms with the
  count *before* the click ("Remove 412 facts from Templates? Re-ticking
  later extracts them again — ~$17."). Corrections survive: they are keyed
  by subject id with `natural_keys` for re-identification after a
  re-extraction, which is exactly what a prune-then-re-tick is.

  Why the stamp instead of editing the state file: the state file is on
  the worker's disk (`state_path`, `crawler.py:456-467`) and the API may
  run on another host (observability §7.1 "why not the crawl state file").
  Without the stamp a re-tick would find every entry `done` and extract
  nothing, leaving a scope the operator just paid to re-enable with an
  empty graph — the worst silent state.

---

## 8. Cost visibility before spend — the estimate

### 8.1 What can be measured and what must be projected

| quantity | source | status |
|---|---|---|
| documents in scope, by outcome (eligible / tier / path / type / not indexed / no text) | `corpus_files` + rules, one query per collection | **exact** |
| documents that already carry facts | `EXISTS (claims WHERE corpus_file_id = f.id)` | exact for "has ≥1 claim"; a document extracted to zero facts is not distinguishable from an unextracted one — labelled |
| input characters per document | `SUM(LENGTH(corpus_chunks.text))` per file — the joined chunks *are* what the model is sent | **exact** |
| system-prompt prefix tokens | `len(build_system_prompt(prompt, ontology)) / 4` — pure, rendered at estimate time | measured ±20% (chars/4) |
| calls per document (retry rate) | last completed pass: `extraction_runs.usage.facts.calls / documents` | projected |
| output tokens per input token | last completed pass: `output_tokens / input_tokens` | projected |
| price | `src.llm_pricing.cost_usd(model=_model(), …)` — the one place tokens become USD | list price, named |

```
input_tokens  = Σ_d (chunk_chars_d / 4 + prefix_tokens) × calls_per_doc
output_tokens = input_tokens × output_ratio
cost_usd      = cost_usd(model, input_tokens, output_tokens)      # no cache terms: nothing caches today (cost-levers §6)
```

**Basis when there is no completed pass on this instance:** the observability
spec forbids a $ line from a hard-coded tokens-per-document guess, and
this design keeps that rule for *tokens per document*. But the input side
here is not a guess — it is the stored text — and the two projected
factors have a published measurement (cost-levers §1: 1.86 calls/doc, 0.23
output/input). The estimate therefore **shows a $ figure with the basis
labelled `reference measurement (cost-levers spec §1), not this instance`**
rather than nothing. A tick with no number is precisely the blind spend
this document exists to prevent; a number with its provenance on it is
what P10 asks for. Once a pass completes, the basis flips to
`last completed pass <date>` automatically.

Sensitivity is stated on the surface, once: "The retry rate is the largest
unknown — at 0% this is ~$X, at 100% ~$Y." Both ends come from the same
formula with `calls_per_doc` at 1 and 2.

### 8.2 The endpoint

`POST /api/admin/sharepoint/connections/{id}/facts-estimate`

```json
{ "scopes": [ { "source_scope_id": "…", "tier": "graph", "graph_paths": { … } } ] }
```

Hypothetical inputs, so the wizard can price a tick **before** it is saved
and step 3 can price all scopes in one call. Omitted `tier`/`graph_paths`
default to the stored row. Response, per scope and in total:

```json
{
  "scopes": [{
    "source_scope_id": "…",
    "documents": 9670,
    "eligible": 1240,
    "already_extracted": 0,
    "to_extract": 1240,
    "excluded": {"tier": 0, "path": 8390, "type": 41, "not_indexed": 3, "no_text": 0},
    "input_chars": 49_600_000,
    "est_input_tokens": 25_300_000, "est_output_tokens": 5_800_000,
    "est_cost_usd": 67.4,
    "est_cost_usd_range": [36.2, 72.5]
  }],
  "totals": { … },
  "basis": {
    "model": "…", "calls_per_doc": 1.86, "output_ratio": 0.23,
    "source": "last completed pass 2026-09-02T09:14Z (run …)" | "reference measurement — cost-levers spec §1, not this instance",
    "prefix_tokens": 2450, "pricing": "list price, src/llm_pricing.py",
    "instance_switch_on": true
  }
}
```

- `require_admin` + the router's `sharepoint.enabled` gate. **Not**
  `require_facts_enabled`: the whole point is to size the spend before
  turning the surface on; `basis.instance_switch_on` tells the UI what to
  say.
- PG-only (`claims`, `corpus_chunks` aggregates): typed `501
  requires_postgres_backend`, the same answer every facts route gives.
- One query per collection (new `corpus_files_repo().graph_estimate_rows
  (corpus_id)` returning `{id, path, file_type, processing_status,
  chunk_chars, has_claims}`), bounded by the scope count. Not computed in
  `_scope_out`, which runs on every card render.
- **Shown, never enforced.** No threshold blocks a run; the guardrail for
  runaway spend stays where it is (`extraction.facts.enabled`).

### 8.3 What this does not fix, and the seam for it

Realised spend **per scope** is not attributable today: `facts_ingest_runs`
is instance-wide by design (`facts_ingest_runs_pg.py:244-256`), and the
state file's per-document entries (`seconds`, counts) carry no tokens and
live worker-side. The seam, when it is wanted: `extract_one` returns its
own usage delta, `_accept` writes `facts_input_tokens`,
`facts_output_tokens`, `facts_calls`, `facts_model`, `facts_extracted_at`
as additive nullable columns on `corpus_file_sources` (the same shape
observability §7.3 proposes for conversion provenance). That also gives the
estimate an exact `already_extracted` set and would let the state file
retire. Not in this design's build order; named so the estimate's
`already_extracted` caveat has a known end.

---

## 9. What must not be curated away

The `ontology-building` skill is explicit: the sample must be **five to
ten documents chosen to be different from each other — one index, one
long prose document, one deck, one edge case (a scan, a template, something
half-filled-in)** — and "the recurring nouns are rarely the ones people
name in a meeting". Curation that removed those documents from the
*corpus* would be fine; curation that removed them from the *sample* would
starve the ontology of exactly the cases it exists to catch. The design
keeps the two apart:

1. **Curation is by scope, path and suffix — never by content or by
   "looks like a template".** A half-filled document inside a graph-tier
   folder is extracted like its neighbours. The only content-shaped rule
   is "no text at all" (unchanged). §4.4 refuses recency and series
   heuristics for this reason as much as for cost.
2. **The extension rule is a denylist of known-worthless formats**, not an
   allowlist of expected ones. Office *template formats* are on it;
   `.docx` files that happen to be templates are not, because the
   operator's `never_under: ["Templates"]` is the right tool and the
   estimate shows what it cut.
3. **The ontology builder's sample and dry-run bypass curation entirely.**
   The builder picks documents from any collection
   (`ontology_builder.html:171-187`, `/api/admin/ontology/dry-run`) and
   `run_facts_extraction(doc_ids=…)` skips the tier, path and type checks
   when explicit ids are given (§6.3) — an operator naming a document is
   the operator saying "this one". The skill's step 1 gains one sentence in
   the implementation PR: *choose the sample from every scope you index,
   not only the ones you will extract — the search-only tiers are where
   the edge cases live.*
4. **Over-curation is visible before it happens.** The estimate's
   `excluded` block and step 3's "excluded by your rules: N documents"
   line put the size of the cut next to the size of the spend. A rule that
   excludes 95% of a scope reads as a warning, not a saving.
5. **The freeze checklist already asks for the cost of the full run to be
   known and approved.** The step-3 total is that number; the skill's
   "New type = re-extraction ≈ $ over the corpus" line
   (`ontology_builder.html:217`) should read the same estimate for the
   graph-tier document count instead of the sample's collections.

What the design accepts: a fact that exists only in a search-only scope
is not in the graph. That is the point. Search still finds the document,
the citation still opens it, and promoting the scope is one tick with a
price on it.

---

## 10. API additions

| Route | Auth | Purpose |
|---|---|---|
| `POST /connections/{id}/scopes` (existing) | admin | body gains `tier`, `graph_paths`; errors `422 tier_not_available`, `422 graph_paths_need_real_paths`, `422 invalid_graph_path` |
| `GET /connections/{id}/scopes` (existing) | admin | rows gain `tier`, `graph_paths`, `graph_pruned_at`, `graph_effective` |
| `POST /connections/{id}/facts-estimate` | admin | §8.2 |
| `POST /connections/{id}/scopes/{source_scope_id}/facts/prune` | admin + `require_facts_enabled` | §7.3; `409 extraction_running` if a `corpus-extraction` job for this connection is active (a prune under a running pass would race the shipper's replace-mode writes) |

All under the existing `/api/admin/sharepoint` router (its
`sharepoint.enabled` 409 gate applies), registered with the route-coverage
guard, RBAC per `agnes-conventions/references/endpoint-rbac.md`, audit per
`references/audit.md` (the confirm route's declared fallback action
`sharepoint_connection.scope_confirm` already covers a tier change; prune
writes `sharepoint_facts.prune` itself and nothing else, per the
"never write both for the same event" rule).

Config: `extraction.facts.skip_extensions` (§6.1) documented beside
`extraction.facts.concurrency` in `docs/feature-flags.md`'s
`extraction_facts` row and in `config/instance.yaml.example`; the
`extraction_facts` switch description gains one clause ("per scope: the
wizard's `facts` checkbox — nothing is extracted from a scope that has not
opted in") and drops the known-false per-document dollar figures
(cost-levers §7 item 9).

---

## 11. Build order (each step ships alone and is useful alone)

1. **Planner + scope field + counters.** `graph_scopes_for`, the `tier`
   field end to end (`ConfirmScopeBody` → row → `_scope_out` →
   `spExistingScopeFields` → both checkboxes), zone inheritance,
   `only_scope_ids`, the `curation` report block, card rows showing
   `search` / `search + graph`. This alone delivers the whole cost lever.
2. **Extension rule.** `skip_extensions` with the default list, replacing
   `_TABULAR_EXTENSIONS`; `skipped-type` state; docs.
3. **Estimate.** Repo aggregate, endpoint, tick-time rendering, step-3
   totals, the basis switch between "last pass" and "reference".
4. **Path rules.** `graph_paths` validation, casefolded prefix matching
   (extract `under_prefix` into `connectors/sharepoint/paths.py` so
   `facts_extraction` does not import the crawler module), the
   saved-scopes textareas, the anonymize refusal.
5. **Prune.** `delete_claims_for_corpora`, the endpoint, `graph_pruned_at`
   and the planner's `pruned_after`, the card verb with its pre-count.
6. **Docs and the skill sentence.** `docs/sharepoint-extraction.md` §2
   ("per scope, three decisions that matter later"), `feature-flags.md`,
   `instance.yaml.example`, `ontology-building` step 1.

Steps 1–2 are one PR; 3, 4 and 5 are each one PR. None needs a migration.

---

## 12. Tests that define "done"

Planner (`tests/test_facts_extraction.py`, the `_run` seam at `:226`):
- a `search`-tier scope's documents are never yielded and the report says
  `search_only_scopes: 1`, `docs_seen` unchanged;
- a row with no `tier` behaves as `search`;
- a zone collection under a `graph` scope is yielded; under a `search`
  scope it is not;
- `only_under` / `never_under` / both, casefolded, component-safe (`Archive`
  does not match `Archived`); a rule on an anonymize-marked scope is
  ignored by the planner and refused by the API;
- `skip_extensions`: default list, configured list, invalid value → default
  + `skip_extensions_source: invalid`; `.XLSX` and `path/with.dots/file.dotx`;
- explicit `doc_ids` bypass tier, path and type;
- `pruned_after`: a `done` entry older than `graph_pruned_at` is
  re-extracted, a newer one is not;
- `only_scope_ids` narrows the pass;
- legacy `skipped-tabular` entries read as `skipped-type`.

API (`tests/test_admin_sharepoint*.py`, `tests/db_pg/…`):
- `tier` persists on confirm, resets when omitted (the always-persisted
  contract), `graph_paths` survives an omitted field and clears on `{[],[]}`;
- `422 tier_not_available`, `422 graph_paths_need_real_paths`, `422
  invalid_graph_path` with the offending entry named;
- estimate: exact counts per outcome; `already_extracted` counts a file
  with one claim; basis switches when a completed pass exists; DuckDB → 501;
  hypothetical `tier` in the body does not persist;
- prune: deletes only this scope's (and its zones') claims, sweeps orphans,
  stamps `graph_pruned_at`, audits once, refuses while a job is running,
  404s when `facts.enabled` is off.

Template contract:
- `spExistingScopeFields` mentions `tier` (a static assertion in the same
  spirit as the config carry-forward ratchet — the class of bug is "a
  round-trip forgot a field", and it has happened once already);
- both checkbox families (`data-spw-facts`, `data-spw-saved-facts`) exist
  and are disabled on an unselected row.

Docs guard: `feature-flags.md` names `skip_extensions`; the switch
description no longer contains a per-document dollar figure.

---

## 13. Non-goals and the seams left for them

- **`catalog` tier** (metadata-only index of everything). Needs the intake
  spec's T2 table, a no-download crawl mode, and an agent surface. The
  `tier` field reserves the value; `confirm_scope` refuses it.
- **Crawl-time rule evaluation** with a persisted `graph_eligible` per
  document (sees real paths on anonymized scopes; costs a re-crawl per
  rule edit). The planner's `rules.path_ok(path)` is the one call site to
  swap.
- **Per-scope realised spend** — §8.3's additive `corpus_file_sources`
  columns.
- **Recency/series rules and the triage call** — §4.4.
- **Per-connection curation of the NER detector or scan OCR.** Both are
  cheaper by an order of magnitude and already per-scope in effect
  (anonymize is a scope flag; OCR only fires on text-less PDFs).
- **Nested-scope detection in the wizard.** Named in §4.2; a separate fix.

---

## 14. Open questions (for the owner, not the builder)

1. **D4 — (A) or (B)?** The recommendation is (A); it is the only decision
   here that changes what an already-running instance does the morning
   after an upgrade.
2. **Should `never_under` accept a filename glob** (`**/*status*.pptx`) as
   well as a prefix? It covers the status-deck case without a series
   heuristic, at the price of a second matching rule (`fnmatch`, casefolded)
   the operator has to learn. Leaning yes, as a later addition to the same
   field — the estimate makes the trade visible either way.
3. **Is the reference basis acceptable on a first estimate**, or should a
   never-run instance see tokens only, as observability §5.3 prescribes for
   the crawl estimate? §8.1 argues the input side here is measured, not
   guessed, which is the distinction that spec drew.
