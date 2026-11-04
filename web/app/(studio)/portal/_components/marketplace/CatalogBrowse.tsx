"use client";
// web/app/(studio)/portal/_components/marketplace/CatalogBrowse.tsx
// AA-671 — Marketplace, view "All tours": the Adventure Asia catalog as a card grid with filters, the detail drawer and
// the (bulk) rewrite. Was PoolTab.tsx (Browse Tours: a list with a side panel).
//
// API: GET /api/tenant/v1/tours/pool?page&page_size&search&country&duration_min&duration_max&activity&intensity&
// added_within_days (server paging and filtering; the country, activity and intensity options come from the same
// answer, AA-739 lesson; each row carries its activities, intensity and cover photo), POST /api/tenant/v1/tours/pool/{id}/rewrite.
// Each card's status is the shared work-status model on the tour's latest version (the ["my-versions"] query, polled
// while a rewrite runs; AA-760); a tour the server marks `already_rewritten` with no loaded version reads "In My Tours".

import { useMemo, useState } from "react";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { Globe2, Search, X } from "lucide-react";
import { Button, EmptyState, ErrorState, K, alpha, sans, useLang, useT, useToast } from "@/app/_kit";
import { fmtCount } from "@/lib/studio/format";
import { MARKET_MESSAGES } from "./marketplace.messages";
import { MY_VERSIONS_KEY } from "@/app/_kit/local/studio/useShellData";
import {
  ACTIVITY_LABELS, CATALOG_PAGE_SIZE, INTENSITY_LABELS, NO_CATALOG_FILTERS, addedOptions, durationOptions,
  activityLabel, cardTags, facetOptions, fmtDuration, hasCatalogFilters, intensityLabel, isRecentlyAdded, pageCount,
  poolQuery, rewriteQuotaNote, themeTags, type AddedFilter, type CatalogFilters, type DurationFilter, type QuotaNote,
} from "@/lib/studio/tours";
import { fetchMyVersions } from "../catalogApi";
import { ServedBadge, workClosed } from "../ServedBadge";
import { activePollInterval, isActiveWork, latestVersionByTour, tourWorkStatus } from "../workStatus";
import { POOL_KEY, PICKED_KEY, fetchPool, startRewrites, type PoolTour } from "./marketplaceApi";
import { CardGridSkeleton, FromPriceLine, GRID_STYLE, TourCard, type CardStatus } from "./TourCard";
import { QuotaLine, TourDrawer } from "./TourDrawer";
import type { UsageMeter } from "@/lib/studio/account";

const controlStyle: React.CSSProperties = {
  padding: "9px 12px", background: K.card, border: `1px solid ${K.line}`, borderRadius: 8, color: K.body,
  fontSize: 13, fontFamily: sans, cursor: "pointer",
  // A <select> grows to its longest option: cap it so a phone never scrolls sideways (lesson S221).
  maxWidth: "100%", minWidth: 0,
};

/** `externalSearch` (the topbar / ⌘K search) seeds the search box; the page remounts this view with
 *  `key={globalSearch}` when it changes, so no effect syncs it (React Compiler safe). */
export function CatalogBrowse({ externalSearch = "", meter }: { externalSearch?: string; meter: UsageMeter | null }) {
  const t = useT(MARKET_MESSAGES);
  const lang = useLang();
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
  const activityOptions = facetOptions(poolQ.data?.facets?.activities, ACTIVITY_LABELS, (c) => activityLabel(c, lang));
  const intensityOptions = facetOptions(poolQ.data?.facets?.intensities, INTENSITY_LABELS, (c) => intensityLabel(c, lang));

  const versionsQ = useQuery({
    queryKey: MY_VERSIONS_KEY,
    queryFn: fetchMyVersions,
    refetchInterval: (q) => activePollInterval((q.state.data ?? []).map(tourWorkStatus)),
  });
  const latest = useMemo(() => latestVersionByTour(versionsQ.data ?? []), [versionsQ.data]);
  const statusOf = (tour: PoolTour): CardStatus => {
    const v = latest.get(tour.id);
    if (v) return tourWorkStatus(v);
    return tour.already_rewritten ? "done" : null;
  };

  // The open tour stays readable while a filter / page change drops it from the list.
  const [openTour, setOpenTour] = useState<PoolTour | null>(null);
  const open = openId ? (tours.find((tour) => tour.id === openId) ?? openTour) : null;

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
      const res = await startRewrites(ids, lang);
      qc.invalidateQueries({ queryKey: MY_VERSIONS_KEY });
      qc.invalidateQueries({ queryKey: [POOL_KEY] });
      qc.invalidateQueries({ queryKey: PICKED_KEY });
      qc.invalidateQueries({ queryKey: ["quota"] });
      qc.invalidateQueries({ queryKey: ["tenant-billing"] });
      if (res.started.length > 0) {
        toast.success(res.started.length === 1 ? t("rewriteStartedOne") : t("rewriteStartedMany", res.started.length));
      }
      if (res.failed.length > 0) {
        const first = res.failed[0].message;
        toast.error(res.failed.length === 1 ? t("rewriteFailedOne", first) : t("rewriteFailedMany", res.failed.length, first), 8000);
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
  const bulkNote: QuotaNote | null = rewriteQuotaNote(meter, Math.max(1, bulkIds.length), lang);
  const pages = pageCount(total);
  const filtered = hasCatalogFilters(filters);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16, fontFamily: sans }}>
      {/* Filter bar — wraps on a phone. */}
      <div data-testid="marketplace-filters" style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
        <div style={{ position: "relative", flex: "1 1 240px", minWidth: 0 }}>
          <Search size={14} aria-hidden style={{ position: "absolute", left: 11, top: "50%", transform: "translateY(-50%)", color: K.muted2 }} />
          <input value={filters.search} onChange={(e) => update({ search: e.target.value })}
            placeholder={t("searchPlaceholder")} aria-label={t("searchAria")}
            style={{ ...controlStyle, width: "100%", padding: "9px 12px 9px 32px", cursor: "text", boxSizing: "border-box", outline: "none" }} />
        </div>
        <select value={filters.country} onChange={(e) => update({ country: e.target.value })} aria-label={t("country")} style={controlStyle}>
          <option value="">{t("allCountries")}</option>
          {countries.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
        <select value={filters.duration} onChange={(e) => update({ duration: e.target.value as DurationFilter })} aria-label={t("length")} style={controlStyle}>
          {durationOptions(lang).map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
        <select value={filters.activity} onChange={(e) => update({ activity: e.target.value })} aria-label={t("activity")} style={controlStyle}>
          <option value="">{t("allActivities")}</option>
          {activityOptions.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
        <select value={filters.intensity} onChange={(e) => update({ intensity: e.target.value })} aria-label={t("intensityLabel")} style={controlStyle}>
          <option value="">{t("anyIntensity")}</option>
          {intensityOptions.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
        <select value={filters.added} onChange={(e) => update({ added: e.target.value as AddedFilter })} aria-label={t("recentlyAdded")} style={controlStyle}>
          {addedOptions(lang).map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
        {filtered && (
          <Button variant="ghost" size="sm" onClick={() => update(NO_CATALOG_FILTERS)}>
            <X size={13} aria-hidden /> {t("clearFilters")}
          </Button>
        )}
      </div>

      {/* Count + sort (the API lists newest first; there is no other order). */}
      {!poolQ.isPending && !poolQ.isError && (
        <div style={{ fontSize: 12.5, color: K.muted }} data-testid="marketplace-count">
          {filtered ? t("countMatch", fmtCount(total, lang), total === 1) : t("countAvailable", fmtCount(total, lang), total === 1)}
        </div>
      )}

      {/* Bulk bar — shown once a card is selected; sticky at the top of the scrolling <main>, so the Rewrite button stays
          in reach after scrolling down the grid (a phone shows one card per row). Opaque card background: cards pass
          under it. */}
      {checked.size > 0 && (
        <div data-testid="marketplace-bulk-bar" style={{
          display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap", padding: "10px 14px",
          background: K.card, border: `1px solid ${K.accentSoft}`, borderRadius: 10,
          position: "sticky", top: 8, zIndex: 5, boxShadow: `0 4px 14px ${alpha(K.ink, 12)}`,
        }}>
          <span style={{ fontSize: 13, fontWeight: 600, color: K.accentDeep }}>
            {t("selected", checked.size)}{bulkIds.length < checked.size ? t("alreadyWriting", checked.size - bulkIds.length) : ""}
          </span>
          <Button variant="primary" size="sm" disabled={busy || bulkIds.length === 0} onClick={() => rewrite(bulkIds)}>
            {busy ? t("starting") : bulkIds.length === 1 ? t("rewriteOne") : t("rewriteMany", bulkIds.length)}
          </Button>
          <Button variant="ghost" size="sm" onClick={() => setChecked(new Set())}>{t("clearSelection")}</Button>
          <div style={{ flex: "1 1 220px", minWidth: 0 }}><QuotaLine note={bulkNote} /></div>
        </div>
      )}

      {/* Grid */}
      {poolQ.isPending ? (
        <CardGridSkeleton label={t("loadingTours")} withCover />
      ) : poolQ.isError && tours.length === 0 ? (
        <ErrorState message={t("loadError")} onRetry={() => poolQ.refetch()} />
      ) : tours.length === 0 ? (
        <EmptyState icon={<Globe2 size={30} strokeWidth={1.5} />} title={t("noTours")}
          description={filtered ? t("noToursFiltered") : t("noToursYet")}
          action={filtered ? <Button variant="secondary" size="sm" onClick={() => update(NO_CATALOG_FILTERS)}>{t("clearFilters")}</Button> : undefined} />
      ) : (
        <div style={GRID_STYLE} data-testid="marketplace-grid" aria-busy={poolQ.isFetching && poolQ.isPlaceholderData ? "true" : undefined}>
          {tours.map((tour) => (
            <TourCard key={tour.id}
              name={tour.aa_name}
              subtitle={tour.aa_subtitle}
              country={tour.country}
              duration={fmtDuration(tour.duration, lang)}
              intensity={tour.intensity ? intensityLabel(tour.intensity, lang) : null}
              isNew={isRecentlyAdded(tour.published_at, now)}
              tags={cardTags(tour.activities, themeTags(tour.seo_keywords_used, 3, [tour.country]), 3, lang)}
              cover={tour.cover_image_url ?? null}
              coverLarge={tour.cover_image_large_url ?? null}
              // AA-1029: AA-Booking's "from" price (price.from.v1) or no price column; never the source price text.
              priceBlock={tour.from_price ? <FromPriceLine price={tour.from_price} compact /> : null}
              status={statusOf(tour)}
              // AA-986 L3: a tour AA paused or retired is refused (409): no select box, its badge says why.
              extraBadge={tour.served_notice ? <ServedBadge fields={tour} /> : undefined}
              selected={checked.has(tour.id)}
              onSelectToggle={workClosed(tour) ? undefined : () => toggle(tour.id)}
              active={open?.id === tour.id}
              onOpen={() => { setOpenId(tour.id); setOpenTour(tour); }}
            />
          ))}
        </div>
      )}

      {/* Pagination (server side). */}
      {total > CATALOG_PAGE_SIZE && (
        <div style={{ display: "flex", gap: 8, justifyContent: "center", alignItems: "center" }}>
          <Button variant="ghost" size="sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>{t("previous")}</Button>
          <span style={{ fontSize: 12.5, color: K.muted, fontVariantNumeric: "tabular-nums" }}>{t("pageOf", page, pages)}</span>
          <Button variant="ghost" size="sm" disabled={page >= pages} onClick={() => setPage((p) => p + 1)}>{t("next")}</Button>
        </div>
      )}

      <TourDrawer tour={open} status={open ? statusOf(open) : null} quota={rewriteQuotaNote(meter, 1, lang)} busy={busy}
        onClose={() => setOpenId(null)} onRewrite={(t) => rewrite([t.id])} />
    </div>
  );
}
