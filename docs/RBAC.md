# Access control (v14)

Two-layer authorization model:

- **App-level access** = membership in the seeded `Admin` user-group. Admins can do everything; everyone else is gated through resource grants.
- **Resource-level access** = generic `(group, resource_type, resource_id)` grants. A user has access to a specific resource if any of their groups holds a matching grant.

There is no role hierarchy, no session cache, no implies expansion, no module-author registration step. Every protected endpoint resolves authorization with one or two DuckDB queries.

---

## What this model does *not* govern

Agnes authorizes **its own entities** — registered tables, data packages, marketplace plugins, memory domains, and the other `ResourceType` values. It does not reach inside the systems it connects to.

A frequent and consequential misreading is that Agnes can scope a connected system's contents — "restrict this agent to one Confluence space", "let this group see only the tickets in project X". It cannot. When Agnes talks to an upstream system it does so as whatever principal the connection was configured with (a service account, an API token, a workload identity), and **that principal's permissions in the target system are the real boundary**. Agnes governs who may reach a connection; the target system governs what that connection can see.

The practical consequences:

- **Scope at the source.** If a group must not see a Confluence space or a Jira project, that restriction belongs on the service account in Confluence/Jira — typically as a second, narrower connection. Granting or withholding the Agnes-side resource is all-or-nothing over whatever the credential can reach.
- **A shared service account flattens identity.** Everyone reaching a system through one connection is the same principal upstream, which is the point (people without a seat in the upstream tool can still read through it) and also the cost: the upstream audit log records the service account, not the human. Agnes's own audit trail is where per-user attribution survives.
- **Grants are deterministic and flat.** No nesting, no inheritance, no negative grants. A user's access is the union over their groups — there is no way to express "everything in this package except one table". Split the package instead.
- **Grants are table-level.** A group either can or cannot see a whole registered table — there is no row- or column-grain grant in this model. The internal `agnes_sessions` / `agnes_telemetry` / `agnes_audit` tables carry their own hard-coded per-row filter, applied at query time regardless of grants (`src/rbac.py`, `connectors/internal/access.py`); that mechanism is specific to those three tables, not a general primitive.

To limit what an *agent* can reach, use agent scopes (an agent's effective authority is owner grants ∩ agent scope, enforced live at every brokered request) — but the same boundary applies underneath: the agent inherits whatever the connection's upstream principal can see.

### Table grants: agent-scope and co-session ceilings

A direct `resource_grants(group, 'table', id)` row does **not** grant an
analyst visibility into that table — ordinary analyst access is entirely
Data-Package-mediated (`src/rbac.py::can_access_table` / `get_accessible_tables`).
What a direct `TABLE` grant still does is set the ceiling that **agent
scoping** (`tables_mode='selected'`, `src/agent_scope_intersection.py::
compute_agent_intersection`) and **co-session** grant intersection
(`src/grant_intersection.py::compute_grant_intersection`) narrow against —
both read `app.auth.access._allowed_ids_for_user(owner_id, 'table')`, a raw
`resource_grants` lookup that never expands through a Data Package.

Practical consequence: to let a scoped agent (or a co-drive session) reach a
table, an admin must grant that table to the owner's group **in addition
to** — never instead of — putting it in a Data Package the owner has access
to. Granting only the package leaves the agent's/session's effective table
set empty for that table, regardless of the owner's own query access to it
via `agnes query`/`agnes pull`. If nobody in the instance uses agent scoping
or co-sessions, a `TABLE` grant has no live effect at all.

### A third layer: row and column access policies

Grants and agent scopes both answer "can this group/agent reach the table at all". A **separate, optional layer** answers a narrower question on top of that: once a group can reach a table, an admin may attach **one SQL policy** that Agnes substitutes for the table on every server-side read — filtering rows and masking columns by the caller's identity (`$user_email` / `$user_id` / `$user_groups`). It only applies to tables that never leave the server (`query_mode='remote'` or `server_only=true`, so a policy can't be routed around via `agnes pull`), and it is off by default behind the `access_policies.enabled` feature flag. Full reference — authoring, the mapping-table pattern, disclosure, and known v1 limitations: [`table-access-policies.md`](table-access-policies.md).

---

## Tables

| Table | Purpose |
|---|---|
| `user_groups` | Named groups. Two rows seeded as `is_system=TRUE`: **Admin** (god mode) and **Everyone** (auto-membership at creation for every new user by default; Workspace-mirrored instead when `AGNES_GROUP_EVERYONE_EMAIL` is set — see [Group membership sources](#group-membership-sources)). |
| `user_group_members` | `(user_id, group_id, source)`. `source ∈ {admin, google_sync, system_seed}` so each writer only manipulates its own rows — Google sync's nightly DELETE+INSERT does not clobber admin-added members. **v14**: FK constraint on `group_id` referencing `user_groups.id` (cascade delete). |
| `resource_grants` | `(group_id, resource_type, resource_id)`. The grant table the resolver hits when Admin short-circuit doesn't apply. **v14**: FK constraint on `group_id` referencing `user_groups.id` (cascade delete). |

`resource_type` is a string from the `app.resource_types.ResourceType` `StrEnum`. `resource_id` is a path string whose format is owned by the registering module — for `marketplace_plugin` it's `<marketplace_slug>/<plugin_name>`.

### Owner-writable grants (Library sharing)

Most `resource_grants` rows are admin-written (`/admin/access`), but the types
registered with the owner-sharing service (`app/services/library_sharing.py`:
collections, agents, corpus files, **data apps**) can also be granted by the
resource's *owner* through the Library's Share dialog — same rows, narrower
writer (the owner may only share what they own; admins pass for everything,
including linked data apps whose synthetic `system` owner matches no real
user).

Governance note for `data_app`, decided deliberately (Devin Review on #1321):
sharing a hosted app shares its **rendered output**. The app executes under
its own service credentials regardless of who views it, so a grant — `Everyone`
included — widens who can see whatever data the app displays, independent of
the viewers' own table/package grants. This is the same publish-what-you-built
model as sharing a collection or a file (those too can embed data the grantee
could not query directly); the owner had access to the data when building the
app, and sharing is their call to publish that view. Admins retain full
oversight: every grant is visible and revocable in `/admin/access`, and grant
writes are audited like any other.

---

## Authorization API

```python
from app.auth.access import require_admin, require_resource_access
from app.resource_types import ResourceType

# App-level — admin actions, settings, user management.
@router.post("/admin/users")
async def create_user(user = Depends(require_admin)): ...

# Resource-level — entity-scoped reads/writes.
@router.get("/marketplace/{slug}/plugins/{name}")
async def get_plugin(
    slug: str, name: str,
    user = Depends(require_resource_access(
        ResourceType.MARKETPLACE_PLUGIN, "{slug}/{name}",
    )),
): ...
```

The `path_template` argument is a Python format string resolved against the request's `path_params` at gate time — `"{slug}/{name}"` becomes the `resource_id` for the grant lookup.

Admin short-circuits both helpers — admins never need explicit grants.

### Admin elevation consent gate

The short-circuit is subject to a per-browser consent gate
(`app/auth/elevation.py`): an admin can **pause** their own elevation via
`POST /api/me/elevation` (UI on `/profile`), and while paused the
short-circuit is skipped — `can_access` falls through to the explicit
group-grant path and `require_admin` refuses with the distinct detail
`admin_elevation_paused`. State rides the `agnes_elevation` cookie, read
into a request contextvar by middleware; the cookie can only ever
*reduce* privilege (enforcement remains the server-side Admin-membership
check), so this is a guard against accidental god-mode use plus an audit
hook (`admin_elevation_paused`/`admin_elevation_resumed` audit actions),
not a containment boundary — a paused admin can re-elevate at will.

Scope, precisely: the gate sits in `can_access` and `require_admin`. Surfaces
that consult Admin membership directly rather than going through those — the
table/catalog visibility path is the notable one — are unaffected, so a paused
admin still sees every table. WebSocket routes are unaffected too: the stamping
middleware is `@app.middleware("http")` and never runs for a `ws` scope, so
those connections read the contextvar default (elevated). Both cases fail
toward the historical behavior and neither can grant a non-admin anything.
Widening the gate to the membership checks is a separate change: several of
them decide whether to offer the toggle at all.

The pause belongs to the admin who set it. `can_access` is also asked about
OTHER users (co-drive invites check the invitee), so the request also carries
whose pause it is — a paused admin's own checks fall through to their grants
while a question about a colleague is answered from the colleague's own
permissions.

The instance default is `access.admin_default_elevation: "elevated"`
(historical behavior); set `"paused"` for consent-first deployments.
The default applies to **browser sessions only**: Bearer-authenticated
requests (CLI, PATs, service tokens) carrying no elevation cookie always
run elevated — automation has no cookie jar to re-elevate with, so a
paused default must not 403 every `agnes admin …` call (an explicit
`paused` cookie is still honored even alongside a Bearer header).
Non-HTTP contexts (scheduler internals, background jobs) likewise run
elevated via the contextvar default. Each god-mode grant of a
resource the admin holds no explicit grant for emits a deduplicated
`god_mode_bypass` log line — the observability data for deciding where
explicit grants should replace god-mode reliance.

---

## Adding a new resource type

Everything lives in `app/resource_types.py`. Three edits, one file:

1. Add an enum member to `ResourceType`:

   ```python
   class ResourceType(StrEnum):
       MARKETPLACE_PLUGIN = "marketplace_plugin"
       DATASET = "dataset"  # new
   ```

2. Write a `list_blocks` delegate (no arguments) that reads through the `src.repositories` factory and projects the domain tables into the `(block → items)` shape the admin /access page consumes. Each item must include `resource_id` matching the path string written into `resource_grants`. Read through the factory — never a raw system-DB connection — so the projection hits the active backend (Postgres when configured) instead of the frozen DuckDB system file:

   ```python
   def _dataset_blocks() -> list[Block]:
       from src.repositories import table_registry_repo

       blocks: dict[str, Block] = {}
       for row in table_registry_repo().list_all():
           bucket = row.get("bucket") or "(no bucket)"
           block = blocks.setdefault(bucket, {"id": bucket, "name": bucket, "items": []})
           block["items"].append({
               "resource_id": f"{bucket}.{row['name']}",
               "name": row["name"],
               "description": row.get("description"),
           })
       return list(blocks.values())
   ```

3. Register a `ResourceTypeSpec` in `RESOURCE_TYPES`. The dataclass requires `list_blocks` so the type checker will catch a missing delegate:

   ```python
   RESOURCE_TYPES[ResourceType.DATASET] = ResourceTypeSpec(
       key=ResourceType.DATASET,
       display_name="Datasets",
       description="A table available in the analytics catalog.",
       id_format="<bucket>.<table_name>",
       list_blocks=_dataset_blocks,
   )
   ```

Then wire your endpoints with `require_resource_access(ResourceType.DATASET, "{bucket}.{table}")`.

No DB migration, no startup hook, no second wiring step in `access-overview` — the registry drives both `/api/admin/resource-types` (UI dropdown) and `/api/admin/access-overview` (resource tree).

---

## Group membership sources

Members are added to groups by three sources, distinguished by the `source` column:

- **`google_sync`** — written by the OAuth callback on every login. The previous Google-sync set is wholesale replaced (DELETE + INSERT) so a removed Workspace membership disappears immediately.
- **`admin`** — written by admin actions in the UI (`/admin/groups/{id}` → Members), CLI (`agnes admin group add-member …`), or REST (`POST /api/admin/groups/{id}/members`). Survives Google sync. Admin can only delete admin-source rows.
- **`system_seed`** — written at deploy time (the `SEED_ADMIN_EMAIL` → Admin-group binding) **and** at every new-user creation (the Everyone auto-grant, issue #748 — every creation path: Google OAuth first sign-in, `POST /auth/bootstrap`, admin `POST /api/users`, marketplace import stubs — unless `AGNES_GROUP_EVERYONE_EMAIL` maps Everyone to a Workspace group instead, in which case Everyone comes exclusively from `google_sync`). The Everyone grant fires once, at creation time, and is never re-asserted afterward — an admin who later removes a user from Everyone stays removed on their next login/boot.

Removing a user from a group via the admin path (UI/CLI/REST) only deletes admin-source rows. To revoke a Google-synced membership, the operator must change the upstream Workspace group instead — Agnes will pick up the change on the user's next login.

---

## Admin workflows

### UI

Accounts and access are two sections:

- **People** (`/admin/users`) — accounts: invite, activate/deactivate, passwords, delete. **Tokens** (`/admin/tokens`) sits beside it: every personal access token across users, for incident response and offboarding.
- **Access** (`/admin/access`) — groups, and what each one can use.

#### The Access workspace

A group is one object with two sides — an audience, and a bundle of what that audience can use — so it has one editor. `/admin/access` is a two-pane workspace:

- **Left** — every group, with its origin (system / custom / Google-synced), member count and grant count. Search matches name, description and Workspace address. `+ New group` opens the create drawer and selects the result here.
- **Right** — the selected group. Its header carries the name, the Workspace address it is really stored under, the origin pill, the description, the created date, and **Rename** / **Delete** (hidden for system and Google-synced rows, which the API refuses to change).
  - **Who it reaches** — a member count stated as its consequence, avatars, and one search box that both adds someone and answers "is Maria in this group?". **Show all N** expands the full roster with each member's source (`added by admin` / `synced from Google` / `system-managed`) and a Remove button on admin-added rows only.
  - **What it can use** — the grant matrix, by resource type, with a filter matching name, `resource_id`, block, category and description. Backed by `/api/admin/access-overview` + `/api/admin/grants`.

The second lens, **Simulate a person** (`/admin/access?lens=simulate`), walks one person's membership → grant → tier and names what is *not* shared with them.

Retired URLs, all 308 onto the workspace: `/admin/grants` and `/admin/groups` → `/admin/access`; `/admin/groups/{id}` → `/admin/access?group=<id>` (unknown ids still 404). `/admin/tables`' per-row *Manage access* arrives as `/admin/access?resource=<type>:<id>`, which pre-filters the grant tree; the older `#table:<id>` fragment is rewritten to it.

The one editor rule has two deliberate exceptions, both *transposes* rather than copies: a data package's **Share** panel answers "which groups get this package", and `/admin/users/{id}` answers "which groups is this person in". The user detail page also toggles Admin-group membership when an operator switches a user between admin and non-admin — there's no four-level hierarchy, just admin / non-admin — and lists that user's **effective access** (each row links to the granting group in the workspace) and their **access tokens**, so an offboarding runs on one page.

### CLI

```bash
agnes admin group list
agnes admin group create Engineering --description "Eng team"
agnes admin group delete Engineering
agnes admin group members Engineering
agnes admin group add-member Engineering alice@example.com
agnes admin group remove-member Engineering alice@example.com

agnes admin grant resource-types
agnes admin grant create Engineering marketplace_plugin foundry-ai/metrics-plugin
agnes admin grant list --type marketplace_plugin
agnes admin grant list --group Engineering
agnes admin grant delete <grant-id>
```

All subcommands authenticate via PAT and exit non-zero on API errors.

### REST

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/admin/groups` | GET / POST | list / create groups |
| `/api/admin/groups/{id}` | PATCH / DELETE | rename / delete (system groups read-only) |
| `/api/admin/groups/{id}/members` | GET / POST | list / add member |
| `/api/admin/groups/{id}/members/{user_id}` | DELETE | remove (admin-source rows only) |
| `/api/admin/grants` | GET / POST | list (with `?resource_type=` / `?group_id=`) / create |
| `/api/admin/grants/{id}` | DELETE | delete |
| `/api/admin/resource-types` | GET | enumerate the StrEnum |

Every mutation writes an audit log entry (`user_group.created`, `resource_grant.deleted`, …).

---

## PAT lifetime & renewal

`agnes auth login` (browser loopback flow) mints a 90-day personal access
token (PAT). Two options were on the table for keeping analysts signed in
without re-authenticating constantly:

1. A refresh-token grant (new server primitive: a long-lived refresh
   secret that mints short-lived access tokens).
2. **Proactive re-mint** (chosen) — keep the 90-day, individually revocable
   PAT as the only credential; have the CLI remind the analyst to re-run
   `agnes auth login` before it expires.

Option 2 ships: it needs no new server-side grant type, no new secret class
to protect, and no new revocation surface — the existing PAT list/revoke
API (`agnes auth token list` / `revoke`, `/me/profile`) already covers it.
The tradeoff is a small UX cost (an occasional re-login) in exchange for
not introducing a longer-lived secret than the 90-day PAT already is.

Mechanically: Agnes PATs are HS256 JWTs, so the `exp` claim is
client-decodable without the signing secret (which never leaves the
server). `cli/token_status.py` decodes it locally and prints a one-line
stderr nudge on non-quiet commands once the token is within
`AGNES_TOKEN_RENEW_DAYS` (default 7 days; `0` disables) of expiring, at
most once per UTC day. `agnes auth whoami` shows the same expiry
year-round; `agnes update`'s convergence report carries a `token` stage
(status `ok` / `renew-soon` / `skipped`) for the same info without ever
prompting from the unattended SessionStart hook. Renewal is just
`agnes auth login` again — it overwrites the stored token in place.

No server change was needed for this: no new grant type, no PAT default
TTL change. See [`docs/HEADLESS_USAGE.md`](./HEADLESS_USAGE.md#renewal-interactive-analysts).

---

## Bootstrapping the first admin

`SEED_ADMIN_EMAIL` (env var, set by the infra Terraform module) points at the operator's email. The app startup hook in `app/main.py`:

1. Creates a `users` row for that email if missing (with `password_hash` from `SEED_ADMIN_PASSWORD` if provided).
2. Adds an Admin-group membership with `source='system_seed'`.

The hook is idempotent — re-running deploy does not duplicate or revoke. To add additional initial admins post-deploy, log in as the seed admin and use `/admin/access` or `agnes admin group add-member Admin <email>`.

---

## Migration from v9–v12 (schema v13 cutover)

The v12→v13 migration is a single-step hard cutover. The Python helper `_v12_to_v13_finalize` runs after the new tables are created and:

1. Seeds Admin/Everyone in `user_groups` (idempotent).
2. Backfills `user_group_members` from `users.groups` JSON with `source='google_sync'`.
3. Promotes every `core.admin` user-role grant to Admin-group membership with `source='system_seed'`.
4. Adds Everyone-group membership for every existing user.
5. Translates `plugin_access` rows to `resource_grants` of type `marketplace_plugin`, resource_id `<marketplace>/<plugin>`.
6. Drops `plugin_access`, `user_role_grants`, `group_mappings`, `internal_roles` (FK-correct order).
7. Drops the `users.groups` JSON column. The legacy `users.role` column is kept NULL'd as an artifact (DuckDB historical FK constraints sometimes block DROP COLUMN; the field carries no semantic meaning post-v13).

No dual-write window. Either the schema is on v12 (old code) or v13 (new code).

---

## Schema v49 — `requirement` enum + new resource types

Schema v49 (unified Browse + My Stack for Data Packages and Memory):

- `resource_grants` gains a `requirement VARCHAR DEFAULT 'available'` column. Enum: `'available'` | `'required'`. Applies to `data_package`, `memory_domain`, and `memory_item` grants. Per-group decision: same resource can be Required for Sales but Available for Engineering without duplicating the resource itself.
- New resource types in `app.resource_types.ResourceType`:
  - `DATA_PACKAGE` — admin-curated bundle of tables (`data_packages` table; M:N to `table_registry` via `data_package_tables`). At v49 the effective `TABLE` set for a user was `(direct TABLE grants) ∪ (tables in DATA_PACKAGE grants the user has)`. This was later hardened: analyst table visibility now flows through Data Packages **only** (`src/rbac.py::can_access_table` / `get_accessible_tables`) — a direct `TABLE` grant no longer contributes to it at all. See [Table grants: agent-scope and co-session ceilings](#table-grants-agent-scope-and-co-session-ceilings) for what a direct `TABLE` grant is still for.
  - `MEMORY_ITEM` — per-group item-level Required override. Default for an item comes from `knowledge_items.is_required` flag; a `MEMORY_ITEM` grant flips that for the specified group.
- `MEMORY_DOMAIN` grants migrated from slug strings to `memory_domains.id` references. Orphan grants (pointing at non-existent domains) preserved for admin cleanup.
- Marketplace plugins: v49 originally left them out (`marketplace_plugins.is_system` was the only mandatory path), but the tier now applies to `marketplace_plugin` grants too — `resolve_user_marketplace` serves `granted ∩ (subscribed ∪ required)`, so a required grant puts the plugin in every group member's served set without a subscription row (unsubscribe/uninstall return 409). `is_system` remains the *global* (all-users) mandatory flag; `requirement='required'` is the *group-scoped* one.

Effective Required = OR across grants. Any grant with `requirement='required'` wins for the user.

**Stack membership modes** (`features.stack_auto_membership`, spec `docs/superpowers/specs/2026-08-07-default-chrome-ux-parity.md`): for `data_package`/`memory_domain` grants, `StackResolver` resolves in one of two modes.

- **Classic — the default** (the original v49 "subscribe to add" model): effective stack = `required ∪ (subscribed ∩ available)`. A `user_stack_subscriptions` row means "is in the stack", and every member is downloaded by `agnes pull`. A `required → available` downgrade on `PUT /api/admin/grants/{id}` eagerly fans out subscription rows to the group's members (idempotent `subscribe_group_members`) so nobody silently loses the resource.
- **Auto-membership — opt-in** (`features.stack_auto_membership: true`, implied by `instance.experience: redesign`): BOTH `required` and `available` grants are automatically in the user's stack — no subscription needed for visibility or server-side query authorization. `user_stack_subscriptions` keeps its schema but is reinterpreted: a row means "keep a local copy" (`agnes pull` downloads it), not "is in the stack". The `required → available` downgrade needs no fan-out — the resource stays in every granted user's stack, only the "always downloaded" guarantee relaxes to "downloaded once subscribed".

The flag flips behavior instantly; rows are interpreted, never rewritten. `marketplace_plugin` grants are mode-independent: they resolve via `resolve_user_marketplace` (`granted ∩ (subscribed ∪ required)`) off the separate opt-out `user_plugin_optouts` table, so a `required → available` downgrade there always eagerly fans out to keep every group member's served set from silently shrinking.

## Schema v14 — FK constraints

The v13→v14 migration adds DuckDB foreign-key constraints to `user_group_members` and `resource_grants`:

- `user_group_members.group_id` → `user_groups.id` (ON DELETE CASCADE)
- `resource_grants.group_id` → `user_groups.id` (ON DELETE CASCADE)

This prevents orphaned member/grant rows pointing at a deleted group. The migration uses RENAME → CREATE-with-FK → INSERT → DROP, wrapped in `BEGIN TRANSACTION` so a partial failure rolls back without leaving the DB at a half-applied schema.

No semantic changes — v14 is backward compatible with v13 application code.
