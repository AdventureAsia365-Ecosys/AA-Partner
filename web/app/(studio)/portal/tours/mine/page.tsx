"use client";
// web/app/(studio)/portal/tours/mine/page.tsx — My Tours (T4 — the tenant's own rewritten versions).
// AA-759: was /portal/t4-pool ("My Catalog Tours"; 308 from the old slug, next.config.ts).
// CatalogTab reads useSearchParams() (?tour_id=, also the ⌘K "My tours" target), so it needs a
// Suspense boundary or `next build` fails prerendering this route.
import { Suspense } from "react";
import { PageHeader, useT } from "@/app/_kit";
import { PAGES_MESSAGES } from "../../content/content.messages";
import CatalogTab from "../../_components/CatalogTab";

export default function MyToursPage() {
  const t = useT(PAGES_MESSAGES);
  return (
    <>
      <PageHeader title={t("myToursTitle")} description={t("myToursSub")} />
      <Suspense>
        <CatalogTab />
      </Suspense>
    </>
  );
}
