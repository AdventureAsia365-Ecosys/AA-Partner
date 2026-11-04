"use client";
// web/app/(studio)/portal/_components/ReviewDrawer.tsx
// AA-669 — the My Content detail view, shown in the kit Drawer when a row is opened. Carries over
// the copy-lock, tenant-safe context, flag banner, inline edit and export actions from the old
// ReviewList card (AA-501/AA-519/AA-614) unchanged in behaviour.

import { Download, HelpCircle, Loader2, Pencil, Save, Search, Sparkles, X } from "lucide-react";
import { useState } from "react";
import { type ReviewItem } from "./reviewListApi";
import { AiNotice, Button, K, mono, sans, useT } from "@/app/_kit";
import { REVIEW_DRAWER_MESSAGES } from "./publish.messages";
import { MarkdownBody } from "./markdownText";

export function ReviewDrawer({
  item,
  onSave,
  onExport,
  saving,
  onCopyAttempt,
}: {
  item: ReviewItem;
  onSave: (pieceId: string, text: string) => Promise<void>;
  onExport: (item: ReviewItem, format: "text" | "html", mode?: "document" | "fragment") => void;
  saving: boolean;
  onCopyAttempt: () => void;
}) {
  const t = useT(REVIEW_DRAWER_MESSAGES);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(item.content_text ?? "");
  const editable = item.content_text !== null;

  async function doSave() {
    await onSave(item.piece_id, draft);
    setEditing(false);
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14, fontFamily: sans }}>
      {/* content */}
      {editing ? (
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          rows={14}
          style={{
            width: "100%",
            padding: "14px 16px",
            borderRadius: 10,
            border: `1px solid ${K.accent}`,
            background: K.card,
            fontSize: 13.5,
            lineHeight: 1.6,
            color: K.body,
            fontFamily: sans,
            resize: "vertical",
            outline: "none",
            boxSizing: "border-box",
          }}
        />
      ) : item.ready_state === "in_progress" ? (
        <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "14px 16px", borderRadius: 10, border: `1px solid ${K.line}`, background: K.bg, fontSize: 13, color: K.muted }}>
          <Loader2 size={14} style={{ animation: "spin 1s linear infinite" }} /> {t("stillWriting")}
        </div>
      ) : item.ready_state === "not_ready" && !item.content_text ? (
        <div style={{ padding: "14px 16px", borderRadius: 10, border: `1px solid ${K.line}`, background: K.bg, fontSize: 13, color: K.muted, lineHeight: 1.5 }}>
          {t("didntFinish")}
        </div>
      ) : (
        <div
          onCopy={(e) => { e.preventDefault(); onCopyAttempt(); }}
          onCut={(e) => { e.preventDefault(); onCopyAttempt(); }}
          onContextMenu={(e) => { e.preventDefault(); onCopyAttempt(); }}
          style={{
            padding: "14px 16px",
            borderRadius: 10,
            border: `1px solid ${K.line}`,
            background: K.card,
            fontSize: 13.5,
            lineHeight: 1.6,
            color: K.body,
            userSelect: "none",
            WebkitUserSelect: "none",
            MozUserSelect: "none",
          }}
        >
          {/* AA-836 (D6) — rendered, not raw markdown; the Edit textarea keeps the source. */}
          <MarkdownBody text={item.content_text} />
        </div>
      )}

      {/* AA-819 — AUP notice next to the export actions (the drawer covers the page's own line). */}
      {editable && <AiNotice variant="publisher" />}

      {/* actions */}
      {editable &&
        (editing ? (
          <div style={{ display: "flex", gap: 8 }}>
            <Button variant="primary" size="sm" disabled={saving} onClick={doSave}>
              <Save size={12} /> {saving ? t("saving") : t("save")}
            </Button>
            <Button variant="ghost" size="sm" disabled={saving} onClick={() => { setEditing(false); setDraft(item.content_text ?? ""); }}>
              <X size={12} /> {t("cancel")}
            </Button>
          </div>
        ) : (
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <Button variant="secondary" size="sm" onClick={() => setEditing(true)}>
              <Pencil size={12} /> {t("edit")}
            </Button>
            {item.channel === "blog" ? (
              <>
                <Button variant="secondary" size="sm" onClick={() => onExport(item, "text")}>
                  <Download size={12} /> {t("exportText")}
                </Button>
                <Button variant="secondary" size="sm" onClick={() => onExport(item, "html", "document")}>
                  <Download size={12} /> {t("exportHtmlFile")}
                </Button>
                <Button variant="secondary" size="sm" onClick={() => onExport(item, "html", "fragment")}>
                  <Download size={12} /> {t("exportHtmlCms")}
                </Button>
              </>
            ) : (
              <Button variant="secondary" size="sm" onClick={() => onExport(item, "text")}>
                <Download size={12} /> {t("export")}
              </Button>
            )}
          </div>
        ))}

      <ContextSection item={item} />
    </div>
  );
}

function ContextRow({ icon, label, value }: { icon: React.ReactNode; label: string; value: string }) {
  return (
    <div style={{ display: "flex", alignItems: "flex-start", gap: 6, fontSize: 12, color: K.body }}>
      <span style={{ color: K.muted2, marginTop: 1 }}>{icon}</span>
      <span><span style={{ color: K.muted2 }}>{label}:</span> {value}</span>
    </div>
  );
}

function ContextSection({ item }: { item: ReviewItem }) {
  const t = useT(REVIEW_DRAWER_MESSAGES);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10, paddingTop: 14, borderTop: `1px solid ${K.line}` }}>
      <div style={{ fontSize: 10.5, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.12em", color: K.muted2 }}>
        {t("whereFrom")}
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
        {item.goal && <ContextRow icon={<Sparkles size={12} />} label={t("goal")} value={item.goal.label} />}
        {item.tour && <ContextRow icon={<Search size={12} />} label={t("tour")} value={`${item.tour.name} (${item.tour.destination})`} />}
      </div>

      {item.angle && (
        <div style={{ padding: "10px 12px", borderRadius: 8, background: K.bg, fontSize: 12, lineHeight: 1.6, color: K.body }}>
          <div style={{ fontWeight: 600, color: K.ink, marginBottom: 2 }}>{t("angle", item.angle.name)}</div>
          <div style={{ color: K.muted }}>{item.angle.why_it_works}</div>
          <div style={{ marginTop: 4, fontSize: 11, color: K.muted2 }}>
            {item.angle.formula_fit} · {item.angle.best_final_style}
          </div>
        </div>
      )}

      {item.atom && (
        <div style={{ padding: "10px 12px", borderRadius: 8, background: K.bg, fontSize: 12, lineHeight: 1.6, color: K.body }}>
          <div style={{ fontWeight: 600, color: K.ink, marginBottom: 2 }}>{t("sourceFact")}</div>
          <div>{item.atom.text}</div>
          {(item.atom.activity_type || item.atom.emotional_hook || item.atom.season_note) && (
            <div style={{ marginTop: 4, fontSize: 11, color: K.muted2 }}>
              {[item.atom.activity_type, item.atom.emotional_hook, item.atom.season_note].filter(Boolean).join(" · ")}
            </div>
          )}
        </div>
      )}

      {item.dfs_paa_snapshot &&
        (item.dfs_paa_snapshot.people_also_ask.length > 0 || item.dfs_paa_snapshot.related_keywords.length > 0) && (
          <div style={{ padding: "10px 12px", borderRadius: 8, background: K.bg, fontSize: 12, lineHeight: 1.6, color: K.body }}>
            <div style={{ display: "flex", alignItems: "center", gap: 5, fontWeight: 600, color: K.ink, marginBottom: 4 }}>
              <HelpCircle size={12} /> {t("searchContext")}
            </div>
            {item.dfs_paa_snapshot.people_also_ask.length > 0 && (
              <div style={{ color: K.muted }}>{t("alsoAsk", item.dfs_paa_snapshot.people_also_ask.join("; "))}</div>
            )}
            {item.dfs_paa_snapshot.related_keywords.length > 0 && (
              <div style={{ marginTop: 2, fontSize: 11, color: K.muted2 }}>
                {t("relatedTerms", item.dfs_paa_snapshot.related_keywords.join(", "))}
              </div>
            )}
          </div>
        )}

      {item.cta && (
        <div style={{ fontSize: 11.5, color: K.muted, fontFamily: mono }}>{t("cta", item.cta)}</div>
      )}
    </div>
  );
}
