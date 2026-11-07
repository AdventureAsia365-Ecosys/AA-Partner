# AA-1038 R5 slice 3 — TripPlanner leads into the CRM (AA-Partner part)

Session S272 (lane l10). Root ADR 0019 d11 (+ the S272 amendment), contract `lead.submitted.v1` (root
`docs/contracts/lead.submitted.v1.openapi.yaml`). Other parts: AA-TripPlanner-Web (producer, outbox, M2M routes), CIS
390 (the lead route in `catalog.v_tenant_tripplanner_v1`), root (contract, ADR, DM-7.1).

## Decisions

- **Pull over HTTP, not a DB grant.** ADR 0008a d1: the CRM holds no grant on another schema and the isolation probe
  asserts it both ways, so the CRM pulls TripPlanner's `/trip/m2m/v1` with its existing M2M client (one new scope,
  `tripplanner.leads/deliver`) and acknowledges each lead; the ack is what lets TripPlanner forget it.
- **One agency per call** (`/tenants/{agency}/leads`): TripPlanner binds that tenant (RLS) and the CRM checks the lead's
  tenant again in its own `system_tx` (`ForeignLead`: never stored, never acknowledged under the wrong agency).
- **Reuse the inquiry path.** A lead becomes Stage 1 answers (`stage1.validate`, the hosted form's validator) and goes
  through `store_submission` + `crm_intake_process`: the same dedupe (e-mail / phone blind index), consent rows, reply
  task and workflows as a website inquiry. No migration: `form_submission` needs an intake form for stage s1, so each
  agency gets one system form "Trip planner" (lead source `tripplanner`, status `disabled`, created by the job on the
  first lead). The agency sets the default owner of planner leads on it in Settings > Forms.
- **Mapping:** countries -> Stage 1 destination codes (anything else `not_sure`), `days - 1` -> nights, no budget ->
  `not_sure`, the trip summary (tours, stops per day) and the traveller's message -> the inquiry message (<= 2000
  chars). An optional answer that does not validate (a past start date, a phone the CRM rejects) is dropped; a required
  one (name, e-mail, contact consent) rejects the lead (`ack rejected` with the reason).

## Changed

- `app/tripplanner/leads.py` (new), kind `crm_tp_lead_pull` (`kinds.py`, `handlers.py`, hook), `TRIPPLANNER_API_URL`
  (`settings.py`, `main.py`), lead source `tripplanner` (`lead_sources.py`, web labels EN / VI), `test_code_rules`
  (`system_tx` allowed in the new module), `test_lifespan_worker` (kind + hook lists).
- Tests: `tests/unit/test_aa1038_tp_leads.py`, `tests/integration/test_aa1038_tp_leads_db.py` (A's lead in A only,
  ciphertext only, idempotent, refused under B, the intake job makes a trip in A).

## Tradeoffs

- A per-minute slot calls TripPlanner once per agency: fine at pilot scale; a "which tenants have leads" call is the
  next step if the agency count grows.
- A rejected lead is lost to the agency (TripPlanner redacts it). Since the S272 second review pass it is never silent:
  the ops alert `tp_lead_rejected` (ids and the reason code), an ERROR line, and TripPlanner's daily run asks for
  attention (`lead_rejected_by_crm`). Both sides apply ONE Traveller rule with shared vectors, so it should not happen.

## Should know

- Live only after AA-CIS-Infra: the aa-m2m resource server `tripplanner.leads` (scope `deliver`) granted to the CRM
  client, and `TRIPPLANNER_API_URL` on the CRM task (API + worker). Until then the job logs `not_configured` /
  `token_unauthorized`, never lists a tenant, and TripPlanner refuses crm leads (503 `lead_channel_unavailable`,
  nothing stored): its switch `TP_LEAD_CRM_PULL_ENABLED` is off, and with it on, the per-tenant beacon is stale.

## Review fix pass (S272)

- **A phone is never lost.** TripPlanner now pins `Traveller.phone` to E.164 with its country code (contract pattern
  `^\+[1-9][0-9 ()-]{5,39}$`), but `normalise_phone` (no region) still rejects numbers `phonenumbers` does not know.
  When the phone does not normalise, `answers_of` drops it from the phone field as before AND writes it on the second
  line of the inquiry message ("Phone as typed (not checked): ..."), above the trip summary, so a cut message keeps
  it. Test `test_a_phone_tripplanner_accepts_is_never_lost` runs TripPlanner's pattern over sample phones.
- **The pull starts only when TripPlanner offers it.** TripPlanner refuses crm-routed leads (503) until its
  `TP_LEAD_CRM_PULL_ENABLED` is on; switch it on only with this CRM's M2M scope and `TRIPPLANNER_API_URL` (Infra).

## Review fix pass 2 (S272)

- **One Traveller rule on both sides.** `traveller_errors` applies the contract's rule (control characters out, NFC,
  trimmed, length in code points, the e-mail and E.164 phone patterns) with the constants of TripPlanner's `validate`.
  Shared vectors: `docs/contracts/rules/lead.submitted.v1.traveller-rules.json` (a byte copy of the root file; the root
  is the source, TripPlanner keeps another copy under `backend/tests/contracts/`). Tests: every vector gives the same
  verdict here and at TripPlanner, a name / e-mail TripPlanner accepts is never rejected here (the review repro: a
  name NFC lengthens past 120), one it refuses is rejected here too. A phone outside the rule is kept as text on the
  message (never lost), not rejected: the consumer is the lenient side on an optional field.
- **A rejected lead raises an alert.** `pull_agency` emits the ops alert `tp_lead_rejected` (scope = the lead id, one
  e-mail per lead; details tenant id, lead id, reason code; no traveller data) and logs an ERROR line before it acks
  `rejected`. The lead id is echoed only as a UUID (`_lead_ref`, anything else becomes `invalid`).
- **A tenant this CRM cannot serve stops TripPlanner storing its leads.** The agency is entered (an empty `system_tx`)
  BEFORE its leads are listed, so a locked / offboarding / unknown agency is never listed. TripPlanner treats each
  list call as that tenant's beacon and refuses crm leads (503 `lead_channel_unavailable`, nothing stored) when the
  CRM has not listed the tenant within `TP_LEAD_CRM_PULL_FRESH_S` (10 minutes). Chosen over a CIS-side readiness flag
  because CIS does not hold the CRM's lock state and it needs no migration; the limit: a lead sent in the first minutes
  after a lock is still stored and waits for the hold (at most 7 days, then TripPlanner's expiry attention).
