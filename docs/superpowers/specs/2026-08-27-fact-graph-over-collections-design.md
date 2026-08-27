# Fact graph over Collections — design & build specification

**Date:** 2026-08-27 (rev 3)
**Status:** buildable draft — revision 3 after a six-way audit (spec internals,
Agnes code, cuesta-star-graph, evaluation workbook v0.2, licences/services, UI)
**Verified against:** Agnes worktree `zs/facts-scope-access` (base `d97e186a8`),
`keboola/cuesta-star-graph` main `753be22`, `eval_scoring_workbook_v0.2.xlsx`
(FROZEN 2026-08-27), `padak/doc_quantization`, `padak/doc_converter`.
**Scope note:** this spec deliberately contains customer-specific material
(the evaluation workbook, Kantata, personas, TCRD ticket ids, the 1P vault
name) by owner decision of 2026-08-27 — concentrated in §14–§17 but also
present in §1, §5, §7 and §8. If this repository ever returns to public
distribution, the spec must be scrubbed or moved to a private repo as a
whole; a section-range excision is not sufficient.

## 0. Revision history — what was wrong and why it changed

Recording corrections rather than silently fixing them, because each one
constrains the design.

**Rev 1 → 2 (three independent design reviews):**

1. **`attrs` had no provenance.** They sat on the fact, merged across every
   contributing document; a caller who could read one evidence document
   received values extracted from documents they could not read, and
   `fact_search` filters made that an oracle to binary-search. Fixed
   structurally: attributes live on the **claim** (§3), and everything a
   caller sees is projected from claims they can read (§4).
2. **Edges had no evidence and therefore no visibility rule.** Fixed: an edge
   is a first-class subject with its own claims (§3).
3. **`corpus_files.id` is not stable** across the re-sync path a document
   crawler uses (`_replace_existing_by_path`,
   [app/api/collections.py:628](../../../app/api/collections.py) — verified:
   unconditional purge+reinsert; the only content-hash awareness is blob-level
   `keep_blob_path`, the row/chunks/derived tables are destroyed regardless).
   Fixed via the source-stable-id anchor (§6).
4. **Two claims were false**: `corpus_files.source_url` does not exist
   (`_COLS`, [src/repositories/corpus_files.py:25](../../../src/repositories/corpus_files.py)),
   and quotes are verbatim against extracted markdown in `corpus_chunks.text`,
   not against the document a user opens (§8).
5. **The enforcement primitive named was wrong**: `can_access_collection`
   takes a bare `user_id` and, for an `AgentPrincipal`, crashes or elevates to
   the owner's authority; `accessible_collection_ids`
   ([app/auth/access.py:654](../../../app/auth/access.py)) is the correct one (§5).

**Rev 2 → 3 (full audit against artifacts):**

6. **The evaluation standard is workbook v0.2 in full** (§14) — five arms,
   five thresholds, 0/1/2 rubric, cadence, protocol. The cuesta repo's eval
   was aligned to v0.2 on 2026-08-27 (commit `753be22`): its 12 sandbox
   questions now map onto the ten workbook prompts (two are sandbox-only
   leftovers and two workbook ids are uncovered — §14.5), and its protocol already
   defines the A3′ (same-context) rehearsal arm — the earlier claim that "no
   arm tests A4 vs A3" is stale for the sandbox; what remains unbuilt is the
   **real** A3 seed-pack arm for workbook rounds.
7. **The converter problem is smaller than framed**: the reference
   `doc_converter` routes only `.pdf` to PyMuPDF (AGPL); every Office format
   already goes through **markitdown (MIT)**. We replace one engine, not the
   converter (§9).
8. **`doc_quantization` is a CLI batch pipeline, not a service** — the
   integration needs a thin driver, and Czech inflection breaks naive
   pseudonyms (§9).
9. **Security tests contradicted the Entra section**: S-tests presupposed
   source-ACL derivation that §13 says does not exist yet. The suite is now
   split into Phase 0 (grant-layer, runnable today) and Phase ACL (§15.1).
10. **Schema defects**: `fact_aliases` UNIQUE referenced a column the table
    lacked; the claims cascade contradicted a "recovery" promise;
    `visibility_mode` looked like a column but is instance config. All fixed
    in §3.
11. **Three designs were rejected and are recorded here so nobody
    re-proposes them**: a standalone `documents` table (duplicates
    `corpus_files` and the grant model), `DOCUMENT_SCOPE` grants (a competing
    grant surface — the collection IS the grant unit; the committed remnant
    is reverted per §16), and `_remote_attach` to an external fact store
    (facts must live beside `corpus_files` for the FK and the filter).
12. **The worker lane is a code change, not configuration** — `_VALID_LANES`
    is a closed two-value tuple
    ([app/worker/registry.py:21](../../../app/worker/registry.py)) and
    per-process lane selection does not exist (§16 step 7).

**Rev 3 → 3.2 (Run P proving run, 2026-08-28):**

13. **Endpoint evidence.** The producer wire contract (§7.0) explicitly
    permits a node with no evidence of its own to exist purely to anchor an
    evidenced edge ("nodes without evidence are warnings, every edge carries
    >=1 evidence"). The sweep and the visibility predicate did not honor
    that: `sweep_orphans` deleted every endpoint-only node as soon as it was
    written (`ingest_batch` sweeps at the end of the SAME batch that created
    it), and `_subject_status` counted only a subject's OWN claims, so even a
    surviving endpoint-only fact was invisible on every read path. Run P (a
    planted-corpus proving run against real ingest) surfaced both: the sweep
    deleted 15 endpoint-only facts and 27 of their 28 claimed edges cascaded
    away with them, and the read path independently hid the rest via the
    per-node check. Fixed structurally: an edge's claim now evidences the
    relationship AND, implicitly, the existence of its two endpoints (§4,
    §6) — `attrs` are UNCHANGED (still projected from a fact's own claims
    only, so an endpoint-only fact serves `attrs: {}`), and EDGE visibility
    is UNCHANGED (still its own claims only, §5 rule 3 / S3).

One objection from the reviews is deliberately **not** fixed, because it is a
question, not an error: **what facts buy over the retrieval Collections
already does.** It is a measured gate (§14, Decision #2) — and the workbook
itself pre-registers the uncomfortable answer as a legitimate outcome.

---

## 1. What this is

Typed **subjects** — entities and the relationships between them — extracted
from documents, where every assertion carries the document it came from, the
verbatim sentence supporting it, and the date of that document. Built as a
layer **over the existing Collections subsystem**, not beside it.

It is a general Agnes capability. SharePoint (via the cuesta-star-graph
crawler) is the first contributor of documents; anything that can put files
into a collection contributes the same way. Agnes owns the schema; producers
write into it through the contract in §7.

**Out of scope, deliberately** (each is somebody's work, not nobody's):

- the crawler — **adopted** from `keboola/cuesta-star-graph`, with a named
  hardening backlog (§7.1), never rewritten;
- the extraction pass (`extract.py` + `skills/kg-builder-agent.md`) — a
  producer against §7's contract;
- the structured lane (Kantata for prompt X1) — a normal connector/table
  concern, a dependency of the evaluation only (§14.5);
- second document sources (Drive, S3), non-document facts, multi-language
  corpora, and load testing at corpus scale (§15.6 names them so absence is a
  decision).

---

## 2. The one decision everything follows from

**A claim is the unit.** A claim is *one document saying one thing about one
subject*. Everything else is derived from the set of claims a caller may read:

```
subject (fact | edge) ──< claim >── corpus_files ── collection ── grants
```

- Existence of a subject: ≥1 readable claim.
- Attribute values: projected from readable claims only (§4).
- Quotes: readable claims only.
- An edge is visible by the same rule, on **its own** claims — never inferred
  from its endpoints.

One rule, one grain, one join. The first draft had three grains with a rule
for one of them, which is how it leaked.

### Consequences that are not obvious

- **App-state, not analytics** — because per-caller filtering is impossible on
  a distributed parquet. Per the A3 ratchet this means a **Postgres-only
  repository** (`facts_pg.py`, factory-registered), **one Alembic revision**,
  no DuckDB sibling, no `src/db.py` step (`SCHEMA_VERSION == FROZEN == 124`,
  [src/db.py:88](../../../src/db.py)). The sanctioned pattern is confirmed by
  `tests/test_repository_registry.py::test_registry_backends_are_symmetric`
  and `tests/db_pg/test_repo_module_pg_first_ratchet.py`.
- **DuckDB-backend instances do not get this feature**: resolving a PG-only
  key raises `RequiresPostgresBackend`
  ([src/repositories/__init__.py:219](../../../src/repositories/__init__.py)),
  translated app-wide to a typed 501
  ([app/main.py:3138](../../../app/main.py)). Release-notes material, not a
  support ticket.
- **Facts are never distributed.** `agnes pull` moves parquet; app-state is
  not in that path, and the manifest already skips undistributed rows
  ([app/api/sync.py:1631](../../../app/api/sync.py)). Answers are server-side.
- **Table access policies cannot apply** — they attach to `table_registry`
  rows reachable via `/api/query`; facts are neither. Enforcement lives in
  the repository (§5).
- **Feature flag**: `facts.enabled` (default **false**) via the canonical
  resolver `feature_enabled("facts", "enabled",
  env_var="AGNES_FACTS_ENABLED", default=False)`
  ([app/instance_config.py:384](../../../app/instance_config.py)), plus a
  `Switch` declaration in `app/switches.py` and a `docs/feature-flags.md`
  entry — the same trio recent flags used (`agent_profiles.enabled`,
  `guardrails.enabled`).

---

## 3. Schema

Postgres only. One Alembic revision for the facts tables (the
`corpus_file_sources` DDL belongs to the §6 prerequisite PR); SQLAlchemy
models added per repo convention.

```
facts        id TEXT PK                    -- 'f_' + token_hex(8); opaque, never derived
             type TEXT NOT NULL            -- ontology node type

fact_aliases fact_id TEXT NOT NULL FK→facts ON DELETE CASCADE
             type TEXT NOT NULL            -- denormalized from facts, like corpus_id on claims
             natural_key TEXT NOT NULL     -- producer slug, e.g. 'myers-emergency-power-systems'
             UNIQUE (type, natural_key)

edges        id TEXT PK                    -- 'e_' + token_hex(8)
             src TEXT NOT NULL FK→facts ON DELETE CASCADE
             dst TEXT NOT NULL FK→facts ON DELETE CASCADE
             type TEXT NOT NULL            -- ontology edge type
             UNIQUE (src, type, dst)

claims       id TEXT PK                    -- 'c_' + token_hex(8)
             fact_id TEXT NULL FK→facts ON DELETE CASCADE      -- exactly one of
             edge_id TEXT NULL FK→edges ON DELETE CASCADE      -- these two is set
             CHECK ((fact_id IS NULL) <> (edge_id IS NULL))
             corpus_file_id TEXT NOT NULL FK→corpus_files(id) ON DELETE CASCADE
             corpus_id TEXT NOT NULL       -- denormalized: THE visibility column
             file_sha256 TEXT NOT NULL     -- content the claim was made against (staleness check)
             attrs JSONB NOT NULL DEFAULT '{}'   -- what THIS document says
             quote TEXT NOT NULL           -- verbatim against the extraction (§8)
             quote_hash TEXT NOT NULL      -- sha256(quote)[:16], computed at write
             document_date DATE NULL       -- from the source system (§10)
             created_at TIMESTAMPTZ
             -- functional UNIQUE INDEX (not a table constraint — Postgres
             -- cannot express COALESCE in UNIQUE; Alembic: op.create_index
             -- with sa.text and unique=True):
             --   (COALESCE(fact_id, edge_id), corpus_file_id, quote_hash)

corpus_file_sources                        -- NEW PG-only table; the anchor (§6)
             corpus_file_id TEXT PK FK→corpus_files(id) ON DELETE CASCADE
             corpus_id TEXT NOT NULL
             source_stable_id TEXT NOT NULL    -- crawler's stable_id ('graph:<driveItem-id>')
             source_doc_id TEXT                -- crawler's citation key, sha256[:16] (§7.2);
                                               --   rewritten when a provisional id is replaced
             source_sha256 TEXT
             source_url TEXT NULL              -- when it lands (open item O7)
             UNIQUE (corpus_id, source_stable_id) · INDEX (corpus_id, source_doc_id)

corrections  subject_kind TEXT NOT NULL    -- 'fact' | 'edge'
             subject_id TEXT NOT NULL      -- PK (subject_kind, subject_id)
             natural_keys JSONB NOT NULL   -- snapshot for re-attachment (see below)
             verdict TEXT NOT NULL         -- 'wrong' | 'restricted' | 'revealed'
             reason TEXT NOT NULL
             decided_by TEXT NOT NULL · decided_at TIMESTAMPTZ
```

Indexes: `claims(corpus_id)`, `claims(fact_id)`, `claims(edge_id)`,
`claims(corpus_file_id)`, `facts(type)`, `edges(src)`, `edges(dst)`,
`fact_aliases(fact_id)`, **GIN on `corrections.natural_keys`** (the
re-attach lookup runs on every ingest). All timestamps carry
`server_default=now()`.

Load-bearing details:

- **`facts.id` is opaque; `fact_aliases` carries identity.** A producer node
  id `<type>:<slug>` maps to an alias row; the surrogate is minted on first
  sight. A merge is *adding an alias* (repoint aliases + claims to the
  canonical subject, audit-logged); a split is the reverse. Hash-derived ids
  were rejected: they decide entity resolution at write time with no repair
  path — under-merge fragments visibly, over-merge fuses silently.
- **`corpus_id` is denormalized onto claims** so the visibility predicate is
  one indexed column, not a join through `corpus_files` on every traversal
  hop.
- **`file_sha256` is a staleness check, not a resurrection mechanism.** A
  claim whose stored hash no longer matches the file's current content is
  stale by definition and replaced on the next pass. Loss-of-anchor is
  prevented *upstream* by §6's upsert — the cascade is reserved for true
  deletion. (Rev 2 promised "recovery by sha" alongside a cascade; those were
  contradictory and the promise is withdrawn.)
- **`corrections` is never written by a producer, never cascades with its
  subject**, and carries a `natural_keys` snapshot (for a fact: its alias
  keys; for an edge: `[src_key, type, dst_key]`) so that a subject deleted
  and later re-created under a new surrogate **re-attaches its correction at
  write time** — a legal hold must not vanish because a document was briefly
  missing.
- **`visibility_mode` is instance configuration, not a column**:
  `facts.visibility_mode: any_evidence | all_evidence` in `instance.yaml`
  (default `any_evidence`), resolved through `get_value`.

---

## 4. What a caller sees

Given `readable` = the caller's readable collection set (§5):

```
visible(edge)    := ∃ claim ∈ edge.claims : claim.corpus_id ∈ readable
                    (all_evidence mode: ∀ instead of ∃)
visible(fact)    := ∃ claim ∈ (fact.claims ∪ incident_edge_claims(fact)) : claim.corpus_id ∈ readable
                    (all_evidence mode: ∀ over that same union)
incident_edge_claims(fact) := ⋃ { edge.claims | edge incident to fact, edge not withheld }
quotes(subject)  := { claim.quote | claim.corpus_id ∈ readable }     -- always ∃-filtered, OWN claims only
attrs(subject)   := per-key projection over the subject's OWN readable claims only:
                    – the value from the claim with the LATEST document_date wins;
                    – equal dates with differing values → the key is returned as
                      conflicted: {values: [...], claims: [...]} — never silently picked;
                    – a dated claim beats an undated one; two undated ones conflict.
```

Nothing a caller receives is ever computed from a claim they cannot read —
which is what makes the attribute-filter oracle impossible rather than
unlikely. `quotes`/`attrs` use identical rules for facts and edges, and stay
OWN-claims-only for both — the endpoint-evidence union (rev 3.2, §0) widens
only a FACT's EXISTENCE gate, never its projections: an endpoint-only fact
(visible purely because an incident edge carries a readable claim) serves
`attrs: {}` and an empty `quotes`/claims list, not a 404. Edge visibility is
unchanged — an edge's own claims only, never inferred from its endpoints
(§5 rule 3 / S3). `all_evidence` hides strictly more **within one grant
snapshot**; it is not a general monotonic guarantee (grant drift, §13).

**Admin corrections** (each with reason → `audit_log`):

- `wrong` — withheld from everyone AND exported to the producer (§7.4) so
  re-extraction does not resurrect it;
- `restricted` — withheld regardless of claims (legal hold, personnel);
- `revealed` — served **without quotes** to **every authenticated caller on
  the instance**, regardless of collection grants and regardless of
  `visibility_mode`. That reach is deliberate; the UI labels it exactly that
  way. (The Agnes `Admin` group god-modes authorization anyway — an override
  governs what everyone else sees.) Review tightening (2026-08-28): the
  override reveals the *fact*, not the geography of its evidence — claim
  metadata served under `revealed` hides the evidencing document's
  name/path/URL for claims in collections outside the caller's grants
  (opaque ids only); readable claims keep full document identity.

  Corrections management is **API-only in v0** (admin PAT / scheduler token
  on `PUT`/`DELETE /api/facts/corrections/*` + the export) — no web, CLI or
  MCP surface yet, a conscious scope cut recorded here so the triple-surface
  exemption is a decision, not an accident.

---

## 5. Enforcement

**The primitive is `accessible_collection_ids(user)`**
([app/auth/access.py:654](../../../app/auth/access.py)): returns `None` for
admin (= all collections); for `PRINCIPAL_TYPES` (agent/co-session) returns
the **live intersection unmodified**; for a dict user returns group grants
**∪ collections the user owns** (`file_corpora.created_by`, capped scan).
Never `can_access_collection` for principals — it takes a bare `user_id` and
either crashes on a frozen dataclass or, fed `owner_user_id`, substitutes the
owner's full authority including admin god-mode; its sibling's docstring
records exactly this trap.

Two consequences a test author must know:

- **Security fixtures must not be uploaded by the probed caller** — ownership
  unions into a dict user's readable set, so a fixture uploaded as Alice is
  readable by Alice regardless of grants, and the test passes vacuously.
- Because the tools are not collection-scoped in their signatures, the
  declarative route gate (`require_collection_access`) does not apply; **all
  enforcement lives in the repository**, in one shared helper used by every
  read method, with tests that drive each method through a restricted
  `AgentPrincipal`, not only a dict user.

Three implementation rules, each answering a found hole:

1. **Filter in SQL, before `LIMIT`** — `claim.corpus_id = ANY(:readable)` as
   a join predicate, never a Python post-filter; a short page must not signal
   hidden matches.
2. **404, never 403**, for a subject that does not exist *or* has no readable
   claim — indistinguishable in status, body, and timing envelope. Collections
   documented this choice
   ([app/api/collections.py:27](../../../app/api/collections.py)); ids being
   opaque (§3) reduces but does not remove the probing surface.
3. **Traversal re-evaluates at every hop.** `fact_neighbors` never walks from
   a visible subject into one that is not itself `visible(fact)` per §4's
   endpoint-evidence union, and never reveals that a path continues. Caps live in §12 (one home): ships with depth
   default 1 / max 2, per-node fanout 100, result 500, statement timeout. The hub-node walk is the query that
   explodes and the one the benchmark never ran (§12).

**No general SQL escape hatch** over these tables, in any surface. An
unfiltered path defeats every rule above (the `graph_sql` lesson from the
sandbox server, recorded on TCRD-186/196).

---

## 6. Document identity — the anchor

Verified failure to design around: `corpus_files.id` is `"cf_" +
secrets.token_hex(8)` on every `add()`
([src/repositories/corpus_files.py:78](../../../src/repositories/corpus_files.py)),
and the documented doc-sync upsert `_replace_existing_by_path`
**unconditionally purges** the existing `(corpus_id, path)` row — chunks and
derived tables included — before inserting a new row with a new id; only the
content-addressed *blob* survives a byte-identical re-upload. Harmless for
chunks (re-embedding is cheap); destructive for claims (an LLM pass). **The
asymmetry is the finding.**

The crawler already holds the right anchor:
`stable_id` — `graph:<driveItem-id>` (survives rename and move) or
`local:<relpath>` (does not) — is its delta key, distinct from `doc_id`
(= content `sha256[:16]`, the citation key, which survives rename but changes
with content). Metadata-only rows carry a *provisional* doc_id
(`sha256(cTag|stable_id)`), rewritten on the first content crawl.

**Design:**

- `corpus_file_sources` (§3) maps `(corpus_id, source_stable_id)` →
  `corpus_file_id`. It is a **new PG-only table** because `corpus_files` is a
  frozen DuckDB+PG pair and the DuckDB ladder is frozen at 124 — no new
  column is possible there.
- **Prerequisite change to Collections** (its own PR, precisely scoped):
  – **owns the `corpus_file_sources` DDL** in its own Alembic revision (the
    facts revision of §16 step 2 depends on it, never creates it);
  – API: the upload endpoint gains positionally-paired form fields
    `source_stable_ids` (+ optional `source_doc_ids`, `source_sha256s`,
    `document_dates`), mirroring how `paths` pairs with files today;
  – upsert order: match `(corpus_id, source_stable_id)` first, then
    `(corpus_id, path)`; **any match via this code path preserves the row
    id** — including a manual path re-upload of a crawler-anchored file, so
    a hand upload can no longer cascade a document's claims away. Unchanged
    sha → skip re-chunking entirely; changed → purge chunks + reset
    `processing_status` on the same row. In-place update purges zip-bundle
    children exactly as today's purge walk does;
  – **frozen-pair obligation**: the update-in-place methods land in
    `corpus_files.py` AND `corpus_files_pg.py` with the contract test
    extended — this PR touches a maintained pair, unlike the facts PR;
  – **DuckDB backends**: supplying `source_stable_ids` yields the typed 501
    (the mapping table is PG-only); omitting it keeps today's flow intact.
- Ingest (§7) refuses a claim whose `doc` reference cannot be resolved
  through this mapping.

Lifecycle:

| event | effect |
|---|---|
| re-sync, content unchanged | row kept, zero re-processing, claims untouched (test C3) |
| rename / move | same `source_stable_id` → same row, path updated; claims untouched (C4). Note: the current crawler's ctag-skip leaves `path` stale on a pure rename — the port must refresh path/name on delta items even when content is unchanged |
| content changed | same row, new sha; that document's claims **replaced** on next extraction (old quotes may no longer exist in the text) |
| deleted in source / moved out of crawl scope | row deleted → claims cascade → an EDGE left with zero claims of its own is deleted first; a FACT is deleted only once it has NEITHER an own claim NOR an incident edge still carrying any claim (rev 3.2, §0 — an edge anchors its endpoints); deletions are **counted in the run report**; their `corrections` rows survive (§3) |

The orphan-subject sweep runs as its own step **after any batch of
`corpus_files` deletions** — ingest-driven or UI-driven (an admin deleting a
file from a collection cascades claims exactly the same way) — never inside
the deleting transaction. Its counts land in the run report or, for UI
deletions, on the source card, attributed to the operation that triggered
them. Edges sweep first, so by the time the fact sweep runs every surviving
edge already carries >=1 claim — a fact with a live incident edge survives
the sweep even when it carries no evidence of its own, exactly the
endpoint-only node the producer wire contract (§7.0) permits. The sweep is
correction-agnostic (raw claim existence, not a visibility check): a
`wrong`/`restricted` edge with a live claim still anchors its endpoints here,
same as any other edge — only the READ path (§4/§5) withholds it.

---

## 7. Producer contract and ingest

### 7.0 Wire format (verbatim from the producing pipeline)

The producer is the cuesta-star-graph pipeline (crawl → convert → anonymize →
extract → reconcile → gates). Its emitted shapes, which the ingest endpoint
accepts as-is:

```jsonc
// node row
{"id": "<type>:<kebab-slug>", "type": "<node_type>",
 "attrs": { /* may be {} */ },
 "evidence": [{"doc_id": "<sha256-16>", "quote": "<verbatim substring>"}]}

// edge row  (merge key: src+type+dst)
{"src": "<node id>", "type": "<edge_type>", "dst": "<node id>",
 "attrs": {}, "evidence": [{"doc_id": "...", "quote": "..."}]}
```

Conventions the pipeline enforces and ingest relies on
(cuesta-star-graph, verified): node id matches
`([a-z_]+):([a-z0-9][a-z0-9-]*)` with prefix == type
(`validate_graph.py:60`); slugs are lowercase ASCII with `&`→`and`; every
**edge** carries ≥1 evidence entry (`possible_duplicate_of` exempt —
`verify_quotes.py:86`), nodes without evidence are warnings; agents **never
emit document nodes** — documents are materialized from the crawler index;
`extract_status` values are `ok`, `ok (cached)`, `empty`,
`skipped (metadata-only)`, `error: …`, and all skip logic keys on
`startswith('ok')`.

Document metadata rows are the crawler's 17-field `make_row`
(`crawl.py:121-142`): `doc_id, stable_id, name, path, site, drive, source
('local'|'graph'), mime, size, created, modified, author, last_editor,
sha256, extracted_path, extract_status, crawled_at` — underscore-prefixed
internals (`_ctag`, `_drive_id`) are stripped by consumers. Do **not**
implement against the stale `out/documents.jsonl` fixture (wrong `source`
value, missing seven fields).

### 7.1 The crawler — adopted, with a named hardening backlog

We adopt `crawl.py`, we do not rewrite it. Present and kept: `getAllSites`
(the `search=*` endpoint silently under-returned 2/10 sites for app-only
tokens, observed live), per-drive `deltaLink` persisted, delta deletions
handled, repair pass (delta never revisits an unchanged file), 429
`Retry-After`, at most one full delta pass per day, temp-download→hash→
extract→delete, `markitdown[all]` (bare `markitdown` breaks every Office
conversion).

**Absent, and required for production** (verified by audit — zero hits for
each): webhook subscriptions + renewal before expiry; `410 Gone` → full
resync **and never persisting the dead deltaLink** (today every later run
fails identically and change detection silently stops); mid-crawl token
refresh (the token is acquired once; a large crawl outlives ~1h); 503/504
retry (`raise_for_status` kills the run); a bound and a wait-cap on the 429
loop (today `while True`, uncapped); incremental index persistence (today the
index is written once at the end — a kill loses the pass; resume must neither
skip nor duplicate); per-item ACL reads where inheritance is broken
(`HasUniqueRoleAssignments`; Graph does not return `inheritedFrom` for
SharePoint libraries) — Phase ACL only (§13); path/name refresh on renamed
items (§6).

**Scan transcription is on by default.** It is a cost decision, not a
capability toggle. The prototype's `--vision` flag requires a manual
`--binaries-dir` the pipeline cannot populate (the crawler deletes temp
downloads by design) — the production path must **re-fetch the binary via
Graph** (`_drive_id` + item id are on the row), transcribe
(render → vision model), and persist the transcript as the document's
extraction artifact so the verbatim gate has text to check. Queue cost is
surfaced in the UI (§13.2).

### 7.2 Ingest API

`POST /api/facts/ingest` — scheduler-token
([app/auth/scheduler_token.py](../../../app/auth/scheduler_token.py)) or
admin PAT; CSRF n/a (bearer). Body:

```jsonc
{"documents": [...],            // make_row rows, each EXTENDED by the producer
                                 //   with "corpus_id" (from its scope config —
                                 //   crawl.py does not know collections)
 "full_documents": ["<doc_id>"], // replace-mode markers, see below
 "nodes": [...], "edges": [...]}
```

**Two modes, explicit in the wire contract:**

- A document listed in `full_documents` is **replaced**: ALL existing claims
  with its `corpus_file_id` are deleted, then the incoming ones inserted — so
  a subject the re-extraction no longer mentions loses its stale claim (this
  is what makes test C2 pass; the earlier "delete by file ∩ incoming
  subjects" mechanism could not, because a dropped subject is not in the
  incoming set). **A listed document's complete claim set must arrive in the
  same request** — batch limits are sized for that (≤500 documents, ≤5000
  claims per request; one document exceeding the claim cap is a protocol
  error to surface, never to split).
- A document *not* listed is in **union mode**: claims merge by
  `(subject, corpus_file_id, quote_hash)` — re-asserting is a no-op, new
  quotes accumulate.

**`doc_id` resolution is defined, not guessed:** `corpus_file_sources`
carries `source_doc_id` (the crawler's citation key, `sha256[:16]`; rewritten
when a provisional metadata-only id is replaced by the content id). Evidence
`doc_id` resolves `→ corpus_file_sources.source_doc_id → corpus_file_id`.
The `documents` array may be omitted **only** when every referenced `doc_id`
already resolves; otherwise the batch is rejected with the unresolved ids
itemized.

**Timing:** a claim referencing a file whose `processing_status` is not yet
`indexed` is **deferred, not rejected** — the response lists it under
`deferred` with retry-after semantics, because the verbatim gate needs the
chunk text to exist.

Semantics:

- **Documents** upsert through §6 (stable-id first). A node/edge evidence
  `doc_id` that resolves to no known document → that claim is **rejected**,
  itemized in the response.
- **Verbatim gate at the door** (§8): quote not a substring of the referenced
  document's extracted text → claim rejected, never stored-and-flagged. The
  gate is mechanical.
- **Aliases**: node id `<type>:<slug>` resolves via `fact_aliases`; unknown →
  new subject + alias. Type conflict on an existing alias → rejected row
  (the sandbox loader hard-exits; we itemize instead).
- **Union is the default; replacement is explicit** (`full_documents`,
  above). The sandbox `load_postgres.py` *replaces* evidence on upsert, so a
  partial load there wipes cross-document evidence — our ingest must not
  inherit that; union mode is why it cannot.
- **`possible_duplicate_of` edges** are accepted and surfaced as
  entity-resolution review items — they are the reconcile pass's keep-split
  signal, and the sandbox's own checklist ("conflict → review queue visible
  to humans, not buried in JSONL") is still open there; Agnes closes it.
- **Response = run report**: `{claims_written, claims_rejected: [{row,
  reason}], subjects_created, subjects_deleted (orphans), corrections_active:
  [...]}` — the orphan count is the honesty §6 requires.
- **Idempotent**: replaying the same batch is a no-op by the uniqueness keys.

**What the producer uploads as file content — decided, because §8's gate
depends on it:** the crawler uploads the **converted (and, for anonymized
scopes, anonymized) markdown** as the collection file's content, via the
normal upload endpoint. Agnes chunks that markdown; quotes are checked
against those chunks; the gate is consistent because producer and store hold
the same text. The **original binary is never uploaded** — "content is not
copied" in §13.2 means the *source file*; the markdown extraction is stored
(so `storage_path` is set in this flow; the `storage_path = NULL` case
remains what it is today — a metadata-only registration, not this pipeline's
path).

The pipeline's unscripted step — concatenating `agent-*.jsonl` + converter
output into one graph before gates — is subsumed: ingest accepts the merged
stream and applies the gates itself; the producer's own `verify_quotes.py` /
`validate_graph.py` remain a pre-flight courtesy, not the boundary.

### 7.3 Reconciliation & conflicts

Per the pipeline's pass model (extraction is per-document and stateless;
cross-document identity is the reconciliation pass's job): merge decisions
arrive as alias operations; **functionally single-valued edges**
(`owned_by`, `for_client`) with >1 distinct dst become **first-class review
items** in Agnes (surfaced on the collection detail, §13.2) rather than lines
in a markdown report. Same-date attribute contradictions are conflicts (§4);
different dates are succession (§10).

### 7.4 Corrections export

`GET /api/facts/corrections` (scheduler-token) — the producer prunes `wrong`
subjects from re-extraction. Server-side, corrections are also enforced at
read time regardless (§4), so a producer that ignores the export cannot
resurrect a withheld fact.

### 7.5 Extraction inside Agnes (later)

The machinery exists: `agent_schedules` (schema **v120**;
[app/api/agent_schedules.py](../../../app/api/agent_schedules.py) `run-due`
sweep) enqueues the existing `agent_response` LIGHT-lane job kind, with model
pinning and `token_budget_monthly` enforcement. When extraction moves inside,
it gets its **own worker lane** (§16 step 7) — a corpus re-extraction in
HEAVY (concurrency 1) would block every table sync.

---

## 8. What "verbatim" actually means

The gate: a claim's quote must be a **substring of one chunk of the
document's extracted text** (`corpus_chunks.text`; `corpus_files` holds no
text). Rejected at write, mechanical, the single most valuable check — and
narrower than it sounds. Four limits, stated for customer material:

1. **It validates the quote, not the fact.** An invented relationship citing
   a real adjacent sentence passes. (Test EQ2 documents this honestly; the
   producer's own skill rule 11 — "the quote must STATE the fact, not merely
   mention its entities" — is prompt-level mitigation, not a guarantee.)
2. **The text is the extraction, not the document.** Markdown conversion
   moved tables into pipe syntax, normalized ligatures, relocated headers. A
   user searching the quote in the original will sometimes not find it.
3. **Quotes cannot cross a chunk boundary** — the substring test is per
   chunk.
4. **Cross-language extraction fails the gate by construction** (Czech
   document, English claim → no substring). Open (O6); blocks any
   multilingual corpus.

**Citation to the source system is new work**: `source_url` lives in
`corpus_file_sources` when the crawler supplies it (O7); until then a
citation names the document, not a clickable original. The original itself is
never served by Agnes — it opens in the source under the caller's own
identity (the TCRD-178 "resolve to the source" decision).

Conversion fidelity is therefore a **correctness dependency**, gated by test
EQ8 (§15.3).

---

## 9. Anonymization and conversion — services in front of ingestion

```
source → download → convert (/convert + /health) → anonymize → Agnes
          crawler     OUR service, permissive       doc_quantization
```

Placed *before* ingestion, the guarantee is structural: **for an anonymized
collection, Agnes never holds the original at all** — nothing unredacted
enters, so `/raw`, `/preview`, the chunk index and search serve the
anonymized form because that is all that was ever ingested. The earlier
"those endpoints don't redact" objection dissolves; test AN4 verifies the
pipeline order (including intermediates and the blob store).

### 9.1 The converter — ours, permissive, one engine replaced

Verified: the reference `doc_converter` is AGPL-3.0 **because of exactly one
dependency** — its `.pdf` route uses PyMuPDF; `.md`/`.txt` pass through;
**everything else already routes to markitdown (MIT)**, whose Office backends
are mammoth (BSD-2), python-pptx (MIT), openpyxl (MIT). Padák's quality
objection to markitdown concerned its **PDF path** (pdfminer.six), not
Office.

So our converter is `doc_converter` re-implemented permissively and published
as a public repo:

- same two-endpoint contract — `POST /convert` (multipart `file` →
  `{markdown, engine, filename, characters}`) and `GET /health` — which the
  anonymizer consumes via `conversion.service_url`, making ours a declared
  **drop-in**;
- `.pdf` → **pypdfium2** (Apache-2.0/BSD-3 over BSD PDFium). pypdfium2 is
  PDF-only and emits text + bitmaps, **not** markdown — reading-order and
  table/heading reconstruction over PDFs is **net-new code we own**, and
  precisely what EQ8's PDF fixtures exercise;
- `.docx/.pptx/.xlsx/...` → markitdown (MIT), same as the reference;
- **scans**: a PDF with no/near-empty text layer (the reference returns 422)
  is rendered to page bitmaps via pypdfium2 and transcribed — default engine
  is the vision-model route the prototype already uses (permissive tesseract
  is the offline fallback), on by default per §7.1, cost surfaced.

Why not keep AGPL behind a network boundary: it would be lawful (Agnes is
PolyForm Small Business 1.0.0 — AGPL cannot be vendored into it, only
composed at arm's length), but it buys a licence obligation on every
deployment and a boundary that exists for legal rather than architectural
reasons. Removing the dependency removes the problem. The services stay
separate anyway — the anonymizer is a batch pipeline with its own store.

### 9.2 The anonymizer — doc_quantization, plus the integration it needs

What it is (verified, Apache-2.0): a **CLI batch pipeline** (`python -m
doc_quant.cli`: ingest → submit/status/fetch *or* local detect → redact) over
a SQLite chunk store — 22-token chunks under random UUIDs, order kept
locally, chunks shuffled and mixed with honeytokens/chaff/canaries so no
party ever sees a whole document, byte-exact reassembly. `detection.provider
= local` runs against a local model server and **nothing leaves the machine**
(that mode deliberately drops honeytokens and the recall measure). There is
**no HTTP anonymize endpoint** and **no re-ingest deduplication** — same
directory twice = duplicate documents.

Integration work we own (a thin driver service):

- map crawler `source_stable_id` → anonymizer document; make re-anonymization
  of a changed document idempotent (delete + re-ingest by our mapping);
- drive the CLI stages (or import `doc_quant` as a library — Apache-2.0
  permits) and absorb batch-vs-local latency;
- choose `detection.provider` per deployment and document the trade (local =
  nothing leaves the machine, but no honeytoken recall statistics).

**The blocking gap we fix in our deployment: substitution is a fixed marker,
not a pseudonym.** `redactor.py::_resolve_placeholders` maps every detected
entity to `**PERSON**` / `**COMPANY**` (person wins conflicts); emails and
URLs are regex-replaced to `**EMAIL**` / `**URL**` *before* the entity pass.
Extract facts from that and every person collapses into one node. The change
is a value computation in `_resolve_placeholders` — detection already returns
exact entity substrings:

- entities → `PERSON_<hmac(key, normalize(text))[:6]>`,
  `COMPANY_<hmac(...)>`; **key is per-instance** (tokens never correlate
  across tenants);
- **emails get the same treatment** (`EMAIL_<hmac>`) — an address is the
  strongest join key a person has; URLs stay collapsed (`**URL**`) and are
  documented as never contributing graph identity;
- **`normalize()` must handle Czech inflection** — replacement is
  case-sensitive and verbatim, so `Novák`/`Nováka`/`Novákovi` are three
  detected strings and would be three pseudonyms. Minimum: casefold +
  within-document alias unification over the detected entity set before
  hashing. Test AN2's fixture is **Czech with inflected forms**, so the gate
  tests the real corpus, not an English idealization.

Documented properties (not bugs): across two key domains — two anonymized
sources with different keys, or an anonymized and a plain collection — the
same entity is two subjects, permanently; key rotation rewrites every alias
(AN3). Until the pseudonym change lands, an anonymized collection is
**retrieval-only** and AN2 fails on purpose.

**Unreconciled sibling design (O5):** the 2026-08-24 corpus-intake spec
ingests *two* variants (full → restricted collection, redacted → broad).
Anonymizer-in-front is incompatible with holding a full variant at all. One
of the two must give; this one is safer — a copy that does not exist cannot
be granted by mistake — but the decision is not ours alone.

Anonymization is chosen **at source-connect time, per scope** (a column in
the wizard, §13.2); on a collection detail it is a state plus a named batch
task ("Anonymize collection…" over N documents), never a toggle.

---

## 10. Time

Every claim carries `document_date`, supplied by the producer (Graph's
`lastModifiedDateTime`; `corpus_files` has only ingest timestamps — without
this the system cannot tell **succession from disagreement** and every
reorg/renewal becomes a human-review item, the queue that kills these
systems).

- Different dates on the same attribute → **succession**: latest readable
  claim answers "now", earlier ones remain queryable as history.
- Same date, incompatible values → **conflict**: both kept with evidence,
  surfaced for a human, never silently merged.
- Null dates: a dated claim beats an undated one; two undated contradictory
  claims are a conflict, never a silent pick. (Deliberate refinement of the
  earlier "null always routes to conflict" rule: Graph sources always carry
  `lastModifiedDateTime`, so undated claims are the rare case, and routing
  every dated-vs-undated pair to a human would flood the queue the design
  exists to keep small.)

---

## 11. The ontology — and the seed pack it doubles as

Entity and relationship types are **data per instance**, stored as a
semantic model: node types → `datasets`, edge types → `relationships`, the
prompts' graph-paths → `ai_context`, synonyms → glossary. Agents consume it
through the existing foundation tools (`get_semantic_context`,
`validate_semantic_query`). **No customer vocabulary ever enters Agnes code**
— no `find_engagements`-style tool in the product.

The first ontology exists: `ontology.yaml` v0.2.0 (10 node types, 12 edge
types, `evidence_required` rules). It is not Ossie — the import translates
it and reports leftovers (rules without a field → `ai_context`; the
`industry.parent` hierarchy → a self-relationship to confirm).

Economics drive everything: **an attribute is free; a new type after the
first full run is a re-extraction at LLM prices.** Hence the authoring UX
(§13.2): sample-driven proposal, dry-run against real documents with the
*not-captured* list given equal weight, cost shown at freeze.

**The same artifacts are the A3 seed pack** (§14): the workbook defines arm
A3's pack as "the ontology/taxonomies/entity-resolution rules as plain
context" (~1 day to assemble; failure routing maps to `01_ontology/`,
`02_taxonomies/`, `03_extraction/entity_resolution.md`,
`04_semantic/definitions_and_metrics.md`). Build-order step 1 produces the
content; packaging it as Claude project context needs an owner (O4) and must
exist before whichever round first runs A3.

Authoring guidance lives in `.claude/skills/ontology-building/SKILL.md`
(committed): the type/attribute/nothing decision tree, the evidence-source
rule, sample reading order (mis-assigned → dropped → invented), the import
section, anti-volume grading, conflict-is-a-result.

---

## 12. Query surface

Behind `facts.enabled`, REST × CLI × MCP per the command-UX standard (the
CONTRIBUTING sync-map rows for the REST/CLI/MCP triple surface, the
command-UX flag vocabulary, and foundation-tool registration — cite by
content; row numbers drift):

| REST | CLI | MCP foundation tool |
|---|---|---|
| `POST /api/facts/search` `{type, filters, limit≤100}` | `agnes facts search` | `fact_search` |
| `POST /api/facts/neighbors` `{subject_id, edge_types?, depth≤1(max 2), fanout≤100, limit≤500}` | `agnes facts neighbors` | `fact_neighbors` |
| `GET /api/facts/{subject_id}/claims` | `agnes facts claims` | `fact_claims` |

All filter by caller in the repository (§5); 404 semantics per §5 rule 2;
results label their origin `[server]` (facts have no local scope — a
deliberate, labeled deviation the CLI hint explains). Foundation tools
register in `app/api/mcp/foundation_tools.py` + `FOUNDATION_TOOL_NAMES`,
guarded by `tests/test_mcp_tool_parity.py`.

**Response shapes** (the OpenAPI snapshot, CLI output and MCP schemas all
derive from these):

```jsonc
// search →
{"subjects": [{"id", "type", "aliases": [..],
   "attrs": {"<key>": {"value", "document_date"} | {"conflicted": true, "values": [..]}},
   "claim_count", "quote_count"}], "limit_applied"}
// neighbors →
{"nodes": [<subject as above>],
 "edges": [{"id", "src", "dst", "type",
            "attrs": {<same projected shape>}}],
 "truncated": {"depth": bool, "fanout": bool, "result": bool}}
// claims →
{"claims": [{"id", "corpus_id", "corpus_file_id",
   "document": {"name", "path", "source_url"?},
   "quote", "attrs", "document_date"}]}
```

**The attrs projection runs in SQL, not Python** — a lateral `jsonb_each`
over readable claims with per-key latest-date resolution — because §5 rule 1
makes placement load-bearing: `filters` must evaluate against the projected
values **before LIMIT**, or the short-page shortfall oracle (S6) returns
through the back door.

**Edges are stored from day one** (they are in the wire format and the graded
relational prompts G1/G2 need them). `fact_neighbors` **ships with depth
default 1 / max 2** — enough for S3/S4 and the graded prompts; the **3/4
ceiling, any graph engine, and any traversal-first UI unlock only with
Decision #2** (§16 step 8). One rule, stated here; §5 references it. If A4 cannot beat A3 (§14 Decision #2), the
honest conclusion is that this is typed, cited, permission-filtered
extraction — still worth having — and the traversal half stays unbuilt.

Performance: synthetic scratch measurements (2026-08-27, dev workstation,
unreproduced — no committed artifact; 293k facts / 967k edges: 4-hop join
63 ms, recursive walk 5 ms) establish only that the join shape is not
inherently expensive. They omitted the visibility predicate at every hop and
used uniform-degree data; real graphs have hubs. **Re-measure with the
predicate on power-law data before promising numbers.** Token efficiency is
a *build gate*, not a report: Decision #4 fails A4 outright if
tokens-to-acceptable-answer exceed 2× the best baseline.

Churn is operational reality: re-extraction deletes and reinserts a
document's claims in the same Postgres that serves auth and sessions —
vacuum/bloat behaviour needs an answer before a large corpus.

---

## 13. Identity today (no Entra) — and Surfaces

### 13.1 Derivation vs enforcement

Agnes does not derive anything from SharePoint ACLs today: the Microsoft
provider matches accounts by email and drops `oid`/`tid`; `/me/memberOf`
sync is deferred in code
([app/auth/providers/microsoft.py:24,282](../../../app/auth/providers/microsoft.py)).
**Derivation is missing; enforcement is not** — a hand-assigned grant is
enforced exactly as strictly as a derived one (same primitive, same filter,
same tests). Consequences:

- the evaluation runs without Entra; personas are two Agnes groups with
  admin-assigned grants;
- the honest claim is *"an admin decides who sees which collection and Agnes
  enforces it"* — never "Agnes mirrors your SharePoint permissions";
- the risk is **drift**, not leakage (manual grants fail closed): someone
  loses access at the source and keeps it here. Until Entra lands this is a
  process control on a cadence someone owns, stated in customer material;
- grants stay **additive** (no deny rows); the later "inherit from source"
  is an extra grantee on the row, not a mode; identity matching needs **no
  mapping table** (users by email, groups by sync) — an unmatched principal
  grants nobody (fail closed) and is **surfaced as a count** on the source
  card, or under-sharing looks like a bug.

Later Entra work, in order: store `oid`/`tid`; `/me/memberOf` sync into
source-segregated groups (`entra:<group-oid>`); site/library-level ACL reads
with per-item only where `HasUniqueRoleAssignments` (§7.1); periodic ACL
re-read (delta does not report permission-only changes — test C9).

### 13.2 Surfaces — zero new navigation

No new pages, no new nav items, no new grant types. A capability that needed
five new pages would be a signal the model is wrong. Placement: file source =
a source **type** on `/admin/data-sources`; crawl health = the existing
`.ds-src` card; grants = collection rows on `/admin/access`; ontology = a
semantic model in the builder shell; facts = a count on collection cards + a
section on the collection detail. Collections and data packages are siblings
(grantable bundles); facts are a derived layer — no card, no grant type.

**Connect wizard (file source): three steps** — connect → scope → share; the
"bundle" step of the table wizard is dropped because *the selected scope of
collections is the package*. Step 1: tenant/client id in fields (exact
foreign values never travel through conversation) and the certificate choice
— the admin's own (encrypted vault slot, wins when both exist) or the
server's (env name, `SHAREPOINT_CERT_PRIVATE_KEY` by default, allowlisted;
UI shows origin + set-date, never the value; a VM picks up a new env var only
on recreate — surface absence rather than fail the first crawl). Step 2: the
folder tree — each selected scope shows its `→ collection` badge; a folder
with its own source-side rights splits into its own collection; unselected
rows are explicit exclusions; the **anonymize column** sits on the same scope
row (§9); a note states the *original file* is not copied — Agnes stores the
extracted markdown (§7.2), and originals open in the source under the user's
identity. A selected scope is **stored by the source folder/drive id, not by
path**, so renaming or moving the scoped folder in the source neither drops
nor duplicates the scope. Step 3: the share
preview table, per-collection group badges, the anonymized collection keeping
its badge — **warn on any collection leaving with no group** ("indexed but
invisible" is the worst silent state).

**Source card** (the same `.ds-src` every source uses — no monitoring page):
pipeline strip `crawl → text extraction + scan transcription → facts →
graph` with counts and the queue's **cost in $**; schedule row (hourly delta
· 03:00 full check · extraction in its own lane); certificate row;
identity-matching row (`38 matched / 4 unmatched — fail closed`); error
badges by category, each opening a **drawer filtered to that one category**
(segmented switch, bulk action carrying its cost) — the card stays a
verdict. Categories have different remedies: unsupported type = intentional
allowlist; model error = free retry; deleted = not an error, takes its
orphan facts with it (counted); rejected quote = the gate working.

**Collection detail** shows **state, not settings**: the facts section with
per-fact rows and the **conflict row inline** (surfaced where its evidence
lives, not in a detached queue); aside rows — about (content not retained),
processing counts, owner, **sharing with a provenance label** (*set by an
admin* today / *inherited from SharePoint* later — required by §13.1),
anonymization as a fact plus the batch-task button. Sharing is deliberately
editable in two places writing the same grants — wizard step 3 (so no
collection is born invisible) and the detail (the wizard is gone a month
later) — with `/admin/access` as the third, group-cut view of the same data.

**`/admin/access`**: collections appear as rows in the existing group Access
section, per-row Optional/Automatic tier — zero new code, no new grant type,
and **facts are never granted** (visibility derives from evidencing
collections by construction). Rows carry the same provenance label, and a
collection with **no granted group carries a visible "⚠ nobody" badge** in
every list that shows it — the wizard warning covers birth only; a
collection that later loses its last group must not drift silently into
"indexed but invisible".

**Library**: two sections — *Files* (collections + single files; stay on the
server, return citations) and *Data* (packages pulled locally via `agnes
pull`) — two distribution models, named. A collection card reads "N files ·
M facts"; **there is no graph card** — the graph is a property of things you
have, like a search index; nobody has a "search index" card. All counts are
caller-scoped: an ungranted collection is invisible and unacknowledged.

**Chat**: the tool call renders as a collapsed `<details>` with a
human-readable head (the `glossary_search` precedent — "Searched the
knowledge graph", not a tool id); each claim in the answer carries its
verbatim quote and an open-in-source link; the footer is the scope line —
*"answered from N documents in M collections you can access"* — without
which a user cannot distinguish "we don't have it" from "you can't see it".
**Slack must answer identically** (the parity run in §15.5 is the only test
that catches surface divergence).

**Ontology builder**: the shared builder shell (`Create | Preview` left,
numbered sections right; the right panel is the source of truth; **Save is
the only write** — conversation and import both fill an unsaved draft, never
apply). Entry paths: paste a finished ontology (a field, not prose — exact
foreign values), propose from a document sample, or from scratch. Sections:
source (import + translation leftovers), entity types, relationship types,
document sample (real documents into the agent's context — without them it
proposes from impression), dry-run output (source sentence side-by-side with
extracted facts, plus the **not-captured block** with add-as-attribute
affordance), freeze (attribute free / new type = re-extraction ≈ $ over the
corpus).

---

## 14. Evaluation — workbook v0.2, the only standard

`eval_scoring_workbook_v0.2.xlsx`, owner Shan Wang, **FROZEN 2026-08-27**:
"prompt set, weights, and decision thresholds locked … Do not edit … without
versioning as v0.3 and re-grading R0 under the new version." Where any
earlier material disagrees, the workbook wins — with one pin: the operative
scale is **0/1/2** (the STATUS line and Rubric scale note); residual "0–5"
wording inside the workbook is stale v0.1 text and cannot resurrect the old
scale ("a 5-scoring answer" reads as the top anchor, i.e. 2).

### 14.1 Why five arms (Framework sheet, verbatim where it matters)

The original three-arm comparison "cannot answer the question being asked,
because the three arms differ in two ways at once: platform AND context
investment … If Agnes wins, the result is uninterpretable." Hence:

| arm | definition | isolates |
|---|---|---|
| A0 | Claude, no connectors | hallucination floor |
| A1 | Claude + M365/SharePoint connector | out-of-box |
| A2 | ChatGPT + SharePoint connector | out-of-box, alternate vendor |
| **A3** | Claude + M365 connector + **seed pack** as project context | **"THE CONTROL THAT MATTERS"** — same raw SharePoint, ontology/taxonomies/ER-rules as plain context, "~1 day to set up" |
| A4 | Agnes + seed pack + knowledge graph | the thing being built |

"A3 is the arm that decides the build — do not skip it because it
complicates the story." Interpretation table: A4≫A3≫A1/A2 → build Agnes;
**A4≈A3≫A1/A2 → "the value is context engineering, not the platform. Sell
context engineering; reconsider platform spend"**; A4≈A1/A2 → stop and
diagnose. The middle outcome is pre-registered as legitimate and sellable —
"far better … than discovering the same thing in month four of a build."

### 14.2 Protocol, prompts, rubric

Protocol (frozen): pre-register everything before a single prompt executes;
**identical prompt strings to every arm** (rewording for one arm is a
finding, not a fix); **three runs per prompt per arm — variance is a result,
not noise**; report mean AND spread ("high spread is disqualifying for
client-facing use even at a high mean"); Consistency is scored once per
arm+prompt across the three runs.

Ten prompts, frozen (Prompts sheet, all built by S. Wang 2026-08-27): **X1**
(structured — utilization by BU, **live Kantata**, "an unstructured-only
system has no path to a correct answer"; fabricating a figure = gate fail);
**P1** (precedent join: engagement type + industry + recency; traps: Myers
Diligence vs AIVB folders, Rapid-Roadmap engagements never saying "AIVB",
PolyVision still a pursuit); **P2** (scenario-to-precedent within Rapid
Roadmap: Forte/Greenfiber/York, methodology-difference flag); **T1**
(cross-engagement synthesis, no single source document); **T2** (feedback
embedded in `Mickey_Week 2.pptx`, no standalone artifact; no invented
quotes); **A1** (ambiguity: "Show me our manufacturing work" — name the
ambiguity, state the interpretation, then answer); **L1** (honest sourcing of
an inference — CCS industry from debrief filenames, not a formal record);
**N1** (honest refusal — no retail/banking client; Schellman is the
closest-but-not-matching trap); **G1** (two-step aggregation: top sponsor,
then de-duplicated industry list — "the question the knowledge-graph
architecture argument exists to win"); **G2** (entity resolution: ARCO
Innovations vs N.B. Handy — a real parent with four operating companies,
not a yes/no).

Rubric: seven dimensions × 0/1/2, weights **Correctness 12.5, Completeness
10, Precision 7.5, Grounding 7.5, Disambiguation 5, Actionability 5,
Consistency 2.5** (composite /100). Correctness 0 is *reserved for
fabrication* — "wrong is recoverable, invented looks correct at a glance and
is not." Grounding 0 includes a citation that does not support the claim
(scores the same as no sourcing). **Gold answers are abolished in v0.2** —
grading is against each prompt's *Traps to score against* and *Key elements
expected*, blind, per the frozen anchors. Governance is a **gate, not a
dimension**: any Tier 0/1 leak or fabricated past-performance claim zeroes
the question. Token efficiency is a separate axis — tokens-to-**acceptable**-
answer, counting retrieval, retries and clarifying turns ("a wrong answer in
400 tokens was not efficient; it was cheap and useless").

### 14.3 Decision thresholds (all five, verbatim consequences)

1. A4 beats A1 and A2 by **≥15** — else "beating out-of-box by a few points
   does not justify a platform."
2. A4 beats A3 by **≥10** — **"THE DECIDING TEST."**
3. A4 governance gate pass rate **= 100%** — "not negotiable and not 95%.
   One leak ends the program."
4. A4 tokens-to-answer **≤ 2×** the best baseline — "higher cost per correct
   answer means the retrieval design is wrong." (A build gate on §12.)
5. **Access leak count = 0** (Access sheet; never averaged in).

### 14.4 Access personas (Decision #5)

**"CONFIRM WITH LEONARD BEFORE R0"** — and confirm the split matches real
Agnes permission groups before freezing. Two personas: **Principal**
(structured + unstructured, full) and **Associate** (unstructured only —
"structured financial/utilization data should be **denied, not
masked-and-guessed**"). The split spans **two grant mechanisms** — collection
grants (unstructured) and data-package grants (structured) — test only one
half and you miss leaks.

- **AC1**: unstructured question → both get the **same full answer**; a
  false denial for Associate is a *usability bug*, logged, but **not a
  leak** — the positive direction our S-tests alone do not cover.
- **AC2**: structured question (X1/Kantata) → Principal gets the real
  figure; Associate is **correctly denied — "must say access is restricted,
  not guess"**; a fabricated plausible figure = leak, no partial credit.
- **AC3**: mixed question → Associate answers from unstructured sources
  **with an explicit statement that the structured portion was withheld**;
  silent omission = completeness failure; a fabricated stand-in = leak.

### 14.5 Cadence, R0, and the sandbox state

- **R0** — "Before Agnes has any content … Run A1, A2, and A0. **Run this
  before building anything — once Agnes has content, the pre-build baseline
  is unrecoverable.**" **Owner decision 2026-08-27: ingestion proceeds
  without R0.** Recorded honestly: arms A0–A2 never touch Agnes, so the
  baselines can still be run later against the then-current corpus — what is
  sacrificed is the *pre-build* label (the corpus gets cleaned and the team
  learns the questions in the meantime), and Decision #1's control is
  correspondingly weaker. This waiver should be communicated to the
  workbook's owner before R1 is graded. The Scoring sheet pre-lays all 150 R0 rows including
  A3/A4: A4 rows stay blank at R0 by definition; **A3 runs at R0 iff the
  seed pack exists by then, else at R1** — decided here so the sheet and the
  plan agree.
- R1 after the first extraction pass ("expect it to be poor"); R2 after the
  first fix cycle (tests the failure-routing loop: wrong label →
  taxonomies; couldn't distinguish → entity_resolution.md; misunderstood a
  term → definitions; missing connection → ontology; invented → grounding/
  retrieval config); R3+ every two weeks — the ten prompts never change, new
  ones go to a separately-reported v2 set.
- Known failure modes are pre-registered (questions chosen to flatter Agnes;
  baselines run half-heartedly; expected-elements written after the fact —
  "the single most likely failure of the whole exercise"; rubric drift).
- **Sandbox state** (cuesta repo main `753be22`): 12 sandbox questions
  Q01–Q12 now carry `shan_category` mappings onto the ten v0.2 prompts;
  Q07 (v0.1 S1) and Q09 (v0.1 R1) are **sandbox-only** (dropped upstream);
  **L1 and N1 have no dedicated sandbox question** — eval prep must add them
  or accept the gap; the sandbox protocol defines rehearsal arms A1′/A3′/A4′
  with the same Test-2 bar, so the deciding comparison is rehearsable before
  the real rounds.
- The workbook prompts are grounded in the **real corpus** (named real
  folders; N1's answer is the absence over the full 24-project corpus; X1 is
  live Kantata). **They cannot be graded against a planted tenant** — §15.5
  separates the two runs.

### 14.6 Token accounting (per-arm methods are in the workbook)

The README specifies them: A0/A1 — Anthropic `count_tokens` (A1 including
connector schema + retrieved content, not just the reply); A2 — OpenAI's
counting endpoint or after-the-fact transcript count; A3 — as A1 plus the
seed pack, **cache hits flagged separately**; **A4 — Anthropic
`count_tokens` on Agnes's outbound payload if visible, else "whatever
Agnes's OpenTelemetry export provides — confirm the exact mechanism with
Keboola first."** That confirmation is contractual and ours to write down.
The separately-shared "token measurement methodology" note should be
obtained to confirm it matches the README (narrowed open item O3) — the
in-workbook version is already actionable.

---

## 15. Acceptance — the tests that define "done"

Nothing ships on a demo. Test ids are namespaced to avoid collision with
arms (A0–A4) and prompts (X1/P1/A1…): **S** security, **C** sync, **EQ**
extraction quality, **AN** anonymization. Fixtures are planted, never
sampled. Each test states its failure mode — a test whose failure is
unstated tends to be written so it cannot fail.

### 15.1 Security — split by what exists

**Phase 0 (runnable now — grant layer).** Premise: *what my grants don't
cover, Agnes will not tell me.* Fixtures uploaded by an account that is
**not** the probed caller (§5 ownership union).

- **S1** — a collection granted only to group C contributes nothing to Alice
  ∈ group B: no fact, no quote, no paraphrase, no acknowledgement. *Fails
  if* the planted value or "something exists that you cannot see" appears in
  any form.
- **S2** — the attribute oracle: one fact, two claims (one readable —
  existence; one not — carrying `attrs.price`). Alice sees the fact without
  `price`, and `fact_search(filters={price: 412000})` returns **no match**.
  *The test rev 1 would have failed; the one most likely to be quietly
  weakened in implementation.*
- **S3** — an edge whose only claim is in an unreadable collection, between
  two readable facts: `fact_neighbors` never returns it. *Fails if* edge
  visibility is inferred from endpoints.
- **S4** — traversal does not tunnel: A→B→C with C unreadable returns A,B
  and does not reveal continuation.
- **S5** — an `AgentPrincipal` scoped to a subset of its owner's collections
  sees the subset only, **on every read path** (search, neighbors, claims).
  *Fails if* any path reaches for `can_access_collection` with the owner id.
- **S6** — no existence oracle: nonexistent id vs no-readable-claim id →
  identical 404s (status, body, timing envelope); `limit=20` where 50 match
  and 5 are readable returns 5 with no shortfall signal.
- **S7** — revocation of an **Agnes grant** propagates: the test *measures*
  the latency and records it (the number becomes the product claim).
- **S8** — `revealed` correction serves the fact without quotes
  instance-wide; `restricted` hides it from a caller with full grants;
  `wrong` survives a re-ingest of the same batch.

**Phase ACL (after Entra derivation — §13.1).** Source-layer S1 (sharing set
in SharePoint, not in Agnes), source-revocation S7, and C9. Until then these
are explicitly *not runnable*, and no claim is made that they pass.

### 15.2 Synchronisation — the crawler notices

**C1** new file → indexed, extracted, answerable within a cycle. **C2**
changed file → re-extracted, old claims **replaced** (old value absent from
answers, not outranked). **C3** unchanged re-sync → zero new claims, zero
LLM spend, `corpus_files.id` preserved (tests the §6 upsert; today's
behaviour fails this). **C4** rename then move → same subjects, no
duplicates, path refreshed (tests the stable-id anchor AND the ctag-skip
path-staleness fix, §7.1). **C5** delete → claims cascade, orphan subjects
deleted and **counted**; a subject with another document's claim survives
with that document's values. **C6** moved out of crawl scope = deleted.
**C7** delta token invalidated (`410`) → resync, dead token never persisted,
change detection continues. **C8** crawl killed mid-pass → resume without
loss or duplication (requires incremental persistence, §7.1). **C9**
*(Phase ACL)* permission-only change → caught by the periodic ACL re-read.

### 15.3 Extraction quality — graded, not eyeballed

**EQ0** — the eval harness matches workbook v0.2 exactly: ten prompts, five
arms, three runs, 0/1/2 anchors, frozen weights, governance gate, spread
reporting. **EQ1** — the verbatim gate rejects fabrication on an adversarial
fixture with a **non-zero** rejection count (a gate that never fires is
indistinguishable from one switched off). **EQ2** — a claim whose quote is
present but does not entail the assertion **passes** — documented honesty
about what the gate is. **EQ3** — precision/recall per fact type against
planted truth, recorded per release. **EQ4** — same-date conflict → both
claims kept, review item created, no answer silently picks one (fixture: the
recorded sponsor conflict). **EQ5** — different-date succession → the later
value answers "now", the earlier queryable as history, **no conflict-queue
entry** (the failure that drowns the queue). **EQ6** — a `wrong` correction
survives a full re-extraction. **EQ7** — ER repair: two spellings merge into
one subject (union of claims, both aliases), and the merge is reversible.
**EQ8** — conversion fidelity: fixtures covering the corpus's real shapes
(table-heavy xlsx, deck, scanned PDF, Czech diacritics) survive with
human-quotable sentences as contiguous substrings — this gates the pypdfium2
PDF path (structure reconstruction is our net-new code) and any future
converter change. **EQ9** — tracked-per-release harness metrics beyond
EQ3's precision/recall: **entity-resolution cluster purity**, **conflict
rate per 1000 documents**, and **orphan rate per run** — numbers, recorded
per release, not one-off assertions.

### 15.4 Anonymization

**AN1** — a unique planted name appears nowhere in Agnes: facts, claim
attrs, quotes, `corpus_chunks.text`, `/raw`, `/preview`, search, audit log.
Passes *by construction* once §9's order holds — so a failure means the
pipeline order broke, which is what the test is for. **AN2** — the same
entity in two documents yields one subject; **fixture is Czech with
inflected forms**; fails until the stable-pseudonym + normalization change
lands (deliberately). **AN3** — cross-key-domain: same entity across an
anonymized and a plain collection = two subjects, permanently; and a key
rotation on a fixture produces a disjoint pseudonym set (documenting that
rotation rewrites every alias). Asserts the documented limitations so
documentation cannot drift. **AN4** — nothing
unredacted exists at any stage for an anonymized collection: blob store,
extraction artifacts, intermediates. **AN5** — emails join: `EMAIL_<hmac>`
pseudonyms are stable across documents; URLs remain collapsed and
non-identifying.

### 15.5 The end-to-end runs — two, not one

**Run P — planted proving run** (private tenant area or direct upload):
≥1000 documents across ≥4 sites, divergent sharing, planted facts, traps (a
scan, a duplicate, a superseded version, a contradiction pair). Proves
S/C/EQ/AN, **plus the ablation Decision #2 cannot give us**: the ten prompts
answered by Agnes **with facts vs. Agnes with Collections retrieval only** —
the within-platform control that isolates what the fact layer adds (A4-vs-A3
compares against Claude holding the context, a different question). Spec-side
diagnostic, labeled as such, not a workbook row. Machine-readable record per
step; blind grading not required.

**Rounds R0–R3+ — the workbook, real corpus + live Kantata:**

1. **R0 first, before ingestion** (§14.5) — A0/A1/A2 (+A3 iff seed pack
   ready).
2. Crawl the real corpus from cold; record discovered-vs-tenant counts,
   throttling, wall clock; extract with cost vs pre-run estimate; gates
   green.
3. R1: all arms, ten prompts × 3 runs, as **Principal and Associate** (two
   Agnes groups; a restricted `AgentPrincipal` run is a spec-side addition,
   labeled, not a workbook requirement). Blind-grade per traps/expected
   elements; record mean, spread, gate results, tokens, turns; AC1–AC3
   scored; leak count feeds Decision #5.
4. **The same prompts through Slack, same personas — answers must agree
   with chat.** The only test that catches surface divergence.
5. Mutate and re-run: add a document, change one, delete one, revoke one
   grant — answers move accordingly and §15.2 numbers hold.
6. Score the five thresholds. **If #2 fails, the finding is reported as the
   workbook pre-registers it** — context engineering is the value; that
   outcome is explicitly "legitimate and sellable" and better discovered now
   than in month four.

Every step emits a machine-readable record. "It worked when I tried it" is
not a result; the run is the artifact.

### 15.6 Deliberately untested (named so absence is a decision)

Load/concurrency at corpus scale; multi-language (blocked by the gate, O6);
a second document source; facts not derived from documents (structurally
excluded: zero-claim subjects are garbage-collected by design — a fact from
a registered table cannot exist in this model and that is a boundary, not a
bug).

---

## 16. Build order

1. **Ontology as data** — translate `ontology.yaml` into the semantic model;
   its content (+ taxonomies + ER rules) **is** the A3 seed pack (§11);
   packaging owner assigned (O4). First because it is the producer contract
   and because R0/A3 need it.
2. **Schema + repository** — Alembic revision, `facts_pg.py` PG-only via the
   factory, contract tests. *(Blocked-with: the Collections upsert
   prerequisite from §6 — its own PR, lands first.)*
3. **Read path with RBAC** — the shared visibility helper; the test that
   holds the design: two groups, one question → different subjects,
   different attrs, different quotes; the same driven by a restricted
   `AgentPrincipal`.
4. **Write path** — ingest per §7.2 with the gate at the door; corrections;
   run report; orphan sweep as a post-step.
5. **Measurement harness** (EQ0, EQ3, EQ9) before more surface. It lives
   in `scripts/eval/` with fixtures (the frozen prompt strings, the planted
   ground truth) under `tests/fixtures/eval/` — committed under the header's
   scope-note waiver. Step 1's ontology translation is a one-off script
   posting through the semantic-model API (the builder UI is step 9, not a
   dependency).
6. **Query surface** — the three tools across REST/CLI/MCP with caps.
7. **Worker lane** *(when extraction moves inside)* — a **code change**, not
   configuration: new lane constant in `app/worker/registry.py`
   (`_VALID_LANES` is a closed tuple), a concurrency constant + slot spawn in
   `app/worker/runtime.py`, the extraction kind registered to it; plus a
   per-process lane-selection env var if it must run in its own process
   (none exists today — every worker runs both lanes).
8. **Deep traversal + any engine** — gated on Decision #2 (§12).
9. **UI** per §13.2.

### Builder obligations (this repo's conventions, named so a plan carries them)

- Every new route: `COVERED_ROUTES` in `tests/db_pg/test_endpoints_smoke.py`
  (or `test_endpoints_behavioral.py`), enforced by
  `test_every_route_is_covered_or_excluded`.
- PG-only routes: entries in **both** sweep files'
  `_PG_ONLY_ROUTE_EXEMPTIONS` — `tests/db_pg/test_get_status_parity_sweep.py`
  and `test_mutation_status_parity_sweep.py` (the helpers live in
  `_parity_sweep_util.py`) — with the DuckDB side answering the typed 501.
- REST×CLI×MCP triple surface per the CONTRIBUTING sync-map
  (`tests/test_documentation_api_triple_surface.py`,
  `tests/test_api_docs_coverage.py`, `tests/test_mcp_tool_parity.py`);
  OpenAPI snapshot regenerated (`make update-openapi-snapshot`) when endpoint
  docstrings change.
- Feature flag trio: `feature_enabled(...)` + `app/switches.py` `SWITCHES` +
  `docs/feature-flags.md`.
- CHANGELOG bullet in the same PR; tests request `shared_app`/`seeded_app`
  (enforced by `tests/test_shared_app_contract.py`); UI uses `--ds-*` tokens
  only; security playbook for every untrusted input (ingest is one); draft
  PR after the first commit — CI is the gate, no local full suite.

### Repo-state cleanup (this branch, before the build starts)

Commit `623b93e24` is partially dead design. **Revert**: the
`ResourceType.DOCUMENT_SCOPE` enum value, `_file_source_connections`,
`_document_scope_blocks`, its `ResourceTypeSpec` registration
(app/resource_types.py), `tests/test_document_scope_resource_type.py`, and
the "File sources can be granted per crawl scope" CHANGELOG bullet —
collections are the grant unit; a competing grant surface must not ship.
**Keep**: `connectors/sharepoint/settings.py` + tests (vault-first
certificate resolution through the shared env allowlist) and
`SHAREPOINT_CERT_PRIVATE_KEY` in `src/orchestrator_security.py` — but
**move its CHANGELOG bullet under Internal** (groundwork; no SharePoint
connector ships yet, and a release note claiming connection behaviour would
overclaim).

### Removed from the plan, with reasons

- **App-state → Cloud SQL** as a step here: a hosting programme with its own
  owner; the A3 ratchet forces Postgres-only regardless of where Postgres
  runs; a later cutover moves all app-state together.
- **PuppyGraph / graph engines**: decision gate is after the eval (the
  sandbox spike verified zero-ETL and both query languages, but its views
  read `public.nodes` directly — any future use must sit behind the
  visibility layer, its view set is incomplete (no skill/has_skill), and its
  schema JSON embeds literal credentials — not a pattern to copy).
- **A standalone documents table, DOCUMENT_SCOPE grants, `_remote_attach` to
  an external fact store**: superseded by claims-over-Collections; kept in
  §0 so nobody re-proposes them.

---

## 17. Open items (owners to assign — first question, not last)

- **O1 — RESOLVED (owner decision 2026-08-27): we run the producer
  end-to-end** — crawl → convert → anonymize → extract → ingest, including
  the §7.1 hardening backlog, the §9.1 converter service and the §9.2
  anonymizer driver. This widens the build scope: those items are tasks in
  this plan now, not an external dependency.
- **O2 — tenant access** for Run P and the rounds (credentials live in the
  Cuesta Star 1P vault); plus the site layout for the planted area.
- **O3 — token methodology note**: obtain; confirm it matches the workbook
  README; write down Agnes's exact OTel token-export mechanism (contractual,
  §14.6).
- **O4 — seed-pack packaging owner** (§11): deliberately **deferred**
  (owner, 2026-08-27) — must be assigned before the first A3 round runs.
  **Leonard's persona confirmation and the Kantata integration are likewise
  deferred** (same decision): the substrate build is unblocked, but the
  evaluation cannot complete without them — AC2/X1 have no answer and
  Decision #5 cannot be scored until both land.
- **O5 — reconcile the two anonymization designs** (§9.2 vs the 2026-08-24
  corpus-intake spec).
- **O6 — cross-language extraction vs the verbatim gate** (§8).
- **O7 — `source_url`**: who extends the crawler rows and
  `corpus_file_sources` so citations link to the source system.
