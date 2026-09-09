# Reading Agnes data from a hosted app — `agnesQuery.ts`

The baked scaffold (`scaffolds/nodejs-dashboard/server/agnesQuery.ts`) is the
**only** sanctioned way a hosted app reads Agnes data. Never hand-roll a
fetch to `/api/query` elsewhere in the app, and never hardcode a token —
both env vars below are injected by the Agnes apps-runner at container start,
scoped to the app's owner.

## Environment

- `AGNES_URL` — the Agnes server's internal base URL.
- `AGNES_TOKEN` — an owner-scoped bearer token. By default (`data_identity:
  owner`) every REST call the app makes runs under the token of whoever owns
  the app (see `docs/DEPLOYMENT.md` → *Data apps* → "Data access is
  owner-inherited") — never with elevated rights the owner doesn't already
  have. An app switched to `data_identity: viewer` instead runs each request
  as **owner ∩ viewer** — see `X-Agnes-Viewer-Token` below.
- `AGNES_VIEWER_SECRET` — the per-app HMAC key the Agnes proxy signs the
  `X-Agnes-Viewer` header with. Never log it, never send it anywhere; it
  exists only so `agnesViewer.ts`'s `getViewer` can verify a request came
  through the proxy.
- `AGNES_APP_SLUG` — this app's slug, for the `aud` check `getViewer`
  performs on every assertion.
- `AGNES_DATA_IDENTITY` — `owner` (default) or `viewer`. Read via
  `agnesQuery.ts`'s exported `DATA_IDENTITY` constant rather than
  `process.env` directly.

## Who is viewing — `X-Agnes-Viewer` / `agnesViewer.ts`

Every proxied request (HTTP and WS) carries an `X-Agnes-Viewer` header: a
compact HS256 JWT signed with `AGNES_VIEWER_SECRET`, claims `sub`, `email`,
`name` (optional), `groups` (sorted, capped at 200 with `groups_truncated:
true` when capped), `aud` (`data-app:<slug>`), `iat`, `exp` (5 min TTL),
`via` (`session`/`preview`/`pat`). The proxy strips any inbound
`X-Agnes-Viewer*` header from the browser first, so this header is
trustworthy **only because of the signature** — containers share one docker
network, and an unsigned header would be forgeable by any other app on it.

The scaffold's `server/agnesViewer.ts` is the sanctioned way to read it —
never decode the header yourself:

```typescript
// server/agnesViewer.ts (contract)
export type Viewer = { sub: string; email: string; name?: string; groups: string[]; via: string; exp: number };
export function getViewer(req: Request): Viewer | null;          // null on ANY failure, never throws
export function requireViewer(req, res, next): void;             // 401 JSON when null, else res.locals.viewer
export function viewerTokenFrom(req: Request): string | undefined;
```

`getViewer` recomputes the HMAC over the header+payload, compares it with a
constant-time check, and rejects an expired or wrong-audience token — see
the module's own docstring for the exact steps. Use it to personalize a page
or gate a section by `groups`; never trust an unsigned claim from anywhere
else (query string, a cookie you didn't set, `req.body`).

**Python (Flask/Streamlit) apps** verify the same header with `PyJWT`
instead — same claims, same secret:

```python
import os
import jwt

def get_viewer(headers: dict) -> dict | None:
    token = headers.get("X-Agnes-Viewer")
    if not token:
        return None
    try:
        return jwt.decode(
            token,
            os.environ["AGNES_VIEWER_SECRET"],
            algorithms=["HS256"],
            audience=f"data-app:{os.environ['AGNES_APP_SLUG']}",
        )
    except jwt.PyJWTError:
        return None
```

## `X-Agnes-Viewer-Token` — opt-in viewer data identity

When the app's `data_identity` is `viewer` (`PATCH /api/data-apps/{slug}
{"data_identity": "viewer"}`, `agnes app set-identity <slug> viewer`, or MCP
`data_app_set_data_identity`), the proxy *also* adds
`X-Agnes-Viewer-Token`: a short-lived (10 min) server-signed bearer token.
Forward it as `Authorization: Bearer …` to the Agnes data API in place of
`AGNES_TOKEN` — `agnesQuery.ts`'s `runQuery(sql, { viewerToken })` does this
for you; pull the token out of the request with `viewerTokenFrom(req)`.
Agnes then evaluates RBAC as **owner ∩ viewer**, live, and binds any table
access policy's `$user_email`/`$user_id`/`$user_groups` to the viewer, not
the owner. In the default `owner` mode this header is never sent, and
`viewerToken` is simply `undefined` — `runQuery` falls back to `AGNES_TOKEN`
exactly as before.

In `viewer` mode the fallback is deliberately **off**: a call that reaches
`runQuery` without a `viewerToken` throws instead of quietly running as the
owner (that would show every viewer the owner's data under a "viewer" label —
the one bug this mode exists to prevent). Always thread `viewerTokenFrom(req)`
through request handlers. For a call that genuinely has no viewer to act for
(a cache warm-up at boot, a scheduled background job) pass `asOwner: true`
explicitly: `runQuery(sql, { asOwner: true })`. `listTables()` /
`getTableProfile()` take an optional `viewerToken` and are owner-scoped when
called without one.

## Helper shape

```typescript
// server/agnesQuery.ts
export async function runQuery(sql: string): Promise<Record<string, unknown>[]> {
  const res = await fetch(`${process.env.AGNES_URL}/api/query`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${process.env.AGNES_TOKEN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ sql }),
  });
  if (!res.ok) {
    throw new Error(`agnes query failed: ${res.status}`);
  }
  const { rows } = await res.json();
  return rows;
}
```

Catalog/table lookups (what tables/columns exist, before writing a query)
go against the same base URL's catalog endpoints, with the same bearer
token — mirror `runQuery`'s error handling.

## Metrics before hand-written SQL

If a figure the app shows corresponds to a business metric, **fetch that
metric's definition and run its SQL** rather than writing your own query for
it. Two calls, both through the same helper:

```typescript
const def  = await agnesFetch(`/api/metrics/${id}`);  // definition, incl. `sql`
const rows = await runQuery(def.sql);                 // the value
```

`GET /api/metrics` lists what is defined. The lookup is RBAC-gated on the
metric's own tables, so it obeys the same owner-scoped boundary as any other
query the app runs.

Why it matters:

- The app's number then *means* what the same number means everywhere else in
  the organization — a dashboard quietly disagreeing with the rest of the
  business is the failure this prevents.
- The app holds no copy of the SQL. It reads the canonical definition at
  runtime, so a central correction to a metric reaches the app on its next
  load, with no redeploy and nobody having to remember.
- It is the same rule the root workspace `CLAUDE.md` already gives every agent
  reading Agnes data: look up the canonical definition, use that SQL, never
  invent metric calculations. An app is not an exception.

Where no metric exists, write the query — but consider whether the figure is
one others would want too, in which case defining the metric in Agnes serves
more than this app. Cache definitions briefly rather than re-fetching them on
every page load.

## Rules for the query itself

- Every query the app runs is subject to the owner's own RBAC grants — the
  app can only ever see what its owner is allowed to see (in `viewer` data
  identity, narrowed further to what the *viewer* is also allowed to see).
  If a query 403s, that's a real access boundary, not a bug to work around.
- Never embed secrets, other users' data, or PII in client-side code —
  `agnesQuery.ts` runs server-side (Express), and the SPA only ever talks to
  the app's own `/api/data`-style routes, never directly to Agnes.
- Prefer aggregate/paginated queries over `SELECT *` for anything backing a
  dashboard chart — the same discovery-first discipline the root workspace
  `CLAUDE.md` teaches for `agnes query` applies here too.
