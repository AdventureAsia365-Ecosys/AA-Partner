// AA-1096 (S280 settings audit, lane crm): the agency's settings pages say the truth and stay usable after AA changes
// a limit — a markup above a lowered ceiling, a price-check limit outside a narrowed range, AI with no per-minute limit,
// the AI ceiling worded as AA's (not a plan's), form refusals with their reason next to the form, the per-form privacy
// link, owner and hourly quota, a failed read shown as an error, the refund approval limit and its amount.
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { AiSettings } from "@/lib/ai";
import { MESSAGES } from "@/lib/i18n";
import { formErrorKey } from "@/lib/intakeForms";
import { thresholdLimit } from "@/lib/invoices";
import { thresholdInput, type PaymentSettings } from "@/lib/ledger";
import { ownerOnlyText } from "@/lib/retailPayments";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {}, push: () => {} }) }));
const { aboveLimit, changedMarkups, RetailMarkupForm } = await import("@/components/proposals/RetailMarkupForm");
const { inRange, OnlineBookingForm, serverBounds } = await import("@/components/settings/OnlineBookingForm");
const { AiSettingsView } = await import("@/components/settings/AiSettingsView");
const { FormsAdmin, formDetailsBody, PipelineSettingsPanel } = await import("@/components/pipeline/FormsAdmin");
const { PaymentsView } = await import("@/components/ledger/PaymentsView");

const KINDS = ["trip_plan", "hotel_room", "transfer"];

describe("retail markup after AA lowered the ceiling", () => {
  it("sends only the changed kinds, so a markup kept above the limit never blocks another", () => {
    const stored = { trip_plan: "250", hotel_room: "10" };
    expect(changedMarkups(KINDS, { trip_plan: "250", hotel_room: "15", transfer: "" }, stored, 100)).toEqual({ body: { hotel_room: 15 } });
    expect(changedMarkups(KINDS, { trip_plan: "250.00", hotel_room: "10", transfer: "" }, stored, 100)).toEqual({ body: {} });
    expect(changedMarkups(KINDS, { trip_plan: "", hotel_room: "10", transfer: "" }, stored, 100)).toEqual({ body: { trip_plan: null } });
    // a new value above the limit names its product type
    expect(changedMarkups(KINDS, { trip_plan: "250", hotel_room: "10", transfer: "120" }, stored, 100)).toEqual({ invalid: "transfer" });
    expect(aboveLimit("250", 100)).toBe(true);
    expect(aboveLimit("100", 100)).toBe(false);
    expect(aboveLimit(undefined, 100)).toBe(false);
  });

  it("marks the stored value above AA's limit on the form", () => {
    const html = renderToString(
      createElement(RetailMarkupForm, { view: { retail_markup_pct: { trip_plan: "250" }, kinds: KINDS, markup_max_pct: "100" }, lang: "en" }),
    );
    expect(html).toContain("Above AA&#x27;s current limit of 100 %");
  });
});

describe("price checks a minute after AA narrowed the range", () => {
  const view = {
    enabled: true,
    price_checks_per_minute: 300,
    price_checks_bounds: { min: 10, max: 120, default: 60 },
    page_base: null,
    forms: [],
  };

  it("says the saved value is outside the range and still applies", () => {
    const html = renderToString(createElement(OnlineBookingForm, { view, lang: "en" }));
    expect(html).toContain("Your saved limit (300) is outside the range AA allows now (10 to 120)");
    expect(inRange(300, view.price_checks_bounds)).toBe(false);
    expect(serverBounds({ detail: "invalid_price_checks_per_minute", min: 20, max: 90 })).toEqual({ min: 20, max: 90 });
    expect(serverBounds({ detail: "x" })).toBeNull();
  });

  it("ties the range hint to the input (the kit Field describes the control)", () => {
    const html = renderToString(createElement(OnlineBookingForm, { view: { ...view, price_checks_per_minute: 60 }, lang: "en" }));
    const described = /aria-describedby="([^"]+)"[^>]*data-testid="online-booking-ceiling"|data-testid="online-booking-ceiling"[^>]*aria-describedby="([^"]+)"/.exec(html);
    expect(described).not.toBeNull();
    const id = described![1] ?? described![2];
    expect(html).toMatch(new RegExp(`id="${id}"[^>]*>[^<]*How many live price checks`));
    expect(html).not.toContain("outside the range");
  });
});

describe("Settings > AI", () => {
  const VIEW: AiSettings = {
    ai_enabled: true,
    ai_budget_usd: "2.00",
    budget_max_usd: "5.00",
    spent_usd_month: "0.0000",
    budget_used_pct: 0,
    alert_level: null,
    period_start: "2026-11-01",
    provider: "bedrock",
    test_mode: false,
    rate_limit_per_minute: 0,
    can_edit: true,
  };

  it("a rate limit of 0 is no limit, never 'up to 0 requests'", () => {
    const html = renderToString(createElement(AiSettingsView, { settings: VIEW, lang: "en", locale: "en-GB" }));
    expect(html).toContain("No per-minute limit on AI requests");
    expect(html).not.toContain("Up to 0 AI requests");
    const six = renderToString(createElement(AiSettingsView, { settings: { ...VIEW, rate_limit_per_minute: 6 }, lang: "vi", locale: "vi-VN" }));
    expect(six).toContain("Tối đa 6 yêu cầu AI mỗi phút");
  });

  it("the ceiling is AA's limit for the agency, not a plan's", () => {
    for (const lang of ["en", "vi"] as const) {
      for (const key of ["ai.budgetHelp", "ai.error.budget_above_plan"] as const) {
        expect(MESSAGES[lang][key]).toContain("Adventure Asia");
        expect(MESSAGES[lang][key]).toContain("{max}");
        expect(MESSAGES[lang][key].toLowerCase()).not.toMatch(/your plan|gói của bạn/);
      }
    }
  });
});

describe("Settings > Forms", () => {
  const FORM = {
    intake_form_id: "00000000-0000-4000-8000-0000000000f1",
    public_key: "k".repeat(22),
    label: "Website",
    allowed_origins: ["https://www.agency.example"],
    default_owner_member_id: null,
    lead_source: "website_form",
    privacy_notice_url: "https://www.agency.example/privacy",
    status: "active" as const,
    created_at: "2026-11-01T00:00:00Z",
  };
  const TEAM = [
    { member_id: "00000000-0000-4000-8000-0000000000a1", role: "agent" as const, status: "active" as const, email: "anna@agency.example", joined_at: null, open_trips: 0, is_me: false },
    { member_id: "00000000-0000-4000-8000-0000000000a2", role: "agent" as const, status: "disabled" as const, email: "gone@agency.example", joined_at: null, open_trips: 0, is_me: false },
  ];

  it("names why a website list or privacy link is refused", () => {
    expect(formErrorKey({ detail: "invalid_origins" })).toBe("forms.error.invalid_origins");
    expect(formErrorKey({ detail: "error", body: { detail: [{ loc: ["body", "allowed_origins"], msg: "too long" }] } })).toBe("forms.error.invalid_origins");
    expect(formErrorKey({ detail: "invalid_privacy_notice_url" })).toBe("forms.error.invalid_privacy_notice_url");
    expect(formErrorKey({ detail: "form_not_found" })).toBe("forms.error.other");
    for (const lang of ["en", "vi"] as const) {
      expect(MESSAGES[lang]["forms.error.invalid_origins"]).toContain("10");
      expect(MESSAGES[lang]["forms.originsHelp"]).toContain("10");
    }
  });

  it("shows each form's privacy link, its default owner (active members) and the hourly quota", () => {
    const html = renderToString(
      createElement(FormsAdmin, {
        forms: [FORM],
        clientHosts: ["clients.agency.example"],
        isOwner: true,
        lang: "en",
        quota: { submissions_per_hour: 200, submissions_per_hour_own: true },
        team: TEAM,
      }),
    );
    expect(html).toContain('value="https://www.agency.example/privacy"');
    expect(html).toContain("anna@agency.example");
    expect(html).not.toContain("gone@agency.example");
    expect(html).toContain("Each form takes up to 200 submissions an hour");
    expect(html).toContain("Adventure Asia set this limit for your agency.");
    const off = renderToString(createElement(FormsAdmin, { forms: [FORM], clientHosts: [], isOwner: false, lang: "en", quota: { submissions_per_hour: 0 } }));
    expect(off).toContain("Your forms have no hourly limit");
    expect(off).not.toContain('data-testid="forms-owner"');
  });

  it("saves a privacy link alone when the form's owner has since left (the owner goes out only when changed)", () => {
    const kept = { default_owner_member_id: TEAM[1].member_id };
    expect(formDetailsBody(kept, " https://x.example/p ", TEAM[1].member_id)).toEqual({ privacy_notice_url: "https://x.example/p" });
    expect(formDetailsBody(kept, "", TEAM[0].member_id)).toEqual({ privacy_notice_url: null, default_owner_member_id: TEAM[0].member_id });
    expect(formDetailsBody(kept, "", "")).toEqual({ privacy_notice_url: null, default_owner_member_id: null });
    expect(formDetailsBody({ default_owner_member_id: null }, "", "")).toEqual({ privacy_notice_url: null });
    expect(formDetailsBody(kept, "", undefined)).toEqual({ privacy_notice_url: null });
  });

  it("an owner whose pipeline settings did not load sees an error, not 'only the owner'", () => {
    const owner = renderToString(createElement(PipelineSettingsPanel, { settings: null, isOwner: true, lang: "en" }));
    expect(owner).toContain(MESSAGES.en["error.api"]);
    expect(owner).not.toContain(MESSAGES.en["pset.ownerOnly"]);
    const agent = renderToString(createElement(PipelineSettingsPanel, { settings: null, isOwner: false, lang: "en" }));
    expect(agent).toContain(MESSAGES.en["pset.ownerOnly"]);
  });
});

describe("refund approval limit", () => {
  const SETTINGS: PaymentSettings = {
    base_currency: "VND",
    booking_validation_level: "basic",
    fx_mode: "automatic",
    refund_owner_threshold_base: "1000.00",
    levels: ["off", "basic", "strict"],
    fx_modes: ["automatic", "manual"],
    attribution: { text: "Rates", url: "https://example.org" },
  };

  it("the owner sees and edits it in the base currency, warned that a currency change keeps the number", () => {
    const html = renderToString(createElement(PaymentsView, { settings: SETTINGS, overrides: [], lang: "en", locale: "en-GB" }));
    expect(html).toContain("Refund approval limit (VND)");
    expect(html).toContain('value="1000.00"');
    expect(html).toContain("check it after a change");
    const old = renderToString(
      createElement(PaymentsView, { settings: { ...SETTINGS, refund_owner_threshold_base: undefined }, overrides: [], lang: "en", locale: "en-GB" }),
    );
    expect(old).not.toContain("payments-threshold");
  });

  it("reads the typed amount within the column bounds", () => {
    expect(thresholdInput("2,500,000")).toBe("2500000");
    expect(thresholdInput("0")).toBe("0");
    expect(thresholdInput("12.345")).toBeNull();
    expect(thresholdInput("10000001")).toBeNull();
    expect(thresholdInput("")).toBeNull();
  });

  it("an agent's refusal names the limit amount", () => {
    expect(thresholdLimit({ detail: "above_threshold", limit: "1000.00", limit_currency: "USD" })).toEqual({ amount: "1000.00", currency: "USD" });
    expect(thresholdLimit({ detail: "above_threshold" })).toBeNull();
    for (const lang of ["en", "vi"] as const) expect(MESSAGES[lang]["invoice.error.above_threshold_limit"]).toContain("{limit}");
  });

  it("the cancel dialog names the limit an agent may lower the charge by", () => {
    const tr = (key: string) => MESSAGES.en[key as keyof typeof MESSAGES.en];
    const limited = ownerOnlyText(tr, "en-GB", "cancelCharge.needsOwner", { limit: "100.00", limit_currency: "USD" });
    expect(limited).toContain("100");
    expect(limited).toContain("owner");
    expect(ownerOnlyText(tr, "en-GB", "cancelCharge.needsOwner", null)).toBe(MESSAGES.en["cancelCharge.needsOwner"]);
    for (const lang of ["en", "vi"] as const) expect(MESSAGES[lang]["cancelCharge.needsOwnerLimit"]).toContain("{limit}");
  });
});
