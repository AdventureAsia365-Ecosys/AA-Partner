// e2e/tests/onboarding-import.spec.ts — AA-962, card CRM-31 on Dev (the web half of the live verify):
//   1. Getting started (/onboarding): the three required steps, the optional ones, the shared-responsibility card;
//      screenshots at 1440 and 390 px wide, light and dark.
//   2. Settings > Import clients: a run-unique invented CSV of three clients (one row with a problem); Preview shows
//      the counts and the row to check; Import runs the crm_client_import job to done; a second Preview of the same
//      file shows every valid row as "Already imported" and nothing new (card E: no duplicates on a re-run).
//   3. A file without the name column is refused with the missing column named, before anything is written (card R).
//   4. Settings > Connections and privacy and Clients > Duplicates render.
// The R / E checks also run against the API in api/tests/integration/test_onboarding_import_api.py.
// Environment: as slice1.spec.ts (E2ECRM, E2ECRMPARTNER, E2ECRMEMAIL, E2ECRMPASSWORD, E2ECRMTOTP); the e2e user is the
// synthetic agency's owner. The run leaves its three invented clients in the synthetic agency (refs E2E-<run>-n).
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
const PHONE = `+61400${String(Date.now() % 1_000_000).padStart(6, "0")}`; // an invented AU mobile, unique per run

const CSV = [
  "client_ref,name,email,phone,marketing_consent",
  `E2E-${RUN}-1,Linh Sample ${RUN},linh.${RUN}@example.test,,yes`,
  `E2E-${RUN}-2,Oskar Sample ${RUN},,${PHONE},no`,
  `E2E-${RUN}-3,Mira Sample ${RUN},not-an-email,,`,
].join("\n");

async function shot(page: Page, name: string) {
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: `${SHOTS}/onboarding-${name}.png`, fullPage: true });
}

async function count(page: Page, outcome: string): Promise<number> {
  return Number(await page.getByTestId(`onb-count-${outcome}`).first().innerText());
}

test.describe("CRM-31 onboarding and import on Dev", () => {
  test.skip(process.env.E2ECRM !== "1", "opt-in: run through e2e-dev.yml");
  test.setTimeout(300_000);
  test.beforeAll(() => {
    for (const [k, v] of Object.entries({ PARTNER, EMAIL })) {
      if (!v) throw new Error(`missing ${k}`);
    }
    if (!PARTNER.startsWith("https://")) throw new Error("origins must be https");
  });

  test("checklist, import with dry run and re-run, connections, duplicates", async ({ page }) => {
    await page.goto(`${PARTNER}/onboarding`);
    await signIn(page, { email: EMAIL, password: PASSWORD || undefined, totpSecret: TOTP_SECRET || undefined });
    await landOnPartner(page, PARTNER);

    for (const scheme of ["light", "dark"] as const) {
      await page.emulateMedia({ colorScheme: scheme });
      for (const width of [1440, 390]) {
        await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
        await page.goto(`${PARTNER}/onboarding`);
        await expect(page.getByTestId("onb-checklist")).toBeVisible({ timeout: 15_000 });
        await expect(page.getByTestId("onb-required").locator("li")).toHaveCount(3);
        await expect(page.getByTestId("onb-ack")).toBeVisible();
        await shot(page, `01-checklist-${width}-${scheme}`);
      }
    }
    await page.emulateMedia({ colorScheme: "light" });
    await page.setViewportSize({ width: 1440, height: 900 });

    // Card R: a file without the name column is refused, the column named, nothing written.
    await page.goto(`${PARTNER}/settings/import`);
    await expect(page.getByTestId("onb-import")).toBeVisible({ timeout: 15_000 });
    await page.getByTestId("onb-import-file").setInputFiles({
      name: "no-name.csv",
      mimeType: "text/csv",
      buffer: Buffer.from(`client_ref,email\nE2E-${RUN}-9,x.${RUN}@example.test\n`),
    });
    await page.getByTestId("onb-import-preview").getByRole("button").click();
    await expect(page.getByTestId("onb-import-error")).toContainText("name", { timeout: 20_000 });
    await shot(page, "02-missing-column");

    // Dry run, then import.
    await page.getByTestId("onb-import-file").setInputFiles({ name: "clients.csv", mimeType: "text/csv", buffer: Buffer.from(CSV) });
    await page.getByTestId("onb-import-preview").getByRole("button").click();
    await expect(page.getByTestId("onb-import-report")).toBeVisible({ timeout: 20_000 });
    expect(await count(page, "create")).toBe(2);
    expect(await count(page, "invalid")).toBe(1);
    await expect(page.getByTestId("onb-import-issues")).toBeVisible();
    await shot(page, "03-preview");
    await page.getByTestId("onb-import-apply").getByRole("button").click();
    await expect(page.getByTestId("onb-import-progress")).toContainText(/finished/i, { timeout: 120_000 });
    expect(await count(page, "create")).toBe(2);
    await shot(page, "04-imported");

    // Card E: the same file again creates nothing.
    await page.getByTestId("onb-import-file").setInputFiles({ name: "clients.csv", mimeType: "text/csv", buffer: Buffer.from(CSV) });
    await page.getByTestId("onb-import-preview").getByRole("button").click();
    await expect(page.getByTestId("onb-import-report")).toBeVisible({ timeout: 20_000 });
    expect(await count(page, "create")).toBe(0);
    expect(await count(page, "already_imported")).toBe(2);
    await shot(page, "05-rerun-preview");

    await page.goto(`${PARTNER}/settings/connections`);
    await expect(page.getByTestId("onb-connections")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("onb-conn-mailbox")).toBeVisible();
    await shot(page, "06-connections");

    await page.goto(`${PARTNER}/clients/duplicates`);
    await expect(page.getByTestId("crm-duplicates")).toBeVisible({ timeout: 15_000 });
    await shot(page, "07-duplicates");
  });
});
