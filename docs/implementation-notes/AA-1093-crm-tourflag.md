# AA-1093 — part 1, CRM side: an AA tour Adventure Asia no longer serves

Lane l5-crm-tourflag of S279 (Claude Code). Source: the S279 plan (root
`docs/audits/2026-11-13-S279-session/aa1093-plan.md`, section 1 Part 1, section 2 "Tour retired", lane L1 tour part,
decisions D9 / D10) and the S278 system audit gap M29. The offboarding saga part of L1 (crm-064, the erasure feed)
is a later lane.

Migration: crm-063 (expand, one new table `crm.tour_retired_flag`), its own migration-only PR; the code PR follows once
it is applied on Dev (MIG-4 chain, readiness raised to crm-063 in the code PR). The DM-7.1 retention row is a root PR
merged before the Infra copy.

## Decisions

- **D9 as proposed: the hourly sync is the detector.** No CIS event feed. `app.catalog.store.upsert` / `set_state`
  return the old and new served state of the row (a CTE reads the row before the statement in the same snapshot) and
  queue the fan-out row in the same transaction as the state write. A retired tour is never re-checked by the sync
  (`STALE_SQL` skips it), so a transition missed at write time would never be seen again: the state write and the job
  row commit together or not at all.
- **Transitions.** `served|paused -> retired` = retired, `retired -> served` = reinstated; every other move (paused
  alone, a first-seen tour) queues nothing (root ADR 0015 decision 5).
- **Payload carries the uuid source id.** The payload guard accepts ids only as UUID or integer; catalog.http.v1 source
  ids are uuid strings (CIS `raw_tours.source_group_id`). A source id that is not a uuid (only test fixtures such as
  `T-100`) queues nothing and is counted `fanout_unkeyed` with a warning, never an enqueue error that would stop the
  sync. `changed_at` is `state_changed_at` in epoch microseconds (an integer: the ISO string's `+` is not a token).
- **Idempotency in two places.** The job key is `crm_tour_retired_fanout:<transition>:<source_id>:<changed_us>`; the
  flags are unique per (agency, tour, `raised_key`, item), and one item has at most one open flag, so a retried job
  raises nothing new and a task opens only for a trip that got a new flag (the open-task unique index too).
- **Stale rows do nothing.** The job re-reads the tour: a retirement whose tour is no longer retired at that
  `state_changed_at`, or a reinstatement whose tour is retired again, finishes `status: stale`; the later transition
  has its own row.
- **Items (D10 and the brief).** `open_option`: an AA option on the tour in a draft, published or sent builder version
  (an accepted version's work lives on its booking lines). Lines: `confirmed_line` = status confirmed or a Booking id
  (booked on request too); `held_line` = sub-status held or pending (a request in flight); `quoted_line` = quoted or no
  sub-status yet. Lines Booking already ended (sold out, declined, expired, released, not served, failed) and
  cancelled lines are left out. Only live trips (not lost / cancelled / completed, no cancellation), as the AA cancel
  flow.
- **Task.** One `aa_tour_discontinued` per trip that got a new flag: the trip owner while an active member, else the
  agency's first active owner (D10). Urgent, except normal when only booked lines are hit (information only).
- **Reinstated "note on the open task".** `crm.task` has no note column; the note is an internal trip note
  (`crm.note`, sealed with the agency's note DEK, in the agency's default language), written only for a trip whose
  last open flag this run cleared and that still has the open task. The task stays open for the agent to close.
  Without the key provider the note is skipped (`note_skipped_count`), the flags are still cleared.
- **Builder option badge reads the live state.** The builder already returns `no_longer_offered` per AA option
  (`aa_served_state`); the badge uses it (it is true exactly while the tour is retired, which is when the open flag
  exists). The trip page card, the card badge and the pipeline filter read the open flags.

## Changed (vs the brief)

- The flag column is `state`, not `status`: the crm-002 base template guard requires a status column to have
  `crm.status_transition` rows, an INSERT into an existing table that would make crm-063 contract (as crm-060 avoided
  with `resolution`). Values open / cleared as asked.
- No separate "erase_subject coverage" code: the table cascades from `crm.trip` (trip erase, agency erase, trash
  purge), listed in `CASCADE_ERASED` of `tests/integration/test_crm010_schema.py`; the crm-063 schema test proves both
  erases take the rows.

## Tradeoffs

- One job per transition walks every agency (`audit_export_agencies()`), one short transaction each, like the hourly
  ops release. At today's scale (tens of agencies on Dev) that is seconds; a transition is rare.
- An agency that fails makes the whole job retry (max 3); the agencies done before raise nothing new on the retry.
- `MAX_ITEMS` 2000 per agency and tour, logged when reached.

## Should know

- Order: root retention PR -> crm-063 migration PR -> Infra copy + Migration Apply (expand) -> code PR (readiness
  crm-063). The code reads `crm.tour_retired_flag` on every board and trip page load, so it must not deploy first; the
  readiness raise makes Deploy Dev wait.
- Local tests of the code PR need crm-063: copy the migration file into `api/migrations/` for the run only (the code
  branch does not carry it).
- The existing catalog cache test retires `T-100` (not a uuid): it now logs `crm_tour_retired_fanout_unkeyed` and
  queues nothing.
- Dev drill: see the lane's live steps (an E2E-only tour, sen-vang holds an option on it, retire in CIS, run the sync,
  check, restore at once).
