# Reading Agnes data from a hosted app — `agnesQuery.ts`

The baked scaffold (`scaffolds/nodejs-dashboard/server/agnesQuery.ts`) is the
**only** sanctioned way a hosted app reads Agnes data. Never hand-roll a
fetch to `/api/query` elsewhere in the app, and never hardcode a token —
both env vars below are injected by the Agnes apps-runner at container start,
scoped to the app's owner.

## Environment

- `AGNES_URL` — the Agnes server's internal base URL.
- `AGNES_TOKEN` — an owner-scoped bearer token. The app's REST calls run
  under the token of whoever owns the app (see `docs/DEPLOYMENT.md` → *Data
  apps* → "Data access is owner-inherited") — never under the viewer's own
  identity, and never with elevated rights the owner doesn't already have.

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
  app can only ever see what its owner is allowed to see. If a query 403s,
  that's a real access boundary, not a bug to work around.
- Never embed secrets, other users' data, or PII in client-side code —
  `agnesQuery.ts` runs server-side (Express), and the SPA only ever talks to
  the app's own `/api/data`-style routes, never directly to Agnes.
- Prefer aggregate/paginated queries over `SELECT *` for anything backing a
  dashboard chart — the same discovery-first discipline the root workspace
  `CLAUDE.md` teaches for `agnes query` applies here too.
