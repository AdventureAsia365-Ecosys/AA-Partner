// web/app/api/auth/tenant-select/route.ts
// AA-979 P2b — POST from /portal-access/select (a form): open the Content studio of the agency the person chose. The
// partner session is exchanged at CIS for that tenant; CIS checks the person is an active member of it (403
// tenant_not_member otherwise). Same-origin only (lib/writeGuard.ts) before any cookie is read.
// AA-1036 (R3 slice 1, root ADR 0019 d6): moved into the one agency app; the session is the app's own.
import { NextRequest, NextResponse } from "next/server";
import { requestConfig, type WebConfig } from "@/lib/config";
import { cookieOptions, exchange, partnerRef, partnerSession, safeStudioNext, studioCookies } from "@/lib/studio/tenantSession";
import { refuseForeignWrite } from "@/lib/writeGuard";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function redirect(cfg: WebConfig, path: string, req: NextRequest): NextResponse {
  const res = NextResponse.redirect(new URL(path, cfg.publicOrigin || req.nextUrl.origin), 303);
  res.headers.set("Cache-Control", "no-store");
  return res;
}

export async function POST(req: NextRequest) {
  const cfg = requestConfig(req);
  const refused = refuseForeignWrite(cfg, req);
  if (refused) return refused;
  const partner = partnerSession(cfg, req.cookies);
  const form = await req.formData().catch(() => null);
  const tenantId = String(form?.get("tenant_id") ?? "");
  const next = safeStudioNext(String(form?.get("next") ?? ""));
  if (!partner) {
    return redirect(cfg, `/auth/refresh?next=${encodeURIComponent(`/portal-access/select?next=${encodeURIComponent(next)}`)}`, req);
  }
  if (!UUID.test(tenantId)) return redirect(cfg, `/portal-access/select?next=${encodeURIComponent(next)}`, req);
  const result = cfg.cisApiUrl ? await exchange(cfg.cisApiUrl, partner.at, tenantId) : ({ ok: false, reason: "unavailable" } as const);
  if (!result.ok) {
    const where = result.reason === "no_membership" ? "/portal-access/none" : `/portal-access/select?next=${encodeURIComponent(next)}&error=${result.reason}`;
    return redirect(cfg, where, req);
  }
  const res = redirect(cfg, next, req);
  // AA-1025 (S264): the cookies name the partner sign-in they were exchanged for (proxy.ts / requireStudio check it).
  const ref = await partnerRef(partner.iss, partner.sub);
  for (const c of studioCookies(result.session, ref)) res.cookies.set(c.name, c.value, cookieOptions(c.maxAge, cfg.secureCookies));
  return res;
}
