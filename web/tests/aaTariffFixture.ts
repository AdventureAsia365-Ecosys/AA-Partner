// web/tests/aaTariffFixture.ts — AA-1030: the layout fixture of the AA prices page. Invented tours only (never catalog
// text): a short row, a long title and season that must wrap, an open pax band without an RRP, a single-person band and
// a VND-priced view (no decimals). Used by tests/aaTariff.test.ts.
import type { TariffItem, TariffView } from "@/lib/aaTariff";

export const TOUR = "e1000000-0000-4000-8000-0000000c1030";
export const LONG_TITLE =
  "Invented Highland Ridge and Lantern Valley Slow Journey with Village Homestays, Riverside Picnics and Market Mornings";

function money(amount: string, currency: string) {
  return { amount, currency };
}

export function tariffItem(over: Partial<TariffItem> = {}, currency = "USD"): TariffItem {
  return {
    source_id: TOUR,
    title: "Invented River Ramble",
    country: "Invented Land",
    duration_days: 3,
    season: "Dry",
    valid_from: "2031-01-05",
    valid_to: "2031-03-31",
    pax_min: 2,
    pax_max: 4,
    priced_for_pax: 2,
    b2b_price: money("776.00", currency),
    b2b_price_per_person: money("388.00", currency),
    agency_discount_pct: "0.0300",
    rrp_per_person: money("500.00", "USD"),
    ...over,
  };
}

export function tariffView(over: Partial<TariffView> = {}, currency = "USD"): TariffView {
  return {
    currency,
    priced_on: "2031-01-05",
    tier_code: "silver",
    basis: "per_person_band_min_twin_share",
    rate_date: "2031-01-05",
    truncated: false,
    items: [
      tariffItem({}, currency),
      tariffItem({ pax_min: 5, pax_max: null, priced_for_pax: 5, b2b_price_per_person: money("310.00", currency), rrp_per_person: null }, currency),
      tariffItem(
        {
          source_id: "e1000000-0000-4000-8000-0000000c1031",
          title: LONG_TITLE,
          season: "Shoulder season between the long monsoon rains and the cool clear weeks",
          pax_min: 1,
          pax_max: 1,
          priced_for_pax: 1,
          agency_discount_pct: "0.0625",
        },
        currency,
      ),
    ],
    ...over,
  };
}
