"use client";
// web/components/finance/ProfitExport.tsx — AA-1068 (crm-31): the profit cube as CSV (GET /reports/export?section=profit,
// the same rows as the page plus the total; one access-log event per download). Owner and agent only.
import { useState } from "react";
import { Button, K } from "@/app/_kit";
import { ExportLocked } from "@/components/reports/ExportMenu";
import { bffDownload } from "@/lib/bffClient";
import { type Dimension, profitExportPath } from "@/lib/finance";
import { type Lang, t } from "@/lib/i18n";

// AA-1116: locked (disabled, with the hint) when the agency's plan has no insights_export.
export function ProfitExport({
  lang,
  period,
  owner,
  dimension,
  allowed = true,
}: {
  lang: Lang;
  period: string;
  owner: string | null;
  dimension: Dimension;
  allowed?: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);

  async function download() {
    setBusy(true);
    setError(false);
    try {
      const stamp = new Date().toISOString().slice(0, 10).replace(/-/g, "");
      const res = await bffDownload(profitExportPath(period, dimension, owner), { filename: `report-profit-${dimension}-${period}-${stamp}.csv` });
      if (!res.ok && !res.redirecting) setError(true);
    } finally {
      setBusy(false);
    }
  }

  if (!allowed) return <ExportLocked lang={lang} testId="profit-export-locked" label={t(lang, "profit.export")} />;

  return (
    <span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
      <Button size="sm" variant="ghost" disabled={busy} onClick={download} testId="profit-export">
        {t(lang, "profit.export")}
      </Button>
      {error && (
        <span role="alert" style={{ fontSize: 12, color: K.danger }}>
          {t(lang, "profit.exportError")}
        </span>
      )}
    </span>
  );
}
