"use client";
// portal/app/portal/_components/marketplace/MarketplacePage.tsx
// AA-671 — the Marketplace: ONE page for the tours a tenant can pick and the tours it picked (was two near-duplicate
// pages, Browse Tours and Tour overview; decision and trade-offs in docs/implementation-notes/AA-671.md).
//   ?view=all  (default) — All tours: the Adventure Asia catalog, grid + filters + detail drawer + (bulk) rewrite
//   ?view=mine           — In my catalog: the picked tours with price and status; a card opens it in My Tours
// The view lives in the URL (linkable, Back works); the old /portal/tours/marketplace answers 308 to ?view=mine.

import { useQuery } from "@tanstack/react-query";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { PageHeader, Tabs, apiGet } from "@/app/_kit";
import { usageMeter, type BillingFigures, type Quota } from "@/lib/account";
import { marketplaceView, type MarketplaceView } from "@/lib/tours";
import { usePortalShell } from "../PortalShellContext";
import { CatalogBrowse } from "./CatalogBrowse";
import { MyCatalog } from "./MyCatalog";

export function MarketplacePage() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const view = marketplaceView(params.get("view"));
  const { globalSearch, poolTotal, catTotal, billing } = usePortalShell();

  // The rewrite meter of Billing (same query key as the Billing page), for the quota note on every rewrite action.
  const quotaQ = useQuery({ queryKey: ["quota"], queryFn: () => apiGet<Quota>("/api/tenant/v1/quota") });
  const meter = quotaQ.data ? usageMeter(quotaQ.data, (billing ?? null) as BillingFigures | null) : null;

  function setView(v: string) {
    const next = v as MarketplaceView;
    router.push(next === "mine" ? `${pathname}?view=mine` : pathname, { scroll: false });
  }

  const allLabel = poolTotal ? `All tours · ${poolTotal.toLocaleString("en-US")}` : "All tours";
  const mineLabel = catTotal > 0 ? `In my catalog · ${catTotal.toLocaleString("en-US")}` : "In my catalog";

  return (
    <>
      <PageHeader title="Marketplace"
        description={view === "mine"
          ? "The tours you picked, with their price and status. Open one to review, edit or export it in My Tours."
          : "Adventure Asia's published tours. Pick one and rewrite it in your brand voice; it lands in My Tours."} />
      <div style={{ marginBottom: 18 }} data-testid="marketplace-views">
        <Tabs tabs={[{ key: "all", label: allLabel }, { key: "mine", label: mineLabel }]} active={view} onChange={setView} />
      </div>
      {view === "mine"
        ? <MyCatalog />
        // Remount on a new topbar / ⌘K search instead of syncing it in an effect (AA-760).
        : <CatalogBrowse key={globalSearch} externalSearch={globalSearch} meter={meter} />}
    </>
  );
}
