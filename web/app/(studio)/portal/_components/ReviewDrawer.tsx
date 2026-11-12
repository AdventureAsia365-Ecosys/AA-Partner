"use client";
// web/app/(studio)/portal/_components/ReviewDrawer.tsx
// AA-669 — the My Content detail view, shown in the kit Drawer when a row is opened. Carries over
// the copy-lock, flag banner, inline edit and export actions from the old ReviewList card
// (AA-501/AA-519/AA-614) unchanged in behaviour.
// AA-1088 (M6, S278 audit) — the drawer shows only the agency's own text and the AI notice. The old
// "Where this came from" block showed the writer's brief (the angle's formula and style, the source
// fact) and the search context it used (people-also-ask, related terms): internal input of the
// writer, not the agency's content (tenant-sees-content-only rule). tests/studio/reviewDrawer.test.ts
// keeps it out.

import { Download, Loader2, Pencil, Save, X } from "lucide-react";
import { useState } from "react";
import { type ReviewItem } from "./reviewListApi";
import { AiNotice, Button, K, sans, useT } from "@/app/_kit";
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
    </div>
  );
}
