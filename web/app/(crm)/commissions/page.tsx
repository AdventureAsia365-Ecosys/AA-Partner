// web/app/(crm)/commissions/page.tsx — A-12 Commissions (card CRM-27, Module 13; design v3 M13, F10.2, R17): what the
// agency's own suppliers owe it from confirmed bookings, by due date, expected vs received with overdue flags, and the
// follow-up of each row (components/commissions/CommissionActions.tsx). Totals are in the agency's base currency
// (expected frozen at the confirmation day, received at the day it arrived). Owner and agent only: commission is a
// margin field (design §4.6); the assistant gets the "not available for your role" state (the API refuses it too).
import Link from "next/link";
import { Badge, EmptyState, ErrorState, K, KpiTile } from "@/app/_kit";
import { CommissionActions } from "@/components/commissions/CommissionActions";
import { PageTitle } from "@/components/PageTitle";
import { canFollowUp, type CommissionList, FILTERS, isFilter, STATUS_TONE } from "@/lib/commissions";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { formatDate } from "@/lib/dates";
import { fmt, t, type MessageKey } from "@/lib/i18n";
import { formatAmount } from "@/lib/proposals";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

const row: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "minmax(160px, 2fr) minmax(130px, 1.5fr) minmax(100px, 1fr) minmax(120px, 1fr) minmax(100px, 1fr) minmax(170px, 1.5fr)",
  gap: "4px 12px",
  alignItems: "start",
  padding: "10px 12px",
  borderTop: `1px solid ${K.line}`,
  fontSize: 13,
};

export default async function CommissionsPage({ searchParams }: { searchParams: Promise<{ status?: string }> }) {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const tr = (key: string) => t(ctx.lang, key as MessageKey);
  if (!canFollowUp(ctx.role)) {
    return (
      <div data-testid="crm-section-forbidden">
        <PageTitle title={tr("nav.commissions")} />
        <EmptyState title={tr("section.forbidden.title")} description={tr("section.forbidden.body")} />
      </div>
    );
  }
  const { session } = await serverContext();
  if (!session) return null;
  const { status: raw } = await searchParams;
  const status = isFilter(raw) ? raw : "open";
  const view = await crmCall<CommissionList>(ctx.cfg, session, "GET", "commissions", { query: new URLSearchParams({ status }) });
  if (!view.ok) return <ErrorState message={tr("error.api")} />;
  const { commissions, commission_totals: totals, base_currency: base } = view.data;
  const money = (amount: string | null | undefined, currency: string) => formatAmount(amount, currency, ctx.locale);
  return (
    <div data-testid="crm-commissions" style={{ display: "grid", gap: 14, maxWidth: 1200 }}>
      <PageTitle title={tr("commissions.title")} subtitle={tr("commissions.subtitle")} />
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 12 }}>
        <KpiTile size="sm" testId="commissions-open" label={tr("commissions.total.open")} value={money(totals.open_base, base)} hint={fmt(tr("commissions.total.count"), { n: totals.open_count })} />
        <KpiTile
          size="sm"
          testId="commissions-overdue"
          label={tr("commissions.total.overdue")}
          value={money(totals.overdue_base, base)}
          valueTone={totals.overdue_count > 0 ? "danger" : undefined}
          hint={fmt(tr("commissions.total.count"), { n: totals.overdue_count })}
        />
        <KpiTile size="sm" testId="commissions-received" label={tr("commissions.total.received")} value={money(totals.received_base, base)} />
      </div>
      {totals.open_other_base > 0 && <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{fmt(tr("commissions.otherBase"), { n: totals.open_other_base })}</p>}
      <nav style={{ display: "flex", gap: 12, fontSize: 13, flexWrap: "wrap" }} aria-label={tr("commissions.filterLabel")}>
        {FILTERS.map((f) => (
          <Link key={f} href={f === "open" ? "/commissions" : `/commissions?status=${f}`} aria-current={status === f ? "page" : undefined} data-testid={`commissions-filter-${f}`}>
            {tr(`commissions.filter.${f}`)}
          </Link>
        ))}
      </nav>
      {commissions.length === 0 ? (
        <p style={{ margin: 0, fontSize: 13, color: K.muted }} data-testid="commissions-empty">
          {tr(status === "open" ? "commissions.empty" : "commissions.emptyFilter")}
        </p>
      ) : (
        <div style={{ background: K.card, border: `1px solid ${K.line}`, borderRadius: 10, overflowX: "auto" }} data-testid="commissions-table">
          <div style={{ ...row, borderTop: "none", color: K.muted, fontSize: 11.5, fontWeight: 600, minWidth: 860 }} aria-hidden="true">
            <span>{tr("commissions.col.supplier")}</span>
            <span>{tr("commissions.col.trip")}</span>
            <span>{tr("commissions.col.expected")}</span>
            <span>{tr("commissions.col.received")}</span>
            <span>{tr("commissions.col.due")}</span>
            <span>{tr("commissions.col.status")}</span>
          </div>
          {commissions.map((c) => {
            const received = c.commission_received;
            return (
              <div key={c.commission_id} style={{ ...row, minWidth: 860 }} data-testid="commission-row" data-status={c.status} data-overdue={c.overdue ? "true" : undefined}>
                <span style={{ overflowWrap: "anywhere" }}>
                  <strong>{c.supplier_name}</strong>
                  <span style={{ display: "block", color: K.muted, fontSize: 12 }}>{c.line_title}</span>
                </span>
                <Link href={`/pipeline/${c.trip_id}`} style={{ overflowWrap: "anywhere" }}>
                  {c.trip_title}
                </Link>
                <span>{c.commission_expected ? money(c.commission_expected.amount, c.commission_expected.currency) : "—"}</span>
                <span>
                  {received ? money(received.amount, received.currency) : "—"}
                  {received && <span style={{ display: "block", fontSize: 12, color: K.muted }}>{formatDate(received.received_on, ctx.locale)}</span>}
                  {received && received.currency !== received.base_currency && (
                    <span style={{ display: "block", fontSize: 12, color: K.muted }}>
                      {fmt(tr("commission.rate"), { rate: received.fx.rate, date: formatDate(received.fx.rate_date, ctx.locale) })}
                    </span>
                  )}
                  {received?.short_amount && <span style={{ display: "block", fontSize: 12, color: K.danger }}>{fmt(tr("commission.short"), { amount: money(received.short_amount, received.currency) })}</span>}
                  {received?.fx_difference_base && received.fx_difference_base !== "0.00" && (
                    <span style={{ display: "block", fontSize: 12, color: K.muted }}>{fmt(tr("commission.fxDifference"), { amount: money(received.fx_difference_base, received.base_currency ?? base) })}</span>
                  )}
                </span>
                <span style={{ color: c.overdue ? K.danger : undefined }}>
                  {formatDate(c.due_on, ctx.locale)}
                  {c.overdue && <span style={{ display: "block", fontSize: 12 }}>{fmt(tr("commission.overdueDays"), { n: c.days_overdue })}</span>}
                </span>
                <span style={{ display: "grid", gap: 6 }}>
                  <span>
                    <Badge tone={c.overdue ? "danger" : STATUS_TONE[c.status]}>{tr(`commission.status.${c.status}`)}</Badge>
                  </span>
                  {c.claim_ref && <span style={{ fontSize: 12, color: K.muted }}>{fmt(tr("commission.claimRefShown"), { ref: c.claim_ref })}</span>}
                  {c.close_reason && <span style={{ fontSize: 12, color: K.muted }}>{tr(`commission.closeReason.${c.close_reason}`)}</span>}
                  {c.keep_on_cancel && <span style={{ fontSize: 12, color: K.muted }}>{tr("commission.kept")}</span>}
                  <CommissionActions commission={c} lang={ctx.lang} locale={ctx.locale} />
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
