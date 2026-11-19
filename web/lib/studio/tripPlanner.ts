// web/lib/studio/tripPlanner.ts
// AA-1139 (fix pass) — the Content studio's Trip planner page (Account > Trip planner, STUDIO_TRIP_PLANNER_PATH), for
// an agency WITHOUT the CRM add-on: it has no CRM agency (CIS creates one only for a live crm subscription), yet its
// leads go by e-mail or webhook, so its owner needs the lead route, the signing key and the delivery status here.
// The studio's route handler (app/api/studio/trip-planner/[[...path]]) answers the same shapes as the CRM's BFF
// routes, so the page reuses the CRM's view (components/settings/TripPlannerModuleView.tsx):
//   GET  ""             {module, usage, webhook_key}: CIS GET /v1/account/tripplanner (the studio token) + the CRM
//                       API GET /crm/v1/studio/trip-planner (the person's partner token; the API asks CIS which studio
//                       membership it holds) when the planner is active
//   GET  "leads"        the CRM API GET /crm/v1/studio/trip-planner/leads
//   PUT  "lead-route" / "appearance"   CIS PUT /v1/account/tripplanner/<x> -> {module}
//   POST "webhook-key"  the CRM API POST /crm/v1/studio/trip-planner/webhook-key (the key, once)
// Who: the owner and editors read, only the owner changes (CIS and the CRM API check again: the studio role from CIS).
// Pure helpers here (tests/studioTripPlanner.test.ts).

export const STUDIO_TRIP_PLANNER_PATH = "/portal/account/trip-planner";
export const STUDIO_TRIP_PLANNER_API = "/api/studio/trip-planner";

export type PlannerAction = "page" | "leads" | "lead-route" | "appearance" | "webhook-key";

/** The action of (method, path segments after /api/studio/trip-planner), or null (404). */
export function plannerAction(method: string, segments: readonly string[]): PlannerAction | null {
  const m = method.toUpperCase();
  const rest = segments.join("/");
  if (m === "GET" && rest === "") return "page";
  if (m === "GET" && rest === "leads") return "leads";
  if (m === "PUT" && (rest === "lead-route" || rest === "appearance")) return rest;
  if (m === "POST" && rest === "webhook-key") return "webhook-key";
  return null;
}

/** May a Content studio role (the verified studio token's csr) take this action? Reads: owner, editor; changes: owner. */
export function studioMay(role: string | null | undefined, action: PlannerAction): boolean {
  if (action === "page" || action === "leads") return role === "owner" || role === "editor";
  return role === "owner";
}

/** The refusal code of a CIS answer: X-Error-Code first, then the body's detail (a string, or {detail} of a refusal);
 * the tenant role middleware's studio_role_forbidden is the module's role_forbidden. */
export function cisDetail(headerCode: string | null, body: unknown): string {
  let code = headerCode ?? "";
  if (!code && body && typeof body === "object") {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string") code = detail;
    else if (detail && typeof detail === "object" && typeof (detail as { detail?: unknown }).detail === "string") {
      code = (detail as { detail: string }).detail;
    }
  }
  if (code === "studio_role_forbidden") return "role_forbidden";
  return /^[a-z][a-z0-9_]{0,63}$/.test(code) ? code : "error";
}

/** The CRM view's relative path ("trip-planner/<x>") as the studio's route handler URL. */
export function studioPlannerUrl(path: string): string {
  const rest = path.replace(/^trip-planner\/?/, "");
  return rest ? `${STUDIO_TRIP_PLANNER_API}/${rest}` : STUDIO_TRIP_PLANNER_API;
}
