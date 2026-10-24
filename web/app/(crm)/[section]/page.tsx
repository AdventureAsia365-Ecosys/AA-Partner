// web/app/(crm)/[section]/page.tsx — every not-yet-built section of the agency IA (lib/nav.ts) as its empty state.
// Unknown section -> 404; a section the role may not see (Commissions / Marketing for the assistant) -> the
// "not available for your role" state. The card that fills the page is in data-card (for reviewers and smokes).
import { notFound } from "next/navigation";
import { EmptyState } from "@/app/_kit";
import { PageTitle } from "@/components/PageTitle";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import { sectionByKey } from "@/lib/nav";

export default async function SectionPage({ params }: { params: Promise<{ section: string }> }) {
  const { section: key } = await params;
  const section = sectionByKey(key);
  if (!section || !section.empty) notFound();
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { lang } = ctx;
  if (!section.roles.includes(ctx.role)) {
    return (
      <div data-testid="crm-section-forbidden">
        <PageTitle title={t(lang, section.label)} />
        <EmptyState title={t(lang, "section.forbidden.title")} description={t(lang, "section.forbidden.body")} />
      </div>
    );
  }
  return (
    <div data-testid={`crm-section-${section.key}`} data-card={section.card}>
      <PageTitle title={t(lang, section.label)} />
      <EmptyState title={t(lang, section.empty.title)} description={t(lang, section.empty.body)} />
    </div>
  );
}
