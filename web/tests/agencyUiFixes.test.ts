// AA-1129 (S283 UI audit, agency app: P3 translation and formats, P5 client host, A1 Academy). Fake names and values
// only: the agency "Lữ hành Hoa Sen", the tour "Day 1 — Arrival in Testville".
import { readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import { renderToString } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearFormMemo, type PublicForm } from "@/lib/intakeForms";
import { MESSAGES, plural } from "@/lib/i18n";
import { paxSplitText } from "@/lib/travelOps";
import { activityLabel, activityLabelKey } from "@/lib/studio/account";
import { ACTIVITY_LABEL_MESSAGES } from "@/lib/studio/activityLabels.messages";
import { dayBlock, itineraryParagraphs } from "@/lib/tripPage";
import { pathProgress, sourceTitle, type PathSummary } from "@/lib/academy";
import { academyEn, academyVi, say } from "@/lib/i18nAcademy";
import { testConfig } from "./helpers";

const req = { cookie: undefined as string | undefined, hostKind: "client" as string | null };

vi.mock("next/headers", () => ({
  cookies: async () => ({
    get: (name: string) => (name === "aa_crm_locale" && req.cookie ? { name, value: req.cookie } : undefined),
  }),
  headers: async () => new Headers(req.hostKind ? { "x-aa-host-kind": req.hostKind } : {}),
}));

const { default: InquiryPage, generateMetadata: formMetadata } = await import("@/app/c/f/[publicKey]/page");
const { default: FormNotFound } = await import("@/app/c/f/[publicKey]/not-found");
const { default: NotFound } = await import("@/app/not-found");
const { generateMetadata: rootMetadata } = await import("@/app/layout");

const WEB = path.resolve(__dirname, "..");
const read = (rel: string) => readFileSync(path.join(WEB, rel), "utf8");

const KEY = "ZyXwVuTsRqPoNmLkJiHgFe";
const FORM: PublicForm = {
  schema_key: "stage1.v1",
  version: 1,
  fields: [{ name: "destinations", type: "multi_select", required: true, options: ["vietnam"] }],
  honeypot_field: "website",
  agency_name: "Lữ hành Hoa Sen",
  default_locale: "vi-VN",
  allowed_origins: [],
  privacy_notice_url: null,
};

beforeEach(() => {
  testConfig();
  clearFormMemo();
  req.cookie = undefined;
  req.hostKind = "client";
});
afterEach(() => vi.unstubAllGlobals());

function answer(status: number, body: unknown = {}) {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(body), { status })));
}

const props = (lang?: string) => ({ params: Promise.resolve({ publicKey: KEY }), searchParams: Promise.resolve({ lang }) });

describe("P3: the kit speaks the CRM's language", () => {
  it("every CRM page sits inside the kit I18nProvider (StudioLang) fed by the person's language", () => {
    const layout = read("app/(crm)/layout.tsx");
    expect(layout).toMatch(/<AppLang lang=\{ctx\.lang\} locale=\{ctx\.locale\}>\s*<CrmShell/);
    expect(layout).toMatch(/<AppLang lang=\{ctx\.lang\} locale=\{ctx\.locale\}>\s*<PageFrame/);
  });
});

describe("P3: plurals and number formats", () => {
  it("counts 1 in the singular on partners, suppliers, marketing", () => {
    expect(plural("en", "partners.trips", 1)).toBe("1 trip");
    expect(plural("en", "partners.trips", 3)).toBe("3 trips");
    expect(plural("en", "tag.ready.count", 1)).toBe("1 client");
    expect(plural("en", "tag.ready.count", 2)).toBe("2 clients");
    expect(plural("en", "suppliers.lines", 1)).toBe("line");
    expect(plural("en", "suppliers.lines", 4)).toBe("lines");
    expect(plural("vi", "partners.trips", 1)).toBe("1 chuyến");
  });

  it("writes the traveller split with singulars and without zero counts", () => {
    expect(paxSplitText("en", { adults: 1, children: 0, infants: 0 })).toBe("1 (1 adult)");
    expect(paxSplitText("en", { adults: 2, children: 1, infants: 0 })).toBe("3 (2 adults, 1 child)");
    expect(paxSplitText("en", { adults: 2, children: 2, infants: 1 })).toBe("5 (2 adults, 2 children, 1 infant)");
    expect(paxSplitText("vi", { adults: 2, children: 1, infants: 0 })).toBe("3 (2 người lớn, 1 trẻ em)");
  });

  it("formats the supplier commission in the person's locale", () => {
    const view = read("components/ledger/SuppliersView.tsx");
    expect(view).toContain("percentText(row.original.default_commission_pct, locale)");
    expect(read("app/(crm)/suppliers/page.tsx")).toContain("locale={ctx.locale}");
  });
});

describe("P3: one Vietnamese spelling (huỷ, xoá)", () => {
  // The catalogue writes the new tone placement; the old one ("hủy", "xóa") made one page say both.
  const OLD = /hủy|Hủy|HỦY|xóa|Xóa|XÓA/;
  function files(dir: string): string[] {
    return readdirSync(dir).flatMap((name) => {
      const full = path.join(dir, name);
      if (statSync(full).isDirectory()) return name === "core" || name === "node_modules" ? [] : files(full);
      return /\.(ts|tsx)$/.test(name) ? [full] : [];
    });
  }
  it("no message file or component keeps the old spelling", () => {
    const hits = ["lib", "components", "app"].flatMap((d) => files(path.join(WEB, d))).filter((f) => OLD.test(readFileSync(f, "utf8")));
    expect(hits.map((f) => path.relative(WEB, f))).toEqual([]);
  });
  it("the VI dictionary says huỷ and xoá", () => {
    expect(MESSAGES.vi["ledger.status.cancelled"]).toBe("Đã huỷ");
    expect(MESSAGES.vi["ledger.delete"]).toBe("Xoá");
  });
});

describe("P3: studio activity labels in the page language", () => {
  it("maps an action id to its key", () => {
    expect(activityLabelKey("content_piece.published_anyway")).toBe("contentPiecePublishedAnyway");
    expect(activityLabelKey("tenant.login")).toBe("tenantLogin");
  });
  it("words a known action in VI and keeps CIS's label for an unknown one", () => {
    expect(activityLabel("tenant.login", "Signed in", "vi")).toBe("Đã đăng nhập");
    expect(activityLabel("tenant_tour_version.exported", "Downloaded a tour", "en")).toBe("Downloaded a tour");
    expect(activityLabel("some.new_action", "Did something new", "vi")).toBe("Did something new");
  });
  it("has a Vietnamese label for every English one", () => {
    expect(Object.keys(ACTIVITY_LABEL_MESSAGES.vi).sort()).toEqual(Object.keys(ACTIVITY_LABEL_MESSAGES.en).sort());
  });
  it("the Activity column renders the translated label", () => {
    expect(read("app/(studio)/portal/_components/AccountActivity.tsx")).toContain("{activityLabel(r.action, r.label, lang)}");
  });
});

describe("P5: inquiry form on the client host", () => {
  it("shows the agency's name as the brand mark and the tab title, never the partner app's", async () => {
    answer(200, FORM);
    const html = renderToString(await InquiryPage(props()));
    expect(html).toContain('data-testid="centered-card-brand"');
    expect(html).toMatch(/data-testid="centered-card-brand"[^>]*>Lữ hành Hoa Sen</);
    expect(html).not.toContain("Adventure Asia Partner");
    expect(await formMetadata(props())).toEqual({ title: "Plan your trip with Lữ hành Hoa Sen" });
    expect(await formMetadata(props("vi"))).toEqual({ title: "Lên kế hoạch chuyến đi cùng Lữ hành Hoa Sen" });
  });

  it("answers a real 404 for an unknown key (notFound), and its card has no brand mark", async () => {
    answer(404, { detail: "not found" });
    await expect(InquiryPage(props())).rejects.toThrow();
    expect(await formMetadata(props())).toEqual({});
    const html = renderToString(await FormNotFound());
    expect(html).toContain("This form is not available.");
    expect(html).not.toContain("centered-card-brand");
  });

  it("an API fault keeps a 200 card without the partner brand", async () => {
    answer(500, {});
    const html = renderToString(await InquiryPage(props()));
    expect(html).toContain("intake-unavailable");
    expect(html).not.toContain("Adventure Asia Partner");
  });

  it("tells the visitor what the details are for when the agency set no privacy link", async () => {
    answer(200, FORM);
    const html = renderToString(await InquiryPage(props()));
    expect(html).toContain('data-testid="intake-privacy-fallback"');
    expect(html).toContain("Lữ hành Hoa Sen uses these details only to answer this inquiry");
    clearFormMemo();
    answer(200, { ...FORM, privacy_notice_url: "https://hoasen.example.test/privacy" });
    const linked = renderToString(await InquiryPage(props()));
    expect(linked).not.toContain("intake-privacy-fallback");
    expect(linked).toContain("https://hoasen.example.test/privacy");
  });

  it("has VI text for every new client-host string", () => {
    for (const k of ["intake.privacyFallback", "intake.language", "client.notFound", "client.host.title"] as const) {
      expect(MESSAGES.vi[k]).toBeTruthy();
      expect(MESSAGES.vi[k]).not.toBe(MESSAGES.en[k]);
    }
  });
});

describe("P5: client host 404 and tab title", () => {
  it("the client host 404 has no brand mark and no Home link (the host has no home page)", async () => {
    const html = renderToString(await NotFound());
    expect(html).not.toContain("centered-card-brand");
    expect(html).not.toContain('href="/"');
    expect(html).toContain("Check the link you were sent.");
  });

  it("the partner host 404 keeps its brand and Home link", async () => {
    req.hostKind = "partner";
    const html = renderToString(await NotFound());
    expect(html).toContain("Adventure Asia Partner");
    expect(html).toContain('href="/"');
  });

  it("the root tab title is neutral on the client host", async () => {
    expect((await rootMetadata()).title).toBe("Your trip");
    req.hostKind = "partner";
    expect((await rootMetadata()).title).toBe("Adventure Asia Partner");
  });
});

describe("P5: itinerary day headings on /trip", () => {
  const TEXT = "Day 1 — Arrival in Testville\nThe group meets at the hotel.\nDinner together.\n\nOvernight: Testville\n\nDay 2: Lake walk\nA morning walk.";
  it("splits a 'Day N' line (one newline before the text) into a heading", () => {
    const blocks = itineraryParagraphs(TEXT).map(dayBlock);
    expect(blocks).toEqual([
      { heading: "Day 1 — Arrival in Testville", body: "The group meets at the hotel.\nDinner together." },
      { heading: null, body: "Overnight: Testville" },
      { heading: "Day 2: Lake walk", body: "A morning walk." },
    ]);
  });
  it("leaves a paragraph that only mentions a day alone", () => {
    expect(dayBlock("Free time on day 3.")).toEqual({ heading: null, body: "Free time on day 3." });
    expect(dayBlock("Day 4 — Departure")).toEqual({ heading: "Day 4 — Departure", body: "" });
  });
  it("the page renders the heading as h3 and keeps line breaks in the text", () => {
    const page = read("app/trip/[tourId]/page.tsx");
    expect(page).toContain("dayBlock(paragraph)");
    expect(page).toContain('whiteSpace: "pre-line"');
    expect(page).toMatch(/<h3 style=/);
  });
});

describe("A1: Academy small fixes", () => {
  it("names a source row by its section heading, else its ref", () => {
    expect(sourceTitle({ ref: "doc:TEST-01#2-intro", body_md: "## Where state lives\n\nText." })).toBe("Where state lives");
    expect(sourceTitle({ ref: "doc:TEST-01#3", body_md: "No heading here." })).toBe("doc:TEST-01#3");
    expect(sourceTitle({ ref: "doc:TEST-01#4", body_md: null })).toBe("doc:TEST-01#4");
  });

  it("says 'module' for a path with one required module", () => {
    const path1: PathSummary = {
      id: "TP", title: "Test path", tier: "partner", enrolled: true, completed: false, final_quiz: true, certificate_status: null,
      modules: [{ id: "T1", title: "One", required: true, complete: true, status: "active" }],
    };
    const progress = pathProgress(path1);
    expect(progress).toEqual({ done: 1, total: 1 });
    expect(say(academyEn, "academy.path.progress.one", progress)).toBe("1 of 1 required module complete");
    expect(academyVi["academy.quiz.holdsCertificate"]).toBeTruthy();
    expect(read("app/academy/(learn)/page.tsx")).toContain('progress.total === 1 ? "academy.path.progress.one"');
  });

  it("a planned prerequisite is plain text; a missing module, lesson or quiz target answers 404", () => {
    const mod = read("app/academy/(learn)/modules/[moduleId]/page.tsx");
    expect(mod).toMatch(/p\.state === "planned" \?\s*\(\s*<span/);
    expect(mod).toContain('if (!read.ok && read.kind === "not_found") notFound();');
    expect(read("app/academy/(learn)/lessons/[lessonId]/page.tsx")).toContain('if (!read.ok && read.kind === "not_found") notFound();');
    const quiz = read("app/academy/(learn)/quiz/[scope]/[targetId]/page.tsx");
    expect(quiz).toContain("academy-quiz-context");
    expect(quiz).toContain('academyRead<Module>(ctx, `modules/${moduleId}`)');
    expect(quiz).toContain("if (!path || !path.final_quiz) notFound();");
  });
});
