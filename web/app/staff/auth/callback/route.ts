// GET /staff/auth/callback?code=…&state=… — finish the staff sign-in (AA-1019): the state must match the sealed staff
// attempt, the code is exchanged with the crm-staff client's PKCE verifier, the access token must belong to that client
// (lib/oauth.ts accessClaims) and carry a staff group (crm-ops / crm-support); then the staff cookies are set. The
// agency session is neither read nor written. The API checks the token and the group again on every call.
import type { NextRequest } from "next/server";
import { requestConfig } from "@/lib/config";
import { accessClaims, exchangeCode, OAuthError, revoke } from "@/lib/oauth";
import { redirectTo, withCookies } from "@/lib/respond";
import {
  clearedStaffCookies,
  readStaffTx,
  STAFF_SIGNED_OUT,
  staffClient,
  staffCookieNames,
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
  const params = request.nextUrl.searchParams;
  const tx = readStaffTx(cfg, request.cookies);
  const code = params.get("code");
  const state = params.get("state");
  const failed = (reason = "failed") =>
    withCookies(redirectTo(cfg, `${STAFF_SIGNED_OUT}?reason=${reason}`), clearedStaffCookies(cfg));
  if (!tx || !code || !state || state !== tx.state || params.get("error")) return failed();
  const client = staffClient(cfg);
  try {
    const tokens = await exchangeCode(client, code, tx.verifier);
    const claims = accessClaims(client, tokens.accessToken);
    const groups = staffGroups(claims.groups);
    if (groups.length === 0) {
      // A WORKFORCE user of another app (cis-*, booking-*): nothing to show here. Drop the refresh token now.
      if (tokens.refreshToken) await revoke(client, tokens.refreshToken);
      console.warn(JSON.stringify({ event: "crm_web_staff_sign_in_no_group" }));
      return failed("no_group");
    }
    const cookies = [
      staffSessionCookie(cfg, {
        at: tokens.accessToken,
        atExp: claims.exp,
        iss: claims.iss,
        sub: claims.sub,
        cid: claims.clientId,
        groups,
      }),
      { ...clearedStaffCookies(cfg).find((c) => c.name === staffCookieNames(cfg).tx)! },
    ];
    if (tokens.refreshToken) cookies.push(staffRefreshCookie(cfg, tokens.refreshToken, claims.sub));
    return withCookies(redirectTo(cfg, tx.next), cookies);
  } catch (err) {
    if (err instanceof OAuthError) {
      console.warn(JSON.stringify({ event: "crm_web_staff_sign_in_failed", code: err.code, status: err.status }));
      return failed();
    }
    throw err;
  }
}
