"use client";
import { T, mono, sans, fmtDate } from "./ui";

type HistoryItem = {
  id: string;
  version_number: number;
  status: string;
  edit_source: string;
  quality_score: number | null;
  created_at: string;
};

interface VersionHistoryProps {
  versions: HistoryItem[];
  activeVersionId: string;
  onSelect: (item: HistoryItem) => void;
}

const STATUS_LABELS: Record<string, string> = {
  pending: "Queued", approved: "In Catalog",
  rejected: "New Version Requested", ai_generated: "Ready to Review",
  needs_review: "Needs Review", processing: "AI Writing…", ai_generating: "AI Writing…",
};

export function VersionHistory({ versions, activeVersionId, onSelect }: VersionHistoryProps) {
  if (versions.length <= 1) return null;

  return (
    <div style={{ padding: "14px 22px", borderBottom: `1px solid ${T.line}` }}>
      <div style={{ fontSize: 10.5, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.12em", color: T.muted, marginBottom: 10 }}>
        Version History
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
        {versions.map((v, i) => {
          const isActive = v.id === activeVersionId;
          return (
            <button
              key={v.id}
              onClick={() => !isActive && onSelect(v)}
              style={{
                display: "flex", alignItems: "center", gap: 10,
                padding: "7px 10px", borderRadius: 6, fontSize: 12,
                textAlign: "left", fontFamily: sans,
                background: isActive ? "rgba(219,150,40,0.06)" : "transparent",
                border: `1px solid ${isActive ? "rgba(219,150,40,0.3)" : "transparent"}`,
                cursor: isActive ? "default" : "pointer",
                width: "100%",
              }}
            >
              <span style={{ color: T.gold, fontWeight: 700, fontFamily: mono, minWidth: 20 }}>
                v{v.version_number}
              </span>
              {i === 0 && (
                <span style={{ fontSize: 9, background: "#DCFCE7", color: "#16A34A", padding: "1px 6px", borderRadius: 10, fontWeight: 700 }}>
                  CURRENT
                </span>
              )}
              <span style={{ color: T.muted, flex: 1 }}>
                {v.edit_source === "ai_generated" ? "AI Generated" : "Your Edit"}
              </span>
              <span style={{ fontSize: 11, color: T.muted2 }}>
                {STATUS_LABELS[v.status] ?? v.status}
              </span>
              <span style={{ color: T.muted2, fontFamily: mono, fontSize: 11 }}>
                {fmtDate(v.created_at)}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
