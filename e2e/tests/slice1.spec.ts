// e2e/tests/slice1.spec.ts — AA-936, card CRM-08 Done when (Dev): a prospect submits the inquiry form on the client
// host -> the card is on the agency's board within about a minute, waiting on "Me"; opening the trip shows the
// travellers (the API audits the view) and the e-mail only after a click. Plus the R test seen from the browser:
// without the consent box the form refuses and asks for it.
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
// A synthetic prospect per run: .invalid never delivers (RFC 2606); the run id makes the e-mail new each time.
const PROSPECT = { name: `E2E Prospect ${RUN}`, email: `e2e.prospect.${RUN}@example.invalid` };

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
    await expect(page.getByTestId("intake-form")).toBeVisible();
    await page.getByTestId("intake-dest-vietnam").click();
    await page.locator('select[name="budget_band"]').selectOption("2000_3500");
    await page.locator('input[name="nights"]').fill("9");
    await page.locator('input[name="name"]').fill(PROSPECT.name);
    await page.locator('input[name="email"]').fill(PROSPECT.email);
    await page.locator('textarea[name="message"]').fill("E2E run: Ha Giang and Hoi An, slow pace.");
    await page.getByRole("button", { name: /send my inquiry|gửi yêu cầu/i }).click();
    await expect(page.getByTestId("intake-error")).toBeVisible(); // R: no consent -> refused, nothing stored
    await shot(page, "01-no-consent-refused");
    await page.getByTestId("intake-consent").check();
    await page.getByRole("button", { name: /send my inquiry|gửi yêu cầu/i }).click();
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
      await page.getByRole("button", { name: /open|mở/i }).first().click();
    }

    // Done when: the card is on the board within about a minute (worker poll + crm_intake_process).
    const card = page.getByTestId("trip-card").filter({ hasText: PROSPECT.name.split(" ").slice(0, 2).join(" ") });
    await expect
      .poll(
        async () => {
          await page.goto(`${PARTNER}/pipeline`);
          return card.count();
        },
        { timeout: 90_000, intervals: [5_000, 10_000] },
      )
      .toBeGreaterThan(0);
    await expect(page.getByTestId("column-inquiry").getByTestId("trip-card").first()).toBeVisible();
    await shot(page, "03-board-card");

    // Trip workspace: the name shows (the API writes the view event), the e-mail only after a click.
    await card.first().click();
    await expect(page.getByTestId("trip-party")).toContainText(PROSPECT.name);
    await expect(page.getByTestId("revealed-email")).toHaveCount(0);
    await page.getByRole("button", { name: /show email|xem e-mail/i }).first().click();
    await expect(page.getByTestId("revealed-email")).toHaveText(PROSPECT.email);
    await expect(page.getByTestId("trip-tasks")).toContainText(/reply within 2 h|trả lời trong 2 giờ/i);
    await shot(page, "04-trip");

    // A move and its undo-free reverse through the workspace buttons (the DB enforces the edges).
    await page.getByRole("button", { name: /move to qualifying|chuyển sang đang tư vấn/i }).click();
    await expect(page.getByText(/qualifying|đang tư vấn/i).first()).toBeVisible();
    await page.getByRole("button", { name: /move to inquiry|chuyển sang yêu cầu mới/i }).click();
    await shot(page, "05-moved-back");
  });
});
