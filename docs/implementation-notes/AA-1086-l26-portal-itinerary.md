# AA-1086 lane l26: the traveller portal shows the trip's AA tour (S279)

Found by the S279 rehearsal take REH-10-portal: the traveller portal itinerary showed seven dated days with no titles,
places or services. Two causes: (a) `app/portal/person.py` lists only the lines the agency marked
`visible_to_travelers`, and an AA line is read-only for members (crm-014 guard 3a, CRM24), so the agency could never
show its AA tour; (b) the portal's days are the accepted version's days, and the builder skeleton creates them without
title or place. Tech lead decision (13/11/2026): (1) a confirmed AA line is visible to travellers by default; (2) when
the agency adds an AA tour to a proposal, the tour's day titles and places are copied into the proposal days (the
agency can still edit them).

Two PRs (MIG-4): `crm-065` (migration-only, contract: `crm.guard_booking_line` section a lets a member change only
`visible_to_travelers` on a confirmed AA line) first, then the code PR (readiness crm-065).

## Decisions

- **Default at confirm, stored on the row.** The Booking sync sets `visible_to_travelers = true` when it moves an AA
  line to confirmed: `CONFIRM_LINE_SQL` (feed `line.confirmed`, planning -> confirmed only) and `BOOKED_SQL` (Booking's
  booking answer, only when `$4 = 'confirmed'` and the old status is not confirmed). A stored default, not a read-time
  rule, so the voucher release (`travel_ops.vouchers.release_reason`) follows the same flag and the agency's later
  "hide" (next items) is kept.
- **No backfill (review fix pass).** The first pass backfilled every agency's confirmed AA lines in crm-065; review
  found that this released AA vouchers retroactively with no dry run and no agency choice. crm-065 is now the guard
  change only and writes no row; an AA line confirmed before the code stays hidden until the agency shows it (or the
  tech lead runs a reviewed DB Migrate run-script with a DRY RUN first, per agency).
- **The agency hides or shows a confirmed AA line (review fix pass).** crm-065 narrows crm-014 guard 3a: a crm_app
  session that is not a job: / platform: actor may UPDATE an AA line when it is confirmed and live before and after
  and the row differs only in `visible_to_travelers` (`to_jsonb(NEW) - [visible_to_travelers, updated_at]` equals the
  same of OLD: future columns are covered without editing the guard). `ledger.lines.update_line` accepts only
  `{visible_to_travelers}` on an AA line (else 403 `aa_line_read_only`), and only on a confirmed one (422
  `line_not_editable`): the default is set at confirm, so a choice made before it would be overwritten. The sync
  never shows a hidden confirmed line again (`BOOKED_SQL` only on the move to confirmed; `CONFIRM_LINE_SQL` only from
  planning). Web: every line that is not cancelled says "Shown to travellers" / "Hidden from travellers" with a
  Show / Hide switch (agency lines too: the API already took the flag but the web had no control), an AA line with
  the hint that it is shown by default and its voucher names the agency (EN + VI).
- **The travellers' voucher of an AA line names the agency (review fix pass).** `vouchers._supplier`: with
  `travellers_only` an AA line's supplier block is the agency's display name, no contact, no `aa_booking_ref`, no
  `aa_departure_id`; the agent's copy (and the agent-made PDF) still names Adventure Asia with the short AA reference.
  An agency that sells Adventure Asia as its own ground service never shows its supplier to its travellers.
- **Copy only into empty fields, only for the block's only whole tour.** `aa_days.after_add` runs inside
  `add_aa_option` for `component_kind = 'trip_plan'`; tour day *k* goes to the version's day *d + k - 1* (*d* = the
  block's day). A title or place the agency wrote stays; the tour day's zone is copied only together with a copied
  place (the zone belongs to the place). Day 0 (Before you go) is never touched; no day is added when the tour runs
  past the version's last day.
- **Alternative tours copy nothing (review fix pass).** A block can offer several whole tours (client_may_swap): the
  first pass copied the first tour added, and the client's pick of another tour never reached the proposal or the
  portal. Now a second, *different* whole tour clears the text every whole tour of the block copied (`cleared`: a
  title or place still equal to that tour day's; the zone stays, the column is required); the same tour twice (two
  prices) counts once. `after_delete` (in `delete_option`) clears a removed tour that the block no longer offers and
  copies the one whole tour left; `before_block_delete` (in `delete_block`) clears the block's tours' text. The
  portal then shows the confirmed pick's days through the fallback. Provenance is by value (no marker column): an
  agency title that happens to equal the tour day's title is cleared too.
- **Source of the days: the catalog cache** (`crm.aa_catalog_tour.body -> 'days'`, CIS `catalog.http.v1` `TourDay`),
  not Booking's product look-up (`api/app/booking/products.py` has components with `day_no` but no titles or
  places). No new call to CIS or Booking.
- **Portal fallback only for a day with neither a title nor a place (review fix pass).** The rehearsal trip's version
  was accepted before the copy existed and a published version never changes (CRM35), so `person.itinerary` fills a
  dated day that has *neither* a title *nor* a place with both from the AA tour day of a live AA tour line shown to
  travellers on that date (`aa_days.portal_days`; the earliest line wins when two cover one date). A day with either
  field is the agency's and shows as it is: a place the agency cleared stays empty, and an agency title never sits
  next to an AA place. Only shown lines feed it, so a hidden line never leaks its tour's days.
- **Text limits** are the column's CHECK (`proposal_day_text_chk`: 1..120 characters, no control character): titles
  cut with an ellipsis, place names joined by ", " while they fit (the first one cut when alone too long). These are
  storage limits, not thresholds, so they are not settings.

## Changed

- `api/app/booking/sync.py`: `BOOKED_SQL`, `CONFIRM_LINE_SQL` (same line count: the file is at its code-health
  baseline).
- `api/app/proposals/aa_days.py` (new), `api/app/proposals/builder.py` (one call each in `add_aa_option`,
  `delete_option`, `delete_block`; two `execute` calls compacted with `# fmt: skip` so the file stays at its baseline
  of 1,986 lines).
- `api/app/portal/person.py`: `itinerary` fallback; module text.
- `api/app/ledger/lines.py`: `_aa_visibility_only` (kept out of `update_line`, which is at its complexity baseline).
- `api/app/travel_ops/vouchers.py`: `_supplier` (the travellers' copy of an AA line).
- `api/app/db.py`: readiness `crm-065`.
- Web: the AA drawer hint `builder.aa.daysHint`; the Bookings tab's Shown / Hidden switch (`LinesPanel.shown`,
  `lib/ledger.ts` `canToggleShown`, keys `ledger.shown|hidden|show|hide|aaShownHint`, EN + VI).
- `CONTEXT.md`: ledger lines, builder AA items, portal per person; the crm-065 row ships with the migration PR.

## Tradeoffs

- Guard exception by row comparison (jsonb minus the two columns) vs. a SECURITY DEFINER setter: the comparison keeps
  one write path (the ledger PATCH, audited like any line edit) and needs no new grant; a new column of
  `crm.booking_line` stays read-only for members without touching the guard.
- Toggle only on a confirmed AA line: simpler than tracking "the agency chose" before confirm; the default at confirm
  would otherwise overwrite an earlier choice.
- Traveller voucher names the agency (option a of the review) rather than asking for an explicit OK to name Adventure
  Asia to travellers: the white-label case is the safe default; the agent still has the AA reference on their copy.
- Fallback vs. migrating accepted versions' days: rewriting a published or accepted version would break CRM35
  (published versions never change) and the price hash; the read-time fallback leaves the record as it was.
- No new days for a tour longer than the version: adding days changes the version's structure (blocks, rooms); the
  agency adds them in the builder.

## Should know

- Order: crm-065 applied on Dev (Infra Migration Apply, contract) before the code PR deploys (readiness crm-065).
- The code PR's tests need crm-065 in `api/migrations/` (CI runs on main after the migration PR merged; locally the
  file was copied in from the migration worktree, never committed here).
- A confirmed AA line now releases its traveller voucher once the trip is booked (before, an AA voucher always read
  `line_hidden` for travellers); that voucher names the agency.
- The S279 rehearsal trip's AA line was confirmed before this code: it stays hidden until the agency presses "Show to
  travellers" on its Bookings tab (no backfill). Its days already show in the portal only once the line is shown.
- No new threshold, limit or cap in this slice (the 120-character text limit is the column's CHECK, a storage limit).
