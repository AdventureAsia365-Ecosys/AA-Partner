"use client";
// web/components/tags/TagsSettings.tsx — Settings > Tags (card CRM-29b, AA-960; design v3 R3, R29): the agency's trip
// and client tags with how many records carry each, and the app's system tags (read only, worded by code). The owner
// renames, recolours, archives / restores and deletes a tag (the delete dialog says how many records lose it); every
// other role reads. Changes go straight to the API (PATCH / DELETE /tags/{id}) and the list reloads.
import { useState } from "react";
import { Badge, Button, ConfirmModal, Input, K, Select, Switch, useHydrated } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import { TONES, tagErrorKey, tagLabel, type Subject, type Tag, type TagTone, type TagVocabulary } from "@/lib/tags";

function TagRow({ tag, canManage, lang, onDone }: { tag: Tag; canManage: boolean; lang: Lang; onDone: (msg: string | null, error?: boolean) => void }) {
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [name, setName] = useState(tag.name);
  const [tone, setTone] = useState<TagTone>(tag.tone);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const dirty = name.trim() !== tag.name || tone !== tag.tone;

  async function patch(body: Record<string, unknown>) {
    setBusy(true);
    const res = await bffSend<Tag>("PATCH", `tags/${tag.tag_id}`, body);
    setBusy(false);
    if (!res.ok) return res.redirecting ? undefined : onDone(tr(tagErrorKey(res.detail)), true);
    onDone(tr("tag.settings.saved"));
  }

  async function remove() {
    setBusy(true);
    const res = await bffSend("DELETE", `tags/${tag.tag_id}`);
    setBusy(false);
    setConfirm(false);
    if (!res.ok) return res.redirecting ? undefined : onDone(tr(tagErrorKey(res.detail)), true);
    onDone(null);
  }

  return (
    <li data-testid="tag-row" data-code={tag.code} data-archived={tag.archived ? "" : undefined} style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center", padding: "6px 0", borderTop: `1px solid ${K.line}` }}>
      <span style={{ minWidth: 140 }}>
        <Badge tone={tone} dot={false}>
          {tagLabel(lang, tag)}
        </Badge>
      </span>
      <span style={{ fontSize: 12.5, color: K.muted, minWidth: 90 }}>{fmt(tr("tag.settings.uses"), { n: tag.uses ?? 0 })}</span>
      {tag.archived && <Badge tone="neutral">{tr("tag.settings.archived")}</Badge>}
      {canManage && tag.kind === "agency" && (
        <span style={{ display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center", flex: "1 1 320px" }}>
          <span style={{ flex: "1 1 160px", minWidth: 0 }}>
            <Input size="sm" aria-label={tr("tag.settings.name")} maxLength={60} value={name} onChange={(e) => setName(e.target.value)} />
          </span>
          <span style={{ width: 130 }}>
            <Select size="sm" aria-label={tr("tag.settings.tone")} value={tone} onChange={(e) => setTone(e.target.value as TagTone)}
              options={TONES.map((x) => ({ value: x, label: tr(`tag.tone.${x}`) }))} />
          </span>
          <Button size="sm" variant="primary" disabled={!dirty || busy || !hydrated} onClick={() => void patch({ name, tone })}>
            {tr("tag.settings.save")}
          </Button>
          <Button size="sm" variant="ghost" disabled={busy || !hydrated} onClick={() => void patch({ archived: !tag.archived })}>
            {tr(tag.archived ? "tag.settings.restore" : "tag.settings.archive")}
          </Button>
          <Button size="sm" variant="danger" disabled={busy || !hydrated} onClick={() => setConfirm(true)}>
            {tr("tag.settings.delete")}
          </Button>
        </span>
      )}
      <ConfirmModal
        open={confirm}
        onClose={() => setConfirm(false)}
        onConfirm={() => void remove()}
        title={fmt(tr("tag.settings.deleteTitle"), { tag: tag.name })}
        body={fmt(tr("tag.settings.deleteBody"), { n: tag.uses ?? 0 })}
        confirmLabel={tr("tag.settings.delete")}
        cancelLabel={tr("tag.bulk.cancel")}
        destructive
        busy={busy}
        testId="tag-delete-dialog"
      />
    </li>
  );
}

export function TagsSettings({ initial, lang }: { initial: TagVocabulary; lang: Lang }) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const [view, setView] = useState(initial);
  const [archived, setArchived] = useState(false);
  const [notice, setNotice] = useState<{ text: string; error: boolean } | null>(null);

  async function reload(showArchived = archived) {
    const res = await bffSend<TagVocabulary>("GET", `tags${showArchived ? "?archived=true" : ""}`);
    if (res.ok) setView(res.data);
  }

  function done(msg: string | null, error = false) {
    setNotice(msg ? { text: msg, error } : null);
    void reload();
  }

  const section = (subject: Subject, title: string) => {
    const agency = view.tags.filter((x) => x.applies_to === subject && x.kind === "agency");
    const system = view.tags.filter((x) => x.applies_to === subject && x.kind === "system");
    return (
      <Panel title={title} testId={`tags-${subject}`}>
        {agency.length === 0 ? (
          <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{tr("tag.settings.empty")}</p>
        ) : (
          <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
            {agency.map((x) => (
              <TagRow key={`${x.tag_id}:${x.name}:${x.tone}:${x.archived}`} tag={x} canManage={view.can_manage} lang={lang} onDone={done} />
            ))}
          </ul>
        )}
        {system.length > 0 && (
          <div style={{ marginTop: 12 }}>
            <p style={{ margin: "0 0 4px", fontSize: 12.5, color: K.muted }} title={tr("tag.systemHint")}>
              {tr("tag.settings.systemTags")}
            </p>
            <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
              {system.map((x) => (
                <TagRow key={x.tag_id} tag={x} canManage={false} lang={lang} onDone={done} />
              ))}
            </ul>
          </div>
        )}
      </Panel>
    );
  };

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <Switch
          checked={archived}
          label={tr("tag.settings.showArchived")}
          onChange={(on) => {
            setArchived(on);
            void reload(on);
          }}
        />
        <span style={{ fontSize: 13 }}>{tr("tag.settings.showArchived")}</span>
      </div>
      {notice && (
        <p role="status" style={{ margin: 0, fontSize: 13, color: notice.error ? K.danger : K.success }}>
          {notice.text}
        </p>
      )}
      {section("trip", tr("tag.settings.trips"))}
      {section("client", tr("tag.settings.clients"))}
    </div>
  );
}
