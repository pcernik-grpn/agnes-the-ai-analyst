# Marketplace internals

How Agnes ingests admin-registered Claude Code marketplaces and re-serves a
single aggregated, RBAC-filtered marketplace back to user instances. CLAUDE.md
carries a one-paragraph summary; this doc is the reference.

For the *content-authoring* side (cover photos, demo videos, doc links via
`marketplace-metadata.json`), see [`curated-marketplace-format.md`](curated-marketplace-format.md).

## Marketplace repositories (ingestion)

Admin-managed git repos cloned nightly to `${DATA_DIR}/marketplaces/<slug>/` so
FastAPI can read their contents from disk.

- Register via `/admin/marketplaces` (admin UI) or `POST /api/marketplaces`.
- Scheduler calls `POST /api/marketplaces/sync-all` (admin-only, authed via
  `SCHEDULER_API_TOKEN`) at `daily 03:00` UTC. Routing through HTTP keeps the
  app the sole writer to `system.duckdb` — the previous in-process call from the
  scheduler container raced the app's long-lived DB handle and 500-ed on
  `Could not set lock on file`.
- Manual re-sync from the UI ("Sync now") hits `POST /api/marketplaces/{id}/sync`.
- PATs for private repos persist to `${DATA_DIR}/state/.env_overlay` (chmod 600)
  as `AGNES_MARKETPLACE_<SLUG>_TOKEN`. DuckDB stores only the env-var name
  (`token_env`), never the secret.
- Registry lives in DuckDB table `marketplace_registry`.
- A marketplace can be pinned to a fixed **tag name or full 40-char commit
  SHA** via `ref` (mutually exclusive with `branch` — 400 if both are set).
  Once set, nightly and manual syncs stay at that exact ref even when
  upstream's default branch moves; bump the pin by editing the field.
  A SHA pin that isn't reachable (mismatch/typo) fails the sync and leaves
  the previous checkout serving, same as any other sync failure.
- After each successful sync, `src/marketplace.py` parses
  `.claude-plugin/marketplace.json` from the cloned repo and caches the plugin
  list in `marketplace_plugins` (keyed by `(marketplace_id, plugin_name)`).
- `src/marketplace.py` handles clone/fetch/reset with token redaction in any
  surfaced error message.

## Claude Code marketplace endpoint (re-serving)

Agnes serves a single aggregated Claude Code marketplace over two channels, both
gated by PAT auth and filtered per caller:

- `GET /marketplace.zip` — deterministic ZIP download with `ETag` /
  `If-None-Match` (304 when content unchanged). Consumed by a client-side
  SessionStart hook.
- `GET /marketplace.git/*` — git smart-HTTP, served by the real `git
  http-backend` CLI run as a CGI subprocess (dulwich only builds the on-disk
  bare repo; see `app/marketplace_server/git_router.py`). Registered in
  Claude Code once, then Claude Code owns the clone/fetch cycle.

**Auth:** ZIP uses `Authorization: Bearer <PAT>`. Git uses HTTP Basic where the
password field carries the PAT (`https://x:<PAT>@host/marketplace.git/`) — git
CLI does not speak Bearer.

**Content:** filtered via `src.marketplace_filter.resolve_allowed_plugins` which
joins `resource_grants ↔ marketplace_plugins` (matching
`mp.marketplace_id || '/' || mp.name = rg.resource_id`) scoped to the caller's
`user_group_members`. Admin is treated as a regular group here — no god-mode
shortcut for the marketplace feed, so admins curate their own view by granting
plugins to the Admin group (or any group they belong to).

A grant is eligibility, not inclusion: the served set is
`granted ∩ (subscribed ∪ required)`. Users subscribe per-plugin on
`/marketplace`; grants held at `requirement='required'` are always-in-stack
for the granted groups' members (no subscription row needed, unsubscribe
returns 409) — the same tier semantics the StackResolver applies to data
packages. The separate global `marketplace_plugins.is_system` flag remains
the instance-wide mandatory path (fans out materialized subscriptions to
every user).

**Two filtering layers — `admin_disabled` gates above RBAC.** Before the
grant/group join runs, a plugin an admin disables via the `/admin/marketplaces`
Details modal (`marketplace_plugins.admin_disabled = TRUE`) is removed
instance-wide for everyone, regardless of grants: it disappears from the served
feed, the browse page, every user's my-stack, the synthetic served marketplace,
the group Access tab's grant UI, the v2 `/skills` endpoint, and the Library
page. The per-plugin curated endpoints treat it as nonexistent — the detail
page, its inner skill/agent pages, served docs, and the install action all
answer 404, for admins too.

**One deliberate exception: the two cover-art paths** (`…/asset/{path}` and
`…/mirrored/{key}`) keep serving a disabled plugin's images. Both are
login-only with *no* per-plugin RBAC, because the content is curator-designed
marketing visuals carrying no PII, source or secrets — so any authenticated
caller could already fetch them without a grant, and a disable check would
hide nothing from anyone who could not already read it. It would, however, put
one serialized DuckDB round-trip per image back on a render-blocking path
those endpoints were explicitly stripped of DB work for (12-20 covers per
`/marketplace` grid render). A disabled plugin appears on no listing, so the
URL is reachable only by a caller who already knows it. Pinned by
`tests/test_admin_disabled_curated_surfaces.py`.

The only surface that still shows a disabled plugin in the UI is the Details
modal, where it can be re-enabled. Disabling also clears `is_system` (re-enabling does **not** restore
it), and the disabled state survives nightly sync and the built-in re-seed on
boot (the `replace_for_marketplace` upsert never resets `admin_disabled`), so a
disabled plugin stays disabled across restarts.

Because a disabled plugin is also hidden from the group Access tab's grant UI, its
existing `resource_grants` rows are preserved but not editable there while it is
disabled — they are inert (the plugin is filtered out of every served surface
regardless of grants) and are restored intact on re-enable. This is deliberate:
"disabled" means invisible everywhere except the re-enable control, not "grants
deleted". To retire a plugin permanently, tick **"Also revoke all group
grants"** in the disable dialog (or POST the disable endpoint with
`{"revoke_grants": true}`) — the grants are deleted in the same action and the
response reports the count as `revoked_grants`.

The disable/enable pair is also scriptable: `agnes admin marketplace
disable-plugin <marketplace>/<plugin> [--revoke-grants]` and
`enable-plugin <marketplace>/<plugin>`, alongside `list` and `sync <slug>`
(admin PAT required; marketplace registration/deletion stay web-UI-only
because they carry a git token).

**Curator-side deprecation.** A plugin whose `marketplace-metadata.json`
section carries the literal boolean `"deprecated": true` (optionally with
single-line `deprecation_note` and `replacement` strings) is admin-disabled
automatically on every sync — one upstream commit retires the plugin on every
Agnes instance consuming the repo. The flag is one-way: removing it later never
auto-re-enables, and re-enabling a still-flagged plugin only lasts until the
next sync re-disables it. The admin Details modal shows a DEPRECATED pill with
the curator's note; grants are untouched (pair the flag with the retirement
checkbox above when they should go too). Plain Claude Code consumers of the
same repo ignore the metadata file entirely, so the field degrades gracefully
outside Agnes. See
[`docs/curated-marketplace-format.md`](curated-marketplace-format.md) for the
field reference.

On-disk layout in the served ZIP / git tree uses a slug-prefixed directory
(`plugins/<slug>-<plugin>/`) so two marketplaces shipping a same-named plugin
don't overwrite each other's files. The synth marketplace.json's `name` field,
however, is the plugin's authoritative name from its own
`.claude-plugin/plugin.json` (with a fallback to the upstream marketplace.json
`name`) — Claude Code's `/plugin` UI resolves a loaded plugin back to its
catalog entry by `plugin.json` name, so the catalog entry's `name` must match.
Same-named plugins from two upstream marketplaces therefore collide in the
catalog by design; admin RBAC (which grants survive the filter) decides which
one wins, identical to how Claude Code behaves when a user adds two upstream
marketplaces with overlapping plugin names directly. `/marketplace/info` exposes
both `name` and `prefixed_name` so operators can disambiguate.

**Where a plugin's files come from (`source`).** Each entry in the upstream
`.claude-plugin/marketplace.json` declares a `source`. Agnes serves files it has
on disk inside the clone, so it resolves the entry like this
(`src.marketplace_filter._contained_plugin_dir`):

| `source` | Files served from |
|---|---|
| absent / empty | `plugins/<name>/` (the conventional layout) |
| `"./"` | the clone root — the single-plugin-repo shape, where the plugin IS the repo |
| `"./any/sub/dir"` | that subdirectory of the clone |
| object, e.g. `{"source": "github", "repo": "owner/repo"}`, or a remote string (`https://…`, `git@host:owner/repo`) | nothing — unless the curator ALSO vendored the plugin at `plugins/<name>/`, in which case that directory is served |
| a path escaping the clone (absolute, or containing `..`) | the row is dropped at ingest with a warning; it never reaches the catalog |

A plugin with an external `source` and no vendored directory stays a
**catalog-only entry**: it shows on Browse and keeps its grants, but the served ZIP / git tree
carries no files for it (Claude Code fetches those from the external source
itself). The server logs one line per such plugin naming it and the reason.
`.git/**`, `.agnes/**` and `marketplace-metadata.json` are never served, which
matters most for `source: "./"` where the plugin directory is the git clone;
the plugin detail page's file list and bundle size apply the same exclusion.

**Cache:** content-addressed bare repos at `${DATA_DIR}/marketplaces/git-cache/`
keyed by sha256(filtered content). Two users with the same RBAC view share one
repo; content change → new repo next to the old one. No TTL / prune yet.

## User registration inside Claude Code

```
# ZIP channel (typically via a SessionStart hook that unpacks into ./marketplace/)
curl -H "Authorization: Bearer $AGNES_PAT" https://agnes.example.com/marketplace.zip

# Git channel — one-time registration. Two paths; pick the first that works.

# (a) Direct registration — preferred when it works.
/plugin marketplace add https://x:$AGNES_PAT@agnes.example.com/marketplace.git/

# (b) Two-step fallback — required when (a) fails. Bun-compiled `claude` on
#     macOS / Windows ignores the OS trust store and CA env vars on the
#     marketplace HTTPS path, so direct add can fail with TLS errors against
#     a private-CA Agnes instance even when system tools work fine. System
#     `git` honors GIT_SSL_CAINFO + the OS trust store, so cloning manually
#     and pointing Claude Code at the local clone sidesteps the Bun TLS path
#     entirely.
git clone https://x:$AGNES_PAT@agnes.example.com/marketplace.git/ ~/agnes-marketplace
claude plugin marketplace add ~/agnes-marketplace
# Optional hardening: strip the PAT from the cloned repo's origin so it
# doesn't sit in plaintext at ~/agnes-marketplace/.git/config — re-clone via
# the dashboard's setup flow when the PAT rotates.
git -C ~/agnes-marketplace remote set-url origin https://agnes.example.com/marketplace.git/
```

Analysts do not run any of this by hand during setup: `agnes onboard` (the one
command the dashboard's install prompt pastes into Claude Code) calls
`agnes refresh-marketplace --bootstrap`, which clones, strips the PAT from the
clone's origin, and registers the clone with Claude Code — the same reconcile the
`SessionStart` hook runs on every later session. The block above is the manual
equivalent for registering outside that flow (e.g. operators bringing up a new
instance, or a user whose bootstrap failed and wants to retry by hand).
