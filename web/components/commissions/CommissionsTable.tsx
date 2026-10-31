"use client";
// web/components/commissions/CommissionsTable.tsx — the A-12 list (card CRM-27, Module 13) on the kit DataTable. S262
// (AA-957 live acceptance): on a phone each commission is a card (the kit's phone card mode) with its status and its
// follow-up in view, never columns cut off to the right; the trip shows its dates under the title (two trips of one
// title are told apart); a receipt in another currency reads as its pair ("1 EUR = 1.0870 USD on 28 Oct 2026") with
// the amount it counts as in the base currency and the bank reference; a written-off row shows its note.
import type { ColumnDef } from "@tanstack/react-table";
import Link from "next/link";
import { Badge, DataTable, K } from "@/app/_kit";
import { CommissionActions } from "@/components/commissions/CommissionActions";
import { type Commission, rateText, STATUS_TONE, tripWhen } from "@/lib/commissions";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import { formatAmount } from "@/lib/proposals";

const small: React.CSSProperties = { display: "block", fontSize: 12, color: K.muted, overflowWrap: "anywhere" };

/** The Received cell: the amount and day, the rate as a pair, the base amount it counts as, the bank reference. */
export function ReceivedCell({ c, base, lang, locale }: { c: Commission; base: string; lang: Lang; locale: DateLocale }) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const money = (amount: string | null | undefined, currency: string) => formatAmount(amount, currency, locale);
  const r = c.commission_received;
  if (!r) return <span>—</span>;
  const baseCurrency = r.base_currency ?? base;
  return (
    <span data-testid="commission-received" style={{ display: "block", minWidth: 0 }}>
      <span style={{ display: "block" }}>{money(r.amount, r.currency)}</span>
      <span style={small}>{formatDate(r.received_on, locale)}</span>
      {r.currency !== baseCurrency && (
        <>
          <span style={small} data-testid="commission-rate">
            {fmt(tr("commission.rate"), { pair: rateText(r.fx.rate, r.currency, baseCurrency), date: formatDate(r.fx.rate_date, locale) })}
          </span>
          {r.amount_base && <span style={small}>{fmt(tr("commission.inBase"), { amount: money(r.amount_base, baseCurrency) })}</span>}
        </>
      )}
      {r.reference && <span style={small}>{fmt(tr("commission.bankRef"), { ref: r.reference })}</span>}
      {r.short_amount && <span style={{ ...small, color: K.danger }}>{fmt(tr("commission.short"), { amount: money(r.short_amount, r.currency) })}</span>}
      {r.fx_difference_base && r.fx_difference_base !== "0.00" && (
        <span style={small}>{fmt(tr("commission.fxDifference"), { amount: money(r.fx_difference_base, baseCurrency) })}</span>
      )}
    </span>
  );
}

/** The Status cell: the badge, then what explains it (the claim on an open row, the reason and note of a closed one). */
export function StatusCell({ c, lang }: { c: Commission; lang: Lang }) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const open = c.status === "expected" || c.status === "claimed";
  return (
    <span style={{ display: "grid", gap: 4, justifyItems: "start", minWidth: 0 }}>
      <Badge tone={c.overdue ? "danger" : STATUS_TONE[c.status]}>{tr(`commission.status.${c.status}`)}</Badge>
      {open && c.claim_ref && <span style={small}>{fmt(tr("commission.claimRefShown"), { ref: c.claim_ref })}</span>}
      {c.close_reason && <span style={small}>{tr(`commission.closeReason.${c.close_reason}`)}</span>}
      {c.note && <span style={small} data-testid="commission-note">{fmt(tr("commission.noteShown"), { note: c.note })}</span>}
      {c.keep_on_cancel && <span style={small}>{tr("commission.kept")}</span>}
    </span>
  );
}

export function CommissionsTable({ commissions, base, lang, locale }: { commissions: Commission[]; base: string; lang: Lang; locale: DateLocale }) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const money = (amount: string | null | undefined, currency: string) => formatAmount(amount, currency, locale);
  const columns: ColumnDef<Commission, unknown>[] = [
    {
      id: "supplier",
      header: tr("commissions.col.supplier"),
      accessorFn: (c) => c.supplier_name,
      cell: ({ row }) => (
        <span data-testid="commission-row" data-status={row.original.status} data-overdue={row.original.overdue ? "true" : undefined} style={{ display: "block", minWidth: 0, overflowWrap: "anywhere" }}>
          <strong>{row.original.supplier_name}</strong>
          <span style={{ ...small, fontWeight: 400 }}>{row.original.line_title}</span>
        </span>
      ),
    },
    {
      id: "trip",
      header: tr("commissions.col.trip"),
      accessorFn: (c) => c.trip_start_date ?? c.trip_month ?? "",
      cell: ({ row }) => {
        const when = tripWhen(row.original, locale);
        return (
          <span style={{ display: "block", minWidth: 0 }}>
            <Link href={`/pipeline/${row.original.trip_id}`} style={{ overflowWrap: "anywhere" }} data-testid="commission-trip">
              {row.original.trip_title}
            </Link>
            {when && <span style={small} data-testid="commission-trip-when">{when}</span>}
          </span>
        );
      },
    },
    {
      id: "expected",
      header: tr("commissions.col.expected"),
      enableSorting: false,
      cell: ({ row }) => (row.original.commission_expected ? money(row.original.commission_expected.amount, row.original.commission_expected.currency) : "—"),
    },
    {
      id: "received",
      header: tr("commissions.col.received"),
      enableSorting: false,
      cell: ({ row }) => <ReceivedCell c={row.original} base={base} lang={lang} locale={locale} />,
    },
    {
      id: "due",
      header: tr("commissions.col.due"),
      accessorFn: (c) => c.due_on,
      cell: ({ row }) => (
        <span style={{ display: "block", color: row.original.overdue ? K.danger : undefined }}>
          {formatDate(row.original.due_on, locale)}
          {row.original.overdue && <span style={{ display: "block", fontSize: 12 }}>{fmt(tr("commission.overdueDays"), { n: row.original.days_overdue })}</span>}
        </span>
      ),
    },
    {
      id: "status",
      header: tr("commissions.col.status"),
      enableSorting: false,
      cell: ({ row }) => <StatusCell c={row.original} lang={lang} />,
    },
    {
      id: "actions",
      header: tr("commission.col.actions"),
      enableSorting: false,
      meta: { hideLabelOnCard: true },
      cell: ({ row }) => (
        // Full width on a phone card (the kit right-aligns a label-less cell): an open form wraps inside the card.
        <div style={{ width: "100%", minWidth: 0 }}>
          <CommissionActions commission={row.original} lang={lang} locale={locale} />
        </div>
      ),
    },
  ];
  return (
    <div data-testid="commissions-table" style={{ minWidth: 0 }}>
      <DataTable<Commission>
        data={commissions}
        columns={columns}
        tableId="crm-commissions"
        getRowId={(c) => c.commission_id}
        searchable={false}
        compact
        tableMinWidth={980}
        emptyTitle={tr("commissions.emptyFilter")}
      />
    </div>
  );
}
