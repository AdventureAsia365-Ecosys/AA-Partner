// @vitest-environment jsdom
// AA-1070 item crm-35 (crm-059): the "Included" / "Not included" lists. The pure list moves of the builder picker
// (pick once, type a line, move within its kind, remove) and the PUT body; the BFF lines (only the routes the API
// serves); both languages; the builder panel (picks a library line, types one, saves the whole list; a published
// version is read only; an AA tour's lines are shown, never editable); the client page lists in the page's language;
// Settings > Proposal lines (adds a line, the assistant reads only).
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { matchBff } from "@/lib/bff";
import { t } from "@/lib/i18n";
import { inclusionsEn, inclusionsVi } from "@/lib/i18nInclusions";
import {
  draftLines,
  hasInclusions,
  inclusionErrorKey,
  lineText,
  linesBody,
  moveAt,
  ofKind,
  pickItem,
  removeAt,
  sameLines,
  typedLine,
  type ClientInclusions,
  type InclusionItem,
  type InclusionsPanelData,
} from "@/lib/inclusions";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {}, push: () => {} }) }));
const sent: { method: string; path: string; body: unknown }[] = [];
let answers: { ok: boolean; status?: number; detail?: string; data?: unknown }[] = [];
vi.mock("@/lib/bffClient", () => ({
  bffSend: async (method: string, path: string, body?: unknown) => {
    sent.push({ method, path, body });
    return answers.shift() ?? { ok: true, data: {} };
  },
  bffDownload: async () => ({ ok: true, data: null }),
}));
const { InclusionsPanel } = await import("@/components/proposals/InclusionsPanel");
const { ClientInclusionsContent } = await import("@/components/proposals/ClientBuilderProposal");
const { InclusionLibraryView, sortedItems } = await import("@/components/settings/InclusionLibraryView");

const UUID = "00000000-0000-4000-8000-000000000001";
const ITEM = (over: Partial<InclusionItem> = {}): InclusionItem => ({
  inclusion_item_id: "00000000-0000-4000-8000-0000000000a1",
  kind: "included",
  text_en: "Daily breakfast",
  text_vi: "Bữa sáng",
  sort_order: 0,
  archived: false,
  ...over,
});
const FLIGHTS = ITEM({ inclusion_item_id: "00000000-0000-4000-8000-0000000000a2", kind: "not_included", text_en: "Long-haul flights", text_vi: null });

describe("list moves", () => {
  it("picks a library line once, types a line, moves and removes within a kind", () => {
    let lines = pickItem([], ITEM());
    expect(pickItem(lines, ITEM())).toBe(lines);
    lines = pickItem(lines, FLIGHTS);
    const typed = typedLine("included", "  Welcome   dinner ", " ");
    expect(typed).toEqual({ kind: "included", text_en: "Welcome dinner", text_vi: null, inclusion_item_id: null });
    expect(typedLine("included", "   ", "x")).toBeNull();
    expect(typedLine("included", "x".repeat(301), "")).toBeNull();
    lines = [...lines, typed!];
    expect(ofKind(lines, "included").map((l) => l.text_en)).toEqual(["Daily breakfast", "Welcome dinner"]);
    const up = moveAt(lines, "included", 1, -1);
    expect(ofKind(up, "included").map((l) => l.text_en)).toEqual(["Welcome dinner", "Daily breakfast"]);
    expect(moveAt(lines, "included", 0, -1)).toBe(lines);
    expect(moveAt(lines, "not_included", 0, 1)).toBe(lines);
    expect(linesBody(up)).toEqual([
      { kind: "included", text_en: "Welcome dinner" },
      { inclusion_item_id: ITEM().inclusion_item_id },
      { inclusion_item_id: FLIGHTS.inclusion_item_id },
    ]);
    expect(linesBody([typedLine("not_included", "Tips", "Tiền boa")!])).toEqual([{ kind: "not_included", text_en: "Tips", text_vi: "Tiền boa" }]);
    const removed = removeAt(up, "included", 0);
    expect(ofKind(removed, "included").map((l) => l.text_en)).toEqual(["Daily breakfast"]);
    expect(removeAt(up, "included", 9)).toBe(up);
    expect(sameLines(up, lines)).toBe(false);
    expect(sameLines(draftLines([]), [])).toBe(true);
  });
  it("shows a line in the page's language and words refusals", () => {
    expect(lineText({ text_en: "Dinner", text_vi: "Bữa tối" }, "vi")).toBe("Bữa tối");
    expect(lineText({ text_en: "Dinner", text_vi: null }, "vi")).toBe("Dinner");
    expect(lineText({ text_en: "Dinner", text_vi: "Bữa tối" }, "en")).toBe("Dinner");
    expect(hasInclusions(null)).toBe(false);
    expect(hasInclusions({ included: [], not_included: [], aa_tours: [{ source_id: "T", title: "T", included: [], not_included: [] }] })).toBe(false);
    expect(hasInclusions({ included: [], not_included: [], aa_tours: [{ source_id: "T", title: "T", included: ["Guide"], not_included: [] }] })).toBe(true);
    expect(inclusionErrorKey("inclusion_item_archived")).toBe("incl.error.inclusion_item_archived");
    expect(inclusionErrorKey("nope")).toBe("incl.error.other");
    expect(t("en", "incl.error.inclusion_item_exists")).toBe("The library already has this line.");
  });
});

describe("BFF lines and words", () => {
  it("serve the library and the proposal lines the API has, nothing else", () => {
    for (const [m, p] of [
      ["GET", "inclusion-items"],
      ["POST", "inclusion-items"],
      ["PATCH", `inclusion-items/${UUID}`],
      ["DELETE", `inclusion-items/${UUID}`],
      ["GET", `proposals/${UUID}/inclusions`],
      ["PUT", `proposals/${UUID}/inclusions`],
    ] as const) {
      expect(matchBff(m, p), `${m} ${p}`).not.toBeNull();
    }
    expect(matchBff("PUT", "inclusion-items")).toBeNull();
    expect(matchBff("POST", `proposals/${UUID}/inclusions`)).toBeNull();
    expect(matchBff("DELETE", `inclusion-items/${UUID}/x`)).toBeNull();
  });
  it("has every word in English and Vietnamese", () => {
    expect(Object.keys(inclusionsVi).sort()).toEqual(Object.keys(inclusionsEn).sort());
    for (const v of Object.values(inclusionsVi)) expect(v.trim()).not.toBe("");
    expect(t("vi", "client.incl.notIncluded")).toBe("Chưa bao gồm");
  });
});

let root: Root | null = null;
afterEach(() => {
  act(() => root?.unmount());
  root = null;
  document.body.innerHTML = "";
  sent.length = 0;
  answers = [];
});

function mount(el: ReturnType<typeof createElement>): HTMLElement {
  const host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root!.render(el));
  return host;
}

function button(host: HTMLElement, text: string): HTMLButtonElement {
  const b = [...host.querySelectorAll("button")].find((x) => x.textContent?.trim() === text);
  if (!b) throw new Error(`no button ${text}`);
  return b as HTMLButtonElement;
}

function type(input: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
  act(() => {
    setter.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

const PANEL: InclusionsPanelData = {
  lines: [{ proposal_inclusion_id: UUID, kind: "not_included", source: "custom", inclusion_item_id: null, text_en: "Visa fees", text_vi: null }],
  aa_tours: [{ source_id: "T-1", title: "Lantern Valley Loop", included: ["Local guide"], not_included: [] }],
  library: [ITEM(), FLIGHTS],
  max_lines: 60,
};

describe("the builder panel", () => {
  it("picks a library line, types one and saves the whole list", async () => {
    const saved: Record<string, string>[][] = [];
    const host = mount(
      createElement(InclusionsPanel, { data: PANEL, draft: true, busy: false, lang: "en", onSave: async (lines: Record<string, string>[]) => (saved.push(lines), true) }),
    );
    expect(host.querySelectorAll('[data-testid="incl-line"]')).toHaveLength(1);
    expect(host.querySelector('[data-testid="incl-aa-tour"]')?.textContent).toContain("Local guide");
    expect(host.querySelector('[data-testid="incl-aa-tour"] button')).toBeNull(); // AA lines are fixed
    act(() => button(host, "+ Daily breakfast").click());
    const inputs = host.querySelectorAll<HTMLInputElement>('[data-testid="incl-typed-form"] input');
    type(inputs[0], "Welcome dinner");
    act(() => button(host, "Add to this proposal").click());
    expect(host.querySelectorAll('[data-testid="incl-line"]')).toHaveLength(3);
    await act(async () => button(host, "Save lines").click());
    expect(saved).toEqual([[{ inclusion_item_id: ITEM().inclusion_item_id }, { kind: "included", text_en: "Welcome dinner" }, { kind: "not_included", text_en: "Visa fees" }]]);
  });
  it("shows a published version read only", () => {
    const host = mount(createElement(InclusionsPanel, { data: PANEL, draft: false, busy: false, lang: "vi", onSave: async () => true }));
    expect(host.textContent).toContain("Phiên bản này đã phát hành");
    expect(host.querySelector('[data-testid="incl-typed-form"]')).toBeNull();
    expect(host.querySelectorAll("button")).toHaveLength(0);
  });
});

describe("the client page", () => {
  it("lists the agency's lines in the page's language and the tour's fixed lines", () => {
    const data: ClientInclusions = {
      included: [{ text_en: "Daily breakfast", text_vi: "Bữa sáng" }],
      not_included: [{ text_en: "Long-haul flights", text_vi: null }],
      aa_tours: [{ source_id: "T-1", title: "Lantern Valley Loop", included: ["Local guide"], not_included: ["Tips"] }],
    };
    const words: Record<string, string> = { "client.incl.included": "Đã bao gồm", "client.incl.notIncluded": "Chưa bao gồm" };
    const host = mount(createElement(ClientInclusionsContent, { data, lang: "vi", t: (k: string) => words[k] ?? k }));
    const inc = host.querySelector('[data-testid="client-incl-included"]')!.textContent!;
    expect(inc).toContain("Đã bao gồm");
    expect(inc).toContain("Bữa sáng");
    expect(inc).toContain("Local guide");
    const exc = host.querySelector('[data-testid="client-incl-not_included"]')!.textContent!;
    expect(exc).toContain("Long-haul flights");
    expect(exc).toContain("Tips");
  });
});

describe("Settings > Proposal lines", () => {
  it("adds a line for an owner or agent and orders the library", async () => {
    answers = [{ ok: true, data: ITEM({ inclusion_item_id: "00000000-0000-4000-8000-0000000000a3", text_en: "Airport pickup", text_vi: null, sort_order: 1 }) }];
    const host = mount(createElement(InclusionLibraryView, { items: [FLIGHTS, ITEM()], canEdit: true, lang: "en" }));
    expect([...host.querySelectorAll('[data-testid="incl-item"]')].map((x) => x.textContent)).toEqual([
      expect.stringContaining("Daily breakfast"),
      expect.stringContaining("Long-haul flights"),
    ]);
    const add = host.querySelector('[data-testid="incl-library-add"]')!;
    type(add.querySelectorAll("input")[0] as HTMLInputElement, "Airport pickup");
    await act(async () => (add.querySelector('[data-testid="incl-library-add-button"]') as HTMLButtonElement).click());
    expect(sent).toEqual([{ method: "POST", path: "inclusion-items", body: { kind: "included", text_en: "Airport pickup", text_vi: null } }]);
    expect(host.querySelectorAll('[data-testid="incl-item"]')).toHaveLength(3);
    await act(async () => button(host, "Archive").click());
    expect(sent[1]).toEqual({ method: "PATCH", path: `inclusion-items/${ITEM().inclusion_item_id}`, body: { archived: true } });
  });
  it("is read only for the assistant", () => {
    const host = mount(createElement(InclusionLibraryView, { items: [ITEM({ archived: true })], canEdit: false, lang: "en" }));
    expect(host.querySelector('[data-testid="incl-library-add"]')).toBeNull();
    expect(host.querySelectorAll('[data-testid="incl-item"]')).toHaveLength(0); // archived lines hidden by default
    expect(sortedItems([FLIGHTS, ITEM({ sort_order: 2 }), ITEM({ inclusion_item_id: "b", text_en: "Apples", sort_order: 2 })]).map((x) => x.text_en)).toEqual([
      "Apples",
      "Daily breakfast",
      "Long-haul flights",
    ]);
  });
});
