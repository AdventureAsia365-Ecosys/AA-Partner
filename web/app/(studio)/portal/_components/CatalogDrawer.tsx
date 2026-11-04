"use client";
// web/app/(studio)/portal/_components/CatalogDrawer.tsx
// AA-662 PR 3 — the My Content detail/edit drawer, split out of CatalogTab. Uses the kit Drawer +
// react-query (version detail, which also carries the Master text) + mutations (save edit, request rewrite). The
// compare editors (summary / highlights / itinerary) and SeeOriginalToggle are unchanged.
// AA-668 (S239): the drawer shows a finished version only. "Request Rewrite" and a version that is
// still being written or failed open the Writer panel instead (TourRewritePanel.tsx): live
// progress, Retry, and the result — the inline LiveWriter / failed states that lived here moved.

import { useQuery } from "@tanstack/react-query";
import { Download, Save, X } from "lucide-react";
import { useMemo, useState } from "react";
import { parseContent, parseHighlights, fmtDateTime } from "@/lib/studio/format";
import { AiNotice, Button, IconButton, K, LinkButton, LoadingScreen, mono, sans, useLang, useT } from "@/app/_kit";
import { DRAWER_MESSAGES } from "./tours.messages";
import { MasterNewerNotice } from "./MasterNewerNotice";
import { SeeOriginalToggle } from "./SeeOriginalToggle";
import { fetchVersionDetail, type Version } from "./catalogApi";

function TripFact({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ marginBottom: 10 }}>
      <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: K.muted, marginBottom: 4 }}>
        {label}
      </div>
      <div style={{ fontSize: 11.5, color: K.body, lineHeight: 1.6, whiteSpace: "pre-wrap" }}>{value}</div>
    </div>
  );
}

function CompareRow({ label, yours, onEdit }: { label: string; yours: string; onEdit: (v: string) => void }) {
  const t = useT(DRAWER_MESSAGES);
  const [editing, setEditing] = useState(false);
  const [val, setVal] = useState(yours);
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: K.muted, marginBottom: 6, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span>{label}</span>
        {!editing && (
          <LinkButton
            size="sm"
            weight="regular"
            onClick={() => {
              setEditing(true);
              setVal(yours);
            }}
          >
            <span aria-hidden>✏</span> {t("edit")}
          </LinkButton>
        )}
      </div>
      {editing ? (
        <div>
          <textarea
            value={val}
            onChange={(e) => setVal(e.target.value)}
            rows={3}
            style={{ width: "100%", fontSize: 11.5, border: `1px solid ${K.accent}`, borderRadius: 6, padding: "8px 10px", resize: "vertical", fontFamily: sans, outline: "none", boxSizing: "border-box", color: K.body }}
          />
          <div style={{ display: "flex", gap: 6, marginTop: 4 }}>
            <Button size="sm" variant="primary" onClick={() => { onEdit(val); setEditing(false); }}>
              {t("save")}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
              {t("cancel")}
            </Button>
          </div>
        </div>
      ) : (
        <div style={{ fontSize: 11.5, color: K.body, lineHeight: 1.6, padding: "8px 10px", background: K.bg, border: `1px solid ${K.line}`, borderRadius: 6 }}>
          {yours || "—"}
        </div>
      )}
    </div>
  );
}

function HighlightsCompare({ yours, onChange }: { yours: string[]; onChange: (v: string[]) => void }) {
  const t = useT(DRAWER_MESSAGES);
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: K.muted, marginBottom: 6 }}>
        {t("highlights")}
      </div>
      <div style={{ padding: "8px 10px", background: K.bg, border: `1px solid ${K.line}`, borderRadius: 6 }}>
        {yours.map((h, i) => (
          <div key={i} style={{ display: "flex", gap: 4, marginBottom: 4, alignItems: "flex-start" }}>
            <span style={{ color: K.accent, fontWeight: 700, flexShrink: 0 }}>•</span>
            <input
              value={h}
              onChange={(e) => {
                const n = [...yours];
                n[i] = e.target.value;
                onChange(n);
              }}
              aria-label={t("highlightN", i + 1)}
              style={{ flex: 1, fontSize: 11, border: `1px solid ${K.line}`, borderRadius: 4, padding: "2px 6px", fontFamily: sans, outline: "none", background: "transparent" }}
            />
            <IconButton
              variant="plain"
              size="xs"
              tone="danger"
              onClick={() => onChange(yours.filter((_, j) => j !== i))}
              label={t("removeHighlightN", i + 1)}
              title={t("remove")}
              style={{ flexShrink: 0 }}
            >
              <span aria-hidden>×</span>
            </IconButton>
          </div>
        ))}
        <LinkButton size="sm" weight="regular" onClick={() => onChange([...yours, ""])}>
          {t("add")}
        </LinkButton>
      </div>
    </div>
  );
}

function ItineraryCompare({ yours, expand, setExpand }: { yours: string; expand: boolean; setExpand: (v: boolean) => void }) {
  const t = useT(DRAWER_MESSAGES);
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
        <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: K.muted }}>{t("itinerary")}</div>
        <Button variant="dashed" size="sm" onClick={() => setExpand(!expand)} aria-expanded={expand}>
          {expand ? t("collapse") : t("expand")}
        </Button>
      </div>
      <div style={{ fontSize: 11.5, color: K.body, lineHeight: 1.6, maxHeight: expand ? "none" : 100, overflow: expand ? "visible" : "hidden", whiteSpace: "pre-wrap", padding: "8px 10px", background: K.bg, border: `1px solid ${K.line}`, borderRadius: 6 }}>
        {yours || "—"}
      </div>
    </div>
  );
}

export type CatalogDrawerBody = {
  group: Version;
  // callbacks owned by the parent (which runs the mutations against react-query)
  saving: boolean;
  saveOk: boolean;
  dirty: boolean;
  exporting: boolean;
  rewriting: boolean;
  onSave: (edited: Record<string, unknown>) => void;
  onRequestRewrite: () => void;
  onExportDocx: (versionId: string) => void;
  onDirty: () => void;
  onClose: () => void;
};

/**
 * The drawer body. Keyed by group.id in the parent so switching tours / picking up a new version
 * remounts it with a fresh editor (no reset-in-effect; React Compiler rule, AGENTS.md).
 */
export function CatalogDrawer(props: CatalogDrawerBody) {
  const { group, saving, saveOk, dirty, exporting, rewriting } = props;
  const t = useT(DRAWER_MESSAGES);
  const lang = useLang();

  // Version detail (the parent only opens this drawer on a finished version).
  // AA-836 (D4) — "See original content" reads the Master text from this same response: GET
  // /v1/tours/versions/{id} joins published_tours and returns aa_summary / aa_highlights /
  // aa_itineraries (tenant pool, RLS-scoped). The old extra call to GET /v1/tours/pool/{id} hit a
  // route that never existed (404 + console error on every open) and then fell back to these
  // same fields anyway.
  const { data: detail, isLoading } = useQuery({
    queryKey: ["my-version-detail", group.id],
    queryFn: () => fetchVersionDetail(group.id),
  });

  // Editor state, initialised from detail once it loads (lazy-ish: derived initial via useMemo of
  // the parsed content, then held in local state so edits stick). We key the whole drawer by
  // group.id in the parent, so this component is fresh per version.
  const parsed = useMemo(
    () => (detail ? (parseContent(detail.rewritten_content) as Record<string, unknown> | null) : null),
    [detail],
  );
  const [editSummary, setEditSummary] = useState<string | null>(null);
  const [editHighlights, setEditHighlights] = useState<string[] | null>(null);
  const [expandItin, setExpandItin] = useState(true);

  // Resolve the current editor values: local state if the user has touched it, else from data.
  const summaryVal = editSummary ?? (parsed?.summary as string) ?? detail?.aa_summary ?? "";
  const highlightsVal =
    editHighlights ??
    (Array.isArray(parsed?.highlights) ? (parsed!.highlights as string[]) : parseHighlights(detail?.aa_highlights ?? ""));

  function markSummary(v: string) {
    setEditSummary(v);
    props.onDirty();
  }
  function markHighlights(v: string[]) {
    setEditHighlights(v);
    props.onDirty();
  }

  function doSave() {
    props.onSave({
      name: detail?.aa_name ?? group.aa_name,
      subtitle: detail?.aa_subtitle ?? group.aa_subtitle,
      summary: summaryVal,
      highlights: highlightsVal,
      seo_title: detail?.aa_seo_title ?? "",
      seo_meta: detail?.aa_seo_meta ?? "",
    });
  }

  // ── Header ──
  const header = (
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", width: "100%" }}>
      {/* The tour name is the kit Drawer title (CatalogTab.tsx), so only the language stays here. */}
      <div style={{ fontSize: 11.5, color: K.muted, fontFamily: mono }}>{group.rewrite_language}</div>
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        {dirty && (
          <Button variant="primary" size="sm" disabled={saving} onClick={doSave}>
            <Save size={12} /> {saving ? t("saving") : t("save")}
          </Button>
        )}
        {saveOk && !dirty && <span style={{ fontSize: 12, color: K.success, fontWeight: 600 }}>{t("savedTick")}</span>}
        <IconButton variant="plain" size="sm" onClick={props.onClose} label={t("close")} title={t("close")}>
          <X size={16} aria-hidden />
        </IconButton>
      </div>
    </div>
  );

  if (isLoading || !detail) {
    return (
      <>
        {header}
        <LoadingScreen message={t("loadingContent")} />
      </>
    );
  }

  const itinYours = String(parsed?.itineraries ?? "");
  const showItin = Boolean(detail.aa_itineraries || parsed?.itineraries);

  return (
    <>
      {header}
      <div style={{ marginTop: 16 }}>
        <div style={{ display: "flex", gap: 8, marginBottom: 12, fontSize: 11, color: K.muted2, fontFamily: mono }}>
          <span>{t("created", fmtDateTime(detail.created_at, lang))}</span>
          {detail.edited_at && <span>{t("edited", fmtDateTime(detail.edited_at, lang))}</span>}
        </div>

        {/* AA-990: AA updated the tour after this version. Informational only; the tenant's text stays as it is. */}
        <MasterNewerNotice
          version={detail.master_newer != null ? detail : group}
          onRewrite={props.onRequestRewrite}
          rewriting={rewriting}
          style={{ marginBottom: 14 }}
        />

        <CompareRow label={t("summary")} yours={summaryVal} onEdit={markSummary} />
        <HighlightsCompare yours={highlightsVal} onChange={markHighlights} />
        {showItin && <ItineraryCompare yours={itinYours} expand={expandItin} setExpand={setExpandItin} />}

        <SeeOriginalToggle
          summary={String(detail.aa_summary ?? "")}
          seoTitle={null}
          seoMeta={null}
          highlightsRaw={String(detail.aa_highlights ?? "")}
          itineraries={String(detail.aa_itineraries ?? "") || null}
        />

        {(detail.duration || detail.inclusions || detail.exclusions) && (
          <div style={{ paddingTop: 14, marginTop: 14, borderTop: `1px solid ${K.line}` }}>
            <div style={{ fontSize: 10.5, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.12em", color: K.muted, marginBottom: 10 }}>
              {t("tripDetails")}
            </div>
            {detail.duration && <TripFact label={t("duration")} value={detail.duration} />}
            {detail.inclusions && <TripFact label={t("inclusions")} value={detail.inclusions} />}
            {detail.exclusions && <TripFact label={t("exclusions")} value={detail.exclusions} />}
          </div>
        )}

        <div style={{ display: "flex", justifyContent: "flex-end", alignItems: "center", flexWrap: "wrap", gap: 8, paddingTop: 14, marginTop: 14, borderTop: `1px solid ${K.line}` }}>
          {/* AA-819 — AUP notice beside Export DOCX; takes the free space, wraps above the buttons on phones. */}
          <AiNotice variant="publisher" style={{ flex: "1 1 260px", minWidth: 0 }} />
          <Button variant="ghost" disabled={exporting} onClick={() => props.onExportDocx(detail.id)}>
            <Download size={12} /> {exporting ? t("exporting") : t("exportDocx")}
          </Button>
          <Button variant="secondary" disabled={rewriting} onClick={props.onRequestRewrite}>
            {rewriting ? t("starting") : t("requestRewrite")}
          </Button>
        </div>
      </div>
    </>
  );
}
