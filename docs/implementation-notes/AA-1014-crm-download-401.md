# AA-1014 — lane crm-download-401: file downloads renew an expired access token

Found while recording the S262 CRM review video (chunk "reports"): the CSV export failed with "The file could not be
downloaded. Try again." and every retry failed the same way. CloudTrail (Cognito `Token_POST`) and the API's
CloudWatch logs showed no API call for those clicks: the 30-minute access token had expired between page load and
the click, so the BFF (`web/app/bff/[...path]/route.ts` step 4) answered `401 session_expired` without calling the
API. Every other browser call goes through `bffSend` (`web/lib/bffClient.ts`), which handles a 401 by reloading through
`/auth/refresh?next=`; the four blob downloads used a raw `fetch('/bff/...')` and treated any non-2xx as a plain error.

## Decisions

- **New helpers in `web/lib/bffClient.ts`:**
  - `renewSession()` — `fetch('/auth/refresh?next=<current path>', { credentials: 'same-origin', redirect: 'manual' })`.
    `/auth/refresh` answers a 307 with the renewed session cookie; with `redirect: 'manual'` the page stays where it
    is, the browser still stores the `Set-Cookie`, and the opaque answer is ignored. A network failure is swallowed:
    the retry that follows decides.
  - `bffFetch(path, init)` — `fetch('/bff/<path>')` with same-origin credentials; on a 401 it renews once and retries
    once; a second 401 calls `window.location.assign('/auth/refresh?next=…')` (the `bffSend` behaviour: sign in again)
    and returns that 401.
  - `bffDownload(path, { filename })` — `bffFetch` + the blob-link save that was copied four times; returns a
    `BffResult<null>` (`network` on a fetch failure, the API's `detail` on a refusal).
- **Call sites switched:** `components/reports/ExportMenu.tsx` (the bug), `components/settings/AccessLogView.tsx`
  (CSV/JSON export via `bffDownload`; "Load more" via `bffFetch` so the events already loaded are kept),
  `DataLifecycleView.tsx` (offboarding export), `DataPortabilityView.tsx` (portability export after its link POST).
  No other `fetch('/bff/` exists in `components/` or `lib/` (the other raw fetches go to public proposal/portal/intake
  APIs or the staff BFF, which have their own auth).
- **One renewal at a time:** concurrent 401s share one in-flight `renewSession()` promise, so two downloads clicked
  together do not spend the refresh token twice (refresh-token rotation, `identity.tf`).
- **Guard test:** `tests/bffDownload.test.ts` fails when a file under `web/components` or `web/lib` (other than
  `bffClient.ts`) calls `fetch('/bff/…')` directly.

- **Review fix pass (S262 w14):**
  - Callers clear their busy state in a `finally` again; `bffDownload` never throws (a blob that cannot be saved is
    `save_failed`, a 401 after the renewal is `redirecting`).
  - `BffResult` failures may carry `redirecting: true` (the page is already navigating to `/auth/refresh`); the four
    download callers, "Load more" and the portability `send` skip their error message in that case, so no error
    flashes before the navigation.
  - `bffSend(method, path, body, { renew: true })` goes through `bffFetch` (silent renewal, one retry). Only the
    portability link POST (`data/exports/{id}/link`) uses it: its body is the JSON string `"{}"`, safe to send twice,
    and a 401 means nothing was minted. Every other `bffSend` keeps the full-page reload (see Tradeoffs).
  - `bffFetch` documents that the request is sent twice: one-shot bodies (a `ReadableStream`) cannot be retried;
    callers pass strings/JSON only.
  - The guard also scans the `"use client"` files under `web/app`, flags `fetch(` with any string or template literal
    naming `/bff/` (also `${origin}/bff/…`), and any `/bff/` literal outside a short allow-list (`bffClient.ts`, the
    server allow-list `lib/bff.ts`, `lib/hosts.ts` safeNext, and the two XMLHttpRequest uploads in `lib/onboarding.ts`
    and `lib/mailImport.ts`, which keep the reload on 401). Comments are ignored; `/staff/bff/` is not flagged.
  - Test fixtures build their paths with `lib/reports.ts` `exportPath` (`reports/export?period=…&section=…`).

## Changed

- Same error texts as before (`reports.export.error`, `log.downloadError`, `data.error`); no new UI strings.
- `bffSend` is unchanged in behaviour (it now shares `refreshUrl()` / `failure()` with the new helpers).

## Tradeoffs

- **`bffSend` keeps its full-page reload on 401.** Giving it the silent renewal too would save a reload for writes as
  well (a 401 from the BFF or the API means nothing was applied, so a retry is safe), but it touches every call in
  the app; left as a follow-up, this lane fixes the downloads that had no renewal at all.
- **No proactive renewal timer** (renew before `atExp`): the lazy renew-on-401 needs no extra request when the
  person is idle and works the same for every route.

## Should know

- **Tests must not delete the session cookie to simulate expiry**: that also drops the agency (`tid`), so the BFF
  answers `409 no_agency_selected`, not 401. The live check uses Playwright `page.route` to answer one
  `401 session_expired` for the export, then lets requests through.
- **Observability gap:** the CRM web container writes no request log, so a BFF `401 session_expired` (or any BFF
  answer) is invisible in CloudWatch; this bug was diagnosed from Cognito `Token_POST` events in CloudTrail plus the
  absence of API log lines. The web logs only auth-route failures (`crm_web_refresh_failed`, `crm_web_sign_in_failed`, the
  invite and staff equivalents), nothing per BFF request. No paid logging added
  here; a cheap option later is one structured `console.warn` line per BFF 401/5xx (status, route, no token, no
  path ids) on the container's stdout, which ECS already ships to its log group.
- jsdom: `window.location` is replaced with `vi.stubGlobal("location", …)` in the test (it is `globalThis`), and
  `HTMLAnchorElement.prototype.click` is spied to read the download file name.
