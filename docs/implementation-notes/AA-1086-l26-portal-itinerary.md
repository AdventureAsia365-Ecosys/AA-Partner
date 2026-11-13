# AA-1086 lane l26: the traveller portal shows the trip's AA tour (S279)

Found by the S279 rehearsal take REH-10-portal: the traveller portal itinerary showed seven dated days with no titles,
places or services. Two causes: (a) `app/portal/person.py` lists only the lines the agency marked
`visible_to_travelers`, and an AA line is read-only for members (crm-014 guard 3a, CRM24), so the agency could never
show its AA tour; (b) the portal's days are the accepted version's days, and the builder skeleton creates them without
title or place. Tech lead decision (13/11/2026): (1) a confirmed AA line is visible to travellers by default; (2) when
the agency adds an AA tour to a proposal, the tour's day titles and places are copied into the proposal days (the
agency can still edit them).

Two PRs (MIG-4): `crm-065` (migration-only, contract: a data backfill) first, then the code PR (readiness crm-065).

## Decisions

- **Default at confirm, stored on the row.** The Booking sync sets `visible_to_travelers = true` when it moves an AA
  line to confirmed: `CONFIRM_LINE_SQL` (feed `line.confirmed`, planning -> confirmed only) and `BOOKED_SQL` (Booking's
  booking answer, only when `$4 = 'confirmed'` and the old status is not confirmed). A stored default, not a read-time
  rule, so the voucher release (`travel_ops.vouchers.release_reason`) follows the same flag and a later "hide" stays
  possible (see Should know).
- **crm-065 backfills the confirmed AA lines** written before the code (the rehearsal trip's line among them): every
  agency entered through `app.tenant_id` (FORCE RLS), agency ids from `crm.audit_export_tenant` (the crm-014 section 2
  pattern). Runs once: the backfill is skipped when crm-065 is already registered, so a replay never shows a line again.
- **Copy only into empty fields, only for a whole tour.** `aa_days.fill_days` runs inside `add_aa_option` for
  `component_kind = 'trip_plan'`; tour day *k* goes to the version's day *d + k - 1* (*d* = the block's day). A title
  or place the agency wrote stays; the tour day's zone is copied only together with a copied place (the zone belongs to
  the place). Day 0 (Before you go) is never touched; no day is added when the tour runs past the version's last day.
- **Source of the days: the catalog cache** (`crm.aa_catalog_tour.body -> 'days'`, CIS `catalog.http.v1` `TourDay`),
  not Booking's product look-up (`api/app/booking/products.py` has components with `day_no` but no titles or
  places). No new call to CIS or Booking.
- **Portal fallback for older versions.** The rehearsal trip's version was accepted before the copy existed and a
  published version never changes (CRM35), so `person.itinerary` fills a dated day that has no title or place from the
  AA tour day of a live AA tour line shown to travellers on that date (`aa_days.portal_days`; the earliest line wins
  when two cover one date). Only shown lines feed it, so a hidden line never leaks its tour's days.
- **Text limits** are the column's CHECK (`proposal_day_text_chk`: 1..120 characters, no control character): titles
  cut with an ellipsis, place names joined by ", " while they fit (the first one cut when alone too long). These are
  storage limits, not thresholds, so they are not settings.

## Changed

- `api/app/booking/sync.py`: `BOOKED_SQL`, `CONFIRM_LINE_SQL` (same line count: the file is at its code-health
  baseline).
- `api/app/proposals/aa_days.py` (new), `api/app/proposals/builder.py` (one call in `add_aa_option`; two lines merged
  so the file stays at its baseline of 1,986 lines).
- `api/app/portal/person.py`: `itinerary` fallback; module text.
- `api/app/db.py`: readiness `crm-065`.
- Web: the AA drawer hint `builder.aa.daysHint` (EN + VI), shown under the quote hint.
- `CONTEXT.md`: builder AA items, portal per person; the crm-065 row ships with the migration PR.

## Tradeoffs

- No member toggle for an AA line's visibility: the crm-014 guard keeps AA lines read-only for members, and opening one
  column of it is a security-relevant trigger change outside this slice. With the stored default the toggle is a
  later, small change (guard + ledger PATCH + UI) if an agency asks to hide an AA line.
- Fallback vs. migrating accepted versions' days: rewriting a published or accepted version would break CRM35
  (published versions never change) and the price hash; the read-time fallback leaves the record as it was.
- No new days for a tour longer than the version: adding days changes the version's structure (blocks, rooms); the
  agency adds them in the builder.

## Should know

- Order: crm-065 applied on Dev (Infra Migration Apply, contract) before the code PR deploys (readiness crm-065).
- The code PR's tests need crm-065 in `api/migrations/` (CI runs on main after the migration PR merged; locally the
  file was copied in from the migration worktree, never committed here).
- A confirmed AA line now releases its traveller voucher once the trip is booked (before, an AA voucher always read
  `line_hidden` for travellers).
- Follow-up candidate (not built): an agency switch to hide a confirmed AA line from travellers (guard change).
