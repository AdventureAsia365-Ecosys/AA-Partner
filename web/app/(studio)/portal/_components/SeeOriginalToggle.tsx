"use client";
import { useState } from "react";
import { LinkButton, K, useT } from "@/app/_kit";
import { REWRITE_MESSAGES } from "./tours.messages";
import { parseHighlights } from "@/lib/studio/format";

interface SeeOriginalProps {
  summary: string | null;
  seoTitle: string | null;
  seoMeta: string | null;
  highlightsRaw: string | null;
  itineraries: string | null;
}

export function SeeOriginalToggle({ summary, seoTitle, seoMeta, highlightsRaw, itineraries }: SeeOriginalProps) {
  const t = useT(REWRITE_MESSAGES);
  const [open, setOpen] = useState(false);
  const highlights = parseHighlights(highlightsRaw ?? "");
  const hasContent = summary || seoTitle || seoMeta || highlights.length > 0 || itineraries;
  if (!hasContent) return null;

  return (
    <div style={{ borderTop: `1px dashed ${K.line}`, paddingTop: 14, marginTop: 6 }}>
      <LinkButton tone="muted" size="sm" weight="regular" onClick={() => setOpen(o => !o)} aria-expanded={open}>
        <span style={{ fontSize: 10 }}>{open ? "▲" : "▼"}</span>{" "}
        <span>{open ? t("hideOriginal") : t("seeOriginal")}</span>
      </LinkButton>

      {open && (
        <div style={{
          marginTop: 12, borderRadius: 8,
          background: K.bg, border: `1px solid ${K.line}`,
          padding: "14px 16px",
        }}>
          <p style={{ fontSize: 11, color: K.muted2, fontStyle: "italic", marginBottom: 14, lineHeight: 1.5 }}>
            {t("originalNote")}
          </p>
          {summary  && <OrigField label={t("summary")}>{summary}</OrigField>}
          {seoTitle && <OrigField label={t("seoTitle")}>{seoTitle}</OrigField>}
          {seoMeta  && <OrigField label={t("seoMeta")}>{seoMeta}</OrigField>}
          {highlights.length > 0 && (
            <OrigField label={t("highlights")}>
              {highlights.map((h, i) => (
                <div key={i} style={{ marginBottom: 3 }}>• {h}</div>
              ))}
            </OrigField>
          )}
          {itineraries && (
            <OrigField label={t("itinerary")}>
              <div style={{ whiteSpace: "pre-wrap", maxHeight: 220, overflowY: "auto" }}>
                {itineraries}
              </div>
            </OrigField>
          )}
        </div>
      )}
    </div>
  );
}

function OrigField({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ fontSize: 9, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: K.muted, marginBottom: 5 }}>
        {label}
      </div>
      <div style={{ fontSize: 11.5, color: K.body, lineHeight: 1.6 }}>{children}</div>
    </div>
  );
}
