// @vitest-environment jsdom
// AA-1071 (crm-056): the sales partner helpers (who may do what, the terms in words, the commission actions per status,
// the filters, the refusal wording), the BFF lines (the partner routes only), the form body, both languages, the
// lead source label, and the link cookie of the partner page (host-only, httpOnly, scoped to the API's partner path).
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { matchBff } from "@/lib/bff";
import { MESSAGES } from "@/lib/i18n";
import { partnersEn, partnersVi } from "@/lib/i18nPartners";
import {
  canManage,
  commissionActions,
  FILTERS,
  isFilter,
  partnerCookie,
  partnerErrorKey,
  partnerPageErrorKey,
  seesMoney,
  termsText,
} from "@/lib/partners";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {}, push: () => {} }) }));

const UUID = "00000000-0000-4000-8000-000000000071";
const money = (a: string, c: string) => `${a} ${c}`;

describe("partner helpers", () => {
  it("gives management to the owner and money to owner and agent", () => {
    expect([canManage("owner"), canManage("agent"), canManage("assistant")]).toEqual([true, false, false]);
    expect([seesMoney("owner"), seesMoney("agent"), seesMoney("assistant"), seesMoney(null)]).toEqual([true, true, false, false]);
  });
  it("words the terms", () => {
    expect(termsText("en", { commission_type: "percent", rate: "10.00", basis: "revenue" }, money)).toBe("10% of the trip price");
    expect(termsText("en", { commission_type: "net_rate", rate: "12.50", basis: "margin" }, money)).toBe("Net rate: keeps 12.5% of the trip margin");
    expect(termsText("en", { commission_type: "fixed_per_pax", rate: "25.00", currency: "EUR" }, money)).toBe("25.00 EUR per traveller");
    expect(termsText("en", {}, money)).toBe("");
    expect(termsText("vi", { commission_type: "percent", rate: "5", basis: "revenue" }, money)).toBe("5% trên giá chuyến");
  });
  it("offers the next steps per status", () => {
    expect(commissionActions("accrued")).toEqual(["approve", "cancel"]);
    expect(commissionActions("approved")).toEqual(["pay", "unapprove", "cancel"]);
    expect(commissionActions("paid")).toEqual([]);
    expect(commissionActions("cancelled")).toEqual([]);
    expect(FILTERS[0]).toBe("open");
    expect(isFilter("approved")).toBe(true);
    expect(isFilter("overdue")).toBe(false);
  });
  it("words every refusal", () => {
    expect(partnerErrorKey("partner_in_use")).toBe("partners.error.partner_in_use");
    expect(partnerErrorKey("something_new")).toBe("partners.error.generic");
    expect(partnerErrorKey(null)).toBe("partners.error.generic");
    for (const d of ["too_many_codes", "no_email_on_file", "code_delivery_unavailable", "code_expired", "link_not_found"]) {
      expect(partnerPageErrorKey(d)).not.toBe("partnerPage.error");
    }
    expect(partnerPageErrorKey("other")).toBe("partnerPage.error");
  });
  it("sets the link cookie on the API's partner path only", () => {
    const cookie = partnerCookie("A".repeat(43));
    for (const part of ["aa_partner_link=", "Path=/crm/public/v1/partner", "HttpOnly", "Secure", "SameSite=Strict"]) expect(cookie).toContain(part);
  });
});

describe("BFF lines", () => {
  it("serve the partner routes, nothing else", () => {
    expect(matchBff("GET", "sales-partners")).not.toBeNull();
    expect(matchBff("POST", "sales-partners")).not.toBeNull();
    for (const m of ["GET", "PATCH", "DELETE"]) expect(matchBff(m, `sales-partners/${UUID}`)).not.toBeNull();
    expect(matchBff("POST", `sales-partners/${UUID}/links`)).not.toBeNull();
    expect(matchBff("POST", `sales-partners/${UUID}/links/${UUID}/revoke`)).not.toBeNull();
    expect(matchBff("GET", `sales-partners/${UUID}/statement`)?.download).toBe(true);
    expect(matchBff("PUT", `trips/${UUID}/sales-partner`)).not.toBeNull();
    expect(matchBff("POST", `trips/${UUID}/partner-commission/sync`)).not.toBeNull();
    expect(matchBff("GET", "partner-commissions")).not.toBeNull();
    for (const a of ["approve", "unapprove", "pay", "cancel"]) expect(matchBff("POST", `partner-commissions/${UUID}/${a}`)).not.toBeNull();
    expect(matchBff("POST", `partner-commissions/${UUID}/receive`)).toBeNull();
    expect(matchBff("DELETE", `partner-commissions/${UUID}`)).toBeNull();
  });
});

describe("strings", () => {
  it("exist in both languages and reach the dictionary", () => {
    expect(Object.keys(partnersVi).sort()).toEqual(Object.keys(partnersEn).sort());
    for (const key of Object.keys(partnersEn)) expect(MESSAGES.en[key as keyof typeof MESSAGES.en]).toBeTruthy();
    expect(MESSAGES.en["source.sales_partner" as keyof typeof MESSAGES.en]).toBe("Sent by a sales partner");
  });
});

describe("partner form body", () => {
  it("drops the currency of a percent and clears empty contacts", async () => {
    const { partnerBody } = await import("@/components/partners/PartnerForm");
    const base = { kind: "referrer", name: " Blue Kite ", contact_email: " ", contact_phone: "", rate: "7.5", currency: "usd", basis: "margin", payout_due_days: "15" };
    expect(partnerBody({ ...base, commission_type: "percent" })).toEqual({
      kind: "referrer", name: "Blue Kite", contact_email: null, contact_phone: null, commission_type: "percent", rate: "7.5",
      currency: null, basis: "margin", payout_due_days: 15,
    });
    expect(partnerBody({ ...base, commission_type: "fixed_per_pax", rate: "" })).toMatchObject({ currency: "USD", rate: "0" });
  });
});

describe("partner page", () => {
  let root: Root | null = null;
  afterEach(() => {
    act(() => root?.unmount());
    root = null;
    vi.unstubAllGlobals();
  });

  it("asks for a code, then lists the trips without names", async () => {
    const { PartnerView } = await import("@/components/partners/PartnerView");
    let signedIn = false;
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/code")) return new Response(JSON.stringify({ sent: false, to_hint: null, dev_code: "123456" }), { status: 202 });
      if (url.endsWith("/session")) {
        signedIn = true;
        return new Response(JSON.stringify({ signed_in: true }), { status: 200 });
      }
      expect(init?.method ?? "GET").toBe("GET");
      return new Response(
        JSON.stringify(
          signedIn
            ? {
                agency: "Lotus Trails", signed_in: true, link_expires_at: "2027-01-01T00:00:00Z", partner_name: "Blue Kite",
                trips: [{ ref: "T-1A2B3C4D", stage: "booked", start_month: "2027-03", travellers: 2, received_on: "2026-11-02",
                          commission: { amount: "90.00", currency: "USD", status: "accrued", due_on: null, paid_on: null } }],
                totals: [{ currency: "USD", open: "90.00", paid: "0.00" }],
              }
            : { agency: "Lotus Trails", signed_in: false, link_expires_at: "2027-01-01T00:00:00Z" },
        ),
        { status: 200 },
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    const host = document.createElement("div");
    document.body.appendChild(host);
    const dict = Object.fromEntries(Object.entries(partnersEn).filter(([k]) => k.startsWith("partnerPage.") || k.startsWith("partners.pcStatus.")));
    await act(async () => {
      root = createRoot(host);
      root.render(createElement(PartnerView, { dict, lang: "en", locale: "en-GB" }));
    });
    expect(host.querySelector('[data-testid="partner-sign-in"]')).not.toBeNull();
    await act(async () => (host.querySelector('[data-testid="partner-send-code"]') as HTMLButtonElement).click());
    expect(host.querySelector('[data-testid="partner-code-sent"]')?.textContent).toContain("123456");
    const input = host.querySelector('[data-testid="partner-code"]') as HTMLInputElement;
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
      setter?.call(input, "123456");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => (host.querySelector('[data-testid="partner-sign-in-button"]') as HTMLButtonElement).click());
    expect(host.querySelector('[data-testid="partner-view"]')).not.toBeNull();
    expect(host.textContent).toContain("T-1A2B3C4D");
    expect(host.textContent).toContain("Hello Blue Kite");
  });
});
