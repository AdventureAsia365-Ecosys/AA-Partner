// web/lib/bff.ts
// AA-935 — the BFF allow-list (card CRM-07 "BFF allow-list"). The browser never talks to the CRM API: it calls
// /bff/<path> on its own origin, and app/bff/[...path]/route.ts forwards ONLY the (method, path) pairs listed here
// to /crm/v1/<path>, with the session's access token and the session's agency (X-AA-Tenant-Id taken from the
// sealed session, never from a client header). Anything else is 404 before a token is read.
//
// Adding an API route to the browser = adding one line here (reviewed with the route). Server components use the
// same list (lib/crmApi.ts), so there is one place that says what the web may call.

export type Method = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

export interface BffRoute {
  method: Method;
  /** Matched against the encoded sub-path after /bff/ (lib/bffPath.ts proxyPath). Anchored. */
  pattern: RegExp;
  /** The call needs the agency picked in the session (sent as X-AA-Tenant-Id). */
  tenant: boolean;
  /** Card that added it (review trail). */
  card: string;
  /** Largest request body the BFF forwards for this route (default 256 KiB, app/bff/[...path]/route.ts). */
  maxBody?: number;
}

/** AA-937: a document upload is base64 JSON of at most 7 MiB (~9.4 MiB), under the proxy's 10 MiB body clone limit. */
export const DOCUMENT_BODY_BYTES = 10 * 1024 * 1024;

const UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";

export const BFF_ROUTES: readonly BffRoute[] = [
  { method: "GET", pattern: /^me$/, tenant: true, card: "AA-933/AA-935" },
  { method: "GET", pattern: /^memberships$/, tenant: false, card: "AA-935" },
  // AA-936 (CRM-08 Slice 1): board, trip workspace, moves, assignment, notes, tasks, contact reveal, forms.
  { method: "GET", pattern: /^pipeline$/, tenant: true, card: "AA-936" },
  { method: "GET", pattern: new RegExp(`^trips/${UUID}$`), tenant: true, card: "AA-936" },
  { method: "PATCH", pattern: new RegExp(`^trips/${UUID}$`), tenant: true, card: "AA-936" },
  { method: "POST", pattern: new RegExp(`^trips/${UUID}/(stage|owner|notes)$`), tenant: true, card: "AA-936" },
  { method: "POST", pattern: new RegExp(`^tasks/${UUID}/status$`), tenant: true, card: "AA-936" },
  // AA-937 (CRM-09): the reveal now carries a reason code (same path as AA-936).
  { method: "POST", pattern: new RegExp(`^travelers/${UUID}/reveal$`), tenant: true, card: "AA-936/AA-937" },
  { method: "GET", pattern: /^members$/, tenant: true, card: "AA-936" },
  { method: "GET", pattern: /^intake-forms$/, tenant: true, card: "AA-936" },
  { method: "POST", pattern: /^intake-forms$/, tenant: true, card: "AA-936" },
  { method: "PATCH", pattern: new RegExp(`^intake-forms/${UUID}$`), tenant: true, card: "AA-936" },
  // AA-937 (CRM-09): clients (A-06 / A-07), the party of a trip, documents.
  { method: "GET", pattern: /^travelers$/, tenant: true, card: "AA-937" },
  { method: "POST", pattern: /^travelers$/, tenant: true, card: "AA-937" },
  { method: "POST", pattern: /^travelers\/search$/, tenant: true, card: "AA-937" },
  { method: "GET", pattern: new RegExp(`^travelers/${UUID}$`), tenant: true, card: "AA-937" },
  { method: "PATCH", pattern: new RegExp(`^travelers/${UUID}(/profile)?$`), tenant: true, card: "AA-937" },
  { method: "PUT", pattern: new RegExp(`^travelers/${UUID}/health$`), tenant: true, card: "AA-937" },
  { method: "POST", pattern: new RegExp(`^travelers/${UUID}/relations$`), tenant: true, card: "AA-937" },
  { method: "DELETE", pattern: new RegExp(`^travelers/${UUID}/relations/${UUID}$`), tenant: true, card: "AA-937" },
  { method: "POST", pattern: new RegExp(`^trips/${UUID}/travelers$`), tenant: true, card: "AA-937" },
  { method: "DELETE", pattern: new RegExp(`^trips/${UUID}/travelers/${UUID}$`), tenant: true, card: "AA-937" },
  { method: "GET", pattern: new RegExp(`^trips/${UUID}/documents$`), tenant: true, card: "AA-937" },
  { method: "POST", pattern: /^documents$/, tenant: true, card: "AA-937", maxBody: DOCUMENT_BODY_BYTES },
  { method: "POST", pattern: new RegExp(`^documents/${UUID}/download$`), tenant: true, card: "AA-937" },
  { method: "PATCH", pattern: new RegExp(`^documents/${UUID}$`), tenant: true, card: "AA-937" },
  { method: "DELETE", pattern: new RegExp(`^documents/${UUID}$`), tenant: true, card: "AA-937" },
  // AA-940 (CRM-12): the access log (owner only, the API checks) and its download.
  { method: "GET", pattern: /^audit\/events$/, tenant: true, card: "AA-940" },
  { method: "GET", pattern: /^audit\/events\/export$/, tenant: true, card: "AA-940" },
  // AA-939 (CRM-11): Team & roles, plan, owner recovery banner; the invitation accept (no agency yet: the token decides).
  { method: "GET", pattern: /^team$/, tenant: true, card: "AA-939" },
  { method: "POST", pattern: /^team\/invitations$/, tenant: true, card: "AA-939" },
  { method: "DELETE", pattern: new RegExp(`^team/invitations/${UUID}$`), tenant: true, card: "AA-939" },
  { method: "PATCH", pattern: new RegExp(`^team/members/${UUID}$`), tenant: true, card: "AA-939" },
  { method: "POST", pattern: new RegExp(`^team/members/${UUID}/remove$`), tenant: true, card: "AA-939" },
  { method: "GET", pattern: /^team\/reassign-queue$/, tenant: true, card: "AA-939" },
  { method: "POST", pattern: /^team\/reassign$/, tenant: true, card: "AA-939" },
  { method: "GET", pattern: /^plan$/, tenant: true, card: "AA-939" },
  { method: "GET", pattern: /^recovery$/, tenant: true, card: "AA-939" },
  { method: "POST", pattern: new RegExp(`^recovery/${UUID}/cancel$`), tenant: true, card: "AA-939" },
  { method: "POST", pattern: /^invitations\/accept$/, tenant: false, card: "AA-939" },
  // AA-942 (CRM-14): data lifecycle — rules and settings, legal holds, the trash, delete / restore, archive, client
  // erasure, agency offboarding (export download, erase). Owner-only checks are the API's.
  { method: "GET", pattern: /^lifecycle$/, tenant: true, card: "AA-942" },
  { method: "PATCH", pattern: /^lifecycle\/settings$/, tenant: true, card: "AA-942" },
  { method: "POST", pattern: /^lifecycle\/holds$/, tenant: true, card: "AA-942" },
  { method: "POST", pattern: new RegExp(`^lifecycle/holds/${UUID}/release$`), tenant: true, card: "AA-942" },
  { method: "GET", pattern: /^trash$/, tenant: true, card: "AA-942" },
  { method: "POST", pattern: new RegExp(`^trash/(trip|traveler|task|note|document)/${UUID}/restore$`), tenant: true, card: "AA-942" },
  { method: "DELETE", pattern: new RegExp(`^(trips|travelers|tasks|notes)/${UUID}$`), tenant: true, card: "AA-942" },
  { method: "POST", pattern: new RegExp(`^travelers/${UUID}/(archive|unarchive|erasure)$`), tenant: true, card: "AA-942" },
  { method: "GET", pattern: /^lifecycle\/offboarding$/, tenant: true, card: "AA-942" },
  { method: "POST", pattern: /^lifecycle\/offboarding$/, tenant: true, card: "AA-942" },
  { method: "POST", pattern: new RegExp(`^lifecycle/offboarding/${UUID}/(cancel|erase)$`), tenant: true, card: "AA-942" },
  // AA-941 (CRM-13a): the owner confirms an export / deletion request AA raised (decline = cancel).
  { method: "POST", pattern: new RegExp(`^lifecycle/offboarding/${UUID}/accept$`), tenant: true, card: "AA-941" },
  { method: "GET", pattern: new RegExp(`^lifecycle/offboarding/${UUID}/export$`), tenant: true, card: "AA-942" },
];

/** Calls only the web server makes (lib/crmApi.ts crmCall), never reachable through /bff from the browser. */
export const SERVER_ONLY_ROUTES: readonly BffRoute[] = [
  // AA-940: the sign-in event, sent by /auth/agency after a real sign-in or a chooser pick (a browser route would let
  // any member add sign-in events at will).
  { method: "POST", pattern: /^audit\/sign-in$/, tenant: true, card: "AA-940" },
];

export function matchBff(method: string, subPath: string, routes: readonly BffRoute[] = BFF_ROUTES): BffRoute | null {
  const m = method.toUpperCase();
  return routes.find((r) => r.method === m && r.pattern.test(subPath)) ?? null;
}

/** Request headers the BFF builds for the upstream call. Nothing from the browser is copied except the body type. */
export function upstreamHeaders(
  accessToken: string,
  tenantId: string | null,
  contentType: string | null,
  requestId: string,
): Headers {
  const h = new Headers({ Accept: "application/json", Authorization: `Bearer ${accessToken}`, "X-Request-Id": requestId });
  if (tenantId) h.set("X-AA-Tenant-Id", tenantId);
  if (contentType && contentType.toLowerCase().startsWith("application/json")) h.set("Content-Type", "application/json");
  return h;
}
