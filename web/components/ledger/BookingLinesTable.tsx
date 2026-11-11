"use client";
// web/components/ledger/BookingLinesTable.tsx — the A-11 Bookings list (AA-944, card CRM-16) on the kit DataTable
// (AA-1008, S6f: no raw <table> in page code). One row a booking line: service day, trip (a link), item, supplier,
// confirmation number, status, sell (its own currency), the rate reading and who collects. On a phone each line is a
// card (the kit's phone card mode), never columns cut off to the right.
import type { ColumnDef } from "@tanstack/react-table";
import Link from "next/link";
import { Badge, DataTable } from "@/app/_kit";
import { formatDate, type DateLocale } from "@/lib/dates";
import { t, type Lang, type MessageKey } from "@/lib/i18n";
import { formatMoney, fxReading, type Line } from "@/lib/ledger";

const TONE = { planning: "neutral", requested: "info", confirmed: "success", cancelled: "danger" } as const;

/** The width the nine columns need before the wrapper scrolls (the 820 px the raw table had). */
export const BOOKING_LINES_MIN_WIDTH = 820;

export function BookingLinesTable({ lines, lang, locale }: { lines: Line[]; lang: Lang; locale: DateLocale }) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const columns: ColumnDef<Line, unknown>[] = [
    {
      id: "day",
      header: tr("ledger.col.day"),
      accessorFn: (l) => l.service_from ?? "",
      cell: ({ row }) => (row.original.service_from ? formatDate(row.original.service_from, locale) : "—"),
    },
    {
      id: "trip",
      header: tr("ledger.col.trip"),
      accessorFn: (l) => l.trip_title ?? "",
      cell: ({ row }) => <Link href={`/pipeline/${row.original.trip_id}`}>{row.original.trip_title}</Link>,
    },
    { id: "item", header: tr("ledger.col.item"), accessorFn: (l) => l.title },
    { id: "supplier", header: tr("ledger.col.supplier"), accessorFn: (l) => l.supplier_name },
    { id: "conf", header: tr("ledger.col.conf"), accessorFn: (l) => l.confirmation_no ?? "—" },
    {
      id: "status",
      header: tr("ledger.col.status"),
      accessorFn: (l) => l.status,
      cell: ({ row }) => <Badge tone={TONE[row.original.status]}>{tr(`ledger.status.${row.original.status}`)}</Badge>,
    },
    {
      id: "sell",
      header: tr("ledger.col.sell"),
      enableSorting: false,
      cell: ({ row }) => {
        const sell = row.original.money?.sell;
        return sell ? formatMoney(sell.amount, sell.currency, locale) : "—";
      },
    },
    { id: "fx", header: tr("ledger.col.fx"), enableSorting: false, cell: ({ row }) => fxReading(row.original.money?.sell) ?? "—" },
    { id: "collect", header: tr("ledger.col.collect"), accessorFn: (l) => tr(`ledger.collect.${l.collect}`) },
  ];
  return (
    <div data-testid="bookings-lines" style={{ minWidth: 0 }}>
      <DataTable<Line>
        data={lines}
        columns={columns}
        tableId="crm-bookings-lines"
        getRowId={(l) => l.booking_line_id}
        searchable={false}
        compact
        paginate={false}
        tableMinWidth={BOOKING_LINES_MIN_WIDTH}
        emptyTitle={tr("ledger.empty")}
      />
    </div>
  );
}
