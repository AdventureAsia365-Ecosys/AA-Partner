# AA-953 / AA-951 / AA-955 — two red Dev E2E specs after the S261 merges (lane l8)

E2E Dev run 41172484182 (main 132112aa, 31/10/2026) failed two specs: `client-proposal.spec.ts` (CRM-21b) and
`lifecycle-thin.spec.ts` (CRM-18). The last green run (40306078112, 26/10) was at 336a6da, before #96 (AA-951), #136
(AA-955) and #145 (AA-953): neither spec had run against those changes. Both are spec drift, not product regressions.

## Decisions

- **client-proposal step 4 (`UTC+05:45` on the first `client-day`)**: since #96 (AA-951, R38) the client page always
  opens with a "Before you go" day 0 section (`data-day="0"`): the insurance reminder is always on
  (`prep_packs.client_view` returns `insurance_reminder: True`), so `client-day.first()` is that section, not day 1.
  The live AA-955 check on the synthetic agency did not cause it (the day 0 shows for every builder proposal). The
  spec now asserts the real order (the first day section is day 0 and `client-before-you-go` is visible) and reads the
  zone on `[data-day="1"]`.
- **lifecycle-thin step 6 (`portal-sign-in` not found on the post-deposit link)**: before #145 the portal session
  cookie was scoped to `/crm/public/v1/portal/<token>`, so a new link meant a new cookie path and a new sign-in. Since
  AA-953 the link is exchanged for `aa_portal_link` and the session cookie lives on the fixed
  `/crm/public/v1/traveller` path; the deposit's `access.invite_all` re-issues the link on the **same access**, and
  `_signed_in` accepts the session (same access, same address proof). So the same device opens the new link straight
  on `portal-home`. That is the designed behaviour (the session is bound to the access and the address its code went
  to, never to one link; the old link is revoked and answers 404 even with the session). The spec now asserts both:
  the early link shows `portal-unavailable` 404, the new link shows `portal-home` (lead) without a code.
- **lifecycle-thin step 6 signing**: since #136 (AA-955) a Dev API with keys renders the e-sign v1 envelope
  (`SignEnvelope`), which asks the insurance question whenever the agency has a published insurance declination (the
  AA-955 live check published one on the synthetic agency: the sign button would stay disabled). Step 1 now publishes
  its own run-unique insurance declination (as it already did for terms and waiver), so the run does not depend on what
  an earlier check left published; step 6 checks the envelope shows this run's terms, waiver and (after "declined")
  declination, signs, and asserts the signed line carries the declined choice.

## Changed

- `e2e/tests/client-proposal.spec.ts`: day 0 then day 1 zone assertions; header comment.
- `e2e/tests/lifecycle-thin.spec.ts`: publishes an insurance declination in step 1; step 6 asserts the old link 404 and
  the session carried to the new link, then signs the v1 envelope with the insurance choice.
- `api/tests/integration/test_portal_person_api.py`: `test_a_reissued_link_ends_the_old_link_and_keeps_the_session`
  pins the behaviour step 6 relies on (old link 404 with a live session, the session opens the re-issued link, a device
  without the session still needs its code).

## Tradeoffs

- Step 6 no longer exercises the sign-in code on the post-deposit link; step 4 still does the code sign-in, and the new
  integration test covers that another device needs its own code. Signing still needs a fresh code.
- Publishing a declination per run adds one retired version per run on the synthetic agency, like terms and waiver.

## Should know

- Run locally: `test_portal_person_api.py` 9 passed on PostgreSQL 16 (CI uses 15). The specs themselves need the Dev
  E2E secrets; they were not run from this machine — the orchestrator dispatches `e2e-dev.yml` after merge.
- The session carrying over to a re-issued link is by design (AA-953 notes, "A session is bound to the address its code
  went to"); if the product later wants "New portal link" to end sessions, the integration test above is the one to flip.
