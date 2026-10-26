// portal/lib/tenantSession.ts
// AA-979 P2b — the Content studio's partner-pool sign-in (root ADR 0010a): one host, one sign-in with the CRM web.
//
// The CRM web (same host, portal.) keeps the person's partner-pool session in its sealed cookie __Host-aa_crm_s
// (web/lib/session.ts, byte copy here as lib/session.ts). The studio READS it with the same key (WEB_SESSION_SECRET)
// and never writes or refreshes it: the web's /auth/refresh does. With a valid partner access token the studio asks
// CIS for a short tenant JWT (POST /auth/tenant-session, at most 15 min, client=portal, amr=partner, mid, csr) and keeps
// it in the same five host-only cookies the API-key sign-in has always used, so every BFF route and the CIS API work
// unchanged. CIS decides the tenant and the Content studio role; the CRM role plays no part.
//
// API-key sessions (no `amr` claim) keep working while PORTAL_API_KEY_SIGNIN is on (the P2b window): a session that
// existed on Vercel at the DNS flip survives it (critic C3).

import type { WebConfig } from "./config";
import { accessValid, readSession, type CookieReader, type Session } from "./session";

export * from "./studioSession";

/** The CRM web's partner session when its access token is still valid, else null (read only; never refreshed here). */
export function partnerSession(cfg: WebConfig, cookies: CookieReader): Session | null {
  if (!cfg.partnerSignIn) return null;
  const session = readSession(cfg, cookies);
  return accessValid(session) ? session : null;
}
