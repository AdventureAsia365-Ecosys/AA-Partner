// e2e/tests/reports.spec.ts — AA-966, card CRM-34 on Dev (the web half of the live verify):
//   1. Reports (A-14): the page renders for the owner with the tiles, the funnel, money and success metrics; the
//      period links switch the window; the CSV export downloads a file whose first line is a header row.
//   2. Home (A-01): the KPI tiles show and link to Reports.
//   3. Screenshots at 1440 and 390 wide, light and dark (lessons: evidence per viewport and theme).
// The reconcile checks (numbers = direct counts, RLS, assistant without money, formula guard) run against the API in
// api/tests/integration/test_reports_api.py and in the orchestrator's live verify.
// Environment: as slice1.spec.ts (E2ECRM, E2ECRMPARTNER, E2ECRMEMAIL, E2ECRMPASSWORD, E2ECRMTOTP).
import { expect, test, type Page } from "@playwright/test";
import fs from "node:fs";
import { landOnPartner, signIn } from "../helpers/signin";

const env = (name: string) => (process.env[name] ?? "").trim().replace(/\/+$/, "");
const PARTNER = env("E2ECRMPARTNER");
const EMAIL = env("E2ECRMEMAIL");
const PASSWORD = (process.env.E2ECRMPASSWORD ?? "").trim();
const TOTP_SECRET = (process.env.E2ECRMTOTP ?? "").trim();
const SHOTS = "results";

async function shot(page: Page, name: string) {
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: `${SHOTS}/reports-${name}.png`, fullPage: true });
}

test.describe("CRM-34 reports on Dev", () => {
  test.skip(process.env.E2ECRM !== "1", "opt-in: run through e2e-dev.yml");
  test.setTimeout(240_000);
  test.beforeAll(() => {
    for (const [k, v] of Object.entries({ PARTNER, EMAIL })) {
      if (!v) throw new Error(`missing ${k}`);
    }
    if (!PARTNER.startsWith("https://")) throw new Error("origins must be https");
  });

  test("reports, the CSV export and the Home tiles render for the owner", async ({ page }) => {
    await page.goto(`${PARTNER}/reports`);
    await signIn(page, { email: EMAIL, password: PASSWORD || undefined, totpSecret: TOTP_SECRET || undefined });
    await landOnPartner(page, PARTNER);
    await page.goto(`${PARTNER}/reports?period=12m`);
    await expect(page.getByTestId("crm-reports")).toBeVisible({ timeout: 20_000 });
    await expect(page.getByTestId("reports-tiles")).toBeVisible();
    await expect(page.getByTestId("reports-tile-inquiries")).toBeVisible();
    await expect(page.getByTestId("reports-success")).toBeVisible();
    await expect(page.getByTestId("reports-insights")).toBeVisible();

    const download = page.waitForEvent("download");
    await page.getByTestId("reports-export").getByRole("button").click();
    const file = await download;
    expect(file.suggestedFilename()).toMatch(/^report-funnel-12m-\d{8}\.csv$/);
    const body = fs.readFileSync(await file.path(), "utf-8").replace(/^﻿/, "");
    expect(body.split("\r\n")[0]).toBe("stage,trips");

    for (const [w, h] of [[1440, 900], [390, 844]] as const) {
      await page.setViewportSize({ width: w, height: h });
      for (const scheme of ["light", "dark"] as const) {
        await page.emulateMedia({ colorScheme: scheme });
        await shot(page, `${w}-${scheme}`);
      }
    }
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.emulateMedia({ colorScheme: "light" });
    await page.goto(`${PARTNER}/reports?period=30d`);
    await expect(page.getByTestId("crm-reports")).toBeVisible({ timeout: 20_000 });

    await page.goto(`${PARTNER}/`);
    await expect(page.getByTestId("crm-home-tiles")).toBeVisible({ timeout: 20_000 });
    await shot(page, "home-tiles");
  });
});
