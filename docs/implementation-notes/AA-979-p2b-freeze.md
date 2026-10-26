# AA-979 P2b step 3b: freeze Vercel production of the portal

Session S252, agent Claude Code. Branch `feat/aa-979-portal-vercel-freeze`. Design: AA-Ecosys
`.tmp-session/s252/aa979/design.md` §2.1 "Vercel during the window" and critic fix C5. Runbook step 3b.

## Decisions

- **No production deploy on push to main.** `vercel-deploy-portal.yml` deployed production on every push touching
  `portal/**`, and production could be dispatched from `main` only, so merging the P2b portal PR would have shipped
  the Fargate-only sign-in gate (it redirects to the CRM web's `/auth/refresh`, which Vercel does not have) to the live
  `portal.` host before the DNS flip: every tenant locked out. Production is now dispatch-only (from `main`).
- **The old Vercel URL answers 301 with its path** to `https://portal.lumiguides.it.com/:path*`, conditioned on
  `host = aa-cis-portal.vercel.app`, so a rollback that points `portal.` back at Vercel serves the studio and does not
  loop. `statusCode: 301` (Vercel's `permanent: true` would be 308).
- PR previews and the preview smoke stay until the clean-up (P2b + 7 days).

## Should know

- **Merge this BEFORE the P2b portal PR** and right after the merge dispatch `Vercel Deploy (portal)` with
  `target=production` on `main`: that one build (P2a code + the 301) is the rollback target for the whole window.
  Check `curl -sI https://aa-cis-portal.vercel.app/tenant-login` -> 301 `location: https://portal.lumiguides.it.com/tenant-login`
  and `https://portal.lumiguides.it.com/tenant-login` -> 200 (still Vercel).
- Vercel Hobby cap (~100 deployments a day, S239): do this dispatch early in the session.
- Guards: `api/tests/unit/test_portal_guards.py` `test_portal_vercel_production_is_frozen_for_the_p2b_window`,
  `test_portal_vercel_old_url_redirects_to_the_portal_host_only_by_host`.
