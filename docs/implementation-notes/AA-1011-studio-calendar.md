# AA-1011 follow-up — the studio's Calendar card answers through the studio, not the CRM BFF

Lane l24-crm-integrations-409 (S294). Deploy Web Dev of main after Partner #395 (the calendar push) and #397 rolled
back the web service: the post-deploy studio smoke saw `Failed to load resource: the server responded with a status of
409 ()` on `/portal/account/integrations` (`aa761-account-pages.spec.ts` "Integrations: channel health, no API key
card" and `portal-smoke.spec.ts` "portal: Integrations").

## Root cause

The Calendar card (`web/app/(studio)/portal/_components/CalendarSyncCard.tsx`) was the only studio component that
called the CRM's browser BFF (`/bff/calendar/connections`, `bffSend`). Every tenant BFF route needs the agency the CRM
session picked (`session.tid`, set by `/auth/agency` on the CRM side). A person who signs in to the Content studio
never picks a CRM agency (the studio's tenant is its own verified CIS token, cookie `cis_tenant_id`), so the BFF
answered 409 `no_agency_selected` before the API was even called (`web/app/bff/[...path]/route.ts`). The card's
`useQuery` swallowed the failure (the card showed "Not available yet"), but the browser logged the 409.

The smoke's API failure watcher (`e2e/smoke-portal/smoke-helpers.ts` `watchPage`) only recorded same-origin `/api/`
responses; since the studio joined the agency app (R3) a studio page can also call `/bff/`, so the 409 showed up as a
bare console error without its URL or body.

## Decisions

- **A studio route handler, like the Trip planner (AA-1139)**: `web/app/api/studio/calendar/[[...path]]/route.ts`
  calls the CRM API with the person's own partner access token and the studio's verified tenant
  (`tid: auth.tenantId`), never a tenant from the request. The CRM API is unchanged: `tenant_tx` still checks the
  person's `crm.member` row in that agency and answers 403 `not_a_member` / `tenant_locked` like every CRM route.
- **The read is display only, so it answers 200** (root lesson S264 / AA-1025: a display route never answers a
  "normal" 4xx): a 400 / 403 / 404 / 409 from the CRM (no CRM role in this agency, no CRM agency at all for a
  studio-only agency, a locked agency) becomes `{connections: [], providers: {google: false, microsoft: false},
  kinds: [], available: false}`; the card then says the calendar comes with a CRM role and shows no scope picker or
  connect button. A 401 stays 401 (the card reloads through `/auth/refresh`, like `bffSend`); any other failure is a
  503 `calendar_unavailable` (a real outage must stay visible to the smoke).
- **The writes pass the CRM's answer through** (OAuth start / complete, disconnect), so the card keeps its error
  wording (`err_<code>`).
- **The three calendar connection routes left the browser BFF** (`BFF_ROUTES` -> `SERVER_ONLY_ROUTES`): no CRM page
  calls them, and leaving them reachable from `/bff` would let the next studio component repeat the mistake.
- **Guard**: `web/tests/studioCalendar.test.ts` fails when a file under `web/app/(studio)` imports the CRM's browser
  BFF client (`@/lib/bffClient`) or names a `/bff/` path.
- **The watcher records `/bff/` failures too** (same origin): a 4xx on page load from either BFF is a bug of the page
  (it should not call, or the route should answer 200 "not available"), so the smoke names the URL and body.

## Changed

- The card reads `GET /api/studio/calendar` and writes `POST /api/studio/calendar/oauth/start|complete` and
  `POST /api/studio/calendar/<id>/disconnect`; `CalendarConnectionsView.available` (optional; the handler sets it).

## Tradeoffs

- The studio handler adds one hop (web -> CRM API) that the BFF also had; no extra CIS call (the studio token is
  verified by `requireStudio`, cached 20 s).
- A person with a CRM role who opens the studio of another agency sees "comes with a CRM role" for that agency: the
  calendar belongs to the membership of the agency the studio shows.

## Should know

- No migration, no API change, no new setting or threshold.
- Tests: `web/tests/studioCalendar.test.ts` (route mapping, the 200 "not available" answer for each refusal, the
  handler end to end with the studio and the CRM API mocked, the no-BFF guard), `web/tests/calendarSync.test.ts`
  (the routes are server-only now).
