// web/app/(crm)/library/page.tsx — Library (A-16, design R9; AA-963 CRM-32): the picker of the agency's ready-made
// texts and steps. The libraries themselves are live on their own pages (message templates with the AA starters,
// the AA workflow library, the Included / Not included lines); this page lists them and adds the Content studio's
// pieces for a person CIS lists in this agency's studio (the same check as the sidebar's studio link). Every role.
import Link from "next/link";
import { Section } from "@/app/_kit";
import { PageTitle } from "@/components/PageTitle";
import { crmContext } from "@/lib/crmContext";
import { t, type MessageKey } from "@/lib/i18n";
import { contentStudioHref } from "@/lib/nav";
import { libraryEntries } from "@/lib/search";
import { serverContext } from "@/lib/serverContext";
import { studioAccess, studioLink } from "@/lib/studioAccess";

export const dynamic = "force-dynamic";

export default async function LibraryPage() {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  const studioHref = contentStudioHref(ctx.cfg.contentStudioUrl, ctx.cfg.publicOrigin);
  const studio = studioHref && session ? await studioAccess(ctx.cfg.cisApiUrl, session) : "unknown";
  const entries = libraryEntries(studio === "yes" ? studioLink(studioHref, studio) : null);
  const { lang } = ctx;
  return (
    <div data-testid="crm-section-library" style={{ display: "grid", gap: 14, maxWidth: 820 }}>
      <PageTitle title={t(lang, "library.title")} subtitle={t(lang, "library.subtitle")} />
      <div style={{ display: "grid", gap: 12, gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 260px), 1fr))" }}>
        {entries.map((e) => (
          <Section
            key={e.key}
            testId={`crm-library-${e.key}`}
            title={t(lang, `library.${e.key}` as MessageKey)}
            description={t(lang, `library.${e.key}Body` as MessageKey)}
          >
            {e.external ? (
              <a href={e.href} style={{ fontSize: 13 }}>
                {t(lang, "library.open")} →
              </a>
            ) : (
              <Link href={e.href} style={{ fontSize: 13 }}>
                {t(lang, "library.open")} →
              </Link>
            )}
          </Section>
        ))}
      </div>
    </div>
  );
}
