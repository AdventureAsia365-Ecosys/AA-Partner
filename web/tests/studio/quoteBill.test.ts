// AA-1129 (S283 UI audit rows 18 and 24, S1 "Studio money"): the Content studio Billing tab and the Dashboard show the
// monthly bill line by line (GET /v1/billing `quote`), so the amount due adds up: products, extra seats, the founding /
// combo discount, a free pilot, the total and the yearly price. Invented products and amounts only.
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { quoteView, type BillingMoneyRecord } from "@/lib/studio/billingMoney";
import { QUOTE_MESSAGES } from "@/app/(studio)/portal/_components/account.messages";

const { QuoteCard, QuoteLines } = await import("@/app/(studio)/portal/_components/QuoteCard");

const plain = (s: string) => s.replace(/[\u00a0\u202f]/g, " ");

const usd: BillingMoneyRecord = {
  billing_currency: "USD",
  plan_tier: "growth",
  price_usd_monthly: 349,
  overage_usd: 0,
  quote: {
    currency: "USD",
    lines: [
      { kind: "subscription", product: "content", plan_key: "content.growth", name: "Content studio Growth", quantity: 1, unit_price: "349", amount: "349", discount: "34.9" },
      { kind: "subscription", product: "crm", plan_key: "crm.team", name: "Partner CRM Team", quantity: 3, extra_seats: 1, extra_seat_price: "25", unit_price: "29", amount: "112", discount: "11.2" },
      { kind: "subscription", product: "planner", plan_key: "planner.basic", name: "Trip planner", quantity: 1, amount: "0", trialing: true },
    ],
    subtotal: "461",
    discount_kind: "combo",
    discount_pct: "10",
    discount: "46.1",
    pilot_free: false,
    monthly_total: "414.9",
    annual_paid_months: 10,
    annual_total: "4149",
  },
};

describe("quoteView", () => {
  it("turns the quote into rows with their notes, the discount, the total and the yearly price", () => {
    const v = quoteView(usd)!;
    expect(v.currency).toBe("USD");
    expect(v.rows.map((r) => [r.name, r.amount])).toEqual([
      ["Content studio Growth", "$349"],
      ["Partner CRM Team", "$112"],
      ["Trip planner", "$0"],
    ]);
    expect(v.rows[0].notes).toEqual([]);
    expect(v.rows[1].notes).toEqual([
      { kind: "quantity", n: 3 },
      { kind: "extraSeats", n: 1, price: "$25" },
    ]);
    expect(v.rows[2].notes).toEqual([{ kind: "trial" }]);
    expect(v.subtotal).toBe("$461");
    expect(v.discount).toEqual({ kind: "combo", pct: 10, amount: "$46.10" });
    expect(v.total).toBe("$414.90");
    expect(v.annual).toEqual({ months: 10, total: "$4,149" });
    expect(v.pilotFree).toBe(false);
  });

  it("a VND bill reads in VND; a free pilot has no yearly price; no discount is no discount row", () => {
    const v = quoteView({
      billing_currency: "VND",
      quote: {
        currency: "VND",
        lines: [{ name: "Content studio Starter", amount: "1490000", agreed: true, billing_interval: "year" }],
        discount: "0",
        pilot_free: true,
        monthly_total: "0",
        annual_paid_months: 12,
        annual_total: "0",
      },
    })!;
    expect(plain(v.rows[0].amount)).toBe("1.490.000 ₫");
    expect(v.rows[0].notes).toEqual([{ kind: "agreed" }, { kind: "yearly" }]);
    expect(plain(v.subtotal)).toBe("1.490.000 ₫");
    expect(v.discount).toBeNull();
    expect(v.pilotFree).toBe(true);
    expect(v.annual).toBeNull();
  });

  it("no quote lines (an older CIS) or no record: null, the pages keep the plan fee alone", () => {
    expect(quoteView(null)).toBeNull();
    expect(quoteView({ quote: { currency: "USD", monthly_total: "349" } })).toBeNull();
    expect(quoteView({ quote: { currency: "USD", lines: [] } })).toBeNull();
  });
});

describe("the bill on the page", () => {
  it("Billing lists every product, the seats, the combo discount, the total and the yearly price", () => {
    const html = plain(renderToStaticMarkup(createElement(QuoteCard, { quote: quoteView(usd) })));
    expect(html).toContain('data-testid="billing-quote"');
    expect(html).toContain("Your monthly bill");
    expect(html).toContain("Partner CRM Team");
    expect(html).toContain("× 3 · 1 extra seat at $25");
    expect(html).toContain("free trial");
    expect(html).toContain("Subtotal");
    expect(html).toContain("Combo discount (10%)");
    expect(html).toContain("− $46.10");
    expect(html).toContain("Monthly total");
    expect(html).toContain("$414.90");
    expect(html).toContain("Paid yearly: $4,149 (10 months)");
    expect(html.match(/data-testid="quote-line"/g)).toHaveLength(3);
  });

  it("the Dashboard's short form has no subtotal or yearly line; no quote renders nothing", () => {
    const html = plain(renderToStaticMarkup(createElement(QuoteLines, { quote: quoteView(usd)!, compact: true })));
    expect(html).not.toContain("Subtotal");
    expect(html).not.toContain("Paid yearly");
    expect(html).toContain("Combo discount (10%)");
    expect(renderToStaticMarkup(createElement(QuoteCard, { quote: null }))).toBe("");
  });

  it("every bill message has its Vietnamese", () => {
    expect(Object.keys(QUOTE_MESSAGES.vi).sort()).toEqual(Object.keys(QUOTE_MESSAGES.en).sort());
  });
});
