// web/lib/apps.ts
// AA-1036 (R3 slice 1, root ADR 0019 d7) — the agency app's app switcher: Content (the base product, the tenant's home)
// and the add-ons (CRM, Trip planner). An add-on the tenant holds opens; any other shows "Request add-on", a message to
// Adventure Asia (add-ons open per entitlement or after a request to the account manager, never by a second invite).
//
// Where each entry's state comes from:
//   Content       the person's Content studio access (CIS memberships: lib/studioAccess.ts in the CRM; inside the
//                 studio the person is already in it)
//   CRM           the CRM API's GET /crm/v1/me for the studio's tenant: 200 with crm_entitled (the projection of the
//                 tenant's crm.core entitlement, tenant.entitlements.v1) -> open / request; 403 / 409 / 400 (the
//                 person is no CRM member of that agency, or it has no CRM agency) -> request; anything else ->
//                 unknown, and the entry stays a link (the CRM says what is wrong), as the studio link does
//   Trip planner  no entitlement reaches this app yet (tp.* is not projected into the CRM): always "Request add-on"
// Pure functions here; the fetch is crmAccess(). Links between apps are plain <a> (a full page load: the studio's
// stylesheet never stays on a CRM page).

import { SUPPORT_EMAIL } from "@/app/_kit/core/brand";
import type { WebConfig } from "./config";
import { fetchMe } from "./crmApi";
import { CONTENT_STUDIO_PATH, CRM_HOME_PATH } from "./nav";
import type { Session } from "./session";

export type AppKey = "content" | "crm" | "planner";
export type CrmAccess = "entitled" | "not_entitled" | "not_member" | "unknown";

export interface AppEntry {
  key: AppKey;
  label: string;
  /** open: a link to the app; request: the "Request add-on" action; current: the app the person is in. */
  state: "open" | "request" | "current";
  href: string;
}

export const APP_LABELS: Record<AppKey, string> = { content: "Content studio", crm: "CRM", planner: "Trip planner" };

/** The "Request add-on" action: a message to Adventure Asia naming the add-on and, when known, the agency. */
export function requestHref(app: AppKey, agencyName?: string | null): string {
  const label = APP_LABELS[app];
  const subject = `Add-on request: ${label}`;
  const name = (agencyName ?? "").trim().slice(0, 120);
  const body = name ? `Please add ${label} for ${name}.` : `Please add ${label} for our agency.`;
  return `mailto:${SUPPORT_EMAIL}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
}

/** The CRM entry's state from the API's answer to GET /crm/v1/me (status + body). */
export function crmAccessFrom(status: number, body: { crm_entitled?: unknown } | null): CrmAccess {
  if (status === 200) return body && body.crm_entitled === false ? "not_entitled" : "entitled";
  if (status === 403 || status === 409 || status === 400) return "not_member";
  return "unknown";
}

/** The switcher as the Content studio shows it (the person is in the studio). */
export function studioApps(crm: CrmAccess, agencyName?: string | null): AppEntry[] {
  const crmOpen = crm === "entitled" || crm === "unknown";
  return [
    { key: "content", label: APP_LABELS.content, state: "current", href: CONTENT_STUDIO_PATH },
    { key: "crm", label: APP_LABELS.crm, state: crmOpen ? "open" : "request", href: crmOpen ? CRM_HOME_PATH : requestHref("crm", agencyName) },
    { key: "planner", label: APP_LABELS.planner, state: "request", href: requestHref("planner", agencyName) },
  ];
}

/** The switcher as the CRM shows it: Content when the person may open it (null = no studio link), the CRM itself. */
export function crmApps(contentHref: string | null, agencyName?: string | null): AppEntry[] {
  const apps: AppEntry[] = [];
  if (contentHref) apps.push({ key: "content", label: APP_LABELS.content, state: "open", href: contentHref });
  apps.push({ key: "crm", label: APP_LABELS.crm, state: "current", href: CRM_HOME_PATH });
  apps.push({ key: "planner", label: APP_LABELS.planner, state: "request", href: requestHref("planner", agencyName) });
  return apps;
}

/** The CRM entry for one tenant (the studio's): GET /crm/v1/me with that tenant as the agency. Never throws. */
export async function crmAccess(cfg: WebConfig, session: Session, tenantId: string | null, fetchImpl?: typeof fetch): Promise<CrmAccess> {
  if (!tenantId || !cfg.crmApiOrigin) return "unknown";
  try {
    const me = await fetchMe(cfg, { ...session, tid: tenantId }, fetchImpl);
    return me.ok ? crmAccessFrom(200, me.data) : crmAccessFrom(me.status, null);
  } catch {
    return "unknown";
  }
}
