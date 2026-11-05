// web/lib/apps.ts
// AA-1036 (R3, root ADR 0019 d7) — the agency app's app switcher: Content (the base product, the tenant's home) and the
// add-ons (CRM, Trip planner). An add-on the tenant holds opens; any other leads to the studio's Add-ons page
// (Account › Add-ons, ADD_ONS_PATH), where the workspace owner sends a request that Adventure Asia staff approve or
// decline in Tenant 360 (CIS GET /v1/account/add-ons, POST /v1/account/add-on-requests; add-ons open per entitlement
// or after a request to the account manager, never by a second invite). Until R3 the request was a mailto: link.
//
// Where each entry's state comes from:
//   Content       the person's Content studio access (CIS memberships: lib/studioAccess.ts in the CRM; inside the
//                 studio the person is already in it)
//   CRM           the CRM API's GET /crm/v1/me for the studio's tenant: 200 with crm_entitled (the projection of the
//                 tenant's crm.core entitlement, tenant.entitlements.v1) -> open; 403 / 409 / 400 (the person is no
//                 CRM member of that agency, or it has no CRM agency) or not entitled -> the CIS add-on state
//                 (requested / active / request); anything else -> unknown, and the entry stays a link (the CRM says
//                 what is wrong), as the studio link does
//   Trip planner  the CIS add-on state only (no tp.* entitlement reaches this app): active / requested / request
// Pure functions here; the fetches are crmAccess() and cisAddOns(). Links between apps are plain <a> (a full page load:
// the studio's stylesheet never stays on a CRM page).

import type { WebConfig } from "./config";
import { fetchMe } from "./crmApi";
import { CONTENT_STUDIO_PATH, CRM_HOME_PATH } from "./nav";
import type { Session } from "./session";

export type AppKey = "content" | "crm" | "planner";
export type CrmAccess = "entitled" | "not_entitled" | "not_member" | "unknown";
/** The CIS add-on state of one product (GET /v1/account/add-ons), or unknown when CIS did not answer. */
export type AddOnState = "active" | "requested" | "available" | "unknown";
export interface AddOnStates { crm: AddOnState; planner: AddOnState }
export const UNKNOWN_ADD_ONS: AddOnStates = { crm: "unknown", planner: "unknown" };

/** The studio page where the owner asks for an add-on and everyone sees its state. */
export const ADD_ONS_PATH = "/portal/account/add-ons";

export interface AppEntry {
  key: AppKey;
  label: string;
  /** open: a link to the app; current: the app the person is in; request: not on yet (the Add-ons page asks for it);
   *  requested: a request waits for Adventure Asia; active: on for the agency, nothing to open here (the Add-ons page). */
  state: "open" | "request" | "requested" | "active" | "current";
  href: string;
}

export const APP_LABELS: Record<AppKey, string> = { content: "Content studio", crm: "CRM", planner: "Trip planner" };

/** Does the person hold a CRM membership (a crm.member role) in the studio's agency? GET /crm/v1/me answered 200
 *  (entitled or not); not_member and unknown say no (the Add-ons page then offers no "Open CRM" link). */
export function crmMember(access: CrmAccess): boolean {
  return access === "entitled" || access === "not_entitled";
}

/** The CRM entry's state from the API's answer to GET /crm/v1/me (status + body). */
export function crmAccessFrom(status: number, body: { crm_entitled?: unknown } | null): CrmAccess {
  if (status === 200) return body && body.crm_entitled === false ? "not_entitled" : "entitled";
  if (status === 403 || status === 409 || status === 400) return "not_member";
  return "unknown";
}

/** The add-on states from CIS GET /v1/account/add-ons (status + body); anything unexpected is unknown. */
export function addOnStatesFrom(status: number, body: unknown): AddOnStates {
  if (status !== 200 || !body || typeof body !== "object") return UNKNOWN_ADD_ONS;
  const list = (body as { add_ons?: unknown }).add_ons;
  if (!Array.isArray(list)) return UNKNOWN_ADD_ONS;
  const of = (product: string): AddOnState => {
    const hit = list.find((a) => a && typeof a === "object" && (a as { product?: unknown }).product === product) as
      | { state?: unknown }
      | undefined;
    const s = hit?.state;
    return s === "active" || s === "requested" || s === "available" ? s : "unknown";
  };
  return { crm: of("crm"), planner: of("tripplanner") };
}

/** A not-open add-on's entry: active, requested, or "request" (available or unknown), always to the Add-ons page. */
function addOnEntry(key: "crm" | "planner", state: AddOnState): AppEntry {
  const shown = state === "active" ? "active" : state === "requested" ? "requested" : "request";
  return { key, label: APP_LABELS[key], state: shown, href: ADD_ONS_PATH };
}

/** The switcher as the Content studio shows it (the person is in the studio). */
export function studioApps(crm: CrmAccess, addOns: AddOnStates = UNKNOWN_ADD_ONS): AppEntry[] {
  const crmOpen = crm === "entitled" || crm === "unknown";
  return [
    { key: "content", label: APP_LABELS.content, state: "current", href: CONTENT_STUDIO_PATH },
    crmOpen ? { key: "crm", label: APP_LABELS.crm, state: "open", href: CRM_HOME_PATH } : addOnEntry("crm", addOns.crm),
    addOnEntry("planner", addOns.planner),
  ];
}

/** The switcher as the CRM shows it: Content when the person may open it (null = no studio link), the CRM itself, and
 *  the Trip planner through the studio's Add-ons page (only with studio access: the request is the studio owner's). */
export function crmApps(contentHref: string | null): AppEntry[] {
  const apps: AppEntry[] = [];
  if (contentHref) apps.push({ key: "content", label: APP_LABELS.content, state: "open", href: contentHref });
  apps.push({ key: "crm", label: APP_LABELS.crm, state: "current", href: CRM_HOME_PATH });
  const planner = plannerHref(contentHref);
  if (planner) apps.push({ ...addOnEntry("planner", "unknown"), href: planner });
  return apps;
}

/** The CRM's link to the Trip planner entry: the studio's Add-ons page when the person may open the studio. On the
 *  studio's own origin when the studio link is absolute (the studio on another host: a relative path would land on the
 *  CRM's host); a relative path when the studio shares this origin. */
export function plannerHref(contentHref: string | null): string | null {
  if (!contentHref) return null;
  if (!/^[a-z][a-z0-9+.-]*:/i.test(contentHref)) return ADD_ONS_PATH;
  try {
    return new URL(ADD_ONS_PATH, contentHref).toString();
  } catch {
    return null;
  }
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

/** The add-on states of the studio's tenant: CIS GET /v1/account/add-ons with the verified studio token. Never throws. */
export async function cisAddOns(cisApiUrl: string, token: string, fetchImpl: typeof fetch = fetch): Promise<AddOnStates> {
  if (!cisApiUrl || !token) return UNKNOWN_ADD_ONS;
  try {
    const res = await fetchImpl(`${cisApiUrl}/v1/account/add-ons`, {
      headers: { Authorization: `Bearer ${token}`, Accept: "application/json" },
      cache: "no-store",
      signal: AbortSignal.timeout(3000),
    });
    return addOnStatesFrom(res.status, res.ok ? await res.json() : null);
  } catch {
    return UNKNOWN_ADD_ONS;
  }
}
