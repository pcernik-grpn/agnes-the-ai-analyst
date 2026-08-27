# Fact graph over Collections — design

**Date:** 2026-08-27
**Status:** draft — revised after three independent reviews (architecture, RBAC,
product), then against evaluation workbook v0.2 and the anonymisation services
**Verified against:** worktree `zs/facts-scope-access`, base `d97e186a8`

## 0. What changed in this revision, and why

The first draft was reviewed three ways and was wrong in five material places.
Recording them here rather than quietly fixing them, because each correction
constrains the design:

1. **`attrs` had no provenance.** They sat on the fact, merged across every
   document that contributed. A caller who could read *one* evidence document
   received attribute values extracted from documents they could not read — and
   `fact_search`'s attribute filters turned that into an oracle they could
   binary-search. **Fixed structurally:** attributes now live on the claim, and
   what a caller sees is projected from the claims they can read (§4).
2. **Edges had no evidence and therefore no visibility rule.** The schema gave
   them no id to hang evidence on, so the only implementable rule was "visible
   when both endpoints are" — a relationship disclosed with no check of its own.
   **Fixed:** edges are first-class subjects with their own claims (§3).
3. **`corpus_files.id` is not stable.** `_replace_existing_by_path`
   ([app/api/collections.py:628](../../../app/api/collections.py)) hard-deletes
   and reinserts on re-upload at the same path — the exact pattern a document
   sync uses — so an ordinary re-sync would cascade away expensive derived
   facts. **Fixed:** §6, with a prerequisite change to Collections.
4. **Two claims were false.** `corpus_files.source_url` does not exist (so
   "citation → original: no new work" was wrong), and quotes are verbatim
   against the *extracted markdown in `corpus_chunks.text`*, not against the
   document a user opens. **Fixed:** §7 states both plainly.
5. **The enforcement primitive named was the wrong one.** `can_access_collection`
   takes a bare `user_id` and, for an `AgentPrincipal`, either crashes or
   elevates the agent to its owner's authority — its own sibling's docstring
   says so ([app/auth/access.py:654](../../../app/auth/access.py)). **Fixed:** §5.

One review's central objection is *not* fixed here, because it is not a design
error but a question this design cannot answer: **what facts buy over the
retrieval Collections already does.** It is now stated as a gate in §11 rather
than deferred to "open questions".

Three later corrections, from material that arrived after those reviews:

6. **The evaluation standard is workbook v0.2** (frozen 2026-08-27) — ten
   prompts, five arms, 0/1/2 scoring — and the deciding comparison is Agnes
   against Claude holding the same context, which nothing currently runs (§14.4).
7. **Anonymisation belongs in front of ingestion**, not as a redaction on the
   way out. Placed there, Agnes never holds the original at all, which retires
   the objection that `/raw` and `/preview` do not redact (§9).
8. **Entra is not connected**, so nothing is derived from SharePoint ACLs today.
   Derivation is missing; enforcement is not — which is why the evaluation can
   still run (§13.5).

## 1. What this is

Typed **subjects** — entities and the relationships between them — extracted
from documents, where every assertion carries the document it came from, the
verbatim sentence supporting it, and the date of that document. Built as a
layer **over Collections**, not beside it.

Agnes owns the schema; producers write into it. SharePoint is the first
document contributor, not the owner of the model.

**Explicitly out of scope:** the crawler that fills a collection, and the
extraction pass that proposes claims. Both are producers against contracts
defined here. Neither is built by this work — which is why §11 gates the build
on someone owning them.

## 2. The one decision everything follows from

**A claim is the unit.** Not a fact, not an edge: a claim is *one document
saying one thing*, and everything else is derived from the set of claims a
caller may read.

```
subject (fact or edge)  ──<  claim  >──  corpus_files  ──  collection  ──  grants
```

- Existence of a subject: it has ≥1 readable claim.
- Attribute values: merged from readable claims only.
- Quotes: from readable claims only.
- Visibility of an edge: the same rule as a fact, on its own claims.

One rule, one grain, one join. The first draft had three grains (fact
existence, fact attributes, quotes) with a rule for one of them, which is how
it leaked.

Everything Collections already tracks is reused unchanged: what documents
exist, their processing state, who may read them, and the path back to the
original.

### Consequences that are not obvious

- **This is app-state, not analytics.** Not because `corpus_files` is, but
  because per-caller filtering is impossible on a distributed parquet. `agnes
  pull` never carries it; answers come from the server. Being new app-state,
  the A3 ratchet applies: **Postgres-only repository, Alembic revision, no
  DuckDB sibling, no `src/db.py` ladder step** (frozen at 124).
- **Table access policies cannot apply.** They attach to `table_registry` rows
  reachable via `/api/query`; facts are neither. Enforcement is in the
  repository.
- **Instances still on the frozen DuckDB app-state backend do not get this
  feature.** They receive a typed `501`. That is correct per A3 and belongs in
  release notes, not in a support ticket.

## 3. Schema

Postgres only, one Alembic revision.

```
facts         id TEXT PK                 -- opaque surrogate, never derived
              type TEXT

fact_aliases  fact_id TEXT FK→facts ON DELETE CASCADE
              natural_key TEXT
              UNIQUE (type, natural_key)

edges         id TEXT PK                 -- opaque surrogate
              src TEXT FK→facts · dst TEXT FK→facts · type TEXT
              UNIQUE (src, type, dst)

claims        id TEXT PK
              fact_id TEXT FK→facts ON DELETE CASCADE      -- exactly
              edge_id TEXT FK→edges ON DELETE CASCADE      -- one of these
              CHECK ((fact_id IS NULL) <> (edge_id IS NULL))
              corpus_file_id TEXT FK→corpus_files ON DELETE CASCADE
              corpus_id TEXT                -- denormalized: the visibility join
              source_stable_id TEXT         -- the source system's own id — the anchor
              file_sha256 TEXT              -- content at claim time, see §6
              attrs JSONB                   -- what THIS document says
              quote TEXT                    -- verbatim, see §7
              document_date DATE            -- from the source, see §8

corrections   subject_kind TEXT · subject_id TEXT   -- PK
              verdict TEXT                  -- wrong | restricted | revealed
              reason TEXT · decided_by TEXT · decided_at
```

Load-bearing details:

- **`facts.id` is opaque and `fact_aliases` carries the natural key.** The first
  draft made the id *be* `hash(type + normalized_key)`, which decides entity
  resolution at write time with no way to revise it: two spellings fragment
  into two nodes, two different people who normalize alike fuse silently, and
  repairing either means rewriting primary keys across every table. One
  indirection buys the ability to merge and to split later. Many aliases may
  point at one fact; that *is* the merge.
- **`corpus_id` is denormalized onto the claim** so the visibility predicate is
  a single indexed column, not a join through `corpus_files` on every hop of a
  traversal. Index `(corpus_id, fact_id)` and `(corpus_id, edge_id)`.
- **`corrections` is never written by a producer.** Re-extraction replaces
  claims; it must not erase human judgement. Without this, the same wrong fact
  returns every run and people stop reporting anything.
- `corrections` **does not cascade** with the subject. A subject that vanishes
  because a document was briefly missing must not lose a `restricted` legal
  hold and come back unrestricted on the next run.

## 4. What a caller sees

Given the caller's readable collection set:

```
visible(subject)  :=  ∃ claim ∈ subject.claims  where claim.corpus_id ∈ readable
attrs(subject)    :=  merge over  { claim.attrs  | claim.corpus_id ∈ readable }
quotes(subject)   :=          {   claim.quote    | claim.corpus_id ∈ readable }
```

Facts and edges use the identical rule. Nothing a caller receives is ever
computed from a claim they cannot read — which is what makes the attribute
oracle impossible rather than merely unlikely.

`facts.visibility_mode = all_evidence` (default `any_evidence`) requires
*every* claim's collection to be readable. It hides strictly more **within one
ACL snapshot** — it is not a general monotonic guarantee, because both modes
inherit ACL staleness (§9).

**Admin corrections**, always with a reason written to `audit_log`:

- `wrong` — subject withheld from everyone and, unlike the others, **fed back
  to the producer** so re-extraction does not resurrect it;
- `restricted` — withheld regardless of claims (legal hold, personnel);
- `revealed` — served **without quotes** to **every authenticated caller on the
  instance**, regardless of collection grants and regardless of
  `visibility_mode`. That reach is deliberate and must be labelled that way in
  the UI; the Agnes `Admin` group short-circuits authorization anyway, so an
  override governs what *everyone else* sees.

## 5. Enforcement

**The primitive is `accessible_collection_ids(user)`
([app/auth/access.py:654](../../../app/auth/access.py)), not
`can_access_collection`.** The latter takes a bare `user_id`; handed an
`AgentPrincipal` it raises, and handed the agent's *owner* id it substitutes
the owner's full authority — including admin god-mode — for the agent's
narrowed scope. The former branches on `PRINCIPAL_TYPES` and returns the live
intersection, which is what an agent's declared scope means. Its own docstring
records why. `None` means admin: every collection.

Three rules the implementation must satisfy, each of which a reviewer found
missing:

1. **Filter in SQL, before `LIMIT`.** `WHERE claim.corpus_id = ANY(:readable)`
   is a join predicate, never a post-filter in Python — otherwise a page
   returns fewer rows than exist and the shortfall itself signals hidden
   matches.
2. **404, never 403, for a subject with no readable claim.** Ids are opaque
   (§3) but tools take them as parameters; distinguishing "does not exist" from
   "exists, not for you" is an existence oracle. Collections already made this
   choice and documented it.
3. **Traversal re-evaluates at every hop.** `fact_neighbors` may not walk from
   a visible subject into one whose claims are unreadable. Depth is capped and
   hub nodes are fanout-capped — a depth-4 walk from a high-degree node is the
   query that will hurt, and it is not the query the benchmark ran (§10).

Because the tools are not collection-scoped, the declarative
`Depends(require_collection_access(...))` gate cannot be used at the route
level. All enforcement therefore lives in the repository, in three methods
that each re-derive it — which is precisely why it needs a shared helper and a
test that drives all three through a restricted `AgentPrincipal`, not just a
dict user.

## 6. Document identity — the anchor problem

`corpus_files.id` is a fresh `cf_*` token on every `add()`
([src/repositories/corpus_files.py:78](../../../src/repositories/corpus_files.py)),
and re-upload at the same path hard-deletes the old row before inserting the
new one, **regardless of whether the content changed**
([app/api/collections.py:628](../../../app/api/collections.py)). That upsert is
documented as the path a document-sync client uses — i.e. the one our first
producer will use. Cascading from it would delete facts whose document never
went away, and the gap would persist until the (expensive, concurrency-1)
extraction pass ran again.

For chunks this churn is harmless: re-embedding is cheap. For claims it is
destructive: re-extraction costs an LLM pass. **The asymmetry is the finding.**

**Prerequisite (a change to Collections, not to this feature):** the path
upsert must preserve the row when the content hash is unchanged — update in
place instead of delete-and-insert. This is a bug fix on its own terms; it also
stops a pointless re-embedding of every chunk on a no-op re-sync.

**Belt and braces:** `claims.file_sha256` records the content the claim was
made against. If an id is lost anyway, claims are recoverable by
`(corpus_id, sha256)` rather than silently destroyed; and a claim whose stored
hash no longer matches the current file is stale by definition and is dropped
on the next pass whether or not the id survived.

Lifecycle, then:

- **Content unchanged, row re-synced** → claims survive (prerequisite above).
- **Content changed** → hash differs → claims for that document are replaced;
  the quote may no longer be in the text, so keeping them would be wrong.
- **Document deleted** → claims cascade. A subject left with zero claims is
  deleted in the same pass and **counted in the run report** — not silently.
  Its `corrections` row survives (§3).

Orphan collection runs as its own step after ingest, not inside the ingest
transaction: a bundle re-ingest that removes many files must not hold a long
transaction on the hot path of an app-state database that also serves auth and
sessions.

## 7. What "verbatim" actually means

The gate: a claim whose `quote` is not a substring of the referenced document's
extracted text is **rejected at write time**. Mechanical, not a model's
opinion. It is the single most valuable check in the system and it is narrower
than it sounds. State all four limits in customer material:

- **It validates the quote, not the fact.** A model that invents a relationship
  and cites a real adjacent sentence passes cleanly. The gate kills fabricated
  *quotes*, not fabricated *inferences*.
- **The text is the extraction, not the document.** Documents are converted to
  markdown and stored chunked in `corpus_chunks.text`; `corpus_files` holds no
  text. Tables became pipe syntax, ligatures and hyphenation were normalized,
  headers moved. A user who searches the quote in the original PDF will
  sometimes not find it.
- **Quotes cannot cross a chunk boundary**, because the substring test is
  against chunk text.
- **Cross-language extraction fails the gate by construction.** A Czech
  document yielding English claims produces no quote that is a substring of its
  source. Either extraction stays in the document's language, or the gate needs
  a different formulation — this is unresolved and blocks any multilingual
  corpus.

**Citation back to the original is new work, not free.** `corpus_files` has no
`source_url` column — it is proposed in the corpus-intake design and unbuilt.
Until it exists there is no link from a quote to the document in its source
system, which is half the value of a citation.

## 8. Time

Every claim carries `document_date`, supplied by the producer (a crawler has it
— Graph returns `lastModifiedDateTime`). `corpus_files` has only ingest
timestamps, so without this the system cannot tell **succession from
disagreement**, and every ordinary organizational change — a renewal, a
reorganization, a role change — becomes a human-review item. Conflict-queue
volume is the most common operational reason these systems get abandoned.

With it:

- Claims about the same attribute from documents of different dates are
  **succession**: the latest readable claim wins, earlier ones remain queryable
  as history.
- Claims from documents of the **same** date that disagree are a genuine
  conflict: both kept with their evidence, surfaced for a human, never silently
  merged.

"Who is the sponsor *now*" has an answer. "Who was the sponsor in 2024" also
has one. Neither did in the first draft.

## 9. Anonymization — a service in front of ingestion

Anonymization runs **before anything reaches Agnes**, as a service in the
crawler's path:

```
SharePoint  →  download  →  convert to markdown  →  anonymize  →  Agnes
                            (our converter,        (doc_quantization,
                             pypdfium2)             Apache-2.0)
```

The consequence is stronger than a redaction rule, and it is why this
placement is right: **Agnes never holds the original at all.** For a collection
marked anonymized there is nothing to redact on the way out, because nothing
unredacted ever came in. That removes the objection that `/preview`, `/raw`
and the chunk index do not redact — they serve what was ingested, and what was
ingested is the anonymized form. The original stays in the source system under
its own ACL, which is where it belongs.

It also makes the verbatim gate consistent rather than awkward: claims are
extracted from anonymized text, so a quote is verbatim against the text a
reader will actually be shown.

**The anonymiser.** `padak/doc_quantization` (Apache-2.0) is a
decontextualisation pipeline — markdown split into 22-token chunks under random
UUIDs, ordering kept locally, chunks sent shuffled and context-free mixed with
honeytokens and chaff so no party ever sees a whole document, byte-exact
reassembly, and a fully local detection mode where nothing leaves the machine.
We run our own deployment of it.

**The converter is ours, and permissively licensed.** The reference
implementation (`padak/doc_converter`) uses PyMuPDF, which is AGPL — and Agnes
ships under PolyForm Small Business, which AGPL cannot mix with. Rather than
manage that boundary, we build the converter on **pypdfium2** (Apache-2.0 /
BSD-3-Clause, wrapping BSD-licensed PDFium) and publish it as its own public
repository under a permissive licence.

This is worth stating as a decision rather than a detail: the AGPL route was
available and would have worked behind a network boundary, but it costs a
licence obligation on every deployment and a boundary that exists for legal
rather than architectural reasons. Choosing a permissive converter removes
both. The services stay separate for operational reasons — the anonymiser runs
a batch pipeline with its own store and scales differently — not because a
licence forces it.

The trade is conversion quality, and it is a **correctness** dependency rather
than a cosmetic one: quotes are verbatim against converted text, so a converter
that mangles tables or drops structure silently degrades the verbatim gate.
§14.5 tests it.

**One blocking gap: the substitution is not a pseudonym.** The anonymiser
replaces persons with `**PERSON**` and companies with `**COMPANY**` — fixed
markers. Extract facts from that and every person collapses into one node,
every company into another; the graph over an anonymized collection ceases to
exist. Since we run our own deployment, the fix is ours to make and it is
small: replace the fixed marker with `PERSON_<hmac(key, normalized_text)[:6]>`.
The detector already returns exact entity substrings, so this is a
service-layer substitution step, not a change to detection. The key is
per-instance, which also means tokens never correlate across tenants.

With stable pseudonyms the graph joins within one key domain.

Limits to state rather than discover:

- **The claim is "Agnes serves only the anonymized form", never "the document
  is anonymous".** The original is untouched in the source system.
- **Across key domains the same entity is two nodes.** An anonymized and a
  plain collection, or two anonymized sources with different keys, never join.
  Key rotation rewrites every alias. This is a documented property, not a bug
  to fix later.
- **It contradicts the corpus-intake design**, which ingests *two* variants —
  full into a restricted collection, redacted into a broad one. Placing the
  anonymiser in front of ingestion is incompatible with holding a full variant
  at all, so one of the two designs has to give. This one is the safer of the
  two, because a copy that does not exist cannot be granted by mistake.

## 10. Query and cost

Traversal is SQL: recursive CTEs over indexed columns. Measured on this
machine against synthetic data (293k facts / 967k edges): four-hop join 63 ms,
recursive hierarchy walk 5 ms, variable-length walk to depth 4 over an attached
Postgres 9 ms.

**Those numbers do not transfer, and the spec should not lean on them.** The
benchmark omitted the visibility predicate that every real query carries at
every hop; synthetic graphs have uniform degree where real ones have hubs, and
a depth-4 walk from a hub is the query that explodes; density was ~3 facts per
document where LLM extraction typically yields tens; and it was one warm query
with no concurrency. What they establish is only that *the join shape is not
inherently expensive* — not that this will hold at scale. Re-measure with the
predicate, on power-law data, before promising anything.

Tools (REST + CLI + MCP per the command-UX standard):
`fact_search(type, filters, limit)`, `fact_neighbors(subject_id, edge_types,
depth)`, `fact_claims(subject_id)`. Every one filters by caller in the
repository. **No general SQL escape hatch** over these tables — an unfiltered
path defeats every rule above.

Churn is a real operational concern: re-extraction deletes and reinserts every
claim for a document, continuously, in the same Postgres that serves auth,
sessions and audit. Partitioning, retention and vacuum behaviour need an answer
before a large corpus, not after.

## 11. The gate this design cannot pass on its own

Collections already do: extract → chunk → hybrid retrieval → cited answers,
with RBAC. **This spec never states what facts buy over that**, and the
head-to-head evaluation that would show it is unbuilt.

So: **the evaluation is a precondition of the edge half, not a follow-up.**

- Steps 1–4 below (subjects, claims, the gate, filtered read) are defensible
  without it: they are typed, cited, permission-filtered extraction, which is
  a real capability and measurably different from retrieval.
- **Edges and traversal are not.** They are the expensive half and their
  justification is entirely "multi-hop questions", so they wait until one
  question is demonstrated that multi-hop answers and hybrid retrieval does
  not. If none is, the honest conclusion is that this is structured extraction,
  not a graph — which is still worth having.

Quality must be measured, not asserted. The corpus-intake design already set
this bar for the same subsystem — planted ground truth, a labelled question
set, precision tracked per release — and this design inherits it: extraction
precision/recall per type, entity-resolution cluster purity, conflict rate per
1000 documents, orphan rate per run, and answer quality with facts versus
without, on the same questions.

## 12. Build order

The first draft opened with "move app-state to managed Postgres". That is a
hosting programme with its own owner and timeline, the A3 ratchet forces
Postgres-only regardless of where Postgres runs, and a later cutover moves all
app-state together. It is removed; nothing here should be blocked by it.

1. **Ontology as a flat type list.** It is the producer contract — writing
   claims against an implicit vocabulary means reverse-engineering it later, at
   re-extraction prices. Even hand-written, it comes first.
2. **Schema + repository** — `facts_pg.py`, Alembic revision, contract tests.
3. **Read path with RBAC.** The test that holds the design: two groups, one
   question, different subjects, different attributes, different quotes — and
   the same test driven by a restricted `AgentPrincipal`, not only a dict user.
4. **Write path** — producer contract, verbatim gate at the door, the
   Collections upsert prerequisite from §6 landed first.
5. **Measurement harness** (§11). Before more surface, not after.
6. **Own worker lane for extraction.** HEAVY runs at concurrency 1; a corpus
   re-extraction there blocks every table sync. The queue already claims by
   kind, so this is configuration.
7. **Edges + traversal** — gated on §11.
8. **Ontology authoring UI, collection detail, conflict surfacing.**

## 13. Obligations from the customer workbook (v0.2)

Four commitments the workbook creates that this design must absorb. Two are
time-critical, and the first one **reorders the work**.

### 13.1 R0 must run before ingestion — and ingestion is imminent

The workbook: *"Run this before building anything — once Agnes has content, the
pre-build baseline is unrecoverable."* R0 needs only arms A0/A1/A2 and does not
involve Agnes at all.

The crawler is about to fill Agnes with the real corpus. **Once it does, the
baseline cannot be reconstructed** — not by rolling back, not by exporting,
not by asking people to forget. This is the one step in the whole programme
with a genuinely irreversible ordering constraint, and everything in §12's
build order is reversible by comparison.

**Consequence: R0 runs first, this week, before any ingestion into the target
instance.** It blocks nothing else — it needs no Agnes — so the only way to
lose it is to let ingestion start while nobody scheduled it. Raise it at the
daily; treat a completed R0 as a precondition on the crawler's first
production run, not as a parallel workstream.

Without it, §11's gate ("does the graph beat retrieval") loses its control
group, and §14.6's final step compares against a baseline that no longer
exists.

### 13.2 Access personas are now a graded test, not an internal concern

Two personas need confirmation before R0, and Agnes must enforce the split:

| persona | structured data | unstructured (documents, facts) |
|---|---|---|
| **Principal** | yes | yes |
| **Associate** | **no** | yes |

**Leak count = 0 is Decision Test 5** in the customer's rubric — i.e. this is
scored, not assumed.

The split runs across *two different grant mechanisms*, which is worth stating
because it is easy to test only half: unstructured reach is collection grants
(§5), structured reach is **data package** grants. An Associate is a user with
collection grants and no data package grants. Both halves must be exercised —
a persona that cannot reach tables but can reach a fact *derived from* a table
would be a leak the collection-side tests would never catch, and it is one more
reason facts stay document-derived only (§14.7).

§14.1's S1–S7 are the implementation of Decision Test 5.

The workbook's own Access sheet defines three rows, and AC2 and AC3 are
sharper than a leak count suggests. **AC2**: an Associate asked a structured
question must be *correctly denied* — "inventing a plausible-looking number
here is scored the same as a Tier 0/1 leak: any fabricated structured figure
= FAIL, no partial credit". **AC3**: on a mixed question an Associate must
answer from unstructured sources *and say the structured part was withheld* —
a silent omission is a completeness failure, a fabricated stand-in is a leak.
So the tests are not only "did anything leak" but "did it refuse in the right
way", which is a behaviour our security tests do not currently assert. They should be
renamed to the personas so results map onto the customer's sheet without
translation, and the run must record a leak count of exactly zero rather than
"no failures observed".

### 13.3 X1 now sources from Kantata — new, unscoped integration

Per the 26 Aug guidance, X1 (utilisation by business unit) is no longer
out-of-scope structured data: it sources from **Kantata** time tracking, and it
feeds both X1 and the AC2 access test.

Nobody has scoped this. It is a live structured-data integration, which means:
a connection, registered tables, a data package, and grants — the whole
structured path, none of which this design touches. Two things follow:

- It is **not** in scope here and should not be smuggled in. This design covers
  document-derived facts; a Kantata table is the other lane.
- But it **is** a dependency of the evaluation, because X1 is one of the graded
  questions and AC2 tests access against it. So the eval cannot be completed
  without it, and whoever owns the eval owns getting it scoped.

### 13.4 Observability is now a customer requirement

The workbook asks Keboola to confirm **Agnes's OTel token-export method**. That
moves token accounting from internal hygiene to a contractual answer, and it
lands on the same numbers §14.6 already requires (tokens and cost per run,
against a pre-run estimate).

Two gaps: the export method itself has to be confirmed and written down, not
described in a meeting; and the workbook references a separate **token
measurement methodology** note that has not reached us. Until it does, our
numbers and theirs may not be measuring the same thing — which is the kind of
disagreement that surfaces at the worst moment. Ask for it now.

### 13.5 Entra is not connected — what that means today

**Agnes does not derive permissions from SharePoint.** The Microsoft provider
authenticates and nothing more: it matches accounts by email, drops the Entra
`oid`/`tid`, and `/me/memberOf` group sync is deferred in code. So every
statement in §4 about ACLs, inheritance and `HasUniqueRoleAssignments`
describes a **later** capability, not a current one.

What holds today, and what does not:

| | today | after Entra |
|---|---|---|
| who may read a collection | **admin sets it by hand** | derived from the source ACL, admin overrides |
| person ↔ SharePoint identity | matched by email | matched by `oid` |
| Agnes group ↔ Entra group | none | synced |
| a file's sharing changes | Agnes does not notice | picked up by the periodic ACL read |
| enforcement of what a caller sees | **already correct** | unchanged |

The last row is the one that matters: **enforcement and derivation are
different problems, and only derivation is missing.** A grant assigned by hand
is enforced exactly as strictly as a grant derived from a source ACL — same
`accessible_collection_ids`, same repository filter, same tests. Nothing about
§14.1's security tests depends on Entra.

**So the evaluation can run without it**, and the workbook's Principal /
Associate personas are two Agnes users in two Agnes groups with grants an admin
assigned. AC1–AC3 test whether the split is *enforced*, which is precisely the
half we have.

Two obligations follow, and both are about not overclaiming:

- **Do not say "Agnes mirrors your SharePoint permissions."** It does not yet.
  The true sentence is "an admin decides who sees which collection, and Agnes
  enforces that decision" — which is a weaker promise and an honest one.
- **Manual grants must be visibly manual.** A collection whose audience was
  hand-assigned should say so, so nobody later assumes it tracks the source.
  The same UI row that will one day read *inherited from SharePoint* reads
  *set by an admin* today.

The risk of the gap is not a leak — hand-assigned grants fail closed. It is
**drift**: someone loses access in SharePoint, keeps it in Agnes, and nobody
notices because nothing was ever watching. Until Entra lands, that is a
process control (re-check on a cadence someone owns), not a technical one, and
it belongs in the customer material rather than in a footnote.

## 14. Acceptance — the tests that define "done"

**Nothing here ships on a demo. It ships when these pass against a real
SharePoint tenant, with a recorded, graded run.** Test names are the contract;
each states what it plants, what it does, and what failure looks like — because
a test whose failure mode is unstated tends to be written so it cannot fail.

Fixtures are planted, never sampled: the corpus contains documents whose facts
we know, so "correct" is decidable. The corpus-intake design's ground-truth
approach is reused rather than reinvented.

### 14.1 Security — the source's sharing decides

The premise a customer is buying: *what I cannot open in SharePoint, Agnes will
not tell me.*

**S1 — a document not shared to me contributes nothing.** Plant `secret.docx`
in a SharePoint folder shared only with Bob, containing a unique fact
("Project Kestrel budget is $412,000"). Alice asks the question the fact
answers. Expect: no fact, no quote, no paraphrase, no acknowledgement it
exists. *Fails if* the number, the project name, or "I found something you
cannot see" appears in any form.

**S2 — a group boundary holds.** Alice ∈ group B, collection granted to group C
only. Same shape as S1 at the grant layer rather than the source layer. *Fails
if* Alice reaches any subject whose every claim sits in C.

**S3 — the attribute leak.** One fact with two claims: one from a document
Alice reads (establishing it exists), one from a document she cannot
(carrying `attrs.price`). Alice sees the fact **without** `price`. Then Alice
calls `fact_search(type=…, filters={price: 412000})`. Expect **no match** —
the filter must not confirm a value she cannot read. *This is the test the
first design would have failed*, and it is the one most likely to be quietly
weakened during implementation.

**S4 — edges are not a side door.** An edge whose only claim is in a restricted
document, between two facts Alice can see. `fact_neighbors` from either
endpoint must not return it. *Fails if* edge visibility is inferred from
endpoints.

**S5 — traversal does not tunnel.** A path A→B→C where B is readable and C is
not. Depth-3 traversal from A returns A and B, never C, and does not reveal
that the path continues.

**S6 — an agent cannot exceed its scope.** An agent whose `connections_mode` is
`selected` over a strict subset of its owner's collections, run via its PAT.
It must see the subset only. *Fails if* it sees anything its owner can see —
which is what happens if the implementation reaches for
`can_access_collection` with the owner's id instead of
`accessible_collection_ids(principal)`. Run every read path through this, not
just one.

**S7 — no existence oracle.** Request a subject id that (a) does not exist and
(b) exists with no readable claim. Both return 404, indistinguishable in
status, body, and timing envelope. And `fact_search(limit=20)` where 50 match
but only 5 are readable returns 5 **without** signalling that 45 were withheld.

**S8 — revocation propagates, and we know how fast.** Remove Alice's SharePoint
access; measure how long until facts and quotes stop reaching her. The number
is the product claim ("permissions converge within N"), so the test **records**
it rather than asserting a threshold someone invented. Separately: she must
never be able to open the original, at any point — the source enforces that
live.

### 14.2 Synchronisation — the crawler notices

**C1 — a new file is picked up.** Upload to a crawled folder; within one cycle
it is indexed, extracted, and its facts answerable.

**C2 — a changed file is re-processed.** Edit the file's content. The crawler
must notice, re-extract, and **replace** the old claims — not accumulate both.
Verify the old value is gone from answers, not merely outranked.

**C3 — an unchanged file is not re-processed.** Re-run with no changes: zero
new claims, zero LLM spend. *Fails if* the ordinary re-sync path churns
`corpus_files.id` and re-extracts — the finding from §6, tested rather than
assumed.

**C4 — rename and move preserve identity.** Rename the file, then move it to
another crawled folder. Facts survive with the same subject ids and do not
duplicate. This is what `stable_id` (the Graph item id, already the crawler's
delta key) is for; the test proves the anchor actually holds.

**C5 — deletion propagates.** Delete the file. Its claims go; subjects left
with zero claims go with them and are **counted in the run report**. A subject
that still has another document's claim survives with that document's values
only.

**C6 — a moved-out-of-scope file is treated as deleted.** Move a file to a
folder that is not crawled. Same expectation as C5 — otherwise scope
reductions leak indefinitely.

**C7 — a stale delta token recovers.** Force Graph to invalidate the token
(or simulate its `410`). The crawler must resync rather than die, and must not
persist the dead token — the failure that would otherwise stop all change
detection permanently and silently.

**C8 — a crawl interrupted mid-run resumes without loss or duplication.** Kill
the process mid-pass; restart. No document is skipped, none is extracted twice.

**C9 — permissions change without the file changing.** Alter sharing in
SharePoint, touch nothing else. Delta reports nothing, so this must be caught
by the periodic ACL re-read. *Fails if* nothing ever notices — the known gap in
§9, which the test exists to bound rather than hide.

### 14.3 Crawler completeness — against what was actually specified

Verified against TCRD-184 and the reference implementation, so the port does
not silently drop capability:

| requirement | status in the prototype | port must keep |
|---|---|---|
| `/sites/getAllSites`, not `/sites?search=*` | done — search returns a fraction for app-only tokens | yes, with a test that counts sites |
| sites → drives → `/delta`, `deltaLink` persisted | done | yes |
| stable item id as the delta key | done (`stable_id`) | yes — and it becomes the claim anchor |
| `Retry-After` honoured on 429 | done | plus a bounded attempt count and a ceiling on the wait |
| full delta at most once/day | done | yes |
| metadata-only index, no content stored | done | yes |
| repair pass for failed extractions | done | yes — delta never revisits an unchanged file |
| **webhook subscriptions** | **not built** | required, with renewal before expiry |
| **`410` resync** | **not built** | required — C7 |
| **token refresh mid-crawl** | **not built** | required; a large crawl outruns a one-hour token |
| **retry on 503/504** | **not built** | required; Graph sheds load with these too |
| **incremental persistence** | **not built** | required — C8 |
| **OCR for scans** | flag-gated (`--vision`) | **on by default**; it is a cost decision, not a capability toggle |
| **per-item ACL where inheritance breaks** | not built | required for §14.1 to mean anything |

### 14.4 Extraction quality — graded, not eyeballed

**Q1 — the verbatim gate rejects fabrication.** Plant a document that tempts
paraphrase (a table whose caption almost states a fact). Any claim whose quote
is not a substring of the extracted text is rejected at write time. Assert the
rejection count is non-zero on an adversarial fixture — a gate that never fires
is indistinguishable from a gate that is switched off.

**Q2 — the gate's limit is documented by a test.** A claim whose quote *is*
present but whose assertion does not follow from it **passes**. This test
exists to keep everyone honest about what the gate does: it validates quotes,
not inferences.

**Q0 — we validate against the frozen workbook, and only that.** Evaluation
scoring workbook **v0.2**, frozen 2026-08-27, is the standard: ten prompts
(X1, P1, P2, T1, T2, A1, L1, N1, G1, G2), five arms, three runs each, seven
dimensions scored 0/1/2 with the weights in its Rubric sheet, and the five
pre-registered decision thresholds. Nothing is graded against an internal
variant — where earlier material disagrees, the workbook wins.

The arm that matters most is one we do not currently run: **A4 versus A3** —
Agnes against Claude *holding the same context* (the seed pack), by ≥10
points. The workbook calls it "THE DECIDING TEST" and states the consequence
plainly: if Agnes cannot beat Claude given the same context, the value is
context engineering rather than the platform. Standing that arm up is part of
the work, not a comparison to defer.

**Q3 — recall against planted truth.** Precision and recall per fact type on
the labelled corpus. Recorded per release, not asserted once.

**Q4 — conflict is surfaced, not merged.** Two documents, same date,
incompatible values. Both claims survive, the conflict appears for a human,
and no answer silently picks one. The recorded Kohlberg-vs-Jordan-Company case
is the fixture.

**Q5 — succession is not a conflict.** Same shape, different document dates.
The later value answers "now"; the earlier remains queryable as history. *Fails
if* it lands in the conflict queue — the failure mode that drowns the queue and
gets these systems abandoned.

**Q6 — human correction survives re-extraction.** Mark a fact wrong, re-run the
full pass, verify it does not return. *Fails if* the producer overwrites
`corrections`.

**Q7 — entity resolution is repairable.** Two spellings of one company produce
two subjects; merge them; verify a single subject with both aliases and the
union of claims, and that the merge is reversible.

### 14.5 Anonymization

The anonymiser runs in front of ingestion (§9), so these tests assert an
absence in Agnes rather than a redaction on the way out.

**A1 — the planted name appears nowhere in Agnes.** Plant a unique token in a
document destined for an anonymized collection. Assert its absence in facts,
claim attributes, quotes, `corpus_chunks.text`, `GET /files/{id}/raw`, `GET
/files/{id}/preview`, search results, and the audit log. Unlike the earlier
draft, these should now pass by construction — the unredacted form never
entered — so a failure means the pipeline order was broken, which is exactly
what the test is for.

**A2 — the graph still joins.** The same entity in two documents yields one
subject. **Fails against the anonymiser as it stands today**, which substitutes
fixed markers, and passes only once the stable-pseudonym change lands (§9).
Written to fail now, deliberately: it is the gate on that work.

**A3 — cross-key reality is documented, not fixed.** The same entity in an
anonymized and a plain collection is two subjects, permanently. The test
asserts the documented behaviour so the documentation cannot drift from it.

**A4 — the original never reaches Agnes.** Inspect the blob store and the
extracted text for an anonymized collection after a full run: nothing
unredacted is present, at any stage, including intermediates. This is the test
that makes §9's guarantee checkable rather than architectural.

**A5 — conversion fidelity, because the verbatim gate depends on it.** Convert
a fixture set covering the corpus's real shapes — a table-heavy xlsx, a deck, a
scan, a document with Czech diacritics — and assert that sentences a human
would quote survive as contiguous substrings. A converter change is a change to
what quotes are possible, so this test guards the switch to pypdfium2 and any
future one.

### 14.6 The end-to-end run — what "done" actually means

One scripted run, recorded, repeatable, against the real tenant. Not a demo.

1. **Plant** the corpus in SharePoint: ≥1000 documents, ≥4 sites, deliberately
   divergent sharing, planted facts with known answers, and the traps —
   a scan, a duplicate, a superseded version, a document contradicting another.
2. **Crawl** from cold. Record: documents discovered vs. tenant count, sites
   found, wall-clock, throttling events.
3. **Extract.** Record: claims written, claims rejected by the gate, failures
   by category, tokens and cost against the pre-run estimate.
4. **Gate on quality** (§14.4). A run that produces facts nobody graded is not
   a passing run.
5. **Ask in chat**, as three different people with different access, the twelve
   evaluation questions. Grade blind against the frozen gold answers, using the
   agreed rubric. **Record per-person answers** — the security tests above prove
   the negative; this proves the positive is still correct for someone who has
   access.
6. **Ask the same questions through Slack**, same people. Answers must agree
   with the chat run — a surface that answers differently is a bug, and this is
   the only test that catches it.
7. **Mutate and re-run**: add a document, change one, delete one, revoke one
   person's access. Re-run the questions. Answers must move accordingly, and
   §14.2's numbers must hold.
8. **Compare against no graph.** The same twelve questions answered with
   Collections' existing hybrid retrieval alone. This is the gate from §11: if
   the graph does not win where it is supposed to, that result is the finding,
   and it is more valuable than shipping.

Every step emits a machine-readable record. "It worked when I tried it" is not
a result; the run is the artefact.

### 14.7 What is deliberately not tested yet

Named so their absence is a decision rather than an oversight: load and
concurrency at corpus scale, multi-language extraction (blocked by the gate,
§7), a second document source, and facts not derived from documents. Each needs
its own design before it can have a test.

## 15. Open questions

- **Who writes the producer.** Neither crawler nor extraction pass is built
  here, and a store with no producer holds nothing. This is the first question,
  not the last.
- **`/preview` and `/raw` under anonymization** (§9) — in scope or the
  guarantee is void.
- **Reconciling the two anonymization designs** (§9).
- **Cross-language extraction** versus the verbatim gate (§7).
- **`source_url`** — who builds it, since citations are half-blind without it.
- Whether extraction runs as a scheduled agent, and who owns its token budget.
- **The anonymiser substitutes a fixed marker, not a stable pseudonym.** With
  `**PERSON**` / `**COMPANY**` every person collapses into one node and every
  company into another, so an anonymized collection cannot carry a graph. The
  fix is ours to make in our own deployment — replace the marker with
  `PERSON_<hmac(key, normalized_text)[:6]>`, a service-layer step, since
  detection already returns exact entity substrings. Until it lands, an
  anonymized collection is retrieval-only, and §14.5's A2 fails on purpose.
