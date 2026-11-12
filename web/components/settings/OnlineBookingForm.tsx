"use client";
// web/components/settings/OnlineBookingForm.tsx — Settings > Payments, "Online booking" (AA-1072, item crm-02): the
// owner's "Book now" switch (off by default, the business-owner default) and the form keys to put in the AA tour page
// link (<client host>/trip/<tour id>?f=<form key>): the key names the agency on the public page. AA-1092: the agency's
// price checks a minute (every visitor together; the ceiling that keeps a busy or abused page from flooding AA's quotes).
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button, Checkbox, Field, Input, K, useHydrated } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";

export interface OnlineBookingView {
  enabled: boolean;
  price_checks_per_minute: number;
  price_checks_bounds: { min: number; max: number; default: number };
  page_base: string | null;
  forms: { label: string; public_key: string }[];
}

export function OnlineBookingForm({ view, lang }: { view: OnlineBookingView; lang: Lang }) {
  const router = useRouter();
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [enabled, setEnabled] = useState(view.enabled);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const [ceiling, setCeiling] = useState(String(view.price_checks_per_minute));
  const [ceilingNote, setCeilingNote] = useState<{ tone: "danger" | "success"; key: string } | null>(null);
  const bounds = view.price_checks_bounds;

  async function saveCeiling() {
    const n = Number(ceiling);
    if (!Number.isInteger(n) || n < bounds.min || n > bounds.max) {
      setCeilingNote({ tone: "danger", key: "onlineBooking.ceilingRange" });
      return;
    }
    setBusy(true);
    const res = await bffSend("PUT", "settings/online-booking", { price_checks_per_minute: n });
    setBusy(false);
    if (!res.ok) {
      setCeilingNote({ tone: "danger", key: "error.api" });
      return;
    }
    setCeilingNote({ tone: "success", key: "onlineBooking.ceilingSaved" });
    router.refresh();
  }

  async function toggle(next: boolean) {
    setBusy(true);
    setFailed(false);
    const res = await bffSend("PUT", "settings/online-booking", { enabled: next });
    setBusy(false);
    if (!res.ok) {
      setFailed(true);
      return;
    }
    setEnabled(next);
    router.refresh();
  }

  return (
    <Panel title={tr("onlineBooking.title")} testId="online-booking">
      <div style={{ display: "grid", gap: 10 }}>
        <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("onlineBooking.hint")}</p>
        <Checkbox
          checked={enabled}
          disabled={!hydrated || busy}
          onChange={(e) => void toggle(e.target.checked)}
          label={tr("onlineBooking.toggle")}
          data-testid="online-booking-toggle"
        />
        <p role="status" style={{ margin: 0, fontSize: 13, color: failed ? K.danger : K.body }}>
          {failed ? tr("error.api") : tr(enabled ? "onlineBooking.on" : "onlineBooking.off")}
        </p>
        <div style={{ display: "flex", gap: 8, alignItems: "end", flexWrap: "wrap" }}>
          <Field label={tr("onlineBooking.ceiling")}>
            <Input
              size="sm"
              type="number"
              min={bounds.min}
              max={bounds.max}
              value={ceiling}
              onChange={(e) => setCeiling(e.target.value)}
              style={{ width: 110 }}
              data-testid="online-booking-ceiling"
            />
          </Field>
          <Button size="sm" variant="primary" disabled={!hydrated || busy} onClick={() => void saveCeiling()} data-testid="online-booking-ceiling-save">
            {tr("onlineBooking.ceilingSave")}
          </Button>
        </div>
        <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>
          {fmt(tr("onlineBooking.ceilingHint"), { min: String(bounds.min), max: String(bounds.max), default: String(bounds.default) })}
        </p>
        {ceilingNote && (
          <p role="status" style={{ margin: 0, fontSize: 13, color: ceilingNote.tone === "danger" ? K.danger : K.success }}>
            {fmt(tr(ceilingNote.key), { min: String(bounds.min), max: String(bounds.max) })}
          </p>
        )}
        {view.forms.length === 0 ? (
          <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("onlineBooking.noForm")}</p>
        ) : (
          view.forms.map((f) => (
            <p key={f.public_key} style={{ margin: 0, fontSize: 12.5, color: K.body, overflowWrap: "anywhere" }} data-testid="online-booking-link">
              <strong>{f.label}</strong>: {fmt(tr("onlineBooking.link"), { base: view.page_base ?? "/trip/", key: f.public_key })}
            </p>
          ))
        )}
      </div>
    </Panel>
  );
}
