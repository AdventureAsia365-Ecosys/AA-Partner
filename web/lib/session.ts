// web/lib/session.ts
// AA-935 — the server-side session of the CRM web (card CRM-07 "server session"). The browser holds only sealed,
// httpOnly cookies (lib/seal.ts); no token ever reaches page JavaScript, and the BFF (app/bff) attaches the access
// token server side.
//
// Three cookies, all host-only (no Domain attribute: every *.lumiguides.it.com host is the same site because it.com
// is a public suffix, so a cookie for the parent domain could be set by another app; host-only + the __Host- prefix
// stops that, AA-911 lesson):
//
//   __Host-aa_crm_s     Path=/      the access token, its expiry, the token identity (iss, sub, client), the agency
//                                   picked (tid). Lives as long as the refresh token (7 days); the access token inside
//                                   expires after 30 minutes and is renewed through /auth/refresh.
//   __Secure-aa_crm_r   Path=/auth  the refresh token: sent only to /auth/* (refresh, sign-out), never to pages or BFF.
//   __Secure-aa_crm_tx  Path=/auth  one sign-in attempt: state, PKCE verifier, nonce, the page to return to (10 min).
//
// On http://localhost (ENVIRONMENT local/test) the prefixes and Secure are dropped, the names stay distinct.

import type { WebConfig } from "./config";
import { nowSeconds, open, seal } from "./seal";

export const SESSION_DAYS = 7; // = the cis-portal refresh token lifetime (168 h, AA-CIS-Infra identity.tf)
const SESSION_MAX_AGE = SESSION_DAYS * 24 * 3600;
const TX_MAX_AGE = 600;
/** Renew the access token this long before it expires, so a page render never carries a token that dies mid-flight. */
export const ACCESS_SKEW_S = 60;

export interface Session {
  v: 1;
  at: string;
  atExp: number;
  iss: string;
  sub: string;
  cid: string;
  tid: string | null;
  exp: number;
}

export interface RefreshState {
  v: 1;
  rt: string;
  sub: string;
  exp: number;
}

export interface OAuthTx {
  state: string;
  verifier: string;
  nonce: string;
  next: string;
  exp: number;
}

export interface CookieNames {
  session: string;
  refresh: string;
  tx: string;
  locale: string;
}

export function cookieNames(cfg: Pick<WebConfig, "secureCookies">): CookieNames {
  return cfg.secureCookies
    ? { session: "__Host-aa_crm_s", refresh: "__Secure-aa_crm_r", tx: "__Secure-aa_crm_tx", locale: "aa_crm_locale" }
    : { session: "aa_crm_s", refresh: "aa_crm_r", tx: "aa_crm_tx", locale: "aa_crm_locale" };
}

export interface CookieSpec {
  name: string;
  value: string;
  httpOnly: boolean;
  secure: boolean;
  sameSite: "lax" | "strict";
  path: string;
  maxAge: number;
}

/** The minimal cookie reader both NextRequest.cookies and next/headers cookies() satisfy. */
export interface CookieReader {
  get(name: string): { value: string } | undefined;
}

function spec(cfg: WebConfig, name: string, value: string, path: string, maxAge: number): CookieSpec {
  return { name, value, httpOnly: true, secure: cfg.secureCookies, sameSite: "lax", path, maxAge };
}

export function readSession(cfg: WebConfig, cookies: CookieReader, now: number = nowSeconds()): Session | null {
  const s = open<Session>(cfg.sessionSecret, "session", cookies.get(cookieNames(cfg).session)?.value, now);
  return s && s.v === 1 && typeof s.at === "string" && typeof s.sub === "string" ? s : null;
}

export function accessValid(session: Session | null, now: number = nowSeconds()): session is Session {
  return session !== null && session.atExp - ACCESS_SKEW_S > now;
}

export function readRefresh(cfg: WebConfig, cookies: CookieReader, now: number = nowSeconds()): RefreshState | null {
  const r = open<RefreshState>(cfg.sessionSecret, "refresh", cookies.get(cookieNames(cfg).refresh)?.value, now);
  return r && r.v === 1 && typeof r.rt === "string" ? r : null;
}

export function readTx(cfg: WebConfig, cookies: CookieReader, now: number = nowSeconds()): OAuthTx | null {
  return open<OAuthTx>(cfg.sessionSecret, "oauth-tx", cookies.get(cookieNames(cfg).tx)?.value, now);
}

export function sessionCookie(cfg: WebConfig, session: Omit<Session, "v" | "exp">, now = nowSeconds()): CookieSpec {
  const value = seal(cfg.sessionSecret, "session", { v: 1, ...session, exp: now + SESSION_MAX_AGE });
  return spec(cfg, cookieNames(cfg).session, value, "/", SESSION_MAX_AGE);
}

export function refreshCookie(cfg: WebConfig, rt: string, sub: string, now = nowSeconds()): CookieSpec {
  const value = seal(cfg.sessionSecret, "refresh", { v: 1, rt, sub, exp: now + SESSION_MAX_AGE });
  return spec(cfg, cookieNames(cfg).refresh, value, "/auth", SESSION_MAX_AGE);
}

export function txCookie(cfg: WebConfig, tx: Omit<OAuthTx, "exp">, now = nowSeconds()): CookieSpec {
  const value = seal(cfg.sessionSecret, "oauth-tx", { ...tx, exp: now + TX_MAX_AGE });
  return spec(cfg, cookieNames(cfg).tx, value, "/auth", TX_MAX_AGE);
}

/** Expired copies of every session cookie (sign-out, refused refresh). */
export function clearedCookies(cfg: WebConfig): CookieSpec[] {
  const n = cookieNames(cfg);
  return [spec(cfg, n.session, "", "/", 0), spec(cfg, n.refresh, "", "/auth", 0), spec(cfg, n.tx, "", "/auth", 0)];
}
