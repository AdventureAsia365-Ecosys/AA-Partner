// portal/app/api/auth/tenant-select/route.ts
// AA-979 P2b — POST from /portal-access/select (a form): open the Content studio of the agency the person chose. The
// partner session (the CRM web's cookie, read only) is exchanged at CIS for that tenant; CIS checks the person is an
// active member of it (403 tenant_not_member otherwise). Same-origin only (lib/writeGuard.ts) before any cookie is read.
import { NextRequest, NextResponse } from "next/server";
import { API_URL } from "@/lib/auth-server";
import { config } from "@/lib/config";
import { cookieOptions, exchange, partnerSession, safeStudioNext, studioCookies } from "@/lib/tenantSession";
import { publicUrl, refuseForeignWrite } from "@/lib/writeGuard";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function redirect(path: string, req: NextRequest): NextResponse {
  const res = NextResponse.redirect(publicUrl(path, req.url), 303);
  res.headers.set("Cache-Control", "no-store");
  return res;
}

export async function POST(req: NextRequest) {
  const refused = refuseForeignWrite(req);
  if (refused) return refused;
  const cfg = config();
  const partner = partnerSession(cfg, req.cookies);
  const form = await req.formData().catch(() => null);
  const tenantId = String(form?.get("tenant_id") ?? "");
  const next = safeStudioNext(String(form?.get("next") ?? ""));
  if (!partner) {
    return redirect(`${cfg.signInPath}?next=${encodeURIComponent(`/portal-access/select?next=${encodeURIComponent(next)}`)}`, req);
  }
  if (!UUID.test(tenantId)) return redirect(`/portal-access/select?next=${encodeURIComponent(next)}`, req);
  const result = await exchange(API_URL, partner.at, tenantId);
  if (!result.ok) {
    const where = result.reason === "no_membership" ? "/portal-access/none" : `/portal-access/select?next=${encodeURIComponent(next)}&error=${result.reason}`;
    return redirect(where, req);
  }
  const res = redirect(next, req);
  for (const c of studioCookies(result.session)) res.cookies.set(c.name, c.value, cookieOptions(c.maxAge));
  return res;
}
