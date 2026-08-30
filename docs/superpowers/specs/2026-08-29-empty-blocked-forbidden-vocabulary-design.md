# One vocabulary for empty vs blocked vs forbidden (TCRD-207)

> **Sign-off needed on the security call below before this ships wider than
> the surfaces in this PR.** Everything else here is naming and CSS.
>
> **The call:** a resource the caller has no grant for renders IDENTICALLY to
> a resource that doesn't exist — same copy, same icon, same "not found"
> treatment — *except* on the handful of surfaces that already tell the
> caller a specific thing exists before checking their access to it (e.g. a
> data-package URL that names the package in its own 403, or a connections
> card that stays visible after a grant is revoked). Those surfaces keep
> disclosing exactly what they disclose today; nothing new is exposed.
> **Tradeoff:** this is more honest to "don't leak existence" than to "tell
> the user why nothing showed up" — a caller denied a grant sees the same
> screen as a caller who mistyped a slug, and has to ask an admin either way.
> Overrule this if the product wants existence-acknowledgment to be the
> default instead of the exception.

## 1. Investigation — what's actually on `main` today

The ticket's evidence list reads as nine live bugs. On the current
`integration` branch, most of them are already patched — piecemeal, each
with its own wording and its own visual idiom. That inconsistency, not
missing states, is the live problem for seven of the nine:

| # | Surface | Status found | Where |
|---|---|---|---|
| DES-113 | Collections/library search | **Fixed** (#1236) — search APIs return counts + a `hint` string on a genuine miss; access is judged on countable grants, not group membership. Not routed through `error.html`. | `app/api/collections.py`, `app/api/knowledge_search.py` |
| DES-160 | `/admin/linked-apps` fetch summary | **Fixed** — a `skipped` count is now reported and explained separately from `created`/`updated`/`hidden`, so a mapping problem doesn't read as "upstream is empty". | `app/web/templates/admin_linked_apps.html:261-268` |
| DES-95 | Uploaded-but-inaccessible table | Not reproduced as described — table registration is admin-gated and stack subscription is separate from raw existence; no code path currently renders an inaccessible table as an empty result. Left out of scope (no live bug found). | — |
| DES-111 | `/admin/semantic-layer` "imported nothing" | **Fixed**, with an explicit comment citing this exact confusion — "ran, imported nothing" is a distinct span (`.sl-last-none`) from "✓ N updated" (`.sl-last-ok`), "skipped" and "✗ failed". | `app/web/templates/admin_semantic_layer.html:193-213` |
| DES-153 | `/chat` conversation list | **Still live.** A failed `GET /api/chat/sessions` (401/403/network/500) is caught and renders the exact same `#cloud-chat-empty-state` — "No conversations yet." — as a genuinely empty list. Two independent code paths do this. | `app/web/static/js/rail_history.js:426-434`, `app/web/static/js/chat.js:6279-6288` |
| DES-96 | `/chat` answers dropping sources | **Fixed** — `renderSourcesChips` distinguishes "nothing declared and nothing claimed → stay silent" from "figure rendered with no declared source → show 'Sources — none declared'" from per-claim verified/unverified/neutral chips. | `app/web/static/js/chat.js:413-492` |
| DES-62 | `/me/connections` placeholder | **Fixed** (#1167, explicitly) — "No tools are enabled for this source yet" (bootstrap state) is separate copy from "You no longer have access to this source's tools" (revoked-access state), and the revoked card stays visible/removable rather than disappearing. | `app/web/templates/me_connections.html:117-131` |
| DES-112 | `/admin/data-sources` states | **Fixed, and thorough** — a per-card severity fold (`err` sync failing → `warn` no tables → `warn` tables reach nobody → `ok`) with an explicit comment: "broken beats empty beats undelivered." This is close to a reference implementation of the ordering principle in §2 below. | `app/web/templates/admin_data_sources.html:1947-1955` |
| DES-114 | Moderation queue empty state | **Fixed** — `admin_corporate_memory.html`'s review queue already separates "No pending items to review" (empty), "No items match the current filters" (filtered-empty) and "Error loading pending items" (failed) as distinct strings, and `admin_moderation_hub.html`'s two queues separately handle "feature not enabled on this instance" vs "queue empty". | `app/web/templates/admin_corporate_memory.html:2711,3186,3394`, `app/web/templates/admin_moderation_hub.html:60-96` |

No path was found that still routes an empty *result* through `error.html`.
`error.html` today is reserved for actual HTTP error statuses (403/404/500)
and already carries the two "distinct detail, not a bare code" cases the
review flagged: `package_not_shared:<name>` (exists, not granted — names the
package, since a package 403 already discloses existence per the note at
`app/web/router.py:4092`) and `bridge` (wrong-audience admin URL). The one
real, reproducible bug is **DES-153**: an operation *failing* collapses into
the *empty* state, on the single highest-traffic page in the product.

## 2. The states

Four states, one precedence order when more than one could apply
(`admin_data_sources.html`'s comment already states this correctly for its
own domain; this generalizes it):

**FAILED beats BLOCKED beats EMPTY beats NOTHING_FOUND is the wrong
framing — precedence is about severity, not this list's order.** The actual
rule, restated once:

> A request that didn't complete (FAILED) is always reported as failed,
> never silently downgraded to a quieter state. Short of that: an access
> decision (BLOCKED) is checked before a content decision (EMPTY /
> NOTHING_FOUND), because "you can't see this" and "there's nothing here"
> are different facts even when — per the security call above — the caller
> sees the same words either way.

| State | Question it answers | When | Ever tell them something exists? |
|---|---|---|---|
| **NOTHING_FOUND** | "Does anything match what I asked for?" | A search/filter query ran and matched zero rows out of a non-empty universe. | N/A — always visible in this case, since the caller supplied the query. |
| **EMPTY** | "Is there anything here at all?" | A listing ran successfully with no query and the underlying collection genuinely has zero rows (a fresh library, an unused queue, a source with no tables). | N/A |
| **BLOCKED** | "Can I see this?" | The caller lacks a grant for a specific, addressed resource. | **No, by default** (see security call). Renders identically to NOTHING_FOUND/404. The *disclosed* exception (below) is the only carve-out, and it only ever repeats information the product already showed this caller. |
| **FAILED** | "Did this even run?" | The request errored — network failure, 5xx, timeout, an exception before a result was produced. | N/A — always its own state, retry affordance included where one exists. |

### The disclosed exception, precisely

"Exists but you can't see it" is acknowledged ONLY when the product has
*already* told this specific caller, through a surface they reached
legitimately, that the thing exists — never by inferring existence from the
block itself. Every current example fits one pattern: **the caller already
holds a reference to the resource** (a URL they were sent, a card for a
source an admin previously granted them) — the disclosure is repeating
something the caller supplied, not something the system looked up on their
behalf:

- `error.html`'s `package_not_shared:<name>` — the caller already has the
  package's URL (typed, bookmarked, or shared); the 403 names it back.
  Collections, by contrast, 404 blank — see the code comment at
  `app/web/router.py:4092` for the deliberate split.
- `me_connections.html`'s revoked-access card — the caller was connected
  before; losing the grant doesn't erase the card, so they can still remove
  their stored credential.
- `error.html`'s `bridge` case — a non-admin followed a colleague's admin
  URL; the page tells them an equivalent page exists for them, not what's
  behind the admin one.

A NEW surface does not get to invent a fifth "acknowledged block" case by
analogy — each one so far was a deliberate, reviewed exception to the
default. Treat BLOCKED as indistinguishable-from-absent unless the surface
already, today, tells this caller the resource exists.

## 3. Wording per state (canonical, reusable)

Copy is a *template*, not a fixed string — every surface fills in what it's
a list of. The macro (§4) takes `title` and optional `body`/`cta`, but new
copy should follow this shape:

- **NOTHING_FOUND**: `"No matches for {query}"` / `"Nothing matches these
  filters"` — body suggests the adjustment (different term, clear a filter),
  CTA is "Clear filters" when one applies.
- **EMPTY**: `"{Collection} is empty"` / `"No {things} yet"` — body explains
  what would appear here and how to add the first one, CTA is the creation
  action when one exists (never a CTA that can't succeed — a queue with
  nothing to review has no "Add" action, so it gets no button).
- **BLOCKED** (disclosed case only — default is indistinguishable from
  NOTHING_FOUND): `"Not shared with you yet"` — body names the resource
  (since existence is already known) and the ask is a concrete next step
  ("Copy request for your admin"), never a bare "Forbidden".
- **FAILED**: `"Couldn't load {thing}"` — body is honest about not knowing
  why past "the request didn't complete", CTA is "Retry" wired to the same
  fetch, never silently swallowed.

Reconciling with `/admin/access`'s existing three-way split (`no access` /
`not found` / `nothing matched`, flagged by DES-113): those map onto this
vocabulary as `no access → BLOCKED (undisclosed, renders as NOTHING_FOUND)`,
`not found → NOTHING_FOUND` (a specific id didn't resolve) and
`nothing matched → NOTHING_FOUND` (a query matched zero) — i.e. RBAC's
"not found" and "nothing matched" were always the same state under two
names, and this doc doesn't ask `/admin/access` to invent a third rendering
for them.

## 4. Visual treatment

Four states, but only three renderings, because BLOCKED is deliberately
indistinguishable from NOTHING_FOUND by default:

- **Neutral** (NOTHING_FOUND, EMPTY, and default-case BLOCKED): a quiet
  card, `--ds-border` hairline, `--ds-text-secondary` body, muted icon.
  NOTHING_FOUND and EMPTY still read as different states at a glance because
  the icon and copy differ (search glyph + "clear filters" vs. inbox glyph +
  a creation CTA) — same neutral *tone*, different *content*, which is what
  DES-111 actually asked for (a run that found nothing must not look like a
  page that never rendered — it doesn't, because EMPTY still gets full
  card treatment, never a bare dash).
- **Warning-tinted** (BLOCKED, disclosed case): `--ds-accent-warn-{bg,ink,
  line}` — an access fact, not an error, so it takes the same amber
  treatment already used for "you paused admin mode" in `error.html`.
- **Danger-tinted** (FAILED): `--ds-accent-danger-{bg,ink,line}` — this is
  the one that must never look like the other two. A retry action lives
  here when a retry is possible.

Implemented once as `state.panel()` in `app/web/templates/macros/_state.html`
(§5) — no page hand-rolls its own empty/blocked/failed markup going forward.

## 5. Rollout (this PR)

Full nine-surface rollout is explicitly out of scope (per the ticket). This
PR:

1. Ships the macro (`macros/_state.html`) implementing §3/§4.
2. Fixes the one live structural bug found in §1 — DES-153, the chat
   conversation list collapsing FAILED into EMPTY in both `chat.js` and
   `rail_history.js` — using the new FAILED state.
3. Applies the macro to one more surface already showing hand-rolled
   empty/no-match markup, to prove the pattern out beyond the chat rail:
   `admin_moderation_hub.html` (EMPTY — the two moderation queues).

`library.html` was the obvious third — its two `.lib-empty` blocks are exactly
the NOTHING_FOUND / EMPTY pair — and is deliberately NOT in this change. The
Library redesign (TCRD-224 / #1751) rewrites that same template, so converting
it here would put a third open PR on one file and make the merge order matter.
It is kept as an applyable patch and lands once #1751 is in; nothing else here
depends on it.

The remaining rows in the table above need no further work — they're
already correct, just not yet on the shared macro. Migrating their markup to
`state.panel()` without changing behavior is good follow-on cleanup, not a
bug fix, and is left for a separate pass so this PR stays reviewable.
