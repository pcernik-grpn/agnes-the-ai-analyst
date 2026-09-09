# Data App Viewer Identity & Sharing — Design

**Date:** 2026-09-09
**Status:** Draft for review

## 1. Context

Hosted data apps already require an Agnes login at the reverse proxy
(`app/api/data_apps_proxy.py`) and are authorized the same way as every other
Agnes resource type: `resource_grants(data_app, <slug>)` — owner, Admin
(god-mode), or a granted group, `Everyone` included. Data access from inside
the app is described in full in
[`2026-07-21-data-apps-design.md`](2026-07-21-data-apps-design.md) §8 "Data
access from apps": on deploy the control plane mints an owner-scoped service
token (`AGNES_TOKEN`, rotated every deploy), the app is an ordinary REST
client, and every read runs with **the owner's** RBAC grants, live, no matter
who is actually looking at the browser.

That gets an app running behind a login wall and lets the owner "publish"
whatever the app renders — the documented, deliberate semantics of "sharing a
data app shares its rendered output" (`docs/RBAC.md`). It leaves three gaps:

1. **The app has no idea who is looking.** A viewer who opens `/apps/<slug>/`
   is authenticated by the proxy, but nothing about their identity reaches
   the app process — `AGNES_TOKEN` names the owner, not them. An app author
   who wants to greet the viewer by name, or show/hide a section by group
   membership, has no signal to read.
2. **Data access can never be narrower than the owner's.** Owner-inherited
   access is the right default for a published dashboard (spec §8's
   "sharing is publication" framing), but it is wrong for an app meant to
   show each viewer only their *own* slice — a manager's headcount view, a
   customer-scoped report. There is no way to ask Agnes to check the
   *viewer's* grants at all, only the owner's.
3. **Sharing a data app is REST-only.** `GET/PUT /api/sharing/data_app/<slug>`
   already exists (`app/api/sharing.py`, owner-scoped), but nothing surfaces
   it where a builder actually is: no CLI command, no MCP tool the authoring
   agent can call, no control on the `/apps/detail/<slug>` page. An app
   author today either doesn't share the app at all or has to reach for
   `curl`.

This design closes all three, additively, without touching the existing
owner-inherited default or the sharing semantics already documented in
`docs/RBAC.md`.

## 2. Decisions

**Two signed artifacts, two different keys, on purpose.** The viewer identity
*assertion* (`X-Agnes-Viewer`) and the viewer *data token*
(`X-Agnes-Viewer-Token`) answer different questions and carry different
blast radii if leaked. The assertion is inert — it is a claim about who is
looking, useful for personalizing a page, and leaking it only tells an
attacker who else has looked. The data token is a live bearer credential
against the Agnes data API — leaking it is a real RBAC bypass. Keeping them
as two artifacts means an app that only wants personalization (the common
case) never has a live credential in its process at all, and the deploy-time
exposure scan can reason about each independently (`DA005`, extended for
both names plus the header value — see §4).

**Always-on assertion, opt-in data token.** The assertion costs nothing to
verify (an HMAC check, no I/O) and has no access implication, so it is
unconditional — every app gets to know who is looking, whether or not it
ever calls the data API as anyone but the owner. The data token changes RBAC
outcomes, so it stays behind an explicit per-app switch
(`data_identity: viewer`) an owner opts into deliberately, the same posture
the platform already takes toward anything that narrows or widens who sees
what.

**Per-app secret derived, not stored.** `AGNES_VIEWER_SECRET` is
`HMAC-SHA256(server_signing_key, "agnes/data-app-viewer-secret/v1|<slug>|
<service_token_id>")` — the server's existing signing key (already used
elsewhere for HMAC-based tokens) plus the slug and the id of the service
token minted for *this* deploy. Nothing new is persisted: the value is
recomputed wherever it's needed (spec-builder time, verification time) from
inputs Agnes already has. The `service_token_id` component is what makes
rotation free — `AGNES_TOKEN` is already re-minted (a fresh token, a fresh
id) on every deploy and wake per spec §8, so `AGNES_VIEWER_SECRET` rotates in
lockstep automatically, with no separate rotation logic to write or forget.

**Signed header, not a trusted one, because the network is shared.** All
data-app containers sit on one docker bridge (`agnes-apps`, per the
container-hardening notes in `docs/architecture.md`). A header the proxy
merely *adds* without signing would be exactly as trustworthy as one the
container invented itself, if anything on that bridge could reach another
app's ingress path and inject it — and the isolation the platform actually
guarantees is per-container process/capability isolation, not network
segmentation between apps. Signing with a per-app secret means app B cannot
forge a viewer assertion for app A even if it can reach A's proxied port:
the `aud` claim pins the token to the deploy that minted it, and B doesn't
hold A's secret. The proxy additionally strips any inbound
`X-Agnes-Viewer*` header from the browser before adding its own — belt and
suspenders, since a browser attacker is a different threat model than a
sibling container, but cheap to close either way.

**Owner ∩ viewer, never OR, never a full swap.** The tempting alternative —
run the query purely as the viewer once `data_identity: viewer` is set — was
rejected: it would let a viewer see anything *their own* grants allow inside
an app whose owner never had access to it, turning the app into an RBAC
elevation path for whoever the owner happened to grant `data_app` access to.
Intersecting keeps the app's own reach bounded by what its owner could
already see (unchanged from today), while additionally bounding it by what
*this* viewer can see — strictly narrower than owner-only, never wider. No
admin god-mode enters on either side of the intersection; an admin viewer
narrows the same way as anyone else, an admin owner does not widen it.

**`data_identity` is a Postgres-only column.** It is new app-state schema
under the A3 PG-first ratchet — a new column on the existing `data_apps`
row. Per the ratchet, this ships as an Alembic-only migration with no
`src/db.py` `_vN_to_v(N+1)` step; a DuckDB-backed instance reading or writing
it gets the typed `RequiresPostgresBackend` → `501 requires_postgres_backend`
translation, never a raw 500. `PATCH /api/data-apps/{slug}` documents this
explicitly rather than letting it surface as a generic PG-required error, so
`agnes app set-identity` and `data_app_set_data_identity` can give the
authoring agent (and the skill's §3b guidance) an actionable message instead
of a stack trace.

**Switching modes redeploys.** `AGNES_DATA_IDENTITY` is baked into the
container spec (`src/data_apps/spec.py`) alongside `AGNES_APP_SLUG`, the
same way `AGNES_TOKEN`/`AGNES_URL`/`AGNES_APP_ID` already are — env vars are
fixed at container start, so there is no live-reconfiguration path short of
restarting the process with a new spec. This is the same shape as any other
spec-affecting change already forces a redeploy (a new `AGNES_TOKEN` on
every deploy already does this); `data_identity` is not a special case.

## 3. Wire contract

**Assertion — `X-Agnes-Viewer` (always sent).** A compact HS256 JWT signed
with `AGNES_VIEWER_SECRET`. Claims:

| Claim | Meaning |
|---|---|
| `sub` | viewer's user id |
| `email` | viewer's email |
| `name` | optional, may be absent |
| `groups` | sorted list of group names, capped at 200 |
| `groups_truncated` | `true` only when the cap was hit |
| `aud` | `"data-app:<slug>"` |
| `iat` / `exp` | issued-at / expiry, 5 minute TTL |
| `via` | `session` \| `preview` \| `pat` — how the viewer reached the proxy |
| `typ` | `"data_app_viewer_assertion"` |

Minted per proxied HTTP request and per WS handshake. Any inbound
`X-Agnes-Viewer*` header from the browser is stripped before the proxy adds
its own, so the header is trustworthy exactly because — and only because —
the HMAC verifies.

**Data token — `X-Agnes-Viewer-Token` (only when `data_identity: viewer`).**
A short-lived (10 minute) server-signed bearer token, forwarded by the app as
`Authorization: Bearer …` against the same allowlisted data-API surface
`AGNES_TOKEN` already reaches (`/api/query`, `/api/data`, catalog reads,
etc. — see `docs/architecture.md`'s `pat_resolver.py` allowlist). Agnes
evaluates the request's RBAC as **owner ∩ viewer**, computed live on every
call, and binds any table access-policy's `$user_email`/`$user_id`/
`$user_groups` (`docs/table-access-policies.md`) to the *viewer*, not the
owner. In the default `owner` mode this header is never added.

**New container env** (alongside existing `AGNES_URL`, `AGNES_APP_ID`,
`AGNES_TOKEN`): `AGNES_APP_SLUG` (the `aud` value `getViewer` checks against)
and `AGNES_DATA_IDENTITY` (`owner` default | `viewer`).

## 4. Security invariants

- The proxy mints the assertion (and, when applicable, the data token) only
  after the same authorization check that already gates the request:
  live `resource_grants` lookup, the same-origin/subdomain isolation posture
  (`docs/architecture.md` "Origin isolation"), and the target container
  actually `running`. A sleeping or unauthorized request never reaches the
  point where either header would be minted.
- Inbound `X-Agnes-Viewer` / `X-Agnes-Viewer-Token` from the browser side of
  the proxy are always stripped before the proxy's own values are set — a
  hosted app's JS cannot inject a forged assertion for itself, let alone for
  a sibling app.
- The viewer data token is admitted only on the same narrow allowlist
  `AGNES_TOKEN`'s `data-app:<slug>` scope already uses in
  `app/auth/pat_resolver.py` — never the generic authenticated-user surface,
  and `require_admin`-gated routes refuse it exactly as they refuse
  `AGNES_TOKEN` today.
- Both the owner side and the viewer side of the intersection are
  **re-evaluated live per request** — no caching a grant snapshot at
  deploy time, no admin god-mode short-circuit on either side.
- **No internal-table carve-out.** `src/rbac.py` keeps the internal usage
  tables (`agnes_sessions`, `agnes_telemetry`, …) reachable for the *chat*
  principals (co-session / agent-session), which have no personal stack.
  `DataAppViewerPrincipal` opts out (`internal_tables_reachable = False`):
  it runs inside owner-authored code, so a table absent from both the
  owner's and the viewer's grants must stay unreadable through the app. The
  intersection is the whole authority — nothing is appended to it.
- Neither `AGNES_VIEWER_SECRET` nor the token value it signs is ever placed
  on a process argv or in a URL query string, matching the platform's
  existing rule for `AGNES_TOKEN` and every other credential
  (`.claude/skills/agnes-conventions/references/security.md`).
- The deploy-time exposure scan (`src/data_apps/deploy_check.py`, `DA005`)
  is extended to also flag `AGNES_VIEWER_SECRET` and the
  `X-Agnes-Viewer-Token` header value appearing on a response-emitting
  line, same rule shape as the existing `AGNES_TOKEN` check.

## 5. Surfaces

- **REST:** `PATCH /api/data-apps/{slug}` gains an optional `data_identity`
  field (owner or Admin only; triggers a redeploy; `501
  requires_postgres_backend` on a DuckDB-backed instance). The existing
  `GET/PUT /api/sharing/data_app/<slug>` and `GET /api/sharing/groups` are
  reused unchanged as the sharing backend — no new sharing REST surface.
- **CLI:** `agnes app share <slug>` (no args: show current sharing state;
  `--group NAME...`/`--everyone`/`--private` to change it) and `agnes app
  set-identity <slug> owner|viewer`.
- **MCP:** `data_app_share_get(slug)`, `data_app_share(slug, groups, everyone)`,
  and `data_app_set_data_identity(slug, data_identity)` — the tools the
  authoring agent calls from `agnes-data-apps-extras` SKILL.md §3b.
- **Web:** a Share control and a Data identity toggle on `/apps/detail/<slug>`,
  both thin clients over the same REST endpoints above.
- **Skill:** `agnes-data-apps-extras` SKILL.md §3b — after promote, offer
  sharing via `AskUserQuestion`; never share on the agent's own initiative.
  §7 documents `getViewer`/`agnesViewer.ts` for personalization; §8 adds the
  never-echo rule for the two new secret names.

## 6. Risks / non-goals

- **Not a public-apps mechanism.** Every surface here still sits behind the
  existing `resource_grants` gate — this design changes *what an
  authenticated, authorized viewer's request looks like inside the app*, not
  *who can reach the app at all*.
- **No per-app passwords, no OIDC.** The viewer's identity is exactly the
  Agnes identity the proxy already authenticated (session, preview grant, or
  PAT — the existing `via` values); this design adds no new authentication
  mechanism, only a way to assert an existing one into the container.
- **No per-app docker networks.** The shared-network threat this design
  defends against (§2, "signed header, not a trusted one") is closed by
  signing, not by network segmentation; per-app network isolation remains
  future work if profiling or a future audit finds the signing insufficient.
- **Drafts stay `owner`.** A draft app (the in-chat iteration branch,
  `references/promote-flow.md`) is single-viewer by construction — the
  author previewing their own work — so `data_identity` switching is a
  prod-app concern only; drafts never expose the toggle.
