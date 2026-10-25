// e2e/tests/aa-lines.spec.ts — AA-947, card CRM-19 (B2B client + AA lines) on Dev, one synthetic trip:
//   1. a prospect submits the inquiry form; the owner opens the trip;
//   2. Bookings tab > "Add AA item": the AA-973 seed tour (E2ECRMAATOUR) found in the catalog cache, "The whole tour" on
//      our own date, 2 adults, USD, a sell per person below AA's public "from" price -> the form warns (never blocks);
//      the ask answers the line as "AA pending" (a quote request), and Booking's quote turns it "AA quoted": cost in USD
//      with fx source aa_quote_lock, the "from"-price warning on the row;
//   3. card R: PATCH of the AA line -> 403 aa_line_read_only; a hand-typed line with the agency's AA supplier -> 403
//      aa_line_read_only;
//   4. Hold with AA -> "Held by AA" (Booking hold id); the trip delete is refused 409 has_aa_line (the trip_commitment);
//   5. (optional) the agency's assistant asks to confirm -> 403 role_forbidden (needs E2ECRMASSISTANT*; else a note);
//   6. Lost, then Undo inside the 10 s window -> after the 15 s release delay the line is still held (same hold id);
//   7. Lost for good -> the release goes to Booking -> "Released"; the trip is no longer committed, so it can be
//      deleted (to the trash: that is also the run's clean-up).
// Card E (Booking down -> "AA pending", retried without duplicates) stays in the integration tests
// (api/tests/integration/test_aa_lines_api.py): Dev is never pointed at a broken Booking URL.
//
// Dev prerequisites (outside this repo; the orchestrator's one-off setup in the PR body): crm-017 applied and the AA-947
// API + web deployed; the AA-973 synthetic supply on Booking (AA-CIS-Infra db/scripts/aa973_b2b_seed.py: the tour, its
// components, a USD contract with rates in force on the chosen date); a booking.b2b_partners row for the synthetic agency
// (tenant = the CRM agency id, client = the CRM M2M client) and a per-person price hint on the seed plan (else no "from"
// price exists to warn against). The spec checks both first and fails with what is missing.
//
// Environment: as lifecycle-thin.spec.ts (E2ECRM, E2ECRMCLIENT, E2ECRMPARTNER, E2ECRMFORMKEY, E2ECRMEMAIL,
// E2ECRMPASSWORD, E2ECRMTOTP), plus
//   E2ECRMAATOUR                  the CIS source_id of the AA-973 seed tour (variable)
//   E2ECRMASSISTANTEMAIL          (optional, variable) the same agency's assistant e2e user
//   E2ECRMASSISTANTPASSWORD/TOTP  (optional, secrets) that user's partner-pool password and authenticator secret
import { expect, test, type Browser, type Page } from "@playwright/test";
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
const AA_TOUR = env("E2ECRMAATOUR");
const ASSISTANT = {
  email: env("E2ECRMASSISTANTEMAIL"),
  password: (process.env.E2ECRMASSISTANTPASSWORD ?? "").trim(),
  totpSecret: (process.env.E2ECRMASSISTANTTOTP ?? "").trim(),
};
const SHOTS = "results";
const RUN = Date.now().toString(36);
const PROSPECT = { name: `E2E ${RUN} AA lines`, email: `e2e.aalines.${RUN}@example.invalid` };
const CARD_TOKEN = `E2E ${RUN} `;
const ADULTS = 2;
const CURRENCY = "USD"; // the AA-973 seed contract is in USD: no FX row is needed on the Booking side
// app.booking.aa_lines.RELEASE_DELAY_S (15 s after a Lost move) + the nudge at 16 s + a margin for the job runner.
const RELEASE_WAIT_MS = 30_000;
// Booking answers a nudged request in seconds; the minute run of crm_booking_sync is the fallback.
const SYNC_TIMEOUT_MS = 150_000;

interface Answer {
  status: number;
  body: any;
}

interface AaView {
  sub_status: string | null;
  hold_id: string | null;
  booking_id: string | null;
  from_price: { amount: string; currency: string } | null;
  below_from_price: boolean | null;
  pending: { kind: string; attempts: number; last_error_class: string | null } | null;
  actions: string[];
}

interface LineView {
  booking_line_id: string;
  status: string;
  title: string;
  read_only: boolean;
  supplier_kind: string;
  aa?: AaView;
  money?: Record<string, { amount: string; currency: string; fx?: { source: string; rate: string } } | null>;
}

async function shot(page: Page, name: string) {
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: `${SHOTS}/aa-lines-${name}.png`, fullPage: true });
}

async function hydrated(page: Page, testId: string) {
  await expect(page.getByTestId(testId)).toHaveAttribute("data-hydrated", "true", { timeout: 15_000 });
}

async function stageIs(page: Page, stage: string) {
  await expect(page.getByTestId("trip-stage")).toHaveAttribute("data-stage", stage, { timeout: 15_000 });
}

/** A same-origin call of the CRM web's BFF from the signed-in page (the browser's own cookies and Origin). */
async function bff(page: Page, method: "GET" | "POST" | "PATCH" | "DELETE", path: string, body?: unknown): Promise<Answer> {
  return page.evaluate(
    async ({ method, path, body }) => {
      const res = await fetch(`/bff/${path}`, {
        method,
        headers: body === undefined ? { Accept: "application/json" } : { Accept: "application/json", "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
        credentials: "same-origin",
      });
      return { status: res.status, body: await res.json().catch(() => null) };
    },
    { method, path, body },
  );
}

async function line(page: Page, tripId: string, lineId: string): Promise<LineView> {
  const res = await bff(page, "GET", `trips/${tripId}/lines`);
  expect(res.status, "GET trips/{id}/lines").toBe(200);
  const found = (res.body.lines as LineView[]).find((l) => l.booking_line_id === lineId);
  if (!found) throw new Error(`line ${lineId} is not on trip ${tripId}`);
  return found;
}

/** "<sub_status>/<pending kind or ->" of the AA line: what the sync has made of it so far. */
async function aaState(page: Page, tripId: string, lineId: string): Promise<string> {
  const aa = (await line(page, tripId, lineId)).aa;
  return `${aa?.sub_status ?? "-"}/${aa?.pending?.kind ?? "-"}`;
}

async function waitForAa(page: Page, tripId: string, lineId: string, expected: string): Promise<LineView> {
  await expect
    .poll(() => aaState(page, tripId, lineId), { timeout: SYNC_TIMEOUT_MS, intervals: [3_000, 5_000, 10_000] })
    .toBe(expected);
  return line(page, tripId, lineId);
}

async function openTrip(page: Page, url: string) {
  await page.goto(url);
  await hydrated(page, "crm-trip");
  await expect(page.getByTestId("trip-bookings")).toBeVisible({ timeout: 15_000 });
}

async function markLost(page: Page) {
  await page.getByTestId("trip-moves").getByRole("button", { name: /^mark as lost$/i }).click();
  await page.getByTestId("trip-lost-reason").selectOption("price");
  await page.getByTestId("trip-moves").getByRole("button", { name: /^mark lost$/i }).click();
}

/** The agency's assistant asks AA to confirm the line: only owners and agents may (403 role_forbidden). */
async function assistantCannotConfirm(browser: Browser, lineId: string) {
  const context = await browser.newContext();
  try {
    const page = await context.newPage();
    await page.goto(`${PARTNER}/pipeline`);
    await signIn(page, ASSISTANT);
    await landOnPartner(page, PARTNER);
    await page.goto(`${PARTNER}/pipeline`);
    const me = await bff(page, "GET", "me");
    expect(me.status).toBe(200);
    expect(me.body.member_role, "E2ECRMASSISTANTEMAIL must be the agency's assistant").toBe("assistant");
    const refused = await bff(page, "POST", `lines/${lineId}/aa/confirm`);
    expect(refused.status).toBe(403);
    expect(refused.body.detail).toBe("role_forbidden");
  } finally {
    await context.close();
  }
}

test.describe("CRM-19 AA lines on Dev", () => {
  test.skip(process.env.E2ECRM !== "1", "opt-in: run through e2e-dev.yml");
  test.setTimeout(720_000);
  test.beforeAll(() => {
    for (const [k, v] of Object.entries({ CLIENT, PARTNER, FORM_KEY, EMAIL, AA_TOUR })) {
      if (!v) throw new Error(`missing ${k}`);
    }
    if (!CLIENT.startsWith("https://") || !PARTNER.startsWith("https://")) throw new Error("origins must be https");
  });

  test("quote, from-price warning, read-only, hold, Lost with Undo, Lost releases", async ({ page, context, browser }) => {
    // 0. Sign in; the Dev preconditions (Booking partner row, the seed product with a "from" price).
    await page.goto(`${PARTNER}/pipeline`);
    await signIn(page, { email: EMAIL, password: PASSWORD || undefined, totpSecret: TOTP_SECRET || undefined });
    await landOnPartner(page, PARTNER);
    await page.goto(`${PARTNER}/pipeline`);
    const product = await bff(page, "GET", `aa/products/${encodeURIComponent(AA_TOUR)}?currency=${CURRENCY}`);
    expect(product.status, `AA product look-up answered ${product.status} ${product.body?.detail ?? ""} (aa_partner_not_active = no Booking partner row for this agency: run the AA-947 E2E setup)`).toBe(200);
    expect(product.body.components.some((c: { kind: string }) => c.kind === "trip_plan"), "the seed tour has a whole-tour component").toBe(true);
    const fromPrice = product.body.from_price as { amount: string; currency: string } | null;
    expect(fromPrice, "no AA 'from' price for the seed tour: set its per-person price hint (AA-947 E2E setup)").not.toBeNull();
    expect(fromPrice?.currency).toBe(CURRENCY);
    // Half the "from" price per person: below it on purpose (the warning), still a positive sell.
    const sell = ((Number(fromPrice?.amount) * ADULTS) / 2).toFixed(2);
    const suppliers = await bff(page, "GET", "suppliers");
    expect(suppliers.status).toBe(200);
    const aaSupplier = (suppliers.body.suppliers as { agency_supplier_id: string; kind: string }[]).find((s) => s.kind === "aa");
    expect(aaSupplier, "the built-in Adventure Asia supplier").toBeTruthy();

    // 1. The inquiry -> the trip.
    const client = await context.newPage();
    await client.goto(`${CLIENT}/c/f/${FORM_KEY}`);
    await hydrated(client, "intake-form");
    await client.getByTestId("intake-dest-vietnam").click();
    await client.locator('select[name="budget_band"]').selectOption("2000_3500");
    await client.locator('select[name="trip_style"]').selectOption("relaxed");
    await client.locator('input[name="nights"]').fill("6");
    await client.locator('input[name="name"]').fill(PROSPECT.name);
    await client.locator('input[name="email"]').fill(PROSPECT.email);
    await client.getByTestId("intake-consent").check();
    expect(await submitIntake(client)).toBe(202); // waits out the per-address limit once
    await expect(client.getByTestId("intake-thanks")).toBeVisible({ timeout: 15_000 });
    await client.close();
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
    const tripId = tripUrl.split("/").pop()?.split("?")[0] ?? "";
    expect(tripId).toMatch(/^[0-9a-f-]{36}$/);
    await page.getByTestId("trip-owner").selectOption({ label: "Me" });
    await hydrated(page, "crm-trip");
    await stageIs(page, "inquiry");
    const baseCurrency = (await bff(page, "GET", `trips/${tripId}/lines`)).body.base_currency as string;

    // 2. Add AA item: the seed tour, the whole tour on our own date, 2 adults, USD, a sell below the "from" price.
    const bookings = page.getByTestId("trip-bookings");
    await bookings.getByRole("button", { name: /^add aa item$/i }).click();
    const form = page.getByTestId("aa-add-form");
    await expect(async () => {
      await form.getByTestId("aa-search").fill(AA_TOUR);
      await page.waitForTimeout(250);
      await expect(form.getByTestId("aa-search")).toHaveValue(AA_TOUR);
    }).toPass({ timeout: 15_000 });
    await form.getByRole("button", { name: /^search$/i }).click();
    await expect(form.getByTestId("aa-tour").locator(`option[value="${AA_TOUR}"]`)).toHaveCount(1, { timeout: 15_000 });
    const firstLoad = page.waitForResponse((r) => r.url().includes(`/bff/aa/products/`) && r.request().method() === "GET", { timeout: 30_000 });
    await form.getByTestId("aa-tour").selectOption(AA_TOUR);
    expect((await firstLoad).status()).toBe(200);
    await expect(form.getByTestId("aa-component")).toBeVisible({ timeout: 15_000 });
    if (baseCurrency !== CURRENCY) {
      // The form starts in the agency's base currency; USD reloads the product (its "from" price in USD).
      const usdLoad = page.waitForResponse((r) => r.url().includes(`currency=${CURRENCY}`) && r.request().method() === "GET", { timeout: 30_000 });
      const currency = form.getByLabel("Currency", { exact: true });
      await currency.fill(CURRENCY);
      await currency.blur();
      expect((await usdLoad).status()).toBe(200);
      await expect(form.getByTestId("aa-component")).toBeVisible({ timeout: 15_000 });
    }
    const componentLabel = await form.getByTestId("aa-component").locator("option:checked").textContent();
    expect(componentLabel ?? "").toMatch(/whole tour/i);
    const departure = form.getByTestId("aa-departure");
    if ((await departure.count()) > 0) await expect(departure).toHaveValue(""); // "Our own date": no seat count involved
    // A date inside the seed contract (today .. today + 90, AA-973): three weeks ahead, ISO (read the same in every locale).
    const serviceDate = new Date(Date.now() + 21 * 86_400_000).toISOString().slice(0, 10);
    await form.getByTestId("date-input").fill(serviceDate);
    await expect(form.getByTestId("date-read-as")).not.toBeEmpty();
    await form.getByLabel("Adults", { exact: true }).fill(String(ADULTS));
    await form.getByLabel("Children", { exact: true }).fill("0");
    await form.getByTestId("aa-sell").fill(sell);
    await expect(form.getByTestId("aa-from-warning")).toBeVisible({ timeout: 5_000 });
    await shot(page, "01-add-form-from-warning");

    const asked = page.waitForResponse(
      (r) => r.request().method() === "POST" && new URL(r.url()).pathname === `/bff/trips/${tripId}/aa-lines`,
      { timeout: 30_000 },
    );
    await form.getByRole("button", { name: /^ask aa for a price$/i }).click();
    const askedRes = await asked;
    expect(askedRes.status()).toBe(201);
    const created = (await askedRes.json()) as LineView;
    const lineId = created.booking_line_id;
    // "AA pending": the line exists at once, read-only, waiting for Booking's quote.
    expect(created.supplier_kind).toBe("aa");
    expect(created.read_only).toBe(true);
    expect(created.aa?.sub_status).toBe("pending");
    expect(created.aa?.pending?.kind).toBe("quote");
    await shot(page, "02-aa-pending");

    // Booking's quote: "AA quoted", cost and sell under the quote's FX lock, the "from"-price warning on the row.
    const quoted = await waitForAa(page, tripId, lineId, "quoted/-");
    expect(quoted.money?.cost?.currency).toBe(CURRENCY);
    expect(Number(quoted.money?.cost?.amount)).toBeGreaterThan(0);
    expect(quoted.money?.cost?.fx?.source).toBe("aa_quote_lock");
    expect(Number(quoted.money?.sell?.amount)).toBe(Number(sell));
    expect(quoted.money?.sell?.fx?.source).toBe("aa_quote_lock");
    expect(quoted.aa?.from_price?.currency).toBe(CURRENCY);
    expect(Number(quoted.aa?.from_price?.amount)).toBe(Number(fromPrice?.amount));
    expect(quoted.aa?.below_from_price).toBe(true);
    expect(quoted.aa?.actions).toContain("hold");
    await openTrip(page, tripUrl);
    const row = page.getByTestId(`line-${lineId}`);
    await expect(row.getByTestId("aa-status")).toContainText(/AA quoted/i);
    await expect(row.getByTestId("aa-below-from")).toBeVisible();
    await shot(page, "03-quoted");

    // 3. Card R: an AA line is never edited, and never typed by hand.
    const edit = await bff(page, "PATCH", `lines/${lineId}`, { title: `E2E ${RUN} edited` });
    expect(edit.status).toBe(403);
    expect(edit.body.detail).toBe("aa_line_read_only");
    const typed = await bff(page, "POST", `trips/${tripId}/lines`, {
      agency_supplier_id: aaSupplier?.agency_supplier_id,
      title: `E2E ${RUN} typed AA line`,
      collect: "agency",
    });
    expect(typed.status).toBe(403);
    expect(typed.body.detail).toBe("aa_line_read_only");

    // 4. Hold with AA -> held; the hold commits the trip (delete refused, cancel offered instead).
    await row.getByRole("button", { name: /^hold with aa$/i }).click();
    await expect(page.getByTestId("ledger-message")).toContainText(/sent to aa/i, { timeout: 15_000 });
    const held = await waitForAa(page, tripId, lineId, "held/-");
    const holdId = held.aa?.hold_id;
    expect(holdId).toBeTruthy();
    expect(held.aa?.booking_id).toBeNull();
    await openTrip(page, tripUrl);
    await expect(row.getByTestId("aa-status")).toContainText(/held by aa/i);
    await shot(page, "04-held");
    const blocked = await bff(page, "DELETE", `trips/${tripId}`);
    expect(blocked.status).toBe(409);
    expect(blocked.body.blockers).toContain("has_aa_line");
    expect(blocked.body.alternative).toBe("cancel_trip");

    // 5. The assistant cannot confirm with AA (a confirm books for real): 403 role_forbidden.
    if (ASSISTANT.email && ASSISTANT.password && ASSISTANT.totpSecret) {
      await assistantCannotConfirm(browser, lineId);
    } else {
      test.info().annotations.push({
        type: "note",
        description: "no E2ECRMASSISTANT* user in this suite: the assistant's confirm refusal stays covered by test_aa_lines_api.py",
      });
    }

    // 6. Lost, then Undo inside the 10 s window: the queued release is cancelled, the hold stays.
    await openTrip(page, tripUrl);
    await markLost(page);
    const toast = page.getByTestId("undo-toast");
    await expect(toast).toHaveAttribute("data-stage", "lost", { timeout: 10_000 });
    await page.getByTestId("undo-toast-undo").click();
    await expect(toast).toHaveAttribute("data-undone-stage", "inquiry", { timeout: 10_000 });
    await stageIs(page, "inquiry");
    await page.waitForTimeout(RELEASE_WAIT_MS); // past the release delay: had the Undo failed, Booking would release now
    const kept = await line(page, tripId, lineId);
    expect(kept.aa?.sub_status).toBe("held");
    expect(kept.aa?.pending).toBeNull();
    expect(kept.aa?.hold_id).toBe(holdId);
    await openTrip(page, tripUrl);
    await expect(row.getByTestId("aa-status")).toContainText(/held by aa/i);
    await shot(page, "05-undo-keeps-hold");

    // 7. Lost for good: the hold goes back to Booking, the line reads "Released", the trip is no longer committed.
    await markLost(page);
    await expect(page.getByTestId("undo-toast")).toHaveAttribute("data-stage", "lost", { timeout: 10_000 });
    await stageIs(page, "lost");
    const released = await waitForAa(page, tripId, lineId, "released/-");
    expect(released.aa?.booking_id).toBeNull();
    await openTrip(page, tripUrl);
    await expect(row.getByTestId("aa-status")).toContainText(/released/i);
    await shot(page, "06-lost-released");
    const deleted = await bff(page, "DELETE", `trips/${tripId}`);
    expect(deleted.status, `trip delete after the release: ${JSON.stringify(deleted.body)}`).toBe(200);
  });
});
