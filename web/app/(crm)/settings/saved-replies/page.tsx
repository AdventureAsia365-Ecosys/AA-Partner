// web/app/(crm)/settings/saved-replies/page.tsx — Settings > Saved replies (AA-945, card CRM-17).
import { ErrorState } from "@/app/_kit";
import { PageTitle } from "@/components/PageTitle";
import { SavedReplies } from "@/components/mail/SavedReplies";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import type { SavedReply } from "@/lib/mail";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

export default async function SavedRepliesPage() {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  if (!session) return null;
  const list = await crmCall<{ saved_replies: SavedReply[] }>(ctx.cfg, session, "GET", "saved-replies");
  return (
    <div data-testid="crm-saved-replies" data-card="AA-945" style={{ display: "grid", gap: 14, maxWidth: 820 }}>
      <a href="/settings" style={{ fontSize: 13 }}>
        ← {t(ctx.lang, "settings.title")}
      </a>
      <PageTitle title={t(ctx.lang, "replies.title")} subtitle={t(ctx.lang, "replies.subtitle")} />
      {!list.ok ? (
        <ErrorState message={t(ctx.lang, "error.api")} />
      ) : (
        <SavedReplies replies={list.data.saved_replies} role={ctx.role} lang={ctx.lang} />
      )}
    </div>
  );
}
