// e2e/tests/aa-cancel.spec.ts — AA-1087 / AA-1070 crm-13 on Dev (S278 lane l18): Adventure Asia cancels a departure the
// agency has a trip on, and the CRM opens its aa_cancel_case. Two phases around the AA-Booking E2E write run, all
// three driven by ONE seeder output (AA-Booking backend `python -m app.e2e_fixtures --apply --run <run>`, default
// agency = the CRM's E2E agency; its JSON saved locally):
//
//   1. E2ECRMAACANCEL=prepare (BEFORE the cancel): a prospect submits the inquiry form; the owner opens the trip,
//      starts the builder (USD), sets day 1 = the seeded departure's start date and adds the seeded tour as an AA item
//      ON the seeded departure (a live Booking quote). A draft version with an AA option on a departure is what the
//      CRM's feed looks for (api/app/travel_ops/aa_cancel.py AFFECTED_SQL). The trip id goes to the state file.
//   2. AA-Booking frontend/e2e/e2e-writers/writes.spec.ts (BOOKING_E2E_FIXTURES = the same file): the A10e staff
//      address cancels that departure (root ADR 0006a A10e-1).
//   3. E2ECRMAACANCEL=verify (AFTER the cancel): within the feed's minute run, the trip shows one active case for that
//      departure (GET /trips/{id} `aa_cancel`), with the tour and the re-quote offer, and the board's
//      issue=aa_cancelled filter lists the trip.
//
// Agent-only, NEVER e2e-dev.yml: that workflow sets no E2ECRMAACANCEL, so both tests skip there. Environment: as
// proposal-builder.spec.ts (E2ECRM=1, E2ECRMCLIENT, E2ECRMPARTNER, E2ECRMFORMKEY, E2ECRMEMAIL, E2ECRMPASSWORD,
// E2ECRMTOTP), plus
//   E2ECRMAACANCEL            prepare | verify
//   E2ECRMAACANCELFIXTURES    the seeder's APPLY output ({"mode": "APPLY", "fixtures": {...}})
//   E2ECRMAACANCELSTATE       optional, default results/aa-cancel-state.json (prepare writes it, verify reads it)
// The trip stays on the board (named "E2E <run> AA cancel") with its case: it is the evidence of the run.
import { expect, test, type Page } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { flaggedTripIds, readSeeded, type SeededDeparture } from "../helpers/aaCancel";
import { submitIntake } from "../helpers/intake";
import { landOnPartner, signIn } from "../helpers/signin";

const env = (name: string) => (process.env[name] ?? "").trim().replace(/\/+$/, "");
const CLIENT = env("E2ECRMCLIENT");
const PARTNER = env("E2ECRMPARTNER");
const FORM_KEY = env("E2ECRMFORMKEY");
const EMAIL = env("E2ECRMEMAIL");
const PASSWORD = (process.env.E2ECRMPASSWORD ?? "").trim();
const TOTP_SECRET = (process.env.E2ECRMTOTP ?? "").trim();
const PHASE = env("E2ECRMAACANCEL");
const FIXTURES_FILE = env("E2ECRMAACANCELFIXTURES");
const STATE_FILE = env("E2ECRMAACANCELSTATE") || "results/aa-cancel-state.json";
const SHOTS = "results";
const RUN = Date.now().toString(36);
const PROSPECT = { name: `E2E ${RUN} AA cancel`, email: `e2e.aacancel.${RUN}@example.invalid` };
const CARD_TOKEN = `E2E ${RUN} `;
// The CRM pulls the Booking feed every minute (crm_booking_sync); a margin for the job runner.
const FEED_TIMEOUT_MS = 300_000;

interface Answer {
  status: number;
  body: any;
}

/** A same-origin call of the CRM web's BFF from the signed-in page (the browser's own cookies and Origin). */
async function bff(page: Page, method: "GET" | "POST" | "PATCH", url: string, body?: unknown): Promise<Answer> {
  return page.evaluate(
    async ({ method, url, body }) => {
      const res = await fetch(`/bff/${url}`, {
        method,
        headers: body === undefined ? { Accept: "application/json" } : { Accept: "application/json", "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
        credentials: "same-origin",
      });
      return { status: res.status, body: await res.json().catch(() => null) };
    },
    { method, url, body },
  );
}

async function shot(page: Page, name: string) {
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: `${SHOTS}/aa-cancel-${name}.png`, fullPage: true });
}

async function hydrated(page: Page, testId: string) {
  await expect(page.getByTestId(testId)).toHaveAttribute("data-hydrated", "true", { timeout: 15_000 });
}

async function signedIn(page: Page) {
  await page.goto(`${PARTNER}/pipeline`);
  await signIn(page, { email: EMAIL, password: PASSWORD || undefined, totpSecret: TOTP_SECRET || undefined });
  await landOnPartner(page, PARTNER);
}

test.describe("crm-13: AA cancels a departure the agency has a trip on (Dev, agent-only)", () => {
  test.skip(process.env.E2ECRM !== "1" || !["prepare", "verify"].includes(PHASE), "opt-in: E2ECRM=1 E2ECRMAACANCEL=prepare|verify");
  test.setTimeout(480_000);
  let seeded: SeededDeparture;
  test.beforeAll(() => {
    for (const [k, v] of Object.entries({ CLIENT, PARTNER, FORM_KEY, EMAIL, FIXTURES_FILE })) {
      if (!v) throw new Error(`missing ${k}`);
    }
    if (!CLIENT.startsWith("https://") || !PARTNER.startsWith("https://")) throw new Error("origins must be https");
    seeded = readSeeded(fs.readFileSync(FIXTURES_FILE, "utf-8"));
    if (!seeded.crmAgency) throw new Error("the seeder ran for another agency: run it without --any-synthetic / --agency-tenant");
  });

  test("prepare: a CRM trip with the seeded tour as an AA item on the seeded departure", async ({ page, context }) => {
    test.skip(PHASE !== "prepare", "phase prepare only");
    await signedIn(page);
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

    const created = await bff(page, "POST", `trips/${tripId}/builder`, { currency: "USD" });
    expect(created.status, `builder ${JSON.stringify(created.body)}`).toBe(201);
    const proposalId = String(created.body.proposal_id ?? created.body.id ?? "");
    expect(proposalId).toMatch(/^[0-9a-f-]{36}$/);
    let v = (await bff(page, "GET", `proposals/${proposalId}/builder`)).body;
    // Day 1 on the departure's start: the AA item is quoted for that date on that departure.
    const settings = await bff(page, "PATCH", `proposals/${proposalId}/builder`, { seq: v.edit_seq, starts_on: seeded.startDate });
    expect(settings.status, `settings ${JSON.stringify(settings.body)}`).toBe(200);
    v = (await bff(page, "GET", `proposals/${proposalId}/builder`)).body;
    const day1 = (v.days as { day_no: number; blocks: { option_block_id: string }[] }[]).find((d) => d.day_no === 1);
    expect(day1?.blocks.length, "day 1 has a block").toBeGreaterThan(0);
    const added = await bff(page, "POST", `proposals/${proposalId}/blocks/${day1!.blocks[0].option_block_id}/aa-options`, {
      seq: v.edit_seq,
      tour_source_id: seeded.tourSourceId,
      component_id: seeded.componentId,
      component_kind: "trip_plan",
      departure_id: seeded.departureId,
      sell_amount: "1500.00",
    });
    expect(added.status, `AA item ${JSON.stringify(added.body)}`).toBe(201);
    v = (await bff(page, "GET", `proposals/${proposalId}/builder`)).body;
    const aa = (v.days as { blocks: { options: { source: string; aa?: { departure_id?: string } }[] }[] }[])
      .flatMap((d) => d.blocks.flatMap((b) => b.options))
      .find((o) => o.source === "aa");
    expect(aa?.aa?.departure_id, "the AA item is on the seeded departure").toBe(seeded.departureId);
    await page.goto(`${PARTNER}/pipeline/${tripId}/proposal/${proposalId}`);
    await hydrated(page, "builder");
    await shot(page, "1-prepared");
    fs.mkdirSync(path.dirname(STATE_FILE), { recursive: true });
    fs.writeFileSync(STATE_FILE, JSON.stringify({ trip_id: tripId, proposal_id: proposalId, departure_id: seeded.departureId }));
    console.log(`[aa-cancel] prepared trip ${tripId} on departure ${seeded.departureId}: now run the Booking e2e-writers cancel`);
  });

  test("verify: the CRM opened the aa_cancel_case after AA cancelled the departure", async ({ page }) => {
    test.skip(PHASE !== "verify", "phase verify only");
    const state = JSON.parse(fs.readFileSync(STATE_FILE, "utf-8")) as { trip_id: string; departure_id: string };
    expect(state.departure_id, "the state file is from this seeder run").toBe(seeded.departureId);
    await signedIn(page);
    await expect
      .poll(
        async () => {
          const trip = await bff(page, "GET", `trips/${state.trip_id}`);
          expect(trip.status).toBe(200);
          const cases = (trip.body?.aa_cancel?.cases ?? []) as { departure_id: string; active: boolean }[];
          return cases.filter((c) => c.departure_id === state.departure_id && c.active).length;
        },
        { message: "one active aa_cancel_case for the departure", timeout: FEED_TIMEOUT_MS, intervals: [10_000, 20_000, 30_000] },
      )
      .toBe(1);
    const trip = await bff(page, "GET", `trips/${state.trip_id}`);
    const found = (trip.body.aa_cancel.cases as { departure_id: string; tour: { tour_source_id: string | null } | null; can_requote: boolean }[]).find(
      (c) => c.departure_id === state.departure_id,
    );
    expect(found?.tour?.tour_source_id, "the case names the tour that was on the departure").toBe(seeded.tourSourceId);
    expect(found?.can_requote, "a trip before booking may be re-quoted").toBe(true);
    const board = await bff(page, "GET", "pipeline?issue=aa_cancelled");
    expect(board.status).toBe(200);
    expect(flaggedTripIds(board.body), "the board's AA cancelled filter lists the trip").toContain(state.trip_id);
    await page.goto(`${PARTNER}/pipeline?issue=aa_cancelled`);
    await shot(page, "2-board");
  });
});
