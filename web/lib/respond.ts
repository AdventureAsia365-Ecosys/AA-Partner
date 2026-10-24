// web/lib/respond.ts
// AA-935 — small response helpers for the route handlers. Redirects are built on WEB_PUBLIC_ORIGIN, never on the
// request URL: behind CloudFront -> ALB the task sees plain http, so request.url would send the browser to http://.

import { NextResponse } from "next/server";
import type { WebConfig } from "./config";
import type { CookieSpec } from "./session";

export function withCookies<T extends NextResponse>(res: T, specs: CookieSpec[]): T {
  for (const c of specs) {
    res.cookies.set(c.name, c.value, {
      httpOnly: c.httpOnly,
      secure: c.secure,
      sameSite: c.sameSite,
      path: c.path,
      maxAge: c.maxAge,
    });
  }
  res.headers.set("Cache-Control", "no-store");
  return res;
}

/**
 * A redirect to a same-origin path (303 after a POST, 307 otherwise). `fallbackOrigin` (the request's) is used only
 * when WEB_PUBLIC_ORIGIN is not configured, so an unconfigured server can still show its "not configured" page.
 */
export function redirectTo(cfg: WebConfig, path: string, status: 303 | 307 = 307, fallbackOrigin = ""): NextResponse {
  const res = NextResponse.redirect(new URL(path, cfg.publicOrigin || fallbackOrigin), status);
  res.headers.set("Cache-Control", "no-store");
  return res;
}

export function redirectExternal(url: string, status: 303 | 307 = 307): NextResponse {
  const res = NextResponse.redirect(url, status);
  res.headers.set("Cache-Control", "no-store");
  return res;
}

export function json(body: unknown, status = 200): NextResponse {
  const res = NextResponse.json(body, { status });
  res.headers.set("Cache-Control", "no-store");
  return res;
}
