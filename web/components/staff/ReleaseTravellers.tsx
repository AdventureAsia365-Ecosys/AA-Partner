"use client";
// web/components/staff/ReleaseTravellers.tsx — S-02, the travellers of one release (AA-1019): the minimal fields the
// agency shares with AA ground operations (name, age band on the departure date, dietary / mobility codes, flights with
// their IANA zone, the insurance choice). Never contact data, passport, birth date or notes: the API never sends them.
import type { ColumnDef } from "@tanstack/react-table";
import { useMemo } from "react";
import { DataTable } from "@/app/_kit";
import { codeLabel } from "@/lib/accessLog";
import { t, type Lang, type MessageKey } from "@/lib/i18n";
import { flightText, travellerName, type ReleaseTraveller } from "@/lib/staffConsole";

export function ReleaseTravellers({ travellers, lang }: { travellers: ReleaseTraveller[]; lang: Lang }) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const list = (codes: string[]) => (codes.length ? codes.map((c) => c.replace(/_/g, " ")).join(", ") : tr("staff.release.none"));
  const columns = useMemo<ColumnDef<ReleaseTraveller, unknown>[]>(
    () => [
      { id: "name", header: tr("staff.release.col.name"), accessorFn: (p) => travellerName(p) },
      { id: "role", header: tr("staff.release.col.role"), accessorFn: (p) => codeLabel(lang, "staff.release.role", p.role) },
      { id: "age", header: tr("staff.release.col.age"), accessorFn: (p) => (p.age_band ?? "—").replace("_", "–") },
      { id: "dietary", header: tr("staff.release.col.dietary"), enableSorting: false, accessorFn: (p) => list(p.dietary) },
      { id: "mobility", header: tr("staff.release.col.mobility"), enableSorting: false, accessorFn: (p) => list(p.mobility) },
      { id: "arrival", header: tr("staff.release.col.arrival"), enableSorting: false, accessorFn: (p) => flightText(p.arrival) || "—" },
      { id: "departure", header: tr("staff.release.col.departure"), enableSorting: false, accessorFn: (p) => flightText(p.departure) || "—" },
      { id: "insurance", header: tr("staff.release.col.insurance"), enableSorting: false, accessorFn: (p) => (p.insurance ?? "—").replace(/_/g, " ") },
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps -- labels follow lang only
    [lang],
  );
  return (
    <DataTable<ReleaseTraveller>
      tableId="staff-release-travellers"
      data={travellers}
      columns={columns}
      getRowId={(p) => p.traveler_id}
      searchable={false}
      paginate={false}
      compact
      tableMinWidth={900}
      emptyTitle={tr("staff.release.none")}
    />
  );
}
