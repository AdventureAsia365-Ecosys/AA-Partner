"use client";
// web/components/portal/PortalOffline.tsx — the installable, offline parts of the traveller portal (AA-954, card
// CRM-24b, R16; design v3 C-09 "installable, offline", WF-8 "ITINERARY (works offline)", §2.9 rule 7):
//
//   OfflineView     what the page shows when the network cannot be reached: the itinerary kept on this device (the
//                   trip, its days and services as travellers see them, the agency contact) with "last updated", and
//                   what needs a connection; "Remove from this device" drops it. Nothing saved: say so.
//   InstallPrompt   "Install trip app" when the browser offers its install prompt; on iPhone / iPad the "Share, then
//                   Add to Home Screen" hint; nothing once installed.
import { useEffect, useState } from "react";
import { Badge, Button, K, LinkButton } from "@/app/_kit";
import { formatDate, formatInstant, type DateLocale } from "@/lib/dates";
import { fmt } from "@/lib/i18n";
import type { OfflineItinerary } from "@/lib/offlineItinerary";
import { installMode, runningStandalone, type InstallPromptEvent } from "@/lib/portalPwa";
import { TripSection } from "./PortalPerson";

type T = (key: string) => string;

const section: React.CSSProperties = { display: "grid", gap: 8, paddingTop: 12, borderTop: `1px solid ${K.line}` };
const line: React.CSSProperties = { margin: 0, fontSize: 13.5, color: K.body, overflowWrap: "anywhere" };

function browserZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

export function OfflineView({
  copy,
  t,
  locale,
  onRetry,
  onForget,
}: {
  copy: OfflineItinerary | null;
  t: T;
  locale: DateLocale;
  onRetry: () => void;
  onForget: () => void;
}) {
  const retry = (
    <Button variant="secondary" size="lg" testId="portal-offline-retry" onClick={onRetry}>
      {t("client.offline.retry")}
    </Button>
  );
  if (!copy) {
    return (
      <div data-testid="portal-offline" data-saved="false" style={{ display: "grid", gap: 12 }}>
        <h1 style={{ margin: 0, fontSize: 20, color: K.ink }}>{t("client.offline.title")}</h1>
        <p style={line}>{t("client.offline.nothing")}</p>
        <div>{retry}</div>
      </div>
    );
  }
  return (
    <div data-testid="portal-offline" data-saved="true" style={{ display: "grid", gap: 14 }}>
      <div style={{ display: "grid", gap: 4 }}>
        <p style={{ margin: 0, fontSize: 13, color: K.muted, overflowWrap: "anywhere" }}>{fmt(t("client.preparedBy"), { agency: copy.agency_name })}</p>
        <h1 style={{ margin: 0, fontSize: 20, color: K.ink, overflowWrap: "anywhere" }}>{copy.trip.title}</h1>
        {copy.trip.start_date && (
          <p style={{ margin: 0, fontSize: 13, color: K.body }}>
            {formatDate(copy.trip.start_date, locale)}
            {copy.trip.end_date ? ` – ${formatDate(copy.trip.end_date, locale)}` : ""}
          </p>
        )}
      </div>
      <div role="status" data-testid="portal-offline-status" style={{ display: "grid", gap: 4 }}>
        <div>
          <Badge tone="warning">{t("client.offline.title")}</Badge>
        </div>
        <p style={line} data-testid="portal-offline-updated" data-saved-at={copy.saved_at}>
          {fmt(t("client.offline.lastUpdated"), { when: formatInstant(copy.saved_at, locale, browserZone()) })}
        </p>
      </div>

      <TripSection days={copy.days} services={copy.services} t={t} locale={locale} />

      <section data-testid="portal-offline-contact" style={section}>
        <h2 style={{ margin: 0, fontSize: 16, color: K.ink }}>{t("client.offline.contactTitle")}</h2>
        <p style={line}>{copy.agency_name}</p>
        {copy.contact && (
          <p style={line}>
            <a href={`mailto:${copy.contact.email}`} style={{ color: K.accentDeep, fontWeight: 600 }} data-testid="portal-offline-email">
              {fmt(t("client.offline.email"), { agency: copy.agency_name })}
            </a>
            <span style={{ color: K.muted }}>{` · ${copy.contact.email}`}</span>
          </p>
        )}
      </section>

      <p style={{ ...line, color: K.muted }}>{t("client.offline.onlineOnly")}</p>
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "center" }}>
        {retry}
        <LinkButton size="inherit" tone="muted" testId="portal-offline-forget" onClick={onForget}>
          {t("client.offline.forget")}
        </LinkButton>
      </div>
    </div>
  );
}

export function InstallPrompt({ t }: { t: T }) {
  const [prompt, setPrompt] = useState<InstallPromptEvent | null>(null);
  const [installed, setInstalled] = useState(false);
  const [standalone] = useState(runningStandalone);
  const [userAgent] = useState(() => (typeof navigator !== "undefined" ? navigator.userAgent : ""));

  useEffect(() => {
    const offered = (e: Event) => {
      e.preventDefault(); // keep the browser's mini bar away; our button opens the prompt
      setPrompt(e as InstallPromptEvent);
    };
    const done = () => {
      setInstalled(true);
      setPrompt(null);
    };
    window.addEventListener("beforeinstallprompt", offered);
    window.addEventListener("appinstalled", done);
    return () => {
      window.removeEventListener("beforeinstallprompt", offered);
      window.removeEventListener("appinstalled", done);
    };
  }, []);

  const mode = installMode({ standalone, installed, prompt: prompt !== null, userAgent });
  if (mode === "none") return null;
  if (mode === "installed") {
    return installed ? (
      <p role="status" data-testid="portal-install-done" style={{ ...line, color: K.success }}>
        {t("client.install.done")}
      </p>
    ) : null;
  }
  return (
    <div data-testid="portal-install" data-mode={mode} style={{ display: "grid", gap: 6, padding: "10px 12px", border: `1px solid ${K.line}`, borderRadius: 8 }}>
      <p style={line}>{t("client.install.help")}</p>
      {mode === "prompt" ? (
        <div>
          <Button
            variant="secondary"
            size="lg"
            testId="portal-install-button"
            onClick={async () => {
              const offer = prompt;
              if (!offer) return;
              setPrompt(null); // a prompt event can be used once
              try {
                await offer.prompt();
                if ((await offer.userChoice).outcome === "accepted") setInstalled(true);
              } catch {
                // the browser refused to show it again
              }
            }}
          >
            {t("client.install.button")}
          </Button>
        </div>
      ) : (
        <p style={{ ...line, color: K.muted }}>{t("client.install.ios")}</p>
      )}
    </div>
  );
}
