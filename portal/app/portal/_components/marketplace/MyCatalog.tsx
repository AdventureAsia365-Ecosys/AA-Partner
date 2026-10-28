"use client";
// portal/app/portal/_components/marketplace/MyCatalog.tsx
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
import { Badge, EmptyState, ErrorState, K, sans } from "@/app/_kit";
import { useCatalog } from "@/app/_kit/local/useShellData";
import { fmtDuration, fmtUsdWhole } from "@/lib/tours";
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
  const router = useRouter();
  const { data, isPending, isError, refetch } = useQuery({ queryKey: PICKED_KEY, queryFn: fetchPicked });
  const { versions } = useCatalog();
  const byVersion = useMemo(() => new Map((versions ?? []).map((v) => [v.id, v])), [versions]);

  if (isPending) return <CardGridSkeleton count={4} label="Loading your tours" />;
  if (isError || !data) {
    return <ErrorState message="Couldn't load your tours. Something went wrong reaching your tour data." onRetry={() => refetch()} />;
  }
  if (data.tours.length === 0) {
    // AA-760 — an empty view is the empty state alone, with its next step (no "0 tours" next to it).
    return (
      <EmptyState icon={<FolderOpen size={30} strokeWidth={1.5} />} title="Nothing here yet"
        description="Once you rewrite your first tour, it shows up here with its price and status."
        action={<LinkButton href="/portal/tours" testId="empty-state-cta">Browse tours</LinkButton>} />
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16, fontFamily: sans }}>
      <div style={{ display: "flex", gap: 28, flexWrap: "wrap" }} data-testid="marketplace-mine-stats">
        <Stat label="Tours in your catalog" value={data.total_tours} />
        {data.posts_per_week != null && data.posts_per_week > 0 && <Stat label="Posts planned / week" value={data.posts_per_week} />}
      </div>
      <div style={GRID_STYLE} data-testid="marketplace-mine-grid">
        {data.tours.map((t) => {
          const v = byVersion.get(t.version_id);
          const status = v ? tourWorkStatus(v) : fromRollupState(t.status);
          const priced = t.price_available && t.price_usd != null;
          return (
            <TourCard key={t.published_tour_id} testId="marketplace-mine-card"
              name={t.name}
              country={t.country}
              duration={fmtDuration(t.duration)}
              price={priced ? fmtUsdWhole(t.price_usd as number) : null}
              priceSub={priced ? "estimated" : undefined}
              // Every tour here is in My Tours: show the work status itself ("Ready", "Writing"), not "In My Tours".
              status={null}
              extraBadge={<><Badge tone="info" dot={false}>v{t.version_number}</Badge><WorkStatusBadge status={status} /></>}
              onOpen={() => router.push(myToursHref(t.tour_id))}
            />
          );
        })}
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <div style={{ fontSize: 11, fontWeight: 600, color: K.muted, textTransform: "uppercase", letterSpacing: "0.12em", marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 600, color: K.ink, fontVariantNumeric: "tabular-nums" }}>{value.toLocaleString("en-US")}</div>
    </div>
  );
}
