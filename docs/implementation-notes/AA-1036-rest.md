# AA-1036 R3 remainder — add-on requests, CRM AI pause banner, invited-user test (Partner side)

Root ADR 0019 d7, clarifications TL-7. CIS side (table 374, routes, Tenant 360 section, staff bell): AA-CIS-App
`docs/implementation-notes/AA-1036-addon-requests.md`. Slice 1 decisions: `AA-1036.md`.

## Decisions

- **"Request add-on" is a page, not a sidebar action.** Every not-open add-on in the switcher links to Account ›
  Add-ons (`/portal/account/add-ons`): each add-on with its state (On / Requested / Not added), the pending request
  with the agency's note, the last decision (a decline with its reason), and for the owner a note field and a
  "Request …" button. The sidebar entries are labelled by state ("CRM: requested", "Trip planner: on"). The mailto: is
  gone (`requestHref` removed).
- **The state comes from CIS** (`GET /v1/account/add-ons` through the studio's verified token): the switcher reads it
  in `GET /api/studio/apps` (`cisAddOns`, never throws, renewed studio cookies kept), the page through the existing
  tenant BFF proxy. The CRM entry still opens on the CRM API's `crm_entitled`; when the CRM refuses the person, the CIS
  state decides between requested / on / request. "On" with no CRM role means the agency has the add-on and the owner
  still has to give that person a CRM role (TL-7 step 4, ADR 0006b d4).
- **Owner only, shown honestly.** CIS refuses an editor's or viewer's request (403, `/v1/account` writes). The page
  needs the role to show the form only to the owner: `/api/tenant/me` now answers `content_studio_role` from the
  verified studio token's `csr` claim (display only; `studioClaims` reads it, `useStudioRole()` uses the shell's
  `["tenant-me"]` query, so no extra call).
- **CRM side:** the Trip planner entry goes to the studio's Add-ons page and shows only with studio access (the
  request is the studio owner's); without studio access the CRM's "Apps" group is hidden (it would be empty).
- **AI pause signal:** it already existed in the CRM API (`crm.llm_app_profile.ai_enabled`, written by the CIS admin's
  App profiles through `crm.ops.v1`) but only on `/crm/ops/v1`. `GET /crm/v1/me` now answers `ai_paused`
  (`app.llm.app_profile.for_call` on the request's own connection: no row / missing table / unreadable = AI on, the
  gateway's own rule), never the staff reason. The CRM layout shows the amber `AiPausedBanner` (kit tokens, like the
  mailbox and support banners) on every page while it is true. No new table, no CIS change.
- **Done-when test** ("an invited user lands on Content and sees CRM only with the entitlement"):
  `api/tests/integration/test_aa1036_invited_user_content_home.py` walks the existing studio invitation (issue, claim,
  accept, CIS activation with a fake CIS) and then calls the live app over HTTP: 403 on `/crm/v1/me` (no CRM role:
  the switcher offers the CRM as an add-on), 200 with `crm_entitled` false (a CRM role, but CIS cancelled the add-on),
  then true once CIS projects `crm.core`. The web half: `web/tests/studio/merge.test.ts` (the BFF switcher for an
  invited editor with no CRM role and a pending CRM request) plus the existing "/" -> `/portal/dashboard` tests.

## Changed (vs. the lane brief)

- No in-app notification to the tenant on a decision (the Add-ons page shows it); staff get the CIS bell row.
- The Trip planner "On" state has no app link: there is no agency-facing planner surface in this app yet.

## Tradeoffs

- `GET /api/studio/apps` now also verifies the studio token at CIS (cached 20 s per token) and reads the add-ons on
  every sidebar load (stale time 5 min on the client). Cheap, and it keeps the switcher's labels true.
- The page reads CIS errors by status (403 / 409 / 429), because the studio's `ApiError` does not carry CIS's
  structured `detail` object.

## Should know

- Ship after the CIS code PR is deployed (the page and the switcher read the new CIS routes; before that CIS answers
  404/503 and the switcher falls back to "request", the page shows its load error). The portal smoke now opens
  `/portal/account/add-ons` and expects both add-on states, so the Partner deploy must follow the CIS deploy.
- New UI copy is English with Vietnamese in the same catalogues (`account.messages.ts` ADDONS_MESSAGES,
  `studio.messages.ts`, `lib/i18n.ts` `ai.pausedBanner`); `studioI18n.test.ts` passes.
