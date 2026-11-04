"use client";
// web/app/(studio)/portal/content/publish/page.tsx — AA-457 [T11 PR1] + AA-458 [T11 PR2] + AA-815
// AA-759: was /portal/t11-publish (308 from the old slug, next.config.ts).
//
// AA-457 built the connect flow (Option 3+2, AA-456 STEP0 §9): a channel that isn't connected yet
// shows its connect form inline here. AA-458 added the "Ready to Publish" list (PublishPendingList),
// which stays visible even when nothing is connected (its Publish buttons disable with "Connect
// <channel> to publish") so a tenant always sees what they have ready.
//
// AA-815: two channels — WordPress (blog) and a Facebook Page — each with its own connect form /
// status card, the pending list gated per channel, and a "Published" list with Unpublish
// (PublishedList). An Unpublish puts the piece back in Ready to Publish (reloadSignal).
// AA-761: a connected channel shows a one-line summary (health of the last test + "Manage"); the status card with
// "Test connection", "Change credentials" and Disconnect lives on Account › Integrations.
import Link from "next/link";
import { useState } from "react";
import { Settings2 } from "lucide-react";
import { K, PageHeader, sans, useT } from "@/app/_kit";
import { SectionSkeleton } from "../../_components/SectionSkeleton";
import { PAGES_MESSAGES } from "../content.messages";
import { useWordPressStatus, WordPressConnectForm } from "../../_components/WordPressConnect";
import { useFacebookStatus, FacebookConnectForm } from "../../_components/FacebookConnect";
import { ConnectionSummary } from "../../_components/ConnectionSummary";
import { PublishPendingList } from "../../_components/PublishPendingList";
import { PublishedList } from "../../_components/PublishedList";

export default function T11PublishPage() {
  const t = useT(PAGES_MESSAGES);
  const wp = useWordPressStatus();
  const fb = useFacebookStatus();
  const [pendingReload, setPendingReload] = useState(0);

  // AA-760 — no full-page spinner any more: the header and both lists render at once, and each
  // channel card shows its own skeleton until that channel's status answers (one slow call used to
  // blank the whole page). A later refresh (after connect / re-test) keeps the card up.
  const wpPending = wp.loading && !wp.status;
  const fbPending = fb.loading && !fb.status;
  const wpConnected = !!wp.status?.connected;
  const fbConnected = !!fb.status?.connected;

  return (
    // AA-524 — the connection cards keep a form-comfortable width; the content lists are full
    // width. The H1 is not sticky; PublishPendingList's own StickyBar is the one sticky point.
    <div>
      <PageHeader
        title={t("publishTitle")}
        description={t("publishSub")}
        actions={(wpConnected || fbConnected) ? (
          <Link href="/portal/account/integrations" style={{
            display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12.5,
            fontWeight: 600, color: K.ink3, textDecoration: "none", whiteSpace: "nowrap", fontFamily: sans,
          }}>
            <Settings2 size={13} /> {t("manageConnections")}
          </Link>
        ) : undefined}
      />

      <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
        <div style={{
          display: "grid", gap: 16, alignItems: "start",
          gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 340px), 1fr))",
        }}>
          {wpPending ? <SectionSkeleton label={t("loadingWp")} />
            : wpConnected && wp.status
              ? <ConnectionSummary channel={t("wordpress")} target={wp.status.site_url} status={wp.status} testId="publish-wp-summary" />
              : <WordPressConnectForm onConnected={wp.refresh} />}
          {fbPending ? <SectionSkeleton label={t("loadingFb")} />
            : fbConnected && fb.status
              ? <ConnectionSummary channel={t("facebookPage")} target={fb.status.page_id ? t("fbPage", fb.status.page_id) : null} status={fb.status} testId="publish-fb-summary" />
              : <FacebookConnectForm onConnected={fb.refresh} />}
        </div>

        <PublishPendingList
          connected={{ blog: wpConnected, facebook: fbConnected }}
          reloadSignal={pendingReload}
        />

        <PublishedList onUnpublished={() => setPendingReload(n => n + 1)} />
      </div>
    </div>
  );
}
