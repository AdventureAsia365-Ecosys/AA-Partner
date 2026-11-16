"use client";
// web/components/campaigns/CampaignsPanel.tsx — Marketing > Campaigns (card CRM-36, AA-968; design v3 R14, A-15).
// The agency's campaigns with their state and counts; owner / agent open a new or a draft / stopped campaign in the
// editor (CampaignEditor.tsx), delete a draft, cancel a scheduled one with a confirm. A plan without the campaigns
// feature reads only. A stopped campaign says why. Every call goes through the BFF (/bff/campaigns*); the API decides.
import { useState } from "react";
import { Badge, Button, ConfirmModal, EmptyState, K, useHydrated } from "@/app/_kit";
import { CampaignEditor, type Editing } from "@/components/campaigns/CampaignEditor";
import { bffSend } from "@/lib/bffClient";
import {
  STATE_TONE,
  campaignErrorKey,
  cancellable,
  draftOf,
  editable,
  emptyDraft,
  holdText,
  type CampaignDetail,
  type CampaignItem,
  type CampaignList,
  type CampaignOptions,
} from "@/lib/campaigns";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";

type Confirm = { item: CampaignItem; kind: "cancel" | "delete" };
type Notice = { text: string; error?: boolean };

function RowActions({ lang, c, onEdit, onConfirm }: { lang: Lang; c: CampaignItem; onEdit: (c: CampaignItem) => void; onConfirm: (x: Confirm) => void }) {
  const tr = (key: string) => t(lang, key as MessageKey);
  return (
    <span style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
      {editable(c.state) && (
        <Button size="sm" variant="ghost" onClick={() => onEdit(c)}>
          {tr("camp.edit")}
        </Button>
      )}
      {c.state === "draft" && (
        <Button size="sm" variant="ghost" onClick={() => onConfirm({ item: c, kind: "delete" })}>
          {tr("camp.delete")}
        </Button>
      )}
      {cancellable(c.state) && c.state !== "draft" && (
        <Button size="sm" variant="danger" onClick={() => onConfirm({ item: c, kind: "cancel" })}>
          {tr("camp.cancel")}
        </Button>
      )}
    </span>
  );
}

function CampaignRow({ lang, c, canWrite, onEdit, onConfirm }: {
  lang: Lang; c: CampaignItem; canWrite: boolean; onEdit: (c: CampaignItem) => void; onConfirm: (x: Confirm) => void;
}) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const counts = { queued: c.counts.queued, planned: c.counts.planned, skipped: c.counts.skipped, unsubscribed: c.counts.unsubscribed };
  return (
    <li data-testid="campaign-row" data-state={c.state} style={{ display: "grid", gap: 4, padding: "10px 0", borderTop: `1px solid ${K.line}`, minWidth: 0 }}>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
        <strong style={{ fontSize: 14, overflowWrap: "anywhere" }}>{c.name}</strong>
        <Badge tone={STATE_TONE[c.state]} dot={false}>
          {tr(`camp.state.${c.state}`)}
        </Badge>
        {canWrite && <RowActions lang={lang} c={c} onEdit={onEdit} onConfirm={onConfirm} />}
      </div>
      {c.state === "held" && <span style={{ fontSize: 13, color: K.amber }}>{holdText(lang, c.hold_code)}</span>}
      {c.state !== "draft" && <span style={{ fontSize: 12.5, color: K.muted }}>{fmt(tr("camp.counts"), counts)}</span>}
    </li>
  );
}

function CampaignRows({ lang, items, allowed, canWrite, onEdit, onConfirm }: {
  lang: Lang; items: CampaignItem[]; allowed: boolean; canWrite: boolean; onEdit: (c: CampaignItem) => void; onConfirm: (x: Confirm) => void;
}) {
  if (items.length === 0) {
    return allowed ? <span style={{ fontSize: 13, color: K.muted }}>{t(lang, "camp.empty")}</span> : null;
  }
  return (
    <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 8 }}>
      {items.map((c) => (
        <CampaignRow key={c.campaign_id} lang={lang} c={c} canWrite={canWrite} onEdit={onEdit} onConfirm={onConfirm} />
      ))}
    </ul>
  );
}

function ConfirmDialog({ lang, confirm, busy, onClose, onConfirm }: {
  lang: Lang; confirm: Confirm | null; busy: boolean; onClose: () => void; onConfirm: () => void;
}) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const deleting = confirm?.kind === "delete";
  return (
    <ConfirmModal open={confirm !== null} onClose={onClose} onConfirm={onConfirm} busy={busy} danger
      title={tr(deleting ? "camp.delete" : "camp.cancel")}
      body={tr(deleting ? "camp.deleteConfirm" : "camp.cancelConfirm")}
      confirmLabel={tr(deleting ? "camp.delete" : "camp.cancel")} />
  );
}

function NoticeLine({ notice }: { notice: Notice | null }) {
  if (!notice) return null;
  return (
    <span role={notice.error ? "alert" : "status"} style={{ fontSize: 13, color: notice.error ? K.danger : K.success }}>
      {notice.text}
    </span>
  );
}

export function CampaignsPanel({ lang, initial, options }: { lang: Lang; initial: CampaignList; options: CampaignOptions }) {
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [items, setItems] = useState<CampaignItem[]>(initial.items);
  const [editing, setEditing] = useState<Editing | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [confirm, setConfirm] = useState<Confirm | null>(null);
  const [busy, setBusy] = useState(false);
  const canWrite = initial.can_send && initial.feature_allowed;
  const failed = (detail: string) => setNotice({ text: tr(campaignErrorKey(detail)), error: true });

  async function reload() {
    const res = await bffSend<CampaignList>("GET", "campaigns");
    if (res.ok) setItems(res.data.items);
  }

  async function open(item: CampaignItem) {
    const res = await bffSend<CampaignDetail>("GET", `campaigns/${item.campaign_id}`);
    if (!res.ok) return res.redirecting ? undefined : failed(res.detail);
    const tours = Object.fromEntries(res.data.aa_tours.map((x) => [x.source_id, x.title ?? x.source_id]));
    setEditing({ id: item.campaign_id, draft: draftOf(res.data), version: res.data.version, gate: res.data.gate_codes, state: res.data.state, tours });
  }

  async function act() {
    if (!confirm) return;
    const { item, kind } = confirm;
    setBusy(true);
    const res = kind === "cancel"
      ? await bffSend("POST", `campaigns/${item.campaign_id}/cancel`, { version: item.version })
      : await bffSend("DELETE", `campaigns/${item.campaign_id}`);
    setBusy(false);
    setConfirm(null);
    if (!res.ok) return res.redirecting ? undefined : failed(res.detail);
    setNotice({ text: tr(kind === "cancel" ? "camp.cancelled" : "camp.deleted") });
    await reload();
  }

  const fresh = (): Editing => ({ id: null, draft: emptyDraft(), version: 1, gate: [], state: "draft", tours: {} });
  return (
    <div data-testid="campaigns" style={{ display: "grid", gap: 12, minWidth: 0 }}>
      <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{tr("camp.intro")}</p>
      {!initial.feature_allowed && <EmptyState title={tr("camp.notAllowed")} description={tr("camp.notAllowedBody")} />}
      <NoticeLine notice={notice} />
      {canWrite && !editing && (
        <div>
          <Button variant="primary" disabled={!hydrated} onClick={() => setEditing(fresh())}>
            {tr("camp.new")}
          </Button>
        </div>
      )}
      {editing && (
        <CampaignEditor lang={lang} options={options} editing={editing} onChange={setEditing}
          onSaved={(text) => { setNotice({ text }); void reload(); }}
          onClose={() => { setEditing(null); void reload(); }} />
      )}
      <CampaignRows lang={lang} items={items} allowed={initial.feature_allowed} canWrite={canWrite} onEdit={(c) => void open(c)} onConfirm={setConfirm} />
      <ConfirmDialog lang={lang} confirm={confirm} busy={busy} onClose={() => setConfirm(null)} onConfirm={() => void act()} />
    </div>
  );
}
