"use client";
// AA-1081 — Articles page: the adopted tours that have no article (Blog) topic yet, each with the reason in the
// agency's words. Data: `blog_topics` of GET /v1/slate (the last topic update; CIS slate.tenant_blog_topics). The
// reason is a typed code (lib/studio/blogTopics.ts); unknown codes read as "No article topic yet". Shows nothing
// when every tour has a topic or before the first update that computed it. Tenant sees content only: no scores,
// thresholds or internal step names.

import { useState } from "react";
import { Info } from "lucide-react";
import { Button, K, useT } from "@/app/_kit";
import { toursWithoutTopic, type BlogTopics } from "@/lib/studio/blogTopics";
import { BLOG_TOPICS_MESSAGES } from "./writer.messages";

const FIRST_ROWS = 5;

export function BlogTopicsNote({ blogTopics }: { blogTopics: BlogTopics | null | undefined }) {
  const t = useT(BLOG_TOPICS_MESSAGES);
  const [all, setAll] = useState(false);
  const rows = toursWithoutTopic(blogTopics);
  if (!blogTopics || rows.length === 0) return null;
  const shown = all ? rows : rows.slice(0, FIRST_ROWS);
  return (
    <section data-testid="blog-topics-note" aria-label={t("title", rows.length, blogTopics.total)} style={{
      marginTop: 18, padding: "12px 14px", border: `1px solid ${K.line2}`, borderRadius: 10, background: K.bg,
    }}>
      <div style={{ display: "flex", alignItems: "flex-start", gap: 8 }}>
        <Info size={15} aria-hidden style={{ color: K.muted, flexShrink: 0, marginTop: 2 }} />
        <div style={{ minWidth: 0, flex: 1 }}>
          <div style={{ fontSize: 13, fontWeight: 700, color: K.ink }}>{t("title", rows.length, blogTopics.total)}</div>
          <p style={{ fontSize: 12, color: K.muted, lineHeight: 1.6, margin: "4px 0 10px" }}>{t("intro")}</p>
          <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 8 }}>
            {shown.map(r => (
              <li key={r.tourId} data-testid="blog-topics-note-row" style={{ fontSize: 12.5, lineHeight: 1.55 }}>
                <span style={{ fontWeight: 600, color: K.ink, overflowWrap: "anywhere" }}>{r.name || t("untitledTour")}</span>
                <span style={{ color: K.muted }}> — {r.reason ? t(`reason_${r.reason}`) : t("reason_unknown")}</span>
              </li>
            ))}
          </ul>
          {rows.length > FIRST_ROWS && (
            <Button variant="ghost" size="sm" style={{ marginTop: 8 }} onClick={() => setAll(v => !v)}>
              {all ? t("showFewer") : t("showAll", rows.length)}
            </Button>
          )}
        </div>
      </div>
    </section>
  );
}
