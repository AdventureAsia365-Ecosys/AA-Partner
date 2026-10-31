"use client";
// web/components/reports/ReportTable.tsx — a small read-only report table on the kit DataTable (AA-966). The server page
// formats every cell to text first (numbers in the person's locale, money with its currency), so this component gets
// plain strings: the first column is a label, the others are numbers aligned right. Sorting is the DataTable's own.
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
}: {
  tableId: string;
  head: string[];
  rows: ReportRow[];
  empty: string;
}) {
  const columns = useMemo<ColumnDef<ReportRow, unknown>[]>(
    () =>
      head.map((h, i) => ({
        id: `c${i}`,
        header: h,
        accessorFn: (r: ReportRow) => r.cells[i],
        enableSorting: false,
        meta: i === 0 ? undefined : { align: "right" },
        cell: ({ row }) => {
          const text = row.original.cells[i] ?? "";
          if (i === 0 && row.original.href) return <a href={row.original.href}>{text}</a>;
          return i === 0 ? (
            <span style={{ overflowWrap: "anywhere" }}>{text}</span>
          ) : (
            <span style={{ fontVariantNumeric: "tabular-nums", whiteSpace: "nowrap" }}>{text}</span>
          );
        },
      })),
    [head],
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
        emptyTitle={empty}
      />
    </div>
  );
}
