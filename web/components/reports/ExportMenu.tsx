"use client";
// web/components/reports/ExportMenu.tsx — A-14 "[Export CSV]" (AA-966): pick a section, download it as CSV through the
// BFF (the API answers with the same numbers as the page, a spreadsheet formula guard on every text cell, and one
// access-log event per download). The money sections are offered only to roles that see margins; the API refuses
// them to anyone else anyway. AA-1116: the CSV export is the plan's insights_export; without it the menu is locked
// with a hint to ask AA (the API answers 403 feature_not_allowed anyway).
import { Lock } from "lucide-react";
import { useState } from "react";
import { Button, K, Select } from "@/app/_kit";
import { bffDownload } from "@/lib/bffClient";
import { type Lang, t, type MessageKey } from "@/lib/i18n";
import { type ExportSection, type Period, exportFilename, exportPath, exportableSections } from "@/lib/reports";

export function ExportMenu({
  lang,
  period,
  owner,
  moneyVisible,
  allowed = true,
}: {
  lang: Lang;
  period: Period;
  owner: string | null;
  moneyVisible: boolean;
  allowed?: boolean;
}) {
  const sections = exportableSections(moneyVisible);
  const [section, setSection] = useState<ExportSection>(sections[0]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function download() {
    setBusy(true);
    setError(null);
    try {
      // AA-1014: an access token that expired since the page loaded is renewed in the background, then retried; when
      // that fails too the page is already on its way to sign in again (`redirecting`), so no error flashes first.
      const res = await bffDownload(exportPath(section, { period, owner }), { filename: exportFilename(section, period) });
      if (!res.ok && !res.redirecting) setError(t(lang, "reports.export.error"));
    } finally {
      setBusy(false);
    }
  }

  if (!allowed) return <ExportLocked lang={lang} testId="reports-export-locked" label={t(lang, "reports.export")} />;

  return (
    <div data-testid="reports-export" style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8 }}>
      <Select
        size="sm"
        aria-label={t(lang, "reports.export.section")}
        value={section}
        onChange={(e) => setSection(e.target.value as ExportSection)}
        options={sections.map((s) => ({ value: s, label: t(lang, `reports.section.${s}` as MessageKey) }))}
      />
      <Button type="button" size="sm" variant="secondary" disabled={busy} onClick={download}>
        {busy ? t(lang, "reports.export.busy") : t(lang, "reports.export")}
      </Button>
      {error && (
        <span role="alert" style={{ color: K.danger, fontSize: 12.5 }}>
          {error}
        </span>
      )}
    </div>
  );
}

/** AA-1116: a CSV download the agency's plan does not have: the button stays visible, disabled, with the hint. */
export function ExportLocked({ lang, testId, label }: { lang: Lang; testId: string; label: string }) {
  const hint = t(lang, "reports.export.locked");
  return (
    <div data-testid={testId} style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8, minWidth: 0 }}>
      <Button type="button" size="sm" variant="secondary" disabled title={hint}>
        <Lock size={13} aria-hidden="true" /> {label}
      </Button>
      <span style={{ color: K.muted, fontSize: 12.5 }}>{hint}</span>
    </div>
  );
}
