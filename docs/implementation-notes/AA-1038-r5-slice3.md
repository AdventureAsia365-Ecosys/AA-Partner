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

## Review fix pass 3 (S272): the name rule is what the intake needs

- **Finding.** The shared rule accepted any non-empty cleaned name, but `crm_intake_process` indexes the name
  (`vault.name_index` -> `blind_index.name_tokens`), which needs at least one letter or digit token. A lead named `-`
  (or `...`, an em dash, an emoji only) passed both pull checks, was stored and acknowledged `stored` (TripPlanner then
  marked it delivered and redacted it), and the intake failed with `NormalisationError(invalid_name)`: no trip, no
  alert, only an access-log line. The traveller's request was lost silently.
- **Decision: one rule, the intake's own function.** The root rule now says a name must hold at least one token: after
  NFKD, a character of Unicode category L or N (`name_token_pattern` `[\p{L}\p{N}]`, `name_token_normalisation`
  `NFKD` in the rules file; Python's `\w` minus `_` is exactly that set, checked over every code point). The CRM does
  not copy the rule: `stage1.has_name_token` calls `blind_index.name_tokens` itself, and both `traveller_errors` (the
  pull) and Stage 1 `validate` (the hosted form and the pull's second check) use it. A planner lead with such a name
  is now acknowledged `rejected` (reason `invalid_lead`), so the `tp_lead_rejected` alert fires here and TripPlanner
  reports `lead_rejected_by_crm`; TripPlanner also refuses the name before storing (422 `invalid_lead`, fields
  `[name]`), so this is a producer-bug path only.
- **Changed beyond the finding.** The hosted Stage 1 form had the same gap since before this lane: a `-` name is now a
  422 on `name` there too, and a companion name without a token is dropped like a blank one (companions are optional;
  the intake indexes each companion's name too). No alert on `intake_failed` was added: with the gap closed at both
  entry points it has no known trigger left, and an alert without a trigger is noise.
- **Other Traveller fields.** E-mail and phone have no such gap: Stage 1 `validate` already normalises them with the
  same `normalise_email` / `normalise_phone` the intake indexes with, and a phone that does not normalise is dropped to
  the message. Test `test_no_other_traveller_field_passes_the_pull_and_fails_the_intake` pins it over the vectors.
- **Tests.** `test_review_repro_a_pulled_lead_named_dash_acks_rejected_not_stored` (the real `ingest`, `-`, `...`, an
  em dash, an emoji: ack `rejected`, alert, nothing stored; fails on the previous commit),
  `test_the_name_rule_is_what_the_intake_job_needs` (every valid name vector indexes, every refused one that survives
  cleaning cannot), the new vectors in the shared file, and Stage 1 cases in `test_intake_stage1.py`.

## Review fix pass 4 (S272): the e-mail is checked lower-case

- **Finding (TripPlanner side).** TripPlanner checked the e-mail, then lower-cased it; U+0130 lower-cases to two code
  points, so it sent addresses whose local part or total length broke the rule and this CRM rejected them at the pull
  (ack `rejected`, alert `tp_lead_rejected`): loud, but the traveller's request never reached the agency.
- **Decision.** The rule's order is now explicit in the root rules file: lower-case first, then the length and the
  pattern. `traveller_errors` lower-cases the cleaned e-mail before its length and `TRAVELLER_EMAIL` check, so its
  verdict is TripPlanner's on every vector, including the new `dotted_capital_i_*` ones (rules file byte copy).
  Stage 1 `validate` is unchanged: `normalise_email` already lower-cases, and a planner lead that passes
  `traveller_errors` is already lower-case and within 254.
- **Tests.** `test_review_repro_an_email_lower_casing_lengthens_is_checked_lower_case` (the review's two addresses and
  the total case, as typed and lower-cased: `['email']`; the as-typed `'İ' * 33` address passed before this change)
  and `test_a_dotted_capital_i_email_within_the_limits_is_valid`.

## Follow-up (S272, lane l14): the inquiry message is cut after Stage 1's NFC

- **Finding.** `message_text` cut the inquiry message at 2000 code points, then Stage 1 `validate` ran `clean_text`
  (control characters out, NFC, trimmed) and measured again. A catalog name whose NFC form is longer (U+0958..U+095F,
  U+0F73, U+FB1D..U+FB4E, U+0344 decompose under NFC) pushed it past 2000, so `validate` refused `message` and
  `answers_of` dropped it. With a phone `phonenumbers` rejects in the same lead, the phone retry wrote the message back,
  the second (last) round refused it again and the whole lead was acknowledged `rejected` (`invalid_lead`).
- **Decision.** `_fit_message` cleans the whole text with Stage 1's own `clean_text` (no copy of its steps) and only
  then cuts to `MAX_MESSAGE`; the cut text is cleaned again and shortened until it fits (a cut inside a combining
  sequence may re-normalise). The message `answers_of` builds is exactly what Stage 1 stores.
- **The retry loop.** `answers_of` now loops `len(OPTIONAL_FIELDS) + 1` rounds (each round drops at least one more
  optional answer), writes the "Phone as typed" line BEFORE the optional answers are dropped, and never into a message
  that is being or was dropped.
- **Tests** (`tests/unit/test_aa1038_tp_leads.py`): an NFC-lengthening stop name (six code points) plus `+999 1234 5678`
  (contract E.164, `phonenumbers` rejects it) -> stored with the message and the phone line (rejected before);
  `message_text` is Stage 1 clean and within 2000; a dropped message is not rebuilt by the phone retry; every
  optional answer failing one per round still stores the lead. All fail on the previous commit.
- Behaviour otherwise unchanged: a message within the limit is the same text (already NFC and trimmed in practice).
