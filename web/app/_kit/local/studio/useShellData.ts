"use client";
// web/app/_kit/local/studio/useShellData.ts
// AA-759 — the shell's data, all through react-query (was four raw fetches in a mount effect).
//
//   - tenant identity: GET /api/tenant/me (the tenant cookies are httpOnly, AA-427);
//   - Marketplace (catalog) total: GET /v1/tours/pool?page_size=1 → pagination.total;
//   - My Tours count: the SAME query as the My Tours page (key ["my-versions"], fetchMyVersions),
//     counted as unique tours (AA-566 Phần A) — one reading of one endpoint, so the sidebar badge
//     and the page heading can never disagree, and a page refetch refreshes the badge;
//   - billing + activity: GET /v1/billing (tenant-scoped, AA-496).

import { useCallback, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiGet, useT } from "@/app/_kit";
import { STUDIO_MESSAGES } from "./studio.messages";
import { fetchMyVersions, type Version } from "@/app/(studio)/portal/_components/catalogApi";
import { countUniqueTours } from "@/lib/studio/format";
import { activePollInterval, activityState, settledActivityState, tourWorkStatus } from "@/app/(studio)/portal/_components/workStatus";
import type { ShellActivityView, ShellBilling } from "@/app/(studio)/portal/_components/PortalShellContext";

export const MY_VERSIONS_KEY = ["my-versions"] as const;

interface TenantMe { tenant_name?: string; plan_tier?: string; content_studio_role?: string | null }

/** react-query's own status words: "pending" (no answer yet) | "error" | "success". */
export type LoadStatus = "pending" | "error" | "success";

/** AA-1036 (R3) — the person's Content studio role (owner | editor | viewer), null until /api/tenant/me answers or when
 *  it has none. Display only: the Add-ons page shows the request form to the owner (CIS enforces it). Same query key as
 *  useTenantMe, so one call serves both. */
export function useStudioRole(): string | null {
  const { data } = useQuery({
    queryKey: ["tenant-me"],
    queryFn: () => apiGet<TenantMe>("/api/tenant/me"),
    staleTime: 5 * 60_000,
  });
  return data?.content_studio_role ?? null;
}

/** AA-760 — the tenant name is "" while /api/tenant/me is loading (the shell shows a skeleton, not a
 *  generic name); the "Partner" fallback is used only once the call has answered without a name. */
export function useTenantMe(): { tenantName: string; planTier: string; status: LoadStatus } {
  const t = useT(STUDIO_MESSAGES);
  const { data, status } = useQuery({
    queryKey: ["tenant-me"],
    queryFn: () => apiGet<TenantMe>("/api/tenant/me"),
    staleTime: 5 * 60_000,
  });
  return {
    tenantName: data?.tenant_name || (status === "pending" ? "" : t("partnerFallback")),
    planTier: data?.plan_tier || "",
    status,
  };
}

/** The Marketplace catalog total; null until the pool answers (the sidebar shows no count, not 0). */
export function usePoolTotal(): number | null {
  const { data } = useQuery({
    queryKey: ["pool-total"],
    queryFn: () => apiGet<{ pagination?: { total?: number } }>("/api/tenant/v1/tours/pool?page_size=1"),
    staleTime: 5 * 60_000,
  });
  return data ? (data.pagination?.total ?? 0) : null;
}

/** The tenant's versions (shared ["my-versions"] query, polled while a rewrite runs) + the count. */
export function useCatalog(): { versions: Version[] | undefined; count: number; failed: boolean } {
  // AA-760 — the badge was stale after a rewrite finished in the background (it only refreshed when
  // the My Tours page refetched). The shell's own observer now polls the shared query while any
  // version is queued / writing, and stops once all are terminal (shared work-status model).
  const { data, isError } = useQuery({
    queryKey: MY_VERSIONS_KEY,
    queryFn: fetchMyVersions,
    refetchInterval: (q) => activePollInterval((q.state.data ?? []).map(tourWorkStatus), 10_000),
  });
  return { versions: data, count: data ? countUniqueTours(data) : 0, failed: isError && !data };
}

/** AA-760 — the activity feed with each row's state read from its version's job. */
export function useActivityView(
  billing: ShellBilling | null, versions: Version[] | undefined, versionsFailed = false,
): ShellActivityView[] {
  return useMemo(() => {
    const byId = new Map((versions ?? []).map((v) => [v.id, v]));
    return (billing?.activity ?? []).map((a) => {
      const v = byId.get(a.id);
      // Versions loaded but this one is not among them (deleted / archived), or the versions query
      // failed (the row would otherwise show "Loading status" for ever): a settled row.
      const state = (versions && !v) || versionsFailed ? settledActivityState(a) : activityState(a, v);
      return { ...a, state, version_number: v?.version_number ?? null };
    });
  }, [billing, versions, versionsFailed]);
}

/** AA-760 — the billing record plus its load state: pages must tell "still loading" and "failed"
 *  apart from "no data" (Billing showed $0 / "Custom pricing", Activity "No activity yet" while the
 *  call was still in flight). */
export function useBilling(): { billing: ShellBilling | null; status: LoadStatus; retry: () => void } {
  const { data, status, refetch } = useQuery({
    queryKey: ["tenant-billing"],
    queryFn: () => apiGet<ShellBilling>("/api/tenant/v1/billing"),
  });
  const retry = useCallback(() => { void refetch(); }, [refetch]);
  return { billing: data ?? null, status, retry };
}
