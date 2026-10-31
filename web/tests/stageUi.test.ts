// @vitest-environment jsdom
// AA-943 (CRM-15) web, components: the gate dialog opens on a 409 `stage2_incomplete` and the override is sent; the
// Undo toast offers Undo and is gone at 10 s; the planning form (C-02) shows a branch only when its show_if holds;
// the inquiry form (C-01) brings the first refused field (in page order) into focus.
import { act, createElement, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MESSAGES } from "@/lib/i18n";
import { consentLabel, dataRequestHref, type PublicForm } from "@/lib/intakeForms";
import type { Member, TripView } from "@/lib/pipeline";
import { PAGES } from "./stage2Fixture";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {}, push: () => {} }) }));
const { TripPanel } = await import("@/components/pipeline/TripPanel");
const { UndoToast, toast } = await import("@/components/pipeline/UndoToast");
const { PlanningForm } = await import("@/components/intake/PlanningForm");
const { InquiryForm } = await import("@/components/intake/InquiryForm");
type Submit = (body: Record<string, unknown>) => Promise<import("@/components/intake/PlanningForm").SubmitResult>;

const TRIP = "bf33d09a-3f81-41e4-b261-ebaf18438a5e";
const VIEW: TripView = {
  trip: {
    trip_id: TRIP,
    title: "Vietnam, 9 nights",
    stage: "qualifying",
    stage_entered_at: "2026-10-24T04:02:20Z",
    waiting_on: "client",
    start_date: null,
    end_date: null,
    month: null,
    date_flex_days: 0,
    nights: 9,
    destinations: ["vietnam"],
    party_adults: 2,
    party_child_ages: [],
    party_type: null,
    budget_band: "2000_3500",
    trip_style: null,
    owner_member_id: "m1",
    is_mine: true,
    lead_source: "form",
    lost_reason_code: null,
    last_inquiry_at: "2026-10-24T04:02:20Z",
    created_at: "2026-10-24T04:02:20Z",
    intake_completeness: { s1: 71, s2: 60, s3: 0 },
  },
  party: [],
  tasks: [],
  notes: [],
  history: [],
  submissions: [],
  allowed_moves: ["inquiry", "proposal", "lost"],
  can_cancel: false,
  stage2_link: null,
};
const MEMBERS: Member[] = [{ member_id: "m1", role: "owner", status: "active", is_me: true }];
const DICT = MESSAGES.en;

const roots: Root[] = [];
afterEach(() => {
  for (const r of roots.splice(0)) act(() => r.unmount());
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

async function mount(element: ReturnType<typeof createElement>): Promise<HTMLElement> {
  const container = document.createElement("div");
  document.body.appendChild(container);
  await act(async () => {
    const root = createRoot(container);
    roots.push(root);
    root.render(element);
  });
  return container;
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

async function click(el: Element | null) {
  expect(el).not.toBeNull();
  await act(async () => {
    (el as HTMLElement).click();
  });
}

async function choose(select: HTMLSelectElement, value: string) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(select, value);
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

describe("trip page: gate dialog and Undo", () => {
  it("a 409 stage2_incomplete opens the gate dialog; the override is sent and the move offers Undo", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(json(409, { detail: "stage2_incomplete", s2: 60, missing: ["occasion", "budget_flex"], override_reasons: ["client_in_hurry", "repeat_client", "info_by_phone", "other"] }))
      .mockResolvedValueOnce(
        json(200, { trip_id: TRIP, from_stage: "qualifying", stage: "proposal", waiting_on: "client", allowed_moves: ["qualifying", "booked", "lost"], override_reason_code: "info_by_phone", undo: { history_id: "h-1", undo_until: "2026-10-24T04:02:30Z" } }),
      )
      .mockResolvedValueOnce(json(200, { trip_id: TRIP, stage: "qualifying", waiting_on: "client", allowed_moves: [], dispatch_cancelled: 0, links_revoked: 0, tasks_skipped: 0 }));
    vi.stubGlobal("fetch", fetchMock);
    const c = await mount(createElement(TripPanel, { view: VIEW, members: MEMBERS, role: "owner", lang: "en", locale: "en-AU" }));

    await click(c.querySelector("[data-testid=trip-move-proposal] button"));
    const dialog = c.querySelector("[data-testid=gate-dialog]");
    expect(dialog).not.toBeNull();
    expect(c.querySelector("[data-testid=gate-dialog-body]")!.textContent).toContain("60 %");
    expect(c.querySelector("[data-testid=gate-missing]")!.textContent).toBe("Missing: Is it a special occasion?, How flexible is your budget?");
    const confirm = c.querySelector<HTMLButtonElement>("[data-testid=gate-confirm] button")!;
    expect(confirm.disabled).toBe(true); // a reason first
    await choose(c.querySelector<HTMLSelectElement>("[data-testid=gate-reason]")!, "info_by_phone");
    await click(confirm);

    const [url, init] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(url).toBe(`/bff/trips/${TRIP}/stage`);
    expect(JSON.parse(String(init.body))).toEqual({ to: "proposal", override_reason: "info_by_phone" });
    expect(c.querySelector("[data-testid=gate-dialog]")).toBeNull();
    const toastEl = c.querySelector("[data-testid=undo-toast]")!;
    expect(toastEl.textContent).toContain("Moved to Proposal");

    await click(c.querySelector("[data-testid=undo-toast-undo]"));
    const [undoUrl, undoInit] = fetchMock.mock.calls[2] as [string, RequestInit];
    expect(undoUrl).toBe(`/bff/trips/${TRIP}/stage/undo`);
    expect(JSON.parse(String(undoInit.body))).toEqual({ history_id: "h-1" });
    expect(c.querySelector("[data-testid=undo-toast]")!.textContent).toContain("Move undone");
  });

  it("any other refusal is a message, not a dialog", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json(409, { detail: "transition_not_allowed" })));
    const c = await mount(createElement(TripPanel, { view: VIEW, members: MEMBERS, role: "owner", lang: "en", locale: "en-AU" }));
    await click(c.querySelector("[data-testid=trip-move-proposal] button"));
    expect(c.querySelector("[data-testid=gate-dialog]")).toBeNull();
    expect(c.querySelector("[role=alert]")!.textContent).toContain("not allowed");
  });

  it("shows Cancel trip only when the API allows it and never to an assistant", async () => {
    const booked = { ...VIEW, trip: { ...VIEW.trip, stage: "booked" as const }, allowed_moves: ["pre_departure" as const, "travelling" as const], can_cancel: true };
    const owner = await mount(createElement(TripPanel, { view: booked, members: MEMBERS, role: "owner", lang: "en", locale: "en-AU" }));
    expect(owner.querySelector("[data-testid=trip-cancel]")).not.toBeNull();
    const assistant = await mount(createElement(TripPanel, { view: booked, members: MEMBERS, role: "assistant", lang: "en", locale: "en-AU" }));
    expect(assistant.querySelector("[data-testid=trip-cancel]")).toBeNull();
  });
});

describe("Undo toast", () => {
  it("offers Undo and is gone at 10 s", async () => {
    vi.useFakeTimers();
    const undo = vi.fn();
    function Host() {
      const [state, setState] = useState(() => toast("Moved to Qualifying", { undo }));
      return createElement(UndoToast, { state, lang: "en", onClose: () => setState(null as never) });
    }
    const c = await mount(createElement(Host));
    expect(c.querySelector("[data-testid=undo-toast]")!.getAttribute("data-has-undo")).toBe("true");
    expect(c.querySelector("[data-testid=undo-toast]")!.textContent).toContain("10 s");
    await act(async () => {
      vi.advanceTimersByTime(9_000);
    });
    expect(c.querySelector("[data-testid=undo-toast]")!.textContent).toContain("1 s");
    await act(async () => {
      vi.advanceTimersByTime(1_000);
    });
    expect(c.querySelector("[data-testid=undo-toast]")).toBeNull();
    expect(undo).not.toHaveBeenCalled();
  });
});

describe("planning form (C-02) branches", () => {
  function form(tripStyle: string | null, onSubmit: Submit = vi.fn(async () => ({ ok: true as const }))) {
    return createElement(PlanningForm, { pages: PAGES, tripStyle, initial: { group_pref: "private", unknown: "x" }, agency: "Sen Vàng", mode: "client", dict: DICT, onSubmit });
  }

  it("hides the adventure questions and the health page until their rule holds", async () => {
    const c = await mount(form("relaxed"));
    const root = c.querySelector("[data-testid=plan-form]")!;
    expect(root.getAttribute("data-hydrated")).toBe("true");
    expect(root.getAttribute("data-pages")).toBe("travel_style interests planning health_consent");
    expect(c.querySelector("[data-testid=plan-trek_experience]")).toBeNull();
    expect(c.querySelector<HTMLSelectElement>("[data-testid=plan-group_pref]")!.value).toBe("private"); // prefill
    // To the consent page, tick it: the health page appears.
    for (let i = 0; i < 3; i++) await click(c.querySelector("[data-testid=plan-next] button"));
    expect(root.getAttribute("data-page")).toBe("health_consent");
    await click(c.querySelector("[data-testid=plan-health_consent] input"));
    expect(root.getAttribute("data-pages")).toBe("travel_style interests planning health_consent health");
  });

  it("shows the adventure questions for an adventure trip", async () => {
    const c = await mount(form("adventure"));
    expect(c.querySelector("[data-testid=plan-trek_experience]")).not.toBeNull();
    expect(c.querySelector("[data-testid=plan-altitude_ok]")).not.toBeNull();
  });

  it("a refused answer sends the person back to its page", async () => {
    const onSubmit = vi.fn<Submit>(async () => ({ ok: false, status: 422, detail: "invalid_submission", body: { detail: "invalid_submission", fields: ["interests"] } }));
    const c = await mount(form(null, onSubmit));
    for (let i = 0; i < 3; i++) await click(c.querySelector("[data-testid=plan-next] button"));
    await click(c.querySelector("[data-testid=plan-submit] button"));
    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect((onSubmit.mock.calls[0] as unknown[])[0]).toMatchObject({ group_pref: "private", health_consent: false });
    expect(c.querySelector("[data-testid=plan-form]")!.getAttribute("data-page")).toBe("interests");
    expect(c.querySelector("[data-testid=plan-error]")!.textContent).toBe("Please check the marked answers.");
  });
});

describe("inquiry form (C-01)", () => {
  const FORM: PublicForm = {
    schema_key: "stage1.v1",
    version: 1,
    fields: [
      { name: "destinations", type: "multi_select", required: true, options: ["vietnam"] },
      { name: "nights", type: "integer", required: false },
      { name: "email", type: "email", required: true },
    ],
    pages: [
      { key: "trip", fields: ["destinations", "nights"] },
      { key: "contact", fields: ["email"] },
    ],
    honeypot_field: "website",
    agency_name: "Sen Vàng",
    default_locale: "vi-VN",
    allowed_origins: [],
    privacy_notice_url: null,
    call_slots: ["2026-11-02T02:00:00Z"],
    timezone: "Asia/Ho_Chi_Minh",
    call_minutes: 30,
  };

  it("focuses the first refused field in page order and sends the picked call slot", async () => {
    const fetchMock = vi.fn().mockResolvedValue(json(422, { detail: "invalid_submission", fields: ["email", "nights"] }));
    vi.stubGlobal("fetch", fetchMock);
    const c = await mount(createElement(InquiryForm, { publicKey: "AbCdEfGhIjKlMnOpQrStUv", form: FORM, lang: "en", dict: DICT }));
    const slot = c.querySelector<HTMLSelectElement>("[data-testid=intake-call-slot]")!;
    expect(slot.disabled).toBe(false);
    expect(slot.querySelectorAll("optgroup option")).toHaveLength(1);
    await choose(slot, "2026-11-02T02:00:00Z");
    c.querySelector<HTMLInputElement>("[data-testid=intake-consent]")!.checked = true;
    await act(async () => {
      c.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });
    const body = JSON.parse(String((fetchMock.mock.calls[0] as [string, RequestInit])[1].body));
    expect(body.call_slot).toBe("2026-11-02T02:00:00Z");
    expect(document.activeElement?.getAttribute("name")).toBe("nights");
    expect(c.querySelector("[data-testid=intake-error]")!.textContent).toBe("Please check this answer.");
  });

  it("uses the default consent wording and shows no data-request line when the agency set none", async () => {
    const c = await mount(createElement(InquiryForm, { publicKey: "AbCdEfGhIjKlMnOpQrStUv", form: FORM, lang: "en", dict: DICT }));
    expect(c.querySelector("[data-testid=intake-consent-text]")!.textContent).toBe("I agree that Sen Vàng may contact me about this trip.");
    expect(c.querySelector("[data-testid=intake-data-requests]")).toBeNull();
  });

  it("shows the agency's controller settings: its consent wording, privacy link and data-request contact (AA-962)", async () => {
    const own: PublicForm = {
      ...FORM,
      privacy_notice_url: "https://agency.example/privacy",
      consent_text: "Sen Vàng may contact me about my trip by e-mail or phone.",
      data_request_contact: "privacy@agency.example",
    };
    const c = await mount(createElement(InquiryForm, { publicKey: "AbCdEfGhIjKlMnOpQrStUv", form: own, lang: "en", dict: DICT }));
    expect(c.querySelector("[data-testid=intake-consent-text]")!.textContent).toBe(own.consent_text);
    expect(c.querySelector<HTMLAnchorElement>("a[href='https://agency.example/privacy']")).not.toBeNull();
    const line = c.querySelector("[data-testid=intake-data-requests]")!;
    expect(line.querySelector("a")!.getAttribute("href")).toBe("mailto:privacy@agency.example");
  });
});

describe("controller settings helpers (AA-962)", () => {
  it("picks the consent wording and builds the data-request link", () => {
    expect(consentLabel({ consent_text: "  " }, "default")).toBe("default");
    expect(consentLabel({ consent_text: null }, "default")).toBe("default");
    expect(consentLabel({ consent_text: "Own text" }, "default")).toBe("Own text");
    expect(dataRequestHref("privacy@agency.example")).toBe("mailto:privacy@agency.example");
    expect(dataRequestHref("https://agency.example/requests")).toBe("https://agency.example/requests");
    expect(dataRequestHref("javascript:alert(1)")).toBeNull();
    expect(dataRequestHref("Ring the front desk")).toBeNull();
  });
});
