// web/lib/inclusions.ts — AA-1070 item crm-35: the "Included" / "Not included" lists of a proposal. Types of the API
// answers (the agency library, the builder panel, the client page), the pure list moves the builder picker makes
// (pick, unpick, move up / down, type a line), the body of the one PUT that saves them, and the refusal wording.
// The API keeps the lists: a published version's lines never change, an AA tour's lines are read-only.

export const INCLUSION_KINDS = ["included", "not_included"] as const;
export type InclusionKind = (typeof INCLUSION_KINDS)[number];
export const MAX_TEXT = 300;
/** Settings > Proposal lines (the agency library). */
export const INCLUSIONS_SETTINGS_PATH = "/settings/inclusions";

/** One line of the agency library (Settings > Proposal lines). */
export type InclusionItem = {
  inclusion_item_id: string;
  kind: InclusionKind;
  text_en: string;
  text_vi: string | null;
  sort_order: number;
  archived: boolean;
};

/** One line of a builder version: picked from the library (its copied text) or typed for this version. */
export type InclusionLine = {
  proposal_inclusion_id: string;
  kind: InclusionKind;
  source: "library" | "custom";
  inclusion_item_id: string | null;
  text_en: string;
  text_vi: string | null;
};

/** An AA tour's fixed lists (the CIS tour content): read-only everywhere. */
export type AaTourLists = { source_id: string; title: string; included: string[]; not_included: string[] };

/** The builder's panel (`inclusions` on GET /proposals/{id}/builder). */
export type InclusionsPanelData = {
  lines: InclusionLine[];
  aa_tours: AaTourLists[];
  library: InclusionItem[];
  max_lines: number;
};

/** The client page (`inclusions` of the client proposal answer). */
export type ClientLine = { text_en: string; text_vi: string | null };
export type ClientInclusions = { included: ClientLine[]; not_included: ClientLine[]; aa_tours: AaTourLists[] };

/** A line the picker holds before Save: a library pick or a typed line (no id yet). */
export type DraftLine = { kind: InclusionKind; text_en: string; text_vi: string | null; inclusion_item_id: string | null };

export function draftLines(lines: InclusionLine[]): DraftLine[] {
  return lines.map((l) => ({ kind: l.kind, text_en: l.text_en, text_vi: l.text_vi, inclusion_item_id: l.inclusion_item_id }));
}

export function ofKind(lines: DraftLine[], kind: InclusionKind): DraftLine[] {
  return lines.filter((l) => l.kind === kind);
}

/** Pick a library line (at the end of its kind); a line already picked stays where it is. */
export function pickItem(lines: DraftLine[], item: InclusionItem): DraftLine[] {
  if (lines.some((l) => l.inclusion_item_id === item.inclusion_item_id)) return lines;
  return [...lines, { kind: item.kind, text_en: item.text_en, text_vi: item.text_vi, inclusion_item_id: item.inclusion_item_id }];
}

/** Type a line for this version only: trimmed, one line, 1..300 characters; null when it is not a line. */
export function typedLine(kind: InclusionKind, en: string, vi: string): DraftLine | null {
  const textEn = en.split(/\s+/).filter(Boolean).join(" ");
  const textVi = vi.split(/\s+/).filter(Boolean).join(" ");
  if (!textEn || textEn.length > MAX_TEXT || textVi.length > MAX_TEXT) return null;
  return { kind, text_en: textEn, text_vi: textVi || null, inclusion_item_id: null };
}

/** Remove the n-th line of a kind. */
export function removeAt(lines: DraftLine[], kind: InclusionKind, index: number): DraftLine[] {
  const target = ofKind(lines, kind)[index];
  return target ? lines.filter((l) => l !== target) : lines;
}

/** Move the n-th line of a kind up (-1) or down (+1) within its kind. */
export function moveAt(lines: DraftLine[], kind: InclusionKind, index: number, by: -1 | 1): DraftLine[] {
  const same = ofKind(lines, kind);
  const to = index + by;
  if (index < 0 || index >= same.length || to < 0 || to >= same.length) return lines;
  const moved = [...same];
  [moved[index], moved[to]] = [moved[to], moved[index]];
  const other = lines.filter((l) => l.kind !== kind);
  return INCLUSION_KINDS.flatMap((k) => (k === kind ? moved : other.filter((l) => l.kind === k)));
}

/** The PUT body's `lines`: library lines by id (the API keeps or copies their text), typed lines with their text. */
export function linesBody(lines: DraftLine[]): Record<string, string>[] {
  const ordered = INCLUSION_KINDS.flatMap((k) => ofKind(lines, k));
  return ordered.map((l): Record<string, string> => {
    if (l.inclusion_item_id) return { inclusion_item_id: l.inclusion_item_id };
    return l.text_vi ? { kind: l.kind, text_en: l.text_en, text_vi: l.text_vi } : { kind: l.kind, text_en: l.text_en };
  });
}

export function sameLines(a: DraftLine[], b: DraftLine[]): boolean {
  return JSON.stringify(linesBody(a)) === JSON.stringify(linesBody(b));
}

/** The line in the page's language: Vietnamese when the line has it, else English (the default). */
export function lineText(line: { text_en: string; text_vi: string | null }, lang: string): string {
  return lang === "vi" && line.text_vi ? line.text_vi : line.text_en;
}

/** True when the client page has something to show. */
export function hasInclusions(data: ClientInclusions | null | undefined): data is ClientInclusions {
  if (!data) return false;
  return data.included.length > 0 || data.not_included.length > 0 || data.aa_tours.some((t) => t.included.length > 0 || t.not_included.length > 0);
}

const ERRORS = new Set([
  "inclusion_item_exists",
  "inclusion_item_archived",
  "inclusion_item_not_found",
  "inclusion_library_full",
  "too_many_lines",
  "proposal_not_draft",
  "role_forbidden",
]);

export function inclusionErrorKey(detail: unknown): string {
  return typeof detail === "string" && ERRORS.has(detail) ? `incl.error.${detail}` : "incl.error.other";
}
