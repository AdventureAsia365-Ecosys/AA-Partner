"use client";
// web/components/proposals/TextProposalPage.tsx — A-05, the text proposal (AA-951, card CRM-22; design F4, WF-5). One
// proposal version as e-mail or WhatsApp text, in English or Vietnamese, optionally inside a message template (R9),
// rendered by the API from one snapshot with the version's price hash (shown as its reference). Send (owner / agent)
// names the hash the agent saw: when the version changed since, the API answers 409 price_hash_mismatch (card R), the
// page says so and shows the fresh text to review before sending again. E-mail goes out send-as from the agency
// mailbox (10 s Undo); WhatsApp has no channel in the CRM, so the page hands over the final text (copy, or open
// WhatsApp with it). Undoing the e-mail that carried the only copy of a just-made link leaves the version sent with a
// link the client never got: the page says so and the next send asks the API for a new link (new_link).
import Link from "next/link";
import { useState } from "react";
import { Badge, Button, K, useHydrated } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import {
  TEXT_FORMATS,
  TEXT_LANGS,
  linkLostOnUndo,
  linkNoteKey,
  sendBody,
  textErrorKey,
  whatsappShareUrl,
  type MessageTemplate,
  type TextFormat,
  type TextLang,
  type TextPreview,
  type TextSent,
} from "@/lib/textProposal";

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

function newMessageId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : "";
}

export function TextProposalPage({
  initial,
  templates,
  role,
  lang,
}: {
  initial: TextPreview;
  templates: MessageTemplate[];
  role: string;
  lang: Lang;
}) {
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [preview, setPreview] = useState<TextPreview>(initial);
  const [format, setFormat] = useState<TextFormat>(initial.format);
  const [locale, setLocale] = useState<TextLang>(initial.locale);
  const [templateId, setTemplateId] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "danger" | "success"; text: string } | null>(null);
  const [sent, setSent] = useState<TextSent | null>(null);
  const [copied, setCopied] = useState(false);
  const [relink, setRelink] = useState(false);
  const canSend = role === "owner" || role === "agent";

  async function load(next: { format?: TextFormat; locale?: TextLang; templateId?: string }): Promise<TextPreview | null> {
    const f = next.format ?? format;
    const l = next.locale ?? locale;
    const tid = next.templateId ?? templateId;
    setFormat(f);
    setLocale(l);
    setTemplateId(tid);
    setBusy(true);
    const query = new URLSearchParams({ format: f, locale: l });
    if (tid) query.set("template_id", tid);
    const res = await bffSend<TextPreview>("GET", `proposals/${preview.proposal_id}/text?${query.toString()}`);
    setBusy(false);
    if (!res.ok) {
      setMessage({ tone: "danger", text: tr(textErrorKey(res.detail)) });
      return null;
    }
    setPreview(res.data);
    return res.data;
  }

  async function send() {
    setBusy(true);
    setMessage(null);
    setCopied(false);
    const body = sendBody({ format, locale, priceHash: preview.price_hash, templateId, messageId: newMessageId(), relink });
    const res = await bffSend<TextSent>("POST", `proposals/${preview.proposal_id}/text/send`, body);
    setBusy(false);
    if (res.ok) {
      setSent(res.data);
      setRelink(false);
      setMessage({ tone: "success", text: tr(format === "email" ? "text.sentEmail" : "text.sentWhatsapp") });
      await load({}); // the version may be sent now: the link note follows the fresh preview
      return;
    }
    setMessage({ tone: "danger", text: tr(textErrorKey(res.detail)) });
    if (res.detail === "price_hash_mismatch") await load({});
  }

  async function undo() {
    if (!sent?.message) return;
    setBusy(true);
    const res = await bffSend("POST", `messages/${sent.message.message_id}/undo`);
    setBusy(false);
    if (!res.ok) {
      setMessage({ tone: "danger", text: tr("text.undoLate") });
      return;
    }
    // The version stays sent; when this e-mail carried the only copy of its link, the next send re-issues one.
    const lost = linkLostOnUndo(sent);
    setRelink(lost);
    setSent(null);
    setMessage({ tone: "success", text: tr(lost ? "text.undoneRelink" : "text.undone") });
    await load({});
  }

  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }

  const finalText = sent?.body ?? null;

  return (
    <div style={{ display: "grid", gap: 16, minWidth: 0, maxWidth: 900 }} data-testid="text-proposal" data-hydrated={hydrated ? "true" : undefined}>
      <Panel testId="text-proposal-header">
        <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <Link href={`/pipeline/${preview.trip_id}`} style={{ fontSize: 12.5, color: K.accent }}>
            {tr("builder.backToTrip")}
          </Link>
          <strong style={{ fontSize: 15, color: K.ink }}>{fmt(tr("text.title"), { n: preview.version })}</strong>
          <Badge tone="info">{tr(`text.kind.${preview.kind}`)}</Badge>
          <span style={{ fontSize: 12.5, color: K.muted }} data-testid="text-reference">
            {fmt(tr("text.reference"), { ref: preview.reference })}
          </span>
        </div>
        <div style={{ display: "flex", gap: 16, flexWrap: "wrap", marginTop: 12, alignItems: "end" }}>
          <fieldset style={{ border: 0, margin: 0, padding: 0, display: "flex", gap: 10, flexWrap: "wrap" }} disabled={!hydrated || busy}>
            <legend style={{ fontSize: 12, padding: 0, marginBottom: 2 }}>{tr("text.format")}</legend>
            {TEXT_FORMATS.map((f) => (
              <label key={f} style={{ display: "flex", gap: 4, alignItems: "center", fontSize: 13 }}>
                <input type="radio" name="text-format" value={f} checked={format === f} onChange={() => void load({ format: f })} />
                {tr(`text.format.${f}`)}
              </label>
            ))}
          </fieldset>
          <label style={{ display: "grid", gap: 2, fontSize: 12 }}>
            {tr("text.language")}
            <select style={field} value={locale} disabled={!hydrated || busy} onChange={(e) => void load({ locale: e.currentTarget.value as TextLang })} data-testid="text-locale">
              {TEXT_LANGS.map((l) => (
                <option key={l} value={l}>
                  {l === "en" ? "English" : "Tiếng Việt"}
                </option>
              ))}
            </select>
          </label>
          <label style={{ display: "grid", gap: 2, fontSize: 12, minWidth: 0, flex: "1 1 220px" }}>
            {tr("text.template")}
            <select style={field} value={templateId} disabled={!hydrated || busy} onChange={(e) => void load({ templateId: e.currentTarget.value })} data-testid="text-template">
              <option value="">{tr("text.noTemplate")}</option>
              {templates.map((tp) => (
                <option key={tp.message_template_id} value={tp.message_template_id}>
                  {tp.title} · {tp.locale.toUpperCase()}
                </option>
              ))}
            </select>
          </label>
        </div>
        {templates.length === 0 && (
          <p style={{ margin: "8px 0 0", fontSize: 12.5, color: K.muted }}>
            <Link href="/settings/templates" style={{ color: K.accent }}>
              {tr("text.templatesLink")}
            </Link>
          </p>
        )}
      </Panel>

      <Panel title={tr("text.preview")} testId="text-preview">
        {preview.subject && (
          <p style={{ margin: "0 0 8px", fontSize: 13 }} data-testid="text-subject">
            <span style={{ color: K.muted }}>{tr("text.subject")}: </span>
            {preview.subject}
          </p>
        )}
        <pre
          tabIndex={0}
          aria-label={tr("text.preview")}
          style={{ margin: 0, whiteSpace: "pre-wrap", overflowWrap: "anywhere", font: "inherit", fontSize: 13, color: K.ink, background: K.bg, padding: 12, borderRadius: 8, maxHeight: 520, overflowY: "auto" }}
          data-testid="text-body"
        >
          {preview.body}
        </pre>
        {preview.unfilled.length > 0 && (
          <p role="note" style={{ margin: "8px 0 0", fontSize: 12.5, color: K.amber }} data-testid="text-unfilled">
            {fmt(tr("text.unfilled"), { fields: preview.unfilled.join(", ") })}
          </p>
        )}
        <p style={{ margin: "8px 0 0", fontSize: 12.5, color: K.muted }}>{tr(linkNoteKey(preview, relink))}</p>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 12 }}>
          {canSend ? (
            <span data-testid="text-send">
              <Button variant="primary" size="sm" disabled={!hydrated || busy || sent !== null} onClick={send}>
                {tr(format === "email" ? "text.sendEmail" : "text.sendWhatsapp")}
              </Button>
            </span>
          ) : (
            <span style={{ fontSize: 12.5, color: K.muted }}>{tr("text.ownerAgentSends")}</span>
          )}
        </div>
        {message && (
          <p role="status" data-testid="text-message" style={{ margin: "8px 0 0", fontSize: 13, color: message.tone === "danger" ? K.danger : K.success }}>
            {message.text}
          </p>
        )}
      </Panel>

      {sent && (
        <Panel title={tr("text.sentTitle")} testId="text-sent">
          {sent.format === "email" && sent.message ? (
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", fontSize: 13 }}>
              <span>{tr("text.queued")}</span>
              <Button size="sm" disabled={!hydrated || busy} onClick={undo}>
                {tr("text.undo")}
              </Button>
            </div>
          ) : (
            finalText && (
              <div style={{ display: "grid", gap: 8 }}>
                <pre tabIndex={0} style={{ margin: 0, whiteSpace: "pre-wrap", overflowWrap: "anywhere", font: "inherit", fontSize: 13, background: K.bg, padding: 12, borderRadius: 8, maxHeight: 360, overflowY: "auto" }} data-testid="text-final">
                  {finalText}
                </pre>
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
                  <Button size="sm" variant="primary" onClick={() => void copy(finalText)}>
                    {tr("text.copy")}
                  </Button>
                  <a href={whatsappShareUrl(finalText)} target="_blank" rel="noopener noreferrer" style={{ fontSize: 13, color: K.accent }}>
                    {tr("text.openWhatsapp")}
                  </a>
                  {copied && (
                    <span role="status" style={{ fontSize: 12.5, color: K.success }}>
                      {tr("text.copied")}
                    </span>
                  )}
                </div>
              </div>
            )
          )}
          {sent.version_sent && <p style={{ margin: "8px 0 0", fontSize: 12.5, color: K.muted }}>{tr("text.versionSent")}</p>}
        </Panel>
      )}
    </div>
  );
}
