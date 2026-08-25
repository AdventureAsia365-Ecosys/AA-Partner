"use client";
// app/(tenant)/portal/_components/PublishPendingList.tsx — AA-458 [T11 PR2]
//
// The list AA-456's own STEP0 §5 flagged as missing: a tenant's approved content_piece rows for
// the blog channel that haven't been successfully published yet. Publish buttons stay visible
// even when WordPress isn't connected — disabled with the exact copy STEP0 §12 specified
// ("Connect WordPress to publish"), rather than hiding the list entirely, so a tenant always
// sees what they have ready.
//
// API: GET /api/tenant/v1/publish-log/pending, POST /api/tenant/v1/publish-log/{piece_id}/publish
// — same /api/tenant/[...path] proxy convention every other real tenant-portal fetch uses.

import { useState, useEffect, useCallback } from "react";
import { CheckCircle2, XCircle, ExternalLink, Loader2, FileText } from "lucide-react";
import { T, sans, Card, CardHead, Btn, EmptyState } from "./ui";

interface PendingPiece {
  piece_id: string;
  title: string;
  content_preview: string;
  channel: string;
  created_at: string | null;
}

interface PublishResult {
  success: boolean;
  external_url: string | null;
  last_error: string | null;
}

function fmtDate(iso: string | null): string {
  return iso ? new Date(iso).toLocaleDateString() : "—";
}

export function PublishPendingList({ wordpressConnected }: { wordpressConnected: boolean }) {
  const [pieces, setPieces] = useState<PendingPiece[] | null>(null);
  const [publishingId, setPublishingId] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, PublishResult>>({});

  const load = useCallback(() => {
    fetch("/api/tenant/v1/publish-log/pending")
      .then(r => (r.ok ? r.json() : { data: [] }))
      .then(d => setPieces(d.data || []))
      .catch(() => setPieces([]));
  }, []);

  useEffect(() => { load(); }, [load]);

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
      } else {
        setResults(prev => ({
          ...prev,
          [pieceId]: { success: false, external_url: null, last_error: body.last_error || body.detail || "Publish failed." },
        }));
      }
    } catch {
      setResults(prev => ({ ...prev, [pieceId]: { success: false, external_url: null, last_error: "Network error — please try again." } }));
    } finally {
      setPublishingId(null);
    }
  }

  return (
    <Card>
      <CardHead title="Ready to Publish" />
      {pieces === null ? (
        <div style={{ padding: 24, textAlign: "center", color: T.muted, fontSize: 13 }}>Loading…</div>
      ) : pieces.length === 0 ? (
        <EmptyState
          icon={<FileText size={28} color={T.muted2} />}
          title="Nothing to publish yet"
          sub="Approved content will show up here once you've written and it's passed quality review."
        />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {pieces.map(p => {
            const result = results[p.piece_id];
            const busy = publishingId === p.piece_id;
            return (
              <div key={p.piece_id} style={{
                padding: "14px 16px", borderRadius: 10, border: `1px solid ${T.line}`,
                display: "flex", flexDirection: "column", gap: 10,
              }}>
                <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 12 }}>
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <div style={{ fontSize: 14, fontWeight: 600, color: T.ink, marginBottom: 3 }}>{p.title}</div>
                    <div style={{ fontSize: 12, color: T.muted, lineHeight: 1.5, overflow: "hidden", textOverflow: "ellipsis", display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical" }}>
                      {p.content_preview}
                    </div>
                    <div style={{ fontSize: 11, color: T.muted2, marginTop: 6, fontFamily: sans }}>
                      {p.channel} · {fmtDate(p.created_at)}
                    </div>
                  </div>
                  <Btn
                    size="sm" variant="primary"
                    onClick={() => handlePublish(p.piece_id)}
                    disabled={busy || !wordpressConnected}
                    style={!wordpressConnected ? { opacity: 0.5, cursor: "not-allowed" } : undefined}
                  >
                    {busy ? <><Loader2 size={12} style={{ animation: "spin 1s linear infinite" }} /> Publishing…</>
                      : !wordpressConnected ? "Connect WordPress to publish"
                      : "Publish"}
                  </Btn>
                </div>

                {result?.success && (
                  <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "8px 12px", borderRadius: 8, background: T.greenSoft, color: T.green, fontSize: 12.5 }}>
                    <CheckCircle2 size={14} /> Published.{" "}
                    {result.external_url && (
                      <a href={result.external_url} target="_blank" rel="noreferrer" style={{ color: T.green, display: "inline-flex", alignItems: "center", gap: 4 }}>
                        View post <ExternalLink size={11} />
                      </a>
                    )}
                  </div>
                )}
                {result && !result.success && (
                  <div style={{ display: "flex", alignItems: "flex-start", gap: 8, padding: "8px 12px", borderRadius: 8, background: T.redSoft, color: T.red, fontSize: 12.5, lineHeight: 1.5 }}>
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
