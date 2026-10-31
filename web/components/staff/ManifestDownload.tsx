"use client";
// web/components/staff/ManifestDownload.tsx — S-02, the minimal supplier list of one release (AA-1019 on the AA-956 API
// GET .../ops-release/manifest?fields=): the staff member picks the columns (default the API's four), the API answers
// the CSV text with a watermark on every row and logs the export (the agency sees it); the browser saves it as a file
// and nothing is stored on this server. The watermark and row count are shown after the download.
import { useState } from "react";
import { Button, Checkbox, Field, K, useHydrated } from "@/app/_kit";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import {
  MANIFEST_DEFAULT,
  MANIFEST_FIELDS,
  manifestFilename,
  manifestQuery,
  pickFields,
  refusalKey,
  type Manifest,
  type ManifestField,
} from "@/lib/staffConsole";
import { staffSend } from "@/lib/staffClient";

export function ManifestDownload({ tenantId, tripId, lang }: { tenantId: string; tripId: string; lang: Lang }) {
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [picked, setPicked] = useState<ManifestField[]>([...MANIFEST_DEFAULT]);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  function toggle(f: ManifestField, on: boolean) {
    setPicked((now) => pickFields(on ? [...now, f] : now.filter((x) => x !== f)));
  }

  async function download() {
    if (picked.length === 0) {
      setError(tr("staff.manifest.pickOne"));
      return;
    }
    setBusy(true);
    setError(null);
    setDone(null);
    const path = `agencies/${tenantId}/trips/${tripId}/ops-release/manifest`;
    const res = await staffSend<Manifest>("GET", path, { query: manifestQuery(picked) });
    setBusy(false);
    if (!res.ok) {
      setError(tr(refusalKey(res.detail)));
      return;
    }
    const url = URL.createObjectURL(new Blob([res.data.csv], { type: "text/csv;charset=utf-8" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = manifestFilename(res.data);
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    setDone(fmt(tr("staff.manifest.done"), { rows: res.data.row_count, mark: res.data.watermark }));
  }

  return (
    <div style={{ display: "grid", gap: 10 }} data-testid="staff-manifest" data-hydrated={hydrated ? "true" : undefined}>
      <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("staff.manifest.help")}</p>
      <Field label={tr("staff.manifest.fields")} as="div">
        <div style={{ display: "flex", gap: "6px 14px", flexWrap: "wrap" }}>
          {MANIFEST_FIELDS.map((f) => (
            <Checkbox
              key={f}
              label={tr(`field.${f}`)}
              checked={picked.includes(f)}
              onChange={(e) => toggle(f, e.currentTarget.checked)}
            />
          ))}
        </div>
      </Field>
      <div>
        <Button variant="primary" size="sm" disabled={!hydrated || busy} onClick={download} testId="staff-manifest-download">
          {tr("staff.manifest.download")}
        </Button>
      </div>
      {done && (
        <p role="status" data-testid="staff-manifest-done" style={{ margin: 0, fontSize: 13, color: K.success }}>
          {done}
        </p>
      )}
      {error && (
        <p role="alert" style={{ margin: 0, fontSize: 13, color: K.danger }}>
          {error}
        </p>
      )}
    </div>
  );
}
