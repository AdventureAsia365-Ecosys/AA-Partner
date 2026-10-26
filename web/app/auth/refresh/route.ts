// GET /auth/refresh?next=/path — renew the access token with the refresh token (sent only to /auth/*), keep the
// agency picked, then go back. No refresh token, a refused one, or another person's -> sign in again. AA-935.
import type { NextRequest } from "next/server";
import { requestConfig } from "@/lib/config";
import { safeNext } from "@/lib/hosts";
import { accessClaims, OAuthError, refreshTokens } from "@/lib/oauth";
import { redirectTo, withCookies } from "@/lib/respond";
import { clearedCookies, readRefresh, readSession, refreshCookie, sessionCookie } from "@/lib/session";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const cfg = requestConfig(request);
  if (!cfg.authConfigured) return redirectTo(cfg, "/signed-out?reason=unavailable", 307, request.nextUrl.origin);
  const next = safeNext(request.nextUrl.searchParams.get("next"));
  const signIn = () =>
    withCookies(redirectTo(cfg, `/auth/sign-in?next=${encodeURIComponent(next)}`), clearedCookies(cfg));
  const refresh = readRefresh(cfg, request.cookies);
  const session = readSession(cfg, request.cookies);
  if (!refresh || (session && session.sub !== refresh.sub)) return signIn();
  try {
    const tokens = await refreshTokens(cfg, refresh.rt);
    const claims = accessClaims(cfg, tokens.accessToken);
    if (claims.sub !== refresh.sub) return signIn();
    const cookies = [
      sessionCookie(cfg, {
        at: tokens.accessToken,
        atExp: claims.exp,
        iss: claims.iss,
        sub: claims.sub,
        cid: claims.clientId,
        tid: session?.tid ?? null,
      }),
      // Refresh-token rotation (identity.tf) returns a new refresh token; keep the old one when it does not.
      refreshCookie(cfg, tokens.refreshToken ?? refresh.rt, claims.sub),
    ];
    return withCookies(redirectTo(cfg, next), cookies);
  } catch (err) {
    if (err instanceof OAuthError) {
      console.warn(JSON.stringify({ event: "crm_web_refresh_failed", code: err.code, status: err.status }));
      return signIn();
    }
    throw err;
  }
}
