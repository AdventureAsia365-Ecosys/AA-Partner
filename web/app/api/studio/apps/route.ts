// GET /api/studio/apps — AA-1036 (R3, root ADR 0019 d7): the Content studio's app switcher (lib/apps.ts). The studio's
// tenant comes from its own httpOnly cookie cis_tenant_id (set by the server after the CIS exchange), never from the
// browser; the CRM API checks the person's membership in that agency again (tenant_tx). The add-on states (active,
// requested, available) come from CIS GET /v1/account/add-ons with the verified studio token (lib/studio/auth-server.ts
// requireStudio; a renewed token's cookies are set on this answer). Display only, so it ALWAYS answers 200 (a 4xx would
// log a console error on every studio page, AA-1025): {apps: AppEntry[], crm_member: boolean}. crm_member: the person
// holds a CRM membership in the agency (GET /crm/v1/me answered 200); the Add-ons page shows "Open CRM" only then.
import type { NextRequest } from "next/server";
import { cisAddOns, crmAccess, crmMember, studioApps, UNKNOWN_ADD_ONS, type AddOnStates, type CrmAccess } from "@/lib/apps";
import { requestConfig } from "@/lib/config";
import { json } from "@/lib/respond";
import { accessValid, readSession } from "@/lib/session";
import { requireStudio, withRenewed } from "@/lib/studio/auth-server";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const cfg = requestConfig(request);
  const session = cfg.authConfigured ? readSession(cfg, request.cookies) : null;
  const tenantId = request.cookies.get("cis_tenant_id")?.value ?? null;
  let crm: CrmAccess = "unknown";
  let addOns: AddOnStates = UNKNOWN_ADD_ONS;
  if (!accessValid(session)) return json({ apps: studioApps(crm, addOns), crm_member: false });
  const [access, studio] = await Promise.all([crmAccess(cfg, session, tenantId), requireStudio(request)]);
  crm = access;
  if (studio.ok) addOns = await cisAddOns(studio.cfg.cisApiUrl, studio.token);
  const res = json({ apps: studioApps(crm, addOns), crm_member: crmMember(crm) });
  return studio.ok ? withRenewed(res, studio.renewed, studio.cfg) : res;
}
