"use client";
// app/(tenant)/portal/_components/CatalogTab.tsx
// API: GET  /api/tenant/v1/tours/my-versions?page_size=50&status=X
//      GET  /api/tenant/v1/tours/versions/{id}
//      PATCH /api/tenant/v1/tours/versions/{id}

import { useState, useEffect, useCallback, useRef } from "react";
import { Package, ChevronRight, Save, CheckCircle, XCircle, RotateCcw, Clock, X } from "lucide-react";
import {
  T, serif, mono, sans,
  Card, CardHead, Badge, ScoreBadge, Btn, LoadingScreen, EmptyState,
  parseHighlights, parseContent, fmtDate, fmtDateTime, statusVariant,
} from "./ui";

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
  // Original AA tour data for the "AA Original" diff column
  const [origTour, setOrigTour]     = useState<any>(null);

  // Refs for polling — pollingRef holds the interval ID so we never double-start
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const listRef    = useRef<Version[]>([]);

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

  // Keep listRef current for polling comparisons (avoids stale closure)
  useEffect(() => { listRef.current = list; }, [list]);

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
          if (justDone.length > 0) {
            const name = justDone[0].aa_name || 'Tour';
            setLocalToast(`✅ "${name}" — rewrite complete. Click to review.`);
            setTimeout(() => setLocalToast(null), 5000);
          }
          return;
        }

        // Still pending — update list so in-progress badges stay current
        setList(fresh);
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
    setDirty(false); setSaveOk(false); setExpandItin(false);
    try {
      // Fetch version detail + original pool tour in parallel
      const [rDetail, rOrig] = await Promise.all([
        fetch(`/api/tenant/v1/tours/versions/${v.id}`),
        v.published_tour_id
          ? fetch(`/api/tenant/v1/tours/pool/${v.published_tour_id}`)
          : Promise.resolve(null),
      ]);
      if (rDetail.ok) {
        const d: Version = await rDetail.json();
        setDetail(d);
        // Use pool tour for AA Original if available; fallback to version JOIN data
        const orig = rOrig?.ok ? await rOrig.json() : d;
        setOrigTour(orig);
        const rc = parseContent(d.rewritten_content) as Record<string, unknown> | null;
        setEditName((rc?.name ?? d.aa_name ?? "") as string);
        setEditSubtitle((rc?.subtitle ?? d.aa_subtitle ?? "") as string);
        setEditSummary((rc?.summary ?? d.aa_summary ?? "") as string);
        setEditHighlights(Array.isArray(rc?.highlights) ? rc.highlights as string[] : parseHighlights(d.aa_highlights));
        setEditSeoTitle((rc?.seo_title ?? d.aa_seo_title ?? "") as string);
        setEditSeoMeta((rc?.seo_meta ?? d.aa_seo_meta ?? "") as string);
      }
    } finally { setDlLoad(false); }
  }

  async function doAction(action: "approve" | "reject") {
    if (!selected) return;
    setActing(true);
    try {
      const r = await fetch(`/api/tenant/v1/tours/versions/${selected.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action }),
      });
      if (r.ok) { await fetchList(); if (detail) setDetail(await r.json()); }
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

  const seoTitleLen = editSeoTitle.length;
  const seoMetaLen  = editSeoMeta.length;
  const seoTitleOk  = seoTitleLen > 0 && seoTitleLen <= 60;
  const seoMetaOk   = seoMetaLen >= 80 && seoMetaLen <= 160;

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
        {/* Filter pills */}
        <div style={{ display: "flex", gap: 6, marginBottom: 14, flexWrap: "wrap" }}>
          {STATUS_FILTERS.map(s => (
            <button key={s} onClick={() => setFilter(s)} style={{
              padding: "5px 14px", borderRadius: 20, fontSize: 11.5, fontWeight: 600,
              border: `1px solid ${filter === s ? T.gold : T.line}`,
              background: filter === s ? T.goldTint : T.card,
              color: filter === s ? T.amber : T.muted,
              cursor: "pointer", fontFamily: sans,
            }}>{s || "All"}</button>
          ))}
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
                <button key={v.id} onClick={() => loadDetail(v)} style={{
                  background: isActive ? "rgba(219,150,40,0.04)" : T.card,
                  border: `1px solid ${isActive ? "rgba(219,150,40,0.35)" : T.line}`,
                  borderRadius: 10, padding: "11px 14px", cursor: "pointer",
                  textAlign: "left", fontFamily: sans, transition: "all .15s",
                }}>
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
                {selected.quality_score != null && <ScoreBadge score={selected.quality_score} />}
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

              {/* Before / After comparison */}
              <div style={{ padding: "18px 22px", borderBottom: `1px solid ${T.line}` }}>
                <div style={{ fontSize: 10.5, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.12em", color: T.muted, marginBottom: 14 }}>
                  Content Comparison — Before / After
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
              </div>

              {/* SEO health */}
              <div style={{ padding: "14px 22px", borderBottom: `1px solid ${T.line}` }}>
                <div style={{ fontSize: 10.5, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.12em", color: T.muted, marginBottom: 10 }}>SEO Health</div>
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                  {[
                    { l: `Title ≤60 (${seoTitleLen})`, ok: seoTitleOk },
                    { l: `Meta 80-160 (${seoMetaLen})`, ok: seoMetaOk },
                    { l: `Highlights ≥3 (${editHighlights.length})`, ok: editHighlights.length >= 3 },
                    { l: "Summary filled", ok: editSummary.length > 50 },
                  ].map(c => (
                    <span key={c.l} style={{ fontSize: 11, padding: "3px 9px", borderRadius: 20, fontWeight: 600, background: c.ok ? T.greenSoft : T.redSoft, color: c.ok ? T.green : T.red }}>
                      {c.ok ? "✓" : "✗"} {c.l}
                    </span>
                  ))}
                </div>
              </div>

              {/* Version history */}
              {detail.version_history && detail.version_history.length > 0 && (
                <div style={{ padding: "14px 22px", borderBottom: `1px solid ${T.line}` }}>
                  <div style={{ fontSize: 10.5, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.12em", color: T.muted, marginBottom: 10 }}>Version History</div>
                  {detail.version_history.map(h => (
                    <div key={h.id} style={{ display: "flex", alignItems: "center", gap: 10, padding: "7px 0", borderBottom: `1px solid ${T.line2}`, fontSize: 12 }}>
                      <span style={{ color: T.gold, fontWeight: 700, fontFamily: mono, minWidth: 24 }}>v{h.version_number}</span>
                      <span style={{ color: T.muted, flex: 1 }}>{h.edit_source === "ai_generated" ? "AI Generated" : "Your Edit"}</span>
                      {h.quality_score != null && <ScoreBadge score={h.quality_score} />}
                      <Badge variant={statusVariant(h.status)}>{h.status}</Badge>
                      <span style={{ color: T.muted2, fontFamily: mono, fontSize: 11 }}>{fmtDate(h.created_at)}</span>
                    </div>
                  ))}
                </div>
              )}

              {/* Actions */}
              <div style={{ padding: "14px 22px", display: "flex", gap: 10, justifyContent: "flex-end", alignItems: "center" }}>
                {(selected.status === "approved" || selected.status === "rejected") ? (
                  // Terminal state — no further actions allowed
                  <span style={{
                    fontSize: 13, fontWeight: 600,
                    color: selected.status === "approved" ? T.green : T.red,
                  }}>
                    {selected.status === "approved" ? "✅ Approved" : "❌ Rejected — cannot undo"}
                  </span>
                ) : (
                  <>
                    <Btn variant="danger" disabled={acting} onClick={() => doAction("reject")}>
                      <XCircle size={14} /> Reject
                    </Btn>
                    {dirty && (
                      <Btn variant="secondary" disabled={saving} onClick={saveEdit}>
                        <Save size={14} /> {saving ? "Saving…" : "Save as New Version"}
                      </Btn>
                    )}
                    <Btn variant="primary" disabled={acting} onClick={() => doAction("approve")}>
                      <CheckCircle size={14} /> Approve
                    </Btn>
                  </>
                )}
              </div>
            </div>
          ) : null}
        </div>
      )}
    </div>
    </>
  );
}

// ── Status badge — maps DB status to display text + colour + spinner ──────────

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, { label: string; bg: string; color: string; spin?: boolean }> = {
    pending:      { label: "Processing...", bg: "#FEF9C3",  color: "#B45309", spin: true },
    ai_generated: { label: "Ready to review", bg: "#EFF6FF", color: "#2563EB" },
    approved:     { label: "Approved",       bg: "#DCFCE7", color: "#16A34A" },
    rejected:     { label: "Rejected",       bg: "#FEE2E2", color: "#DC2626" },
    needs_review: { label: "Needs review",   bg: "#FEF3C7", color: "#D97706" },
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

function CompareRow({ label, original, yours, onEdit }: {
  label: string; original: string; yours: string; onEdit: (v: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [val, setVal] = useState(yours);
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: T.muted, marginBottom: 6 }}>{label}</div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
        <ColBox color="red" label="AA Original">{original || "—"}</ColBox>
        <ColBox color="green" label="Your Version" editable onEdit={() => { setEditing(true); setVal(yours); }}>
          {editing ? (
            <div>
              <textarea value={val} onChange={e => setVal(e.target.value)} rows={3}
                style={{ width: "100%", fontSize: 11, border: `1px solid ${T.gold}`, borderRadius: 4, padding: 6, resize: "vertical", fontFamily: sans, outline: "none", boxSizing: "border-box" }} />
              <div style={{ display: "flex", gap: 6, marginTop: 4 }}>
                <Btn size="sm" variant="primary" onClick={() => { onEdit(val); setEditing(false); }}>Save</Btn>
                <Btn size="sm" variant="ghost"   onClick={() => setEditing(false)}>Cancel</Btn>
              </div>
            </div>
          ) : yours || "—"}
        </ColBox>
      </div>
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

function HighlightsCompare({ origRaw, yours, onChange }: {
  origRaw: string; yours: string[]; onChange: (v: string[]) => void;
}) {
  const orig = parseHighlights(origRaw);
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: T.muted, marginBottom: 6 }}>Highlights</div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
        <ColBox color="red" label="AA Original">
          {orig.map((h, i) => <div key={i} style={{ fontSize: 11.5, color: T.body, marginBottom: 3 }}>• {h}</div>)}
        </ColBox>
        <ColBox color="green" label="Your Version" editable={false}>
          {yours.map((h, i) => (
            <div key={i} style={{ display: "flex", gap: 4, marginBottom: 4, alignItems: "flex-start" }}>
              <span style={{ color: T.gold, fontWeight: 700, flexShrink: 0 }}>•</span>
              <input value={h} onChange={e => { const n = [...yours]; n[i] = e.target.value; onChange(n); }}
                style={{ flex: 1, fontSize: 11, border: `1px solid ${T.line}`, borderRadius: 4, padding: "2px 6px", fontFamily: sans, outline: "none", background: "transparent" }} />
              <button onClick={() => onChange(yours.filter((_, j) => j !== i))} style={{ background: "none", border: "none", cursor: "pointer", color: T.muted2, padding: 0, flexShrink: 0 }}>×</button>
            </div>
          ))}
          <button onClick={() => onChange([...yours, ""])} style={{ fontSize: 11, color: T.gold, background: "none", border: "none", cursor: "pointer", fontFamily: sans }}>+ Add</button>
        </ColBox>
      </div>
    </div>
  );
}

function ItineraryCompare({ orig, yours, expand, setExpand }: {
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
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
        <ColBox color="red" label="AA Original">
          <div style={{ fontSize: 11.5, color: T.body, lineHeight: 1.6, maxHeight: expand ? "none" : 100, overflow: expand ? "visible" : "hidden", whiteSpace: "pre-wrap" }}>{orig || "—"}</div>
        </ColBox>
        <ColBox color="green" label="Your Version">
          <div style={{ fontSize: 11.5, color: T.body, lineHeight: 1.6, maxHeight: expand ? "none" : 100, overflow: expand ? "visible" : "hidden", whiteSpace: "pre-wrap" }}>{yours || "—"}</div>
        </ColBox>
      </div>
    </div>
  );
}
