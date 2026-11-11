// @vitest-environment jsdom
// AA-1070: the travel panels render from their API answers (mocked): the trip's Travel panel (passport badges, rooms,
// vouchers, visas, PDF buttons), the departure roster, and the portal's travel section (vouchers, a companion's room,
// the visa checklist). Invented names and places only.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const TRIP = "4d0c3a1e-8f7b-4c2a-9e51-2b6f0a7d3c10";
const LEAD = "4d0c3a1e-8f7b-4c2a-9e51-2b6f0a7d3c11";
const MATE = "4d0c3a1e-8f7b-4c2a-9e51-2b6f0a7d3c12";
const LINE = "4d0c3a1e-8f7b-4c2a-9e51-2b6f0a7d3c13";

const VOUCHER = {
  booking_line_id: LINE,
  voucher_no: "4D0C3A1E",
  title: "Orvane bay cruise",
  service_from: "2031-03-02",
  service_to: null,
  confirmation_no: "C-77",
  supplier: { name: "Calm Bay Boats", category: "activity", contact_name: "Rua", aa_booking_ref: null, aa_departure_id: null },
  pickup: "Pier 4",
  dropoff: null,
  service_time: "07:30",
  pnr: "QX7",
  note: null,
  released: true,
  not_released_reason: null,
};
const ROOMING = {
  trip_id: TRIP,
  rooms: [{ room_no: 1, room_type: "twin", travellers: [{ traveler_id: LEAD, name: "Ilse Varo", role: "lead", bed_pref: null }] }],
  unassigned: [{ traveler_id: MATE, name: "Oto Varo", role: "companion" }],
  room_config: { twin: 1 },
};
const VISA = {
  visa_application_id: "v1",
  trip_id: TRIP,
  traveler_id: LEAD,
  traveler_name: "Ilse Varo",
  visa_template_id: null,
  country: "VN",
  visa_type: "evisa",
  status: "collecting",
  deadline: "2031-01-01",
  submitted_on: null,
  decided_on: null,
  visa_no: null,
  valid_from: null,
  valid_until: null,
  checklist: [{ visa_checklist_item_id: "i1", label: "Photo page", done: true, done_by_kind: "traveler" }],
  checklist_done: 1,
};

const answers: Record<string, unknown> = {
  [`trips/${TRIP}/passports`]: { trip_id: TRIP, valid_months: 6, valid_until_needed: "2031-09-30", issues: 1, travellers: [{ traveler_id: LEAD, role: "lead", status: "ok" }, { traveler_id: MATE, role: "companion", status: "expires_too_soon" }] },
  [`trips/${TRIP}/rooming`]: ROOMING,
  [`trips/${TRIP}/vouchers`]: { trip_id: TRIP, agency_name: "Lotus Trails", trip_title: "Bay days", start_date: null, end_date: null, lead_traveller: "Ilse Varo", pax: 2, vouchers: [VOUCHER] },
  [`trips/${TRIP}/visas`]: { trip_id: TRIP, can_add: true, applications: [VISA] },
  [`trips/${TRIP}/invoices`]: { invoices: [{ invoice_id: "inv1", number: "INV-7", status: "open", kind: "deposit" }] },
  [`trips/${TRIP}/lifecycle`]: { proposals: [{ proposal_id: "p1", version: 2, status: "accepted" }] },
  "departures/DEP-1/roster": {
    departure_id: "DEP-1",
    title: "Orvane loop",
    service_from: "2031-03-01",
    service_to: "2031-03-05",
    state: { status: "guaranteed", guaranteed_at: "2031-01-01T00:00:00Z", cancelled_at: null, min_to_operate: 4 },
    pax: { adults: 2, children: 0, infants: 0 },
    stage3_complete: 0,
    trips: [{ trip_id: TRIP, title: "Bay days", stage: "booked", start_date: null, end_date: null, pax: { adults: 2, children: 0, infants: 0 }, s3: 50, travellers: 2, rooms_placed: 1, passport_issues: 1 }],
    travellers: [
      { trip_id: TRIP, traveler_id: LEAD, name: "Ilse Varo", role: "lead", details_done: true, passport: "ok", room: { room_no: 1, room_type: "twin", bed_pref: null } },
      { trip_id: TRIP, traveler_id: MATE, name: "Oto Varo", role: "companion", details_done: false, passport: "expires_too_soon", room: null },
    ],
    manifest_fields: ["name", "room"],
    manifest_default: ["name"],
  },
};

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {}, push: () => {} }) }));
vi.mock("@/lib/bffClient", () => ({
  bffSend: vi.fn(async (method: string, path: string) => (method === "GET" && path in answers ? { ok: true, data: answers[path] } : { ok: true, data: {} })),
}));

const { TripTravelPanel } = await import("@/components/travelOps/TripTravelPanel");
const { DepartureRoster } = await import("@/components/travelOps/DepartureRoster");
const { PortalTravel, bedOf, travelView } = await import("@/components/portal/PortalTravel");

const roots: Root[] = [];
afterEach(() => {
  for (const r of roots.splice(0)) act(() => r.unmount());
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

async function mount(el: ReturnType<typeof createElement>): Promise<HTMLElement> {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  roots.push(root);
  await act(async () => root.render(el));
  await act(async () => new Promise((r) => setTimeout(r, 0)));
  return host;
}

describe("the travel panels", () => {
  it("the trip panel shows the checks, rooms, vouchers, visas and PDF buttons", async () => {
    const el = await mount(createElement(TripTravelPanel, { tripId: TRIP, lang: "en", locale: "en-GB" }));
    const passports = [...el.querySelectorAll('[data-testid="travel-passport-row"]')].map((r) => r.getAttribute("data-status"));
    expect(passports).toEqual(["ok", "expires_too_soon"]);
    expect(el.textContent).toContain("Passport expires too soon");
    expect(el.querySelectorAll('[data-testid="travel-room-person"]')).toHaveLength(2);
    expect(el.querySelector('[data-testid="travel-voucher"]')?.getAttribute("data-released")).toBe("true");
    expect(el.querySelector('[data-testid="travel-visa"]')?.getAttribute("data-status")).toBe("collecting");
    expect(el.textContent).toContain("Invoice INV-7 PDF");
    expect(el.textContent).toContain("Proposal v2 PDF");
    expect(el.textContent).not.toMatch(/travel\.[a-z]/);
  });

  it("the roster shows the feed state and every traveller", async () => {
    const el = await mount(createElement(DepartureRoster, { departureRef: "DEP-1", lang: "vi", locale: "vi-VN" }));
    expect(el.querySelector('[data-testid="departure-state"]')?.getAttribute("data-state")).toBe("guaranteed");
    expect(el.textContent).toContain("Chắc chắn khởi hành");
    expect(el.textContent).toContain("Oto Varo");
    expect(el.textContent).not.toMatch(/travel\.[a-z]/);
  });

  it("the merged manifest asks for a reason once a health column is picked", async () => {
    const el = await mount(createElement(DepartureRoster, { departureRef: "DEP-1", lang: "en", locale: "en-GB" }));
    const download = () => el.querySelector('[data-testid="roster-manifest-download"] button') as HTMLButtonElement;
    expect(download().disabled).toBe(false);
    const dietary = [...el.querySelectorAll("label")].find((l) => l.textContent === "Dietary")?.querySelector("input") as HTMLInputElement;
    await act(async () => dietary.click());
    expect(download().disabled).toBe(true);
    expect(el.textContent).toContain("Needed for more than 5 travellers");
  });

  it("the portal shows the released vouchers, the room and the checklist", async () => {
    const t = (k: string) => k;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ vouchers: [VOUCHER], rooming: { ...ROOMING, can_edit: false }, visas: [VISA] }), { status: 200 })),
    );
    const step = vi.fn(async () => {});
    const el = await mount(createElement(PortalTravel, { base: "/crm/public/v1/traveller", t, locale: "en-GB", busy: false, step, round: 0 }));
    expect(el.querySelectorAll('[data-testid="portal-voucher"]')).toHaveLength(1);
    expect(el.textContent).toContain("Pier 4");
    expect(el.querySelector('[data-testid="portal-rooming"]')?.textContent).toContain("Ilse Varo");
    expect(el.querySelector('[data-testid="portal-visas"]')?.textContent).toContain("Photo page");
    expect(travelView({ vouchers: [] })).toBeNull();
  });

  it("every printed portal voucher carries the agency, the trip, the lead traveller and the party size", async () => {
    const t = (k: string) => k;
    const header = { agency_name: "Lotus Trails", trip_title: "Bay days", start_date: "2031-03-01", end_date: "2031-03-31", lead_traveller: "Ilse Varo", pax: 2 };
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ vouchers: [VOUCHER], voucher_header: header, rooming: { ...ROOMING, can_edit: false }, visas: [] }), { status: 200 })),
    );
    const el = await mount(createElement(PortalTravel, { base: "/crm/public/v1/traveller", t, locale: "en-GB", busy: false, step: vi.fn(async () => {}), round: 0 }));
    const printed = el.querySelector('[data-testid="portal-voucher"]');
    const head = printed?.querySelector('[data-testid="portal-voucher-head"]');
    expect(head?.textContent).toContain("Lotus Trails");
    expect(head?.textContent).toContain("client.travel.leadTraveller: Ilse Varo");
    expect(head?.textContent).toContain("client.travel.party: 2");
    expect(head?.textContent).toContain("Bay days");
  });

  it("the lead's rooming save keeps the bed preference the agency set", async () => {
    const t = (k: string) => k;
    const rooming = {
      ...ROOMING,
      can_edit: true,
      rooms: [{ room_no: 1, room_type: "twin", travellers: [{ traveler_id: LEAD, name: "Ilse Varo", role: "lead", bed_pref: "extra_bed" }] }],
    };
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ vouchers: [], rooming, visas: [] }), { status: 200 })));
    const step = vi.fn(async (...args: unknown[]) => void args);
    const el = await mount(createElement(PortalTravel, { base: "/crm/public/v1/traveller", t, locale: "en-GB", busy: false, step, round: 0 }));
    const save = [...el.querySelectorAll("button")].find((b) => b.textContent === "client.travel.saveRooms");
    await act(async () => save?.click());
    expect(step).toHaveBeenCalledTimes(1);
    expect(step.mock.calls[0][2]).toEqual({ rooms: [{ room_no: 1, room_type: "twin", travellers: [{ traveler_id: LEAD, bed_pref: "extra_bed" }] }] });
    expect(bedOf({ traveler_id: MATE })).toBeNull();
  });
});
