"use client";
// web/components/staff/AgencyTrips.tsx — S-04, the agency's trips inside a live support / emergency session (AA-1019 on
// GET /crm/staff/v1/agencies/{id}/trips): no title, no names; newest activity first, keyset paging ("Load more" asks the
// next page; each page is one support_read event in the agency's access log, naming the trips shown).
import type { ColumnDef } from "@tanstack/react-table";
import Link from "next/link";
import { useMemo, useState } from "react";
import { Button, DataTable, K, useHydrated } from "@/app/_kit";
import { codeLabel } from "@/lib/accessLog";
import { formatDate, type DateLocale } from "@/lib/dates";
import { t, type Lang, type MessageKey } from "@/lib/i18n";
import { refusalKey, TRIPS_PAGE, type StaffTrip, type TripsPage } from "@/lib/staffConsole";
import { staffSend } from "@/lib/staffClient";

const mono = "ui-monospace, SFMono-Regular, Menlo, monospace";

export function AgencyTrips({ tenantId, first, lang, locale }: { tenantId: string; first: TripsPage; lang: Lang; locale: DateLocale }) {
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [items, setItems] = useState<StaffTrip[]>(first.items);
  const [cursor, setCursor] = useState<string | null>(first.next_cursor);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const day = (iso: string | null) => (iso ? formatDate(iso.slice(0, 10), locale) : "—");
  const columns = useMemo<ColumnDef<StaffTrip, unknown>[]>(
    () => [
      {
        id: "trip",
        header: tr("staff.agency.trips"),
        enableSorting: false,
        cell: ({ row }) => (
          <Link href={`/staff/support/${tenantId}/trips/${row.original.trip_id}`} prefetch={false} style={{ fontFamily: mono }}>
            {row.original.trip_id.slice(0, 8)}
          </Link>
        ),
      },
      { id: "stage", header: tr("staff.trip.stage"), accessorFn: (r) => codeLabel(lang, "stage", r.stage) },
      { id: "waiting", header: tr("staff.trip.waiting"), accessorFn: (r) => codeLabel(lang, "waiting", r.waiting_on) },
      { id: "dates", header: tr("staff.trip.dates"), enableSorting: false, accessorFn: (r) => (r.start_date ? `${day(r.start_date)} – ${day(r.end_date)}` : r.month ?? "—") },
      { id: "party", header: tr("staff.trip.party"), enableSorting: false, accessorFn: (r) => `${r.party_adults} + ${r.party_children}` },
      { id: "destinations", header: tr("staff.trip.destinations"), enableSorting: false, accessorFn: (r) => r.destinations.join(", ") || "—" },
      { id: "activity", header: tr("staff.trip.activity"), accessorFn: (r) => r.last_activity_at ?? "", cell: ({ row }) => day(row.original.last_activity_at) },
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps -- labels follow lang/locale only
    [lang, locale, tenantId],
  );

  async function more() {
    if (!cursor) return;
    setBusy(true);
    setError(null);
    const q = new URLSearchParams({ cursor, limit: String(TRIPS_PAGE) });
    const res = await staffSend<TripsPage>("GET", `agencies/${tenantId}/trips`, { query: q });
    setBusy(false);
    if (!res.ok) {
      setError(tr(refusalKey(res.detail)));
      return;
    }
    setItems((now) => [...now, ...res.data.items]);
    setCursor(res.data.next_cursor);
  }

  return (
    <div style={{ display: "grid", gap: 10, gridTemplateColumns: "minmax(0, 1fr)" }} data-testid="staff-agency-trips">
      <DataTable<StaffTrip>
        tableId="staff-agency-trips"
        data={items}
        columns={columns}
        getRowId={(r) => r.trip_id}
        searchable={false}
        paginate={false}
        compact
        tableMinWidth={760}
        emptyTitle={tr("staff.agency.tripsEmpty")}
      />
      {cursor && (
        <div>
          <Button size="sm" disabled={!hydrated || busy} onClick={more}>
            {tr("staff.agency.more")}
          </Button>
        </div>
      )}
      {error && (
        <p role="alert" style={{ margin: 0, fontSize: 13, color: K.danger }}>
          {error}
        </p>
      )}
    </div>
  );
}
