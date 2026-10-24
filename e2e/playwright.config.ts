// e2e/playwright.config.ts — AA-936 (card CRM-08 "Playwright E2E"): the slice on Dev, end to end.
// Run by .github/workflows/e2e-dev.yml only (workflow_dispatch). Hosts and the form key come from the workflow's
// environment (no host of a real environment in this public repo). No retries: a retry asks for another sign-in
// code. No automatic screenshots or traces (the code page could be captured); the spec takes its own.
import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  timeout: 240_000,
  retries: 0,
  workers: 1,
  reporter: [["list"]],
  use: { headless: true, screenshot: "off", trace: "off", video: "off", ...devices["Desktop Chrome"] },
});
