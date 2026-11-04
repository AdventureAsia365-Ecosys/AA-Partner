// web/lib/studio/legal.ts
// AA-1036 (R3 slice 1, root ADR 0019 d6) — where the Content studio's draft legal pages live in the merged agency app.
// They were /terms and /privacy in the separate studio app (AA-886); the CRM already owns /legal/terms (its own AI
// terms, AA-1033, lib/legal.ts), so the studio's pages move to /legal/partner-terms and /legal/privacy. The old paths
// answer 308 here (next.config.ts) and keep doing so after the cut-over sends them to this app.

/** The studio's draft partner terms (app/(studio)/legal/partner-terms). */
export const TERMS_PATH = "/legal/partner-terms";

/** The studio's draft privacy notice (app/(studio)/legal/privacy). */
export const PRIVACY_PATH = "/legal/privacy";

/** The old paths of the separate studio app and where they go now (308, next.config.ts). */
export const LEGACY_LEGAL_REDIRECTS: readonly { source: string; destination: string }[] = [
  { source: "/terms", destination: TERMS_PATH },
  { source: "/privacy", destination: PRIVACY_PATH },
];

/** Adventure Asia's company-wide privacy policy, linked from the studio notice (200 on 20/10/2026). */
export const COMPANY_PRIVACY_URL = "https://adventure.asia/privacy-policy";
