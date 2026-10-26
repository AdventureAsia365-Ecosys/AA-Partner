// @vitest-environment jsdom
// AA-950 (card CRM-21b) — the client's day-by-day proposal: the link cookie exchange (Fix B11), the pure helpers (D1
// deltas, the hold box of C-04 / C-12 and card R, the notice of a new version), the bell wording, the BFF lines, the
// EN + VI strings, and the page itself (Keep / Swap posts a pick with the cookie, a 4th re-check shows "Ask your agent",
// no net / per-line price reaches the page, test ids on plain elements).
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { matchBff } from "@/lib/bff";
import { bellLine } from "@/lib/bell";
import {
  acceptOpen,
  deltaLabel,
  holdAction,
  holdKey,
  interactiveErrorKey,
  isBuilderPage,
  noticeLine,
  PROPOSAL_API,
  proposalCookie,
  type ClientBuilderPage,
  type HoldBox,
} from "@/lib/clientProposal";
import { clientMessages } from "@/lib/clientMessages";
import { dictionary } from "@/lib/i18n";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const TOKEN = "a".repeat(43);
const ID = "00000000-0000-4000-8000-0000000000d1";

function hold(over: Partial<HoldBox> = {}): HoldBox {
  return { status: "missing", expires_at: null, first_hold: true, rechecks_left: 3, ...over };
}

function page(over: Partial<ClientBuilderPage> = {}): ClientBuilderPage {
  return {
    kind: "builder",
    agency_name: "Sen Travel",
    proposal_id: ID,
    version: 2,
    status: "sent",
    trip: { title: "Nepal family", start_date: "2026-12-01", end_date: "2026-12-05" },
    body: "Welcome",
    currency: "USD",
    total_amount: "1560.00",
    deposit_amount: "468.00",
    balance_amount: "1092.00",
    balance_due_on: "2026-11-01",
    terms: null,
    price_hash: "1".repeat(64),
    accepted_at: null,
    schedule: [],
    price_visibility: "total_with_deltas",
    rooms: [{ type: "family", adults: 2, children: 1 }],
    days: [
      {
        day_no: 1,
        before_you_go: false,
        date: "2026-12-01",
        tz: "Asia/Kathmandu",
        utc_offset: "+05:45",
        title: "Arrive",
        place: "Kathmandu",
        notes: null,
        blocks: [
          {
            option_block_id: "b1",
            title: "Lodging",
            type: "lodging",
            client_may_swap: true,
            choose_later: false,
            choose_later_allowed: false,
            fell_back: false,
            options: [
              { option_id: "o1", title: "Lodge", is_default: true, chosen: true, availability: "agency", confirm_class: "agent", pay_on_arrival: false, delta: "0.00", blocked: null },
              { option_id: "o2", title: "Riverside", is_default: false, chosen: false, availability: "agency", confirm_class: "agent", pay_on_arrival: false, delta: "40.00", blocked: null },
              { option_id: "o3", title: "Palace", is_default: false, chosen: false, availability: "sold_out", confirm_class: "on_payment", pay_on_arrival: false, delta: "300.00", blocked: { reason: "sold_out" } },
            ],
          },
        ],
      },
    ],
    groups: [{ confirm_class: "agent", items: [{ title: "Lodge", choose_later: false }] }],
    totals: { currency: "USD", total: "1560.00", pay_on_arrival_total: "45.00", deposit: "468.00", balance: "1092.00", balance_due_on: "2026-11-01" },
    selection_hash: "2".repeat(64),
    hold: hold(),
    notice: { from_version: 1, changes: [{ kind: "added", title: "Jungle lodge" }, { kind: "days", from: 5, to: 6 }], more: 0, total_from: "1500.00", total_to: "1560.00", picks_carried: 1 },
    ...over,
  };
}

describe("the link cookie (Fix B11)", () => {
  it("is host-only, httpOnly, Secure, Strict and scoped to the API's proposal routes", () => {
    const c = proposalCookie(TOKEN);
    expect(c).toContain(`aa_proposal_link=${TOKEN}`);
    expect(c).toContain(`Path=${PROPOSAL_API}`);
    for (const part of ["HttpOnly", "Secure", "SameSite=Strict", "Max-Age=2592000"]) expect(c).toContain(part);
    expect(c).not.toMatch(/Domain=/i);
  });

  it("the landing exchanges the token and strips it from the URL; a bad token is 404", async () => {
    const { GET } = await import("@/app/c/p/[token]/route");
    const res = await GET(new Request(`https://go.test/c/p/${TOKEN}`), { params: Promise.resolve({ token: TOKEN }) });
    expect(res.status).toBe(303);
    expect(res.headers.get("location")).toBe("/c/p");
    expect(res.headers.get("set-cookie")).toContain(`aa_proposal_link=${TOKEN}`);
    expect(res.headers.get("referrer-policy")).toBe("no-referrer");
    const bad = await GET(new Request("https://go.test/c/p/short"), { params: Promise.resolve({ token: "short" }) });
    expect(bad.status).toBe(404);
    expect(bad.headers.get("set-cookie")).toBeNull();
  });
});

describe("the pure helpers", () => {
  it("D1: Included, a plus or a minus sign, or nothing when only the total is shown", () => {
    expect(deltaLabel("0.00", "USD", "en-AU")).toBe("");
    expect(deltaLabel("40.00", "USD", "en-AU")).toBe("+40.00 USD");
    expect(deltaLabel("-20.50", "USD", "en-AU")).toBe("−20.50 USD");
    expect(deltaLabel("150000", "VND", "en-AU")).toBe("+150,000 VND");
    expect(deltaLabel(null, "USD", "en-AU")).toBeNull();
  });

  it("the hold box: first hold, re-checks, then Ask your agent (card R)", () => {
    expect(holdAction(hold())).toBe("hold");
    expect(holdAction(hold({ status: "active" }))).toBe("none");
    expect(holdAction(hold({ status: "none" }))).toBe("none");
    expect(holdAction(hold({ status: "expired", first_hold: false, rechecks_left: 1 }))).toBe("recheck");
    expect(holdAction(hold({ status: "expired", first_hold: false, rechecks_left: 0 }))).toBe("ask_agent");
    expect(holdAction(hold({ status: "stale", first_hold: false, rechecks_left: 2 }))).toBe("recheck");
    expect(holdKey(hold({ status: "expired", rechecks_left: 0 }))).toBe("client.hold.askAgent");
    expect(holdKey(hold({ status: "active" }))).toBe("client.hold.activeUntil");
    expect(acceptOpen(page({ hold: hold({ status: "active" }) }))).toBe(true);
    expect(acceptOpen(page({ hold: hold({ status: "expired" }) }))).toBe(false);
    expect(acceptOpen(page({ hold: hold({ status: "none" }), status: "accepted" }))).toBe(false);
  });

  it("refusal codes, notice lines, the bell line and the page kind", () => {
    expect(interactiveErrorKey("recheck_limit")).toBe("client.proposalError.recheck_limit");
    expect(interactiveErrorKey("hold_in_progress")).toBe("client.proposalError.hold_in_progress");
    expect(interactiveErrorKey("option_conflict")).toBe("client.proposalError.option_conflict");
    expect(interactiveErrorKey("link_not_found")).toBeNull();
    expect(noticeLine({ kind: "days", from: 5, to: 6 })).toEqual({ key: "client.notice.days", vars: { from: 5, to: 6 } });
    expect(noticeLine({ kind: "added", title: "X" }).key).toBe("client.notice.added");
    expect(bellLine({ trip_id: ID, trip_title: "Nepal", proposal_id: ID, version: 2, name: "proposal.rehold", count: 3, last_at: "", detail: {} })).toEqual({
      key: "bell.event.rehold",
      vars: { trip: "Nepal", version: 2, count: 3 },
    });
    expect(isBuilderPage(page())).toBe(true);
  });
});

describe("strings and BFF lines", () => {
  it("every key the client page and the bell use exists in EN and VI; the client page ships only client.*", () => {
    const en = dictionary("en");
    const vi = dictionary("vi");
    const keys = Object.keys(en).filter((k) => k.startsWith("client.proposalError.") || k.startsWith("client.hold.") || k.startsWith("bell.") || k.startsWith("client.change."));
    expect(keys.length).toBeGreaterThan(25);
    for (const k of keys) expect(vi[k as keyof typeof vi], k).toBeTruthy();
    for (const e of ["viewed", "option_chosen", "change_requested", "hold_placed", "rehold", "recheck_refused", "option_sold_out", "hold_failed", "booking_unavailable", "accepted", "nudge_due"]) {
      expect(en[`bell.event.${e}` as keyof typeof en], e).toBeTruthy();
    }
    const shipped = Object.keys(clientMessages("en"));
    expect(shipped.every((k) => k.startsWith("client.") || k.startsWith("common."))).toBe(true);
    expect(shipped).toContain("client.hold.recheck");
  });

  it("the agency routes of the client side are on the BFF allow-list", () => {
    expect(matchBff("POST", `proposals/${ID}/reissue-link`)?.card).toBe("AA-950");
    expect(matchBff("GET", `proposals/${ID}/client-activity`)?.card).toBe("AA-950");
    expect(matchBff("GET", "bell")?.card).toBe("AA-950");
    expect(matchBff("POST", "bell/seen")?.card).toBe("AA-950");
    expect(matchBff("POST", `proposals/${ID}/send`)?.card).toBe("AA-946");
    expect(matchBff("DELETE", "bell")).toBeNull();
  });
});

describe("the page", () => {
  let root: Root | null = null;
  afterEach(() => {
    act(() => root?.unmount());
    root = null;
    vi.unstubAllGlobals();
  });

  async function mount(p: ClientBuilderPage): Promise<HTMLElement> {
    const { ClientBuilderProposal } = await import("@/components/proposals/ClientBuilderProposal");
    const el = document.createElement("div");
    document.body.appendChild(el);
    root = createRoot(el);
    await act(async () => {
      root!.render(createElement(ClientBuilderProposal, { initial: p, dict: clientMessages("en"), locale: "en-AU" }));
    });
    return el;
  }

  it("renders C-03 with deltas, the notice, the zone, the sold-out reason and no money it must not show", async () => {
    const { ClientBuilderProposal } = await import("@/components/proposals/ClientBuilderProposal");
    const html = renderToString(createElement(ClientBuilderProposal, { initial: page(), dict: clientMessages("en"), locale: "en-AU" }));
    expect(html).toContain('data-testid="client-notice"');
    expect(html).toContain("Added: Jungle lodge");
    expect(html).toContain("+40.00 USD");
    expect(html).toContain("UTC+05:45");
    expect(html).toContain("Sold out");
    expect(html).toContain('data-testid="client-hold" data-status="missing"');
    expect(html).not.toMatch(/net_amount|net_total|margin_total|\bnet\b/i); // CSS "margin" is fine
  });

  it("Keep / Swap posts the pick with the cookie (same-origin) and shows the fresh total", async () => {
    const fresh = page({ total_amount: "1600.00" });
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(fresh), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    const el = await mount(page());
    const swap = el.querySelector<HTMLButtonElement>('[data-title="Riverside"] [data-testid="client-swap"]');
    expect(swap).not.toBeNull();
    await act(async () => swap!.click());
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`${PROPOSAL_API}/pick`);
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("same-origin");
    expect(JSON.parse(String(init.body))).toEqual({ block_id: "b1", option_id: "o2" });
    expect(el.querySelector('[data-testid="client-total"]')?.textContent).toContain("1,600.00");
  });

  it("card R: a 4th re-check shows Ask your agent and offers no button", async () => {
    const refused = page({ hold: hold({ status: "expired", first_hold: false, rechecks_left: 0 }) });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "recheck_limit", page: refused }), { status: 429 })));
    const el = await mount(page({ hold: hold({ status: "expired", first_hold: false, rechecks_left: 1 }) }));
    await act(async () => el.querySelector<HTMLButtonElement>('[data-testid="client-recheck"]')!.click());
    expect(el.querySelector('[data-testid="client-error"]')?.textContent).toContain("Ask your agent");
    expect(el.querySelector('[data-testid="client-ask-agent"]')).not.toBeNull();
    expect(el.querySelector('[data-testid="client-recheck"]')).toBeNull();
    expect(el.querySelector('[data-testid="client-accept-form"]')).toBeNull();
  });

  it("C-04: an active hold opens the grouped accept form with the rooms to confirm and the hold deadline", async () => {
    const el = await mount(page({ hold: hold({ status: "active", expires_at: "2026-11-02T10:00:00+00:00", first_hold: false }) }));
    expect(el.querySelector('[data-testid="client-groups"]')?.textContent).toContain("Your agent books");
    expect(el.querySelector('[data-testid="client-rooms"]')?.textContent).toContain("family room (2 adults, 1 children)");
    expect(el.querySelector('[data-testid="client-held-until"]')).not.toBeNull();
    expect(el.querySelector<HTMLButtonElement>('[data-testid="client-accept"]')!.disabled).toBe(true);
  });
});
