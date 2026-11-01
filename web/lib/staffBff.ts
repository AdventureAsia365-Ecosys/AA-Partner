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

// ── Console-only calls (review finding B1) ───────────────────────────────────────────────────────────────────────
// Some staff GETs act: opening one release adds to its view count and writes the agency's access log, the manifest
// export is audited and shown to the agency, and every S-04 read is an access-log line. The staff cookie is SameSite=Lax
// (the OAuth callback needs it), so a top-level navigation from another site, or an <img>/<script> on a sibling
// *.lumiguides.it.com host (`it.com` is a public suffix: those hosts are the SAME site), would carry it. The write guard
// (lib/writeGuard.ts) only looks at POST. So EVERY staff BFF call, GET included, must come from the console's own
// fetch: it carries STAFF_CALL_HEADER (lib/staffClient.ts), which a navigation, an <img>, a <form> or a page of another
// origin cannot send (a custom header makes a cross-origin fetch preflight, and this server answers no CORS), and a
// browser that sends Sec-Fetch-Site must say same-origin.

/** The header the console's own fetches carry (lib/staffClient.ts). */
export const STAFF_CALL_HEADER = "x-aa-staff-console";

/** The refusal code for a staff BFF call that is not the console's own same-origin fetch, or null when it may go on. */
export function consoleCallRefusal(headers: { get(name: string): string | null }): "cross_site_refused" | "console_call_required" | null {
  const site = headers.get("sec-fetch-site");
  if (site !== null && site !== "same-origin") return "cross_site_refused";
  if (headers.get(STAFF_CALL_HEADER) !== "1") return "console_call_required";
  return null;
}

/**
 * True when a console PAGE was reached by a navigation from another origin (Sec-Fetch-Site cross-site or same-site).
 * Pages whose render counts a view or writes the agency's access log then show an "Open" step (components/staff/
 * OpenStep.tsx) instead of reading, so a link on another site cannot make a staff member's browser count or audit
 * anything. A link inside the console (same-origin), a typed URL or bookmark (none) and a browser that sends no
 * Sec-Fetch-Site read at once.
 */
export function foreignNavigation(site: string | null): boolean {
  return site === "cross-site" || site === "same-site";
}
