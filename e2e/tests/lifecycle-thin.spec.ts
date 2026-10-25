// e2e/tests/lifecycle-thin.spec.ts — AA-946, card CRM-18 (thin full lifecycle) Done when (Dev), one synthetic trip:
//   1. the owner publishes run-unique booking terms and a waiver (Settings > Agreements);
//   2. a prospect submits the inquiry form; the owner takes the trip, fills Stage 2 in from a call and moves it to
//      Proposal;
//   3. Proposal tab: a text proposal (total 1000, deposit 300, a balance date) is drafted and sent; the client link
//      (shown once) opens C-03/C-04 on the client host, the client ticks the terms, types the name and accepts;
//   4. the lead's portal link (before the deposit): code sign-in; Stage 3 shows "opens after the deposit" and a crafted
//      PUT is 403 stage3_locked (card R);
//   5. Mark paid (deposit): the trip is Booked and every traveller gets a new portal link;
//   6. the lead signs in again, sends Stage 3 and signs the waiver with a fresh code -> the trip's s3 is 100 ->
//      Pre-departure moves without the gate dialog;
//   7. the trip is cancelled (it leaves the open board).
// Steps 4 and 6 need the code: PORTAL_OTP_DEV_ECHO on the Dev API (the synthetic agency has no mailbox), else they
// are skipped with a note. Card E (no return date -> stays Travelling + task) runs on PostgreSQL
// (api/tests/integration/test_lifecycle_api.py), it needs the date to pass.
// Environment: as slice2-m1m2.spec.ts (E2ECRM, E2ECRMCLIENT, E2ECRMPARTNER, E2ECRMFORMKEY, E2ECRMEMAIL,
// E2ECRMPASSWORD, E2ECRMTOTP).
import { expect, test, type Page } from "@playwright/test";
import fs from "node:fs";
import { landOnPartner, signIn } from "../helpers/signin";

const env = (name: string) => (process.env[name] ?? "").trim().replace(/\/+$/, "");
const CLIENT = env("E2ECRMCLIENT");
const PARTNER = env("E2ECRMPARTNER");
const FORM_KEY = env("E2ECRMFORMKEY");
const EMAIL = env("E2ECRMEMAIL");
const PASSWORD = (process.env.E2ECRMPASSWORD ?? "").trim();
const TOTP_SECRET = (process.env.E2ECRMTOTP ?? "").trim();
const SHOTS = "results";
const RUN = Date.now().toString(36);
const PROSPECT = { name: `E2E ${RUN} Lifecycle`, email: `e2e.lifecycle.${RUN}@example.invalid` };
const CARD_TOKEN = `E2E ${RUN} `;

async function shot(page: Page, name: string) {
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: `${SHOTS}/lifecycle-${name}.png`, fullPage: true });
}

async function hydrated(page: Page, testId: string) {
  await expect(page.getByTestId(testId)).toHaveAttribute("data-hydrated", "true", { timeout: 15_000 });
}

async function stageIs(page: Page, stage: string) {
  await expect(page.getByTestId("trip-stage")).toHaveAttribute("data-stage", stage, { timeout: 15_000 });
}

async function moveTo(page: Page, stage: string) {
  await page.getByTestId(`trip-move-${stage}`).getByRole("button").click();
}

async function publish(page: Page, kind: "booking_terms" | "waiver", label: string, text: string) {
  const form = page.getByTestId("agreement-form");
  await form.locator('select[name="kind"]').selectOption(kind);
  await form.locator('input[name="version_label"]').fill(label);
  await form.locator('textarea[name="body_text"]').fill(text);
  await form.getByRole("button", { name: /save draft/i }).click();
  const row = page.locator(`[data-testid="agreement-row"][data-kind="${kind}"]`).filter({ hasText: label });
  await expect(row).toHaveAttribute("data-status", "draft", { timeout: 15_000 });
  await row.getByRole("button", { name: /publish/i }).click();
  await expect(row).toHaveAttribute("data-status", "published", { timeout: 15_000 });
}

/** The dev code from the portal's hint ("… (test code 123456)"), or null when the API does not echo it. */
async function devCode(page: Page, hintId: string): Promise<string | null> {
  const hint = page.getByTestId(hintId);
  await expect(hint.or(page.getByTestId("portal-note"))).toBeVisible({ timeout: 15_000 });
  if (!(await hint.isVisible())) return null;
  return (await hint.textContent())?.match(/\b(\d{6})\b/)?.[1] ?? null;
}

async function portalSignIn(page: Page, url: string): Promise<boolean> {
  await page.goto(url);
  await expect(page.getByTestId("portal-sign-in")).toBeVisible({ timeout: 15_000 });
  await page.getByTestId("portal-send-code").click();
  const code = await devCode(page, "portal-hint");
  if (!code) return false;
  await page.getByTestId("portal-code").fill(code);
  await page.getByTestId("portal-verify").click();
  await expect(page.getByTestId("portal-home")).toBeVisible({ timeout: 15_000 });
  return true;
}

test.describe("CRM-18 thin full lifecycle on Dev", () => {
  test.skip(process.env.E2ECRM !== "1", "opt-in: run through e2e-dev.yml");
  test.setTimeout(420_000);
  test.beforeAll(() => {
    for (const [k, v] of Object.entries({ CLIENT, PARTNER, FORM_KEY, EMAIL })) {
      if (!v) throw new Error(`missing ${k}`);
    }
    if (!CLIENT.startsWith("https://") || !PARTNER.startsWith("https://")) throw new Error("origins must be https");
  });

  test("proposal accept, mark paid, portal, Stage 3 lock, e-sign v0", async ({ page, context }) => {
    // 1. Agreements.
    await page.goto(`${PARTNER}/settings/agreements`);
    await signIn(page, { email: EMAIL, password: PASSWORD || undefined, totpSecret: TOTP_SECRET || undefined });
    await landOnPartner(page, PARTNER);
    await page.goto(`${PARTNER}/settings/agreements`);
    await hydrated(page, "agreements-view");
    await publish(page, "booking_terms", `e2e-${RUN}`, `E2E ${RUN}: booking terms of the synthetic agency.`);
    await publish(page, "waiver", `e2e-${RUN}`, `E2E ${RUN}: I travel at my own risk.`);
    await shot(page, "01-agreements");

    // 2. The inquiry -> the trip at Proposal.
    const client = await context.newPage();
    await client.goto(`${CLIENT}/c/f/${FORM_KEY}`);
    await hydrated(client, "intake-form");
    await client.getByTestId("intake-dest-vietnam").click();
    await client.locator('select[name="budget_band"]').selectOption("2000_3500");
    await client.locator('select[name="trip_style"]').selectOption("relaxed");
    await client.locator('input[name="nights"]').fill("10");
    await client.locator('input[name="name"]').fill(PROSPECT.name);
    await client.locator('input[name="email"]').fill(PROSPECT.email);
    await client.getByTestId("intake-consent").check();
    await client.getByTestId("intake-submit").getByRole("button").click();
    await expect(client.getByTestId("intake-thanks")).toBeVisible({ timeout: 15_000 });
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
    await card.first().click();
    await hydrated(page, "crm-trip");
    const tripUrl = page.url();
    await page.getByTestId("trip-owner").selectOption({ label: "Me" });
    await hydrated(page, "crm-trip");
    await moveTo(page, "qualifying");
    await stageIs(page, "qualifying");
    await moveTo(page, "proposal");
    await expect(page.getByTestId("gate-dialog")).toBeVisible({ timeout: 10_000 });
    await page.getByTestId("gate-dialog").getByRole("button", { name: /fill in from call/i }).click();
    const fill = page.getByTestId("fill-dialog");
    await fill.getByTestId("plan-accommodation_tier").selectOption("comfort");
    await fill.getByTestId("plan-group_pref").selectOption("private");
    await fill.getByTestId("plan-fitness_band").selectOption("2_4h");
    await fill.getByTestId("plan-next").getByRole("button").click();
    await fill.getByTestId("plan-interests-culture").click();
    await fill.getByTestId("plan-occasion").selectOption("anniversary");
    await fill.getByTestId("plan-next").getByRole("button").click();
    await fill.getByTestId("plan-budget_flex").selectOption("some");
    await fill.getByTestId("plan-decision_maker").selectOption("partner");
    await fill.getByTestId("plan-booking_timeline").selectOption("within_month");
    await fill.getByTestId("plan-competing_quotes").getByLabel(/^no$/i).check();
    await fill.getByTestId("plan-next").getByRole("button").click();
    await fill.getByTestId("plan-submit").getByRole("button").click();
    await expect(fill).toHaveCount(0, { timeout: 10_000 });
    await page.goto(tripUrl);
    await hydrated(page, "crm-trip");
    await moveTo(page, "proposal");
    await stageIs(page, "proposal");

    // 3. Draft, send, the client accepts.
    const tab = page.getByTestId("trip-proposal");
    const form = tab.getByTestId("proposal-form");
    await form.locator('textarea[name="body"]').fill(`E2E ${RUN}: day 1 Hanoi, day 2 Ha Long Bay.`);
    await form.locator('input[name="total"]').fill("1000");
    await form.locator('input[name="deposit"]').fill("300");
    await form.getByTestId("date-input").fill("01/12/2027");
    await form.getByRole("button", { name: /save draft/i }).click();
    await expect(tab.getByTestId("proposal-current")).toHaveAttribute("data-status", "draft", { timeout: 15_000 });
    await tab.getByRole("button", { name: /send to the client/i }).click();
    const proposalLink = await tab.getByTestId("proposal-copy-link").first().inputValue({ timeout: 15_000 });
    expect(proposalLink).toMatch(/\/c\/p\/[A-Za-z0-9_-]{43}$/);
    await shot(page, "02-sent");
    await client.goto(proposalLink);
    await expect(client.getByTestId("client-proposal")).toHaveAttribute("data-status", "sent", { timeout: 15_000 });
    await expect(client.getByTestId("client-amounts")).toContainText("300.00");
    await client.locator('input[name="accept_terms"]').check();
    await client.locator('input[name="full_name"]').fill(PROSPECT.name);
    await shot(client, "03-accept");
    await client.getByTestId("client-accept").click();
    await expect(client.getByTestId("client-accepted")).toBeVisible({ timeout: 15_000 });

    // 4. Before the deposit: the lead's portal, Stage 3 locked (card R).
    await page.goto(tripUrl);
    await hydrated(page, "crm-trip");
    await expect(tab.getByTestId("proposal-current")).toHaveAttribute("data-status", "accepted", { timeout: 15_000 });
    await tab.getByTestId("portal-row").first().getByRole("button", { name: /portal link/i }).click();
    const earlyLink = await tab.getByTestId("proposal-copy-link").first().inputValue({ timeout: 15_000 });
    const echo = await portalSignIn(client, earlyLink);
    if (echo) {
      await expect(client.getByTestId("portal-stage3")).toHaveAttribute("data-locked", "true");
      const token = earlyLink.slice(earlyLink.lastIndexOf("/") + 1);
      const locked = await client.request.put(`${CLIENT}/crm/public/v1/portal/${token}/stage3`, {
        headers: { Origin: CLIENT, "Content-Type": "application/json" },
        data: { nationality: "AU" },
      });
      expect(locked.status()).toBe(403);
      expect((await locked.json()).detail).toBe("stage3_locked");
      await shot(client, "04-stage3-locked");
    } else {
      test.info().annotations.push({ type: "note", description: "PORTAL_OTP_DEV_ECHO is off: portal steps skipped" });
    }

    // 5. Mark the deposit paid: Booked, new portal links.
    await page.goto(tripUrl);
    await hydrated(page, "crm-trip");
    const deposit = tab.locator('[data-testid="schedule-row"][data-kind="deposit"]');
    await deposit.getByRole("button", { name: /mark paid/i }).click();
    await tab.getByTestId("mark-paid-form").getByRole("button", { name: /record payment/i }).click();
    await expect(tab.getByTestId("proposal-message")).toContainText(/booked/i, { timeout: 15_000 });
    const lateLink = await tab.getByTestId("proposal-copy-link").first().inputValue();
    await stageIs(page, "booked");
    await shot(page, "05-booked");

    // 6. Stage 3 and the waiver, then Pre-departure without the gate.
    if (echo && (await portalSignIn(client, lateLink))) {
      const stage3 = client.getByTestId("portal-stage3");
      await expect(stage3).toHaveAttribute("data-locked", "false");
      await client.getByTestId("portal-field-given_name").fill("E2E");
      await client.getByTestId("portal-field-family_name").fill(RUN);
      await client.getByTestId("portal-field-dob").fill("1984-03-02");
      await client.getByTestId("portal-field-nationality").fill("AU");
      await client.getByTestId("portal-field-passport_no").fill("PA1234567");
      await client.getByTestId("portal-field-passport_expiry").fill("2031-01-01");
      await client.getByTestId("portal-field-passport_country").fill("AU");
      await client.getByTestId("portal-field-emergency_contact").fill("Synthetic contact +61 400 000 000");
      await client.getByTestId("portal-stage3-save").click();
      await expect(stage3).toHaveAttribute("data-done", "true", { timeout: 15_000 });
      await client.getByTestId("portal-sign-code").click();
      const signCode = await devCode(client, "portal-sign-hint");
      expect(signCode).toBeTruthy();
      await client.getByTestId("portal-sign-agree").check();
      await client.getByTestId("portal-sign-name").fill(PROSPECT.name);
      await client.getByTestId("portal-sign-code-input").fill(signCode ?? "");
      await client.getByTestId("portal-sign").click();
      await expect(client.getByTestId("portal-waiver")).toHaveAttribute("data-signed", "true", { timeout: 15_000 });
      await shot(client, "06-portal-done");

      await page.goto(tripUrl);
      await hydrated(page, "crm-trip");
      await expect(tab.getByTestId("proposal-portal")).toHaveAttribute("data-s3", "100", { timeout: 15_000 });
      await moveTo(page, "pre_departure");
      await stageIs(page, "pre_departure");
      await expect(page.getByTestId("gate-dialog")).toHaveCount(0);
    }
    await client.close();

    // 7. Cancel the synthetic trip (it leaves the open board).
    await page.getByTestId("trip-cancel").getByRole("button").click();
    await page.getByTestId("cancel-reason").selectOption("duplicate");
    await expect(page.getByTestId("cancel-effects")).toHaveAttribute("data-dry-run", "true", { timeout: 10_000 });
    await page.getByTestId("cancel-typed").fill("CANCEL");
    await page.getByTestId("cancel-confirm").getByRole("button").click();
    await stageIs(page, "cancelled");
    await shot(page, "07-cancelled");
  });
});
