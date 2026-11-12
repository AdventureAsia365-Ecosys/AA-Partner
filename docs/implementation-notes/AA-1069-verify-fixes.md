# AA-1069 — fixes from the S278 live check

The S278 live check of the agency service catalogue and supplier booking requests passed 12 of 13 items. This note
covers the failed item and one wording finding from the clean-up.

## Decisions

- `GET /crm/v1/bookings?status=` takes every line status: `planning`, `requested`, `confirmed`, `cancelled`. The
  router's `Literal` had not been updated when crm-053 added `requested`. The service check (`lines.STATUSES`) and the
  SQL already allowed it, so the API answered 422 before the service ran. The web Bookings page already offered
  "Requested" (its `STATUSES` list), and the BFF forwards the query string as it is, so neither changed.
- A change that only a planning line accepts (moving the line to another supplier, or the trash check) now words its
  refusal from the line's actual status (`lines._not_planning`):
  - cancelled: "this line is cancelled; …";
  - requested: "this line is requested from its supplier; …; move it back to planning first";
  - confirmed: "this line is confirmed; …; cancel it instead".
  The code stays `line_not_editable`, so the web text (keyed on the code) is unchanged.

## Changed

- `api/app/routers/ledger.py`: the status `Literal` lists `requested`.
- `api/app/ledger/lines.py`: `_not_planning()` is used by `update_line` (the supplier move) and `trash_check`.

## Tradeoffs

- The router keeps its own `Literal` (OpenAPI shows the allowed values) and does not read `lines.STATUSES`. A unit test
  checks that the two lists match.

## Should know

- Other filters over line status were checked: the trip lines list, the totals CTE (a requested line is counted as
  pipeline), the cancellation supplier list and the waiting_on hook already know `requested`. The status filters that
  only look at AA lines need no change, because only agency lines are ever requested.
- Tests: `api/tests/unit/test_ledger_routes.py` (each status through the route, 422 for an unknown status),
  `api/tests/unit/test_ledger.py` (the ledger passes `requested` to SQL; refusal wording per status),
  `api/tests/integration/test_aa1069_catalogue_requests_api.py` (the ledger filtered to requested lines; a requested
  line keeps its supplier), `web/tests/crmVerifyFixes.test.ts` (the page asks `status=requested` and lists the line).
