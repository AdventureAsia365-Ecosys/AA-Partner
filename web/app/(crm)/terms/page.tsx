// web/app/(crm)/terms/page.tsx — AA-1033: the CRM terms of use (draft). Today it holds one section, "AI-generated
// content" (id "ai", linked from Settings > AI and the shell footer), modelled on the Content studio terms §3
// (portal/app/terms/page.tsx): AI drafts can contain mistakes, a draft is for information, a person in the agency
// reviews and sends it, and the confirmed quote and booking terms are what binds. Plain, honest text; it does not claim
// to exclude liability, and the final legal wording is reviewed by counsel later. Strings: lib/i18nTerms.ts (EN + VI).
import { K } from "@/app/_kit";
import { Panel, PageTitle } from "@/components/PageTitle";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import { AI_TERMS } from "@/lib/i18nTerms";

export default async function TermsPage() {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { lang } = ctx;
  return (
    <div data-testid="crm-terms" style={{ display: "grid", gap: 14, maxWidth: 820 }}>
      <PageTitle title={t(lang, "terms.title")} subtitle={t(lang, "terms.subtitle")} />
      <section id="ai" style={{ scrollMarginTop: 72 }}>
        <Panel title={`1. ${t(lang, "terms.ai.title")}`} testId="terms-ai">
          <ul style={{ margin: 0, paddingLeft: 18, display: "grid", gap: 8, fontSize: 13.5, color: K.body, lineHeight: 1.55 }}>
            {AI_TERMS.map((key) => (
              <li key={key}>{t(lang, key)}</li>
            ))}
          </ul>
        </Panel>
      </section>
      <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{t(lang, "terms.more")}</p>
    </div>
  );
}
