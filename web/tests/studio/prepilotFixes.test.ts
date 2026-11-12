// @vitest-environment jsdom
// AA-1088 (S278 audit, pre-pilot fixes) in the Content studio:
//   M3 — a piece CIS could not fully confirm against the trip is refused with a 409 (content_needs_final_check). The
//        Publish page shows the reason in the UI language, never "Publish failed (409)"; only the account owner
//        (can_publish_anyway) gets "Publish anyway", which sends {"publish_anyway": true}; My Content's helper throws
//        the same reason.
//   M6 — the piece drawer shows only the agency's own text and the AI notice: no writer brief (angle formula / style,
//        source fact), no search context.
// Invented names and places only.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PublishPendingList } from "@/app/(studio)/portal/_components/PublishPendingList";
import { ReviewDrawer } from "@/app/(studio)/portal/_components/ReviewDrawer";
import { ERROR_MESSAGES } from "@/app/(studio)/portal/_components/errors.messages";
import { publishPiece, type ReviewItem } from "@/app/(studio)/portal/_components/reviewListApi";
import { plainError } from "@/app/(studio)/portal/_components/studioError";
import { NEEDS_FINAL_CHECK, finalCheckRefusal, publishRequestInit } from "@/lib/studio/publishCheck";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
window.matchMedia ??= ((query: string) => ({
  matches: false, media: query, onchange: null,
  addEventListener: () => {}, removeEventListener: () => {}, addListener: () => {}, removeListener: () => {},
  dispatchEvent: () => false,
})) as unknown as typeof window.matchMedia;

const roots: Root[] = [];
afterEach(() => {
  for (const r of roots.splice(0)) act(() => r.unmount());
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

async function mount(element: ReturnType<typeof createElement>): Promise<HTMLElement> {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => {
    const root = createRoot(container);
    roots.push(root);
    root.render(createElement(QueryClientProvider, { client: qc }, element));
  });
  return container;
}

const flush = () => act(async () => { await new Promise((r) => setTimeout(r, 0)); });
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const REFUSAL = (canPublishAnyway: boolean) => ({
  detail: "Before this piece goes live, please read it through once more.",
  code: NEEDS_FINAL_CHECK, can_publish_anyway: canPublishAnyway,
});
const PENDING = {
  data: [{
    piece_id: "piece-1", title: "Lanterns over Willowmere", content_preview: "Paddle out at dawn.", channel: "blog",
    created_at: "2026-11-10T08:00:00Z", route_hub_name: null, route_segment_count: null,
  }],
};
const EN = ERROR_MESSAGES.en;

describe("publishCheck helpers", () => {
  it("reads the final-check refusal from the body, and nothing else", () => {
    expect(finalCheckRefusal(409, REFUSAL(true))).toEqual({ canPublishAnyway: true });
    expect(finalCheckRefusal(409, REFUSAL(false))).toEqual({ canPublishAnyway: false });
    expect(finalCheckRefusal(409, { detail: "A newer version of this content exists" })).toBeNull();
    expect(finalCheckRefusal(422, REFUSAL(true))).toBeNull();
    expect(finalCheckRefusal(409, null)).toBeNull();
  });

  it("a plain publish has no body; Publish anyway sends the owner's choice as JSON", () => {
    expect(publishRequestInit(false)).toEqual({ method: "POST" });
    const init = publishRequestInit(true);
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ publish_anyway: true });
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
  });

  it("the reason names no score, gate or review in either language", () => {
    for (const lang of ["en", "vi"] as const) {
      for (const key of ["needsFinalCheckOwner", "needsFinalCheckEditor"] as const) {
        const text = String(ERROR_MESSAGES[lang][key]).toLowerCase();
        for (const word of ["held", "score", "gate", "judge", "review queue", "chấm điểm", "kiểm duyệt"]) {
          expect(text, `${lang}.${key}`).not.toContain(word);
        }
      }
    }
  });
});

describe("Publish page: a piece that needs a final read", () => {
  function stub(refusal: ReturnType<typeof REFUSAL>) {
    const posts: (RequestInit | undefined)[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/pending")) return json(200, PENDING);
      posts.push(init);
      if (posts.length === 1) return json(409, refusal);
      return json(200, { success: true, status: "published", external_url: "https://willowmere.example/p/1" });
    }));
    return posts;
  }

  it("owner: shows the reason (not a status code) and Publish anyway sends publish_anyway", async () => {
    const posts = stub(REFUSAL(true));
    const el = await mount(createElement(PublishPendingList, { connected: { blog: true } }));
    await flush();
    const publish = [...el.querySelectorAll("button")].find((b) => b.textContent?.trim() === "Publish")!;
    await act(async () => { publish.click(); });
    await flush();
    expect(el.textContent).toContain(String(EN.needsFinalCheckOwner));
    expect(el.textContent).not.toMatch(/Publish failed|\(409\)/);
    expect(posts[0]).toEqual({ method: "POST" });
    const anyway = el.querySelector('[data-testid="publish-anyway"]')!.querySelector("button")!;
    expect(anyway.textContent).toBe("Publish anyway");
    await act(async () => { anyway.click(); });
    await flush();
    expect(JSON.parse(String(posts[1]?.body))).toEqual({ publish_anyway: true });
    expect(el.textContent).toContain("Published.");
  });

  it("editor: shows the hand-to-owner reason and no Publish anyway button", async () => {
    stub(REFUSAL(false));
    const el = await mount(createElement(PublishPendingList, { connected: { blog: true } }));
    await flush();
    const publish = [...el.querySelectorAll("button")].find((b) => b.textContent?.trim() === "Publish")!;
    await act(async () => { publish.click(); });
    await flush();
    expect(el.textContent).toContain(String(EN.needsFinalCheckEditor));
    expect(el.querySelector('[data-testid="publish-anyway"]')).toBeNull();
  });
});

describe("My Content publish helper", () => {
  it("throws the final-read reason in the UI language, and sends publish_anyway when asked", async () => {
    const calls: (RequestInit | undefined)[] = [];
    vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) => {
      calls.push(init);
      return calls.length === 1 ? json(409, REFUSAL(false)) : json(200, { success: true, status: "published", external_url: null });
    }));
    const err = await publishPiece("piece-1").catch((e: unknown) => e);
    expect(plainError(err, "fallback", "en")).toBe(String(EN.needsFinalCheckEditor));
    expect(plainError(err, "fallback", "vi")).toBe(String(ERROR_MESSAGES.vi.needsFinalCheckEditor));
    await publishPiece("piece-1", { publishAnyway: true });
    expect(JSON.parse(String(calls[1]?.body))).toEqual({ publish_anyway: true });
  });
});

describe("piece drawer: the agency's own text and the AI notice only", () => {
  const item: ReviewItem = {
    request_id: "r-1", piece_id: "piece-1", channel: "blog", ready_state: "ready",
    content_text: "## Dawn at Willowmere\n\nPaddle out as the lanterns fade.",
    goal: { key: "awareness", label: "Awareness" },
    angle: { name: "Lantern Mornings", why_it_works: "WHY-BRIEF-MARKER", formula_fit: "FORMULA-MARKER", best_final_style: "STYLE-MARKER" },
    atom: { text: "SOURCE-FACT-MARKER", activity_type: "kayaking", emotional_hook: "HOOK-MARKER", season_note: null },
    tour: { name: "Willowmere Lakes", destination: "Nowhereland" },
    dfs_paa_snapshot: { relevance: "high", people_also_ask: ["PAA-MARKER?"], related_keywords: ["KEYWORD-MARKER"] },
    cta: "CTA-MARKER", created_at: "2026-11-10T08:00:00Z", route_hub_name: null, route_segment_count: null,
  } as ReviewItem;

  it("shows the text and the AI notice, and none of the writer's brief or search context", async () => {
    const el = await mount(createElement(ReviewDrawer, {
      item, onSave: async () => {}, onExport: () => {}, saving: false, onCopyAttempt: () => {},
    }));
    const text = el.textContent ?? "";
    expect(text).toContain("Paddle out as the lanterns fade.");
    expect(text).toMatch(/generated with AI/i);
    for (const marker of ["WHY-BRIEF-MARKER", "FORMULA-MARKER", "STYLE-MARKER", "SOURCE-FACT-MARKER", "HOOK-MARKER",
      "PAA-MARKER", "KEYWORD-MARKER", "CTA-MARKER", "Search context used", "Where this came from"]) {
      expect(text, marker).not.toContain(marker);
    }
  });
});
