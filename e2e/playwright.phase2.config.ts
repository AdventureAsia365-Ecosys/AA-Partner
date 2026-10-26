// e2e/playwright.phase2.config.ts — AA-979 P2b phase 2: the re-enrolment + agency-host studio run (e2e/phase2/), on
// demand only (E2EP2=1; no workflow sets it). Run by the orchestrator on its own machine with the MFA admin profile
// (the e2e mailbox), e.g.  AWS_PROFILE=aa365-admin E2EP2=1 ... npx playwright test -c playwright.phase2.config.ts
//
// Environment (no underscores; no host of a real environment in this public repo):
//   E2EP2PORTAL      the agency host origin (https://portal.<zone>)                                     required
//   E2EP2CF          the crm_agency CloudFront domain: pins the agency host to it in the browser          before cutover
//   E2EP2CISAPI      the CIS API origin (runbook 6a)                                                      required
//   E2EP2TENANT      the synthetic CIS tenant id                                                          required
//   E2EP2INVITEE     the invited address (an e2e mailbox recipient whose partner-pool account is reset)   required
//   E2EP2ROLE        viewer (default) | editor | owner — the Content studio role of the invitation
//   E2EP2STAFF       none (default: the orchestrator made the call; E2EP2SINCE = ISO time just before it)
//                    | browser (run --headed: a person signs in to the CIS admin, the spec makes the call)
//                    | a10b (the Dev-only E2E CIS admin, E2EP2A10BEMAIL; read-only today -> 403 e2e_read_only)
//   E2EP2ADMIN       the CIS admin origin (staff modes browser / a10b)
//   E2EP2PARTNER     optional: the expected origin of the join link (partner-dev. during the window)
//   E2EP2OUT         evidence folder (default e2e/results/phase2; inside the repo only under e2e/results/)
//   E2EP2REUSE=1     skip (a) and (b): sign the person in with the credentials saved under E2EP2OUT/.private
//   E2EP2TENANTKEY   optional: the tenant's API key for the E check (same tenant via an API-key session)
//   E2EP2READYMIN    minutes to wait for "being set up" to become ready (default 5)
//
// Host pinning (before the DNS cutover, runbook §2): Chromium's --host-resolver-rules "MAP <agency host> <CloudFront
// domain>" makes the BROWSER resolve the agency host to the distribution while the URL, the TLS SNI, the certificate
// check, the Host header and the cookies stay on the agency host (the same as curl --connect-to). It covers every
// page, redirect and fetch of the browser (the Cognito callback to the agency host included); it does NOT cover Node
// requests (APIRequestContext), so the spec only sends those to the CIS API. After the cutover leave E2EP2CF empty.
import { defineConfig, devices } from "@playwright/test";

function pinArgs(): string[] {
  const portal = (process.env.E2EP2PORTAL ?? "").trim();
  const cf = (process.env.E2EP2CF ?? "").trim().toLowerCase();
  if (!portal || !cf) return [];
  const host = new URL(portal).hostname;
  if (!/^[a-z0-9-]+(\.[a-z0-9-]+)+$/.test(cf) || !/^[a-z0-9-]+(\.[a-z0-9-]+)+$/.test(host)) {
    throw new Error("E2EP2PORTAL / E2EP2CF must be plain host names");
  }
  return [`--host-resolver-rules=MAP ${host} ${cf}`];
}

export default defineConfig({
  testDir: "./phase2",
  timeout: 15 * 60_000,
  retries: 0,
  workers: 1,
  reporter: [["list"]],
  outputDir: "./test-results/phase2",
  use: {
    ...devices["Desktop Chrome"],
    viewport: { width: 1440, height: 900 },
    headless: true,
    screenshot: "off",
    trace: "off",
    video: "off",
    launchOptions: { args: pinArgs() },
  },
});
