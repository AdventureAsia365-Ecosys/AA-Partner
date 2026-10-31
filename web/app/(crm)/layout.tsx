// web/app/(crm)/layout.tsx
// AA-935 — every agency page: the signed-in agency context (lib/crmContext.ts: session, /me, memberships, locale)
// and the shell around the page. An unreachable API, or one that refuses a just-refreshed token, shows an error state
// instead of the shell.
import { ErrorState } from "@/app/_kit";
import { MailboxBanner } from "@/components/mail/MailboxBanner";
import { SupportBanner } from "@/components/support/SupportBanner";
import { RecoveryBanner } from "@/components/team/RecoveryBanner";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { dictionary, t } from "@/lib/i18n";
import { contentStudioHref } from "@/lib/nav";
import { serverContext } from "@/lib/serverContext";
import { disconnected, type MailboxView } from "@/lib/mail";
import { bannerState, type SupportAccess } from "@/lib/supportAccess";
import type { Recovery } from "@/lib/team";
import { CrmShell } from "@/app/_kit/local/CrmShell";

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
  // AA-945 (R24): a disconnected mailbox holds every client message; the red banner says so on every page.
  const mail = session ? await crmCall<MailboxView>(ctx.cfg, session, "GET", "mailbox") : null;
  const down = disconnected(mail && mail.ok ? mail.data : null);
  // AA-971 (design §2.3): AA support (amber) or AA emergency access (red) in the agency, on every page, not dismissible.
  const support = session ? await crmCall<SupportAccess>(ctx.cfg, session, "GET", "support-access") : null;
  const supportView = support && support.ok ? support.data : null;
  const banner = bannerState(supportView);
  return (
    <CrmShell
      dict={dictionary(ctx.lang)}
      role={ctx.role}
      agencyName={ctx.me.display_name ?? ctx.me.tenant_id}
      agencyCount={active}
      contentStudioHref={contentStudioHref(ctx.cfg.contentStudioUrl, ctx.cfg.publicOrigin)}
      locale={ctx.locale}
      syntheticBadge={ctx.cfg.environment !== "prod"}
    >
      {banner && (
        <SupportBanner state={banner} canManage={!!supportView?.can_manage} lang={ctx.lang} locale={ctx.locale} timeZone={supportView?.timezone ?? "UTC"} />
      )}
      {recovery && <RecoveryBanner recovery={recovery} lang={ctx.lang} locale={ctx.locale} />}
      {down.length > 0 && <MailboxBanner lang={ctx.lang} />}
      {children}
    </CrmShell>
  );
}
