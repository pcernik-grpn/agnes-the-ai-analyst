/**
 * Thin client for the Agnes REST API, in the shape of the upstream
 * `kbcQuery.ts` helper KAI's baked scaffold ships: reads AGNES_URL/AGNES_TOKEN
 * from the environment (injected by the Agnes control plane as an
 * owner-scoped service token, rotated on every deploy — see
 * docs/superpowers/specs/2026-07-21-data-apps-design.md §8) and calls the
 * normal Agnes REST endpoints. No SDK, no special sandbox wiring — this is
 * literally the same API the `agnes` CLI and MCP tools call.
 */

const AGNES_URL = process.env.AGNES_URL ?? "http://app:8000";
const AGNES_TOKEN = process.env.AGNES_TOKEN ?? "";

/**
 * `owner` (default): every call runs as the app owner's `AGNES_TOKEN` —
 * today's behaviour. `viewer`: the app was switched to
 * `data_identity=viewer` (`PATCH /api/data-apps/{slug}`), so a request
 * carrying the proxy's `X-Agnes-Viewer-Token` should authenticate as the
 * viewer instead — see `agnesViewer.ts` `viewerTokenFrom`.
 */
export const DATA_IDENTITY = process.env.AGNES_DATA_IDENTITY ?? "owner";

/**
 * In `viewer` mode a request that forgot to pass the viewer token must NOT
 * quietly fall back to the owner's token — that would show every viewer the
 * owner's data under a "viewer" label, the one bug this mode exists to
 * prevent. Pass `asOwner: true` for a deliberate owner-scoped call (a cache
 * warm-up, a background job) that has no viewer to act for.
 */
function authHeaders(viewerToken?: string, asOwner = false): Record<string, string> {
  if (DATA_IDENTITY === "viewer" && !viewerToken && !asOwner) {
    throw new Error(
      "AGNES_DATA_IDENTITY=viewer: this call needs the viewer token from the request " +
        "(agnesViewer.ts viewerTokenFrom) — refusing to fall back to the owner token",
    );
  }
  return {
    "Content-Type": "application/json",
    Authorization: `Bearer ${viewerToken ?? AGNES_TOKEN}`,
  };
}

async function agnesFetch(
  path: string,
  init: RequestInit = {},
  viewerToken?: string,
  asOwner = false,
): Promise<Response> {
  const res = await fetch(`${AGNES_URL}${path}`, {
    ...init,
    headers: { ...authHeaders(viewerToken, asOwner), ...(init.headers ?? {}) },
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`Agnes API ${path} -> ${res.status}: ${body}`);
  }
  return res;
}

export interface QueryResult {
  columns: string[];
  rows: unknown[][];
  row_count: number;
  truncated: boolean;
}

/**
 * Run a read-only SQL query against Agnes — exactly `POST /api/query` (the
 * same endpoint the CLI/MCP use). RBAC-scoped to the app owner's grants by
 * default; pass `viewerToken` (from `agnesViewer.ts` `viewerTokenFrom`,
 * only present when `DATA_IDENTITY === "viewer"`) to instead run the query
 * as **owner ∩ viewer** — Agnes evaluates both sides live, never just one.
 */
export async function runQuery(
  sql: string,
  {
    limit = 1000,
    viewerToken,
    asOwner = false,
  }: { limit?: number; viewerToken?: string; asOwner?: boolean } = {},
): Promise<QueryResult> {
  const res = await agnesFetch(
    "/api/query",
    {
      method: "POST",
      body: JSON.stringify({ sql, limit }),
    },
    viewerToken,
    asOwner,
  );
  return (await res.json()) as QueryResult;
}

export interface CatalogTable {
  id: string;
  name: string;
  description?: string | null;
  source_type?: string | null;
  sync_strategy?: string | null;
  query_mode: string;
}

/** List the tables this app's owner can see — `GET /api/catalog/tables`. */
export async function listTables(viewerToken?: string): Promise<CatalogTable[]> {
  const res = await agnesFetch("/api/catalog/tables", {}, viewerToken, viewerToken === undefined);
  const body = (await res.json()) as { tables: CatalogTable[]; count: number };
  return body.tables;
}

/** Look up a single table's profile (row counts, column stats) by name. */
export async function getTableProfile(tableName: string, viewerToken?: string): Promise<Record<string, unknown>> {
  const res = await agnesFetch(`/api/catalog/profile/${encodeURIComponent(tableName)}`, {}, viewerToken, viewerToken === undefined);
  return (await res.json()) as Record<string, unknown>;
}
