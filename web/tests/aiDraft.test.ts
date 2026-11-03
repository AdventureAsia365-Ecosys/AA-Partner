// @vitest-environment jsdom
// AA-1027 slice F3 (gap CRM-4; design v3 R39 "AI drafts, humans send"): the AI draft card of the trip workspace — the
// Draft action disabled with the reason (no ai_assist on the plan, AI off: the owner gets the settings link), the
// drawer asks for a draft, shows it with the kit staff AiNotice and accepts (clipboard + POST accept with the feedback
// code) or discards it; a draft the gateway could not write says "write it by hand"; an API refusal is worded; the
// BFF allow-list lines; the pure rules. Invented names only.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { aiNoticeText } from "@/app/_kit";
import type { AiSettings } from "@/lib/ai";
import {
  FEEDBACK_CODES,
  draftAvailability,
  draftErrorKey,
  draftInputError,
  fallbackKey,
  unavailableKey,
  type DraftResult,
} from "@/lib/aiDraft";
import { matchBff } from "@/lib/bff";
import { MESSAGES } from "@/lib/i18n";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {}, push: () => {} }) }));
const { AiDraftPanel } = await import("@/components/pipeline/AiDraftPanel");

const TRIP = "00000000-0000-4000-8000-0000000000d1";
const DRAFT = "00000000-0000-4000-8000-0000000000f1";

const SETTINGS: AiSettings = {
  ai_enabled: true,
  ai_budget_usd: "2.00",
  budget_max_usd: "2.00",
  spent_usd_month: "0.0000",
  budget_used_pct: 0,
  alert_level: null,
  period_start: "2026-11-01",
  provider: "stub",
  test_mode: true,
  rate_limit_per_minute: 6,
  can_edit: false,
  ai_assist_allowed: true,
};

function result(over: Partial<DraftResult> = {}): DraftResult {
  return {
    ai_draft_id: DRAFT,
    outcome: "ok",
    text: "Dear Ines Varga, the lantern walk is on day three.",
    fallback: null,
    refusal_code: null,
    error_class: null,
    llm_call_id: "00000000-0000-4000-8000-000000000001",
    budget_used_pct: 0,
    budget_alert: null,
    truncated: false,
    approval_required: true,
    drafted_at: "2026-11-02T09:00:00+00:00",
    ...over,
  };
}

const roots: Root[] = [];
afterEach(() => {
  for (const r of roots.splice(0)) act(() => r.unmount());
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

async function mount(settings: AiSettings | null, isOwner = false, lang: "en" | "vi" = "en"): Promise<HTMLElement> {
  const container = document.createElement("div");
  document.body.appendChild(container);
  await act(async () => {
    const root = createRoot(container);
    roots.push(root);
    root.render(createElement(AiDraftPanel, { tripId: TRIP, settings, isOwner, lang }));
  });
  return container;
}

type Call = { method: string; url: string; body: unknown };

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function stubFetch(answer: (call: Call) => Response): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const call = { method: init?.method ?? "GET", url, body: init?.body ? JSON.parse(String(init.body)) : undefined };
      calls.push(call);
      return answer(call);
    }),
  );
  return calls;
}

const q = (c: HTMLElement, id: string) => c.querySelector(`[data-testid="${id}"]`) as HTMLElement | null;

async function click(el: HTMLElement | null) {
  await act(async () => el!.dispatchEvent(new MouseEvent("click", { bubbles: true })));
}

async function typeInto(el: HTMLInputElement | HTMLTextAreaElement, value: string) {
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  await act(async () => {
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

async function submit(c: HTMLElement) {
  const form = q(c, "ai-draft-form") as HTMLFormElement;
  await act(async () => form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
}

async function openAndWrite(c: HTMLElement, text = "Is the lantern walk included?") {
  await click(q(c, "ai-draft-open"));
  await typeInto(q(c, "ai-draft-drawer")!.querySelector("textarea")!, text);
  await submit(c);
}

describe("AI draft rules", () => {
  it("puts the plan before the switch and a failed read last", () => {
    expect(draftAvailability(SETTINGS)).toBe("ready");
    expect(draftAvailability({ ...SETTINGS, ai_assist_allowed: false, ai_enabled: false })).toBe("no_plan");
    expect(draftAvailability({ ...SETTINGS, ai_enabled: false })).toBe("off");
    expect(draftAvailability(null)).toBe("unknown");
    const older: AiSettings = { ...SETTINGS };
    delete older.ai_assist_allowed; // an API before F3: the draft call itself answers for the plan
    expect(draftAvailability(older)).toBe("ready");
    expect(unavailableKey("off", true)).toBe("aiDraft.off.owner");
    expect(unavailableKey("off", false)).toBe("aiDraft.off.member");
    expect(unavailableKey("no_plan", true)).toBe("aiDraft.noPlan");
    expect(unavailableKey("unknown", false)).toBe("aiDraft.unknown");
  });

  it("words every refusal and fallback it names, in both languages", () => {
    expect(draftErrorKey("feature_not_allowed")).toBe("aiDraft.error.feature_not_allowed");
    expect(draftErrorKey("something_new")).toBe("aiDraft.error.generic");
    expect(fallbackKey({ outcome: "refused", refusal_code: "budget_exhausted", truncated: false })).toBe("aiDraft.fallback.budget");
    expect(fallbackKey({ outcome: "refused", refusal_code: "stage_disabled", truncated: false })).toBe("aiDraft.fallback.paused");
    expect(fallbackKey({ outcome: "refused", refusal_code: "app_disabled", truncated: false })).toBe("aiDraft.fallback.paused");
    expect(fallbackKey({ outcome: "refused", refusal_code: "app_budget_exhausted", truncated: false })).toBe("aiDraft.fallback.budget");
    expect(fallbackKey({ outcome: "refused", refusal_code: "rate_limited", truncated: false })).toBe("aiDraft.fallback.rate_limited");
    expect(fallbackKey({ outcome: "error", refusal_code: null, truncated: true })).toBe("aiDraft.fallback.truncated");
    expect(fallbackKey({ outcome: "error", refusal_code: null, truncated: false })).toBe("aiDraft.fallback.generic");
    expect(draftInputError("  ", "")).toBe("aiDraft.error.text_required");
    expect(draftInputError("x".repeat(8001), "")).toBe("aiDraft.error.text_long");
    expect(draftInputError("hi", "y".repeat(501))).toBe("aiDraft.error.instruction_long");
    expect(draftInputError("hi", "")).toBeNull();
    const keys = [
      "aiDraft.error.feature_not_allowed", "aiDraft.error.ai_disabled", "aiDraft.error.rate_limited",
      "aiDraft.error.draft_decided", "aiDraft.error.assigned_only", "aiDraft.error.role_forbidden",
      "aiDraft.error.trip_not_found", "aiDraft.error.text_required", "aiDraft.error.generic",
      ...FEEDBACK_CODES.map((code) => `aiDraft.feedback.${code}`),
    ];
    for (const lang of ["en", "vi"] as const) {
      for (const key of keys) expect(MESSAGES[lang][key as keyof typeof MESSAGES.en], `${lang} ${key}`).toBeTruthy();
    }
  });

  it("lets the web ask for a draft and decide on it, nothing else", () => {
    expect(matchBff("POST", `ai/drafts/${DRAFT}/accept`)?.tenant).toBe(true);
    expect(matchBff("POST", `ai/drafts/${DRAFT}/discard`)?.tenant).toBe(true);
    expect(matchBff("POST", `ai/drafts/${DRAFT}/send`)).toBeNull();
    expect(matchBff("GET", `ai/drafts/${DRAFT}/accept`)).toBeNull();
    expect(matchBff("POST", "ai/drafts/not-a-uuid/accept")).toBeNull();
  });
});

describe("the Draft action", () => {
  it("is disabled with the plan's reason when the agency has no AI assistant", async () => {
    const c = await mount({ ...SETTINGS, ai_assist_allowed: false }, true);
    expect((q(c, "ai-draft-open") as HTMLButtonElement).disabled).toBe(true);
    expect(q(c, "ai-draft-unavailable")!.textContent).toBe(MESSAGES.en["aiDraft.noPlan"]);
    expect(c.querySelector('a[href="/settings/ai"]')).toBeNull();
  });

  it("with AI off, links the owner to the settings and tells the others to ask the owner", async () => {
    const owner = await mount({ ...SETTINGS, ai_enabled: false }, true);
    expect((q(owner, "ai-draft-open") as HTMLButtonElement).disabled).toBe(true);
    expect(owner.querySelector('a[href="/settings/ai"]')).not.toBeNull();
    const agent = await mount({ ...SETTINGS, ai_enabled: false }, false);
    expect(q(agent, "ai-draft-unavailable")!.textContent).toBe(MESSAGES.en["aiDraft.off.member"]);
    expect(agent.querySelector('a[href="/settings/ai"]')).toBeNull();
  });

  it("server-renders the card in Vietnamese when the person picked it", () => {
    const html = renderToString(createElement(AiDraftPanel, { tripId: TRIP, settings: SETTINGS, isOwner: false, lang: "vi" }));
    expect(html).toContain(MESSAGES.vi["aiDraft.title"]);
    expect(html).toContain(MESSAGES.vi["aiDraft.open"]);
    expect(html).not.toContain('data-testid="ai-draft-unavailable"');
  });
});

describe("the draft drawer", () => {
  it("asks for a draft, shows it with the staff AI notice, copies and accepts it with the feedback code", async () => {
    const calls = stubFetch((call) =>
      call.url.endsWith("/ai/draft")
        ? json(200, result())
        : json(200, { ai_draft_id: DRAFT, trip_id: TRIP, decision: "accepted", feedback_code: "good", decided_at: "x" }),
    );
    const writeText = vi.fn(async () => {});
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    const c = await mount(SETTINGS);
    await click(q(c, "ai-draft-open"));
    await typeInto(q(c, "ai-draft-drawer")!.querySelector("input")!, "keep it short");
    await typeInto(q(c, "ai-draft-drawer")!.querySelector("textarea")!, "Is the lantern walk included?");
    await submit(c);
    expect(calls[0]).toEqual({
      method: "POST",
      url: `/bff/trips/${TRIP}/ai/draft`,
      body: { kind: "reply", text: "Is the lantern walk included?", instruction: "keep it short" },
    });
    expect(q(c, "ai-draft-text")!.textContent).toBe(result().text);
    const notice = q(c, "ai-draft-result")!.querySelector('[data-testid="ai-notice"]');
    expect(notice?.getAttribute("data-ai-notice")).toBe("staff");
    expect(notice?.textContent).toBe(aiNoticeText("staff", "en"));
    const select = q(c, "ai-draft-result")!.querySelector("select") as HTMLSelectElement;
    await act(async () => {
      select.value = "good";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await click(q(c, "ai-draft-accept"));
    expect(writeText).toHaveBeenCalledWith(result().text);
    expect(calls[1]).toEqual({ method: "POST", url: `/bff/ai/drafts/${DRAFT}/accept`, body: { feedback_code: "good" } });
    expect(q(c, "ai-draft-done")!.dataset.decision).toBe("accepted");
    expect(q(c, "ai-draft-done")!.textContent).toBe(MESSAGES.en["aiDraft.accepted"]);
    expect(q(c, "ai-draft-accept")).toBeNull();
  });

  it("says in test mode whether the privacy check is billed (never 'costs nothing' while it is)", async () => {
    const billed = await mount({ ...SETTINGS, test_mode: false, model_test_mode: true, scrub_billed: true });
    await click(q(billed, "ai-draft-open"));
    expect(q(billed, "ai-draft-test-mode")!.textContent).toContain(MESSAGES.en["aiDraft.testModeScrub"]);
    expect(q(billed, "ai-draft-test-mode")!.textContent).not.toContain(MESSAGES.en["aiDraft.testMode"]);
    const free = await mount({ ...SETTINGS, test_mode: true, model_test_mode: true, scrub_billed: false });
    await click(q(free, "ai-draft-open"));
    expect(q(free, "ai-draft-test-mode")!.textContent).toContain(MESSAGES.en["aiDraft.testMode"]);
    const real = await mount({ ...SETTINGS, provider: "bedrock", test_mode: false, model_test_mode: false, scrub_billed: true });
    await click(q(real, "ai-draft-open"));
    expect(q(real, "ai-draft-test-mode")).toBeNull();
  });

  it("discards without feedback, and says so", async () => {
    const calls = stubFetch((call) =>
      call.url.endsWith("/ai/draft")
        ? json(200, result())
        : json(200, { ai_draft_id: DRAFT, trip_id: TRIP, decision: "discarded", feedback_code: null, decided_at: "x" }),
    );
    const c = await mount(SETTINGS);
    await openAndWrite(c);
    await click(q(c, "ai-draft-discard"));
    expect(calls[1]).toEqual({ method: "POST", url: `/bff/ai/drafts/${DRAFT}/discard`, body: {} });
    expect(q(c, "ai-draft-done")!.textContent).toBe(MESSAGES.en["aiDraft.discarded"]);
  });

  it("a draft the AI could not write says to reply by hand and offers no accept", async () => {
    stubFetch(() =>
      json(200, result({ ai_draft_id: null, outcome: "refused", text: null, refusal_code: "budget_exhausted", fallback: "manual_reply_needed" })),
    );
    const c = await mount(SETTINGS);
    await openAndWrite(c);
    expect(q(c, "ai-draft-fallback")!.textContent).toBe(MESSAGES.en["aiDraft.fallback.budget"]);
    expect(q(c, "ai-draft-result")).toBeNull();
  });

  it("words a 403 for the plan and never calls the API without the client's message", async () => {
    const calls = stubFetch(() => json(403, { detail: "feature_not_allowed" }));
    const c = await mount(SETTINGS);
    await click(q(c, "ai-draft-open"));
    await submit(c);
    expect(calls).toEqual([]);
    expect(c.querySelector('[role="alert"]')!.textContent).toBe(MESSAGES.en["aiDraft.error.text_required"]);
    await typeInto(q(c, "ai-draft-drawer")!.querySelector("textarea")!, "Can we swap the hotel?");
    await submit(c);
    expect(c.querySelector('[role="alert"]')!.textContent).toBe(MESSAGES.en["aiDraft.error.feature_not_allowed"]);
  });
});
