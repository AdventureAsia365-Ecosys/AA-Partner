// e2e/helpers/signin.ts — AA-939: sign in to the CRM through Cognito managed login, for either pool the CRM web may
// point at (AA-CIS-Infra crm_web.tf, crm_partner_signin_enabled):
//   - partner pool (required MFA): password, then the authenticator code (E2ECRMPASSWORD + E2ECRMTOTP, from the
//     e2e user's secret crm/<env>/e2e-test-crm/<agency>-<role> written by tools/identity/crm_dev_identity.py);
//   - EXTERNAL pool (before the switch): the e-mail code from the e2e mailbox.
// Selectors are by label / role text (EN), as the managed login pages render them.
import { expect, type Page } from "@playwright/test";
import { baseline, waitForCode } from "./mailbox";
import { freshTotp } from "./totp";

export interface Credentials {
  email: string;
  password?: string;
  totpSecret?: string;
}

async function next(page: Page): Promise<void> {
  await page.getByRole("button", { name: /^(next|sign in|continue|submit|verify)$/i }).first().click();
}

export async function signIn(page: Page, who: Credentials): Promise<void> {
  const emailField = page.getByLabel(/email/i).first();
  await expect(emailField).toBeVisible({ timeout: 30_000 });
  if (who.password && who.totpSecret) {
    await emailField.fill(who.email);
    const password = page.getByLabel(/^password$/i).first();
    if (!(await password.isVisible().catch(() => false))) await next(page);
    await expect(password).toBeVisible({ timeout: 30_000 });
    await password.fill(who.password);
    await next(page);
    const code = page.getByLabel(/code/i).first();
    await expect(code).toBeVisible({ timeout: 30_000 });
    await code.fill(await freshTotp(who.totpSecret));
    await next(page);
    return;
  }
  const before = await baseline();
  await emailField.fill(who.email);
  await next(page);
  const codeField = page.getByLabel(/code/i).first();
  await expect(codeField).toBeVisible({ timeout: 30_000 });
  await codeField.fill(await waitForCode(who.email, before));
  await next(page);
}

/** After managed login: back on the partner host, the agency picked when the chooser shows. */
export async function landOnPartner(page: Page, partner: string): Promise<void> {
  await page.waitForURL((u) => u.origin === partner, { timeout: 30_000 });
  if (new URL(page.url()).pathname === "/select-agency") {
    await page.getByTestId("agency-option").first().getByRole("button").click(); // plain form POST, no JS needed
  }
}
