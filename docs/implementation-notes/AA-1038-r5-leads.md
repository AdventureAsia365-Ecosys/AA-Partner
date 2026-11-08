# AA-1038 R5 close-out — the lead pull calls TripPlanner only for agencies whose leads come here (lane l2-r5-leads)

Session S273. Follows `AA-1038-r5-slice3.md` (the `crm_tp_lead_pull` consumer of `lead.submitted.v1`). Live state
before this change (08/11): the pull ran every minute over every agency (62), 58 got TripPlanner's 404
`unknown_tenant` and each was counted as an error (`status: partial` every minute).

## Decisions

- **The entitlement lives in the CRM's own projection of the CIS account, not in a new call.** TripPlanner serves a
  tenant only when CIS 390 resolves its `lead_channel` to `crm`: a TripPlanner subscription that is served
  (`tp.hosted` live, in service) and `crm.core` live. `crm_entitlement_pull` already reads the tenant's
  `tenant.entitlements.v1` snapshot (events + hourly reconcile), so it now also projects
  `crm.tenant_entitlement.tp_crm_leads` = both `tp.hosted` and `crm.core` entitled (crm-047). The lead pull reads it in
  the `system_tx` it already opens per agency before any list call; false (or no row) = skipped with no HTTP call
  (`skipped_not_routed`). Rejected: a CIS snapshot per agency per minute (58 calls moved from TripPlanner to CIS), an
  in-memory negative cache of TripPlanner 404s (still calls, lost on restart), a TripPlanner "tenants on crm" route
  (a contract change in two repos for a fact the CRM already receives).
- **`in_service` is not part of the flag**: an agency out of service is locked by the same feed, and the pull never
  enters a locked agency (unchanged).
- **TripPlanner's 404 `tenant_not_on_crm` / `unknown_tenant` is a counted skip** (`skipped_not_on_crm`,
  `TenantNotOnCrm`, a subclass of `HttpError` so other callers still see an HTTP error). It covers the minutes between
  a CIS change and the next projection. Any other 404 (a wrong path, a non-JSON body) stays an error.
- **One summary line per run**, always (also with nothing to do): `tp_lead_pull_summary agencies= listed= pulled=
  skipped= skipped_not_routed= skipped_not_on_crm= skipped_agency= <outcome counts> errors= [error_classes=]`.
- **One `tp_lead_stored tenant_id= lead_id= outcome=` INFO line per committed lead** (ids only, like the existing
  `tp_lead_rejected` ERROR line), so the end-to-end check can count per agency in CloudWatch without reading the CRM
  database or any personal data.

## Changed

- `api/migrations/047_crm_tp_crm_leads.sql` (contract: ALTER of `crm.tenant_entitlement`) +
  `tests/integration/test_crm047_schema.py` — migration-only PR (MIG-4).
- `app/cis/entitlement_feed.py`: `Projection.tp_crm_leads`, `TP_CRM_LEAD_KEYS`, read / upsert / change detection /
  audit fields.
- `app/tripplanner/leads.py`: `ROUTED_SQL`, `routed_here`, `TenantNotOnCrm`, `NOT_ON_CRM_CODES`, counters `listed`,
  `pulled`, `skipped_not_routed`, `skipped_not_on_crm`, `summary_line`, the `tp_lead_stored` line.
- `app/db.py`: readiness raised to crm-047 (code PR only).
- Tests: `tests/unit/test_aa1038_tp_leads.py` (not routed = no HTTP call, 404 codes, summary line, stored line, the real
  `routed_here` SQL), `tests/unit/test_entitlement_feed.py` (projection rule, apply writes / leaves / clears the flag),
  `tests/integration/test_crm043_entitlement_feed.py` (CIS account with `tp.hosted` -> flag -> the pull lists only
  that agency; the add-on removed -> off again).
- `CONTEXT.md`: the job row and the account feed projection.

## Tradeoffs

- After crm-047 is applied every agency reads false until it is projected again: an event for the tenant, or the
  hourly reconcile once the code is deployed. For up to an hour after the deploy a tenant already on the crm channel
  is not listed, so TripPlanner answers its sends 503 `lead_channel_unavailable` (nothing stored, the traveller sees
  the retry line). On Dev no tenant has the channel yet (S272 comment), so nothing is lost; the orchestrator can
  shorten it by resetting `crm.entitlement_feed_state.last_reconcile_at` (not needed for the E2E: the add-on change
  itself raises an event).
- The flag mirrors CIS 390's rule in a second place (`tp.hosted` and `crm.core`). If CIS changes how it resolves
  `lead_channel`, the 404 skip keeps the pull quiet, but a tenant routed to crm by a new rule would not be listed until
  this rule follows (the shared reference: contract `tenant.entitlements.v1` 1.5 text).

## Should know

- Ship order: migration PR -> Infra copy -> Migration Apply (contract) -> code PR (readiness crm-047).
- The end-to-end check kit (enable the channel for one audit tenant, Playwright send, CloudWatch count) is in the root
  scratch folder `.tmp-session/S273/l2-r5/` of the session, not in this repo.
