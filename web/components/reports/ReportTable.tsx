"use client";
// web/components/reports/ReportTable.tsx — a small read-only report table on the kit DataTable (AA-966). The server page
// formats every cell to text first (numbers in the person's locale, money with its currency), so this component gets
// plain strings: the first column is a label, the others are numbers aligned right. Sorting is the DataTable's own.
// AA-1070: `textColumns` names the other columns that hold words (a trip title, a status): they sit left and wrap, so a
// long title does not widen a phone card past the screen (a number cell never wraps).
import type { ColumnDef } from "@tanstack/react-table";
import { useMemo } from "react";
import { DataTable } from "@/app/_kit";

export interface ReportRow {
  key: string;
  cells: string[];
  href?: string | null;
}

export function ReportTable({
  tableId,
  head,
  rows,
  empty,
  textColumns = [],
}: {
  tableId: string;
  head: string[];
  rows: ReportRow[];
  empty: string;
  /** Indexes of the columns after the first that hold text, not numbers. */
  textColumns?: readonly number[];
}) {
  const columns = useMemo<ColumnDef<ReportRow, unknown>[]>(
    () =>
      head.map((h, i) => ({
        id: `c${i}`,
        header: h,
        accessorFn: (r: ReportRow) => r.cells[i],
        enableSorting: false,
        meta: i === 0 || textColumns.includes(i) ? undefined : { align: "right" },
        cell: ({ row }) => {
          const text = row.original.cells[i] ?? "";
          if (i === 0 && row.original.href) return <a href={row.original.href}>{text}</a>;
          return i === 0 || textColumns.includes(i) ? (
            <span style={{ overflowWrap: "anywhere" }}>{text}</span>
          ) : (
            <span style={{ fontVariantNumeric: "tabular-nums", whiteSpace: "nowrap" }}>{text}</span>
          );
        },
      })),
    [head, textColumns],
  );
  return (
    <div data-testid={`report-table-${tableId}`} style={{ minWidth: 0 }}>
      <DataTable<ReportRow>
        data={rows}
        columns={columns}
        tableId={`reports-${tableId}`}
        getRowId={(r) => r.key}
        searchable={false}
        paginate={false}
        compact
        tableMinWidth={Math.max(320, head.length * 110)}
        emptyTitle={empty}
      />
    </div>
  );
}
