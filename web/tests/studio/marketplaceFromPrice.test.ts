// AA-1029 R4: the Content studio Marketplace shows AA-Booking's "from" price (contract price.from.v1, CIS pool
// `from_price`) on the catalog card and in the detail drawer, "From $X per person" with the suggested retail (RRP)
// caveat (AA-1188, D10), and "Price on request" without one (AA-1188): never the source price text. Invented tour
// names and places only.
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { FromPrice } from "@/lib/studio/tours";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: () => {}, refresh: () => {} }), usePathname: () => "/portal/marketplace" }));
const { FromPriceLine, TourCard } = await import("@/app/(studio)/portal/_components/marketplace/TourCard");
const { TourDetail } = await import("@/app/(studio)/portal/_components/marketplace/TourDrawer");
type PoolTour = import("@/app/(studio)/portal/_components/marketplace/marketplaceApi").PoolTour;

const PRICE: FromPrice = { amount: "235.00", currency: "USD", basis: "per_person", pax: 2, season: "Low", valid_to: "9999-12-31" };
const NOTE = "Suggested retail (RRP) for 2 travellers, not your agency's price.";

function tour(over: Partial<PoolTour> = {}): PoolTour {
  return {
    id: "pt-1", tour_id: "t-1", aa_name: "Lantern Valley Ridge Walk", aa_subtitle: "Three days above Mistwater",
    aa_summary: null, aa_highlights: null, aa_itineraries: null, seo_title: null, seo_meta: null,
    seo_keywords_used: null, published_at: null, country: "Vietnam", duration: "3 days",
    already_rewritten: false, activities: [], intensity: null,
    cover_image_url: null, cover_image_large_url: null, from_price: PRICE,
    ...over,
  } as PoolTour;
}

// Visible text of static markup: the text between tags (React escapes a "<" in text as &lt;, so every "<" opens a tag).
// It splits on the tags instead of stripping them with replace(): it reads text for an assertion, it sanitizes nothing.
const text = (html: string) =>
  html.split(/<[^>]*>/).join("").replace(/&#x27;/g, "'").replace(/\s+/g, " ");

function card(priceBlock: React.ReactNode) {
  return renderToStaticMarkup(createElement(TourCard, {
    name: "Lantern Valley Ridge Walk", status: null, onOpen: () => {}, priceBlock,
  }));
}

describe("from price line", () => {
  it("reads From $X per person with the caveat", () => {
    const html = renderToStaticMarkup(createElement(FromPriceLine, { price: PRICE }));
    expect(text(html)).toContain("From $235 per person");
    expect(text(html)).toContain(NOTE);
  });

  it("reads Price on request without a usable price (AA-1188), never an amount", () => {
    for (const price of [null, undefined, { ...PRICE, amount: "0" }, { ...PRICE, currency: "" }]) {
      const html = renderToStaticMarkup(createElement(FromPriceLine, { price }));
      expect(text(html).trim()).toBe("Price on request");
      expect(html).toContain('data-price="on-request"');
    }
  });
});

describe("catalog card", () => {
  it("shows the from price in place of the price column", () => {
    const html = card(createElement(FromPriceLine, { price: PRICE, compact: true }));
    expect(text(html)).toContain("From $235 per person");
    expect(text(html)).toContain(NOTE);
    expect(text(html)).not.toContain("On request");
  });

  it("shows Price on request when the tour has no from price (AA-1188)", () => {
    const html = card(createElement(FromPriceLine, { price: null, compact: true }));
    expect(text(html)).toContain("Price on request");
    expect(text(html)).not.toMatch(/US\$|per person/);
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
    expect(text(html)).toContain("From $235 per person");
    expect(text(html)).toContain(NOTE);
    expect(text(html)).not.toContain("$999");
  });

  it("shows Price on request without a from price (null, or an older CIS answer without the field)", () => {
    for (const over of [{ from_price: null }, { from_price: undefined }]) {
      const html = renderToStaticMarkup(createElement(TourDetail, { tour: tour(over), status: null }));
      expect(html).toContain("marketplace-drawer-price");
      expect(text(html)).toContain("Price on request");
      expect(text(html)).not.toMatch(/US\$|\$999|per person/);
    }
  });
});
