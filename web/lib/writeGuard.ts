// web/lib/writeGuard.ts
// AA-935 (S249 sign-in check) — the CRM web's cross-site write refusal, checked against the origin users see.
//
// The vendored rule (lib/sameOrigin.ts isCrossSiteWrite, byte copy of AA-CIS-App) compares the browser's `Origin`
// with `request.nextUrl.origin`. That is right on Vercel, where the URL is the public one. Here the Next standalone
// server runs behind CloudFront -> ALB with HOSTNAME=0.0.0.0, PORT=3000, and builds `request.nextUrl` from its own
// bind address (`http://0.0.0.0:3000/...`, Next `attachRequestMeta`), never from the partner host. So every browser
// POST (`Origin: https://partner-dev...`) looked cross-site and got 403: sign-out, the language switch, the agency
// pick and every BFF write.
//
// The fix keeps the vendored files untouched (CI "Vendored CIS copies") and hands them the public origin
// (WEB_PUBLIC_ORIGIN) instead; the request's own origin is used only when that is not configured (local tools,
// tests of an unconfigured server). Every write route of the web calls refuseForeignWrite() before it reads a
// cookie or a body (tests/writeGuard.test.ts checks the list).

import type { NextResponse } from "next/server";
import type { WebConfig } from "./config";
import { refuseCrossSiteWrite } from "./crossSiteGuard";
import type { WriteCheckRequest } from "./sameOrigin";

/** The origin a same-origin browser request carries in `Origin`: the public one, not the server's bind address. */
export function expectedOrigin(cfg: Pick<WebConfig, "publicOrigin">, req: WriteCheckRequest): string {
  return cfg.publicOrigin || req.nextUrl.origin;
}

/** A 403 for a cross-site write (Origin / Sec-Fetch-Site of another origin), or null when the request may go on. */
export function refuseForeignWrite(cfg: Pick<WebConfig, "publicOrigin">, req: WriteCheckRequest): NextResponse | null {
  return refuseCrossSiteWrite({
    method: req.method,
    headers: req.headers,
    nextUrl: { origin: expectedOrigin(cfg, req) },
  });
}
