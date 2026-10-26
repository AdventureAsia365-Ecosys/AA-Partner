"use client";
// web/components/mail/MailImport.tsx — Settings > Mailbox > Import mail history (AA-945, card CRM-17 "history import").
// The member picks an mbox (Google Takeout, Thunderbird) or CSV file of at most 25 MB; the kind comes from the file
// name and can be changed. The file goes as the raw body to /bff/mail/imports (lib/mailImport.ts uploadImport), the
// API answers 202 with the import id, and the section asks for its progress every few seconds until it is done or
// failed. There is no list endpoint: the imports of this browser tab are remembered by id (sessionStorage), so a
// reload shows them again. Every role may import its own history (OWN_MAILBOX_CONNECT).
import { useEffect, useRef, useState } from "react";
import { Button, K, StatusBadge, useHydrated } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import {
  apiMessage,
  checkFile,
  createImportPoller,
  detectKind,
  failureKey,
  formatBytes,
  importErrorKey,
  IMPORT_POLL_MS,
  isFinished,
  readRemembered,
  uploadImport,
  writeRemembered,
  type ImportKind,
  type MailImport as ImportRow,
} from "@/lib/mailImport";

const field: React.CSSProperties = {
  font: "inherit",
  fontSize: 13,
  padding: "6px 8px",
  borderRadius: 8,
  border: `1px solid ${K.line2}`,
  background: K.card,
  color: K.ink,
  boxSizing: "border-box",
  maxWidth: "100%",
};

interface Entry {
  id: string;
  kind: ImportKind;
  row: ImportRow | null;
  /** The text of the error that stopped the progress checks (a 4xx, or several failures in a row). */
  error: string | null;
  polling: boolean;
}

function storage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.sessionStorage;
  } catch {
    return null;
  }
}

const COUNTS = [
  ["total_count", "mail.import.count.total"],
  ["imported_count", "mail.import.count.imported"],
  ["duplicate_count", "mail.import.count.duplicate"],
  ["skipped_count", "mail.import.count.skipped"],
  ["failed_count", "mail.import.count.failed"],
] as const;

export function MailImport({ lang, pollMs = IMPORT_POLL_MS }: { lang: Lang; pollMs?: number }) {
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [file, setFile] = useState<File | null>(null);
  const [kind, setKind] = useState<ImportKind>("mbox");
  const [guessed, setGuessed] = useState(false);
  const [progress, setProgress] = useState<number | null>(null);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [entries, setEntries] = useState<Entry[]>([]);
  const pollers = useRef(new Map<string, ReturnType<typeof createImportPoller>>());
  const input = useRef<HTMLInputElement>(null);

  function errorText(status: number, detail: string, body?: unknown): string {
    const key = importErrorKey(status, detail);
    if (key) return tr(key);
    const fromApi = status >= 400 && status < 500 ? apiMessage(body) : null;
    return fromApi ?? tr("mail.error.other");
  }

  function patch(id: string, change: Partial<Entry>) {
    setEntries((list) => list.map((e) => (e.id === id ? { ...e, ...change } : e)));
  }

  function watch(id: string) {
    if (pollers.current.has(id)) return;
    const poller = createImportPoller(
      id,
      (importId) => bffSend<ImportRow>("GET", `mail/imports/${importId}`),
      {
        update: (row) => patch(id, { row, polling: !isFinished(row.status), error: null }),
        error: (status, detail, body) => patch(id, { polling: false, error: errorText(status, detail, body) }),
      },
      pollMs,
    );
    pollers.current.set(id, poller);
    poller.start();
  }

  useEffect(() => {
    const remembered = readRemembered(storage());
    setEntries(remembered.map((r) => ({ id: r.id, kind: r.kind, row: null, error: null, polling: true })));
    for (const r of remembered) watch(r.id);
    const live = pollers.current;
    return () => {
      for (const p of live.values()) p.stop();
      live.clear();
    };
  }, []);

  function pick(next: File | null) {
    setMessage(null);
    setFile(next);
    if (!next) return;
    const detected = detectKind(next.name);
    setKind(detected ?? "mbox");
    setGuessed(detected === null);
    const check = checkFile(next);
    if (check !== "ok") setMessage({ ok: false, text: tr(check === "empty" ? "mail.error.empty_upload" : "mail.error.import_too_large") });
  }

  async function start() {
    if (!file) return;
    const check = checkFile(file);
    if (check !== "ok") {
      setMessage({ ok: false, text: tr(check === "empty" ? "mail.error.empty_upload" : "mail.error.import_too_large") });
      return;
    }
    setMessage(null);
    setProgress(0);
    const res = await uploadImport(file, kind, setProgress);
    setProgress(null);
    if (!res.ok) {
      setMessage({ ok: false, text: errorText(res.status, res.detail, res.body) });
      return;
    }
    const id = res.data.mail_import_id;
    const next: Entry = { id, kind, row: null, error: null, polling: true };
    setEntries((list) => [next, ...list.filter((e) => e.id !== id)]);
    const remembered = readRemembered(storage()).filter((r) => r.id !== id);
    writeRemembered(storage(), [{ id, kind }, ...remembered]);
    setMessage({ ok: true, text: tr("mail.import.started") });
    setFile(null);
    setGuessed(false);
    if (input.current) input.current.value = "";
    watch(id);
  }

  const uploading = progress !== null;
  const blocked = !file || checkFile(file) !== "ok";
  return (
    <Panel title={tr("mail.import.title")} testId="mail-import">
      <div style={{ display: "grid", gap: 10, fontSize: 13 }}>
        <p style={{ margin: 0, color: K.muted }}>{tr("mail.import.help")}</p>
        <ul style={{ margin: 0, paddingLeft: 18, color: K.muted, display: "grid", gap: 2 }}>
          <li>{tr("mail.import.helpMbox")}</li>
          <li>{tr("mail.import.helpCsv")}</li>
          <li>{tr("mail.import.helpLimits")}</li>
        </ul>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "end" }}>
          <label style={{ display: "grid", gap: 2, fontSize: 12.5, minWidth: 0, flex: "1 1 220px" }}>
            {tr("mail.import.file")}
            <input
              ref={input}
              type="file"
              accept=".mbox,.mbx,.csv,text/csv,application/mbox"
              data-testid="mail-import-file"
              disabled={!hydrated || uploading}
              onChange={(e) => pick(e.currentTarget.files?.[0] ?? null)}
              style={{ ...field, width: "100%" }}
            />
          </label>
          <label style={{ display: "grid", gap: 2, fontSize: 12.5, minWidth: 0, flex: "0 1 240px" }}>
            {tr("mail.import.kind")}
            <select
              data-testid="mail-import-kind"
              value={kind}
              disabled={!hydrated || uploading}
              onChange={(e) => {
                setKind(e.currentTarget.value === "csv" ? "csv" : "mbox");
                setGuessed(false);
              }}
              style={field}
            >
              <option value="mbox">{tr("mail.import.kind.mbox")}</option>
              <option value="csv">{tr("mail.import.kind.csv")}</option>
            </select>
          </label>
          <Button size="sm" variant="primary" disabled={!hydrated || uploading || blocked} onClick={() => void start()}>
            <span data-testid="mail-import-start">{tr("mail.import.start")}</span>
          </Button>
        </div>
        {file && (
          <p style={{ margin: 0, color: K.muted, overflowWrap: "anywhere" }} data-testid="mail-import-selected">
            {fmt(tr("mail.import.selected"), { name: file.name, size: formatBytes(file.size) })}
          </p>
        )}
        {file && guessed && (
          <p style={{ margin: 0, color: K.amber }} data-testid="mail-import-guess">
            {tr("mail.import.kindGuess")}
          </p>
        )}
        {uploading && (
          <div style={{ display: "grid", gap: 4 }} data-testid="mail-import-progress">
            <span>{fmt(tr("mail.import.uploading"), { pct: Math.round((progress ?? 0) * 100) })}</span>
            <div
              role="progressbar"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={Math.round((progress ?? 0) * 100)}
              style={{ height: 6, borderRadius: 3, background: K.line, overflow: "hidden" }}
            >
              <div style={{ width: `${Math.round((progress ?? 0) * 100)}%`, height: "100%", background: K.accent }} />
            </div>
          </div>
        )}
        {message && (
          <p
            role={message.ok ? "status" : "alert"}
            style={{ margin: 0, color: message.ok ? K.ink : K.danger }}
            data-testid="mail-import-message"
          >
            {message.text}
          </p>
        )}
        <h3 style={{ margin: "6px 0 0", fontSize: 12.5, fontWeight: 600, color: K.ink }}>{tr("mail.import.recent")}</h3>
        {entries.length === 0 ? (
          <p style={{ margin: 0, color: K.muted }} data-testid="mail-import-empty">
            {tr("mail.import.none")}
          </p>
        ) : (
          <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 10 }}>
            {entries.map((e) => (
              <ImportItem key={e.id} entry={e} tr={tr} />
            ))}
          </ul>
        )}
      </div>
    </Panel>
  );
}

function ImportItem({ entry, tr }: { entry: Entry; tr: (key: string) => string }) {
  const row = entry.row;
  return (
    <li
      data-testid={`mail-import-row-${entry.id}`}
      data-status={row?.status ?? ""}
      style={{ border: `1px solid ${K.line}`, borderRadius: 8, padding: "10px 12px", display: "grid", gap: 8, minWidth: 0 }}
    >
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center", justifyContent: "space-between" }}>
        <span style={{ color: K.muted }}>
          {row
            ? `${tr(`mail.import.kind.${row.source_kind}`)} · ${formatBytes(row.size_bytes)}`
            : tr(`mail.import.kind.${entry.kind}`)}
        </span>
        {row ? (
          <span data-testid={`mail-import-status-${entry.id}`}>
            <StatusBadge status={row.status} label={tr(`mail.import.status.${row.status}`)} />
          </span>
        ) : (
          entry.polling && <span style={{ color: K.muted }}>{tr("mail.import.checking")}</span>
        )}
      </div>
      {row && (
        <dl
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(110px, 1fr))",
            gap: "6px 12px",
            margin: 0,
          }}
        >
          {COUNTS.map(([name, label]) => (
            <div key={name} style={{ minWidth: 0 }}>
              <dt style={{ color: K.muted, fontSize: 12 }}>{tr(label)}</dt>
              <dd style={{ margin: 0, fontWeight: 600, color: K.ink }} data-testid={`mail-import-${name}-${entry.id}`}>
                {row[name]}
              </dd>
            </div>
          ))}
        </dl>
      )}
      {row?.status === "failed" && (
        <p role="alert" style={{ margin: 0, color: K.danger }} data-testid={`mail-import-failure-${entry.id}`}>
          {tr(failureKey(row.error_code))}
        </p>
      )}
      {entry.error && (
        <p role="alert" style={{ margin: 0, color: K.danger }} data-testid={`mail-import-error-${entry.id}`}>
          {entry.error} {row && !isFinished(row.status) ? tr("mail.import.stopped") : ""}
        </p>
      )}
    </li>
  );
}
