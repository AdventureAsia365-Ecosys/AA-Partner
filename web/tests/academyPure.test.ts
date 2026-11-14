// AA-1105: the pure parts of the AA Academy web: the public verify page's per-visitor limit (from the CIS setting the
// API reports, never a literal), the verify call, the lesson Markdown reader (no HTML, only https links open), the FSRS
// review submission the CIS checks accept, the quiz helpers, the EN/VI strings and the page components' rendering.
// Fake names and values only.
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { errorKind, lessonNeighbours, pathProgress, quizResponses, retakeWaiting, toggleChoice, verifyUrl, type PathSummary, type ReviewCard } from "@/lib/academy";
import { parseBlocks, parseInline, safeHref } from "@/lib/academyMarkdown";
import { reviewSubmission, retention } from "@/lib/academyReview";
import { isCertificateCode, verifyCertificate, VisitorLimiter } from "@/lib/academyVerify";
import { academyEn, academyVi, say } from "@/lib/i18nAcademy";
import { MESSAGES } from "@/lib/i18n";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {} }), usePathname: () => "/academy" }));
const { LessonBody } = await import("@/components/academy/LessonBody");
const { QuizRunner } = await import("@/components/academy/QuizRunner");
const { ReviewRunner } = await import("@/components/academy/ReviewRunner");
const { AcademyShell, activeSection } = await import("@/app/_kit/local/AcademyShell");

const CODE = "Abcdefghij_klmnopqr-12";
const CIS = "https://cis-api.example.test";

describe("verify page limit", () => {
  it("before CIS reports its limit: one call in flight per visitor, others are free", () => {
    const l = new VisitorLimiter();
    const a = l.admit("v1", 0);
    expect(a.ok).toBe(true);
    expect(l.admit("v1", 0)).toEqual({ ok: false, retryAfterS: 1 });
    expect(l.admit("v2", 0).ok).toBe(true);
    if (a.ok) a.release();
    expect(l.admit("v1", 0).ok).toBe(true);
  });

  it("after CIS reports its limit: that many calls per visitor per minute, then the next minute", () => {
    const l = new VisitorLimiter();
    l.learn("3");
    expect(l.limit).toBe(3);
    for (let i = 0; i < 3; i++) {
      const r = l.admit("v1", 1_000);
      expect(r.ok).toBe(true);
      if (r.ok) r.release();
    }
    const refused = l.admit("v1", 1_000);
    expect(refused).toEqual({ ok: false, retryAfterS: 59 });
    expect(l.admit("v2", 1_000).ok).toBe(true);
    expect(l.admit("v1", 61_000).ok).toBe(true);
  });

  it("ignores a header that is not a positive integer", () => {
    const l = new VisitorLimiter();
    for (const bad of [null, "", "0", "-1", "abc", "1.5", "9999999"]) l.learn(bad);
    expect(l.limit).toBeNull();
  });

  it("calls CIS without a token, learns the limit, passes 404 / 429 / errors on, never calls for a bad code", async () => {
    const l = new VisitorLimiter();
    const ok = vi.fn<typeof fetch>(async () =>
      Response.json(
        { path_id: "fundamentals", path_title: "Fundamentals", holder_display_name: "Test Learner", issued_at: "2026-11-01T00:00:00Z", expires_at: null, status: "valid", module_versions: [] },
        { headers: { "X-RateLimit-Limit": "30" } },
      ),
    );
    const res = await verifyCertificate(CIS, CODE, "198.51.100.7:443", { limiter: l, fetchImpl: ok });
    expect(res.kind).toBe("ok");
    expect(ok.mock.calls[0][0]).toBe(`${CIS}/learn/public/v1/certificates/${CODE}`);
    expect(new Headers(ok.mock.calls[0][1]?.headers).get("authorization")).toBeNull();
    expect(l.limit).toBe(30);

    const nf = vi.fn(async () => new Response("{}", { status: 404 }));
    expect((await verifyCertificate(CIS, CODE, null, { limiter: l, fetchImpl: nf })).kind).toBe("not_found");
    const busy = vi.fn(async () => new Response("{}", { status: 429, headers: { "Retry-After": "17" } }));
    expect(await verifyCertificate(CIS, CODE, null, { limiter: l, fetchImpl: busy })).toEqual({ kind: "rate_limited", retryAfterS: 17 });
    const down = vi.fn(async () => {
      throw new Error("down");
    });
    expect((await verifyCertificate(CIS, CODE, null, { limiter: l, fetchImpl: down })).kind).toBe("unavailable");

    const never = vi.fn();
    expect((await verifyCertificate(CIS, "short", null, { limiter: l, fetchImpl: never })).kind).toBe("not_found");
    expect((await verifyCertificate(CIS, `${CODE}/../x`, null, { limiter: l, fetchImpl: never })).kind).toBe("not_found");
    expect(never).not.toHaveBeenCalled();
    expect(isCertificateCode(CODE)).toBe(true);
  });

  it("refuses a visitor over the learned limit without calling CIS", async () => {
    const l = new VisitorLimiter();
    l.learn("1");
    const fetchImpl = vi.fn(async () => new Response("{}", { status: 404 }));
    await verifyCertificate(CIS, CODE, "203.0.113.9:1234", { limiter: l, fetchImpl, nowMs: 5_000 });
    const second = await verifyCertificate(CIS, CODE, "203.0.113.9:1234", { limiter: l, fetchImpl, nowMs: 6_000 });
    expect(second.kind).toBe("rate_limited");
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    // Another visitor behind the same web is not affected.
    expect((await verifyCertificate(CIS, CODE, "203.0.113.10:1234", { limiter: l, fetchImpl, nowMs: 6_000 })).kind).toBe("not_found");
  });
});

describe("lesson Markdown", () => {
  it("reads headings, lists, quotes, code fences, tables and paragraphs", () => {
    const blocks = parseBlocks(
      ["# Title", "", "Intro line one", "line two", "", "- a", "- b", "", "1. one", "2. two", "", "> quoted", "", "```text", "<b>raw</b>", "```", "", "| a | b |", "| - | - |", "", "---"].join("\n"),
    );
    expect(blocks.map((b) => b.kind)).toEqual(["heading", "para", "ul", "ol", "quote", "code", "table", "rule"]);
    expect(blocks[5]).toEqual({ kind: "code", text: "<b>raw</b>" });
  });

  it("opens only absolute https links", () => {
    expect(safeHref("https://example.org/a")).toBe("https://example.org/a");
    expect(safeHref("../adr/0001.md")).toBeNull();
    expect(safeHref("javascript:alert(1)")).toBeNull();
    expect(safeHref("http://example.org")).toBeNull();
    const tokens = parseInline("See [the guide](https://example.org/g), [a file](../x.md), `code`, **bold** and *it*.");
    expect(tokens.filter((t) => t.kind === "link")).toEqual([
      { kind: "link", text: "the guide", href: "https://example.org/g" },
      { kind: "link", text: "a file", href: null },
    ]);
    expect(tokens.some((t) => t.kind === "strong" && t.text === "bold")).toBe(true);
    expect(tokens.some((t) => t.kind === "em" && t.text === "it")).toBe(true);
  });

  it("renders text, never markup, and drops the title heading when asked", () => {
    const html = renderToStaticMarkup(createElement(LessonBody, { text: "# Lesson title\n\nHello <script>x</script> [link](javascript:alert(1))", skipFirstHeading: true }));
    expect(html).not.toContain("<script>");
    expect(html).toContain("&lt;script&gt;");
    expect(html).not.toContain("Lesson title");
    expect(html).not.toContain("javascript:");
  });
});

describe("spaced review", () => {
  const stored = {
    due: "2026-11-15T08:00:00.000Z",
    stability: 0,
    difficulty: 0,
    elapsed_days: 0,
    scheduled_days: 0,
    learning_steps: 0,
    reps: 0,
    lapses: 0,
    state: 0,
    last_review: null,
  };

  it("builds a card the CIS checks accept: last review = reviewed_at, due never before the stored due", () => {
    const now = new Date("2026-11-15T09:00:00.000Z");
    for (const rating of ["again", "hard", "good", "easy"] as const) {
      const body = reviewSubmission(stored, rating, [1], now, 0.9);
      expect(body.reviewed_at).toBe(now.toISOString());
      expect(body.card.last_review).toBe(body.reviewed_at);
      expect(Date.parse(body.card.due)).toBeGreaterThanOrEqual(Date.parse(stored.due));
      expect(body.card.reps).toBe(1);
      expect(body.chosen).toEqual([1]);
    }
    const good = reviewSubmission(stored, "good", [], now);
    const easy = reviewSubmission(stored, "easy", [], now);
    expect(Date.parse(easy.card.due)).toBeGreaterThan(Date.parse(good.card.due));
  });

  it("keeps the stored due when the next one would be earlier, and uses only a valid retention", () => {
    const future = { ...stored, due: "2027-01-01T00:00:00.000Z", state: 2, stability: 1, difficulty: 5, reps: 3, last_review: "2026-11-01T00:00:00.000Z" };
    const body = reviewSubmission(future, "again", [0], new Date("2026-11-15T09:00:00.000Z"));
    expect(body.card.due).toBe(future.due);
    expect(retention(0.9)).toBe(0.9);
    expect(retention(1)).toBeUndefined();
    expect(retention(undefined)).toBeUndefined();
  });
});

describe("quiz and catalogue helpers", () => {
  it("toggles single and multiple choices and submits every item", () => {
    expect(toggleChoice([2], 1, "single")).toEqual([1]);
    expect(toggleChoice([2], 0, "multiple")).toEqual([0, 2]);
    expect(toggleChoice([0, 2], 2, "multiple")).toEqual([0]);
    const items = [
      { id: "F3-Q1", kind: "single" as const, prompt: "p", choices: ["a", "b"] },
      { id: "F3-Q2", kind: "multiple" as const, prompt: "p", choices: ["a", "b"] },
    ];
    expect(quizResponses(items, { "F3-Q1": [1] })).toEqual([
      { item_id: "F3-Q1", chosen: [1] },
      { item_id: "F3-Q2", chosen: [] },
    ]);
  });

  it("knows retake waits, refusals, progress and the verify URL shape", () => {
    expect(retakeWaiting("2026-11-16T00:00:00Z", Date.parse("2026-11-15T00:00:00Z"))).toBe(true);
    expect(retakeWaiting("2026-11-14T00:00:00Z", Date.parse("2026-11-15T00:00:00Z"))).toBe(false);
    expect(retakeWaiting(null)).toBe(false);
    expect(errorKind(403, "not_a_learner")).toBe("not_a_learner");
    expect(errorKind(403, "forbidden")).toBe("forbidden");
    expect(errorKind(503, "unavailable")).toBe("unavailable");
    expect(errorKind(404, "not_found")).toBe("not_found");
    const path: PathSummary = {
      id: "fundamentals",
      title: "Fundamentals",
      tier: "staff",
      enrolled: true,
      completed: false,
      final_quiz: true,
      certificate_status: null,
      modules: [
        { id: "F1", title: "One", required: true, status: "active", complete: true },
        { id: "F2", title: "Two", required: true, status: "active", complete: false },
        { id: "F4", title: null, required: true, status: "planned", complete: false },
        { id: "E1", title: "Extra", required: false, status: "active", complete: false },
      ],
    };
    expect(pathProgress(path)).toEqual({ done: 1, total: 2 });
    expect(verifyUrl("https://academy.example.test", `/verify/${CODE}`)).toBe(`https://academy.example.test/verify/${CODE}`);
    expect(verifyUrl("https://academy.example.test", "https://evil.example.test")).toBeNull();
    expect(lessonNeighbours(["F3-01", "F3-02", "F3-03"], "F3-02")).toEqual({ prev: "F3-01", next: "F3-03" });
    expect(lessonNeighbours(["F3-01"], "F3-01")).toEqual({ prev: null, next: null });
    expect(lessonNeighbours([], "F3-01")).toEqual({ prev: null, next: null });
  });
});

describe("EN / VI", () => {
  it("every academy string exists in both languages, is wired into the web dictionary and names no issue id", () => {
    expect(Object.keys(academyVi).sort()).toEqual(Object.keys(academyEn).sort());
    for (const [k, v] of Object.entries(academyEn)) {
      expect(MESSAGES.en[k as keyof typeof MESSAGES.en]).toBe(v);
      expect(v).not.toMatch(/AA-\d+/);
      expect(academyVi[k as keyof typeof academyVi]).not.toMatch(/AA-\d+/);
    }
    expect(say(academyEn, "academy.minutes", { n: 7 })).toBe("7 min");
  });
});

describe("page components", () => {
  it("the quiz starts only when the learner asks (no attempt on render)", () => {
    const html = renderToStaticMarkup(
      createElement(QuizRunner, { scope: "module", targetId: "F3", backHref: "/academy/modules/F3", backLabel: "Back", origin: "https://academy.example.test", locale: "en-AU", dict: academyEn }),
    );
    expect(html).toContain('data-testid="academy-quiz-start"');
    expect(html).toContain("Start the quiz");
  });

  it("a review card shows the question and the four ratings, never the answer", () => {
    const cards: ReviewCard[] = [
      { module_id: "F3", item: { id: "F3-Q1", kind: "single", prompt: "Which list wins?", choices: ["The first", "The second"] }, card: { due: "2026-11-15T08:00:00Z", stability: 0, difficulty: 0, elapsed_days: 0, scheduled_days: 0, reps: 0, lapses: 0, state: 0, last_review: null } },
    ];
    const html = renderToStaticMarkup(createElement(ReviewRunner, { cards, targetRetention: 0.9, dict: academyEn }));
    expect(html).toContain("Which list wins?");
    for (const r of ["Forgot", "Hard", "Good", "Easy"]) expect(html).toContain(r);
    expect(html).not.toContain("Correct answer");
  });

  it("the shell shows the academy, its three sections and the academy sign-out", () => {
    const html = renderToStaticMarkup(
      createElement(AcademyShell, { dict: MESSAGES.en, locale: "en-AU", environment: "dev", learnerName: "Test Learner", testLearner: true }, "x"),
    );
    expect(html).toContain("AA Academy");
    expect(html).toContain('action="/academy/auth/sign-out"');
    expect(html).toContain("Signed in as Test Learner");
    expect(html).toContain('data-testid="academy-test-learner"');
    expect(html).not.toContain("/staff");
    expect(activeSection("/academy/lessons/F3-01")).toBe("/academy");
    expect(activeSection("/academy/review")).toBe("/academy/review");
  });
});
