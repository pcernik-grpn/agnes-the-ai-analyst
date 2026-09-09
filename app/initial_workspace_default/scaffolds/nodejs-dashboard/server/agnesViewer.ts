/**
 * Verifies the `X-Agnes-Viewer` header the Agnes ingress proxy attaches to
 * every proxied request/WS handshake — a compact HS256 JWT signed with a
 * per-app secret (`AGNES_VIEWER_SECRET`, injected the same way as
 * `AGNES_TOKEN`, see `agnesQuery.ts`). Any inbound `x-agnes-viewer*` header
 * from the browser is stripped by the proxy, so this header is trustworthy
 * only if the signature checks out here — that is the entire job of this
 * module.
 *
 * `node:crypto` only — no JWT library, no new npm dependency. See
 * `references/agnes-query.md` for the wire contract this verifies.
 */

import { createHmac, timingSafeEqual } from "node:crypto";
import type { NextFunction, Request, Response } from "express";

export type Viewer = {
  sub: string;
  email: string;
  name?: string;
  groups: string[];
  via: string;
  exp: number;
};

interface ViewerHeaderClaims {
  alg?: string;
}

interface ViewerPayloadClaims {
  sub?: string;
  email?: string;
  name?: string;
  groups?: string[];
  groups_truncated?: boolean;
  aud?: string;
  iat?: number;
  exp?: number;
  via?: string;
  typ?: string;
}

function base64UrlDecode(segment: string): Buffer {
  return Buffer.from(segment, "base64url");
}

/**
 * Verify and decode the `X-Agnes-Viewer` header. Returns `null` on ANY
 * failure — missing header, malformed token, bad signature, expired,
 * wrong audience — never throws. Callers that need to reject an
 * unauthenticated request use `requireViewer` instead of re-deriving the
 * 401 branch themselves.
 */
export function getViewer(req: Request): Viewer | null {
  try {
    const raw = req.header("x-agnes-viewer");
    if (!raw) return null;

    const parts = raw.split(".");
    if (parts.length !== 3) return null;
    const [h, p, s] = parts;

    const header = JSON.parse(base64UrlDecode(h).toString("utf8")) as ViewerHeaderClaims;
    if (header.alg !== "HS256") return null;

    const secret = process.env.AGNES_VIEWER_SECRET ?? "";
    const expected = createHmac("sha256", secret).update(`${h}.${p}`).digest();
    const actual = base64UrlDecode(s);
    if (actual.length !== expected.length) return null;
    if (!timingSafeEqual(actual, expected)) return null;

    const payload = JSON.parse(base64UrlDecode(p).toString("utf8")) as ViewerPayloadClaims;
    const now = Math.floor(Date.now() / 1000);
    if (typeof payload.exp !== "number" || payload.exp <= now - 30) return null;

    const expectedSlug = process.env.AGNES_APP_SLUG;
    if (expectedSlug && payload.aud !== `data-app:${expectedSlug}`) return null;

    if (typeof payload.sub !== "string" || typeof payload.email !== "string" || typeof payload.via !== "string") {
      return null;
    }

    return {
      sub: payload.sub,
      email: payload.email,
      name: payload.name,
      groups: Array.isArray(payload.groups) ? payload.groups : [],
      via: payload.via,
      exp: payload.exp,
    };
  } catch {
    return null;
  }
}

/**
 * Express middleware form of `getViewer` — attaches `res.locals.viewer` on
 * success, otherwise answers 401 and never calls `next()`.
 */
export function requireViewer(req: Request, res: Response, next: NextFunction): void {
  const viewer = getViewer(req);
  if (!viewer) {
    res.status(401).json({ error: "unauthenticated" });
    return;
  }
  res.locals.viewer = viewer;
  next();
}

/**
 * The short-lived server-signed bearer token (`X-Agnes-Viewer-Token`) the
 * proxy adds only when the app's `data_identity` is `viewer` — forward it
 * as `Authorization: Bearer …` to the Agnes data API (see `agnesQuery.ts`
 * `runQuery`'s `viewerToken` option). `undefined` when the app runs in the
 * default `owner` data-identity mode.
 */
export function viewerTokenFrom(req: Request): string | undefined {
  return req.header("x-agnes-viewer-token") ?? undefined;
}
