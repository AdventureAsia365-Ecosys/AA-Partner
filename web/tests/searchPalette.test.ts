// @vitest-environment jsdom
// AA-963 review: the palette never opens a result of the previous term. While the debounce has not sent the term typed
// now, the earlier answer is hidden ("Searching…") and Enter opens nothing; once the new answer arrives, Enter opens
// its first row. Also: the notice when clients were left out (a wildcard). Invented names only.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MESSAGES } from "@/lib/i18n";
import type { SearchConfig, SearchResult } from "@/lib/search";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {}, push }) }));
const answers = new Map<string, SearchResult>();
const sent: string[] = [];
vi.mock("@/lib/bffClient", () => ({
  bffSend: async (_method: string, _path: string, body: { q: string }) => {
    sent.push(body.q);
    const data = answers.get(body.q);
    return data ? { ok: true, data } : { ok: false, status: 500, detail: "error" };
  },
}));
const { SearchPalette } = await import("@/components/search/SearchPalette");

const ON: SearchConfig = { enabled: true, kinds: ["trip"], query_max_chars: 120, min_text_chars: 2, results_per_kind: 5 };
const TRIP_A = "00000000-0000-4000-8000-0000000009b1";
const TRIP_B = "00000000-0000-4000-8000-0000000009b2";

function trip(id: string, label: string): SearchResult {
  return { mode: "text", kinds: ["trip"], groups: [{ kind: "trip", items: [{ id, label, meta: {} }] }], notice: null };
}

const roots: Root[] = [];
beforeEach(() => {
  vi.useFakeTimers();
  push.mockClear();
  answers.clear();
  sent.length = 0;
});
afterEach(() => {
  for (const r of roots.splice(0)) act(() => r.unmount());
  document.body.innerHTML = "";
  vi.useRealTimers();
});

async function mount(): Promise<HTMLInputElement> {
  const container = document.createElement("div");
  document.body.appendChild(container);
  await act(async () => {
    const root = createRoot(container);
    roots.push(root);
    root.render(
      createElement(SearchPalette, {
        open: true,
        onClose: () => {},
        dict: MESSAGES.en,
        config: ON,
        contentEnabled: false,
        tenantId: TRIP_A,
        lang: "en",
      }),
    );
  });
  return document.querySelector<HTMLInputElement>("[data-testid=crm-search-input]")!;
}

async function type(input: HTMLInputElement, value: string): Promise<void> {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

async function settle(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

async function enter(input: HTMLInputElement): Promise<void> {
  await act(async () => {
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  });
}

function rows(): string[] {
  return [...document.querySelectorAll("[data-testid=crm-search-row-trip]")].map((el) => el.textContent ?? "");
}

describe("SearchPalette", () => {
  it("never opens a result of the previous term with Enter during the debounce", async () => {
    answers.set("Lantern Hill", trip(TRIP_A, "Lantern Hill weekend"));
    answers.set("Lantern Hill x", trip(TRIP_B, "Lantern Hill x tour"));
    const input = await mount();
    await type(input, "Lantern Hill");
    await settle(300);
    expect(rows()).toEqual(["Lantern Hill weekend"]);

    await type(input, "Lantern Hill x"); // typed on: the debounce has not sent it yet
    expect(rows()).toEqual([]); // the previous answer is hidden ...
    expect(document.querySelector("[data-testid=crm-search-loading]")).not.toBeNull();
    await enter(input);
    expect(push).not.toHaveBeenCalled(); // ... and Enter does not open it

    await settle(300);
    expect(sent).toEqual(["Lantern Hill", "Lantern Hill x"]);
    expect(rows()).toEqual(["Lantern Hill x tour"]);
    await enter(input);
    expect(push).toHaveBeenCalledWith(`/pipeline/${TRIP_B}`);
  });

  it("says when clients were left out of a text search", async () => {
    answers.set("100% Lantern", { ...trip(TRIP_A, "100% Lantern loop"), notice: "partial_search_refused" });
    const input = await mount();
    await type(input, "100% Lantern");
    await settle(300);
    expect(document.querySelector("[data-testid=crm-search-notice]")?.textContent).toBe(
      MESSAGES.en["search.notice.partial_search_refused"],
    );
    expect(rows()).toEqual(["100% Lantern loop"]);
  });
});
