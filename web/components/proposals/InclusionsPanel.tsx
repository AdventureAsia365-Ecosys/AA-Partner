"use client";
// web/components/proposals/InclusionsPanel.tsx — the builder's "Included and not included" lists (AA-1070 item crm-35,
// crm-059). Two lists, each with the version's own lines (picked from the agency library or typed for this proposal)
// that the agent orders and removes, then saves in one builder write (PUT /proposals/{id}/inclusions with the version
// counter); below them each AA tour's fixed lines (the CIS tour content, read-only). A published version shows its
// frozen lists read only. Kit components only (ADR 0016).
import Link from "next/link";
import { useState } from "react";
import { Badge, Button, Field, Input, K, Select, useHydrated } from "@/app/_kit";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import {
  INCLUSION_KINDS,
  INCLUSIONS_SETTINGS_PATH,
  draftLines,
  linesBody,
  moveAt,
  ofKind,
  pickItem,
  removeAt,
  sameLines,
  typedLine,
  type DraftLine,
  type InclusionKind,
  type InclusionsPanelData,
} from "@/lib/inclusions";

export function InclusionsPanel({
  data,
  draft,
  busy,
  lang,
  onSave,
}: {
  data: InclusionsPanelData;
  draft: boolean;
  busy: boolean;
  lang: Lang;
  onSave: (lines: Record<string, string>[]) => Promise<boolean>;
}) {
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const saved = draftLines(data.lines);
  const [lines, setLines] = useState<DraftLine[]>(saved);
  const [kind, setKind] = useState<InclusionKind>("included");
  const [en, setEn] = useState("");
  const [vi, setVi] = useState("");
  const dirty = !sameLines(lines, saved);
  const typed = typedLine(kind, en, vi);
  const disabled = !draft || !hydrated || busy;
  const full = lines.length >= data.max_lines;

  return (
    <div style={{ display: "grid", gap: 12 }}>
      <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr(draft ? "incl.panelHint" : "incl.panelPublished")}</p>
      <div style={{ display: "grid", gap: 12, gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))" }}>
        {INCLUSION_KINDS.map((k) => {
          const own = ofKind(lines, k);
          const offered = data.library.filter((i) => i.kind === k && !lines.some((l) => l.inclusion_item_id === i.inclusion_item_id));
          return (
            <section key={k} data-testid={`incl-list-${k}`} style={{ display: "grid", gap: 6, alignContent: "start", minWidth: 0 }}>
              <h3 style={{ margin: 0, fontSize: 13.5, color: K.ink }}>{tr(`incl.kind.${k}`)}</h3>
              {own.length === 0 && <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("incl.empty")}</p>}
              <ol style={{ margin: 0, padding: 0, listStyle: "none", display: "grid", gap: 4 }}>
                {own.map((l, i) => (
                  <li key={`${l.inclusion_item_id ?? "typed"}-${l.text_en}`} data-testid="incl-line" style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap", fontSize: 13, borderTop: `1px solid ${K.line}`, paddingTop: 4 }}>
                    <span style={{ flex: "1 1 160px", overflowWrap: "anywhere", minWidth: 0 }}>
                      {l.text_en}
                      {l.text_vi && <span style={{ color: K.muted }}> · {l.text_vi}</span>}
                    </span>
                    {!l.inclusion_item_id && <Badge tone="info">{tr("incl.typed")}</Badge>}
                    {draft && (
                      <span style={{ display: "inline-flex", gap: 4 }}>
                        <Button size="sm" variant="ghost" ariaLabel={tr("incl.up")} disabled={disabled || i === 0} onClick={() => setLines(moveAt(lines, k, i, -1))}>
                          ↑
                        </Button>
                        <Button size="sm" variant="ghost" ariaLabel={tr("incl.down")} disabled={disabled || i === own.length - 1} onClick={() => setLines(moveAt(lines, k, i, 1))}>
                          ↓
                        </Button>
                        <Button size="sm" variant="ghost" disabled={disabled} onClick={() => setLines(removeAt(lines, k, i))}>
                          {tr("incl.remove")}
                        </Button>
                      </span>
                    )}
                  </li>
                ))}
              </ol>
              {draft && offered.length > 0 && (
                <div style={{ display: "grid", gap: 4 }} data-testid={`incl-offered-${k}`}>
                  <span style={{ fontSize: 12, color: K.muted }}>{tr("incl.pick")}</span>
                  <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                    {offered.map((item) => (
                      <Button key={item.inclusion_item_id} size="sm" disabled={disabled || full} onClick={() => setLines(pickItem(lines, item))}>
                        + {item.text_en}
                      </Button>
                    ))}
                  </div>
                </div>
              )}
              {data.aa_tours
                .filter((tour) => tour[k].length > 0)
                .map((tour) => (
                  <div key={tour.source_id} data-testid="incl-aa-tour" style={{ display: "grid", gap: 2, padding: "6px 8px", borderRadius: 8, background: K.bg }}>
                    <span style={{ fontSize: 12, color: K.muted, overflowWrap: "anywhere" }}>{fmt(tr("incl.aaTour"), { tour: tour.title })}</span>
                    <ul style={{ margin: 0, paddingLeft: 16, fontSize: 12.5, color: K.body }}>
                      {tour[k].map((line) => (
                        <li key={line} style={{ overflowWrap: "anywhere" }}>
                          {line}
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
            </section>
          );
        })}
      </div>
      {draft && (
        <div style={{ display: "grid", gap: 8, gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", alignItems: "end" }} data-testid="incl-typed-form">
          <Field label={tr("incl.kind")}>
            <Select size="sm" value={kind} onChange={(e) => setKind(e.target.value as InclusionKind)} options={INCLUSION_KINDS.map((k) => ({ value: k, label: tr(`incl.kind.${k}`) }))} />
          </Field>
          <Field label={tr("incl.textEn")}>
            <Input size="sm" maxLength={300} value={en} onChange={(e) => setEn(e.target.value)} />
          </Field>
          <Field label={tr("incl.textVi")}>
            <Input size="sm" maxLength={300} value={vi} onChange={(e) => setVi(e.target.value)} />
          </Field>
          <div>
            <Button
              size="sm"
              disabled={disabled || !typed || full}
              onClick={() => {
                if (!typed) return;
                setLines([...lines, typed]);
                setEn("");
                setVi("");
              }}
            >
              {tr("incl.addTyped")}
            </Button>
          </div>
        </div>
      )}
      {draft && (
        <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <span data-testid="incl-save">
            <Button size="sm" variant="primary" disabled={disabled || !dirty} onClick={() => void onSave(linesBody(lines))}>
              {tr("incl.save")}
            </Button>
          </span>
          <span style={{ fontSize: 12, color: K.muted }}>{fmt(tr("incl.maxLines"), { n: data.max_lines })}</span>
          {data.library.length === 0 && <span style={{ fontSize: 12, color: K.muted }}>{tr("incl.pickNone")}</span>}
          <Link href={INCLUSIONS_SETTINGS_PATH} style={{ fontSize: 12.5, color: K.accent }}>
            {tr("incl.libraryLink")}
          </Link>
        </div>
      )}
    </div>
  );
}
