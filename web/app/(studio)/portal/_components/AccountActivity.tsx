"use client";
// web/app/(studio)/portal/_components/AccountActivity.tsx — AA-761: the Activity page, the tenant's own trail from
// GET /v1/account/activity (acp_shared.audit_log, an allowlist of actions, CIS api/routers/v1_account.py).
//
// Kit DataTable with server paging (the API pages and counts; the table holds one page). Filters by type and by
// date (UTC days, both inclusive) go to the server; "Export CSV" downloads every row of the filtered set from
// GET /v1/account/activity/export (the kit's own CSV button would only export the current page, so it is off).
// Was the five-row /v1/billing `activity` list (rewrites only); the dashboard feed and the bell still use that one.
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Download } from "lucide-react";
import { useCallback, useMemo, useState } from "react";
import {
  Badge, Button, type ColumnDef, DataTable, PageHeader, Section, apiGet, downloadCsv, type Tone, K, sans, useFormat, useLang, useT,
} from "@/app/_kit";
import { ACTIVITY_MESSAGES } from "./account.messages";
import {
  ACTIVITY_DATE_MAX,
  ACTIVITY_DATE_MIN,
  ACTIVITY_PAGE_SIZES,
  NO_FILTERS,
  activityDetail,
  activityQuery,
  actorLabel,
  exportFilename,
  hasFilters,
  type ActivityFilters,
  type ActivityItem,
  type ActivityPage,
} from "@/lib/studio/account";
import { usePortalShell } from "./PortalShellContext";
import { plainError } from "./studioError";

const CATEGORY_TONE: Record<string, Tone> = { content: "info", tours: "accent", account: "neutral" };
const CATEGORY_KEY = { content: "catContent", tours: "catTours", account: "catAccount" } as const;

const control: React.CSSProperties = {
  padding: "7px 10px", borderRadius: 999, border: `1px solid ${K.line}`, background: K.card, color: K.ink,
  fontSize: 12.5, fontFamily: sans, minWidth: 0, maxWidth: "100%", boxSizing: "border-box",
};

function todayUtc(): string {
  return new Date().toISOString().slice(0, 10);
}

export default function AccountActivity() {
  const t = useT(ACTIVITY_MESSAGES);
  const lang = useLang();
  const { formatDateTime } = useFormat();
  const catLabel = useCallback(
    (id: string) => (Object.hasOwn(CATEGORY_KEY, id) ? t(CATEGORY_KEY[id as keyof typeof CATEGORY_KEY]) : id),
    [t],
  );
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
  const categories = (data?.categories ?? Object.keys(CATEGORY_KEY).map((id) => ({ id, label: id })))
    .map((c) => ({ id: c.id, label: Object.hasOwn(CATEGORY_KEY, c.id) ? catLabel(c.id) : c.label }));

  function setFilter(patch: Partial<ActivityFilters>) {
    setFilters((f) => ({ ...f, ...patch }));
    setPageIndex(0);
  }

  async function exportCsv() {
    setExporting(true);
    setExportError(null);
    try {
      const res = await fetch(`/api/tenant/v1/account/activity/export${activityQuery(filters)}`);
      if (!res.ok) throw new Error(t("exportFailedStatus", res.status));
      downloadCsv(exportFilename(filters, todayUtc()), await res.text());
    } catch (e) {
      setExportError(plainError(e, t("exportFailed"), lang));
    } finally {
      setExporting(false);
    }
  }

  const columns = useMemo<ColumnDef<ActivityItem, unknown>[]>(() => [
    {
      id: "at",
      header: t("colWhen"),
      accessorFn: (r) => r.at ?? "",
      enableSorting: false,
      cell: (c) => <span style={{ color: K.muted, fontSize: 12, whiteSpace: "nowrap" }}>{formatDateTime(c.row.original.at)}</span>,
    },
    {
      id: "label",
      header: t("colActivity"),
      accessorFn: (r) => r.label,
      enableSorting: false,
      cell: (c) => {
        const r = c.row.original;
        return (
          <div style={{ minWidth: 0 }}>
            <div style={{ fontSize: 13, fontWeight: 600, color: K.ink }}>{r.label}</div>
            {r.detail && <div style={{ fontSize: 11.5, color: K.muted, marginTop: 2 }}>{activityDetail(r.action, r.detail, lang)}</div>}
          </div>
        );
      },
    },
    {
      id: "category",
      header: t("colType"),
      accessorFn: (r) => catLabel(r.category),
      enableSorting: false,
      cell: (c) => {
        const cat = c.row.original.category;
        return <Badge tone={CATEGORY_TONE[cat] ?? "neutral"}>{catLabel(cat)}</Badge>;
      },
    },
    {
      id: "actor",
      header: t("colBy"),
      accessorFn: (r) => actorLabel(r.actor, lang),
      enableSorting: false,
      cell: (c) => <span style={{ fontSize: 12.5, color: K.body }}>{actorLabel(c.row.original.actor, lang)}</span>,
    },
  ], [t, lang, formatDateTime, catLabel]);

  const filtered = hasFilters(filters);

  return (
    <div style={{ maxWidth: 960 }}>
      <PageHeader title={t("title")} description={t("sub", tenantName)} />
      <Section>
        <DataTable<ActivityItem>
          data={rows}
          columns={columns}
          tableId="portal-account-activity"
          getRowId={(r) => r.id}
          loading={isLoading}
          error={isError ? plainError(error, t("loadError"), lang) : null}
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
          emptyTitle={filtered ? t("emptyFiltered") : t("empty")}
          emptyDescription={filtered ? t("emptyFilteredSub") : t("emptySub")}
          toolbarExtra={
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center", minWidth: 0 }}
              data-testid="activity-filters">
              <select aria-label={t("type")} value={filters.category} style={control}
                onChange={(e) => setFilter({ category: e.target.value })}>
                <option value="">{t("allTypes")}</option>
                {categories.map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
              </select>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12, color: K.muted }}>
                {t("from")}
                <input type="date" aria-label={t("fromDate")} value={filters.since}
                  min={ACTIVITY_DATE_MIN} max={filters.until || ACTIVITY_DATE_MAX}
                  style={control} onChange={(e) => setFilter({ since: e.target.value })} />
              </label>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12, color: K.muted }}>
                {t("to")}
                <input type="date" aria-label={t("toDate")} value={filters.until}
                  min={filters.since || ACTIVITY_DATE_MIN} max={ACTIVITY_DATE_MAX}
                  style={control} onChange={(e) => setFilter({ until: e.target.value })} />
              </label>
              {filtered && (
                <Button size="sm" variant="ghost" onClick={() => { setFilters(NO_FILTERS); setPageIndex(0); }}>
                  {t("clear")}
                </Button>
              )}
              <Button size="sm" variant="secondary" onClick={exportCsv} disabled={exporting || total === 0}
                testId="activity-export">
                <Download size={13} /> {exporting ? t("exporting") : t("exportCsv")}
              </Button>
            </div>
          }
        />
        {exportError && (
          <p role="alert" style={{ margin: "10px 0 0", fontSize: 12, color: K.danger }}>{t("tryAgain", exportError)}</p>
        )}
        <p style={{ margin: "12px 0 0", fontSize: 11.5, color: K.muted, lineHeight: 1.5 }}>
          {t("timesNote")}
        </p>
      </Section>
    </div>
  );
}
