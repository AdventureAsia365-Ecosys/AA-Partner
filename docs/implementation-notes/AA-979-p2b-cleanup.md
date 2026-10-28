# AA-979 P2b clean-up (runbook §4.5): the portal leaves Vercel, API-key sign-in off (AA-Partner side)

Session S256, agent Claude Code. Branch `chore/aa-979-portal-cleanup`. Runbook: root
`docs/audits/2026-10-26-S252-aa979-p2b/RUNBOOK.md` §4.5; design `design.md` (same folder) §5 redirect table; cutover
evidence `docs/audits/2026-10-27-S253-aa979-cutover/`. Pulled ahead of the "from 03/11" gate: Dev has no real users.
Companion PRs: AA-CIS-Infra `chore/aa-979-portal-cleanup` (flag off, `partner-dev.` 301, CRM API origins), AA-CIS-App
`chore/aa-979-remove-frozen-portal`, root `docs/aa-979-portal-cleanup`.

## Decisions

- **API-key sign-in default flipped to OFF** (`portal/lib/config.ts`: only `PORTAL_API_KEY_SIGNIN=true` turns it on).
  Infra sets `"false"` as well; the default makes the clean-up hold even if the env var is ever dropped. A local studio
  without partner sign-in sets it to `true`.
- **`/tenant-login` answers 308** to `<CRM_WEB_SIGN_IN_PATH>?next=/portal/dashboard` on the public origin (design §5:
  "308 `/auth/sign-in?next=/portal/dashboard`"; the studio's configured sign-in path is `/auth/refresh`, which signs in
  when there is no session and renews otherwise). Before, the flag-off branch answered 307 with `next=/tenant-login`:
  once signed in, the web would send the person back to `/tenant-login`, which sends to the web again: a loop. Without
  partner sign-in (local) it stays 404.
- **Vercel removed**: `vercel-deploy-portal.yml`, `portal/vercel.json`, root `.vercelignore`, the CODEOWNERS lines, the
  deploy trigger exclusion. `ui-smoke-portal.yml` loses the `target` input (always the Fargate studio,
  `PORTAL_TARGET: fargate`) and the bypass secret.
- **The logged-in portal smoke signs in like a person**: `auth.setup.ts` goes `/portal/dashboard` -> the web's
  `/auth/refresh` -> partner-pool managed login (password + authenticator code, shared `helpers/signin.ts`; the passkey
  offer helper moved there from the phase 2 helpers) -> the studio. Credentials: repository secrets `E2EPORTALEMAIL`,
  `E2EPORTALPASSWORD`, `E2EPORTALTOTP` (no underscores, like the CRM E2E secrets). `require_tenant_key` became
  `require_tenant_login` (red when any of the three is missing). A local studio run still uses `E2E_TENANT_API_KEY`.
- **Post-deploy verify of the studio**: the page whose assets are checked is now `/terms` (public studio page); a new
  row `api-key-signin-off` wants `/tenant-login` -> 308 to `/auth/refresh?next=%2Fportal%2Fdashboard` (a 200, a 307 or
  a `next=/tenant-login` loop are red). The container smoke in CI checks the same.
- **Studio sidebar CRM link** is the same-origin `/` (the CRM home on the shared agency host) instead of the absolute
  `partner-dev.` origin, which only answers 301 now. The smoke checks `href="/"`.
- **CRM web verify default** (`CRM_WEB_BASE_URL` fallback) moved to `https://portal.lumiguides.it.com`: `partner-dev.`
  answers 301 for everything but `/crm/*`, so the web's signed-out redirect check must run on `portal.`.
  `CRM_API_BASE_URL` stays on `partner-dev.` (the `/crm/*` paths are not redirected; the deploy gates POST there).

## Changed

- The runbook lists "delete the Vercel project / domain / `VERCEL_PORTAL_*` + `E2E_TENANT_API_KEY`"; the code side also
  needed the smoke's sign-in rewritten (without it the Deploy Portal Dev smoke turns red once the flag is off), the
  verify rows changed and the CRM link moved. `VERCEL_TOKEN` (team-scoped, used only by the portal workflow) goes too.

## Tradeoffs

- No portal PR previews any more: Portal Build + Portal Docker Build Check (with its container smoke) before merge,
  the Fargate verify + smoke after deploy. Accepted in the design (previews were kept "until clean-up" only).
- The `installBypassRoute` / `x-vercel-protection-bypass` helpers in the smoke specs stay: they do nothing without the
  secret and removing them touches every spec for no behaviour change.
- `scripts/pre-push.sh` still names `.vercelignore` in its path rules and self-test fixtures; harmless (59/59 pass).

## Should know

- **Order** (orchestrator): Infra PR first (apply: task def revision with `PORTAL_API_KEY_SIGNIN=false`), then this PR.
  Before merging this PR, the three `E2EPORTAL*` repository secrets must exist and the user must have an active
  studio membership on the E2E tenant (the Deploy Portal Dev run this merge triggers needs both). If the Infra apply has
  not reached the running task, the verify's `api-key-signin-off` row is red (the default is off, so a fresh deploy
  from this code is off anyway, unless an old task def still says `true`).
- The e2e studio user: a synthetic partner-pool identity (password + TOTP) with a studio role able to read every page
  the smoke opens (owner proposed, the API-key session had full access). The S253 phase 2 run created `studio-viewer`
  (viewer); either raise its role or invite a new one with `phase2.spec.ts` (`E2EP2STAFF=a10b`, `E2EP2ROLE=owner`).
- Repo variables to point at `portal.` once the 301 is live: `E2ECRMPARTNER` (e2e-dev.yml) and, if set, the `dev`
  environment variable `CRM_WEB_BASE_URL`.
