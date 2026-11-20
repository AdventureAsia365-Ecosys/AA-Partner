// web/lib/studio/catalogExport.ts
// AA-1168 (EU AI Act Art. 50) — the My Content CSV / XLSX export (CatalogTab "CSV (n)" / "XLSX (n)", files
// my-catalog.csv and my-catalog.xlsx): its rows, the machine-readable AI columns and the visible disclosure.
// Moved out of CatalogTab.tsx (AA-662 logic unchanged for the eleven text columns) so the rows are testable.
//
// Every exported tour text is AI-written: the version's `rewritten_content` is a T2 rewrite and the `aa_*` fallbacks
// are the A-series Master text. Each row therefore carries four AI columns from the version's `ai` object, which CIS
// adds to every tenant version read (`/v1/tours/my-versions`, `/v1/tours/versions/{id}`; CIS
// api/routers/_tenant_projection.py project_version -> ai_marking.version_mark): `AI generated`, `AI generator`,
// `AI generated at`, `AI human edited` ("true" / "false", empty when not known). The file also ends with the
// disclosure line, the same wording as CIS `ai_marking.DISCLOSURE_TEXT` (WordPress, Facebook and the text export),
// and the XLSX workbook carries the keyword `ai-generated` in its document properties, like the T2 DOCX download.
// Pure (no React); xlsx is the same SheetJS build CatalogTab already loads.

import * as XLSX from "xlsx";

import { parseContent } from "./format";

/** The visible AI disclosure, word for word the CIS `ai_marking.DISCLOSURE_TEXT` (one wording on every channel). */
export const AI_DISCLOSURE_TEXT = "This content was created with the help of AI.";

/** The XLSX document property keyword of AI-generated content (the T2 DOCX download uses the same keyword). */
export const AI_KEYWORD = "ai-generated";

/** The `ai` object of a tenant tour version (CIS ai_marking.version_mark). */
export interface VersionAiMark {
  ai_generated: boolean;
  generator?: string | null;
  model?: string | null;
  generated_at?: string | null;
  human_edited?: boolean | null;
}

/** The fields of a version (list row or detail) the export reads. */
export interface ExportVersion {
  rewritten_content?: string | null;
  aa_name?: string | null;
  aa_subtitle?: string | null;
  aa_summary?: string | null;
  aa_highlights?: string | null;
  aa_itineraries?: string | null;
  aa_seo_title?: string | null;
  aa_seo_meta?: string | null;
  country?: string | null;
  duration?: string | null;
  created_at?: string | null;
  rewrite_language?: string | null;
  edit_source?: string | null;
  ai?: VersionAiMark | null;
}

export const AI_EXPORT_HEADERS = ["AI generated", "AI generator", "AI generated at", "AI human edited"] as const;

export const CATALOG_EXPORT_HEADERS = [
  "Name",
  "Subtitle",
  "Country",
  "Duration",
  "SEO Title",
  "SEO Meta",
  "Summary",
  "Highlights",
  "Itineraries",
  "Created At",
  "Language",
  ...AI_EXPORT_HEADERS,
] as const;

/** XLSX column widths, one per header. */
export const CATALOG_EXPORT_WIDTHS = [30, 30, 15, 12, 40, 50, 60, 60, 80, 15, 15, 12, 30, 24, 14];

export type ExportRow = Record<(typeof CATALOG_EXPORT_HEADERS)[number], string>;

function oneLine(s: string): string {
  return s.replace(/[\t\n\r]+/g, " ").trim();
}

function itemText(x: unknown): string {
  if (x == null) return "";
  if (typeof x === "string") return oneLine(x);
  if (typeof x !== "object") return String(x);
  const o = x as Record<string, unknown>;
  return String(o.keyword ?? o.text ?? o.description ?? o.title ?? o.value ?? JSON.stringify(x));
}

/** A JSON-ish field (string, JSON string, list, object) as one line of text; lists are joined with " | ". */
export function flattenVal(v: unknown): string {
  if (v == null || v === "") return "";
  if (typeof v === "string") {
    try {
      return flattenVal(JSON.parse(v));
    } catch {
      /* plain string */
    }
    return oneLine(v);
  }
  if (Array.isArray(v)) return v.map(itemText).filter(Boolean).join(" | ");
  if (typeof v === "object") {
    return Object.values(v as Record<string, unknown>)
      .filter((x) => x != null && x !== "")
      .map((x) => String(x))
      .join(" | ");
  }
  return String(v);
}

function firstSet(...values: unknown[]): unknown {
  return values.find((x) => x != null);
}

function boolText(b: boolean | null | undefined): string {
  if (b == null) return "";
  return b ? "true" : "false";
}

function humanEditedOf(mark: VersionAiMark | null, editSource: unknown): boolean | null | undefined {
  if (mark) return mark.human_edited;
  return editSource == null ? null : editSource === "tenant_edit";
}

/** The four AI columns of a version. Every exported version is AI text, so `AI generated` reads "true" unless the
 *  mark says otherwise; human edited falls back to the version's own `edit_source` when the mark is absent. */
export function aiColumns(detail: ExportVersion | null, version: ExportVersion): Record<(typeof AI_EXPORT_HEADERS)[number], string> {
  const d: ExportVersion = detail ?? {};
  const mark = (firstSet(d.ai, version.ai) as VersionAiMark | undefined) ?? null;
  const m: Partial<VersionAiMark> = mark ?? {};
  return {
    "AI generated": m.ai_generated === false ? "false" : "true",
    "AI generator": String(firstSet(m.generator, "")),
    "AI generated at": String(firstSet(m.generated_at, "")),
    "AI human edited": boolText(humanEditedOf(mark, firstSet(d.edit_source, version.edit_source))),
  };
}

/** The text columns: the rewrite's own field first, then the detail's `aa_*` column, then the list row's. */
const TEXT_COLUMNS = [
  ["Name", "name", "aa_name", false],
  ["Subtitle", "subtitle", "aa_subtitle", false],
  ["SEO Title", "seo_title", "aa_seo_title", false],
  ["SEO Meta", "seo_meta", "aa_seo_meta", false],
  ["Summary", "summary", "aa_summary", false],
  ["Highlights", "highlights", "aa_highlights", true],
  ["Itineraries", "itineraries", "aa_itineraries", true],
] as const;

/** One export row of a selected version and its detail read (null when the detail read failed). */
export function catalogExportRow(detail: ExportVersion | null, version: ExportVersion): ExportRow {
  const d: ExportVersion = detail ?? {};
  const rc: Record<string, unknown> = parseContent(firstSet(d.rewritten_content, version.rewritten_content)) ?? {};
  const text: Record<string, string> = {};
  for (const [header, key, aaKey, flatten] of TEXT_COLUMNS) {
    const value = firstSet(rc[key], d[aaKey], version[aaKey], "");
    text[header] = flatten ? flattenVal(value) : String(value);
  }
  return {
    Name: text.Name,
    Subtitle: text.Subtitle,
    Country: String(firstSet(d.country, version.country, "")),
    Duration: String(firstSet(d.duration, version.duration, "")),
    "SEO Title": text["SEO Title"],
    "SEO Meta": text["SEO Meta"],
    Summary: text.Summary,
    Highlights: text.Highlights,
    Itineraries: text.Itineraries,
    "Created At": exportDate(version.created_at),
    Language: String(firstSet(version.rewrite_language, "")),
    ...aiColumns(detail, version),
  };
}

function exportDate(iso: string | null | undefined): string {
  return iso ? new Date(iso).toLocaleDateString("en-GB") : "";
}

/** The rows of the selected versions; `details[i]` is the detail read of `versions[i]` (or null). */
export function catalogExportRows(details: (ExportVersion | null)[], versions: ExportVersion[]): ExportRow[] {
  return versions.map((v, i) => catalogExportRow(details[i] ?? null, v));
}

/** The my-catalog.csv body (tab separated, as before): the header, one line per row, then the disclosure line. */
export function catalogTsv(rows: ExportRow[]): string {
  const lines = [
    CATALOG_EXPORT_HEADERS.join("\t"),
    ...rows.map((r) => CATALOG_EXPORT_HEADERS.map((h) => oneLineCell(r[h])).join("\t")),
    AI_DISCLOSURE_TEXT,
  ];
  return lines.join("\n");
}

function oneLineCell(s: string | undefined): string {
  return (s ?? "").replace(/[\t\n\r]+/g, " ");
}

/** The my-catalog.xlsx workbook: sheet "My Tours" (the rows, then the disclosure in the last row) and the document
 *  properties keyword `ai-generated` with the disclosure as the comment. */
export function catalogWorkbook(rows: ExportRow[]): XLSX.WorkBook {
  const ws = XLSX.utils.json_to_sheet(rows, { header: [...CATALOG_EXPORT_HEADERS] });
  XLSX.utils.sheet_add_aoa(ws, [[AI_DISCLOSURE_TEXT]], { origin: -1 });
  ws["!cols"] = CATALOG_EXPORT_WIDTHS.map((w) => ({ wch: w }));
  const wb = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, ws, "My Tours");
  wb.Props = { Title: "My Tours", Keywords: AI_KEYWORD, Comments: AI_DISCLOSURE_TEXT };
  return wb;
}
