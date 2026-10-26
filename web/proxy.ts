// web/proxy.ts
// AA-935 — the CRM web's request gate (Next 16 Proxy, Node.js runtime): host routing first, then the session gate.
//
//   /healthz                 any Host (the ALB target-group health check sends the task IP as Host)
//   unknown Host             404 (the ALB host rule already sends only the partner host here)
//   client host              /c/* only (+ static assets); everything else 404 (lib/hosts.ts). The inquiry form page
//                            /c/f/<key> (AA-936) may be framed by the form's own allowed origins: its CSP
//                            frame-ancestors comes from the form definition (lib/intakeForms.ts, 60 s memo)
//   partner host, public     /auth/*, /prefs/*, /health, /signed-out, static: through
//   partner host, /bff/*     through: the route handler answers 401 JSON itself (no redirect for fetch calls)
//   partner host, any other  needs a session whose access token is still valid; otherwise -> /auth/refresh?next=...
//                            (which renews with the refresh token or falls back to sign-in)
//
// Headers the app reads (x-aa-path, x-aa-host-kind) are always set here after any client-sent copy is dropped, so a
// browser cannot forge them.

import { NextResponse, type NextRequest } from "next/server";
import { config as webConfig, forHost } from "./lib/config";
import { classifyHost, clientPathAllowed, isPublicPartnerPath, type HostKind } from "./lib/hosts";
import { formKey, frameAncestors, loadForm, viewerKey } from "./lib/intakeForms";
import { accessValid, readSession } from "./lib/session";

const INTERNAL_HEADERS = ["x-aa-path", "x-aa-host-kind"];

/**
 * The CSP of every page (the portal's AA-885 policy): no framing, no <base> hijack, no plugins, forms only to this
 * origin and the managed-login domain (sign-out answers with a redirect to its logout page, and browsers apply
 * form-action to that redirect). script-src / style-src stay open for the kit's inline styles and Next's scripts.
 */
export function contentSecurityPolicy(cognitoDomain: string, ancestors = "'none'"): string {
  const forms = ["'self'", cognitoDomain].filter(Boolean).join(" ");
  return `frame-ancestors ${ancestors}; base-uri 'self'; form-action ${forms}; object-src 'none'`;
}

function secure<T extends NextResponse>(res: T, cognitoDomain: string, ancestors?: string): T {
  res.headers.set("Content-Security-Policy", contentSecurityPolicy(cognitoDomain, ancestors));
  return res;
}

function notFound(): NextResponse {
  return new NextResponse("Not Found", { status: 404, headers: { "Content-Type": "text/plain", "Cache-Control": "no-store" } });
}

function pass(request: NextRequest, kind: HostKind, cognitoDomain = "", ancestors?: string): NextResponse {
  const h = new Headers(request.headers);
  for (const name of INTERNAL_HEADERS) h.delete(name);
  h.set("x-aa-path", `${request.nextUrl.pathname}${request.nextUrl.search}`);
  h.set("x-aa-host-kind", kind);
  return secure(NextResponse.next({ request: { headers: h } }), cognitoDomain, ancestors);
}


/**
 * The whole gate, synchronous. A form page (/c/f/<key>, AA-936) gets frame-ancestors 'self' here; {@link proxy}
 * widens it to the form's allowed origins after reading the form.
 */
export function gate(request: NextRequest): NextResponse {
  const { pathname, search } = request.nextUrl;
  if (pathname === "/healthz") return pass(request, "unknown");

  // AA-979 P2b: the public origin of THIS host (partner-dev. or portal.), so the refresh redirect stays on it.
  const cfg = forHost(webConfig(), request.headers.get("host"));
  const kind = classifyHost(request.headers.get("host"), cfg);
  if (kind === "unknown") return notFound();
  const ancestors = formKey(pathname) ? "'self'" : undefined;
  if (kind === "client") return clientPathAllowed(pathname) ? pass(request, kind, cfg.cognitoDomain, ancestors) : notFound();

  // Partner host. Client pages live only on the client host (local development serves both on localhost).
  const localDev = cfg.environment === "local" || cfg.environment === "test";
  if (pathname.startsWith("/c/") && !localDev) return notFound();
  if (ancestors) return pass(request, kind, cfg.cognitoDomain, ancestors);
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

/** Next's entry: {@link gate}, then for a served form page the form's own frame-ancestors (unknown form: 'self'). */
export async function proxy(request: NextRequest): Promise<NextResponse> {
  const res = gate(request);
  const key = formKey(request.nextUrl.pathname);
  if (!key || res.headers.get("x-middleware-next") !== "1") return res;
  const cfg = webConfig();
  const viewer = viewerKey(request.headers.get("cloudfront-viewer-address"));
  const found = cfg.crmApiOrigin ? await loadForm(cfg.crmApiOrigin, key, { viewer }) : null;
  res.headers.set("Content-Security-Policy", contentSecurityPolicy(cfg.cognitoDomain, frameAncestors(found && found.ok ? found.form : null)));
  return res;
}

// AA-945: /bff/mail/imports (the mail history upload, up to 25 MiB, and its status) is left out. When the proxy runs,
// Next clones the request body for it and silently cuts it at 10 MiB (`proxyClientMaxBodySize`), so a larger file would
// reach the API short. The BFF route repeats the host check for these paths (lib/bff.ts UNPROXIED_BFF_PREFIX); it
// answers 401 itself without a session, as for every /bff/* path. Next reads this literal statically: keep it in step
// with UNPROXIED_BFF_PREFIX (tests/mailImport.test.ts checks it).
export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico|bff/mail/imports).*)"],
};
