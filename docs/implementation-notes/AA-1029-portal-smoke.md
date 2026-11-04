# AA-1029 R4: the studio from price in the portal UI smoke (S268, lane portal-smoke)

Spec: `e2e/smoke-portal/s268-studio-price-vi.spec.ts`, first test. The second test (Vietnamese) is AA-1036, see
`AA-1036-portal-smoke.md`. Runs in UI Smoke (portal) after every Deploy Web Dev, signed in as the E2E studio tenant.

## Decisions

- **One live tour, its known row.** The Marketplace is searched for the Dev tour whose AA-Booking price.from.v1 row is
  235 USD per person for 2. The test reads the pool answer of that search (`GET /api/tenant/v1/tours/pool?search=`)
  before looking at the page, so Dev data drift fails at once with a message that names the tour and what the API
  answered: no such tour, `from_price` null, no `from_price` field (an older CIS), or another amount / currency / pax.
  Never a locator timeout for a data problem.
- **Same text on both surfaces.** The card's `marketplace-from-price` block and the drawer's `marketplace-drawer-price`
  line both contain "From $235 per person" and the caveat "Suggested price for 2 travellers. Your advisor confirms the
  final price." (English catalogues `marketplace.messages.ts` and `lib/studio/lib.messages.ts`).
- **Screenshots.** Card (element shot) and drawer (page shot) at 1440 and 390, light and dark:
  `results/smoke-portal/aa1029-from-price-{card,drawer}-{desktop,mobile}-{light,dark}.png` (kept by the workflow's
  artifact glob). One browser context per colour scheme (`colorScheme` on the context), so the theme's pre-paint script
  sees it from the first paint and the search does not have to be redone after a reload.

## Changed

- Nothing in the app. A new spec file only; `aa761-account-pages.spec.ts` untouched (another lane).

## Tradeoffs

- Pinning the exact price (235 USD, pax 2) makes the smoke red when the Booking row is edited on Dev. That is wanted:
  the message says the row changed, and the constants at the top of the spec are the one place to update.

## Should know

- Not run locally: the logged-in portal smoke needs the E2E studio user's sign-in (E2EPORTALEMAIL / PASSWORD / TOTP),
  which is not on disk here. Checked with `npx tsc --noEmit` and `playwright test --list` (e2e has no ESLint config;
  CI runs tsc only). The first real run is the UI Smoke (portal) after the next Deploy Web Dev.
