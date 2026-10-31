// web/app/(crm)/payments/page.tsx — A-10 Payments, Receivables (card CRM-23 slice 2; design v3 M7, WF-9): the
// agency's invoices by due date with a status filter (default: issued and not paid), overdue ones flagged, each linked
// to its trip, with what is still owed. Amounts are the client's sell amounts; every role reads (the API decides).
// Invoices are raised and payments recorded on the trip's Invoices card. Slice 3: the refund requests to act on.
import Link from "next/link";
import { Badge, ErrorState, K } from "@/app/_kit";
import { RefundsPanel } from "@/components/invoices/RefundsPanel";
import { PageTitle } from "@/components/PageTitle";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { formatDate } from "@/lib/dates";
import { fmt, t, type MessageKey } from "@/lib/i18n";
import { claimNote, STATUSES, STATUS_TONE, type InvoiceStatus, type ReceivablesView, type RefundRequest } from "@/lib/invoices";
import { formatAmount } from "@/lib/proposals";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

const row: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "minmax(110px, 1fr) minmax(140px, 2fr) minmax(110px, 1fr) minmax(110px, 1fr) minmax(120px, 1fr)",
  gap: "4px 12px",
  alignItems: "center",
  padding: "8px 12px",
  borderTop: `1px solid ${K.line}`,
  fontSize: 13,
};

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
  const filters: (InvoiceStatus | null)[] = [null, "draft", "paid", "void"];
  return (
    <div data-testid="crm-payments" style={{ display: "grid", gap: 14, maxWidth: 1100 }}>
      <PageTitle title={tr("receivables.title")} subtitle={tr("receivables.subtitle")} />
      <nav style={{ display: "flex", gap: 12, fontSize: 13, flexWrap: "wrap" }} aria-label={tr("receivables.filter")}>
        {filters.map((s) => (
          <Link key={s ?? "open"} href={s ? `/payments?status=${s}` : "/payments"} aria-current={status === s ? "page" : undefined}>
            {s ? tr(`invoice.status.${s}`) : tr("receivables.unpaid")}
          </Link>
        ))}
      </nav>
      {view.data.invoices.length === 0 ? (
        <p style={{ margin: 0, fontSize: 13, color: K.muted }} data-testid="receivables-empty">
          {tr(status ? "receivables.emptyFilter" : "receivables.empty")}
        </p>
      ) : (
        <div style={{ background: K.card, border: `1px solid ${K.line}`, borderRadius: 10, overflowX: "auto" }} data-testid="receivables">
          <div style={{ ...row, borderTop: "none", color: K.muted, fontSize: 11.5, fontWeight: 600, minWidth: 640 }} aria-hidden="true">
            <span>{tr("receivables.col.number")}</span>
            <span>{tr("receivables.col.trip")}</span>
            <span>{tr("receivables.col.amount")}</span>
            <span>{tr("receivables.col.due")}</span>
            <span>{tr("receivables.col.status")}</span>
          </div>
          {view.data.invoices.map((inv) => (
            <div key={inv.invoice_id} style={{ ...row, minWidth: 640 }} data-testid="receivable-row" data-overdue={inv.overdue ? "true" : undefined}>
              <span>
                <strong>{inv.number ?? tr("invoice.draftNumber")}</strong>
                <span style={{ display: "block", color: K.muted, fontSize: 12 }}>{tr(`invoice.kind.${inv.kind}`)}</span>
              </span>
              <Link href={`/pipeline/${inv.trip_id}`} style={{ overflowWrap: "anywhere" }}>
                {inv.trip_title}
              </Link>
              <span>{formatAmount(inv.balance_amount ?? inv.total_amount, inv.currency, ctx.locale)}</span>
              <span style={{ color: inv.overdue ? K.danger : undefined }}>
                {inv.due_on ? formatDate(inv.due_on, ctx.locale) : "—"}
                {inv.overdue && <span style={{ display: "block", fontSize: 12 }}>{tr("receivables.overdue")}</span>}
              </span>
              <span>
                <Badge tone={STATUS_TONE[inv.status]}>{tr(`invoice.status.${inv.status}`)}</Badge>
                {(() => {
                  // AA-952 slice 4 (WF-9): "Client says paid; verify before <time>", a late deposit being re-held, ...
                  const note = claimNote(inv);
                  if (!note) return null;
                  const until = note.vars.until ? new Date(note.vars.until).toLocaleString(ctx.locale, { dateStyle: "medium", timeStyle: "short" }) : "";
                  const code = note.vars.code !== undefined ? tr(`invoice.rehold.${note.vars.code || "other"}`) : "";
                  return (
                    <span data-testid="receivable-claim-note" style={{ display: "block", fontSize: 12, color: K.muted }}>
                      {fmt(tr(note.key), { until, code })}
                    </span>
                  );
                })()}
              </span>
            </div>
          ))}
        </div>
      )}
      {refunds.ok && <RefundsPanel refunds={refunds.data.refund_requests} role={ctx.role} lang={ctx.lang} locale={ctx.locale} />}
    </div>
  );
}
