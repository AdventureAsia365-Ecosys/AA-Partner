"use client";
// app/(tenant)/portal/t4-pool/page.tsx — AA-430 route migration.
// Was the "catalog" tab in the old portal/page.tsx (T4 — Tenant Tour Pool: tenant's
// own rewritten versions, approve/reject/edit). Route slug confirmed against the
// ADR-2026-038 mapping — NOT T3 (T3 is the tenant-facing QA-failure view, which has no
// UI yet, see AA-430 implementation notes).
import CatalogTab from "../_components/CatalogTab";

export default function T4PoolPage() {
  return <CatalogTab />;
}
