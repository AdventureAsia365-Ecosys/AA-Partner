"use client";
// web/components/invoices/ReceivablesTable.tsx — the A-10 Payments list (card CRM-23) on the kit DataTable, and its
// status filter on the shared FilterTabs (AA-957 S262 round 3: the /commissions approach). Before, the list was a hand
// grid with a 640 px minimum that clipped "Amount due", Due and Status at 390 px, and the filter was four plain links
// with no visible active mark. Now a phone gets one card per invoice (the kit's phone card mode) and the active filter
// is the kit's marked tab, kept in view.
import type { ColumnDef } from "@tanstack/react-table";
import Link from "next/link";
import { Badge, DataTable, K } from "@/app/_kit";
import { FilterTabs } from "@/components/FilterTabs";
import { formatDate, formatInstant, type DateLocale } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import { claimNote, STATUS_TONE, type InvoiceStatus, type Receivable } from "@/lib/invoices";
import { formatAmount } from "@/lib/proposals";

/** The filters of /payments: unpaid (issued, not paid: the default), draft, paid, void. */
export const RECEIVABLE_FILTERS: readonly (InvoiceStatus | null)[] = [null, "draft", "paid", "void"];

export function receivablesHref(status: InvoiceStatus | null): string {
  return status ? `/payments?status=${status}` : "/payments";
}

/** Four columns plus the status: the content width of a 900 px window beside the sidebar (as A-12). */
export const RECEIVABLES_TABLE_MIN_WIDTH = 560;

const small: React.CSSProperties = { display: "block", fontSize: 12, color: K.muted, overflowWrap: "anywhere" };

export function ReceivableFilters({ status, lang }: { status: InvoiceStatus | null; lang: Lang }) {
  return (
    <FilterTabs
      testId="receivables-filters"
      ariaLabel={t(lang, "receivables.filter")}
      active={status ?? "unpaid"}
      tabs={RECEIVABLE_FILTERS.map((s) => ({
        key: s ?? "unpaid",
        label: s ? t(lang, `invoice.status.${s}` as MessageKey) : t(lang, "receivables.unpaid"),
        href: receivablesHref(s),
      }))}
    />
  );
}

export function ReceivablesTable({ invoices, lang, locale, timeZone }: { invoices: Receivable[]; lang: Lang; locale: DateLocale; timeZone: string }) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const columns: ColumnDef<Receivable, unknown>[] = [
    {
      id: "number",
      header: tr("receivables.col.number"),
      accessorFn: (inv) => inv.number ?? "",
      cell: ({ row }) => (
        <span data-testid="receivable-row" data-overdue={row.original.overdue ? "true" : undefined} style={{ display: "block", minWidth: 0 }}>
          <strong>{row.original.number ?? tr("invoice.draftNumber")}</strong>
          <span style={small}>{tr(`invoice.kind.${row.original.kind}`)}</span>
        </span>
      ),
    },
    {
      id: "trip",
      header: tr("receivables.col.trip"),
      accessorFn: (inv) => inv.trip_title,
      cell: ({ row }) => (
        <Link href={`/pipeline/${row.original.trip_id}`} style={{ overflowWrap: "break-word" }}>
          {row.original.trip_title}
        </Link>
      ),
    },
    {
      id: "amount",
      header: tr("receivables.col.amount"),
      enableSorting: false,
      meta: { align: "right" },
      cell: ({ row }) => formatAmount(row.original.balance_amount ?? row.original.total_amount, row.original.currency, locale),
    },
    {
      id: "due",
      header: tr("receivables.col.due"),
      accessorFn: (inv) => inv.due_on ?? "",
      cell: ({ row }) => (
        <span style={{ color: row.original.overdue ? K.danger : undefined }}>
          {row.original.due_on ? formatDate(row.original.due_on, locale) : "—"}
          {row.original.overdue && <span style={{ display: "block", fontSize: 12 }}>{tr("receivables.overdue")}</span>}
        </span>
      ),
    },
    {
      id: "status",
      header: tr("receivables.col.status"),
      enableSorting: false,
      cell: ({ row }) => {
        const inv = row.original;
        // AA-952 slice 4 (WF-9): "Client says paid; verify before <time>", a late deposit being re-held, ...
        const note = claimNote(inv);
        // The agency timezone with its label (S261 F2), as on the trip's Invoices card.
        const until = note ? formatInstant(note.vars.until, locale, timeZone) : "";
        const code = note && note.vars.code !== undefined ? tr(`invoice.rehold.${note.vars.code || "other"}`) : "";
        return (
          <span style={{ display: "grid", gap: 4, justifyItems: "start", minWidth: 0 }}>
            <Badge tone={STATUS_TONE[inv.status]}>{tr(`invoice.status.${inv.status}`)}</Badge>
            {note && (
              <span data-testid="receivable-claim-note" style={small}>
                {fmt(tr(note.key), { until, code })}
              </span>
            )}
          </span>
        );
      },
    },
  ];
  return (
    <div data-testid="receivables" style={{ minWidth: 0 }}>
      <DataTable<Receivable>
        data={invoices}
        columns={columns}
        tableId="crm-receivables"
        getRowId={(inv) => inv.invoice_id}
        searchable={false}
        compact
        tableMinWidth={RECEIVABLES_TABLE_MIN_WIDTH}
        emptyTitle={tr("receivables.emptyFilter")}
      />
    </div>
  );
}
