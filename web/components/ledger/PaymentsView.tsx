"use client";
// web/components/ledger/PaymentsView.tsx — Settings > Payments, the CRM-16 part (AA-944; design v3 R17, R18): base
// currency, booking validation (Off / Basic / Strict, Basic by default), exchange rates automatic or manual, and the
// owner's own rates for a day. Owner only (the page shows other roles why). A changed base currency never re-converts
// confirmed lines: the stored base amounts stay as frozen.
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button, K, useHydrated } from "@/app/_kit";
import { DateInput } from "@/components/DateInput";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { formatDate, type DateLocale } from "@/lib/dates";
import { t, type Lang, type MessageKey } from "@/lib/i18n";
import { CURRENCY, errorKey, type FxOverride, type PaymentSettings } from "@/lib/ledger";

const field: React.CSSProperties = {
  font: "inherit",
  fontSize: 13,
  padding: "6px 8px",
  borderRadius: 8,
  border: `1px solid ${K.line2}`,
  background: K.card,
  color: K.ink,
  boxSizing: "border-box",
};

export function PaymentsView({
  settings,
  overrides,
  lang,
  locale,
}: {
  settings: PaymentSettings;
  overrides: FxOverride[];
  lang: Lang;
  locale: DateLocale;
}) {
  const router = useRouter();
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  async function send(method: "PUT" | "POST", path: string, body: unknown) {
    setBusy(true);
    setMessage(null);
    const res = await bffSend<unknown>(method, path, body);
    setBusy(false);
    if (!res.ok) {
      setMessage({ ok: false, text: res.status === 422 ? tr("payments.invalid") : tr(errorKey(res.detail)) });
      return;
    }
    setMessage({ ok: true, text: tr("payments.saved") });
    router.refresh();
  }

  return (
    <div style={{ display: "grid", gap: 14 }} data-testid="payments-view">
      {message && (
        <p role="status" style={{ margin: 0, fontSize: 13, color: message.ok ? K.ink : K.danger }}>
          {message.text}
        </p>
      )}
      <Panel title={tr("payments.ledger")} testId="payments-settings">
        <form
          action={(data) => {
            const base = String(data.get("base_currency") ?? "").toUpperCase();
            if (!CURRENCY.test(base)) {
              setMessage({ ok: false, text: tr("payments.invalid") });
              return;
            }
            void send("PUT", "settings/payments", {
              base_currency: base,
              booking_validation_level: String(data.get("booking_validation_level")),
              fx_mode: String(data.get("fx_mode")),
            });
          }}
          style={{ display: "grid", gap: 10, maxWidth: 520 }}
        >
          <label style={{ display: "grid", gap: 2, fontSize: 12.5 }}>
            {tr("payments.baseCurrency")}
            <input name="base_currency" style={field} maxLength={3} defaultValue={settings.base_currency} />
            <span style={{ color: K.muted, fontSize: 12 }}>{tr("payments.baseHelp")}</span>
          </label>
          <fieldset style={{ border: "none", padding: 0, margin: 0, display: "grid", gap: 4 }}>
            <legend style={{ fontSize: 12.5, marginBottom: 4 }}>{tr("payments.level")}</legend>
            {settings.levels.map((level) => (
              <label key={level} style={{ display: "flex", gap: 6, fontSize: 13, alignItems: "flex-start" }}>
                <input type="radio" name="booking_validation_level" value={level} defaultChecked={settings.booking_validation_level === level} />
                <span>
                  <strong>{tr(`ledger.level.${level}`)}</strong> — {tr(`payments.level.${level}`)}
                </span>
              </label>
            ))}
          </fieldset>
          <fieldset style={{ border: "none", padding: 0, margin: 0, display: "grid", gap: 4 }}>
            <legend style={{ fontSize: 12.5, marginBottom: 4 }}>{tr("payments.fxMode")}</legend>
            {(["automatic", "manual"] as const).map((mode) => (
              <label key={mode} style={{ display: "flex", gap: 6, fontSize: 13 }}>
                <input type="radio" name="fx_mode" value={mode} defaultChecked={settings.fx_mode === mode} />
                {tr(`payments.fxMode.${mode}`)}
              </label>
            ))}
          </fieldset>
          <div>
            <Button type="submit" size="sm" variant="primary" disabled={!hydrated || busy}>
              {tr("ledger.save")}
            </Button>
          </div>
          <a href={settings.attribution.url} target="_blank" rel="noreferrer" style={{ fontSize: 11.5 }}>
            {settings.attribution.text}
          </a>
        </form>
      </Panel>
      <Panel title={tr("payments.overrides")} testId="payments-overrides">
        <p style={{ margin: "0 0 8px", fontSize: 12.5, color: K.muted }}>{tr("payments.overridesHelp")}</p>
        {overrides.length > 0 && (
          <ul style={{ margin: "0 0 10px", paddingLeft: 18, fontSize: 13 }}>
            {overrides.map((o) => (
              <li key={o.fx_rate_override_id}>
                {formatDate(o.rate_date, locale)}: 1 {o.base} = {o.rate} {o.quote}
              </li>
            ))}
          </ul>
        )}
        <form
          action={(data) =>
            void send("POST", "fx/overrides", {
              base: String(data.get("base") ?? "").toUpperCase(),
              quote: String(data.get("quote") ?? "").toUpperCase(),
              rate: String(data.get("rate") ?? "").replace(/[\s,]/g, ""),
              rate_date: String(data.get("rate_date") ?? "") || null,
            })
          }
          style={{ display: "grid", gap: 8, maxWidth: 420 }}
        >
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "end" }}>
            <label style={{ display: "grid", gap: 2, fontSize: 12 }}>
              {tr("payments.base")}
              <input name="base" style={{ ...field, width: 70 }} maxLength={3} defaultValue="USD" />
            </label>
            <label style={{ display: "grid", gap: 2, fontSize: 12 }}>
              {tr("payments.quote")}
              <input name="quote" style={{ ...field, width: 70 }} maxLength={3} defaultValue="VND" />
            </label>
            <label style={{ display: "grid", gap: 2, fontSize: 12 }}>
              {tr("payments.rate")}
              <input name="rate" style={{ ...field, width: 120 }} inputMode="decimal" required />
            </label>
          </div>
          <DateInput locale={locale} name="rate_date" label={tr("payments.date")} readAs={tr("settings.readAs")} invalid={tr("settings.invalidDate")} />
          <div>
            <Button type="submit" size="sm" disabled={!hydrated || busy}>
              {tr("payments.addOverride")}
            </Button>
          </div>
        </form>
      </Panel>
    </div>
  );
}
