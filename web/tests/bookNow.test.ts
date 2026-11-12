// @vitest-environment jsdom
// AA-1072: "Book now" on the public tour page (lib/bookNow.ts, components/booking/BookNowWidget.tsx), "Pay online" in
// the client's pay box, the gateway's return route, and the words of both languages. Synthetic data, invented names.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { bookNowEn, bookNowVi } from "@/lib/i18nBookNow";
import { MESSAGES } from "@/lib/i18n";
import { bookErrorKey, bookingLink, childAges, safeLinkPath, widgetTour, widgetUrl, type BookingWidget } from "@/lib/bookNow";
import type { ClientInvoice } from "@/lib/proposals";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {}, push: () => {} }) }));
const { BookNowWidget } = await import("@/components/booking/BookNowWidget");
const { ClientPayBox } = await import("@/components/proposals/ClientPayBox");
const { GET: paidReturn } = await import("@/app/c/p/paid/route");
const { OnlinePaymentForm } = await import("@/components/settings/OnlinePaymentForm");

const KEY = "k".repeat(22);
const PAGE = "11111111-1111-4111-8111-111111111111";
const dict = Object.fromEntries(Object.entries(MESSAGES.en).filter(([k]) => k.startsWith("book.")));
const tr = (key: string) => MESSAGES.en[key as keyof typeof MESSAGES.en] ?? key;

const WIDGET: BookingWidget = {
  agency_name: "Lotus Lane Travel",
  tour: "T-1072",
  title: "Lantern bay overnight cruise",
  duration_days: 2,
  currency: "VND",
  child_age_max: 11,
  infant_age_max: 1,
  honeypot_field: "website",
  consent_text: null,
  privacy_notice_url: "https://lotus-lane.example.test/privacy",
  departures: [{ departure_id: "d1", start_date: "2031-05-01", seats_left: 9 }],
};

const roots: Root[] = [];
afterEach(() => {
  for (const r of roots.splice(0)) act(() => r.unmount());
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

async function mount(element: ReturnType<typeof createElement>): Promise<HTMLElement> {
  const container = document.createElement("div");
  document.body.appendChild(container);
  await act(async () => {
    const root = createRoot(container);
    roots.push(root);
    root.render(element);
  });
  await act(async () => {
    await new Promise((r) => setTimeout(r, 0));
  });
  return container;
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

describe("the link and the helpers", () => {
  it("names an agency only with a well-formed form key", () => {
    expect(bookingLink({})).toBeNull();
    expect(bookingLink({ f: "short" })).toBeNull();
    expect(bookingLink({ f: KEY })).toEqual({ formKey: KEY, tour: null });
    expect(bookingLink({ f: [KEY, "x"], p: "T-1072" })).toEqual({ formKey: KEY, tour: "T-1072" });
    expect(bookingLink({ f: KEY, p: "bad\u0001" })).toEqual({ formKey: KEY, tour: null });
    expect(widgetUrl(KEY, "T 1", PAGE)).toBe(`/crm/public/v1/book/${KEY}?tour=T+1&page=${PAGE}`);
    expect(widgetUrl(KEY, null, PAGE)).toBe(`/crm/public/v1/book/${KEY}?page=${PAGE}`);
  });

  it("books the page's own catalogue key, a link key only when the page names none (AA-1094)", () => {
    expect(widgetTour("T-PAGE", "T-LINK")).toBe("T-PAGE");
    expect(widgetTour("T-PAGE", null)).toBe("T-PAGE");
    expect(widgetTour(null, "T-LINK")).toBe("T-LINK");
    expect(widgetTour("", "T-LINK")).toBe("T-LINK");
    expect(widgetTour("bad\u0001", "T-LINK")).toBe("T-LINK");
    expect(widgetTour(null, null)).toBeNull();
  });

  it("reads child ages and refusal codes", () => {
    expect(childAges(["7", "", " 1 "])).toEqual([7, 1]);
    expect(childAges(["18"])).toBeNull();
    expect(childAges(["seven"])).toBeNull();
    expect(childAges(Array(11).fill("5"))).toBeNull();
    expect(bookErrorKey("aa_sold_out", 409)).toBe("book.error.aa_sold_out");
    // S277 live check: a party priced below cost reads as "on request", never as the generic error.
    expect(bookErrorKey("price_on_request", 409)).toBe("book.error.price_on_request");
    expect(bookErrorKey(undefined, 429)).toBe("book.error.rate_limited");
    // AA-1092: the agency's price checks a minute are used up (every visitor together).
    expect(bookErrorKey("agency_rate_limited", 429)).toBe("book.error.agency_rate_limited");
    expect(bookErrorKey("something_else", 500)).toBe("book.error.other");
    expect(safeLinkPath(`/c/p/${"a".repeat(43)}`)).toBe(`/c/p/${"a".repeat(43)}`);
    expect(safeLinkPath("https://evil.example.test/c/p/x")).toBeNull();
    expect(safeLinkPath(null)).toBeNull();
  });

  it("has every word in both languages and every refusal it can show", () => {
    expect(Object.keys(bookNowVi).sort()).toEqual(Object.keys(bookNowEn).sort());
    for (const code of ["aa_sold_out", "no_retail_price", "price_on_request", "booking_rate_limited", "consent_required"]) {
      expect(MESSAGES.en[`book.error.${code}` as keyof typeof MESSAGES.en]).toBeTruthy();
      expect(MESSAGES.vi[`book.error.${code}` as keyof typeof MESSAGES.vi]).toBeTruthy();
    }
    for (const k of ["task.payment_review", "reason.web_booking", "source.web_booking", "client.pay.online"] as const) {
      expect(MESSAGES.vi[k]).toBeTruthy();
    }
  });
});

describe("the widget", () => {
  it("asks the agency when Book now is off or the tour cannot be sold online", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(404, { detail: "booking_not_offered" })));
    const box = await mount(createElement(BookNowWidget, { formKey: KEY, tour: null, pageId: PAGE, locale: "en-AU", dict }));
    expect(box.querySelector('[data-testid="book-now-ask"]')).not.toBeNull();
    expect(box.querySelector("a")?.getAttribute("href")).toBe(`/c/f/${KEY}`);
  });

  it("prices a departure for the party, then books and opens the client page", async () => {
    const link = `/c/p/${"t".repeat(43)}`;
    const calls: { url: string; body: unknown }[] = [];
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : null });
        if (url.endsWith("/price")) {
          return json(200, { currency: "VND", total: "12000000.00", availability: "available", start_date: "2031-05-01",
            lines: [{ pax_type: "adult", qty: 2, unit: "4000000.00" }, { pax_type: "child", qty: 1, unit: "4000000.00" }] });
        }
        if (url.endsWith("/bookings")) return json(201, { status: "created", link_path: link, currency: "VND", total: "12000000.00" });
        return json(200, WIDGET);
      }),
    );
    const box = await mount(createElement(BookNowWidget, { formKey: KEY, tour: "T-1072", pageId: PAGE, locale: "en-AU", dict }));
    const form = box.querySelector('[data-testid="book-now"]') as HTMLFormElement;
    expect(form).not.toBeNull();
    const select = box.querySelector('[data-testid="book-now-departure"]') as HTMLSelectElement;
    await act(async () => {
      select.value = "d1";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    const addChild = [...box.querySelectorAll("button")].find((b) => b.textContent === tr("book.addChild")) as HTMLButtonElement;
    await act(async () => addChild.click());
    const age = box.querySelector('input[min="0"]') as HTMLInputElement;
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
      setter?.call(age, "7");
      age.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const check = box.querySelector('[data-testid="book-now-check"] button') as HTMLButtonElement;
    await act(async () => check.click());
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(calls.find((c) => c.url.endsWith("/price"))?.body).toEqual({ tour: "T-1072", departure_id: "d1", adults: 2, child_ages: [7] });
    expect(box.querySelector('[data-testid="book-now-price"]')?.textContent).toContain("12,000,000 VND");
    expect(box.textContent).not.toMatch(/b2b|net/i);
    (box.querySelector('[data-testid="book-now-name"]') as HTMLInputElement).value = "Mira Solenne";
    (box.querySelector('[data-testid="book-now-email"]') as HTMLInputElement).value = "mira@example.test";
    (box.querySelector('[data-testid="book-now-consent"]') as HTMLInputElement).checked = true;
    await act(async () => {
      form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      await new Promise((r) => setTimeout(r, 0));
    });
    const booked = calls.find((c) => c.url.endsWith("/bookings"))?.body as Record<string, unknown>;
    expect(booked).toMatchObject({ tour: "T-1072", departure_id: "d1", adults: 2, child_ages: [7], name: "Mira Solenne",
      email: "mira@example.test", consent_contact: true, consent_marketing: false, website: "" });
    expect(typeof booked.client_submission_id).toBe("string");
    expect(assign).toHaveBeenCalledWith(link);
  });

  it("shows the refusal of a full departure in words", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => (url.endsWith("/price") ? json(409, { detail: "aa_sold_out" }) : json(200, WIDGET))),
    );
    const box = await mount(createElement(BookNowWidget, { formKey: KEY, tour: null, pageId: PAGE, locale: "en-AU", dict }));
    const select = box.querySelector('[data-testid="book-now-departure"]') as HTMLSelectElement;
    await act(async () => {
      select.value = "d1";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await act(async () => (box.querySelector('[data-testid="book-now-check"] button') as HTMLButtonElement).click());
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(box.querySelector('[data-testid="book-now-error"]')?.textContent).toBe(tr("book.error.aa_sold_out"));
  });
});

describe("pay online", () => {
  const inv: ClientInvoice = {
    invoice_id: "22222222-2222-4222-8222-222222222222", number: "INV-000201", kind: "deposit", seq: 1, currency: "VND",
    total_amount: "3600000.00", balance_amount: "3600000.00", due_on: "2031-03-02", pay_window: { state: "open", until: null },
    pay_by: { name: "Bank transfer", instructions: "Account 0000 7777" }, can_claim: true, pay_online: true,
  };

  it("offers the agency's gateway beside the bank details", () => {
    const html = renderToStaticMarkup(createElement(ClientPayBox, { invoices: [inv], agency: "Lotus Lane Travel", locale: "en-AU", t: tr, onDone: () => {} }));
    expect(html).toContain(tr("client.pay.online"));
    expect(html).toContain("Account 0000 7777");
    const plain = renderToStaticMarkup(createElement(ClientPayBox, { invoices: [{ ...inv, pay_online: false }], agency: "x", locale: "en-AU", t: tr, onDone: () => {} }));
    expect(plain).not.toContain(tr("client.pay.online"));
  });

  it("sends the client to the gateway page and shows a refusal otherwise", async () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, search: "", assign });
    vi.stubGlobal("fetch", vi.fn(async () => json(200, { redirect_url: "https://pay.example.test/vpcpay.html?x=1" })));
    const box = await mount(createElement(ClientPayBox, { invoices: [inv], agency: "Lotus Lane Travel", locale: "en-AU", t: tr, onDone: () => {} }));
    await act(async () => (box.querySelector('[data-testid="client-pay-online"] button') as HTMLButtonElement).click());
    expect(assign).toHaveBeenCalledWith("https://pay.example.test/vpcpay.html?x=1");
    // The page is leaving for the gateway: the button stays busy.
    expect((box.querySelector('[data-testid="client-pay-online"] button') as HTMLButtonElement).disabled).toBe(true);
    vi.stubGlobal("fetch", vi.fn(async () => json(409, { detail: "currency_not_supported" })));
    const again = await mount(createElement(ClientPayBox, { invoices: [inv], agency: "Lotus Lane Travel", locale: "en-AU", t: tr, onDone: () => {} }));
    await act(async () => (again.querySelector('[data-testid="client-pay-online"] button') as HTMLButtonElement).click());
    expect(again.querySelector('[data-testid="client-pay-error"]')?.textContent).toBe(tr("client.error.currency_not_supported"));
  });

  it("the settings panel says Not connected once the gateway is disconnected", () => {
    // S277 live check: after a disconnect the API answers connected: false (the status follows the active account).
    const panel = (status: { connected: boolean; mode: "stub" | null }) =>
      renderToStaticMarkup(
        createElement(OnlinePaymentForm, {
          status: { ...status, ipn_url: "https://client.example.test/crm/public/v1/hooks/vnpay/ipn" },
          lang: "en",
          stubAllowed: true,
        }),
      );
    const on = panel({ connected: true, mode: "stub" });
    expect(on).toContain("Connected (");
    expect(on).not.toContain(tr("gateway.notConnected"));
    const off = panel({ connected: false, mode: null });
    expect(off).toContain(tr("gateway.notConnected"));
    expect(off).not.toContain("Connected (");
  });

  it("offers no Pay online when the API says the invoice cannot be paid online", () => {
    // A disconnected gateway: the API's pay_online is false (no active account), the bank details stay.
    const html = renderToStaticMarkup(
      createElement(ClientPayBox, { invoices: [{ ...inv, pay_online: false }], agency: "Lotus Lane Travel", locale: "en-AU", t: tr, onDone: () => {} }),
    );
    expect(html).not.toContain('data-testid="client-pay-online"');
    expect(html).toContain("Account 0000 7777");
  });

  it("the return route drops the gateway's fields and opens the proposal page", async () => {
    const res = paidReturn();
    expect(res.status).toBe(303);
    expect(res.headers.get("location")).toBe("/c/p?payment=returned");
    expect(res.headers.get("cache-control")).toBe("no-store");
  });
});
