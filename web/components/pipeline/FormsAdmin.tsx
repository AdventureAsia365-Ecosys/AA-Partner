"use client";
// web/components/pipeline/FormsAdmin.tsx — inquiry forms list, hosted link + embed code with copy buttons, and the
// owner's controls (create, on/off, allowed websites). AA-936.
// AA-943 (CRM-15): the owner's pipeline settings: the agency timezone (the travel-date moves and the call windows),
// Stage 2 auto-send on Inquiry -> Qualifying, and the weekly call windows the inquiry form offers (R15).
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, EmptyState, K } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { type Lang, type MessageKey, t } from "@/lib/i18n";
import { callSlotRules, embedSnippet, hostedUrl, SLOT_DEFAULTS, type PipelineSettings, type SlotWindow } from "@/lib/intakeForms";

export interface FormRow {
  intake_form_id: string;
  public_key: string;
  label: string;
  allowed_origins: string[];
  default_owner_member_id: string | null;
  lead_source: string;
  privacy_notice_url: string | null;
  status: "active" | "disabled";
  created_at: string;
}

const field: React.CSSProperties = {
  font: "inherit",
  fontSize: 13,
  padding: "7px 9px",
  borderRadius: 8,
  border: `1px solid ${K.line2}`,
  background: K.card,
  color: K.ink,
  width: "100%",
  boxSizing: "border-box",
};

export function FormsAdmin({ forms, clientHosts, isOwner, lang }: { forms: FormRow[]; clientHosts: string[]; isOwner: boolean; lang: Lang }) {
  const router = useRouter();
  const [label, setLabel] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const [origins, setOrigins] = useState<Record<string, string>>(() =>
    Object.fromEntries(forms.map((f) => [f.intake_form_id, f.allowed_origins.join("\n")])),
  );

  async function send(method: "POST" | "PATCH", path: string, body: unknown) {
    setError(null);
    const res = await bffSend(method, path, body);
    if (!res.ok) setError(t(lang, "trip.error"));
    else router.refresh();
    return res.ok;
  }

  async function copy(id: string, text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(id);
      setTimeout(() => setCopied(null), 2000);
    } catch {
      setCopied(null);
    }
  }

  return (
    <div style={{ display: "grid", gap: 14 }}>
      {!isOwner && <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{t(lang, "forms.ownerOnly")}</p>}
      {clientHosts.length === 0 && <p style={{ margin: 0, fontSize: 13, color: K.amber }}>{t(lang, "forms.noClientHost")}</p>}
      {isOwner && (
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            if (label.trim() && (await send("POST", "intake-forms", { label: label.trim() }))) setLabel("");
          }}
          style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "flex-end" }}
        >
          <label style={{ display: "grid", gap: 4, fontSize: 13, flex: "1 1 240px" }}>
            {t(lang, "forms.label")}
            <input value={label} onChange={(e) => setLabel(e.target.value)} maxLength={80} style={field} data-testid="forms-new-label" />
          </label>
          <Button type="submit" variant="primary" disabled={!label.trim()}>
            {t(lang, "forms.create")}
          </Button>
        </form>
      )}
      {error && (
        <p role="alert" style={{ margin: 0, color: K.danger, fontSize: 13 }}>
          {error}
        </p>
      )}
      {forms.length === 0 ? (
        <EmptyState title={t(lang, "forms.title")} description={t(lang, "forms.none")} />
      ) : (
        forms.map((f) => {
          const url = hostedUrl(clientHosts, f.public_key);
          return (
            <Panel key={f.intake_form_id} testId="forms-item">
              <div style={{ display: "flex", justifyContent: "space-between", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
                <strong style={{ fontSize: 14, color: K.ink }}>{f.label}</strong>
                <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
                  <Badge tone={f.status === "active" ? "success" : "neutral"}>{t(lang, f.status === "active" ? "forms.active" : "forms.disabled")}</Badge>
                  {isOwner && (
                    <Button size="sm" variant="ghost" onClick={() => void send("PATCH", `intake-forms/${f.intake_form_id}`, { status: f.status === "active" ? "disabled" : "active" })}>
                      {t(lang, f.status === "active" ? "forms.disable" : "forms.enable")}
                    </Button>
                  )}
                </span>
              </div>
              {url && (
                <div style={{ display: "grid", gap: 8, marginTop: 10 }}>
                  <label style={{ display: "grid", gap: 4, fontSize: 12.5, color: K.muted }}>
                    {t(lang, "forms.link")}
                    <span style={{ display: "flex", gap: 6 }}>
                      <input readOnly value={url} style={field} data-testid="forms-link" />
                      <Button size="sm" variant="secondary" onClick={() => void copy(`${f.intake_form_id}:url`, url)}>
                        {copied === `${f.intake_form_id}:url` ? t(lang, "forms.copied") : t(lang, "forms.copy")}
                      </Button>
                    </span>
                  </label>
                  <label style={{ display: "grid", gap: 4, fontSize: 12.5, color: K.muted }}>
                    {t(lang, "forms.embed")}
                    <span style={{ display: "flex", gap: 6, alignItems: "flex-start" }}>
                      <textarea readOnly rows={3} value={embedSnippet(url, f.label)} style={{ ...field, fontFamily: "'JetBrains Mono', monospace", fontSize: 12 }} />
                      <Button size="sm" variant="secondary" onClick={() => void copy(`${f.intake_form_id}:embed`, embedSnippet(url, f.label))}>
                        {copied === `${f.intake_form_id}:embed` ? t(lang, "forms.copied") : t(lang, "forms.copy")}
                      </Button>
                    </span>
                  </label>
                </div>
              )}
              <label style={{ display: "grid", gap: 4, fontSize: 12.5, color: K.muted, marginTop: 10 }}>
                {t(lang, "forms.origins")}
                <textarea
                  rows={2}
                  readOnly={!isOwner}
                  value={origins[f.intake_form_id] ?? ""}
                  onChange={(e) => setOrigins({ ...origins, [f.intake_form_id]: e.target.value })}
                  style={field}
                />
                <span>{t(lang, "forms.originsHelp")}</span>
              </label>
              {isOwner && (
                <div style={{ marginTop: 8 }}>
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() =>
                      void send("PATCH", `intake-forms/${f.intake_form_id}`, {
                        allowed_origins: (origins[f.intake_form_id] ?? "").split(/\s+/).filter(Boolean),
                      })
                    }
                  >
                    {t(lang, "forms.saveOrigins")}
                  </Button>
                </div>
              )}
            </Panel>
          );
        })
      )}
    </div>
  );
}

const DAYS = [1, 2, 3, 4, 5, 6, 7] as const;

export function PipelineSettingsPanel({ settings, isOwner, lang }: { settings: PipelineSettings | null; isOwner: boolean; lang: Lang }) {
  const router = useRouter();
  const rules = settings?.call_slot_rules ?? {};
  const [timezone, setTimezone] = useState(settings?.timezone ?? "Asia/Ho_Chi_Minh");
  const [autoSend, setAutoSend] = useState(settings?.stage2_auto_send ?? false);
  const [windows, setWindows] = useState<SlotWindow[]>(rules.windows ?? []);
  const [duration, setDuration] = useState(rules.duration_min ?? SLOT_DEFAULTS.duration_min);
  const [horizon, setHorizon] = useState(rules.horizon_days ?? SLOT_DEFAULTS.horizon_days);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!isOwner || !settings) {
    return (
      <Panel title={t(lang, "pset.title")} testId="pset">
        <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{t(lang, "pset.ownerOnly")}</p>
      </Panel>
    );
  }

  function setWindow(i: number, change: Partial<SlotWindow>) {
    setWindows((all) => all.map((w, j) => (j === i ? { ...w, ...change } : w)));
    setSaved(false);
  }

  async function save() {
    setBusy(true);
    setError(null);
    setSaved(false);
    const res = await bffSend<PipelineSettings>("PUT", "settings/pipeline", {
      timezone: timezone.trim(),
      stage2_auto_send: autoSend,
      call_slot_rules: callSlotRules(windows, duration, horizon),
    });
    setBusy(false);
    if (res.ok) {
      setSaved(true);
      router.refresh();
    } else {
      const known = res.detail === "invalid_timezone" || res.detail === "invalid_call_slot_rules";
      setError(known ? t(lang, `pset.error.${res.detail}` as MessageKey) : t(lang, "trip.error"));
    }
  }

  const small: React.CSSProperties = { ...field, width: "auto" };
  return (
    <Panel title={t(lang, "pset.title")} testId="pset">
      <div style={{ display: "grid", gap: 12, fontSize: 13 }}>
        <label style={{ display: "grid", gap: 4, color: K.muted, fontSize: 12.5 }}>
          {t(lang, "pset.timezone")}
          <input value={timezone} onChange={(e) => (setTimezone(e.target.value), setSaved(false))} maxLength={64} style={field} data-testid="pset-timezone" />
          <span>{t(lang, "pset.timezoneHelp")}</span>
        </label>
        <label style={{ display: "flex", gap: 8, alignItems: "flex-start", color: K.ink2 }}>
          <input type="checkbox" checked={autoSend} onChange={(e) => (setAutoSend(e.target.checked), setSaved(false))} style={{ marginTop: 3 }} data-testid="pset-autosend" />
          <span>{t(lang, "pset.autoSend")}</span>
        </label>
        <fieldset style={{ border: 0, padding: 0, margin: 0, display: "grid", gap: 8 }}>
          <legend style={{ fontSize: 12.5, color: K.muted, marginBottom: 4 }}>{t(lang, "pset.slots")}</legend>
          <span style={{ fontSize: 12, color: K.muted }}>{t(lang, "pset.slotsHelp")}</span>
          {windows.map((w, i) => (
            <div key={i} data-testid="pset-window" style={{ display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center" }}>
              <select aria-label={t(lang, "pset.day")} value={w.dow} onChange={(e) => setWindow(i, { dow: Number(e.target.value) })} style={small} data-testid="pset-day">
                {DAYS.map((d) => (
                  <option key={d} value={d}>
                    {t(lang, `dow.${d}` as MessageKey)}
                  </option>
                ))}
              </select>
              <input type="time" aria-label={t(lang, "pset.from")} value={w.start} step={300} onChange={(e) => setWindow(i, { start: e.target.value })} style={small} data-testid="pset-from" />
              <span style={{ color: K.muted }}>–</span>
              <input type="time" aria-label={t(lang, "pset.to")} value={w.end} step={300} onChange={(e) => setWindow(i, { end: e.target.value })} style={small} data-testid="pset-to" />
              <Button size="sm" variant="ghost" onClick={() => (setWindows(windows.filter((_, j) => j !== i)), setSaved(false))}>
                {t(lang, "pset.remove")}
              </Button>
            </div>
          ))}
          {windows.length < SLOT_DEFAULTS.max_windows && (
            <span data-testid="pset-add-window">
              <Button size="sm" variant="secondary" onClick={() => (setWindows([...windows, { dow: 1, start: "09:00", end: "12:00" }]), setSaved(false))}>
                {t(lang, "pset.addWindow")}
              </Button>
            </span>
          )}
          {windows.length > 0 && (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 12 }}>
              <label style={{ display: "grid", gap: 4, color: K.muted, fontSize: 12.5 }}>
                {t(lang, "pset.duration")}
                <input type="number" min={10} max={240} step={5} value={duration} onChange={(e) => (setDuration(Number(e.target.value)), setSaved(false))} style={small} data-testid="pset-duration" />
              </label>
              <label style={{ display: "grid", gap: 4, color: K.muted, fontSize: 12.5 }}>
                {t(lang, "pset.horizon")}
                <input type="number" min={1} max={60} value={horizon} onChange={(e) => (setHorizon(Number(e.target.value)), setSaved(false))} style={small} data-testid="pset-horizon" />
              </label>
            </div>
          )}
        </fieldset>
        {error && (
          <p role="alert" style={{ margin: 0, color: K.danger, fontSize: 13 }}>
            {error}
          </p>
        )}
        <span style={{ display: "flex", gap: 10, alignItems: "center" }}>
          <span data-testid="pset-save" style={{ display: "contents" }}>
            <Button size="sm" variant="primary" disabled={busy} onClick={() => void save()}>
              {t(lang, "pset.save")}
            </Button>
          </span>
          {saved && (
            <span role="status" data-testid="pset-saved" style={{ color: K.success, fontSize: 12.5 }}>
              {t(lang, "pset.saved")}
            </span>
          )}
        </span>
      </div>
    </Panel>
  );
}
