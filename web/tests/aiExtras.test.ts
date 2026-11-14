// @vitest-environment jsdom
// AA-965 (card CRM-33b): the three AI features beside the reply draft — R10 the supplier confirmation reader (only own
// flight / insurance / visa / transfer / fee suppliers with an open line; the agent's picks become the apply body; the
// AiNotice shows with the proposal), R40 the builder's "AI ideas" tab (ideas with their reason and the AiNotice; Add uses
// the normal AA add), R41 the voices (the per-member limit, the draft's voice picker); the BFF allow-list lines and the
// error wording. Invented names only.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { aiNoticeText } from "@/app/_kit";
import {
  aiExtraErrorKey,
  aiExtraFallbackKey,
  applyBody,
  canAddVoice,
  defaultAction,
  readableSuppliers,
  voiceOptions,
  type NotesOptionsResult,
  type ProposedLine,
  type SupplierConfirmResult,
  type VoicesView,
} from "@/lib/aiExtras";
import { matchBff } from "@/lib/bff";
import type { BuilderView } from "@/lib/builder";
import { MESSAGES } from "@/lib/i18n";
import type { Line, Supplier } from "@/lib/ledger";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {}, push: () => {} }) }));
const { AiSupplierConfirmPanel } = await import("@/components/pipeline/AiSupplierConfirmPanel");
const { BuilderAiIdeasTab } = await import("@/components/proposals/BuilderAiIdeasTab");
const { AiVoicesPanel } = await import("@/components/settings/AiVoicesPanel");

const TRIP = "00000000-0000-4000-8000-0000000009d1";
const AIR = "00000000-0000-4000-8000-0000000009a1";
const HOTEL = "00000000-0000-4000-8000-0000000009a2";
const AA = "00000000-0000-4000-8000-0000000009a3";
const LINE = "00000000-0000-4000-8000-0000000009b1";
const DRAFT = "00000000-0000-4000-8000-0000000009f1";
const DAY = "00000000-0000-4000-8000-000000000901";
const PROPOSAL = "00000000-0000-4000-8000-000000000902";
const BLOCK = "00000000-0000-4000-8000-000000000904";
const VOICE = "00000000-0000-4000-8000-000000000903";
const VIEW = { proposal_id: PROPOSAL, days: [{ proposal_day_id: DAY, blocks: [{ option_block_id: BLOCK }] }] } as unknown as Pick<
  BuilderView,
  "proposal_id" | "days"
>;

function line(over: Partial<Line>): Line {
  return {
    booking_line_id: LINE, trip_id: TRIP, supplier_kind: "agency", agency_supplier_id: AIR, supplier_name: "Skylark Air",
    status: "requested", aa_sub_status: null, title: "Flight leg 1", confirmation_no: null, service_from: null,
    service_to: null, collect: "agency", confirm_class: null, visible_to_travelers: false, confirmed_at: null,
    read_only: false, ...over,
  } as Line;
}

function supplier(id: string, kind: "own" | "aa", category: string, name: string): Supplier {
  return { agency_supplier_id: id, kind, builtin: kind === "aa", name, category, confirmation_unique: false,
           has_contact_name: false, has_contact_email: false } as Supplier;
}

const SUPPLIERS = [supplier(AIR, "own", "flight", "Skylark Air"), supplier(HOTEL, "own", "accommodation", "Lagoon Inn"),
                   supplier(AA, "aa", "dmc", "Adventure Asia")];
const LINES = [line({}), line({ booking_line_id: "l2", agency_supplier_id: HOTEL }),
               line({ booking_line_id: "l3", agency_supplier_id: AA, supplier_kind: "aa", status: "planning" })];

const PROPOSED: ProposedLine = { code: "L1", booking_line_id: LINE, title: "Flight leg 1", status: "confirmed",
                                 confirmation_no: "SKY7Q2", service_from: null, service_to: null, times: "07:40" };
const OUTCOME = { outcome: "ok", fallback: null, refusal_code: null, error_class: null, llm_call_id: "c1",
                  budget_used_pct: 1, budget_alert: null, truncated: false, approval_required: true } as const;

let root: Root | null = null;
let host: HTMLDivElement | null = null;
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  vi.unstubAllGlobals();
});

function mount(element: React.ReactElement): HTMLDivElement {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root!.render(element));
  return host;
}

function fetchReturning(bodies: Record<string, unknown>) {
  const calls: { url: string; body: unknown }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    const key = Object.keys(bodies).find((k) => url.includes(k));
    return new Response(JSON.stringify(key ? bodies[key] : {}), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  return calls;
}

async function flush() {
  await act(async () => {
    await new Promise((r) => setTimeout(r, 0));
  });
}

describe("pure rules", () => {
  it("offers only own ancillary suppliers with an open line, never the AA row or a hotel", () => {
    expect(readableSuppliers(LINES, SUPPLIERS).map((s) => s.agency_supplier_id)).toEqual([AIR]);
    expect(readableSuppliers([line({ status: "confirmed" })], SUPPLIERS)).toEqual([]);
  });

  it("turns the agent's picks into the apply body", () => {
    expect(defaultAction({ status: "confirmed" })).toBe("confirm");
    expect(defaultAction({ status: "declined" })).toBe("back_to_planning");
    expect(defaultAction({ status: "unclear" })).toBeNull();
    expect(
      applyBody([
        { line: PROPOSED, action: "confirm", reference: "  SKY7Q3 " },
        { line: { ...PROPOSED, booking_line_id: "l2" }, action: null, reference: "x" },
        { line: { ...PROPOSED, booking_line_id: "l3" }, action: "back_to_planning", reference: "ignored" },
      ]),
    ).toEqual([
      { booking_line_id: LINE, action: "confirm", confirmation_no: "SKY7Q3" },
      { booking_line_id: "l3", action: "back_to_planning" },
    ]);
  });

  it("words refusals and fallbacks, in both languages", () => {
    expect(aiExtraErrorKey("aa_line_read_only")).toBe("aiX.error.aa_line_read_only");
    expect(aiExtraErrorKey("something_new")).toBe("aiX.error.generic");
    expect(aiExtraFallbackKey({ outcome: "refused", refusal_code: "budget_exhausted", truncated: false })).toBe("aiX.fallback.budget");
    expect(aiExtraFallbackKey({ outcome: "error", refusal_code: null, truncated: true })).toBe("aiX.fallback.truncated");
    for (const key of Object.keys(MESSAGES.en).filter((k) => k.startsWith("aiX."))) {
      expect(MESSAGES.vi[key as keyof typeof MESSAGES.vi], key).toBeTruthy();
    }
  });

  it("caps voices at the per-member limit and lists them for the picker", () => {
    const view: VoicesView = { voices: [], per_member_max: 2, mine: 2, descriptors: { formality: ["neutral", "formal"] } };
    expect(canAddVoice(view)).toBe(false);
    expect(canAddVoice({ ...view, mine: 1 })).toBe(true);
    expect(canAddVoice(null)).toBe(false);
    expect(voiceOptions(null)).toEqual([]);
  });

  it("allows exactly the new BFF routes", () => {
    expect(matchBff("POST", `trips/${TRIP}/ai/supplier-confirmation`)?.card).toBe("AA-965");
    expect(matchBff("POST", `ai/drafts/${DRAFT}/apply`)?.card).toBe("AA-965");
    expect(matchBff("POST", `proposal-days/${DAY}/ai/options`)?.card).toBe("AA-965");
    expect(matchBff("GET", "ai/voices")?.card).toBe("AA-965");
    expect(matchBff("PATCH", `ai/voices/${VOICE}`)?.card).toBe("AA-965");
    expect(matchBff("DELETE", `ai/voices/${VOICE}`)?.card).toBe("AA-965");
    expect(matchBff("PUT", `ai/voices/${VOICE}`)).toBeNull();
    expect(matchBff("POST", "ai/drafts/not-a-uuid/apply")).toBeNull();
  });
});

describe("supplier confirmation panel", () => {
  it("is disabled without a readable supplier", () => {
    const html = renderToString(
      createElement(AiSupplierConfirmPanel, { tripId: TRIP, lines: [LINES[1]], suppliers: SUPPLIERS, lang: "en", closed: false }),
    );
    expect(html).toContain(MESSAGES.en["aiX.sc.noSupplier"]);
    expect(html.match(/<button[^>]*data-testid="ai-supplier-confirm-open"[^>]*>/)?.[0]).toContain("disabled");
  });

  it("reads, shows the proposal with the AI notice and applies the agent's picks", async () => {
    const result: SupplierConfirmResult = { ...OUTCOME, ai_draft_id: DRAFT, agency_supplier_id: AIR, lines: [PROPOSED],
                                            rejected: 1, reasons: { unknown_line: 1 }, source: "text" };
    const calls = fetchReturning({ "ai/supplier-confirmation": result, "/apply": { decision: "accepted" } });
    const el = mount(createElement(AiSupplierConfirmPanel, { tripId: TRIP, lines: LINES, suppliers: SUPPLIERS, lang: "en", closed: false }));
    act(() => (el.querySelector('[data-testid="ai-supplier-confirm-open"]') as HTMLButtonElement).click());
    const area = document.querySelector('[data-testid="ai-supplier-confirm-drawer"] textarea') as HTMLTextAreaElement;
    act(() => {
      const set = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
      set.call(area, "Confirmed, reference SKY7Q2");
      area.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => {
      (document.querySelector('[data-testid="ai-supplier-confirm-read"]') as HTMLButtonElement).click();
    });
    await flush();
    expect(calls[0].url).toContain(`trips/${TRIP}/ai/supplier-confirmation`);
    expect(calls[0].body).toEqual({ agency_supplier_id: AIR, text: "Confirmed, reference SKY7Q2" });
    const section = document.querySelector('[data-testid="ai-supplier-confirm-result"]')!;
    expect(section.textContent).toContain(aiNoticeText("staff", "en"));
    expect(section.textContent).toContain("1 answer(s)");
    await act(async () => {
      (document.querySelector('[data-testid="ai-supplier-confirm-apply"]') as HTMLButtonElement).click();
    });
    await flush();
    expect(calls[1].url).toContain(`ai/drafts/${DRAFT}/apply`);
    expect(calls[1].body).toEqual({ lines: [{ booking_line_id: LINE, action: "confirm", confirmation_no: "SKY7Q2" }] });
    expect(document.querySelector('[data-testid="ai-supplier-confirm-done"]')?.getAttribute("data-decision")).toBe("accepted");
  });
});

describe("AI ideas tab", () => {
  it("runs for the day and adds an idea through the normal AA add", async () => {
    const result: NotesOptionsResult = {
      ...OUTCOME, ai_draft_id: DRAFT, proposal_day_id: DAY, proposal_id: PROPOSAL, closed_list_size: 5, rejected: 2,
      reasons: { not_in_closed_list: 2 },
      options: [{ code: "A3", tour_source_id: "tour-river", title: "River Lantern Cruise", component_id: "c2",
                  component_kind: "activity", tour_day: 2, why: "Loves boats." }],
    };
    const calls = fetchReturning({ "ai/options": result });
    const writes: { path: string; body: Record<string, unknown> }[] = [];
    const write = async (_m: "POST", path: string, body: Record<string, unknown>) => {
      writes.push({ path, body });
      return true;
    };
    const el = mount(createElement(BuilderAiIdeasTab, { view: VIEW, blockId: BLOCK, lang: "en", write, onAdded: () => {} }));
    await act(async () => {
      (el.querySelector('[data-testid="builder-ai-ideas-run"]') as HTMLButtonElement).click();
    });
    await flush();
    expect(calls[0].url).toContain(`proposal-days/${DAY}/ai/options`);
    const section = el.querySelector('[data-testid="builder-ai-ideas-result"]')!;
    expect(section.textContent).toContain("Loves boats.");
    expect(section.textContent).toContain(aiNoticeText("staff", "en"));
    await act(async () => {
      (el.querySelector('[data-testid="builder-ai-idea"] button') as HTMLButtonElement).click();
    });
    expect(writes).toEqual([
      { path: `proposals/${PROPOSAL}/blocks/${BLOCK}/aa-options`, body: { tour_source_id: "tour-river", component_id: "c2", component_kind: "activity" } },
    ]);
  });
});

describe("voices panel", () => {
  it("shows the limit and disables Add at it", () => {
    const view: VoicesView = {
      per_member_max: 1, mine: 1, descriptors: { formality: ["neutral", "formal"], length: ["short", "long"] },
      voices: [{ voice_profile_id: VOICE, member_id: "m", mine: true, can_edit: true, name: "Warm and short", locale: "en",
                 channel: "email", descriptors: { formality: "formal", length: "short" }, created_at: "", updated_at: "" }],
    };
    const html = renderToString(createElement(AiVoicesPanel, { view, lang: "en" }));
    expect(html).toContain("You keep 1 of 1 voices.");
    expect(html).toContain("Warm and short");
    expect(html.match(/<button[^>]*data-testid="ai-voices-add"[^>]*>/)?.[0]).toContain("disabled");
  });
});
