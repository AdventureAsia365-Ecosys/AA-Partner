// web/proxy.ts
// AA-935 — the CRM web's request gate (Next 16 Proxy, Node.js runtime): host routing first, then the session gate.
//
//   /healthz                 any Host (the ALB target-group health check sends the task IP as Host)
//   unknown Host             404 (the ALB host rule already sends only the partner host here)
//   client host              /c/* only (+ static assets); everything else 404 (lib/hosts.ts)
//   partner host, public     /auth/*, /prefs/*, /health, /signed-out, static: through
//   partner host, /bff/*     through: the route handler answers 401 JSON itself (no redirect for fetch calls)
//   partner host, any other  needs a session whose access token is still valid; otherwise -> /auth/refresh?next=...
//                            (which renews with the refresh token or falls back to sign-in)
//
// Headers the app reads (x-aa-path, x-aa-host-kind) are always set here after any client-sent copy is dropped, so a
// browser cannot forge them.

import { NextResponse, type NextRequest } from "next/server";
import { config as webConfig } from "./lib/config";
import { classifyHost, clientPathAllowed, isPublicPartnerPath, type HostKind } from "./lib/hosts";
import { accessValid, readSession } from "./lib/session";

const INTERNAL_HEADERS = ["x-aa-path", "x-aa-host-kind"];

/**
 * The CSP of every page (the portal's AA-885 policy): no framing, no <base> hijack, no plugins, forms only to this
 * origin and the managed-login domain (sign-out answers with a redirect to its logout page, and browsers apply
 * form-action to that redirect). script-src / style-src stay open for the kit's inline styles and Next's scripts.
 */
export function contentSecurityPolicy(cognitoDomain: string): string {
  const forms = ["'self'", cognitoDomain].filter(Boolean).join(" ");
  return `frame-ancestors 'none'; base-uri 'self'; form-action ${forms}; object-src 'none'`;
}

function secure<T extends NextResponse>(res: T, cognitoDomain: string): T {
  res.headers.set("Content-Security-Policy", contentSecurityPolicy(cognitoDomain));
  return res;
}

function notFound(): NextResponse {
  return new NextResponse("Not Found", { status: 404, headers: { "Content-Type": "text/plain", "Cache-Control": "no-store" } });
}

function pass(request: NextRequest, kind: HostKind, cognitoDomain = ""): NextResponse {
  const h = new Headers(request.headers);
  for (const name of INTERNAL_HEADERS) h.delete(name);
  h.set("x-aa-path", `${request.nextUrl.pathname}${request.nextUrl.search}`);
  h.set("x-aa-host-kind", kind);
  return secure(NextResponse.next({ request: { headers: h } }), cognitoDomain);
}

export function proxy(request: NextRequest): NextResponse {
  const { pathname, search } = request.nextUrl;
  if (pathname === "/healthz") return pass(request, "unknown");

  const cfg = webConfig();
  const kind = classifyHost(request.headers.get("host"), cfg);
  if (kind === "unknown") return notFound();
  if (kind === "client") return clientPathAllowed(pathname) ? pass(request, kind, cfg.cognitoDomain) : notFound();

  // Partner host. Client pages live only on the client host (local development serves both on localhost).
  const localDev = cfg.environment === "local" || cfg.environment === "test";
  if (pathname.startsWith("/c/") && !localDev) return notFound();
  if (isPublicPartnerPath(pathname) || pathname.startsWith("/prefs/") || pathname.startsWith("/bff/")) {
    return pass(request, kind, cfg.cognitoDomain);
  }
  if (cfg.authConfigured && accessValid(readSession(cfg, request.cookies))) return pass(request, kind, cfg.cognitoDomain);

  const target = new URL("/auth/refresh", cfg.publicOrigin || request.nextUrl.origin);
  target.searchParams.set("next", `${pathname}${search}`);
  const res = NextResponse.redirect(target, 307);
  res.headers.set("Cache-Control", "no-store");
  return res;
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
