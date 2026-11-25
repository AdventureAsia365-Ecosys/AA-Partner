// /connect/mail/callback — where Google / Microsoft send the browser back after the mailbox consent (AA-945; the
// redirect URI is <MAIL_OAUTH_REDIRECT_BASE>/connect/mail/callback). This GET has no side effect: it only hands the
// code and state to Settings > Mailbox, which completes the connection with a same-origin BFF write
// (POST mailbox/oauth/complete, crossSiteGuard). A GET with a side effect could be triggered by a sibling host
// (it.com is a public suffix, AA-911); and the sealed state opens only for the member who started it, in their agency.
// AA-1011: a calendar consent uses the same redirect URI; its state starts with "cal.", so the browser goes back to
// Account > Integrations instead (the prefix only routes: the API opens a calendar state for a calendar only).
import type { NextRequest } from "next/server";
import { requestConfig } from "@/lib/config";
import { CALENDAR_RETURN, isCalendarState } from "@/lib/calendarSync";
import { callbackParams } from "@/lib/mail";
import { redirectTo } from "@/lib/respond";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const cfg = requestConfig(request);
  const params = callbackParams(request.nextUrl.searchParams);
  if (isCalendarState(request.nextUrl.searchParams.get("state"))) {
    if ("error" in params) return redirectTo(cfg, `${CALENDAR_RETURN}?calendar_error=${params.error}`);
    const q = new URLSearchParams({ calendar_code: params.code, calendar_state: params.state });
    return redirectTo(cfg, `${CALENDAR_RETURN}?${q.toString()}`);
  }
  if ("error" in params) return redirectTo(cfg, `/settings/mailbox?oauth_error=${params.error}`);
  const q = new URLSearchParams({ code: params.code, state: params.state });
  return redirectTo(cfg, `/settings/mailbox?${q.toString()}`);
}
