// e2e/tests/slice1.spec.ts — AA-936, card CRM-08 Done when (Dev): a prospect submits the inquiry form on the client
// host -> the card is on the agency's board within about a minute, waiting on "Me"; opening the trip shows the
// travellers (the API audits the view) and the e-mail only after a click. Plus the R test seen from the browser:
// without the consent box the form refuses and asks for it. Both hosts open in English (owner rule S249: the seeded
// agency's default_locale is vi-VN, but only the person's own choice switches the language).
//
// Robustness (run 40060691620 clicked "Show email" on the server-rendered trip page before React hydrated: the click
// did nothing and no reveal reached the API): every click waits for the page's `data-hydrated="true"` marker, the
// locators are test ids / roles inside test ids rather than translated text, and the prospect's card is found by a
// run-unique token (earlier runs leave their cards on the board).
//
// Environment (set by .github/workflows/e2e-dev.yml; names without underscores on purpose):
//   E2ECRM=1            opt-in switch
//   E2ECRMCLIENT        https origin of the client host (go-dev.)
//   E2ECRMPARTNER       https origin of the partner host (partner-dev.)
//   E2ECRMFORMKEY       public key of the seeded agency's form (python -m app.seed --apply prints it)
//   E2ECRMEMAIL         the agency e2e user's address (owner of that agency)
// AWS credentials of the read-only mailbox role come from the job.
import { expect, test, type Page } from "@playwright/test";
import fs from "node:fs";
import { baseline, waitForCode } from "../helpers/mailbox";

const env = (name: string) => (process.env[name] ?? "").trim().replace(/\/+$/, "");
const CLIENT = env("E2ECRMCLIENT");
const PARTNER = env("E2ECRMPARTNER");
const FORM_KEY = env("E2ECRMFORMKEY");
const EMAIL = env("E2ECRMEMAIL");
const SHOTS = "results";
const RUN = Date.now().toString(36);
// A synthetic prospect per run: .invalid never delivers (RFC 2606); the run id makes the e-mail new each time. The run
// id sits in the name's first words because the board card shortens the last word ("E2E <run> P.").
const PROSPECT = { name: `E2E ${RUN} Prospect`, email: `e2e.prospect.${RUN}@example.invalid` };
const CARD_TOKEN = `E2E ${RUN} `;

async function shot(page: Page, name: string) {
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true });
}

test.describe("CRM-08 slice 1 on Dev", () => {
  test.skip(process.env.E2ECRM !== "1", "opt-in: run through e2e-dev.yml");
  test.beforeAll(() => {
    for (const [k, v] of Object.entries({ CLIENT, PARTNER, FORM_KEY, EMAIL })) {
      if (!v) throw new Error(`missing ${k}`);
    }
    if (!CLIENT.startsWith("https://") || !PARTNER.startsWith("https://")) throw new Error("origins must be https");
  });

  test("the form refuses without consent, then an inquiry becomes a card on the board", async ({ page }) => {
    await page.goto(`${CLIENT}/c/f/${FORM_KEY}`);
    // Handlers attached before the first click (a click on the server HTML is lost).
    await expect(page.getByTestId("intake-form")).toHaveAttribute("data-hydrated", "true", { timeout: 15_000 });
    await expect(page.getByTestId("intake-page-body")).toHaveAttribute("lang", "en"); // English by default
    await page.getByTestId("intake-dest-vietnam").click();
    await page.locator('select[name="budget_band"]').selectOption("2000_3500");
    await page.locator('input[name="nights"]').fill("9");
    await page.locator('input[name="name"]').fill(PROSPECT.name);
    await page.locator('input[name="email"]').fill(PROSPECT.email);
    await page.locator('textarea[name="message"]').fill("E2E run: Ha Giang and Hoi An, slow pace.");
    await page.getByTestId("intake-submit").getByRole("button").click();
    await expect(page.getByTestId("intake-error")).toBeVisible(); // R: no consent -> refused, nothing stored
    await shot(page, "01-no-consent-refused");
    await page.getByTestId("intake-consent").check();
    await page.getByTestId("intake-submit").getByRole("button").click();
    await expect(page.getByTestId("intake-thanks")).toBeVisible({ timeout: 15_000 });
    await shot(page, "02-thanks");

    // Agency side: sign in with the e2e owner's e-mail code (managed login), pick the agency if asked.
    await page.goto(`${PARTNER}/pipeline`);
    const before = await baseline();
    const emailField = page.getByLabel(/email/i).first();
    await expect(emailField).toBeVisible({ timeout: 30_000 });
    await emailField.fill(EMAIL);
    await page.getByRole("button", { name: /^(next|sign in)$/i }).first().click();
    const codeField = page.getByLabel(/code/i).first();
    await expect(codeField).toBeVisible({ timeout: 30_000 });
    await codeField.fill(await waitForCode(EMAIL, before));
    await page.getByRole("button", { name: /^(continue|sign in|next)$/i }).first().click();
    await page.waitForURL((u) => u.origin === PARTNER, { timeout: 30_000 });
    if (new URL(page.url()).pathname === "/select-agency") {
      await page.getByTestId("agency-option").first().getByRole("button").click(); // plain form POST, no JS needed
    }
    // The agency is vi-VN; with no saved choice the shell is English.
    await expect(page.locator("html")).toHaveAttribute("lang", "en");

    // Done when: the card is on the board within about a minute (worker poll + crm_intake_process).
    const card = page.getByTestId("trip-card").filter({ hasText: CARD_TOKEN });
    await expect
      .poll(
        async () => {
          await page.goto(`${PARTNER}/pipeline`);
          return card.count();
        },
        { timeout: 90_000, intervals: [5_000, 10_000] },
      )
      .toBeGreaterThan(0);
    await expect(page.getByTestId("column-inquiry").getByTestId("trip-card").filter({ hasText: CARD_TOKEN })).toHaveCount(1);
    await shot(page, "03-board-card");

    // Trip workspace: the name shows (the API writes the view event), the e-mail only after a click.
    await card.first().click(); // a plain link: a full server render of the trip page follows
    const trip = page.getByTestId("crm-trip");
    await expect(trip).toHaveAttribute("data-hydrated", "true", { timeout: 15_000 });
    await expect(page.getByTestId("trip-party")).toContainText(PROSPECT.name);
    await expect(page.getByTestId("revealed-email")).toHaveCount(0);
    // AA-937: Reveal asks why first, then Show posts one reveal with the reason (one audit event).
    const email = page.getByTestId("reveal-email").first();
    await email.getByTestId("show-email").getByRole("button").click();
    await email.getByTestId("reveal-reason-email").selectOption("contact_client");
    await email.getByTestId("reveal-show-email").getByRole("button").click();
    await expect(page.getByTestId("revealed-email")).toHaveText(PROSPECT.email, { timeout: 10_000 });
    await expect(page.locator('[data-testid="trip-task"][data-kind="reply_inquiry"][data-status="open"]')).toHaveCount(1);
    await expect(page.getByTestId("trip-tasks")).toContainText(/reply within 2 h/i); // English labels
    await shot(page, "04-trip");

    // A move and its reverse through the workspace buttons (the DB enforces the edges).
    await page.getByTestId("trip-move-qualifying").getByRole("button").click();
    await expect(page.getByTestId("trip-stage")).toHaveAttribute("data-stage", "qualifying", { timeout: 10_000 });
    await page.getByTestId("trip-move-inquiry").getByRole("button").click();
    await expect(page.getByTestId("trip-stage")).toHaveAttribute("data-stage", "inquiry", { timeout: 10_000 });
    await shot(page, "05-moved-back");

    // AA-937 (CRM-09): the lead's client profile shows the name, masked identity fields and the trip.
    await page.getByTestId("trip-party").getByRole("link", { name: PROSPECT.name }).click();
    await expect(page.getByTestId("client-name")).toHaveText(PROSPECT.name);
    await expect(page.getByTestId("client-identity")).not.toContainText(PROSPECT.email);
    await expect(page.getByTestId("client-trips").getByRole("link")).toHaveCount(1);
    await shot(page, "06-client-profile");
  });
});
