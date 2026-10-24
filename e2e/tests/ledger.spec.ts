// e2e/tests/ledger.spec.ts — AA-944, card CRM-16 on Dev (the web half of the plan's verify step 5):
//   1. Suppliers (A-13): Adventure Asia is listed once and built in; the owner adds a run-unique own supplier (and
//      deletes it at the end).
//   2. Bookings (A-11): the ledger page renders, with the totals panel for the owner.
//   3. Settings > Payments: the owner's form and the exchange-rate overrides render.
// The R / E checks of the card run against the API in the integration tests (api/tests/integration/test_ledger_api.py)
// and in the plan's live verify (API calls with the synthetic agency's token).
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
const RUN = Date.now().toString(36);

async function shot(page: Page, name: string) {
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: `${SHOTS}/ledger-${name}.png`, fullPage: true });
}

test.describe("CRM-16 ledger and suppliers on Dev", () => {
  test.skip(process.env.E2ECRM !== "1", "opt-in: run through e2e-dev.yml");
  test.setTimeout(240_000);
  test.beforeAll(() => {
    for (const [k, v] of Object.entries({ PARTNER, EMAIL })) {
      if (!v) throw new Error(`missing ${k}`);
    }
    if (!PARTNER.startsWith("https://")) throw new Error("origins must be https");
  });

  test("suppliers, bookings and payment settings render for the owner", async ({ page }) => {
    await page.goto(`${PARTNER}/suppliers`);
    await signIn(page, { email: EMAIL, password: PASSWORD || undefined, totpSecret: TOTP_SECRET || undefined });
    await landOnPartner(page, PARTNER);
    await page.goto(`${PARTNER}/suppliers`);
    await expect(page.getByTestId("suppliers-view")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("supplier-aa")).toHaveCount(1);

    const name = `E2E ${RUN} Hotel`;
    await page.getByRole("button", { name: /add supplier/i }).click();
    const form = page.getByTestId("supplier-add-form");
    await form.locator('input[name="name"]').fill(name);
    await form.locator('select[name="category"]').selectOption("accommodation");
    await form.getByRole("button", { name: /save/i }).click();
    await expect(page.getByText(name)).toBeVisible({ timeout: 15_000 });
    await shot(page, "01-suppliers");

    await page.goto(`${PARTNER}/bookings`);
    await expect(page.getByTestId("crm-bookings")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("bookings-totals")).toBeVisible();
    await shot(page, "02-bookings");

    await page.goto(`${PARTNER}/settings/payments`);
    await expect(page.getByTestId("payments-settings")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("payments-overrides")).toBeVisible();
    await shot(page, "03-payments");

    // Leave nothing behind: the run's supplier names no line, so it goes to the trash.
    await page.goto(`${PARTNER}/suppliers`);
    await page.getByRole("row", { name: new RegExp(name) }).getByRole("button", { name: /delete/i }).click();
    await expect(page.getByText(name)).toHaveCount(0, { timeout: 15_000 });
  });
});
