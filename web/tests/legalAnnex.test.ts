// AA-970 (design v3 card CRM-37b): the public pages "How we handle your data" (/legal/data) and "Sub-processors"
// (/legal/subprocessors), rendered from the root DPA Annex 3 public summary (lib/legal/annex3.generated.json, written by
// api/scripts/gen_legal_annex.py). The parser reads the Markdown subset; the pages are public; nothing unsafe or
// internal reaches them. Invented text in the parser tests.
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import annex from "@/lib/legal/annex3.generated.json";
import { annexPages, annexSummary, inline, parseAnnexMarkdown } from "@/lib/legal/annexMarkdown";
import { annexDoc, annexVersion } from "@/app/(studio)/_legal/annexDoc";
import { docShape, docText } from "@/app/(studio)/_legal/legalDoc";
import { isPublicPartnerPath } from "@/lib/hosts";

const state: { lang: "en" | "vi" } = { lang: "en" };
vi.mock("@/lib/serverContext", () => ({ serverContext: async () => ({ lang: state.lang }) }));

const { default: DataPage } = await import("@/app/(studio)/legal/data/page");
const { default: SubprocessorsPage } = await import("@/app/(studio)/legal/subprocessors/page");

const SAMPLE = `### How we handle your data

Invented Harbour Tours keeps its data
on two lines of one paragraph.

- **Where it is kept.** In an invented region,
  continued on a second line.
- A link to [the list](/legal/subprocessors) and [mail](mailto:help@example.test).
- A bad link [click](javascript:void) stays text.

### Sub-processors

| Sub-processor | What it does | Where |
|---|---|---|
| Invented Cloud | Hosting | Nowhere Bay |
| **Bold Mail** | E-mail | To be confirmed |
`;

describe("the annex Markdown subset", () => {
  it("reads sections, paragraphs, lists with continuations, links and a table as row cards", () => {
    const [data, sub] = parseAnnexMarkdown(SAMPLE);
    expect(data.id).toBe("how-we-handle-your-data");
    expect(data.blocks[0]).toEqual({ p: ["Invented Harbour Tours keeps its data on two lines of one paragraph."] });
    const list = data.blocks[1] as { ul: unknown[][] };
    expect(list.ul).toHaveLength(3);
    expect(list.ul[0]).toEqual([{ b: "Where it is kept." }, " In an invented region, continued on a second line."]);
    expect(list.ul[1]).toEqual(["A link to ", { a: "the list", href: "/legal/subprocessors" }, " and ", { a: "mail", href: "mailto:help@example.test" }, "."]);
    expect(list.ul[2]).toEqual(["A bad link ", "click", " stays text."]);
    expect(sub.id).toBe("sub-processors");
    expect(sub.blocks).toEqual([
      {
        rows: [
          { title: ["Invented Cloud"], fields: [{ label: "What it does", value: ["Hosting"] }, { label: "Where", value: ["Nowhere Bay"] }] },
          { title: [{ b: "Bold Mail" }], fields: [{ label: "What it does", value: ["E-mail"] }, { label: "Where", value: ["To be confirmed"] }] },
        ],
      },
    ]);
  });

  it("keeps plain text plain and never builds an unsafe link", () => {
    expect(inline("no marks")).toEqual(["no marks"]);
    expect(inline("")).toEqual([""]);
    expect(inline("[x](http://plain.example)")).toEqual(["x"]);
    expect(inline("[x](https://ok.example/p)")).toEqual([{ a: "x", href: "https://ok.example/p" }]);
  });
});

describe("the committed annex summary", () => {
  it("parses into the data page and the sub-processor page", () => {
    const pages = annexPages(annexSummary());
    expect(pages.data?.title).toBe("How we handle your data");
    expect(pages.subprocessors?.title).toBe("Sub-processors");
    const rows = pages.subprocessors!.blocks.find((b) => "rows" in b) as { rows: { title: unknown[] }[] };
    expect(rows.rows.length).toBeGreaterThanOrEqual(5);
    expect(docText(annexDoc("subprocessors", "en"))).toContain("Amazon Web Services");
    expect(annex.source.path).toBe("docs/legal/dpa-annex-3-subprocessors.md");
  });

  it("carries no account number, ARN or internal id", () => {
    expect(annex.markdown).not.toMatch(/\b\d{12}\b|arn:aws|\bAA-\d+\b|\bacc[1-4]\b/i);
  });

  it("has the same shape in both languages, and the Vietnamese frame says the body is the English original", () => {
    for (const page of ["data", "subprocessors"] as const) {
      expect(docShape(annexDoc(page, "vi"))).toEqual(docShape(annexDoc(page, "en")));
    }
    const runs = (doc: ReturnType<typeof annexDoc>) => doc.intro.map((r) => (typeof r === "string" ? r : "b" in r ? r.b : r.a)).join("");
    expect(runs(annexDoc("data", "vi"))).toContain("tiếng Anh");
    expect(runs(annexDoc("data", "en"))).not.toContain("tiếng Anh");
    expect(annexVersion("en").version).toBe(annex.version.replace(/^draft /, "Draft "));
  });
});

describe("the public pages", () => {
  it("are reachable without a session", () => {
    expect(isPublicPartnerPath("/legal/data")).toBe(true);
    expect(isPublicPartnerPath("/legal/subprocessors")).toBe(true);
  });

  it.each(["en", "vi"] as const)("render the annex in the legal frame (%s)", async (lang) => {
    state.lang = lang;
    const data = renderToStaticMarkup(await DataPage());
    expect(data).toContain('data-testid="legal-draft-banner"');
    expect(data).toContain("Where it is kept.");
    expect(data).toContain('href="/legal/subprocessors"');
    expect(data).not.toContain("legal-row-card");
    const sub = renderToStaticMarkup(await SubprocessorsPage());
    expect((sub.match(/data-testid="legal-row-card"/g) ?? []).length).toBeGreaterThanOrEqual(5);
    expect(sub).toContain("Amazon Bedrock");
    expect(sub).toContain('href="/legal/data"');
    expect(sub).not.toContain("<table");
  });
});
