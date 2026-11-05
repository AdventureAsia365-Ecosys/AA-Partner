// web/lib/addOns.ts
// AA-1036 (R3, root ADR 0019 d7) — the CRM's Settings > Add-ons page (CRM_ADD_ONS_PATH): an agency owner who works in
// the CRM without a Content studio membership sees the agency's add-ons and asks Adventure Asia for one. Found live on
// Dev (S269): the CRM's "Trip planner: request add-on" entry led a CRM-only owner to the studio's "No Content studio
// access" page. The CRM API relays to CIS tenant.addons.v1 (GET /crm/v1/add-ons, POST /crm/v1/add-on-requests, owner
// only), so the requests keep one store in CIS and the studio's Account › Add-ons page shows the same ones.
// Pure helpers here; the page is app/(crm)/settings/add-ons, the view components/settings/AddOnsView.tsx.

import type { MessageKey } from "./i18n";

/** The CRM's own Add-ons page (the sidebar's Trip planner entry and Settings link here). */
export const CRM_ADD_ONS_PATH = "/settings/add-ons";

export type AddOnProduct = "crm" | "tripplanner";

export interface AddOnRequest {
  request_id: string;
  product: AddOnProduct;
  status: "pending" | "approved" | "declined";
  note: string | null;
  requested_at: string;
  decided_at: string | null;
  decision_note: string | null;
  setup: "in_progress" | "done" | "failed" | null;
}

export interface AddOn {
  product: AddOnProduct;
  state: "active" | "requested" | "available";
  subscription_status: "trialing" | "active" | "past_due" | null;
  request: AddOnRequest | null;
  last_decision: AddOnRequest | null;
}

export interface AddOnList {
  in_service: boolean;
  add_ons: AddOn[];
}

/** The Trip planner first: it is what a CRM agency asks for; the CRM (on for a CRM agency) after it. */
export function orderAddOns(list: AddOn[]): AddOn[] {
  const rank = (p: AddOnProduct) => (p === "tripplanner" ? 0 : 1);
  return [...list].sort((a, b) => rank(a.product) - rank(b.product));
}

/** The page's message when the list cannot be shown (GET /crm/v1/add-ons answered status + detail). */
export function loadErrorKey(status: number, detail: string): MessageKey {
  if (status === 403) return "addons.ownerOnly";
  if (status === 404 && detail === "addons_no_account") return "addons.err.noAccount";
  return "addons.err.load";
}

/** The sentence for a refused request (POST /crm/v1/add-on-requests answered status + detail, CIS's reason). */
export function refusalKey(status: number, detail: string): MessageKey {
  if (status === 403) return "addons.err.ownerOnly";
  if (status === 409 && detail === "tenant_not_in_service") return "addons.err.notInService";
  if (status === 409 && detail === "already_subscribed") return "addons.err.alreadyOn";
  if (status === 429 && detail === "too_many_requests") return "addons.err.tooMany";
  if (status === 404 && detail === "addons_no_account") return "addons.err.noAccount";
  return "addons.err.send";
}

/** The sentence of the last decision: a decline, an approval being set up, an approval whose setup failed, or done. */
export function decisionKey(last: AddOnRequest): MessageKey {
  if (last.status === "declined") return "addons.declined";
  if (last.setup === "failed") return "addons.approvedSetupFailed";
  if (last.setup === "in_progress") return "addons.approvedSettingUp";
  return "addons.approved";
}

/** The date a decision sentence names: a decline names its request's date (as the studio does), an approval its own. */
export function decisionDate(last: AddOnRequest): string {
  return last.status === "declined" ? last.requested_at : (last.decided_at ?? last.requested_at);
}
