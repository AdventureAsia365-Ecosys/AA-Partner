# AA-979 P2b step 4: the Content studio on Fargate with the partner-pool sign-in

Session S252, agent Claude Code. Branch `feat/aa-979-p2b-portal-fargate`. Design: AA-Ecosys
`.tmp-session/s252/aa979/design.md` §2.1 / §2.4 with critic fixes C2, C3, S1, S2, S8, S10; orchestrator decisions Q2
(share the web session read-only), Q3 (API-key sign-in behind `PORTAL_API_KEY_SIGNIN`), Q8 (shared log group, Infra),
Q13 (Spot, Infra). Not in this PR: the DNS cutover (Infra `crm_agency_live`), the `/trip` slice, the clean-up.

## Decisions

- **One build, two targets.** `PORTAL_STANDALONE=1` (set only by `portal/Dockerfile`) turns on `output: standalone`,
  `assetPrefix: /portal-assets` and the multi-zone rewrite of `/portal-assets/_next/*` to `/_next/*`. The Vercel build
  sets nothing and stays the P2a portal (the rollback target), so no Vercel env change is needed for previews.
- **Gate order** (critic C3): API-key session (no `amr`) first while the flag is on, then the CRM web's partner session,
  then the web's `/auth/refresh`. The exchange happens in the proxy for pages (the renewed cookies are put on the
  response AND on the request the page renders with) and in the BFF for client fetches (critic S1); the BFF's 401
  carries `x-aa-session: expired` and `SessionWatch` reloads the page once (never more than once a minute).
- **Chooser and no-access pages live under `/portal-access/`**, outside the `/portal` shell (the shell needs a studio
  session they do not have); the ALB's `/portal*` rule still sends them to the studio. The no-access page offers sign-out
  and the support address, not a CRM link (for a studio-only person the CRM sends them straight back).
- **Write guard on the public origin** (`lib/writeGuard.ts`, critic C2), used by every write route; no route calls the
  vendored `refuseCrossSiteWrite` directly (guard test). Redirects are built with `publicUrl()`.
- **Read-only session copy**: `lib/session.ts` / `lib/seal.ts` are byte copies of `web/lib/` (a local unit test compares
  the bytes; the network checker `check_vendored_copies.py` is for CIS copies only). A test refuses any writer of the
  CRM cookies in `portal/`. `lib/config.ts` exports a `WebConfig` type with `secureCookies` / `sessionSecret`, so the
  copy compiles unchanged.
- **One sign-out** (`lib/signOut.ts`): the BFF clears the studio cookies and answers the web's sign-out path when a
  partner session exists; the client POSTs it (the web also clears the `cis_*` cookies, web PR).
- **Per-browser limit on the API-key sign-in** (`lib/viewerLimit.ts`, critic S2): `CloudFront-Viewer-Address`, 10 tries
  per 5 minutes per IP (IPv6 /64), in memory; no header (Vercel) = CIS's own throttle only.
- **Sentry** `@sentry/nextjs` 11.6.0 (published 08/10/2026), server and browser, only with a DSN; Sentry 11's
  `dataCollection` off for users, cookies, headers, query strings, bodies, frame variables, plus `beforeSend` scrub; no
  tracing. No `withSentryConfig` (no source-map upload, nothing at build time).
- **Favicon** moved to `app/portal/icon.ico` (critic S10: `/favicon.ico` on the agency host is the CRM web's).
- **Smoke**: `ui-smoke-portal.yml` gains `target` (vercel | fargate) and `resolve_via` (pins the agency host to the new
  CloudFront distribution in `/etc/hosts` for a pre-cutover run); the host spec knows both targets.

## Changed

- Design: drop the `/` -> `/portal/dashboard` redirect on Fargate via `PORTAL_ROOT_REDIRECT`. Not needed: the ALB sends
  `/` to the CRM web on the agency host, so the studio never sees it; the redirect stays for Vercel.
- Design: pages `/portal/select-tenant` and `/portal/no-access` became `/portal-access/select` and `/portal-access/none`
  (outside the shell, see above).
- The portal BFF does not add `x-api-key` limits beyond the sign-in (unchanged CIS behaviour).

## Tradeoffs

- `next build` was not run locally (RAM rule); "Portal Build" and "Portal Docker Build Check" in CI are the build
  checks, the latter also proves the asset prefix (`/portal-assets/_next/static/*` 200) — the design's gate.
- The browser bundle carries the Sentry SDK even without a DSN (tens of kB); accepted for one code path everywhere.

## Should know

- **Merge order**: the Vercel freeze PR (`feat/aa-979-portal-vercel-freeze`) merges and its one production deploy runs
  BEFORE this PR, otherwise this PR would have shipped to the live Vercel host (critic C5). This PR does not touch
  `vercel-deploy-portal.yml`, so it rebases cleanly on the freeze.
- Needs AA-CIS-Infra PR-1 applied (ECR, service at desired 0, edge, deploy role) and the GitHub `dev` environment
  variables `CRM_PORTAL_ENABLED=true`, `CRM_AGENCY_CF_DOMAIN` (output `crm_agency_edge.distribution_domain`), optional
  `PORTAL_PUBLIC_SENTRY_DSN`; then scale `aa-crm-dev-portal` 0 -> 1 after the first image.
- Needs the CIS PR deployed for the partner path (`/auth/tenant-session`); without it the partner path shows
  `/portal-access/none?reason=unavailable` and the API-key sign-in still works.
- Verified locally: `tsc --noEmit` (portal and e2e) clean, `npm test` 39 passed, ESLint clean on the changed files,
  `api/tests/unit/test_portal_guards.py` 68 passed. No `next build`, no Docker build, no browser.
