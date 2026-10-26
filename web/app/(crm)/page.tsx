// web/app/(crm)/page.tsx — Home (A-01, design v3 §2.2): KPI tiles, "Needs you", the getting-started checklist.
// AA-935 builds the frame with empty values; the numbers arrive with the cards that create trips (AA-936 and later).
import { CheckCircle2, Circle } from "lucide-react";
import { EmptyState, K } from "@/app/_kit";
import { Panel, PageTitle } from "@/components/PageTitle";
import { crmContext } from "@/lib/crmContext";
import { formatDate } from "@/lib/dates";
import { t, type MessageKey } from "@/lib/i18n";
import { contentStudioHref } from "@/lib/nav";

export default async function HomePage() {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { lang, locale } = ctx;
  const studio = contentStudioHref(ctx.cfg.contentStudioUrl, ctx.cfg.publicOrigin);
  const steps: { key: MessageKey; done: boolean; href?: string | null }[] = [
    { key: "home.check.signin", done: true },
    { key: "home.check.agency", done: true },
    { key: "home.check.inquiry", done: false, href: "/pipeline" },
    { key: "home.check.studio", done: false, href: studio },
  ];
  return (
    <div style={{ display: "grid", gap: 16 }} data-card="AA-935" data-testid="crm-home">
      <PageTitle
        title={`${t(lang, "home.title")} · ${ctx.me.display_name ?? ""}`}
        subtitle={`${formatDate(new Date(), locale)} · ${t(lang, "home.subtitle")}`}
      />
      <div style={{ display: "grid", gap: 16, gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))" }}>
        <Panel title={t(lang, "home.needs")} testId="crm-home-needs">
          <EmptyState title={t(lang, "home.needs")} description={t(lang, "home.needsEmpty")} />
        </Panel>
        <Panel title={t(lang, "home.checklist")} testId="crm-home-checklist">
          <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 10 }}>
            {steps.map((s) => (
              <li key={s.key} style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 13.5 }}>
                {s.done ? (
                  <CheckCircle2 size={18} color="var(--aa-green)" aria-hidden />
                ) : (
                  <Circle size={18} color="var(--aa-muted2)" aria-hidden />
                )}
                {s.href && !s.done ? (
                  <a href={s.href} style={{ color: K.accentDeep }}>
                    {t(lang, s.key)}
                  </a>
                ) : (
                  <span style={{ color: s.done ? K.muted : K.body, textDecoration: s.done ? "line-through" : "none" }}>
                    {t(lang, s.key)}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </Panel>
      </div>
    </div>
  );
}
