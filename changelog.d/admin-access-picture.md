### Added
- **Agents can now answer the admin access questions.** New read-only MCP
  foundation tool `admin_access_picture` (admin identity required) returns the
  instance's access picture in one call: every data package with the groups
  granted it and their member counts, a per-group view of what a member can
  reach (the Admin group flagged as bypassing grants), and what nobody can
  reach — distributable tables in no package and packages granted to no group,
  folded the same way as the `/admin` gap cards. Until now no MCP tool exposed
  groups, grants or admin-side package membership, so the chat landing page's
  admin starters asked questions the agent could only decline. The picture is
  membership-mode aware (`in_stack: always | if_subscribed`), applies the stack
  resolver's draft / coming-soon rules to the per-group view, reads the table
  inventory from the admin registry (the catalog narrows a stack-surface admin
  credential to their own stack), says when a list was capped, and takes a
  `section` (`packages` / `by_group` / `unreachable`) so a large instance can
  be read one part at a time. See `docs/RBAC.md` → *Admin workflows → MCP
  (agents)*.
- `GET /api/admin/registry` carries `packaged_read_ok` so a consumer can tell
  a genuinely unpackaged table from the all-`false` stamps the endpoint falls
  back to when its package-membership read fails.
- `GET /api/admin/data-packages` accepts `?limit=` (1–5000, default 200 as
  before) so a reader can ask for the whole package inventory instead of
  silently losing every package past the 200th.

### Changed
- **The admin starters on the chat landing page say what they mean.** "Who can
  see what?" is now "Who has access to which data?", "What can nobody reach?"
  is "What is shared with no one?", and "What would an analyst see?" is "What
  does a non-admin see?", each with a subtitle naming the check it runs.
