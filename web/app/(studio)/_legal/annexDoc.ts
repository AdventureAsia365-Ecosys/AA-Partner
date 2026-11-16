// web/app/(studio)/_legal/annexDoc.ts
// AA-970 (design v3 card CRM-37b) — the documents of the public pages /legal/data and /legal/subprocessors: the frame
// (annex.messages.ts) around one section of the root DPA Annex 3 public summary (lib/legal/annexMarkdown.ts: the first
// heading is the data page, the second the sub-processor page). No React here.
import { translator, type Lang } from "@/app/_kit/core/i18n";
import { annexPages, annexSummary, type AnnexSummary } from "@/lib/legal/annexMarkdown";
import { DATA_PATH, SUBPROCESSORS_PATH } from "@/lib/studio/legal";
import { ANNEX_MESSAGES } from "./annex.messages";
import type { Inline, LegalDoc } from "./legalDoc";

export type AnnexPage = "data" | "subprocessors";

export function annexDoc(page: AnnexPage, lang: Lang, summary: AnnexSummary = annexSummary()): LegalDoc {
  const t = translator(ANNEX_MESSAGES, lang);
  const section = annexPages(summary)[page];
  const other: Inline =
    page === "data" ? { a: t("subTitle"), href: SUBPROCESSORS_PATH } : { a: t("dataTitle"), href: DATA_PATH };
  const intro: Inline[] = [page === "data" ? t("dataIntro") : t("subIntro"), " ", t("seeAlso"), " ", other, "."];
  if (t("englishOnly")) intro.push(" ", t("englishOnly"));
  return {
    metaTitle: page === "data" ? t("dataMeta") : t("subMeta"),
    title: page === "data" ? t("dataTitle") : t("subTitle"),
    intro,
    // The section's heading is the page title already: its blocks go under one untitled section.
    sections: section ? [{ id: section.id, title: "", blocks: section.blocks }] : [],
  };
}

/** The version line of both pages: the annex's version and review date. */
export function annexVersion(lang: Lang, summary: AnnexSummary = annexSummary()): { label: string; version: string; updated: string } {
  const t = translator(ANNEX_MESSAGES, lang);
  return { label: t("versionLabel"), version: t("version", summary.version), updated: t("updated", summary.updated) };
}
