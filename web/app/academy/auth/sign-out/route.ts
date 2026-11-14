// POST /academy/auth/sign-out — same-origin only (lib/writeGuard.ts): revoke the academy refresh token, clear the
// academy cookies, end the WORKFORCE managed-login session (AA-1105). Other sessions are left alone.
import type { NextRequest } from "next/server";
import { requestConfig } from "@/lib/config";
import { logoutUrl, revoke } from "@/lib/oauth";
import { json, redirectExternal, redirectTo, withCookies } from "@/lib/respond";
import { ACADEMY_SIGNED_OUT, academyClient, clearedAcademyCookies, readAcademyRefresh } from "@/lib/academySession";
import { refuseForeignWrite } from "@/lib/writeGuard";

export const dynamic = "force-dynamic";

export async function POST(request: NextRequest) {
  const cfg = requestConfig(request);
  const refused = refuseForeignWrite(cfg, request);
  if (refused) return refused;
  if (!cfg.academyAuthConfigured) {
    return withCookies(redirectTo(cfg, ACADEMY_SIGNED_OUT, 303, request.nextUrl.origin), clearedAcademyCookies(cfg));
  }
  const client = academyClient(cfg);
  const refresh = readAcademyRefresh(cfg, request.cookies);
  if (refresh) await revoke(client, refresh.rt);
  return withCookies(redirectExternal(logoutUrl(client), 303), clearedAcademyCookies(cfg));
}

export function GET() {
  return json({ detail: "method_not_allowed" }, 405);
}
