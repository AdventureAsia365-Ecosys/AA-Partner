// web/lib/studioCookies.ts
// AA-979 P2b — the Content studio's host-only cookies on the agency host (portal.). The studio sets them (Path=/,
// httpOnly, SameSite=Lax; names unchanged since AA-427); sign-out clears them so one sign-out ends both sessions.
// AA-1025 (S264): + cis_partner_ref, the partner sign-in the studio cookies belong to.
// AA-1036 (R3): the studio now lives in this app; lib/studio/studioSession.ts re-exports this list (the one source,
// tests/studioAccess.test.ts checks the wiring).

import type { WebConfig } from "./config";
import type { CookieSpec } from "./session";

export const STUDIO_COOKIES = [
  "cis_role", "cis_tenant_token", "cis_tenant_id", "cis_tenant_name", "cis_tenant_plan", "cis_partner_ref",
] as const;

export function studioCookiesCleared(cfg: Pick<WebConfig, "secureCookies">): CookieSpec[] {
  return STUDIO_COOKIES.map((name) => ({
    name,
    value: "",
    httpOnly: true,
    secure: cfg.secureCookies,
    sameSite: "lax" as const,
    path: "/",
    maxAge: 0,
  }));
}
