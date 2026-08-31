# Anonymization for file sources

What the connect wizard's per-scope "anonymize" checkbox actually does on
the Agnes side, and — just as important — what it does **not** do. Design
rationale: [`superpowers/specs/2026-08-27-fact-graph-over-collections-design.md`](superpowers/specs/2026-08-27-fact-graph-over-collections-design.md)
§9 (anonymize-in-front pipeline) and §9.2 (the pseudonym scheme).

## The pipeline, and where Agnes sits in it

```
source → crawl → convert → anonymize → Agnes
         └──────── connectors/sharepoint/crawler.py ────────┘
```

Anonymization happens **before** ingestion, inside the built-in extraction
pipeline (owner decision 2026-08-31: the in-repo crawl → convert →
anonymize → ingest chain is the only pipeline; the external-producer
subprocess and its `extraction.producer.*` config were removed). Placed in
front like this, the guarantee is structural for a genuinely anonymized
scope: **Agnes never holds the original at all.** The downloaded bytes live
in a temp file that is deleted on every path, and only the anonymized
markdown is stored — so every downstream surface (`/raw`, `/preview`, the
chunk index, search) necessarily serves the anonymized form, because that
is all that was ever ingested.

Three moving parts:

1. **Mark which scopes need it.** The connect wizard's step-2 "anonymize"
   column (`/admin/data-sources`, backed by `app/api/admin_sharepoint.py`)
   sets a per-scope flag stored on the connection's own
   `config.scopes[].anonymize`. `GET /connections/{id}/scopes` returns it.
2. **Fail closed on those scopes.** The crawl anonymizes every document of
   an anonymize-marked scope before ingesting it, and a document that
   cannot be anonymized — no resolvable key, a detector that is
   unavailable, any error at all — is **counted in the run report's
   `anonymize_failed` and dropped, never ingested raw**.
3. **The per-instance pseudonym key.** Design spec §9.2 requires the
   anonymizer's entity substitution to be a per-instance HMAC pseudonym
   (`PERSON_<hmac(key, ...)>`, `COMPANY_<hmac(...)>`, `EMAIL_<hmac(...)>`),
   never a fixed marker like `**PERSON**` — a fixed marker collapses every
   person in a document into one node once facts are extracted, and a key
   shared across instances would let the same person correlate across
   tenants. `app/worker/kinds.py::_resolve_anonymization_key` resolves it
   (see below), and only when at least one scope in the run needs it.

An ingest that arrives through `POST /api/facts/ingest` from something
other than this crawl still carries the caller's own `anonymization`
declaration — see *What Agnes records* below; the declaration is a claim
Agnes stores, not something it can verify.

## Configuring the key

`config/instance.yaml`, under the existing `extraction:` block:

```yaml
extraction:
  anonymization:
    hmac_key_env: ""   # empty = use the default name below
```

`hmac_key_env` names the environment variable holding the key — checked
against an allowlist *before* the value is read, because the config value
is admin-writable and an unchecked name could be pointed at an unrelated
instance secret. Leave `hmac_key_env` empty (or set it to
`AGNES_ANONYMIZATION_HMAC_KEY`) to use the default name; any other name is
refused.

That allowlist is **deliberately its own, separate list** — NOT the
connector-ATTACH `token_env` allowlist (`AGNES_REMOTE_ATTACH_TOKEN_ENVS`)
the SharePoint certificate and other data-source credentials use. A
`_remote_attach` row in a connector's `extract.duckdb` can never reference
`AGNES_ANONYMIZATION_HMAC_KEY` as its `token_env`, because the two
allowlists never share membership — see `src/orchestrator_security.py`'s
`_PRODUCER_KEY_ENVS` for the full reasoning.

Generate a random value once per instance and never reuse it across
deployments:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

A connection with an anonymize-marked scope and no resolvable key fails the
`corpus-extraction` job cleanly (a named error, not a silent run without a
key) — set the variable, or unmark the scope in the connect wizard.

## Choosing a detector

```yaml
extraction:
  anonymization:
    detector: "regex"   # regex (default) | llm
```

- **`regex`** — the deterministic tier (`src/anonymization.py`): emails,
  URLs, and pattern-matched person/company names. Free, and it runs over
  every document of every crawl.
- **`llm`** — regex *union* an LLM reader (`src/anonymization_ner.py`,
  using this instance's existing model credentials). It catches the names a
  pattern cannot, is hallucination-filtered to verbatim spans, and fails
  **loudly**: a document whose LLM pass is unavailable is counted in
  `anonymize_failed` and dropped, rather than ingested with a redaction that
  quietly degraded to regex-only.

`regex` is the default as a **cost decision, not a legacy one**. The LLM
tier sends every crawled document to the model — on the order of **~$5 per
1,000 documents** at the default Haiku-class model — so an operator opts in
when the extra recall is worth that per-corpus bill.

## Badge semantics: requested vs. declared

Two different things share the word "anonymize" on the UI, and the wizard
used to render only the first as if it were the second:

- **Requested** — the wizard checkbox is ticked (`anonymize=true` on the
  scope row). This is an admin's *intent*, nothing more.
- **Declared** — the most recent `POST /api/facts/ingest` batch for this
  collection reported (via the optional `anonymization` block, see below)
  that documents actually went through the anonymizer.

The connect wizard's share preview, the source card
(`/admin/data-sources`), and `GET /connections/{id}/scopes`'s
`anonymization_declared` field all distinguish the two:

| Requested | Declared | Badge | Tone |
|---|---|---|---|
| no | — | (none) | — |
| yes | no | "anonymization requested" | warn |
| yes | yes | "anonymized" | ok |

A collection never reads "anonymized" from the checkbox alone. This is
also why, per design spec §13.2, anonymization on a collection detail is
"a state plus a named batch task, never a toggle" — the checkbox sets
intent for *future* crawls; it does not retroactively anonymize documents
already ingested, and there is no batch action that would (see design spec
§1).

## The `anonymization` ingest block

A producer MAY include an optional block in its `POST /api/facts/ingest`
body declaring what it anonymized in that batch:

```jsonc
{
  "documents": [...], "nodes": [...], "edges": [...],
  "anonymization": {
    "declared": true,
    "scopes": {
      "<collection_id>": {"docs_anonymized": 12, "docs_skipped": 1}
    }
  }
}
```

This rides into the persisted run report (`facts_ingest_runs`,
`GET /api/facts/ingest-runs`) — never into the ingest's own returned report
— and is what flips a scope from "requested" to "declared" on the next
page load. `scopes` is keyed by collection id; a key that does not match a
real collection is kept, not rejected (a self-reported tally, not a join
against `file_corpora` — see "Current limits" below). Malformed shapes
(wrong types) are rejected with `422`; the block itself is entirely
optional — a producer that never anonymizes omits it.

## Ingest-time enforcement: undeclared is refused, not silently accepted

`POST /api/facts/ingest` (spec §7.2/§9) **refuses** a batch that documents a
corpus whose SharePoint scope is anonymize-marked (`config.scopes[]
.anonymize`) when that same batch's `anonymization` block does not declare
the corpus — `403`, `{"reason": "anonymization_not_declared", "corpus_ids":
[...]}`, itemizing exactly which collection(s) are missing a declaration.
Nothing from that batch is written: the check runs before the ingest
transaction, so a refused batch leaves no claim, no subject, and no run
report behind.

This closes a real fail-open path: an admin's confirmed scopes
(`config.scopes`) live on the connection row and are server-written by the
connect wizard's own endpoints — never typed by hand, and never rendered by
the generic connection editor. Editing that connection through the ordinary
editor (a rename, a certificate change, anything that replaces `config`
without echoing `scopes` back) used to wipe them silently, which in turn
left the `corpus-extraction` job with no anonymize-marked scope, skipped
the HMAC-key resolution, raised no error anywhere, and let the pipeline ship
un-anonymized content into a collection an admin believed was anonymized —
reported as a normal, successful run. The generic editor now preserves
`scopes` across an update that does not mention them (an explicit
`scopes: []`, or the wizard's own unselect endpoint, still clears them
deliberately), and this ingest-time gate is the second, independent
backstop: even if config, an env var, or an external ingest caller
misbehaves, an anonymize-marked corpus with no declaration is refused at
the door.

A lookup failure while answering "is this corpus marked" (e.g. the
`source_connections` table is unreadable) is **not** treated as "nothing is
marked" — it is refused the same way (`503`,
`anonymization_check_unavailable`), never waved through as a silent accept.

## Current limits — read this before trusting the badge

**A declaration is not a verification.** The `anonymization_declared`
state is exactly what its name says: the ingesting caller's self-reported
declaration, persisted and surfaced, never independently checked against
document content. For the built-in crawl the enforcement is real — an
anonymize-marked scope's documents are pseudonymized before they are
stored, and one that cannot be is dropped — but for a batch pushed in
through `POST /api/facts/ingest` by anything else, Agnes's contribution is:
mark intent, refuse an ingest batch that skips the declaration entirely for
a marked corpus (above), and honestly label the two states as *requested*
and *declared* rather than collapsing them into one unverified "anonymized"
claim. A caller that *sends* the declaration but is unreliable or
misconfigured — claiming to have anonymized a document it did not — can
still make "declared" wrong; there is currently no mechanism in Agnes to
catch that. What Agnes refuses is the declaration being *absent* for a
corpus it knows was supposed to get one, not a false declaration.

**Detection is not exhaustive.** Neither tier finds every identifier: the
regex tier is patterns, and the LLM tier is a reader with a reader's
misses. An anonymized scope is materially safer than an un-anonymized one;
it is not a guarantee that nothing identifying survived.
