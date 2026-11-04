// GET /api/studio/apps — AA-1036 (R3 slice 1, root ADR 0019 d7): the Content studio's app switcher (lib/apps.ts). The
// studio's tenant comes from its own httpOnly cookie cis_tenant_id (set by the server after the CIS exchange), never
// from the browser; the CRM API checks the person's membership in that agency again (tenant_tx). Display only, so it
// ALWAYS answers 200 (a 4xx would log a console error on every studio page, AA-1025): {apps: AppEntry[]}.
import type { NextRequest } from "next/server";
import { crmAccess, studioApps, type CrmAccess } from "@/lib/apps";
import { requestConfig } from "@/lib/config";
import { json } from "@/lib/respond";
import { accessValid, readSession } from "@/lib/session";

export const dynamic = "force-dynamic";

const NAME_COOKIE = "cis_tenant_name";

export async function GET(request: NextRequest) {
  const cfg = requestConfig(request);
  const session = cfg.authConfigured ? readSession(cfg, request.cookies) : null;
  const tenantId = request.cookies.get("cis_tenant_id")?.value ?? null;
  const name = request.cookies.get(NAME_COOKIE)?.value ?? null;
  let crm: CrmAccess = "unknown";
  if (accessValid(session)) crm = await crmAccess(cfg, session, tenantId);
  return json({ apps: studioApps(crm, name) });
}
