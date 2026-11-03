// AA-964 (CRM-33a): Settings > AI — the meter tone at the 75 / 90 / 100 % alert levels, budget checks, refusal
// wording, the BFF allow-list lines, and the page for the owner (form) and another role (read only, no form).
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { aiErrorKey, budgetError, meterTone, meterWidth, notOnPlan, testModeNote, usd, type AiSettings } from "@/lib/ai";
import { matchBff } from "@/lib/bff";
import { MESSAGES } from "@/lib/i18n";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {} }) }));
const { AiSettingsView } = await import("@/components/settings/AiSettingsView");

const TRIP = "00000000-0000-4000-8000-0000000000d1";

const VIEW: AiSettings = {
  ai_enabled: false,
  ai_budget_usd: "2.00",
  budget_max_usd: "2.00",
  spent_usd_month: "0.0000",
  budget_used_pct: 0,
  alert_level: null,
  period_start: "2026-10-01",
  provider: "stub",
  test_mode: true,
  rate_limit_per_minute: 6,
  can_edit: true,
};

describe("AI settings helpers", () => {
  it("colours the meter at the alert levels", () => {
    expect([0, 74.9, 75, 90, 99.9, 100, 140].map(meterTone)).toEqual([
      "success", "success", "warning", "warning", "warning", "danger", "danger",
    ]);
    expect([-1, 50, 140, Number.NaN].map(meterWidth)).toEqual([0, 50, 100, 0]);
  });

  it("checks a budget like the API does", () => {
    expect(budgetError("1.50", "2.00")).toBeNull();
    expect(budgetError("0", "2.00")).toBeNull();
    expect(budgetError("2.01", "2.00")).toBe("ai.error.budget_above_plan");
    expect(budgetError("1.555", "2.00")).toBe("ai.error.budget_format");
    expect(budgetError("abc", "2.00")).toBe("ai.error.budget_format");
    expect(aiErrorKey("budget_above_plan")).toBe("ai.error.budget_above_plan");
    expect(aiErrorKey("something_else")).toBe("ai.error.generic");
    for (const key of ["ai.error.budget_format", "ai.error.budget_above_plan", "ai.error.generic", "ai.alert.75", "ai.alert.90", "ai.alert.100"]) {
      expect(MESSAGES.en[key as keyof typeof MESSAGES.en]).toBeTruthy();
    }
    expect(usd("1.5", "en-AU")).toContain("1.50");
  });

  it("says nothing is charged only when the API says no billable step runs", () => {
    expect(testModeNote(null)).toBeNull();
    expect(testModeNote({ ...VIEW, test_mode: true, model_test_mode: true, scrub_billed: false })).toBe("free");
    expect(testModeNote({ ...VIEW, test_mode: false, model_test_mode: true, scrub_billed: true })).toBe("scrub_billed");
    expect(testModeNote({ ...VIEW, provider: "bedrock", test_mode: false, model_test_mode: false, scrub_billed: true })).toBeNull();
    // An older API sends only test_mode (the stub model): the note does not claim the privacy check is free.
    expect(testModeNote({ ...VIEW, test_mode: true })).toBe("scrub_billed");
    expect(notOnPlan({ ...VIEW, ai_assist_allowed: false })).toBe(true);
    expect(notOnPlan({ ...VIEW, ai_assist_allowed: true })).toBe(false);
    expect(notOnPlan(VIEW)).toBe(false);
  });

  it("lets the web read and change the settings and ask for a draft, nothing else", () => {
    expect(matchBff("GET", "settings/ai")?.tenant).toBe(true);
    expect(matchBff("PUT", "settings/ai")?.tenant).toBe(true);
    expect(matchBff("POST", `trips/${TRIP}/ai/draft`)?.tenant).toBe(true);
    expect(matchBff("DELETE", "settings/ai")).toBeNull();
    expect(matchBff("POST", "trips/not-a-uuid/ai/draft")).toBeNull();
    expect(matchBff("POST", `trips/${TRIP}/ai/send`)).toBeNull();
  });
});

describe("AI settings page", () => {
  it("shows the owner the form, the test-mode note and the meter", () => {
    const html = renderToString(createElement(AiSettingsView, { settings: VIEW, lang: "en", locale: "en-AU" }));
    expect(html).toContain('data-testid="ai-settings-form"');
    expect(html).toContain("Let the team ask for AI drafts");
    expect(html).toContain('data-testid="ai-test-mode"');
    expect(html).toContain('role="meter"');
    expect(html).not.toContain('data-testid="ai-owner-only"');
  });

  it("shows other roles the state without a form, and the alert when the budget is used up", () => {
    const html = renderToString(
      createElement(AiSettingsView, {
        settings: { ...VIEW, can_edit: false, ai_enabled: true, test_mode: false, budget_used_pct: 100, alert_level: 100 },
        lang: "en",
        locale: "en-AU",
      }),
    );
    expect(html).toContain('data-testid="ai-owner-only"');
    expect(html).not.toContain("<form");
    expect(html).toContain("budget is used up");
    expect(html).not.toContain('data-testid="ai-test-mode"');
  });

  it("says the privacy check is billed while the test model writes the drafts", () => {
    const free = renderToString(
      createElement(AiSettingsView, { settings: { ...VIEW, model_test_mode: true, scrub_billed: false }, lang: "en", locale: "en-AU" }),
    );
    expect(free).toContain("nothing is charged");
    const billed = renderToString(
      createElement(AiSettingsView, {
        settings: { ...VIEW, test_mode: false, model_test_mode: true, scrub_billed: true },
        lang: "en",
        locale: "en-AU",
      }),
    );
    expect(billed).toContain('data-testid="ai-test-mode"');
    expect(billed).toContain("Only the privacy check");
    expect(billed).not.toContain("nothing is charged");
    const vi = renderToString(
      createElement(AiSettingsView, {
        settings: { ...VIEW, test_mode: false, model_test_mode: true, scrub_billed: true },
        lang: "vi",
        locale: "vi-VN",
      }),
    );
    expect(vi).toContain("Chỉ bước kiểm tra quyền riêng tư");
    expect(vi).not.toContain("không tính phí");
  });

  it("shows the plan state when the plan lacks the AI assistant, with the trip card's wording", () => {
    const html = renderToString(
      createElement(AiSettingsView, { settings: { ...VIEW, ai_enabled: true, ai_assist_allowed: false }, lang: "en", locale: "en-AU" }),
    );
    expect(html).toContain('data-testid="ai-no-plan"');
    expect(html).toContain('data-testid="ai-status-no-plan"');
    expect(html).toContain("The AI assistant is not on your agency");
    expect(MESSAGES.en["aiDraft.noPlan"]).toContain("The AI assistant is not on your agency");
    expect(html).toContain("You can still switch AI on now");
    expect(html).toContain('data-testid="ai-settings-form"'); // the owner keeps the switch
    const member = renderToString(
      createElement(AiSettingsView, {
        settings: { ...VIEW, can_edit: false, ai_assist_allowed: false },
        lang: "en",
        locale: "en-AU",
      }),
    );
    expect(member).toContain("The AI assistant is not on your agency");
    expect(member).not.toContain("You can still switch AI on now");
    const allowed = renderToString(
      createElement(AiSettingsView, { settings: { ...VIEW, ai_assist_allowed: true }, lang: "en", locale: "en-AU" }),
    );
    expect(allowed).not.toContain('data-testid="ai-no-plan"');
    expect(allowed).not.toContain('data-testid="ai-status-no-plan"');
  });

  it("speaks Vietnamese only when the person picked it", () => {
    const html = renderToString(createElement(AiSettingsView, { settings: VIEW, lang: "vi", locale: "vi-VN" }));
    expect(html).toContain("Cho phép đội yêu cầu bản nháp AI");
  });
});
