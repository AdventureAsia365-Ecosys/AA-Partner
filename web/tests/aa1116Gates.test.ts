// @vitest-environment jsdom
// AA-1116: the CRM plan gates in the web. The Reports CSV export is locked when the plan has no insights_export (an
// older API without the field keeps it open), and Settings > Payments marks a provider the plan lacks (Stripe without
// stripe_connect) "Not on your plan" with the ask, in English and in Vietnamese.
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { MESSAGES } from "@/lib/i18n";
import { providerBadge, type PaymentMethodsView } from "@/lib/invoices";
import { exportLocked } from "@/lib/reports";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {} }) }));

const { PaymentMethodsForm } = await import("@/components/invoices/PaymentMethodsForm");

describe("plan gates: helpers", () => {
  it("locks the export only when the API says the plan lacks it", () => {
    expect(exportLocked({ export_allowed: false })).toBe(true);
    expect(exportLocked({ export_allowed: true })).toBe(false);
    expect(exportLocked({})).toBe(false);
  });

  it("puts the plan before the provider's own availability", () => {
    expect(providerBadge({ available: false, on_plan: false })).toBe("methods.notOnPlan");
    expect(providerBadge({ available: true, on_plan: false })).toBe("methods.notOnPlan");
    expect(providerBadge({ available: false, on_plan: true })).toBe("methods.notYet");
    expect(providerBadge({ available: true })).toBe("methods.available");
  });

  it("has every new message in both languages", () => {
    for (const key of ["reports.export.locked", "methods.notOnPlan", "methods.notOnPlanHint"] as const) {
      expect(MESSAGES.en[key], key).toBeTruthy();
      expect(MESSAGES.vi[key], key).toBeTruthy();
      expect(MESSAGES.vi[key], key).not.toBe(MESSAGES.en[key]);
    }
  });
});

function methods(onPlan: boolean | undefined): PaymentMethodsView {
  return {
    accounts: [],
    providers: [
      { provider: "manual", available: true, on_plan: true },
      { provider: "stripe", available: false, on_plan: onPlan },
      { provider: "vn_gateway", available: true, on_plan: true },
    ],
  };
}

describe("Settings > Payments: providers off the plan", () => {
  it("marks Stripe not on the plan, with the ask", () => {
    const html = renderToString(createElement(PaymentMethodsForm, { view: methods(false), lang: "en" }));
    expect(html).toContain('data-testid="payment-provider-stripe"');
    expect(html).toContain("Not on your plan");
    expect(html).toContain("Ask Adventure Asia to add it to your agency&#x27;s plan.");
    expect(html).not.toContain("AA-1116");
  });

  it("says it in Vietnamese too", () => {
    const html = renderToString(createElement(PaymentMethodsForm, { view: methods(false), lang: "vi" }));
    expect(html).toContain(MESSAGES.vi["methods.notOnPlan"]);
  });

  it("keeps the old answer when the plan has it or an older API does not say", () => {
    for (const onPlan of [true, undefined]) {
      const html = renderToString(createElement(PaymentMethodsForm, { view: methods(onPlan), lang: "en" }));
      expect(html, String(onPlan)).not.toContain("Not on your plan");
      expect(html, String(onPlan)).toContain("Coming later");
    }
  });
});
