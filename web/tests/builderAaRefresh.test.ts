// @vitest-environment jsdom
// AA-949 E2E run 40262333536: after an AA option was added, the spec's getByTestId("builder-aa-refresh").click() waited
// 545 s. The page did render the button, but the kit Button (a byte copy of CIS app/_kit, drift-guarded) forwards no
// data-* props, so the data-testid it was given never reached the DOM. The test id now sits on a wrapper span (as
// show-email in TripPanel); this pins it: present on a draft with an AA option, disabled until hydration, a click posts
// one aa-refresh with the page's seq and shows "AA prices checked".
import { act, createElement } from "react";
import { hydrateRoot, type Root } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { BuilderOption, BuilderView } from "@/lib/builder";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {}, push: () => {} }) }));
const { BuilderPage } = await import("@/components/proposals/BuilderPage");

const P = "00000000-0000-4000-8000-0000000000c1";
const X = "00000000-0000-4000-8000-0000000000c2";

function option(source: "aa" | "agency"): BuilderOption {
  return {
    option_id: `o-${source}`,
    source,
    title: source === "aa" ? "AA lodge" : "Homestay",
    tier: null,
    agency_supplier_id: null,
    sell_amount: "1500.00",
    sell_delta: "0.00",
    net_amount: "900.00",
    pay_on_arrival: false,
    choose_later_allowed: false,
    availability: source === "aa" ? "instant" : "agency",
    availability_checked_at: null,
    is_default: true,
    picked: false,
    ...(source === "aa"
      ? {
          aa: {
            tour_source_id: "t1",
            component_id: "c1",
            component_kind: "trip_plan",
            departure_id: null,
            nights: null,
            unpriced: false,
            quote_valid_until: null,
            quote_expired: false,
            from_price: null,
            below_from_price: null,
            no_longer_offered: false,
          },
        }
      : {}),
  };
}

function view(source: "aa" | "agency", status: BuilderView["status"] = "draft"): BuilderView {
  return {
    proposal_id: P,
    trip_id: X,
    trip_stage: "proposal",
    version: 1,
    status,
    kind: "builder",
    edit_seq: 7,
    selection_seq: 0,
    currency: "USD",
    price_visibility: "total_with_deltas",
    room_config: [{ type: "double", adults: 2, children: 0 }],
    deposit_pct: "30.00",
    starts_on: "2026-12-01",
    balance_due_on: null,
    terms_doc_id: null,
    intro: "",
    price_hash: null,
    published_at: null,
    based_on: null,
    days: [
      {
        proposal_day_id: "d1",
        day_no: 1,
        before_you_go: false,
        date: "2026-12-01",
        title: null,
        place: null,
        tz: "Asia/Kathmandu",
        utc_offset: "+05:45",
        notes: null,
        blocks: [
          {
            option_block_id: "b1",
            title: "Lodging",
            type: "lodging",
            sort_order: 0,
            client_may_swap: true,
            default_option_id: `o-${source}`,
            default_pinned: false,
            options: [option(source)],
          },
        ],
      },
    ],
    rules: [],
    picks: [],
    totals: { currency: "USD", sell_total: "1500.00", net_total: "900.00", margin_total: "600.00", pay_on_arrival_total: "0.00", deposit: "450.00", balance: "1050.00" },
    checks: { publishable: true, blocking: [], warnings: [] },
    stage2: { accommodation_tier: null, group_pref: null, pace: null, party_adults: 2, party_children: 0 },
  };
}

const page = (v: BuilderView) => createElement(BuilderPage, { initial: v, role: "owner", lang: "en", locale: "en-AU", terms: [], suppliers: [] });

const roots: Root[] = [];
afterEach(() => {
  for (const r of roots.splice(0)) act(() => r.unmount());
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

describe("Refresh AA prices button (AA-949)", () => {
  it("server HTML: the test id reaches the DOM on a draft with an AA option; the button is disabled before hydration", () => {
    const container = document.createElement("div");
    container.innerHTML = renderToString(page(view("aa")));
    const button = container.querySelector<HTMLButtonElement>("[data-testid=builder-aa-refresh] button");
    expect(button, "the builder-aa-refresh test id must reach the DOM").not.toBeNull();
    expect(button!.textContent).toBe("Refresh AA prices");
    expect(button!.disabled).toBe(true);
  });

  it("absent without an AA option or on a published version", () => {
    for (const v of [view("agency"), view("aa", "published")]) {
      const container = document.createElement("div");
      container.innerHTML = renderToString(page(v));
      expect(container.querySelector("[data-testid=builder-aa-refresh]")).toBeNull();
    }
  });

  it("hydrated: enabled; a click posts one aa-refresh with the seq and says how many prices it checked", async () => {
    const answer = { ...view("aa"), edit_seq: 8, refresh: { quoted: 1, failed: [], skipped: 0 } };
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(answer), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const container = document.createElement("div");
    container.innerHTML = renderToString(page(view("aa")));
    document.body.appendChild(container);
    await act(async () => {
      roots.push(hydrateRoot(container, page(view("aa"))));
    });
    expect(container.querySelector("[data-testid=builder]")!.getAttribute("data-hydrated")).toBe("true");
    const button = container.querySelector<HTMLButtonElement>("[data-testid=builder-aa-refresh] button")!;
    expect(button.disabled).toBe(false);
    await act(async () => {
      button.click();
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(String(url)).toBe(`/bff/proposals/${P}/aa-refresh`);
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ seq: 7 });
    expect(container.querySelector("[data-testid=builder-message]")!.textContent).toMatch(/AA prices checked/);
    expect(container.querySelector("[data-testid=builder]")!.getAttribute("data-seq")).toBe("8");
  });
});
