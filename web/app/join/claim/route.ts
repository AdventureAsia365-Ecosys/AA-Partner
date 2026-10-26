// POST /join/claim — open the invitation (AA-939): the API's public claim creates the partner-pool account (or finds
// the existing one). Same-origin only (refuseForeignWrite, before any cookie or body is read). The answer, with the
// one-time temporary password, is kept only in a sealed 15-minute cookie on /join (lib/invite.ts) and shown by
// /join/invite; never in a URL or a log line.
import type { NextRequest } from "next/server";
import { requestConfig } from "@/lib/config";
import { claimCookie, claimInvitation, inviteCookieNames, joinError, openInvite } from "@/lib/invite";
import { redirectTo, withCookies } from "@/lib/respond";
import { refuseForeignWrite } from "@/lib/writeGuard";

export const dynamic = "force-dynamic";

export async function POST(request: NextRequest) {
  const cfg = requestConfig(request);
  const refused = refuseForeignWrite(cfg, request);
  if (refused) return refused;
  const token = openInvite(cfg, request.cookies.get(inviteCookieNames(cfg).token)?.value);
  if (!cfg.authConfigured || !token) return redirectTo(cfg, "/join/invite?error=no_invitation", 303, request.nextUrl.origin);
  const outcome = await claimInvitation(cfg, token);
  if (!outcome.ok) {
    console.warn(JSON.stringify({ event: "crm_web_invite_claim_failed", status: outcome.status, detail: joinError(outcome.detail) }));
    return redirectTo(cfg, `/join/invite?error=${joinError(outcome.detail)}`, 303, request.nextUrl.origin);
  }
  return withCookies(redirectTo(cfg, "/join/invite", 303, request.nextUrl.origin), [claimCookie(cfg, outcome.claim)]);
}
