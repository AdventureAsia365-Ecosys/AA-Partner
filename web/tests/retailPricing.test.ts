// AA-1066 (crm-048): retail pricing on the web — the traveller-price editor and its body rule, the discount box by
// role and state, the rule form's body (each kind's own condition), the client page's "Adult × 2 · Child × 1" lines,
// the builder's refusal and check words, the BFF allow-list lines of the new routes, and the copy in both languages.
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { DiscountBox } from "@/components/proposals/DiscountBox";
import { OptionPricesEditor } from "@/components/proposals/OptionPricesEditor";
import { builderErrorKey, checkKey, type BuilderOption } from "@/lib/builder";
import { matchBff } from "@/lib/bff";
import { MESSAGES } from "@/lib/i18n";
import { retailPricingEn } from "@/lib/i18nRetailPricing";
import {
  discountInput,
  draftRows,
  EMPTY_RULE,
  priceLines,
  pricesBody,
  pricingErrorKey,
  ruleBody,
  ruleSummary,
  signedPct,
  type DiscountState,
} from "@/lib/retailPricing";

const P = "11111111-1111-4111-8111-111111111111";
const O = "22222222-2222-4222-8222-222222222222";
const tr = (key: string) => MESSAGES.en[key as keyof typeof MESSAGES.en] ?? key;

function option(over: Partial<BuilderOption> = {}): BuilderOption {
  return {
    option_id: O, source: "agency", title: "Riverside lodge", tier: null, agency_supplier_id: null, sell_amount: "1130.00",
    sell_delta: null, net_amount: "560.00", net_delta: null, pay_on_arrival: false, choose_later_allowed: false,
    availability: "agency", availability_checked_at: null, is_default: true, picked: false,
    prices: [{ pax_type: "adult", unit_sell: "400.00", qty: 2, unit_net: "200.00" }],
    suggested_prices: [{ pax_type: "adult", unit_sell: "376.67", qty: 2 }, { pax_type: "child", unit_sell: "376.67", qty: 1 }],
    ...over,
  };
}

function state(over: Partial<DiscountState> = {}): DiscountState {
  return {
    discount_pct: "15.00", limit_pct: "10.00", above_limit: true, below_cost: false, needs_approval: true,
    approved_pct: null, pending: null, history: [], ...over,
  };
}

const write = async () => true;

describe("traveller prices", () => {
  it("words who a price is for, leaving types with nobody out", () => {
    expect(priceLines([{ pax_type: "adult", qty: 2 }, { pax_type: "child", qty: 1 }, { pax_type: "infant", qty: 0 },
      { pax_type: "single_supp", qty: 1 }], tr)).toBe("Adult × 2 · Child × 1 · Single room × 1");
    expect(priceLines([], tr)).toBe("");
  });

  it("starts the editor from the saved rows, else the suggested grid", () => {
    expect(draftRows(option().prices ?? [], option().suggested_prices ?? [])).toEqual([
      { pax_type: "adult", qty: "2", unit_sell: "400.00", unit_net: "200.00" },
    ]);
    expect(draftRows([], option().suggested_prices ?? []).map((r) => r.pax_type)).toEqual(["adult", "child"]);
  });

  it("builds the PUT body or refuses a bad row", () => {
    expect(pricesBody([{ pax_type: "adult", qty: "2", unit_sell: "1,000.5", unit_net: "" }], true)).toEqual([
      { pax_type: "adult", qty: 2, unit_sell: "1000.5" },
    ]);
    expect(pricesBody([{ pax_type: "adult", qty: "2", unit_sell: "10", unit_net: "4" }], false)).toEqual([
      { pax_type: "adult", qty: 2, unit_sell: "10" },
    ]);  // no net for an AA item
    for (const bad of [
      [],
      [{ pax_type: "adult", qty: "100", unit_sell: "1", unit_net: "" }],
      [{ pax_type: "adult", qty: "x", unit_sell: "1", unit_net: "" }],
      [{ pax_type: "adult", qty: "1", unit_sell: "1.234", unit_net: "" }],
      [{ pax_type: "adult", qty: "1", unit_sell: "1", unit_net: "" }, { pax_type: "adult", qty: "1", unit_sell: "1", unit_net: "" }],
      [{ pax_type: "adult", qty: "1", unit_sell: "1", unit_net: "2" }, { pax_type: "child", qty: "1", unit_sell: "1", unit_net: "" }],
    ] as const) {
      expect(pricesBody([...bad], true)).toBeNull();
    }
  });

  it("renders the editor with a net column for an agency item only", () => {
    const html = renderToStaticMarkup(createElement(OptionPricesEditor, { option: option(), busy: false, tr, onSave: write, onClose: () => {} }));
    expect(html).toContain('data-testid="option-prices"');
    expect(html).toContain("Net each");
    expect(html).toContain("Price as one amount");  // rows exist: they can be removed
    const aa = renderToStaticMarkup(createElement(OptionPricesEditor, { option: option({ source: "aa", prices: [] }), busy: false, tr, onSave: write, onClose: () => {} }));
    expect(aa).not.toContain("Net each");
    expect(aa).not.toContain("Price as one amount");
    expect(aa).toContain("Fill from the suggestion");
  });
});

describe("the discount box", () => {
  const props = { proposalId: P, draft: true, canMoney: true, busy: false, locale: "en-GB" as const, tr, write, onMessage: () => {} };

  it("lets an agent ask the owner when the discount needs approval", () => {
    const html = renderToStaticMarkup(createElement(DiscountBox, { ...props, state: state(), isOwner: false }));
    expect(html).toContain('data-needs-approval="true"');
    expect(html).toContain("Ask the owner to approve");
    expect(html).toContain("The owner approves discounts above 10%.");
  });

  it("lets the owner approve a pending request", () => {
    const pending = {
      discount_approval_id: "33333333-3333-4333-8333-333333333333", proposal_id: P, discount_pct: "15.00", limit_pct: "10.00",
      status: "pending" as const, requested_by: "member:x", requested_at: "2026-11-10T09:00:00Z", decided_by: null, decided_at: null,
    };
    const html = renderToStaticMarkup(createElement(DiscountBox, { ...props, state: state({ pending }), isOwner: true }));
    expect(html).toContain('data-testid="builder-discount-approve"');
    expect(html).toContain("Waiting for the owner: 15% asked on");
    expect(html).toContain("As the owner, publishing records your approval");
    expect(html).not.toContain("Ask the owner to approve");
  });

  it("warns of a sale below cost and shows an approval that covers the discount", () => {
    const html = renderToStaticMarkup(createElement(DiscountBox, { ...props, state: state({ below_cost: true, needs_approval: false, approved_pct: "20.00" }), isOwner: false }));
    expect(html).toContain("a sale at a loss");
    expect(html).toContain("approved discounts up to 20%");
    expect(html).not.toContain("Ask the owner to approve");
  });

  it("reads a typed discount", () => {
    expect(discountInput("")).toBeNull();
    expect(discountInput("12,5")).toBe("12.5");
    expect(discountInput("100")).toBe("100");
    expect(discountInput("101")).toBeUndefined();
    expect(discountInput("1.234")).toBeUndefined();
    expect(discountInput("-1")).toBeUndefined();
  });
});

describe("price rules", () => {
  it("sends only the kind's own condition", () => {
    expect(ruleBody({ ...EMPTY_RULE, name: " Peak ", kind: "season", date_from: "2027-01-01", date_to: "2027-01-31", adjust_pct: "20", min_pax: "4" })).toEqual({
      name: "Peak", kind: "season", product_scope: "all", adjust_pct: "20", priority: 100, stacking: "add", date_from: "2027-01-01", date_to: "2027-01-31",
    });
    expect(ruleBody({ ...EMPTY_RULE, name: "Group", kind: "group_size", min_pax: "6", adjust_pct: "-5" })?.min_pax).toBe(6);
    expect(ruleBody({ ...EMPTY_RULE, name: "Late", kind: "last_minute", days_before: "0", adjust_pct: "-30", aa_tour_source_id: "T-1" })).toMatchObject({ days_before: 0, aa_tour_source_id: "T-1" });
    for (const bad of [
      { ...EMPTY_RULE, adjust_pct: "10" },  // no name
      { ...EMPTY_RULE, name: "X", adjust_pct: "0", kind: "group_size" as const, min_pax: "4" },
      { ...EMPTY_RULE, name: "X", adjust_pct: "301", kind: "group_size" as const, min_pax: "4" },
      { ...EMPTY_RULE, name: "X", adjust_pct: "5", kind: "group_size" as const, min_pax: "1" },
      { ...EMPTY_RULE, name: "X", adjust_pct: "5", kind: "early_booking" as const, days_before: "" },
      { ...EMPTY_RULE, name: "X", adjust_pct: "5", kind: "season" as const, date_from: "2027-02-01", date_to: "2027-01-01" },
      { ...EMPTY_RULE, name: "X", adjust_pct: "5", kind: "group_size" as const, min_pax: "4", priority: "0" },
    ]) {
      expect(ruleBody(bad)).toBeNull();
    }
  });

  it("words a rule's condition and change", () => {
    const date = (iso: string) => iso;
    expect(ruleSummary({ kind: "season", date_from: "2027-01-01", date_to: "2027-01-31", min_pax: null, days_before: null }, tr, date)).toBe("2027-01-01 to 2027-01-31");
    expect(ruleSummary({ kind: "group_size", date_from: null, date_to: null, min_pax: 6, days_before: null }, tr, date)).toBe("6 travellers or more");
    expect(ruleSummary({ kind: "last_minute", date_from: null, date_to: null, min_pax: null, days_before: 14 }, tr, date)).toBe("14 days or fewer before");
    expect([signedPct("20"), signedPct("-10.5"), signedPct("0")]).toEqual(["+20%", "−10.5%", "0%"]);
  });
});

describe("words, routes and copy", () => {
  it("words the API's new refusal codes and checks", () => {
    for (const code of ["amount_from_prices", "no_suggestion", "unit_net_partial", "prices_nothing_charged", "discount_needs_approval",
      "discount_approval_not_needed", "discount_approval_not_pending", "invalid_discount_pct"]) {
      expect(builderErrorKey(code)).not.toBe("builder.err.generic");
      expect(pricingErrorKey(code)).toBe(builderErrorKey(code));
    }
    expect(pricingErrorKey("other")).toBeNull();
    for (const code of ["discount_needs_approval", "discount_owner_approves", "prices_party_changed"]) {
      expect(checkKey(code)).not.toBe("builder.err.generic");
    }
  });

  it("lets the new routes through the BFF and nothing else", () => {
    expect(matchBff("PUT", `proposals/${P}/options/${O}/prices`)).not.toBeNull();
    expect(matchBff("POST", `proposals/${P}/discount-approval`)).not.toBeNull();
    expect(matchBff("POST", `proposals/${P}/discount-approval/${O}`)).not.toBeNull();
    expect(matchBff("GET", "settings/pricing")).not.toBeNull();
    expect(matchBff("PUT", "settings/pricing")).not.toBeNull();
    expect(matchBff("POST", "settings/price-rules")).not.toBeNull();
    expect(matchBff("PATCH", `settings/price-rules/${O}`)).not.toBeNull();
    expect(matchBff("DELETE", `settings/price-rules/${O}`)).not.toBeNull();
    expect(matchBff("DELETE", `proposals/${P}/discount-approval/${O}`)).toBeNull();
    expect(matchBff("DELETE", "settings/pricing")).toBeNull();
    expect(matchBff("PATCH", "settings/price-rules/not-a-uuid")).toBeNull();
  });

  it("has every new message in English and Vietnamese, the Vietnamese its own", () => {
    for (const key of Object.keys(retailPricingEn) as (keyof typeof retailPricingEn)[]) {
      expect(MESSAGES.en[key], key).toBeTruthy();
      expect(MESSAGES.vi[key], key).toBeTruthy();
    }
    for (const key of ["pricing.perTraveller", "discount.request", "pricingSettings.title", "task.discount_approval"] as const) {
      expect(MESSAGES.vi[key]).not.toBe(MESSAGES.en[key]);
    }
  });
});
