"use client";
// web/app/_kit/local/studio/PortalShell.tsx
// AA-759 — the single tenant-portal shell, on the UI kit (same shape as the admin AdminShell,
// AA-752): [sidebar | topbar over <main>], a ⌘K / Ctrl K command palette, a phone bottom bar, the
// kit toast. app/portal/layout.tsx renders it once around every portal page.
//
// Responsive behaviour is CSS: below 900 px the sidebar is an off-canvas drawer opened from the topbar ☰ or the bottom
// bar "More" (the kit core ShellFrame: drawer, backdrop, Esc, the palette's ⌘K / Ctrl K, the <main>; AA-1008 S6e);
// below 768 px the bottom bar shows and the search pill becomes an icon (studio.css, the `data-pshell` hooks). No JS
// media query, so the server HTML is already right on a phone. This file is the studio's configuration of the
// ShellFrame: its slots, the shell data and the topbar search text the Marketplace catalog reads (`globalSearch`).

import { useCallback, useMemo, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";
import { ShellFrame, ToastProvider, useToast } from "@/app/_kit";
import { PortalShellContext, type PortalShellValue } from "@/app/(studio)/portal/_components/PortalShellContext";
import { PortalSidebar } from "./PortalSidebar";
import { PortalTopbar } from "./PortalTopbar";
import { PortalBottomNav } from "./PortalBottomNav";
import { PortalCommandPalette } from "./PortalCommandPalette";
import { ROUTES } from "./portalNav";
import { MY_VERSIONS_KEY, useActivityView, useBilling, useCatalog, usePoolTotal, useTenantMe } from "./useShellData";

export function PortalShell({ children }: { children: React.ReactNode }) {
  return (
    <ToastProvider>
      <StudioShell>{children}</StudioShell>
    </ToastProvider>
  );
}

function StudioShell({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const qc = useQueryClient();
  const toast = useToast();

  const [globalSearch, setGlobalSearch] = useState("");

  const { tenantName, planTier, status: tenantStatus } = useTenantMe();
  const poolTotal = usePoolTotal();
  const { versions, count: catTotal, failed: versionsFailed } = useCatalog();
  const { billing, status: billingStatus, retry: retryBilling } = useBilling();
  const activity = useActivityView(billing, versions, versionsFailed);

  const searchTours = useCallback((q: string) => {
    setGlobalSearch(q);
    // AA-671 — the search runs in the Marketplace's catalog view: also leave its "In my catalog" view (?view=mine).
    const onCatalog = pathname === ROUTES.tours && !window.location.search.includes("view=mine");
    if (!onCatalog) router.push(ROUTES.tours);
  }, [pathname, router]);

  const showToast = useCallback((msg: string) => toast.success(msg), [toast]);
  const refreshCatalogCount = useCallback(() => {
    qc.invalidateQueries({ queryKey: MY_VERSIONS_KEY });
  }, [qc]);

  const value: PortalShellValue = useMemo(() => ({
    tenantName, planTier, tenantStatus, poolTotal, catTotal, billing, billingStatus, retryBilling, activity,
    globalSearch, refreshCatalogCount, showToast,
  }), [tenantName, planTier, tenantStatus, poolTotal, catTotal, billing, billingStatus, retryBilling, activity,
    globalSearch, refreshCatalogCount, showToast]);

  return (
    <PortalShellContext.Provider value={value}>
      <ShellFrame
        drawerBelow={900}
        sidebarId="portal-sidebar"
        mainId="main"
        frameProps={{ "data-pshell": "frame" }}
        mainProps={{ "data-pshell": "main" }}
        sidebar={(c) => (
          <PortalSidebar
            drawerOpen={c.drawerOpen}
            poolCount={poolTotal}
            catalogCount={catTotal}
            onNavigate={() => { c.closeDrawer(); setGlobalSearch(""); }}
          />
        )}
        topbar={(c) => (
          <PortalTopbar
            tenantName={tenantName}
            planTier={planTier}
            activity={activity}
            onOpenSearch={c.openPalette}
            onOpenMenu={c.openDrawer}
            menuControls={c.sidebarId}
            menuExpanded={c.drawerOpen}
          />
        )}
        bottomNav={(c) => <PortalBottomNav onOpenMore={c.openDrawer} />}
        palette={(c) => <PortalCommandPalette open={c.paletteOpen} onClose={c.closePalette} onSearchTours={searchTours} />}
      >
        {children}
      </ShellFrame>
    </PortalShellContext.Provider>
  );
}
