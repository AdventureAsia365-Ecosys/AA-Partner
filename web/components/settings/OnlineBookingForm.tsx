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

type Bounds = OnlineBookingView["price_checks_bounds"];

export function inRange(n: number, bounds: Pick<Bounds, "min" | "max">): boolean {
  return n >= bounds.min && n <= bounds.max;
}

/** The bounds a refusal carries (``invalid_price_checks_per_minute``: ``min`` / ``max``), or null. */
export function serverBounds(body: unknown): { min: number; max: number } | null {
  const b = body as { min?: unknown; max?: unknown } | null;
  return b && typeof b.min === "number" && typeof b.max === "number" ? { min: b.min, max: b.max } : null;
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
  const [bounds, setBounds] = useState(view.price_checks_bounds);
  const stored = view.price_checks_per_minute;
  // AA-1096 S280: AA may narrow the range after the owner saved; the saved value still applies and stays savable
  const storedOutside = !inRange(stored, bounds);

  async function saveCeiling() {
    const n = Number(ceiling);
    if (!Number.isInteger(n) || (!inRange(n, bounds) && n !== stored)) {
      setCeilingNote({ tone: "danger", key: "onlineBooking.ceilingRange" });
      return;
    }
    setBusy(true);
    const res = await bffSend("PUT", "settings/online-booking", { price_checks_per_minute: n });
    setBusy(false);
    if (!res.ok) {
      // the range may have changed since the page loaded: the API names the bounds it checked
      const range = serverBounds(res.body);
      if (res.detail === "invalid_price_checks_per_minute") {
        if (range) setBounds({ ...bounds, ...range });
        setCeilingNote({ tone: "danger", key: "onlineBooking.ceilingRange" });
        router.refresh();
      } else {
        setCeilingNote({ tone: "danger", key: "error.api" });
      }
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
        <Field
          label={tr("onlineBooking.ceiling")}
          hint={fmt(tr("onlineBooking.ceilingHint"), { min: String(bounds.min), max: String(bounds.max), default: String(bounds.default) })}
        >
          <span style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <Input
              size="sm"
              type="number"
              min={Math.min(bounds.min, stored)}
              max={Math.max(bounds.max, stored)}
              value={ceiling}
              onChange={(e) => setCeiling(e.target.value)}
              style={{ width: 110 }}
              data-testid="online-booking-ceiling"
            />
            <Button size="sm" variant="primary" disabled={!hydrated || busy} onClick={() => void saveCeiling()} testId="online-booking-ceiling-save">
              {tr("onlineBooking.ceilingSave")}
            </Button>
          </span>
        </Field>
        {storedOutside && (
          <p style={{ margin: 0, fontSize: 12.5, color: K.amber }} data-testid="online-booking-outside-range">
            {fmt(tr("onlineBooking.outsideRange"), { value: String(stored), min: String(bounds.min), max: String(bounds.max) })}
          </p>
        )}
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
