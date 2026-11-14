// web/lib/academySession.ts
// AA-1105 (root ADR 0022 decision 6) — the AA Academy's own server session on the academy host. Staff sign in to the
// WORKFORCE pool with the academy-web client; the CIS learning API (/learn/v1) accepts that client only, and decides
// on every call who is a learner (403 not_a_learner otherwise). The agency session (lib/session.ts) and the staff
// console session (lib/staffSession.ts) never stand in for this one, nor this one for them.
//
// Separation, by construction (the same shape as the staff console, AA-1019):
//   - another HOST: the academy cookies are host-only on academy.<zone> (no Domain), so the partner and client hosts
//     never receive them, and the academy host never receives theirs;
//   - other cookie NAMES (aa_academy_*) and PATHS (/academy, /academy/auth);
//   - another KEY: sealed with ACADEMY_SESSION_SECRET (never the agency or the staff key, lib/config.ts refuses equal
//     keys) and other seal purposes (academy-session / academy-refresh / academy-oauth-tx);
//   - another CLIENT: the access token inside must belong to the academy-web client (lib/oauth.ts accessClaims).
//
// Three cookies, httpOnly, Secure, SameSite=Lax (the OAuth callback is a cross-site top-level GET):
//
//   __Secure-aa_academy_s   Path=/academy        the access token (15 min, academy-web client) and identity (iss, sub,
//                                                client); lives as long as the refresh token (12 h)
//   __Secure-aa_academy_r   Path=/academy/auth   the refresh token: refresh and sign-out only
//   __Secure-aa_academy_tx  Path=/academy/auth   one sign-in attempt (10 min)
//
// No token ever reaches page JavaScript: server components and the academy BFF (app/academy/bff) attach it server side.

import type { WebConfig } from "./config";
import { ACADEMY_PREFIX, safeNext } from "./hosts";
import type { OAuthClient } from "./oauth";
import { nowSeconds, open, seal } from "./seal";
import type { CookieReader, CookieSpec, OAuthTx } from "./session";

/** = the academy-web client's refresh token lifetime (12 h, AA-CIS-Infra identity.tf id_workforce_clients). */
export const ACADEMY_SESSION_HOURS = 12;
const SESSION_MAX_AGE = ACADEMY_SESSION_HOURS * 3600;
const TX_MAX_AGE = 600;
export const ACADEMY_ACCESS_SKEW_S = 60;

export const ACADEMY_AUTH_PREFIX = `${ACADEMY_PREFIX}/auth`;
export const ACADEMY_BFF_PREFIX = `${ACADEMY_PREFIX}/bff`;
export const ACADEMY_CAPTIONS_PREFIX = `${ACADEMY_PREFIX}/captions`;
export const ACADEMY_SIGNED_OUT = `${ACADEMY_PREFIX}/signed-out`;

export interface AcademySession {
  v: 1;
  at: string;
  atExp: number;
  iss: string;
  sub: string;
  cid: string;
  exp: number;
}

export interface AcademyRefresh {
  v: 1;
  rt: string;
  sub: string;
  exp: number;
}

export interface AcademyCookieNames {
  session: string;
  refresh: string;
  tx: string;
}

export function academyCookieNames(cfg: Pick<WebConfig, "secureCookies">): AcademyCookieNames {
  return cfg.secureCookies
    ? { session: "__Secure-aa_academy_s", refresh: "__Secure-aa_academy_r", tx: "__Secure-aa_academy_tx" }
    : { session: "aa_academy_s", refresh: "aa_academy_r", tx: "aa_academy_tx" };
}

/** The academy-web client as lib/oauth.ts sees it: its own domain, id, secret, callback and sign-out page. */
export function academyClient(cfg: WebConfig): OAuthClient {
  return {
    publicOrigin: cfg.publicOrigin,
    cognitoDomain: cfg.academyCognitoDomain,
    cognitoClientId: cfg.academyCognitoClientId,
    cognitoClientSecret: cfg.academyCognitoClientSecret,
    callbackPath: `${ACADEMY_AUTH_PREFIX}/callback`,
    logoutPath: ACADEMY_SIGNED_OUT,
  };
}

function spec(cfg: WebConfig, name: string, value: string, path: string, maxAge: number): CookieSpec {
  return { name, value, httpOnly: true, secure: cfg.secureCookies, sameSite: "lax", path, maxAge };
}

export function readAcademySession(cfg: WebConfig, cookies: CookieReader, now: number = nowSeconds()): AcademySession | null {
  if (!cfg.academyAuthConfigured) return null;
  const s = open<AcademySession>(cfg.academySessionSecret, "academy-session", cookies.get(academyCookieNames(cfg).session)?.value, now);
  if (!s || s.v !== 1 || typeof s.at !== "string" || typeof s.sub !== "string") return null;
  // A session sealed for another client (a configuration change) is no session.
  return s.cid === cfg.academyCognitoClientId ? s : null;
}

export function academyAccessValid(session: AcademySession | null, now: number = nowSeconds()): session is AcademySession {
  return session !== null && session.atExp - ACADEMY_ACCESS_SKEW_S > now;
}

export function readAcademyRefresh(cfg: WebConfig, cookies: CookieReader, now: number = nowSeconds()): AcademyRefresh | null {
  if (!cfg.academyAuthConfigured) return null;
  const r = open<AcademyRefresh>(cfg.academySessionSecret, "academy-refresh", cookies.get(academyCookieNames(cfg).refresh)?.value, now);
  return r && r.v === 1 && typeof r.rt === "string" ? r : null;
}

export function readAcademyTx(cfg: WebConfig, cookies: CookieReader, now: number = nowSeconds()): OAuthTx | null {
  if (!cfg.academyAuthConfigured) return null;
  return open<OAuthTx>(cfg.academySessionSecret, "academy-oauth-tx", cookies.get(academyCookieNames(cfg).tx)?.value, now);
}

export function academySessionCookie(cfg: WebConfig, session: Omit<AcademySession, "v" | "exp">, now = nowSeconds()): CookieSpec {
  const value = seal(cfg.academySessionSecret, "academy-session", { v: 1, ...session, exp: now + SESSION_MAX_AGE });
  return spec(cfg, academyCookieNames(cfg).session, value, ACADEMY_PREFIX, SESSION_MAX_AGE);
}

export function academyRefreshCookie(cfg: WebConfig, rt: string, sub: string, now = nowSeconds()): CookieSpec {
  const value = seal(cfg.academySessionSecret, "academy-refresh", { v: 1, rt, sub, exp: now + SESSION_MAX_AGE });
  return spec(cfg, academyCookieNames(cfg).refresh, value, ACADEMY_AUTH_PREFIX, SESSION_MAX_AGE);
}

export function academyTxCookie(cfg: WebConfig, tx: Omit<OAuthTx, "exp">, now = nowSeconds()): CookieSpec {
  const value = seal(cfg.academySessionSecret, "academy-oauth-tx", { ...tx, exp: now + TX_MAX_AGE });
  return spec(cfg, academyCookieNames(cfg).tx, value, ACADEMY_AUTH_PREFIX, TX_MAX_AGE);
}

/** Expired copies of every academy cookie (sign-out, refused refresh). Other sessions' cookies are never touched. */
export function clearedAcademyCookies(cfg: WebConfig): CookieSpec[] {
  const n = academyCookieNames(cfg);
  return [
    spec(cfg, n.session, "", ACADEMY_PREFIX, 0),
    spec(cfg, n.refresh, "", ACADEMY_AUTH_PREFIX, 0),
    spec(cfg, n.tx, "", ACADEMY_AUTH_PREFIX, 0),
  ];
}

function under(next: string, prefix: string): boolean {
  return next === prefix || next.startsWith(`${prefix}/`) || next.startsWith(`${prefix}?`);
}

/** An academy page to return to after sign-in: a same-origin /academy path that is not auth, BFF or captions. */
export function safeAcademyNext(raw: string | null | undefined): string {
  const next = safeNext(raw);
  if (!under(next, ACADEMY_PREFIX)) return ACADEMY_PREFIX;
  if (under(next, ACADEMY_AUTH_PREFIX) || under(next, ACADEMY_BFF_PREFIX) || under(next, ACADEMY_CAPTIONS_PREFIX)) return ACADEMY_PREFIX;
  return next;
}

/** Academy paths reachable without an academy session: the sign-in flow, the signed-out page, the verify page. */
export function isPublicAcademyPath(pathname: string): boolean {
  return pathname.startsWith(`${ACADEMY_AUTH_PREFIX}/`) || pathname === ACADEMY_SIGNED_OUT || pathname.startsWith("/verify/");
}

/** Academy route handlers that answer 401 JSON themselves (never a sign-in redirect for a fetch or a <track>). */
export function isAcademyHandlerPath(pathname: string): boolean {
  return under(pathname, ACADEMY_BFF_PREFIX) || under(pathname, ACADEMY_CAPTIONS_PREFIX);
}
