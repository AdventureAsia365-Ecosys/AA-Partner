// web/lib/staffSession.ts
// AA-1019 — the AA staff console's own server session (S-02 ops releases, S-04 support sessions). Root ADR 0006b
// decision 6: AA staff sign in to the WORKFORCE pool with the crm-staff client and call /crm/staff/v1 only; the agency
// session (lib/session.ts, partner pool) and this one never stand in for each other.
//
// Separation, by construction:
//   - other cookie NAMES (aa_crm_staff_*), other PATHS (/staff, /staff/auth): the browser never sends a staff cookie
//     to an agency page, the agency BFF or /auth/*;
//   - another KEY: sealed with STAFF_SESSION_SECRET (not WEB_SESSION_SECRET, which the Content studio holds too) and
//     other seal purposes (staff-session / staff-refresh / staff-oauth-tx), so neither secret opens the other's cookie;
//   - another CLIENT: the access token inside is checked to belong to the crm-staff client (lib/oauth.ts
//     accessClaims), and the CRM API accepts it on /crm/staff/v1 only (403 wrong_audience on /crm/v1).
//
// Three cookies, host-only (no Domain), httpOnly, Secure, SameSite=Lax (the OAuth callback is a cross-site top-level
// GET). Path=/ is not used, so the __Host- prefix cannot be; __Secure- still forbids a non-Secure write:
//
//   __Secure-aa_crm_staff_s   Path=/staff        the access token (15 min, crm-staff client), identity (iss, sub,
//                                                client), the token's groups (for the page; the API decides on its own),
//                                                lives as long as the refresh token (12 h)
//   __Secure-aa_crm_staff_r   Path=/staff/auth   the refresh token: refresh and sign-out only
//   __Secure-aa_crm_staff_tx  Path=/staff/auth   one sign-in attempt (10 min)
//
// No token ever reaches page JavaScript: the staff BFF (app/staff/bff) attaches it server side.

import type { WebConfig } from "./config";
import { safeNext } from "./hosts";
import type { OAuthClient } from "./oauth";
import { nowSeconds, open, seal } from "./seal";
import type { CookieReader, CookieSpec, OAuthTx } from "./session";

/** = the crm-staff client's refresh token lifetime (12 h, AA-CIS-Infra identity.tf id_workforce_clients). */
export const STAFF_SESSION_HOURS = 12;
const SESSION_MAX_AGE = STAFF_SESSION_HOURS * 3600;
const TX_MAX_AGE = 600;
export const STAFF_ACCESS_SKEW_S = 60;

/** The staff console's paths (proxy.ts, the route handlers). */
export const STAFF_PREFIX = "/staff";
export const STAFF_AUTH_PREFIX = "/staff/auth";
export const STAFF_BFF_PREFIX = "/staff/bff";
export const STAFF_SIGNED_OUT = "/staff/signed-out";

/** The WORKFORCE groups the console knows (root ADR 0006b decision 6). */
export const STAFF_GROUPS = ["crm-ops", "crm-support"] as const;
export type StaffGroup = (typeof STAFF_GROUPS)[number];

export interface StaffSession {
  v: 1;
  at: string;
  atExp: number;
  iss: string;
  sub: string;
  cid: string;
  groups: StaffGroup[];
  exp: number;
}

export interface StaffRefresh {
  v: 1;
  rt: string;
  sub: string;
  exp: number;
}

export interface StaffCookieNames {
  session: string;
  refresh: string;
  tx: string;
}

export function staffCookieNames(cfg: Pick<WebConfig, "secureCookies">): StaffCookieNames {
  return cfg.secureCookies
    ? { session: "__Secure-aa_crm_staff_s", refresh: "__Secure-aa_crm_staff_r", tx: "__Secure-aa_crm_staff_tx" }
    : { session: "aa_crm_staff_s", refresh: "aa_crm_staff_r", tx: "aa_crm_staff_tx" };
}

/** The crm-staff client as lib/oauth.ts sees it: its own domain, id, secret, callback and sign-out page. */
export function staffClient(cfg: WebConfig): OAuthClient {
  return {
    publicOrigin: cfg.publicOrigin,
    cognitoDomain: cfg.staffCognitoDomain,
    cognitoClientId: cfg.staffCognitoClientId,
    cognitoClientSecret: cfg.staffCognitoClientSecret,
    callbackPath: `${STAFF_AUTH_PREFIX}/callback`,
    logoutPath: STAFF_SIGNED_OUT,
  };
}

/** Only the groups the console knows, in a fixed order (anything else in the token is ignored). */
export function staffGroups(groups: readonly string[]): StaffGroup[] {
  return STAFF_GROUPS.filter((g) => groups.includes(g));
}

function spec(cfg: WebConfig, name: string, value: string, path: string, maxAge: number): CookieSpec {
  return { name, value, httpOnly: true, secure: cfg.secureCookies, sameSite: "lax", path, maxAge };
}

export function readStaffSession(cfg: WebConfig, cookies: CookieReader, now: number = nowSeconds()): StaffSession | null {
  if (!cfg.staffAuthConfigured) return null;
  const s = open<StaffSession>(cfg.staffSessionSecret, "staff-session", cookies.get(staffCookieNames(cfg).session)?.value, now);
  if (!s || s.v !== 1 || typeof s.at !== "string" || typeof s.sub !== "string" || !Array.isArray(s.groups)) return null;
  // A session sealed for another client (a configuration change) is no session.
  return s.cid === cfg.staffCognitoClientId ? s : null;
}

export function staffAccessValid(session: StaffSession | null, now: number = nowSeconds()): session is StaffSession {
  return session !== null && session.atExp - STAFF_ACCESS_SKEW_S > now;
}

export function readStaffRefresh(cfg: WebConfig, cookies: CookieReader, now: number = nowSeconds()): StaffRefresh | null {
  if (!cfg.staffAuthConfigured) return null;
  const r = open<StaffRefresh>(cfg.staffSessionSecret, "staff-refresh", cookies.get(staffCookieNames(cfg).refresh)?.value, now);
  return r && r.v === 1 && typeof r.rt === "string" ? r : null;
}

export function readStaffTx(cfg: WebConfig, cookies: CookieReader, now: number = nowSeconds()): OAuthTx | null {
  if (!cfg.staffAuthConfigured) return null;
  return open<OAuthTx>(cfg.staffSessionSecret, "staff-oauth-tx", cookies.get(staffCookieNames(cfg).tx)?.value, now);
}

export function staffSessionCookie(cfg: WebConfig, session: Omit<StaffSession, "v" | "exp">, now = nowSeconds()): CookieSpec {
  const value = seal(cfg.staffSessionSecret, "staff-session", { v: 1, ...session, exp: now + SESSION_MAX_AGE });
  return spec(cfg, staffCookieNames(cfg).session, value, STAFF_PREFIX, SESSION_MAX_AGE);
}

export function staffRefreshCookie(cfg: WebConfig, rt: string, sub: string, now = nowSeconds()): CookieSpec {
  const value = seal(cfg.staffSessionSecret, "staff-refresh", { v: 1, rt, sub, exp: now + SESSION_MAX_AGE });
  return spec(cfg, staffCookieNames(cfg).refresh, value, STAFF_AUTH_PREFIX, SESSION_MAX_AGE);
}

export function staffTxCookie(cfg: WebConfig, tx: Omit<OAuthTx, "exp">, now = nowSeconds()): CookieSpec {
  const value = seal(cfg.staffSessionSecret, "staff-oauth-tx", { ...tx, exp: now + TX_MAX_AGE });
  return spec(cfg, staffCookieNames(cfg).tx, value, STAFF_AUTH_PREFIX, TX_MAX_AGE);
}

/** Expired copies of every staff cookie (sign-out, refused refresh). Agency cookies are never touched here. */
export function clearedStaffCookies(cfg: WebConfig): CookieSpec[] {
  const n = staffCookieNames(cfg);
  return [
    spec(cfg, n.session, "", STAFF_PREFIX, 0),
    spec(cfg, n.refresh, "", STAFF_AUTH_PREFIX, 0),
    spec(cfg, n.tx, "", STAFF_AUTH_PREFIX, 0),
  ];
}

/** A console page to return to after sign-in: a same-origin /staff path that is not auth or BFF, else /staff. */
export function safeStaffNext(raw: string | null | undefined): string {
  const next = safeNext(raw);
  if (next !== STAFF_PREFIX && !next.startsWith(`${STAFF_PREFIX}/`) && !next.startsWith(`${STAFF_PREFIX}?`)) return STAFF_PREFIX;
  if (next.startsWith(`${STAFF_AUTH_PREFIX}/`) || next === STAFF_AUTH_PREFIX) return STAFF_PREFIX;
  if (next.startsWith(`${STAFF_BFF_PREFIX}/`) || next === STAFF_BFF_PREFIX) return STAFF_PREFIX;
  return next;
}

/** Whether a path belongs to the staff console (so the agency gate leaves it alone). */
export function isStaffPath(pathname: string): boolean {
  return pathname === STAFF_PREFIX || pathname.startsWith(`${STAFF_PREFIX}/`);
}

/** Staff paths reachable without a staff session: sign-in flow and the signed-out page. */
export function isPublicStaffPath(pathname: string): boolean {
  return pathname.startsWith(`${STAFF_AUTH_PREFIX}/`) || pathname === STAFF_SIGNED_OUT;
}
