"use client";
// portal/app/portal/_components/marketplace/CatalogBrowse.tsx
// AA-671 — Marketplace, view "All tours": the Adventure Asia catalog as a card grid with filters, the detail drawer and
// the (bulk) rewrite. Was PoolTab.tsx (Browse Tours: a list with a side panel).
//
// API: GET /api/tenant/v1/tours/pool?page&page_size&search&country&duration_min&duration_max (server paging; the
// country options come from the same answer, AA-739 lesson), POST /api/tenant/v1/tours/pool/{id}/rewrite.
// Each card's status is the shared work-status model on the tour's latest version (the ["my-versions"] query, polled
// while a rewrite runs; AA-760); a tour the server marks `already_rewritten` with no loaded version reads "In My Tours".

import { useMemo, useState } from "react";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { Globe2, Search, X } from "lucide-react";
import { Button, EmptyState, ErrorState, K, sans, useToast } from "@/app/_kit";
import { MY_VERSIONS_KEY } from "@/app/_kit/local/useShellData";
import {
  CATALOG_PAGE_SIZE, DURATION_OPTIONS, NO_CATALOG_FILTERS, fmtDuration, hasCatalogFilters, isRecentlyAdded, pageCount,
  poolQuery, rewriteQuotaNote, themeTags, type CatalogFilters, type DurationFilter, type QuotaNote,
} from "@/lib/tours";
import { fetchMyVersions } from "../catalogApi";
import { activePollInterval, isActiveWork, latestVersionByTour, tourWorkStatus } from "../workStatus";
import { POOL_KEY, PICKED_KEY, fetchPool, startRewrites, type PoolTour } from "./marketplaceApi";
import { CardGridSkeleton, GRID_STYLE, TourCard, type CardStatus } from "./TourCard";
import { QuotaLine, TourDrawer } from "./TourDrawer";
import type { UsageMeter } from "@/lib/account";

const controlStyle: React.CSSProperties = {
  padding: "9px 12px", background: K.card, border: `1px solid ${K.line}`, borderRadius: 8, color: K.body,
  fontSize: 13, fontFamily: sans, cursor: "pointer",
  // A <select> grows to its longest option: cap it so a phone never scrolls sideways (lesson S221).
  maxWidth: "100%", minWidth: 0,
};

/** `externalSearch` (the topbar / ⌘K search) seeds the search box; the page remounts this view with
 *  `key={globalSearch}` when it changes, so no effect syncs it (React Compiler safe). */
export function CatalogBrowse({ externalSearch = "", meter }: { externalSearch?: string; meter: UsageMeter | null }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [filters, setFilters] = useState<CatalogFilters>({ ...NO_CATALOG_FILTERS, search: externalSearch });
  const [page, setPage] = useState(1);
  const [openId, setOpenId] = useState<string | null>(null);
  const [checked, setChecked] = useState<Set<string>>(() => new Set());
  const [busy, setBusy] = useState(false);
  const [now] = useState(() => new Date());

  const query = useMemo(() => poolQuery(filters, page), [filters, page]);
  const poolQ = useQuery({
    queryKey: [POOL_KEY, query],
    queryFn: () => fetchPool(query),
    placeholderData: keepPreviousData,
  });
  const tours = useMemo(() => poolQ.data?.data ?? [], [poolQ.data]);
  const total = poolQ.data?.pagination?.total ?? 0;
  const countries = poolQ.data?.countries ?? [];

  const versionsQ = useQuery({
    queryKey: MY_VERSIONS_KEY,
    queryFn: fetchMyVersions,
    refetchInterval: (q) => activePollInterval((q.state.data ?? []).map(tourWorkStatus)),
  });
  const latest = useMemo(() => latestVersionByTour(versionsQ.data ?? []), [versionsQ.data]);
  const statusOf = (t: PoolTour): CardStatus => {
    const v = latest.get(t.id);
    if (v) return tourWorkStatus(v);
    return t.already_rewritten ? "done" : null;
  };

  // The open tour stays readable while a filter / page change drops it from the list.
  const [openTour, setOpenTour] = useState<PoolTour | null>(null);
  const open = openId ? (tours.find((t) => t.id === openId) ?? openTour) : null;

  function update(patch: Partial<CatalogFilters>) {
    setFilters((f) => ({ ...f, ...patch }));
    setPage(1);
  }
  function toggle(id: string) {
    setChecked((prev) => {
      const s = new Set(prev);
      if (s.has(id)) s.delete(id); else s.add(id);
      return s;
    });
  }

  async function rewrite(ids: string[]) {
    if (ids.length === 0) return;
    setBusy(true);
    try {
      const res = await startRewrites(ids);
      qc.invalidateQueries({ queryKey: MY_VERSIONS_KEY });
      qc.invalidateQueries({ queryKey: [POOL_KEY] });
      qc.invalidateQueries({ queryKey: PICKED_KEY });
      qc.invalidateQueries({ queryKey: ["quota"] });
      qc.invalidateQueries({ queryKey: ["tenant-billing"] });
      if (res.started.length > 0) {
        toast.success(res.started.length === 1
          ? "Rewrite started. Follow it here or in My Tours."
          : `${res.started.length} rewrites started. Follow them here or in My Tours.`);
      }
      if (res.failed.length > 0) {
        const first = res.failed[0].message;
        toast.error(res.failed.length === 1
          ? `Couldn't start the rewrite: ${first}`
          : `${res.failed.length} rewrites couldn't start (${first}).`, 8000);
      }
      setChecked((prev) => {
        const s = new Set(prev);
        for (const id of res.started) s.delete(id);
        return s;
      });
    } finally {
      setBusy(false);
    }
  }

  // Bulk: the checked tours minus any already being written (the API would refuse them with 409).
  const checkedTours = Array.from(checked);
  const bulkIds = checkedTours.filter((id) => {
    const v = latest.get(id);
    return !(v && isActiveWork(tourWorkStatus(v)));
  });
  const bulkNote: QuotaNote | null = rewriteQuotaNote(meter, Math.max(1, bulkIds.length));
  const pages = pageCount(total);
  const filtered = hasCatalogFilters(filters);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16, fontFamily: sans }}>
      {/* Filter bar — wraps on a phone. */}
      <div data-testid="marketplace-filters" style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
        <div style={{ position: "relative", flex: "1 1 240px", minWidth: 0 }}>
          <Search size={14} aria-hidden style={{ position: "absolute", left: 11, top: "50%", transform: "translateY(-50%)", color: K.muted2 }} />
          <input value={filters.search} onChange={(e) => update({ search: e.target.value })}
            placeholder="Search tours by name…" aria-label="Search tours by name"
            style={{ ...controlStyle, width: "100%", padding: "9px 12px 9px 32px", cursor: "text", boxSizing: "border-box", outline: "none" }} />
        </div>
        <select value={filters.country} onChange={(e) => update({ country: e.target.value })} aria-label="Country" style={controlStyle}>
          <option value="">All countries</option>
          {countries.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
        <select value={filters.duration} onChange={(e) => update({ duration: e.target.value as DurationFilter })} aria-label="Length" style={controlStyle}>
          {DURATION_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
        {filtered && (
          <Button variant="ghost" size="sm" onClick={() => update(NO_CATALOG_FILTERS)}>
            <X size={13} aria-hidden /> Clear filters
          </Button>
        )}
      </div>

      {/* Count + sort (the API lists newest first; there is no other order). */}
      {!poolQ.isPending && !poolQ.isError && (
        <div style={{ fontSize: 12.5, color: K.muted }} data-testid="marketplace-count">
          {total.toLocaleString("en-US")} {total === 1 ? "tour" : "tours"}{filtered ? " match" : " available"} · newest first
        </div>
      )}

      {/* Bulk bar — shown once a card is selected. */}
      {checked.size > 0 && (
        <div data-testid="marketplace-bulk-bar" style={{
          display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap", padding: "10px 14px",
          background: K.accentTint, border: `1px solid ${K.accentSoft}`, borderRadius: 10,
        }}>
          <span style={{ fontSize: 13, fontWeight: 600, color: K.accentDeep }}>
            {checked.size} selected{bulkIds.length < checked.size ? ` · ${checked.size - bulkIds.length} already being written` : ""}
          </span>
          <Button variant="primary" size="sm" disabled={busy || bulkIds.length === 0} onClick={() => rewrite(bulkIds)}>
            {busy ? "Starting…" : bulkIds.length === 1 ? "Rewrite 1 tour" : `Rewrite ${bulkIds.length} tours`}
          </Button>
          <Button variant="ghost" size="sm" onClick={() => setChecked(new Set())}>Clear selection</Button>
          <div style={{ flex: "1 1 220px", minWidth: 0 }}><QuotaLine note={bulkNote} /></div>
        </div>
      )}

      {/* Grid */}
      {poolQ.isPending ? (
        <CardGridSkeleton label="Loading tours" />
      ) : poolQ.isError && tours.length === 0 ? (
        <ErrorState message="Couldn't load the tours. Something went wrong on our side." onRetry={() => poolQ.refetch()} />
      ) : tours.length === 0 ? (
        <EmptyState icon={<Globe2 size={30} strokeWidth={1.5} />} title="No tours found"
          description={filtered ? "Try another search or fewer filters." : "Adventure Asia has not published tours yet."}
          action={filtered ? <Button variant="secondary" size="sm" onClick={() => update(NO_CATALOG_FILTERS)}>Clear filters</Button> : undefined} />
      ) : (
        <div style={GRID_STYLE} data-testid="marketplace-grid" aria-busy={poolQ.isFetching && poolQ.isPlaceholderData ? "true" : undefined}>
          {tours.map((t) => (
            <TourCard key={t.id}
              name={t.aa_name}
              subtitle={t.aa_subtitle}
              country={t.country}
              duration={fmtDuration(t.duration)}
              isNew={isRecentlyAdded(t.published_at, now)}
              tags={themeTags(t.seo_keywords_used, 3, [t.country])}
              price={t.price_raw}
              status={statusOf(t)}
              selected={checked.has(t.id)}
              onSelectToggle={() => toggle(t.id)}
              active={open?.id === t.id}
              onOpen={() => { setOpenId(t.id); setOpenTour(t); }}
            />
          ))}
        </div>
      )}

      {/* Pagination (server side). */}
      {total > CATALOG_PAGE_SIZE && (
        <div style={{ display: "flex", gap: 8, justifyContent: "center", alignItems: "center" }}>
          <Button variant="ghost" size="sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>← Previous</Button>
          <span style={{ fontSize: 12.5, color: K.muted, fontVariantNumeric: "tabular-nums" }}>Page {page} of {pages}</span>
          <Button variant="ghost" size="sm" disabled={page >= pages} onClick={() => setPage((p) => p + 1)}>Next →</Button>
        </div>
      )}

      <TourDrawer tour={open} status={open ? statusOf(open) : null} quota={rewriteQuotaNote(meter, 1)} busy={busy}
        onClose={() => setOpenId(null)} onRewrite={(t) => rewrite([t.id])} />
    </div>
  );
}
