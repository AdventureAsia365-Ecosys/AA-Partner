# AA-1036 — R3 slice 2: cut-over and retire (AA-Partner side)

Follows `AA-1036.md` (slice 1, #210). Root ADR 0019 d6 / d7. Two PRs here, each paired with an AA-CIS-Infra PR
(`docs/implementation-notes/AA-1036-r3-cutover.md` there).

## Decisions

- **Step A (cut-over)** is a workflow and verify change plus copy, no studio code change: the merged studio already
  answers every studio path (checked against a local standalone build with `WEB_CONTENT_HOME=true`:
  `edge_verify.sh crm-web` all green, `e2e/smoke-portal/aa885-portal-host.spec.ts` 24 passed with
  `PORTAL_TARGET=fargate`).
- **The crm-portal checks move into the crm-web verify** instead of re-pointing `crm-portal.rows` at the web:
  `crm-web.rows` gains `studio-healthz` (`/api/healthz` with `service: "aa-crm-web"` and `studio: "merged"`, on the
  deployed commit) and `edge_verify.sh crm-web` gains `content-home`, `studio-signin`, `api-key-signin-off`,
  `studio-legal`, `studio-page`, `web-assets`. A studio rule still forwarding to the old service is red twice
  (`aa-partner-portal` on `/api/healthz`, a 200 on `/terms`).
- **The studio smoke moves to Deploy Web Dev** (`smoke` job, `ui-smoke-portal.yml`, sign-in required, the same three
  secrets). Its specs now expect the merged app on the agency host: "/" lands on `/portal/dashboard`, the draft legal
  pages are `/legal/privacy` and `/legal/partner-terms` (the old paths answer 308), the CRM entry of the "Apps"
  switcher (`portal-crm-link` -> `/home`, or the `portal-app-crm` "Request add-on" mail link).
- **Deploy Portal Dev becomes dispatch-only** in step A (rollback path: rules pointed back at the portal group, then
  a dispatch redeploys and verifies the old service as `crm-portal`); it no longer runs the smoke, whose specs now
  describe the merged app. Step B deletes it.
- **"Open CRM" links**: nothing in `web/` links the CRM home as "/" from the studio (the switcher links `/home`, slice
  1); the old studio's "/" link dies with `portal/`. The CRM's own "Home" (`/home`), `not-found` and `join` links to
  "/" now land on Content, which is the d7 intent.
- **`AGENCY_HOST_ROUTED_ELSEWHERE`** (`web/lib/legal.ts`) is just `/crm`, `/crm/*` after the cut-over; the CRM terms
  stay at `/legal/terms` (the studio's `/terms` is a 308 to its own draft terms).
- **Browser Sentry**: Deploy Web Dev passes the `dev` variable `WEB_PUBLIC_SENTRY_DSN` as the
  `NEXT_PUBLIC_SENTRY_DSN` build arg (empty = off, same as today). The server `SENTRY_DSN` needs a web DSN secret in
  Infra: follow-up.

- **Step B (retire)** deletes `portal/`, `deploy-portal-dev.yml`, `crm-portal.rows`, the `crm-portal` app of the
  verify workflow and of `edge_verify.sh`, the CI jobs "Portal Build" and "Portal Docker Build Check" and the portal
  `npm audit` step, the dependabot `/portal` entry, the portal lines of CODEOWNERS (the studio's BFF, helpers, 308 map
  and guards in `web/` get owners instead), the portal parts of the pre-push hook, the `PORTAL_COPIES` vendored set and
  the portal kit lock check. `test_portal_guards.py` goes with `portal/`: the rules that still apply moved into
  `test_studio_guards.py` (its shared constants and helpers, the studio smoke secrets / no auth state upload, the
  kit-token contrast checks, the bare-accent and placeholder rules on `web/app/(studio)`), plus a guard that nothing
  builds, deploys or updates `portal/` any more.
- **Kept on purpose**: `ui-smoke-portal.yml`, `e2e/smoke-portal/` and `playwright.portal.config.ts` (the studio smoke,
  now run by Deploy Web Dev; the local default port is the web's 3005), the secrets `E2EPORTAL*`, and
  `studio_suppressions_base.py` (with no `portal/eslint-suppressions.json` at the base it adds nothing; removing it is
  a follow-up once no open PR has a base with `portal/`).

## Changed (vs. the lane brief)

- "deploy workflow smoke URLs": the smoke URL itself does not change (the agency host); the smoke moved from the portal
  deploy to the web deploy.

## Tradeoffs

- Keeping `crm-portal` (rows, `post-deploy-verify.yml` choice, `edge_verify.sh` branch) until step B costs a few lines
  but keeps a verified rollback.

## Should know

- Order: AA-CIS-Infra step A merged and applied BEFORE this PR merges (its Deploy Web Dev verify expects the studio
  paths on the web and "/" -> `/portal/dashboard`, which needs the new task definition revision with
  `WEB_CONTENT_HOME`; the deploy copies the latest revision, so the apply must come first).
- Step B: "Portal Build" is a required status check on `main` (branch protection); it must be removed from the
  required list before step B can merge, since the job is deleted.
- After step B merges, the GitHub environment `dev` variables of the old deploy (`CRM_PORTAL_ENABLED`,
  `ECR_PORTAL_REPOSITORY`, `ECS_PORTAL_SERVICE`, `ECS_PORTAL_TASK_FAMILY`, `PORTAL_HOST`, `PORTAL_PUBLIC_SENTRY_DSN`)
  are unused and can be deleted.
