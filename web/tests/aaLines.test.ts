// AA-947 (CRM-19): the AA line helpers ("AA pending", the "from"-price warning, the add form's body, refusal wording),
// the Bookings tab rendering of a read-only AA line (status, retry note, warning, only the listed asks), and the BFF
// allow-list lines of the AA routes.
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { aaErrorKey, aaLineBody, allowedAaActions, belowFromPrice, isRetrying, statusKey, type AaBlock, AA_SUB_STATUSES } from "@/lib/aa";
import { matchBff } from "@/lib/bff";
import { MESSAGES, type MessageKey } from "@/lib/i18n";
import type { Line, TripLines } from "@/lib/ledger";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {} }) }));
const { LinesPanel } = await import("@/components/ledger/LinesPanel");

const ID = "00000000-0000-4000-8000-0000000000e1";

function aa(over: Partial<AaBlock> = {}): AaBlock {
  return {
    sub_status: "quoted",
    tour_source_id: "T-100",
    component_kind: "trip_plan",
    currency: "USD",
    pax: { adult: 2, child: 0 },
    quote_valid_until: "2026-10-27T00:00:00+00:00",
    hold_expires_at: null,
    hold_id: null,
    booking_id: null,
    from_price: { amount: "800.00", currency: "USD" },
    below_from_price: true,
    decline_code: null,
    result_code: null,
    no_longer_offered: false,
    pending: null,
    actions: ["hold", "release", "requote"],
    ...over,
  };
}

function line(over: Partial<Line> = {}): Line {
  return {
    booking_line_id: ID,
    trip_id: ID,
    supplier_kind: "aa",
    agency_supplier_id: ID,
    supplier_name: "Adventure Asia",
    status: "planning",
    aa_sub_status: "quoted",
    title: "Mekong Delta 3 days",
    confirmation_no: null,
    service_from: "2026-12-01",
    service_to: "2026-12-03",
    collect: "agency",
    confirm_class: null,
    visible_to_travelers: false,
    confirmed_at: null,
    read_only: true,
    aa: aa(),
    money: { cost: { amount: "1000.00", currency: "USD" }, sell: { amount: "1500.00", currency: "USD" } },
    ...over,
  };
}

function render(lines: Line[], role = "agent"): string {
  const view: TripLines = { trip_id: ID, lines, base_currency: "USD", booking_validation_level: "basic", lost_reason_hint: null };
  return renderToString(createElement(LinesPanel, { tripId: ID, view, suppliers: [], role, lang: "en", locale: "en-AU" }));
}

describe("AA line helpers", () => {
  it("reads 'AA pending' whatever the last state, and a retry only after a failed try", () => {
    const pending = { aa_request_id: ID, kind: "hold" as const, attempts: 0, next_attempt_at: "2026-10-25T08:00:00Z", last_error_class: null };
    expect(statusKey(aa())).toBe("aa.status.quoted");
    expect(statusKey(aa({ pending }))).toBe("aa.status.pending");
    expect(statusKey(aa({ sub_status: null }))).toBe("aa.status.confirmed");
    expect(isRetrying(aa({ pending }))).toBe(false);
    expect(isRetrying(aa({ pending: { ...pending, attempts: 2, last_error_class: "booking_timeout" } }))).toBe(true);
    for (const s of [...AA_SUB_STATUSES, "confirmed"]) {
      expect(MESSAGES.en[`aa.status.${s}` as MessageKey]).toBeTruthy();
      expect(MESSAGES.vi[`aa.status.${s}` as MessageKey]).toBeTruthy();
    }
  });

  it("warns below the 'from' price per person, never judging what it cannot", () => {
    const from = { amount: "800.00", currency: "USD" };
    expect(belowFromPrice("1500", "USD", 2, 0, from)).toBe(true);
    expect(belowFromPrice("1,700", "USD", 2, 0, from)).toBe(false);
    expect(belowFromPrice("1500", "VND", 2, 0, from)).toBeNull();
    expect(belowFromPrice("", "USD", 2, 0, from)).toBeNull();
    expect(belowFromPrice("1500", "USD", 0, 0, from)).toBeNull();
    expect(belowFromPrice("1500", "USD", 2, 0, null)).toBeNull();
  });

  it("builds the add form's body and names what is wrong", () => {
    const form = {
      tourSourceId: "T-100", componentId: "rt:2", componentKind: "hotel_room", departureId: "", serviceDate: "2026-12-01",
      nights: "2", adults: "2", children: "1", infants: "", currency: "VND", sell: "3,000,000",
    };
    const ok = aaLineBody(form);
    expect(ok).toEqual({
      ok: true,
      body: {
        tour_source_id: "T-100", component_id: "rt:2", component_kind: "hotel_room", departure_id: null,
        service_date: "2026-12-01", nights: 2, pax: { adult: 2, child: 1, infant: 0 }, currency: "VND", sell_amount: "3000000",
      },
    });
    expect(aaLineBody({ ...form, componentId: "" })).toEqual({ ok: false, error: "aa.error.pickProduct" });
    expect(aaLineBody({ ...form, serviceDate: "1/12/2026" })).toEqual({ ok: false, error: "aa.error.date" });
    expect(aaLineBody({ ...form, adults: "0" })).toEqual({ ok: false, error: "aa.error.pax" });
    expect(aaLineBody({ ...form, currency: "vnd" })).toEqual({ ok: false, error: "aa.error.currency" });
    expect(aaLineBody({ ...form, nights: "" })).toEqual({ ok: false, error: "aa.error.nights" });
    expect(aaLineBody({ ...form, sell: "1.234" })).toEqual({ ok: false, error: "aa.error.sell" });
    expect(aaLineBody({ ...form, componentKind: "trip_plan", nights: "", sell: "" })).toMatchObject({ ok: true });
  });

  it("words every AA refusal in both languages", () => {
    for (const code of ["aa_request_pending", "deposit_required", "booking_unavailable", "aa_partner_not_active", "trip_closed"]) {
      const key = aaErrorKey(code) as MessageKey;
      expect(MESSAGES.en[key]).toBeTruthy();
      expect(MESSAGES.vi[key]).toBeTruthy();
    }
    expect(aaErrorKey("teapot")).toBe("ledger.error.other");
  });
});

describe("Bookings tab with an AA line", () => {
  it("shows the AA status, the warning and only the listed asks; no edit, cancel or delete", () => {
    const html = render([line()]);
    expect(html).toContain("AA quoted");
    expect(html).toContain("below AA&#x27;s public");
    expect(html).toContain("Hold with AA");
    expect(html).toContain("Re-quote");
    expect(html).not.toContain("Confirm with AA");
    expect(html).not.toContain("Cancel line");
    expect(html).not.toContain(">Money<");
    expect(html).toContain("Add AA item");
  });

  it("offers 'Confirm with AA' to the owner and the agent only (a confirm books with AA)", () => {
    const held = line({ aa_sub_status: "held", aa: aa({ sub_status: "held", actions: ["confirm", "release"] }) });
    expect(render([held], "agent")).toContain("Confirm with AA");
    expect(render([held], "owner")).toContain("Confirm with AA");
    const assistant = render([held], "assistant");
    expect(assistant).not.toContain("Confirm with AA");
    expect(assistant).toContain("Release");
    expect(allowedAaActions(["hold", "confirm", "release"], "assistant")).toEqual(["hold", "release"]);
    expect(allowedAaActions(["confirm"], "owner")).toEqual(["confirm"]);
  });

  it("shows 'AA pending' with the next try while Booking is down, and 'No longer offered'", () => {
    const pending = { aa_request_id: ID, kind: "quote" as const, attempts: 3, next_attempt_at: "2026-10-25T08:00:00Z", last_error_class: "booking_timeout" };
    const html = render([line({ aa: aa({ sub_status: "pending", pending, actions: [], no_longer_offered: true, below_from_price: null }) })]);
    expect(html).toContain("AA pending");
    expect(html).toContain('data-testid="aa-retrying"');
    expect(html).toContain("attempt 3");
    expect(html).toContain("No longer offered");
    expect(html).not.toContain("Hold with AA");
  });
});

describe("BFF allow-list (AA-947)", () => {
  it.each([
    ["GET", "aa/catalog"],
    ["GET", "aa/products/T-100"],
    ["POST", `trips/${ID}/aa-lines`],
    ["POST", `lines/${ID}/aa/hold`],
    ["POST", `lines/${ID}/aa/confirm`],
    ["POST", `lines/${ID}/aa/release`],
    ["POST", `lines/${ID}/aa/requote`],
  ])("forwards %s %s", (method, path) => {
    expect(matchBff(method, path)).toBeTruthy();
  });

  it.each([
    ["PATCH", `lines/${ID}/aa/hold`],
    ["POST", `lines/${ID}/aa/edit`],
    ["POST", "aa/catalog"],
    ["GET", "aa/products/../../admin"],
  ])("refuses %s %s", (method, path) => {
    expect(matchBff(method, path)).toBeFalsy();
  });
});
