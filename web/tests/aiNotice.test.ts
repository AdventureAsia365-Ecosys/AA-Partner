// AA-1033: the AI notice in CRM web — the kit staff notice under every AI draft surface (guard), Settings > AI (the
// "AI can make mistakes" rule first, the notice as a draft shows it, the link to the terms' AI section), the terms of
// use page (/terms#ai: mistakes, for information, what binds; never a liability waiver), the shell footer link, and
// the traveller notice on the public trip page.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { aiNoticeText } from "@/app/_kit";
import type { AiSettings } from "@/lib/ai";
import { MESSAGES } from "@/lib/i18n";
import { AI_TERMS, termsEn, termsVi } from "@/lib/i18nTerms";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {} }), usePathname: () => "/settings/ai" }));
const { AiSettingsView } = await import("@/components/settings/AiSettingsView");

const WEB = join(__dirname, "..");

const VIEW: AiSettings = {
  ai_enabled: true,
  ai_budget_usd: "2.00",
  budget_max_usd: "2.00",
  spent_usd_month: "0.0000",
  budget_used_pct: 0,
  alert_level: null,
  period_start: "2026-10-01",
  provider: "stub",
  test_mode: true,
  rate_limit_per_minute: 6,
  can_edit: false,
};

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const abs = join(dir, name);
    if (name === "node_modules" || name === ".next" || name === "core") return [];
    if (statSync(abs).isDirectory()) return sources(abs);
    return /\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name) ? [abs] : [];
  });
}

/** Files that ask for an AI draft (the BFF route trips/{id}/ai/draft) but render no kit AiNotice. */
function draftSurfacesWithoutNotice(files: { path: string; text: string }[]): string[] {
  return files
    .filter((f) => /ai\/draft/.test(f.text) && !/<AiNotice\b/.test(f.text))
    .map((f) => f.path);
}

describe("AI draft surfaces", () => {
  it("every component or page that asks for an AI draft renders the kit AiNotice", () => {
    const files = [...sources(join(WEB, "app")), ...sources(join(WEB, "components"))].map((abs) => ({
      path: relative(WEB, abs),
      text: readFileSync(abs, "utf8"),
    }));
    expect(files.length).toBeGreaterThan(50); // the scan sees the code
    expect(draftSurfacesWithoutNotice(files)).toEqual([]);
  });

  it("the guard catches a draft panel without the notice", () => {
    const panel = 'await bffSend("POST", `trips/${id}/ai/draft`, body); return <pre>{draft}</pre>;';
    expect(draftSurfacesWithoutNotice([{ path: "components/x/DraftPanel.tsx", text: panel }])).toEqual([
      "components/x/DraftPanel.tsx",
    ]);
    expect(draftSurfacesWithoutNotice([{ path: "ok.tsx", text: `${panel} <AiNotice lang={lang} />` }])).toEqual([]);
  });
});

describe("Settings > AI", () => {
  it("lists 'AI can make mistakes' first, shows the staff notice and links the terms' AI section", () => {
    const html = renderToString(createElement(AiSettingsView, { settings: VIEW, lang: "en", locale: "en-AU" }));
    const rules = html.slice(html.indexOf('data-testid="ai-rules"'));
    expect(rules.indexOf("AI can make mistakes: check facts before sending.")).toBeLessThan(
      rules.indexOf(MESSAGES.en["ai.rule.review"]),
    );
    expect(html).toContain('data-testid="ai-notice-example"');
    expect(html).toContain('role="note"');
    expect(html).toContain(aiNoticeText("staff", "en"));
    expect(html).toContain('href="/terms#ai"');
  });

  it("speaks Vietnamese, the notice included, when the person picked it", () => {
    const html = renderToString(createElement(AiSettingsView, { settings: VIEW, lang: "vi", locale: "vi-VN" }));
    expect(html).toContain(MESSAGES.vi["ai.rule.mistakes"]);
    expect(html).toContain(aiNoticeText("staff", "vi"));
    expect(html).not.toContain(aiNoticeText("staff", "en"));
  });
});

describe("terms of use: AI-generated content", () => {
  it("has every key in both languages, none empty", () => {
    expect(Object.keys(termsVi).sort()).toEqual(Object.keys(termsEn).sort());
    for (const key of Object.keys(termsEn) as (keyof typeof termsEn)[]) {
      expect(termsVi[key].trim(), key).not.toBe("");
      expect(MESSAGES.en[key]).toBe(termsEn[key]);
    }
  });

  it("says AI can be wrong, that a draft is for information and what binds, and waives nothing", () => {
    const text = AI_TERMS.map((key) => termsEn[key]).join(" ");
    expect(text).toContain("can contain mistakes");
    expect(text).toContain("for information only");
    expect(text).toContain("confirmed quote and the booking terms");
    expect(text).toContain("Nothing reaches a client until a person in your agency reviews it and sends it.");
    expect(text).not.toMatch(/liab|not responsible|no responsibility|disclaim/i);
  });

  it("the page renders the section with id ai, and the shell footer links it", () => {
    const page = readFileSync(join(WEB, "app/(crm)/terms/page.tsx"), "utf8");
    expect(page).toContain('<section id="ai"');
    expect(page).toContain("AI_TERMS.map");
    const shell = readFileSync(join(WEB, "app/_kit/local/CrmShell.tsx"), "utf8");
    expect(shell).toMatch(/<footer data-crm="footer">[\s\S]*<Link href="\/terms"[\s\S]*dict\["terms.link"\]/);
  });
});

describe("public trip page", () => {
  it("shows the traveller notice under the AI-written tour text, before the call to action", () => {
    const page = readFileSync(join(WEB, "app/trip/[tourId]/page.tsx"), "utf8");
    const notice = page.indexOf('<AiNotice variant="traveller"');
    expect(notice).toBeGreaterThan(page.indexOf("itineraryParagraphs(trip.itineraries)"));
    expect(notice).toBeLessThan(page.indexOf("Design This Journey"));
  });
});
