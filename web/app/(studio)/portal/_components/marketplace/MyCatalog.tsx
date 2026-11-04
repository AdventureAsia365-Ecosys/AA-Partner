"use client";
// web/app/(studio)/portal/_components/marketplace/MyCatalog.tsx
// AA-671 — Marketplace, view "In my catalog": the tours this tenant picked, one card per tour (its latest version),
// with the estimated price and the status. Was the "Tour overview" page (MarketplaceTab.tsx, AA-444 / AA-760), now a
// view of the same page as the catalog. A card opens the tour in My Tours, where the work on it happens.
//
// API: GET /api/tenant/v1/marketplace (api/routers/v1_marketplace.py: a read-only rollup of the tenant's
// tenant_tour_versions; ADR-2026-038 §0.3, no pipeline stage of its own). The status badge is the shared work status
// of the same version in ["my-versions"] (job-backed, AA-760), the rollup's coarse state only until that list has it.
// Tenant sees content only (AA-770): atom counts and runway months stay admin-side.

import { useMemo } from "react";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { FolderOpen } from "lucide-react";
import { Badge, EmptyState, ErrorState, K, sans, useLang, useT } from "@/app/_kit";
import { fmtCount } from "@/lib/studio/format";
import { MARKET_MESSAGES } from "./marketplace.messages";
import { useCatalog } from "@/app/_kit/local/studio/useShellData";
import { fmtDuration, fmtUsdWhole } from "@/lib/studio/tours";
import { WorkStatusBadge, tourWorkStatus, type WorkStatus } from "../workStatus";
import { PICKED_KEY, fetchPicked } from "./marketplaceApi";
import { CardGridSkeleton, GRID_STYLE, TourCard } from "./TourCard";
import { LinkButton, myToursHref } from "./TourDrawer";

/** The rollup's coarse state in work-status words, for a version not in ["my-versions"] yet. */
function fromRollupState(s: string): WorkStatus {
  if (s === "writing") return "writing";
  if (s === "failed") return "failed";
  return "done";
}

export function MyCatalog() {
  const t = useT(MARKET_MESSAGES);
  const lang = useLang();
  const router = useRouter();
  const { data, isPending, isError, refetch } = useQuery({ queryKey: PICKED_KEY, queryFn: fetchPicked });
  const { versions } = useCatalog();
  const byVersion = useMemo(() => new Map((versions ?? []).map((v) => [v.id, v])), [versions]);

  if (isPending) return <CardGridSkeleton count={4} label={t("loadingYourTours")} />;
  if (isError || !data) {
    return <ErrorState message={t("loadYoursError")} onRetry={() => refetch()} />;
  }
  if (data.tours.length === 0) {
    // AA-760 — an empty view is the empty state alone, with its next step (no "0 tours" next to it).
    return (
      <EmptyState icon={<FolderOpen size={30} strokeWidth={1.5} />} title={t("nothingYet")}
        description={t("nothingYetSub")}
        action={<LinkButton href="/portal/tours" testId="empty-state-cta">{t("browseTours")}</LinkButton>} />
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16, fontFamily: sans }}>
      <div style={{ display: "flex", gap: 28, flexWrap: "wrap" }} data-testid="marketplace-mine-stats">
        <Stat label={t("statTours")} value={fmtCount(data.total_tours, lang)} />
        {data.posts_per_week != null && data.posts_per_week > 0 && <Stat label={t("statPosts")} value={fmtCount(data.posts_per_week, lang)} />}
      </div>
      <div style={GRID_STYLE} data-testid="marketplace-mine-grid">
        {data.tours.map((tour) => {
          const v = byVersion.get(tour.version_id);
          const status = v ? tourWorkStatus(v) : fromRollupState(tour.status);
          const priced = tour.price_available && tour.price_usd != null;
          return (
            <TourCard key={tour.published_tour_id} testId="marketplace-mine-card"
              name={tour.name}
              country={tour.country}
              duration={fmtDuration(tour.duration, lang)}
              price={priced ? fmtUsdWhole(tour.price_usd as number) : null}
              priceSub={priced ? t("estimated") : undefined}
              // Every tour here is in My Tours: show the work status itself ("Ready", "Writing"), not "In My Tours".
              status={null}
              extraBadge={<><Badge tone="info" dot={false}>v{tour.version_number}</Badge><WorkStatusBadge status={status} /></>}
              onOpen={() => router.push(myToursHref(tour.tour_id))}
            />
          );
        })}
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div style={{ fontSize: 11, fontWeight: 600, color: K.muted, textTransform: "uppercase", letterSpacing: "0.12em", marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 600, color: K.ink, fontVariantNumeric: "tabular-nums" }}>{value}</div>
    </div>
  );
}
