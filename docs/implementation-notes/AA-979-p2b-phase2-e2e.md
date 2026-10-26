# AA-979 P2b phase 2: the re-enrolment and agency-host studio run (Playwright, on demand)

Session S252, agent Claude Code. Branch `test/aa-979-phase2-reenrol-e2e`. Runbook (AA-Ecosys `.tmp-session/s252/aa979/`)
§2.4-§2.7, §3 and §6a; the orchestrator's one-line commands are in `PHASE2-AUTOMATION.md` next to it.

## Decisions

- **Its own config, `e2e/playwright.phase2.config.ts` (testDir `e2e/phase2/`), and an opt-in flag `E2EP2=1`.** No
  workflow runs it: the e2e mailbox is read with the orchestrator's MFA admin profile, the run creates a partner-pool
  account and a CIS membership, and it is needed a few times only (pre-cutover, after the cutover, the rollback drill).
- **The agency host is pinned in the browser with Chromium `--host-resolver-rules=MAP <host> <CloudFront domain>`**
  (set from `E2EP2PORTAL` + `E2EP2CF` in the config). URL, SNI, certificate check, Host header and cookies stay on the
  agency host, as with `curl --connect-to`; the Cognito callback to the agency host is pinned too. Node requests
  (`APIRequestContext`) are not pinned, so the spec sends them to the CIS API only. A `/healthz` navigation proves the
  pin (a CloudFront `via` / `x-cache` header) and records the image SHA.
- **Staff side (a), three modes.** The CIS route needs a staff Cognito session with `tenant.write` (the relay forwards
  that staff member's own token to `crm.ops.v1`). The A10b Dev-only E2E CIS admin is read-only (root ADR 0006a A10b:
  403 `e2e_read_only` outside two sentence-apply routes), so it cannot send the invitation today. Modes: `none`
  (default: the orchestrator made the call; the spec only reads mail stored after `E2EP2SINCE`), `browser` (headed: a
  person signs in to the CIS admin, the spec makes the call through the admin BFF in that session), `a10b` (works only
  if the A10b allow-list gains the two invitation routes; until then it fails with a message that says so).
- **TOTP: the existing `helpers/totp.ts` (AA-939), generalised** to `totpFromKey(key, at, {step, digits, algorithm})`
  so it can be checked against every RFC 6238 Appendix B vector (SHA-1 / SHA-256 / SHA-512, 8 digits); `totp()` keeps
  its signature and output. Node `crypto` only.
- **`parseSetupSecret` now needs at least 26 base32 characters** (128 bits, RFC 4226 R6). The unit test found that a
  24-letter label ("Scan the QR code with your app") parsed as a secret. Managed login shows 52 characters, so the
  CRM invitation E2E is unaffected.
- **The person's password and authenticator secret** live in memory and in one file,
  `<E2EP2OUT>/.private/<hash>.json` (0600, folder 0700), written as soon as each exists, so a failed run can still sign
  the person in again (`E2EP2REUSE=1`). `E2EP2OUT` inside the repo is refused unless it is under `e2e/results/`
  (gitignored). No trace, video or automatic screenshot; the managed login pages and the claimed-invitation page (it
  shows the one-time password) are never captured.
- **6a probes change nothing even if the check were missing:** viewer -> `POST /v1/tours/pool/<id that does not
  exist>/rewrite` (the route answers 404 / 422 before any charge); editor -> `POST /v1/account/api-key` (no POST route
  exists, so 405). With the check, the middleware answers 403 `studio_role_forbidden` before routing.
- **Mailbox helper:** `baseline()` / `waitForCode()` take an optional prefix (the A10b codes land under
  `inbound-admin/`), and `baselineBefore(sinceMs)` keeps only mail stored after a given time (the invitation sent
  outside the run). The existing calls are unchanged.

## Changed

- `e2e/helpers/totp.ts`, `e2e/helpers/mailbox.ts` (above); new `e2e/phase2/`, `e2e/unit/`,
  `e2e/playwright.phase2.config.ts`, `e2e/playwright.unit.config.ts`; `package.json` scripts `test:phase2`, `test:unit`.

## Tradeoffs

- The unit tests run with Playwright's runner (no browser is launched) instead of adding a unit-test dependency; they
  are not in CI yet (the e2e folder is only type-checked by `e2e-dev.yml`). Adding `npm run test:unit` there is a small
  follow-up.
- `claimEnrolAccept` repeats the managed login steps of `tests/invite.spec.ts` instead of refactoring that spec: the
  CRM invitation E2E keeps working untouched, and both use the same selectors.

## Should know

- A fresh address must be a recipient of the e2e mailbox receipt rule (AA-CIS-Infra `e2e_mailbox.tf`, fixed list).
  Today the only one meant for a new partner-pool account is the CRM invitee (`crm_e2e_users.json` `"invitee": true`);
  its account must be reset first (`crm_dev_identity.py invitee-reset --apply`, a Cognito admin call by the
  orchestrator). Dedicated studio invitees (one viewer, one editor) would be an AA-CIS-Infra change.
- Before the cutover the join link points at `partner-dev.` (`PARTNER_WEB_ORIGIN` stays there for the window). The
  studio sign-in on the agency host is a second, full partner sign-in (password + a fresh TOTP step).
