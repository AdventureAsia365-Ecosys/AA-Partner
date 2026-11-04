// web/lib/studio/tenantSession.ts
// AA-979 P2b — the Content studio's partner-pool sign-in (root ADR 0010a), now inside the one agency app (AA-1036, root
// ADR 0019 d6): one host, one sign-in, one session.
//
// The agency app keeps the person's partner-pool session in its sealed cookie (lib/session.ts). With a valid partner
// access token the studio asks CIS for a short tenant JWT (POST /auth/tenant-session, at most 15 min, client=portal,
// amr=partner, mid, csr) and keeps it in the host-only studio cookies, so every studio BFF route and the CIS API work
// unchanged. CIS decides the tenant and the Content studio role; the CRM role plays no part. There is no API-key
// session any more (root ADR 0019 d6): a studio token without amr=partner is never used.

import type { WebConfig } from "../config";
import { accessValid, readSession, type CookieReader, type Session } from "../session";

export * from "./studioSession";

/** The partner session when its access token is still valid, else null (renewed only by /auth/refresh). */
export function partnerSession(cfg: WebConfig, cookies: CookieReader): Session | null {
  if (!cfg.authConfigured) return null;
  const session = readSession(cfg, cookies);
  return accessValid(session) ? session : null;
}
