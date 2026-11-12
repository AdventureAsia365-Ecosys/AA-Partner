// e2e/visual/pages.ts
// AA-1008 (S1d/S2c, root ADR 0016 decision 9, guard G16) — the pages of the agency app's visual run (AA-Partner web:
// the CRM and, since AA-1036, the Content studio). Pure data (no Playwright import), so web/tests/visualPages.test.ts can
// check that every web/app/**/page.tsx route is listed here or excluded with a reason, and changed-pages.mjs can map a
// diff to the pages it touches.
//
// auth "public": no session. Captured by the per-PR run (`next build` + the standalone server in the runner, no
//   backend, ENVIRONMENT=test) and after the Dev deploy on the agency host.
// auth "signed": after the Dev deploy only, signed in as the e2e studio user (the studio smoke's partner sign-in,
//   e2e/smoke-portal/auth.setup.ts). A page that user's session does not open (it is sent elsewhere, e.g. a CRM page
//   without a CRM membership) is skipped and named in the report, never passed.
// gallery: the gated kit gallery route (web/app/kit-gallery), served only by a KIT_GALLERY=1 server (the per-PR run);
//   selftest: its planted-overflow pages, run only by the self-test step, which must FAIL.

export interface VisualPage {
  /** File-name-safe id, used in the screenshot names: `<slug>__<1440|390>__<light|dark>.png`. */
  slug: string;
  /** The app route (a page.tsx folder under web/app/, without route groups); dynamic segments in brackets. */
  route: string;
  /** The path to open. */
  path: string;
  auth: "public" | "signed";
  /** The page renders a kit frame (`[data-kit-frame]` PageFrame, or the ShellFrame `<main>`); default true. */
  frame?: boolean;
  /** The document's expected HTTP status (default 200). */
  status?: number;
  gallery?: boolean;
  /** "document" or "frame": the planted overflow the self-test expects the run to catch. */
  selftest?: "document" | "frame";
}

const crm = (slug: string, route: string): VisualPage => ({ slug, route, path: route, auth: "signed" });
const studio = (slug: string, route: string): VisualPage => ({ slug, route, path: route, auth: "signed" });

export const PAGES: VisualPage[] = [
  // Public (no session).
  { slug: "signed-out", route: "/signed-out", path: "/signed-out", auth: "public", frame: false },
  { slug: "health", route: "/health", path: "/health", auth: "public", frame: false },
  { slug: "legal-partner-terms", route: "/legal/partner-terms", path: "/legal/partner-terms", auth: "public" },
  { slug: "legal-privacy", route: "/legal/privacy", path: "/legal/privacy", auth: "public" },
  { slug: "staff-signed-out", route: "/staff/signed-out", path: "/staff/signed-out", auth: "public", frame: false },
  { slug: "kit-gallery", route: "/kit-gallery", path: "/kit-gallery", auth: "public", gallery: true },
  { slug: "selftest-document", route: "/kit-gallery", path: "/kit-gallery?plant=overflow", auth: "public", gallery: true, selftest: "document" },
  { slug: "selftest-frame", route: "/kit-gallery", path: "/kit-gallery?plant=frame", auth: "public", gallery: true, selftest: "frame" },
  // app/not-found.tsx (no page.tsx of its own): a signed-out visit to an unknown path goes to the sign-in instead.
  { slug: "not-found", route: "(not-found)", path: "/this-page-does-not-exist", auth: "signed", frame: false, status: 404 },
  // The CRM (signed in; skipped when the e2e user's session has no CRM membership).
  crm("home", "/home"),
  crm("pipeline", "/pipeline"),
  crm("inbox", "/inbox"),
  crm("tasks", "/tasks"),
  crm("calendar", "/calendar"),
  crm("clients", "/clients"),
  crm("clients-duplicates", "/clients/duplicates"),
  crm("bookings", "/bookings"),
  crm("aa-tariff", "/aa-tariff"),
  crm("aa-bookings", "/aa-bookings"), // AA-1068: Bookings with AA (reads AA live; the copy when AA is down)
  crm("payments", "/payments"),
  crm("commissions", "/commissions"),
  crm("partners", "/partners"), // AA-1071
  crm("suppliers", "/suppliers"),
  crm("services", "/services"), // AA-1069: the agency service catalogue
  crm("travelling", "/travelling"),
  crm("departures", "/departures"), // AA-1070: the departure roster
  crm("reports", "/reports"),
  crm("automations", "/automations"),
  crm("onboarding", "/onboarding"),
  crm("help", "/help"),
  crm("legal-terms", "/legal/terms"),
  crm("settings", "/settings"),
  crm("settings-access-log", "/settings/access-log"),
  crm("settings-add-ons", "/settings/add-ons"),
  crm("settings-agreements", "/settings/agreements"),
  crm("settings-ai", "/settings/ai"),
  crm("settings-travel", "/settings/travel"), // AA-1070
  crm("settings-inclusions", "/settings/inclusions"), // AA-1070 crm-35
  crm("settings-channels", "/settings/channels"),
  crm("settings-connections", "/settings/connections"),
  crm("settings-data", "/settings/data"),
  crm("settings-forms", "/settings/forms"),
  crm("settings-import", "/settings/import"),
  crm("settings-mailbox", "/settings/mailbox"),
  crm("settings-payments", "/settings/payments"),
  crm("settings-pricing", "/settings/pricing"), // AA-1066: age bands, discount limit, price rules (owner)
  crm("settings-cancellation", "/settings/cancellation"), // AA-1067: the traveller cancellation policy
  crm("settings-saved-replies", "/settings/saved-replies"),
  crm("settings-team", "/settings/team"),
  crm("settings-templates", "/settings/templates"),
  // The Content studio (signed in as the e2e studio user).
  studio("portal-dashboard", "/portal/dashboard"),
  studio("portal-tours", "/portal/tours"),
  studio("portal-tours-mine", "/portal/tours/mine"),
  studio("portal-content", "/portal/content"),
  studio("portal-content-publish", "/portal/content/publish"),
  studio("portal-create-article", "/portal/create/article"),
  studio("portal-create-social", "/portal/create/social"),
  studio("portal-create-write", "/portal/create/write"),
  studio("portal-brand", "/portal/brand"),
  studio("portal-facts", "/portal/facts"),
  studio("portal-help-facebook", "/portal/help/facebook"),
  studio("portal-account-activity", "/portal/account/activity"),
  studio("portal-account-add-ons", "/portal/account/add-ons"),
  studio("portal-account-billing", "/portal/account/billing"),
  studio("portal-account-integrations", "/portal/account/integrations"),
  studio("portal-account-members", "/portal/account/members"),
  studio("portal-account-settings", "/portal/account/settings"),
];

/** The pages one run captures (visual.spec.ts; pure, so the unit guard tests it too). */
export function selectPages(pages: VisualPage[], opts: { auth: "public" | "signed"; slugs: string; gallery: boolean; selftest: boolean }): VisualPage[] {
  if (opts.selftest) return pages.filter((p) => p.selftest);
  const wanted = opts.slugs === "all" ? null : new Set(opts.slugs.split(",").map((s) => s.trim()).filter(Boolean));
  return pages.filter((p) => {
    if (p.selftest) return false;
    if (p.gallery) return opts.gallery && opts.auth === "public"; // the gallery is always captured where it exists
    if (p.auth !== opts.auth) return false;
    return wanted === null || wanted.has(p.slug);
  });
}

/** Routes with a page.tsx that the run does not open, each with its reason. */
export const EXCLUDED: Record<string, string> = {
  "/": "redirects: to /portal/dashboard on the agency host (WEB_CONTENT_HOME), else the CRM home (/home, listed)",
  "/[section]": "the CRM's placeholder route for a section not built yet (an unknown section answers 404)",
  "/portal": "redirects to /portal/dashboard (listed)",
  "/portal/account": "redirects to the first account tab (the tabs are listed)",
  "/portal/create": "redirects to the first create tab (the tabs are listed)",
  "/portal-access/none": "a sign-in step shown only to a person with no studio membership (the e2e user has one)",
  "/portal-access/select": "a sign-in step (pick one of several studios) that redirects once one is picked",
  "/select-agency": "a sign-in step (pick one of several agencies) that redirects once one is picked",
  "/join/accept": "an invitation step: needs a signed-in person holding a live invitation",
  "/join/invite": "an invitation landing: needs a real invitation token in the link",
  "/clients/[travelerId]": "one traveller of the agency: opened from the client list (agency data, no stable id)",
  "/pipeline/[tripId]": "one trip: opened from the board (agency data, no stable id)",
  "/departures/[ref]": "one group departure (AA-1068): opened from a trip's Money card or Home (agency data, no stable ref)",
  "/pipeline/[tripId]/proposal/[proposalId]": "one proposal of a trip: opened from the trip (agency data, no stable id)",
  "/pipeline/[tripId]/proposal/[proposalId]/text": "the plain-text view of one proposal (agency data, no stable id)",
  "/c/[...slug]": "the client host (the traveller's links), not served on the agency host the run opens",
  "/c/f/[publicKey]": "the client host's inquiry form (a form's public key), not served on the agency host",
  "/c/p": "the client host's proposal view (needs a signed client link), not served on the agency host",
  "/c/s/[token]": "the client host's signing page (needs a live signing token), not served on the agency host",
  "/c/t": "the client host's trip view (needs a signed client link), not served on the agency host",
  "/c/a": "AA-1071: a sales partner's own page on the client host (needs a signed partner link and an e-mail code)",
  "/c/r/[token]": "AA-1071: a sales partner's inquiry link on the client host (needs a signed partner link)",
  "/trip/[tourId]": "the client host's public tour page (redirects to the tour of a published catalog)",
  "/staff": "the AA staff console: a staff Cognito sign-in, no staff E2E user in CI (redirects to /staff/ops)",
  "/staff/ops": "the AA staff console: a staff Cognito sign-in, no staff E2E user in CI",
  "/staff/ops/[tenantId]/[tripId]": "the AA staff console: a staff Cognito sign-in, no staff E2E user in CI",
  "/staff/support": "the AA staff console: a staff Cognito sign-in, no staff E2E user in CI",
  "/staff/support/[tenantId]": "the AA staff console: a staff Cognito sign-in, no staff E2E user in CI",
  "/staff/support/[tenantId]/trips/[tripId]": "the AA staff console: a staff Cognito sign-in, no staff E2E user in CI",
};
