# Google Workspace Group Sync

How Agnes pulls a user's Workspace group memberships at Google sign-in and
where they end up in the database.

## Flow at a glance

The OAuth callback in `app/auth/providers/google.py` calls
`app.auth.group_sync.fetch_user_groups(email)` and feeds the result into
`UserGroupMembersRepository.replace_google_sync_groups`, which DELETE+INSERTs
the user's `source='google_sync'` rows in `user_group_members`. Admin-added
rows (`source='admin'`) and seeded system rows (`source='system_seed'`) are
untouched.

```
Browser → /auth/google/callback
  → exchange code for ID token (email)
  → fetch_user_groups(email)        ← keyless DWD + Admin SDK groups.list
  → optional prefix filter + system-group mapping
  → ensure each group in user_groups
  → replace_google_sync_groups(...)  ← per-user DELETE+INSERT, source-scoped
  → set session cookie, redirect to /dashboard
```

The fetch is **fail-soft**: any error (missing config, API 4xx/5xx, network
outage) returns `[]`, the membership snapshot from the previous login stays
intact, and the user is signed in regardless. A transient outage does not
empty a user's groups.

## How `fetch_user_groups` authenticates to Google

The function in `app/auth/group_sync.py` uses **keyless Domain-Wide
Delegation**: the VM service account signs the impersonation JWT through
the IAM `signJwt` API (no private key on disk anywhere), then exchanges
that JWT for a short-lived OAuth token scoped to
`admin.directory.group.readonly`. The Admin SDK
`groups.list?userKey=<email>` endpoint returns both static and dynamic
group memberships in one paginated call.

Two identities are involved:

- **The VM service account** (auto-detected from the GCE metadata server)
  is the issuer of the JWT. Its IAM unique ID must be allowlisted via DWD.
- **The impersonated subject** (`GOOGLE_ADMIN_SDK_SUBJECT` env var) is a
  real Workspace user with directory read privileges. The Admin SDK call
  is authorized as if that admin made it.

## Filtering and storage

What the OAuth callback does with the list returned by `fetch_user_groups`:

1. **Prefix filter.** If `AGNES_GOOGLE_GROUP_PREFIX` is set (e.g.
   `grp_acme_`), only emails whose local part starts with the prefix
   survive into Agnes; the rest are discarded. If unset, every fetched
   group is mirrored (legacy behavior).
2. **System-group mapping.** One optional env var routes a specific
   Workspace email into a seeded system row instead of creating a fresh
   `user_groups` entry:
   - `AGNES_GROUP_ADMIN_EMAIL` — when set, membership in the matching
     Workspace group adds the user to the seeded `Admin` row.

   This lets operators have a Workspace group like
   `grp_acme_admin@example.com` show up in Agnes as the canonical
   `Admin` system group (with the same `is_system=TRUE` semantics, the
   same membership-table id) — no parallel "near-Admin" row.

   `AGNES_GROUP_EVERYONE_EMAIL` did the same for `Everyone` and is
   **retired** (migration `0098`) — see [Everyone membership](#everyone-membership-single-mode-since-migration-0098)
   below for why, and for what an upgrade does to an instance that has it
   set. `Admin` keeps its mapping because it is a capability, not an
   audience: narrowing it to a Workspace group takes nothing away from
   anybody.
3. **Login gate.** If `AGNES_GOOGLE_GROUP_PREFIX` is set AND the fetch
   returned a non-empty list AND none of those groups match the prefix →
   the callback redirects to `/login?error=not_in_allowed_group`. The
   prefix gate fires only on a real, prefix-mismatched answer; if the
   Admin SDK returned an empty list (transient failure or genuine
   no-membership), the previous cached snapshot is preserved (fail-soft)
   and the login proceeds — locking returning users out on a flaky API
   call would be worse than the alternative.
4. **Storage.** Surviving groups land in `user_group_members` with
   `source='google_sync'`. The underlying `user_groups` row's `name` is
   the **full Workspace email** (no separate `external_id` column — the
   email IS the canonical identifier), `created_by='system:google-sync'`.
   Admin UI strips the prefix and `@domain` for display
   ("grp_acme_finance@example.com" → "Finance" big + email subtitle
   small).
5. **Refresh semantics.** The previous Google-sync set is wholesale
   replaced (DELETE + INSERT for `source='google_sync'` rows) so a removed
   Workspace membership disappears immediately. Admin-added memberships
   (`source='admin'`) are preserved — Google sync only touches its own
   rows. Memberships refresh on every Google sign-in **and** on demand
   via `POST /auth/refresh-groups` (CLI: `agnes auth refresh-groups`),
   which reuses the same write path so a CLI / PAT-only user who just
   got added to a new Workspace group doesn't have to re-sign-in on the
   dashboard to see their access. Both paths run through
   `app.auth.group_sync.apply_user_groups` so policy stays single-sourced.

**Read-only admin UI on Google-managed rows.** The admin UI hides the
Edit / Delete affordances on rows owned by Google sync
(`created_by='system:google-sync'`) and on the seeded `Admin` / `Everyone`
rows when their email-mapping env var is set. The REST API enforces the
same rule: PATCH / DELETE / add-member / remove-member return
`409 google_managed_readonly` for these rows. To add or remove members,
an operator changes Workspace membership at admin.google.com and the user
signs in again to Agnes.

**Everyone membership: single-mode since migration `0098`.** Every new user
is auto-granted `Everyone` at creation time (`source='system_seed'`), across
all creation paths (Google OAuth first sign-in, `POST /auth/bootstrap`, admin
`POST /api/users`, marketplace import stubs). The grant happens once, at
creation — it is **not** re-asserted at login or on boot. Schema v86
backfills users created before this behavior existed.

**`AGNES_GROUP_EVERYONE_EMAIL` is retired and inert.** It used to mirror the
seeded `Everyone` row from a Workspace group, which meant "everyone" named a
SUBSET of the accounts on such an instance — while
`marketplace_plugins.is_system` meant all of them, so the two disagreed about
who "everyone" was. That is the ambiguity the `scope` model removes.

Migration `0098` converts it: an instance with the variable set gets an
ordinary group **named after the Workspace email**, holding the same members
with their `source` preserved (so this sync keeps writing them) and holding
the grants that were written against the pseudo-group. Nobody gains or loses
access. Afterwards the address takes the ordinary path above — a group whose
`name` IS the email, `created_by='system:google-sync'` — and the seeded
`Everyone` row goes back to meaning every account, reporting
`origin='system'` with `mapped_email: null` on `GET /api/admin/groups`.

Leaving the variable set does nothing; nothing reads it at runtime. It is
safe to remove from the environment at any point after upgrading.

`AGNES_GROUP_ADMIN_EMAIL` is **unaffected** and still maps the seeded `Admin`
row: `Admin` is a capability, not an audience, so mapping it narrows nothing.

One irreversibility to know about: the conversion is one-way. A downgrade
restores the columns `0098` dropped but cannot tell which of the converted
group's members were originally the seeded row's.

The `user_group_members` table is the single source of truth for group
memberships, used by:

- RBAC authorization (`app/auth/access.py`) — `require_resource_access`
  checks group grants
- Admin UI (`/admin/access`) — member lists, grant counts
- CLI (`agnes admin group members`) — group membership queries
- Marketplace filtering (`src/marketplace_filter.py`) — plugin access
  based on group grants

## GCP setup (one-off, per deployment)

1. **Enable Admin SDK API** on the project:
   ```
   APIs & Services → Library → "Admin SDK API" → Enable
   ```
2. **IAM binding on the VM SA** — grant the SA `roles/iam.serviceAccountTokenCreator`
   on itself, so it can call `IAMCredentials.signJwt`:
   ```bash
   gcloud iam service-accounts add-iam-policy-binding <sa-email> \
     --member="serviceAccount:<sa-email>" \
     --role="roles/iam.serviceAccountTokenCreator" \
     --project=<project-id>
   ```
3. **Domain-Wide Delegation** in `admin.google.com`:
   ```
   Security → API controls → Domain-wide Delegation → Add new
   Client ID:   <SA's numeric Unique ID, e.g. 103511645014740068359>
   OAuth scope: https://www.googleapis.com/auth/admin.directory.group.readonly
   ```
   The Unique ID is the field `uniqueId` returned by
   `gcloud iam service-accounts describe <sa-email>`.

This setup is per Workspace tenant. A Workspace super admin must grant
the DWD entry; project-level GCP IAM cannot do it.

## Required env on the VM

```env
GOOGLE_ADMIN_SDK_SUBJECT=admin@your-domain.com
```

The Workspace admin email the SA impersonates. **Without this, the function
fails soft and returns `[]`** — group sync is silently disabled. The admin
must already have directory read privileges in `admin.google.com`; a regular
user with no admin role will produce a `403 Not Authorized` from the Admin
SDK even with DWD in place.

## Optional env

```env
AGNES_GOOGLE_GROUP_PREFIX=grp_acme_
AGNES_GROUP_ADMIN_EMAIL=grp_acme_admin@example.com
GOOGLE_ADMIN_SDK_SA_EMAIL=explicit-sa@project.iam.gserviceaccount.com
```

- `AGNES_GOOGLE_GROUP_PREFIX` / `AGNES_GROUP_ADMIN_EMAIL` — see
  [Filtering and storage](#filtering-and-storage). Empty / unset = legacy
  "mirror all groups, no gate, no system mapping".
- `AGNES_GROUP_EVERYONE_EMAIL` is retired and inert — nothing reads it.
  Safe to delete from the environment once the instance has run `0098`.
- `GOOGLE_ADMIN_SDK_SA_EMAIL` — when unset, the SA email is auto-detected
  from the GCE metadata server. Set this only when running off-VM (CI /
  local dev with explicit ADC) or when impersonating a different SA than
  the one the VM is attached to.

## Local dev / CI mock

```env
GOOGLE_ADMIN_SDK_MOCK_GROUPS=engineers@example.com,admins@example.com
```

When set, all Google calls in `fetch_user_groups` are bypassed and the
function returns the parsed list verbatim. Empty value (`""`) returns
`[]`. Unset → real keyless-DWD path. The mock is honoured regardless of
`LOCAL_DEV_MODE` so integration tests can exercise the full callback path
with deterministic group lists.

A separate mechanism, `LOCAL_DEV_GROUPS`, is used when `LOCAL_DEV_MODE=1`
bypasses the OAuth flow entirely (so `fetch_user_groups` is never called).
`get_current_user` in `app/auth/dependencies.py` reads that JSON array and
writes it directly into `user_group_members`:

```bash
export LOCAL_DEV_GROUPS='[{"id":"engineers@example.com","name":"Engineering"},{"id":"admins@example.com","name":"Admins"}]'
```

`docker-compose.local-dev.yml` carries a commented example at the right
escape level for Compose YAML. **Never set this in production** — the
variable is only honoured when `LOCAL_DEV_MODE=1`.

## Verifying the setup

After Terraform apply + subject seeded into `.env`, on the VM:

```bash
sudo docker exec agnes-app-1 python -c "
from app.auth.group_sync import fetch_user_groups
print(fetch_user_groups('user@your-domain.com'))
"
```

Expected: a Python list of group emails. `[]` means either the user has no
groups or the function fail-softed — check `docker logs agnes-app-1 | grep
"group sync\|group fetch\|Admin SDK"` for the actual reason.

Common failure modes:

- `... GOOGLE_ADMIN_SDK_SUBJECT not set; skipping group fetch` — env var
  missing.
- `... Admin SDK init failed: ...` — DWD entry missing or wrong client ID,
  Admin SDK API disabled, or `tokenCreator` IAM binding missing.
- `... Group fetch failed for X: HttpError 403 Not Authorized to access
  this resource/api` — the impersonated subject does not have directory
  read privileges in Workspace.

## Custom (admin-managed) groups

Admins can still create / rename / delete groups manually via
`/admin/access`. Two caveats vs. the prefix-mapped flow:

- A renamed group's primary key (`id`) stays put, but DuckDB's UNIQUE
  constraint on `name` combined with the FK from
  `user_group_members.group_id` makes renaming a populated group awkward
  — the operator must clear members + grants first, rename, then re-add.
  Documented limitation; the same constraint blocks the prefix-mapping
  design from using `external_id` so the email is the name.
- System groups (`Admin`, `Everyone`) refuse renames at the repository
  level regardless of `created_by` — those names are referenced from
  code (`app.auth.access`, marketplace filter, the email-mapping check)
  and must not move.

## Why not the simpler approaches

Earlier iterations tried two simpler paths that did not work in every
deployment:

- **User OAuth token + Cloud Identity API + `groups.security` label**.
  Worked at one tenant where every group carried the `security` label, but
  returned `403 Error 4013` at another where group label coverage differs.
  Tenant-dependent, so dropped from the codebase.
- **VM SA + Cloud Identity `searchTransitiveGroups` with admin role**.
  Requires assigning a Workspace admin role to the SA, which several
  Workspace tenants block for cross-tenant service accounts (`prj-*` SAs
  living under a different Cloud Organization than the Workspace customer
  ID). DWD is the documented way around that.

Keyless DWD is the path that works regardless of tenant configuration and
keeps zero key material on the host.
