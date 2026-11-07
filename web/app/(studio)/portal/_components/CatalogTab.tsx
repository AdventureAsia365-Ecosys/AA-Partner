"use client";
// web/app/(studio)/portal/_components/CatalogTab.tsx — My Content.
// AA-662 PR 3: migrated to the UI kit (DataTable, Drawer, Toast) + react-query. The hand-rolled
// list fetch + the two setInterval polling effects are replaced by one useQuery with a
// refetchInterval that runs only while a tour is still being AI-written (isAiWriting) and stops on
// its own — no setState-in-effect (React Compiler rule, frontend/AGENTS.md). The drawer body lives
// in CatalogDrawer.tsx. Export (CSV/XLSX/DOCX) and the one-row-per-tour grouping are unchanged.
// AA-668 (S239): "Request Rewrite" (and opening a tour that is being written or failed) opens the
// Writer panel (TourRewritePanel.tsx) — live progress, Retry, the result; closing it mid-write keeps
// the job and the page toasts when it settles (useWriteWatcher). The version drawer opens on
// finished versions only, and stays on the version it was opened on (a save moves it to the new
// version the save created) — a rewrite of the same tour started elsewhere neither closes it nor
// reopens it when that rewrite finishes.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "next/navigation";
import Link from "next/link";
import { Download } from "lucide-react";
import { useCallback, useMemo, useState } from "react";
import * as XLSX from "xlsx";
import { CtaLink } from "@/app/_kit/local/studio/StudioCard";
import { parseContent } from "@/lib/studio/format";
import { dispositionFileName } from "@/lib/studio/downloadHeaders";
import { AiNotice, Button, type ColumnDef, DataTable, Drawer, ToastProvider, useToast, K, sans, useLang, useT } from "@/app/_kit";
import { CATALOG_MESSAGES } from "./tours.messages";
import { CatalogDrawer } from "./CatalogDrawer";
import { TourRewritePanel, watchTourRewrite, type TourRewriteTarget } from "./TourRewritePanel";
import { WorkStatusBadge, activePollInterval, latestVersionByTour, tourWorkStatus } from "./workStatus";
import { ServedBadge } from "./ServedBadge";
import { plainError } from "./studioError";
import { MasterNewerBadge } from "./MasterNewerNotice";
import { showMasterNotice } from "@/lib/studio/masterNotice";
import { useWriteWatcher } from "./WriterPanel";
import { forgetLiveSnapshot } from "./LiveWriter";
import {
  fetchMyVersions,
  fetchVersionDetail,
  isAiWriting,
  isRewriteFailed,
  requestRewrite as apiRequestRewrite,
  retryRewrite as apiRetryRewrite,
  saveVersionEdit,
  type Version,
} from "./catalogApi";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function flattenVal(v: any): string {
  if (v == null || v === "") return "";
  if (typeof v === "string") {
    try {
      return flattenVal(JSON.parse(v));
    } catch {
      /* plain string */
    }
    return v.replace(/[\t\n\r]+/g, " ").trim();
  }
  if (Array.isArray(v)) {
    return v
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      .map((x: any) => {
        if (x == null) return "";
        if (typeof x === "string") return x.replace(/[\t\n\r]+/g, " ").trim();
        if (typeof x === "object")
          return String(x.keyword ?? x.text ?? x.description ?? x.title ?? x.value ?? JSON.stringify(x));
        return String(x);
      })
      .filter(Boolean)
      .join(" | ");
  }
  if (typeof v === "object") {
    return Object.values(v)
      .filter((x) => x != null && x !== "")
      .map((x) => String(x))
      .join(" | ");
  }
  return String(v);
}

function CatalogInner() {
  const t = useT(CATALOG_MESSAGES);
  const lang = useLang();
  const toast = useToast();
  const qc = useQueryClient();
  const searchParams = useSearchParams();
  const tourIdFilter = searchParams.get("tour_id");

  // The version open in the drawer — pinned by version id, not tour id (AA-668 review): the tour's
  // latest version can become a rewrite in progress while the drawer is open (another tab, the
  // Browse Tours batch Rewrite), and following it would close the drawer, drop unsaved edits and
  // pop it open again when that rewrite finished.
  const [openVersionId, setOpenVersionId] = useState<string | null>(null);
  const [isExporting, setIsExporting] = useState(false);
  const [exportingDocxId, setExportingDocxId] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [saveOk, setSaveOk] = useState(false);
  // AA-668 — the tour rewrite open in the Writer panel, and its retry count (remounts the live view).
  const [rewrite, setRewrite] = useState<TourRewriteTarget | null>(null);
  const [retries, setRetries] = useState(0);
  const { watch, unwatch } = useWriteWatcher();

  // ── Query: my versions. Poll (5s) only while a tour is still being written; stop automatically
  //    once nothing is writing — read the live result in the interval fn so no effect is needed. ──
  const { data: list = [], isLoading } = useQuery({
    queryKey: ["my-versions"],
    queryFn: fetchMyVersions,
    // AA-760 — shared work-status model (same predicate as Browse Tours and the sidebar badge).
    refetchInterval: (query) => activePollInterval((query.state.data ?? []).map(tourWorkStatus)),
  });

  // One row per tour: highest version_number per published_tour_id (unchanged).
  const grouped = useMemo(
    () => [...latestVersionByTour(list).values()].sort((a, b) => (a.created_at < b.created_at ? 1 : -1)),
    [list],
  );

  const visible = useMemo(
    () => (tourIdFilter ? grouped.filter((g) => g.tour_id === tourIdFilter) : grouped),
    [grouped, tourIdFilter],
  );

  // The version drawer shows finished versions only; writing / failed ones live in the panel.
  // (A finished version never turns writing again, so this guard only filters a stale id.)
  const openGroup = useMemo(() => {
    const g = openVersionId ? list.find((v) => v.id === openVersionId) ?? null : null;
    return g && !isAiWriting(g) && !isRewriteFailed(g) ? g : null;
  }, [list, openVersionId]);

  const rewriteVersion = useMemo(
    () => (rewrite ? list.find((v) => v.id === rewrite.versionId) ?? null : null),
    [list, rewrite],
  );

  const openRewritePanel = useCallback((v: Version) => {
    if (!v.published_tour_id) return;
    unwatch(v.id);
    setOpenVersionId(null);
    setRewrite({ versionId: v.id, publishedTourId: v.published_tour_id, title: v.aa_name || t("tour"), language: v.rewrite_language || "en-US" });
  }, [unwatch, t]);

  function openRow(v: Version) {
    if (isAiWriting(v) || isRewriteFailed(v)) openRewritePanel(v);
    else setOpenVersionId(v.id);
  }

  const invalidate = () => qc.invalidateQueries({ queryKey: ["my-versions"] });

  // ── Mutations ──
  const retryM = useMutation({
    mutationFn: apiRetryRewrite,
    // AA-668 — a retry reruns the job under the same version id: the live view must not reopen
    // on the failed run's remembered text (the Writer panel or the row's "View progress").
    onMutate: (versionId: string) => forgetLiveSnapshot("tour", versionId),
    onSuccess: (_r, versionId) => {
      forgetLiveSnapshot("tour", versionId);
      setRetries((n) => n + 1);
      invalidate();
    },
    onError: (e) => toast.error(plainError(e, t("retryFailed"), lang)),
  });

  const saveM = useMutation({
    mutationFn: (vars: { versionId: string; edited: Record<string, unknown> }) =>
      saveVersionEdit(vars.versionId, vars.edited),
    onSuccess: async (r) => {
      // An edit is saved as a new version: refetch first, then move the drawer onto it (the list
      // has the row by then, so the drawer never blinks closed).
      await invalidate();
      if (r.new_version_id) setOpenVersionId(r.new_version_id);
      setSaveOk(true);
      setDirty(false);
      toast.success(t("saved"));
    },
    onError: (e) => toast.error(plainError(e, t("saveFailed"), lang)),
  });

  const rewriteM = useMutation({
    mutationFn: (vars: { publishedTourId: string; lang: string; title: string }) =>
      apiRequestRewrite(vars.publishedTourId, vars.lang),
    onSuccess: (r, vars) => {
      // AA-668 — follow the new version in the Writer panel instead of a toast.
      setOpenVersionId(null);
      setDirty(false);
      setSaveOk(false);
      setRewrite({ versionId: r.version_id, publishedTourId: vars.publishedTourId, title: vars.title, language: vars.lang });
      invalidate();
    },
    onError: (e) => toast.error(plainError(e, t("rewriteRequestFailed"), lang)),
  });

  // ── DOCX export (unchanged endpoint/trigger) ──
  async function exportDocx(versionId: string) {
    setExportingDocxId(versionId);
    try {
      const r = await fetch(`/api/tenant/v1/tours/versions/${versionId}/export-docx`);
      if (!r.ok) {
        toast.error(t("docxFailed"));
        return;
      }
      const blob = await r.blob();
      // AA-1040 — the BFF passes CIS's file name on (quoted, checked: lib/studio/downloadHeaders.ts).
      const filename = dispositionFileName(r.headers.get("content-disposition")) ?? "tour.docx";
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
    } finally {
      setExportingDocxId(null);
    }
  }

  // ── CSV/XLSX multi-export (unchanged logic) ──
  async function exportSelected(fmt: "csv" | "xls", selectedVersions: Version[]) {
    if (selectedVersions.length === 0) return;
    setIsExporting(true);
    try {
      const fullData = await Promise.all(
        selectedVersions.map((v) => fetchVersionDetail(v.id).catch(() => null)),
      );
      const headers = [
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
      ];
      const rows: Record<string, string>[] = fullData.map((d, i) => {
        const v = selectedVersions[i];
        const rc = parseContent(d?.rewritten_content ?? v.rewritten_content) as Record<string, unknown> | null;
        return {
          Name: String(rc?.name ?? d?.aa_name ?? v.aa_name ?? ""),
          Subtitle: String(rc?.subtitle ?? d?.aa_subtitle ?? ""),
          Country: String(d?.country ?? v.country ?? ""),
          Duration: String(d?.duration ?? v.duration ?? ""),
          "SEO Title": String(rc?.seo_title ?? d?.aa_seo_title ?? ""),
          "SEO Meta": String(rc?.seo_meta ?? d?.aa_seo_meta ?? ""),
          Summary: String(rc?.summary ?? d?.aa_summary ?? ""),
          Highlights: flattenVal(rc?.highlights ?? d?.aa_highlights ?? ""),
          Itineraries: flattenVal(rc?.itineraries ?? d?.aa_itineraries ?? ""),
          "Created At": v.created_at ? new Date(v.created_at).toLocaleDateString("en-GB") : "",
          Language: v.rewrite_language,
        };
      });
      if (fmt === "csv") {
        const san = (s: string) => s.replace(/[\t\n\r]+/g, " ");
        const tsv = [headers.join("\t"), ...rows.map((r) => headers.map((h) => san(r[h] ?? "")).join("\t"))].join("\n");
        const blob = new Blob(["\ufeff" + tsv], { type: "text/csv;charset=utf-8" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = "my-catalog.csv";
        a.click();
        URL.revokeObjectURL(url);
      } else {
        const ws = XLSX.utils.json_to_sheet(rows, { header: headers });
        ws["!cols"] = [30, 30, 15, 12, 40, 50, 60, 60, 80, 15, 15].map((w) => ({ wch: w }));
        const wb = XLSX.utils.book_new();
        XLSX.utils.book_append_sheet(wb, ws, "My Tours");
        XLSX.writeFile(wb, "my-catalog.xlsx");
      }
    } finally {
      setIsExporting(false);
    }
  }

  // ── Columns ──
  const columns = useMemo<ColumnDef<Version, unknown>[]>(
    () => [
      {
        accessorKey: "aa_name",
        header: t("colName"),
        cell: (c) => {
          const v = c.row.original;
          return (
            <div>
              <div style={{ fontWeight: 600, color: K.ink }}>{v.aa_name || t("tour")}</div>
              {/* AA-986 L3: persistent badge when AA no longer serves (or has paused) the tour */}
              {v.served_notice && <div style={{ marginTop: 4 }}><ServedBadge fields={v} /></div>}
              {/* AA-990: AA updated the tour after this version; the drawer says it in full, nothing is changed */}
              {showMasterNotice(v) && <div style={{ marginTop: 4 }}><MasterNewerBadge version={v} /></div>}
              {v.aa_subtitle && (
                <div style={{ fontWeight: 400, fontSize: 11.5, color: K.muted, marginTop: 2 }}>{v.aa_subtitle}</div>
              )}
            </div>
          );
        },
      },
      {
        accessorKey: "country",
        header: t("colCountry"),
        cell: (c) => <span style={{ color: K.body }}>{c.row.original.country || "—"}</span>,
      },
      {
        id: "state",
        header: t("colStatus"),
        enableSorting: false,
        cell: (c) => <WorkStatusBadge status={tourWorkStatus(c.row.original)} />,
      },
      {
        id: "actions",
        header: t("colActions"),
        enableSorting: false,
        enableHiding: false,
        meta: { hideLabelOnCard: true }, // AA-760 — no "ACTIONS" label row on the phone card
        cell: (c) => {
          const v = c.row.original;
          return (
            <div onClick={(e) => e.stopPropagation()} style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
              {isRewriteFailed(v) ? (
                <Button variant="secondary" size="sm" disabled={retryM.isPending} onClick={() => retryM.mutate(v.id)}>
                  {retryM.isPending ? t("retrying") : t("retry")}
                </Button>
              ) : isAiWriting(v) ? (
                <Button variant="secondary" size="sm" onClick={() => openRewritePanel(v)}>
                  {t("viewProgress")}
                </Button>
              ) : (
                <Button variant="secondary" size="sm" onClick={() => setOpenVersionId(v.id)}>
                  {t("open")}
                </Button>
              )}
            </div>
          );
        },
      },
    ],
    [retryM, openRewritePanel, t],
  );

  return (
    <>
      {tourIdFilter && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 10,
            padding: "9px 14px",
            marginBottom: 12,
            borderRadius: 8,
            background: K.accentTint,
            border: `1px solid ${K.accentSoft}`,
            fontFamily: sans,
          }}
        >
          <span style={{ fontSize: 12, color: K.amber }}>
            {t("showingFor")} <strong>{visible[0]?.aa_name ?? t("thisTour")}</strong>
          </span>
          <Link href="/portal/tours/mine" style={{ fontSize: 12, color: K.amber, fontWeight: 600, textDecoration: "none" }}>
            {t("clearFilter")}
          </Link>
        </div>
      )}

      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
        <span style={{ fontSize: 15, fontWeight: 700, color: K.ink, fontFamily: sans }}>{t("yourTours")}</span>
        {/* AA-760 — no count until the versions load ("0 tours" read as "you have no tours"). */}
        {/* AA-760 — and none when there is nothing yet (the empty state below says so). */}
        {!isLoading && grouped.length > 0 && (
          <span style={{ fontSize: 12, color: K.muted2 }}>
            {visible.length < grouped.length ? t("countOf", visible.length, grouped.length) : `${grouped.length}`} {t("tourWord", grouped.length)}
          </span>
        )}
      </div>
      {/* AA-819 — AUP notice for the CSV/XLSX/DOCX exports on this page. */}
      <AiNotice variant="publisher" style={{ marginBottom: 12 }} />

      <DataTable<Version>
        data={visible}
        columns={columns}
        tableId="my-content"
        getRowId={(r) => r.id}
        loading={isLoading}
        searchable
        searchPlaceholder={t("searchPlaceholder")}
        enableSelection
        enableSavedViews
        enableCsv
        csvFilename="my-catalog"
        pageSize={25}
        emptyTitle={t("emptyTitle")}
        emptyDescription={t("emptySub")}
        emptyAction={grouped.length === 0 ? <CtaLink href="/portal/tours">{t("browseTours")}</CtaLink> : undefined}
        onRowClick={openRow}
        bulkActions={({ selectedRows, clearSelection }) => (
          <>
            <Button variant="secondary" size="sm" disabled={isExporting} onClick={() => exportSelected("csv", selectedRows)}>
              <Download size={12} /> {isExporting ? t("fetching") : `CSV (${selectedRows.length})`}
            </Button>
            <Button variant="primary" size="sm" disabled={isExporting} onClick={() => exportSelected("xls", selectedRows)}>
              <Download size={12} /> {isExporting ? t("fetching") : `XLSX (${selectedRows.length})`}
            </Button>
            <Button variant="ghost" size="sm" onClick={clearSelection}>
              {t("clear")}
            </Button>
          </>
        )}
      />

      {/* S241 UI verify — the tour name is the kit Drawer title (it stays visible while the body
          scrolls); CatalogDrawer's in-body header no longer repeats it. */}
      <Drawer open={!!openGroup} onClose={() => setOpenVersionId(null)} title={openGroup?.aa_name || t("tour")} width={1000}>
        {openGroup && (
          <CatalogDrawer
            key={openGroup.id}
            group={openGroup}
            saving={saveM.isPending}
            saveOk={saveOk}
            dirty={dirty}
            exporting={exportingDocxId === openGroup.id}
            rewriting={rewriteM.isPending}
            onDirty={() => {
              setDirty(true);
              setSaveOk(false);
            }}
            onSave={(edited) => saveM.mutate({ versionId: openGroup.id, edited })}
            onRequestRewrite={() =>
              openGroup.published_tour_id &&
              rewriteM.mutate({
                publishedTourId: openGroup.published_tour_id,
                lang: openGroup.rewrite_language || "en-US",
                title: openGroup.aa_name || t("tour"),
              })
            }
            onExportDocx={(id) => exportDocx(id)}
            onClose={() => setOpenVersionId(null)}
          />
        )}
      </Drawer>

      <TourRewritePanel
        target={rewrite}
        version={rewriteVersion}
        retries={retries}
        retrying={retryM.isPending}
        exporting={!!rewrite && exportingDocxId === rewrite.versionId}
        onClose={(running) => {
          if (rewrite && running) watch(watchTourRewrite(rewrite, lang));
          setRewrite(null);
        }}
        onRetry={() => rewrite && retryM.mutate(rewrite.versionId)}
        onOpenResult={() => {
          if (!rewrite) return;
          setOpenVersionId(rewrite.versionId);
          setRewrite(null);
        }}
        onExportDocx={() => rewrite && exportDocx(rewrite.versionId)}
      />

      {/* StatusBadge's "running" variant uses animation: spin — define the keyframe once. */}
      <style>{`@keyframes spin{to{transform:rotate(360deg)}}`}</style>
    </>
  );
}

export default function CatalogTab() {
  return (
    <ToastProvider>
      <CatalogInner />
    </ToastProvider>
  );
}
