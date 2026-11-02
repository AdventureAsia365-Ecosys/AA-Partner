// /bff/identity — AA-1025 (S264 hotfix): who is signed in to the CRM, for the Content studio on the same agency host
// (portal/lib/crmIdentity.ts). A literal segment, so Next serves it before the catch-all app/bff/[...path]/route.ts.
//
// Why not /bff/me: the CRM web relies on /bff/me answering 401 (no session, the page reloads through /auth/refresh) and
// 409 no_agency_selected, and those stay. But the browser logs every non-2xx fetch as a console error, so a studio page
// asking /bff/me for a person without an agency (or signed out) showed a console error on every load. This route asks
// the same question and ALWAYS answers a GET with 200 JSON:
//   {kind: "signed_out"}                              no session, or its access token expired
//   {kind: "no_agency"}                               signed in, no agency picked in the session
//   {kind: "agency", display_name, member_role}       the CRM API /me answer, only these two fields
//   {kind: "unknown"}                                 the API failed, timed out or answered something unexpected
// Same order as the catch-all: a Host other than the partner host -> 404 first (the proxy's check repeated); the token
// and agency come only from the sealed session, never from a browser header. Other methods: a cross-site write -> 403
// (lib/writeGuard.ts, as every write route), otherwise 405.
import { randomUUID } from "node:crypto";
import type { NextRequest } from "next/server";
import { matchBff, upstreamHeaders } from "@/lib/bff";
import { requestConfig } from "@/lib/config";
import { pickIdentity, type CrmIdentityAnswer } from "@/lib/crmIdentity";
import { classifyHost } from "@/lib/hosts";
import { json } from "@/lib/respond";
import { accessValid, readSession } from "@/lib/session";
import { refuseForeignWrite } from "@/lib/writeGuard";

export const dynamic = "force-dynamic";

const UPSTREAM_TIMEOUT_MS = 15_000; // as the catch-all's default for "me"

function onPartnerHost(request: NextRequest): boolean {
  return classifyHost(request.headers.get("host"), requestConfig(request)) === "partner";
}

export async function GET(request: NextRequest) {
  if (!onPartnerHost(request)) return json({ detail: "not_found" }, 404);
  const cfg = requestConfig(request);
  const session = cfg.authConfigured ? readSession(cfg, request.cookies) : null;
  if (!accessValid(session)) return json({ kind: "signed_out" } satisfies CrmIdentityAnswer);
  if (!session.tid) return json({ kind: "no_agency" } satisfies CrmIdentityAnswer);

  // The allow-list entry of "me" decides the agency header and the timeout, as in the catch-all's forward.
  const route = matchBff("GET", "me");
  if (!route || !cfg.crmApiOrigin) return json({ kind: "unknown" } satisfies CrmIdentityAnswer);
  try {
    const upstream = await fetch(`${cfg.crmApiOrigin}/crm/v1/me`, {
      method: "GET",
      headers: upstreamHeaders(session.at, route.tenant ? session.tid : null, null, randomUUID()),
      cache: "no-store",
      redirect: "manual",
      signal: AbortSignal.timeout(route.timeoutMs ?? UPSTREAM_TIMEOUT_MS),
    });
    if (upstream.status !== 200) return json({ kind: "unknown" } satisfies CrmIdentityAnswer);
    return json(pickIdentity(await upstream.json()));
  } catch {
    return json({ kind: "unknown" } satisfies CrmIdentityAnswer);
  }
}

async function notAllowed(request: NextRequest) {
  if (!onPartnerHost(request)) return json({ detail: "not_found" }, 404);
  const refused = refuseForeignWrite(requestConfig(request), request); // every write route, as the catch-all
  if (refused) return refused;
  const res = json({ detail: "method_not_allowed" }, 405);
  res.headers.set("Allow", "GET");
  return res;
}

export const POST = notAllowed;
export const PUT = notAllowed;
export const PATCH = notAllowed;
export const DELETE = notAllowed;
