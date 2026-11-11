"use client";
// web/components/travelOps/TravelSettingsView.tsx — AA-1070, Settings > Travel: how many months a passport must outlast
// the trip (default 6; the trip party badge, the pipeline filter and the Booked workflow event read it) and the agency's
// own visa guidance per country and visa type (processing days, fee, the documents a traveller collects, the official
// page). The agency keeps the guidance: Adventure Asia gives no visa advice. Every role reads; the owner edits.
import { useState } from "react";
import { Badge, Button, Field, Input, K, Select, Textarea, useHydrated } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import { VISA_TYPES, travelErrorKey, type VisaTemplate, type VisaType } from "@/lib/travelOps";

const EMPTY = { country: "", visa_type: "evisa" as VisaType, processing_days: "", fee: "", fee_currency: "", documents: "", official_url: "" };

export function TravelSettingsView({
  months: initialMonths,
  templates: initial,
  isOwner,
  lang,
}: {
  months: number;
  templates: VisaTemplate[];
  isOwner: boolean;
  lang: Lang;
}) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const hydrated = useHydrated();
  const [months, setMonths] = useState(String(initialMonths));
  const [templates, setTemplates] = useState(initial);
  const [form, setForm] = useState(EMPTY);
  const [notice, setNotice] = useState<{ tone: "danger" | "success"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  async function send<T>(method: "POST" | "PUT" | "PATCH" | "DELETE", path: string, body?: unknown): Promise<T | null> {
    setBusy(true);
    const res = await bffSend<T>(method, path, body);
    setBusy(false);
    if (!res.ok) {
      if (!res.redirecting) setNotice({ tone: "danger", text: tr(travelErrorKey(res.detail)) });
      return null;
    }
    setNotice({ tone: "success", text: tr("travel.settings.saved") });
    return res.data;
  }

  async function saveMonths() {
    const n = Number(months);
    if (!Number.isInteger(n) || n < 0 || n > 24) return setNotice({ tone: "danger", text: tr("travel.settings.monthsRange") });
    await send("PUT", "settings/travel", { passport_valid_months: n });
  }

  async function addTemplate() {
    const body = {
      country: form.country.trim().toUpperCase(),
      visa_type: form.visa_type,
      processing_days: form.processing_days ? Number(form.processing_days) : null,
      fee: form.fee.trim() || null,
      fee_currency: form.fee.trim() ? form.fee_currency.trim().toUpperCase() || null : null,
      documents: form.documents.split("\n").map((d) => d.trim()).filter(Boolean),
      official_url: form.official_url.trim() || null,
    };
    const made = await send<VisaTemplate>("POST", "visa-templates", body);
    if (made) {
      setTemplates([...templates, made].sort((a, b) => `${a.country}${a.visa_type}`.localeCompare(`${b.country}${b.visa_type}`)));
      setForm(EMPTY);
    }
  }

  const disabled = busy || !hydrated || !isOwner;
  return (
    <div style={{ display: "grid", gap: 16 }}>
      {notice && (
        <p role="status" style={{ margin: 0, fontSize: 13, color: notice.tone === "danger" ? K.danger : K.success }}>
          {notice.text}
        </p>
      )}
      <Panel title={tr("travel.settings.passport")} testId="travel-settings-passport">
        <p style={{ margin: "0 0 8px", fontSize: 13, color: K.muted }}>{tr("travel.settings.passportIntro")}</p>
        <div style={{ display: "flex", gap: 8, alignItems: "end", flexWrap: "wrap" }}>
          <Field label={tr("travel.settings.months")}>
            <Input size="sm" type="number" min={0} max={24} value={months} disabled={!isOwner} onChange={(e) => setMonths(e.target.value)} style={{ width: 100 }} />
          </Field>
          {isOwner && (
            <Button size="sm" variant="primary" disabled={disabled} onClick={() => void saveMonths()}>
              {tr("travel.settings.save")}
            </Button>
          )}
        </div>
      </Panel>
      <Panel title={tr("travel.settings.visas")} testId="travel-settings-visas">
        <p style={{ margin: "0 0 8px", fontSize: 13, color: K.muted }}>{tr("travel.settings.visasIntro")}</p>
        {templates.length === 0 && <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{tr("travel.settings.noTemplates")}</p>}
        <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 10 }}>
          {templates.map((x) => (
            <li key={x.visa_template_id} data-testid="visa-template" style={{ display: "grid", gap: 4 }}>
              <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                <strong style={{ fontSize: 13 }}>
                  {x.country} · {tr(`travel.visaType.${x.visa_type}`)}
                </strong>
                {!x.active && <Badge>{tr("travel.settings.inactive")}</Badge>}
                {x.processing_days != null && <span style={{ fontSize: 12.5, color: K.muted }}>{fmt(tr("travel.settings.days"), { n: String(x.processing_days) })}</span>}
                {x.fee && <span style={{ fontSize: 12.5, color: K.muted }}>{`${x.fee} ${x.fee_currency ?? ""}`}</span>}
                {x.official_url && (
                  <a href={x.official_url} target="_blank" rel="noopener noreferrer" style={{ fontSize: 12.5 }}>
                    {tr("travel.settings.official")}
                  </a>
                )}
                {isOwner && (
                  <>
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={disabled}
                      onClick={async () => {
                        const out = await send<VisaTemplate>("PATCH", `visa-templates/${x.visa_template_id}`, { active: !x.active });
                        if (out) setTemplates(templates.map((y) => (y.visa_template_id === out.visa_template_id ? out : y)));
                      }}
                    >
                      {tr(x.active ? "travel.settings.deactivate" : "travel.settings.activate")}
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={disabled}
                      onClick={async () => {
                        if (await send("DELETE", `visa-templates/${x.visa_template_id}`)) setTemplates(templates.filter((y) => y.visa_template_id !== x.visa_template_id));
                      }}
                    >
                      {tr("travel.settings.delete")}
                    </Button>
                  </>
                )}
              </div>
              {x.documents.length > 0 && <span style={{ fontSize: 12.5, color: K.muted, overflowWrap: "anywhere" }}>{x.documents.join(" · ")}</span>}
            </li>
          ))}
        </ul>
        {isOwner && (
          <div data-testid="visa-template-form" style={{ display: "grid", gap: 8, marginTop: 12, gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))" }}>
            <Field label={tr("travel.settings.country")} hint={tr("travel.settings.countryHint")}>
              <Input size="sm" maxLength={2} value={form.country} onChange={(e) => setForm({ ...form, country: e.target.value })} />
            </Field>
            <Field label={tr("travel.settings.visaType")}>
              <Select
                size="sm"
                value={form.visa_type}
                onChange={(e) => setForm({ ...form, visa_type: e.target.value as VisaType })}
                options={VISA_TYPES.map((v) => ({ value: v, label: tr(`travel.visaType.${v}`) }))}
              />
            </Field>
            <Field label={tr("travel.settings.processing")}>
              <Input size="sm" type="number" min={0} max={365} value={form.processing_days} onChange={(e) => setForm({ ...form, processing_days: e.target.value })} />
            </Field>
            <Field label={tr("travel.settings.fee")}>
              <Input size="sm" inputMode="decimal" value={form.fee} onChange={(e) => setForm({ ...form, fee: e.target.value })} />
            </Field>
            <Field label={tr("travel.settings.currency")}>
              <Input size="sm" maxLength={3} value={form.fee_currency} onChange={(e) => setForm({ ...form, fee_currency: e.target.value })} />
            </Field>
            <Field label={tr("travel.settings.url")}>
              <Input size="sm" type="url" maxLength={500} value={form.official_url} onChange={(e) => setForm({ ...form, official_url: e.target.value })} />
            </Field>
            <Field label={tr("travel.settings.documents")} hint={tr("travel.settings.documentsHint")} style={{ gridColumn: "1 / -1" }}>
              <Textarea rows={3} value={form.documents} onChange={(e) => setForm({ ...form, documents: e.target.value })} />
            </Field>
            <div>
              <Button size="sm" variant="primary" disabled={disabled || form.country.trim().length !== 2} onClick={() => void addTemplate()}>
                {tr("travel.settings.addTemplate")}
              </Button>
            </div>
          </div>
        )}
      </Panel>
    </div>
  );
}
