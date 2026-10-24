// /auth/agency — pick the agency the session works in (AA-935). The id is checked against the caller's own
// memberships from the API (GET /crm/v1/memberships), so a session can only ever hold an agency the person belongs
// to; the API checks the membership again on every call (tenant_tx()).
//
//   POST (form tenant_id, next)  same-origin only; switch to that agency
//   GET  ?next=…                 pick automatically when the person has exactly one active agency, else the chooser
import type { NextRequest } from "next/server";
import { config } from "@/lib/config";
import { fetchMemberships, selectable } from "@/lib/crmApi";
import { refuseCrossSiteWrite } from "@/lib/crossSiteGuard";
import { safeNext } from "@/lib/hosts";
import { redirectTo, withCookies } from "@/lib/respond";
import { accessValid, readSession, sessionCookie, type Session } from "@/lib/session";

export const dynamic = "force-dynamic";

function keep(session: Session, tid: string) {
  const { v: _v, exp: _exp, ...rest } = session;
  return { ...rest, tid };
}

export async function GET(request: NextRequest) {
  const cfg = config();
  const next = safeNext(request.nextUrl.searchParams.get("next"));
  const session = readSession(cfg, request.cookies);
  if (!cfg.authConfigured || !accessValid(session)) {
    return redirectTo(cfg, `/auth/refresh?next=${encodeURIComponent(`/select-agency?next=${encodeURIComponent(next)}`)}`);
  }
  const found = await fetchMemberships(cfg, session);
  const active = found.ok ? found.data.filter((m) => m.tenant_status === "active") : [];
  if (active.length === 1) return withCookies(redirectTo(cfg, next), [sessionCookie(cfg, keep(session, active[0].tenant_id))]);
  return redirectTo(cfg, `/select-agency?next=${encodeURIComponent(next)}&manual=1`);
}

export async function POST(request: NextRequest) {
  const refused = refuseCrossSiteWrite(request);
  if (refused) return refused;
  const cfg = config();
  const form = await request.formData();
  const next = safeNext(String(form.get("next") ?? "/"));
  const tenantId = String(form.get("tenant_id") ?? "");
  const session = readSession(cfg, request.cookies);
  if (!cfg.authConfigured || !accessValid(session)) return redirectTo(cfg, `/auth/refresh?next=${encodeURIComponent("/select-agency")}`, 303);
  const found = await fetchMemberships(cfg, session);
  const pick = found.ok ? selectable(found.data, tenantId) : null;
  if (!pick) return redirectTo(cfg, `/select-agency?next=${encodeURIComponent(next)}&manual=1`, 303);
  return withCookies(redirectTo(cfg, next, 303), [sessionCookie(cfg, keep(session, pick.tenant_id))]);
}
