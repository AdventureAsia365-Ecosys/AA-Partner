// web/lib/legal.ts
// AA-1033 (S264 fix F1) — where the CRM terms of use live. On the agency host portal.<zone> the ALB sends some paths
// to other services before the CRM web's default rule (AA-CIS-Infra accounts/aa365/crm_agency_edge.tf): the Content
// studio owns /terms, /privacy and /assets/* (its own draft terms), so a CRM page at /terms was never served there and
// every CRM link to it opened the studio's partner terms. The CRM terms therefore live at /legal/terms, a path no
// agency-host rule routes elsewhere; every CRM link uses the constants below (guard: tests/crmTermsPath.test.ts).

/** The signed-in CRM terms of use page (web/app/(crm)/legal/terms). */
export const CRM_TERMS_PATH = "/legal/terms";

/** The terms' "AI-generated content" section (Settings > AI links it). */
export const CRM_TERMS_AI_HREF = `${CRM_TERMS_PATH}#ai`;

/**
 * The ALB path patterns of the agency-host rules that send a request somewhere other than the CRM web, in rule order
 * (crm_agency_edge.tf): crm_agency_api (the CRM API), crm_agency_studio and crm_agency_studio_x (the Content studio).
 * Keep in step with that file when a rule changes.
 */
export const AGENCY_HOST_ROUTED_ELSEWHERE: readonly string[] = [
  "/crm",
  "/crm/*",
  "/portal*",
  "/api/*",
  "/tenant-login*",
  "/terms",
  "/privacy",
  "/assets/*",
];

/** ALB path-pattern matching: case-sensitive, `*` matches any run of characters (`/` included), `?` exactly one. */
export function albPathMatches(pattern: string, path: string): boolean {
  const re = pattern.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".");
  return new RegExp(`^${re}$`).test(path);
}

/** The agency-host rule pattern that takes this path away from the CRM web, or null when the CRM web serves it. */
export function agencyHostRuleFor(path: string): string | null {
  const pathname = path.split(/[?#]/, 1)[0];
  return AGENCY_HOST_ROUTED_ELSEWHERE.find((p) => albPathMatches(p, pathname)) ?? null;
}
