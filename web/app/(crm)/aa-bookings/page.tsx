// web/app/(crm)/aa-bookings/page.tsx — AA-1068 (crm-26): "Bookings with AA". The agency's AA bookings (its AA lines with
// a hold or booking: trip, status, the quoted B2B price and what AA invoiced, with a flag when AA billed a booking in full
// at another amount), AA's invoices to the agency (software fee and tours: status, total, balance, due day, overdue) and
// the billing status, with a banner while AA's credit status is warn / past due (new AA holds pause when past due).
// Read live from AA on every open (GET /aa-bookings); when AA cannot answer, the CRM's last copy is shown and the page
// says when it was read. Owner and agent only: the B2B price is the agency's AA cost (the API refuses the assistant).
import Link from "next/link";
import { Badge, EmptyState, ErrorState, K, KpiTile } from "@/app/_kit";
import { Panel, PageTitle } from "@/components/PageTitle";
import { ReportTable } from "@/components/reports/ReportTable";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { formatDate } from "@/lib/dates";
import { type AaBookingRow, type AaBookingsView, canSeeMoney } from "@/lib/finance";
import { fmt, t, type MessageKey } from "@/lib/i18n";
import { formatMoney } from "@/lib/ledger";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

const BANNER_TONE = { past_due: K.danger, warn: K.amber, overdue: K.amber } as const;

export default async function AaBookingsPage() {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const tr = (key: string) => t(ctx.lang, key as MessageKey);
  if (!canSeeMoney(ctx.role)) {
    return (
      <div data-testid="crm-section-forbidden">
        <PageTitle title={tr("nav.aaBookings")} />
        <EmptyState title={tr("section.forbidden.title")} description={tr("section.forbidden.body")} />
      </div>
    );
  }
  const { session } = await serverContext();
  if (!session) return null;
  const view = await crmCall<AaBookingsView>(ctx.cfg, session, "GET", "aa-bookings", { timeoutMs: 30_000 });
  if (!view.ok) return <ErrorState message={tr("error.api")} />;
  const v = view.data;
  const { locale } = ctx;
  const money = (amount: string | null | undefined, cur: string) => formatMoney(amount, cur, locale);
  const check = (b: AaBookingRow) =>
    !b.invoiced_b2b ? tr("aaBookings.notInvoiced") : b.mismatch ? tr("aaBookings.mismatch") : b.invoiced_b2b.complete ? tr("aaBookings.match") : tr("aaBookings.partial");
  const overdue = v.invoices.filter((i) => i.overdue);
  return (
    <div data-testid="crm-aa-bookings" style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 14, maxWidth: 1200 }}>
      <PageTitle title={tr("aaBookings.title")} subtitle={tr("aaBookings.subtitle")} />
      {v.banner && (
        <div
          role="alert"
          data-testid="aa-billing-banner"
          style={{ border: `1px solid ${BANNER_TONE[v.banner.level]}`, borderRadius: 10, padding: "10px 14px", fontSize: 13.5, display: "grid", gap: 4 }}
        >
          <strong style={{ color: BANNER_TONE[v.banner.level] }}>
            {v.banner.level === "overdue"
              ? fmt(tr("aaBookings.banner.overdue"), { n: v.banner.overdue_invoices, days: v.banner.oldest_overdue_days })
              : tr(`aaBookings.banner.${v.banner.level}`)}
          </strong>
          {v.banner.overdue_b2b.length > 0 && (
            <span>{fmt(tr("aaBookings.banner.amount"), { amounts: v.banner.overdue_b2b.map((a) => money(a.amount, a.currency)).join(", ") })}</span>
          )}
        </div>
      )}
      {v.source === "cache" && (
        <p style={{ margin: 0, fontSize: 12.5, color: K.muted }} data-testid="aa-bookings-cache">
          {v.booking_error === "aa_partner_not_active"
            ? tr("aaBookings.source.notActive")
            : v.refreshed_at
              ? fmt(tr("aaBookings.source.cache"), { when: formatDate(v.refreshed_at.slice(0, 10), locale) })
              : tr("aaBookings.source.never")}
        </p>
      )}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 12 }}>
        <KpiTile size="sm" testId="aa-bookings-count" label={tr("aaBookings.bookings")} value={String(v.bookings.length)} />
        <KpiTile
          size="sm"
          testId="aa-bookings-mismatch"
          label={tr("aaBookings.mismatch")}
          value={String(v.mismatches)}
          valueTone={v.mismatches > 0 ? "warning" : undefined}
        />
        <KpiTile
          size="sm"
          testId="aa-bookings-overdue"
          label={tr("agenda.aaOverdue")}
          value={String(overdue.length)}
          valueTone={overdue.length > 0 ? "danger" : undefined}
          hint={v.billing_status?.paid_through ? fmt(tr("aaBookings.paidThrough"), { date: formatDate(v.billing_status.paid_through, locale) }) : undefined}
        />
      </div>
      <Panel title={tr("aaBookings.bookings")} testId="aa-bookings-table">
        {v.bookings.length === 0 ? (
          <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{tr("aaBookings.none")}</p>
        ) : (
          <ReportTable
            tableId="aa-bookings"
            head={[tr("aaBookings.col.trip"), tr("aaBookings.col.item"), tr("aaBookings.col.booking"), tr("aaBookings.col.quoted"), tr("aaBookings.col.invoiced"), tr("aaBookings.col.check")]}
            rows={v.bookings.map((b) => ({
              key: b.booking_line_id,
              href: `/pipeline/${b.trip_id}`,
              cells: [
                b.trip_title,
                `${b.title} · ${tr(`ledger.status.${b.status}`)}`,
                b.booking_id ?? b.hold_id ?? "—",
                b.quoted_b2b ? money(b.quoted_b2b.amount, b.quoted_b2b.currency) : "—",
                b.invoiced_b2b ? money(b.invoiced_b2b.amount, b.invoiced_b2b.currency) : "—",
                check(b),
              ],
            }))}
            empty={tr("aaBookings.none")}
          />
        )}
      </Panel>
      <Panel title={tr("aaBookings.invoices")} testId="aa-invoices-table">
        {v.invoices.length === 0 ? (
          <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{tr("aaBookings.noInvoices")}</p>
        ) : (
          <div style={{ display: "grid", gap: 8 }}>
            {v.invoices.map((i) => (
              <div key={i.number} data-testid="aa-invoice" style={{ border: `1px solid ${K.line}`, borderRadius: 8, padding: "8px 10px", fontSize: 13, display: "grid", gap: 4 }}>
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", justifyContent: "space-between" }}>
                  <span>
                    <strong>{i.number}</strong> · {tr(`aaBookings.kind.${i.invoice_kind}`)} · {formatDate(i.issued_on, locale)}
                  </span>
                  <span style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                    <Badge tone={i.status === "paid" ? "success" : i.status === "void" ? "neutral" : "info"}>{tr(`aaBookings.status.${i.status}`)}</Badge>
                    {i.overdue && <Badge tone="danger">{fmt(tr("aaBookings.overdueDays"), { days: i.overdue_days })}</Badge>}
                  </span>
                </div>
                <span style={{ fontVariantNumeric: "tabular-nums" }}>
                  {tr("aaBookings.col.total")}: {money(i.b2b_total, i.currency)} · {tr("aaBookings.col.balance")}: {money(i.b2b_balance, i.currency)} ·{" "}
                  {tr("aaBookings.col.due")}: {formatDate(i.due_on, locale)}
                </span>
                {i.bookings.map((b) => (
                  <span key={b.booking_id} style={{ fontSize: 12.5, color: K.muted }}>
                    {b.booking_id}: {money(b.b2b_amount, i.currency)}
                    {b.lines.map((l) => (
                      <span key={l.booking_line_id}>
                        {" · "}
                        <Link href={`/pipeline/${l.trip_id}`}>{l.trip_title}</Link>
                      </span>
                    ))}
                  </span>
                ))}
              </div>
            ))}
          </div>
        )}
      </Panel>
    </div>
  );
}
