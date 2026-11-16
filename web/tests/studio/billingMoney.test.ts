// AA-1116 (option B, contract tenant.entitlements.v1 1.9): the studio Billing tab and Dashboard show the amount and the
// currency GET /v1/billing returns, never a literal "$". Invented figures only.
import { describe, expect, it } from "vitest";
import { billingCurrency, currentPlanPrice, dueSoFar, fmtMoney, monthMoney, planPrice } from "@/lib/studio/billingMoney";

const plain = (s: string) => s.replace(/ | /g, " ");

describe("fmtMoney", () => {
  it("formats by currency, not by UI language", () => {
    expect(plain(fmtMoney("3790000", "VND"))).toBe("3.790.000 ₫");
    expect(fmtMoney(149, "USD")).toBe("$149");
    expect(fmtMoney("19.5", "usd")).toBe("$19.50");
    expect(fmtMoney("1234.5", "EUR")).toBe("1,234.50 EUR");
    expect(fmtMoney(null, "VND")).toBe("—");
    expect(fmtMoney("x", "USD")).toBe("—");
  });
});

describe("the /v1/billing record", () => {
  const vnd = {
    billing_currency: "VND",
    plan_tier: "growth",
    price_usd_monthly: 149,
    overage_usd: 0,
    plans: [
      { plan_name: "starter", price_usd_monthly: 59, currency: "VND", price_monthly: "1490000" },
      { plan_name: "growth", price_usd_monthly: 149, currency: "VND", price_monthly: "3790000" },
      { plan_name: "enterprise", price_usd_monthly: null, currency: "VND", price_monthly: null },
    ],
    quote: { currency: "VND", monthly_total: "4270000", pilot_free: false },
  };

  it("reads the plan price and the amount due in the agency currency", () => {
    expect(billingCurrency(vnd)).toBe("VND");
    expect(currentPlanPrice(vnd)).toEqual({ amount: 3790000, currency: "VND" });
    expect(planPrice(vnd.plans[2])).toEqual({ amount: null, currency: "VND" });
    expect(dueSoFar(vnd)).toEqual({ amount: 4270000, currency: "VND", overageUsd: 0, overageApart: false });
  });

  it("keeps a USD overage apart from a VND total, and adds it to a USD total", () => {
    expect(dueSoFar({ ...vnd, overage_usd: 12.5 })).toEqual({ amount: 4270000, currency: "VND", overageUsd: 12.5, overageApart: true });
    const usd = { ...vnd, billing_currency: "USD", quote: { currency: "USD", monthly_total: "168.00" }, overage_usd: 2 };
    expect(dueSoFar(usd)).toEqual({ amount: 170, currency: "USD", overageUsd: 2, overageApart: false });
  });

  it("a free pilot owes nothing", () => {
    expect(dueSoFar({ ...vnd, quote: { currency: "VND", monthly_total: "0", pilot_free: true } }).amount).toBe(0);
  });

  it("an older CIS without the 438 fields falls back to the USD price", () => {
    const old = { plan_tier: "growth", price_usd_monthly: 149, overage_usd: 3, plans: [{ plan_name: "growth", price_usd_monthly: 149 }] };
    expect(billingCurrency(old)).toBe("USD");
    expect(currentPlanPrice(old)).toEqual({ amount: 149, currency: "USD" });
    expect(dueSoFar(old)).toEqual({ amount: 152, currency: "USD", overageUsd: 3, overageApart: false });
  });
});

describe("monthMoney (Dashboard)", () => {
  it("formats the plan fee and the amount due, or dashes", () => {
    expect(monthMoney(null)).toEqual({ planFee: "—", due: "—" });
    const m = monthMoney({ billing_currency: "VND", plan_tier: "growth", overage_usd: 0,
      plans: [{ plan_name: "growth", currency: "VND", price_monthly: "3790000" }],
      quote: { currency: "VND", monthly_total: "3790000" } });
    expect(plain(m.planFee)).toBe("3.790.000 ₫");
    expect(plain(m.due)).toBe("3.790.000 ₫");
    expect(monthMoney({ plan_tier: "enterprise", price_usd_monthly: null, plans: [] }).planFee).toBe("—");
  });
});
