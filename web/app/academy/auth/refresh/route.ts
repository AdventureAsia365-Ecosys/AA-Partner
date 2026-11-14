// GET /academy/auth/refresh?next=/academy/... — renew the academy access token with the academy refresh token (sent
// only to /academy/auth/*), then go back (AA-1105). No refresh token, a refused one or another person's -> sign in
// again. No other session is involved.
import type { NextRequest } from "next/server";
import { requestConfig } from "@/lib/config";
import { accessClaims, OAuthError, refreshTokens } from "@/lib/oauth";
import { redirectTo, withCookies } from "@/lib/respond";
import {
  ACADEMY_AUTH_PREFIX,
  ACADEMY_SIGNED_OUT,
  academyClient,
  academyRefreshCookie,
  academySessionCookie,
  clearedAcademyCookies,
  readAcademyRefresh,
  readAcademySession,
  safeAcademyNext,
} from "@/lib/academySession";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const cfg = requestConfig(request);
  if (!cfg.academyAuthConfigured) {
    return redirectTo(cfg, `${ACADEMY_SIGNED_OUT}?reason=unavailable`, 307, request.nextUrl.origin);
  }
  const next = safeAcademyNext(request.nextUrl.searchParams.get("next"));
  const signIn = () =>
    withCookies(redirectTo(cfg, `${ACADEMY_AUTH_PREFIX}/sign-in?next=${encodeURIComponent(next)}`), clearedAcademyCookies(cfg));
  const refresh = readAcademyRefresh(cfg, request.cookies);
  const session = readAcademySession(cfg, request.cookies);
  if (!refresh || (session && session.sub !== refresh.sub)) return signIn();
  const client = academyClient(cfg);
  try {
    const tokens = await refreshTokens(client, refresh.rt);
    const claims = accessClaims(client, tokens.accessToken);
    if (claims.sub !== refresh.sub) return signIn();
    const cookies = [
      academySessionCookie(cfg, { at: tokens.accessToken, atExp: claims.exp, iss: claims.iss, sub: claims.sub, cid: claims.clientId }),
      academyRefreshCookie(cfg, tokens.refreshToken ?? refresh.rt, claims.sub),
    ];
    return withCookies(redirectTo(cfg, next), cookies);
  } catch (err) {
    if (err instanceof OAuthError) {
      console.warn(JSON.stringify({ event: "crm_web_academy_refresh_failed", code: err.code, status: err.status }));
      return signIn();
    }
    throw err;
  }
}
