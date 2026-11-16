// web/app/(studio)/_legal/legalDoc.ts
// AA-1036 (R3) — the studio's draft legal pages as structured text, one document per UI language (English is the
// version that applies; the Vietnamese one is a translation for reading). A document is sections of paragraphs and
// lists; a run of text is plain, bold, or a link. LegalPage.tsx renders it, so the pages hold no copy of their own.
// AA-970: a block may also be a list of row cards (one card per row of a table: a title and labelled fields), for the
// public sub-processor page rendered from the root DPA Annex 3 (lib/legal/annexMarkdown.ts); no raw table on a phone.
// No React here.

/** A run of text: plain, bold (`b`), or a link (`a` with `href`; mailto, a site path or an external URL). */
export type Inline = string | { b: string } | { a: string; href: string };

/** A list item: its text, and an optional nested list. */
export type ListItem = Inline[] | { text: Inline[]; ul: ListItem[] };

/** One row of a table as a card: its first cell is the title, the others are labelled fields (AA-970). */
export interface RowCard {
  title: Inline[];
  fields: { label: string; value: Inline[] }[];
}

export type Block = { p: Inline[] } | { ul: ListItem[] } | { rows: RowCard[] };

export interface LegalSectionDoc {
  /** The anchor (the same in every language). */
  id: string;
  title: string;
  blocks: Block[];
}

export interface LegalDoc {
  /** The browser title. */
  metaTitle: string;
  title: string;
  intro: Inline[];
  sections: LegalSectionDoc[];
}

/** The shape of a document without its words: section ids and block kinds, for the EN/VI parity test. */
export function docShape(doc: LegalDoc): string[] {
  const item = (i: ListItem): string => (Array.isArray(i) ? "li" : `li[${i.ul.map(item).join(",")}]`);
  const block = (b: Block): string => ("p" in b ? "p" : "ul" in b ? `ul(${b.ul.map(item).join(",")})` : `rows(${b.rows.length})`);
  return doc.sections.map((s) => `${s.id}:${s.blocks.map(block).join("+")}`);
}

/** The plain text of a document (every run, links by their label), e.g. for checks on what a section names. */
export function docText(doc: LegalDoc, sectionId?: string): string {
  const run = (r: Inline): string => (typeof r === "string" ? r : "b" in r ? r.b : r.a);
  const item = (i: ListItem): string => (Array.isArray(i) ? i.map(run).join("") : `${i.text.map(run).join("")} ${i.ul.map(item).join(" ")}`);
  const sections = sectionId ? doc.sections.filter((s) => s.id === sectionId) : doc.sections;
  return sections
    .map((s) => [s.title, ...s.blocks.map((b) => blockText(b, run, item))].join(" "))
    .join(" ")
    .replace(/\s+/g, " ")
    .trim();
}

function blockText(b: Block, run: (r: Inline) => string, item: (i: ListItem) => string): string {
  if ("p" in b) return b.p.map(run).join("");
  if ("ul" in b) return b.ul.map(item).join(" ");
  return b.rows.map((r) => [r.title.map(run).join(""), ...r.fields.map((f) => `${f.label} ${f.value.map(run).join("")}`)].join(" ")).join(" ");
}
