"use client";
// portal/app/portal/_components/AccountActivity.tsx — AA-761: the Activity page, the tenant's own trail from
// GET /v1/account/activity (acp_shared.audit_log, an allowlist of actions, CIS api/routers/v1_account.py).
//
// Kit DataTable with server paging (the API pages and counts; the table holds one page). Filters by type and by
// date (UTC days, both inclusive) go to the server; "Export CSV" downloads every row of the filtered set from
// GET /v1/account/activity/export (the kit's own CSV button would only export the current page, so it is off).
// Was the five-row /v1/billing `activity` list (rewrites only); the dashboard feed and the bell still use that one.
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Download } from "lucide-react";
import { useMemo, useState } from "react";
import { Badge, type ColumnDef, DataTable, apiGet, downloadCsv, type Tone } from "@/app/_kit";
import { formatDateTime } from "@/app/_kit/tokens";
import {
  ACTIVITY_DATE_MAX,
  ACTIVITY_DATE_MIN,
  ACTIVITY_PAGE_SIZES,
  NO_FILTERS,
  activityQuery,
  actorLabel,
  exportFilename,
  hasFilters,
  type ActivityFilters,
  type ActivityItem,
  type ActivityPage,
} from "@/lib/account";
import { T, sans, Card, PageHeader } from "./ui";
import { usePortalShell } from "./PortalShellContext";

const CATEGORY_TONE: Record<string, Tone> = { content: "info", tours: "accent", account: "neutral" };
const CATEGORY_LABEL: Record<string, string> = { content: "Content", tours: "Tours", account: "Account" };

const control: React.CSSProperties = {
  padding: "7px 10px", borderRadius: 999, border: `1px solid ${T.line}`, background: T.card, color: T.ink,
  fontSize: 12.5, fontFamily: sans, minWidth: 0, maxWidth: "100%", boxSizing: "border-box",
};
const btn: React.CSSProperties = {
  display: "inline-flex", alignItems: "center", gap: 6, padding: "7px 12px", borderRadius: 999,
  border: `1px solid ${T.line}`, background: T.card, color: T.ink3, fontSize: 12, fontWeight: 600,
  cursor: "pointer", fontFamily: sans, whiteSpace: "nowrap",
};

function todayUtc(): string {
  return new Date().toISOString().slice(0, 10);
}

export default function AccountActivity() {
  const { tenantName } = usePortalShell();
  const [filters, setFilters] = useState<ActivityFilters>(NO_FILTERS);
  const [pageIndex, setPageIndex] = useState(0);
  const [pageSize, setPageSize] = useState(25);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);

  const query = activityQuery(filters, pageIndex + 1, pageSize);
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["account-activity", query],
    queryFn: () => apiGet<ActivityPage>(`/api/tenant/v1/account/activity${query}`),
    placeholderData: keepPreviousData,
  });
  const rows = data?.data ?? [];
  const total = data?.pagination.total ?? 0;
  const categories = data?.categories ?? Object.entries(CATEGORY_LABEL).map(([id, label]) => ({ id, label }));

  function setFilter(patch: Partial<ActivityFilters>) {
    setFilters((f) => ({ ...f, ...patch }));
    setPageIndex(0);
  }

  async function exportCsv() {
    setExporting(true);
    setExportError(null);
    try {
      const res = await fetch(`/api/tenant/v1/account/activity/export${activityQuery(filters)}`);
      if (!res.ok) throw new Error(`Export failed (${res.status})`);
      downloadCsv(exportFilename(filters, todayUtc()), await res.text());
    } catch (e) {
      setExportError(e instanceof Error ? e.message : "Export failed");
    } finally {
      setExporting(false);
    }
  }

  const columns = useMemo<ColumnDef<ActivityItem, unknown>[]>(() => [
    {
      id: "at",
      header: "When",
      accessorFn: (r) => r.at ?? "",
      enableSorting: false,
      cell: (c) => <span style={{ color: T.muted, fontSize: 12, whiteSpace: "nowrap" }}>{formatDateTime(c.row.original.at)}</span>,
    },
    {
      id: "label",
      header: "Activity",
      accessorFn: (r) => r.label,
      enableSorting: false,
      cell: (c) => {
        const r = c.row.original;
        return (
          <div style={{ minWidth: 0 }}>
            <div style={{ fontSize: 13, fontWeight: 600, color: T.ink }}>{r.label}</div>
            {r.detail && <div style={{ fontSize: 11.5, color: T.muted, marginTop: 2 }}>{r.detail}</div>}
          </div>
        );
      },
    },
    {
      id: "category",
      header: "Type",
      accessorFn: (r) => CATEGORY_LABEL[r.category] ?? r.category,
      enableSorting: false,
      cell: (c) => {
        const cat = c.row.original.category;
        return <Badge tone={CATEGORY_TONE[cat] ?? "neutral"}>{CATEGORY_LABEL[cat] ?? cat}</Badge>;
      },
    },
    {
      id: "actor",
      header: "By",
      accessorFn: (r) => actorLabel(r.actor),
      enableSorting: false,
      cell: (c) => <span style={{ fontSize: 12.5, color: T.body }}>{actorLabel(c.row.original.actor)}</span>,
    },
  ], []);

  const filtered = hasFilters(filters);

  return (
    <div style={{ maxWidth: 960 }}>
      <PageHeader title="Activity Log"
        sub={`Sign-ins, writing, publishing and account changes${tenantName ? ` for ${tenantName}` : ""}.`} />
      <Card>
        <DataTable<ActivityItem>
          data={rows}
          columns={columns}
          tableId="portal-account-activity"
          getRowId={(r) => r.id}
          loading={isLoading}
          error={isError ? (error instanceof Error ? error.message : "Could not load your activity") : null}
          onRetry={() => refetch()}
          searchable={false}
          serverPagination={{
            pageIndex,
            pageSize,
            total,
            onChange: (pi, ps) => {
              if (ps !== pageSize) {
                setPageSize(ps);
                setPageIndex(0);
              } else {
                setPageIndex(pi);
              }
            },
          }}
          pageSizeOptions={ACTIVITY_PAGE_SIZES}
          tableMinWidth={560}
          emptyTitle={filtered ? "No activity matches these filters" : "No activity yet"}
          emptyDescription={filtered
            ? "Try another type or a wider date range."
            : "Sign-ins, rewrites, posts and account changes show up here."}
          toolbarExtra={
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center", minWidth: 0 }}
              data-testid="activity-filters">
              <select aria-label="Type" value={filters.category} style={control}
                onChange={(e) => setFilter({ category: e.target.value })}>
                <option value="">All types</option>
                {categories.map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
              </select>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12, color: T.muted }}>
                From
                <input type="date" aria-label="From date" value={filters.since}
                  min={ACTIVITY_DATE_MIN} max={filters.until || ACTIVITY_DATE_MAX}
                  style={control} onChange={(e) => setFilter({ since: e.target.value })} />
              </label>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12, color: T.muted }}>
                To
                <input type="date" aria-label="To date" value={filters.until}
                  min={filters.since || ACTIVITY_DATE_MIN} max={ACTIVITY_DATE_MAX}
                  style={control} onChange={(e) => setFilter({ until: e.target.value })} />
              </label>
              {filtered && (
                <button type="button" style={btn} onClick={() => { setFilters(NO_FILTERS); setPageIndex(0); }}>
                  Clear
                </button>
              )}
              <button type="button" style={btn} onClick={exportCsv} disabled={exporting || total === 0}
                data-testid="activity-export">
                <Download size={13} /> {exporting ? "Exporting…" : "Export CSV"}
              </button>
            </div>
          }
        />
        {exportError && (
          <p role="alert" style={{ margin: "10px 0 0", fontSize: 12, color: T.red }}>{exportError}. Please try again.</p>
        )}
        <p style={{ margin: "12px 0 0", fontSize: 11.5, color: T.muted, lineHeight: 1.5 }}>
          Times are shown in your browser&rsquo;s time zone; the date filters use UTC days. The export holds every row
          that matches the filters.
        </p>
      </Card>
    </div>
  );
}
