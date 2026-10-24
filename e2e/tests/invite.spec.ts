// e2e/tests/invite.spec.ts — AA-939, card CRM-11 Done when (Dev): "invite -> active < 5 min (timed)", on the real
// partner pool with required MFA. The owner invites the e2e invitee address with the assistant role; the AA mail
// arrives in the e2e mailbox; the invitee opens the link, gets the one-time password, signs in through managed login
// (own password, authenticator enrolment), accepts, and lands in the agency as an assistant. Then the owner removes
// the member again.
//
// Opt-in (E2ECRMINVITE=1) and one-off: the invitee's partner-pool account must not exist before a run
// (AA-CIS-Infra tools/identity/crm_dev_identity.py invitee-reset --apply deletes it). Environment (no underscores):
//   E2ECRMPARTNER, E2ECRMEMAIL, E2ECRMPASSWORD, E2ECRMTOTP   the owner (as slice1.spec.ts)
//   E2ECRMINVITEE   the invitee address (a recipient of the e2e mailbox rule, crm_e2e_users.json "invitee": true)
//
// UNVERIFIED until the first Dev run: the managed login v2 labels of the new-password and authenticator-setup pages
// (the selectors below try the usual texts; the secret is read from the page as a base32 string).
import { expect, test, type Page } from "@playwright/test";
import { randomBytes } from "node:crypto";
import fs from "node:fs";
import { baseline, waitForInviteLink } from "../helpers/mailbox";
import { landOnPartner, signIn } from "../helpers/signin";
import { freshTotp } from "../helpers/totp";

const env = (name: string) => (process.env[name] ?? "").trim().replace(/\/+$/, "");
const PARTNER = env("E2ECRMPARTNER");
const OWNER = env("E2ECRMEMAIL");
const OWNER_PASSWORD = (process.env.E2ECRMPASSWORD ?? "").trim();
const OWNER_TOTP = (process.env.E2ECRMTOTP ?? "").trim();
const INVITEE = env("E2ECRMINVITEE").toLowerCase();
const SHOTS = "results";
const LIMIT_MS = 5 * 60 * 1000;

async function shot(page: Page, name: string) {
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true });
}

function newPassword(): string {
  // >= 14 characters with all four classes (the partner pool's policy).
  return `Aa1!${randomBytes(18).toString("base64url")}`;
}

async function click(page: Page, name: RegExp): Promise<void> {
  await page.getByRole("button", { name }).first().click();
}

test.describe("CRM-11 invite -> active on Dev", () => {
  test.skip(process.env.E2ECRMINVITE !== "1", "opt-in, one-off: run through e2e-dev.yml with E2ECRMINVITE=1");
  test.beforeAll(() => {
    for (const [k, v] of Object.entries({ PARTNER, OWNER, OWNER_PASSWORD, OWNER_TOTP, INVITEE })) {
      if (!v) throw new Error(`missing ${k}`);
    }
  });

  test("an invited assistant is active in under five minutes", async ({ browser }) => {
    const owner = await (await browser.newContext()).newPage();
    await owner.goto(`${PARTNER}/settings/team`);
    await signIn(owner, { email: OWNER, password: OWNER_PASSWORD, totpSecret: OWNER_TOTP });
    await landOnPartner(owner, PARTNER);
    await owner.goto(`${PARTNER}/settings/team`);
    await expect(owner.getByTestId("team-admin")).toHaveAttribute("data-hydrated", "true", { timeout: 15_000 });

    const before = await baseline();
    const started = Date.now();
    await owner.getByTestId("team-invite-email").fill(INVITEE);
    await owner.getByTestId("team-invite-role").selectOption("assistant");
    await click(owner, /send invitation|gửi lời mời/i);
    await expect(owner.getByTestId("team-message")).toContainText(/invitation sent|đã gửi lời mời/i);
    await shot(owner, "invite-01-sent");

    const link = await waitForInviteLink(INVITEE, before);
    expect(new URL(link).origin).toBe(PARTNER);

    const person = await (await browser.newContext()).newPage();
    await person.goto(link);
    await person.waitForURL(`${PARTNER}/join/invite`);
    expect(person.url()).not.toContain("t="); // the token left the address bar
    await person.getByTestId("join-open").click();
    await expect(person.getByTestId("join-claimed")).toBeVisible();
    await expect(person.getByTestId("join-email")).toHaveText(INVITEE);
    const temporary = (await person.getByTestId("join-temporary-password").textContent())?.trim() ?? "";
    expect(temporary.length).toBeGreaterThanOrEqual(14);
    await person.getByTestId("join-sign-in").click();

    // Managed login: e-mail + one-time password, then the person's own password, then the authenticator.
    const email = person.getByLabel(/email/i).first();
    await expect(email).toBeVisible({ timeout: 30_000 });
    await email.fill(INVITEE);
    const password = person.getByLabel(/^password$/i).first();
    if (!(await password.isVisible().catch(() => false))) await click(person, /^(next|sign in|continue)$/i);
    await password.fill(temporary);
    await click(person, /^(next|sign in|continue)$/i);
    const mine = newPassword();
    const fresh = person.getByLabel(/new password/i).first();
    await expect(fresh).toBeVisible({ timeout: 30_000 });
    await fresh.fill(mine);
    const confirm = person.getByLabel(/confirm|re-?enter/i).first();
    if (await confirm.isVisible().catch(() => false)) await confirm.fill(mine);
    await click(person, /^(next|change password|continue|submit|save)$/i);
    const showKey = person.getByRole("button", { name: /secret|key|can.?t scan/i }).first();
    if (await showKey.isVisible({ timeout: 15_000 }).catch(() => false)) await showKey.click();
    const secret = /\b([A-Z2-7]{32,64})\b/.exec((await person.locator("body").innerText()).replace(/\s(?=[A-Z2-7]{4}\b)/g, ""))?.[1];
    if (!secret) throw new Error("authenticator secret not found on the setup page");
    await person.getByLabel(/code/i).first().fill(await freshTotp(secret));
    await click(person, /^(next|verify|continue|submit|confirm)$/i);
    const skip = person.getByRole("button", { name: /skip|not now|later/i }).first(); // passkey offer, if any
    if (await skip.isVisible({ timeout: 5_000 }).catch(() => false)) await skip.click();

    await person.waitForURL(`${PARTNER}/join/accept`, { timeout: 60_000 });
    await person.getByTestId("join-accept").click();
    await person.waitForURL((u) => u.origin === PARTNER && !u.pathname.startsWith("/join"), { timeout: 30_000 });
    const elapsed = Date.now() - started;
    await person.goto(`${PARTNER}/settings`);
    await expect(person.getByTestId("crm-settings")).toContainText(/assistant|trợ lý/i);
    await shot(person, "invite-02-active");
    expect(elapsed, `invite -> active took ${Math.round(elapsed / 1000)} s`).toBeLessThan(LIMIT_MS);
    console.log(JSON.stringify({ event: "crm_e2e_invite_to_active", seconds: Math.round(elapsed / 1000) }));

    // Clean-up: the owner removes the member again (the account stays; invitee-reset deletes it before a rerun).
    await owner.goto(`${PARTNER}/settings/team`);
    owner.once("dialog", (d) => void d.accept());
    const row = owner.getByTestId("team-member-row").filter({ hasText: INVITEE });
    await row.getByRole("button", { name: /remove|xoá khỏi nhóm/i }).click();
    await expect(owner.getByTestId("team-message")).toContainText(/removed|đã xoá/i);
  });
});
