"use client";
// app/(tenant)/portal/t6-atoms/page.tsx — AA-431 (T6 — Atom Curation, tenant-facing).
// Route convention reserved by AA-430 (route migration) — first real use of it.
import { Suspense } from "react";
import AtomsTab from "../_components/AtomsTab";

// AA-454 — AtomsTab now reads useSearchParams() (?tour_id= from CatalogTab's nav link),
// which requires a Suspense boundary or `next build` fails prerendering this route
// (same pattern as frontend/app/admin/curation/page.tsx).
export default function T6AtomsPage() {
  return (
    <Suspense>
      <AtomsTab />
    </Suspense>
  );
}
