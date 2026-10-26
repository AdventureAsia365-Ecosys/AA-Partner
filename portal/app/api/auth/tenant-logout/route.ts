// portal/app/api/auth/tenant-logout/route.ts (AA-885: moved from the admin app)
// AA-427 — clears the 5 httpOnly tenant cookies server-side.
//
// Sidebar.tsx / PlaceholderTabs.tsx used to clear the session by looping
// `document.cookie = "<name>=; path=/; max-age=0"` over the 5 cookie names.
// That stops working once the cookies are httpOnly — client JS can no
// longer see or overwrite them at all (expiry included), so the browser
// would keep sending a "logged out" user's real tenant JWT on every request
// after clicking Logout. This route does the clearing server-side instead.
//
// AA-979 P2b: when the CRM web's partner session is on the same host, the answer names the web's sign-out
// (`sign_out`), which the shell then POSTs: one sign-out ends the CRM session, the Cognito session and (the web
// clears them too) these cookies.
import { NextRequest, NextResponse } from "next/server";
import { TENANT_COOKIES, TENANT_TOKEN_COOKIE, tenantVerifyCache } from "@/lib/auth-server";
import { config } from "@/lib/config";
import { readSession } from "@/lib/session";
import { refuseForeignWrite } from "@/lib/writeGuard";

export async function POST(req: NextRequest) {
  // AA-911 — refuse a cross-site write before any cookie or body is read (lib/sameOrigin.ts).
  const refused = refuseForeignWrite(req);
  if (refused) return refused;
  const cfg = config();
  // AA-885: drop the token's verify-cache entry on this instance too (same idea as the staff logout).
  const token = req.cookies.get(TENANT_TOKEN_COOKIE)?.value;
  if (token) tenantVerifyCache.delete(token);
  const partner = cfg.partnerSignIn && readSession(cfg, req.cookies) !== null;
  const response = NextResponse.json({ ok: true, sign_out: partner ? cfg.signOutPath : null });
  response.headers.set("Cache-Control", "no-store");
  for (const name of TENANT_COOKIES) {
    response.cookies.set(name, "", { path: "/", maxAge: 0 });
  }
  return response;
}
