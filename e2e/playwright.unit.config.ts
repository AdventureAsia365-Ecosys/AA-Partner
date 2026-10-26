// e2e/playwright.unit.config.ts — AA-979 P2b phase 2: pure unit tests of the e2e helpers (e2e/unit/, e.g. the RFC 6238
// vectors of helpers/totp.ts). No browser is launched (the specs ask for no page fixture), no network, no AWS. The
// default config (CRM E2E, ./tests), the portal smoke and the phase 2 config never see these specs.
import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./unit",
  timeout: 10_000,
  retries: 0,
  workers: 1,
  reporter: [["list"]],
  outputDir: "./test-results/unit",
});
