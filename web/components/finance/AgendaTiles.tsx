// web/components/finance/AgendaTiles.tsx — AA-1068 (crm-31): the Home "today" tiles (GET /reports/agenda): open
// proposals, deposits due this week, overdue payments, trips leaving soon with passport details missing, overdue AA
// invoices (owner / agent: null for the assistant, the tile is left out) and the next group departures with their
// travellers, each opening its work list. Counts only (no amount), so every role sees them. A failed read shows
// nothing: Home stays usable.
import Link from "next/link";
import { K, KpiTile } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import type { WebConfig } from "@/lib/config";
import { crmCall } from "@/lib/crmApi";
import { formatDate, type DateLocale } from "@/lib/dates";
import { type AgendaView, departureHref } from "@/lib/finance";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import type { Session } from "@/lib/session";

export async function AgendaTiles({ cfg, session, lang, locale }: { cfg: WebConfig; session: Session; lang: Lang; locale: DateLocale }) {
  const view = await crmCall<AgendaView>(cfg, session, "GET", "reports/agenda");
  if (!view.ok) return null;
  const a = view.data;
  const tr = (key: string) => t(lang, key as MessageKey);
  const tile = (testId: string, label: string, value: number, href: string, tone?: "warning" | "danger", hint?: string) => (
    <Link href={href} style={{ textDecoration: "none", color: "inherit", minWidth: 0 }}>
      <KpiTile size="sm" testId={testId} label={label} value={String(value)} valueTone={value > 0 ? tone : undefined} hint={hint} />
    </Link>
  );
  return (
    <Panel title={tr("agenda.title")} testId="crm-home-agenda">
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 10 }}>
        {tile("agenda-proposals", tr("agenda.openProposals"), a.open_proposals, "/pipeline")}
        {tile("agenda-deposits", tr("agenda.depositsDue"), a.deposits_due_week, "/payments", "warning")}
        {tile("agenda-overdue", tr("agenda.overdue"), a.overdue_instalments, "/payments", "danger")}
        {tile("agenda-passports", tr("agenda.passports"), a.passport_missing, "/travelling", "warning", fmt(tr("agenda.passportsHint"), { days: a.passport_days }))}
        {a.aa_overdue_invoices !== null && tile("agenda-aa-overdue", tr("agenda.aaOverdue"), a.aa_overdue_invoices, "/aa-bookings", "danger")}
      </div>
      <h3 style={{ margin: "14px 0 6px", fontSize: 12.5, color: K.muted, fontWeight: 600 }}>{tr("agenda.nextDepartures")}</h3>
      {a.next_departures.length === 0 ? (
        <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{fmt(tr("agenda.noDepartures"), { days: a.departure_days })}</p>
      ) : (
        <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 6, fontSize: 13 }} data-testid="agenda-departures">
          {a.next_departures.map((d) => (
            <li key={d.departure_ref} style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <Link href={departureHref(d.departure_ref)}>{d.departure_ref}</Link>
              <span style={{ color: K.muted }}>
                {fmt(tr("agenda.departure"), { date: d.start_date ? formatDate(d.start_date, locale) : "—", trips: d.trips, pax: d.pax })}
              </span>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}
