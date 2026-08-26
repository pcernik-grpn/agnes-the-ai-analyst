# Unstructured Corpus Intake — Design

**Date:** 2026-08-24
**Status:** Draft for review
**Verified against:** `0.84.8` (HEAD `3a73ef3ba`)

## 1. Context & goal

Organizations keep 10³–10⁵ unstructured documents (docx, pptx, pdf, spreadsheets)
in enterprise file stores — SharePoint/OneDrive, Google Drive, S3 and the like.
Agnes already has a Collections subsystem (upload → extract → chunk → hybrid
retrieval with citations, RBAC per collection), but no story for getting a
*file-store corpus* into it: at what scale, through what transport, with what
sanitisation, and without turning Agnes into a document management system.

**Goal:** a repeatable intake pipeline from any enterprise file store into
Agnes that is

1. **tiered** — index everything's *metadata*, ingest only the curated
   fraction's *content*, fetch the long tail on demand;
2. **governed** — an anonymized (redacted) twin of the corpus with its own
   grants, produced *before* anything is indexed;
3. **incremental** — re-runs converge; deletes propagate; no full re-crawls;
4. **measurable** — retrieval quality is scored against planted ground truth,
   not eyeballed.

**Non-goals:** Agnes as a DMS or file browser; mirroring the source system's
full tree; per-document ACL sync from the source (v1 is tier-level grants);
knowledge-graph construction. The source file store remains the system of
record; Agnes holds the index, the metadata map, and the redacted tier.

This matches how the large assistants are built (M365 Copilot, Vertex AI
Search, Glean): the corpus itself is never displayed — users see answers with
citations that deep-link back into the source system, and authorization is
enforced at query time, not by curating what gets indexed.

## 2. Design overview — three tiers

```
                 ┌──────────────────────────────────────────────┐
                 │  Source file store (system of record)        │
                 └───────┬───────────────────┬──────────────────┘
                enumerate│ (all)        fetch│ (selected)
                         ▼                   ▼
                 T2 metadata map      extraction worker
                 (registered table)   fetch → anonymize → convert
                         │                   │
                         │            zip batches (paths preserved)
                         │                   ▼
                         │            T1 Collections ×2
                         │            full (restricted) / redacted (broad)
                         ▼                   ▼
                 SQL via chat/agents  knowledge search + citations
                                      (citation deep-links via source_url)
```

- **T2 — metadata map, every document, no content.** One row per file: path,
  name, size, mtime, content hash/etag, source URL, mined metadata (top-level
  folder → client/project, filename conventions → doc type/version). Lands as
  a regular Agnes table; agents answer "what exists" questions over it in SQL.
  Legally near-zero (no content), built in minutes, and it is where curation
  decisions are made.
- **T1 — curated golden corpus, content ingested.** The high-value fraction
  (final deliverables, templates, reference material — typically hundreds of
  documents, not thousands). Ingested twice: the **full** variant into a
  restricted collection, the **redacted** variant into a broadly-granted one.
- **T3 — fetch on demand.** Documents outside T1 discovered via T2 are pulled
  ad hoc (and optionally promoted into T1). No standing copy.

Ingest-everything is explicitly rejected: consulting-style trees are dominated
by drafts, versions and near-duplicates that *degrade* retrieval (a superseded
draft outranking the final is the characteristic failure), and every ingested
document is legal surface.

## 3. Extraction worker

A standalone sidecar process (phase 1: operator-run CLI; phase 3: product
connector). It touches Agnes **only through existing public APIs** — no new
server surface is required for v1.

### 3.1 Stages

| stage | contract |
|---|---|
| **enumerate** | list every file (id, path, size, mtime, etag/hash, source URL). Write/refresh T2 rows *immediately*, before any content is fetched. |
| **select** | apply tier rules over T2 (folder allowlist, doc-type/version filters, legal allowlist) → the T1 candidate set. |
| **fetch** | download bytes for T1 candidates whose hash/etag changed since the worker's cursor. Prefer direct/pre-signed download URLs over inline base64 payloads. |
| **anonymize** | detect → deterministic pseudonymization (§4) → emit `full` and `redacted` text variants. |
| **convert** | layout-aware extraction to Markdown as the canonical text representation (Docling-class tooling; same family Agnes uses server-side). |
| **package** | deterministic zip batches, member paths = source-relative paths. Respect ingest caps: ≤1000 members, ≤1 GiB uncompressed per bundle, ≤100 MiB per upload (`src/ingest/bundle.py`, `src/corpus_allowlist.py`). |
| **upload** | bundle upload to the two collections via the existing `POST /api/collections/{id}/files` flow, PAT-authenticated. |
| **verify** | reconcile counts (enumerated / selected / uploaded / indexed / rejected) against the collection's ingest stats; emit a JSON run report. |

### 3.2 Transport: MCP first, native delta APIs second

Phase 1 uses **MCP servers as the uniform extraction interface** (list-type +
fetch-type tools), for the same reason Agnes's Universal MCP connector exists:
one client speaks to any source that has a server, with no per-API connector
code. Known limits, accepted for phase 1 scale (≤ tens of thousands of
documents): binary content arrives base64-encoded in JSON-RPC (+33% volume,
size caps), and MCP servers generally expose no change feed.

Phase 2 swaps enumeration to the source's native delta mechanism (Microsoft
Graph `/delta` tokens, Google Drive `changes.list`, S3 event notifications)
while keeping every other stage unchanged. The worker's own `(path, etag)`
cursor makes phase 1 re-runs incremental even without a delta API.

MCP tool output is untrusted input: member names are validated/normalized
before use as paths (the server-side bundle ingest re-validates: zip-slip,
member caps — `src/ingest/bundle.py`), and worker credentials (PAT, source
tokens) are env/config-file only, never argv.

### 3.3 Idempotence & deletion propagation

- Server-side upsert is by `(filename, sha256)` within a bundle's children and
  `path` for direct uploads (`src/repositories/corpus_files.py`), so
  re-uploading an unchanged file is a no-op and a changed file replaces its
  chunks.
- Bundle re-ingest **deletes unmatched leftovers from the previous run of the
  same archive row** (`src/ingest/bundle.py`). The worker therefore MUST
  partition files into bundles deterministically (e.g. stable hash of the
  source-relative path mod N, or per top-level folder) and always upload the
  full current membership of each bundle. A document deleted at the source
  then disappears from Agnes on the next run of its bundle — deletion
  propagation falls out of the existing contract.
- The run report lists per-bundle adds/replacements/deletes so an operator can
  audit convergence.

## 4. Anonymization & the vault

Principle (industry-standard): the large assistants do **not** anonymize for
internal search — they rely on query-time permission trimming. Anonymization
belongs at a *boundary crossing*: content leaving its native audience (into a
broadly-granted collection, a vendor engagement, or model training). Hence the
two-variant design, and hence anonymize **before** indexing — an embedding of
full text is a copy of the full text.

- **Detection:** dictionary first (the organization's own entity list — client
  names, project codenames — is a stronger signal than generic NER), then NER
  + regex/checksum detectors (Presidio-class tooling).
- **Deterministic pseudonymization:** `token = label + HMAC-SHA256(key, normalized_entity)[:8]`
  (e.g. `Client_7f3a`). The same entity maps to the same token across every
  document, query, and table — random masking would break both retrieval and
  joins. Numeric/date generalization where exact values are themselves
  sensitive.
- **The vault** — the token↔real mapping plus the HMAC key — is the crown
  jewel. It lives worker-side (encrypted at rest), never in the broad tier,
  and is not uploaded to Agnes in v1. Query-time de-anonymization for
  privileged callers is explicitly out of scope for v1.
- **Symmetry:** both variants keep the same relative path, so citations,
  bundle partitioning, and re-ingest behave identically in both collections.
- Structured data is masked by policy, not by rewriting: table-access-policies
  (`docs/table-access-policies.md`) remain the mechanism for tables; this
  spec's anonymizer applies to document text only.

## 5. Landing contract in Agnes — what exists, what changes

Already sufficient (no change): bundle ingest with path preservation and
idempotent re-ingest; per-collection RBAC; hybrid retrieval + digest pass;
`/preview` and `/raw` endpoints for T1 documents.

Additive changes (small, ordered):

1. **`corpus_files.source_url`** (nullable TEXT) — the deep link back to the
   system of record, surfaced in knowledge-search citations and file preview.
   Both backends + migration ladder step, per the dual-backend rules. This is
   the only schema change v1 needs.
2. **Per-collection ingest stats** — an aggregate endpoint/summary (counts by
   `processing_status`, rejected reasons) so the admin surface is an ops
   console rather than a file listing. `list_for_corpus` is unpaginated today;
   at T1 scale (hundreds) that holds, and the stats endpoint removes the
   temptation to list thousands.
3. *(later, scale-triggered)* reranker stage and a VSS/HNSW index behind the
   existing `search()` interface (`src/ingest/retrieval.py` documents the
   brute-force design scale), and visual-retrieval (ColPali-class) for
   deck-heavy corpora as a roadmap item.

Consumption surfaces (all existing): answers with citations in chat;
T2 as a SQL-queryable table for agents; artifacts/data apps as *generated
views* (corpus coverage dashboard, per-engagement one-pager) — never a file
tree. A collection-scoped agent combines T1 retrieval, T2 SQL, and the
organization's structured tables in one question, which is the capability the
generic assistants cannot offer.

## 6. Quality measurement

Retrieval quality is gated on planted ground truth: a synthetic corpus
generator produces a realistic tree (format mix, version churn, junk files)
with **unique facts planted in final documents and contradicting distractors
in superseded drafts**, plus a `questions.jsonl` of
`question → (expected answer, expected file, must-not-win file)`. The gate:

- extraction coverage — no silently-rejected tier-1 format in the target
  deployment (docx/pptx require the `docling` extra; `retrieval_mode()` must
  not silently degrade to `lexical_only` when embeddings are expected);
- retrieval precision — expected file in top-k, distractor not ranked above
  it; tracked per release on the same corpus.

The generator currently lives as an operator tool; graduating it into
`scripts/dev/` is optional and out of scope here.

## 7. Rollout

| phase | shape |
|---|---|
| **1 — operator script** | worker run manually against a curated folder tree; MCP transport; two collections; T2 table registered. No Agnes code changes except §5.1–5.2. |
| **2 — scheduled sync** | worker on a schedule with native delta enumeration; run reports surfaced to the admin; T3 on-demand fetch flow. |
| **3 — product connector** | worker folded into a first-class Agnes source with admin-UI registration, per the connector conventions; per-folder ACL mirroring evaluated here, not before. |

## 8. Open questions

- Vault custody long-term: stays operator-side, or becomes a restricted Agnes
  table with its own resource type + grants?
- Near-duplicate collapse in T1 (same deliverable exported twice): hash-level
  dedup exists via `(filename, sha256)`; content-level near-dup detection is
  unsolved and may matter more than any retrieval tuning.
- Retention: does a source-side legal hold / deletion need to propagate faster
  than the next scheduled bundle run?
