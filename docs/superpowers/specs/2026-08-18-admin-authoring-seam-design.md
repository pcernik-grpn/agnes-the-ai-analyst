# The admin authoring seam — design

**Status:** proposal, pending decisions in §7
**Surfaces:** `/library`, `/catalog`, `/catalog/p/{slug}`, `/admin/data-packages`,
`/admin/data-packages/{id}`, `/admin/tables`, `macros/_detail.html`

---

## 1. The problem

Agnes runs two information architectures over the same governed objects:

| Tree | Question it answers | Surfaces |
|---|---|---|
| **Authoring / governance** | "what exists, who gets it, how is it built" | `/admin/*` |
| **Consumption** | "what do I have, what can I add" | `/library`, `/catalog`, `/marketplace`, `/memory` |

A data package lives in both trees and has **two detail pages** —
`/catalog/p/{slug}` (a reading page: what is this, add it to my stack) and
`/admin/data-packages/{id}` (a governance workspace: what is in it, who can
use it, who actually pulled it).

The admin is **both personas at once**, and switches between them many times an
hour. Today the seam between the trees is crossed by **navigation** — teleport
to another lens — rather than by **capability** — do the thing where you are
standing. Every object-scoped edit costs a full context switch, and the
context you land in is frequently *about something else*.

## 2. The mechanical root (verified, not inferred)

Package **creation** is a shared component. Package **editing** is page
furniture on one lens. That single asymmetry produces the whole complaint.

- **Create** — `app/web/static/js/components/package_drawer.js`, riding the
  shared drawer chrome (`css/drawer.css`). Opens in place on
  `/admin/data-packages` and `/admin/tables`, and via `?new_package=1`.
  It is create-only: `open()` takes no package id, and there is no edit mode.
- **Edit** — a legacy `.modal-overlay` (`#editDataPackageModal`,
  `admin_tables.html:3883`) opened by `openEditDataPackageModal(pkgId)`
  (`admin_tables.html:2989`), which **exists only inside `admin_tables.html`**.
  It is also auto-opened from `?edit_package=<id>` (`admin_tables.html:8615`).

The deep links all work — `?new_package=1`, `?group_by_bucket=1` and
`?edit_package=` are each handled by inline scripts in `admin_tables.html`.
Nothing here is broken code. It is placement.

The consequence chain:

1. `/admin/data-packages/{id}` — the page whose docstring is *"ONE package, end
   to end"* — **cannot edit its own package's** name, description, icon,
   category, status, or delete it. Its "Edit details…" control
   (`admin_package_detail.html:480`) is a link to `/admin/tables?edit_package=`.
   The template comment already concedes the gap: *"Until the drawer grows an
   edit mode that can open in place, this page at least names the door."*
2. `/catalog/p/{slug}` — the analyst page — answers the admin's "edit this"
   with the same link, from an overflow-menu item
   (`catalog_package_detail.html:162`) and a legacy edit icon (`:224`).
3. So **both** package pages answer "edit this package" by sending you to a
   third page that is about *tables*.

**The backend is already complete.** `app/api/data_packages.py` has
`PUT /{pkg_id}`, `POST /{pkg_id}/tables`, `DELETE /{pkg_id}/tables/{table_id}`,
`DELETE /{pkg_id}`, `POST /{pkg_id}/restore`; sharing writes through
`/api/admin/grants`. No new endpoint, no repository method, therefore **no
DuckDB↔Postgres parity sibling and no migration**. This is a front-end and IA
problem end to end.

## 3. The governing principle

Two kinds of admin action, and they belong in different places:

- **Object-scoped** — acts on the one object the reader is looking at: edit
  this package's metadata, add or remove its tables, change who it is shared
  with, retire it.
- **Instance-scoped** — acts on the instance: connect a source, register
  tables, server config, groups, database backend.

> **Object-scoped admin actions travel with the object, onto whatever surface
> shows it. Instance-scoped actions never leave `/admin`.**

This is the rule that answers the original question in both directions:
editing a package from the Library/catalog detail page is *right*, and
connecting a data source from the Library is *wrong* — not as a matter of
taste, but because one acts on what you are pointing at and the other
reconfigures the instance underneath everybody.

A corollary that matters for the visual design: on a consumption surface the
**admin layer must never outrank the user layer**. The admin arrived as a
reader; the primary action stays the reading verb ("Add to stack"), and the
governance cluster is secondary chrome.

## 4. The design

### 4.1 Keystone — promote the edit modal into the shared drawer

Move `#editDataPackageModal` out of `admin_tables.html` and into
`package_drawer.js` as an **edit mode** (`open({mode: 'edit', pkgId})`),
writing through the existing `PUT /api/admin/data-packages/{id}`.

Nothing else in this plan is possible while the only editor is furniture on one
lens. It repeats, exactly, the move the repo already made when the *create*
modal became the shared drawer (CHANGELOG "Creating a data package is one
shared drawer, opened on the lens you are standing on") — same argument, the
other verb.

Immediate payoffs, with no new surface:

- `/admin/data-packages/{id}` edits its own package in place; "Edit details…"
  stops being a link to somewhere else.
- `/admin/data-packages` can edit a card in place.
- `?edit_package=<id>` on `/admin/tables` keeps working through the same
  component, so no existing link breaks and the guards
  (`tests/test_web_admin_package_create_drawer.py`,
  `tests/test_admin_tables_tab_ui.py`) stay meaningful.

### 4.2 One governance affordance, replacing four idioms

There are currently four different ways an admin control appears on a
consumption page:

| Surface | Idiom |
|---|---|
| `catalog_package_detail.html:162` | overflow-menu item |
| `catalog_package_detail.html:224` | legacy `detail.edit_icon()` |
| `memory_domain_detail.html:167` | inline "Edit · admin-only" text link per item |
| `library_detail.html:187` | status-conditional block |

Add **one** macro to `macros/_detail.html` — `detail.manage(actions, admin_href)`
— rendering a single, visually distinct cluster that:

- renders only when the caller can administer **this object**;
- carries object-scoped actions only, which open the shared drawers **in place**;
- always ends with exactly one door: **"Manage in Admin →"** to the canonical
  admin page for that object.

Treatment: a bordered, labelled cluster reading as *a different authority* —
not a filled primary button. The existing `.admin-only-hint` class is the seed
of this vocabulary.

### 4.3 Library's `+ Add` — name the blast radius

The worry that putting package creation behind Library's `+ Add` is "tricky and
misleading" is correct, and the reason is precise: `+ Add` today means *add to
me*, and creating a shared package means *add for everyone*. One menu, two
blast radii, no signal which is which.

Resolve it with a labelled group rather than a mode switch or a second button:

```
+ Add ▾
  ── Add to your library ──
     Build a skill · Build a plugin · Build an agent template
     Upload a file
     Install from the marketplace
     Add shared data & recipes        → /catalog
  ── Publish to the workspace ──      (admin only)
     New data package                 → shared create drawer, in place
```

The section heading carries the warning that a bare menu item cannot. The
admin gets the verb where they stand; the reader's menu keeps its meaning.

**Deliberately absent: "Connect a data source".** Per §3 it is instance-scoped.
The dead end it would have covered — *"the table I want isn't in the list"* —
is handled contextually instead: a **"Don't see a table? Register tables →"**
link inside the package composition drawer. Link out, never embed.

### 4.4 Two package pages, one honest relationship

**Do not merge `/catalog/p/{slug}` into `/admin/data-packages/{id}`.** They are
not duplicates: one is an editorial reading page whose main column is
deliberately given over to the tables, the other is a governance workspace
whose centrepiece — the delivery read-out that turns "shared with 14 people"
into "11 of them actually have it" — has no place on a reading page. The admin
page was built recently and deliberately, to give the package "a home"; merging
reverses that decision rather than building on it.

Make the relationship symmetric and cheap instead:

- analyst page → **"Manage in Admin →"** (via `detail.manage`)
- admin page → **"View as analyst →"** (already exists,
  `admin_package_detail.html:483`)
- and because the frequent object-scoped edits now work on both, the trip
  becomes **optional** rather than mandatory. That is the actual fix.

## 5. Explicitly not doing

- **No "viewing as admin / analyst" mode toggle.** Agnes deliberately removed
  `browse_admin` god-mode from the user-facing `/catalog` under
  auto-membership ("auditing lives at `/admin/data-packages`"). A mode toggle
  reintroduces it under a new name and doubles the state space of every page.
- **No admin god-mode listing in `/library`.** Its contract is "what *I* have"
  — stated in the handler docstring and load-bearing: it is the only page that
  shows an admin what a normal user's experience actually is.
- **No source connection outside `/admin/data-sources`.**

## 6. Phasing

| Phase | Change | Risk |
|---|---|---|
| **P0** | Edit mode in `package_drawer.js`; wire in place on `/admin/data-packages/{id}` and `/admin/data-packages`; keep `?edit_package=` working | low — component move, endpoints exist |
| **P1** | `detail.manage()` macro; adopt on `catalog_package_detail.html`, retiring the overflow item + legacy `edit_icon` | low |
| **P2** | Library `+ Add` labelled admin group | low |
| **P3** | Composition + sharing in place on the analyst detail page | **medium — validate before building** |
| **P4** | Sweep remaining idioms (memory domain, library file); settle naming (§7.1) | low |

P0 is worth doing on its own merits even if the rest is rejected: it closes a
gap the codebase has already documented against itself.

## 7. Decisions (settled 2026-08-18)

1. **Terminology — settled.** **Data package** is the Agnes container.
   **Bucket** is the source-side Keboola container (`table_registry.bucket`).
   They are different things and the UI must never use one for the other.
2. **`detail.manage()` is a general contract.** Every detail page whose object
   the caller can manage gets the same cluster, and every action it offers
   opens the shared right-side drawer. It keys on **owner OR admin** — files
   and collections are owner-owned, store entities have both — and the
   "Manage in Admin →" door renders for admins only.
3. **The `+ Add` menu is grouped by blast radius, not by role** (§4.3 revised):
   *Create for the workspace* (skill · plugin · agent template · data package)
   and *Just for you* (upload a file). Verified against behaviour: publishing a
   skill already reaches other people, while an uploaded file is private by
   default. Acquisition rows leave the menu entirely — see §8.
4. **Admin "delete" means archive.** `store_entities_repo().archive()` already
   exists: soft, reversible, records `archived_at`/`archived_by`, hidden from
   every listing. No hard delete.
5. **Verification does not survive a new version.** Publishing a new version of
   a Verified item drops its marker to Community until an admin re-verifies.
   A carried-over marker on unreviewed content is a supply-chain hole.
6. **Narrowing an audience is gated by trust level.** For **Verified**
   items only an admin may narrow the audience; for **Community** items the
   owner may narrow their own.
7. **`required` stays organization-only** (already enforced at
   `app/api/access.py:790`): a colleague's Community skill can be shared with a
   group but never made automatic in their stack.
8. **Retire the "flea" vocabulary without breaking anything** — see §9.

---

## 8. Marketplace and Catalog fold into the Library

**Finding — the `+ Add` acquisition rows are not a design.** `Install from the
marketplace` and `Add shared data & recipes` were put there by PR #1276
(*"five user pages were unreachable under the rail chrome"*), whose own message
says they carry *"the in-page paths their rail retirement pointed at"*. The rail
redesign retired both nav rows; the replacement paths did not exist; #1276
stopped two live pages being orphans. `tests/test_web_nav_user_parity.py` now
pins them as the only inbound links.

**The merge direction is already approved policy** —
[`2026-08-12-library-memory-dataapps-merge-design.md`](2026-08-12-library-memory-dataapps-merge-design.md)
folds `/corporate-memory`, `/apps` and `/stack` into `/library`, naming the
problem as *"two overlapping surfaces per kind … split the 'what do I have /
what can I add' answer across pages."* Its Out-of-scope list never mentions
Marketplace or Catalog: they were not deferred, they were never examined. The
Library already renders **all eight kinds** (`_SECTION_LABELS`,
`app/web/router.py:3528`).

**Decision.** One browse destination with a **scope control** — the pattern
`/stack → /library?stack=in_stack` already proved. "Things I could add" becomes
a state of the one list, not a separate page. The store's richer browse
(categories, trust, versions, ratings) survives as a **shelf inside** the
Skills/Plugins sections rather than a top-level destination. `/marketplace` and
`/catalog` redirect in, exactly as the three before them did.

Consequence: `+ Add` keeps only *create* verbs, which is what makes decision 3
honest.

## 9. Audience vs moderation — the structural blocker

**A store entity cannot express "shared with a group" today, and the reason is
one overloaded column.** `visibility_status` carries two axes at once
(`app/web/router.py:2320`):

| value | what it actually encodes |
|---|---|
| `approved` | **audience:** Everyone |
| `pending` | **moderation:** In review |
| `hidden` | **audience:** Private |
| `archived` | **lifecycle:** Archived |

There is no room for a third audience value on a column that also encodes
review state. Meanwhile the correct mechanism exists and **nothing enforces
it**: `ResourceType.STORE_ENTITY` is registered and its spec text promises
*"Grant a group access to it"* (`app/resource_types.py:863`), yet `library_page`
lists every approved skill and plugin with no grant filter
(`app/web/router.py:2746`), and `app/web/templates/library.html:352` records the
gap directly — *"`access=private|everyone` is accept-only at CREATE … the
missing piece is an access-change endpoint."*

**Decision — separate the axes onto the mechanisms that already own them:**

| Axis | Home | Values |
|---|---|---|
| Moderation | `visibility_status` | pending · approved · hidden · archived |
| **Audience** | **`resource_grants`** | private = no grants · group = grants · workspace = grant to `Everyone` |
| Trust | `publisher_kind` + `verification_state` | Organization · Verified · Community |

This makes store entities behave like every other governed resource, and the
requirement *"lands in the Library of everyone it was shared with, marked
Community"* then needs no special case: the Library's granted-resources path
already does exactly that for data packages, memory domains, recipes and
curated plugins.

**Retiring "flea" (decision 8).** The trust vocabulary moved to
Organization/Verified/Community, but `flea` is still live in routes
(`/marketplace/flea/{entity_id}`), ~23 templates, the CLI, and the admin nav
entry "Flea submissions". Retire the **word**, not the URLs: rename user-visible
labels first, then move routes with permanent redirects from the old paths so no
bookmark, CLI call or test breaks.
