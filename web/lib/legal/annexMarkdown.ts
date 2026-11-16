// web/lib/legal/annexMarkdown.ts
// AA-970 (design v3 card CRM-37b) — the public data page and the sub-processor page are rendered from the root DPA
// Annex 3 (AA-Ecosys docs/legal/dpa-annex-3-subprocessors.md, section 5), copied by api/scripts/gen_legal_annex.py
// into ./annex3.generated.json (the CI job "Vendored CIS copies" fails when the copy drifts from the root file). This
// module reads that block's small Markdown subset into the studio legal pages' document shape
// (app/(studio)/_legal/legalDoc.ts): `###` headings start a section, blank lines split paragraphs, `- ` lines are a
// list (a two-space indented line continues the item), a `|` table becomes one card per row (the first column is its
// title, the other columns its labelled fields: no raw table on a phone), `**bold**` and `[label](href)` inline. A
// link keeps only https:, mailto: and site paths. Anything else is plain text. No React here.

import type { Block, Inline, LegalSectionDoc, RowCard } from "@/app/(studio)/_legal/legalDoc";
import annex from "./annex3.generated.json";

export interface AnnexSummary {
  /** "draft v0.6" */
  version: string;
  /** YYYY-MM-DD, the annex's last review. */
  updated: string;
  sections: LegalSectionDoc[];
}

const SAFE_HREF = /^(https:\/\/[^\s"'<>]+|mailto:[^\s"'<>]+|\/[^\s"'<>]*)$/;
const INLINE = /\*\*(.+?)\*\*|\[([^\]]+)\]\(([^)\s]+)\)/g;

/** The runs of one line of text: plain, **bold**, [label](href) (an unsafe href stays plain text). */
export function inline(text: string): Inline[] {
  const out: Inline[] = [];
  let last = 0;
  for (const m of text.matchAll(INLINE)) {
    const at = m.index ?? 0;
    if (at > last) out.push(text.slice(last, at));
    if (m[1] !== undefined) out.push({ b: m[1] });
    else if (SAFE_HREF.test(m[3])) out.push({ a: m[2], href: m[3] });
    else out.push(m[2]);
    last = at + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out.length ? out : [""];
}

function cells(line: string): string[] {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
}

function isRule(line: string): boolean {
  return /^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/.test(line.trim());
}

/** "How we handle your data" -> "how-we-handle-your-data" (the section anchor). */
export function anchor(title: string): string {
  return title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "section";
}

/** Parse the annex block into sections (pure; tests feed it invented text). */
export function parseAnnexMarkdown(markdown: string): LegalSectionDoc[] {
  const sections: LegalSectionDoc[] = [];
  let current: LegalSectionDoc | null = null;
  let para: string[] = [];
  let list: string[] | null = null;
  let table: string[][] | null = null;

  const blocks = (): Block[] => {
    if (!current) {
      current = { id: "summary", title: "", blocks: [] };
      sections.push(current);
    }
    return current.blocks;
  };
  const flush = () => {
    if (para.length) blocks().push({ p: inline(para.join(" ")) });
    if (list) blocks().push({ ul: list.map((item) => inline(item)) });
    if (table && table.length > 1) {
      const [head, ...rows] = table;
      const cards: RowCard[] = rows.map((r) => ({
        title: inline(r[0] ?? ""),
        fields: head.slice(1).map((label, i) => ({ label, value: inline(r[i + 1] ?? "") })),
      }));
      blocks().push({ rows: cards });
    }
    para = [];
    list = null;
    table = null;
  };

  for (const raw of markdown.split("\n")) {
    const line = raw.replace(/\s+$/, "");
    if (line.startsWith("### ")) {
      flush();
      const title = line.slice(4).trim();
      current = { id: anchor(title), title, blocks: [] };
      sections.push(current);
    } else if (!line.trim()) {
      flush();
    } else if (line.trimStart().startsWith("|")) {
      if (para.length || list) flush();
      if (!isRule(line)) (table ??= []).push(cells(line));
    } else if (line.startsWith("- ")) {
      if (para.length || table) flush();
      (list ??= []).push(line.slice(2).trim());
    } else if (list && /^\s{2,}\S/.test(line)) {
      list[list.length - 1] += ` ${line.trim()}`;
    } else {
      if (list || table) flush();
      para.push(line.trim());
    }
  }
  flush();
  return sections;
}

/** The committed copy of the annex summary, parsed. */
export function annexSummary(): AnnexSummary {
  return { version: annex.version, updated: annex.updated, sections: parseAnnexMarkdown(annex.markdown) };
}

/** The section of the data page (the first heading) and of the sub-processor page (the second). */
export function annexPages(summary: AnnexSummary = annexSummary()): { data: LegalSectionDoc | null; subprocessors: LegalSectionDoc | null } {
  const titled = summary.sections.filter((s) => s.title);
  return { data: titled[0] ?? null, subprocessors: titled[1] ?? null };
}
