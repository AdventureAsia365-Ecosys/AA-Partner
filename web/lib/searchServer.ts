// web/lib/searchServer.ts
// AA-963 (CRM-32) — the CRM layout's read of the global search config (GET /crm/v1/search/config) into the shell's
// `search` prop: null while search is off for the agency (or the read failed: fail closed). Content pieces join only
// when CIS lists this agency among the person's studios ("yes"). Server only (crmCall).
import type { ShellSearch } from "@/app/_kit/local/CrmShell";
import { crmCall } from "./crmApi";
import type { WebConfig } from "./config";
import { parseConfig } from "./search";
import type { Session } from "./session";
import type { StudioAccess } from "./studioAccess";

export async function shellSearch(
  cfg: WebConfig,
  session: Session | null,
  ctx: { tenantId: string; lang: "en" | "vi"; studio: StudioAccess },
): Promise<ShellSearch | null> {
  if (!session) return null;
  const read = await crmCall<unknown>(cfg, session, "GET", "search/config");
  const config = parseConfig(read.ok ? read.data : null);
  if (!config.enabled) return null;
  return { config, contentEnabled: ctx.studio === "yes", tenantId: ctx.tenantId, lang: ctx.lang };
}
