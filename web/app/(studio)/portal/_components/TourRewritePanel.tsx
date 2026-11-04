"use client";
// web/app/(studio)/portal/_components/TourRewritePanel.tsx — AA-668 part 2 (S239): the tour rewrite in
// the Writer panel.
//
// "Request Rewrite" in the My Catalog Tours drawer (CatalogDrawer.tsx) used to swap the drawer's
// content for an inline LiveWriter. It now opens the same right-side writer drawer as Social
// Content (`WriterDrawer`): context header (tour title, language, channel), the live steps + text
// of the `t2_rewrite` job (LiveWriter compact), "Writing didn't finish" + Retry on a failed job,
// and on success "Open in My Catalog" (the version drawer) + Export DOCX. Closing the panel never
// stops the job: the page's `useWriteWatcher` polls the version and toasts when it settles.
//
// Market is not in the header: T2 rewrites have no market field (the version carries a language),
// and the Slate API has no per-Subject market either — see docs/implementation-notes/AA-668.md.
//
// The panel does not poll on its own for the version status: CatalogTab's my-versions query
// already refetches every 5 s while any version is being written, and hands the row in (`version`).

import { CheckCircle2, Download, ExternalLink, RotateCcw } from "lucide-react";
import { AiNotice, Button, K, translator, useT, type UiLang } from "@/app/_kit";
import { REWRITE_MESSAGES } from "./tours.messages";
import LiveWriter from "./LiveWriter";
import { WriterDrawer, type WatchItem, type WatchOutcome } from "./WriterPanel";
import { fetchVersionDetail, type Version } from "./catalogApi";
import { tourWorkStatus } from "./workStatus";

/** What My Catalog hands the panel when a rewrite starts (or a writing / failed row is opened). */
export interface TourRewriteTarget {
  versionId: string;
  publishedTourId: string;
  title: string;
  language: string;
}

/** The channel a T2 rewrite writes for: the tenant's own tour catalog. */

export type RewriteState = "starting" | "writing" | "failed" | "ready";

export function rewriteState(version: Pick<Version, "status" | "edit_source" | "job_status"> | null): RewriteState {
  if (!version) return "starting"; // the list has not picked the new version up yet
  // AA-760 — the shared work-status model (job_status included); queued counts as writing here.
  const s = tourWorkStatus(version);
  return s === "failed" ? "failed" : s === "done" ? "ready" : "writing";
}

/** The watch item for a tour rewrite closed mid-write (useWriteWatcher in WriterPanel.tsx). */
export function watchTourRewrite(t: TourRewriteTarget, lang: UiLang = "en"): WatchItem {
  const m = translator(REWRITE_MESSAGES, lang);
  return {
    id: t.versionId, group: t.versionId, title: t.title,
    check: () => fetchVersionDetail(t.versionId).then((v): WatchOutcome => {
      const s = rewriteState(v);
      return s === "ready" ? "ready" : s === "failed" ? "failed" : "running";
    }),
    readyText: m("readyText", t.title),
    failedText: m("failedText", t.title),
    slowText: m("slowText", t.title),
  };
}

export function TourRewritePanel({
  target, version, retries, retrying, exporting, onClose, onRetry, onOpenResult, onExportDocx,
}: {
  target: TourRewriteTarget | null;
  /** The version row from the my-versions list (null until the list has it). */
  version: Version | null;
  /** Bumped on every retry so the live view starts again from a fresh poll. */
  retries: number;
  retrying: boolean;
  exporting: boolean;
  /** `running` = the job was still writing when the tenant closed the panel. */
  onClose: (running: boolean) => void;
  onRetry: () => void;
  onOpenResult: () => void;
  onExportDocx: () => void;
}) {
  const t = useT(REWRITE_MESSAGES);
  const state = rewriteState(version);
  const running = state === "starting" || state === "writing";
  const ready = state === "ready";

  return (
    <WriterDrawer
      open={target !== null}
      onClose={() => onClose(running)}
      title={target?.title ?? ""}
      context={target ? [
        { label: t("rewritingIn"), value: target.language },
        { label: t("for"), value: t("channel") },
      ] : []}
      footer={ready && target ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 10, width: "100%", minWidth: 0 }}>
          <AiNotice variant="publisher" />
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", justifyContent: "flex-end" }}>
            <Button size="sm" variant="secondary" disabled={exporting} onClick={onExportDocx}>
              <Download size={12} /> {exporting ? t("exporting") : t("exportDocx")}
            </Button>
            <Button size="sm" variant="primary" onClick={onOpenResult}>
              <ExternalLink size={12} /> {t("openInMyTours")}
            </Button>
          </div>
        </div>
      ) : undefined}
    >
      {target && state === "failed" && (
        <div data-testid="tour-rewrite-failed" style={{ display: "flex", flexDirection: "column", gap: 10, alignItems: "flex-start" }}>
          <div style={{ fontSize: 14, fontWeight: 600, color: K.ink }}>{t("didntFinish")}</div>
          <div style={{ fontSize: 12.5, color: K.muted }}>
            {t("didntFinishSub")}
          </div>
          <Button size="sm" variant="primary" disabled={retrying} onClick={onRetry}>
            <RotateCcw size={12} /> {retrying ? t("retrying") : t("retryWriting")}
          </Button>
        </div>
      )}
      {target && ready && (
        <div data-testid="tour-rewrite-ready" style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12.5, color: K.body }}>
          <CheckCircle2 size={15} color={K.success} />
          <span>{t("readyLine")}</span>
        </div>
      )}
      {target && state !== "failed" && (
        <>
          {/* Polls until the version leaves "writing" (untilInactive), and is keyed per retry so a
              retried job starts from a fresh view. Once ready it keeps the last snapshot (the
              finished text). */}
          <LiveWriter key={`${target.versionId}:${retries}`} kind="tour" jobId={target.versionId}
            title={t("writingYourTour")} compact active={running} untilInactive />
          {running && (
            <div style={{ fontSize: 12, color: K.muted }}>
              {t("runningNote")}
            </div>
          )}
        </>
      )}
    </WriterDrawer>
  );
}
