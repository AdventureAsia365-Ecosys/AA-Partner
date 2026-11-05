// web/lib/studio/studioSession.ts
// AA-979 P2b — the pure helpers of the Content studio's partner-pool sign-in: the CIS token's claims, when to renew it,
// the exchange with CIS POST /auth/tenant-session and the chooser read GET /auth/tenant-memberships, the studio
// cookies. lib/studio/tenantSession.ts wires them to the request.
// AA-1036 (R3 slice 1, root ADR 0019 d6): moved from the separate studio app (portal/lib) into the one agency app. The
// exchange is still a server-side call to CIS (/v1/* only accepts CIS tenant JWTs); the session it reads is this
// app's own (lib/session.ts), so the studio's byte copies of session.ts and seal.ts are gone.

/** The host-only cookies of a studio session (AA-427 names; sign-out clears them). AA-1025 (S264): `cis_partner_ref`
 *  names the partner sign-in the studio token was exchanged for (see partnerRef). One list: lib/studioCookies.ts. */
export { STUDIO_COOKIES } from "../studioCookies";
export const PARTNER_REF_COOKIE = "cis_partner_ref";
/** Re-exchange this long before the studio token expires, so no page or BFF call carries a token that dies mid-flight. */
export const RENEW_SKEW_S = 60;
const EXCHANGE_TIMEOUT_MS = 5000;

export interface StudioClaims {
  exp: number | null;
  amr: string | null;
  sub: string | null;
  /** AA-1036: the Content studio role (csr: owner | editor | viewer), for display only (CIS enforces it). */
  csr: string | null;
}

/** The claims of a CIS tenant JWT WITHOUT checking the signature: only to decide whether to renew it or which kind of
 * session it is. CIS verifies every use. */
export function studioClaims(token: string | undefined | null): StudioClaims {
  const empty = { exp: null, amr: null, sub: null, csr: null };
  if (!token) return empty;
  try {
    const part = token.split(".")[1];
    if (!part) return empty;
    const b64 = part.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(part.length / 4) * 4, "=");
    const data = JSON.parse(atob(b64)) as Record<string, unknown>;
    return {
      exp: typeof data.exp === "number" ? data.exp : null,
      amr: typeof data.amr === "string" ? data.amr : null,
      sub: typeof data.sub === "string" ? data.sub : null,
      csr: typeof data.csr === "string" ? data.csr : null,
    };
  } catch {
    return empty;
  }
}

export function isPartnerToken(token: string | undefined | null): boolean {
  return studioClaims(token).amr === "partner";
}

/** True when the studio token needs a new exchange (missing, unreadable, or expiring within RENEW_SKEW_S). */
export function needsRenewal(token: string | undefined | null, nowS: number = Math.floor(Date.now() / 1000)): boolean {
  const { exp } = studioClaims(token);
  return exp === null || exp - RENEW_SKEW_S <= nowS;
}

export interface StudioSession {
  token: string;
  tenant_id: string;
  tenant_name: string;
  plan_tier: string;
  content_studio_role: string;
  expires_in: number;
}

export type ExchangeResult =
  | { ok: true; session: StudioSession }
  | { ok: false; reason: "choose_tenant" | "no_membership" | "tenant_not_member" | "unauthorized" | "unavailable" };

export type Fetch = typeof fetch;

/** POST CIS /auth/tenant-session with the partner access token (and the tenant the person chose, if any). */
export async function exchange(
  apiUrl: string,
  accessToken: string,
  tenantId: string | null,
  fetchImpl: Fetch = fetch,
): Promise<ExchangeResult> {
  let res: Response;
  try {
    res = await fetchImpl(`${apiUrl}/auth/tenant-session`, {
      method: "POST",
      headers: { Authorization: `Bearer ${accessToken}`, "Content-Type": "application/json" },
      body: JSON.stringify({ tenant_id: tenantId }),
      cache: "no-store",
      signal: AbortSignal.timeout(EXCHANGE_TIMEOUT_MS),
    });
  } catch {
    return { ok: false, reason: "unavailable" };
  }
  if (res.ok) {
    try {
      const data = (await res.json()) as StudioSession;
      if (typeof data.token !== "string" || typeof data.tenant_id !== "string") return { ok: false, reason: "unavailable" };
      return { ok: true, session: data };
    } catch {
      return { ok: false, reason: "unavailable" };
    }
  }
  let detail = "";
  try {
    detail = String(((await res.json()) as { detail?: unknown }).detail ?? "");
  } catch {
    /* no JSON body */
  }
  if (res.status === 409 && detail === "choose_tenant") return { ok: false, reason: "choose_tenant" };
  if (res.status === 403 && (detail === "no_membership" || detail === "tenant_not_member")) {
    return { ok: false, reason: detail };
  }
  if (res.status === 401 || res.status === 403) return { ok: false, reason: "unauthorized" };
  return { ok: false, reason: "unavailable" };
}

export interface Membership {
  tenant_id: string;
  tenant_name: string;
  content_studio_role: string;
  membership_id: string;
}

/** GET CIS /auth/tenant-memberships (the chooser); null when CIS refuses or cannot be reached. */
export async function memberships(apiUrl: string, accessToken: string, fetchImpl: Fetch = fetch): Promise<Membership[] | null> {
  try {
    const res = await fetchImpl(`${apiUrl}/auth/tenant-memberships`, {
      headers: { Authorization: `Bearer ${accessToken}` },
      cache: "no-store",
      signal: AbortSignal.timeout(EXCHANGE_TIMEOUT_MS),
    });
    if (!res.ok) return null;
    const data = (await res.json()) as { memberships?: Membership[] };
    return Array.isArray(data.memberships) ? data.memberships : null;
  } catch {
    return null;
  }
}

export interface CookieSpec {
  name: string;
  value: string;
  maxAge: number;
}

/**
 * AA-1025 (S264): a short reference of the partner sign-in (the CRM web session's `iss` + `sub`): the first 16 hex of
 * SHA-256, never the identity itself. The studio cookies carry it, and a studio token is used only while the CRM web's
 * live session has the same reference. Before this, a person who signed in to the CRM as someone else on the same
 * browser (a join link, a second account) kept the previous person's studio, name and agency for up to 15 minutes.
 */
export async function partnerRef(iss: string, sub: string): Promise<string> {
  const data = new TextEncoder().encode(`${iss}|${sub}`);
  const digest = new Uint8Array(await globalThis.crypto.subtle.digest("SHA-256", data));
  return Array.from(digest.slice(0, 8), (b) => b.toString(16).padStart(2, "0")).join("");
}

/** True when the studio cookies were made for this partner sign-in (`ref` = partnerRef of the live session). */
export function boundTo(cookieRef: string | undefined | null, ref: string): boolean {
  return !!cookieRef && cookieRef === ref;
}

/** The studio cookies of an exchanged session (with the partner reference when given); they live exactly as long
 *  as the token. */
export function studioCookies(s: StudioSession, ref?: string): CookieSpec[] {
  const maxAge = Math.max(0, Math.floor(s.expires_in));
  return [
    { name: "cis_role", value: "tenant", maxAge },
    { name: "cis_tenant_token", value: s.token, maxAge },
    { name: "cis_tenant_id", value: s.tenant_id, maxAge },
    { name: "cis_tenant_name", value: s.tenant_name ?? "", maxAge },
    { name: "cis_tenant_plan", value: s.plan_tier ?? "", maxAge },
    ...(ref ? [{ name: PARTNER_REF_COOKIE, value: ref, maxAge }] : []),
  ];
}

/** Cookie options of every studio cookie (host-only: no Domain attribute, on purpose). `secure` = the app's
 *  `secureCookies` (false only on http://localhost), as for every other cookie of this app. */
export function cookieOptions(maxAge: number, secure: boolean) {
  return { httpOnly: true, secure, sameSite: "lax" as const, path: "/", maxAge };
}

/** A same-origin path to return to (no scheme, no protocol-relative, no backslash); default the dashboard. */
export function safeStudioNext(raw: string | null | undefined): string {
  if (!raw || raw.length > 512 || !raw.startsWith("/portal/") || raw.startsWith("//") || raw.includes("\\")) {
    return "/portal/dashboard";
  }
  return /[\u0000-\u001f]/.test(raw) ? "/portal/dashboard" : raw;
}
