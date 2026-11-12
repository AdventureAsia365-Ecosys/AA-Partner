# AA-1070 — departure roster at 390 px (S278 live check)

## Decisions

- The roster's traveller table is a `ReportTable`. In that table, every column after the first was a number cell:
  right-aligned and `white-space: nowrap`. On a phone, the kit DataTable shows each row as a card. The "Trip" value, a
  long tour title, stayed on one line, so the card and the page grew to 458 px at a 390 px viewport.
- `ReportTable` takes `textColumns` (the indexes of the later columns that hold words). Those columns are left-aligned
  and wrap (`overflow-wrap: anywhere`), the way the first column does. The roster names columns 1–4 (trip, details,
  passport, room). No other `ReportTable` user changes: by default every later column is still a number.
- The fix is in the app's own component (`web/components/reports/ReportTable.tsx`). Nothing under `_kit/core` changed.

## Changed

- `web/components/reports/ReportTable.tsx`: new optional `textColumns` prop.
- `web/components/travelOps/DepartureRoster.tsx`: passes `ROSTER_TEXT_COLUMNS`.

## Tradeoffs

- The other option was to wrap every cell in `ReportTable`. A money value would then break across lines in the report
  tables, so the change applies only to columns that are named as text.

## Should know

- Layout check (S278, local harness: the real `DepartureRoster` bundled with a stubbed BFF answer, a long tour title,
  Chromium with scrollbars on): the document is 390 px wide at a 390 px viewport and 1440 px at 1440, in light and
  dark. The same harness without the fix measured 542 px at 390.
- `/departures/[ref]` is excluded from the visual run (it has no stable ref). After deploy, check it live on Dev at
  390 px.
- Test: `web/tests/crmVerifyFixes.test.ts`, in phone card mode: a text column wraps and a number column stays on one
  line.
