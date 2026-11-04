// web/app/(studio)/_legal/legalDoc.ts
// AA-1036 (R3) — the studio's draft legal pages as structured text, one document per UI language (English is the
// version that applies; the Vietnamese one is a translation for reading). A document is sections of paragraphs and
// lists; a run of text is plain, bold, or a link. LegalPage.tsx renders it, so the pages hold no copy of their own.
// No React here.

/** A run of text: plain, bold (`b`), or a link (`a` with `href`; mailto, a site path or an external URL). */
export type Inline = string | { b: string } | { a: string; href: string };

/** A list item: its text, and an optional nested list. */
export type ListItem = Inline[] | { text: Inline[]; ul: ListItem[] };

export type Block = { p: Inline[] } | { ul: ListItem[] };

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
  return doc.sections.map((s) => `${s.id}:${s.blocks.map((b) => ("p" in b ? "p" : `ul(${b.ul.map(item).join(",")})`)).join("+")}`);
}

/** The plain text of a document (every run, links by their label), e.g. for checks on what a section names. */
export function docText(doc: LegalDoc, sectionId?: string): string {
  const run = (r: Inline): string => (typeof r === "string" ? r : "b" in r ? r.b : r.a);
  const item = (i: ListItem): string => (Array.isArray(i) ? i.map(run).join("") : `${i.text.map(run).join("")} ${i.ul.map(item).join(" ")}`);
  const sections = sectionId ? doc.sections.filter((s) => s.id === sectionId) : doc.sections;
  return sections
    .map((s) => [s.title, ...s.blocks.map((b) => ("p" in b ? b.p.map(run).join("") : b.ul.map(item).join(" ")))].join(" "))
    .join(" ")
    .replace(/\s+/g, " ")
    .trim();
}
