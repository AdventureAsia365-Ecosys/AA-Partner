// GET /auth/callback?code=…&state=… — finish sign-in: check state against the sealed attempt, exchange the code with
// the PKCE verifier, keep the tokens in sealed httpOnly cookies, go back to the page the person asked for. AA-935.
import type { NextRequest } from "next/server";
import { config } from "@/lib/config";
import { accessClaims, exchangeCode, OAuthError } from "@/lib/oauth";
import { redirectTo, withCookies } from "@/lib/respond";
import { clearedCookies, cookieNames, readTx, refreshCookie, sessionCookie } from "@/lib/session";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const cfg = config();
  if (!cfg.authConfigured) return redirectTo(cfg, "/signed-out?reason=unavailable", 307, request.nextUrl.origin);
  const params = request.nextUrl.searchParams;
  const tx = readTx(cfg, request.cookies);
  const code = params.get("code");
  const state = params.get("state");
  const failed = () => withCookies(redirectTo(cfg, "/signed-out?reason=failed"), clearedCookies(cfg));
  if (!tx || !code || !state || state !== tx.state || params.get("error")) return failed();
  try {
    const tokens = await exchangeCode(cfg, code, tx.verifier);
    const claims = accessClaims(cfg, tokens.accessToken);
    const cookies = [
      sessionCookie(cfg, {
        at: tokens.accessToken,
        atExp: claims.exp,
        iss: claims.iss,
        sub: claims.sub,
        cid: claims.clientId,
        tid: null,
      }),
      // The attempt is used up: expire it.
      { ...clearedCookies(cfg).find((c) => c.name === cookieNames(cfg).tx)! },
    ];
    if (tokens.refreshToken) cookies.push(refreshCookie(cfg, tokens.refreshToken, claims.sub));
    return withCookies(redirectTo(cfg, tx.next), cookies);
  } catch (err) {
    if (err instanceof OAuthError) {
      console.warn(JSON.stringify({ event: "crm_web_sign_in_failed", code: err.code, status: err.status }));
      return failed();
    }
    throw err;
  }
}
