# AA-939 — join fix (S263): the one-time password that nobody saw, and AA's clean-up actions

Found live on Dev (01/11/2026) by the tech lead, two test agencies created from the CIS admin "Agencies (CRM)" panel:

- Agency 1: the owner invitation was claimed once (`invitation_claim` allowed, reason `account_created`: a temporary
  password was made), yet the join page said "This invitation was opened before" and never showed the password.
- Agency 2 (same mailbox with a `+` tag): claimed once again, and the page showed "opened before" **with agency 1's
  address**: the claim cookie of the earlier invitation was rendered on the new one.
- Both partner-pool accounts stayed in `FORCE_CHANGE_PASSWORD` with passwords nobody saw, and "Invite first owner"
  answered 409 `owner_invite_pending` (a claimed owner invitation blocks a new one for its 7 days). AA had no way to
  withdraw the invitation, nor to delete the two test agencies (only the agency owner offboards).

Cause of the hidden password: `web/app/join/claim/route.ts` called the API's claim on every POST and always overwrote
the sealed claim cookie. A second POST (double click on "Open invitation", a re-POST on reload or Back, a second tab)
got `already_claimed` and replaced the cookie that held the password. Cause of the wrong address: the claim cookie was
not tied to an invitation, and `/join/invite` rendered whatever claim cookie was present.

## Decisions

- **Web — answer cookies belong to one invitation** (`web/lib/invite.ts`). Both answer cookies carry a fingerprint of
  the token (SHA-256, inside the sealed value; never the token). `openClaim` / `openNotice` return nothing for another
  invitation's answer; `/join/invite` reads `shownAnswer` (the claim answer of this invitation, else its notice).
  `GET /join?t=` drops answer cookies that do not belong to the new token (`staleAnswerCookies`); an answer of the same
  link stays, so opening the mail link again in the same browser still shows the password.
- **Web — `already_claimed` has its own cookie.** It is sealed with purpose `invite-notice` into `aa_crm_invn` (secure
  name `__Secure-aa_crm_invn`), never into the claim cookie. This closes the real race, which a "keep the cookie the
  request carries" rule cannot: a double click sends the second POST before the first answer's cookie arrived, so the
  second request carries no claim cookie at all. With two cookies the browser keeps the password cookie from the first
  answer and the notice from the second, and the page shows the password. `replacesPrior` (previous draft) is gone.
- **Web — no second API call while a password is held** (`claimStep` "keep"), and **"Open invitation" submits once**
  (`components/SubmitOnceForm.tsx`: a plain POST form with the kit `Button`; later submits are dropped, ready again
  after a back-forward cache restore; without JavaScript the server-side rules above keep a second POST harmless).
- **API — a claimed invitation is never claimed again.** It answers `already_claimed` with no password and changes
  nothing; the attempt is audited (`invitation_claim`, `denied`, reason `already_claimed`; before, this path wrote no
  event). Content studio claims alike (log line).
- **API — recovery is a new invitation.** `PartnerDirectory.create_or_find`: an address whose account is still
  `FORCE_CHANGE_PASSWORD` gets a new temporary password through `AdminCreateUser` with `MessageAction=RESEND`, a new
  random `TemporaryPassword` and `DesiredDeliveryMediums=["EMAIL"]` (audit reason `account_password_reissued`). A
  `CONFIRMED` account is `existing` and never reset. RESEND is inside the CRM task role's allow-list (AA-CIS-Infra
  `identity_partner.tf`: AdminCreateUser, ListUsers, AdminDisableUser, AdminUserGlobalSignOut) and outside the
  partner-pool "admin bypass" alert (AdminCreateUser by the CRM task role is expected). No IAM or Terraform change.
- **API — withdraw the first-owner invitation** (crm.ops.v1 1.8, `POST /crm/ops/v1/tenants/{id}/owner-invite/withdraw`,
  `crm-platform-admin`, ticket): every live owner invitation AA sent (`role = owner`, `invited_by LIKE 'platform:%'`,
  pending or claimed, not trashed) becomes `revoked` (the crm-008 guard lets a platform actor do it), one
  `owner_invite_withdraw` event each with `ticket:<REF>`. Active and locked agencies. 404 `owner_invite_not_found`.
  The owner's own team invitations are never touched. The stuck Cognito account is handled by the next invitation's
  claim (RESEND above).
- **API — delete a test agency** (crm.ops.v1 1.8, `POST /crm/ops/v1/tenants/{id}/delete` with `agency_name` typed
  exactly + ticket, `crm-platform-admin`). Guards, in order: the row (locked `FOR UPDATE`) must be synthetic (else 403
  `not_synthetic`, a `denied` event on record), the name must match exactly (422 `agency_name_mismatch`), the status
  active / locked / offboarded (409 `tenant_status`: never during an erase), no active or invited member (409
  `has_members`), no traveller or stored inquiry (409 `has_travellers`), no active legal hold (409 `legal_hold`). Then
  in one transaction: live invitations revoked (`invite_revoke`, reason `agency_delete`), the agency keys
  crypto-shredded (`KeyProvider.destroy`, `key_destroy` event), the `crm.tenant` row trashed (`deleted_at`,
  `deleted_by` = the platform actor, `purge_after` = now + `BACKUP_PURGE_DAYS`), then `agency_delete` (reason
  `synthetic_test_agency`, `ticket:<REF>`). Every context refuses a trashed agency (`TENANT_SQL`), the L1 list skips it,
  and its invitation links answer 404 `invitation_not_found`.
- **LocaleSwitch** (unchanged from the first draft): the option text is `English (US) · dates like Apr 3, 2026` (VI:
  `ngày dạng`), i18n `shell.dateExample`; the portal has no copy of this component.

## Changed (vs. the brief)

- The first draft of this lane (never pushed) also re-issued a password when the **same** claimed link was opened
  again. Dropped: a second tab or a double submit would rotate the password the first claim is showing (the page in
  tab 1 would show a dead password). Recovery is withdraw / revoke + a new invitation, as the brief says.
- (2) is met by the notice cookie rather than by "keep the cookie on already_claimed": the request of a racing second
  submit carries no cookie, so only a separate cookie keeps the password.
- (5) "soft delete via the existing erase/purge machinery if one fits": the offboarding erase does not fit (crm-011
  CRM18 forbids AA to confirm or run an agency request; changing that trigger is a security-relevant migration for a
  Dev clean-up). The fit is the base template trash columns of `crm.tenant` plus the offboarding crypto-shred
  (`KeyProvider.destroy`). No migration.
- The delete route is `POST …/delete` with a body (typed name, ticket), like every other L1 write (the CIS relay and
  `crmWrite` only speak POST / PUT with JSON bodies).

## Tradeoffs

- **Trash + crypto-shred, not a hard delete.** DM-7.1 says agency rows are hard deleted at offboarding. A synthetic
  agency that AA deletes keeps its rows in the trash (no job purges a trashed `crm.tenant` yet), but nothing in them is
  readable personal data after the shred (the invited addresses are under the destroyed DEK; members hold `(iss, sub)`
  only, and there are no travellers by the guard). A hard purge of trashed synthetic agencies is a follow-up.
- **RESEND mails the temporary password** (Cognito's default text, from the pool's sender). The address is the one
  the invitation link went to, so this opens nothing new; the alternative without the mail (`AdminSetUserPassword`,
  `Permanent=false`) needs a new IAM action and trips the partner-pool "admin bypass" alert.
- **One password per invitation.** A person who closed the tab before reading the password needs a new invitation
  (one owner or AA action) instead of re-opening the link. That is the price of never rotating a password a page shows.

## Should know

- Contract: root `docs/contracts/crm.ops.v1.openapi.yaml` 1.8 (two paths, responses `OwnerInviteWithdrawn`,
  `AgencyDeleted`); root PR first, then this one, then the CIS panel PR.
- Tests: web `tests/team.test.ts` (cookie binding, notice cookie, GET /join drops a stale cookie, the double-submit
  race), `tests/joinInvitePage.test.ts` (stale cookie of another invitation, password next to a notice); API unit
  `test_members.py`, `test_studio_invitations.py`, `test_ops_console.py` (withdraw, delete guards and order),
  `test_ops_contract.py` (both routes, success and 4xx); integration `test_crm008_members.py` (same link never
  re-issues, a new invitation does), `test_crm011_console.py` (withdraw then re-invite then accept as owner; delete:
  revoke, shred, trash, unreachable, refusals).
- No migration, no Terraform, no new IAM. Unit coverage 91.7 % (gate 90 %).
