"use client";
// app/(tenant)/portal/t11-publish/connection/page.tsx — AA-457 [T11 PR1]
//
// The standalone "manage connection" half of Option 3+2 (AA-456 STEP0 §9) — where a tenant goes
// to re-test or change WordPress credentials after the initial inline connect on
// /portal/t11-publish. Shares WordPressConnectForm/WordPressStatusCard with that page rather
// than duplicating the form.
import Link from "next/link";
import { useState } from "react";
import { ArrowLeft } from "lucide-react";
import { T, serif, sans, LoadingScreen } from "../../_components/ui";
import { useWordPressStatus, WordPressConnectForm, WordPressStatusCard } from "../../_components/WordPressConnect";

export default function T11PublishConnectionPage() {
  const { status, loading, refresh } = useWordPressStatus();
  const [changing, setChanging] = useState(false);

  if (loading || !status) return <LoadingScreen message="Loading connection status…" />;

  return (
    <div style={{ maxWidth: 640 }}>
      <Link href="/portal/t11-publish" style={{
        display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12.5, fontWeight: 600,
        color: T.muted, textDecoration: "none", marginBottom: 16, fontFamily: sans,
      }}>
        <ArrowLeft size={13} /> Back to Publish
      </Link>

      <div style={{ marginBottom: 24 }}>
        <h1 style={{ fontFamily: serif, fontSize: 24, fontWeight: 500, color: T.ink, margin: "0 0 6px" }}>
          WordPress Connection
        </h1>
        <p style={{ fontSize: 13, color: T.muted, margin: 0 }}>
          Manage the WordPress site your published content is sent to.
        </p>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
        {status.connected && !changing && (
          <>
            <WordPressStatusCard status={status} onRetest={refresh} />
            <button onClick={() => setChanging(true)} style={{
              alignSelf: "flex-start", background: "none", border: "none", cursor: "pointer",
              fontSize: 12.5, fontWeight: 600, color: T.muted, textDecoration: "underline",
              fontFamily: sans, padding: 0,
            }}>
              Change credentials
            </button>
          </>
        )}

        {(!status.connected || changing) && (
          <WordPressConnectForm onConnected={() => { setChanging(false); refresh(); }} />
        )}
      </div>
    </div>
  );
}
