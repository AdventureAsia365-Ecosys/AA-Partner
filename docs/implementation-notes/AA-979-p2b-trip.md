# AA-979 P2b §4.3 — `/trip/[tourId]` on the client host

Runbook: root `docs/audits/2026-10-26-S252-aa979-p2b/RUNBOOK.md` §4.3 (design §3 step 9, §5 301 map; critic S5, S9).
Session S254, agent Claude Code. This PR is the AA-Partner half (1); the CIS redirect (2) ships only after this page
is live on `go-dev.`.

## Decisions

- **Data from CIS `GET /v1/trip/{id}`, server side** (`web/lib/tripPage.ts`), base URL from the new env
  `CIS_API_URL` (`web/lib/config.ts`, optional, an https origin; no code default: the repo is public and every host
  comes from Terraform). The page is unchanged in what it shows: name, subtitle, summary, highlights, itinerary, the
  "Design This Journey" mail link; no price (ADR-2026-030 §8.4).
- **Same answers as the CIS page**: 301 from CIS (an older version of a served catalog key) -> `permanentRedirect`
  (308) to `/trip/<current id>` on this host; 410 (retired) and 404 -> the not-found page (Next cannot answer 410 from
  a page). A tour id that is not a UUID is 404 before any request.
- **One deliberate difference: CIS 5xx / timeout / unset `CIS_API_URL` throws (500)** instead of the CIS page's 404.
  A 404 tells crawlers and the T10 consumers that the trip is gone; a 500 is retried.
- **Client host only.** `/trip/` joins `CLIENT_PATH_PREFIXES`; on the partner host it is 404 like `/c/*` (the partner
  host would otherwise send a signed-out visitor to sign-in for a public page). Local / test serve both on localhost.
- **Indexable, as on CIS today** (critic S9). The CIS admin layout sets no robots rule, so `aa-cis…/trip/*` is
  indexable now. Here the root layout says `noindex`, so the trip page overrides it (`robots: index, follow`), and
  `public/robots.txt` gains `Allow: /trip/` above `Disallow: /`. A static rule is enough, no host-aware
  `app/robots.ts`: `/trip/` exists only on the client host (404 on every other host), and the longest matching rule
  wins (RFC 9309), so every other path of every host stays disallowed.
- **Kit tokens, not the CIS page's fixed colours**, so the page follows the light / dark theme like the other client
  pages; the brand header and typography are the CIS page's.
- **Next data cache 1 h** (`revalidate: 3600`) as on CIS; the CIS read stamps `url_alive` on every uncached call.

## Changed

- `web/lib/tripPage.ts` (new): the CIS read, answer mapping, body parsing.
- `web/app/trip/[tourId]/page.tsx` (new): the page and its metadata.
- `web/lib/hosts.ts`, `web/proxy.ts`: `/trip/` on the client host; 404 on the partner host.
- `web/lib/config.ts`: `CIS_API_URL` -> `cfg.cisApiUrl`.
- `web/public/robots.txt`: `Allow: /trip/`.
- Tests: `web/tests/tripPage.test.ts` (new), `web/tests/routing.test.ts`.
- `CONTEXT.md`: client host serves `/trip/*`; web env `CIS_API_URL`.

## Tradeoffs

- 500 on a CIS fault vs. the CIS page's 404: a short CIS outage no longer looks like "trip gone" to crawlers; the cost
  is a 500 page for visitors during it (there was a 404 page before, so no visitor-facing loss).
- Static `robots.txt` vs. host-aware `robots.ts`: fewer moving parts; the one cost is that the partner hosts also show
  `Allow: /trip/`, which matches nothing there.

## Should know

- **Needs AA-CIS-Infra** (separate PR, branch `feat/aa-979-web-cis-api-url`): `CIS_API_URL` in `crm_web.tf`'s web env
  (the value of the existing `var.crm_cis_api_url`). Without it every `/trip/<id>` is 500 (nothing links there yet).
  Apply it, then deploy the web (Deploy Web Dev) so the running task has the env.
- Order (runbook §4.3): Infra env -> this PR + web deploy -> live `curl -sI https://go-dev.lumiguides.it.com/trip/<id>`
  200 -> only then the CIS redirect PR (AA-CIS-App `feat/aa-979-trip-redirect`, 307).
- The CIS page stays in AA-CIS-App until the clean-up: the 307 is the rollback lever (revert it, the CIS page serves
  again at once).
