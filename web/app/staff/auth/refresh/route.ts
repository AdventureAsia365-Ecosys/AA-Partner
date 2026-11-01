// GET /staff/auth/refresh?next=/staff/... — renew the staff access token with the staff refresh token (sent only to
// /staff/auth/*), then go back (AA-1019). No refresh token, a refused one, another person's, or a token that lost its
// staff groups -> sign in again. The agency session is never involved.
import type { NextRequest } from "next/server";
import { requestConfig } from "@/lib/config";
import { accessClaims, OAuthError, refreshTokens } from "@/lib/oauth";
import { redirectTo, withCookies } from "@/lib/respond";
import {
  clearedStaffCookies,
  readStaffRefresh,
  readStaffSession,
  safeStaffNext,
  STAFF_AUTH_PREFIX,
  STAFF_SIGNED_OUT,
  staffClient,
  staffGroups,
  staffRefreshCookie,
  staffSessionCookie,
} from "@/lib/staffSession";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const cfg = requestConfig(request);
  if (!cfg.staffAuthConfigured) {
    return redirectTo(cfg, `${STAFF_SIGNED_OUT}?reason=unavailable`, 307, request.nextUrl.origin);
  }
  const next = safeStaffNext(request.nextUrl.searchParams.get("next"));
  const signIn = () =>
    withCookies(redirectTo(cfg, `${STAFF_AUTH_PREFIX}/sign-in?next=${encodeURIComponent(next)}`), clearedStaffCookies(cfg));
  const refresh = readStaffRefresh(cfg, request.cookies);
  const session = readStaffSession(cfg, request.cookies);
  if (!refresh || (session && session.sub !== refresh.sub)) return signIn();
  const client = staffClient(cfg);
  try {
    const tokens = await refreshTokens(client, refresh.rt);
    const claims = accessClaims(client, tokens.accessToken);
    const groups = staffGroups(claims.groups);
    if (claims.sub !== refresh.sub || groups.length === 0) return signIn();
    const cookies = [
      staffSessionCookie(cfg, {
        at: tokens.accessToken,
        atExp: claims.exp,
        iss: claims.iss,
        sub: claims.sub,
        cid: claims.clientId,
        groups,
      }),
      staffRefreshCookie(cfg, tokens.refreshToken ?? refresh.rt, claims.sub),
    ];
    return withCookies(redirectTo(cfg, next), cookies);
  } catch (err) {
    if (err instanceof OAuthError) {
      console.warn(JSON.stringify({ event: "crm_web_staff_refresh_failed", code: err.code, status: err.status }));
      return signIn();
    }
    throw err;
  }
}
