// web/lib/studio/tenantRoutes.ts
// AA-1188 (S295 data-boundary audit, DB-01) — the allow-list of the studio BFF proxy (app/api/tenant/[...path]/route.ts).
//
// The proxy forwards a Content studio call to CIS with the member's studio token. Before AA-1188 it forwarded ANY path,
// so a member could call, from devtools, a CIS tenant route the studio never uses (for example GET
// /v1/tours/{id}/full, which answers the raw source row: supplier name, source price text, pipeline state). Internal
// AA data never reaches an agency, so the proxy now forwards ONLY the (method, path) pairs the studio pages call; any
// other path is 404 before the studio token is read or CIS is called. Same idea as the CRM BFF (lib/bff.ts).
//
// Adding a studio call to CIS = adding one line here (reviewed with the call). tests/studio/tenantRoutes.test.ts scans
// the web sources for every `/api/tenant/...` literal and fails when one is not listed (or the list keeps a dead line).
// GET /api/tenant/me is its own route (app/api/tenant/me/route.ts) and never reaches the proxy.
//
// Edge-safe: no Node APIs.

export type TenantMethod = "GET" | "POST" | "PATCH" | "DELETE";

export interface TenantRoute {
  method: TenantMethod;
  /** Matched against the encoded CIS sub-path (lib/bffPath.ts proxyPath: no query, no dot or slash segment). Anchored. */
  pattern: RegExp;
}

/** One path parameter: one encoded segment (proxyPath already refused "", ".", ".." and slash-carrying ones). */
const ID = "[^/]+";
const CHANNEL = "(wordpress|facebook)";

const r = (method: TenantMethod, path: string): TenantRoute => ({ method, pattern: new RegExp(`^${path}$`) });

export const TENANT_ROUTES: readonly TenantRoute[] = [
  // Shell, dashboard, account pages (useShellData, DashboardTab, PlaceholderTabs, Account*).
  r("GET", "v1/billing"),
  r("GET", "v1/quota"),
  r("GET", "v1/notifications"),
  r("POST", "v1/notifications/read-all"),
  r("GET", "v1/account/members"),
  r("GET", "v1/account/add-ons"),
  r("POST", "v1/account/add-on-requests"),
  r("GET", "v1/account/activity"),
  r("GET", "v1/account/activity/export"),
  r("GET", "v1/account/tour-retirement"),
  r("PATCH", "v1/account/tour-retirement"),
  // Brand voice (BrandTab, GettingStarted) and the agency's own facts (BrandFacts, lib/studio/facts.ts).
  r("GET", "admin/brand-identity"),
  r("POST", "admin/brand-identity"),
  r("POST", "admin/brand-identity/upload"),
  r("POST", "admin/brand-identity/parse"),
  r("GET", "v1/facts"),
  r("POST", "v1/facts"),
  r("PATCH", `v1/facts/${ID}`),
  r("DELETE", `v1/facts/${ID}`),
  // Tour catalogue: the pool (marketplace, command palette), the agency's own versions (catalogApi, CatalogTab).
  // Never GET v1/tours/{id} or v1/tours/{id}/full: the studio reads tours only through the pool and its versions.
  r("GET", "v1/tours/pool"),
  r("POST", `v1/tours/pool/${ID}/rewrite`),
  r("GET", "v1/marketplace"),
  r("GET", "v1/tours/my-versions"),
  r("GET", `v1/tours/versions/${ID}`),
  r("PATCH", `v1/tours/versions/${ID}`),
  r("POST", `v1/tours/versions/${ID}/retry`),
  r("GET", `v1/tours/versions/${ID}/export-docx`),
  r("GET", `v1/progress/(tour|piece)/${ID}`),
  // Content writing: angle gate, write, review, export (AngleGateWizard, WriterPanel, reviewListApi, CtaReviewNotice).
  r("GET", "v1/angle-gate/goals"),
  r("GET", `v1/angle-gate/requests/${ID}`),
  r("POST", `v1/angle-gate/requests/${ID}/(goal|choose|reopen)`),
  r("POST", `v1/content-writing/requests/${ID}/write`),
  r("GET", `v1/content-writing/requests/${ID}/latest-piece`),
  r("GET", "v1/content-writing/reviews"),
  r("GET", `v1/content-writing/pieces/${ID}`),
  r("PATCH", `v1/content-writing/pieces/${ID}`),
  r("GET", `v1/content-writing/pieces/${ID}/export`),
  r("GET", "v1/content-writing/ai-disclosure"),
  r("POST", "v1/content-writing/cta-check"),
  // Publishing (Publish*List, PublishedList, PutBackList, RetiredPagesList).
  r("GET", "v1/publish-log"),
  r("GET", "v1/publish-log/pending"),
  r("GET", "v1/publish-log/put-back"),
  r("GET", "v1/publish-log/retired-pages"),
  r("POST", `v1/publish-log/${ID}/(publish|put-back|move-to-draft)`),
  r("DELETE", `v1/publish-log/${ID}`),
  // Channels (WordPressConnect, FacebookConnect, DisconnectConnection, GettingStarted).
  r("GET", `v1/integrations/${CHANNEL}`),
  r("POST", `v1/integrations/${CHANNEL}`),
  r("DELETE", `v1/integrations/${CHANNEL}`),
  r("POST", `v1/integrations/${CHANNEL}/test`),
  // Planning: the slate and the trip reallocation (SlateTab, PlanningTab).
  r("GET", "v1/slate"),
  r("POST", "v1/slate/refresh"),
  r("POST", `v1/subjects/${ID}/(pick|cut)`),
  r("GET", "v1/planning/trip-reallocation/suggest"),
  r("POST", "v1/planning/trip-reallocation/confirm"),
];

/** The listed route for this call, or null (the proxy answers 404 and never calls CIS). `subPath` is the encoded CIS
 *  sub-path from proxyPath (no leading slash, no query). */
export function matchTenantRoute(
  method: string, subPath: string, routes: readonly TenantRoute[] = TENANT_ROUTES,
): TenantRoute | null {
  const m = method.toUpperCase();
  return routes.find((route) => route.method === m && route.pattern.test(subPath)) ?? null;
}
