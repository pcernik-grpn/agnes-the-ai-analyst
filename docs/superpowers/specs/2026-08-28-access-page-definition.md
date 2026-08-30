# The access page — what an admin manages instead of a matrix

**Status:** proposed — draft for review
**Date:** 2026-08-28
**Ticket:** TCRD-203 (definition only; implementation is someone else's, per
Zdeněk's note on TCRD-176: *"určitě potřebuji nejdřív pomoc s definicí,
implementaci může dělat někdo jiný"*)
**Scope:** what `/admin/access` should be, given that the library stays the way
data is shared, that maintaining an access matrix does not scale, and that the
**Library redesign is being implemented** (artifact *Library Rebuilt*,
2026-08-28).
**Drawn:** `2026-08-28-access-page-mock.html` — three states (how it opens, one
group opened, by bundle) plus the search states.

## Two decisions this spec does not make

Both are the owner's, and the shape of the page changes with them. Stated
first so nobody has to infer which parts are settled.

1. **What happens to the per-resource grants that exist today** — migrated into
   the new unit, demoted to an *Advanced* view kept for exceptions, or frozen
   where they are. This spec assumes *demoted*, and marks every place that
   assumption carries weight. See [The grants that already exist](#the-grants-that-already-exist).
2. **Whether "exists but you can't see it" is acknowledged to the user.** That
   is a security posture, not a copy choice, and it is the same question
   TCRD-207 exists to answer across nine surfaces. **It should be decided once,
   there.** The Library redesign gives it a sharper form — see
   [Facts, not just documents](#facts-not-just-documents).

## What the Library redesign settles for this page

The access page and the Library are two views of one set of `resource_grants`
rows. If they group and name things differently, the admin and the user are
looking at two taxonomies of the same fact. Four things the redesign decides,
which this page now has to inherit rather than re-decide:

**Two families, not fifteen types.** The Library splits into **Knowledge** —
what Agnes knows, which you read, query or open — and **Capabilities** — only
what changes what your agent can do: skills, plugins, agent templates.

**A grant is the membership.** `features.stack_auto_membership` is default-on
since Wave 0 (`app/instance_config.py:632` — the `redesign` preset is the only
preset, and it implies the flag). Everyone in a granted group can query a
package the moment it is granted. There is nothing for the analyst to add.
*(Note: `docs/RBAC.md` still describes auto-membership as opt-in. Stale — worth
a one-line fix, since it is the doc this definition rests on.)*

**Therefore the tier is a download control, not an access control.** Required =
permanent and always downloaded. Available = the user gets exactly one choice,
keep a local copy or don't. Server-side query works either way; a local copy is
speed, not access.

**Sharing moved off the row onto the item's detail page.** The redesign cut
Owner / Sharing / Actions from the list because they repeated one value on 27 of
36 rows.

**And one invariant, stated plainly in the redesign:** *knowledge search is
grant-scoped — if you can see it, your agent can cite it.* That is what granting
a document actually does, and the access page should say so.

## What the page is today

Not a strawman — measured on `main`.

`/admin/access` is a two-pane workspace. Groups on the left with origin,
member count and grant count. On the right, the selected group as one object
with two sides: **Who it reaches** (members, their source, add/remove) and
**What it can use** — the grant matrix, filterable, backed by
`/api/admin/access-overview` and `/api/admin/grants`.

The matrix spans **15 grantable resource types**: tables, data packages,
semantic models, memory domains, memory items, recipes, collections, agents,
corpus files, data apps, marketplace plugins, chats, Slack channels, knowledge
digests, store entities. Each row is one resource, granted or not, and — for
packages, memory and plugins — carrying a tier rendered as **Optional /
Automatic** (`available` / `required` in the API).

A second lens, **Simulate a person**, walks one person's membership → grant →
tier and names what is *not* shared with them.

Three surfaces already transpose the same rows rather than duplicating the
editor: a data package's **Share** panel, `/admin/users/{id}`, and
`/admin/tables`' per-row *Manage access*, which arrives as
`/admin/access?resource=<type>:<id>` and pre-filters the tree.

And, load-bearing for everything below: **grants are already not admin-only.**
`app/services/library_sharing.py` lets a resource's *owner* write the same
`resource_grants` rows for collections, agents, corpus files and data apps,
through the Library's Share dialog.

## Why the matrix doesn't scale

**It is O(groups × resources).** Every new table, agent, collection or file adds
a row to every group's matrix. The page grows with the instance; nothing about
the work is proportional to the decision being made.

**It asks about the wrong unit.** An admin does not think "does Sales get
`orders_2024`". They think "Sales gets the sales data". The bundle that expresses
that already exists — a data package bundles tables (`data_package_tables`) —
and the matrix asks the question one level below it, every time.

**It is already lying about which lever matters.** Per `CLAUDE.md`, admin RBAC
for auto-sync flows through data packages and per-table `resource_grants` no
longer surface tables in analyst manifests: the package grant is what puts a
table in someone's stack. The matrix still presents hundreds of individually
toggleable table rows with equal weight to the package row that does the work.

**And now: it disagrees with the Library.** Fifteen flat types against two
families, with a mandatory-ness control the user reads as "Required by your
admin" and the admin reads as "Automatic".

## The layout

The two-pane workspace goes. Its failure was not density — it was that
**master-detail on groups answers one question out of three.** "Who gets
Revenue Core?" and "why can Maria see this?" both begin with a hunt for the
right group to click; comparing two groups costs you your place; and a fifth of
the width is spent permanently on navigation used for two seconds, with the
dense part squeezed into what is left.

**One full-width list, grouped, with a switch.** By group · By bundle ·
Everything — the same device the Library uses for By subject · By type ·
Everything, and the reason the page can be entered from any of the three
directions an admin arrives from. *By bundle* is what makes two things visible
that the group view structurally cannot show: the same app shared to two
different groups, and the bundles granted to nobody (TCRD-221, as a side effect
of the grouping rather than a feature).

**Collapsed on arrival**, with one rule attached, because the Library redesign
lists *"nothing is collapsed on arrival"* among its own fixes: **a collapsed row
must answer something.** Origin, reach, and a count per family is a summary —
enough to skip a group without opening it. A bar that hides content and says
nothing is the thing the Library removed, and this rule is what stops someone
citing this page as precedent for it.

**The search is the filter, and typing is what expands.** One control, not two.
A group opens because it matched, showing only the rows that matched and saying
how many it hid. That is also the answer at sixty synced groups, where scrolling
is no longer a way in.

**A miss says it is a miss.** DES-113 found an empty result presented as an
access problem, routed through `error.html`. On this surface in particular an
admin must never be told they lack access to their own instance — so the
zero-result state names the search, not a permission.


## The definition

**One sentence:** a group is an audience; what an admin hands it is a small
number of named bundles, sectioned the way the Library sections them —
everything else on the page is oversight of what other people shared, not a
place to grant.

### 1. Two families, mirroring the Library

Inside an opened group, *what it can use* stops being one flat tree and becomes
the Library's two sections, in the Library's order and words:

**Knowledge** — data packages, memory domains, semantic models, recipes, apps,
and the document side (collections, corpus files, digests). What the group can
read, query or open, and what its agents may cite.

**Capabilities** — marketplace plugins, store entities (skills, plugins, agent
templates), agent profiles. What changes what the group's agents can do.

Two types belong to neither, and the flat matrix hid that: **chats** and
**Slack channels** are surfaces, not library items. They get their own small
third block — *Surfaces* — rather than sitting in a list of things you can hold.

Tables leave the default view. A table reaches a person by being in a package;
the page should stop offering a second path that does not do what it appears to.
`/admin/tables`' *Manage access* stops deep-linking into a table row and instead
answers *which packages contain this table, and who gets those packages*.

### 2. The tier says what it does

The control is not access — the grant already gave access. It decides whether
the resource is permanent and always downloaded, or whether the user chooses to
keep a local copy. So it takes the words the user will read in the Library:
**Required by your admin** and **Available — the user chooses a local copy**.
Not Optional / Automatic, which names neither state and matches nothing the user
sees.

It also applies to fewer rows than the matrix implies: packages, memory and
plugins only. On everything else the tier column should not exist.

### 3. Owner-shared things appear as oversight, with one editor

Collections, agents, corpus files and data apps are shared by their owners. The
access page shows them per group as **shared by someone**, with the owner named,
and offers exactly one action: revoke. To *change* a share you go to the item's
**detail page** — `/library/d/<id>` in the redesign — which is where the
redesign put sharing when it cut the column from the list.

That is also the answer to *how library sharing is expressed on the page*: as
origin. Every row states whether it is there because an admin granted it or
because an owner shared it, and the second kind is read-and-revoke.

Worth restating on the page for apps, because it surprises people: sharing a
hosted app shares its **rendered output** — the app runs under its own service
credentials, so a grant widens who sees that data regardless of the viewers' own
table grants (`docs/RBAC.md`, decided on #1321).

### 4. Simulate becomes the verification step

If the page no longer shows a cell per resource, the admin loses "can Maria see
X" by looking. *Simulate a person* already answers exactly that, including
naming what is **not** shared. It belongs one click from the audience side, not
behind a lens someone has to know exists.

## The grants that already exist

**Assumed: demoted, not deleted.** The matrix survives as **Advanced**, opened
from a resource rather than browsed by default. Existing rows keep working;
nothing is migrated, nothing is revoked, and an instance that curated
per-resource grants for a year does not wake up to a different authorization
result.

What it costs: two ways to grant the same thing, indefinitely — tolerable only
if the default path covers the common case, which is the bet here.

The alternatives, and why they lose:

- **Migrate** — fold loose table grants into generated packages. Changes
  authorization outcomes on upgrade, for a page nobody has complained is wrong,
  in the weeks before a demo. Wrong risk, wrong time.
- **Freeze** — keep the rows, remove the editor. Leaves an admin unable to undo
  a grant the page still enforces. Not defensible.

**Overrule this if the intent is that the matrix actually goes away**; the rest
holds either way, but *Advanced* and its entry point disappear.

## Facts, not just documents

The Library redesign's own open decision #2 — *do nodes and edges land in Agnes
or stay behind the MCP server* — lands on this page either way, because the
redesign also promises every fact traces to a verbatim quote in a source
document.

The rule that follows from grant-scoped search, and which this spec proposes:
**a fact is visible if you can read at least one document that evidences it.**
Nothing new to grant; the graph inherits document grants.

That produces the sharp version of TCRD-207's question, and it is worth deciding
with the example in hand rather than in the abstract: a fact evidenced by two
documents, one you may read and one you may not. Do you see the fact with the
one citation you're allowed, or not at all? Showing it is right — the evidence
you can see is real evidence — but it means the visible citation list is not the
whole story, and a user comparing notes with a colleague will notice.

TCRD-196 owns the graph's access model; this page has to express whatever it
decides, and *Simulate* is where an admin would check it.

## Handoffs

- **TCRD-207** — acknowledge or stay silent, plus wording for a group with no
  grants and a person with no access. Now also the two-document case above.
- **TCRD-208** — the tier vocabulary. Three names for one two-state control:
  `available`/`required` in the API and docs, **Optional / Automatic** in the
  admin UI, **Required by your admin** / **Keep a local copy** in the Library
  redesign. The redesign's words are the ones a user reads, so they win.
- **`docs/RBAC.md`** — auto-membership described as opt-in; default-on since
  Wave 0.

## Not in scope

Implementation. IdP propagation — Google and Entra groups flowing into Agnes
groups, so access management is something the customer's admin already did
elsewhere — is a real dependency and named as one, but it is not this page and
not this ticket.
