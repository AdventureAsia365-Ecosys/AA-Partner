// web/lib/staffBff.ts
// AA-1019 — the staff BFF allow-list. The staff console's browser code calls /staff/bff/<path> on its own origin; the
// handler (app/staff/bff/[...path]/route.ts) forwards ONLY the (method, path) pairs listed here, and only onto
// /crm/staff/v1/<path> (never /crm/v1 or /crm/ops/v1), with the STAFF session's access token. Each pair names the
// WORKFORCE groups that may use it; the handler refuses a session without one of them (403 missing_group) before any
// upstream call. The API checks the token class and the group again on every call (root ADR 0006b decision 6).
//
// Server components use the same list (lib/staffApi.ts). Adding a browser call = adding one line here.

import type { StaffGroup } from "./staffSession";

export type StaffMethod = "GET" | "POST";

export interface StaffBffRoute {
  method: StaffMethod;
  /** Matched against the encoded sub-path after /staff/bff/ (lib/bffPath.ts proxyPath). Anchored. */
  pattern: RegExp;
  /** The WORKFORCE groups that may call it (any one). */
  groups: readonly StaffGroup[];
  /** Card that added it (review trail). */
  card: string;
}

const UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";
const OPS: readonly StaffGroup[] = ["crm-ops"];
const SUPPORT: readonly StaffGroup[] = ["crm-support"];
const ANY: readonly StaffGroup[] = ["crm-ops", "crm-support"];

/** The upstream prefix: the staff route class only. */
export const STAFF_API_PREFIX = "/crm/staff/v1";

export const STAFF_BFF_ROUTES: readonly StaffBffRoute[] = [
  // S-02 ops release (AA-956 API): the board, one release (counted and audited), the watermarked manifest.
  { method: "GET", pattern: /^ops-releases$/, groups: OPS, card: "AA-1019" },
  { method: "GET", pattern: new RegExp(`^agencies/${UUID}/trips/${UUID}/ops-release$`), groups: OPS, card: "AA-1019" },
  { method: "GET", pattern: new RegExp(`^agencies/${UUID}/trips/${UUID}/ops-release/manifest$`), groups: OPS, card: "AA-1019" },
  // S-04 support (L2) and break-glass (L3) sessions (AA-971 API).
  { method: "GET", pattern: /^support\/agencies$/, groups: SUPPORT, card: "AA-1019" },
  { method: "POST", pattern: /^support\/sessions$/, groups: SUPPORT, card: "AA-1019" },
  { method: "POST", pattern: /^support\/sessions\/end$/, groups: ANY, card: "AA-1019" },
  { method: "GET", pattern: /^breakglass\/agencies$/, groups: OPS, card: "AA-1019" },
  { method: "POST", pattern: /^breakglass\/sessions$/, groups: OPS, card: "AA-1019" },
  { method: "POST", pattern: /^breakglass\/sessions\/end$/, groups: ANY, card: "AA-1019" },
  { method: "GET", pattern: new RegExp(`^agencies/${UUID}/(session|overview|trips)$`), groups: ANY, card: "AA-1019" },
  { method: "GET", pattern: new RegExp(`^agencies/${UUID}/trips/${UUID}$`), groups: ANY, card: "AA-1019" },
];

export function matchStaffBff(method: string, subPath: string): StaffBffRoute | null {
  const m = method.toUpperCase();
  return STAFF_BFF_ROUTES.find((r) => r.method === m && r.pattern.test(subPath)) ?? null;
}

/** Whether the session's groups allow the route (the API decides again; this keeps a call from being sent at all). */
export function groupAllowed(route: StaffBffRoute, groups: readonly string[]): boolean {
  return route.groups.some((g) => groups.includes(g));
}

/** Query parameters a staff call may carry, per path (everything else is dropped, never forwarded). */
export function allowedQuery(subPath: string, search: URLSearchParams): URLSearchParams {
  const out = new URLSearchParams();
  const keep = (name: string, valid: (v: string) => boolean) => {
    for (const v of search.getAll(name)) if (valid(v)) out.append(name, v);
  };
  if (subPath === "ops-releases") keep("days", (v) => /^\d{1,3}$/.test(v));
  if (subPath.endsWith("/ops-release/manifest")) keep("fields", (v) => /^[a-z_]{1,32}$/.test(v));
  if (/\/trips$/.test(subPath)) {
    keep("stage", (v) => /^[a-z_]{1,20}$/.test(v));
    keep("cursor", (v) => /^[A-Za-z0-9_.:+|-]{1,80}$/.test(v));
    keep("limit", (v) => /^\d{1,3}$/.test(v));
  }
  return out;
}

/** The upstream request headers: the staff token only (no tenant header: the path names the agency). */
export function staffUpstreamHeaders(accessToken: string, requestId: string, json: boolean): Headers {
  const h = new Headers({ Accept: "application/json", Authorization: `Bearer ${accessToken}`, "X-Request-Id": requestId });
  if (json) h.set("Content-Type", "application/json");
  return h;
}
