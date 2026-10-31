// e2e/tests/client-proposal.spec.ts — AA-950, card CRM-21b (client proposal + accept + hold) on Dev, one synthetic
// trip. Prepared for e2e-dev.yml (it runs every spec of e2e/tests); NOT run by the lane that wrote it.
//   1. a prospect submits the inquiry form for Nepal (2 adults); the owner takes the trip, fills Stage 2 in from a call
//      and moves it to Proposal;
//   2. a day-by-day version through the BFF: day 1 date inside the AA-973 seed contract window (today + 30), a balance
//      date, two lodging options on night 1 (the first included), a dinner on day 2 that may be chosen later (Fix B9);
//      optional (E2ECRMAATOUR): an AA day tour from a live quote, so the hold set is placed on Booking; Publish v1;
//   3. Proposal tab > "Send to client" -> the link shown once;
//   4. the client on a phone (390 x 844): the link lands on /c/p with the token gone from the URL and the host-only
//      httpOnly cookie on the API's proposal path (Fix B11); C-03: the "Before you go" day 0 first (AA-951), day 1 in
//      Nepal time (UTC+05:45), "Included" / "+120.00 USD"; no sideways scroll;
//      Swap to the suite -> the total moves by 120; Choose later on the dinner;
//   5. card E: a second device opens the same link and sees the same choice;
//   6. the agent: "Viewed" on the version, the client's choices (R19), the bell has items;
//   7. Fix B11: "Re-issue link" -> the old link (and the devices that exchanged it) answer "link does not work";
//   8. C-04 on the new link: the hold box (AA item: "Check availability and continue" -> held until …; no AA item:
//      nothing to hold), the groups (R26), rooms confirmed, terms, full name -> accepted; the agent sees the accepted
//      total;
//   9. clean-up: the trip goes to the trash.
// Card R (accept after the hold expired -> 409 hold_expired with the re-quote offer; a 4th re-check in a day -> "Ask
// your agent", no hold) and the sold-out fallback of card E need Booking's clock or stock: they run on PostgreSQL with
// a fake Booking (api/tests/integration/test_client_proposal_api.py).
//
// Dev prerequisites: crm-021 applied, the AA-950 API + web deployed. Environment: as proposal-builder.spec.ts
// (E2ECRM, E2ECRMCLIENT, E2ECRMPARTNER, E2ECRMFORMKEY, E2ECRMEMAIL, E2ECRMPASSWORD, E2ECRMTOTP); optional
// E2ECRMAATOUR (the AA-973 seed tour, with a Booking partner row for the synthetic agency). Dates stay inside the seed
// contract window (lesson AA-949: a day 120 days out was refused).
import { expect, test, type Browser, type BrowserContext, type Page } from "@playwright/test";
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
const SHOTS = "results";
const RUN = Date.now().toString(36);
const PROSPECT = { name: `E2E ${RUN} Client`, email: `e2e.client.${RUN}@example.invalid` };
const CARD_TOKEN = `E2E ${RUN} `;
const PHONE = { width: 390, height: 844 };

interface Answer {
  status: number;
  body: any;
}

async function shot(page: Page, name: string) {
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: `${SHOTS}/client-proposal-${name}.png`, fullPage: true });
}

/** On a failure, one screenshot of a CRM page (agency or client host) into results/, never the managed login. */
async function shotOnFailure(page: Page, title: string) {
  try {
    const origin = new URL(page.url()).origin;
    if (page.isClosed() || ![PARTNER, CLIENT].some((o) => o && new URL(o).origin === origin)) return;
    await shot(page, `failed-${title.replace(/[^a-z0-9]+/gi, "-").slice(0, 40)}`);
  } catch {
    // best effort: never mask the failure itself
  }
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

function inputDate(daysAhead: number): string {
  return new Date(Date.now() + daysAhead * 86_400_000).toISOString().slice(0, 10);
}

async function phone(browser: Browser): Promise<{ context: BrowserContext; page: Page }> {
  const context = await browser.newContext({ viewport: PHONE, isMobile: true, hasTouch: true });
  return { context, page: await context.newPage() };
}

async function noSidewaysScroll(page: Page) {
  const width = await page.evaluate(() => document.documentElement.scrollWidth);
  expect(width, "the client page fits 390 px (no horizontal scroll)").toBeLessThanOrEqual(PHONE.width);
}

async function option(block: ReturnType<Page["getByTestId"]>, title: string) {
  return block.locator(`[data-testid="client-option"][data-title="${title}"]`);
}

test.describe("CRM-21b client proposal on Dev", () => {
  test.skip(process.env.E2ECRM !== "1", "opt-in: run through e2e-dev.yml");
  test.setTimeout(540_000);
  test.beforeAll(() => {
    for (const [k, v] of Object.entries({ CLIENT, PARTNER, FORM_KEY, EMAIL })) {
      if (!v) throw new Error(`missing ${k}`);
    }
    if (!CLIENT.startsWith("https://") || !PARTNER.startsWith("https://")) throw new Error("origins must be https");
  });
  test.afterEach(async ({ page }, testInfo) => {
    if (testInfo.status !== testInfo.expectedStatus) await shotOnFailure(page, testInfo.title);
  });

  test("send, cookie link, Keep / Swap, second device, re-issue, hold and accept, agent view", async ({ page, context, browser }) => {
    // 1. Sign in; the inquiry -> the trip at Proposal.
    await page.goto(`${PARTNER}/pipeline`);
    await signIn(page, { email: EMAIL, password: PASSWORD || undefined, totpSecret: TOTP_SECRET || undefined });
    await landOnPartner(page, PARTNER);
    const form = await context.newPage();
    await form.goto(`${CLIENT}/c/f/${FORM_KEY}`);
    await hydrated(form, "intake-form");
    await form.getByTestId("intake-dest-nepal").click();
    await form.locator('select[name="budget_band"]').selectOption("2000_3500");
    await form.locator('select[name="trip_style"]').selectOption("balanced");
    await form.locator('input[name="nights"]').fill("4");
    await form.locator('input[name="name"]').fill(PROSPECT.name);
    await form.locator('input[name="email"]').fill(PROSPECT.email);
    await form.getByTestId("intake-consent").check();
    expect(await submitIntake(form)).toBe(202);
    await form.close();
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

    // 2. The version through the BFF (the builder page itself is proposal-builder.spec.ts).
    const created = await bff(page, "POST", `trips/${tripId}/builder`, { currency: "USD" });
    expect(created.status, `builder answered ${created.status} ${created.body?.detail ?? ""}`).toBe(201);
    const proposalId: string = created.body.proposal_id;
    let v = created.body;
    const patch = async (path: string, body: Record<string, unknown>, method: "POST" | "PATCH" = "POST") => {
      const res = await bff(page, method, path, { seq: v.edit_seq, ...body });
      expect(res.status, `${method} ${path} answered ${res.status} ${res.body?.detail ?? ""}`).toBeLessThan(300);
      v = res.body;
      return res.body;
    };
    await patch(`proposals/${proposalId}/builder`, { starts_on: inputDate(30), balance_due_on: inputDate(10) }, "PATCH");
    const night1 = v.days.find((d: any) => d.day_no === 1).blocks[0].option_block_id;
    await patch(`proposals/${proposalId}/blocks/${night1}/options`, { title: "Garden lodge", sell_amount: "300.00", net_amount: "180.00", tier: "comfort" });
    await patch(`proposals/${proposalId}/blocks/${night1}/options`, { title: "Riverside suite", sell_amount: "420.00", net_amount: "260.00" });
    const day2 = v.days.find((d: any) => d.day_no === 2);
    await patch(`proposals/${proposalId}/days/${day2.proposal_day_id}/blocks`, { title: "Dinner", type: "meal" });
    const dinner = v.days.find((d: any) => d.day_no === 2).blocks.find((b: any) => b.title === "Dinner").option_block_id;
    await patch(`proposals/${proposalId}/blocks/${dinner}/options`, { title: "Cooking class dinner", sell_amount: "60.00", choose_later_allowed: true });
    let withAa = false;
    if (AA_TOUR) {
      const product = await bff(page, "GET", `aa/products/${encodeURIComponent(AA_TOUR)}?currency=USD`);
      const component = product.status === 200 ? (product.body.components as any[]).find((c) => c.kind === "trip_plan") : null;
      if (component) {
        await patch(`proposals/${proposalId}/days/${day2.proposal_day_id}/blocks`, { title: "Day tour", type: "activity" });
        const tour = v.days.find((d: any) => d.day_no === 2).blocks.find((b: any) => b.title === "Day tour").option_block_id;
        const added = await bff(page, "POST", `proposals/${proposalId}/blocks/${tour}/aa-options`, {
          seq: v.edit_seq,
          tour_source_id: AA_TOUR,
          component_id: component.component_id,
          component_kind: component.kind,
          sell_amount: "1500.00",
        });
        if (added.status === 201) {
          v = added.body;
          withAa = true;
        } else {
          test.info().annotations.push({ type: "note", description: `AA item not added: ${added.body?.detail}` });
        }
      } else {
        test.info().annotations.push({ type: "note", description: `AA product look-up ${product.status}: no AA item, no hold set` });
      }
    } else {
      test.info().annotations.push({ type: "note", description: "E2ECRMAATOUR not set: no AA item, the hold box says nothing to hold" });
    }
    await patch(`proposals/${proposalId}/publish`, {});
    expect(v.status).toBe("published");
    const included = Number(v.totals.sell_total);

    // 3. Send to client from the Proposal tab: the link shown once.
    await page.goto(tripUrl);
    await hydrated(page, "crm-trip");
    const tab = page.getByTestId("trip-proposal");
    await tab.getByTestId("builder-send").getByRole("button").click();
    const link = await tab.getByTestId("proposal-copy-link").first().inputValue({ timeout: 15_000 });
    expect(link).toMatch(/\/c\/p\/[A-Za-z0-9_-]{43}$/);

    // 4. The client's phone: the token leaves the URL for a host-only, httpOnly cookie on the API path (Fix B11).
    const first = await phone(browser);
    await first.page.goto(link);
    await expect(first.page).toHaveURL(new RegExp(`^${CLIENT}/c/p$`), { timeout: 15_000 });
    // context.cookies(url) returns only the cookies sent to that URL: ask for the API path the cookie is scoped to.
    const cookie = (await first.context.cookies(`${CLIENT}/crm/public/v1/proposal`)).find((c) => c.name === "aa_proposal_link");
    expect(cookie, "the link cookie").toBeTruthy();
    expect(cookie!.httpOnly && cookie!.secure && cookie!.sameSite === "Strict").toBe(true);
    expect(cookie!.path).toBe("/crm/public/v1/proposal");
    const client = first.page;
    await expect(client.getByTestId("client-proposal")).toHaveAttribute("data-kind", "builder", { timeout: 15_000 });
    const lodging = client.getByTestId("client-block").filter({ has: client.locator('[data-title="Garden lodge"]') });
    await expect(await option(lodging, "Garden lodge")).toHaveAttribute("data-chosen", "true");
    await expect((await option(lodging, "Riverside suite")).getByTestId("client-delta")).toHaveText("+120.00 USD");
    // AA-951 (R38): the page opens with the "Before you go" day 0 (the insurance reminder is always there), so the
    // zone is read on day 1 itself, never on the first day section.
    await expect(client.getByTestId("client-day").first()).toHaveAttribute("data-day", "0");
    await expect(client.getByTestId("client-before-you-go")).toBeVisible();
    await expect(client.locator('[data-testid="client-day"][data-day="1"]')).toContainText("UTC+05:45");
    await noSidewaysScroll(client);
    await shot(client, "1-c03-phone");
    await (await option(lodging, "Riverside suite")).getByTestId("client-swap").click();
    await expect(await option(lodging, "Riverside suite")).toHaveAttribute("data-chosen", "true", { timeout: 15_000 });
    await expect(client.getByTestId("client-total")).toContainText((included + 120).toLocaleString("en-AU", { minimumFractionDigits: 2 }));
    const dinnerBlock = client.getByTestId("client-block").filter({ hasText: "Cooking class dinner" });
    await dinnerBlock.getByTestId("client-later").click();
    await expect(dinnerBlock.getByTestId("client-later-badge")).toBeVisible({ timeout: 15_000 });
    await noSidewaysScroll(client);
    await shot(client, "2-swapped");

    // 5. Card E: the same link on a second device sees the same choice.
    const second = await phone(browser);
    await second.page.goto(link);
    await expect(second.page).toHaveURL(new RegExp(`^${CLIENT}/c/p$`), { timeout: 15_000 });
    const lodging2 = second.page.getByTestId("client-block").filter({ has: second.page.locator('[data-title="Garden lodge"]') });
    await expect(await option(lodging2, "Riverside suite")).toHaveAttribute("data-chosen", "true", { timeout: 15_000 });

    // 6. The agent: viewed, the client's choices (R19), the bell.
    await page.goto(tripUrl);
    await hydrated(page, "crm-trip");
    await expect(tab.getByTestId("builder-viewed")).toContainText(/viewed/i, { timeout: 15_000 });
    await tab.getByTestId("builder-activity").getByRole("button").click();
    await expect(tab.getByTestId("builder-choices")).toContainText("Riverside suite", { timeout: 15_000 });
    await expect(tab.getByTestId("builder-choices")).toContainText(/to choose later/);
    const bell = await bff(page, "GET", "bell");
    expect(bell.status).toBe(200);
    expect(bell.body.items.some((i: any) => i.trip_id === tripId && i.name === "proposal.option_chosen")).toBe(true);
    await expect(page.getByTestId("crm-bell")).not.toHaveAttribute("data-unseen", "0", { timeout: 15_000 });
    await shot(page, "3-agent");

    // 7. Fix B11: Re-issue link; every device that exchanged the old one loses it.
    await tab.getByTestId("builder-reissue").getByRole("button").click();
    const fresh = await tab.getByTestId("proposal-copy-link").first().inputValue({ timeout: 15_000 });
    expect(fresh).not.toBe(link);
    await second.page.reload();
    await expect(second.page.getByTestId("proposal-unavailable")).toHaveAttribute("data-status", "404", { timeout: 15_000 });
    await second.context.close();
    await client.goto(fresh);
    await expect(client).toHaveURL(new RegExp(`^${CLIENT}/c/p$`), { timeout: 15_000 });
    await expect(client.getByTestId("client-proposal")).toHaveAttribute("data-status", "sent", { timeout: 15_000 });

    // 8. C-04: the hold box, the groups, rooms, terms, name -> accepted.
    const holdBox = client.getByTestId("client-hold");
    if (withAa) {
      await expect(holdBox).toHaveAttribute("data-status", "missing");
      await client.getByTestId("client-hold-button").click();
      await expect(holdBox).toHaveAttribute("data-status", "active", { timeout: 60_000 });
      await expect(client.getByTestId("client-held-until")).toBeVisible();
    } else {
      await expect(holdBox).toHaveAttribute("data-status", "none");
      await client.getByTestId("client-continue").click();
    }
    const accept = client.getByTestId("client-accept-form");
    await expect(accept.getByTestId("client-groups")).toContainText("Your agent books");
    await accept.getByTestId("client-rooms").locator("input").check();
    await accept.locator('input[name="accept_terms"]').check();
    await accept.locator('input[name="full_name"]').fill(PROSPECT.name);
    await noSidewaysScroll(client);
    await shot(client, "4-c04");
    await client.getByTestId("client-accept").click();
    await expect(client.getByTestId("client-accepted")).toBeVisible({ timeout: 15_000 });
    await shot(client, "5-accepted");
    await first.context.close();
    await page.goto(tripUrl);
    await hydrated(page, "crm-trip");
    await expect(tab.getByTestId("builder-accepted-total")).toBeVisible({ timeout: 15_000 });

    // 9. Clean-up: the trip goes to the trash (a hold set on Booking runs out on its own: 48 h on Dev).
    const deleted = await bff(page, "DELETE", `trips/${tripId}`);
    expect(deleted.status, `trip delete answered ${deleted.status} ${deleted.body?.detail ?? ""}`).toBe(200);
  });
});
