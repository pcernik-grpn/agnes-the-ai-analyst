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

## 2. The page is organised around the schema, not around tasks

by-group, by-resource and by-person are three **pivots over one join**
(`user_group_members` × `resource_grants`). A pivot answers *"what is currently
true"*. It structurally cannot answer *"where do I change it"* — which is
verbatim what the report says is missing.

Nobody arrives at this page wanting a pivot. They arrive with one of three jobs:

| The job | The question | Where it is served today |
|---|---|---|
| **Grant** | "give the finance team the revenue package" | the workspace lens, if you already know which group |
| **Audit a person** | "why can Jana see this?", "what will a new hire get?" | the **Simulate** lens — the best thing on the page, behind `?lens=simulate` |
| **Audit a resource** | "who can reach this table?" | partly the right pane, partly a package's own Share panel |

Three verbs, not three tabs of a table. Note that Simulate already answers the
hardest of the three and is the least discoverable thing on the surface.

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

### The case that proves it

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

So the admin's experience is: see a grant → try to revoke it → bare failure → no
statement of which page owns the control. That is *"I cannot confidently answer
where I set what"*, reproduced exactly, and no amount of re-laying-out the page
fixes it.

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

**0 — Explain the refusal.** Catch `cannot_revoke_system_grant` in the page and
say which surface owns the control, with a link. Roughly an hour, independent of
everything below, and it removes the single most infuriating dead end.

**1 — Fix the writers.** One chokepoint every grant creation goes through, plus a
provenance column recording the surface and the reason. Invisible, unglamorous,
and it is what makes any UI built on top of this truthful. Under the A3 ratchet
this is a Postgres-only column with clean DuckDB degradation
(`RequiresPostgresBackend` → 501), not a new DuckDB migration step.

**2 — Re-frame the page around the three verbs** of section 2. Simulate stops
being a secondary lens and becomes a front door. This is the "rethink", and it is
cheap *after* stage 1 because the page can finally say where each grant came
from.

**3 — Write the reach chain down once**, as reference documentation, and link it
from the page.

### What not to do

**Do not start with the visual.** It reads as progress, it is the thing this
surface already did once, and it leaves the reported question exactly as
unanswerable as it is today. The four-editors consolidation was good work that
did not stop the drift, because the drift comes from the writers.

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
