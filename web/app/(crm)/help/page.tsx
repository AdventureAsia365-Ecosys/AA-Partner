// web/app/(crm)/help/page.tsx — Help (AA-962, card CRM-31 "help links"; R43 in-app): the getting-started guides each
// onboarding step and connection links to (/help#<topic>). The full help centre with videos arrives with its own card;
// this page replaces the Help empty state with these guides.
import Link from "next/link";
import { Panel, PageTitle } from "@/components/PageTitle";
import { K } from "@/app/_kit";
import { crmContext } from "@/lib/crmContext";
import { t, type MessageKey } from "@/lib/i18n";

const TOPICS = [
  "mailbox",
  "team",
  "form",
  "import",
  "duplicates",
  "payments",
  "templates",
  "privacy",
  "kickoff",
  "sms",
  "calendar",
  "esign",
] as const;

export default async function HelpPage() {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { lang } = ctx;
  return (
    <div data-testid="crm-help" style={{ display: "grid", gap: 14, maxWidth: 820 }}>
      <PageTitle title={t(lang, "onb.helpTitle")} subtitle={t(lang, "onb.helpSubtitle")} />
      <p style={{ margin: 0, fontSize: 13.5 }}>
        <Link href="/onboarding" style={{ color: K.accentDeep }}>
          {t(lang, "onb.open")} →
        </Link>
      </p>
      {TOPICS.map((topic) => (
        <section key={topic} id={topic} style={{ scrollMarginTop: 72 }}>
          <Panel title={t(lang, `onb.h.${topic}.t` as MessageKey)} testId={`help-${topic}`}>
            <p style={{ margin: 0, fontSize: 13.5, color: K.body, lineHeight: 1.55 }}>{t(lang, `onb.h.${topic}.b` as MessageKey)}</p>
          </Panel>
        </section>
      ))}
    </div>
  );
}
