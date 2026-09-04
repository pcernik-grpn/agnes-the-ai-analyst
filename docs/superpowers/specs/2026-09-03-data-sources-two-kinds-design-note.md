# Data sources of two kinds — a design note on `/admin/data-sources`

**Docs only. No behaviour change.** This note exists so the shape of the
Data section gets decided before anyone builds it. It is an argument to
agree or disagree with, not scheduled work. A clickable mock sits beside it:
[`2026-09-03-data-sources-two-kinds-mock.html`](2026-09-03-data-sources-two-kinds-mock.html).

The proposal below is the third version. The first two were taken apart by
five independent review passes, one lens each — information architecture,
an admin-persona task walkthrough, consistency with the design system and
the admin nav contract, a simplicity/YAGNI adversary, and coherence with the
analyst Library. Where they agreed, the note follows them; where they split,
it says so. **It is not finished, and it is not asking for approval.** It is
a hand-off: this is how far the thinking got, §8 lists what is still open,
and the branch is there to be pushed into by whoever takes the design the
rest of the way.

## 1. The observed problem

`/admin/data-sources` was built for sources that produce **tables**:
Keboola projects, BigQuery, Snowflake, Databricks, Jira, uploads. The page's
own comment (`admin_data_sources.html`, the "Connected sources" section
head) fought for *one list, every source* — a connector that can be added
here must be visible here.

A second **kind** of source has since arrived. SharePoint produces
documents, not tables: its pipeline is crawl → text extraction → facts →
knowledge graph, and what it registers are **collections**, not tables and
not data packages. On one production instance the page now reads as
follows:

- 9 sources: 8 SharePoint connections and 1 Keboola project. The Keboola
  card is the last one on the page — not by design, but because
  `source_connections` lists `ORDER BY name` and the SharePoint connections
  are named with a leading `00_`.
- Roughly 390 scope collections, most of them flagged
  *"N collections with no group"* — granted to nobody, therefore invisible
  to every analyst (fail-closed, as the SharePoint spec requires). The
  badge that says so is a plain `<span>`; it is the loudest warning on the
  page and it has no click target.
- The lede still says *"Where the tables come from — the Keboola and
  BigQuery projects …"*. Documents are not mentioned anywhere above the
  list.
- The tab strip reads `Sources → Tables → Data packages | Semantic sources`.
  The arrows state a pipeline that is true for table sources and false for
  document sources, whose outputs never appear under Tables or Data
  packages.

None of this is wrong code. It is a page that still describes the world it
was built in.

## 2. What a document source actually produces

Two words that the UI currently uses side by side without ever defining
either:

- A **scope** is a SharePoint folder or drive the admin ticked in the
  connect wizard. It lives as a JSON row inside the connection's own
  `config.scopes` (no table of its own — see the module docstring of
  `app/api/admin_sharepoint.py`). It decides *what is crawled* and, with
  ACL mirroring on, *whose SharePoint permissions are mirrored*.
- A **collection** (`file_corpora`) is the container on the Agnes side that
  the scope's documents land in. It carries the access grants
  (`resource_grants` of type `collection`); every read of a fact, a quote,
  a chunk or a file is gated by "is this collection in the caller's
  accessible set". One confirmed scope maps to exactly one collection,
  created by `_create_scope_collection` and re-adopted, never duplicated,
  on an untick/re-tick.

So: scope is the source-side selection, collection is the Agnes-side
container. The analogy the reviewers converged on:

| table world | document world | what it is |
|---|---|---|
| table | file | the unit; has a sync / processing status |
| data package | collection | the grant-carrying container |

Where an admin can see collections today:

- inside the collapsed body of each SharePoint card, as "Scope collections"
  rows — up to 178 per card, each of which opens the **wizard** (an edit
  door, not a read door);
- on `/admin/access`, which lists every collection god-mode with owner, file
  count, grant count and a *"granted to nobody"* fold — but without the
  source it came from, the processing state of its files, or its facts;
- on `/library`, which is deliberately grant-scoped and not admin god-mode,
  so an ungranted collection is invisible there even to an admin (the
  detail page `/library/{slug}` is admin-openable; the listing is not).

An earlier draft of this note claimed admins had *no* god-mode view of
collections. That was wrong — Access has one. What is missing is the
**data** view: which source produced which collections, in what state.

## 3. The proposal (v3)

### 3.1 The Sources page

1. **Sort by kind, then name.** Table sources first — the section's other
   lenses (Tables, Data packages) are about them — then file sources, then
   derived rows. This alone fixes the buried card.
2. **Two bands, only when both kinds exist.** The shared group header the
   Library and Access already use (`.fbar-groupband`: title · count · one
   line of hint · caret), rendered only when the instance has at least one
   source of each kind. A single-kind instance sees exactly today's page.
   Band names: **Table sources** and **File sources**. The hint is one
   plain clause, no links, no arrows — the component owns layout, not
   vocabulary:
   - *Warehouses and projects; what they register is in Tables.*
   - *Document libraries; what they index lands in Collections.*
   Collapse state persists the way the Library's bands do
   (`data-sec-toggle`).
3. **The lede tells the truth.** One vendor-agnostic sentence naming both
   kinds; the MCP-sources sentence stays.
4. **The amber badge is a link.** *"178 collections shared with nobody"* —
   the same word Access uses — opens the place where that is fixed. Until
   the Collections lens exists, that is the Share step of this connection's
   wizard; afterwards it is `Collections?source=<id>&sharing=nobody`.
5. **The per-scope rows leave the card body.** A **Collections** cell in
   the pipeline strip carries the count and the link into the lens filtered
   to this source. 178 clickable rows that open the wizard were the wrong
   shape for a read.
6. **Extraction says what needs attention.** The cell already carries
   per-status counts in its `title`; *"23 need review"* becomes visible text
   linking to the collection's Library page filtered to `needs_review` —
   where the reason and *Re-ingest* already live. The *Review queue* cell
   counts fact-level rejections, not files; the persona walkthrough read it
   as "files that failed", and this is what disambiguates the two.

### 3.2 The tab strip

The arrows **stay**. Removing them was v2's answer to "the strip is only
true for tables", and three reviews rejected it independently: the arrows
are the section's only explanatory device (`admin_nav.py`, the Data
section comment: *"why is Sources next to Packages?"*) and two tests pin
them. The mechanism that already answers "a lens that is not part of the
sentence" is the break before Semantic sources — *"reachable from the same
strip, visibly not part of the sentence"*. The new lens takes the same
route:

```
Sources → Tables → Data packages | Collections · Semantic sources
```

A `tabs` entry without `chain`, `when:`-gated so it renders only on an
instance that has a file source or at least one collection. No template or
CSS change.

### 3.3 The Collections lens (`/admin/collections`)

For documents what Tables is for tables: the registry of what came in,
grouped by source, with its state and its reach. One row per collection,
one band per source (plus an *Uploaded* band for Library uploads):

| column | source of the value |
|---|---|
| Collection | name → `/library/{slug}` (files, statuses, Re-ingest) |
| Source | the connection, or the uploader |
| Files | `corpus_files_repo().count_by_corpus()` — one query |
| Needs attention | `needs_review + rejected`, one grouped count; links to the Library page filtered |
| Facts | `count_visible_facts_for_collections`; **absent** on a DuckDB-backed instance |
| Sharing | the visibility chip: *2 groups* / *nobody* |
| action | **Share…** — the existing share dialog the Library detail already opens |

What it deliberately is not:

- **Not a grant editor.** *Who can see it* has one home, Access, which
  already lists every collection with owner and grant count. The lens shows
  the chip and opens the existing dialog; a fourth writer of collection
  grants would drift the way the four editors Access consolidated did.
- **Not a file browser.** A single-file view (250 000 rows behind a filter)
  is the per-collection Library page's job, which already has status chips
  and Re-ingest. Cross-source file triage, if it is ever needed, is a "log
  you check", not a lens you manage — an off-nav page reached from the
  Extraction cell, the way `/admin/sync` hangs off the Sync cell.
- **Not named Files.** The Library's section is *Artefacts*; Access, the
  wizard and the card all say *Collections*. "Files" beside "Tables" reads
  as CSV uploads.

**Build the endpoint first, the page second.** The Sources page today
folds every corpus file of every scope on every render and on every
post-mutation refresh (`_sharepoint_pipeline_cell` iterates
`list_for_corpus`). A count-first endpoint (`count_by_corpus` plus one
grouped status count) that the source card reads from makes the lens the
fix for that cost rather than an addition to it.

### 3.4 Dropped after review

- Grouping the *+ Add source* picker into the same two kinds. SharePoint is
  the only document connector; a group of one is a label. Revisit with the
  second connector.
- Saying "scope" anywhere outside the wizard. One tab away, Access uses
  the same word for an audience. On the band and the lens it is a folder or
  a collection.
- Linking *Ontology & graph* from a band hint. The page has no nav home;
  a hint would paper over that rather than fix it.
- Splitting Data into two sections (see §4).

## 4. The one open decision

Everything above follows from one answer, and the reviews split on it:

**(A) A collection is the document world's data package** — a grant
container that lives in the same Data section, beside Data packages. This
note. One Sources list, one picker, one strip; Tables and Data packages are
empty lenses on a documents-only instance and Collections is empty on a
tables-only one, exactly as Semantic sources is Keboola-only today.

**(B) Documents are a second product line** with their own section:
`Documents: Sources → Collections → Ontology & graph`. Every arrow true,
per-kind lede and empty states, and Ontology finally gets the nav entry it
lacks. Costs: two Sources pages and two pickers, the admin must know a
source's kind before finding it, and it reverses the "one list, every
source" decision this page was rebuilt on.

The reviews lean to (A) now and (B) once a second document connector
exists — at which point the split also has a natural home for Ontology.
The vocabulary, the bands, the lede and the count-first endpoint are
identical under both; only the strip and the section differ.

## 5. Found beside the question

Not part of the proposal, but each affects whether the result is usable on
an instance of this size:

- **The Sources page is O(files).** See §3.3. Every render and every
  `/api/admin/source-pipelines` refresh decodes every corpus file of every
  scope.
- **Access may not load on such an instance.** `app/api/access.py` eagerly
  calls every `list_blocks()`, including `corpus_file`'s — ~390 collections
  × all their files in one JSON payload. Verify before linking anything
  there.
- **Access's "Open" on a collection is a dead link.** It targets
  `/library/d/{id}`; the only route is `/library/{slug}` and the slug is
  already in the projection. Ten minutes, and admins get "open" from Access
  today.
- **Seven of the eight SharePoint connections are one site**, split for
  crawl parallelism. Bands do not shrink seven cards; a root-scope
  connection does. Do not design the page around a workaround.
- **Facts are Postgres-only.** The card already degrades; the lens's Facts
  column and endpoint must too, or a DuckDB-backed instance gets a 500.

## 6. Build order, if agreed

1. *Half a day, no new page:* lede; sort by kind; bands when both kinds
   exist; badge → wizard Share step; Extraction cell surfaces the
   needs-review count; Overview signal for ungranted collections (the
   plugin sibling in `admin_signals.py` is the pattern); fix the Access
   link.
2. *Collections lens:* the count endpoint (with the source card switched to
   it), then the page — `require_admin`, `page_hero_title = "Data"`, the
   admin tab include, `SECTION_PAGES`, the wide-table pattern
   (`.data-table-wrap`, fixed layout, pinned action column), read posture in
   `src/audit_posture.py`, nav-coverage and label guards, PG smoke coverage,
   `_PG_ONLY_ROUTE_EXEMPTIONS` with a typed 501 if the endpoint touches
   facts.
3. *Later, on evidence:* cross-source file triage as an off-nav page;
   picker grouping with the second document connector; the two-section
   split.

## 7. Vocabulary

| word | verdict | why |
|---|---|---|
| Table sources | keep | the reader's word |
| Document sources | → **File sources** | collides with Semantic sources' own gloss ("where documents come from"); the system's word is `file_source` |
| Files (as a tab) | → **Collections** | the Library section is *Artefacts*; Access, wizard and card already say Collections |
| Collections | keep, without "as folders" | folder is the Library's *shape*, not a name |
| scope | wizard only | means *audience* one tab away in Access |
| Data packages | keep verbatim | the nav comment already argues this |
| "N collections with no group" | → **shared with nobody** | Access's word for the same fact |
| Who can see it | → **Sharing** | the shared visibility chip's vocabulary |

## 8. Still open — where help is wanted

Nothing in §3 should be read as settled where one of these is unanswered.

1. **One section or two** (§4). Everything else is the same under both
   answers; the strip and the picker are not.
2. **Does the Collections lens earn a page**, or is the honest answer to
   give Access's Collections block a *Source* column and a *Needs
   attention* count and stop there? The YAGNI pass argued the latter is a
   false economy only because nothing answers "which files are stuck,
   across sources" — which the lens as drawn does not answer either.
3. **Where cross-source file triage lives**, if anywhere: an off-nav page
   reached from the Extraction cell was the consensus, but nobody drew it.
4. **What the SharePoint card body says once the scope rows leave it.**
   Today the body is schedule, certificate, identity matching, anonymization
   and the last run's error badges; the scope list was its longest row.
5. **The band names.** *Table sources / File sources* survived the
   vocabulary pass, but "file source" is the system's word (`file_source`),
   not obviously the reader's.
6. **The badge's target before the lens exists** — the wizard's Share step
   grants per scope in a 178-row list with no search; is that better than
   no link, or should the badge wait for the lens?
7. **The seven-card split.** A root-scope connection is the stated goal;
   until then, should the page fold connections that share a site, or say
   nothing?

