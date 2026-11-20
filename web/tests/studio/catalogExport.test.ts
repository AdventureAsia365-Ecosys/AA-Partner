// AA-1168 (EU AI Act Art. 50): the My Content CSV / XLSX export carries the AI columns and the disclosure
// (lib/studio/catalogExport.ts). Run: npm test (from web/). Invented tours and places only.
import { test } from "vitest";
import assert from "node:assert/strict";
import * as XLSX from "xlsx";
import {
  AI_DISCLOSURE_TEXT,
  AI_KEYWORD,
  CATALOG_EXPORT_HEADERS,
  CATALOG_EXPORT_WIDTHS,
  catalogExportRow,
  catalogExportRows,
  catalogTsv,
  catalogWorkbook,
  exportDisclosure,
  flattenVal,
  type ExportVersion,
} from "@/lib/studio/catalogExport";

const MARK = {
  ai_generated: true,
  generator: "Adventure Asia Content studio",
  model: null,
  generated_at: "2026-11-02T09:30:00+00:00",
  human_edited: false,
};

const listRow = (over: Partial<ExportVersion> = {}): ExportVersion => ({
  rewritten_content: JSON.stringify({
    name: "Lantern Valley Ride",
    subtitle: "Two days on quiet roads",
    seo_title: "Lantern Valley by bike",
    seo_meta: "Ride the valley",
    summary: "Cycle past\nrice terraces.",
    highlights: ["Morning market", { text: "River ferry" }],
    itineraries: [{ title: "Day 1", description: "Ride\tnorth" }],
  }),
  aa_name: "Master name",
  country: "Testland",
  duration: "2 days",
  created_at: "2026-11-02T09:30:00Z",
  rewrite_language: "en-US",
  edit_source: "ai_generated",
  ai: MARK,
  ...over,
});

test("the headers end with the four AI columns and every header has a width", () => {
  assert.deepEqual(CATALOG_EXPORT_HEADERS.slice(-4), ["AI generated", "AI generator", "AI generated at", "AI human edited"]);
  assert.equal(CATALOG_EXPORT_WIDTHS.length, CATALOG_EXPORT_HEADERS.length);
});

test("the fallback disclosure is the English seed of the CIS setting", () => {
  assert.equal(AI_DISCLOSURE_TEXT, "This content was created with the help of AI.");
});

const WORDING = {
  text: { en: "Drafted with a writing model.", vi: "Soạn bằng mô hình viết." },
  social_tag: "#ModelDrafted",
};

test("the export uses the wording read from CIS, in the versions' language", () => {
  assert.equal(exportDisclosure(WORDING, [listRow()]), "Drafted with a writing model.");
  assert.equal(exportDisclosure(WORDING, [listRow({ rewrite_language: "vi" }), listRow({ rewrite_language: "VI-vn" })]),
    "Soạn bằng mô hình viết.");
  // mixed languages, or a language without a line: English
  assert.equal(exportDisclosure(WORDING, [listRow({ rewrite_language: "vi" }), listRow()]), "Drafted with a writing model.");
  assert.equal(exportDisclosure(WORDING, [listRow({ rewrite_language: "th" })]), "Drafted with a writing model.");
  assert.equal(exportDisclosure({ text: { en: "Line\nbreak" } }, []), "Line break");
});

test("the fallback line is used only when the wording cannot be read or has no English line", () => {
  assert.equal(exportDisclosure(null, [listRow()]), AI_DISCLOSURE_TEXT);
  assert.equal(exportDisclosure(undefined), AI_DISCLOSURE_TEXT);
  assert.equal(exportDisclosure({ text: { vi: "Chỉ tiếng Việt." } }, [listRow({ rewrite_language: "vi" })]),
    AI_DISCLOSURE_TEXT);
  assert.equal(exportDisclosure({ text: { en: "  " } }, []), AI_DISCLOSURE_TEXT);
});

test("the CSV and the XLSX end with the wording read from CIS", () => {
  const rows = catalogExportRows([null], [listRow()]);
  const line = exportDisclosure(WORDING, [listRow()]);
  assert.equal(catalogTsv(rows, line).split("\n").at(-1), "Drafted with a writing model.");
  assert.equal(catalogTsv(rows, "  ").split("\n").at(-1), AI_DISCLOSURE_TEXT);   // never an empty last line
  const back = XLSX.read(XLSX.write(catalogWorkbook(rows, line), { type: "buffer", bookType: "xlsx" }), { type: "buffer" });
  const aoa = XLSX.utils.sheet_to_json<string[]>(back.Sheets["My Tours"], { header: 1, defval: "" });
  assert.equal(aoa[aoa.length - 1][0], "Drafted with a writing model.");
  assert.equal(back.Props?.Comments, "Drafted with a writing model.");
});

test("a row takes the rewrite text first and the AI columns from the version mark", () => {
  const r = catalogExportRow(null, listRow());
  assert.equal(r.Name, "Lantern Valley Ride");
  assert.equal(r.Subtitle, "Two days on quiet roads");
  assert.equal(r.Summary, "Cycle past\nrice terraces.");
  assert.equal(r.Highlights, "Morning market | River ferry");
  assert.equal(r.Itineraries, "Ride\tnorth");
  assert.equal(r.Country, "Testland");
  assert.equal(r["Created At"], "02/11/2026");
  assert.equal(r.Language, "en-US");
  assert.equal(r["AI generated"], "true");
  assert.equal(r["AI generator"], "Adventure Asia Content studio");
  assert.equal(r["AI generated at"], "2026-11-02T09:30:00+00:00");
  assert.equal(r["AI human edited"], "false");
});

test("the detail read wins over the list row, for the text and for the mark", () => {
  const detail = listRow({
    rewritten_content: JSON.stringify({ name: "Edited Valley Ride" }),
    edit_source: "tenant_edit",
    ai: { ...MARK, generated_at: null, human_edited: true },
  });
  const r = catalogExportRow(detail, listRow());
  assert.equal(r.Name, "Edited Valley Ride");
  assert.equal(r["AI human edited"], "true");
  assert.equal(r["AI generated at"], "");
});

test("without a rewrite the Master aa_* text is exported, still marked as AI text", () => {
  const r = catalogExportRow(null, listRow({ rewritten_content: null, aa_subtitle: "Master subtitle" }));
  assert.equal(r.Name, "Master name");
  assert.equal(r.Subtitle, "Master subtitle");
  assert.equal(r["AI generated"], "true");
});

test("a version read without the mark is still AI generated; human edited comes from edit_source", () => {
  const plain = catalogExportRow(null, listRow({ ai: null }));
  assert.equal(plain["AI generated"], "true");
  assert.equal(plain["AI generator"], "");
  assert.equal(plain["AI human edited"], "false");
  const edited = catalogExportRow(null, listRow({ ai: undefined, edit_source: "tenant_edit" }));
  assert.equal(edited["AI human edited"], "true");
  const unknown = catalogExportRow(null, listRow({ ai: undefined, edit_source: null }));
  assert.equal(unknown["AI human edited"], "");
});

test("a failed detail read (null) falls back to the list row", () => {
  const rows = catalogExportRows([null, listRow({ country: "Otherland" })], [listRow(), listRow()]);
  assert.equal(rows[0].Country, "Testland");
  assert.equal(rows[1].Country, "Otherland");
});

test("flattenVal joins lists, reads JSON strings and keeps one line", () => {
  assert.equal(flattenVal('["a", "b"]'), "a | b");
  assert.equal(flattenVal("plain\ntext"), "plain text");
  assert.equal(flattenVal({ a: "x", b: "", c: "y" }), "x | y");
  assert.equal(flattenVal(null), "");
});

test("the CSV body is the header, one line per tour, then the disclosure line", () => {
  const tsv = catalogTsv(catalogExportRows([null], [listRow()]));
  const lines = tsv.split("\n");
  assert.equal(lines.length, 3);
  assert.equal(lines[0], CATALOG_EXPORT_HEADERS.join("\t"));
  const cells = lines[1].split("\t");
  assert.equal(cells.length, CATALOG_EXPORT_HEADERS.length);
  assert.equal(cells[CATALOG_EXPORT_HEADERS.indexOf("Summary")], "Cycle past rice terraces.");
  assert.equal(cells[CATALOG_EXPORT_HEADERS.indexOf("AI generated")], "true");
  assert.equal(lines[2], AI_DISCLOSURE_TEXT);
});

test("the XLSX workbook keeps the AI columns, ends with the disclosure and carries the ai-generated keyword", () => {
  const wb = catalogWorkbook(catalogExportRows([null], [listRow()]));
  const back = XLSX.read(XLSX.write(wb, { type: "buffer", bookType: "xlsx" }), { type: "buffer" });
  assert.deepEqual(back.SheetNames, ["My Tours"]);
  const aoa = XLSX.utils.sheet_to_json<string[]>(back.Sheets["My Tours"], { header: 1, defval: "" });
  assert.deepEqual(aoa[0], [...CATALOG_EXPORT_HEADERS]);
  assert.equal(aoa[1][CATALOG_EXPORT_HEADERS.indexOf("AI generator")], "Adventure Asia Content studio");
  assert.equal(aoa[aoa.length - 1][0], AI_DISCLOSURE_TEXT);
  assert.equal(back.Props?.Keywords, AI_KEYWORD);
  assert.equal(back.Props?.Comments, AI_DISCLOSURE_TEXT);
});
