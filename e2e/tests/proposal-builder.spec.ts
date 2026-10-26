// e2e/tests/proposal-builder.spec.ts — AA-949, card CRM-21a (proposal model + day-list builder) on Dev, one synthetic
// trip. Prepared for e2e-dev.yml (it runs every spec of e2e/tests); NOT run by the lane that wrote it.
//   1. a prospect submits the inquiry form for Nepal, 6 nights, 2 adults; the owner opens the trip;
//   2. Proposal tab > "Start the builder" (USD) -> the builder page: day 0 "Before you go" + 7 days, every Nepal day
//      in Asia/Kathmandu shown as UTC+05:45 (card E), a Lodging block per night with "Client may swap" on (Fix E1), the
//      rooms line from the party (Fix B10);
//   3. "+ Add" -> Agency item, twice on day 1 (sell + net): the first becomes the default; the totals bar shows sell,
//      net and margin (owner);
//   4. card R: a rule "Homestay excludes <the day 2 default>" through the BFF -> 422 option_conflict, nothing written;
//   5. §11.2: a write with a stale seq (another editor) -> 409 version_conflict {reason: edited}; the UI banner on a
//      stale page offers Reload;
//   6. (optional) the AA drawer: with E2ECRMAATOUR set and the AA-947 Booking partner row in place, an AA item is added
//      from a live quote with the owner's sell (net = Booking's b2b price, availability badge), then "Refresh AA
//      prices" from the page reports the prices it checked; else a note;
//   7. Settings: day 1 date + balance date; Publish v1 -> "Published"; a write to it -> 409 proposal_not_draft;
//   8. "New version from this" -> working copy v2 (lineage) -> Publish v2 -> v1 replaced;
//   9. clean-up: the trip goes to the trash (proposals block nothing).
// The client-pick conflict of card E (the client swaps on v1 while the agent edits v2) needs the client page of
// CRM-21b (AA-950); it is covered by api/tests/integration/test_builder_api.py until then.
//
// Dev prerequisites: crm-019 applied, the AA-949 API + web deployed. Environment: as lifecycle-thin.spec.ts
// (E2ECRM, E2ECRMCLIENT, E2ECRMPARTNER, E2ECRMFORMKEY, E2ECRMEMAIL, E2ECRMPASSWORD, E2ECRMTOTP); optional
// E2ECRMAATOUR (the AA-973 seed tour, step 6).
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
const AA_TOUR = env("E2ECRMAATOUR");
const SHOTS = "results";
const RUN = Date.now().toString(36);
const PROSPECT = { name: `E2E ${RUN} Builder`, email: `e2e.builder.${RUN}@example.invalid` };
const CARD_TOKEN = `E2E ${RUN} `;

interface Answer {
  status: number;
  body: any;
}

async function shot(page: Page, name: string) {
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: `${SHOTS}/builder-${name}.png`, fullPage: true });
}

async function hydrated(page: Page, testId: string) {
  await expect(page.getByTestId(testId)).toHaveAttribute("data-hydrated", "true", { timeout: 15_000 });
}

/** A same-origin call of the CRM web's BFF from the signed-in page (the browser's own cookies and Origin). */
async function bff(page: Page, method: "GET" | "POST" | "PATCH" | "PUT" | "DELETE", path: string, body?: unknown): Promise<Answer> {
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

/** The builder's state as the server has it (never the toast). */
async function builder(page: Page, proposalId: string): Promise<any> {
  const res = await bff(page, "GET", `proposals/${proposalId}/builder`);
  expect(res.status, "GET proposals/{id}/builder").toBe(200);
  return res.body;
}

function inputDate(daysAhead: number): string {
  const d = new Date(Date.now() + daysAhead * 86_400_000);
  return d.toISOString().slice(0, 10);
}

async function addAgencyItem(page: Page, blockIndex: number, title: string, sell: string, net: string) {
  const block = page.getByTestId("builder-block").nth(blockIndex);
  await block.getByRole("button", { name: /^\+ add$/i }).click();
  const drawer = block.getByTestId("builder-drawer");
  await drawer.getByRole("button", { name: /^agency item$/i }).click();
  await expect(async () => {
    await drawer.getByTestId("builder-agency-title").fill(title);
    await page.waitForTimeout(250);
    await expect(drawer.getByTestId("builder-agency-title")).toHaveValue(title);
  }).toPass({ timeout: 15_000 });
  await drawer.getByTestId("builder-agency-sell").fill(sell);
  await drawer.getByTestId("builder-agency-net").fill(net);
  const saved = page.waitForResponse((r) => r.url().includes("/options") && r.request().method() === "POST", { timeout: 30_000 });
  await drawer.getByRole("button", { name: /^add item$/i }).click();
  expect((await saved).status()).toBe(201);
  await expect(block.getByTestId("builder-option").filter({ hasText: title })).toHaveCount(1, { timeout: 15_000 });
}

test.describe("CRM-21a proposal builder on Dev", () => {
  test.skip(process.env.E2ECRM !== "1", "opt-in: run through e2e-dev.yml");
  test.setTimeout(480_000);
  test.beforeAll(() => {
    for (const [k, v] of Object.entries({ CLIENT, PARTNER, FORM_KEY, EMAIL })) {
      if (!v) throw new Error(`missing ${k}`);
    }
    if (!CLIENT.startsWith("https://") || !PARTNER.startsWith("https://")) throw new Error("origins must be https");
  });

  test("skeleton from Stage 1/2, Nepal +05:45, options, card R, version conflict, publish v1 and v2", async ({ page, context }) => {
    // 1. Sign in; the inquiry -> the trip.
    await page.goto(`${PARTNER}/pipeline`);
    await signIn(page, { email: EMAIL, password: PASSWORD || undefined, totpSecret: TOTP_SECRET || undefined });
    await landOnPartner(page, PARTNER);
    const client = await context.newPage();
    await client.goto(`${CLIENT}/c/f/${FORM_KEY}`);
    await hydrated(client, "intake-form");
    await client.getByTestId("intake-dest-nepal").click();
    await client.locator('select[name="budget_band"]').selectOption("2000_3500");
    await client.locator('select[name="trip_style"]').selectOption("balanced");
    await client.locator('input[name="nights"]').fill("6");
    await client.locator('input[name="name"]').fill(PROSPECT.name);
    await client.locator('input[name="email"]').fill(PROSPECT.email);
    await client.getByTestId("intake-consent").check();
    expect(await submitIntake(client)).toBe(202);
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
    const tripId = page.url().split("/").pop()?.split("?")[0] ?? "";
    expect(tripId).toMatch(/^[0-9a-f-]{36}$/);

    // 2. Start the builder from the Proposal tab.
    const section = page.getByTestId("proposal-builder");
    await expect(section).toBeVisible({ timeout: 15_000 });
    await section.getByTestId("builder-currency").fill("USD");
    await section.getByRole("button", { name: /^start the builder$/i }).click();
    await page.waitForURL(/\/proposal\/[0-9a-f-]{36}$/, { timeout: 30_000 });
    await hydrated(page, "builder");
    const proposalId = page.url().split("/").pop() ?? "";
    let v = await builder(page, proposalId);
    expect(v.kind).toBe("builder");
    expect(v.status).toBe("draft");
    expect(v.days.map((d: any) => d.day_no)).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);
    expect(new Set(v.days.map((d: any) => d.tz))).toEqual(new Set(["Asia/Kathmandu"]));
    // Card E: a Nepal day shows +05:45 (API and page).
    expect(v.days[1].utc_offset).toBe("+05:45");
    await expect(page.getByTestId("builder-day-offset")).toContainText("UTC+05:45");
    expect(v.days[1].blocks[0].type).toBe("lodging");
    expect(v.days[1].blocks[0].client_may_swap).toBe(true);
    await expect(page.getByTestId("builder-rooms-line")).toContainText(/\dA/);
    await shot(page, "1-skeleton");

    // 3. Two agency items on day 1's Lodging block; the first is the default.
    await addAgencyItem(page, 0, "Thamel boutique", "300", "180");
    await addAgencyItem(page, 0, "Garden of Dreams suite", "420", "260");
    v = await builder(page, proposalId);
    const lodging = v.days[1].blocks[0];
    expect(lodging.options.map((o: any) => o.is_default)).toEqual([true, false]);
    expect(lodging.options[1].sell_delta).toBe("120.00");
    expect(v.totals.sell_total).toBe("300.00");
    expect(v.totals.net_total).toBe("180.00");
    await expect(page.getByTestId("builder-totals")).toContainText("40.0%");
    // Day 2 gets one item too (the rule below needs a second block with a default).
    await page.getByTestId("builder-day").filter({ hasText: /^Day 2/ }).click();
    await addAgencyItem(page, 0, "Bhaktapur heritage inn", "250", "150");
    v = await builder(page, proposalId);
    await shot(page, "2-options");

    // 4. Card R: "Thamel boutique excludes Bhaktapur heritage inn" breaks the defaults -> 422, nothing written.
    const seqBefore = v.edit_seq;
    const day2Default = v.days[2].blocks[0].default_option_id;
    const refused = await bff(page, "POST", `proposals/${proposalId}/rules`, {
      seq: v.edit_seq,
      kind: "excludes",
      option_id: lodging.options[0].option_id,
      target_option_id: day2Default,
    });
    expect(refused.status).toBe(422);
    expect(refused.body.detail).toBe("option_conflict");
    v = await builder(page, proposalId);
    expect(v.edit_seq).toBe(seqBefore);
    expect(v.rules).toEqual([]);

    // 5. §11.2: another editor's stale write -> 409 version_conflict; the page shows the banner and reloads.
    const ok = await bff(page, "PATCH", `proposals/${proposalId}/builder`, { seq: v.edit_seq, price_visibility: "total_only" });
    expect(ok.status).toBe(200);
    const stale = await bff(page, "PATCH", `proposals/${proposalId}/builder`, { seq: v.edit_seq, price_visibility: "total_with_deltas" });
    expect(stale.status).toBe(409);
    expect(stale.body.detail).toBe("version_conflict");
    expect(stale.body.reason).toBe("edited");
    // The open page still holds the old seq: its next write meets the same conflict and offers Reload.
    await page.getByTestId("builder-may-swap").first().click();
    await expect(page.getByTestId("builder-conflict")).toHaveAttribute("data-reason", "edited", { timeout: 15_000 });
    await shot(page, "3-conflict");
    await page.getByTestId("builder-conflict").getByRole("button", { name: /^reload$/i }).click();
    await expect(page.getByTestId("builder-conflict")).toHaveCount(0, { timeout: 15_000 });

    // 7 (before 6: AA quotes need the day 1 date). Settings: the day 1 date and the balance date.
    v = await builder(page, proposalId);
    const settings = await bff(page, "PATCH", `proposals/${proposalId}/builder`, {
      seq: v.edit_seq,
      // Inside the AA-973 seed contract window (today .. today + 90): a later day 1 is refused as aa_quote_refused.
      starts_on: inputDate(30),
      balance_due_on: inputDate(10),
    });
    expect(settings.status).toBe(200);

    // 6. (optional) An AA item from a live quote on day 1.
    if (AA_TOUR) {
      const product = await bff(page, "GET", `aa/products/${encodeURIComponent(AA_TOUR)}?currency=USD`);
      if (product.status === 200) {
        const component = (product.body.components as { component_id: string; kind: string }[]).find((c) => c.kind === "trip_plan") ?? product.body.components[0];
        v = await builder(page, proposalId);
        const added = await bff(page, "POST", `proposals/${proposalId}/blocks/${v.days[1].blocks[0].option_block_id}/aa-options`, {
          seq: v.edit_seq,
          tour_source_id: AA_TOUR,
          component_id: component.component_id,
          component_kind: component.kind,
          // The owner prices it now: an AA item without a sell blocks Publish (aa_item_unpriced, design §4.6).
          sell_amount: "1500.00",
          ...(component.kind === "hotel_room" ? { nights: 2 } : {}),
        });
        expect([201, 409], `AA option answered ${added.status} ${added.body?.detail ?? ""}`).toContain(added.status);
        if (added.status === 201) {
          const aa = added.body.days[1].blocks[0].options.find((o: any) => o.source === "aa");
          expect(aa.net_amount).not.toBeNull();
          expect(aa.aa.unpriced).toBe(false);
          expect(["instant", "on_request"]).toContain(aa.availability);
          // Refresh AA prices from the page: it says how many prices it checked.
          await page.reload();
          await hydrated(page, "builder");
          await page.getByTestId("builder-aa-refresh").click();
          await expect(page.getByText(/AA prices checked/)).toBeVisible({ timeout: 30_000 });
        } else {
          test.info().annotations.push({ type: "note", description: `AA item not added: ${added.body.detail}` });
        }
      } else {
        test.info().annotations.push({ type: "note", description: `AA product look-up ${product.status}: AA step skipped` });
      }
    } else {
      test.info().annotations.push({ type: "note", description: "E2ECRMAATOUR not set: AA drawer step skipped" });
    }

    // 7. Publish v1 from the page.
    await page.reload();
    await hydrated(page, "builder");
    await page.getByTestId("builder-header").getByRole("button", { name: /^publish v1$/i }).click();
    await expect(page.getByTestId("builder")).toHaveAttribute("data-status", "published", { timeout: 15_000 });
    v = await builder(page, proposalId);
    expect(v.status).toBe("published");
    expect(v.price_hash).toMatch(/^[0-9a-f]{64}$/);
    const sealed = await bff(page, "PATCH", `proposals/${proposalId}/builder`, { seq: v.edit_seq, intro: "late change" });
    expect(sealed.status).toBe(409);
    expect(sealed.body.detail).toBe("proposal_not_draft");
    await shot(page, "4-published");

    // 8. New version from v1 -> v2 -> publish: v1 replaced.
    await page.getByTestId("builder-header").getByRole("button", { name: /^new version from this$/i }).click();
    await page.waitForURL((url) => !url.pathname.endsWith(proposalId), { timeout: 30_000 });
    await hydrated(page, "builder");
    const v2Id = page.url().split("/").pop() ?? "";
    const v2 = await builder(page, v2Id);
    expect(v2.version).toBe(2);
    expect(v2.based_on.version).toBe(1);
    expect(v2.totals.sell_total).toBe(v.totals.sell_total);
    await page.getByTestId("builder-header").getByRole("button", { name: /^publish v2$/i }).click();
    await expect(page.getByTestId("builder")).toHaveAttribute("data-status", "published", { timeout: 15_000 });
    expect((await builder(page, proposalId)).status).toBe("superseded");
    await shot(page, "5-v2");

    // 9. Clean-up: the trip goes to the trash.
    const deleted = await bff(page, "DELETE", `trips/${tripId}`);
    expect(deleted.status, `trip delete answered ${deleted.status} ${deleted.body?.detail ?? ""}`).toBe(200);
  });
});
