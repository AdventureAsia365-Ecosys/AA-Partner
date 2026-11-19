// web/components/home/NeedsYou.tsx — AA-1129 (P1, S283 audit row 2): Home's "Needs you" panel, built from the agenda
// read (GET /reports/agenda `needs_you` + overdue instalments): the caller's overdue tasks, late payments, tasks due
// today and trips waiting on them, each a link to the page that lists them. Was a fixed empty state. Server component.
import Link from "next/link";
import { Badge, K } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { needsYou, type AgendaView } from "@/lib/finance";
import { plural, t, type Lang } from "@/lib/i18n";

export function NeedsYou({ agenda, lang }: { agenda: AgendaView | null; lang: Lang }) {
  const items = agenda ? needsYou(agenda) : [];
  return (
    <Panel title={t(lang, "home.needs")} testId="crm-home-needs">
      {items.length === 0 ? (
        <p style={{ margin: 0, fontSize: 13, color: K.muted }} data-testid="crm-home-needs-none">
          {t(lang, "home.needs.none")}
        </p>
      ) : (
        <>
          <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 8 }} data-testid="crm-home-needs-list">
            {items.map((i) => (
              <li key={i.key} style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", fontSize: 13.5 }}>
                <Badge tone={i.tone === "neutral" ? "info" : i.tone}>{i.n}</Badge>
                <Link href={i.href} style={{ color: K.accentDeep }} data-testid={`crm-home-needs-${i.key.split(".").pop()}`}>
                  {plural(lang, i.key, i.n)}
                </Link>
              </li>
            ))}
          </ul>
          <p style={{ margin: "10px 0 0", fontSize: 12, color: K.muted }}>{t(lang, "home.needs.mine")}</p>
        </>
      )}
    </Panel>
  );
}
