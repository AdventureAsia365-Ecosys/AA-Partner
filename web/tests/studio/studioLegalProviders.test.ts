// AA-1062 guard: the Content studio's draft legal pages (partner terms §3, privacy notice §5) name as writers only the
// providers the AI Use Register lists for the writer stages, in English and in Vietnamese.
//
// Source of truth: root docs/architecture/ai-use-register.md (v1.13), section "Providers, for the tenant terms", and
// root docs/generated/llm-routes.md. Since CIS migration 383 (route control version 7, 2026-11-06) every writer route
// is Claude only (the Anthropic API first, Amazon Bedrock second); OpenAI models only review, score and check, and
// Amazon Nova / Meta Llama only check. A writer whose Claude models all fail waits and is retried (queue_retry); no
// model of another maker writes "as a last resort". When the register adds a writer provider, add it to
// WRITER_PROVIDERS here in the same change as the page copy (and the page version in _legal/legal.messages.ts).
import { describe, expect, it } from "vitest";
import { docText, type LegalDoc } from "@/app/(studio)/_legal/legalDoc";
import { LEGAL_MESSAGES } from "@/app/(studio)/_legal/legal.messages";
import { TERMS_DOC } from "@/app/(studio)/legal/partner-terms/terms.messages";
import { PRIVACY_DOC } from "@/app/(studio)/legal/privacy/privacy.messages";

/** Provider names the register lists for the writer stages (Claude on the Anthropic API or Amazon Bedrock). */
const WRITER_PROVIDERS = ["Claude", "Anthropic", "Anthropic API", "Amazon Bedrock", "Amazon Web Services"] as const;
/** Provider names the pages may use, but never in a sentence about writing (they review, score or check). */
const NON_WRITER_PROVIDERS = ["OpenAI", "OpenAI API", "GPT", "Amazon Nova", "Nova", "Meta Llama", "Llama", "Jev", "TypeSafe", "Cohere"] as const;

/** Phrases of the writing fallback removed by CIS 383 (EN and the VI translation of Draft v0.5). */
const STALE_WRITER_PHRASES = [
  "last resort",
  "may write it",
  "may write the content",
  "khi không còn lựa chọn nào khác",
  "phương án cuối",
  "một mô hình OpenAI qua OpenAI API có thể viết",
];

/** A sentence about writing the content: an English or a Vietnamese write verb. */
const WRITE_VERB = /\b(write|writes|written|writing|wrote)\b|(^|[\s:,(])viết([\s:,.)]|$)/i;

const DOCS: ReadonlyArray<[string, Readonly<Record<"en" | "vi", LegalDoc>>, string]> = [
  ["partner terms", TERMS_DOC, "ai"],
  ["privacy notice", PRIVACY_DOC, "ai"],
];

function sentences(text: string): string[] {
  return text.split(/(?<=[.;:!?])\s+/).filter((s) => s.trim() !== "");
}

function namesIn(sentence: string, names: readonly string[]): string[] {
  return names.filter((name) => new RegExp(`\\b${name.replace(/ /g, "\\s+")}\\b`).test(sentence));
}

describe("studio legal pages: writer providers follow the AI Use Register", () => {
  for (const [name, doc] of DOCS) {
    for (const lang of ["en", "vi"] as const) {
      it(`${name} (${lang}): no "last resort" writer and no other writer sentence`, () => {
        const whole = docText(doc[lang]);
        for (const phrase of STALE_WRITER_PHRASES) {
          expect(whole.toLowerCase(), `${name} ${lang}: stale writer wording "${phrase}"`).not.toContain(phrase.toLowerCase());
        }
      });

      it(`${name} (${lang}): every sentence about writing names only register writer providers`, () => {
        const writing = sentences(docText(doc[lang])).filter((s) => WRITE_VERB.test(s));
        expect(writing.length, `${name} ${lang}: no sentence about who writes the content`).toBeGreaterThan(0);
        for (const sentence of writing) {
          expect(namesIn(sentence, NON_WRITER_PROVIDERS), `${name} ${lang}: a non-writer provider in "${sentence}"`).toEqual([]);
        }
        // Who writes is said, and it is a register writer provider.
        expect(writing.some((s) => namesIn(s, WRITER_PROVIDERS).includes("Claude")), `${name} ${lang}: Claude as the writer`).toBe(true);
      });
    }

    it(`${name}: says only Claude models write, in English and Vietnamese`, () => {
      const ai = (lang: "en" | "vi"): string => docText(doc[lang], "ai");
      expect(ai("en")).toMatch(/Only Claude models write (it|the content): if no Claude model is available, the writing step waits and is tried again later\./);
      expect(ai("vi")).toContain("Chỉ các mô hình Claude viết nội dung: nếu không có mô hình Claude nào khả dụng, bước viết sẽ chờ và được thử lại sau.");
      // OpenAI models are still named, as reviewers and checkers (register: judge / validate stages).
      expect(ai("en")).toMatch(/OpenAI models review, score and check/);
    });
  }

  it("the page frame shows Draft v0.6 in English and Vietnamese", () => {
    expect(LEGAL_MESSAGES.en.version).toBe("Draft v0.6");
    expect(LEGAL_MESSAGES.vi.version).toBe("Bản nháp v0.6");
  });

  it("the guard itself catches the Draft v0.5 sentence", () => {
    const old = "If no Claude model is available, an OpenAI model through the OpenAI API may write it as a last resort.";
    const writing = sentences(old).filter((s) => WRITE_VERB.test(s));
    expect(writing).toHaveLength(1);
    expect(namesIn(writing[0], NON_WRITER_PROVIDERS)).toContain("OpenAI");
    const oldVi = "Nếu không có mô hình Claude nào khả dụng, một mô hình OpenAI qua OpenAI API có thể viết nội dung khi không còn lựa chọn nào khác.";
    const writingVi = sentences(oldVi).filter((s) => WRITE_VERB.test(s));
    expect(writingVi).toHaveLength(1);
    expect(namesIn(writingVi[0], NON_WRITER_PROVIDERS)).toContain("OpenAI");
  });
});
