// web/components/staff/OpenStep.tsx — AA-1019 review finding B1. A console page whose render counts a view or writes the
// agency's access log (S-02 one release, S-04 agency and trip) shows this instead of reading when the browser came
// from another origin (lib/staffBff.ts foreignNavigation): a link on another site must not make a staff member's
// browser count or audit anything. The "Open" link is a same-origin navigation of the console, so the read then runs.
// prefetch off: a prefetch is a render, and a render here is the counted read.
import Link from "next/link";
import { K } from "@/app/_kit";
import { Panel, PageTitle } from "@/components/PageTitle";
import { t, type Lang } from "@/lib/i18n";

export function OpenStep({ href, back, lang }: { href: string; back: React.ReactNode; lang: Lang }) {
  return (
    <div style={{ display: "grid", gap: 12, gridTemplateColumns: "minmax(0, 1fr)" }} data-testid="staff-open-step">
      {back}
      <PageTitle title={t(lang, "staff.openStep.title")} />
      <Panel>
        <div style={{ display: "grid", gap: 10, fontSize: 13.5, color: K.body }}>
          <p style={{ margin: 0 }}>{t(lang, "staff.openStep.body")}</p>
          <Link href={href} prefetch={false} style={{ fontWeight: 600, color: K.accentDeep }} data-testid="staff-open-step-link">
            {t(lang, "staff.openStep.open")} →
          </Link>
        </div>
      </Panel>
    </div>
  );
}
