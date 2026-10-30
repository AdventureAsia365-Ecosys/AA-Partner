"use client";
// web/components/settings/DataPortabilityView.tsx — Settings > Data and privacy, owner only (AA-967, card CRM-35,
// Module 18): "Export your data" (a whole-agency ZIP: JSON + CSV per table, documents, manifest, SHA-256 checksums;
// progress while it builds; download through a single-use link; the checksum to compare) and "Data requests from your
// clients" (the agency's log: log a request, export one client's data, check then erase a client with the receipt,
// extend, close with an outcome). Writes go through the BFF allow-list (lib/bff.ts); the API decides who may.
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, Field, Input, K, Select } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import {
  addDays,
  erasureLines,
  erasureRefusal,
  exportFileName,
  formatBytes,
  isOpenRequest,
  OPEN_EXPORT,
  outcomesFor,
  progressPercent,
  shortDigest,
  statusTone,
  type DataExport,
  type DataRequest,
  type ErasureAnswer,
  type ExportLink,
  type ExportsView,
  type RequestsView,
} from "@/lib/portability";

const row: React.CSSProperties = { display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", fontSize: 13 };
const muted: React.CSSProperties = { color: K.muted, fontSize: 13 };
const item: React.CSSProperties = { display: "grid", gap: 6, padding: "10px 0", borderTop: `1px solid ${K.line}` };

export function DataPortabilityView({
  exportsView,
  requestsView,
  lang,
  locale,
}: {
  exportsView: ExportsView | null;
  requestsView: RequestsView | null;
  lang: Lang;
  locale: DateLocale;
}) {
  const router = useRouter();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function send<T>(method: "POST", path: string, body?: unknown) {
    setError(null);
    setBusy(true);
    const res = await bffSend<T>(method, path, body ?? {});
    setBusy(false);
    if (!res.ok) {
      const known = erasureRefusal(res.detail);
      setError(
        known
          ? t(lang, known)
          : res.detail === "export_running"
            ? t(lang, "data.exp.running")
            : res.detail === "deletion_dry_run"
              ? t(lang, "data.dryRun")
              : t(lang, "data.error"),
      );
      return null;
    }
    return res.data;
  }

  async function download(e: DataExport) {
    const link = await send<ExportLink>("POST", `data/exports/${e.export_id}/link`);
    if (!link) return;
    try {
      const res = await fetch(`/bff/${link.path}`, { credentials: "same-origin" });
      if (!res.ok) throw new Error(String(res.status));
      const url = URL.createObjectURL(await res.blob());
      const a = document.createElement("a");
      a.href = url;
      a.download = exportFileName(e);
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      router.refresh();
    } catch {
      setError(t(lang, "data.error"));
    }
  }

  const exportsList = exportsView?.exports ?? [];
  const building = exportsList.some((e) => OPEN_EXPORT.includes(e.status));

  return (
    <div style={{ display: "grid", gap: 14 }}>
      {error && (
        <p role="alert" style={{ margin: 0, fontSize: 13, color: K.danger }}>
          {error}
        </p>
      )}

      {exportsView && (
        <Panel title={t(lang, "data.exp.title")} testId="data-exports">
          <p style={{ ...muted, margin: "0 0 10px" }}>
            {fmt(t(lang, "data.exp.help"), { minutes: exportsView.link_minutes, days: exportsView.file_days })}
          </p>
          <div style={row}>
            <Button variant="primary" disabled={busy} onClick={async () => (await send("POST", "data/exports", { scope: "tenant" })) && router.refresh()}>
              {t(lang, "data.exp.agency")}
            </Button>
            {building && (
              <Button size="sm" variant="ghost" onClick={() => router.refresh()}>
                {t(lang, "data.exp.refresh")}
              </Button>
            )}
          </div>
          {exportsList.length === 0 ? (
            <p style={{ ...muted, margin: "10px 0 0" }}>{t(lang, "data.exp.none")}</p>
          ) : (
            <ul style={{ listStyle: "none", padding: 0, margin: "10px 0 0" }}>
              {exportsList.map((e) => (
                <li key={e.export_id} data-testid="data-export" style={item}>
                  <div style={row}>
                    <Badge tone={statusTone(e.status)}>{tr(`data.exp.status.${e.status}`)}</Badge>
                    <span style={{ color: K.ink }}>{tr(`data.exp.scope.${e.scope}`)}</span>
                    <span style={muted}>{formatDate(e.created_at, locale, { year: true })}</span>
                    {e.status === "ready" && (
                      <Button size="sm" disabled={busy} onClick={() => download(e)}>
                        {t(lang, "data.exp.download")}
                      </Button>
                    )}
                  </div>
                  {OPEN_EXPORT.includes(e.status) && (
                    <div style={{ display: "grid", gap: 4, maxWidth: 420 }}>
                      <div
                        role="progressbar"
                        aria-valuemin={0}
                        aria-valuemax={100}
                        aria-valuenow={progressPercent(e)}
                        style={{ height: 6, borderRadius: 3, background: K.line, overflow: "hidden" }}
                      >
                        <div style={{ width: `${progressPercent(e)}%`, height: "100%", background: K.accent }} />
                      </div>
                      <span style={muted}>{fmt(t(lang, "data.exp.progress"), { done: e.progress.done, total: e.progress.total })}</span>
                    </div>
                  )}
                  {e.status === "ready" && (
                    <>
                      <span style={muted}>
                        {fmt(t(lang, "data.exp.summary"), {
                          rows: e.rows,
                          tables: e.tables,
                          docs: e.documents_included,
                          size: formatBytes(e.size_bytes),
                        })}
                      </span>
                      {e.documents_skipped > 0 && <span style={muted}>{fmt(t(lang, "data.exp.skipped"), { n: e.documents_skipped })}</span>}
                      <span style={muted} title={e.content_sha256 ?? undefined}>
                        {t(lang, "data.exp.checksum")} <code style={{ fontSize: 12 }}>{shortDigest(e.content_sha256)}…</code>
                      </span>
                      <span style={muted}>
                        {e.expires_at && fmt(t(lang, "data.exp.expires"), { date: formatDate(e.expires_at, locale, { year: true }) })}
                        {e.downloaded_at && ` · ${fmt(t(lang, "data.exp.downloaded"), { date: formatDate(e.downloaded_at, locale, { year: true }) })}`}
                      </span>
                    </>
                  )}
                  {e.status === "failed" && (
                    <span style={{ fontSize: 13, color: K.danger }}>{fmt(t(lang, "data.exp.failed"), { code: e.failure_class ?? "error" })}</span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </Panel>
      )}

      {requestsView && (
        <DataRequestsPanel view={requestsView} lang={lang} locale={locale} busy={busy} send={send} onDone={() => router.refresh()} />
      )}
    </div>
  );
}

function DataRequestsPanel({
  view,
  lang,
  locale,
  busy,
  send,
  onDone,
}: {
  view: RequestsView;
  lang: Lang;
  locale: DateLocale;
  busy: boolean;
  send: <T>(method: "POST", path: string, body?: unknown) => Promise<T | null>;
  onDone: () => void;
}) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const [kind, setKind] = useState(view.kinds[0] ?? "access");
  const [channel, setChannel] = useState(view.channels[0] ?? "email");
  const [received, setReceived] = useState(view.today);
  const [due, setDue] = useState(addDays(view.today, view.default_due_days));
  const [client, setClient] = useState("");

  return (
    <Panel title={t(lang, "data.req.title")} testId="data-requests">
      <p style={{ ...muted, margin: "0 0 10px" }}>{t(lang, "data.req.help")}</p>
      {view.requests.length === 0 ? (
        <p style={{ ...muted, margin: "0 0 10px" }}>{t(lang, "data.req.none")}</p>
      ) : (
        <ul style={{ listStyle: "none", padding: 0, margin: "0 0 10px" }}>
          {view.requests.map((r) => (
            <RequestItem key={r.data_request_id} r={r} view={view} lang={lang} locale={locale} busy={busy} send={send} onDone={onDone} />
          ))}
        </ul>
      )}
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          const ok = await send("POST", "data/requests", {
            kind,
            channel,
            received_on: received,
            due_on: due || null,
            traveler_id: client.trim() || null,
          });
          if (ok) {
            setClient("");
            onDone();
          }
        }}
        style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end" }}
      >
        <Field label={t(lang, "data.req.kind")}>
          <Select value={kind} onChange={(e) => setKind(e.target.value)} options={view.kinds.map((k) => ({ value: k, label: tr(`data.req.kind.${k}`) }))} />
        </Field>
        <Field label={t(lang, "data.req.channel")}>
          <Select
            value={channel}
            onChange={(e) => setChannel(e.target.value)}
            options={view.channels.map((c) => ({ value: c, label: tr(`data.req.channel.${c}`) }))}
          />
        </Field>
        <Field label={t(lang, "data.req.received")}>
          <Input
            type="date"
            value={received}
            max={view.today}
            onChange={(e) => {
              setReceived(e.target.value);
              setDue(addDays(e.target.value, view.default_due_days));
            }}
          />
        </Field>
        <Field label={t(lang, "data.req.due")}>
          <Input type="date" value={due} min={received} max={addDays(received, view.max_due_days)} onChange={(e) => setDue(e.target.value)} />
        </Field>
        <Field label={t(lang, "data.req.client")}>
          <Input value={client} onChange={(e) => setClient(e.target.value)} style={{ minWidth: 280 }} autoComplete="off" />
        </Field>
        <Button type="submit" disabled={busy || !received}>
          {t(lang, "data.req.add")}
        </Button>
      </form>
    </Panel>
  );
}

function RequestItem({
  r,
  view,
  lang,
  locale,
  busy,
  send,
  onDone,
}: {
  r: DataRequest;
  view: RequestsView;
  lang: Lang;
  locale: DateLocale;
  busy: boolean;
  send: <T>(method: "POST", path: string, body?: unknown) => Promise<T | null>;
  onDone: () => void;
}) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const closing = (["done", "refused", "withdrawn"] as const).flatMap((s) => outcomesFor(view, s).map((o) => ({ value: `${s}:${o}`, label: `${tr(`data.req.status.${s}`)} · ${tr(`data.req.outcome.${o}`)}` })));
  const [close, setClose] = useState(closing[0]?.value ?? "");
  const [extendTo, setExtendTo] = useState(r.max_due_on);
  const [preview, setPreview] = useState<ErasureAnswer | null>(null);
  const [erased, setErased] = useState<ErasureAnswer | null>(null);
  const open = isOpenRequest(r);
  const canExport = open && r.traveler_id && (r.kind === "access" || r.kind === "portability");
  const canErase = open && r.traveler_id && r.kind === "erasure";

  return (
    <li data-testid="data-request" style={item}>
      <div style={row}>
        <Badge tone={statusTone(r.status, r.overdue)}>{r.overdue ? t(lang, "data.req.overdue") : tr(`data.req.status.${r.status}`)}</Badge>
        <span style={{ color: K.ink }}>{tr(`data.req.kind.${r.kind}`)}</span>
        <span style={muted}>{tr(`data.req.channel.${r.channel}`)}</span>
        <span style={muted}>
          {formatDate(r.received_on, locale, { year: true })} → {formatDate(r.due_on, locale, { year: true })}
        </span>
        {r.traveler_id && (
          <a href={`/clients/${r.traveler_id}`} style={{ fontSize: 12 }}>
            <code>{r.traveler_id.slice(0, 8)}</code>
          </a>
        )}
        {r.outcome && <span style={muted}>{tr(`data.req.outcome.${r.outcome}`)}</span>}
      </div>
      {open && (
        <div style={row}>
          {canExport && (
            <Button
              size="sm"
              disabled={busy}
              onClick={async () =>
                (await send("POST", "data/exports", { scope: "traveler", traveler_id: r.traveler_id, data_request_id: r.data_request_id })) && onDone()
              }
            >
              {t(lang, "data.req.exportClient")}
            </Button>
          )}
          {canErase && !preview && (
            <Button size="sm" disabled={busy} onClick={async () => setPreview(await send<ErasureAnswer>("POST", `travelers/${r.traveler_id}/erasure`, { dry_run: true }))}>
              {t(lang, "data.req.checkErase")}
            </Button>
          )}
          <Input type="date" size="sm" value={extendTo} min={r.due_on} max={r.max_due_on} onChange={(e) => setExtendTo(e.target.value)} aria-label={t(lang, "data.req.extend")} />
          <Button size="sm" variant="ghost" disabled={busy || extendTo <= r.due_on} onClick={async () => (await send("POST", `data/requests/${r.data_request_id}/extend`, { due_on: extendTo })) && onDone()}>
            {t(lang, "data.req.extend")}
          </Button>
          <Select size="sm" value={close} onChange={(e) => setClose(e.target.value)} options={closing} aria-label={t(lang, "data.req.close")} />
          <Button
            size="sm"
            variant="ghost"
            disabled={busy || !close}
            onClick={async () => {
              const [status, outcome] = close.split(":");
              if (await send("POST", `data/requests/${r.data_request_id}/close`, { status, outcome })) onDone();
            }}
          >
            {t(lang, "data.req.close")}
          </Button>
        </div>
      )}
      {preview && !erased && (
        <div data-testid="data-erasure-preview" style={{ display: "grid", gap: 6, maxWidth: 640 }}>
          {erasureLines(preview, lang).map((line) => (
            <span key={line} style={{ fontSize: 13 }}>
              {line}
            </span>
          ))}
          <div style={row}>
            <Button
              size="sm"
              variant="danger"
              disabled={busy}
              onClick={async () => {
                const done = await send<ErasureAnswer>("POST", `travelers/${r.traveler_id}/erasure`, {
                  dry_run: false,
                  data_request_id: r.data_request_id,
                });
                if (done) setErased(done);
              }}
            >
              {t(lang, "data.era.confirm")}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setPreview(null)}>
              {t(lang, "data.off.cancel")}
            </Button>
          </div>
        </div>
      )}
      {erased && (
        <p data-testid="data-erasure-done" style={{ margin: 0, fontSize: 13 }}>
          {fmt(t(lang, "data.era.done"), {
            id: (erased.request_id ?? "").slice(0, 8),
            date: formatDate(erased.backup_purge_by ?? null, locale, { year: true }),
          })}
        </p>
      )}
    </li>
  );
}
