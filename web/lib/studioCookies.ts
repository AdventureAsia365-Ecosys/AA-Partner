// web/lib/studioCookies.ts
// AA-979 P2b — the Content studio's (portal/) five host-only cookies on the same agency host (portal.). The portal
// sets them (Path=/, httpOnly, SameSite=Lax; names unchanged since AA-427); the web clears them at sign-out so one
// sign-out ends both sessions. Kept out of lib/session.ts on purpose: the portal holds a byte copy of that file.

import type { WebConfig } from "./config";
import type { CookieSpec } from "./session";

export const STUDIO_COOKIES = ["cis_role", "cis_tenant_token", "cis_tenant_id", "cis_tenant_name", "cis_tenant_plan"] as const;

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
