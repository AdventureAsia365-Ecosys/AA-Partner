"use client";
// app/(tenant)/portal/t11-publish/page.tsx — AA-457 [T11 PR1]
//
// This PR only builds the connect flow (Option 3+2, AA-456 STEP0 §9): if WordPress isn't
// connected yet, this page IS the connect form — no separate navigation for the common
// first-time case. Once connected, this page currently shows a placeholder for what AA-458
// (PR 2) builds next: the list of approved content_piece rows + the real publish action. The
// route/skeleton exists now so AA-458 extends this file rather than creating it from scratch.
//
// No Sidebar entry yet (deliberate, per this task's own scope) — reachable by direct URL only
// until AA-458 adds real list/publish content and the nav entry together. Middleware needs no
// change: `{ prefix: "/portal", roles: ["admin","tenant"] }` in middleware.ts already covers
// every /portal/* route with one blanket entry (unlike the admin side's per-page allowlist).
import Link from "next/link";
import { Settings2 } from "lucide-react";
import { T, serif, sans, LoadingScreen } from "../_components/ui";
import { useWordPressStatus, WordPressConnectForm, WordPressStatusCard } from "../_components/WordPressConnect";

export default function T11PublishPage() {
  const { status, loading, refresh } = useWordPressStatus();

  if (loading || !status) return <LoadingScreen message="Loading connection status…" />;

  return (
    <div style={{ maxWidth: 640 }}>
      <div style={{ marginBottom: 24 }}>
        <h1 style={{ fontFamily: serif, fontSize: 24, fontWeight: 500, color: T.ink, margin: "0 0 6px" }}>
          Publish
        </h1>
        <p style={{ fontSize: 13, color: T.muted, margin: 0, lineHeight: 1.5 }}>
          Publish your approved content directly to your blog. Currently supports WordPress.
        </p>
      </div>

      {!status.connected ? (
        <WordPressConnectForm onConnected={refresh} />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
          <WordPressStatusCard status={status} onRetest={refresh} />

          <div style={{
            padding: "18px 20px", borderRadius: 12, border: `1px dashed ${T.line}`,
            display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12,
          }}>
            <span style={{ fontSize: 13, color: T.muted, lineHeight: 1.5 }}>
              Your approved content will appear here to publish — coming soon.
            </span>
            <Link href="/portal/t11-publish/connection" style={{
              display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12.5,
              fontWeight: 600, color: T.ink3, textDecoration: "none", whiteSpace: "nowrap",
              fontFamily: sans,
            }}>
              <Settings2 size={13} /> Manage connection
            </Link>
          </div>
        </div>
      )}
    </div>
  );
}
