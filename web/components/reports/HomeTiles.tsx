// web/components/reports/HomeTiles.tsx — the A-01 Home KPI tiles (AA-966, design v3 "Home shows the same 4 tiles"):
// inquiries, win rate, sold and margin (owner / agent; the API leaves the money tiles out for the assistant) and the
// Adventure Asia share, over the last 12 months, each opening its work list. A failed read shows nothing: Home stays
// usable when the reports API is slow or down.
import Link from "next/link";
import { K } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { TileRow } from "@/components/reports/ReportParts";
import { crmCall } from "@/lib/crmApi";
import type { WebConfig } from "@/lib/config";
import type { DateLocale } from "@/lib/dates";
import { type Lang, t } from "@/lib/i18n";
import type { TilesView } from "@/lib/reports";
import type { Session } from "@/lib/session";

export async function HomeTiles({
  cfg,
  session,
  lang,
  locale,
}: {
  cfg: WebConfig;
  session: Session;
  lang: Lang;
  locale: DateLocale;
}) {
  const view = await crmCall<TilesView>(cfg, session, "GET", "reports/tiles");
  if (!view.ok || !view.data.tiles.length) return null;
  return (
    <Panel title={t(lang, "home.tiles")} testId="crm-home-tiles">
      <TileRow
        tiles={view.data.tiles}
        compare={view.data.period.compare}
        currency={view.data.period.base_currency}
        lang={lang}
        locale={locale}
      />
      <p style={{ margin: "10px 0 0", fontSize: 13 }}>
        <Link href="/reports" style={{ color: K.accentDeep }}>
          {t(lang, "home.tiles.more")}
        </Link>
      </p>
    </Panel>
  );
}
