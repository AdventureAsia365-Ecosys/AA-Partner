"use client";
// app/(tenant)/portal/t8-angle-gate/page.tsx — AA-449 (T8 Angle Gate).
// Route naming follows the existing convention (t0-brand, t1-rewrite, t4-pool, t6-atoms,
// t7-planning) — see docs/claude_audit/AA-449-00-step0-t8-angle-gate-investigation.md §8.
import AngleGateTab from "../_components/AngleGateTab";

export default function AngleGatePage() {
  return <AngleGateTab />;
}
