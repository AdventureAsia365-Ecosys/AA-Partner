// web/app/(crm)/payments/page.tsx — A-10 Payments, Receivables (card CRM-23 slice 2; design v3 M7, WF-9): the
// agency's invoices by due date with a status filter (default: issued and not paid), overdue ones flagged, each linked
// to its trip, with what is still owed. Amounts are the client's sell amounts; every role reads (the API decides).
// Invoices are raised and payments recorded on the trip's Invoices card. Slice 3: the refund requests to act on.
// AA-957 S262 round 3: the kit DataTable (a card per invoice on a phone) and the kit underline tabs for the filter,
// as on /commissions (components/invoices/ReceivablesTable.tsx).
import { ErrorState, K } from "@/app/_kit";
import { ReceivableFilters, ReceivablesTable } from "@/components/invoices/ReceivablesTable";
import { RefundsPanel } from "@/components/invoices/RefundsPanel";
import { PageTitle } from "@/components/PageTitle";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { t, type MessageKey } from "@/lib/i18n";
import { STATUSES, type InvoiceStatus, type ReceivablesView, type RefundRequest } from "@/lib/invoices";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

export default async function PaymentsPage({ searchParams }: { searchParams: Promise<{ status?: string }> }) {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  if (!session) return null;
  const { status: raw } = await searchParams;
  const status = STATUSES.includes(raw as InvoiceStatus) ? (raw as InvoiceStatus) : null;
  const query = status ? new URLSearchParams({ status }) : undefined;
  const [view, refunds] = await Promise.all([
    crmCall<ReceivablesView>(ctx.cfg, session, "GET", "invoices", { query }),
    crmCall<{ refund_requests: RefundRequest[] }>(ctx.cfg, session, "GET", "refund-requests"),
  ]);
  const tr = (key: string) => t(ctx.lang, key as MessageKey);
  if (!view.ok) return <ErrorState message={tr("error.api")} />;
  return (
    // minmax(0, 1fr): the table's min width never stretches the page's grid (S254 lesson); room for the toast on a phone.
    <div data-testid="crm-payments" data-crm-toast-room="" style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 14, maxWidth: 1100 }}>
      <PageTitle title={tr("receivables.title")} subtitle={tr("receivables.subtitle")} />
      <ReceivableFilters status={status} lang={ctx.lang} />
      {view.data.invoices.length === 0 ? (
        <p style={{ margin: 0, fontSize: 13, color: K.muted }} data-testid="receivables-empty">
          {tr(status ? "receivables.emptyFilter" : "receivables.empty")}
        </p>
      ) : (
        <ReceivablesTable invoices={view.data.invoices} lang={ctx.lang} locale={ctx.locale} timeZone={view.data.timezone ?? "UTC"} />
      )}
      {refunds.ok && <RefundsPanel refunds={refunds.data.refund_requests} role={ctx.role} lang={ctx.lang} locale={ctx.locale} />}
    </div>
  );
}
