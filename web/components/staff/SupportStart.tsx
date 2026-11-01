"use client";
// web/components/staff/SupportStart.tsx — S-04 (AA-1019 on the AA-971 API), group crm-support: the agencies whose owner
// allows AA support now (the owner's window, whether names are shown, whether someone works there) and "Start support
// session" with a ticket (SUP-1042). A started session opens the agency's read-only view. One staff member at a time
// per agency (409 support_session_in_use); the session ends with the owner's grant or after 8 hours.
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, Field, Input, K, useHydrated } from "@/app/_kit";
import { formatInstant, type DateLocale } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import { refusalKey, ticketValid, type GrantedAgency, type SessionView } from "@/lib/staffConsole";
import { staffSend } from "@/lib/staffClient";

function StartRow({ agency, lang, locale }: { agency: GrantedAgency; lang: Lang; locale: DateLocale }) {
  const router = useRouter();
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [ticket, setTicket] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const href = `/staff/support/${agency.tenant_id}`;

  async function start() {
    if (!ticketValid(ticket)) {
      setError(tr("staff.support.ticketInvalid"));
      return;
    }
    setBusy(true);
    setError(null);
    const res = await staffSend<SessionView>("POST", "support/sessions", { body: { tenant_id: agency.tenant_id, ticket_ref: ticket.trim() } });
    setBusy(false);
    if (!res.ok) {
      setError(tr(refusalKey(res.detail)));
      return;
    }
    router.push(href);
  }

  return (
    <li
      data-testid="staff-support-agency"
      style={{ listStyle: "none", border: `1px solid ${K.line}`, borderRadius: 10, padding: "12px 14px", display: "grid", gap: 8, background: K.card }}
    >
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        <strong style={{ color: K.ink, fontSize: 14 }}>{agency.agency_name}</strong>
        <Badge tone="neutral" dot={false}>
          {fmt(tr("staff.support.until"), { date: formatInstant(agency.expires_at, locale, "UTC") })}
        </Badge>
        <Badge tone={agency.show_identity ? "warning" : "info"} dot={false}>
          {tr(agency.show_identity ? "staff.support.identityShown" : "staff.support.identityHidden")}
        </Badge>
        {agency.mine && <Badge tone="success">{tr("staff.support.mine")}</Badge>}
        {agency.in_use && !agency.mine && <Badge tone="warning">{tr("staff.support.inUse")}</Badge>}
      </div>
      {agency.mine ? (
        <div>
          <Link href={href} style={{ fontSize: 13 }}>
            {tr("staff.support.open")} →
          </Link>
        </div>
      ) : (
        !agency.in_use && (
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "flex-end" }}>
            <Field label={tr("staff.support.ticket")} hint={tr("staff.support.ticketHelp")} style={{ minWidth: 0, flex: "1 1 200px", maxWidth: 320 }}>
              <Input value={ticket} onChange={(e) => setTicket(e.currentTarget.value)} maxLength={27} placeholder="SUP-1042" size="sm" />
            </Field>
            <Button variant="primary" size="sm" disabled={!hydrated || busy} onClick={start} testId="staff-support-start">
              {tr("staff.support.start")}
            </Button>
          </div>
        )
      )}
      {error && (
        <p role="alert" style={{ margin: 0, fontSize: 13, color: K.danger }}>
          {error}
        </p>
      )}
    </li>
  );
}

export function SupportStart({ agencies, lang, locale }: { agencies: GrantedAgency[]; lang: Lang; locale: DateLocale }) {
  if (agencies.length === 0) {
    return (
      <p style={{ margin: 0, fontSize: 13, color: K.muted }} data-testid="staff-support-none">
        {t(lang, "staff.support.grantedEmpty")}
      </p>
    );
  }
  return (
    <ul style={{ margin: 0, padding: 0, display: "grid", gap: 10 }}>
      {agencies.map((a) => (
        <StartRow key={a.tenant_id} agency={a} lang={lang} locale={locale} />
      ))}
    </ul>
  );
}
