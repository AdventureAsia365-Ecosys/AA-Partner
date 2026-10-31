// web/app/(crm)/page.tsx — Home (A-01, design v3 §2.2): KPI tiles, "Needs you", the getting-started checklist.
// AA-935 builds the frame with empty values; the numbers arrive with the cards that create trips (AA-936 and later);
// AA-966: the KPI tiles (components/reports/HomeTiles.tsx).
// AA-962 (card CRM-31, Fix E2): the checklist is the agency's own (GET /crm/v1/onboarding): the three required steps,
// then the optional ones not done and not on "Later", each linking to its setup page; the full list, the marks and the
// shared-responsibility acknowledgement are on /onboarding.
import { CheckCircle2, Circle } from "lucide-react";
import Link from "next/link";
import { EmptyState, K } from "@/app/_kit";
import { Panel, PageTitle } from "@/components/PageTitle";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { formatDate } from "@/lib/dates";
import { t, type MessageKey } from "@/lib/i18n";
import { contentStudioHref } from "@/lib/nav";
import { HomeTiles } from "@/components/reports/HomeTiles";
import { serverContext } from "@/lib/serverContext";
import { fmt, t, type MessageKey } from "@/lib/i18n";
import { homeSteps, type OnboardingView } from "@/lib/onboarding";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

export default async function HomePage() {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { lang, locale } = ctx;
  const studio = contentStudioHref(ctx.cfg.contentStudioUrl, ctx.cfg.publicOrigin);
  const { session } = await serverContext();
  const steps: { key: MessageKey; done: boolean; href?: string | null }[] = [
    { key: "home.check.signin", done: true },
    { key: "home.check.agency", done: true },
    { key: "home.check.inquiry", done: false, href: "/pipeline" },
    { key: "home.check.studio", done: false, href: studio },
  ];
  const { session } = await serverContext();
  const onboarding = session ? await crmCall<OnboardingView>(ctx.cfg, session, "GET", "onboarding") : null;
  const view = onboarding && onboarding.ok ? onboarding.data : null;
  const steps = view ? homeSteps(view) : [];
  return (
    <div style={{ display: "grid", gap: 16 }} data-testid="crm-home">
      <PageTitle
        title={`${t(lang, "home.title")} · ${ctx.me.display_name ?? ""}`}
        subtitle={`${formatDate(new Date(), locale)} · ${t(lang, "home.subtitle")}`}
      />
      {session && <HomeTiles cfg={ctx.cfg} session={session} lang={lang} locale={locale} />}
      <div style={{ display: "grid", gap: 16, gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))" }}>
        <Panel title={t(lang, "home.needs")} testId="crm-home-needs">
          <EmptyState title={t(lang, "home.needs")} description={t(lang, "home.needsEmpty")} />
        </Panel>
        <Panel title={t(lang, "home.checklist")} testId="crm-home-checklist">
          {view ? (
            <>
              <p style={{ margin: "0 0 10px", fontSize: 13, color: K.muted }} data-testid="crm-home-progress">
                {fmt(t(lang, "onb.progress"), { done: view.required_done, total: view.required_total })}
              </p>
              <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 10 }}>
                {steps.map((s) => (
                  <li key={s.key} style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 13.5 }}>
                    {s.done ? (
                      <CheckCircle2 size={18} color={K.success} aria-hidden style={{ flex: "none" }} />
                    ) : (
                      <Circle size={18} color={K.muted2} aria-hidden style={{ flex: "none" }} />
                    )}
                    {s.done ? (
                      <span style={{ color: K.muted, textDecoration: "line-through" }}>{t(lang, `onb.step.${s.key}` as MessageKey)}</span>
                    ) : (
                      <a href={s.href} style={{ color: K.accentDeep }}>
                        {t(lang, `onb.step.${s.key}` as MessageKey)}
                      </a>
                    )}
                    {s.required && !s.done && <span style={{ fontSize: 11.5, color: K.muted }}>· {t(lang, "onb.required")}</span>}
                  </li>
                ))}
              </ul>
              <p style={{ margin: "12px 0 0", fontSize: 13 }}>
                <Link href="/onboarding" style={{ color: K.accentDeep, fontWeight: 500 }} data-testid="crm-home-onboarding">
                  {t(lang, "onb.open")} →
                </Link>
              </p>
            </>
          ) : (
            <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{t(lang, "onb.error")}</p>
          )}
        </Panel>
      </div>
    </div>
  );
}
