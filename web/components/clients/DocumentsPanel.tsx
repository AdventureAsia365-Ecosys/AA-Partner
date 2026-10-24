"use client";
// web/components/clients/DocumentsPanel.tsx — documents of a client or a trip (AA-937, card CRM-09; design v3 §4.1
// Fix A7, WF-2 "Travellers & docs"). Upload reads the file in the browser and sends base64 JSON through the BFF; the
// API checks the type, seals it with the agency's docs key and stores it. A passport scan is write-only (never shown
// to travellers). Download asks why (one reveal event) and hands the file to the browser as a Blob, never a link.
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, K } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { DOC_KINDS, REVEAL_REASONS, fileProblem, fileSize, fromBase64, label, toBase64, type DocKind, type DocumentRow } from "@/lib/clients";
import { formatDate, type DateLocale } from "@/lib/dates";
import { type Lang, type MessageKey, t } from "@/lib/i18n";
import type { Role } from "@/lib/nav";

const field: React.CSSProperties = {
  font: "inherit",
  fontSize: 13,
  padding: "6px 8px",
  borderRadius: 8,
  border: `1px solid ${K.line2}`,
  background: K.card,
  color: K.ink,
  maxWidth: "100%",
};

export function DocumentsPanel({
  documents,
  travelerId,
  tripId,
  role,
  lang,
  locale,
}: {
  documents: DocumentRow[];
  travelerId?: string;
  tripId?: string;
  role: Role;
  lang: Lang;
  locale: DateLocale;
}) {
  const router = useRouter();
  const [kind, setKind] = useState<DocKind>(travelerId ? "passport_scan" : "ticket");
  const [visible, setVisible] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [asking, setAsking] = useState<string | null>(null);
  const [reason, setReason] = useState("");

  async function upload() {
    if (!file) return;
    const problem = fileProblem(file);
    if (problem) {
      setError(t(lang, problem === "too_big" ? "docs.tooBig" : "docs.badType"));
      return;
    }
    setBusy(true);
    setError(null);
    const content_b64 = toBase64(new Uint8Array(await file.arrayBuffer()));
    const res = await bffSend("POST", "documents", {
      traveler_id: travelerId ?? null,
      trip_id: tripId ?? null,
      kind,
      filename: file.name,
      content_type: file.type,
      content_b64,
      visible_to_travelers: kind === "passport_scan" ? false : visible,
    });
    setBusy(false);
    if (!res.ok) {
      setError(t(lang, res.detail === "content_mismatch" || res.detail === "invalid_content_type" ? "docs.badType" : "clients.error.save"));
      return;
    }
    setFile(null);
    router.refresh();
  }

  async function download(doc: DocumentRow) {
    setBusy(true);
    const res = await bffSend<{ content_b64: string; content_type: string; filename: string }>("POST", `documents/${doc.document_id}/download`, { reason });
    setBusy(false);
    setAsking(null);
    if (!res.ok) {
      setError(t(lang, "trip.error"));
      return;
    }
    const bytes = fromBase64(res.data.content_b64);
    const url = URL.createObjectURL(new Blob([bytes.buffer as ArrayBuffer], { type: res.data.content_type }));
    const a = document.createElement("a");
    a.href = url;
    a.download = res.data.filename;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 10_000);
  }

  async function act(method: "PATCH" | "DELETE", doc: DocumentRow, body?: unknown) {
    setBusy(true);
    const res = await bffSend(method, `documents/${doc.document_id}`, body);
    setBusy(false);
    if (!res.ok) setError(t(lang, "trip.error"));
    else router.refresh();
  }

  return (
    <Panel title={t(lang, "docs.title")} testId="documents">
      {documents.length === 0 ? (
        <p style={{ margin: "0 0 10px", fontSize: 13, color: K.muted }}>{t(lang, "docs.none")}</p>
      ) : (
        <ul style={{ listStyle: "none", margin: "0 0 12px", padding: 0, display: "grid", gap: 10 }}>
          {documents.map((d) => (
            <li key={d.document_id} style={{ display: "grid", gap: 4 }} data-testid="document-row">
              <span style={{ display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center", fontSize: 13.5, color: K.ink }}>
                <span style={{ overflowWrap: "anywhere" }}>{d.filename}</span>
                <Badge tone="neutral">{label(lang, "doc", d.kind)}</Badge>
                {d.write_only ? <Badge tone="warning">{t(lang, "docs.writeOnly")}</Badge> : <Badge tone={d.visible_to_travelers ? "accent" : "neutral"}>{t(lang, d.visible_to_travelers ? "docs.shown" : "docs.hidden")}</Badge>}
              </span>
              <span style={{ fontSize: 11.5, color: K.muted }}>
                {fileSize(d.size_bytes)} · {formatDate(d.created_at, locale)}
              </span>
              <span style={{ display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center" }}>
                {asking === d.document_id ? (
                  <>
                    <select aria-label={t(lang, "clients.why")} value={reason} onChange={(e) => setReason(e.target.value)} style={field}>
                      <option value="">{t(lang, "clients.why")}</option>
                      {REVEAL_REASONS.map((r) => (
                        <option key={r} value={r}>
                          {t(lang, `why.${r}` as MessageKey)}
                        </option>
                      ))}
                    </select>
                    <Button size="sm" variant="secondary" disabled={!reason || busy} onClick={() => void download(d)}>
                      {t(lang, "docs.download")}
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => setAsking(null)}>
                      {t(lang, "clients.cancel")}
                    </Button>
                  </>
                ) : (
                  <Button size="sm" variant="ghost" onClick={() => setAsking(d.document_id)}>
                    {t(lang, "docs.download")}
                  </Button>
                )}
                {!d.write_only && (
                  <Button size="sm" variant="ghost" disabled={busy} onClick={() => void act("PATCH", d, { visible_to_travelers: !d.visible_to_travelers })}>
                    {t(lang, d.visible_to_travelers ? "docs.hidden" : "docs.shown")}
                  </Button>
                )}
                {role !== "assistant" && (
                  <Button size="sm" variant="ghost" disabled={busy} onClick={() => void act("DELETE", d)}>
                    {t(lang, "docs.delete")}
                  </Button>
                )}
              </span>
            </li>
          ))}
        </ul>
      )}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void upload();
        }}
        style={{ display: "grid", gap: 8 }}
      >
        <label style={{ display: "grid", gap: 4, fontSize: 12.5, color: K.muted }}>
          {t(lang, "docs.kind")}
          <select value={kind} onChange={(e) => setKind(e.target.value as DocKind)} style={field} data-testid="document-kind">
            {DOC_KINDS.map((k) => (
              <option key={k} value={k}>
                {label(lang, "doc", k)}
              </option>
            ))}
          </select>
        </label>
        <label style={{ display: "grid", gap: 4, fontSize: 12.5, color: K.muted }}>
          {t(lang, "docs.file")}
          <input type="file" accept="application/pdf,image/jpeg,image/png,image/webp,image/heic" onChange={(e) => setFile(e.target.files?.[0] ?? null)} style={field} data-testid="document-file" />
        </label>
        {kind !== "passport_scan" && (
          <label style={{ display: "flex", gap: 6, alignItems: "center", fontSize: 12.5, color: K.body }}>
            <input type="checkbox" checked={visible} onChange={(e) => setVisible(e.target.checked)} />
            {t(lang, "docs.visible")}
          </label>
        )}
        <div>
          <Button size="sm" type="submit" variant="secondary" disabled={!file || busy}>
            {t(lang, "docs.upload")}
          </Button>
        </div>
        <p style={{ margin: 0, fontSize: 11.5, color: K.muted }}>
          {t(lang, "docs.passportNote")} {t(lang, "docs.tooBig")}
        </p>
        {error && (
          <p role="alert" style={{ margin: 0, fontSize: 12.5, color: K.danger }}>
            {error}
          </p>
        )}
      </form>
    </Panel>
  );
}
