"use client";
// web/app/(studio)/portal/_components/PublishPendingList.tsx — AA-458 [T11 PR2]
//
// The list AA-456's own STEP0 §5 flagged as missing: a tenant's approved content_piece rows for
// the blog channel that haven't been successfully published yet. Publish buttons stay visible
// even when WordPress isn't connected — disabled with the exact copy STEP0 §12 specified
// ("Connect WordPress to publish"), rather than hiding the list entirely, so a tenant always
// sees what they have ready.
//
// API: GET /api/tenant/v1/publish-log/pending, POST /api/tenant/v1/publish-log/{piece_id}/publish
// — same /api/tenant/[...path] proxy convention every other real tenant-portal fetch uses.
//
// AA-815: the Publish button is gated per channel (blog needs WordPress, facebook needs the
// Facebook Page) instead of WordPress for every piece, and a publish refreshes the Published list.

import { useState, useEffect, useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, XCircle, ExternalLink, Loader2, FileText, Milestone } from "lucide-react";
import { Card, CardHead, StickyBar } from "@/app/_kit/local/studio/StudioCard";
import { stripMarkdown } from "./markdownText";
import { PUBLISH_LOG_QUERY_KEY, channelName } from "./PublishedList";
import { channelName as channelLabel } from "./channelLabels";
import { AiNotice, Button, EmptyState, K, sans, useFormat, useLang, useT, type Translator } from "@/app/_kit";
import { PUBLISH_MESSAGES } from "./publish.messages";
import {
  ServedBadge, plainError, publishClosedLabel, servedTitle, workClosed, type ServedFields,
} from "./ServedBadge";

interface PendingPiece extends ServedFields {
  piece_id: string;
  title: string;
  content_preview: string;
  channel: string;
  created_at: string | null;
  // AA-519 Việc 4 — null for a Segment pick or pre-Slate request (not a Route-aware piece).
  route_hub_name: string | null;
  route_segment_count: number | null;
}

interface PublishResult {
  success: boolean;
  external_url: string | null;
  last_error: string | null;
}

// AA-760 — the shared date format ("9 Oct 2026"), was the browser locale ("10/20/2026").
/** AA-760 — "Route · 4 stops" (was "ROUTE: A → B → C → D · 8 SEGMENTS": all caps, and "segments"
 *  is pipeline vocabulary). The stop names stay in the tooltip. */
function routeLabel(hub: string | null, t: Translator<typeof PUBLISH_MESSAGES.en>): { text: string; title: string } {
  const stops = (hub ?? "").split("→").map(x => x.trim()).filter(Boolean);
  const title = stops.map(x => x.charAt(0).toUpperCase() + x.slice(1)).join(" → ");
  return { text: stops.length > 1 ? t("routeStops", stops.length) : t("route"), title };
}

export function PublishPendingList({ connected, reloadSignal = 0 }: {
  /** Connection per channel: { blog: WordPress connected, facebook: Facebook Page connected }. */
  connected: Record<string, boolean>;
  /** Bump to re-fetch (e.g. after an Unpublish puts a piece back in this list). */
  reloadSignal?: number;
}) {
  const t = useT(PUBLISH_MESSAGES);
  const lang = useLang();
  const { formatDate: fmtDate } = useFormat();
  const qc = useQueryClient();
  const [pieces, setPieces] = useState<PendingPiece[] | null>(null);
  const [publishingId, setPublishingId] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, PublishResult>>({});

  const load = useCallback(() => {
    fetch("/api/tenant/v1/publish-log/pending")
      .then(r => (r.ok ? r.json() : { data: [] }))
      .then(d => setPieces(d.data || []))
      .catch(() => setPieces([]));
  }, []);

  useEffect(() => { load(); }, [load, reloadSignal]);

  async function handlePublish(pieceId: string) {
    setPublishingId(pieceId);
    setResults(prev => { const next = { ...prev }; delete next[pieceId]; return next; });
    try {
      const res = await fetch(`/api/tenant/v1/publish-log/${pieceId}/publish`, { method: "POST" });
      const body = await res.json().catch(() => ({}));
      if (res.ok && body.success) {
        setResults(prev => ({ ...prev, [pieceId]: { success: true, external_url: body.external_url, last_error: null } }));
        // Published pieces drop out of the pending list entirely — refresh from the server
        // rather than guessing the new shape client-side.
        load();
        qc.invalidateQueries({ queryKey: PUBLISH_LOG_QUERY_KEY });
      } else {
        setResults(prev => ({
          ...prev,
          // AA-986 L3: a 409 tour_discontinued / tour_paused reads as its plain sentence
          [pieceId]: { success: false, external_url: null,
                       last_error: body.last_error || plainError(body.detail, t("publishFailed"), lang) },
        }));
      }
    } catch {
      setResults(prev => ({ ...prev, [pieceId]: { success: false, external_url: null, last_error: t("network") } }));
    } finally {
      setPublishingId(null);
    }
  }

  return (
    <Card>
      {/* AA-524 — sticky, bleeding out to Card's own default padding (22px), same pattern as
          ReviewList.tsx's identical card-list layout. */}
      <StickyBar
        background={K.card}
        style={{
          marginTop: -22, marginLeft: -22, marginRight: -22,
          paddingTop: 22, paddingLeft: 22, paddingRight: 22, paddingBottom: 14,
          borderRadius: "12px 12px 0 0",
        }}
      >
        <CardHead title={t("readyToPublish")} />
      </StickyBar>
      {/* AA-819 — AUP notice above the Publish buttons; AA-760: only when there is something to review. */}
      {pieces !== null && pieces.length > 0 && <AiNotice variant="publisher" style={{ marginBottom: 14 }} />}
      {pieces === null ? (
        <div aria-busy="true" style={{ padding: 24, textAlign: "center", color: K.muted, fontSize: 13 }}>{t("loading")}</div>
      ) : pieces.length === 0 ? (
        <EmptyState
          icon={<FileText size={28} color={K.muted2} />}
          title={t("nothingTitle")}
          description={t("nothingSub")}
        />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {pieces.map(p => {
            const result = results[p.piece_id];
            const busy = publishingId === p.piece_id;
            const channelConnected = !!connected[p.channel];
            return (
              <div key={p.piece_id} style={{
                padding: "14px 16px", borderRadius: 10, border: `1px solid ${K.line}`,
                display: "flex", flexDirection: "column", gap: 10,
              }}>
                {/* AA-836 (D2) — wraps: on a phone the button drops below the content, so the title
                    and preview use the full card width instead of a ~90px column beside it. */}
                <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
                  <div style={{ minWidth: 0, flex: "1 1 320px" }}>
                    <div style={{ fontSize: 14, fontWeight: 600, color: K.ink, marginBottom: 3, overflowWrap: "anywhere" }}>{p.title}</div>
                    {/* AA-1040 — the "AI-assisted" label of the piece (kit AiNotice badge form). */}
                    <div style={{ marginBottom: 5 }}><AiNotice variant="publisher" form="badge" testId="publish-pending-ai-badge" /></div>
                    {/* AA-986 L3: persistent badge; publishing is closed for a tour AA no longer serves */}
                    {p.served_notice && <div style={{ marginBottom: 5 }}><ServedBadge fields={p} /></div>}
                    {/* AA-519 Việc 4 — Route-aware piece, distinct from a plain single-atom one. */}
                    {p.route_segment_count != null && (() => {
                      const r = routeLabel(p.route_hub_name, t);
                      return (
                        <div style={{ marginBottom: 5 }}>
                          <span title={r.title || undefined} style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 11.5, fontWeight: 600, padding: "2px 9px", borderRadius: 999, background: K.accentTint, color: K.accentDeep }}>
                            <Milestone size={11} aria-hidden /> {r.text}
                          </span>
                        </div>
                      );
                    })()}
                    <div style={{ fontSize: 12, color: K.muted, lineHeight: 1.5, overflow: "hidden", textOverflow: "ellipsis", display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical" }}>
                      {/* AA-836 (D6) — plain text, never raw markdown (`## Title`, `**bold**`). */}
                      {/* S241 — a separator between blocks so a heading never runs into the next
                          sentence. The card title is the ANGLE name (v1_publish.py), not the
                          writer's `# Heading` — that heading is the post title that goes live, so
                          it stays in the preview unless it is the very same text as the card title. */}
                      {stripMarkdown(p.content_preview, { dropHeadingMatching: p.title, blockSeparator: " · " })}
                    </div>
                    <div style={{ fontSize: 11, color: K.muted2, marginTop: 6, fontFamily: sans }}>
                      {t("postMeta", channelLabel(p.channel, lang), fmtDate(p.created_at))}
                    </div>
                  </div>
                  {/* AA-760 — not connected: plain muted text at full opacity (a faded gold button was
                      white on gold at 50% opacity, well under 3:1). */}
                  {workClosed(p) ? (
                    <span title={servedTitle(p, lang)} style={{ flexShrink: 0, fontSize: 12, color: K.muted, alignSelf: "center", maxWidth: 220 }}>
                      {publishClosedLabel(p, lang)}
                    </span>
                  ) : channelConnected ? (
                    <Button
                      size="sm" variant="primary"
                      onClick={() => handlePublish(p.piece_id)}
                      disabled={busy}
                      style={{ flexShrink: 0 }}
                    >
                      {busy ? <><Loader2 size={12} style={{ animation: "spin 1s linear infinite" }} /> {t("publishing")}</> : t("publish")}
                    </Button>
                  ) : (
                    <span style={{ flexShrink: 0, fontSize: 12, color: K.muted, alignSelf: "center" }}>
                      {t("connectFirst", channelName(p.channel))}
                    </span>
                  )}
                </div>

                {result?.success && (
                  <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "8px 12px", borderRadius: 8, background: K.successSoft, color: K.success, fontSize: 12.5 }}>
                    <CheckCircle2 size={14} /> {t("published")}{" "}
                    {result.external_url && (
                      <a href={result.external_url} target="_blank" rel="noreferrer" style={{ color: K.success, display: "inline-flex", alignItems: "center", gap: 4 }}>
                        {t("viewPost")} <ExternalLink size={11} />
                      </a>
                    )}
                  </div>
                )}
                {result && !result.success && (
                  <div style={{ display: "flex", alignItems: "flex-start", gap: 8, padding: "8px 12px", borderRadius: 8, background: K.dangerSoft, color: K.danger, fontSize: 12.5, lineHeight: 1.5 }}>
                    <XCircle size={14} style={{ flexShrink: 0, marginTop: 1 }} />
                    <span>{result.last_error}</span>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}
