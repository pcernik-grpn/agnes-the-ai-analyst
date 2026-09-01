# The Access surface needs a rethink, and the page is the symptom

Status: **design note, nothing built.** Written to be argued with before anyone
commits to the work.

Origin: two admins independently reported the same thing about `/admin/access`
(issue #1956, item 13):

> the by-group / by-resource / by-person views with nested expanding rows
> (People, Access, knowledge/capabilities/surfaces counts) don't communicate
> where you set what. The page's mental model needs a rethink more than any
> single element.

That is a precise complaint and it deserves a precise answer. This note argues
the page is where the problem is *visible*, not where it is *caused*, and that
fixing the page first would repeat a mistake this surface has already made once.

---

## 1. What the page is today

`/admin/access` is comprehensive. It renders every registered resource type —
16 of them, across the three families the report names:

| Family | Types |
|---|---|
| Knowledge (10) | `collection`, `corpus_file`, `data_app`, `data_package`, `knowledge_digest`, `memory_domain`, `memory_item`, `recipe`, `semantic_model`, `table` |
| Capabilities (4) | `agent`, `marketplace_plugin`, `mcp_source`, `store_entity` |
| Surfaces (2) | `chat`, `slack_channel` |

So the complaint is **not** "my resource type is missing". Every type is there.

It has also already been consolidated once. The template's own docstring records
it: four editors — this workspace, a group list page, a group detail page, and
the create drawer's copies of both panes — were collapsed into one, over a
single pair of tables (`user_group_members`, `resource_grants`). `/admin/groups`
and `/admin/groups/{id}` now 308 here.

**That consolidation fixed the readers and never touched the writers.** Section 3
is why that matters.

## 2. The three jobs — and the page does two of them well

This section originally argued the page was "three pivots over one join" that
could not answer *"where do I change it"*. **That was written from the code and
it is wrong.** Walking the page on a seeded instance (5 real groups, grants of
several types, a Required plugin) shows the opposite for two of the three jobs:

| The job | The question | How it actually goes |
|---|---|---|
| **Grant** | "give Finance the revenue package" | **Works well.** By group → the group → Access → *"Add to this group"* opens a searchable drawer of everything grantable, grouped by family, with type chips. Three clicks and a search. |
| **Audit a resource** | "who can reach this?" | **Works well.** By resource lists each resource with the groups and people that reach it, and even flags *"Granted to nobody: 1 cloud chat, 6 memory domains. Authored, then never handed to anyone."* |
| **Audit a person** | "why can Jana see this?" | By person exists; the reach chain behind it is section 4. |

So the tabs are not the problem, and a re-frame around "three verbs" would be
rebuilding something that already works. Corrected because the original claim
would have sent the redesign in the wrong direction.

The page also has **three tabs, not two lenses** — By group / By resource / By
person, exactly as the report describes. The earlier draft said two, from
reading the `?lens=` parameter rather than opening the page.

## 3. The actual cause: ten writers, no provenance

`resource_grants` rows are created from **ten** places. Only one of them is the
Access page:

| Writer | What it is |
|---|---|
| `app/api/access.py` | the Access page itself (`POST /api/admin/grants`) |
| `app/api/marketplaces.py` | **"Required" plugin — fans out to every group** |
| `app/api/collections.py` | collection CRUD / upload |
| `app/api/admin_sharepoint.py` | the SharePoint connect wizard |
| `app/api/share_requests_admin.py` | the agent-sharing approval queue |
| `app/services/library_sharing.py` | owner-initiated Library sharing |
| `app/chat/grant_seed.py` | one-time chat grant for `Everyone` |
| `src/mcp_source_grants.py` | default-visibility seeding for MCP sources |
| `src/skill_contribution.py` | externally-generated skill publishing |
| `src/marketplace.py` | nightly marketplace sync |

The table records `assigned_by` (an actor email) but **not which surface made the
grant, or why**. A row written by an automated fanout is indistinguishable from
one an admin created by hand.

### The case that proves it — observed, not inferred

Marking a plugin **Required** on `/admin/marketplaces` loops
`for group in user_groups_repo().list_all()` and writes a grant to *every group*
(`app/api/marketplaces.py`). Those rows show up on the Access page — and the page
**refuses to delete them**:

```python
raise HTTPException(status_code=409, detail="cannot_revoke_system_grant")
```
`app/api/access.py`

`admin_access.html` contains no handling for that error code and no lock
affordance for it. (Its `is_system` references are about system *groups* —
`Admin`, `Everyone` — not system plugins.)

Walked on a seeded instance. Marking one plugin Required wrote a grant to all
seven groups. In **By resource**, that plugin then expanded to seven rows, each
reading:

> `Analysts · 0 people · granted by dev`  ·  [Optional | Automatic]  ·  **Revoke**

Three untruths in one row. `granted by dev` is the *actor* — me, on a different
page — so seven machine-written rows are indistinguishable from seven an admin
typed here. The tier pair offers a choice that does not exist on a mandatory
plugin. And **Revoke** cannot work; its confirm modal even promised *"You can
grant it again from this page."*

Clicking it produced `Could not revoke: HTTP 409`.

That is *"I cannot confidently answer where I set what"* reproduced exactly —
and note that **no amount of re-laying-out the page fixes it**, because the page
was not lying about its layout. It was lying about who owns the grant.

### The sharpened diagnosis

The page's model is sound. Its **truthfulness** is not: it renders every grant as
though this page created it and this page can remove it. Where that is untrue it
offers a control that fails and a sentence that is false.

That is a much smaller and more fixable claim than "the mental model needs a
rethink", and it is what the evidence actually supports.

## 4. A grant is not the whole answer anyway

For a table, reach is a chain:

```
person → group → resource_grant → data package → stack membership
       → server_only → row/column access policy
```

Each hop can independently explain why someone does or does not see data. The
workspace lens shows the grant hop. Simulate walks membership → grant → tier,
which is the closest thing to the whole chain that exists.

No single screen teaches this chain, so admins reverse-engineer it from
behaviour. That is a documentation gap as much as a UI one.

## 5. What to do, in this order

Deliberately bottom-up. Each stage is worth shipping alone.

**0 — Stop offering the control, and say who owns it. DONE, in this PR.**
`/api/admin/access-overview` now returns `managed_by` per grant (non-null only
where another surface owns it — today, a Required plugin). Those rows render
`Required plugin · Marketplaces →` in place of the tier pair and Revoke, the
same shape the existing `via Everyone →` case already uses. The 409 is still
translated as a backstop for any path that reaches it.

This is deliberately not a schema change: `is_system` is already derivable from
`marketplace_plugins`, so the one externally-owned grant kind that exists today
can be named without one.

**1 — Fix the writers.** One chokepoint every grant creation goes through, plus a
provenance column recording the surface and the reason. Invisible, unglamorous,
and it is what makes any UI built on top of this truthful. Under the A3 ratchet
this is a Postgres-only column with clean DuckDB degradation
(`RequiresPostgresBackend` → 501), not a new DuckDB migration step.

**2 — Show provenance wherever a grant is listed**, once stage 1 exists: replace
`granted by <actor>` with the surface and the rule. Explicitly **not** a re-frame
of the tabs — section 2 retracts that. The tabs work; what they print does not.

**3 — Write the reach chain down once**, as reference documentation, and link it
from the page.

### What not to do

**Do not start with the visual, and do not re-frame the tabs.** Both read as
progress. The four-editors consolidation was good work that did not stop the
drift, because the drift comes from the writers — and the tabs, tested on a
seeded instance, do their jobs. What fails is what the rows *say*.

## 6. Open questions

- Should provenance be a free-text reason or a closed vocabulary of surfaces? A
  closed set is greppable and testable; free text survives a new writer nobody
  updated.
- Is the "Required plugin" fanout the right design at all? It materialises N
  grants to express one rule. A computed/virtual grant would need no fanout and
  no un-revokable rows — but it is a bigger change than provenance and should be
  decided separately.
- Does `by-resource` deserve to be a first-class view, or is it satisfied by each
  resource's own Share panel (the transpose the page docstring already defers to)?
