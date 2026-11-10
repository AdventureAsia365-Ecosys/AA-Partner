// @vitest-environment jsdom
// AA-1039 (option A) — "Put back online": after a tour is back on sale, the WordPress pages moved to draft while it was
// off sale are listed on Publish (PutBackList) and in the bell's restore notice (PutBackNote), each with one button.
// A click sends ONE POST to the CIS put-back route through the tenant BFF and shows the outcome (online again, already
// online, left as is); nothing is called before the click; an empty list or a refused read shows nothing.
// Fake agency names and sites only.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PutBackList } from "@/app/(studio)/portal/_components/PutBackList";
import { PutBackNote, type TenantNotification } from "@/app/(studio)/portal/_components/NotificationsBell";
import { PUT_BACK_MESSAGES } from "@/app/(studio)/portal/_components/putBack.messages";
import { outcomeText, type PutBackResult } from "@/app/(studio)/portal/_components/putBackApi";
import { translator } from "@/app/_kit/core/i18n";

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

const PAGE = {
  publish_id: "p-1", piece_id: "c-1", title: "Dawn over Juniper Bay", channel: "blog",
  external_url: "https://lantern-bay.example/dawn-juniper-bay/", tour_name: "Juniper Bay Loop", drafted_at: null,
};
const result = (over: Partial<PutBackResult> = {}): PutBackResult => ({
  publish_id: "p-1", outcome: "published", wordpress_status: "publish", status: "published",
  external_url: PAGE.external_url, replayed: false, message: "The page is online again.", ...over,
});

function stubApi(list: unknown, post: () => Response) {
  const calls: { url: string; method: string }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    calls.push({ url, method });
    if (url === "/api/tenant/v1/publish-log/put-back" && method === "GET") {
      return list instanceof Response ? list : Response.json({ data: list, total: (list as unknown[]).length });
    }
    if (url === "/api/tenant/v1/publish-log/p-1/put-back" && method === "POST") return post();
    return Response.json({}, { status: 404 });
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

describe("Publish › Pages to put back online", () => {
  it("lists the drafted page and publishes it with one POST only after the click", async () => {
    const calls = stubApi([PAGE], () => Response.json(result()));
    const { q, text } = await mount(createElement(PutBackList));
    expect(q("[data-testid=put-back-list]")).not.toBeNull();
    expect(text()).toContain("Dawn over Juniper Bay");
    expect(text()).toContain("Tour: Juniper Bay Loop");
    expect(q("#put-back")).not.toBeNull();
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(0);
    await click(q("[data-testid=put-back-button-p-1]"));
    const posts = calls.filter((c) => c.method === "POST");
    expect(posts).toEqual([{ url: "/api/tenant/v1/publish-log/p-1/put-back", method: "POST" }]);
    expect(text()).toContain("The page is online again.");
  });

  it("says when the page was changed on WordPress and left as is", async () => {
    stubApi([PAGE], () => Response.json(result({ outcome: "changed_on_wordpress", wordpress_status: "private",
      status: "unpublished" })));
    const { q, text } = await mount(createElement(PutBackList));
    await click(q("[data-testid=put-back-button-p-1]"));
    expect(text()).toContain("it is private now");
  });

  it("shows the API's reason when the click is refused", async () => {
    stubApi([PAGE], () => Response.json({ detail: "This page is already being put back online — refresh in a minute." },
      { status: 409 }));
    const { q, text } = await mount(createElement(PutBackList));
    await click(q("[data-testid=put-back-button-p-1]"));
    expect(text()).toContain("already being put back online");
  });

  it("renders nothing for an empty list or a refused read", async () => {
    stubApi([], () => Response.json(result()));
    const empty = await mount(createElement(PutBackList));
    expect(empty.q("[data-testid=put-back-list]")).toBeNull();
    stubApi(Response.json({ detail: "View as tenant is read-only" }, { status: 403 }), () => Response.json(result()));
    const refused = await mount(createElement(PutBackList));
    expect(refused.q("[data-testid=put-back-list]")).toBeNull();
  });
});

describe("Bell › restore notice with drafted pages", () => {
  const note: TenantNotification = {
    id: 9, event_type: "tour.pages_to_put_back", title: "Pages to put back online",
    message: "Juniper Bay Loop is back on sale. 1 of your WordPress page moved to draft while it was off sale stays in draft until you put it back online.",
    tour_id: "t-1", tour_name: "Juniper Bay Loop", reason: null, live_posts: 0, is_read: false, created_at: null,
    pages: [{ publish_id: "p-1", url: PAGE.external_url }],
  };

  it("puts the page back from the bell with one click, then shows the outcome instead of the button", async () => {
    const calls = stubApi([], () => Response.json(result({ outcome: "already_online", replayed: true })));
    const { q, text } = await mount(createElement(PutBackNote, { note }));
    expect(text()).toContain(PAGE.external_url);
    expect(q("a")?.getAttribute("href")).toBe("/portal/content/publish#put-back");
    await click(q("[data-testid=bell-put-back-button-p-1]"));
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(1);
    expect(q("[data-testid=bell-put-back-button-p-1]")).toBeNull();
    expect(q("[data-testid=bell-put-back-result-p-1]")?.textContent).toBe("The page was already online on WordPress.");
  });
});

describe("outcome text", () => {
  it("speaks Vietnamese and names the WordPress state", () => {
    const vi_ = translator(PUT_BACK_MESSAGES, "vi");
    expect(outcomeText(vi_, result())).toBe("Trang đã lên mạng lại.");
    expect(outcomeText(vi_, result({ outcome: "changed_on_wordpress", wordpress_status: "trash" })))
      .toContain("trong thùng rác");
    const en = translator(PUT_BACK_MESSAGES, "en");
    expect(outcomeText(en, result({ outcome: "changed_on_wordpress", wordpress_status: "odd" }))).toContain("changed");
  });
});
