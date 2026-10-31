// web/app/(crm)/commissions/page.tsx — A-12 Commissions (card CRM-27, Module 13; design v3 M13, F10.2, R17): what the
// agency's own suppliers owe it from confirmed bookings, by due date, expected vs received with overdue flags, and the
// follow-up of each row (components/commissions/CommissionActions.tsx). Totals are in the agency's base currency
// (expected frozen at the confirmation day, received at the day it arrived). Owner and agent only: commission is a
// margin field (design §4.6); the assistant gets the "not available for your role" state (the API refuses it too).
// S262 (AA-957 live): the kit DataTable (a card per commission on a phone, status and follow-up in view) and the kit
// underline tabs for the filter (components/commissions/CommissionsTable.tsx, CommissionFilters.tsx).
import { EmptyState, ErrorState, K, KpiTile } from "@/app/_kit";
import { CommissionFilters } from "@/components/commissions/CommissionFilters";
import { CommissionsTable } from "@/components/commissions/CommissionsTable";
import { PageTitle } from "@/components/PageTitle";
import { canFollowUp, type CommissionList, isFilter } from "@/lib/commissions";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { fmt, t, type MessageKey } from "@/lib/i18n";
import { formatAmount } from "@/lib/proposals";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

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
    // minmax(0, 1fr): the table's min width never stretches the page's grid (S254 lesson).
    <div data-testid="crm-commissions" style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 14, maxWidth: 1200 }}>
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
      <CommissionFilters active={status} lang={ctx.lang} />
      {commissions.length === 0 ? (
        <p style={{ margin: 0, fontSize: 13, color: K.muted }} data-testid="commissions-empty">
          {tr(status === "open" ? "commissions.empty" : "commissions.emptyFilter")}
        </p>
      ) : (
        <CommissionsTable commissions={commissions} base={base} lang={ctx.lang} locale={ctx.locale} />
      )}
    </div>
  );
}
