// GET /join?t=<token> — the link of the AA invitation mail (AA-939). Public. The token is sealed into a cookie on
// path /join (lib/invite.ts) and the browser goes to /join/invite at once, so the token leaves the address bar and
// the history. A malformed or missing token goes to the same page, which then says the link is not valid.
// S263: answer cookies of another invitation (an earlier link opened in this browser) are dropped here, so the page of
// this link never shows another link's address or "opened before" notice; an answer of this same link stays.
import type { NextRequest } from "next/server";
import { requestConfig } from "@/lib/config";
import { inviteCookie, isInviteToken, staleAnswerCookies } from "@/lib/invite";
import { redirectTo, withCookies } from "@/lib/respond";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const cfg = requestConfig(request);
  const token = request.nextUrl.searchParams.get("t");
  const res = redirectTo(cfg, "/join/invite", 303, request.nextUrl.origin);
  res.headers.set("Referrer-Policy", "no-referrer");
  if (!cfg.authConfigured || !isInviteToken(token)) return res;
  return withCookies(res, [inviteCookie(cfg, token), ...staleAnswerCookies(cfg, request.cookies, token)]);
}
