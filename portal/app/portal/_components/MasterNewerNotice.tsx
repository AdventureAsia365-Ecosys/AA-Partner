"use client";
// portal/app/portal/_components/MasterNewerNotice.tsx
// AA-990 scope 5 — Adventure Asia updated the tour after the tenant's version. One calm, informational line in the
// My Tours drawer (with the existing Request Rewrite action) and a small badge on the list row. Nothing is
// overwritten: the tenant decides whether to rewrite. The rules (when, which sentence) live in lib/masterNotice.ts;
// both fields are optional, so before the CIS change ships this renders nothing. Tenant-sees-content-only: the copy
// talks about the tour text, never about checks, gates or flags.

import { Info } from "lucide-react";
import { K, sans } from "@/app/_kit";
import { MASTER_NEWER_LABEL, masterNoticeText, showMasterNotice, type MasterFreshness } from "@/lib/masterNotice";
import { Badge, Btn, fmtDate } from "./ui";

/** The list-row badge; its tooltip is the full sentence. */
export function MasterNewerBadge({ version }: { version: MasterFreshness | null | undefined }) {
  if (!showMasterNotice(version)) return null;
  return (
    <span
      title={masterNoticeText(version, fmtDate) ?? undefined}
      data-testid="master-newer-badge"
      style={{ display: "inline-flex", maxWidth: "100%" }}
    >
      <Badge variant="info">{MASTER_NEWER_LABEL}</Badge>
    </span>
  );
}

/** The drawer notice. `onRewrite` is the drawer's existing Request Rewrite action (omitted: no button). */
export function MasterNewerNotice({
  version,
  onRewrite,
  rewriting = false,
  style,
}: {
  version: MasterFreshness | null | undefined;
  onRewrite?: () => void;
  rewriting?: boolean;
  style?: React.CSSProperties;
}) {
  const text = masterNoticeText(version, fmtDate);
  if (!text) return null;
  return (
    <div
      role="note"
      data-testid="master-newer-notice"
      style={{
        display: "flex",
        alignItems: "flex-start",
        flexWrap: "wrap",
        gap: 10,
        padding: "10px 12px",
        background: K.infoSoft,
        border: `1px solid ${K.line}`,
        borderRadius: 8,
        fontFamily: sans,
        fontSize: 12,
        lineHeight: 1.5,
        color: K.body,
        ...style,
      }}
    >
      <div style={{ display: "flex", alignItems: "flex-start", gap: 8, flex: "1 1 240px", minWidth: 0 }}>
        <Info size={14} aria-hidden style={{ flexShrink: 0, marginTop: 2, color: K.info }} />
        <span style={{ minWidth: 0, overflowWrap: "anywhere" }}>{text}</span>
      </div>
      {onRewrite && (
        <Btn variant="secondary" size="sm" disabled={rewriting} onClick={onRewrite}>
          {rewriting ? "Starting…" : "Rewrite"}
        </Btn>
      )}
    </div>
  );
}
