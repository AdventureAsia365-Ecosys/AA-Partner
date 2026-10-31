# AA-952 (CRM-23) — S261 live findings F1 and F2 (lane pay-fixes)

Source: root `docs/audits/2026-10-31-S261-evidence/AA-952/README.md` (live check of slice 4, AA-Partner #143), findings
F1 (medium) and F2 (minor). Touches AA-955 (CRM-25, card E "terms updated after viewing -> re-accept before payment").
No migration.

## Decisions

- **F1, client side: a new pay window state `terms`** (`app/payments/claims.py`). `pay_window(..., terms_first=True)`
  turns an `open` window into `terms`; claimed, settling, expired and paid keep their own state. Only the client view
  passes it (`client_invoices`, one `reaccept_needed` read per page, deposit invoices only: the same rule as
  `proposals.service.require_terms_current`). `_client_invoice` gives `pay_by: null` and `can_claim: false` for any
  state but `open`, so no bank details and no button. The agency views (`agency_view`, receivables, reminders) are
  unchanged: the agent already gets 409 `terms_reaccept_required` on Record payment and sees the re-accept state on
  the trip's agreements card.
- **F1, API: the claim and the card session refuse first.** `claims.claim` and `claims.card_payment` call
  `_refuse_terms_first` after the window checks and **before any write**: 409 `terms_reaccept_required` (with
  `version_label`), one `denied` audit event (`invoice_claim` / `invoice_card_session`, reason
  `terms_reaccept_required`; `tenant_tx` / `client_link_tx` keep denied events on rollback). Since `claim` raises,
  `public.claim_payment` never reaches `claims.extend`: no claim, no verify task, no bell, no commitment move, no hold
  extension. A repeat press on an invoice already `payment_claimed` still answers "claimed" (idempotent, as before).
  A balance invoice is not held by the rule (the deposit is paid by then, so `reaccept_needed` is None anyway).
- **Why builder versions saved `terms_doc_id` null**: `builder._skeleton` inserted `NULL` terms and the builder UI's
  select defaulted to "No terms" (the text proposal panel defaults to the published version); the API path the C-M3
  proof and the S261 run use (`POST builder`, settings without `terms_doc_id`, publish, send) never set it. The
  client then accepted without terms, and `reaccept_needed` (published terms with no acceptance on the trip) blocked
  the deposit straight away.
- **Design-v3 rule** (F6 step 3 "the client accepts the terms version (stored with the `price_hash` and hold ids,
  R34)", WF-7 "[x] I accept <agency>'s booking terms (v...)", M12): the accept always carries the agency's booking
  terms when it has published some. So: the builder skeleton starts with the published version, and
  `proposals.service.pin_current_terms` pins it to a draft without terms at builder **publish** (where the builder's
  price hash is fixed, so it covers the terms) and at text **send** (where the text hash is fixed). A version that
  names terms keeps them; an agency with no published terms is unchanged. One audit event `proposal_terms_pin`
  (reason `current_terms`; the version label has hyphens and the audit reason pattern refuses them).
- `text_proposal.current_hash` (the text draft preview's hash) computes with the terms Send will pin, so card R's
  "the hash you saw is the hash sent" still holds for a draft without terms.
- **F2: the agency timezone travels with the data.** `GET /trips/{id}/invoices` and `GET /invoices` carry
  `timezone` (`crm.tenant_settings.timezone`; default `Asia/Ho_Chi_Minh` like calendar / duty / reports; an unknown
  name falls back to the default). The web prints the claim note's deadline with `formatInstant(value, locale,
  timeZone)` (`lib/dates.ts`: day, short month, year, time, `timeZoneName: "short"`, e.g. "5 Nov 2026, 11:23 am
  GMT+7") on both the trip Invoices card and `/payments`, the pattern of #142 (calendar). The server render and the
  browser print the same text (no UTC-then-local flip at hydration).

## Changed (vs the finding's suggestion)

- The finding suggested "hide or disable the pay box ... and/or refuse the claim": both are done, and the card
  session refuses too (the same gate on every client pay path).
- The box is not hidden: the deposit line and "X to pay by <date>" stay, with an amber line "Please accept the updated
  booking terms above first ..." (EN + VI, `client.pay.terms` / `termsUntil` with the hold deadline). The client
  error `client.error.terms_reaccept_required` words a 409 that a stale page might still get.
- Builder and text proposal forms: "No terms" is offered only when the agency has no published booking terms (it
  would be pinned anyway), and the builder select defaults to the published version.

## Tradeoffs

- Pinning at publish / send overrides an explicit "No terms" on a version when terms are published. Accepted: with
  published terms such a version can never take its deposit without the client accepting them (the AA-955 rule), so
  the choice only moved the acceptance to a separate re-accept step; the UI no longer offers it.
- The client view reads `reaccept_needed` twice per page (the `terms_reaccept` block and the pay box). Two small
  indexed reads; kept separate so `claims` does not depend on the page builder.
- The client's own pay box times (`ClientPayBox.when`) stay in the traveller's browser zone: the client is not the
  agency and F2 is about the agency views. Not changed.
- `/payments` and the trip card fall back to "UTC" (labelled) if an older API answers without `timezone` (deploy
  order); the API in the same PR always sends it.

## Should know

- No migration, no new SQLSTATE, no new job kind. API + web in one PR.
- Existing data on Dev: builder versions already sent keep `terms_doc_id` null (sealed after draft). Only new
  versions (and drafts published after the deploy) pin terms. The S261 trips A and B already re-accepted.
- Tests: unit `test_invoice_claims.py` (terms state, claim / card refuse with nothing written), `test_builder_service.py`
  (skeleton + publish pin, hash covers the terms), `test_text_proposal.py` (preview hash with the pinned terms,
  `pin_current_terms`), `test_invoices.py` (`timezone`); integration (PostgreSQL 16, RDS role shape)
  `test_invoice_claims_api.py::test_terms_first_the_pay_box_and_the_claim_wait_for_the_reaccept`,
  `test_client_proposal_api.py::test_a_builder_version_carries_the_published_terms_and_no_reaccept_waits`,
  `test_invoices_api.py` (timezone); web vitest `dates.test.ts` (`formatInstant`), `clientProposal.test.ts` (terms
  state, EN/VI strings), `invoiceClaimNote.test.ts` (Invoices card note in the agency zone whatever zone renders).
