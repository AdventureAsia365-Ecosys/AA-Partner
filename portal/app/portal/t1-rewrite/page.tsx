"use client";
// app/(tenant)/portal/t1-rewrite/page.tsx — AA-430 route migration.
// Was the "pool" tab in the old portal/page.tsx (T1 — Tour Selection, triggers the
// rewrite job that runs T2-T5). Route slug confirmed against the ADR-2026-038 mapping.
import { useRouter } from "next/navigation";
import PoolTab from "../_components/PoolTab";
import { usePortalShell } from "../_components/PortalShellContext";

export default function T1RewritePage() {
  const router = useRouter();
  const { globalSearch, showToast, refreshCatalogCount } = usePortalShell();

  function handleRewriteDone() {
    showToast("Rewrite started — check My Catalog in ~30 seconds.");
    refreshCatalogCount();
    router.push("/portal/t4-pool");
  }

  return <PoolTab onRewriteDone={handleRewriteDone} externalSearch={globalSearch} />;
}
