// portal/proxy.ts
// AA-885 — the tenant portal's page gate (Next 16 Proxy, Node.js runtime; no `runtime` export).
// AA-979 P2b — on the agency host portal. (Fargate behind CloudFront -> ALB) the studio signs people in with the
// partner pool together with the CRM web; on Vercel (no WEB_SESSION_SECRET, no PORTAL_PUBLIC_ORIGIN) it behaves
// exactly as before: API-key sessions only.
//
//   Host guard        a Host outside PORTAL_HOSTS -> 404 (when PORTAL_HOSTS is set; the ALB sends only portal.)
//   /tenant-login     public while PORTAL_API_KEY_SIGNIN is on; off -> the CRM web's sign-in (clean-up)
//   /api/*, legal     through: the route handlers answer for themselves (BFF 401 JSON, never a redirect)
//   /portal/*         order (critic C3: an API-key session that existed at the DNS flip must survive it):
//                     1. a cis_tenant_token WITHOUT amr=partner (an API-key session) that CIS verifies -> through,
//                        while PORTAL_API_KEY_SIGNIN is on;
//                     2. the CRM web's partner session with a valid access token -> make sure a studio token for it
//                        exists and is fresh (CIS POST /auth/tenant-session), then through; several agencies ->
//                        /portal-access/select; none -> /portal-access/none (outside the /portal shell);
//                     3. otherwise -> the CRM web's /auth/refresh?next=<path> (renews or signs in), built on
//                        PORTAL_PUBLIC_ORIGIN (never the standalone server's bind address, critic C2); on Vercel ->
//                        /tenant-login as before.
//   Backend unreachable -> fail CLOSED outside development, as before.
import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { API_URL, TENANT_COOKIES, TENANT_TOKEN_COOKIE, verifyTenantToken } from "./lib/auth-server";
import { config as portalConfig, hostAllowed } from "./lib/config";
import {
  cookieOptions,
  exchange,
  isPartnerToken,
  needsRenewal,
  partnerSession,
  studioCookies,
  type CookieSpec,
} from "./lib/tenantSession";
import { publicUrl } from "./lib/writeGuard";

const LOGIN_PATH = "/tenant-login";
const SELECT_PATH = "/portal-access/select";
const NO_ACCESS_PATH = "/portal-access/none";

function noStore<T extends NextResponse>(res: T): T {
  res.headers.set("Cache-Control", "no-store");
  return res;
}

function toLogin(request: NextRequest, clearCookies: boolean): NextResponse {
  const response = NextResponse.redirect(publicUrl(LOGIN_PATH, request.url));
  if (clearCookies) {
    for (const name of TENANT_COOKIES) response.cookies.delete(name);
  }
  return noStore(response);
}

function toPartnerSignIn(request: NextRequest): NextResponse {
  const cfg = portalConfig();
  const { pathname, search } = request.nextUrl;
  const target = publicUrl(cfg.signInPath, request.url);
  target.searchParams.set("next", `${pathname}${search}`);
  return noStore(NextResponse.redirect(target, 307));
}

function toStudioPage(request: NextRequest, path: string, extra: Record<string, string> = {}): NextResponse {
  const { pathname, search } = request.nextUrl;
  const target = publicUrl(path, request.url);
  target.searchParams.set("next", `${pathname}${search}`);
  for (const [k, v] of Object.entries(extra)) target.searchParams.set(k, v);
  return noStore(NextResponse.redirect(target, 307));
}

/** Through, with the renewed studio cookies on the response AND on the request the page renders with. */
function passWith(request: NextRequest, renewed: CookieSpec[]): NextResponse {
  if (renewed.length === 0) return NextResponse.next();
  const jar = new Map(request.cookies.getAll().map((c) => [c.name, c.value]));
  for (const c of renewed) jar.set(c.name, c.value);
  const headers = new Headers(request.headers);
  headers.set("cookie", [...jar].map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("; "));
  const res = NextResponse.next({ request: { headers } });
  for (const c of renewed) res.cookies.set(c.name, c.value, cookieOptions(c.maxAge));
  return noStore(res);
}

function notFound(): NextResponse {
  return new NextResponse("Not Found", { status: 404, headers: { "Content-Type": "text/plain", "Cache-Control": "no-store" } });
}

export async function proxy(request: NextRequest) {
  const cfg = portalConfig();
  if (!hostAllowed(cfg, request.headers.get("host"))) return notFound();
  const { pathname } = request.nextUrl;

  if (pathname === LOGIN_PATH || pathname.startsWith(LOGIN_PATH + "/")) {
    if (cfg.apiKeySignIn) return NextResponse.next();
    return cfg.partnerSignIn ? toPartnerSignIn(request) : notFound();
  }
  if (!pathname.startsWith("/portal")) return NextResponse.next(); // BFF routes and legal pages answer themselves

  const token = request.cookies.get(TENANT_TOKEN_COOKIE)?.value;
  const partner = partnerSession(cfg, request.cookies);
  const studioPage = pathname === SELECT_PATH || pathname === NO_ACCESS_PATH;

  // 1. An API-key session (no amr=partner) keeps working through the window.
  if (token && !isPartnerToken(token) && cfg.apiKeySignIn && !studioPage) {
    const verified = await verifyTenantToken(token);
    if (verified.ok) return NextResponse.next();
    if (verified.status === 0 || verified.status >= 500) {
      if (process.env.NODE_ENV === "development") return NextResponse.next();
      return toLogin(request, false); // unreachable: fail closed, keep the cookies
    }
    if (!partner) {
      // Refused, and no partner session to fall back to: on the agency host the sign-in is the CRM web's
      // /auth/refresh (step 3), on Vercel the API-key page; the refused cookies are cleared either way.
      if (!cfg.partnerSignIn) return toLogin(request, true);
      const res = toPartnerSignIn(request);
      for (const name of TENANT_COOKIES) res.cookies.delete(name);
      return res;
    }
  }

  // 2. The partner session of the CRM web (same host).
  if (partner) {
    if (studioPage) return NextResponse.next();
    if (token && isPartnerToken(token) && !needsRenewal(token)) {
      const verified = await verifyTenantToken(token);
      if (verified.ok) return NextResponse.next();
    }
    const chosen = request.cookies.get("cis_tenant_id")?.value ?? null;
    let result = await exchange(API_URL, partner.at, chosen);
    // A cis_tenant_id cookie left by another session (an API-key sign-in of another agency): ask again without it.
    if (!result.ok && result.reason === "tenant_not_member" && chosen) result = await exchange(API_URL, partner.at, null);
    if (result.ok) return passWith(request, studioCookies(result.session));
    if (result.reason === "choose_tenant" || result.reason === "tenant_not_member") return toStudioPage(request, SELECT_PATH);
    if (result.reason === "no_membership") return toStudioPage(request, NO_ACCESS_PATH);
    if (result.reason === "unauthorized") return toPartnerSignIn(request); // the access token was refused: renew it
    return toStudioPage(request, NO_ACCESS_PATH, { reason: "unavailable" }); // CIS unreachable: fail closed
  }

  // 3. No usable session.
  if (cfg.partnerSignIn) return toPartnerSignIn(request);
  return toLogin(request, Boolean(token));
}

export const config = {
  // Every path except the Next assets (both prefixes), the health check and the favicon; the Host guard covers the
  // BFF and the legal pages too.
  matcher: ["/((?!_next/|portal-assets/|healthz|favicon.ico).*)"],
};
