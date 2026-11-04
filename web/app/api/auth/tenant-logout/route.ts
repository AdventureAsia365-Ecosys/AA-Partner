// web/app/api/auth/tenant-logout/route.ts (AA-885: moved from the admin app; AA-1036: into the one agency app)
// AA-427 — clears the httpOnly studio cookies server side and drops the token's verify-cache entry on this instance.
// The answer names the app's sign-out (`sign_out`), which the shell then POSTs as a form: one sign-out ends the
// agency session, the Cognito session and (the sign-out clears them too) these cookies. Root ADR 0019 d6: there is
// no API-key session to fall back to any more.
import { NextRequest, NextResponse } from "next/server";
import { requestConfig } from "@/lib/config";
import { TENANT_TOKEN_COOKIE, tenantVerifyCache } from "@/lib/studio/auth-server";
import { studioCookiesCleared } from "@/lib/studioCookies";
import { refuseForeignWrite } from "@/lib/writeGuard";

const SIGN_OUT_PATH = "/auth/sign-out";

export async function POST(req: NextRequest) {
  const cfg = requestConfig(req);
  // AA-911 — refuse a cross-site write before any cookie or body is read (lib/sameOrigin.ts).
  const refused = refuseForeignWrite(cfg, req);
  if (refused) return refused;
  const token = req.cookies.get(TENANT_TOKEN_COOKIE)?.value;
  if (token) tenantVerifyCache.delete(token);
  const response = NextResponse.json({ ok: true, sign_out: SIGN_OUT_PATH });
  response.headers.set("Cache-Control", "no-store");
  for (const c of studioCookiesCleared(cfg)) {
    response.cookies.set(c.name, "", { path: c.path, maxAge: 0, httpOnly: true, secure: c.secure, sameSite: c.sameSite });
  }
  return response;
}
