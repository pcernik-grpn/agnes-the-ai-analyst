# Anonymization for file sources

What the connect wizard's per-scope "anonymize" checkbox actually does on
the Agnes side, and — just as important — what it does **not** do. Design
rationale: [`superpowers/specs/2026-08-27-fact-graph-over-collections-design.md`](superpowers/specs/2026-08-27-fact-graph-over-collections-design.md)
§9 (anonymize-in-front pipeline) and §9.2 (the pseudonym scheme).

## The pipeline, and where Agnes sits in it

```
source → crawl → convert → anonymize → Agnes
                             (producer)
```

Anonymization happens **before** ingestion, run entirely by the external
producer (the crawl/convert/anonymize/extract pipeline an operator supplies
per [`docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md`](superpowers/specs/2026-08-27-fact-graph-over-collections-design.md)
§7.1 — not code in this repo). Placed in front like this, the guarantee is
structural for a genuinely anonymized scope: **Agnes never holds the
original at all.** Nothing unredacted enters, so every downstream surface —
`/raw`, `/preview`, the chunk index, search — necessarily serves the
anonymized form, because that is all that was ever ingested.

Agnes's own role is three things, all in `app/worker/kinds.py`'s
`corpus-extraction` job handler:

1. **Mark which scopes need it.** The connect wizard's step-2 "anonymize"
   column (`/admin/data-sources`, backed by `app/api/admin_sharepoint.py`)
   sets a per-scope flag stored on the connection's own
   `config.scopes[].anonymize`. `GET /connections/{id}/scopes` returns it;
   the producer handoff endpoint, `GET /connections/{id}/corpus-map`, stays
   the flat `{source_scope_id: collection_id}` shape it always was — a
   producer that needs the per-scope flag reads the `/scopes` endpoint
   instead.
2. **Hand the anonymize-marked scopes to the producer.** When at least one
   confirmed scope on a connection is `anonymize=true`, the
   `corpus-extraction` job's child process environment carries
   `AGNES_EXTRACTION_ANONYMIZE_SCOPES` — a JSON object,
   `{source_scope_id: collection_id}`, covering only the anonymize-marked
   scopes. A connection with no anonymize-marked scope never sets this var
   at all.
3. **Hand the per-instance pseudonym key.** Design spec §9.2 requires the
   anonymizer's entity substitution to be a per-instance HMAC pseudonym
   (`PERSON_<hmac(key, ...)>`, `COMPANY_<hmac(...)>`, `EMAIL_<hmac(...)>`),
   never a fixed marker like `**PERSON**` — a fixed marker collapses every
   person in a document into one node once facts are extracted, and a key
   shared across instances would let the same person correlate across
   tenants. Agnes resolves this instance's key and forwards it as
   `AGNES_ANONYMIZATION_HMAC_KEY`, alongside the scopes map, only when at
   least one scope needs it.

Both vars reach the producer **only via the child process environment**,
never on the command line and never logged — the same rule every other
credential this handler resolves (SharePoint tenant/client id, certificate)
already follows.

## Configuring the key

`config/instance.yaml`, under the existing `extraction:` block:

```yaml
extraction:
  anonymization:
    hmac_key_env: ""   # empty = use the default name below
```

`hmac_key_env` names the environment variable holding the key — resolved
the SAME way `connectors/sharepoint/settings.py` resolves the SharePoint
certificate: the name is checked against the shared token-env allowlist
(`AGNES_REMOTE_ATTACH_TOKEN_ENVS`, default includes
`AGNES_ANONYMIZATION_HMAC_KEY`) *before* the value is read, because the
config value is admin-writable and an unchecked name could be pointed at an
unrelated instance secret. Leave `hmac_key_env` empty to use the default
name, `AGNES_ANONYMIZATION_HMAC_KEY`.

Generate a random value once per instance and never reuse it across
deployments:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

A connection with an anonymize-marked scope and no resolvable key fails the
`corpus-extraction` job cleanly (a named error, not a silent run without a
key) — set the variable, or unmark the scope in the connect wizard.

## Badge semantics: requested vs. declared

Two different things share the word "anonymize" on the UI, and the wizard
used to render only the first as if it were the second:

- **Requested** — the wizard checkbox is ticked (`anonymize=true` on the
  scope row). This is an admin's *intent*, nothing more.
- **Declared** — the producer's most recent `POST /api/facts/ingest` batch
  reported (via the optional `anonymization` block, see below) that it
  actually ran documents for this collection through the anonymizer.

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
already ingested, and Agnes has no batch action of its own that would (that
lives in the producer, out of scope for this repo — see design spec §1).

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

## Current limits — read this before trusting the badge

**Agnes cannot verify that content was actually anonymized.** The
`anonymization_declared` state is exactly what its name says: a producer's
self-reported declaration, persisted and surfaced, never independently
checked against document content. The enforcement — actually redacting and
pseudonymizing entities before upload — lives entirely in the producer.
Agnes's contribution is: mark intent, hand the producer what it needs
(scopes + key), and honestly label the two states as *requested* and
*declared* rather than collapsing them into one unverified "anonymized"
claim. If your producer is unreliable or misconfigured, "declared" can be
wrong; there is currently no mechanism in Agnes to catch that.
