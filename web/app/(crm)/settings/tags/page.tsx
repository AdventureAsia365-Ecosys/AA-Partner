// web/app/(crm)/settings/tags/page.tsx — Settings > Tags (AA-960, card CRM-29b; crm-061): the agency's trip and client
// tags with their use counts and the app's system tags. Every role reads it; only the owner renames, archives or
// deletes a tag (the API refuses the others); any member adds a new tag from a trip or a client.
import Link from "next/link";
import { ErrorState } from "@/app/_kit";
import { PageTitle } from "@/components/PageTitle";
import { TagsSettings } from "@/components/tags/TagsSettings";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import { serverContext } from "@/lib/serverContext";
import type { TagVocabulary } from "@/lib/tags";

export const dynamic = "force-dynamic";

export default async function TagsSettingsPage() {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  if (!session) return null;
  const view = await crmCall<TagVocabulary>(ctx.cfg, session, "GET", "tags");
  return (
    <div data-testid="crm-tags-settings" style={{ display: "grid", gap: 14, maxWidth: 900 }}>
      <Link href="/settings" style={{ fontSize: 13 }}>
        ← {t(ctx.lang, "settings.title")}
      </Link>
      <PageTitle title={t(ctx.lang, "tag.settings.title")} subtitle={t(ctx.lang, ctx.role === "owner" ? "tag.settings.subtitle" : "tag.settings.ownerOnly")} />
      {view.ok ? <TagsSettings initial={view.data} lang={ctx.lang} /> : <ErrorState message={t(ctx.lang, "error.api")} />}
    </div>
  );
}
