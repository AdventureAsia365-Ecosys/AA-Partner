// web/app/(crm)/layout.tsx
// AA-935 — every agency page: the signed-in agency context (lib/crmContext.ts: session, /me, memberships, locale)
// and the shell around the page. An unreachable API, or one that refuses a just-refreshed token, shows an error state
// instead of the shell.
import { ErrorState } from "@/app/_kit";
import { RecoveryBanner } from "@/components/team/RecoveryBanner";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { dictionary, t } from "@/lib/i18n";
import { contentStudioHref } from "@/lib/nav";
import { serverContext } from "@/lib/serverContext";
import type { Recovery } from "@/lib/team";
import { CrmShell } from "./_shell/CrmShell";

export const dynamic = "force-dynamic";

export default async function CrmLayout({ children }: { children: React.ReactNode }) {
  const ctx = await crmContext();
  if (ctx.apiDown) {
    const refused = ctx.reason === "refused";
    return (
      <main
        style={{ minHeight: "100vh", display: "grid", placeItems: "center" }}
        data-testid={refused ? "crm-session-refused" : "crm-api-down"}
      >
        <div style={{ display: "grid", justifyItems: "center", gap: 8 }}>
          <ErrorState message={t(ctx.lang, refused ? "error.session" : "error.api")} />
          <a href={refused ? "/auth/sign-in?next=%2F" : "/"} style={{ fontSize: 13 }}>
            {t(ctx.lang, refused ? "auth.signIn" : "error.retry")}
          </a>
        </div>
      </main>
    );
  }
  const active = ctx.memberships.filter((m) => m.tenant_status === "active").length;
  // AA-939 (Q1 6.3): a pending owner recovery is shown to every member, with Cancel. A failed read shows no banner.
  const { session } = await serverContext();
  const pending = session ? await crmCall<{ recovery: Recovery | null }>(ctx.cfg, session, "GET", "recovery") : null;
  const recovery = pending && pending.ok ? pending.data.recovery : null;
  return (
    <CrmShell
      dict={dictionary(ctx.lang)}
      role={ctx.role}
      agencyName={ctx.me.display_name ?? ctx.me.tenant_id}
      agencyCount={active}
      contentStudioHref={contentStudioHref(ctx.cfg.contentStudioUrl)}
      locale={ctx.locale}
      syntheticBadge={ctx.cfg.environment !== "prod"}
    >
      {recovery && <RecoveryBanner recovery={recovery} lang={ctx.lang} locale={ctx.locale} />}
      {children}
    </CrmShell>
  );
}
