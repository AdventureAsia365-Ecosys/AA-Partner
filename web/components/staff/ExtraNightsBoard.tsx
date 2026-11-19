"use client";
// web/components/staff/ExtraNightsBoard.tsx — AA-1144 (B11): the open requests of every agency for hotel nights
// before or after a tour (API GET /crm/staff/v1/aa-extra-nights, group crm-ops). An agency sells no hotel of its own:
// AA ground operations quote these nights from an AA supplier hotel in Booking and answer the agency. No personal
// data: the agency, a trip reference, the place, the nights, rooms and party size. The API writes one
// `extra_nights_list` event per agency listed.
import type { ColumnDef } from "@tanstack/react-table";
import { useMemo } from "react";
import { DataTable, K } from "@/app/_kit";
import type { ExtraNightRequest } from "@/lib/agencyItems";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";

export interface StaffExtraNight extends ExtraNightRequest {
  tenant_id: string;
  agency_name: string | null;
  trip_ref: string;
}

const mono = "ui-monospace, SFMono-Regular, Menlo, monospace";

export function ExtraNightsBoard({ items, lang, locale }: { items: StaffExtraNight[]; lang: Lang; locale: DateLocale }) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const columns = useMemo<ColumnDef<StaffExtraNight, unknown>[]>(
    () => [
      { id: "agency", header: tr("staff.extraNights.col.agency"), accessorFn: (r) => r.agency_name ?? "—" },
      {
        id: "trip",
        header: tr("staff.extraNights.col.trip"),
        enableSorting: false,
        cell: ({ row }) => <span style={{ fontFamily: mono }}>{row.original.trip_ref}</span>,
      },
      { id: "place", header: tr("staff.extraNights.col.place"), accessorFn: (r) => r.place },
      {
        id: "dates",
        header: tr("staff.extraNights.col.dates"),
        accessorFn: (r) => r.check_in,
        cell: ({ row }) =>
          `${formatDate(row.original.check_in, locale)} – ${formatDate(row.original.check_out, locale)} · ${tr(
            `extraNights.position.${row.original.position}`,
          )}`,
      },
      {
        id: "party",
        header: tr("staff.extraNights.col.party"),
        enableSorting: false,
        cell: ({ row }) =>
          fmt(tr("staff.extraNights.party"), { rooms: row.original.rooms, adults: row.original.adults, children: row.original.children }),
      },
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the words follow lang, the dates locale
    [lang, locale],
  );
  return (
    <section style={{ display: "grid", gap: 8, marginTop: 24 }} data-testid="staff-extra-nights">
      <h2 style={{ margin: 0, fontSize: 16 }}>{tr("staff.extraNights.title")}</h2>
      <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("staff.extraNights.subtitle")}</p>
      <DataTable<StaffExtraNight>
        tableId="staff-extra-nights"
        data={items}
        columns={columns}
        getRowId={(r) => `${r.tenant_id}:${r.request_id}`}
        searchable={false}
        paginate={false}
        compact
        tableMinWidth={720}
        emptyTitle={tr("staff.extraNights.empty")}
      />
    </section>
  );
}
