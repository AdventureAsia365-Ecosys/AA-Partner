# AA-1036 — studio UI smoke: the Activity sign-in row (S268 follow-up)

## Decisions
- The Activity smoke (`e2e/smoke-portal/aa761-account-pages.spec.ts`) now looks for the row the partner sign-in of
  `auth.setup.ts` leaves: CIS `POST /auth/tenant-session` (the studio session exchange) writes `tenant.login` with
  `client=portal`, which `api/services/tenant_account.py` shows as label "Signed in" with detail "Content studio".
  The old "Signed in with your API key" row came from the API-key sign-in removed by AA-1036 R3 slice 3, so the
  Deploy Web Dev studio smoke failed on every deploy since #214.
- The check reads real rows three ways, as before: the JSON answer (a `tenant.login` row on page 1 with that label
  and detail), the table (a `kit-datatable-row` holding both texts), the server-side type filter (the account
  filter keeps the sign-in), and the CSV (a line `<date>,Account,Signed in,<by>,Content studio,...`).

## Changed
- Only the smoke spec; no app code. The mocked pager case uses detail "Content studio" instead of "API".

## Tradeoffs
- The row match is scoped to table rows (`kit-datatable-row`) because "Signed in" alone also appears in the
  studio's "Signed in to AA Partner ..." identity line and on the Settings page.

## Should know
- The detail string is CIS copy (`detail_text`); if CIS renames it, this smoke fails on the next Deploy Web Dev.
