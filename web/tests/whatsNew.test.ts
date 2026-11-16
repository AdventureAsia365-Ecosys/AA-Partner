// AA-970 (design v3 card CRM-37b, R42): the What's new feed and the per-agency preview switch. E (card): an agency on
// preview sees the flagged UI (the Help page's Preview panel and the preview entries of What's new); every other
// agency does not. The feed has both languages, unique ids, valid dates, links only for the roles that see that part
// of the app, and no internal ids in its words. The page and the kit components are real; the context is replaced.
import { renderToString } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MESSAGES } from "@/lib/i18n";
import { PREVIEW_FEATURES, isPreviewFlag, previewFeatures, previewOn } from "@/lib/preview";
import { WHATS_NEW, entryHref, visibleEntries } from "@/lib/whatsNew";

const state: { role: "owner" | "agent" | "assistant"; preview: boolean | undefined; lang: "en" | "vi" } = {
  role: "owner",
  preview: false,
  lang: "en",
};

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {} }), redirect: () => {} }));
vi.mock("@/lib/crmContext", () => ({
  crmContext: async () => ({
    apiDown: false,
    lang: state.lang,
    locale: state.lang === "vi" ? "vi-VN" : "en-AU",
    role: state.role,
    cfg: {},
    me: { tenant_id: "t", member_id: "m", member_role: state.role, display_name: "Invented Lotus Travel", default_locale: "en-AU", preview: state.preview },
  }),
}));

const { default: HelpPage } = await import("@/app/(crm)/help/page");

async function help(): Promise<string> {
  return renderToString((await HelpPage())!);
}

beforeEach(() => {
  state.role = "owner";
  state.preview = false;
  state.lang = "en";
});

describe("the preview switch (E: an agency on preview sees the flagged UI; others do not)", () => {
  it("previewOn is true only when /me says preview: true", () => {
    expect(previewOn({ preview: true }, "help.preview-panel")).toBe(true);
    for (const me of [{ preview: false }, {}, null, undefined]) expect(previewOn(me, "help.preview-panel")).toBe(false);
    expect(previewOn({ preview: true }, "no.such-flag" as never)).toBe(false);
    expect(previewFeatures({ preview: false })).toEqual([]);
    expect(previewFeatures({ preview: true }).map((f) => f.flag)).toEqual(Object.keys(PREVIEW_FEATURES));
  });

  it("an agency on preview sees the Preview panel and the preview entries", async () => {
    state.preview = true;
    const html = await help();
    expect(html).toContain('data-testid="help-preview"');
    expect(html).toContain('data-testid="help-preview-help.preview-panel"');
    expect(html).toContain('data-testid="whats-new-preview-channel"');
    expect(html).toContain('data-testid="whats-new-preview-badge"');
  });

  it.each([false, undefined])("an agency not on preview (%s) sees neither", async (preview) => {
    state.preview = preview;
    const html = await help();
    expect(html).not.toContain('data-testid="help-preview"');
    expect(html).not.toContain("whats-new-preview-channel");
    expect(html).not.toContain("whats-new-preview-badge");
    // The released entries are for everyone.
    expect(html).toContain('data-testid="whats-new-help-centre"');
  });

  it("visibleEntries keeps preview entries for an agency on preview only, newest first", () => {
    const off = visibleEntries({ preview: false });
    const on = visibleEntries({ preview: true });
    expect(off.some((e) => e.preview)).toBe(false);
    expect(on.filter((e) => e.preview).map((e) => e.id)).toEqual(WHATS_NEW.filter((e) => e.preview).map((e) => e.id));
    for (const list of [off, on]) expect([...list].map((e) => e.date)).toEqual([...list].map((e) => e.date).sort().reverse());
  });
});

describe("the What's new feed", () => {
  it("every entry: unique id, a real date, both languages, a known preview flag", () => {
    const ids = WHATS_NEW.map((e) => e.id);
    expect(new Set(ids).size).toBe(ids.length);
    for (const e of WHATS_NEW) {
      expect(e.date, e.id).toMatch(/^\d{4}-\d{2}-\d{2}$/);
      expect(Number.isNaN(Date.parse(e.date)), e.id).toBe(false);
      for (const lang of ["en", "vi"] as const) {
        expect(e.text[lang].title.trim().length, `${e.id} ${lang}`).toBeGreaterThan(3);
        expect(e.text[lang].body.trim().length, `${e.id} ${lang}`).toBeGreaterThan(20);
      }
      expect(e.text.vi.title).not.toBe(e.text.en.title);
      if (e.preview) expect(isPreviewFlag(e.preview), e.id).toBe(true);
      expect(`${e.text.en.title} ${e.text.en.body} ${e.text.vi.title} ${e.text.vi.body}`).not.toMatch(/\b[A-Z]{2,}-\d+\b/);
    }
  });

  it("a link shows only to a role that sees that part of the app", () => {
    const services = WHATS_NEW.find((e) => e.id === "service-catalogue")!;
    expect(entryHref(services, "owner")).toBe("/services");
    const hidden = { ...services, section: "commissions" };
    expect(entryHref(hidden, "assistant")).toBeNull();
    expect(entryHref(hidden, "agent")).toBe("/services");
    expect(entryHref({ ...services, href: undefined }, "owner")).toBeNull();
  });

  it("renders in Vietnamese with the Vietnamese entries", async () => {
    state.lang = "vi";
    const html = await help();
    expect(html).toContain(MESSAGES.vi["help.whatsNew.title"]);
    expect(html).toContain(WHATS_NEW.find((e) => e.id === "help-centre")!.text.vi.title);
  });
});

describe("the help topics beyond getting started (R43)", () => {
  it("shows the CRM areas and the links to the public data pages", async () => {
    const html = await help();
    for (const topic of ["pipeline", "clients", "proposals", "inbox", "tasks"]) {
      expect(html).toContain(`data-testid="help-using-${topic}"`);
    }
    expect(html).toContain('data-testid="help-mailbox"'); // the getting-started guides stay
    expect(html).toContain('href="/legal/data"');
    expect(html).toContain('href="/legal/subprocessors"');
    expect(html).toContain('href="/settings/data"');
  });
});
