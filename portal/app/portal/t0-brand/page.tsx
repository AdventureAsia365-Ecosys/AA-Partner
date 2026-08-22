"use client";
// app/(tenant)/portal/t0-brand/page.tsx — AA-430 route migration.
// Was the "brand" tab in the old portal/page.tsx (T0 — Brand Identity Setup).
import BrandTab from "../_components/BrandTab";

export default function T0BrandPage() {
  return <BrandTab />;
}
