"use client";
// web/app/(studio)/portal/account/integrations/page.tsx — AA-457 [T11 PR1] + AA-815
// AA-759: was /portal/t11-publish/connection (308 from the old slug, next.config.ts).
//
// The standalone "manage connections" half of Option 3+2 (AA-456 STEP0 §9): re-test, change
// credentials or disconnect each publish channel after the first inline connect on
// /portal/content/publish. Shares the connect forms / status cards with that page.
// AA-815: Facebook Page next to WordPress, and Disconnect (confirm step) on both cards.
// AA-761: each channel's health next to its title (the last test's result) — the "Test connection" lives here now,
// Publish only shows a summary that links back. AA-1036 R3 slice 3: the API key card went with the tenant API keys.
import { useState } from "react";
import { Badge, LinkButton, PageHeader, type Tone, K, useLang, useT } from "@/app/_kit";
import { connectionHealth, healthLabel, type ConnectionHealth } from "@/lib/studio/account";
import { SectionSkeleton } from "../../_components/SectionSkeleton";
import { INTEGRATIONS_MESSAGES } from "./integrations.messages";
import { useWordPressStatus, WordPressConnectForm, WordPressStatusCard } from "../../_components/WordPressConnect";
import { useFacebookStatus, FacebookConnectForm, FacebookStatusCard } from "../../_components/FacebookConnect";

const changeLinkStyle: React.CSSProperties = { alignSelf: "flex-start", textDecoration: "underline" };

const sectionTitle: React.CSSProperties = {
  fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em",
  color: K.muted, margin: "0 0 2px",
};

const HEALTH_TONE: Record<ConnectionHealth, Tone> = {
  not_connected: "neutral", healthy: "success", failing: "danger", untested: "warning",
};

function SectionTitle({ title, health, testId }: { title: string; health: ConnectionHealth | null; testId: string }) {
  const lang = useLang();
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
      <h2 style={sectionTitle}>{title}</h2>
      {health && <span data-testid={testId} data-health={health}><Badge tone={HEALTH_TONE[health]}>{healthLabel(health, lang)}</Badge></span>}
    </div>
  );
}

export default function T11PublishConnectionPage() {
  const t = useT(INTEGRATIONS_MESSAGES);
  const wp = useWordPressStatus();
  const fb = useFacebookStatus();
  const [changingWp, setChangingWp] = useState(false);
  const [changingFb, setChangingFb] = useState(false);

  // AA-760 — the header and section titles render at once; each section shows a card skeleton until
  // its own status answers (was one full-page spinner waiting for both calls).
  const wpPending = wp.loading && !wp.status;
  const fbPending = fb.loading && !fb.status;
  const wpConnected = !!wp.status?.connected;
  const fbConnected = !!fb.status?.connected;

  return (
    <div style={{ maxWidth: 640 }}>
      {/* AA-760 — no "Back to Publish" link: this page is an Account item in the sidebar, and Publish
          is not a sidebar item, so the link named a place the tenant could not find. */}
      <PageHeader title={t("title")} description={t("sub")} />

      <div style={{ display: "flex", flexDirection: "column", gap: 28 }}>
        <section style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <SectionTitle title={t("wordpress")} health={wpPending ? null : connectionHealth(wp.status)} testId="wp-health" />
          {wpPending && <SectionSkeleton label={t("loadingWp")} />}
          {!wpPending && wpConnected && wp.status && !changingWp && (
            <>
              <WordPressStatusCard status={wp.status} onRetest={wp.refresh} onDisconnected={wp.refresh} />
              <LinkButton tone="muted" size="sm" onClick={() => setChangingWp(true)} style={changeLinkStyle}>{t("changeCredentials")}</LinkButton>
            </>
          )}
          {!wpPending && (!wpConnected || changingWp) && (
            <WordPressConnectForm onConnected={() => { setChangingWp(false); wp.refresh(); }} />
          )}
        </section>

        <section style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <SectionTitle title={t("facebookPage")} health={fbPending ? null : connectionHealth(fb.status)} testId="fb-health" />
          {fbPending && <SectionSkeleton label={t("loadingFb")} />}
          {!fbPending && fbConnected && fb.status && !changingFb && (
            <>
              <FacebookStatusCard status={fb.status} onRetest={fb.refresh} onDisconnected={fb.refresh} />
              <LinkButton tone="muted" size="sm" onClick={() => setChangingFb(true)} style={changeLinkStyle}>{t("changePage")}</LinkButton>
            </>
          )}
          {!fbPending && (!fbConnected || changingFb) && (
            <FacebookConnectForm onConnected={() => { setChangingFb(false); fb.refresh(); }} />
          )}
        </section>
      </div>
    </div>
  );
}
