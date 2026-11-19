"use client";
// web/app/(studio)/portal/account/trip-planner/page.tsx — AA-1139 (fix pass): Account › Trip planner in the Content
// studio, for an agency WITHOUT the CRM add-on (it has no CRM agency, so the CRM's Settings › Add-ons › Trip planner
// cannot serve it), and the page where its e-mail / webhook lead route really takes effect. Same view as the CRM page
// (components/settings/TripPlannerModuleView.tsx), fed by the studio's route handler (app/api/studio/trip-planner:
// CIS for the site settings with the studio token, the CRM API for TripPlanner's usage, leads and key with the
// person's partner token; lib/studio/tripPlanner.ts). The owner and editors see it; only the owner changes it (CIS and
// the CRM API check again). Kit: PageHeader, ErrorState, SkeletonBar; react-query keys ["studio-trip-planner", ...].
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, apiGet, apiSend, ErrorState, PageHeader, SkeletonBar, sans, useLang } from "@/app/_kit";
import { useStudioRole } from "@/app/_kit/local/studio/useShellData";
import { TripPlannerModuleView, type PlannerSend } from "@/components/settings/TripPlannerModuleView";
import { ADD_ONS_PATH } from "@/lib/apps";
import { t } from "@/lib/i18n";
import { STUDIO_TRIP_PLANNER_API, studioPlannerUrl } from "@/lib/studio/tripPlanner";
import { loadErrorKey, type PlannerLeadList, type PlannerPage } from "@/lib/tripPlannerModule";

const PAGE_KEY = ["studio-trip-planner"] as const;
const LEADS_KEY = ["studio-trip-planner", "leads"] as const;

function failure(e: unknown): { ok: false; status: number; detail: string } {
  if (e instanceof ApiError) return { ok: false, status: e.status, detail: e.code || e.detail || "error" };
  return { ok: false, status: 0, detail: "network" };
}

const studioSend: PlannerSend = async <T,>(method: "PUT" | "POST", path: string, body?: unknown) => {
  try {
    return { ok: true as const, data: await apiSend<T>(studioPlannerUrl(path), { method, body }) };
  } catch (e) {
    return failure(e);
  }
};

export default function StudioTripPlannerPage() {
  const lang = useLang();
  const role = useStudioRole();
  const qc = useQueryClient();
  const canSee = role === "owner" || role === "editor";
  const page = useQuery({
    queryKey: PAGE_KEY,
    queryFn: () => apiGet<PlannerPage>(STUDIO_TRIP_PLANNER_API),
    enabled: canSee,
    retry: false,
  });
  const active = page.data?.module.active === true;
  const leads = useQuery({
    queryKey: LEADS_KEY,
    queryFn: () => apiGet<PlannerLeadList>(`${STUDIO_TRIP_PLANNER_API}/leads`),
    enabled: canSee && active,
    retry: false,
  });
  const err = page.error ? failure(page.error) : null;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12, fontFamily: sans, maxWidth: 960 }}
         data-testid="studio-trip-planner">
      <PageHeader title={t(lang, "tp.title")} description={t(lang, "tp.subtitle")}
                  breadcrumbs={[{ label: t(lang, "addons.title"), href: ADD_ONS_PATH }, { label: t(lang, "tp.title") }]} />
      {role !== null && !canSee && (
        <p data-testid="tp-no-access" style={{ margin: 0, fontSize: 13 }}>{t(lang, "tp.err.noAccess")}</p>
      )}
      {(role === null || (canSee && page.isLoading)) && <SkeletonBar h={160} r={12} />}
      {err && <ErrorState message={t(lang, loadErrorKey(err.status, err.detail))} onRetry={() => void page.refetch()} />}
      {page.data && (
        <TripPlannerModuleView
          page={page.data}
          leads={leads.data ?? null}
          owner={role === "owner"}
          lang={lang}
          locale={lang === "vi" ? "vi-VN" : "en-GB"}
          send={studioSend}
          onRefresh={() => void qc.invalidateQueries({ queryKey: PAGE_KEY })}
        />
      )}
    </div>
  );
}
