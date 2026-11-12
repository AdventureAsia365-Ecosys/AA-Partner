"use client";
// web/components/settings/InclusionLibraryView.tsx — Settings > Proposal lines (AA-1070 item crm-35, crm-059): the
// agency's library of standard "Included" / "Not included" lines the builder offers (English text, optional
// Vietnamese text). Add, edit, reorder, archive (no longer offered; proposals that picked a line keep their copy),
// restore, delete. Every role reads; the owner and agents change it (the API refuses the assistant). Kit only.
import { useState } from "react";
import { Badge, Button, Checkbox, Field, Input, K, Select, useHydrated } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { t, type Lang, type MessageKey } from "@/lib/i18n";
import { INCLUSION_KINDS, inclusionErrorKey, typedLine, type InclusionItem, type InclusionKind } from "@/lib/inclusions";

type Edit = { id: string; en: string; vi: string };

/** The library in display order: kind, then the agency's order, then the text. */
export function sortedItems(items: InclusionItem[]): InclusionItem[] {
  return [...items].sort(
    (a, b) => INCLUSION_KINDS.indexOf(a.kind) - INCLUSION_KINDS.indexOf(b.kind) || a.sort_order - b.sort_order || a.text_en.localeCompare(b.text_en),
  );
}

export function InclusionLibraryView({ items: initial, canEdit, lang }: { items: InclusionItem[]; canEdit: boolean; lang: Lang }) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const hydrated = useHydrated();
  const [items, setItems] = useState(sortedItems(initial));
  const [showArchived, setShowArchived] = useState(false);
  const [kind, setKind] = useState<InclusionKind>("included");
  const [en, setEn] = useState("");
  const [vi, setVi] = useState("");
  const [edit, setEdit] = useState<Edit | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ tone: "danger" | "success"; text: string } | null>(null);
  const disabled = busy || !hydrated || !canEdit;
  const typed = typedLine(kind, en, vi);

  async function send<T>(method: "POST" | "PATCH" | "DELETE", path: string, body?: unknown): Promise<T | null> {
    setBusy(true);
    const res = await bffSend<T>(method, path, body);
    setBusy(false);
    if (!res.ok) {
      if (!res.redirecting) setNotice({ tone: "danger", text: tr(inclusionErrorKey(res.detail)) });
      return null;
    }
    setNotice({ tone: "success", text: tr("incl.saved") });
    return res.data;
  }

  function replaced(item: InclusionItem) {
    setItems(sortedItems(items.map((x) => (x.inclusion_item_id === item.inclusion_item_id ? item : x))));
  }

  async function add() {
    if (!typed) return;
    const made = await send<InclusionItem>("POST", "inclusion-items", { kind, text_en: typed.text_en, text_vi: typed.text_vi });
    if (made) {
      setItems(sortedItems([...items, made]));
      setEn("");
      setVi("");
    }
  }

  async function move(item: InclusionItem, by: -1 | 1) {
    const same = items.filter((x) => x.kind === item.kind);
    const other = same[same.indexOf(item) + by];
    if (!other) return;
    // Swap the two orders (equal orders: the moved line takes the next free number).
    const a = await send<InclusionItem>("PATCH", `inclusion-items/${item.inclusion_item_id}`, { sort_order: other.sort_order === item.sort_order ? Math.max(0, item.sort_order + by) : other.sort_order });
    if (!a) return;
    const b = other.sort_order === item.sort_order ? other : await send<InclusionItem>("PATCH", `inclusion-items/${other.inclusion_item_id}`, { sort_order: item.sort_order });
    setItems(sortedItems(items.map((x) => (x.inclusion_item_id === a.inclusion_item_id ? a : b && x.inclusion_item_id === b.inclusion_item_id ? b : x))));
  }

  async function saveEdit() {
    if (!edit) return;
    const line = typedLine("included", edit.en, edit.vi);
    if (!line) return;
    const out = await send<InclusionItem>("PATCH", `inclusion-items/${edit.id}`, { text_en: line.text_en, text_vi: line.text_vi });
    if (out) {
      replaced(out);
      setEdit(null);
    }
  }

  return (
    <div style={{ display: "grid", gap: 16 }}>
      {notice && (
        <p role="status" style={{ margin: 0, fontSize: 13, color: notice.tone === "danger" ? K.danger : K.success }}>
          {notice.text}
        </p>
      )}
      <Checkbox label={tr("incl.showArchived")} checked={showArchived} onChange={(e) => setShowArchived(e.currentTarget.checked)} />
      {INCLUSION_KINDS.map((k) => {
        const shown = items.filter((x) => x.kind === k && (showArchived || !x.archived));
        const live = items.filter((x) => x.kind === k);
        return (
          <Panel key={k} title={tr(`incl.kind.${k}`)} testId={`incl-library-${k}`}>
            {shown.length === 0 && <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{tr("incl.none")}</p>}
            <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 8 }}>
              {shown.map((x) => {
                const at = live.indexOf(x);
                return (
                  <li key={x.inclusion_item_id} data-testid="incl-item" style={{ display: "grid", gap: 4, borderTop: `1px solid ${K.line}`, paddingTop: 6 }}>
                    {edit?.id === x.inclusion_item_id ? (
                      <div style={{ display: "grid", gap: 8, gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", alignItems: "end" }}>
                        <Field label={tr("incl.textEn")}>
                          <Input size="sm" maxLength={300} value={edit.en} onChange={(e) => setEdit({ ...edit, en: e.target.value })} />
                        </Field>
                        <Field label={tr("incl.textVi")}>
                          <Input size="sm" maxLength={300} value={edit.vi} onChange={(e) => setEdit({ ...edit, vi: e.target.value })} />
                        </Field>
                        <div style={{ display: "flex", gap: 6 }}>
                          <Button size="sm" variant="primary" disabled={disabled || !typedLine("included", edit.en, edit.vi)} onClick={() => void saveEdit()}>
                            {tr("incl.saveLine")}
                          </Button>
                          <Button size="sm" variant="ghost" disabled={busy} onClick={() => setEdit(null)}>
                            {tr("incl.cancel")}
                          </Button>
                        </div>
                      </div>
                    ) : (
                      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                        <span style={{ flex: "1 1 200px", fontSize: 13, overflowWrap: "anywhere", minWidth: 0 }}>
                          {x.text_en}
                          {x.text_vi && <span style={{ color: K.muted }}> · {x.text_vi}</span>}
                        </span>
                        {x.archived && <Badge>{tr("incl.archived")}</Badge>}
                        {canEdit && (
                          <span style={{ display: "inline-flex", gap: 4, flexWrap: "wrap" }}>
                            <Button size="sm" variant="ghost" ariaLabel={tr("incl.up")} disabled={disabled || at <= 0} onClick={() => void move(x, -1)}>
                              ↑
                            </Button>
                            <Button size="sm" variant="ghost" ariaLabel={tr("incl.down")} disabled={disabled || at >= live.length - 1} onClick={() => void move(x, 1)}>
                              ↓
                            </Button>
                            <Button size="sm" variant="ghost" disabled={disabled} onClick={() => setEdit({ id: x.inclusion_item_id, en: x.text_en, vi: x.text_vi ?? "" })}>
                              {tr("incl.edit")}
                            </Button>
                            <Button
                              size="sm"
                              variant="ghost"
                              disabled={disabled}
                              onClick={async () => {
                                const out = await send<InclusionItem>("PATCH", `inclusion-items/${x.inclusion_item_id}`, { archived: !x.archived });
                                if (out) replaced(out);
                              }}
                            >
                              {tr(x.archived ? "incl.unarchive" : "incl.archive")}
                            </Button>
                            <Button
                              size="sm"
                              variant="ghost"
                              disabled={disabled}
                              onClick={async () => {
                                if (await send("DELETE", `inclusion-items/${x.inclusion_item_id}`)) setItems(items.filter((y) => y.inclusion_item_id !== x.inclusion_item_id));
                              }}
                            >
                              {tr("incl.delete")}
                            </Button>
                          </span>
                        )}
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          </Panel>
        );
      })}
      {canEdit && (
        <Panel title={tr("incl.add")} testId="incl-library-add">
          <div style={{ display: "grid", gap: 8, gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", alignItems: "end" }}>
            <Field label={tr("incl.kind")}>
              <Select size="sm" value={kind} onChange={(e) => setKind(e.target.value as InclusionKind)} options={INCLUSION_KINDS.map((k) => ({ value: k, label: tr(`incl.kind.${k}`) }))} />
            </Field>
            <Field label={tr("incl.textEn")} hint={tr("incl.textHint")}>
              <Input size="sm" maxLength={300} value={en} onChange={(e) => setEn(e.target.value)} />
            </Field>
            <Field label={tr("incl.textVi")}>
              <Input size="sm" maxLength={300} value={vi} onChange={(e) => setVi(e.target.value)} />
            </Field>
            <div>
              <Button size="sm" variant="primary" testId="incl-library-add-button" disabled={disabled || !typed} onClick={() => void add()}>
                {tr("incl.add")}
              </Button>
            </div>
          </div>
        </Panel>
      )}
    </div>
  );
}
