"use client";
// web/components/campaigns/CampaignsPanel.tsx — Marketing > Campaigns (card CRM-36, AA-968; design v3 R14, A-15).
// The agency's campaigns with their state and counts; owner / agent write a draft (name, the sending mailbox: the
// agency's or their own, never Adventure Asia's; the Adventure Asia tours or campaign text it is about, or "news about
// our agency"; client tags; subject and text), see the content rules it still breaks (root ADR 0019 d21, worded by
// code), check who gets it (left-out reasons, the days at the mailbox's daily limit), then schedule it with the
// previewed count. A stopped campaign says why and is edited and scheduled again; a campaign is cancelled with a
// confirm. Every call goes through the BFF (/bff/campaigns*); the API decides every rule.
import { useState } from "react";
import { Badge, Button, Checkbox, ChipButton, ConfirmModal, EmptyState, Field, Input, K, Select, Switch, Textarea, useHydrated } from "@/app/_kit";
import { bffSend } from "@/lib/bffClient";
import {
  STATE_TONE,
  campaignErrorKey,
  cancellable,
  draftOf,
  editable,
  emptyDraft,
  excludedText,
  gateText,
  holdText,
  payloadOf,
  previewSummary,
  scheduleAt,
  type CampaignDetail,
  type CampaignItem,
  type CampaignList,
  type CampaignOptions,
  type Draft,
  type Preview,
  type TourHit,
} from "@/lib/campaigns";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";

type Editing = { id: string | null; draft: Draft; version: number; gate: string[]; state: string; tours: Record<string, string> };

function Editor({
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
  onSaved: (msg: string, error?: boolean) => void;
  onClose: () => void;
}) {
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const d = editing.draft;
  const set = (patch: Partial<Draft>) => onChange({ ...editing, draft: { ...d, ...patch } });
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<TourHit[] | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [when, setWhen] = useState("");
  const [error, setError] = useState<string | null>(null);

  function fail(detail: string) {
    setError(tr(campaignErrorKey(detail)));
  }

  async function save(): Promise<Editing | null> {
    setBusy(true);
    setError(null);
    const body = payloadOf(d);
    const res = editing.id
      ? await bffSend<{ campaign_id: string; gate_codes: string[]; version: number; state: string }>("PATCH", `campaigns/${editing.id}`, { ...body, version: editing.version })
      : await bffSend<{ campaign_id: string; gate_codes: string[]; version: number; state: string }>("POST", "campaigns", body);
    setBusy(false);
    if (!res.ok) {
      if (!res.redirecting) fail(res.detail);
      return null;
    }
    const next = { ...editing, id: res.data.campaign_id, version: res.data.version, gate: res.data.gate_codes, state: res.data.state };
    onChange(next);
    setPreview(null);
    onSaved(tr("camp.saved"));
    return next;
  }

  async function check() {
    const saved = await save();
    if (!saved?.id) return;
    setBusy(true);
    const res = await bffSend<Preview>("POST", `campaigns/${saved.id}/preview`);
    setBusy(false);
    if (!res.ok) return res.redirecting ? undefined : fail(res.detail);
    setPreview(res.data);
  }

  async function schedule() {
    if (!editing.id || !preview) return;
    setBusy(true);
    const res = await bffSend<{ eligible: number; days: number }>("POST", `campaigns/${editing.id}/schedule`, {
      scheduled_at: scheduleAt(when),
      expected: preview.eligible,
      version: editing.version,
    });
    setBusy(false);
    if (!res.ok) return res.redirecting ? undefined : fail(res.detail);
    onSaved(fmt(tr("camp.scheduled"), { n: res.data.eligible, days: res.data.days }));
    onClose();
  }

  async function search() {
    const res = await bffSend<{ items: TourHit[] }>("GET", `campaigns/tours?${new URLSearchParams({ q: query.trim() })}`);
    if (res.ok) setHits(res.data.items);
  }

  async function applyBlock() {
    if (!d.aa_content_id) return;
    const res = await bffSend<{ title: string; body: string }>("GET", `campaigns/blocks/${d.aa_content_id}`);
    if (res.ok) set({ body: res.data.body, subject: d.subject || res.data.title });
  }

  function addTour(hit: TourHit) {
    if (d.aa_tour_ids.includes(hit.source_id)) return;
    onChange({ ...editing, tours: { ...editing.tours, [hit.source_id]: hit.title }, draft: { ...d, aa_tour_ids: [...d.aa_tour_ids, hit.source_id] } });
  }

  const boxes = options.mailboxes.map((m) => ({
    value: m.channel_account_id,
    label: `${tr(m.scope === "agency" ? "camp.field.mailboxAgency" : "camp.field.mailboxMine")}: ${m.address}`,
  }));
  return (
    <div data-testid="campaign-editor" style={{ display: "grid", gap: 10, padding: 12, border: `1px solid ${K.line2}`, borderRadius: 8, minWidth: 0 }}>
      <Field label={tr("camp.field.name")}>
        <Input value={d.name} maxLength={120} onChange={(e) => set({ name: e.target.value })} />
      </Field>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
        <span style={{ flex: "1 1 260px", minWidth: 0 }}>
          <Field label={tr("camp.field.mailbox")} hint={tr("camp.field.mailboxHint")}>
            <Select value={d.channel_account_id ?? ""} placeholder={tr("camp.field.mailboxNone")} options={boxes}
              onChange={(e) => set({ channel_account_id: e.target.value || null })} />
          </Field>
        </span>
        <span style={{ width: 160 }}>
          <Field label={tr("camp.field.locale")}>
            <Select value={d.locale} options={[{ value: "en", label: "English" }, { value: "vi", label: "Tiếng Việt" }]}
              onChange={(e) => set({ locale: e.target.value === "vi" ? "vi" : "en" })} />
          </Field>
        </span>
      </div>
      <Field as="div" label={tr("camp.field.tours")}>
        <div style={{ display: "grid", gap: 6 }}>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }} data-testid="campaign-tours">
            {d.aa_tour_ids.map((id) => (
              <ChipButton key={id} size="sm" selected aria-label={fmt(tr("camp.field.tourRemove"), { tour: editing.tours[id] ?? id })}
                onClick={() => set({ aa_tour_ids: d.aa_tour_ids.filter((x) => x !== id) })}>
                {editing.tours[id] ?? id} ×
              </ChipButton>
            ))}
          </div>
          <div style={{ display: "flex", gap: 6 }}>
            <span style={{ flex: "1 1 auto", minWidth: 0 }}>
              <Input size="sm" aria-label={tr("camp.field.tourSearch")} placeholder={tr("camp.field.tourSearch")} value={query}
                onChange={(e) => setQuery(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); void search(); } }} />
            </span>
            <Button size="sm" disabled={!hydrated} onClick={() => void search()}>
              {tr("camp.field.tourSearch")}
            </Button>
          </div>
          {hits && (hits.length === 0 ? (
            <span style={{ fontSize: 12.5, color: K.muted }}>{tr("camp.field.tourNone")}</span>
          ) : (
            <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 4 }}>
              {hits.map((h) => (
                <li key={h.source_id} style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 13 }}>
                  <span style={{ flex: "1 1 auto", overflowWrap: "anywhere" }}>{h.title}</span>
                  <Button size="sm" variant="ghost" onClick={() => addTour(h)} disabled={d.aa_tour_ids.includes(h.source_id)}>
                    {tr("camp.field.tourAdd")}
                  </Button>
                </li>
              ))}
            </ul>
          ))}
        </div>
      </Field>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "end" }}>
        <span style={{ flex: "1 1 260px", minWidth: 0 }}>
          <Field label={tr("camp.field.block")}>
            <Select value={d.aa_content_id ?? ""} placeholder={tr("camp.field.blockNone")}
              options={options.blocks.map((b) => ({ value: b.content_id, label: `${b.title} (${b.locale})` }))}
              onChange={(e) => set({ aa_content_id: e.target.value || null })} />
          </Field>
        </span>
        <Button size="sm" disabled={!d.aa_content_id || !hydrated} onClick={() => void applyBlock()}>
          {tr("camp.field.blockUse")}
        </Button>
      </div>
      <Switch checked={d.brand_only} label={tr("camp.field.brand")} onChange={(v: boolean) => set({ brand_only: v })} />
      {options.tags.length > 0 && (
        <Field as="div" label={tr("camp.field.tags")} hint={tr("camp.field.tagsHint")}>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
            {options.tags.map((tag) => (
              <Checkbox key={tag.code} label={tag.name} checked={d.audience_tags.includes(tag.code)}
                onChange={(e) => set({ audience_tags: e.target.checked ? [...d.audience_tags, tag.code] : d.audience_tags.filter((c) => c !== tag.code) })} />
            ))}
          </div>
        </Field>
      )}
      <Field label={tr("camp.field.subject")}>
        <Input value={d.subject} maxLength={300} onChange={(e) => set({ subject: e.target.value })} />
      </Field>
      <Field label={tr("camp.field.body")} hint={tr("camp.field.bodyHint")}>
        <Textarea rows={8} value={d.body} maxLength={20000} onChange={(e) => set({ body: e.target.value })} />
      </Field>
      <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>
        {fmt(tr("camp.limits"), { limit: options.daily_limit, cap: options.max_recipients })} {tr("camp.consentHint")}
      </p>
      {editing.gate.length > 0 && (
        <div data-testid="campaign-gate" role="status" style={{ display: "grid", gap: 4, padding: "8px 10px", borderRadius: 8, background: K.amberSoft, color: K.amber }}>
          <strong style={{ fontSize: 13 }}>{tr("camp.gateTitle")}</strong>
          <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
            {editing.gate.map((c) => (
              <li key={c} data-code={c}>
                {gateText(lang, c)}
              </li>
            ))}
          </ul>
        </div>
      )}
      {preview && (
        <div data-testid="campaign-preview" style={{ display: "grid", gap: 4, fontSize: 13 }}>
          <span>{previewSummary(lang, preview)}</span>
          {excludedText(lang, preview.excluded).map((line) => (
            <span key={line} style={{ color: K.muted }}>
              {line}
            </span>
          ))}
          {preview.over_cap && <span style={{ color: K.danger }}>{tr("camp.preview.overCap")}</span>}
        </div>
      )}
      {error && (
        <span role="alert" style={{ color: K.danger, fontSize: 13 }}>
          {error}
        </span>
      )}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "end" }}>
        <Button variant="secondary" disabled={busy || !hydrated} onClick={() => void save()}>
          {tr("camp.save")}
        </Button>
        <Button variant="secondary" disabled={busy || !hydrated} onClick={() => void check()}>
          {tr("camp.preview")}
        </Button>
        {preview && preview.eligible > 0 && !preview.over_cap && editing.gate.length === 0 && (
          <>
            <span style={{ width: 230 }}>
              <Field label={tr("camp.field.whenLabel")}>
                <Input size="sm" type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)} />
              </Field>
            </span>
            <Button variant="primary" disabled={busy || !hydrated} onClick={() => void schedule()}>
              {tr("camp.schedule")}
            </Button>
          </>
        )}
        <Button variant="ghost" onClick={onClose}>
          {tr("camp.close")}
        </Button>
      </div>
    </div>
  );
}

export function CampaignsPanel({ lang, initial, options }: { lang: Lang; initial: CampaignList; options: CampaignOptions }) {
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [items, setItems] = useState<CampaignItem[]>(initial.items);
  const [editing, setEditing] = useState<Editing | null>(null);
  const [message, setMessage] = useState<{ text: string; error?: boolean } | null>(null);
  const [confirm, setConfirm] = useState<{ item: CampaignItem; kind: "cancel" | "delete" } | null>(null);
  const [busy, setBusy] = useState(false);
  const canWrite = initial.can_send && initial.feature_allowed;

  async function reload() {
    const res = await bffSend<CampaignList>("GET", "campaigns");
    if (res.ok) setItems(res.data.items);
  }

  async function open(item: CampaignItem) {
    const res = await bffSend<CampaignDetail>("GET", `campaigns/${item.campaign_id}`);
    if (!res.ok) return res.redirecting ? undefined : setMessage({ text: tr(campaignErrorKey(res.detail)), error: true });
    const tours = Object.fromEntries(res.data.aa_tours.map((x) => [x.source_id, x.title ?? x.source_id]));
    setEditing({ id: item.campaign_id, draft: draftOf(res.data), version: res.data.version, gate: res.data.gate_codes, state: res.data.state, tours });
  }

  async function act() {
    if (!confirm) return;
    setBusy(true);
    const { item, kind } = confirm;
    const res = kind === "cancel"
      ? await bffSend("POST", `campaigns/${item.campaign_id}/cancel`, { version: item.version })
      : await bffSend("DELETE", `campaigns/${item.campaign_id}`);
    setBusy(false);
    setConfirm(null);
    if (!res.ok) return res.redirecting ? undefined : setMessage({ text: tr(campaignErrorKey(res.detail)), error: true });
    setMessage({ text: tr(kind === "cancel" ? "camp.cancelled" : "camp.deleted") });
    await reload();
  }

  return (
    <div data-testid="campaigns" style={{ display: "grid", gap: 12, minWidth: 0 }}>
      <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{tr("camp.intro")}</p>
      {!initial.feature_allowed && <EmptyState title={tr("camp.notAllowed")} description={tr("camp.notAllowedBody")} />}
      {message && (
        <span role={message.error ? "alert" : "status"} style={{ fontSize: 13, color: message.error ? K.danger : K.success }}>
          {message.text}
        </span>
      )}
      {canWrite && !editing && (
        <div>
          <Button variant="primary" disabled={!hydrated} onClick={() => setEditing({ id: null, draft: emptyDraft(), version: 1, gate: [], state: "draft", tours: {} })}>
            {tr("camp.new")}
          </Button>
        </div>
      )}
      {editing && (
        <Editor lang={lang} options={options} editing={editing} onChange={setEditing}
          onSaved={(text, error) => { setMessage({ text, error }); void reload(); }}
          onClose={() => { setEditing(null); void reload(); }} />
      )}
      {items.length === 0 ? (
        initial.feature_allowed && <span style={{ fontSize: 13, color: K.muted }}>{tr("camp.empty")}</span>
      ) : (
        <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 8 }}>
          {items.map((c) => (
            <li key={c.campaign_id} data-testid="campaign-row" data-state={c.state}
              style={{ display: "grid", gap: 4, padding: "10px 0", borderTop: `1px solid ${K.line}`, minWidth: 0 }}>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
                <strong style={{ fontSize: 14, overflowWrap: "anywhere" }}>{c.name}</strong>
                <Badge tone={STATE_TONE[c.state]} dot={false}>
                  {tr(`camp.state.${c.state}`)}
                </Badge>
                {canWrite && (
                  <span style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
                    {editable(c.state) && (
                      <Button size="sm" variant="ghost" onClick={() => void open(c)}>
                        {tr("camp.edit")}
                      </Button>
                    )}
                    {c.state === "draft" && (
                      <Button size="sm" variant="ghost" onClick={() => setConfirm({ item: c, kind: "delete" })}>
                        {tr("camp.delete")}
                      </Button>
                    )}
                    {cancellable(c.state) && c.state !== "draft" && (
                      <Button size="sm" variant="danger" onClick={() => setConfirm({ item: c, kind: "cancel" })}>
                        {tr("camp.cancel")}
                      </Button>
                    )}
                  </span>
                )}
              </div>
              {c.state === "held" && <span style={{ fontSize: 13, color: K.amber }}>{holdText(lang, c.hold_code)}</span>}
              {c.state !== "draft" && (
                <span style={{ fontSize: 12.5, color: K.muted }}>
                  {fmt(tr("camp.counts"), { queued: c.counts.queued, planned: c.counts.planned, skipped: c.counts.skipped, unsubscribed: c.counts.unsubscribed })}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
      <ConfirmModal open={confirm !== null} onClose={() => setConfirm(null)} onConfirm={() => void act()} busy={busy}
        title={confirm?.kind === "delete" ? tr("camp.delete") : tr("camp.cancel")}
        body={confirm?.kind === "delete" ? tr("camp.deleteConfirm") : tr("camp.cancelConfirm")}
        confirmLabel={confirm?.kind === "delete" ? tr("camp.delete") : tr("camp.cancel")} danger />
    </div>
  );
}
