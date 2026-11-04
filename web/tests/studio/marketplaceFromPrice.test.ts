// AA-1029 R4: the Content studio Marketplace shows AA-Booking's "from" price (contract price.from.v1, CIS pool
// `from_price`) on the catalog card and in the detail drawer, "From US$X per person" with the TripPlanner caveat, and
// nothing without one: never the source price text (`price_raw`). Invented tour names and places only.
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { FromPrice } from "@/lib/studio/tours";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: () => {}, refresh: () => {} }), usePathname: () => "/portal/marketplace" }));
const { FromPriceLine, TourCard } = await import("@/app/(studio)/portal/_components/marketplace/TourCard");
const { TourDetail } = await import("@/app/(studio)/portal/_components/marketplace/TourDrawer");
type PoolTour = import("@/app/(studio)/portal/_components/marketplace/marketplaceApi").PoolTour;

const PRICE: FromPrice = { amount: "235.00", currency: "USD", basis: "per_person", pax: 2, season: "Low", valid_to: "9999-12-31" };
const NOTE = "Suggested price for 2 travellers. Your advisor confirms the final price.";

function tour(over: Partial<PoolTour> = {}): PoolTour {
  return {
    id: "pt-1", tour_id: "t-1", aa_name: "Lantern Valley Ridge Walk", aa_subtitle: "Three days above Mistwater",
    aa_summary: null, aa_highlights: null, aa_itineraries: null, seo_title: null, seo_meta: null,
    seo_keywords_used: null, published_at: null, country: "Vietnam", duration: "3 days",
    price_raw: "From $999 per person (source text)", already_rewritten: false, activities: [], intensity: null,
    cover_image_url: null, cover_image_large_url: null, from_price: PRICE,
    ...over,
  } as PoolTour;
}

const text = (html: string) => html.replace(/<[^>]+>/g, "").replace(/&#x27;/g, "'").replace(/\s+/g, " ");

function card(priceBlock: React.ReactNode) {
  return renderToStaticMarkup(createElement(TourCard, {
    name: "Lantern Valley Ridge Walk", status: null, onOpen: () => {}, priceBlock,
  }));
}

describe("from price line", () => {
  it("reads From US$X per person with the caveat", () => {
    const html = renderToStaticMarkup(createElement(FromPriceLine, { price: PRICE }));
    expect(text(html)).toContain("From US$235 per person");
    expect(text(html)).toContain(NOTE);
  });

  it("renders nothing without a usable price", () => {
    for (const price of [null, undefined, { ...PRICE, amount: "0" }, { ...PRICE, currency: "" }]) {
      expect(renderToStaticMarkup(createElement(FromPriceLine, { price }))).toBe("");
    }
  });
});

describe("catalog card", () => {
  it("shows the from price in place of the price column", () => {
    const html = card(createElement(FromPriceLine, { price: PRICE, compact: true }));
    expect(text(html)).toContain("From US$235 per person");
    expect(text(html)).toContain(NOTE);
    expect(text(html)).not.toContain("On request");
  });

  it("shows no price column at all when the tour has no from price", () => {
    const html = card(null);
    expect(text(html)).not.toMatch(/Price|On request|US\$/);
  });

  it("keeps the default price column for other callers (My Tours)", () => {
    const html = renderToStaticMarkup(createElement(TourCard, { name: "Lantern Valley", status: null, onOpen: () => {}, price: null }));
    expect(text(html)).toContain("On request");
  });
});

describe("detail drawer", () => {
  it("shows the from price and never the source price text", () => {
    const html = renderToStaticMarkup(createElement(TourDetail, { tour: tour(), status: null }));
    expect(html).toContain('data-testid="marketplace-drawer-price"');
    expect(text(html)).toContain("From US$235 per person");
    expect(text(html)).toContain(NOTE);
    expect(text(html)).not.toContain("$999");
  });

  it("shows no price without a from price (null, or an older CIS answer without the field)", () => {
    for (const over of [{ from_price: null }, { from_price: undefined }]) {
      const html = renderToStaticMarkup(createElement(TourDetail, { tour: tour(over), status: null }));
      expect(html).not.toContain("marketplace-drawer-price");
      expect(text(html)).not.toMatch(/US\$|\$999|per person/);
    }
  });
});
