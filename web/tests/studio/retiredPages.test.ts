// @vitest-environment jsdom
// AA-1039 follow-up (tech lead decision S276) — a retired tour changes nothing on the agency's site by default.
// Publish lists the live posts about it (RetiredPagesList), the bell's notice lists the WordPress pages
// (ReviewPagesNote), each with one "Move to draft"; a click sends ONE POST to the CIS move-to-draft route through the
// tenant BFF and nothing is called before it. Settings has the owner-only opt-in to automatic drafts after N days
// (TourRetirementSettingsCard). Fake agency names and sites only.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RetiredPagesList } from "@/app/(studio)/portal/_components/RetiredPagesList";
import { ReviewPagesNote, type TenantNotification } from "@/app/(studio)/portal/_components/NotificationsBell";
import {
  TourRetirementSettingsCard, TourRetirementSettingsForm,
} from "@/app/(studio)/portal/_components/TourRetirementSettingsCard";
import { RETIRED_PAGES_MESSAGES } from "@/app/(studio)/portal/_components/retiredPages.messages";
import {
  moveOutcomeText, parseDays, type MoveToDraftResult, type TourRetirementSettings,
} from "@/app/(studio)/portal/_components/retiredPagesApi";
import { missingTranslations, translator } from "@/app/_kit/core/i18n";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
window.matchMedia ??= ((query: string) => ({
  matches: false, media: query, onchange: null,
  addEventListener: () => {}, removeEventListener: () => {}, addListener: () => {}, removeListener: () => {},
  dispatchEvent: () => false,
})) as unknown as typeof window.matchMedia;

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: unknown }) =>
    createElement("a", { href, ...rest }, children as never),
}));

const roots: Root[] = [];
afterEach(() => {
  for (const r of roots.splice(0)) act(() => r.unmount());
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

const WP = {
  publish_id: "p-1", piece_id: "c-1", title: "Lanterns over Copperfield Cove", channel: "blog",
  external_url: "https://heron-trails.example/lanterns-copperfield/", tour_name: "Copperfield Cove Ramble",
  can_move_to_draft: true, draft_due_at: null, flagged_at: "2026-11-10T08:00:00+00:00",
};
const FB = {
  ...WP, publish_id: "p-2", piece_id: "c-2", title: "Copperfield Cove at dusk", channel: "facebook",
  external_url: "https://social.example/heron-trails/posts/2", can_move_to_draft: false,
};
const result = (over: Partial<MoveToDraftResult> = {}): MoveToDraftResult => ({
  publish_id: "p-1", outcome: "drafted", status: "unpublished", external_url: WP.external_url, replayed: false,
  message: "The page is now a draft on WordPress.", ...over,
});
const SETTINGS: TourRetirementSettings = {
  auto_draft: false, notice_days: 7, min_days: 1, max_days: 30, default_notice_days: 7,
};

type Call = { url: string; method: string; body?: string };

function stubApi(routes: Record<string, (init?: RequestInit) => Response>) {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    calls.push({ url, method, body: typeof init?.body === "string" ? init.body : undefined });
    const handler = routes[`${method} ${url}`];
    return handler ? handler(init) : Response.json({}, { status: 404 });
  }));
  return calls;
}

async function mount(el: ReturnType<typeof createElement>) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => {
    const root = createRoot(container);
    roots.push(root);
    root.render(createElement(QueryClientProvider, { client: qc }, el));
  });
  await act(async () => { await new Promise((r) => setTimeout(r, 20)); });
  return {
    q: (s: string) => container.querySelector(s) as HTMLElement | null,
    text: () => document.body.textContent ?? "",
  };
}

async function click(el: HTMLElement | null) {
  expect(el).not.toBeNull();
  await act(async () => { el!.click(); await new Promise((r) => setTimeout(r, 20)); });
}

async function type(el: HTMLElement | null, value: string) {
  expect(el).not.toBeNull();
  const input = el as HTMLInputElement;
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
  await act(async () => {
    setter.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    await new Promise((r) => setTimeout(r, 10));
  });
}

const LIST = "GET /api/tenant/v1/publish-log/retired-pages";
const MOVE = "POST /api/tenant/v1/publish-log/p-1/move-to-draft";

describe("Publish › Posts about tours no longer offered", () => {
  it("lists the live posts, says the site is unchanged, and drafts a page with one POST only after the click", async () => {
    const calls = stubApi({ [LIST]: () => Response.json({ data: [WP, FB], total: 2 }), [MOVE]: () => Response.json(result()) });
    const { q, text } = await mount(createElement(RetiredPagesList));
    expect(q("[data-testid=retired-pages-list]")).not.toBeNull();
    expect(q("#retired-pages")).not.toBeNull();
    expect(text()).toContain("Lanterns over Copperfield Cove");
    expect(text()).toContain("Your posts stay online");
    expect(text()).toContain("Tour: Copperfield Cove Ramble");
    // Facebook: no draft button, an instruction instead
    expect(q("[data-testid=move-to-draft-button-p-2]")).toBeNull();
    expect(text()).toContain("Edit or remove this post on Facebook.");
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(0);
    await click(q("[data-testid=move-to-draft-button-p-1]"));
    expect(calls.filter((c) => c.method === "POST").map((c) => c.url))
      .toEqual(["/api/tenant/v1/publish-log/p-1/move-to-draft"]);
    expect(text()).toContain("The page is now a draft on WordPress.");
  });

  it("shows the automatic draft date only for a page of an agency that turned it on", async () => {
    stubApi({ [LIST]: () => Response.json({ data: [{ ...WP, draft_due_at: "2026-11-17T08:00:00+00:00" }], total: 1 }) });
    const { q } = await mount(createElement(RetiredPagesList));
    expect(q("[data-testid=retired-page-due-p-1]")?.textContent).toContain("Moves to draft on");
  });

  it("shows the API's reason when the click is refused", async () => {
    stubApi({
      [LIST]: () => Response.json({ data: [WP], total: 1 }),
      [MOVE]: () => Response.json({ detail: "This tour is back on sale, so the page stays online." }, { status: 409 }),
    });
    const { q, text } = await mount(createElement(RetiredPagesList));
    await click(q("[data-testid=move-to-draft-button-p-1]"));
    expect(text()).toContain("back on sale");
  });

  it("renders nothing for an empty list or a refused read", async () => {
    stubApi({ [LIST]: () => Response.json({ data: [], total: 0 }) });
    const empty = await mount(createElement(RetiredPagesList));
    expect(empty.q("[data-testid=retired-pages-list]")).toBeNull();
    stubApi({ [LIST]: () => Response.json({ detail: "View as tenant is read-only" }, { status: 403 }) });
    const refused = await mount(createElement(RetiredPagesList));
    expect(refused.q("[data-testid=retired-pages-list]")).toBeNull();
  });
});

describe("Bell › pages about a tour no longer offered", () => {
  const note: TenantNotification = {
    id: 11, event_type: "tour.pages_to_review", title: "Pages about a tour no longer offered",
    message: "server sentence", tour_id: "t-1", tour_name: "Copperfield Cove Ramble", reason: null, live_posts: 0,
    is_read: false, created_at: null, pages: [{ publish_id: "p-1", url: WP.external_url, draft_on: null }],
  };

  it("offers the three choices, never says AA changes the site, and drafts with one click", async () => {
    const calls = stubApi({ [MOVE]: () => Response.json(result({ outcome: "already_drafted", replayed: true })) });
    const { q, text } = await mount(createElement(ReviewPagesNote, { note }));
    expect(text()).toContain("still online");
    expect(text()).toContain("move it to draft with one click, or keep it");
    expect(text()).not.toContain("will be moved");
    expect(q("a")?.getAttribute("href")).toBe("/portal/content/publish#retired-pages");
    expect(calls).toHaveLength(0);
    await click(q("[data-testid=bell-move-to-draft-button-p-1]"));
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(1);
    expect(q("[data-testid=bell-move-to-draft-button-p-1]")).toBeNull();
    expect(q("[data-testid=bell-review-result-p-1]")?.textContent).toBe("The page is already a draft on WordPress.");
  });

  it("mentions the date only when a page has one (automatic draft on)", async () => {
    stubApi({});
    const dated = { ...note, pages: [{ publish_id: "p-1", url: WP.external_url, draft_on: "17 Nov 2026" }] };
    const { text } = await mount(createElement(ReviewPagesNote, { note: dated }));
    expect(text()).toContain("17 Nov 2026");
    expect(text()).toContain("as set in your Settings");
  });
});

describe("Settings › automatic drafts of posts about a discontinued tour", () => {
  it("is off by default and the owner turns it on with a number of days (one PATCH)", async () => {
    const calls = stubApi({
      "PATCH /api/tenant/v1/account/tour-retirement": (init) =>
        Response.json({ ...SETTINGS, ...JSON.parse(String(init?.body)) }),
    });
    const { q, text } = await mount(createElement(TourRetirementSettingsForm, { settings: SETTINGS, isOwner: true }));
    const box = q("[data-testid=tour-retirement-auto]") as HTMLInputElement;
    expect(box.checked).toBe(false);
    expect(text()).toContain("Off: you are told, and your site is left as it is.");
    expect((q("[data-testid=tour-retirement-save]") as HTMLButtonElement).disabled).toBe(true);
    await click(box);
    await type(q("[data-testid=tour-retirement-days]"), "10");
    await click(q("[data-testid=tour-retirement-save]"));
    const patches = calls.filter((c) => c.method === "PATCH");
    expect(patches).toHaveLength(1);
    expect(JSON.parse(patches[0].body!)).toEqual({ auto_draft: true, notice_days: 10 });
    expect(q("[data-testid=tour-retirement-note]")?.textContent).toBe("Setting saved.");
  });

  it("refuses days outside 1-30 before any call", async () => {
    const calls = stubApi({});
    const { q, text } = await mount(createElement(TourRetirementSettingsForm,
      { settings: { ...SETTINGS, auto_draft: true }, isOwner: true }));
    await type(q("[data-testid=tour-retirement-days]"), "45");
    expect(text()).toContain("Enter a whole number from 1 to 30.");
    expect((q("[data-testid=tour-retirement-save]") as HTMLButtonElement).disabled).toBe(true);
    expect(calls.filter((c) => c.method === "PATCH")).toHaveLength(0);
  });

  it("is read-only for an editor or viewer", async () => {
    stubApi({});
    const { q, text } = await mount(createElement(TourRetirementSettingsForm,
      { settings: { ...SETTINGS, auto_draft: true, notice_days: 5 }, isOwner: false }));
    expect((q("[data-testid=tour-retirement-auto]") as HTMLInputElement).disabled).toBe(true);
    expect(q("[data-testid=tour-retirement-save]")).toBeNull();
    expect(text()).toContain("Only the Content studio owner can change this setting.");
    expect(text()).toContain("On: posts move to draft 5 days after the tour stops.");
  });

  it("the card hides itself when the setting cannot be read (before the CIS migration)", async () => {
    stubApi({ "GET /api/tenant/v1/account/tour-retirement": () =>
      Response.json({ detail: "This setting is not available yet." }, { status: 503 }) });
    const { q } = await mount(createElement(TourRetirementSettingsCard));
    expect(q("[data-testid=tour-retirement-settings]")).toBeNull();
  });

  it("the card reads the setting and the role", async () => {
    stubApi({
      "GET /api/tenant/v1/account/tour-retirement": () => Response.json(SETTINGS),
      "GET /api/tenant/me": () => Response.json({ content_studio_role: "owner" }),
    });
    const { q } = await mount(createElement(TourRetirementSettingsCard));
    expect(q("[data-testid=tour-retirement-settings]")).not.toBeNull();
    expect(q("[data-testid=tour-retirement-save]")).not.toBeNull();
  });
});

describe("helpers and wording", () => {
  it("parses whole days within bounds only", () => {
    expect(parseDays("7", 1, 30)).toBe(7);
    expect(parseDays(" 30 ", 1, 30)).toBe(30);
    for (const bad of ["0", "31", "2.5", "", "-3", "abc"]) expect(parseDays(bad, 1, 30)).toBeNull();
  });

  it("speaks Vietnamese, every key is translated, and no copy says AA changes the site", () => {
    expect(missingTranslations(RETIRED_PAGES_MESSAGES)).toEqual([]);
    const vi_ = translator(RETIRED_PAGES_MESSAGES, "vi");
    expect(moveOutcomeText(vi_, result())).toContain("bản nháp");
    expect(vi_("bellBody", "Copperfield Cove Ramble", 2)).toContain("2 trang WordPress");
    const en = translator(RETIRED_PAGES_MESSAGES, "en");
    expect(en("bellBody", "Copperfield Cove Ramble", 1)).toContain("Your WordPress page about it is still online");
    expect(JSON.stringify(RETIRED_PAGES_MESSAGES)).not.toMatch(/AA-\d+/);
  });
});
