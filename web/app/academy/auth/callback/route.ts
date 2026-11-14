// GET /academy/auth/callback?code=…&state=… — finish the AA Academy sign-in (AA-1105): the state must match the sealed
// academy attempt, the code is exchanged with the academy-web client's PKCE verifier and the access token must belong
// to that client (lib/oauth.ts accessClaims); then the academy cookies are set. No group is required here: CIS decides
// on every call who is a learner (403 not_a_learner), and the pages say so. No other session is read or written.
import type { NextRequest } from "next/server";
import { requestConfig } from "@/lib/config";
import { accessClaims, exchangeCode, OAuthError } from "@/lib/oauth";
import { redirectTo, withCookies } from "@/lib/respond";
import {
  ACADEMY_SIGNED_OUT,
  academyClient,
  academyCookieNames,
  academyRefreshCookie,
  academySessionCookie,
  clearedAcademyCookies,
  readAcademyTx,
} from "@/lib/academySession";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const cfg = requestConfig(request);
  if (!cfg.academyAuthConfigured) {
    return redirectTo(cfg, `${ACADEMY_SIGNED_OUT}?reason=unavailable`, 307, request.nextUrl.origin);
  }
  const params = request.nextUrl.searchParams;
  const tx = readAcademyTx(cfg, request.cookies);
  const code = params.get("code");
  const state = params.get("state");
  const failed = () => withCookies(redirectTo(cfg, `${ACADEMY_SIGNED_OUT}?reason=failed`), clearedAcademyCookies(cfg));
  if (!tx || !code || !state || state !== tx.state || params.get("error")) return failed();
  const client = academyClient(cfg);
  try {
    const tokens = await exchangeCode(client, code, tx.verifier);
    const claims = accessClaims(client, tokens.accessToken);
    const cookies = [
      academySessionCookie(cfg, { at: tokens.accessToken, atExp: claims.exp, iss: claims.iss, sub: claims.sub, cid: claims.clientId }),
      { ...clearedAcademyCookies(cfg).find((c) => c.name === academyCookieNames(cfg).tx)! },
    ];
    if (tokens.refreshToken) cookies.push(academyRefreshCookie(cfg, tokens.refreshToken, claims.sub));
    return withCookies(redirectTo(cfg, tx.next), cookies);
  } catch (err) {
    if (err instanceof OAuthError) {
      console.warn(JSON.stringify({ event: "crm_web_academy_sign_in_failed", code: err.code, status: err.status }));
      return failed();
    }
    throw err;
  }
}
