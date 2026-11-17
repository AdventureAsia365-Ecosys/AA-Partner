// @vitest-environment jsdom
// AA-1126 L1 / L2 — the Content studio side of a used-up AI allowance: a version held by the plan's AI writing
// allowance reads "AI allowance used up" (not "Writing failed"), its row offers "Upgrade plan" and keeps Retry off; a
// rewrite or Retry refused with 402 says the real reason; the bell's allowance rows (studio writing, trip planner) are
// worded in the UI language and link to the plan page. Invented ids and names only.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AllowanceHeldActions, resetsOnLabel } from "@/app/(studio)/portal/_components/AllowanceHeld";
import { AllowanceNote, type TenantNotification } from "@/app/(studio)/portal/_components/NotificationsBell";
import {
  isAllowanceHeld,
  isRewriteFailed,
  requestRewrite,
  retryRewrite,
} from "@/app/(studio)/portal/_components/catalogApi";
import { COMMON_MESSAGES } from "@/app/(studio)/portal/_components/common.messages";
import { BELL_MESSAGES } from "@/app/(studio)/portal/_components/account.messages";
import { ERROR_MESSAGES } from "@/app/(studio)/portal/_components/errors.messages";
import { StudioError } from "@/app/(studio)/portal/_components/studioError";
import { tourWorkStatus, activityState } from "@/app/(studio)/portal/_components/workStatus";
import { PLAN_PATH } from "@/lib/apps";

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

async function mount(el: ReturnType<typeof createElement>) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  await act(async () => {
    const root = createRoot(container);
    roots.push(root);
    root.render(el);
  });
  return container;
}

const HELD = { status: "pending", edit_source: "ai_generated", job_status: "stopped_budget", job_stop_code: "ai_quota_exhausted" };
const FAILED = { status: "pending", edit_source: "ai_generated", job_status: "stopped_budget", job_stop_code: null };

describe("a version held by the AI allowance", () => {
  it("reads held, not failed, and only with the quota code", () => {
    expect(isAllowanceHeld(HELD)).toBe(true);
    expect(isAllowanceHeld(FAILED)).toBe(false);
    expect(isAllowanceHeld({ job_stop_code: undefined })).toBe(false);
    expect(tourWorkStatus(HELD)).toBe("held");
    expect(tourWorkStatus(FAILED)).toBe("failed");
    expect(isRewriteFailed(FAILED)).toBe(true);
    expect(activityState({ id: "v", status: "pending", edit_source: "ai_generated" }, HELD)).toBe("held");
  });

  it("has its wording in both languages, none of it says Writing failed", () => {
    for (const lang of ["en", "vi"] as const) {
      const m = COMMON_MESSAGES[lang];
      expect(m.wsAllowanceUsed).toBeTruthy();
      expect(m.upgradePlan).toBeTruthy();
      expect(m.allowanceUsedNote("1 December 2026")).toContain("1 December 2026");
      expect(m.allowanceUsedNote("")).not.toContain("undefined");
      expect(ERROR_MESSAGES[lang].aiAllowanceUsed).toBeTruthy();
      expect(BELL_MESSAGES[lang].allowanceBody("x")).toContain("x");
      expect(BELL_MESSAGES[lang].plannerAllowanceBody("x")).toContain("x");
    }
    expect(COMMON_MESSAGES.en.wsAllowanceUsed).not.toMatch(/failed/i);
  });

  it("formats the reset day per language and tolerates no date", () => {
    expect(resetsOnLabel("2026-12-01", "en")).toBe("1 December 2026");
    expect(resetsOnLabel("2026-12-01", "vi")).toBe("1/12/2026");
    expect(resetsOnLabel(null, "en")).toBe("");
    expect(resetsOnLabel("not a date", "en")).toBe("");
  });

  it("offers Upgrade plan and keeps Retry off", async () => {
    const c = await mount(createElement(AllowanceHeldActions, { resetsAt: "2026-12-01" }));
    const link = c.querySelector("a")!;
    expect(link.getAttribute("href")).toBe(PLAN_PATH);
    expect(link.textContent).toBe(COMMON_MESSAGES.en.upgradePlan);
    const retry = c.querySelector("button")!;
    expect(retry.disabled).toBe(true);
    expect(retry.getAttribute("title")).toContain("1 December 2026");
  });
});

describe("a request refused with 402 ai_quota_exhausted", () => {
  const refuse = () => vi.stubGlobal("fetch", vi.fn(async () => Response.json(
    { detail: { code: "ai_quota_exhausted", message: "Your plan's AI writing allowance for this month is used up." } },
    { status: 402 },
  )));

  it("a rewrite says the real reason", async () => {
    refuse();
    const err = await requestRewrite("pt-1", "en-US").catch((e) => e);
    expect(err).toBeInstanceOf(StudioError);
    expect((err as StudioError).code).toBe("aiAllowanceUsed");
  });

  it("a Retry says the real reason", async () => {
    refuse();
    const err = await retryRewrite("v-1").catch((e) => e);
    expect((err as StudioError).code).toBe("aiAllowanceUsed");
  });
});

describe("the bell's allowance rows", () => {
  const note = (event_type: string): TenantNotification => ({
    id: 7, event_type, title: "AI writing allowance used up", message: "English text from the API", tour_id: null,
    tour_name: null, reason: null, live_posts: 0, pages: [], upgrade: true, resets_at: "2026-12-01", is_read: false,
    created_at: "2026-11-17T08:00:00Z",
  });

  it("the studio row links to the plan page with the reset day", async () => {
    const c = await mount(createElement(AllowanceNote, { note: note("ai.allowance_used"), onNavigate: () => {} }));
    const a = c.querySelector("[data-testid=bell-allowance]")!;
    expect(a.getAttribute("href")).toBe(PLAN_PATH);
    expect(a.textContent).toContain(BELL_MESSAGES.en.allowanceTitle);
    expect(a.textContent).toContain("1 December 2026");
  });

  it("the trip planner row says the planner keeps working without AI-written days", async () => {
    const c = await mount(createElement(AllowanceNote, { note: note("tripplanner.ai_allowance_used"), onNavigate: () => {} }));
    expect(c.textContent).toContain(BELL_MESSAGES.en.plannerAllowanceTitle);
    expect(c.textContent).not.toContain("English text from the API");
  });
});
