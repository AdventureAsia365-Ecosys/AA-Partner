// @vitest-environment jsdom
// AA-1081 — the Articles page note (BlogTopicsNote): the tours with no article topic and why, in agency words; nothing
// when every tour has one or before the first update; the first 5 rows, then "Show all". Invented tour names only.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { BlogTopicsNote } from "@/app/(studio)/portal/_components/BlogTopicsNote";
import type { BlogTopics } from "@/lib/studio/blogTopics";

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
});

function mount(blogTopics: BlogTopics | null) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  act(() => {
    const root = createRoot(container);
    roots.push(root);
    root.render(createElement(BlogTopicsNote, { blogTopics }));
  });
  return container;
}

const tour = (i: number, topics: number, reason: string | null) =>
  ({ tour_id: `t-${i}`, tour_name: `Valdoria loop ${i}`, topics, reason });

describe("BlogTopicsNote", () => {
  it("lists the tours without a topic with the reason in plain words", () => {
    const el = mount({ total: 3, with_topic: 1, tours: [
      tour(1, 0, "seed_too_thin"), tour(2, 0, "low_search_interest"), tour(3, 2, null)] });
    const rows = [...el.querySelectorAll("[data-testid=blog-topics-note-row]")].map(r => r.textContent);
    expect(el.textContent).toContain("2 of your 3 tours have no article topic yet");
    expect(rows).toEqual([
      "Valdoria loop 1 — The tour's description is too short to carry a full article (about 900 words). Its stops are offered as shorter posts instead.",
      "Valdoria loop 2 — Too few people search for this tour's places yet to carry an article. It still gets social post topics.",
    ]);
    expect(el.textContent).not.toMatch(/\b(bar|debate|score|gate|seed)\b/i);
  });

  it("shows nothing when every tour has a topic or before the first update", () => {
    expect(mount({ total: 1, with_topic: 1, tours: [tour(1, 1, null)] }).textContent).toBe("");
    expect(mount(null).textContent).toBe("");
  });

  it("shows the first 5 rows, then all on request; an unknown code reads as no topic yet", () => {
    const tours = Array.from({ length: 7 }, (_, i) => tour(i + 1, 0, i === 6 ? "brand_fit_below_7" : "not_ready"));
    const el = mount({ total: 7, with_topic: 0, tours });
    expect(el.querySelectorAll("[data-testid=blog-topics-note-row]").length).toBe(5);
    const more = [...el.querySelectorAll("button")].find(b => b.textContent === "Show all 7")!;
    act(() => more.click());
    const rows = el.querySelectorAll("[data-testid=blog-topics-note-row]");
    expect(rows.length).toBe(7);
    expect(rows[6].textContent).toBe("Valdoria loop 7 — No article topic yet.");
  });
});
