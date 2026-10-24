// web/lib/clientSearch.ts — the clients directory search term, kept out of URLs (AA-937, card CRM-09; design §4.4).
// Server-only (node:crypto). An exact search by e-mail or phone would otherwise sit in /clients?q=… (Vercel / CDN
// request logs, browser history). The form POSTs the term to /clients/search, which seals it into a short-lived
// cookie (AES-256-GCM, purpose "client-search", bound to the signed-in person and the agency picked) and redirects to
// /clients?s=1; the page opens it and searches through POST /crm/v1/travelers/search, so the term travels in bodies
// and an encrypted cookie only.
import type { WebConfig } from "./config";
import { nowSeconds, open, seal } from "./seal";
import type { CookieSpec, Session } from "./session";

/** 15 minutes: long enough to page through results, short enough not to linger. */
export const SEARCH_TTL_S = 15 * 60;

interface SealedSearch {
  q: string;
  sub: string;
  tid: string | null;
  exp: number;
}

export function searchCookieName(cfg: Pick<WebConfig, "secureCookies">): string {
  return cfg.secureCookies ? "__Secure-aa_crm_q" : "aa_crm_q";
}

/** The Set-Cookie for a search term (an empty term clears the cookie). Path /clients: never sent elsewhere. */
export function searchCookie(cfg: WebConfig, session: Session | null, q: string, now: number = nowSeconds()): CookieSpec {
  const base = { name: searchCookieName(cfg), httpOnly: true, secure: cfg.secureCookies, sameSite: "lax" as const, path: "/clients" };
  if (!q || !session) return { ...base, value: "", maxAge: 0 };
  const value = seal(cfg.sessionSecret, "client-search", { q, sub: session.sub, tid: session.tid, exp: now + SEARCH_TTL_S });
  return { ...base, value, maxAge: SEARCH_TTL_S };
}

/** The saved term, or "" when there is none, it expired, or it belongs to another person or agency. */
export function openSearch(cfg: WebConfig, session: Session | null, sealed: string | undefined, now: number = nowSeconds()): string {
  if (!session) return "";
  const v = open<SealedSearch>(cfg.sessionSecret, "client-search", sealed, now);
  if (!v || typeof v.q !== "string" || v.sub !== session.sub || v.tid !== session.tid) return "";
  return v.q;
}
