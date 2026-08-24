# Semantic layer: chat-first authoring — one apply surface with outcome branching

Date: 2026-08-24
Status: designed, implementing
Builds on: [2026-08-13-open-semantic-layer-contract-design.md](2026-08-13-open-semantic-layer-contract-design.md)
(the contract), [2026-08-14-semantic-layer-ui-and-agent-parity-design.md](2026-08-14-semantic-layer-ui-and-agent-parity-design.md)
(the parity effort — this delivers its deferred "wave 4.3" mutation surface, in
chat-first form rather than per-object web forms).

## Problem

A connector whose source has no semantic layer of its own (e.g. the Jira
connector — data comes straight from the tracker, there is no upstream
metastore to import from) leaves its dataset without a semantic model unless
an admin hand-authors an Ossie document and drives the admin API directly.
Every write path into `semantic_models` today is `require_admin` and
JSON-body-only; the read-only browse UI (`/semantic-layer`) shipped without
its mutation counterpart ("wave 4.3"), and the three write MCP tools named in
the parity spec §5 were never built.

Meanwhile the dataset's actual experts are often *not* admins: they know the
operational caveats ("this flag never needs filtering", "a missing org is
intentional", "this JSON column should be parsed") but have no surface to
contribute them.

## Direction

The editor for semantic models is **chat plus one apply surface**, not a form
per object type. An agent that knows the methodology (survey → schema →
draft → validate → apply) assembles the document conversationally; a single
endpoint decides what "apply" means based on the caller's authority:

- **admin** → direct write (validate, upsert as `source='manual'`, project),
  response `{"outcome": "applied"}`.
- **non-admin** → a moderation-queue suggestion (`authoring_suggestions`,
  domain `semantic-layer`), response `{"outcome": "submitted_for_review"}`.

One toolset, no `propose_*` twins: CLI, MCP tool, and the studio builder page
all call the same endpoint, and the outcome label tells the caller (human or
agent) what actually happened. This mirrors the command-UX standard: one
surface, origin/outcome always labeled, no role-dependent flag vocabulary.

Crucially, a pending proposal **never lands in `semantic_models`** — the
payload sits in the queue table until approval, so an unreviewed document can
never reach `get_semantic_context`, `knowledge_search`, or query validation.
(The corporate-memory precedent got this wrong — pending `knowledge_items`
are already searchable; the suggestion-queue design is immune by
construction.)

## Non-goals

- Per-object web form editing (the parity spec §2 shape: dataset/metric
  forms, referential-integrity 409s). Chat + whole-document apply may make it
  unnecessary; revisit only if demanded.
- `update_semantic_objects` / `delete_semantic_objects` targeted-mutation
  tools. Whole-document apply covers create and edit; targeted object patches
  are a later refinement if documents grow too large for round-tripping.
- Delete via this surface. Destructive actions keep a different review
  standard; `DELETE /api/admin/semantic-models/{id}` stays admin-only.
- Field-level partial approval of a proposal. The admin approves or rejects
  the document as submitted.
- A deterministic scaffold engine. The chat agent *is* the scaffolder — it
  reads `catalog`/`schema`/samples through existing read tools and drafts the
  document itself; a human reviews in chat before apply, every time.

## Design

### 1. `POST /api/semantic-models/apply` — the one write surface

Auth: `get_current_user` (any authenticated caller). Body:

```json
{
  "document": "<Ossie YAML/JSON text>",
  "description": "optional listing description",
  "expected_content_hash": "optional — for read-modify-write edits"
}
```

Shared pipeline for both roles, in order:

1. **Validate** via `validate_document` — schema errors → 422, never stored
   half-valid. Slug = the document's first `semantic_model` entry name.
2. **Source-ownership guard**: if the slug resolves to an existing model whose
   `source != 'manual'` → `409 source_owned` naming the owning source. This
   is *stronger* than the raw admin POST (which would create a shadow
   `manual/_/<slug>` row next to the imported one); the apply surface refuses
   for admins and non-admins alike.
3. **Staleness guard**: when `expected_content_hash` is supplied and an
   existing manual model's `content_hash` differs → `409 stale_document`.
   This is the optimistic lock for the chat agent's read → modify → apply
   loop.

Then the branch:

- **Admin** → upsert `manual/_/<slug>` + `_project` (the exact same code the
  existing admin POST runs, extracted into a module-level
  `apply_manual_model(document, description)` helper so the two paths and the
  replay cannot diverge). Response
  `{"outcome": "applied", "model": {...}}`.
- **Non-admin** → **pending-dedup guard**: an existing `pending` suggestion in
  domain `semantic-layer` whose payload targets the same slug →
  `409 duplicate_pending` carrying the suggestion id. Otherwise create the
  suggestion row + audit `authoring_suggestion.submit`. Response
  `{"outcome": "submitted_for_review", "suggestion_id": "..."}`.

The Studio instance toggle governs the non-admin branch the same way it
governs the rest of the suggestion surface (`403 studio_disabled`); the admin
branch is not Studio-gated — it is a plain admin write that happens to share
the endpoint.

### 2. Studio domain + chat profile

- `STUDIO_DOMAINS["semantic-layer"]` — builder page `/admin/studio/semantic-layer`,
  fields `document` (textarea, required) + `description`, endpoint
  `/api/semantic-models/apply`, profile `semantic-model-builder`. The generic
  studio page already renders Create (admin) vs Submit for approval
  (non-admin); `studio.js`'s required-field check learns that a `document`
  payload is submittable without `name`/`slug`.
- `ChatProfile("semantic-model-builder")` in `app/chat/profiles.py` — persona
  CLAUDE.md plus an inline knowledge skill carrying the authoring method:
  survey what exists (`agnes semantic-model context`), read the schema
  (`agnes semantic-model schema`), check canonical metrics before inventing
  SQL, draft, validate, apply through the endpoint, and report the outcome
  honestly (`applied` vs `submitted_for_review` — never claim a model is live
  when it went to the queue).

### 3. Replay: `_SAFE_REPLAY["semantic-layer"]`

Approval replays the stored payload through the same `apply_manual_model`
helper — full document re-validation included, so a payload that rotted while
pending (or was never valid) fails the approve with `409 create_failed` and
the suggestion reopens for retry, per the queue's existing claim/reopen
discipline. The source-ownership guard re-runs at replay time too: if an
imported model claimed the slug while the proposal sat in the queue, the
approve refuses rather than shadowing it. Attribution: the created model is
the *submitter's* content; the audit row records both.

### 4. MCP foundation tool `apply_semantic_model`

Thin proxy to the endpoint (same pattern as every foundation tool: HTTP call
carrying the caller's own PAT, so the endpoint's gate is the tool's gate).
Registered in `FOUNDATION_TOOL_NAMES`; the tool description tells the agent
the outcome semantics so it narrates correctly for both roles. This makes the
main Agnes chat (not just the studio page) able to author: an admin in chat
says "create a semantic model for the support dataset", the agent drafts,
shows the document, and applies on explicit confirmation.

### 5. CLI `agnes semantic-model apply <file>`

Non-admin command group (`cli/commands/semantic_model.py`): reads the file,
POSTs to the endpoint, prints the labeled outcome (`Applied: <slug>` /
`Submitted for review: <suggestion id> — an admin will approve or reject`).
`--description`, `--expect-hash`, `--json`. Offline pre-check stays
`agnes admin semantic-model validate` (no server, no token).

## Security posture

- A proposal is data in a queue, not a live model: nothing agent-facing can
  read it before approval.
- The stored payload is never trusted at approve time — full document
  re-validation plus the ownership guard run again inside the replay.
- The moderation UI renders the complete payload; approval is informed
  consent (the queue's existing contract).
- Both branches audit (`authoring_suggestion.submit` / the admin write), and
  the MCP tool inherits endpoint auth via the caller's PAT — no separate
  authority path to reason about.

## Testing

- Endpoint: admin applies (model row + projection exist), non-admin submits
  (suggestion row exists, `semantic_models` untouched), invalid document 422,
  source-owned slug 409 for both roles, stale hash 409, duplicate pending
  409, studio-disabled 403 on the non-admin branch only.
- Replay: approve creates the model + projection; approve of a
  since-invalidated or since-shadowed payload reopens the suggestion.
- Parity: `apply_semantic_model` present in `FOUNDATION_TOOL_NAMES` (both
  transports inherit via the shared module); PG endpoints smoke picks the new
  route up automatically (`{}` body → 422 on both backends).
- Studio: domain renders in create mode for admin, submit mode for non-admin
  (existing parametrized tests extend by the new slug).
