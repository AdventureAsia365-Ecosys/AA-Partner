// POST /join/confirm — accept the invitation after sign-in (AA-939). Same-origin only. The API binds the membership to
// the session token's (iss, sub) (POST /crm/v1/invitations/accept with the sealed invitation token); on success both
// invitation cookies are cleared and the session switches to the agency just joined.
import type { NextRequest } from "next/server";
import { requestConfig } from "@/lib/config";
import { crmCall } from "@/lib/crmApi";
import { clearedInviteCookies, inviteCookieNames, joinError, openInvite } from "@/lib/invite";
import { redirectTo, withCookies } from "@/lib/respond";
import { accessValid, readSession, sessionCookie } from "@/lib/session";
import { refuseForeignWrite } from "@/lib/writeGuard";

export const dynamic = "force-dynamic";

export async function POST(request: NextRequest) {
  const cfg = requestConfig(request);
  const refused = refuseForeignWrite(cfg, request);
  if (refused) return refused;
  const session = readSession(cfg, request.cookies);
  if (!cfg.authConfigured || !accessValid(session)) {
    return redirectTo(cfg, `/auth/refresh?next=${encodeURIComponent("/join/accept")}`, 303, request.nextUrl.origin);
  }
  const token = openInvite(cfg, request.cookies.get(inviteCookieNames(cfg).token)?.value);
  if (!token) return redirectTo(cfg, "/join/accept?error=no_invitation", 303, request.nextUrl.origin);
  const res = await crmCall<{ tenant_id: string; member_id?: string; member_role?: string; kind?: string; status?: string }>(cfg, session, "POST", "invitations/accept", {
    body: { token },
  });
  if (!res.ok) {
    console.warn(JSON.stringify({ event: "crm_web_invite_accept_failed", status: res.status, detail: joinError(res.detail) }));
    return redirectTo(cfg, `/join/accept?error=${joinError(res.detail)}`, 303, request.nextUrl.origin);
  }
  // AA-979 P2b: a Content studio invitation (CIS tenant, no CRM agency): the session keeps its agency; the page says
  // whether the studio is ready (CIS activated the membership) or still being set up, and links the studio.
  if (res.data.kind === "studio") {
    const status = res.data.status === "activated" ? "activated" : "accepted";
    return withCookies(redirectTo(cfg, `/join/accept?studio=${status}`, 303, request.nextUrl.origin), clearedInviteCookies(cfg));
  }
  const { v: _v, exp: _exp, ...rest } = session;
  return withCookies(redirectTo(cfg, "/", 303, request.nextUrl.origin), [
    ...clearedInviteCookies(cfg),
    sessionCookie(cfg, { ...rest, tid: res.data.tenant_id }),
  ]);
}
