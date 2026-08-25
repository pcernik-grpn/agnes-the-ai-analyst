---
name: agnes-rbac
description: Rules for endpoint gating (require_admin vs require_resource_access), ResourceType registration, and the user_groups model. Use when adding or changing endpoints in app/api/, touching app/auth/, or introducing a new resource type.
---

# Agnes access control

Two-layer model with no role hierarchy. See `CLAUDE.md § Access control` and
`docs/RBAC.md`.

## Tables

- `user_groups` — named groups. `Admin` (god-mode short-circuit on every
  authorization check) and `Everyone` (auto-membership) are seeded as
  `is_system=TRUE`.
- `user_group_members` — `(user_id, group_id, source)`. `source` segregates
  writers so Google's nightly sync does not clobber admin-added members.
- `resource_grants` — `(group, resource_type, resource_id)` triples for any
  entity-scoped grant.

## Gate decision

For every new endpoint, pick one:

- `Depends(require_admin)` — app-level mutations (anything that changes shared
  state without a per-entity scope: registering tables, creating users,
  managing groups, server config).
- `Depends(require_resource_access(ResourceType.X, "{path}"))` — entity-scoped
  reads or mutations. The path expression extracts the `resource_id` from the
  request.

Both imports live in `app.auth.access`.

## Adding a new ResourceType

1. Extend the `ResourceType` `StrEnum` in `app/resource_types.py` with the
   new value.
2. Register a `ResourceTypeSpec` for it in the same file, including a
   `list_blocks` projection delegate that returns the rows visible to a
   given caller.
3. **No DB migration needed** — `resource_grants` is generic.
4. Gate the endpoints that consume the new type with
   `require_resource_access(ResourceType.NEW, "{path}")`.

## Admin layer is the source of truth for auto-sync

For `agnes pull`: `query_mode IN ('local', 'materialized')` plus a
`resource_grants` row for one of the analyst's groups → table appears in
their manifest. There is no per-user sync config.

## Auth providers

Auth providers live in `app/auth/`:

- **Google OAuth** — sign-in via Google. Workspace group memberships are
  pulled at sign-in (see `docs/auth-groups.md` for GCP setup checklist + the
  `security` label gotcha).
- **Email magic link** — itsdangerous token.
- **Desktop JWT** — for the CLI / API.

## Admin UI and CLI

- Admin UI: `/admin/access`.
- CLI: `agnes admin group …` and `agnes admin grant …`.
