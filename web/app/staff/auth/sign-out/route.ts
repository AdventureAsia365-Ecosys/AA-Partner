// POST /staff/auth/sign-out — same-origin only (lib/writeGuard.ts): revoke the staff refresh token, clear the staff
// cookies, end the WORKFORCE managed-login session (AA-1019). The agency session on the same host is left alone: an
// AA staff member who is also an agency member signs out of each separately.
import type { NextRequest } from "next/server";
import { requestConfig } from "@/lib/config";
import { logoutUrl, revoke } from "@/lib/oauth";
import { json, redirectExternal, redirectTo, withCookies } from "@/lib/respond";
import { clearedStaffCookies, readStaffRefresh, STAFF_SIGNED_OUT, staffClient } from "@/lib/staffSession";
import { refuseForeignWrite } from "@/lib/writeGuard";

export const dynamic = "force-dynamic";

export async function POST(request: NextRequest) {
  const cfg = requestConfig(request);
  const refused = refuseForeignWrite(cfg, request);
  if (refused) return refused;
  if (!cfg.staffAuthConfigured) {
    return withCookies(redirectTo(cfg, STAFF_SIGNED_OUT, 303, request.nextUrl.origin), clearedStaffCookies(cfg));
  }
  const client = staffClient(cfg);
  const refresh = readStaffRefresh(cfg, request.cookies);
  if (refresh) await revoke(client, refresh.rt);
  return withCookies(redirectExternal(logoutUrl(client), 303), clearedStaffCookies(cfg));
}

export function GET() {
  return json({ detail: "method_not_allowed" }, 405);
}
