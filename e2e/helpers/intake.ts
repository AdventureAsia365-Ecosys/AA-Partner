// e2e/helpers/intake.ts — AA-946: submit the public inquiry form and wait out the per-address limit once.
//
// The API allows RATE_LIMIT_INTAKE_PER_MINUTE (default 5) submissions per client address in a fixed one-minute window
// (api/app/settings.py DEFAULT_INTAKE_PER_MINUTE, api/app/ratelimit.py WINDOW_S, api/app/routers/public.py
// post_submission). Every POST counts, the refused no-consent one too, and the specs of one run share the runner's
// address: run 40190930352 got 429 ("Too many tries. Please wait a minute and send again.") on slice1 right after
// lifecycle-thin and slice2-m1m2. The product is right; the suite waits one window plus a margin and sends once more.
// Only the per-address limit (`rate_limited`) is waited out; the per-form hourly quota (`form_rate_limited`) fails.
import { expect, test, type Page } from "@playwright/test";

/** api/app/ratelimit.py WINDOW_S (60 s): the fixed window of the per-address intake limiter. */
export const INTAKE_WINDOW_MS = 60_000;
const MARGIN_MS = 5_000;
const SUBMISSIONS = /\/crm\/public\/v1\/forms\/[^/]+\/submissions$/;

async function sendOnce(page: Page): Promise<{ status: number; detail: string }> {
  const answer = page.waitForResponse(
    (r) => r.request().method() === "POST" && SUBMISSIONS.test(new URL(r.url()).pathname),
    { timeout: 30_000 },
  );
  await page.getByTestId("intake-submit").getByRole("button").click();
  const res = await answer;
  const body = (await res.json().catch(() => ({}))) as { detail?: unknown };
  return { status: res.status(), detail: typeof body.detail === "string" ? body.detail : "" };
}

/**
 * Click the form's submit button and return the API's status for that POST. On a 429 `rate_limited` (the per-address
 * limit) it waits one window plus a margin (the test timeout grows by the same amount) and sends once more. The caller
 * keeps its own assertions on the page (error or thanks); a second 429 fails here.
 */
export async function submitIntake(page: Page): Promise<number> {
  let sent = await sendOnce(page);
  if (sent.status === 429 && sent.detail === "rate_limited") {
    await expect(page.getByTestId("intake-error")).toBeVisible(); // the "Too many tries" message
    const wait = INTAKE_WINDOW_MS + MARGIN_MS;
    test.info().setTimeout(test.info().timeout + wait);
    test.info().annotations.push({ type: "note", description: `intake per-address limit: waited ${wait / 1000} s` });
    await page.waitForTimeout(wait);
    await expect(page.getByTestId("intake-submit").getByRole("button")).toBeEnabled();
    sent = await sendOnce(page);
  }
  expect(sent.status, `intake submit answered 429 ${sent.detail}`).not.toBe(429);
  return sent.status;
}
