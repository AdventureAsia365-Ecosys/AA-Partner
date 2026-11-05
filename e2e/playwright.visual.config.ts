// e2e/playwright.visual.config.ts — AA-1008 (S1d/S2c, root ADR 0016 decision 9): the visual run of the agency app
// (e2e/visual/), run by .github/workflows/visual.yml: per PR against the standalone server in the runner (BASE_URL
// http://localhost:3005, no session, KIT_GALLERY=1), and after the Dev deploy against the agency host. The other
// configs never see these specs.
//   - "visual-1440" / "visual-390": no session (VISUAL_AUTH=public): the public pages and the gated kit gallery.
//   - "visual-setup": the studio smoke's partner sign-in (e2e/smoke-portal/auth.setup.ts, the e2e studio user; skips
//     without its secrets), writing .auth/tenant-state.json (never uploaded, deleted by the workflow).
//   - "visual-signed-1440" / "visual-signed-390": the signed-in pages from that state (VISUAL_AUTH=signed).
// Every page at 1440 x 900 and 390 x 844, light and dark; scrollbars on (lesson S239: headless hides them, so a frame
// that fits by 2 px would pass here and scroll on a real screen). No retries: a finding is a finding.
import { defineConfig, devices } from "@playwright/test";

const viewports = [
  { vp: "1440", use: { viewport: { width: 1440, height: 900 } } },
  { vp: "390", use: { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true } },
] as const;

const base = {
  ...devices["Desktop Chrome"],
  reducedMotion: "reduce" as const,
  screenshot: "off" as const,
  trace: "off" as const,
  video: "off" as const,
  launchOptions: { ignoreDefaultArgs: ["--hide-scrollbars"] },
};

export default defineConfig({
  testDir: ".",
  timeout: 120_000,
  retries: 0,
  workers: 1,
  reporter: [["list"]],
  outputDir: "./visual/results/test-output",
  use: { baseURL: process.env.BASE_URL || "http://localhost:3005", headless: true },
  projects: [
    { name: "visual-setup", testMatch: /smoke-portal\/auth\.setup\.ts$/, use: { ...devices["Desktop Chrome"] } },
    ...viewports.flatMap(({ vp, use }) => [
      { name: `visual-${vp}`, testMatch: /visual\/.*\.spec\.ts$/, use: { ...base, ...use } },
      { name: `visual-signed-${vp}`, testMatch: /visual\/.*\.spec\.ts$/, dependencies: ["visual-setup"], use: { ...base, ...use } },
    ]),
  ],
});
