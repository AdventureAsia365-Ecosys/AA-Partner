"use client";
// web/components/partners/TripPartnerPicker.tsx — AA-1071 (crm-056): the trip's sales partner on the trip workspace
// (PUT /crm/v1/trips/{id}/sales-partner; every member). The trip page reads the partner list on the server (GET
// /sales-partners: names only for the assistant) and hands it here. A booked trip with a partner adds the partner's
// commission to the trip's costs (the database does it, the Money card shows it). A closed trip shows the name only.
// The trip page has its own undo toast, not the kit's provider: the confirmation shows under the picker.
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button, K, Select, useHydrated } from "@/app/_kit";
import { bffSend } from "@/lib/bffClient";
import { t, type Lang, type MessageKey } from "@/lib/i18n";
import { partnerErrorKey } from "@/lib/partners";

export interface PickerPartner {
  id: string;
  name: string;
  active: boolean;
}

export function TripPartnerPicker({
  tripId,
  partnerId,
  partners,
  closed,
  lang,
}: {
  tripId: string;
  partnerId: string | null;
  partners: PickerPartner[];
  closed: boolean;
  lang: Lang;
}) {
  const router = useRouter();
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(partnerId ?? "");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);

  async function save() {
    setBusy(true);
    setError(null);
    const res = await bffSend("PUT", `trips/${tripId}/sales-partner`, { sales_partner_id: value || null });
    setBusy(false);
    if (!res.ok) {
      setError(tr(partnerErrorKey(res.detail)));
      return;
    }
    setSaved(true);
    setEditing(false);
    router.refresh();
  }

  const current = partners.find((p) => p.id === partnerId)?.name;
  if (!partnerId && partners.length === 0) return null; // no partner yet: nothing to pick (Partners page adds them)
  return (
    <div data-testid="trip-partner" style={{ display: "grid", gap: 6, marginTop: 12, minWidth: 0 }}>
      <span style={{ fontSize: 12, color: K.muted }}>{tr("partners.trip.label")}</span>
      {!editing ? (
        <span style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <span data-testid="trip-partner-value" style={{ fontSize: 13.5, overflowWrap: "anywhere" }}>
            {partnerId ? (current ?? "—") : tr("partners.trip.none")}
          </span>
          {!closed && (
            <Button size="sm" variant="ghost" disabled={!hydrated} onClick={() => setEditing(true)} testId="trip-partner-edit">
              {tr("partners.edit")}
            </Button>
          )}
        </span>
      ) : (
        <span style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", minWidth: 0 }}>
          <Select
            size="sm"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            data-testid="trip-partner-select"
            options={[
              { value: "", label: tr("partners.trip.none") },
              ...partners.filter((p) => p.active || p.id === partnerId).map((p) => ({ value: p.id, label: p.name })),
            ]}
          />
          <Button size="sm" disabled={busy} onClick={save} testId="trip-partner-save">
            {tr("partners.form.save")}
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
            {tr("partners.form.cancel")}
          </Button>
        </span>
      )}
      <span style={{ fontSize: 12, color: K.muted }}>{tr("partners.trip.help")}</span>
      {saved && (
        <span role="status" style={{ fontSize: 12, color: K.success }}>
          {tr("partners.trip.saved")}
        </span>
      )}
      {error && (
        <span role="alert" style={{ fontSize: 12, color: K.danger }}>
          {error}
        </span>
      )}
    </div>
  );
}
