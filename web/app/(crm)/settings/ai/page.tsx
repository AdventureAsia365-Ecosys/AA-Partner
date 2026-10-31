// web/app/(crm)/settings/ai/page.tsx — Settings > AI (AA-964, card CRM-33a; design v3 A-18 "AI", R5 AI meter): whether
// the team may ask for AI drafts (off by default), the monthly AI budget, this month's use against it with the 75 / 90 /
// 100 % alerts, and how drafts work. Every role sees it; only the owner changes it (the API refuses the others too).
import Link from "next/link";
import { ErrorState } from "@/app/_kit";
import { PageTitle } from "@/components/PageTitle";
import { AiSettingsView } from "@/components/settings/AiSettingsView";
import type { AiSettings } from "@/lib/ai";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

export default async function AiSettingsPage() {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  if (!session) return null;
  const settings = await crmCall<AiSettings>(ctx.cfg, session, "GET", "settings/ai");
  return (
    <div data-testid="crm-ai-settings" style={{ display: "grid", gap: 14, maxWidth: 820 }}>
      <Link href="/settings" style={{ fontSize: 13 }}>
        ← {t(ctx.lang, "settings.title")}
      </Link>
      <PageTitle title={t(ctx.lang, "ai.title")} subtitle={t(ctx.lang, "ai.subtitle")} />
      {!settings.ok ? (
        <ErrorState message={t(ctx.lang, "error.api")} />
      ) : (
        <AiSettingsView settings={settings.data} lang={ctx.lang} locale={ctx.locale} />
      )}
    </div>
  );
}
