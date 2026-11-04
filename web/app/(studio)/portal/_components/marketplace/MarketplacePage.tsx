"use client";
// web/app/(studio)/portal/_components/marketplace/MarketplacePage.tsx
// AA-671 — the Marketplace: ONE page for the tours a tenant can pick and the tours it picked (was two near-duplicate
// pages, Browse Tours and Tour overview; decision and trade-offs in docs/implementation-notes/AA-671.md).
//   ?view=all  (default) — All tours: the Adventure Asia catalog, grid + filters + detail drawer + (bulk) rewrite
//   ?view=mine           — In my catalog: the picked tours with price and status; a card opens it in My Tours
// The view lives in the URL (linkable, Back works); the old /portal/tours/marketplace answers 308 to ?view=mine.

import { useQuery } from "@tanstack/react-query";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { PageHeader, Tabs, apiGet, useLang, useT } from "@/app/_kit";
import { fmtCount } from "@/lib/studio/format";
import { PAGES_MESSAGES } from "../../content/content.messages";
import { usageMeter, type BillingFigures, type Quota } from "@/lib/studio/account";
import { marketplaceView, type MarketplaceView } from "@/lib/studio/tours";
import { usePortalShell } from "../PortalShellContext";
import { CatalogBrowse } from "./CatalogBrowse";
import { MyCatalog } from "./MyCatalog";

export function MarketplacePage() {
  const t = useT(PAGES_MESSAGES);
  const lang = useLang();
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

  const allLabel = poolTotal ? t("allToursN", fmtCount(poolTotal, lang)) : t("allTours");
  const mineLabel = catTotal > 0 ? t("inCatalogN", fmtCount(catTotal, lang)) : t("inCatalog");

  return (
    <>
      <PageHeader title={t("marketTitle")} description={view === "mine" ? t("marketSubMine") : t("marketSubAll")} />
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
