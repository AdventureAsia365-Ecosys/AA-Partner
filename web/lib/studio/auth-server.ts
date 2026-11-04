// web/lib/studio/auth-server.ts
// AA-885 — the tenant part of the CIS admin's auth-server (AA-253 / AA-551 / AA-656) for the Content studio: the studio
// token is verified against CIS POST /auth/verify-tenant before it is forwarded anywhere, with a short per-instance
// cache. Route handlers always fail closed (no dev carve-out): the Bearer is never forwarded on an unverified cookie.
// AA-1036 (R3 slice 1, root ADR 0019 d6): moved into the one agency app. CIS is the app's CIS_API_URL (lib/config.ts;
// no host constant in this public repo, DM-6.10); the people-facing API-key session is gone, so only exchanged
// partner tokens (amr=partner) bound to the live sign-in are used.
import { NextRequest, NextResponse } from "next/server";
import { requestConfig, type WebConfig } from "../config";
import { accessValid, readSession } from "../session";
import {
  boundTo,
  cookieOptions,
  exchange,
  isPartnerToken,
  needsRenewal,
  PARTNER_REF_COOKIE,
  partnerRef,
  STUDIO_COOKIES,
  studioCookies,
  type CookieSpec,
} from "./tenantSession";

// Same TTL as the admin app had for tenants (S241: the tenant cache keeps 20 s). Every entry is also capped at the
// token's own `exp`, so an expired JWT is never served from cache.
const TENANT_VERIFY_CACHE_TTL_MS = 20_000;

/** The studio's httpOnly session cookies (AA-427 names, + cis_partner_ref since AA-1025). */
export const TENANT_COOKIES = STUDIO_COOKIES;

export const TENANT_TOKEN_COOKIE = "cis_tenant_token";

/** The JWT's `exp` in epoch ms, or null. Decoded WITHOUT checking the signature — only ever used to SHORTEN a cache
 * entry for a token the backend has just verified. */
function tokenExpiryMs(token: string): number | null {
  try {
    const part = token.split(".")[1];
    if (!part) return null;
    const b64 = part.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(part.length / 4) * 4, "=");
    const exp = (JSON.parse(atob(b64)) as { exp?: unknown }).exp;
    return typeof exp === "number" && Number.isFinite(exp) ? exp * 1000 : null;
  } catch {
    return null;
  }
}

export interface VerifiedTenant {
  tenantId: string;
  name: string;
  planTier: string;
}

interface CachedVerify { result: VerifiedTenant; expiresAt: number; }

// Only successful verifications are cached (AA-551): a failure is never cached.
class TenantVerifyCache {
  private entries = new Map<string, CachedVerify>();

  get(token: string): VerifiedTenant | undefined {
    const hit = this.entries.get(token);
    if (!hit) return undefined;
    if (Date.now() >= hit.expiresAt) {
      this.entries.delete(token);
      return undefined;
    }
    return hit.result;
  }

  set(token: string, result: VerifiedTenant) {
    const now = Date.now();
    const exp = tokenExpiryMs(token);
    const expiresAt = exp === null ? now + TENANT_VERIFY_CACHE_TTL_MS : Math.min(now + TENANT_VERIFY_CACHE_TTL_MS, exp);
    if (expiresAt <= now) return;
    this.entries.set(token, { result, expiresAt });
    if (this.entries.size > 500) {
      for (const [key, entry] of this.entries) {
        if (now > entry.expiresAt) this.entries.delete(key);
      }
    }
  }

  delete(token: string) {
    this.entries.delete(token);
  }
}

/** Shared by the route handlers below and by proxy.ts (AA-573: one cache per instance). */
export const tenantVerifyCache = new TenantVerifyCache();

export type VerifyTenantResult =
  | ({ ok: true } & VerifiedTenant)
  // status: the backend's answer (401 invalid/expired), or 0 when it could not be reached (or CIS is not configured).
  | { ok: false; status: number };

/** POST /auth/verify-tenant for one token, through the shared cache. Never fails open. */
export async function verifyTenantToken(cisApiUrl: string, token: string, fetchImpl: typeof fetch = fetch): Promise<VerifyTenantResult> {
  const cached = tenantVerifyCache.get(token);
  if (cached) return { ok: true, ...cached };
  if (!cisApiUrl) return { ok: false, status: 0 };
  try {
    const res = await fetchImpl(`${cisApiUrl}/auth/verify-tenant`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
      cache: "no-store",
      signal: AbortSignal.timeout(3000),
    });
    if (!res.ok) return { ok: false, status: res.status };
    const data = await res.json();
    const result: VerifiedTenant = { tenantId: data.tenant_id, name: data.name, planTier: data.plan_tier };
    tenantVerifyCache.set(token, result);
    return { ok: true, ...result };
  } catch {
    return { ok: false, status: 0 };
  }
}

/** Header on a BFF 401: the shell reloads the page once, so the page gate can renew the session through
 * /auth/refresh (client fetches never pass the page gate, critic S1). */
export const SESSION_EXPIRED_HEADER = "x-aa-session";

export function unauthorizedStudio(): NextResponse {
  const res = NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  res.headers.set(SESSION_EXPIRED_HEADER, "expired");
  return res;
}

export type RequireStudioResult =
  | { ok: true; tenantId: string; name: string; planTier: string; token: string; renewed: CookieSpec[]; cfg: WebConfig }
  | { ok: false; response: NextResponse };

/**
 * The BFF's tenant check. Exchanged studio tokens live at most 15 minutes, so when the cookie holds no usable token
 * (missing, not a partner token, made for another sign-in, or about to expire) and the partner session is still valid,
 * it is exchanged again here (the caller sets `renewed` on its response). Then the token is verified at CIS.
 * AA-1025 (S264): a token made for ANOTHER partner sign-in than the live session (cookie cis_partner_ref) is never
 * used: it is exchanged again for the live person, without the old agency choice; a failed exchange answers 401.
 */
export async function requireStudio(request: NextRequest, fetchImpl: typeof fetch = fetch): Promise<RequireStudioResult> {
  const cfg = requestConfig(request);
  const session = cfg.authConfigured ? readSession(cfg, request.cookies) : null;
  if (!session) return { ok: false, response: unauthorizedStudio() };
  const ref = await partnerRef(session.iss, session.sub);
  const bound = boundTo(request.cookies.get(PARTNER_REF_COOKIE)?.value, ref);
  let token = request.cookies.get(TENANT_TOKEN_COOKIE)?.value;
  let renewed: CookieSpec[] = [];
  if (!token || !isPartnerToken(token) || !bound || needsRenewal(token)) {
    if (!accessValid(session) || !cfg.cisApiUrl) return { ok: false, response: unauthorizedStudio() };
    const chosen = bound ? (request.cookies.get("cis_tenant_id")?.value ?? null) : null;
    const result = await exchange(cfg.cisApiUrl, session.at, chosen, fetchImpl);
    if (!result.ok) return { ok: false, response: unauthorizedStudio() };
    token = result.session.token;
    renewed = studioCookies(result.session, ref);
  }
  const verified = await verifyTenantToken(cfg.cisApiUrl, token, fetchImpl);
  if (!verified.ok) return { ok: false, response: unauthorizedStudio() };
  return { ok: true, tenantId: verified.tenantId, name: verified.name, planTier: verified.planTier, token, renewed, cfg };
}

/** Set the renewed studio cookies (if any) on a BFF response. */
export function withRenewed<T extends NextResponse>(res: T, renewed: CookieSpec[], cfg: Pick<WebConfig, "secureCookies">): T {
  for (const c of renewed) res.cookies.set(c.name, c.value, cookieOptions(c.maxAge, cfg.secureCookies));
  return res;
}
