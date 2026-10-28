"use client";
// portal/app/portal/tours/page.tsx — Marketplace (AA-671): the Adventure Asia catalog (T1, tour selection; a rewrite
// runs T2–T5) and, as the view ?view=mine, the tours the tenant picked (was the separate "Tour overview" page at
// /portal/tours/marketplace, which now answers 308 here). AA-759: was /portal/t1-rewrite (308, next.config.ts).
// The page reads useSearchParams() (the view), so it needs a Suspense boundary or `next build` fails prerendering it.
import { Suspense } from "react";
import { MarketplacePage } from "../_components/marketplace/MarketplacePage";

export default function ToursMarketplacePage() {
  return (
    <Suspense>
      <MarketplacePage />
    </Suspense>
  );
}
