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

The document's **filename and folder path** get the same treatment, not
just its body: for a lot of documents (a deal room, a client-named folder
tree) the source name is the single most re-identifying string in it, so
storing it verbatim would have broken the "never holds the original"
guarantee even with a fully redacted body. The crawler
(`connectors.sharepoint.crawler._anonymize_identity`) anonymizes each path
SEGMENT independently — not the path as one opaque string — under the same
per-instance key and the same detector as the body, and keeps the `/`
separator structure: two files that really are under one folder still share
one anonymized folder prefix, so folder-scoped browsing and prefix matching
over the anonymized tree work the same shape they did over the real one.
Routing decisions (which collection a file lands in, which exclusion rule
applies) are still made against the REAL path, earlier in the pipeline —
only what gets *persisted* is anonymized.

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

## The key: nothing to configure (owner decision, 2026-09-01)

**There is normally nothing to set up.** The first time a run actually
needs the key, Agnes generates one (`secrets.token_bytes(32)`), stores it
encrypted in its own secret vault, and reuses it from then on
(`src/anonymization_key.py`). The only prerequisite is the one every
Agnes secret already has: a stable `AGNES_VAULT_KEY` on the server.

Resolution order, strictly:

| # | Source | When it applies |
|---|---|---|
| 1 | `$<hmac_key_env>` (default `AGNES_ANONYMIZATION_HMAC_KEY`) | an operator minted their own key — always wins |
| 2 | the vault-stored instance key | no env key, one was stored earlier |
| 3 | a freshly generated + stored key | no env key, none stored, `AGNES_VAULT_KEY` set |
| 4 | fail closed (named error) | no env key and no usable vault |

Provisioning is **write-once**: an existing key is never overwritten, two
workers provisioning concurrently converge on one key, and there is no
rotate/regenerate command. That is deliberate — rotating this key rewrites
every pseudonym, so `PERSON_<hmac(k1, …)>` and `PERSON_<hmac(k2, …)>`
become different nodes and everything already ingested stops referring to
the same person as everything ingested afterwards. Losing or regenerating
the key by accident is a data-integrity bug, not an inconvenience. One
cataloged audit row (`anonymization.key_provisioned`) records when the key
came into existence, by fingerprint (`sha256(key)[:12]`) — the value never
appears in a log, an audit row, or the UI.

### Minting your own key instead

Set `hmac_key_env` only if you want to supply the key yourself:

```yaml
extraction:
  anonymization:
    hmac_key_env: ""   # empty = the default name, AGNES_ANONYMIZATION_HMAC_KEY
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
deployments (this is the same shape Agnes generates for itself):

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

## Provisioning the key on a deployment

The value is a single line, so any env-delivery mechanism works. On a VM
managed by the bundled Terraform module
(`infra/modules/customer-instance`), store it in your cloud's secret
manager and map it through `runtime_secret_env`:

```hcl
runtime_secret_env = {
  AGNES_ANONYMIZATION_HMAC_KEY = "<your-secret-name>"
}
```

The startup script reads the secret into `/opt/agnes/.env` at boot. On an
already-running VM a newly added mapping lands **only on the next VM
recreate** (startup metadata is frozen by design) — plan it together with
enabling the extraction worker so production restarts once, not twice.

**Never rotate this key, and do not lose it.** Pseudonyms are
`hmac(key, entity)`: a new key gives every already-known person and company
a brand-new token, so facts extracted after the change no longer join to
facts extracted before it — one real entity silently becomes two subjects,
and there is no re-keying tool. The rotate-on-schedule hygiene that
credentials deserve actively damages this value; it is closer to data than
to a credential (it never appears in output, logs, or error messages, so
use does not wear it out). If it is ever truly compromised, rotating it is
a *reset*: plan to re-run extraction over the full corpus so the graph is
rebuilt in the new token space.

A connection with an anonymize-marked scope and **no** resolvable key — no
env key *and* no usable vault — still fails the `corpus-extraction` job
cleanly (a named error, not a silent run without a key). The error names
both fixes: set `AGNES_VAULT_KEY` and let Agnes generate the key, or set
the env variable yourself. Or unmark the scope in the connect wizard.

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

## Deterministic identifier tiers

Neither detector above finds a phone number, a bank account or a birth
number, because none of them is a *name* — they are decided from characters
alone, which makes them the wrong job for a name detector and the right job
for a shape pass. Three such tiers run before entity substitution:

```yaml
extraction:
  anonymization:
    detect:
      phones: true          # PHONE_<hmac>
      ibans: true           # IBAN_<hmac>
      national_ids: true    # ID_<hmac>
```

**All three default to ON, and that default is the recommendation.** They
cost nothing per document (no model, no network), they are exact by
construction, and each guards an identifier whose disclosure is more severe
than a name's. Turn one off only for a corpus where that shape is noise
rather than identity.

The pseudonyms use the same per-instance HMAC key as `PERSON_` /
`COMPANY_` / `EMAIL_`, so a phone number tokenizes identically in every
document of an instance and never correlates across instances.

The tiers run **IBAN → national id → phone**, and that order is load-bearing:
a grouped IBAN (`CZ65 0800 0000 1920 0014 5399`) contains a run that reads
as an international phone number, so the IBAN has to be consumed whole
first. The other order shreds it into a redacted fragment and a surviving
one.

What each tier matches, and what it deliberately does not:

| Tier | Matches | Deliberately does **not** |
|---|---|---|
| `phones` | `+CC…` / `00CC…` (8–15 digits, any of space/NBSP/`.`/`-`/parens as separators) and the Czech 3-3-3 grouping `776 041 900` | a **bare nine-digit run** (`776041900`) — that shape is an order number or an account reference at least as often as a phone |
| `ibans` | both written forms — solid (`CZ6508000000192000145399`) and grouped in fours — **validated with the ISO 13616 mod-97 checksum** | anything failing the checksum, which also means a **typo'd or OCR-mangled** account number is not redacted |
| `national_ids` | Czech birth number (rodné číslo): `760419/0341`, `760419 / 0341`, and the slash-less ten-digit form. Date field **and** mod-11 check (with the pre-1985 remainder-10 allowance) must both pass | the slash-less **nine-digit** (pre-1954) form, which carries no checksum and is indistinguishable from any other nine-digit number |

Canonicalization unifies the written forms: `+420 776 041 900` and
`+420776041900` are one token; `760419/0341` and `7604190341` are one token;
a grouped and a solid IBAN are one token. A **national** phone form and an
**international** one are NOT unified (`776 041 900` ≠ `+420 776 041 900`) —
that would require assuming a default country, and an anonymizer that
guesses a country prefix produces a pseudonym that is wrong in exactly the
corpus where it matters.

## Custom terms — the vocabulary only you have

```yaml
extraction:
  anonymization:
    custom_terms:
      - "Projekt Fénix"
      - "ACME-*"
```

Redacted as `TERM_<hmac>`. No general detector can know a project codename,
an internal system, or a site name; this list is where the operator supplies
them.

Entries are **literal text**, matched case-insensitively and on word
boundaries. A `.` is a dot, not "any character". One trailing `*` is allowed
and means "followed by more word characters" (`ACME-*` catches `ACME-1234`),
compiled as a bounded run rather than an unbounded quantifier.

**Regular expressions are refused.** A pattern from config runs over every
document of every crawl, where one badly written quantifier is a denial of
service (security playbook §5), so an entry carrying regex metacharacters is
rejected *by name* with a message saying which character and why. Also
refused: an entry under 2 characters (it would redact most of a document),
one over 128 characters, more than 200 entries, and any of the anonymizer's
own placeholder words.

An unusable entry **fails**, it is not skipped — the crawl counts the
document in `anonymize_failed` and drops it, and the preview panel below
answers `400` naming the bad entry. Silently dropping a term an operator
added would leave them believing something is redacted that is not, which is
the one failure mode this whole feature exists to prevent.

## Preview: see the redaction before the crawl

Configuration can say which tiers are on. It cannot say what they will do to
*your* documents — and that is the question actually being asked before a
crawl over thousands of files ("does it catch our case numbers? does it eat
our product codes?").

`/admin/data-sources` → the source card's **config drawer** → **Preview
redaction**: paste a sample, press the button, see the redacted text and a
per-kind count of what changed.

The same drawer lists each tier's effective state and the number of
configured custom terms above the panel (the terms themselves are not
printed — they are the words an operator considered sensitive enough to
redact). A `custom_terms` list that cannot compile shows as `invalid` with
the compiler's own message, because until it is fixed every anonymized
document of every crawl is dropped, and the run report should not be the
first place that surfaces.

The panel calls `POST /api/admin/sharepoint/anonymization/preview`
(admin-only), body `{"text": "...", "detector": "regex" | "llm"}`. It runs
the **real** anonymizer: this instance's real key resolution, the configured
tiers, the configured custom terms — so a pseudonym in the preview is the
pseudonym a crawl will produce.

Four things are true of it by construction:

- **Nothing is persisted.** No collection row, no extraction run, no stored
  sample. The one write is the audit row (`anonymization.preview`), and that
  row carries the sample's *length* and the redaction counts, never its
  text.
- **The response cannot echo an original.** It returns the redacted text
  plus the pseudonyms and counts — a list of tokens, never a mapping back to
  what they replaced.
- **No fake key.** An instance with no resolvable key answers `409` naming
  `extraction.anonymization.hmac_key_env` rather than previewing under a
  throwaway key. A pseudonym an admin cannot reproduce in production is
  worse than no preview.
- **`detector: "llm"` spends real tokens**, one call per 30k-character
  chunk, and the response returns that call's own usage so the cost is
  visible while the tier is being evaluated. It defaults to `regex`
  regardless of what the instance is configured for — opening a drawer must
  not bill anyone.

Samples are capped at 50,000 characters (`413` above that, with the limit
named).

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

**Detection is not exhaustive.** No tier finds every identifier: the regex
tier is patterns, the LLM tier is a reader with a reader's misses, and the
deterministic identifier tiers above are honest about the shapes they
refuse (a bare nine-digit run, a checksum-failing IBAN, a nine-digit birth
number) precisely because matching them would redact ordinary order numbers
across the corpus. Addresses, case numbers, VAT ids, account numbers in
national (non-IBAN) format and passport numbers are still **not** detected
at all. An anonymized scope is materially safer than an un-anonymized one;
it is not a guarantee that nothing identifying survived — which is what the
preview panel is for: check a real sample of *your* documents before
trusting a crawl over thousands of them.

**Retroactive folder-exclusion/zone cleanup doesn't reach an anonymized
scope's already-ingested files.** The ACL sweep that retroactively purges
content under a newly excluded folder or a dissolved permission zone
(`connectors.sharepoint.acl_sync._cleanup_connection_content`) matches on
the STORED `corpus_files.path` — which is the anonymized path for a marked
scope, and can never prefix-match the admin's real, un-anonymized exclusion
folder or zone path. A file-kind exclusion (matched by the Graph item's
stable id, not its path) is unaffected; only folder-kind exclusions and
zone dissolutions lose their retroactive reach. Known, tracked (#2011), not
yet fixed — see the comments at the match site in that module.
