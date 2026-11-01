# AA-939 — join fix (S263): the one-time password that nobody saw

Found live on Dev (01/11/2026). The owner invitation of a test agency was claimed once (`invitation_claim` allowed,
reason `account_created`, a temporary password created), yet the join page said "This invitation was opened before"
and never showed the password. The partner-pool account stayed in `FORCE_CHANGE_PASSWORD` with a password nobody
saw, so the person could not sign in.

Cause: `web/app/join/claim/route.ts` called the API's claim on every POST and always overwrote the sealed claim
cookie. A second POST (double click on "Open invitation", a re-POST on reload or Back, a second tab) got
`already_claimed` and replaced the cookie that held the password.

## Decisions

- **Web, claim route.** The claim cookie now carries a fingerprint of the invitation token (SHA-256, inside the
  sealed value; never the token). `openClaim(cfg, sealed, token)` returns nothing for another invitation's answer,
  so a stale answer from an earlier link never shows on a new one. POST `/join/claim`:
  - while this invitation's claim cookie holds a password: no API call, redirect to `/join/invite` (the person sees
    the same password; asking again would rotate it while they may be typing it);
  - an `already_claimed` answer never replaces an answer the browser already has for that address (`replacesPrior`).
  This is a stronger form of the brief's rule (1): the API is not even asked.
- **Web, form.** `components/SubmitOnceForm.tsx`: a plain POST form with the kit `Button` (primary, `type=submit`).
  The first submit goes through and disables the button ("Opening…", `aria-busy`); later submits are dropped
  (`preventDefault`). A `pageshow` with `persisted` (back-forward cache) makes it ready again. Without JavaScript it
  is an ordinary form and the server-side rule above keeps a second POST harmless. It replaces a hand-styled
  `<button>` (one `aa-kit/no-hand-styled-button` suppression pruned).
- **API, a password never seen is re-issued.** `PartnerDirectory` gets `reissue_temporary_password(sub, email)`;
  `create_or_find` re-issues too. Both use `AdminCreateUser` with `MessageAction=RESEND`, a new random temporary
  password and `DesiredDeliveryMediums=["EMAIL"]` — the action the API task role already has (AA-CIS-Infra
  `identity_partner.tf` allow-list: AdminCreateUser, ListUsers, AdminDisableUser, AdminUserGlobalSignOut). No IAM
  change.
  - A **new** invitation to an address whose account is still `FORCE_CHANGE_PASSWORD`: claim answers `created` with
    the new password (was 409 `account_setup_pending`). Audit reason `account_password_reissued`.
  - The **same** claimed invitation opened again: when the bound account (found by `sub`, same pool issuer, same
    address) is still `FORCE_CHANGE_PASSWORD`, claim answers `created` with a new password instead of
    `already_claimed`. Audit reason `account_password_reissued`. Once the person has their own password the answer
    stays `already_claimed` (no password).
  - Content studio claims (`app/members/studio.py`) share the directory and get the same two rules (log line, no
    agency audit, as before).
- **Text.** `join.alreadyClaimedHelp` (EN + VI) now only appears for an account that is set up, and says: if you never
  saw a one-time password, ask the agency owner or Adventure Asia for a new invitation. New `join.opening`.
- **LocaleSwitch.** The option text is `English (US) · dates like Apr 3, 2026` (VI: `ngày dạng`), i18n
  `shell.dateExample`, passed by the four full-size callers (settings, join, signed-out, staff signed-out); compact
  options (shells) unchanged. The portal has no copy of this component.

## Changed (vs. the brief)

- The brief asked to re-issue only for a **new** invitation. The same claimed link re-issues too, because a new owner
  invitation cannot be sent while the first is claimed: `invite_first_owner` answers 409 `owner_invite_pending` for a
  pending or claimed owner invitation until it expires (7 days), and the L1 console has no revoke. Without this the
  live person (an owner) stays locked out for a week; with it they open the mail link again.
- Rule (1) became "do not ask the API while the cookie holds a password", plus the literal rule for an
  `already_claimed` answer.
- `account_setup_pending` is no longer answered by the API; the web keeps its text for a deploy window where the
  old API answers it.

## Tradeoffs

- **RESEND mails the temporary password** (Cognito's default invitation text, from the pool's sender: Cognito default
  on Dev, the AA SES identity when `identity_ses_enabled`). The address is the one the invitation link went to, and
  the link alone already lets its holder get a password, so this opens nothing new; it is also a second copy for a
  person who lost the page. The alternative without the mail, `AdminSetUserPassword` (`Permanent=false`), needs a new
  IAM action on the CRM task role (Terraform), so it was not taken. Cognito's default sender has a low daily quota;
  the claim route is rate limited per client IP (`RATE_LIMIT_CLAIM_PER_MINUTE`).
- **Re-issue on a claimed link widens a window.** Before, a leaked token was useless once claimed; now it can reset
  the temporary password until the account's first sign-in (at most the invitation's TTL). Same power the token had
  before the claim; the account's first sign-in still needs the new password and an authenticator.
- **Rotation.** Each re-issue invalidates the previous temporary password. The web does not ask again while it holds
  one, the button submits once, and two concurrent claims run one after the other (row lock), so the page normally
  shows the newest one.

## Should know

- Integration test `test_two_claims_and_two_accepts_of_one_invitation_run_one_after_the_other` now expects
  `["created", "created"]` (one account; the second claim re-issued), and a new integration test replays the live case
  (claim, claim again, revoke, re-invite, claim, accept).
- Live re-test (orchestrator): open the existing invitation mail link of the test agency owner again — the page must
  show a one-time password (and Cognito mails one); a double click on "Open invitation" must keep one stable password.
- No migration, no Terraform, no new IAM.
