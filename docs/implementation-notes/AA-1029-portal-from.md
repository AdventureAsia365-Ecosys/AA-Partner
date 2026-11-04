# AA-1029 R4: the Content studio Marketplace "from" price, AA-Partner web part (S268, lane portal-from)

## Decisions

- **Data path**: the studio already lists the catalogue from CIS `GET /v1/tours/pool` through the tenant BFF; CIS now
  adds `from_price` to each row (read from AA-Booking's contract view `booking.v_from_prices`, price.from.v1, granted to
  `cis_app` by Booking bk-040). The web adds no call, no route and no Booking access (AA-Partner never owns or reads
  prices outside `booking.b2b.v1`; a studio-only member has no CRM agency).
- **Text**: "From US$235 per person" and the TripPlanner caveat "Suggested price for 2 travellers. Your advisor confirms
  the final price." (party size from the row). Whole units, rounded up (never below Booking's price); `US$` for USD,
  the ISO code for another currency.
- **Nothing without a price**: no row, an unusable row or an older CIS answer (no field) = the card has no price
  column and the drawer no price line. The source price text (`price_raw`) is no longer shown as a price anywhere in the
  catalog view (root ADR 0005 d7: reference only; the issue's fallback "no price shown"). `fmtPriceRaw` went with it.
- **Kit**: `FromPriceLine` (in `TourCard.tsx`, next to the card's other building blocks) uses kit tokens only; the
  card takes it through a new `priceBlock` slot so My Tours keeps its estimated price / "On request" column unchanged.

## Changed

- `TourDetail` is exported so the drawer body can be rendered in a vitest test (the kit Drawer itself portals).

## Tradeoffs

- On Dev most tours have no Booking price yet (only the E2E tour is priced), so most cards show no price where they
  used to show the source text: that is the rule (the text is not a selling price), not a regression to fix here.
- Per-tenant tiers and markup are AA-1030 (out of scope).

## Should know

- Order: CIS pool PR deployed (the field) and Booking bk-040 applied (the grant) for a live price; this PR works before
  both (no price shown).
- Tests: `tests/studio/marketplaceFromPrice.test.ts` (card + drawer markup), `tests/studio/tours.test.ts` (formatting).
