"use client";
// app/(tenant)/portal/t11-publish/page.tsx — AA-457 [T11 PR1] + AA-458 [T11 PR2]
//
// AA-457 built the connect flow (Option 3+2, AA-456 STEP0 §9): if WordPress isn't connected
// yet, this page IS the connect form. AA-458 adds the real list+publish content that was
// deliberately left as a placeholder — the "Ready to Publish" list (PublishPendingList) now
// stays visible even when not connected (its own Publish buttons disable with STEP0 §12's exact
// copy, "Connect WordPress to publish", rather than the whole list disappearing) so a tenant
// always sees what they have ready.
//
// Sidebar entry added in this same PR (Sidebar.tsx NAV1) — deliberately not added in AA-457
// per that PR's own scope note, since the route wasn't yet functionally complete.
import Link from "next/link";
import { Settings2 } from "lucide-react";
import { T, serif, sans, LoadingScreen } from "../_components/ui";
import { useWordPressStatus, WordPressConnectForm, WordPressStatusCard } from "../_components/WordPressConnect";
import { PublishPendingList } from "../_components/PublishPendingList";

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

      <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
        {!status.connected ? (
          <WordPressConnectForm onConnected={refresh} />
        ) : (
          <>
            <WordPressStatusCard status={status} onRetest={refresh} />
            <Link href="/portal/t11-publish/connection" style={{
              display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12.5,
              fontWeight: 600, color: T.ink3, textDecoration: "none", whiteSpace: "nowrap",
              fontFamily: sans, alignSelf: "flex-start", marginTop: -10,
            }}>
              <Settings2 size={13} /> Manage connection
            </Link>
          </>
        )}

        <PublishPendingList wordpressConnected={status.connected} />
      </div>
    </div>
  );
}
