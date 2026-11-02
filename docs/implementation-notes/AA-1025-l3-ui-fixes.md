# AA-1025 (S264 lane l3-ui-fixes): Content studio shows a stale session, "Open Content studio" for every agency

Follow-up of the S263 tech lead test (CRM invite fixes). No closer open Linear issue was found, so it is filed under
AA-1025.

## Decisions

- **What the tech lead most likely saw (review fix pass).** The studio never shows a person's e-mail. The only
  `info@...` address it renders is Adventure Asia's support address (`SUPPORT_EMAIL`, kit `brand.ts`). On
  `/portal-access/none` it was a bare link right after "sign in with the address it was sent to", so it read as the
  signed-in account's address; that is the page an agency without a studio reached from "Open Content studio" (the
  second half of the same report). Fix: the address is labelled as a contact everywhere it could read as an account
  field ("Questions? Contact Adventure Asia: ..." on both access pages, "Contact Adventure Asia" in the studio's
  contact modal, "Email Adventure Asia at ..." on Billing), and the access pages and Account settings show who is
  signed in, live from the CRM session.
- **"Show the current user's identity" = the CRM agency and role.** The browser asks the CRM web's own BFF
  `GET /bff/me` on the same origin (the agency host routes `/bff/*` to the web; `me` is on the BFF allow-list for
  every role): "Signed in to AA Partner as Agent of <agency>." / "..., with no agency selected." (`portal/lib/crmIdentity.ts`,
  `useCrmIdentity.ts`, `app/portal-access/SignedInAs.tsx`). No new config, cookie, token or CIS/CRM API change. Off
  the agency host (Vercel, local, API-key session) the call fails and nothing is shown. Never the agency id as a
  fallback.
- **The person's e-mail is still not shown.** The CRM gives member e-mails only on the owner-only Team page
  (`TEAM_VIEW` owner), the partner access token has only `sub`, CIS memberships hold no e-mail. Showing it needs an
  auth change (the ID token's e-mail at sign-in, or a `/me` field): left out on purpose.
- **Cookie binding = hardening, not the reported bug.** The cross-person stale-cookie case below was not reproduced;
  it is plausible and cheap, so it ships as its own commit in the same PR, labelled hardening.
- **Hardening: stale studio cookies after another sign-in.** The studio (portal/) keeps its own cookies
  (`cis_tenant_token` and the agency name) for up to 15 minutes. `portal/proxy.ts` trusted any valid partner studio
  token without checking that it was exchanged for the person in the CRM web's LIVE session. Signing in to the CRM as
  someone else on the same browser (a join link, a second account) replaced the CRM session but not the studio
  cookies, so "Open Content studio" showed the previous person's studio and name, and kept their agency choice.
- **Bind the studio cookies to the partner sign-in.** A new host-only cookie `cis_partner_ref` = first 16 hex of
  SHA-256(`iss|sub`) of the partner session the token was exchanged for (`portal/lib/studioSession.ts` `partnerRef`,
  never the identity itself). The gate, the BFF (`requireStudio`) and the chooser (`tenant-select`) write it; the gate
  and the BFF use a token only when the reference matches the live session, otherwise they exchange again WITHOUT the
  old `cis_tenant_id`, and when the new person has no studio or must choose, the stale cookies are cleared on that
  redirect. The BFF answers 401 (shell reloads) instead of serving the old token. Cookies from before this change have
  no reference: one extra exchange per person after deploy.
- **"Show the current user's identity".** The studio has no e-mail of the signed-in person anywhere: the partner pool
  uses e-mail as username (`username_attributes = ["email"]`), so the access token carries only `sub`; the CRM web keeps
  no ID token; CIS memberships hold no e-mail. What the studio shows (agency name, plan) now always comes from an
  exchange made for the live CRM session, which is what was wrong. Showing the person's e-mail would need the ID token
  verified at sign-in (or a token trigger): a separate auth change, not done here.
- **Hide "Open Content studio" for an agency without a studio.** The CRM layout asks CIS `GET /auth/tenant-memberships`
  with the person's partner access token (the same source the studio uses; a studio's CIS tenant id is the CRM agency
  id, as in the membership feed) and shows the link only when the current agency is among the person's active studio
  memberships (`web/lib/studioAccess.ts`). This also hides it from a member of a studio agency who has no studio
  membership (they would land on "No Content studio access").
- **When CIS cannot answer** (no `CIS_API_URL`, throttled, down, refused): the link stays (old behaviour); the studio
  itself explains what is wrong. Answers are memoised per (person, agency) in the web process: 5 minutes for yes / no,
  30 s for unknown, because CIS throttles this route per person (30 a minute) together with the studio sign-in, and the
  layout renders on every page. The memo key is a hash; no token or tenant list is kept.

## Changed

- Review fix pass (the reported symptom): portal `app/portal-access/AccessCard.tsx` (`SupportContact`),
  `SignedInAs.tsx` (new), `none/page.tsx`, `select/page.tsx`, `app/portal/_components/PlaceholderTabs.tsx`
  (contact modal label, Billing invoice link, Settings "Signed in" row), `lib/crmIdentity.ts` + `lib/useCrmIdentity.ts`
  (new); test `lib/crmIdentity.test.ts` (parsing, fetch, and a source guard that the access pages render the address
  only through `SupportContact`).

- portal: `lib/studioSession.ts` (`STUDIO_COOKIES` + `cis_partner_ref`, `PARTNER_REF_COOKIE`, `partnerRef`,
  `boundTo`, `studioCookies(session, ref?)`), `lib/auth-server.ts` (`TENANT_COOKIES`, `requireStudio`), `proxy.ts`
  (step 2), `app/api/auth/tenant-select/route.ts`; tests `lib/studioSession.test.ts`.
- web: `lib/studioCookies.ts` (sign-out also clears `cis_partner_ref`), `lib/studioAccess.ts` (new),
  `app/(crm)/layout.tsx`; tests `tests/studioAccess.test.ts`, `tests/writeGuard.test.ts`, `tests/routes.test.ts`.
- api: guard `tests/unit/test_portal_guards.py::test_studio_cookies_are_bound_to_the_live_partner_sign_in`.

## Tradeoffs

- One more CIS call per CRM page per 5 minutes per person and agency, instead of a CRM-side flag: CIS owns studio
  tenants and memberships (ADR 0010 §7), the CRM has no copy of them.
- A reference cookie instead of putting the partner `sub` into the CIS studio token: no CIS change, and the reference
  is a hash.

## Should know

- Commits are split: (1) contact label + CRM sign-in (the reported symptom), (2) "Open Content studio" only with a
  studio, (3) cookie binding (hardening).
- Live check of the symptom: as an agency WITHOUT a studio, the sidebar has no "Open Content studio"; opening
  `/portal-access/none` directly shows "Signed in to AA Partner as <role> of <agency>." and "Questions? Contact
  Adventure Asia: info@..."; no address on the page reads as the account's.

- Both deploys run on merge (`web/**` and `portal/**` change): portal Fargate and CRM web.
- Live check needs two partner identities on one browser (sign in as A, open the studio, sign in to the CRM as B
  without signing out): the studio must show B's agency or "No Content studio access", never A's.
