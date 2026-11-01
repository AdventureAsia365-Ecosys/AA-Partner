// POST /join/claim — open the invitation (AA-939): the API's public claim creates the partner-pool account (or finds
// the existing one). Same-origin only (refuseForeignWrite, before any cookie or body is read). The answer, with the
// one-time temporary password, is kept only in a sealed 15-minute cookie on /join (lib/invite.ts) and shown by
// /join/invite; never in a URL or a log line.
//
// Submitting again (a double click, a reload or Back that re-POSTs, a second tab) never loses the password: while the
// claim cookie of this invitation holds one, the API is not asked again, and an "already_claimed" answer never
// replaces a claim answer the browser already has (S263, found live on Dev). When the person never saw a password, the
// API gives the account a new one as long as it was never signed in to (api app/members/invitations.py).
import type { NextRequest } from "next/server";
import { requestConfig } from "@/lib/config";
import { claimCookie, claimInvitation, claimStep, inviteCookieNames, joinError, openClaim, openInvite, replacesPrior } from "@/lib/invite";
import { redirectTo, withCookies } from "@/lib/respond";
import { refuseForeignWrite } from "@/lib/writeGuard";

export const dynamic = "force-dynamic";

export async function POST(request: NextRequest) {
  const cfg = requestConfig(request);
  const refused = refuseForeignWrite(cfg, request);
  if (refused) return refused;
  const names = inviteCookieNames(cfg);
  const token = openInvite(cfg, request.cookies.get(names.token)?.value);
  if (!cfg.authConfigured || !token) return redirectTo(cfg, "/join/invite?error=no_invitation", 303, request.nextUrl.origin);
  const shown = redirectTo(cfg, "/join/invite", 303, request.nextUrl.origin);
  const prior = openClaim(cfg, request.cookies.get(names.claim)?.value, token);
  if (claimStep(prior) === "keep") return shown;
  const outcome = await claimInvitation(cfg, token);
  if (!outcome.ok) {
    console.warn(JSON.stringify({ event: "crm_web_invite_claim_failed", status: outcome.status, detail: joinError(outcome.detail) }));
    return redirectTo(cfg, `/join/invite?error=${joinError(outcome.detail)}`, 303, request.nextUrl.origin);
  }
  if (!replacesPrior(prior, outcome.claim)) return shown;
  return withCookies(shown, [claimCookie(cfg, outcome.claim, token)]);
}
