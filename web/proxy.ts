// web/proxy.ts
// AA-935 — the CRM web's request gate (Next 16 Proxy, Node.js runtime): host routing first, then the session gate.
//
//   /healthz                 any Host (the ALB target-group health check sends the task IP as Host)
//   unknown Host             404 (the ALB host rule already sends only the partner host here)
//   client host              /c/* and /trip/* only (+ static assets); everything else 404 (lib/hosts.ts). The inquiry form page
//                            /c/f/<key> (AA-936) may be framed by the form's own allowed origins: its CSP
//                            frame-ancestors comes from the form definition (lib/intakeForms.ts, 60 s memo)
//   partner host, public     /auth/*, /prefs/*, /health, /signed-out, static: through
//   partner host, /bff/*     through: the route handler answers 401 JSON itself (no redirect for fetch calls)
//   partner host, any other  needs a session whose access token is still valid; otherwise -> /auth/refresh?next=...
//                            (which renews with the refresh token or falls back to sign-in)
//   partner host, /staff*    AA-1019, the AA staff console, decided BEFORE the agency rule and only by the staff
//                            session (lib/staffSession.ts): /staff/auth/* and /staff/signed-out public, /staff/bff/*
//                            through (the handler answers 401 JSON), any other /staff page needs a valid staff access
//                            token, otherwise -> /staff/auth/refresh?next=... The agency session never opens a staff
//                            page and the staff session never opens an agency page.
//   partner host, studio     AA-1036 (R3 slice 1, root ADR 0019 d6): the Content studio is part of this app. Order
//                            host -> /staff -> /c -> /portal -> CRM:
//                              /tenant-login*   the retired API-key sign-in: 308 to /auth/refresh?next=/portal/dashboard
//                              /api/*           the studio BFF: through (the handlers answer 401 JSON themselves)
//                              /legal/partner-terms, /legal/privacy  public (the studio's draft legal pages)
//                              /portal*         needs the session (as any page); then {@link proxy} makes sure a CIS
//                                               studio token exists for it and is fresh (POST /auth/tenant-session):
//                                               several studios -> /portal-access/select, none -> /portal-access/none,
//                                               CIS refused the token -> /auth/refresh, CIS unreachable -> none (fail
//                                               closed). Studio cookies made for another sign-in (cis_partner_ref) are
//                                               never used (AA-1025)
//                              "/"              with WEB_CONTENT_HOME on: 307 to /portal/dashboard (a tenant lands on
//                                               Content, root ADR 0019 d7); the CRM home is /home
//
// Headers the app reads (x-aa-path, x-aa-host-kind) are always set here after any client-sent copy is dropped, so a
// browser cannot forge them.

import { NextResponse, type NextRequest } from "next/server";
import { config as webConfig, forHost, type WebConfig } from "./lib/config";
import {
  classifyHost,
  clientOnlyPath,
  clientPathAllowed,
  isApiKeySignIn,
  isPublicPartnerPath,
  isStudioBff,
  isStudioPage,
  STUDIO_HOME_PATH,
  type HostKind,
} from "./lib/hosts";
import { formKey, frameAncestors, loadForm, viewerKey } from "./lib/intakeForms";
import { accessValid, readSession } from "./lib/session";
import { studioCookiesCleared } from "./lib/studioCookies";
import { TENANT_TOKEN_COOKIE, verifyTenantToken } from "./lib/studio/auth-server";
import {
  boundTo,
  cookieOptions,
  exchange,
  isPartnerToken,
  needsRenewal,
  PARTNER_REF_COOKIE,
  partnerRef,
  studioCookies,
  type CookieSpec as StudioCookie,
} from "./lib/studio/tenantSession";
import { isPublicStaffPath, isStaffPath, readStaffSession, STAFF_AUTH_PREFIX, STAFF_BFF_PREFIX, staffAccessValid } from "./lib/staffSession";

const INTERNAL_HEADERS = ["x-aa-path", "x-aa-host-kind"];

/**
 * The CSP of every page (the portal's AA-885 policy): no framing, no <base> hijack, no plugins, forms only to this
 * origin and the managed-login domain (sign-out answers with a redirect to its logout page, and browsers apply
 * form-action to that redirect). script-src / style-src stay open for the kit's inline styles and Next's scripts.
 */
export function contentSecurityPolicy(cognitoDomain: string, ancestors = "'none'", staffCognitoDomain = ""): string {
  // AA-1019: the staff console's sign-out redirects to the WORKFORCE managed login (its own domain).
  const forms = ["'self'", cognitoDomain, staffCognitoDomain].filter(Boolean).join(" ");
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
  return secure(NextResponse.next({ request: { headers: forwardHeaders(request, kind) } }), cognitoDomain, ancestors);
}

function forwardHeaders(request: NextRequest, kind: HostKind): Headers {
  const h = new Headers(request.headers);
  for (const name of INTERNAL_HEADERS) h.delete(name);
  h.set("x-aa-path", `${request.nextUrl.pathname}${request.nextUrl.search}`);
  h.set("x-aa-host-kind", kind);
  return h;
}

function redirectOn(cfg: WebConfig, request: NextRequest, path: string, status: 307 | 308): NextResponse {
  const res = NextResponse.redirect(new URL(path, cfg.publicOrigin || request.nextUrl.origin), status);
  res.headers.set("Cache-Control", "no-store");
  return secure(res, cfg.cognitoDomain);
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

  // Partner host. Client pages (/c/*, the trip page /trip/*) live only on the client host (local development serves
  // both on localhost).
  const localDev = cfg.environment === "local" || cfg.environment === "test";
  if (clientOnlyPath(pathname) && !localDev) return notFound();
  if (ancestors) return pass(request, kind, cfg.cognitoDomain, ancestors);
  if (isStaffPath(pathname)) return staffGate(request, cfg);
  // AA-1036: the Content studio's routes before the agency rule (see the header).
  if (isApiKeySignIn(pathname)) return redirectOn(cfg, request, `/auth/refresh?next=${encodeURIComponent(STUDIO_HOME_PATH)}`, 308);
  if (pathname === "/" && cfg.contentHome) return redirectOn(cfg, request, STUDIO_HOME_PATH, 307);
  if (isStudioBff(pathname)) return pass(request, kind, cfg.cognitoDomain);
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

/** AA-1019: the staff console's gate (partner host only; see the header). */
function staffGate(request: NextRequest, cfg: ReturnType<typeof forHost>): NextResponse {
  const { pathname, search } = request.nextUrl;
  const passStaff = () => {
    const res = pass(request, "partner", cfg.cognitoDomain);
    res.headers.set("Content-Security-Policy", contentSecurityPolicy(cfg.cognitoDomain, undefined, cfg.staffCognitoDomain));
    return res;
  };
  if (isPublicStaffPath(pathname) || pathname === STAFF_BFF_PREFIX || pathname.startsWith(`${STAFF_BFF_PREFIX}/`)) {
    return passStaff();
  }
  if (cfg.staffAuthConfigured && staffAccessValid(readStaffSession(cfg, request.cookies))) return passStaff();
  if (!cfg.staffAuthConfigured) {
    const off = NextResponse.redirect(new URL("/staff/signed-out?reason=unavailable", cfg.publicOrigin || request.nextUrl.origin), 307);
    off.headers.set("Cache-Control", "no-store");
    return off;
  }
  const target = new URL(`${STAFF_AUTH_PREFIX}/refresh`, cfg.publicOrigin || request.nextUrl.origin);
  target.searchParams.set("next", `${pathname}${search}`);
  const res = NextResponse.redirect(target, 307);
  res.headers.set("Cache-Control", "no-store");
  return res;
}

// ── AA-1036 (R3 slice 1): the Content studio's page step, after the gate let a signed-in /portal* page through ───────

/** Through, with renewed studio cookies on the response AND on the request the page renders with. */
function passStudio(request: NextRequest, cfg: WebConfig, renewed: StudioCookie[]): NextResponse {
  const h = forwardHeaders(request, "partner");
  if (renewed.length > 0) {
    const jar = new Map(request.cookies.getAll().map((c) => [c.name, c.value]));
    for (const c of renewed) jar.set(c.name, c.value);
    h.set("cookie", [...jar].map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("; "));
  }
  const res = NextResponse.next({ request: { headers: h } });
  for (const c of renewed) res.cookies.set(c.name, c.value, cookieOptions(c.maxAge, cfg.secureCookies));
  if (renewed.length > 0) res.headers.set("Cache-Control", "no-store");
  return secure(res, cfg.cognitoDomain);
}

/** A studio access page (choose / none) with the asked path as `next`; `clear` drops studio cookies of another sign-in. */
function toStudioAccess(request: NextRequest, cfg: WebConfig, path: string, extra: Record<string, string> = {}, clear = false): NextResponse {
  const { pathname, search } = request.nextUrl;
  const target = new URL(path, cfg.publicOrigin || request.nextUrl.origin);
  target.searchParams.set("next", `${pathname}${search}`);
  for (const [k, v] of Object.entries(extra)) target.searchParams.set(k, v);
  const res = NextResponse.redirect(target, 307);
  res.headers.set("Cache-Control", "no-store");
  if (clear) for (const c of studioCookiesCleared(cfg)) res.cookies.set(c.name, "", { ...c, maxAge: 0 });
  return secure(res, cfg.cognitoDomain);
}

/**
 * The studio page step (see the header): a fresh studio token bound to this sign-in and verified by CIS -> through;
 * otherwise exchange the partner session for one. `fetchImpl` is for tests.
 */
export async function studioGate(request: NextRequest, fetchImpl: typeof fetch = fetch): Promise<NextResponse> {
  const cfg = forHost(webConfig(), request.headers.get("host"));
  const session = cfg.authConfigured ? readSession(cfg, request.cookies) : null;
  const refresh = () => {
    const { pathname, search } = request.nextUrl;
    return redirectOn(cfg, request, `/auth/refresh?next=${encodeURIComponent(`${pathname}${search}`)}`, 307);
  };
  if (!accessValid(session)) return refresh();
  if (!cfg.cisApiUrl) return toStudioAccess(request, cfg, "/portal-access/none", { reason: "unavailable" });
  const ref = await partnerRef(session.iss, session.sub);
  // AA-1025 (S264): studio cookies made for another partner sign-in (or before the reference existed) are stale.
  const bound = boundTo(request.cookies.get(PARTNER_REF_COOKIE)?.value, ref);
  const token = request.cookies.get(TENANT_TOKEN_COOKIE)?.value;
  if (bound && token && isPartnerToken(token) && !needsRenewal(token)) {
    const verified = await verifyTenantToken(cfg.cisApiUrl, token, fetchImpl);
    if (verified.ok) return passStudio(request, cfg, []);
  }
  // The agency choice of a stale session belongs to the previous person: never send it.
  const chosen = bound ? (request.cookies.get("cis_tenant_id")?.value ?? null) : null;
  let result = await exchange(cfg.cisApiUrl, session.at, chosen, fetchImpl);
  if (!result.ok && result.reason === "tenant_not_member" && chosen) result = await exchange(cfg.cisApiUrl, session.at, null, fetchImpl);
  if (result.ok) return passStudio(request, cfg, studioCookies(result.session, ref));
  const stale = !bound && Boolean(token);
  if (result.reason === "choose_tenant" || result.reason === "tenant_not_member") {
    return toStudioAccess(request, cfg, "/portal-access/select", {}, stale);
  }
  if (result.reason === "no_membership") return toStudioAccess(request, cfg, "/portal-access/none", {}, stale);
  if (result.reason === "unauthorized") return refresh(); // the access token was refused: renew it
  return toStudioAccess(request, cfg, "/portal-access/none", { reason: "unavailable" }); // CIS unreachable: fail closed
}

/**
 * Next's entry: {@link gate}; then for a served studio page the studio step ({@link studioGate}), and for a served form
 * page the form's own frame-ancestors (unknown form: 'self').
 */
export async function proxy(request: NextRequest): Promise<NextResponse> {
  const res = gate(request);
  if (res.headers.get("x-middleware-next") === "1" && isStudioPage(request.nextUrl.pathname)) return studioGate(request);
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
