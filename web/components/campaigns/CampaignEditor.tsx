"use client";
// web/components/campaigns/CampaignEditor.tsx — the campaign editor of Marketing > Campaigns (card CRM-36, AA-968):
// name, the sending mailbox (the agency's or the member's own, never Adventure Asia's), the Adventure Asia tours (search
// in the CRM's cache), an Adventure Asia campaign text to start from, "news about our agency", client tags, subject and
// text; the content rules it still breaks (root ADR 0019 d21, worded by code); "Check who gets it" (left-out reasons,
// the days at the mailbox's daily limit) and Schedule with the previewed count. Every call goes through the BFF.
import { useState } from "react";
import { Button, Checkbox, ChipButton, Field, Input, K, Select, Switch, Textarea, useHydrated } from "@/app/_kit";
import { bffSend } from "@/lib/bffClient";
import {
  campaignErrorKey,
  excludedText,
  gateText,
  payloadOf,
  previewSummary,
  scheduleAt,
  type CampaignOptions,
  type Draft,
  type Preview,
  type TourHit,
} from "@/lib/campaigns";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";

export type Editing = { id: string | null; draft: Draft; version: number; gate: string[]; state: string; tours: Record<string, string> };
type Saved = { campaign_id: string; gate_codes: string[]; version: number; state: string };

function useTr(lang: Lang) {
  return (key: string) => t(lang, key as MessageKey);
}

function TourPicker({ lang, editing, onChange }: { lang: Lang; editing: Editing; onChange: (e: Editing) => void }) {
  const tr = useTr(lang);
  const hydrated = useHydrated();
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<TourHit[] | null>(null);
  const d = editing.draft;
  const label = (id: string) => editing.tours[id] ?? id;

  async function search() {
    const res = await bffSend<{ items: TourHit[] }>("GET", `campaigns/tours?${new URLSearchParams({ q: query.trim() })}`);
    if (res.ok) setHits(res.data.items);
  }

  function add(hit: TourHit) {
    if (d.aa_tour_ids.includes(hit.source_id)) return;
    onChange({ ...editing, tours: { ...editing.tours, [hit.source_id]: hit.title }, draft: { ...d, aa_tour_ids: [...d.aa_tour_ids, hit.source_id] } });
  }

  return (
    <Field as="div" label={tr("camp.field.tours")}>
      <div style={{ display: "grid", gap: 6 }}>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }} data-testid="campaign-tours">
          {d.aa_tour_ids.map((id) => (
            <ChipButton key={id} size="sm" selected aria-label={fmt(tr("camp.field.tourRemove"), { tour: label(id) })}
              onClick={() => onChange({ ...editing, draft: { ...d, aa_tour_ids: d.aa_tour_ids.filter((x) => x !== id) } })}>
              {label(id)} ×
            </ChipButton>
          ))}
        </div>
        <div style={{ display: "flex", gap: 6 }}>
          <span style={{ flex: "1 1 auto", minWidth: 0 }}>
            <Input size="sm" aria-label={tr("camp.field.tourSearch")} placeholder={tr("camp.field.tourSearch")} value={query}
              onChange={(e) => setQuery(e.target.value)} />
          </span>
          <Button size="sm" disabled={!hydrated} onClick={() => void search()}>
            {tr("camp.field.tourSearch")}
          </Button>
        </div>
        {hits && <TourHits lang={lang} hits={hits} chosen={d.aa_tour_ids} onAdd={add} />}
      </div>
    </Field>
  );
}

function TourHits({ lang, hits, chosen, onAdd }: { lang: Lang; hits: TourHit[]; chosen: string[]; onAdd: (h: TourHit) => void }) {
  const tr = useTr(lang);
  if (hits.length === 0) return <span style={{ fontSize: 12.5, color: K.muted }}>{tr("camp.field.tourNone")}</span>;
  return (
    <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 4 }}>
      {hits.map((h) => (
        <li key={h.source_id} style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 13 }}>
          <span style={{ flex: "1 1 auto", overflowWrap: "anywhere" }}>{h.title}</span>
          <Button size="sm" variant="ghost" onClick={() => onAdd(h)} disabled={chosen.includes(h.source_id)}>
            {tr("camp.field.tourAdd")}
          </Button>
        </li>
      ))}
    </ul>
  );
}

function BlockPicker({ lang, options, draft, set }: { lang: Lang; options: CampaignOptions; draft: Draft; set: (p: Partial<Draft>) => void }) {
  const tr = useTr(lang);
  const hydrated = useHydrated();

  async function apply() {
    if (!draft.aa_content_id) return;
    const res = await bffSend<{ title: string; body: string }>("GET", `campaigns/blocks/${draft.aa_content_id}`);
    if (res.ok) set({ body: res.data.body, subject: draft.subject || res.data.title });
  }

  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "end" }}>
      <span style={{ flex: "1 1 260px", minWidth: 0 }}>
        <Field label={tr("camp.field.block")}>
          <Select value={draft.aa_content_id ?? ""} placeholder={tr("camp.field.blockNone")}
            options={options.blocks.map((b) => ({ value: b.content_id, label: `${b.title} (${b.locale})` }))}
            onChange={(e) => set({ aa_content_id: e.target.value || null })} />
        </Field>
      </span>
      <Button size="sm" disabled={!draft.aa_content_id || !hydrated} onClick={() => void apply()}>
        {tr("camp.field.blockUse")}
      </Button>
    </div>
  );
}

function TagPicker({ lang, options, draft, set }: { lang: Lang; options: CampaignOptions; draft: Draft; set: (p: Partial<Draft>) => void }) {
  const tr = useTr(lang);
  if (options.tags.length === 0) return null;
  const toggle = (code: string, on: boolean) =>
    set({ audience_tags: on ? [...draft.audience_tags, code] : draft.audience_tags.filter((c) => c !== code) });
  return (
    <Field as="div" label={tr("camp.field.tags")} hint={tr("camp.field.tagsHint")}>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
        {options.tags.map((tag) => (
          <Checkbox key={tag.code} label={tag.name} checked={draft.audience_tags.includes(tag.code)} onChange={(e) => toggle(tag.code, e.target.checked)} />
        ))}
      </div>
    </Field>
  );
}

function MailboxAndLanguage({ lang, options, draft, set }: { lang: Lang; options: CampaignOptions; draft: Draft; set: (p: Partial<Draft>) => void }) {
  const tr = useTr(lang);
  const boxes = options.mailboxes.map((m) => ({
    value: m.channel_account_id,
    label: `${tr(m.scope === "agency" ? "camp.field.mailboxAgency" : "camp.field.mailboxMine")}: ${m.address}`,
  }));
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
      <span style={{ flex: "1 1 260px", minWidth: 0 }}>
        <Field label={tr("camp.field.mailbox")} hint={tr("camp.field.mailboxHint")}>
          <Select value={draft.channel_account_id ?? ""} placeholder={tr("camp.field.mailboxNone")} options={boxes}
            onChange={(e) => set({ channel_account_id: e.target.value || null })} />
        </Field>
      </span>
      <span style={{ width: 160 }}>
        <Field label={tr("camp.field.locale")}>
          <Select value={draft.locale} options={[{ value: "en", label: "English" }, { value: "vi", label: "Tiếng Việt" }]}
            onChange={(e) => set({ locale: e.target.value === "vi" ? "vi" : "en" })} />
        </Field>
      </span>
    </div>
  );
}

function GateList({ lang, codes }: { lang: Lang; codes: string[] }) {
  const tr = useTr(lang);
  if (codes.length === 0) return null;
  return (
    <div data-testid="campaign-gate" role="status" style={{ display: "grid", gap: 4, padding: "8px 10px", borderRadius: 8, background: K.amberSoft, color: K.amber }}>
      <strong style={{ fontSize: 13 }}>{tr("camp.gateTitle")}</strong>
      <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
        {codes.map((c) => (
          <li key={c} data-code={c}>
            {gateText(lang, c)}
          </li>
        ))}
      </ul>
    </div>
  );
}

function PreviewBox({ lang, preview }: { lang: Lang; preview: Preview | null }) {
  const tr = useTr(lang);
  if (!preview) return null;
  return (
    <div data-testid="campaign-preview" style={{ display: "grid", gap: 4, fontSize: 13 }}>
      <span>{previewSummary(lang, preview)}</span>
      {excludedText(lang, preview.excluded).map((line) => (
        <span key={line} style={{ color: K.muted }}>
          {line}
        </span>
      ))}
      {preview.over_cap && <span style={{ color: K.danger }}>{tr("camp.preview.overCap")}</span>}
    </div>
  );
}

function canSchedule(preview: Preview | null, gate: string[]): boolean {
  return preview !== null && preview.eligible > 0 && !preview.over_cap && gate.length === 0;
}

/** Save, check and schedule: the three calls of the editor, with their busy flag, preview and error. */
function useEditorCalls(lang: Lang, editing: Editing, onChange: (e: Editing) => void, onSaved: (msg: string) => void, onClose: () => void) {
  const tr = useTr(lang);
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function call<T>(method: "POST" | "PATCH", path: string, body?: unknown): Promise<T | null> {
    setBusy(true);
    setError(null);
    const res = await bffSend<T>(method, path, body);
    setBusy(false);
    if (res.ok) return res.data;
    if (!res.redirecting) setError(tr(campaignErrorKey(res.detail)));
    return null;
  }

  async function save(): Promise<Editing | null> {
    const body = payloadOf(editing.draft);
    const done = editing.id
      ? await call<Saved>("PATCH", `campaigns/${editing.id}`, { ...body, version: editing.version })
      : await call<Saved>("POST", "campaigns", body);
    if (!done) return null;
    const next = { ...editing, id: done.campaign_id, version: done.version, gate: done.gate_codes, state: done.state };
    onChange(next);
    setPreview(null);
    onSaved(tr("camp.saved"));
    return next;
  }

  async function check() {
    const saved = await save();
    const seen = saved?.id ? await call<Preview>("POST", `campaigns/${saved.id}/preview`) : null;
    if (seen) setPreview(seen);
  }

  async function schedule(when: string) {
    if (!editing.id || !preview) return;
    const body = { scheduled_at: scheduleAt(when), expected: preview.eligible, version: editing.version };
    const done = await call<{ eligible: number; days: number }>("POST", `campaigns/${editing.id}/schedule`, body);
    if (!done) return;
    onSaved(fmt(tr("camp.scheduled"), { n: done.eligible, days: done.days }));
    onClose();
  }

  return { busy, preview, error, save, check, schedule };
}

function EditorActions({ lang, calls, gate, onClose }: { lang: Lang; calls: ReturnType<typeof useEditorCalls>; gate: string[]; onClose: () => void }) {
  const tr = useTr(lang);
  const hydrated = useHydrated();
  const [when, setWhen] = useState("");
  const off = calls.busy || !hydrated;
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "end" }}>
      <Button variant="secondary" disabled={off} onClick={() => void calls.save()}>
        {tr("camp.save")}
      </Button>
      <Button variant="secondary" disabled={off} onClick={() => void calls.check()}>
        {tr("camp.preview")}
      </Button>
      {canSchedule(calls.preview, gate) && (
        <>
          <span style={{ width: 230 }}>
            <Field label={tr("camp.field.whenLabel")}>
              <Input size="sm" type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)} />
            </Field>
          </span>
          <Button variant="primary" disabled={off} onClick={() => void calls.schedule(when)}>
            {tr("camp.schedule")}
          </Button>
        </>
      )}
      <Button variant="ghost" onClick={onClose}>
        {tr("camp.close")}
      </Button>
    </div>
  );
}

export function CampaignEditor({
  lang,
  options,
  editing,
  onChange,
  onSaved,
  onClose,
}: {
  lang: Lang;
  options: CampaignOptions;
  editing: Editing;
  onChange: (e: Editing) => void;
  onSaved: (msg: string) => void;
  onClose: () => void;
}) {
  const tr = useTr(lang);
  const d = editing.draft;
  const set = (patch: Partial<Draft>) => onChange({ ...editing, draft: { ...d, ...patch } });
  const calls = useEditorCalls(lang, editing, onChange, onSaved, onClose);
  return (
    <div data-testid="campaign-editor" style={{ display: "grid", gap: 10, padding: 12, border: `1px solid ${K.line2}`, borderRadius: 8, minWidth: 0 }}>
      <Field label={tr("camp.field.name")}>
        <Input value={d.name} maxLength={120} onChange={(e) => set({ name: e.target.value })} />
      </Field>
      <MailboxAndLanguage lang={lang} options={options} draft={d} set={set} />
      <TourPicker lang={lang} editing={editing} onChange={onChange} />
      <BlockPicker lang={lang} options={options} draft={d} set={set} />
      <Switch checked={d.brand_only} label={tr("camp.field.brand")} onChange={(v: boolean) => set({ brand_only: v })} />
      <TagPicker lang={lang} options={options} draft={d} set={set} />
      <Field label={tr("camp.field.subject")}>
        <Input value={d.subject} maxLength={300} onChange={(e) => set({ subject: e.target.value })} />
      </Field>
      <Field label={tr("camp.field.body")} hint={tr("camp.field.bodyHint")}>
        <Textarea rows={8} value={d.body} maxLength={20000} onChange={(e) => set({ body: e.target.value })} />
      </Field>
      <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>
        {fmt(tr("camp.limits"), { limit: options.daily_limit, cap: options.max_recipients })} {tr("camp.consentHint")}
      </p>
      <GateList lang={lang} codes={editing.gate} />
      <PreviewBox lang={lang} preview={calls.preview} />
      {calls.error && (
        <span role="alert" style={{ color: K.danger, fontSize: 13 }}>
          {calls.error}
        </span>
      )}
      <EditorActions lang={lang} calls={calls} gate={editing.gate} onClose={onClose} />
    </div>
  );
}
