"use client";
// app/(tenant)/portal/_components/CatalogTab.tsx
// API: GET  /api/tenant/v1/tours/my-versions?page_size=50&status=X
//      GET  /api/tenant/v1/tours/versions/{id}
//      PATCH /api/tenant/v1/tours/versions/{id}

import { useState, useEffect, useCallback, useRef } from "react";
import * as XLSX from "xlsx";
import { Package, ChevronRight, Save, CheckCircle, XCircle, RotateCcw, Clock, X } from "lucide-react";
import {
  T, serif, mono, sans,
  Card, CardHead, Badge, ScoreBadge, Btn, LoadingScreen, EmptyState,
  parseHighlights, parseContent, fmtDate, fmtDateTime, statusVariant,
} from "./ui";
import { SeoHealthBar } from "./SeoHealthBar";
import { SeeOriginalToggle } from "./SeeOriginalToggle";
import { NextStepGuide } from "./NextStepGuide";
import { VersionHistory } from "./VersionHistory";

interface Version {
  id: string; version_number: number; status: string;
  quality_score: number | null; edit_source: string;
  rewrite_language: string; created_at: string; edited_at: string | null;
  rewritten_content: string; seo_mode: string; aa_name: string;
  aa_subtitle: string; aa_summary: string; aa_highlights: string;
  aa_itineraries: string | null; aa_seo_title: string; aa_seo_meta: string;
  aa_quality_score: number; country: string | null; duration: string | null;
  published_tour_id?: string;
  version_history?: { id: string; version_number: number; status: string; edit_source: string; quality_score: number | null; created_at: string }[];
}

const STATUS_FILTERS = ["", "pending", "approved", "rejected"];
const FILTER_LABELS: Record<string, string> = {
  "": "All", pending: "Queued", approved: "In Catalog", rejected: "New Version Requested",
};

export default function CatalogTab() {
  const [list, setList]         = useState<Version[]>([]);
  const [loading, setLoading]   = useState(true);
  const [filter, setFilter]     = useState("");
  const [selected, setSelected] = useState<Version | null>(null);
  const [detail, setDetail]     = useState<Version | null>(null);
  const [dlLoad, setDlLoad]     = useState(false);
  const [acting, setActing]     = useState(false);
  const [saving, setSaving]     = useState(false);
  const [saveOk, setSaveOk]     = useState(false);
  const [dirty, setDirty]       = useState(false);
  const [expandItin, setExpandItin] = useState(false);
  const [localToast, setLocalToast] = useState<string | null>(null);
  const [actionState, setActionState] = useState<'approved' | 'rejected' | null>(null);
  const [showQuotaConfirm, setShowQuotaConfirm] = useState(false);
  const [quota, setQuota] = useState<{ rewrites_remaining: number } | null>(null);
  // Original AA tour data for the "AA Original" diff column
  const [origTour, setOrigTour]     = useState<any>(null);

  // AA-28 multi-select export
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [isExporting, setIsExporting] = useState(false);

  // Refs for polling — pollingRef holds the interval ID so we never double-start
  const pollingRef  = useRef<ReturnType<typeof setInterval> | null>(null);
  const listRef     = useRef<Version[]>([]);
  const selectedRef = useRef<Version | null>(null);

  // Edit state
  const [editName, setEditName]       = useState("");
  const [editSubtitle, setEditSubtitle] = useState("");
  const [editSummary, setEditSummary] = useState("");
  const [editHighlights, setEditHighlights] = useState<string[]>([]);
  const [editSeoTitle, setEditSeoTitle] = useState("");
  const [editSeoMeta, setEditSeoMeta] = useState("");

  const fetchList = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams({ page_size: "50" });
      if (filter) params.set("status", filter);
      const r = await fetch(`/api/tenant/v1/tours/my-versions?${params}`);
      if (r.ok) { const d = await r.json(); setList(d.data ?? []); }
    } finally { setLoading(false); }
  }, [filter]);

  useEffect(() => { fetchList(); }, [fetchList]);

  useEffect(() => {
    fetch('/api/tenant/v1/quota')
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (d) setQuota(d); })
      .catch(() => {});
  }, []);

  // Keep refs current for polling comparisons (avoids stale closure)
  useEffect(() => { listRef.current = list; }, [list]);
  useEffect(() => { selectedRef.current = selected; }, [selected]);

  // Polling: start when pending items are visible, stop on completion or unmount
  useEffect(() => {
    const hasPending = list.some(v => v.status === 'pending');

    if (!hasPending) {
      if (pollingRef.current) { clearInterval(pollingRef.current); pollingRef.current = null; }
      return;
    }
    if (pollingRef.current) return; // already running

    const startTime = Date.now();
    pollingRef.current = setInterval(async () => {
      if (Date.now() - startTime > 300_000) {
        if (pollingRef.current) { clearInterval(pollingRef.current); pollingRef.current = null; }
        return;
      }
      try {
        const r = await fetch('/api/tenant/v1/tours/my-versions?page_size=50');
        if (!r.ok) return;
        const fresh: Version[] = (await r.json()).data ?? [];

        // KEY FIX: stop as soon as NO items are pending — catches both
        // "just completed" and "manually approved/rejected" cases
        const stillPending = fresh.some(v => v.status === 'pending');
        if (!stillPending) {
          if (pollingRef.current) { clearInterval(pollingRef.current); pollingRef.current = null; }
          // Which items just transitioned from pending in our snapshot?
          const justDone = fresh.filter(v =>
            listRef.current.find(o => o.id === v.id)?.status === 'pending'
          );
          setList(fresh); // update all badges at once with fresh data
          // Sync selected if its status changed (stops header spinner immediately)
          const sel0 = selectedRef.current;
          if (sel0) { const upd0 = fresh.find(v => v.id === sel0.id); if (upd0 && upd0.status !== sel0.status) setSelected(upd0); }
          if (justDone.length > 0) {
            const name = justDone[0].aa_name || 'Tour';
            setLocalToast(`✅ "${name}" — rewrite complete. Click to review.`);
            setTimeout(() => setLocalToast(null), 5000);
          }
          return;
        }

        // Still pending — update list so in-progress badges stay current
        setList(fresh);
        // Sync selected if its status changed (e.g. pending → ai_generated)
        const sel = selectedRef.current;
        if (sel) { const upd = fresh.find(v => v.id === sel.id); if (upd && upd.status !== sel.status) setSelected(upd); }
      } catch {}
    }, 5000);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [list]);

  // Cleanup polling on unmount
  useEffect(() => () => {
    if (pollingRef.current) { clearInterval(pollingRef.current); pollingRef.current = null; }
  }, []);

  async function loadDetail(v: Version) {
    setSelected(v); setDlLoad(true); setDetail(null); setOrigTour(null);
    setDirty(false); setSaveOk(false); setExpandItin(true); setActionState(null);
    try {
      // allSettled: pool fetch failure (network error or 4xx) must not kill the detail fetch
      const [detailResult, origResult] = await Promise.allSettled([
        fetch(`/api/tenant/v1/tours/versions/${v.id}`),
        v.published_tour_id
          ? fetch(`/api/tenant/v1/tours/pool/${v.published_tour_id}`)
          : Promise.reject(new Error('no published_tour_id')),
      ]);

      if (detailResult.status === 'rejected' || !detailResult.value.ok) return;
      const d: Version = await detailResult.value.json();
      setDetail(d);

      // Pool tour preferred; fallback maps aa_* field names to pool-compatible shape
      let orig: Record<string, unknown> | null = null;
      if (origResult.status === 'fulfilled' && origResult.value.ok) {
        orig = await origResult.value.json();
      }
      if (!orig) {
        orig = {
          aa_summary:     d.aa_summary     ?? null,
          seo_title:      d.aa_seo_title   ?? null,  // pool uses seo_title, not aa_seo_title
          seo_meta:       d.aa_seo_meta    ?? null,
          aa_highlights:  d.aa_highlights  ?? null,
          aa_itineraries: d.aa_itineraries ?? null,
        };
      }
      setOrigTour(orig);

      const rc = parseContent(d.rewritten_content) as Record<string, unknown> | null;
      setEditName((rc?.name ?? d.aa_name ?? "") as string);
      setEditSubtitle((rc?.subtitle ?? d.aa_subtitle ?? "") as string);
      setEditSummary((rc?.summary ?? d.aa_summary ?? "") as string);
      setEditHighlights(Array.isArray(rc?.highlights) ? rc.highlights as string[] : parseHighlights(d.aa_highlights));
      setEditSeoTitle((rc?.seo_title ?? d.aa_seo_title ?? "") as string);
      setEditSeoMeta((rc?.seo_meta ?? d.aa_seo_meta ?? "") as string);
    } finally { setDlLoad(false); }
  }

  async function doAction(action: "approve" | "reject") {
    if (!selected) return;
    setActing(true);
    const nextState = action === "approve" ? "approved" as const : "rejected" as const;
    setActionState(nextState); // hide buttons immediately before API responds
    try {
      const r = await fetch(`/api/tenant/v1/tours/versions/${selected.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action }),
      });
      if (r.ok) { const upd: Version = await r.json(); await fetchList(); setDetail(upd); setSelected(upd); }
      else { setActionState(null); } // revert optimistic update on failure
    } finally { setActing(false); }
  }

  async function saveEdit() {
    if (!selected || !dirty) return;
    setSaving(true); setSaveOk(false);
    try {
      const r = await fetch(`/api/tenant/v1/tours/versions/${selected.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action: "edit",
          edited_content: { name: editName, subtitle: editSubtitle, summary: editSummary, highlights: editHighlights, seo_title: editSeoTitle, seo_meta: editSeoMeta },
          edited_by: "tenant",
        }),
      });
      if (r.ok) { setSaveOk(true); setDirty(false); await fetchList(); }
    } finally { setSaving(false); }
  }

  function flattenVal(v: any): string {
    if (v == null || v === "") return "";
    if (typeof v === "string") {
      try { return flattenVal(JSON.parse(v)); } catch {}
      return v.replace(/[\t\n\r]+/g, " ").trim();
    }
    if (Array.isArray(v)) {
      return v.map((x: any) => {
        if (x == null) return "";
        if (typeof x === "string") return x.replace(/[\t\n\r]+/g, " ").trim();
        if (typeof x === "object") return String(x.keyword ?? x.text ?? x.description ?? x.title ?? x.value ?? JSON.stringify(x));
        return String(x);
      }).filter(Boolean).join(" | ");
    }
    if (typeof v === "object") {
      return Object.values(v).filter(x => x != null && x !== "").map(x => String(x)).join(" | ");
    }
    return String(v);
  }

  async function exportSelected(fmt: "csv" | "xls") {
    const selectedVersions = list.filter(v => selectedIds.has(v.id));
    if (selectedVersions.length === 0) return;
    setIsExporting(true);
    try {
      const fullData = await Promise.all(
        selectedVersions.map(async v => {
          try {
            const r = await fetch(`/api/tenant/v1/tours/versions/${v.id}`);
            return r.ok ? await r.json() : null;
          } catch { return null; }
        })
      );

      const headers = [
        "Name", "Subtitle", "Country", "Duration",
        "SEO Title", "SEO Meta", "Summary", "Highlights", "Itineraries",
        "Quality Score", "Created At", "Version", "Status", "Language",
      ];

      const rows: Record<string, string>[] = fullData.map((d, i) => {
        const v  = selectedVersions[i];
        const rc = parseContent(d?.rewritten_content ?? v.rewritten_content) as Record<string, unknown> | null;
        return {
          "Name":          String(rc?.name        ?? d?.aa_name      ?? v.aa_name      ?? ""),
          "Subtitle":      String(rc?.subtitle    ?? d?.aa_subtitle  ?? ""),
          "Country":       String(d?.country      ?? v.country       ?? ""),
          "Duration":      String(d?.duration     ?? v.duration      ?? ""),
          "SEO Title":     String(rc?.seo_title   ?? d?.aa_seo_title ?? ""),
          "SEO Meta":      String(rc?.seo_meta    ?? d?.aa_seo_meta  ?? ""),
          "Summary":       String(rc?.summary     ?? d?.aa_summary   ?? ""),
          "Highlights":    flattenVal(rc?.highlights    ?? d?.aa_highlights    ?? ""),
          "Itineraries":   flattenVal(rc?.itineraries   ?? d?.aa_itineraries   ?? ""),
          "Quality Score": v.quality_score != null ? String(v.quality_score) : "",
          "Created At":    v.created_at ? new Date(v.created_at).toLocaleDateString("en-GB") : "",
          "Version":       String(v.version_number),
          "Status":        v.status,
          "Language":      v.rewrite_language,
        };
      });

      if (fmt === "csv") {
        const san = (s: string) => s.replace(/[\t\n\r]+/g, " ");
        const tsv = [headers.join("\t"), ...rows.map(r => headers.map(h => san(r[h] ?? "")).join("\t"))].join("\n");
        const blob = new Blob(["﻿" + tsv], { type: "text/csv;charset=utf-8" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a"); a.href = url; a.download = "my-catalog.csv"; a.click(); URL.revokeObjectURL(url);
      } else {
        const ws = XLSX.utils.json_to_sheet(rows, { header: headers });
        ws["!cols"] = [30,30,15,12,40,50,60,60,80,12,15,10,15,15].map(w => ({ wch: w }));
        const wb = XLSX.utils.book_new();
        XLSX.utils.book_append_sheet(wb, ws, "My Catalog");
        XLSX.writeFile(wb, "my-catalog.xlsx");
      }
    } finally { setIsExporting(false); }
  }

  const allSelected = list.length > 0 && list.every(v => selectedIds.has(v.id));

  return (
    <>
    <style>{`@keyframes cis-spin { to { transform: rotate(360deg); } }`}</style>

    {/* Local toast — bottom-right, auto-dismiss after 5s */}
    {localToast && (
      <div style={{
        position: "fixed", bottom: 24, right: 28, zIndex: 9999,
        padding: "12px 20px", background: "#16A34A", borderRadius: 10,
        color: "#fff", fontSize: 13, fontWeight: 600,
        boxShadow: "0 4px 20px rgba(0,0,0,0.2)", maxWidth: 380,
      }}>
        {localToast}
      </div>
    )}

    <div style={{ display: "grid", gridTemplateColumns: selected ? "300px 1fr" : "1fr", gap: 20, alignItems: "start" }}>

      {/* LEFT — version list */}
      <div>
        {/* Filter pills + select-all + export */}
        <div style={{ display: "flex", gap: 6, marginBottom: 14, flexWrap: "wrap", alignItems: "center" }}>
          {STATUS_FILTERS.map(s => (
            <button key={s} onClick={() => setFilter(s)} style={{
              padding: "5px 14px", borderRadius: 20, fontSize: 11.5, fontWeight: 600,
              border: `1px solid ${filter === s ? T.gold : T.line}`,
              background: filter === s ? T.goldTint : T.card,
              color: filter === s ? T.amber : T.muted,
              cursor: "pointer", fontFamily: sans,
            }}>{FILTER_LABELS[s] ?? s}</button>
          ))}
          {list.length > 0 && (
            <label style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 11.5, color: T.muted, cursor: "pointer", fontFamily: sans, marginLeft: 4 }}>
              <input type="checkbox" checked={allSelected}
                onChange={() => {
                  if (allSelected) setSelectedIds(new Set());
                  else setSelectedIds(new Set(list.map(v => v.id)));
                }}
                style={{ cursor: "pointer", accentColor: T.gold }} />
              Select all
            </label>
          )}
          {selectedIds.size > 0 && (
            <div style={{ display: "flex", gap: 6, marginLeft: "auto" }}>
              <button onClick={() => exportSelected("csv")} disabled={isExporting}
                style={{ padding: "5px 12px", borderRadius: 20, fontSize: 11.5, fontWeight: 600, border: "none", background: isExporting ? "#86EFAC" : "#22C55E", color: "#fff", cursor: isExporting ? "default" : "pointer", fontFamily: sans }}>
                {isExporting ? "Fetching…" : `↓ CSV (${selectedIds.size})`}
              </button>
              <button onClick={() => exportSelected("xls")} disabled={isExporting}
                style={{ padding: "5px 12px", borderRadius: 20, fontSize: 11.5, fontWeight: 600, border: "none", background: T.gold, color: "#fff", cursor: isExporting ? "default" : "pointer", fontFamily: sans, opacity: isExporting ? 0.6 : 1 }}>
                {isExporting ? "Fetching…" : `↓ XLSX (${selectedIds.size})`}
              </button>
            </div>
          )}
        </div>

        {loading ? <LoadingScreen message="Loading catalog…" /> :
         list.length === 0 ? (
           <EmptyState icon={<Package size={36} />} title="No rewrites yet" sub="Browse the pool and rewrite your first tour" />
         ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {list.map(v => {
              const rc = parseContent(v.rewritten_content) as Record<string, unknown> | null;
              const isActive = selected?.id === v.id;
              return (
                <div key={v.id} style={{
                  background: isActive ? "rgba(219,150,40,0.04)" : T.card,
                  border: `1px solid ${isActive ? "rgba(219,150,40,0.35)" : T.line}`,
                  borderRadius: 10, padding: "11px 14px",
                  fontFamily: sans, transition: "all .15s",
                }}>
                  <div style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
                    <label onClick={e => e.stopPropagation()} style={{ display: "flex", alignItems: "center", paddingTop: 3, flexShrink: 0, cursor: "pointer" }}>
                      <input type="checkbox" checked={selectedIds.has(v.id)}
                        onChange={() => setSelectedIds(prev => { const n = new Set(prev); selectedIds.has(v.id) ? n.delete(v.id) : n.add(v.id); return n; })}
                        style={{ cursor: "pointer", accentColor: T.gold }} />
                    </label>
                    <button onClick={() => loadDetail(v)} style={{ flex: 1, background: "none", border: "none", cursor: "pointer", textAlign: "left", padding: 0, fontFamily: sans }}>
                      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 8 }}>
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div style={{ fontSize: 13, fontWeight: 600, color: T.ink, marginBottom: 2, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                            {v.aa_name || (rc?.name as string) || "Tour"}
                          </div>
                          <div style={{ fontSize: 11, color: T.muted2, display: "flex", gap: 8, fontFamily: mono }}>
                            <span>v{v.version_number}</span>
                            <span>{v.rewrite_language}</span>
                            {v.duration && <span>{v.duration}</span>}
                          </div>
                          {rc?.summary != null && (
                            <div style={{ fontSize: 11.5, color: T.muted, marginTop: 5, lineHeight: 1.45, overflow: "hidden", display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical" }}>
                              {String(rc.summary)}
                            </div>
                          )}
                        </div>
                        <StatusBadge status={v.status} />
                      </div>
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* RIGHT — editorial workspace */}
      {selected && (
        <div style={{ background: T.card, border: `1px solid ${T.line}`, borderRadius: 12, overflow: "hidden" }}>
          {/* Header */}
          <div style={{ padding: "14px 22px", borderBottom: `1px solid ${T.line}`, background: T.bg, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <div>
              <div style={{ fontSize: 15, fontWeight: 700, color: T.ink }}>{selected.aa_name}</div>
              <div style={{ fontSize: 11.5, color: T.muted, marginTop: 3, display: "flex", gap: 10, fontFamily: mono }}>
                <span>v{selected.version_number}</span>
                <span>{selected.rewrite_language}</span>
              </div>
            </div>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              {dirty && (
                <Btn variant="primary" size="sm" disabled={saving} onClick={saveEdit}>
                  <Save size={12} /> {saving ? "Saving…" : "Save"}
                </Btn>
              )}
              {saveOk && !dirty && <span style={{ fontSize: 12, color: T.green, fontWeight: 600 }}>✓ Saved</span>}
              <StatusBadge status={selected.status} />
              <button onClick={() => { setSelected(null); setDetail(null); }}
                style={{ background: "none", border: "none", cursor: "pointer", color: T.muted2 }}>
                <X size={16} />
              </button>
            </div>
          </div>

          {dlLoad ? <LoadingScreen message="Loading editorial workspace…" /> :
           detail ? (
            <div style={{ maxHeight: "78vh", overflowY: "auto" }}>

              {/* Guided next step */}
              <div style={{ padding: "14px 22px 0" }}>
                <NextStepGuide status={actionState ?? selected.status} />
              </div>

              {/* Your Version */}
              <div style={{ padding: "18px 22px", borderBottom: `1px solid ${T.line}` }}>
                <div style={{ fontSize: 10.5, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.12em", color: T.muted, marginBottom: 14 }}>
                  Your Version
                </div>
                <div style={{ display: "flex", gap: 8, marginBottom: 12, fontSize: 11, color: T.muted2, fontFamily: mono }}>
                  <span>Created: {fmtDateTime(detail.created_at)}</span>
                  {detail.edited_at && <span>· Edited: {fmtDateTime(detail.edited_at)}</span>}
                </div>
                {[
                  { label: "Summary",   orig: origTour?.aa_summary  ?? detail.aa_summary,   yours: editSummary,  set: (v: string) => { setEditSummary(v); setDirty(true); } },
                  { label: "SEO Title", orig: origTour?.seo_title   ?? detail.aa_seo_title, yours: editSeoTitle, set: (v: string) => { setEditSeoTitle(v); setDirty(true); } },
                  { label: "SEO Meta",  orig: origTour?.seo_meta    ?? detail.aa_seo_meta,  yours: editSeoMeta,  set: (v: string) => { setEditSeoMeta(v); setDirty(true); } },
                ].map(row => (
                  <CompareRow key={row.label} label={row.label} original={row.orig} yours={row.yours} onEdit={row.set} />
                ))}
                <HighlightsCompare
                  origRaw={origTour?.aa_highlights ?? detail.aa_highlights}
                  yours={editHighlights}
                  onChange={h => { setEditHighlights(h); setDirty(true); }}
                />
                {((origTour?.aa_itineraries ?? detail.aa_itineraries) || Boolean(parseContent(detail.rewritten_content)?.itineraries)) && (
                  <ItineraryCompare
                    orig={origTour?.aa_itineraries ?? detail.aa_itineraries ?? ""}
                    yours={String(parseContent(detail.rewritten_content)?.itineraries ?? "")}
                    expand={expandItin}
                    setExpand={setExpandItin}
                  />
                )}
                <SeeOriginalToggle
                  summary={String(origTour?.aa_summary ?? detail.aa_summary ?? "")}
                  seoTitle={String(origTour?.seo_title ?? detail.aa_seo_title ?? "")}
                  seoMeta={String(origTour?.seo_meta ?? detail.aa_seo_meta ?? "")}
                  highlightsRaw={String(origTour?.aa_highlights ?? detail.aa_highlights ?? "")}
                  itineraries={String(origTour?.aa_itineraries ?? detail.aa_itineraries ?? "") || null}
                />
              </div>

              {/* SEO health */}
              <div style={{ padding: "14px 22px", borderBottom: `1px solid ${T.line}` }}>
                <SeoHealthBar
                  seoTitle={editSeoTitle}
                  seoMeta={editSeoMeta}
                  highlights={editHighlights}
                  summary={editSummary}
                  rulesApplied={(detail as any).rules_applied}
                />
              </div>

              {/* Version history */}
              {detail.version_history && (
                <VersionHistory
                  versions={detail.version_history}
                  activeVersionId={selected.id}
                  onSelect={h => loadDetail({ ...selected, ...h } as Version)}
                />
              )}

              {/* Actions */}
              <div style={{ padding: "14px 22px", display: "flex", gap: 10, justifyContent: "flex-end", alignItems: "center" }}>
                {(actionState || selected.status === "approved" || selected.status === "rejected") ? (
                  // Terminal state — actionState gives immediate feedback, selected.status is source of truth on load
                  <span style={{
                    fontSize: 13, fontWeight: 600,
                    color: (actionState ?? selected.status) === "approved" ? T.green : T.amber,
                  }}>
                    {(actionState ?? selected.status) === "approved"
                      ? "✅ Added to your catalog. Available via API."
                      : "🔄 New version requested."}
                  </span>
                ) : (
                  <>
                    <Btn variant="danger" disabled={acting} onClick={() => setShowQuotaConfirm(true)}>
                      <XCircle size={14} /> Request New Version
                    </Btn>
                    {dirty && (
                      <Btn variant="secondary" disabled={saving} onClick={saveEdit}>
                        <Save size={14} /> {saving ? "Saving…" : "Save as New Version"}
                      </Btn>
                    )}
                    <Btn variant="primary" disabled={acting} onClick={() => doAction("approve")}>
                      <CheckCircle size={14} /> Add to Catalog
                    </Btn>
                  </>
                )}
              </div>
            </div>
          ) : null}
        </div>
      )}
    </div>

    {/* Quota confirm dialog */}
    {showQuotaConfirm && (
      <div style={{
        position: "fixed", inset: 0, background: "rgba(0,0,0,0.2)",
        display: "flex", alignItems: "center", justifyContent: "center", zIndex: 9999,
      }}>
        <div style={{
          background: "#fff", borderRadius: 16, padding: "24px 28px",
          maxWidth: 380, width: "calc(100% - 32px)",
          boxShadow: "0 8px 32px rgba(0,0,0,0.15)", fontFamily: sans,
        }}>
          <h3 style={{ fontWeight: 700, color: T.ink, marginBottom: 8, fontSize: 15, margin: "0 0 8px" }}>
            Request new version?
          </h3>
          <p style={{ fontSize: 13, color: T.muted, margin: "0 0 6px" }}>
            This will use <strong>1 rewrite credit</strong>.
          </p>
          {quota && (
            <p style={{ fontSize: 13, color: T.muted2, margin: "0 0 20px" }}>
              You have{" "}
              <strong style={{ color: quota.rewrites_remaining < 5 ? T.red : T.ink }}>
                {quota.rewrites_remaining} credit{quota.rewrites_remaining !== 1 ? "s" : ""}
              </strong>{" "}
              remaining this month.
            </p>
          )}
          <div style={{ display: "flex", gap: 10, marginTop: quota ? 0 : 20 }}>
            <button
              onClick={() => setShowQuotaConfirm(false)}
              style={{ flex: 1, padding: "10px 0", borderRadius: 8, border: `1px solid ${T.line}`, background: T.card, fontSize: 13, color: T.muted, cursor: "pointer", fontFamily: sans }}
            >
              Cancel
            </button>
            <button
              onClick={async () => { setShowQuotaConfirm(false); await doAction("reject"); }}
              style={{ flex: 1, padding: "10px 0", borderRadius: 8, border: "none", background: T.gold, fontSize: 13, fontWeight: 600, color: T.ink, cursor: "pointer", fontFamily: sans }}
            >
              Confirm & Request
            </button>
          </div>
        </div>
      </div>
    )}
    </>
  );
}

// ── Status badge — maps DB status to display text + colour + spinner ──────────

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, { label: string; bg: string; color: string; spin?: boolean }> = {
    pending:       { label: "Queued",                bg: "#F3F4F6",  color: "#6B7280" },
    processing:    { label: "AI Writing…",           bg: "#EFF6FF",  color: "#2563EB", spin: true },
    ai_generating: { label: "AI Writing…",           bg: "#EFF6FF",  color: "#2563EB", spin: true },
    ai_generated:  { label: "Ready to Review",       bg: "#FEF3C7",  color: "#B45309" },
    approved:      { label: "In Catalog",            bg: "#DCFCE7",  color: "#16A34A" },
    rejected:      { label: "New Version Requested", bg: "#F3F4F6",  color: "#6B7280" },
    needs_review:  { label: "Needs Review",          bg: "#FEF3C7",  color: "#D97706" },
  };
  const m = map[status] ?? { label: status, bg: "#F3F4F6", color: "#6B7280" };
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 5,
      fontSize: 11, padding: "3px 9px", borderRadius: 20,
      fontWeight: 600, background: m.bg, color: m.color,
      whiteSpace: "nowrap",
    }}>
      {m.spin && (
        <span style={{
          display: "inline-block", width: 9, height: 9, flexShrink: 0,
          border: `1.5px solid ${m.color}`, borderTopColor: "transparent",
          borderRadius: "50%", animation: "cis-spin 0.8s linear infinite",
        }} />
      )}
      {m.label}
    </span>
  );
}

// ── Compare row ───────────────────────────────────────────────────────────────

function CompareRow({ label, original: _original, yours, onEdit }: {
  label: string; original: string; yours: string; onEdit: (v: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [val, setVal] = useState(yours);
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: T.muted, marginBottom: 6, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span>{label}</span>
        {!editing && (
          <button onClick={() => { setEditing(true); setVal(yours); }}
            style={{ background: "none", border: "none", cursor: "pointer", color: T.gold, fontSize: 10, fontFamily: sans }}>
            ✏ Edit
          </button>
        )}
      </div>
      {editing ? (
        <div>
          <textarea value={val} onChange={e => setVal(e.target.value)} rows={3}
            style={{ width: "100%", fontSize: 11.5, border: `1px solid ${T.gold}`, borderRadius: 6, padding: "8px 10px", resize: "vertical", fontFamily: sans, outline: "none", boxSizing: "border-box", color: T.body }} />
          <div style={{ display: "flex", gap: 6, marginTop: 4 }}>
            <Btn size="sm" variant="primary" onClick={() => { onEdit(val); setEditing(false); }}>Save</Btn>
            <Btn size="sm" variant="ghost"   onClick={() => setEditing(false)}>Cancel</Btn>
          </div>
        </div>
      ) : (
        <div style={{ fontSize: 11.5, color: T.body, lineHeight: 1.6, padding: "8px 10px", background: T.bg, border: `1px solid ${T.line}`, borderRadius: 6 }}>
          {yours || "—"}
        </div>
      )}
    </div>
  );
}

function ColBox({ color, label, children, editable = false, onEdit }: {
  color: "red" | "green"; label: string; children: React.ReactNode; editable?: boolean; onEdit?: () => void;
}) {
  const bg = color === "red" ? "rgba(239,68,68,0.04)" : "rgba(34,197,94,0.04)";
  const br = color === "red" ? "rgba(239,68,68,0.12)" : "rgba(34,197,94,0.12)";
  const lc = color === "red" ? "#f87171" : "#22c55e";
  return (
    <div style={{ background: bg, border: `1px solid ${br}`, borderRadius: 6, padding: "8px 10px" }}>
      <div style={{ fontSize: 9, fontWeight: 700, color: lc, marginBottom: 5, display: "flex", justifyContent: "space-between" }}>
        <span>{label.toUpperCase()}</span>
        {editable && <button onClick={onEdit} style={{ background: "none", border: "none", cursor: "pointer", color: T.gold, fontSize: 10, fontFamily: sans }}>✏ Edit</button>}
      </div>
      <div style={{ fontSize: 11.5, color: T.body, lineHeight: 1.6 }}>{children}</div>
    </div>
  );
}

function HighlightsCompare({ origRaw: _origRaw, yours, onChange }: {
  origRaw: string; yours: string[]; onChange: (v: string[]) => void;
}) {
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: T.muted, marginBottom: 6 }}>Highlights</div>
      <div style={{ padding: "8px 10px", background: T.bg, border: `1px solid ${T.line}`, borderRadius: 6 }}>
        {yours.map((h, i) => (
          <div key={i} style={{ display: "flex", gap: 4, marginBottom: 4, alignItems: "flex-start" }}>
            <span style={{ color: T.gold, fontWeight: 700, flexShrink: 0 }}>•</span>
            <input value={h} onChange={e => { const n = [...yours]; n[i] = e.target.value; onChange(n); }}
              style={{ flex: 1, fontSize: 11, border: `1px solid ${T.line}`, borderRadius: 4, padding: "2px 6px", fontFamily: sans, outline: "none", background: "transparent" }} />
            <button onClick={() => onChange(yours.filter((_, j) => j !== i))}
              style={{ background: "none", border: "none", cursor: "pointer", color: T.muted2, padding: 0, flexShrink: 0 }}>×</button>
          </div>
        ))}
        <button onClick={() => onChange([...yours, ""])}
          style={{ fontSize: 11, color: T.gold, background: "none", border: "none", cursor: "pointer", fontFamily: sans }}>+ Add</button>
      </div>
    </div>
  );
}

function ItineraryCompare({ orig: _orig, yours, expand, setExpand }: {
  orig: string; yours: string; expand: boolean; setExpand: (v: boolean) => void;
}) {
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
        <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: T.muted }}>Itinerary</div>
        <button onClick={() => setExpand(!expand)} style={{ fontSize: 11, color: T.gold, background: T.goldTint, border: `1px solid ${T.goldSoft}`, borderRadius: 4, padding: "2px 10px", cursor: "pointer", fontFamily: sans, fontWeight: 600 }}>
          {expand ? "▲ Collapse" : "▼ Expand"}
        </button>
      </div>
      <div style={{ fontSize: 11.5, color: T.body, lineHeight: 1.6, maxHeight: expand ? "none" : 100, overflow: expand ? "visible" : "hidden", whiteSpace: "pre-wrap", padding: "8px 10px", background: T.bg, border: `1px solid ${T.line}`, borderRadius: 6 }}>
        {yours || "—"}
      </div>
    </div>
  );
}
