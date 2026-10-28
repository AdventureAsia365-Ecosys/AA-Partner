// e2e/playwright.portal.config.ts — AA-979 (CRM-P2a): the tenant portal UI smoke (e2e/smoke-portal/), ported from the
// smoke-portal-setup and smoke-portal projects of AA-CIS-App playwright.config.ts (AA-885). Run by
// .github/workflows/ui-smoke-portal.yml against the studio on the agency host (BASE_URL
// https://portal.lumiguides.it.com), or by hand against a local studio. The default config (playwright.config.ts) is
// the CRM E2E and never sees these specs (its testDir is ./tests).
//   - "portal-setup": the tenant sign-in once, writing .auth/tenant-state.json (agency host: the e2e studio user's
//     partner sign-in, AA-979 §4.5; local studio: the API key E2E_TENANT_API_KEY); skips without them.
//   - "portal": the specs; the logged-in ones skip without the state, the host checks always run.
import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./smoke-portal",
  timeout: 60_000,
  retries: 1,
  workers: 1,
  use: {
    baseURL: process.env.BASE_URL || "http://localhost:3004",
    headless: true,
    screenshot: "only-on-failure",
    video: "off",
  },
  projects: [
    { name: "portal-setup", testMatch: /.*\.setup\.ts$/, use: { ...devices["Desktop Chrome"] } },
    {
      name: "portal",
      testMatch: /.*\.spec\.ts$/,
      dependencies: ["portal-setup"],
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  outputDir: "./test-results",
  reporter: [["list"], ["html", { outputFolder: "results/smoke-portal-report", open: "never" }]],
});
