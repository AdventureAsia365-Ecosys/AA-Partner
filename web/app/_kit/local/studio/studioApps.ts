// web/app/_kit/local/studio/studioApps.ts
// AA-1036 (R3) — the one fetch of GET /api/studio/apps (lib/apps.ts) behind the react-query key ["studio-apps"]: the
// sidebar's app switcher reads `apps`, Account › Add-ons reads `crmMember` (show "Open CRM" only to a person who holds a
// CRM role). One key, one shape, so both share one call. The route always answers 200; anything else reads as empty.
import type { AppEntry } from "@/lib/apps";

export const STUDIO_APPS_KEY = ["studio-apps"] as const;

export interface StudioApps { apps: AppEntry[]; crmMember: boolean }

export async function fetchStudioApps(): Promise<StudioApps> {
  const res = await fetch("/api/studio/apps", { cache: "no-store", headers: { Accept: "application/json" } });
  if (!res.ok) return { apps: [], crmMember: false };
  const body = (await res.json()) as { apps?: AppEntry[]; crm_member?: unknown };
  return { apps: Array.isArray(body.apps) ? body.apps : [], crmMember: body.crm_member === true };
}
