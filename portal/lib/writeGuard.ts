// portal/lib/writeGuard.ts
// AA-979 P2b (critic C2) — the studio's cross-site write refusal, checked against the origin users see.
//
// The vendored rule (lib/sameOrigin.ts isCrossSiteWrite, byte copy of AA-CIS-App) compares the browser's `Origin`
// with `request.nextUrl.origin`. On Vercel that is the public URL. On Fargate the Next standalone server runs behind
// CloudFront -> ALB and builds `nextUrl` from its bind address (`http://0.0.0.0:3000`), so every same-origin POST
// would look cross-site (the CRM web met this in S249, web/lib/writeGuard.ts). The vendored files stay untouched and
// get the public origin (PORTAL_PUBLIC_ORIGIN) instead; the request's own origin only when that is not configured
// (Vercel, local). Every write route of the studio calls refuseForeignWrite(req) before it reads a cookie or a body
// (api/tests/unit/test_portal_guards.py).

import type { NextResponse } from "next/server";
import { config, type WebConfig } from "./config";
import { refuseCrossSiteWrite } from "./crossSiteGuard";
import type { WriteCheckRequest } from "./sameOrigin";

/** The origin a same-origin browser request carries in `Origin`: the public one, not the server's bind address. */
export function expectedOrigin(cfg: Pick<WebConfig, "publicOrigin">, req: WriteCheckRequest): string {
  return cfg.publicOrigin || req.nextUrl.origin;
}

/** A 403 for a cross-site write, or null when the request may go on. */
export function refuseForeignWrite(req: WriteCheckRequest, cfg: Pick<WebConfig, "publicOrigin"> = config()): NextResponse | null {
  return refuseCrossSiteWrite({ method: req.method, headers: req.headers, nextUrl: { origin: expectedOrigin(cfg, req) } });
}

/** An absolute URL for a same-origin path, on the public origin (never the bind address). */
export function publicUrl(path: string, requestUrl: string, cfg: Pick<WebConfig, "publicOrigin"> = config()): URL {
  return new URL(path, cfg.publicOrigin || requestUrl);
}
