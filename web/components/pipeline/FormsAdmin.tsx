"use client";
// web/components/pipeline/FormsAdmin.tsx — inquiry forms list, hosted link + embed code with copy buttons, and the
// owner's controls (create, on/off, allowed websites). AA-936. AA-1096 S280: each form's privacy link and default owner,
// the hourly submission quota (read-only), and refusals that say why, next to the form being edited.
// AA-943 (CRM-15): the owner's pipeline settings: the agency timezone (the travel-date moves and the call windows),
// Stage 2 auto-send on Inquiry -> Qualifying, and the weekly call windows the inquiry form offers (R15).
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, EmptyState, ErrorState, K } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { fmt, type Lang, type MessageKey, t } from "@/lib/i18n";
import {
  callSlotRules,
  embedSnippet,
  formErrorKey,
  hostedUrl,
  SLOT_DEFAULTS,
  type FormQuota,
  type PipelineSettings,
  type SlotWindow,
} from "@/lib/intakeForms";
import { memberLabel, type TeamMember } from "@/lib/team";

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

type Note = { tone: "danger" | "success"; text: string };

export function FormsAdmin({
  forms,
  clientHosts,
  isOwner,
  lang,
  quota,
  team = [],
}: {
  forms: FormRow[];
  clientHosts: string[];
  isOwner: boolean;
  lang: Lang;
  /** AA-1096 S280: the hourly submission quota each form takes (read-only; AA sets it). */
  quota?: FormQuota;
  /** The agency's members, for the default owner of a form (owner only). */
  team?: TeamMember[];
}) {
  const router = useRouter();
  const [label, setLabel] = useState("");
  // AA-1096 S280: a refusal is shown in the panel of the form being edited ("new" = the create row), with its reason
  const [notes, setNotes] = useState<Record<string, Note | undefined>>({});

  async function send(scope: string, method: "POST" | "PATCH", path: string, body: unknown) {
    setNotes((all) => ({ ...all, [scope]: undefined }));
    const res = await bffSend(method, path, body);
    if (!res.ok) {
      setNotes((all) => ({ ...all, [scope]: { tone: "danger", text: t(lang, formErrorKey(res) as MessageKey) } }));
      return false;
    }
    setNotes((all) => ({ ...all, [scope]: { tone: "success", text: t(lang, "forms.saved") } }));
    router.refresh();
    return true;
  }

  return (
    <div style={{ display: "grid", gap: 14 }}>
      {!isOwner && <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{t(lang, "forms.ownerOnly")}</p>}
      {clientHosts.length === 0 && <p style={{ margin: 0, fontSize: 13, color: K.amber }}>{t(lang, "forms.noClientHost")}</p>}
      <QuotaNote quota={quota} lang={lang} />
      {isOwner && (
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            if (label.trim() && (await send("new", "POST", "intake-forms", { label: label.trim() }))) setLabel("");
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
      {notes.new?.tone === "danger" && <NoteLine note={notes.new} />}
      {forms.length === 0 ? (
        <EmptyState title={t(lang, "forms.title")} description={t(lang, "forms.none")} />
      ) : (
        forms.map((f) => (
          <FormCard
            key={f.intake_form_id}
            form={f}
            url={hostedUrl(clientHosts, f.public_key)}
            isOwner={isOwner}
            lang={lang}
            team={team}
            note={notes[f.intake_form_id]}
            send={(body) => send(f.intake_form_id, "PATCH", `intake-forms/${f.intake_form_id}`, body)}
          />
        ))
      )}
    </div>
  );
}

function NoteLine({ note }: { note: Note }) {
  return (
    <p role={note.tone === "danger" ? "alert" : "status"} style={{ margin: 0, color: note.tone === "danger" ? K.danger : K.success, fontSize: 13 }}>
      {note.text}
    </p>
  );
}

/** The hourly quota, read-only: AA sets it (a platform setting, or this agency's own); 0 means no quota. */
function QuotaNote({ quota, lang }: { quota?: FormQuota; lang: Lang }) {
  if (quota?.submissions_per_hour === undefined) return null;
  const n = quota.submissions_per_hour;
  return (
    <p data-testid="forms-quota" style={{ margin: 0, fontSize: 12.5, color: K.muted }}>
      {n > 0 ? fmt(t(lang, "forms.quota"), { n }) : t(lang, "forms.quotaOff")}
      {quota.submissions_per_hour_own && <> {t(lang, "forms.quotaOwn")}</>}
      {n > 0 && <> {t(lang, "forms.quotaHelp")}</>}
    </p>
  );
}

function FormCard({
  form: f,
  url,
  isOwner,
  lang,
  team,
  note,
  send,
}: {
  form: FormRow;
  url: string | null;
  isOwner: boolean;
  lang: Lang;
  team: TeamMember[];
  note?: Note;
  send: (body: Record<string, unknown>) => Promise<boolean>;
}) {
  const active = f.status === "active";
  return (
    <Panel testId="forms-item">
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        <strong style={{ fontSize: 14, color: K.ink }}>{f.label}</strong>
        <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <Badge tone={active ? "success" : "neutral"}>{t(lang, active ? "forms.active" : "forms.disabled")}</Badge>
          {isOwner && (
            <Button size="sm" variant="ghost" onClick={() => void send({ status: active ? "disabled" : "active" })}>
              {t(lang, active ? "forms.disable" : "forms.enable")}
            </Button>
          )}
        </span>
      </div>
      {url && <FormLinks url={url} label={f.label} lang={lang} />}
      <FormOrigins form={f} isOwner={isOwner} lang={lang} send={send} />
      <FormDetails form={f} isOwner={isOwner} lang={lang} team={team} send={send} />
      {note && (
        <div style={{ marginTop: 8 }}>
          <NoteLine note={note} />
        </div>
      )}
    </Panel>
  );
}

function FormLinks({ url, label, lang }: { url: string; label: string; lang: Lang }) {
  const [copied, setCopied] = useState<string | null>(null);
  async function copy(id: string, text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(id);
      setTimeout(() => setCopied(null), 2000);
    } catch {
      setCopied(null);
    }
  }
  const snippet = embedSnippet(url, label);
  return (
    <div style={{ display: "grid", gap: 8, marginTop: 10 }}>
      <label style={{ display: "grid", gap: 4, fontSize: 12.5, color: K.muted }}>
        {t(lang, "forms.link")}
        <span style={{ display: "flex", gap: 6 }}>
          <input readOnly value={url} style={field} data-testid="forms-link" />
          <Button size="sm" variant="secondary" onClick={() => void copy("url", url)}>
            {t(lang, copied === "url" ? "forms.copied" : "forms.copy")}
          </Button>
        </span>
      </label>
      <label style={{ display: "grid", gap: 4, fontSize: 12.5, color: K.muted }}>
        {t(lang, "forms.embed")}
        <span style={{ display: "flex", gap: 6, alignItems: "flex-start" }}>
          <textarea readOnly rows={3} value={snippet} style={{ ...field, fontFamily: "'JetBrains Mono', monospace", fontSize: 12 }} />
          <Button size="sm" variant="secondary" onClick={() => void copy("embed", snippet)}>
            {t(lang, copied === "embed" ? "forms.copied" : "forms.copy")}
          </Button>
        </span>
      </label>
    </div>
  );
}

type FormPartProps = { form: FormRow; isOwner: boolean; lang: Lang; send: (body: Record<string, unknown>) => Promise<boolean> };

function FormOrigins({ form: f, isOwner, lang, send }: FormPartProps) {
  const [origins, setOrigins] = useState(f.allowed_origins.join("\n"));
  const helpId = `forms-origins-help-${f.intake_form_id}`;
  return (
    <>
      <label style={{ display: "grid", gap: 4, fontSize: 12.5, color: K.muted, marginTop: 10 }}>
        {t(lang, "forms.origins")}
        <textarea rows={2} readOnly={!isOwner} value={origins} onChange={(e) => setOrigins(e.target.value)} style={field} aria-describedby={helpId} />
        <span id={helpId}>{t(lang, "forms.originsHelp")}</span>
      </label>
      {isOwner && (
        <div style={{ marginTop: 8 }}>
          <Button size="sm" variant="secondary" onClick={() => void send({ allowed_origins: origins.split(/\s+/).filter(Boolean) })}>
            {t(lang, "forms.saveOrigins")}
          </Button>
        </div>
      )}
    </>
  );
}

/** The PATCH body of "Save privacy link and owner". ``owner`` is undefined when no owner list is shown. The owner goes
 * out only when it changed: a form whose owner has since left keeps them (the list still shows them), and resending
 * that inactive member would refuse a privacy-link-only save with invalid_owner. */
export function formDetailsBody(
  form: Pick<FormRow, "default_owner_member_id">,
  privacy: string,
  owner: string | undefined,
): { privacy_notice_url: string | null; default_owner_member_id?: string | null } {
  const changed = owner !== undefined && owner !== (form.default_owner_member_id ?? "");
  return { privacy_notice_url: privacy.trim() || null, ...(changed ? { default_owner_member_id: owner || null } : {}) };
}

/** The form's privacy link (empty: the agency's own, from Connections) and, for the owner, who gets its new inquiries. */
function FormDetails({ form: f, isOwner, lang, team, send }: FormPartProps & { team: TeamMember[] }) {
  const [privacy, setPrivacy] = useState(f.privacy_notice_url ?? "");
  const [owner, setOwner] = useState(f.default_owner_member_id ?? "");
  const owners = isOwner ? team.filter((m) => m.status === "active" || m.member_id === f.default_owner_member_id) : [];
  const helpId = `forms-privacy-help-${f.intake_form_id}`;
  const body = () => formDetailsBody(f, privacy, owners.length > 0 ? owner : undefined);
  return (
    <div style={{ display: "grid", gap: 8, marginTop: 12 }}>
      <label style={{ display: "grid", gap: 4, fontSize: 12.5, color: K.muted }}>
        {t(lang, "forms.privacy")}
        <input
          value={privacy}
          readOnly={!isOwner}
          onChange={(e) => setPrivacy(e.target.value)}
          maxLength={500}
          inputMode="url"
          placeholder="https://"
          style={field}
          aria-describedby={helpId}
          data-testid="forms-privacy"
        />
        <span id={helpId}>{t(lang, "forms.privacyHelp")}</span>
      </label>
      {owners.length > 0 && (
        <label style={{ display: "grid", gap: 4, fontSize: 12.5, color: K.muted }}>
          {t(lang, "forms.owner")}
          <select value={owner} onChange={(e) => setOwner(e.target.value)} style={field} data-testid="forms-owner">
            <option value="">{t(lang, "forms.ownerNone")}</option>
            {owners.map((m) => (
              <option key={m.member_id} value={m.member_id}>
                {memberLabel(m)}
              </option>
            ))}
          </select>
        </label>
      )}
      {isOwner && (
        <div>
          <Button size="sm" variant="secondary" onClick={() => void send(body())}>
            {t(lang, "forms.saveDetails")}
          </Button>
        </div>
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

  if (!isOwner) {
    return (
      <Panel title={t(lang, "pset.title")} testId="pset">
        <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{t(lang, "pset.ownerOnly")}</p>
      </Panel>
    );
  }
  if (!settings) {
    // AA-1096 S280: the owner's read failed (5xx, timeout): say so, never "only the owner"
    return (
      <Panel title={t(lang, "pset.title")} testId="pset">
        <ErrorState message={t(lang, "error.api")} />
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
