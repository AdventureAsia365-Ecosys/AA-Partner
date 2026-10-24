// @vitest-environment jsdom
// AA-936 E2E run 40060691620: the board card is a plain link, so the trip page is a full server render. The spec
// clicked "Show email" on the server HTML before React hydrated it; the click did nothing and no reveal request reached
// the API (CRM API log: GET /crm/v1/trips/<id> then nothing). The action buttons now stay disabled until hydration and
// the root says data-hydrated="true" once handlers are attached; after hydration a click reveals the e-mail.
// AA-937 (CRM-09): Reveal first asks why; Show posts one reveal with the reason.
import { act, createElement } from "react";
import { hydrateRoot, type Root } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Member, TripView } from "@/lib/pipeline";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {} }) }));
const { TripPanel } = await import("@/components/pipeline/TripPanel");

const TRAVELER = "6c950969-ae80-4258-ab68-6ed80f2bbe71";
const VIEW: TripView = {
  trip: {
    trip_id: "bf33d09a-3f81-41e4-b261-ebaf18438a5e",
    title: "Vietnam, 9 nights",
    stage: "inquiry",
    stage_entered_at: "2026-10-24T04:02:20Z",
    waiting_on: "agency",
    start_date: null,
    end_date: null,
    month: null,
    date_flex_days: 0,
    nights: 9,
    destinations: ["vietnam"],
    party_adults: 1,
    party_child_ages: [],
    party_type: null,
    budget_band: "2000_3500",
    trip_style: null,
    owner_member_id: null,
    is_mine: true,
    lead_source: "form",
    lost_reason_code: null,
    last_inquiry_at: "2026-10-24T04:02:20Z",
    created_at: "2026-10-24T04:02:20Z",
  },
  party: [{ traveler_id: TRAVELER, role: "lead", display_name: "E2E run Prospect", preferred_channel: null, has_email: true, has_phone: false }],
  tasks: [{ task_id: "t1", kind: "reply_inquiry", assignee_member_id: null, status: "open", due_at: null, done_at: null }],
  notes: [],
  history: [],
  submissions: [],
  allowed_moves: ["qualifying", "lost"],
};
const MEMBERS: Member[] = [{ member_id: "m1", role: "owner", status: "active", is_me: true }];

const roots: Root[] = [];
afterEach(() => {
  for (const r of roots.splice(0)) act(() => r.unmount());
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

const panel = () => createElement(TripPanel, { view: VIEW, members: MEMBERS, role: "owner", lang: "en", locale: "en-AU" });

describe("trip workspace before and after hydration", () => {
  it("server HTML: action buttons disabled, no hydrated marker, stable test ids, English labels", () => {
    const container = document.createElement("div");
    container.innerHTML = renderToString(panel());
    const root = container.querySelector("[data-testid=crm-trip]")!;
    expect(root.getAttribute("data-hydrated")).toBeNull();
    expect(container.querySelector<HTMLButtonElement>("[data-testid=show-email] button")!.disabled).toBe(true);
    expect(container.querySelector<HTMLButtonElement>("[data-testid=trip-move-qualifying] button")!.disabled).toBe(true);
    expect(container.querySelector("[data-testid=trip-stage]")!.getAttribute("data-stage")).toBe("inquiry");
    expect(container.querySelector('[data-testid=trip-task][data-kind=reply_inquiry][data-status=open]')!.textContent).toContain("Reply within 2 h");
  });

  it("hydrated: marker set, Reveal -> reason -> Show posts one reveal with the reason and shows the address", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ value: "e2e.prospect@example.invalid" }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const container = document.createElement("div");
    container.innerHTML = renderToString(panel());
    document.body.appendChild(container);
    await act(async () => {
      roots.push(hydrateRoot(container, panel()));
    });
    expect(container.querySelector("[data-testid=crm-trip]")!.getAttribute("data-hydrated")).toBe("true");
    const button = container.querySelector<HTMLButtonElement>("[data-testid=show-email] button")!;
    expect(button.disabled).toBe(false);
    await act(async () => {
      button.click();
    });
    expect(fetchMock).not.toHaveBeenCalled(); // Reveal only asks why
    const select = container.querySelector<HTMLSelectElement>("[data-testid=reveal-reason-email]")!;
    const show = container.querySelector<HTMLButtonElement>("[data-testid=reveal-show-email] button")!;
    expect(show.disabled).toBe(true); // no reason yet
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(select, "contact_client");
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(show.disabled).toBe(false);
    await act(async () => {
      show.click();
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(String(url)).toBe(`/bff/travelers/${TRAVELER}/reveal`);
    expect(JSON.parse(String(init.body))).toEqual({ field: "email", reason: "contact_client" });
    expect(container.querySelector("[data-testid=revealed-email]")!.textContent).toBe("e2e.prospect@example.invalid");
  });
});
