// POST /auth/sign-out — same-origin only (AA-911 rule: every *.lumiguides.it.com host is the same site); revoke the
// refresh token, clear every session cookie, end the managed-login session. AA-935.
import type { NextRequest } from "next/server";
import { config } from "@/lib/config";
import { refuseCrossSiteWrite } from "@/lib/crossSiteGuard";
import { logoutUrl, revoke } from "@/lib/oauth";
import { json, redirectExternal, redirectTo, withCookies } from "@/lib/respond";
import { clearedCookies, readRefresh } from "@/lib/session";

export const dynamic = "force-dynamic";

export async function POST(request: NextRequest) {
  const refused = refuseCrossSiteWrite(request);
  if (refused) return refused;
  const cfg = config();
  if (!cfg.authConfigured) {
    return withCookies(redirectTo(cfg, "/signed-out", 303, request.nextUrl.origin), clearedCookies(cfg));
  }
  const refresh = readRefresh(cfg, request.cookies);
  if (refresh) await revoke(cfg, refresh.rt);
  return withCookies(redirectExternal(logoutUrl(cfg), 303), clearedCookies(cfg));
}

export function GET() {
  return json({ detail: "method_not_allowed" }, 405);
}
