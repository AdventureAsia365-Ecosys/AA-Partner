// @vitest-environment jsdom
// AA-968 (card CRM-36) web: the BFF allow-list of the campaign routes; the words of the states, holds, content rules
// and left-out reasons in both languages; the payload and the schedule time; Marketing > Campaigns (a plan without the
// feature reads, a stopped campaign says why, a draft is saved, checked and scheduled with the previewed count); the
// public unsubscribe page (unsubscribe, subscribe again, a dead link). Invented names only.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { matchBff } from "@/lib/bff";
import {
  campaignErrorKey,
  cancellable,
  editable,
  emptyDraft,
  excludedText,
  gateText,
  holdText,
  payloadOf,
  previewSummary,
  scheduleAt,
  GATE_CODES,
  HOLD_CODES,
  UNSUBSCRIBE_TOKEN,
  type CampaignList,
  type CampaignOptions,
  type Preview,
} from "@/lib/campaigns";
import { MESSAGES } from "@/lib/i18n";
import { unsubscribeMessages } from "@/lib/unsubscribeMessages";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn() }) }));
const { CampaignsPanel } = await import("@/components/campaigns/CampaignsPanel");
const { UnsubscribePage } = await import("@/components/campaigns/UnsubscribePage");

const ID = "00000000-0000-4000-8000-0000000c4a01";
const MAILBOX = "00000000-0000-4000-8000-0000000c4a02";
const TOKEN = "a".repeat(43);
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

async function click(el: Element | null | undefined) {
  expect(el).toBeTruthy();
  await act(async () => (el as HTMLButtonElement).click());
  await act(async () => {
    await new Promise((r) => setTimeout(r, 0));
  });
}

function buttonByText(c: HTMLElement, text: string): HTMLButtonElement | undefined {
  return [...c.querySelectorAll("button")].find((b) => b.textContent?.trim() === text) as HTMLButtonElement | undefined;
}

function options(over: Partial<CampaignOptions> = {}): CampaignOptions {
  return {
    feature_allowed: true,
    mailboxes: [{ channel_account_id: MAILBOX, scope: "agency", kind: "gmail", status: "connected", address: "hello@agency.example.test" }],
    tags: [{ code: "repeat_client", name: "Repeat client", kind: "agency" }],
    blocks: [],
    daily_limit: 400,
    max_recipients: 2000,
    client_origin: "https://go.crm.example.test",
    tripplanner_origin: null,
    ...over,
  };
}

function list(over: Partial<CampaignList> = {}): CampaignList {
  return {
    feature_allowed: true,
    can_send: true,
    items: [
      {
        campaign_id: ID,
        name: "Spring river valley",
        state: "held",
        hold_code: "aa_tour_retired",
        gate_codes: ["aa_tour_not_served"],
        scheduled_at: null,
        started_at: null,
        finished_at: null,
        recipient_count: 4,
        excluded: {},
        counts: { planned: 2, queued: 2, skipped: 0, sent: 2, unsubscribed: 1 },
        version: 3,
        created_at: null,
      },
    ],
    ...over,
  };
}

describe("campaign words and helpers", () => {
  it("lets only the campaign routes through the BFF", () => {
    for (const [m, p] of [
      ["GET", "campaigns"],
      ["GET", "campaigns/options"],
      ["GET", "campaigns/tours"],
      ["GET", `campaigns/blocks/${ID}`],
      ["POST", "campaigns"],
      ["GET", `campaigns/${ID}`],
      ["PATCH", `campaigns/${ID}`],
      ["DELETE", `campaigns/${ID}`],
      ["POST", `campaigns/${ID}/preview`],
      ["POST", `campaigns/${ID}/schedule`],
      ["POST", `campaigns/${ID}/cancel`],
    ] as const) expect(matchBff(m, p), `${m} ${p}`).not.toBeNull();
    expect(matchBff("POST", `campaigns/${ID}/send`)).toBeNull();
    expect(matchBff("DELETE", "campaigns")).toBeNull();
    expect(matchBff("PUT", `campaigns/${ID}`)).toBeNull();
  });

  it("words every code in both languages", () => {
    for (const lang of ["en", "vi"] as const) {
      for (const c of GATE_CODES) expect(gateText(lang, c)).not.toContain("camp.");
      for (const c of HOLD_CODES) expect(holdText(lang, c)).not.toContain("camp.");
      expect(MESSAGES[lang]["unsub.button"]).toBeTruthy();
    }
    expect(gateText("en", "unknown_code")).toBe("unknown_code");
    expect(holdText("en", "something_new")).toContain("Stopped");
    expect(holdText("en", null)).toBe("");
    expect(campaignErrorKey("count_changed")).toBe("camp.err.count_changed");
    expect(campaignErrorKey("boom")).toBe("camp.err.generic");
    expect(excludedText("en", { no_consent: 2, suppressed: 0, no_email: 1 })).toEqual([
      "2 left out: no yes to news",
      "1 left out: no e-mail address",
    ]);
  });

  it("summarises a preview and builds the payload", () => {
    const p: Preview = { gate_codes: [], eligible: 900, excluded: {}, already_done: 0, daily_limit: 400, max_recipients: 2000, over_cap: false, days: 3, has_mailbox: true, feature_allowed: true };
    expect(previewSummary("en", p)).toBe("900 clients get it over 3 days (your mailbox sends up to 400 a day).");
    expect(previewSummary("en", { ...p, eligible: 3, days: 1 })).toContain("today");
    expect(previewSummary("en", { ...p, eligible: 0 })).toContain("nobody");
    expect(payloadOf({ ...emptyDraft(), name: "  Spring ", channel_account_id: "" })).toMatchObject({ name: "Spring", channel_account_id: null });
    expect(scheduleAt("")).toBeNull();
    expect(scheduleAt("not a date")).toBeNull();
    expect(scheduleAt("2027-03-01T08:30")).toMatch(/^2027-03-0[12]T/);
    expect(editable("held") && editable("draft") && !editable("sent")).toBe(true);
    expect(cancellable("sending") && !cancellable("sent")).toBe(true);
    expect(UNSUBSCRIBE_TOKEN.test(TOKEN) && !UNSUBSCRIBE_TOKEN.test("short")).toBe(true);
    expect(Object.keys(unsubscribeMessages("vi")).every((k) => k.startsWith("unsub."))).toBe(true);
  });
});

describe("Marketing > Campaigns", () => {
  it("reads without the feature and offers no writes", async () => {
    const c = await mount(createElement(CampaignsPanel, { lang: "en", initial: list({ feature_allowed: false }), options: options({ feature_allowed: false }) }));
    expect(c.textContent).toContain("not on your agency's plan");
    expect(buttonByText(c, "New campaign")).toBeUndefined();
    expect(buttonByText(c, "Edit")).toBeUndefined();
  });

  it("says why a campaign stopped and shows its counts", async () => {
    const c = await mount(createElement(CampaignsPanel, { lang: "en", initial: list(), options: options() }));
    const row = c.querySelector('[data-testid="campaign-row"]');
    expect(row?.getAttribute("data-state")).toBe("held");
    expect(row?.textContent).toContain("no longer offered");
    expect(row?.textContent).toContain("1 unsubscribed");
  });

  it("saves a draft, checks it, then schedules it with the previewed count", async () => {
    const calls: { method: string; url: string; body: unknown }[] = [];
    vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
      const body = init.body ? JSON.parse(String(init.body)) : null;
      calls.push({ method: String(init.method), url: String(url), body });
      if (url === "/bff/campaigns" && init.method === "POST") return new Response(JSON.stringify({ campaign_id: ID, gate_codes: [], version: 1, state: "draft" }), { status: 201 });
      if (String(url).endsWith("/preview")) return new Response(JSON.stringify({ gate_codes: [], eligible: 5, excluded: { no_consent: 2 }, already_done: 0, daily_limit: 2, max_recipients: 2000, over_cap: false, days: 3, has_mailbox: true, feature_allowed: true }), { status: 200 });
      if (url === `/bff/campaigns/${ID}` && init.method === "PATCH") return new Response(JSON.stringify({ campaign_id: ID, gate_codes: [], version: 2, state: "draft" }), { status: 200 });
      if (String(url).endsWith("/schedule")) return new Response(JSON.stringify({ eligible: 5, days: 3 }), { status: 200 });
      if (url === "/bff/campaigns") return new Response(JSON.stringify(list({ items: [] })), { status: 200 });
      return new Response("{}", { status: 200 });
    });
    const c = await mount(createElement(CampaignsPanel, { lang: "en", initial: list({ items: [] }), options: options() }));
    await click(buttonByText(c, "New campaign"));
    expect(c.querySelector('[data-testid="campaign-editor"]')).toBeTruthy();
    await click(buttonByText(c, "Save draft"));
    expect(calls.find((x) => x.method === "POST" && x.url === "/bff/campaigns")).toBeTruthy();
    await click(buttonByText(c, "Check who gets it"));
    expect(c.querySelector('[data-testid="campaign-preview"]')?.textContent).toContain("5 clients get it over 3 days");
    expect(c.querySelector('[data-testid="campaign-preview"]')?.textContent).toContain("2 left out: no yes to news");
    await click(buttonByText(c, "Schedule"));
    const scheduled = calls.find((x) => x.url.endsWith("/schedule"));
    expect(scheduled?.body).toEqual({ scheduled_at: null, expected: 5, version: 2 });
    expect(c.textContent).toContain("Scheduled: 5 clients over 3 day(s).");
  });

  it("shows the content rules a draft still breaks and the API's refusal", async () => {
    vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
      if (url === "/bff/campaigns" && init.method === "POST") return new Response(JSON.stringify({ campaign_id: ID, gate_codes: ["link_not_allowed"], version: 1, state: "draft" }), { status: 201 });
      if (String(url).endsWith("/preview")) return new Response(JSON.stringify({ detail: "feature_not_allowed" }), { status: 403 });
      return new Response(JSON.stringify(list({ items: [] })), { status: 200 });
    });
    const c = await mount(createElement(CampaignsPanel, { lang: "en", initial: list({ items: [] }), options: options() }));
    await click(buttonByText(c, "New campaign"));
    await click(buttonByText(c, "Check who gets it"));
    expect(c.querySelector('[data-testid="campaign-gate"] [data-code="link_not_allowed"]')?.textContent).toContain("Adventure Asia tour pages");
    expect(c.querySelector('[role="alert"]')?.textContent).toContain("not on your agency's plan");
  });

  it("cancels a campaign after a confirm", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
      calls.push(`${init.method} ${url} ${init.body ?? ""}`);
      if (String(url).endsWith("/cancel")) return new Response(JSON.stringify({ state: "cancelled" }), { status: 200 });
      return new Response(JSON.stringify(list({ items: [] })), { status: 200 });
    });
    const c = await mount(createElement(CampaignsPanel, { lang: "en", initial: list(), options: options() }));
    await click(buttonByText(c, "Cancel campaign"));
    const dialog = document.querySelector('[data-testid="confirm-dialog"]') as HTMLElement;
    await click([...dialog.querySelectorAll("button")].find((b) => b.textContent?.trim() === "Cancel campaign"));
    expect(calls).toContain(`POST /bff/campaigns/${ID}/cancel {"version":3}`);
  });
});

describe("the public unsubscribe page", () => {
  it("unsubscribes, then subscribes again", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", async (url: string, init: RequestInit = {}) => {
      calls.push(`${init.method ?? "GET"} ${url}`);
      const subscribed = !(init.method === "POST" && !String(url).endsWith("/resubscribe"));
      return new Response(JSON.stringify({ agency: "Sen Vang Travel", subscribed }), { status: 200 });
    });
    const c = await mount(createElement(UnsubscribePage, { token: TOKEN, dict: unsubscribeMessages("en") }));
    expect(c.querySelector('[data-testid="unsubscribe-state"]')?.textContent).toBe("You get news by e-mail from Sen Vang Travel.");
    await click(buttonByText(c, "Unsubscribe"));
    expect(c.querySelector('[data-testid="unsubscribe-state"]')?.getAttribute("data-subscribed")).toBe("no");
    await click(buttonByText(c, "Subscribe again"));
    expect(c.querySelector('[data-testid="unsubscribe-state"]')?.getAttribute("data-subscribed")).toBe("yes");
    expect(calls).toEqual([
      `GET /crm/public/v1/unsubscribe/${TOKEN}`,
      `POST /crm/public/v1/unsubscribe/${TOKEN}`,
      `POST /crm/public/v1/unsubscribe/${TOKEN}/resubscribe`,
    ]);
  });

  it("says a dead link is not valid", async () => {
    vi.stubGlobal("fetch", async () => new Response(JSON.stringify({ detail: "link_not_found" }), { status: 404 }));
    const c = await mount(createElement(UnsubscribePage, { token: TOKEN, dict: unsubscribeMessages("vi") }));
    expect(c.querySelector('[role="alert"]')?.textContent).toBe("Đường dẫn này không còn hiệu lực.");
  });
});
