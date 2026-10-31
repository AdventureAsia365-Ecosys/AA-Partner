// e2e/tests/slice2-m1m2.spec.ts — AA-943, card CRM-15 (Modules 1 + 2) Done when (Dev), one synthetic trip end to end:
//   1. the owner turns Stage 2 auto-send on and opens call windows (Settings > Forms);
//   2. a prospect submits the inquiry form with a call slot -> the card is on the board within about a minute;
//   3. the owner takes the trip and moves it to Qualifying: the planning form is auto-sent; Undo within 10 s takes the
//      trip back to Inquiry and cancels the queued message (card E);
//   4. moved again; Send again shows the planning-form link; the client page opens it and sends answers; a crafted
//      request with a hidden-branch answer is accepted (202) but the answer never reaches the profile (R31); a bad
//      token is 404 link_not_found;
//   5. Proposal below 80 % -> the gate dialog (card R); Fill in from call to 100 %; a bulk preview of the Qualifying
//      column (dry run only); Proposal OK;
//   6. Booked through the booking gate's override; Cancel: the dry run lists the effects and changes nothing, the run
//      cancels the trip (which also takes the synthetic trip off the open board);
//   7. the settings are put back (auto-send off, no call windows).
// Environment: as slice1.spec.ts (E2ECRM, E2ECRMCLIENT, E2ECRMPARTNER, E2ECRMFORMKEY, E2ECRMEMAIL, E2ECRMPASSWORD,
// E2ECRMTOTP). Robustness rules of slice1 apply: wait for `data-hydrated="true"` before a click, test ids not text,
// the card found by a run-unique token. The form definition is memoised by the web for 60 s, so the call-slot picker
// can take up to a minute to show the new windows: the page is reloaded until it does.
import { expect, test, type Page } from "@playwright/test";
import fs from "node:fs";
import { submitIntake } from "../helpers/intake";
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
const PROSPECT = { name: `E2E ${RUN} Planner`, email: `e2e.planner.${RUN}@example.invalid` };
const CARD_TOKEN = `E2E ${RUN} `;

async function shot(page: Page, name: string) {
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: `${SHOTS}/m1m2-${name}.png`, fullPage: true });
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

/** Settings > Forms: auto-send and the call windows (every day 08:00-20:00 when `on`, none when off). */
async function pipelineSettings(page: Page, on: boolean) {
  await page.goto(`${PARTNER}/settings/forms`);
  const panel = page.getByTestId("pset");
  await expect(panel.getByTestId("pset-autosend")).toBeVisible({ timeout: 15_000 });
  await panel.getByTestId("pset-autosend").setChecked(on);
  const windows = panel.getByTestId("pset-window");
  while ((await windows.count()) > 0) await windows.first().getByRole("button").click();
  if (on) {
    for (let day = 1; day <= 7; day++) {
      await panel.getByTestId("pset-add-window").getByRole("button").click();
      const row = windows.nth(day - 1);
      await row.getByTestId("pset-day").selectOption(String(day));
      await row.getByTestId("pset-from").fill("08:00");
      await row.getByTestId("pset-to").fill("20:00");
    }
    await panel.getByTestId("pset-duration").fill("30");
    await panel.getByTestId("pset-horizon").fill("14");
  }
  // AA-954 live check: the save is proven by the PUT answer and by the values after a reload, never only by the
  // transient "Saved" marker. The marker is lost when a web deploy is rolling: router.refresh() then hard-reloads the
  // page (deployment skew) and the panel remounts without it.
  const put = page.waitForResponse(
    (r) => r.request().method() === "PUT" && new URL(r.url()).pathname === "/bff/settings/pipeline",
    { timeout: 15_000 },
  );
  await panel.getByTestId("pset-save").getByRole("button").click();
  const answer = await put;
  expect(answer.status(), `PUT settings/pipeline answered ${answer.status()}`).toBe(200);
  const marker = await panel
    .getByTestId("pset-saved")
    .waitFor({ state: "visible", timeout: 5_000 })
    .then(() => true)
    .catch(() => false);
  if (!marker) test.info().annotations.push({ type: "note", description: "pset-saved marker not seen (page reloaded?); the reload check decides" });
  await page.goto(`${PARTNER}/settings/forms`);
  await expect(panel.getByTestId("pset-autosend")).toBeVisible({ timeout: 15_000 });
  if (on) await expect(panel.getByTestId("pset-autosend")).toBeChecked();
  else await expect(panel.getByTestId("pset-autosend")).not.toBeChecked();
  await expect(windows).toHaveCount(on ? 7 : 0);
  if (on) {
    await expect(panel.getByTestId("pset-duration")).toHaveValue("30");
    await expect(panel.getByTestId("pset-horizon")).toHaveValue("14");
  }
}

test.describe("CRM-15 modules 1 + 2 on Dev", () => {
  test.skip(process.env.E2ECRM !== "1", "opt-in: run through e2e-dev.yml");
  test.setTimeout(480_000);
  test.beforeAll(() => {
    for (const [k, v] of Object.entries({ CLIENT, PARTNER, FORM_KEY, EMAIL })) {
      if (!v) throw new Error(`missing ${k}`);
    }
    if (!CLIENT.startsWith("https://") || !PARTNER.startsWith("https://")) throw new Error("origins must be https");
  });

  test("stage engine, planning form, call slot, gates, undo, bulk preview and cancel", async ({ page, context }) => {
    // 1. Owner settings.
    await page.goto(`${PARTNER}/settings/forms`);
    await signIn(page, { email: EMAIL, password: PASSWORD || undefined, totpSecret: TOTP_SECRET || undefined });
    await landOnPartner(page, PARTNER);
    await pipelineSettings(page, true);
    await shot(page, "01-settings");

    try {
      // 2. The inquiry, with a call slot (the web's 60 s form memo: reload until the picker lists the windows).
      const client = await context.newPage();
      const slot = client.getByTestId("intake-call-slot");
      await expect
        .poll(
          async () => {
            await client.goto(`${CLIENT}/c/f/${FORM_KEY}`);
            await hydrated(client, "intake-form");
            return (await slot.count()) > 0 ? slot.locator("option").count() : 0;
          },
          { timeout: 120_000, intervals: [10_000, 15_000] },
        )
        .toBeGreaterThan(1);
      await client.getByTestId("intake-dest-vietnam").click();
      await client.locator('select[name="budget_band"]').selectOption("2000_3500");
      await client.locator('select[name="trip_style"]').selectOption("relaxed"); // no adventure branch
      await client.locator('input[name="nights"]').fill("10");
      await client.locator('input[name="name"]').fill(PROSPECT.name);
      await client.locator('input[name="email"]').fill(PROSPECT.email);
      await slot.selectOption({ index: 1 });
      await client.getByTestId("intake-consent").check();
      await shot(client, "02-inquiry-with-slot");
      expect(await submitIntake(client)).toBe(202); // waits out the per-address limit once
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
      await expect(page.getByTestId("column-inquiry").getByTestId("trip-card").filter({ hasText: CARD_TOKEN })).toHaveAttribute("data-waiting", "agency");
      await shot(page, "03-board");

      // 3. Take the trip; Qualifying auto-sends the planning form; Undo within 10 s cancels it.
      await card.first().click();
      await hydrated(page, "crm-trip");
      const tripUrl = page.url();
      await expect(page.locator('[data-testid="trip-task"][data-kind="call_scheduled"]')).toHaveCount(1); // R15 slot booked
      await page.getByTestId("trip-owner").selectOption({ label: "Me" });
      await hydrated(page, "crm-trip");
      await moveTo(page, "qualifying");
      const toast = page.getByTestId("undo-toast");
      await expect(toast).toHaveAttribute("data-stage2-sent", "true", { timeout: 10_000 });
      await shot(page, "04-qualifying-auto-sent");
      await page.getByTestId("undo-toast-undo").click();
      await expect(toast).toHaveAttribute("data-undone-stage", "inquiry", { timeout: 10_000 });
      expect(Number(await toast.getAttribute("data-dispatch-cancelled"))).toBeGreaterThanOrEqual(1);
      await stageIs(page, "inquiry");
      await shot(page, "05-undone");

      // 4. Again to Qualifying; Send again shows the link; the client page answers it.
      await moveTo(page, "qualifying");
      await stageIs(page, "qualifying");
      await page.getByTestId("info-send").getByRole("button").click();
      const linkUrl = await page.getByTestId("info-link-url").inputValue({ timeout: 10_000 });
      expect(linkUrl).toMatch(new RegExp(`^${CLIENT.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}/c/s/[A-Za-z0-9_-]{43}$`));
      const token = linkUrl.slice(linkUrl.lastIndexOf("/") + 1);
      await client.goto(linkUrl);
      await hydrated(client, "plan-form");
      await expect(client.getByTestId("plan-page-body")).toHaveAttribute("lang", "en");
      expect(await client.getByTestId("plan-form").getAttribute("data-pages")).not.toContain("health "); // consent first
      await expect(client.getByTestId("plan-trek_experience")).toHaveCount(0); // not an adventure trip
      await client.getByTestId("plan-accommodation_tier").selectOption("comfort");
      await shot(client, "06-planning-form");
      for (let i = 0; i < 3; i++) await client.getByTestId("plan-next").getByRole("button").click();
      await client.getByTestId("plan-submit").getByRole("button").click();
      await expect(client.getByTestId("plan-thanks")).toBeVisible({ timeout: 15_000 });
      // A crafted answer of a hidden branch is accepted and dropped (counted server side, never stored).
      const crafted = await client.request.post(`${CLIENT}/crm/public/v1/s/${token}`, {
        headers: { Origin: CLIENT, "Content-Type": "application/json" },
        data: { trek_experience: "experienced", altitude_ok: true, health_consent: false, client_submission_id: `e2e-${RUN}-crafted` },
      });
      expect(crafted.status()).toBe(202);
      const bad = await client.request.get(`${CLIENT}/crm/public/v1/s/${"x".repeat(43)}`);
      expect(bad.status()).toBe(404);
      expect((await bad.json()).detail).toBe("link_not_found");
      await client.close();

      // 5. Proposal below 80 % -> the gate; fill in from call; bulk preview; Proposal.
      await page.goto(tripUrl);
      await hydrated(page, "crm-trip");
      await expect(page.getByTestId("info-link")).toHaveAttribute("data-status", "submitted");
      await expect(page.getByTestId("info-answers").locator('[data-field="trek_experience"]')).toHaveCount(0);
      await moveTo(page, "proposal");
      await expect(page.getByTestId("gate-dialog")).toBeVisible({ timeout: 10_000 });
      await expect(page.getByTestId("gate-dialog-body")).toHaveAttribute("data-gate", "stage2");
      await shot(page, "07-gate");
      await page.getByTestId("gate-dialog").getByRole("button", { name: /fill in from call/i }).click();
      const fill = page.getByTestId("fill-dialog");
      await expect(fill).toBeVisible();
      await fill.getByTestId("plan-accommodation_tier").selectOption("comfort");
      await fill.getByTestId("plan-group_pref").selectOption("private");
      await fill.getByTestId("plan-fitness_band").selectOption("2_4h");
      await fill.getByTestId("plan-next").getByRole("button").click();
      await fill.getByTestId("plan-interests-culture").click();
      await fill.getByTestId("plan-interests-food").click();
      await fill.getByTestId("plan-occasion").selectOption("anniversary");
      await fill.getByTestId("plan-next").getByRole("button").click();
      await fill.getByTestId("plan-budget_flex").selectOption("some");
      await fill.getByTestId("plan-decision_maker").selectOption("partner");
      await fill.getByTestId("plan-booking_timeline").selectOption("within_month");
      await fill.getByTestId("plan-competing_quotes").getByLabel(/^no$/i).check();
      await fill.getByTestId("plan-next").getByRole("button").click();
      await fill.getByTestId("plan-submit").getByRole("button").click();
      await expect(fill).toHaveCount(0, { timeout: 10_000 });
      await expect(page.getByTestId("info-s2")).toHaveAttribute("data-percent", "100", { timeout: 15_000 });
      await expect(page.locator('[data-testid="trip-task"][data-kind="get_planning_info"][data-status="open"]')).toHaveCount(0);
      await shot(page, "08-filled-from-call");

      await page.goto(`${PARTNER}/pipeline`);
      await page.getByTestId("column-menu-qualifying").locator("summary").click();
      await page.getByTestId("bulk-to-proposal").click();
      const preview = page.getByTestId("bulk-preview");
      await expect(preview).toBeVisible({ timeout: 10_000 });
      expect(Number(await preview.getAttribute("data-eligible"))).toBeGreaterThanOrEqual(1);
      await shot(page, "09-bulk-preview");
      await page.getByTestId("bulk-dialog").getByRole("button", { name: /^cancel$/i }).click(); // a preview only

      await page.goto(tripUrl);
      await hydrated(page, "crm-trip");
      await moveTo(page, "proposal");
      await stageIs(page, "proposal");

      // 6. Booked through the booking gate's override; cancel: the dry run first, then the run.
      await moveTo(page, "booked");
      await expect(page.getByTestId("gate-dialog-body")).toHaveAttribute("data-gate", "booking", { timeout: 10_000 });
      await page.getByTestId("gate-confirm").getByRole("button").click(); // manual_booking, the only reason
      await stageIs(page, "booked");
      await page.getByTestId("trip-cancel").getByRole("button").click();
      await page.getByTestId("cancel-reason").selectOption("duplicate");
      const effects = page.getByTestId("cancel-effects");
      await expect(effects).toHaveAttribute("data-dry-run", "true", { timeout: 10_000 });
      await expect(effects.locator('li[data-kind="call_slots_cancelled"]')).toHaveAttribute("data-count", "1");
      await shot(page, "10-cancel-dry-run");
      await stageIs(page, "booked"); // the dry run changed nothing
      await page.getByTestId("cancel-typed").fill("CANCEL");
      await page.getByTestId("cancel-confirm").getByRole("button").click();
      await stageIs(page, "cancelled");
      await shot(page, "11-cancelled");
    } finally {
      // 7. Put the seeded agency back (slice1 expects no auto-send).
      await pipelineSettings(page, false);
    }
  });
});
