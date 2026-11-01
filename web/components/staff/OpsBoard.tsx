"use client";
// web/components/staff/OpsBoard.tsx — S-02, the AA ground operations board (AA-1019 on the AA-956 API; design v3 S-02,
// Fix B3): the releases of every agency departing in the window and the withdrawals of the last 14 days, without any
// personal data (agency, trip reference, dates, party size, completeness counts, the Booking departure ids, the
// deletion countdown, views). "Open" goes to one release (that view is counted and shown to the agency). Incidents are
// flagged in the Booking staff admin (the link above the table), never here.
import type { ColumnDef } from "@tanstack/react-table";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo } from "react";
import { Badge, DataTable, K, SegmentedControl } from "@/app/_kit";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import {
  BOARD_WINDOWS,
  missingItems,
  purgeLabel,
  purgeTone,
  releaseHref,
  type Board,
  type BoardItem,
} from "@/lib/staffConsole";

const mono = "ui-monospace, SFMono-Regular, Menlo, monospace";

export function OpsBoard({ board, days, lang, locale }: { board: Board; days: number; lang: Lang; locale: DateLocale }) {
  const router = useRouter();
  const tr = (key: string) => t(lang, key as MessageKey);
  const day = (iso: string | null) => (iso ? formatDate(iso.slice(0, 10), locale) : "—");
  const columns = useMemo<ColumnDef<BoardItem, unknown>[]>(
    () => [
      {
        id: "agency",
        header: tr("staff.ops.col.agency"),
        accessorFn: (r) => r.agency_name ?? "",
        cell: ({ row }) => (
          <span style={{ display: "grid", gap: 2 }}>
            <span>{row.original.agency_name ?? "—"}</span>
            {row.original.agency_status === "locked" && <Badge tone="warning">{tr("staff.ops.locked")}</Badge>}
          </span>
        ),
      },
      {
        id: "trip",
        header: tr("staff.ops.col.trip"),
        enableSorting: false,
        cell: ({ row }) =>
          row.original.status === "live" ? (
            <Link href={releaseHref(row.original.tenant_id, row.original.trip_id)} prefetch={false} style={{ fontFamily: mono }}>
              {row.original.trip_ref}
            </Link>
          ) : (
            <span style={{ fontFamily: mono, color: K.muted }}>{row.original.trip_ref}</span>
          ),
      },
      {
        id: "dates",
        header: tr("staff.ops.col.dates"),
        accessorFn: (r) => r.start_date ?? "9999",
        cell: ({ row }) => `${day(row.original.start_date)} – ${day(row.original.end_date)}`,
      },
      { id: "travellers", header: tr("staff.ops.col.travellers"), accessorFn: (r) => r.traveller_count },
      {
        id: "completeness",
        header: tr("staff.ops.col.completeness"),
        enableSorting: false,
        cell: ({ row }) => {
          const r = row.original;
          if (r.status !== "live") return "—";
          return (
            <span style={{ display: "flex", gap: 4, flexWrap: "wrap", alignItems: "center" }}>
              <Badge tone={r.complete ? "success" : "warning"}>
                {r.complete
                  ? tr("staff.ops.complete")
                  : fmt(tr("staff.ops.incomplete"), { complete: r.completeness.complete, n: r.completeness.travellers })}
              </Badge>
              {missingItems(r.completeness).map((m) => (
                <span key={m.item} style={{ fontSize: 11.5, color: K.muted }}>
                  {fmt(tr(`staff.ops.missing.${m.item}`), { n: m.n })}
                </span>
              ))}
              {r.t21_alerted && <Badge tone="danger">{tr("staff.ops.t21")}</Badge>}
            </span>
          );
        },
      },
      {
        id: "departures",
        header: tr("staff.ops.col.departures"),
        enableSorting: false,
        cell: ({ row }) => (
          <span style={{ fontFamily: mono, fontSize: 12, overflowWrap: "anywhere" }}>{row.original.departure_ids.join(", ") || "—"}</span>
        ),
      },
      {
        id: "deletion",
        header: tr("staff.ops.col.deletion"),
        accessorFn: (r) => r.days_to_purge ?? 99999,
        cell: ({ row }) => {
          if (row.original.status !== "live") return "—";
          const p = purgeLabel(row.original.days_to_purge);
          return (
            <span data-testid="staff-ops-purge" title={row.original.purge_at ? day(row.original.purge_at) : undefined}>
              <Badge tone={purgeTone(row.original.days_to_purge)} dot={false}>
                {fmt(tr(p.key), { n: p.n })}
              </Badge>
            </span>
          );
        },
      },
      { id: "views", header: tr("staff.ops.col.views"), accessorFn: (r) => r.view_count, cell: ({ row }) => fmt(tr("staff.ops.views"), { n: row.original.view_count }) },
      {
        id: "status",
        header: tr("staff.ops.col.status"),
        enableSorting: false,
        cell: ({ row }) => {
          const r = row.original;
          if (r.status === "live") return <Badge tone="info">{tr("staff.ops.status.live")}</Badge>;
          return (
            <span style={{ display: "grid", gap: 2 }}>
              <Badge tone="neutral">{tr("staff.ops.status.withdrawn")}</Badge>
              <span style={{ fontSize: 11.5, color: K.muted }}>
                {fmt(tr("staff.ops.withdrawnOn"), { date: day(r.withdrawn_at) })}
                {r.withdraw_reason ? ` (${tr(`staff.ops.reason.${r.withdraw_reason}`)})` : ""}
              </span>
            </span>
          );
        },
      },
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps -- labels follow lang/locale only
    [lang, locale],
  );

  return (
    <div style={{ display: "grid", gap: 14, gridTemplateColumns: "minmax(0, 1fr)" }} data-testid="staff-ops-board">
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "center", justifyContent: "space-between" }}>
        <SegmentedControl<string>
          label={tr("staff.ops.window")}
          options={BOARD_WINDOWS.map((n) => ({ value: String(n), label: fmt(tr("staff.ops.days"), { n }) }))}
          value={String(days)}
          onChange={(v) => router.push(`/staff/ops?days=${v}`)}
        />
        {board.incident_admin_url ? (
          <a
            href={board.incident_admin_url}
            target="_blank"
            rel="noopener noreferrer"
            data-testid="staff-ops-incident-link"
            style={{ fontSize: 13, color: K.accentDeep }}
          >
            {tr("staff.ops.incident")} ↗
          </a>
        ) : (
          <span style={{ fontSize: 12.5, color: K.muted }}>{tr("staff.ops.incidentMissing")}</span>
        )}
      </div>
      <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("staff.ops.incidentHelp")}</p>
      <DataTable<BoardItem>
        tableId="staff-ops-board"
        data={board.items}
        columns={columns}
        getRowId={(r) => `${r.tenant_id}:${r.trip_id}`}
        searchable={false}
        paginate={false}
        compact
        tableMinWidth={980}
        emptyTitle={tr("staff.ops.empty")}
      />
      <p style={{ margin: 0, fontSize: 12, color: K.muted }}>{tr("staff.audited")}</p>
    </div>
  );
}
