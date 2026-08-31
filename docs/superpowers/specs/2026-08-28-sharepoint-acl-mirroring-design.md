# SharePoint permission mirroring into Agnes — Phase-2 access-control design

**Date:** 2026-08-28
**Status:** design draft for review — nothing here is committed to build until
the Q7 answer (§1) and the owner sign-offs in §13 land.
**Anchor:** TCRD-196 "Access control & identity propagation for the fact
graph (Phase 2 design)".
**Parent:** [`2026-08-27-fact-graph-over-collections-design.md`](2026-08-27-fact-graph-over-collections-design.md)
— §5 (enforcement primitive), §13.1 (identity today), §13.2 (wizard/source
card), §15.1 (Phase-ACL tests). This document IS the "Phase ACL" that spec
repeatedly defers to.
**Verified against:** this worktree (branch base `origin/integration`
`5c018ba78`): `app/auth/access.py` (`accessible_collection_ids`,
`_allowed_ids_for_user`), `src/repositories/facts_pg.py` (visibility
predicate landed), `src/repositories/user_group_members{,_pg}.py`
(`replace_synced_groups`), `app/api/admin_sharepoint.py` (wizard scopes →
collections → grants), `connectors/sharepoint/graph_client.py` (app-only
Graph client), `app/worker/registry.py` (three lanes incl. `EXTRACTION_LANE`
— the parent spec's "closed two-value tuple" note is stale), and the
producer crawler (external repo, `crawl.py` — the Phase-ACL `TODO` at its
delta loop is the hook this design fills).

## 0. What this is, and what stays true for V0

The demo-phase decision recorded on TCRD-178 (decision #2) was: **no ACL
mirroring — Agnes collection grants are the authority**, an admin assigns
them, Agnes enforces them, and the honest customer claim is *"an admin
decides who sees which collection and Agnes enforces it"*, never *"Agnes
mirrors your SharePoint permissions"*. That decision **stands for V0** and
nothing below changes V0 behavior.

On 2026-08-28 (canvas review) the project owner ratified that mirroring must
now be added ("musíme přidat to mirorování") — as a **designed, buildable
Phase 2**, not a V0 scope change. This document is that design. It answers,
grounded in the code as it exists today:

1. what is mirrored, into which existing Agnes primitive (§2);
2. the granularity gap between SharePoint's item-level ACLs and Agnes's
   collection-grain fact visibility, honestly (§3);
3. how mirroring composes with the index-time audience-variant mechanism
   agreed on 2026-08-27 (§4);
4. the sync lifecycle — owner, cadence, drift window, revocation SLA,
   outage behavior, audit (§5);
5. the four-link identity-propagation chain TCRD-196 names, end to end,
   with the Graph permission model and its real cost (§6, §7);
6. which leak surfaces mirroring closes and which remain, per Graph
   permission type (§8), and the escape-hatch audit (§9);
7. the repo boundary (§10) and the smallest buildable slice (§11).

One framing rule up front, because two different sanitization problems keep
getting conflated: **document-level redaction** (protected packages, the
anonymizer pipeline, parent §9 / open item O5) and **fact-graph visibility**
(claims, quotes, attrs) are independent surfaces. This design governs the
second. Where the first intersects (audience variants need per-audience
document derivations, §4.4), the intersection is named and routed to O5 —
never silently absorbed.

---

## 1. The Q7 fork: MUST NOT vs SHOULD NOT

The blocking question from the 2026-08-27 customer alignment, asked as Q7
and **still unanswered**: is the requirement *"no cross-audience leak,
ever"* (a hard guarantee) or best-effort? The owner's 28 Aug decision raises
the urgency of mirroring but **does not answer Q7** — "add mirroring" is
compatible with both readings. This design therefore presents both
architectures and marks every section that forks on the answer, so the
build can start on the shared core (§11 slice 1 is Q7-independent) while
the question is escalated.

### 1.1 Shared core (identical under both answers)

- Enforcement stays where the parent spec put it: **in the repository, in
  SQL, before LIMIT** (`facts_pg.py` — verified landed: every read method
  funnels through one visibility predicate over `claims.corpus_id`,
  404-never-403, `AgentPrincipal` intersection honored). Mirroring changes
  *who holds which grant*, never *where grants are enforced*.
- Grants stay **additive** (no deny rows) — parent §13.1: "inherit from
  source is an extra grantee on the row, not a mode".
- Unmatched principals **fail closed** and are surfaced as counts (parent
  §13.2's identity-matching row on the source card).

### 1.2 MUST NOT (hard guarantee) — the deltas

If Q7 answers "MUST NOT, ever", these become requirements, not options:

- **Granularity**: option (b) of §3 — *default-exclude* unique-permission
  subtrees from the crawl — is **mandatory** until option (a) (scope
  splitting) ships. Coarser-than-source visibility with a warning (§3
  option (c)) is disqualified: it is a standing cross-audience leak inside
  a scope.
- **Outage behavior**: stale mirrored grants get a hard ceiling
  (`acl_sync.max_stale_hours`); past it, mirrored grants are **suspended**
  (enforced as absent) until a sync completes — mass false denial is the
  accepted cost of the guarantee (§5.4).
- **Drift window**: the nightly cadence alone is not enough as a
  *guarantee* — it is a ≤24 h leak-after-revocation window. MUST NOT
  requires the webhook-triggered permission re-read (§5.2) in v1.1, and
  the SLA statement must present the window as a bounded *violation
  budget*, agreed with the customer, not as "eventual consistency".
- **Audience variants** (§4): the variant selector must live in the same
  repository predicate as the collection filter (one SQL gate), and
  serving an un-tagged (pre-variant) claim to a non-privileged audience is
  forbidden — re-index before enabling a scope's tiers.
- **Verification**: the parent's Phase-ACL tests (S1-source, S7-source,
  C9) become release gates with the same standing as Phase 0's S-tests.

### 1.3 SHOULD NOT (best effort) — the deltas

- Option (b) default-exclude relaxes to an **advisory** (the admin may
  knowingly include a unique-permission subtree in its parent scope's
  collection, with the warning recorded).
- Outage behavior: stale grants persist with escalating staleness warnings
  on the source card; no automatic suspension.
- Nightly-only cadence is acceptable at v1; webhooks are an optimization.
- The remaining-leaks table (§8.3) is customer documentation, not a
  violation register.

### 1.4 Sections that fork on Q7

| section | forks how |
|---|---|
| §3 recommendation | (b) mandatory vs advisory |
| §5.2 cadence | webhook re-read required vs optional |
| §5.4 outage | suspend vs stale-with-warning |
| §4.3 variant gate | re-index-before-enable required vs staged |
| §11 build order | v1.1 items promoted into v1 under MUST NOT |

Everything else — the mirroring model (§2), the chain (§6, §7), the
escape-hatch audit (§9), the repo boundary (§10) — is Q7-independent.

---

## 2. What is mirrored, into which primitive

### 2.1 The model in one paragraph

For every connected scope (the wizard's `config.scopes` rows,
`app/api/admin_sharepoint.py` — each already mapped to one collection),
Agnes periodically reads the scope root's **role assignments** from Graph
(`GET /drives/{drive}/items/{root}/permissions`), resolves each honored
principal (§8.2) to an **Agnes user group**, resolves each group's members
to Agnes users **by case-insensitive email** (§2.4), and writes two things
through the primitives that already exist: source-segregated
`user_group_members` rows, and auto-managed `resource_grants` rows of type
`COLLECTION` on the scope's collection. Nothing new is enforced — the
mirrored grants are ordinary grants, read by the same
`accessible_collection_ids` → `claims.corpus_id ∈ readable` predicate that
already gates every fact, quote, chunk, and file read.

This is deliberately the **Google Workspace nightly-sync pattern**
(`docs/auth-groups.md`): an external directory materialized into
`user_groups` + `user_group_members` with a `source` tag so the sync only
ever clobbers its own rows. It is the third sync writer after
`google_sync` and `microsoft_sync`.

### 2.2 Groups — naming, ownership, convergence with Entra memberOf sync

- **Entra (AAD) security groups and M365 groups** found in a scope's role
  assignments become Agnes `user_groups` rows named **`entra:<group-oid>`**
  — exactly the naming the parent spec §13.1 reserves for the later
  `/me/memberOf` sync, so the two syncs **converge on the same rows**
  instead of creating parallel near-duplicates. `created_by =
  'system:sharepoint-acl-sync'`; display name (the group's mail/label) is
  UI presentation, not identity — same rule as Google sync, where the
  canonical identifier is the address, because `user_groups.name` is the
  UNIQUE canonical key.
- **Direct user role assignments** (a person granted on the folder, no
  group) collect into one synthetic per-scope group named
  **`sp-direct:<source_scope_id>`**. One group per scope, not one per
  user: grants stay legible on `/admin/access` and the grant count on a
  collection stays O(principals-classes), not O(people).
- Mirrored groups are **read-only in the admin UI and API**, following the
  existing `409 google_managed_readonly` precedent — membership is fixed
  at the source; an admin who wants to add someone adds them in SharePoint
  or via a separate admin-source grant/group (additive, §1.1).
- Group membership rows are written with `source='sharepoint_sync'`. The
  `source` column is free-text with writer-segregation semantics
  (`replace_synced_groups` DELETEs only its own source's rows) — no schema
  change needed. Because a user can hold the same `(user_id, group_id)`
  pair from another source, the existing `ON CONFLICT DO NOTHING` insert
  semantics apply unchanged.

**One repo addition (frozen-pair obligation):** the existing
`replace_synced_groups(user_id, group_ids, source, added_by)` is
*user-oriented* — right for a login-driven sync, wrong for a
resource-driven one (a per-connection sweep computing one group's full
member set would have to pivot the whole tenant into per-user views first,
and two connections syncing concurrently would clobber each other's rows
for shared users). The sync needs the transpose:

```python
def replace_group_members_for_source(
    self, group_id: str, user_ids: list[str], source: str, added_by: str
) -> None:
    """DELETE this group's rows WHERE source=?, then INSERT user_ids —
    the group-oriented transpose of replace_synced_groups, same
    source-segregation invariant, same single-transaction atomicity."""
```

`user_group_members` is a maintained pre-A3 DuckDB↔PG pair, so the method
lands in **both** `user_group_members.py` and `user_group_members_pg.py`
with the cross-engine contract test extended in the same PR.

### 2.3 Grants — provenance without a schema change

Mirrored grants are ordinary `resource_grants` rows written with
**`assigned_by='system:sharepoint-acl-sync'`**. That column already exists
on both backends and is already populated by every writer
(`ensure_grant(..., assigned_by=...)`). The sync's reconciliation deletes
**only** rows whose `assigned_by` is its own sentinel and whose
`resource_id` is one of the connection's scope collections — an
admin-written grant on the same collection is untouchable by construction.

Why not a `source` column on `resource_grants`, symmetric with
memberships: `resource_grants` is a frozen DuckDB↔PG pair and the DuckDB
schema ladder is frozen (`FROZEN_DUCKDB_SCHEMA_VERSION`, A3 ratchet) — a
new column there is exactly the change the ratchet forbids. Why not a new
PG-only `mirrored_grants` table: every enforcement point in the codebase
reads `resource_grants`; a second grant table would have to be unioned
into `can_access`, `_allowed_ids_for_user`, `accessible_collection_ids`,
the agent-scope intersection, and every future reader — a standing drift
risk in the one place drift means a leak. Reusing the existing table means
**enforcement needs zero changes**; the sentinel is bookkeeping for the
writer, and the provenance label the UI shows ("inherited from SharePoint"
— parent §13.2 already requires the label) reads it.

The wizard's step-3 handler (`confirm_scope`) revokes unticked groups by
deleting grants on the collection. It must learn one rule: **manual
un-ticking never deletes a mirrored row** (it would resurrect at the next
sync anyway); the UI shows mirrored grants as non-editable rows with their
provenance label, and "stop mirroring" is a per-scope mode switch (§2.5),
not a checkbox.

### 2.4 Identity join — email, case-insensitively, fail-closed

SharePoint principals resolve to Agnes users by **email/UPN ↔ login
email**. Two verified facts shape the rule:

- Agnes login email lookup is case-sensitive today (`get_by_email`), and a
  case-insensitive sibling already exists (`get_by_email_ci`,
  `src/repositories/users.py`). The Microsoft OAuth provider already
  lowercases the address it extracts. The sync **must** join through the
  case-insensitive path — a UPN `Alice.Novak@…` versus a login
  `alice.novak@…` silently failing closed would present as "mirroring
  randomly misses people" and erode trust in the whole mechanism.
- An unmatched principal (guest `#EXT#` UPN, an email-less service
  principal, a user with no Agnes account) grants **nobody** — fail
  closed — and increments the per-connection unmatched count the source
  card's identity-matching row shows (`38 matched / 4 unmatched — fail
  closed`, parent §13.2). No mapping table: parent §13.1's decision
  ("identity matching needs no mapping table — users by email, groups by
  sync") holds; a mapping table is the rejected alternative recorded in
  §12.

The sync **never creates Agnes user accounts.** A person visible in
SharePoint ACLs but absent from Agnes stays absent (counted). Account
creation remains the provisioning flows' job (OAuth first sign-in, admin
invite).

### 2.5 Per-scope opt-in

Mirroring is **per scope**, chosen in the wizard and changeable on the
collection detail: `access_mode: 'manual' | 'mirrored'` stored on the
scope row in `source_connections.config.scopes` (a JSON list — no schema
change, same as `anonymize`). `manual` is today's behavior and the
default; existing connections are untouched on upgrade. Switching to
`mirrored` runs a first sync inline-ish (enqueues the job and reports);
switching back to `manual` deletes the sync's own grants for that scope
(sentinel-scoped) and **converts nothing** — the admin re-grants manually,
and the wizard's existing "indexed but invisible" warning catches the gap.

---

## 3. Granularity honesty — scope grain vs item grain

Fact visibility is per-collection (`claims.corpus_id`); a scope maps to
one collection; SharePoint permission grain is arbitrary (any folder or
file can break inheritance, `hasUniqueRoleAssignments`). A
broken-inheritance subfolder inside a scope is therefore **finer than
Agnes can express** with collection grants alone. Three options, one
recommendation:

**(a) Split scopes automatically at unique-permission boundaries.** Each
unique-permission subtree becomes its own scope → its own collection →
its own mirrored grants. Exact fidelity at folder grain. Costs: collection
explosion on messy tenants (a library with hundreds of broken-inheritance
folders becomes hundreds of collections); scope churn when permissions
change (a folder whose inheritance is re-linked must merge its collection
back — document identity survives via `corpus_file_sources`, but claims
carry a denormalized `corpus_id`, so a merge is a claims-rewrite);
silent automatic splitting surprises the admin who selected one folder
and finds seventeen collections.

**(b) Exclude unique-permission subtrees from the crawl by default, with
an advisory.** The crawl skips a subtree whose root breaks inheritance;
the wizard and source card show the advisory ("N subtrees excluded —
their permissions differ from the scope's; select them as their own
scopes to include them"). The admin can then *deliberately* promote a
subtree to a scope of its own — which is option (a) executed by a human,
one confirmed decision at a time. Fail-closed: what is not crawled cannot
leak. Costs: content invisible until the admin acts; the advisory must be
loud enough that "we indexed the library" is never silently false.

**(c) Accept coarser-than-source visibility with a documented warning.**
Crawl everything; the scope's collection grant (mirrored from the scope
*root's* ACL) governs all of it, including subtrees the source restricts
further. Cost: a standing within-scope leak — precisely the class Q7's
MUST NOT reading forbids.

**Recommendation: (b) for v1, with (a) as the admin-confirmed migration
path.** Rationale: (b) is the only option that is simultaneously
fail-closed, buildable without a claims-rewrite mechanism, and honest in
the UI. It degrades gracefully into (a): "promote this excluded subtree to
its own scope" is an existing wizard operation (confirm a scope), not new
machinery. Under a SHOULD NOT answer to Q7, (c) becomes available as a
per-subtree admin override on the advisory ("include anyway — the scope's
audience may see this"), recorded in the audit log; under MUST NOT it stays
disqualified (§1.2). The subtree advisory UI is **already being built
separately** — this design consumes it (the excluded-subtree counts ride
the same surface), it does not re-specify it.

File-grain unique permissions (a single shared file inside a scope) are
**not honored in v1 in either direction** — the file is crawled iff its
containing folder chain is, and its visibility is the collection's. The
per-file detection sweep is the cost §6.2 refuses (443k-file reality); the
honest statement ships in the advisory ("permissions finer than folders
are not mirrored") and §8.3 carries it as a named residual.

Migration path (b) → (a), for the record: no schema change — a promoted
subtree is a new scope row + collection; its documents move collections on
the next crawl via the `corpus_file_sources` stable-id upsert (the parent
§6 anchor was built for exactly this class of re-homing), and their claims
are re-pointed by the same replace-mode ingest that handles any content
change. The one rule: the promotion is admin-confirmed, never automatic —
option (a)'s failure mode was *silent* splitting, not splitting.

---

## 4. Composition with index-time audience variants

### 4.1 The agreed mechanism (2026-08-27, restated as constraints)

The mechanism direction agreed between the two engineers on 27 Aug:
anonymization / audience scoping happens at **index time, never in the
crawler** — one fact, N evidence variants, each carrying an `audience`
tag. The canonical example: the same monetary attribute serves `$20k` to
the privileged audience, `<thousands of dollars>` to the practitioner
audience, and a redacted attr to the public one. Two consequences the
design must honor: **audiences must be known at indexing time**, and **a
new audience means a re-index**.

### 4.2 How mirroring composes — two layers, AND-ed, each with its job

```
outer gate (reachability):  claim.corpus_id ∈ accessible_collection_ids(caller)
inner selector (variant):   claim.audience  ∈ audiences(caller)   [NULL = unrestricted]
```

- **The collection grant is the outer gate and always wins on
  reachability.** No audience tag can make a claim in an unreadable
  collection readable — the audience column is a *selector within* the
  readable set, never an alternative route into it. This keeps the parent
  spec's single-join security argument intact: everything a caller sees is
  still projected from claims they can read.
- **The audience tag is the inner selector** — among a subject's readable
  claims, the caller sees the variants whose `audience` maps to an
  audience class they hold (plus untagged claims, which are
  unrestricted-within-collection, i.e. today's behavior). Where several
  variants of the same underlying evidence are visible, the projection
  picks the **most privileged** variant the caller qualifies for; a caller
  with no matching audience class in a tiered scope sees only untagged
  claims — under MUST NOT, a tiered scope has none (§1.2), so they see
  nothing from it.
- **Mirroring's role: it defines and refreshes audience *membership*,
  never audience *classes*.** An audience class (`full`, `redacted`, …) is
  configured per scope at wizard time as a mapping
  `{audience_class → set of Agnes group ids}` — mirrored `entra:<oid>`
  groups being the expected members of that mapping. An ACL change that
  moves people between existing classes is just a membership change:
  picked up by the nightly sync, **no re-index**. Adding a new class is a
  new audience: **re-index the scope** (the agreed consequence, surfaced
  in the UI as a named batch task with its cost, same pattern as
  "Anonymize collection…").

So the full chain the Linear issue asks to see stated:

```
SP ACL ──(nightly sync §5)──► Agnes groups (entra:<oid>, sp-direct:<scope>)
        ├──► resource_grants on scope collections   = outer gate membership
        └──► audience-class mapping {class → groups} = inner selector membership
                    │
   producer/indexer tags evidence variants with audience classes (index time)
                    │
   facts_pg predicate: corpus_id ∈ readable  AND  (audience IS NULL
                        OR audience ∈ caller's classes)   — one SQL gate
```

### 4.3 Which layer wins where — the conflict table

| situation | winner | why |
|---|---|---|
| readable collection, caller in no audience class | collection admits, selector serves untagged claims only | selector narrows, never widens |
| unreadable collection, caller in a privileged class | **collection denies** | outer gate absolute |
| audience-scoped evidence finer than the collection | selector — within the collection | this is the mechanism's purpose |
| unique-permission subtree (§3) | **collection layer** (exclusion/splitting), not audiences | subtree restriction is reachability, not redaction tier — modeling it as an audience would make "can this caller see the document at all" depend on index-time tagging, which re-indexes on every ACL change; wrong tool |
| `revealed` correction | correction (instance-wide, no quotes) — unchanged from parent §4 | admin override outranks both layers by design, labeled in UI |
| admin god-mode | sees everything, both layers | unchanged; parent §5 primitive returns `None` = all |

The subtree row is the important one: **audience variants are for
redaction tiers, collection grants are for reachability.** A boundary
where the source says "these people cannot see these documents *at all*"
must be a collection boundary (§3), because reachability must never
depend on the extraction pipeline having run correctly.

### 4.4 Schema and enforcement placement (Q7-sensitive)

V1 ships **without** variants (§11); the schema reserves the seam: adding
`claims.audience TEXT NULL` is one Alembic revision on a PG-only table
(no frozen-pair cost), the wire format gains an optional
`evidence[].audience`, and the predicate gains one AND term **inside
`facts_pg`'s existing visibility helper** — the same single place, so S2's
attribute-oracle guarantee extends to variants mechanically. Under MUST
NOT, enabling tiers on a scope requires the re-index to have completed
(no untagged full-detail claims may remain in a tiered scope) — the
enable is a gated batch task, not a flag flip.

The document-store intersection is real and out of scope here: N audience
variants of *evidence* imply N derivations of the *document text* the
verbatim gate checks quotes against (`corpus_chunks` holds one text per
file today). That is the O5 reconciliation (anonymizer-in-front vs
two-variant intake) extended from 2 variants to N — routed to O5's owner
(§13), explicitly not solved by this document.

---

## 5. Sync lifecycle

### 5.1 Who runs it

A worker job kind — **`sharepoint-acl-sync`** — registered in
`app/worker/kinds.py`, **LIGHT lane** (network-bound Graph paging + a few
hundred repo writes; nothing that belongs in HEAVY's concurrency-1 slot or
the extraction lane), default lease `_DEFAULT_LIGHT_LEASE_S`, retry 300 s.
One job per connection per cycle, enqueued by the same scheduler surface
that drives the other nightly kinds (`marketplaces-sync` precedent), plus
an on-demand **"Sync access now"** action on the source card (admin-gated,
enqueues the same kind — never a second code path). The job:

1. resolves the connection's certificate → app-only Graph token (the
   existing `resolve_sharepoint_settings` + `get_app_token` seam — typed
   409/502 failures surface on the source card exactly as the wizard's
   do);
2. for each `access_mode='mirrored'` scope: reads the scope root's
   permissions, classifies each permission per §8.2, expands honored
   groups via `transitiveMembers` (paged);
3. computes the target state (groups, memberships, grants) and diffs
   against current sync-owned rows;
4. writes: `ensure` groups → `replace_group_members_for_source` per group
   → `ensure_grant`/`delete` (sentinel-scoped) per collection — each
   collection's grant set updated atomically enough that a scope never
   passes through a granted-to-nobody window it didn't have before;
5. records the run: matched/unmatched counts, per-scope grant deltas,
   duration, and errors into the connection's `config` (last-run block,
   the source card's data) and one `audit_log` entry per **change**
   (`sharepoint_acl.grant_added`, `.grant_removed`,
   `.membership_replaced` with counts, `.principal_unmatched`) plus one
   per run (`sharepoint_acl.sync_completed` / `.sync_failed`). Grant
   *changes* are individually audited — revocations especially, because
   "when did access actually end" is the question an incident asks.

No new tables: run bookkeeping lives in the connection config JSON
(pattern already used for `scopes`), memberships and grants in their
existing tables. If run history outgrows the config blob, a PG-only
`acl_sync_runs` table is the A3-conformant escape (parallel to
`facts_ingest_runs_pg`), noted as future work, not built now.

### 5.2 Cadence and drift window

- **Nightly per connection** (default), plus on-demand. The **drift
  window** for a source-side change is therefore ≤ 24 h + run duration.
- Delta crawls **do not** report permission-only changes (parent test C9
  names this) — the nightly permission re-read is the floor, not an
  optimization.
- **v1.1 (promoted into v1 under MUST NOT, §1.2):** the producer's Graph
  drive subscriptions (already built in the producer repo:
  `subscriptions.py`, notification → targeted delta) additionally enqueue
  an ACL sync for the affected connection on any notification — Graph
  webhooks do not say *what* changed, so any change triggers the cheap
  scope-root re-read. This shrinks the typical revocation latency from
  hours to minutes without trusting webhooks for correctness (nightly
  remains the guarantee; webhooks are acceleration).

### 5.3 Revocation latency — the SLA statement

Stated the way the parent spec demands product claims be stated (S7:
"the test measures the latency and records it"):

> A revocation made **in Agnes** (grant removed, group deleted, mirroring
> disabled) is enforced immediately — the next request evaluates the live
> predicate. A revocation made **in SharePoint** is enforced no later than
> the next completed ACL sync: ≤ 24 h by default, minutes when webhook
> acceleration is enabled. Between the source-side change and the sync,
> Agnes serves what the last sync mirrored.

The Phase-ACL S7-source test measures the source-side number per release;
the number goes in customer material, and under MUST NOT it is presented
as the agreed violation budget (§1.2), not as fine print.

### 5.4 Graph outage — fail-closed vs stale-grant grace (Q7 fork)

A failed sync **never partially applies**: the diff is computed against a
fully-read target state; a Graph error mid-read aborts the scope's
reconciliation, keeps the previous rows, marks the run failed, and shows
staleness on the source card ("access last mirrored N hours ago"). Then:

- **SHOULD NOT:** stale mirrored state persists indefinitely with
  escalating warnings. Rationale: mass false denial on every Graph
  hiccup is a worse product than a bounded staleness window, and manual
  Agnes-side revocation remains available for urgent cases.
- **MUST NOT:** `acl_sync.max_stale_hours` (default 72) caps the grace;
  past it, the connection's mirrored grants are **suspended** — the
  enforcement predicate treats them as absent (implementation: the sync
  marks the connection stale in config; `accessible_collection_ids` is
  NOT touched — instead the suspension deletes the sentinel grants and
  the next successful sync rewrites them, so the enforcement path stays
  zero-special-cases). Loud on the source card and in the audit log.

Fail-soft on *identity resolution* is different from fail-soft on
*reachability*: a group whose `transitiveMembers` read failed keeps its
previous membership (Google sync's fail-soft precedent — a transient API
error must not empty a group), but is flagged stale in the run report.

---

## 6. The four-link chain — link 1 and its real cost

TCRD-196 names four links: (1) the crawler captures access metadata, (2)
the indexer attaches audience scoping, (3) the tool layer filters by
caller identity, (4) Agnes forwards caller identity to the tools. Links
3–4 are assessed in §7 (they mostly exist); link 2 is §4. Link 1:

### 6.1 What is read, with which permission

- **Scope-root role assignments** (Agnes-side, the §5 job):
  `GET /drives/{drive-id}/items/{item-id}/permissions`. Reading
  SharePoint item permissions app-only requires **`Sites.FullControl.All`**
  — a hard step up from the crawler app's current read-only grant
  (`Sites.Read.All` + `Files.Read.All`, per the producer's auth header).
  This is the single most consequential operational ask in this design:
  the customer's tenant admin must consent to a full-control application
  permission for a feature that only reads. Two mitigations, both
  recommended: (i) a **separate app registration** for the ACL reader, so
  the crawl credential keeps its read-only blast radius and the
  full-control credential can be independently audited/revoked; (ii)
  **`Sites.Selected`** as the preferred alternative where the tenant
  supports granting it per-site with full-control role on just the
  connected sites — narrower consent, same API. The wizard's certificate
  step gains a second credential slot; absence of the ACL credential
  degrades to `access_mode='manual'` with a named reason, never a broken
  crawl.
- **Group expansion** (Agnes-side): `GET /groups/{oid}/transitiveMembers`
  (paged, `$select=id,mail,userPrincipalName`) — requires
  **`GroupMember.Read.All`** (or `Directory.Read.All`), which
  `Sites.FullControl.All` does **not** include. Named here so the consent
  screen holds no surprises.
- **Broken-inheritance detection** (producer-side, feeding §3's
  exclusions and the advisory): the folder-walk probes
  `hasUniqueRoleAssignments` per folder. The crawler's delta loop already
  carries the `TODO(Phase ACL)` hook at exactly the right place; folders
  currently fall through its `"file" not in item` skip, so detection is
  an added branch on folder items during full walks, plus a periodic
  re-sweep (permission-only changes never appear in delta).

### 6.2 Cost model, against the first production library's real numbers

The reference library: **~443k files, ~98k folders**. SharePoint Graph
throttling is **pooled per app per tenant** (resource-unit budgets — the
crawler already lives inside this budget and already implements
`Retry-After`/backoff; the ACL reader shares the tenant pool even from a
separate app registration's *SharePoint* allocation, so cost here is
budget the crawl does not get).

| read | volume | verdict |
|---|---|---|
| scope-root permissions | tens per connection, nightly | negligible |
| group `transitiveMembers` | per distinct honored group, paged | negligible (dozens of requests) |
| folder `hasUniqueRoleAssignments` sweep | ~98k probes | **the expensive one**: `$batch` (20 inner requests each) amortizes HTTP round-trips but **not** the throttle budget — inner requests bill individually. At a sustained effective few req/s under app-only throttling this is a multi-hour pass. Feasible as: full probe during the initial crawl (folders are enumerated anyway), then a **periodic re-sweep** (weekly default, configurable) rather than nightly |
| per-FILE permission reads | ~443k | **refused** — this is why file-grain fidelity is out (§3): a >12 h nightly permission pass that starves the content crawl's throttle budget buys grain the collection model cannot express anyway |

The run report carries the sweep's request count and 429 time so the cost
stays observed, not assumed — same discipline as the crawler's existing
`crawl-report.json` cost accumulators.

### 6.3 Division of labor (matches the repo boundary, §10)

- **Agnes** reads scope-root permissions and group membership (it already
  holds an app-only Graph client and the certificate resolution seam) —
  the sync must not depend on a producer run to refresh access.
- **The producer** detects broken-inheritance subtrees (it is the thing
  walking every folder) and reports them per scope in its index/ingest
  stream; Agnes turns them into the §3 advisory and (option (b))
  exclusion list the crawler consumes back. The crawler **never decides
  access** — it captures metadata and honors the exclusion list; the
  decision surface is Agnes's.

---

## 7. The chain end-to-end — one user, one agent

Concrete walk, all steps naming the code that exists or the § that adds
it. Setup: scope "/Engagements" on connection `spc_1`, mirrored; its
collection `col_eng`; the folder's SP ACL grants read to AAD group
*Consulting* (oid `g-123`); Alice is a member in Entra; her Agnes login is
`alice@example.com`; her agent `research-bot` is scoped
(`'selected'`) to a collection subset that includes `col_eng`.

**Human caller (chat, web, CLI, Slack — one path):**

1. **SP ACL → Agnes groups.** Nightly `sharepoint-acl-sync` (§5) reads
   `/permissions` on the scope root → sees `g-123`/read → ensures group
   `entra:g-123` → `transitiveMembers` → resolves Alice by
   case-insensitive email → `replace_group_members_for_source('entra:g-123',
   [...alice...], source='sharepoint_sync')`.
2. **Groups → collection grants.** Same run:
   `ensure_grant('entra:g-123', COLLECTION, 'col_eng',
   assigned_by='system:sharepoint-acl-sync')`.
3. **Grants → claims visibility.** Alice asks a question; the facts tools
   call `facts_repo().search(caller=alice_user, ...)`; `_readable_ids`
   delegates to `accessible_collection_ids(alice)` → her group grants
   (now including `col_eng` via `entra:g-123`) ∪ collections she owns →
   the SQL predicate `claims.corpus_id = ANY(:readable)` admits `col_eng`
   claims → the fact, its attrs projection, and its verbatim quotes are
   served; the chat footer's scope line counts `col_eng` among her M
   collections. *(With audience variants, §4: the same predicate's
   AND-term selects the variant whose audience class maps to a group she
   holds.)*
4. **Revocation, both directions.** Alice leaves *Consulting* in Entra →
   next sync rewrites `entra:g-123` membership → her next request finds
   `col_eng` gone from `readable` → the same subject is now a 404
   indistinguishable from nonexistence (S6). An admin deleting the grant
   in Agnes is immediate.

**Agent caller (link 4 — identity forwarding, assessed):**

5. `research-bot` runs as an **`AgentPrincipal`** — already a live,
   enforced primitive: `accessible_collection_ids` returns the live
   intersection (owner grants ∩ agent scope) **unmodified** for
   `PRINCIPAL_TYPES`, with no ownership union and no admin god-mode
   (verified in `app/auth/access.py`; `facts_pg` funnels every read
   through it; parent test S5 pins it). Alice's mirrored grant flows into
   the intersection because the intersection reads her grants live — a
   mirrored revocation therefore narrows her agents at the same moment it
   narrows her.
6. **The gap to close:** the intersection carries *collection ids*, not
   *groups* — sufficient for the outer gate, insufficient for the §4
   audience selector, which needs the caller's audience classes
   (group-derived). Rule, decided here: an `AgentPrincipal`'s audience
   set is **the least-privileged class of each tiered scope** unless the
   agent's scope declaration explicitly pins a class ≤ the owner's own
   (checked live, like everything else about agent authority). Fail
   toward redacted: an agent's output can be forwarded anywhere; the
   privileged variant should require an explicit, auditable opt-in on the
   agent. This is a small, named work item (§11 slice 3 / §14) — not a
   redesign; it extends the same intersection machinery.
7. Slack/chat surfaces resolve the platform identity to the Agnes user
   before any tool call, so steps 3–6 are identical there — which is why
   the parent's §15.5 Slack-parity run is the surface-divergence catch
   for this design too.

---

## 8. Leak surfaces — closed, honored, and remaining

### 8.1 What mirroring closes

The TCRD-178 accepted leak was: **verbatim quotes (and facts, and chunks)
served to Agnes users who cannot read the source document in SharePoint**,
because hand-assigned collection grants are coarser and drift from the
source. Mirroring closes, at scope grain:

- **wrong-audience hand-grants** — the grant set on a mirrored scope is
  computed from the source ACL, not from an admin's recollection of it;
- **revocation drift** — bounded by §5.3's window instead of unbounded
  ("someone loses access at the source and keeps it here" was §13.1's
  named risk; it becomes a measured ≤24 h / minutes window);
- **onboarding drift** in the other direction — a person newly granted at
  the source appears in Agnes at the next sync without a ticket.

### 8.2 Graph permission types — honored in v1, or out with a reason

A Graph `permission` object carries `roles` plus one of several grantee
shapes. The classification the sync applies to each entry on a scope root:

| type | v1 | reason |
|---|---|---|
| direct user role assignment (`grantedToV2.user` with resolvable email) | **honored** → `sp-direct:<scope>` | the base case |
| Entra security / M365 group assignment (`grantedToV2.group`) | **honored** → `entra:<oid>`, transitive expansion | the base case; covers group-connected team sites' Members/Owners in the common modern configuration |
| inherited permissions | **honored implicitly** — the scope *root's* effective permission set IS the inherited baseline; that is what the read returns | inheritance below the root is uniform by construction once §3(b) excludes broken subtrees |
| SharePoint site groups (`grantedToV2.siteGroup`) | **out** — counted unmatched, fail closed | site-group membership is not enumerable through the app-only Graph surface the connector uses (it lives behind the SharePoint REST API); resolving it means a second protocol client for a shrinking legacy configuration. Revisit if unmatched counts show it matters on real tenants |
| sharing links, "specific people" (`link` + `grantedToIdentitiesV2`) | **out** — counted, fail closed | links are per-item sharing, not scope ACL: honoring them at the scope root mirrors almost nothing (links overwhelmingly live on items below the root, which v1 does not probe — §6.2), and honoring a root-level link would grant the whole collection off a one-document intent. Under-sharing is the safe direction |
| sharing links, "people in your organization" | **out** in v1 | mapping to `Everyone` would grant the whole instance's user set off one link — the blast radius is wrong for an automatic write; if real tenants use org-links on scope roots deliberately, an explicit admin confirmation flow can add it |
| anonymous links | **never** | no enumerable principals; an anonymous link mirrored into Agnes has no meaning that is not a leak |
| external/guest users (`#EXT#`, cross-tenant) | **out** — counted, fail closed | no Agnes account to resolve to; provisioning externals is an identity decision, not a sync side effect |
| application principals | **out** — counted | services are not readers |

Every "out" row lands in the unmatched/unhonored counts on the source
card — parent §13.2's rule that fail-closed absence must be *visible*, or
under-sharing looks like a bug.

### 8.3 What remains, stated for customer material

1. **Within-scope, sub-folder restrictions** — v1 excludes broken
   subtrees (§3(b)); what remains inside an included scope is uniform by
   construction, but **file-grain** restrictions are not mirrored (§3):
   a single restricted file inside an open folder is visible at the
   collection's grain. Named in the advisory.
2. **The drift window** (§5.3) — bounded, measured, nonzero.
3. **Detection staleness for broken inheritance** — a folder whose
   inheritance breaks *after* the last sweep is excluded only at the next
   sweep (weekly default); until then its content, crawled under the
   scope's grant, is the residual §3(c) exposure. Under MUST NOT this
   motivates promoting the sweep cadence alongside §5.2's webhooks.
4. **Admins and `revealed` corrections** — god-mode and the instance-wide
   correction override are unchanged, deliberate, and labeled (parent §4).
5. **Verbatim quotes vs conversion drift** — a quote is verbatim against
   the extraction, not the live source document (parent §8); mirroring
   changes who sees it, not what it is.
6. **Agnes accounts are the perimeter** — mirroring governs Agnes users'
   visibility; it cannot restore SharePoint's audit trail for reads that
   happen inside Agnes. Agnes's own audit log is the compensating record
   (RBAC docs' shared-service-account paragraph, unchanged).

### 8.4 The two sanitization surfaces, once more

Document-level redaction (protected packages; the anonymizer; O5) decides
**what text exists** in Agnes. This design decides **who can read which
claims/quotes/collections**. They compose — an anonymized scope's mirrored
grants gate the anonymized derivation — but neither substitutes for the
other, and §4.4's variant/derivation intersection is the one place they
must be designed together (owner: O5's).

---

## 9. Escape-hatch audit — every raw path, named

The Linear issue's standing worry: a read-only `graph_sql`-style tool
reading tables directly bypasses any audience filter. Audit of the current
tree (verified by grep/read on this branch):

| surface | status |
|---|---|
| `POST /api/facts/search` / `/neighbors` / `/{id}/claims`, `agnes facts *`, MCP `fact_search`/`fact_neighbors`/`fact_claims` | **filtered** — all funnel through `facts_pg`'s single visibility helper (module contract: "every read method funnels through the SAME visibility primitives"; `_readable_ids` → `accessible_collection_ids`; 404-never-403; principal-safe) |
| `graph_sql` (the sandbox spike's raw-SQL MCP tool) | **retired — zero occurrences in Agnes** (`grep -r graph_sql app/ src/ cli/` is empty); the parent spec records the lesson as "no general SQL escape hatch over these tables, in any surface", and this design re-ratifies it as a standing review rule for every future facts/claims surface |
| `/api/query`, `/api/query/hybrid`, `agnes query` | **cannot reach claims** — they execute against analytics DuckDB (`analytics.duckdb` / registered BQ paths); facts/claims are app-state Postgres, never ATTACHed, never in `table_registry`, never distributed by `agnes pull` (parent §2's consequences, verified still true) |
| collections/chunks reads (`knowledge_search`, `/raw`, `/preview`, Library) | **filtered** by the same collection primitive — and must adopt the §4 variant selector in the same change that adds variants, or chunks leak what claims withhold (named in §11 slice 4) |
| PuppyGraph / external graph engines | **not wired**; the parent's removal note stands — its spike read `public.nodes` directly and embedded credentials; any future engine sits behind the visibility layer or does not ship |
| direct Postgres credentials | infrastructure boundary, not an app surface — DB credentials are operator-scope; noted so nobody mistakes app-layer completeness for total coverage |

Rule going forward (sync-map material, §11): **any new surface that can
emit a claim, quote, or claim-derived attr must go through
`facts_pg`'s visibility helpers** — a PR adding one without touching that
module is the review smell.

---

## 10. Repo boundary — what lives in public Agnes

The vendor-agnostic rule applied:

- **In Agnes (this repo):** everything SharePoint-*generic* — the
  `sharepoint-acl-sync` job, the Graph permission/`transitiveMembers`
  client additions (`connectors/sharepoint/graph_client.py`), the
  group/grant mirroring writes, the wizard `access_mode` field, the
  source-card rows, the audience-class *mechanism* (§4). SharePoint as a
  connector name is fine here, exactly as the connector already is.
- **In the producer repo:** broken-inheritance detection during the walk,
  the exclusion-list consumption, the per-scope subtree report — crawler
  concerns, and per O1 the producer is run end-to-end by us but remains a
  separate repo against the §7 contract.
- **Data, never code:** audience-class definitions, group→class mappings,
  scope selections, tier vocabularies. No customer's tier names, group
  names, or tenant shapes appear in either repo's code; they are rows in
  config the wizard writes. (The test for this boundary: a second
  customer with different tiers onboards with zero code change.)
- **Not in Agnes at all:** customer-shaped answer-time redaction prompts
  or per-customer policy engines. If a customer needs logic the
  class-mapping cannot express, that is a design conversation, not a
  fork.

---

## 11. V1 scope cut and build order

Smallest buildable slice, honoring the repo's conventions (PG-first
ratchet, frozen-pair obligations, sync-map, feature-flag trio, CHANGELOG,
draft-PR-then-CI):

**Slice 1 — mirroring core (Q7-independent; the §2/§5 mechanism):**

1. `connectors/sharepoint/graph_client.py`: `list_item_permissions`,
   `list_group_transitive_members` (paged, `$select`-minimal, same typed
   `SharePointGraphError` seam and mock-transport test idiom).
2. `user_group_members` frozen pair: `replace_group_members_for_source`
   in **both** siblings + contract test extension, same PR.
3. The `sharepoint-acl-sync` job kind (LIGHT lane) with the §5.1
   read-classify-diff-write-audit loop; per-scope `access_mode` honored;
   sentinel-scoped grant reconciliation; unmatched counts + last-run
   block in connection config; audit actions.
4. Wizard/API: `access_mode` on the scope row (`confirm_scope` body +
   `_scope_out`), mirrored-grant rows rendered read-only with the
   "inherited from SharePoint" provenance label (the label the parent
   §13.2 already reserves); "Sync access now" on the source card.
5. Feature flag: `acl_mirroring.enabled` (default **false**) via
   `feature_enabled(...)` + `app/switches.py` + `docs/feature-flags.md` —
   the same trio as `facts.enabled`.
6. Tests: the Phase-ACL set the parent §15.1 pre-declared — S1-source
   (sharing set in SharePoint only), S7-source (measured source-side
   revocation latency), C9 (permission-only change caught by the
   re-read) — plus sync-unit tests: source segregation (a sync never
   touches admin rows), sentinel-scoped deletion (a sync never deletes an
   admin grant), fail-soft membership on expansion failure, unmatched
   fail-closed, case-insensitive join (fixture with mixed-case UPN).

**Slice 2 — granularity (§3, needs the producer):** folder
`hasUniqueRoleAssignments` probing in the crawler's walk + periodic
re-sweep + per-scope subtree report → Agnes advisory counts (the advisory
UI itself is **already being built separately** — this slice feeds it,
it does not build it) → default-exclude list consumed by the crawl
(option (b)); "promote subtree to scope" = the existing confirm-scope
flow.

**Slice 3 — identity-forwarding gap (§7.6):** audience-class membership
resolution for `AgentPrincipal` (least-privileged default, explicit
pinned-class opt-in on the agent scope), extending the live-intersection
machinery. Small, but it must exist before any tiered scope is enabled
for agent access.

**Slice 4 — audience variants (§4; gated on Q7 + O5):**
`claims.audience` Alembic revision (PG-only, no frozen-pair cost), wire
format `evidence[].audience`, the one AND-term in `facts_pg`'s predicate,
chunk/`/raw` variant alignment (§9), per-scope class→groups mapping in
the wizard, re-index-as-batch-task. Not started until Q7 is answered and
the O5 derivation question has an owner-approved shape.

**Builder obligations riding along** (so the plan carries them): new
routes into the PG smoke `COVERED_ROUTES`; PG-only surfaces into both
parity-sweep exemption maps with the typed-501 proof; REST×CLI×MCP is
**not** triggered by slice 1 (admin-gated sync trigger is REST+UI only —
recorded as a deliberate exemption the way the parent recorded
corrections-API-only); CHANGELOG bullets per PR; the §9 review rule
proposed as a sync-map row ("claims-emitting surface ⇒ `facts_pg`
visibility helpers").

---

## 12. Rejected alternatives (recorded so nobody re-proposes them)

- **A principal↔user mapping table.** Parent §13.1 already decided
  against; nothing here needs it — email join + fail-closed counts cover
  v1, and a mapping table is where silent over-grants go to hide.
- **A `source` column on `resource_grants`** — forbidden by the frozen
  DuckDB ladder (A3); `assigned_by` sentinel does the job with zero
  enforcement-path changes (§2.3).
- **A separate `mirrored_grants` table** — every enforcement point would
  need a union; drift there is a leak by construction (§2.3).
- **One Agnes group per SP *user*,** or direct per-user grants —
  explodes `/admin/access`, and per-user grants have no writer-
  segregation story; the per-scope `sp-direct` group keeps the writer
  model uniform.
- **Modeling broken-inheritance subtrees as audiences** — reachability
  decided at index time by the extraction pipeline; wrong layer (§4.3).
- **Answer-time (LLM/prompt-layer) redaction as the enforcement
  mechanism** — the store/query layer is where both Q7 answers put the
  gate; prompt-level filtering is the thing the parent's whole §5 exists
  to not rely on. (Index-time variants + SQL selection is the agreed
  direction precisely because it is enforceable below the model.)
- **Mirroring via the crawler** (producer writes grants through ingest) —
  access refresh would then depend on content-crawl cadence and producer
  uptime; the ACL sync must be Agnes-owned (§6.3).
- **`Sites.FullControl.All` on the existing crawler app** — one
  credential holding read-everything *and* full-control; separate
  registration (or `Sites.Selected`) keeps blast radii apart (§6.1).

---

## 13. Open items (owners to assign — first question, not last)

- **Q7 — MUST NOT vs SHOULD NOT** (§1): with the customer; blocks §3's
  final form, §5.4, slice-4 gating, and the SLA framing. The single
  blocking item.
- **QA — tenant consent** for `Sites.FullControl.All` or `Sites.Selected`
  + `GroupMember.Read.All` on a (preferably new) app registration —
  customer tenant admin; blocks slice 1 live testing (unit tests run on
  the mock transport regardless).
- **QB — O5 reconciliation extended to N variants** (§4.4): the
  document-derivation store for audience-tiered scopes — same owner as
  parent O5; blocks slice 4.
- **QC — audience-class authoring UX**: where the class→groups mapping
  lives in the wizard and what the re-index batch task shows — with the
  UI owner alongside the (already in-flight) subtree advisory.
- **QD — sweep cadence under MUST NOT** (§8.3 item 3): whether weekly
  broken-inheritance re-detection satisfies the violation budget or the
  webhook path must also trigger sweeps — decide with Q7.
- **QE — site-group prevalence check** (§8.2): after the first real
  tenant sync, read the unhonored counts before deciding whether the
  SharePoint-REST site-group resolver is ever worth building.

---

## 14. TCRD-196 acceptance-criteria mapping

| checkbox | where this design answers it |
|---|---|
| Q7 (MUST/SHOULD) recorded, both architectures presented | §1 — shared core, both deltas, per-section fork table; still **open**, escalation owner Q7 |
| index-time vs answer-time decision | §4 + §12: index-time variants + store/query-layer SQL enforcement ratified; answer-time redaction rejected as an enforcement mechanism |
| audience mechanism composed with ACL mirroring | §4.2–§4.3: mirroring feeds audience *membership*; classes are wizard data; collection gate outer, audience selector inner; conflict table says which layer wins where |
| four-link chain designed | §6 (link 1 + cost against 443k/98k), §4 (link 2), §7 (links 3–4 walked end-to-end for a user and an agent) |
| escape hatches named and closed | §9 — per-surface audit of the current tree; `graph_sql` verified retired; sync-map rule proposed |
| repo boundary decided and justified | §10 — SharePoint-generic in Agnes, detection in the producer, customer shape as data only |
| per-ticket scope impacts | §11 slices 1–4 with conventions; §3's granularity decision (b→a); §5's lifecycle incl. SLA statement |
| new Agnes identity-forwarding work item | §7.6 / §11 slice 3 — `AgentPrincipal` audience-class resolution, least-privileged default, explicit pinned-class opt-in |
