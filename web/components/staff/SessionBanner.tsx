"use client";
// web/components/staff/SessionBanner.tsx — the staff side of the support banner (AA-1019, design v3 S-04 "banners on
// both sides"): amber for an AA support session, red for emergency access (break-glass), on every page of the agency
// view; the agency, the ticket, the reason (break-glass) and when it ends; "End session" ends the caller's own session
// (POST support|breakglass/sessions/end) and goes back to the start page. Not dismissible.
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button, K, useHydrated } from "@/app/_kit";
import { formatInstant, type DateLocale } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import { refusalKey, type SessionView } from "@/lib/staffConsole";
import { staffSend } from "@/lib/staffClient";

export function SessionBanner({
  session,
  agencyName,
  lang,
  locale,
}: {
  session: SessionView;
  agencyName: string;
  lang: Lang;
  locale: DateLocale;
}) {
  const router = useRouter();
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const emergency = session.kind === "breakglass";
  const time = formatInstant(session.expires_at, locale, "UTC");
  const text = emergency
    ? fmt(tr("staff.session.breakglassBanner"), {
        agency: agencyName,
        reason: tr(`staff.breakglass.reason.${session.reason_code ?? "service_outage"}`),
        ticket: session.ticket_ref,
        time,
      })
    : fmt(tr("staff.session.supportBanner"), { agency: agencyName, ticket: session.ticket_ref, time });

  async function end() {
    setBusy(true);
    setError(null);
    const res = await staffSend("POST", `${session.kind}/sessions/end`, { body: { tenant_id: session.tenant_id } });
    setBusy(false);
    if (!res.ok && res.detail !== "no_support_session") {
      setError(tr(refusalKey(res.detail)));
      return;
    }
    router.push("/staff/support");
    router.refresh();
  }

  return (
    <div
      role="status"
      data-testid={emergency ? "staff-session-banner-emergency" : "staff-session-banner"}
      style={{
        display: "flex",
        gap: 12,
        alignItems: "center",
        flexWrap: "wrap",
        justifyContent: "space-between",
        background: emergency ? K.dangerSoft : K.amberSoft,
        border: `1px solid ${emergency ? K.danger : K.amber}`,
        borderRadius: 10,
        padding: "10px 14px",
        fontSize: 13.5,
        color: K.ink,
      }}
    >
      <span style={{ minWidth: 0, overflowWrap: "anywhere" }}>{text}</span>
      <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
        {error && <span style={{ color: K.danger, fontSize: 12.5 }}>{error}</span>}
        <Button size="sm" variant={emergency ? "danger" : "secondary"} disabled={!hydrated || busy} onClick={end} testId="staff-session-end">
          {tr("staff.session.end")}
        </Button>
      </span>
    </div>
  );
}
