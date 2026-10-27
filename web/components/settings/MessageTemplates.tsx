"use client";
// web/components/settings/MessageTemplates.tsx — Settings > Message templates (AA-951, card CRM-22, R9): the agency's
// library (one language each; merge fields such as {{traveller_first_name}} and {{proposal}}), the AA starter
// templates CIS publishes that the agency has not added yet ("Add AA starter templates" copies each once, so the
// agency's edits are never overwritten), and for the owner create / edit / delete (the API checks the role). Every
// member uses them on the text proposal page (A-05).
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, K, useHydrated } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import type { Role } from "@/lib/nav";
import { textErrorKey, type MessageTemplate, type TemplateLibrary } from "@/lib/textProposal";

const field: React.CSSProperties = {
  font: "inherit",
  fontSize: 13,
  padding: "6px 8px",
  borderRadius: 8,
  border: `1px solid ${K.line2}`,
  background: K.card,
  color: K.ink,
  boxSizing: "border-box",
  minWidth: 0,
  maxWidth: "100%",
};
const label: React.CSSProperties = { display: "grid", gap: 2, fontSize: 12.5 };

function TemplateForm({ lang, initial, onSubmit, onCancel }: { lang: Lang; initial?: MessageTemplate; onSubmit: (body: Record<string, string>) => void; onCancel?: () => void }) {
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  return (
    <form
      action={(data) =>
        onSubmit({
          locale: String(data.get("locale") ?? "en"),
          title: String(data.get("title") ?? "").trim(),
          body: String(data.get("body") ?? ""),
        })
      }
      style={{ display: "grid", gap: 8, maxWidth: 640 }}
    >
      <label style={label}>
        {tr("templates.name")}
        <input name="title" required maxLength={120} style={field} defaultValue={initial?.title ?? ""} />
      </label>
      <label style={label}>
        {tr("templates.text")}
        <textarea name="body" required maxLength={20000} style={{ ...field, minHeight: 140, width: "100%", resize: "vertical" }} defaultValue={initial?.body ?? ""} />
      </label>
      <p style={{ margin: 0, fontSize: 12, color: K.muted }}>{tr("templates.fieldsHint")}</p>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "end" }}>
        <label style={label}>
          {tr("templates.locale")}
          <select name="locale" defaultValue={initial?.locale ?? lang} style={field}>
            <option value="en">English</option>
            <option value="vi">Tiếng Việt</option>
          </select>
        </label>
        <Button type="submit" size="sm" variant="primary" disabled={!hydrated}>
          {tr("templates.save")}
        </Button>
        {onCancel && (
          <Button size="sm" variant="ghost" onClick={onCancel}>
            {tr("templates.cancel")}
          </Button>
        )}
      </div>
    </form>
  );
}

export function MessageTemplates({ library, role, lang }: { library: TemplateLibrary; role: Role; lang: Lang }) {
  const router = useRouter();
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [notice, setNotice] = useState<{ tone: "danger" | "success"; text: string } | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const owner = role === "owner";

  async function run(method: "POST" | "PATCH" | "DELETE", path: string, body?: unknown, ok?: string) {
    setBusy(true);
    const res = await bffSend<{ added?: number }>(method, path, body);
    setBusy(false);
    if (!res.ok) {
      setNotice({ tone: "danger", text: tr(textErrorKey(res.detail)) });
      return;
    }
    setNotice(ok ? { tone: "success", text: ok.replace("{n}", String(res.data.added ?? "")) } : null);
    setEditing(null);
    router.refresh();
  }

  return (
    <div style={{ display: "grid", gap: 14 }} data-testid="message-templates">
      {notice && (
        <p role="status" style={{ margin: 0, fontSize: 13, color: notice.tone === "danger" ? K.danger : K.success }}>
          {notice.text}
        </p>
      )}
      {library.aa_starters.length > 0 && (
        <Panel title={tr("templates.aaTitle")} testId="templates-aa">
          <p style={{ margin: "0 0 8px", fontSize: 13, color: K.muted }}>{fmt(tr("templates.aaHint"), { n: library.aa_starters.length })}</p>
          <ul style={{ margin: "0 0 8px", paddingLeft: 18, fontSize: 13 }}>
            {library.aa_starters.map((s) => (
              <li key={`${s.group_key}:${s.locale}`}>
                {s.title} · {s.locale.toUpperCase()}
              </li>
            ))}
          </ul>
          {owner ? (
            <span data-testid="templates-import">
              <Button size="sm" variant="primary" disabled={!hydrated || busy} onClick={() => run("POST", "message-templates/import-aa", undefined, tr("templates.imported"))}>
                {tr("templates.import")}
              </Button>
            </span>
          ) : (
            <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("templates.ownerOnly")}</p>
          )}
        </Panel>
      )}
      <Panel title={tr("templates.title")} testId="templates-list">
        {library.templates.length === 0 ? (
          <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{tr("templates.empty")}</p>
        ) : (
          <ul style={{ margin: 0, padding: 0, display: "grid", gap: 12 }}>
            {library.templates.map((tp) => (
              <li key={tp.message_template_id} style={{ listStyle: "none", display: "grid", gap: 4, fontSize: 13 }} data-testid="template-row">
                <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                  <strong style={{ overflowWrap: "anywhere" }}>{tp.title}</strong>
                  <span style={{ color: K.muted }}>{tp.locale.toUpperCase()}</span>
                  {tp.source === "aa_starter" && <Badge tone="info">{tr("templates.fromAa")}</Badge>}
                  {owner && editing !== tp.message_template_id && (
                    <>
                      <Button size="sm" variant="ghost" disabled={!hydrated || busy} onClick={() => setEditing(tp.message_template_id)}>
                        {tr("templates.edit")}
                      </Button>
                      <Button size="sm" variant="ghost" disabled={!hydrated || busy} onClick={() => run("DELETE", `message-templates/${tp.message_template_id}`)}>
                        {tr("templates.delete")}
                      </Button>
                    </>
                  )}
                </div>
                {editing === tp.message_template_id ? (
                  <TemplateForm lang={lang} initial={tp} onSubmit={(body) => void run("PATCH", `message-templates/${tp.message_template_id}`, body)} onCancel={() => setEditing(null)} />
                ) : (
                  <div style={{ whiteSpace: "pre-wrap", color: K.muted, overflowWrap: "anywhere" }}>{tp.body}</div>
                )}
              </li>
            ))}
          </ul>
        )}
      </Panel>
      {owner && (
        <Panel title={tr("templates.new")} testId="templates-new">
          <TemplateForm lang={lang} onSubmit={(body) => void run("POST", "message-templates", body)} />
        </Panel>
      )}
    </div>
  );
}
