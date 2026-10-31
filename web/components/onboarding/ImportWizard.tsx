"use client";
// web/components/onboarding/ImportWizard.tsx — Settings > Import clients (AA-962, card CRM-31 "phased CSV import with
// dry run"; design v3 §2.7: clients -> relationships -> trips -> notes). Per phase: the columns and a template, the
// file (CSV or .xlsx, <= 5 MB, checked before any byte is sent), Preview (the API's dry run: counts per outcome and the
// rows to check, codes only), Import (the same file, its SHA-256 pinned by the preview; a crm_client_import job), the
// progress until done, and the agency's recent imports. A missing column is the API's 422 before any write (card R).
import type { ColumnDef } from "@tanstack/react-table";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { Badge, Button, DataTable, Field, Input, K, Tabs } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import {
  checkImportFile,
  COLUMNS,
  ENTITIES,
  IMPORT_POLL_MS,
  importErrorKey,
  importQuery,
  isFinished,
  missingColumns,
  normaliseRegion,
  OUTCOMES,
  templateCsv,
  uploadRaw,
  type Entity,
  type ImportStatus,
  type Issue,
  type PreviewReport,
} from "@/lib/onboarding";

export function ImportWizard({
  recent,
  lang,
  locale,
}: {
  recent: ImportStatus[];
  lang: Lang;
  locale: DateLocale;
}) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const [entity, setEntity] = useState<Entity>("clients");
  const [file, setFile] = useState<File | null>(null);
  const [region, setRegion] = useState("");
  const [busy, setBusy] = useState<"preview" | "start" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<PreviewReport | null>(null);
  const [running, setRunning] = useState<ImportStatus | null>(null);
  const [history, setHistory] = useState<ImportStatus[]>(recent);
  const inputRef = useRef<HTMLInputElement>(null);

  function reset(next?: Entity) {
    if (next) setEntity(next);
    setFile(null);
    setReport(null);
    setRunning(null);
    setError(null);
    if (inputRef.current) inputRef.current.value = "";
  }

  function fail(status: number, detail: string, body?: unknown) {
    const key = importErrorKey(status, detail);
    setError(detail === "missing_columns" ? fmt(tr(key), { cols: missingColumns(body).join(", ") }) : tr(key));
  }

  async function preview() {
    if (!file) return;
    const check = checkImportFile(file);
    if (check !== "ok") {
      setError(tr(check === "wrong_type" ? "onb.err.wrong_type" : check === "empty" ? "onb.err.empty_upload" : "onb.err.import_too_large"));
      return;
    }
    setBusy("preview");
    setError(null);
    setReport(null);
    setRunning(null);
    const res = await uploadRaw<PreviewReport>(`client-imports/preview?${importQuery(entity, normaliseRegion(region))}`, file);
    setBusy(null);
    if (!res.ok) fail(res.status, res.detail, res.body);
    else setReport(res.data);
  }

  async function start() {
    if (!file || !report) return;
    setBusy("start");
    setError(null);
    const res = await uploadRaw<{ import_id: string }>(
      `client-imports?${importQuery(entity, normaliseRegion(region), report.sha256)}`,
      file,
    );
    setBusy(null);
    if (!res.ok) {
      fail(res.status, res.detail, res.body);
      return;
    }
    setRunning({
      import_id: res.data.import_id,
      entity,
      status: "queued",
      row_count: report.row_count,
      processed_count: 0,
      counts: { create: 0, match: 0, already_imported: 0, duplicate_in_file: 0, invalid: 0 },
      possible_duplicate_count: 0,
      created_at: new Date().toISOString(),
      finished_at: null,
    });
  }

  const runningId = running && !isFinished(running.status) ? running.import_id : null;
  useEffect(() => {
    if (!runningId) return;
    let stopped = false;
    let errors = 0;
    let handle: ReturnType<typeof setTimeout> | null = null;
    async function tick() {
      const res = await bffSend<ImportStatus>("GET", `client-imports/${runningId}`);
      if (stopped) return;
      if (res.ok) {
        errors = 0;
        setRunning(res.data);
        if (isFinished(res.data.status)) {
          const list = await bffSend<{ imports: ImportStatus[] }>("GET", "client-imports");
          if (!stopped && list.ok) setHistory(list.data.imports);
          return;
        }
      } else if (++errors >= 5 || (res.status >= 400 && res.status < 500 && res.status !== 404)) {
        fail(res.status, res.detail);
        return;
      }
      handle = setTimeout(tick, IMPORT_POLL_MS);
    }
    void tick();
    return () => {
      stopped = true;
      if (handle) clearTimeout(handle);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runningId]);

  const issueColumns = useMemo<ColumnDef<Issue, unknown>[]>(
    () => [
      { id: "row", header: t(lang, "onb.imp.row"), accessorKey: "row", enableSorting: false },
      { id: "outcome", header: t(lang, "onb.imp.outcome"), enableSorting: false, cell: ({ row }) => tr(`onb.imp.out.${row.original.outcome}`) },
      {
        id: "why",
        header: t(lang, "onb.imp.problem"),
        enableSorting: false,
        cell: ({ row }) => row.original.codes.map((c) => codeText(lang, c)).join("; ") || codeText(lang, "other"),
      },
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [lang],
  );
  const historyColumns = useMemo<ColumnDef<ImportStatus, unknown>[]>(
    () => [
      { id: "when", header: t(lang, "onb.imp.when"), enableSorting: false, cell: ({ row }) => formatDate(new Date(row.original.created_at), locale) },
      { id: "entity", header: t(lang, "onb.imp.entity"), enableSorting: false, cell: ({ row }) => tr(`onb.imp.entity.${row.original.entity}`) },
      {
        id: "status",
        header: t(lang, "onb.imp.status"),
        enableSorting: false,
        cell: ({ row }) => {
          const s = row.original.status;
          return <Badge tone={s === "done" ? "success" : s === "failed" ? "danger" : "info"}>{tr(`onb.imp.state.${s}`)}</Badge>;
        },
      },
      { id: "create", header: t(lang, "onb.imp.out.create"), enableSorting: false, cell: ({ row }) => row.original.counts.create },
      {
        id: "already",
        header: t(lang, "onb.imp.out.already_imported"),
        enableSorting: false,
        cell: ({ row }) => row.original.counts.already_imported,
      },
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [lang, locale],
  );

  const templateHref = `data:text/csv;charset=utf-8,${encodeURIComponent(templateCsv(entity))}`;
  const toApply = report ? report.counts.create + report.counts.match : 0;
  return (
    <div style={{ display: "grid", gap: 16 }} data-testid="onb-import">
      <Tabs
        tabs={ENTITIES.map((e) => ({ key: e, label: tr(`onb.imp.phase.${e}`) }))}
        active={entity}
        onChange={(key: string) => reset(key as Entity)}
        variant="underline"
        ariaLabel={t(lang, "onb.imp.title")}
        gap={18}
      />
      <Panel title={tr(`onb.imp.phase.${entity}`)} testId={`onb-import-${entity}`}>
        <div style={{ display: "grid", gap: 12 }}>
          <p style={{ margin: 0, fontSize: 13.5, color: K.body }}>{tr(`onb.imp.phaseHelp.${entity}`)}</p>
          <p style={{ margin: 0, fontSize: 12.5, color: K.muted, overflowWrap: "anywhere" }}>
            {fmt(t(lang, "onb.imp.requiredCols"), { cols: COLUMNS[entity].required.join(", ") })}
            {COLUMNS[entity].optional.length > 0 && (
              <>
                <br />
                {fmt(t(lang, "onb.imp.optionalCols"), { cols: COLUMNS[entity].optional.join(", ") })}
              </>
            )}
          </p>
          <a href={templateHref} download={`${entity}-template.csv`} style={{ fontSize: 13, color: K.accentDeep, width: "fit-content" }}>
            {t(lang, "onb.imp.template")}
          </a>
          <div style={{ display: "grid", gap: 12, gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))", alignItems: "start" }}>
            <Field label={t(lang, "onb.imp.file")}>
              <input
                ref={inputRef}
                type="file"
                accept=".csv,.xlsx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                data-testid="onb-import-file"
                onChange={(e) => {
                  setFile(e.target.files?.[0] ?? null);
                  setReport(null);
                  setRunning(null);
                  setError(null);
                }}
                style={{ fontSize: 13, color: K.body, maxWidth: "100%" }}
              />
            </Field>
            {entity === "clients" && (
              <Field label={t(lang, "onb.imp.region")} hint={t(lang, "onb.imp.regionHint")}>
                <Input value={region} onChange={(e) => setRegion(e.target.value)} maxLength={2} placeholder="VN" style={{ maxWidth: 90 }} />
              </Field>
            )}
          </div>
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
            <span data-testid="onb-import-preview">
              <Button variant="secondary" disabled={!file || busy !== null} onClick={preview}>
                {busy === "preview" ? t(lang, "onb.imp.previewing") : t(lang, "onb.imp.preview")}
              </Button>
            </span>
            {report && !running && (
              <span data-testid="onb-import-apply">
                <Button variant="primary" disabled={busy !== null || toApply === 0} onClick={start}>
                  {`${t(lang, "onb.imp.apply")} (${toApply})`}
                </Button>
              </span>
            )}
          </div>
          {error && (
            <p role="alert" style={{ margin: 0, color: K.danger, fontSize: 13 }} data-testid="onb-import-error">
              {error}
            </p>
          )}
        </div>
      </Panel>

      {report && (
        <Panel title={fmt(t(lang, "onb.imp.report"), { rows: report.row_count })} testId="onb-import-report">
          <Counts counts={report.counts} lang={lang} />
          {report.counts.create + report.counts.match === 0 && (
            <p style={{ margin: "10px 0 0", fontSize: 13, color: K.muted }}>{t(lang, "onb.imp.nothing")}</p>
          )}
          {report.possible_duplicate_count > 0 && (
            <p style={{ margin: "10px 0 0", fontSize: 13, color: K.body }}>
              {fmt(t(lang, "onb.imp.possibleDup"), { n: report.possible_duplicate_count })}
            </p>
          )}
          {report.ignored_columns.length > 0 && (
            <p style={{ margin: "10px 0 0", fontSize: 12.5, color: K.muted, overflowWrap: "anywhere" }}>
              {fmt(t(lang, "onb.imp.ignored"), { cols: report.ignored_columns.join(", ") })}
            </p>
          )}
          {report.issues.length > 0 && (
            <div style={{ marginTop: 14 }}>
              <h3 style={{ margin: "0 0 6px", fontSize: 13, color: K.ink }}>{t(lang, "onb.imp.issues")}</h3>
              <span data-testid="onb-import-issues" style={{ display: "block" }}>
                <DataTable<Issue>
                  data={report.issues}
                  columns={issueColumns}
                  tableId="onb-import-issues"
                  getRowId={(i) => String(i.row)}
                  searchable={false}
                  paginate={report.issues.length > 25}
                  pageSize={25}
                  compact
                />
              </span>
              {report.issues_truncated && <p style={{ margin: "8px 0 0", fontSize: 12.5, color: K.muted }}>{t(lang, "onb.imp.truncated")}</p>}
            </div>
          )}
          <p style={{ margin: "12px 0 0", fontSize: 12.5, color: K.muted }}>{t(lang, "onb.imp.rerun")}</p>
        </Panel>
      )}

      {running && (
        <Panel testId="onb-import-progress">
          <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", marginBottom: 10 }}>
            <strong style={{ fontSize: 14, color: K.ink }}>
              {running.status === "done"
                ? t(lang, "onb.imp.done")
                : running.status === "failed"
                  ? t(lang, "onb.imp.state.failed")
                  : t(lang, "onb.imp.started")}
            </strong>
            <span style={{ fontSize: 13, color: K.muted }}>
              {fmt(t(lang, "onb.imp.progress"), { done: running.processed_count, total: running.row_count })}
            </span>
          </div>
          {running.status === "failed" && <p style={{ margin: "0 0 10px", fontSize: 13, color: K.danger }}>{t(lang, "onb.imp.failed")}</p>}
          <Counts counts={running.counts} lang={lang} />
          {running.status === "done" && running.possible_duplicate_count > 0 && (
            <p style={{ margin: "10px 0 0", fontSize: 13 }}>
              <Link href="/clients/duplicates" style={{ color: K.accentDeep }}>
                {t(lang, "onb.dup.link")} →
              </Link>
            </p>
          )}
        </Panel>
      )}

      <Panel title={t(lang, "onb.imp.recent")} testId="onb-import-recent">
        {history.length === 0 ? (
          <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{t(lang, "onb.imp.none")}</p>
        ) : (
          <DataTable<ImportStatus>
            data={history}
            columns={historyColumns}
            tableId="onb-import-recent"
            getRowId={(h) => h.import_id}
            searchable={false}
            paginate={false}
            compact
          />
        )}
      </Panel>
    </div>
  );
}

function codeText(lang: Lang, code: string): string {
  const key = `onb.code.${code}`;
  const text: string | undefined = t(lang, key as MessageKey);
  return text || t(lang, "onb.code.other");
}

function Counts({ counts, lang }: { counts: Record<(typeof OUTCOMES)[number], number>; lang: Lang }) {
  return (
    <dl
      style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 8, margin: 0 }}
      data-testid="onb-import-counts"
    >
      {OUTCOMES.map((o) => (
        <div key={o} style={{ border: `1px solid ${K.line}`, borderRadius: 8, padding: "8px 10px" }}>
          <dt style={{ fontSize: 12, color: K.muted }}>{t(lang, `onb.imp.out.${o}` as MessageKey)}</dt>
          <dd style={{ margin: "2px 0 0", fontSize: 20, color: K.ink, fontWeight: 500 }} data-testid={`onb-count-${o}`}>
            {counts[o]}
          </dd>
        </div>
      ))}
    </dl>
  );
}
