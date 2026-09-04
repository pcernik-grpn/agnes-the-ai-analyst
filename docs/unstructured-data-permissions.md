# Permissions on unstructured data — how they work, how to set them up

The question this page answers: **"how do I make it so that one person can
read the contents of certain files and another person cannot?"**

Short version: the unit of permission is the **Collection**, not the file and
not the folder. A file lives in exactly one collection; a group is granted the
collection; everyone in that group can read what is inside it. Everything else
on this page is a narrowing of that one rule — a single-file exception, two
detail levels inside one collection, or keeping content out of Agnes
altogether.

Structured data (tables) is a different mechanism with a different document:
[`RBAC.md`](RBAC.md) for grants, [`table-access-policies.md`](table-access-policies.md)
for row/column filtering. Nothing here applies to tables, and nothing there
applies to documents.

---

## 1. The objects, and which one carries the permission

```
SharePoint site / folder            ← the source
        │  connect wizard: one "scope" per selected folder
        ▼
Collection  (file_corpora)          ← THE PERMISSION UNIT
        │
        ├── files      (corpus_files)        ← optional per-file grant
        ├── chunks     (corpus_chunks)       ← what search returns
        ├── claims     (fact graph)          ← what agents cite
        └── digest / knowledge.duckdb        ← what `agnes pull` distributes
```

A collection is created two ways: an admin selects a SharePoint folder in the
connect wizard (one collection per selected folder), or somebody uploads files
in the Library. Either way it is the same object with the same permission
model.

Consequences worth stating out loud before any configuration:

- **A collection is all-or-nothing by default.** Reaching a collection means
  reaching every file in it, its chunks, and its claims.
- **Therefore the folder layout you connect *is* your permission layout.** If
  two audiences must not see the same documents, connect two folders, not one.
  This is the single most important design decision on this page, and it is
  made before any grant is written.
- **Grants are additive. There are no deny rules.** Access is the union of
  everything granted; you cannot subtract from a grant with another grant.

---

## 2. Layer 1 — who can reach a collection

The one mechanism: a `resource_grants` row tying a **group** to
`(collection, <collection-id>)`. Ordinary RBAC, the same primitive that grants
tables, packages, agents and plugins ([`RBAC.md`](RBAC.md)).

A collection is readable by:

| who | why |
|---|---|
| members of any group holding a grant on it | the normal path |
| its creator (`created_by`) | uploads stay reachable to their owner |
| members of the `Admin` group | god-mode short-circuit on every check |

and by nobody else. A new collection is **private to its creator** until a
grant exists — there is no implicit "everyone" default. (One narrow exception,
off by default: `library.auto_share_admin_uploads` auto-shares a collection an
*admin* creates in the Library to `Everyone`, revocable per collection in the
Share dialog. Non-admin uploads are never auto-shared.)

### How to write the grant

Four surfaces, one effect:

```bash
# CLI
agnes admin grant create Finance collection col_a1b2c3
agnes admin grant list --type collection
```

```bash
# REST
curl -X POST https://<host>/api/admin/grants \
  -H 'Content-Type: application/json' \
  -d '{"group_id":"<group-id>","resource_type":"collection","resource_id":"col_a1b2c3"}'
```

- **`/admin/access`** — the admin page; pick a group, tick what it can reach
  (or pick a collection and see which groups reach it).
- **Library Share dialog** — `/library/<slug>` → Share. This one is
  *owner-scoped*: the creator of a collection can share it with groups they
  themselves belong to, plus `Everyone`. An owner can neither push an item into
  a team they are not in, nor revoke a grant an admin wrote.
- **The SharePoint connect wizard, step 3** — the group checkboxes there write
  exactly these same `resource_grants` rows on the scope's collection. There is
  no separate "SharePoint sharing" table.

### How it is enforced

Every read surface resolves the caller's set once through one function
(`app/auth/access.py::accessible_collection_ids`) and filters against it:
`GET /api/collections`, `/api/collections/search`, the file preview and raw
download, `/api/knowledge/search`, the Library pages, the chat and MCP tools
(`collections_list`, `collections_search`, `knowledge_search`), the fact-graph
reads, and the `agnes pull` manifest. An unreachable collection answers **404,
never 403**, so the URL space cannot be probed for what exists.

Two caller kinds behave deliberately differently:

- **An agent (or an agent PAT)** gets the live intersection of its owner's
  grants and its own declared scope — never its owner's ownership, never its
  owner's admin god-mode. An agent cannot reach a collection its scope does not
  name, even if its owner is an admin.
- **A delegated sub-request** runs as the *original caller*, not as either
  agent's owner, so delegation cannot widen reach.

---

## 3. Layer 2 — one file, not the whole collection

Sometimes the honest answer is "this one document should be visible to one more
person". That is a grant on the **file**, resource type `corpus_file`:

```bash
agnes admin grant create Legal corpus_file <file-id>
```

or in the UI, `/library/<slug>/f/<file-id>` → Share.

What it does: it widens the **preview** and **raw download** of that one file.
It does not put the file in any listing, does not make the parent collection
reachable, and does not surface the file in search — those stay collection-
scoped. Use it for "a file shared out of a folder", not as a general
file-by-file permission scheme. (If you find yourself writing many of these,
the folder layout is wrong — see §1.)

---

## 4. Layer 3 — two audiences inside one collection (audience classes)

For the case where the same documents must answer *differently* for two
audiences — full detail for one group, a reduced view for another — a
SharePoint scope can carry an ordered list of **audience classes**, each mapped
to Agnes groups.

```jsonc
// POST /api/admin/sharepoint/connections/{id}/scopes
{
  "source_scope_id": "01ABC…",          // the Graph item id of the folder
  "display_path": "HR Site/Documents/Personnel",
  "drive_id": "b!xyz…",
  "audience_classes": [
    {"name": "full",     "group_ids": ["<hr-leads-group-id>"]},
    {"name": "redacted", "group_ids": ["<all-employees-group-id>"]}
  ]
}
```

**Order is the privilege ranking** — most privileged first. `name` is the tag
carried on each extracted claim (`[a-z0-9_-]{1,64}`).

Which classes a caller holds (`src/audience_classes.py`, resolved live on every
request, never cached beyond it):

| caller | classes held |
|---|---|
| admin | all of them |
| plain user | every class whose `group_ids` intersect their current group memberships |
| agent principal | the **least**-privileged class by default; its scope-pinned class only if the pin still exists *and* the agent's owner holds that class or better |
| co-session principal | the least-privileged class, always |

Every failure mode — a renamed class, a stale pin, an owner who lost the class,
an unresolvable identity — degrades to least-privileged. It never degrades to
"unchecked".

What tiering actually changes:

1. **Fact-graph reads.** Claims are tagged with an audience at index time. The
   filter is one SQL predicate applied before `LIMIT`, and where several
   variants of the same claim exist, the caller gets the most privileged one
   they hold.
2. **Document text.** For a tiered collection, raw bytes, the text preview and
   `/api/collections/search` snippets are served only to a caller holding the
   collection's **top** class (or an admin). Everyone else gets the collection's
   listing and metadata but no text. This is a deliberately blunt stand-in:
   per-audience *document derivations* do not exist yet, so the fallback is
   "top class only" rather than "some redacted version of the file".
3. **Untagged claims** (indexed before tiers were enabled) depend on the
   guarantee mode — see §7.

Two caveats to take seriously:

- Audience classes are configured **through the API only** today. The connect
  wizard's UI has no tier editor.
- Enabling tiers on a scope that already has indexed content does not retag
  it. Re-index the scope, or rely on `must_not` mode hiding untagged claims from
  non-admins.
- **Tiering is a server-read control, not a distribution control.** See the
  limits in §10 before you rely on it.

---

## 5. Layer 0 — keeping content out of Agnes entirely

The strongest permission is content that was never ingested. Four mechanisms,
all automatic once a scope is mirrored:

- **Don't connect the folder.** A scope nobody selected produces no collection
  and no content. Boring, and the most reliable control on this page.
- **Files with unique permissions are excluded.** The sweep probes files, not
  just folders; a file whose ACL differs from its parent is excluded from the
  crawl and never ingested. Per-file ACLs are never mirrored — Agnes does not
  pretend to a granularity it cannot enforce.
- **Broken-inheritance folders become their own permission zone.** A subfolder
  that stopped inheriting its parent's permissions is promoted to a *zone*: a
  fresh collection of its own, with its own mirrored ACL, and the crawl
  descends into it instead of stopping. If the folder later re-links
  inheritance, the zone is marked `dissolved` (never deleted from the record)
  and its collection is retired — files, grants and row.
- **Retroactive purge.** Content already ingested that later falls under an
  exclusion, or moves into a zone, is purged on the next sweep through the same
  code path a manual file delete uses.

On top of that there is a server-side **ingest gate**: uploads and fact-ingest
batches that would land content Agnes knows should not have been crawled are
refused at the API, independent of what any crawler claims it skipped.

An admin may knowingly override a subtree exclusion
(`include_excluded_subtrees: true`, audited) — but only in `should_not` mode.
In the default `must_not` mode the override is refused with
`409 must_not_forbids_subtree_override`.

### Not a permission: anonymization

The wizard's per-scope **anonymize** checkbox is a *masking* control, not an
access control: names, companies, emails and URLs are replaced with stable
per-instance pseudonyms before ingestion, for everyone equally. A document that
cannot be anonymized is skipped, never ingested raw. Use it when the content
may be broadly readable but identities must not be — it does not make anything
reachable or unreachable. See [`anonymization.md`](anonymization.md).

---

## 6. Where the groups come from

Grants are always written against Agnes groups. Those groups' *membership* can
have four different writers, and each writer only ever touches its own rows —
so a directory sync can never clobber an admin's hand-added member, and vice
versa.

| membership source | written by | group naming | when it refreshes |
|---|---|---|---|
| `admin` | `/admin/access`, `agnes admin group add-member`, REST | whatever you name it | immediately |
| `google_sync` | Google Workspace sync at sign-in | the Workspace group's email address | on each Google sign-in |
| `microsoft_sync` | Entra `GET /me/memberOf` at sign-in (off by default) | the Entra group's `mail`, or `displayName` when it is not mail-enabled | on each Microsoft sign-in |
| `sharepoint_sync` | the `sharepoint-acl-sync` job | `entra:<group-object-id>` for an Entra/M365 group; `sp-direct:<scope-id>` for the synthetic group collecting direct user assignments on a folder | every ACL sync run (§7) |
| `system_seed` | the seeded `Admin` / `Everyone` groups | — | — |

Setup for the two login-time syncs: [`auth-groups.md`](auth-groups.md) (Google),
[`auth-microsoft-oauth.md`](auth-microsoft-oauth.md#entra-group-sync-off-by-default)
(Entra). Both are per-user and only run when that user signs in.

**Three things about Entra ID that surprise people:**

1. **The two Entra paths do not share group rows.** The login-time
   `memberOf` sync names a group by its mail/display name; SharePoint ACL
   mirroring names the same group `entra:<oid>`. They are separate rows today.
   Write your grants against whichever one you actually use, and don't expect a
   membership from one to show up in the other.
2. **ACL mirroring does not require anybody to have logged in with Microsoft.**
   The job reads the group's transitive members from Graph with the
   connection's own app-only credential and matches each member to an Agnes
   account **by email, case-insensitively**. A member with no matching Agnes
   account grants nobody and is counted as `unmatched` on the source card.
3. **Mirrored groups and grants are the sync's, not yours.** You cannot add a
   member to an `entra:<oid>` or `sp-direct:<…>` group from the admin UI — the
   source owns it. And hand-removing a *mirrored* grant does not stick: the
   next sync recomputes the target state and writes it back. To widen access,
   add a *second* (admin-owned) grant — additive, and the sync never touches a
   row it did not write. To narrow it, change the permission in
   SharePoint/Entra.

---

## 7. Mirroring SharePoint permissions

Two postures per scope, chosen with `access_mode` when the scope is confirmed:

- **`manual`** (default) — **an admin decides who sees which collection and
  Agnes enforces it.** SharePoint's own ACLs are not consulted. This is the
  honest claim to make to a customer unless mirroring is explicitly turned on.
- **`mirrored`** — Agnes periodically re-reads the scope root's role
  assignments from Graph and reconciles Agnes groups, memberships and
  collection grants to match.

```jsonc
// POST /api/admin/sharepoint/connections/{id}/scopes
{
  "source_scope_id": "01ABC…",
  "display_path": "Finance Site/Documents/Reports",
  "drive_id": "b!xyz…",          // REQUIRED for mirrored scopes
  "access_mode": "mirrored"
}
```

Then either wait for the schedule or force a run:

```bash
curl -X POST https://<host>/api/admin/sharepoint/connections/<id>/acl-sync
curl -X POST https://<host>/api/admin/sharepoint/connections/<id>/subtree-sweep
```

The whole connector — wizard, crawling, mirroring, zones, ingest gate — sits
behind one switch, **off by default**:

```yaml
sharepoint:
  enabled: true          # AGNES_SHAREPOINT_ENABLED
```

Turning it on changes nothing until a scope opts into `mirrored`.

### Which SharePoint principals are honored

Mirroring is deliberately conservative: anything it cannot resolve to a real
directory principal is **counted and not granted**, so the failure direction is
under-sharing (visible on the source card), never over-sharing.

| on the folder's permissions | mirrored? |
|---|---|
| Entra ID security group / M365 group | **yes** → `entra:<oid>`, expanded via transitive members |
| a person granted directly on the folder | **yes** → collected into `sp-direct:<scope-id>` |
| SharePoint **site** group (Members/Owners/Visitors, custom site groups) | no — not enumerable through the app-only Graph surface |
| "specific people" sharing link | no |
| "people in your organization" link | no |
| anonymous link | no |
| external / guest user (`#EXT#`) | no |
| application principal | no |
| a user grantee with no resolvable email | no |

The practical reading: **if your library is shared through SharePoint site
groups or sharing links, mirroring will grant almost nobody.** Convert to Entra
groups on the folder, or use `manual` mode and write Agnes grants.

### Timing — how long a change takes to bite

| change | when it takes effect |
|---|---|
| an Agnes grant, group membership, or audience-class mapping | **immediately** — resolved live per request |
| a permission change on a mirrored scope or zone root in SharePoint | at most `acl_sync.interval_hours` (default **4 h**) |
| a newly broken-inheritance folder or newly unique-permission file | the daily sweep (07:00 UTC), with a per-connection `acl_sync.sweep_interval_days` guard (default **1 day**) |
| a document's *content* change | the extraction schedule / Graph change notifications — a different pipeline, unrelated to permissions |

Grant **additions are written before removals**, so a scope never passes
through a granted-to-nobody window it did not already have.

### When the sync cannot reach Graph

Controlled by the cross-audience posture switch (design question "must a
cross-audience leak be impossible, or merely unlikely?"):

```yaml
acl_sync:
  guarantee_mode: "must_not"   # must_not (default) | should_not
  max_stale_hours: 72
  interval_hours: 4
  sweep_interval_days: 1
```

- **`must_not`** (default, fail closed): after `max_stale_hours` (default 72)
  without a *successful* run, every mirrored grant on that connection's
  collections and active zones is **suspended** — deleted until the next
  successful sync rewrites them. Mass false denial is the accepted cost.
  Broken-inheritance subtrees are always excluded, advisory overrides are
  refused, and untagged claims in a tiered collection are admin-only.
- **`should_not`** (best effort): stale mirrored grants persist, with the
  staleness visible on the source card; overrides are allowed; untagged claims
  stay unrestricted within their collection.

A failure to read one *group's* membership is fail-soft — that group keeps its
previous members and the scope is marked stale, rather than being emptied on a
transient error. A failure to read a *scope root's* permissions aborts that
scope's reconciliation entirely: no partial diff is ever applied.

---

## 8. Recipes

### A. Two audiences, two folders — start here

The layout that needs no tiering, no per-file grants and no mirroring.

1. In SharePoint, put the documents in two folders.
2. In the connect wizard, select both. You get two collections.
3. Grant each collection to the group that should read it (wizard step 3, or
   `agnes admin grant create <group> collection <id>`).

Done. Reachability and text visibility agree everywhere, on every surface,
including what `agnes pull` distributes.

### B. Let SharePoint decide

1. Make sure the folder's permissions are **Entra groups** (see the honored
   table in §7).
2. Confirm the scope with `access_mode: "mirrored"` and its `drive_id`.
3. `POST …/acl-sync`, then read the source card: matched vs unmatched
   identities, and which principal types were not honored.
4. Fix the unmatched ones (usually: the person has no Agnes account, or their
   Agnes login email differs from their SharePoint email).

Keep in mind the revocation window is `acl_sync.interval_hours`, not instant.

### C. One person, one file

`agnes admin grant create <group> corpus_file <file-id>` — widens preview and
raw download of that one file only (§3).

### D. Same documents, two levels of detail

Audience classes (§4). Full detail for the top class, no document text for the
others, and claim variants selected per caller. Read §10 first — this is the
one recipe with real gaps.

### E. Never ingest it at all

Don't select the folder; or let the sweep exclude it (unique-permission files,
broken-inheritance subtrees promoted to zones). §5.

---

## 9. Verifying it, and proving it to a customer

Reading the grant graph tells you what you *intended*. It does not tell you
what the read surfaces *answer* — and those are different code paths.

- **`scripts/leak_matrix.py`** — presents each persona's own credential to the
  real read surfaces (collections list, collections search, knowledge search,
  catalog, query, agents) and records what actually comes back, including
  whether canary text surfaces in content. Run it after every grant change.
  See [`leak-matrix.md`](leak-matrix.md).
- **`GET /api/me/effective-access`** (and `/admin/users/{id}/effective-access`)
  — what the server claims a given person can reach. The leak matrix diffs this
  claim against actual reads; a mismatch means the audit view an operator reads
  during an incident is not describing enforcement.
- **`/admin/access`** — by group, or by resource: which groups reach this
  collection.
- **The source card** on `/admin/data-sources` — for a mirrored connection:
  last run, matched/unmatched identity counts, unhonored permission types,
  per-collection grant deltas, stale scopes, zone count.
- **The audit log** — every mirroring decision is recorded:
  `sharepoint_acl.sync_completed` / `sync_failed` / `grant_added` /
  `grant_removed` / `membership_replaced` / `principal_unmatched` /
  `grants_suspended` / `subtree_override` / `zone_created` / `zone_dissolved` /
  `content_purged`; and on the read side `collection.file_download`,
  `collection.file_preview`, `collection.search`.

---

## 10. Limits — state these before a customer discovers them

| limit | what it means in practice |
|---|---|
| **Collection grain, not item grain** | SharePoint can express per-item ACLs; a collection cannot. Agnes closes the gap by *excluding* what it cannot express (unique-permission files) and by *splitting* what it can (zones) — it never approximates an item ACL with a coarser grant. |
| **Unified search is not tier-aware** | `/api/knowledge/search` and the MCP `knowledge_search` tool filter chunk hits by collection reachability only. On a **tiered** collection they will return document snippets to a caller below the top class, which `/api/collections/search`, `/preview` and `/raw` withhold. If audience tiers are load-bearing, treat `collections_search` as the tier-safe search surface, and do not rely on tiering alone for content that must not reach the lower class at all. |
| **Distribution is not tier-aware** | `agnes pull` delivers per-collection `knowledge.duckdb` chunk artifacts and knowledge digests to analyst workspaces, filtered by collection grants only. Tiering narrows *server* reads; it does not narrow what a reachable collection ships to a laptop. Audience tiers are therefore a read-surface control, not a data-distribution boundary. |
| **"Top class only" is not redaction** | A lower class gets *no* document text, not a redacted version. Per-audience document derivations do not exist yet. |
| **Grants are additive; there is no deny** | You cannot carve an exception out of a grant. Split the collection instead. |
| **Mirroring under-shares by design** | Site groups, sharing links, guests and application principals are not honored (§7). A library shared exclusively that way mirrors to nearly nobody. |
| **Identity join is by email** | A SharePoint principal with no Agnes account of the same email grants nobody. Counted, not silently dropped — but still invisible access until someone reads the count. |
| **Revocation is eventually consistent** | A source-side revocation lands within `acl_sync.interval_hours` (default 4 h), not instantly. If the guarantee must be hard, that window is a violation budget to agree with the customer, not "eventual consistency" to wave through. |
| **Audience classes are API-only** | No UI editor; and enabling tiers does not retag already-indexed claims. |
| **Anonymization is not access control** | It masks for everyone equally (§5). |

---

## See also

- [`RBAC.md`](RBAC.md) — groups, members, resource grants, the god-mode and
  `Everyone` rules, sync-source segregation
- [`admin/collections-vs-data-packages.md`](admin/collections-vs-data-packages.md)
  — whether content belongs in a Collection or a data package at all
- [`anonymization.md`](anonymization.md) — the per-scope masking pipeline
- [`auth-microsoft-oauth.md`](auth-microsoft-oauth.md) — Entra ID sign-in and
  the login-time group sync
- [`auth-groups.md`](auth-groups.md) — Google Workspace group sync (the pattern
  ACL mirroring follows)
- [`leak-matrix.md`](leak-matrix.md) — persona × surface verification sweep
- [`table-access-policies.md`](table-access-policies.md) — the equivalent
  question for tables
- [`feature-flags.md`](feature-flags.md) — `sharepoint.enabled`,
  `acl_sync.guarantee_mode`, and the rest of the switch registry
