// @vitest-environment jsdom
// AA-1069 (crm-052 / crm-053): the catalogue helpers (amounts, group prices that never overlap, the quantity basis, the
// validity window, refusal wording), the BFF lines (only the routes the API serves), both languages, the Services nav
// entry, the builder's party for the preview, and the supplier requests panel: it prepares one draft per supplier,
// sends the edited one with its lines, and says when a supplier has no e-mail.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { matchBff } from "@/lib/bff";
import { catalogueErrorKey, quantityFor, tiersInput, unitInput, validOn, type SupplierRequests } from "@/lib/catalogue";
import { t, type MessageKey } from "@/lib/i18n";
import { catalogueEn, catalogueVi } from "@/lib/i18nCatalogue";
import { builderErrorKey } from "@/lib/builder";
import { visibleSections } from "@/lib/nav";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {}, push: () => {} }) }));
const sent: { method: string; path: string; body: unknown }[] = [];
let answers: { ok: boolean; status?: number; detail?: string; data?: unknown }[] = [];
vi.mock("@/lib/bffClient", () => ({
  bffSend: async (method: string, path: string, body?: unknown) => {
    sent.push({ method, path, body });
    return answers.shift() ?? { ok: true, data: {} };
  },
  bffDownload: async () => ({ ok: true, data: null }),
}));
const { SupplierRequestsPanel } = await import("@/components/catalogue/SupplierRequestsPanel");
const { builderParty } = await import("@/components/catalogue/BuilderCatalogueTab");
const { ToastProvider } = await import("@/app/_kit");

const UUID = "00000000-0000-4000-8000-000000000001";
const TRIP = "00000000-0000-4000-8000-0000000000f1";
const MSG = "00000000-0000-4000-8000-0000000000e1";
const LINE = "00000000-0000-4000-8000-0000000000a1";

describe("catalogue helpers", () => {
  it("reads amounts and group prices", () => {
    expect(unitInput(" 1,250.5 ")).toBe("1250.5");
    expect(unitInput("")).toBeNull();
    expect(unitInput("12.345")).toBeUndefined();
    expect(tiersInput([{ min: "5", max: "", net: "70" }, { min: "1", max: "4", net: "80" }, { min: "", max: "", net: "" }])).toEqual([
      { min_pax: 1, max_pax: 4, unit_net: "80" },
      { min_pax: 5, max_pax: null, unit_net: "70" },
    ]);
    expect(tiersInput([{ min: "1", max: "", net: "1" }, { min: "3", max: "", net: "1" }])).toBeNull(); // overlap
    expect(tiersInput([{ min: "4", max: "2", net: "1" }])).toBeNull();
    expect(tiersInput([{ min: "1", max: "", net: "" }])).toBeNull();
  });
  it("counts units like the API and knows the validity window", () => {
    const party = { travellers: 3, adults: 2, rooms: 2 };
    expect(quantityFor("per_person", party, 4)).toBe(3);
    expect(quantityFor("per_adult", party, 4)).toBe(2);
    expect(quantityFor("per_booking", party, 4)).toBe(1);
    expect(quantityFor("per_room", party, 4)).toBe(2);
    expect(quantityFor("per_night", party, 0)).toBe(1);
    expect(quantityFor("per_person_night", party, 4)).toBe(12);
    expect(validOn({ valid_from: "2031-03-01", valid_to: "2031-03-31" }, "2031-03-10")).toBe(true);
    expect(validOn({ valid_from: "2031-03-01", valid_to: null }, "2031-02-28")).toBe(false);
    expect(validOn({ valid_from: null, valid_to: "2031-03-31" }, null)).toBe(true);
  });
  it("words refusals in the catalogue and in the builder", () => {
    expect(catalogueErrorKey("tier_overlap")).toBe("catalogue.error.tier_overlap");
    expect(catalogueErrorKey("something_new")).toBe("catalogue.error.other");
    expect(t("en", builderErrorKey("service_not_valid"))).toBe("This service is not offered on that date.");
  });
  it("reads the builder's party from the rooms, else the Stage 2 party", () => {
    const stage2 = { accommodation_tier: null, group_pref: null, pace: null, party_adults: 2, party_children: 1 };
    expect(builderParty({ room_config: [], stage2 })).toEqual({ travellers: 3, adults: 2, rooms: 1 });
    expect(builderParty({ room_config: [{ type: "double", adults: 2, children: 1 }, { type: "single", adults: 1, children: 0 }], stage2 }))
      .toEqual({ travellers: 4, adults: 3, rooms: 2 });
  });
});

describe("BFF lines", () => {
  it("serve the catalogue and supplier request routes the API has, nothing else", () => {
    for (const [m, p] of [
      ["GET", "services"],
      ["POST", "services"],
      ["GET", `services/${UUID}`],
      ["PATCH", `services/${UUID}`],
      ["DELETE", `services/${UUID}`],
      ["POST", `proposals/${UUID}/blocks/${UUID}/catalogue-options`],
      ["POST", `trips/${UUID}/supplier-requests`],
      ["POST", `trips/${UUID}/supplier-requests/send`],
      ["POST", `lines/${UUID}/back-to-planning`],
      ["POST", `lines/${UUID}/confirm`],
    ] as const) {
      expect(matchBff(m, p), `${m} ${p}`).not.toBeNull();
    }
    expect(matchBff("PUT", "services")).toBeNull();
    expect(matchBff("GET", `trips/${UUID}/supplier-requests`)).toBeNull();
    expect(matchBff("DELETE", `services/${UUID}/x`)).toBeNull();
  });
});

describe("words and navigation", () => {
  it("has every catalogue word in English and Vietnamese", () => {
    expect(Object.keys(catalogueVi).sort()).toEqual(Object.keys(catalogueEn).sort());
    for (const v of Object.values(catalogueVi)) expect(v.trim()).not.toBe("");
    expect(t("vi", "nav.services" as MessageKey)).toBe("Dịch vụ");
    expect(t("en", "ledger.status.requested" as MessageKey)).toBe("Requested");
  });
  it("lists Services for every role", () => {
    for (const role of ["owner", "agent", "assistant"] as const) expect(visibleSections(role).map((s) => s.key)).toContain("services");
  });
});

let root: Root | null = null;
afterEach(() => {
  act(() => root?.unmount());
  root = null;
  document.body.innerHTML = "";
  sent.length = 0;
  answers = [];
});

const DRAFTS: SupplierRequests = {
  trip_id: TRIP,
  locale: "en",
  skipped: [],
  requests: [
    { agency_supplier_id: UUID, supplier_name: "Lantern Air", category: "flight", line_ids: [LINE], include_names: true, has_email: true,
      conversation_id: UUID, message_id: MSG, subject: "Booking request: Hill towns", body: "Dear Lantern Air team," },
    { agency_supplier_id: TRIP, supplier_name: "River Boats", category: "transport", line_ids: [UUID], include_names: false, has_email: false,
      reason: "supplier_no_email" },
  ],
};

function setValue(el: HTMLTextAreaElement | HTMLInputElement, value: string) {
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

describe("SupplierRequestsPanel", () => {
  it("prepares one draft per supplier and sends the edited one with its lines", async () => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => {
      root!.render(createElement(ToastProvider, null, createElement(SupplierRequestsPanel, { tripId: TRIP, lang: "en", onClose: () => {} })));
    });
    const button = (text: string) => [...host.querySelectorAll("button")].find((b) => b.textContent === text)!;
    answers = [{ ok: true, data: DRAFTS }];
    await act(async () => button("Prepare e-mails").click());
    expect(sent[0]).toEqual({ method: "POST", path: `trips/${TRIP}/supplier-requests`, body: { include_names: null } });
    expect(host.textContent).toContain("River Boats has no e-mail");
    const body = host.querySelector("textarea") as HTMLTextAreaElement;
    await act(async () => setValue(body, "Dear Lantern Air team, please book."));
    answers = [{ ok: true, data: { status: "queued" } }];
    await act(async () => button("Send to Lantern Air").click());
    expect(sent[1]).toEqual({
      method: "POST",
      path: `trips/${TRIP}/supplier-requests/send`,
      body: { message_id: MSG, subject: "Booking request: Hill towns", body: "Dear Lantern Air team, please book.", line_ids: [LINE] },
    });
    expect(host.textContent).toContain("Requested");
  });
  it("words a refusal", async () => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => {
      root!.render(createElement(ToastProvider, null, createElement(SupplierRequestsPanel, { tripId: TRIP, lang: "en", onClose: () => {} })));
    });
    answers = [{ ok: false, status: 409, detail: "nothing_to_request" }];
    await act(async () => [...host.querySelectorAll("button")].find((b) => b.textContent === "Prepare e-mails")!.click());
    expect(host.querySelector("[data-testid=supplier-requests-error]")?.textContent).toBe("No planning line of your own suppliers to request.");
  });
});
