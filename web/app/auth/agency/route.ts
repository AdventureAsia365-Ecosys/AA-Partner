// /auth/agency — pick the agency the session works in (AA-935). The id is checked against the caller's own
// memberships from the API (GET /crm/v1/memberships), so a session can only ever hold an agency the person belongs
// to; the API checks the membership again on every call (tenant_tx()).
//
//   POST (form tenant_id, next)  same-origin only; switch to that agency
//   GET  ?next=…                 pick automatically when the person has exactly one active agency, else the chooser
//
// AA-940: the agency audit's "sign-ins" (design §4.9), sent server side as POST /crm/v1/audit/sign-in for the picked
// agency, best effort:
//   - GET records only when the session carries the one-shot `si` flag that /auth/callback sets after a real Cognito
//     sign-in. A GET has a side effect only then: it.com is a public suffix (AA-911), so a sibling host can make a
//     signed-in browser load /auth/agency with the Lax cookie, and must not be able to add sign-in events.
//   - POST (a person picking or switching agency in the chooser, behind refuseForeignWrite) always records.
// Either way the new session cookie drops the flag. The browser has no BFF route to the sign-in endpoint.
import type { NextRequest } from "next/server";
import { requestConfig } from "@/lib/config";
import { fetchMemberships, recordSignIn, selectable } from "@/lib/crmApi";
import { safeNext } from "@/lib/hosts";
import { redirectTo, withCookies } from "@/lib/respond";
import { accessValid, readSession, sessionCookie, type Session } from "@/lib/session";
import { refuseForeignWrite } from "@/lib/writeGuard";

export const dynamic = "force-dynamic";

function keep(session: Session, tid: string) {
  const { v: _v, exp: _exp, si: _si, ...rest } = session;
  return { ...rest, tid };
}

export async function GET(request: NextRequest) {
  const cfg = requestConfig(request);
  const next = safeNext(request.nextUrl.searchParams.get("next"));
  const session = readSession(cfg, request.cookies);
  if (!cfg.authConfigured || !accessValid(session)) {
    return redirectTo(cfg, `/auth/refresh?next=${encodeURIComponent(`/select-agency?next=${encodeURIComponent(next)}`)}`);
  }
  const found = await fetchMemberships(cfg, session);
  const active = found.ok ? found.data.filter((m) => m.tenant_status === "active") : [];
  if (active.length === 1) {
    const picked = keep(session, active[0].tenant_id);
    if (session.si === 1) await recordSignIn(cfg, { ...session, tid: picked.tid });
    return withCookies(redirectTo(cfg, next), [sessionCookie(cfg, picked)]);
  }
  return redirectTo(cfg, `/select-agency?next=${encodeURIComponent(next)}&manual=1`);
}

export async function POST(request: NextRequest) {
  const cfg = requestConfig(request);
  const refused = refuseForeignWrite(cfg, request);
  if (refused) return refused;
  const form = await request.formData();
  const next = safeNext(String(form.get("next") ?? "/"));
  const tenantId = String(form.get("tenant_id") ?? "");
  const session = readSession(cfg, request.cookies);
  if (!cfg.authConfigured || !accessValid(session)) return redirectTo(cfg, `/auth/refresh?next=${encodeURIComponent("/select-agency")}`, 303);
  const found = await fetchMemberships(cfg, session);
  const pick = found.ok ? selectable(found.data, tenantId) : null;
  if (!pick) return redirectTo(cfg, `/select-agency?next=${encodeURIComponent(next)}&manual=1`, 303);
  await recordSignIn(cfg, { ...session, tid: pick.tenant_id });
  return withCookies(redirectTo(cfg, next, 303), [sessionCookie(cfg, keep(session, pick.tenant_id))]);
}
