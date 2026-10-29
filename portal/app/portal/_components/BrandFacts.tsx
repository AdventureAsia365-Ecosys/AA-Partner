"use client";
// portal/app/portal/_components/BrandFacts.tsx
// AA-814 — Brand › Facts: the agency's own Facts Entries (prices, seasons, booking terms … with their source) that the
// writer may cite in its posts, and the Adventure Asia facts it can replace. CIS /v1/facts through the BFF
// (/api/tenant/v1/facts):
//   GET    /v1/facts             { own, platform, kinds, max_own }
//   POST   /v1/facts             a new own fact
//   PATCH  /v1/facts/{id}        changed fields + expected_updated_at (409 when stale)
//   DELETE /v1/facts/{id}
// The tenant id comes from the session token, never from the page. A viewer may read; every write answers 403 (CIS
// tenant_role middleware), shown as a plain sentence. Content only: no internal flags, ids of other people or QA data.
// Kit: PageHeader, DataTable, Drawer, Modal, ConfirmModal, Button, Badge; react-query key ["brand-facts"].

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, RefreshCw, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import {
  apiGet, Badge, Button, type ColumnDef, ConfirmModal, DataTable, Drawer, formatDate, K, Modal, PageHeader, RADIUS,
  sans, useToast,
} from "@/app/_kit";
import {
  EMPTY_FORM, FACT_KINDS, FACTS_API, KIND_LABEL, MAX_BODY, MAX_COUNTRY, MAX_SOURCE, MAX_TITLE, PROBLEM_TEXT,
  changedFields, createBody, factCall, formOf, formProblem, overrideForm, ownOnSubject,
  type FactForm, type FactKind, type TenantFact, type TenantFactList,
} from "@/lib/facts";

export const BRAND_FACTS_QUERY_KEY = ["brand-facts"] as const;

const fieldStyle: React.CSSProperties = {
  fontSize: 13.5, padding: "8px 10px", border: `1px solid ${K.line}`, borderRadius: RADIUS.sm, background: K.card,
  color: K.ink, width: "100%", boxSizing: "border-box", fontFamily: sans, minHeight: 36,
};
const noteStyle: React.CSSProperties = { fontSize: 12.5, color: K.muted, lineHeight: 1.55, fontFamily: sans };

function Field({ label, htmlFor, hint, children }: {
  label: string; htmlFor: string; hint?: string; children: React.ReactNode;
}) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 5, minWidth: 0 }}>
      <label htmlFor={htmlFor} style={{ fontSize: 12.5, fontWeight: 600, color: K.ink3 }}>{label}</label>
      {children}
      {hint && <span style={{ fontSize: 12, color: K.muted }}>{hint}</span>}
    </div>
  );
}

function ErrorLine({ message }: { message: string }) {
  return (
    <div role="alert" style={{ padding: "8px 10px", borderRadius: RADIUS.md, background: K.dangerSoft, color: K.ink,
      fontSize: 12.5, lineHeight: 1.45, overflowWrap: "anywhere" }}>{message}</div>
  );
}

function subjectCell(f: TenantFact) {
  return (
    <span style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
      <span style={{ fontWeight: 600, color: K.ink, overflowWrap: "anywhere" }}>{f.title}</span>
      <span style={{ fontSize: 11.5, color: K.muted }}>{f.country || "Any country"}</span>
    </span>
  );
}

function factCell(f: TenantFact) {
  return (
    <span style={{ display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden",
      overflowWrap: "anywhere", maxWidth: 380 }}>{f.body}</span>
  );
}

function validCell(f: TenantFact) {
  if (f.expired) return <Badge tone="warning">Expired</Badge>;
  return f.valid_until
    ? <span style={{ whiteSpace: "nowrap" }}>{formatDate(f.valid_until)}</span>
    : <span style={{ color: K.muted }}>No end</span>;
}

export function BrandFacts() {
  const qc = useQueryClient();
  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: BRAND_FACTS_QUERY_KEY,
    queryFn: () => apiGet<TenantFactList>(FACTS_API),
  });
  const own = useMemo(() => data?.own ?? [], [data]);
  const platform = data?.platform ?? [];
  const [editing, setEditing] = useState<TenantFact | null>(null);
  const [creating, setCreating] = useState<FactForm | null>(null);
  const refresh = () => { void qc.invalidateQueries({ queryKey: BRAND_FACTS_QUERY_KEY }); };
  const errorMessage = isError ? (error instanceof Error ? error.message : "Could not load the facts") : null;

  const ownColumns = useMemo<ColumnDef<TenantFact, unknown>[]>(() => [
    { id: "subject", accessorFn: (f) => f.title, header: "Subject", cell: (c) => subjectCell(c.row.original) },
    { id: "kind", accessorFn: (f) => KIND_LABEL[f.kind] ?? f.kind, header: "Kind" },
    { id: "fact", accessorFn: (f) => f.body, header: "Fact", cell: (c) => factCell(c.row.original) },
    {
      id: "source", accessorFn: (f) => f.provenance, header: "Source",
      cell: (c) => <span style={{ color: K.ink3, overflowWrap: "anywhere" }}>{c.row.original.provenance}</span>,
    },
    { id: "valid", accessorFn: (f) => f.valid_until ?? "", header: "Valid until", cell: (c) => validCell(c.row.original) },
  ], []);

  const ownById = useMemo(() => new Map(own.map((f) => [f.fact_id, f])), [own]);
  const platformColumns = useMemo<ColumnDef<TenantFact, unknown>[]>(() => [
    { id: "subject", accessorFn: (f) => f.title, header: "Subject", cell: (c) => subjectCell(c.row.original) },
    { id: "kind", accessorFn: (f) => KIND_LABEL[f.kind] ?? f.kind, header: "Kind" },
    { id: "fact", accessorFn: (f) => f.body, header: "Fact", cell: (c) => factCell(c.row.original) },
    { id: "valid", accessorFn: (f) => f.valid_until ?? "", header: "Valid until", cell: (c) => validCell(c.row.original) },
    {
      id: "yours", header: "Your version", enableSorting: false,
      cell: (c) => {
        const f = c.row.original;
        const mine = f.overridden_by ? ownById.get(f.overridden_by) : undefined;
        if (mine) {
          return (
            <Button size="sm" variant="ghost" onClick={() => setEditing(mine)}>
              <Badge tone="success" dot={false}>Replaced by yours</Badge>
            </Button>
          );
        }
        return <Button size="sm" variant="secondary" onClick={() => setCreating(overrideForm(f))}>Use my own</Button>;
      },
    },
  ], [ownById]);

  const atLimit = !!data && own.length >= data.max_own;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14, fontFamily: sans }}>
      <PageHeader
        title="Facts"
        description="Facts the writer may state in your posts, each with its source: your prices, seasons, booking terms, transfer times. Your fact replaces an Adventure Asia fact on the same subject in your next posts. A fact past its valid-until date is no longer used."
        actions={
          <>
            <Button variant="secondary" size="sm" onClick={refresh} disabled={isFetching}>
              <RefreshCw size={13} aria-hidden /> {isFetching ? "Loading…" : "Refresh"}
            </Button>
            <Button variant="primary" size="sm" onClick={() => setCreating(EMPTY_FORM)} disabled={atLimit}
              title={atLimit ? `You can keep at most ${data?.max_own} facts. Delete one first.` : undefined}>
              <Plus size={13} aria-hidden /> Add a fact
            </Button>
          </>
        }
      />

      <section aria-labelledby="facts-own" data-testid="brand-facts-own" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <h2 id="facts-own" style={{ margin: 0, fontSize: 15, fontWeight: 700, color: K.ink }}>
          Your facts{data ? ` (${own.length} of ${data.max_own})` : ""}
        </h2>
        <DataTable<TenantFact>
          data={own}
          columns={ownColumns}
          tableId="brand-facts-own"
          getRowId={(f) => f.fact_id}
          loading={isLoading}
          error={errorMessage}
          onRetry={() => refetch()}
          enableCsv={false}
          pageSize={25}
          onRowClick={(f) => setEditing(f)}
          emptyTitle="No facts of your own yet"
          emptyDescription="Add a price, a season or your booking terms with its source. The writer can then state it in your posts."
          tableMinWidth={760}
        />
      </section>

      <section aria-labelledby="facts-aa" data-testid="brand-facts-platform" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <h2 id="facts-aa" style={{ margin: 0, fontSize: 15, fontWeight: 700, color: K.ink }}>Adventure Asia facts</h2>
        <div style={noteStyle}>
          Every agency&apos;s writer may use these. Use my own writes your fact on the same subject; from then on your
          posts use yours.
        </div>
        <DataTable<TenantFact>
          data={platform}
          columns={platformColumns}
          tableId="brand-facts-platform"
          getRowId={(f) => f.fact_id}
          loading={isLoading}
          error={errorMessage}
          onRetry={() => refetch()}
          enableCsv={false}
          pageSize={25}
          emptyTitle="No Adventure Asia facts right now"
          emptyDescription="Facts Adventure Asia adds for every agency show here."
          tableMinWidth={760}
        />
      </section>

      {creating && (
        <CreateFact key={JSON.stringify(creating)} initial={creating} own={own}
          onClose={() => setCreating(null)} onCreated={() => { setCreating(null); refresh(); }} />
      )}
      {editing && (
        <EditFact key={`${editing.fact_id}:${editing.updated_at}`} fact={editing} own={own}
          onClose={() => setEditing(null)} onChanged={(next) => { refresh(); setEditing(next); }} />
      )}
    </div>
  );
}

// ── the form ─────────────────────────────────────────────────────────────────────────────────────────────────────

function FactFields({ form, setForm, disabled, idPrefix }: {
  form: FactForm; setForm: (f: FactForm) => void; disabled: boolean; idPrefix: string;
}) {
  const set = <F extends keyof FactForm>(key: F, value: FactForm[F]) => setForm({ ...form, [key]: value });
  const id = (s: string) => `${idPrefix}-${s}`;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <Field label="Kind" htmlFor={id("kind")}>
        <select id={id("kind")} value={form.kind} disabled={disabled} style={fieldStyle}
          onChange={(e) => set("kind", e.target.value as FactKind)}>
          {FACT_KINDS.map((k) => <option key={k} value={k}>{KIND_LABEL[k]}</option>)}
        </select>
      </Field>
      <Field label="Subject" htmlFor={id("title")}
        hint='A short name for what the fact is about, e.g. "Mekong cruise price". The same kind, subject and country as an Adventure Asia fact replaces it.'>
        <input id={id("title")} type="text" autoComplete="off" maxLength={MAX_TITLE} value={form.title} disabled={disabled}
          onChange={(e) => set("title", e.target.value)} style={fieldStyle} />
      </Field>
      <Field label="Fact" htmlFor={id("body")} hint="One or two sentences the writer may state as true.">
        <textarea id={id("body")} rows={4} maxLength={MAX_BODY} value={form.body} disabled={disabled}
          onChange={(e) => set("body", e.target.value)} style={{ ...fieldStyle, resize: "vertical" }} />
      </Field>
      <Field label="Source" htmlFor={id("source")} hint="Where it comes from: your rate sheet, a supplier, a website.">
        <input id={id("source")} type="text" autoComplete="off" maxLength={MAX_SOURCE} value={form.provenance}
          disabled={disabled} onChange={(e) => set("provenance", e.target.value)} style={fieldStyle} />
      </Field>
      <Field label="Country" htmlFor={id("country")} hint="Leave empty for every country, or the country name the tours use, e.g. Laos.">
        <input id={id("country")} type="text" autoComplete="off" maxLength={MAX_COUNTRY} value={form.country}
          disabled={disabled} onChange={(e) => set("country", e.target.value)} style={fieldStyle} />
      </Field>
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
        <div style={{ flex: "1 1 160px", minWidth: 0 }}>
          <Field label="Stated on" htmlFor={id("stated")} hint="The date of the source.">
            <input id={id("stated")} type="date" value={form.stated_on} disabled={disabled}
              onChange={(e) => set("stated_on", e.target.value)} style={fieldStyle} />
          </Field>
        </div>
        <div style={{ flex: "1 1 160px", minWidth: 0 }}>
          <Field label="Valid until" htmlFor={id("until")} hint="Last day it holds. Empty: no end.">
            <input id={id("until")} type="date" value={form.valid_until} disabled={disabled}
              onChange={(e) => set("valid_until", e.target.value)} style={fieldStyle} />
          </Field>
        </div>
      </div>
    </div>
  );
}

function CreateFact({ initial, own, onClose, onCreated }: {
  initial: FactForm; own: TenantFact[]; onClose: () => void; onCreated: () => void;
}) {
  const toast = useToast();
  const [form, setForm] = useState<FactForm>(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const problem = formProblem(form);
  const duplicate = ownOnSubject(own, form);

  const close = () => { if (!busy) onClose(); };
  const submit = async () => {
    if (problem) { setError(PROBLEM_TEXT[problem]); return; }
    if (busy) return;
    setBusy(true);
    setError(null);
    const r = await factCall<TenantFact>("POST", "", createBody(form));
    setBusy(false);
    if (!r.ok) { setError(r.message); return; }
    toast.success("Fact added. Your next posts can use it.");
    onCreated();
  };

  return (
    <Modal open onClose={close} title="Add a fact" width={560}
      footer={
        <>
          <Button variant="ghost" onClick={close} disabled={busy}>Cancel</Button>
          <Button variant="primary" onClick={() => void submit()} disabled={busy || !!problem}>
            {busy ? "Adding…" : "Add fact"}
          </Button>
        </>
      }>
      <form data-testid="brand-fact-create" onSubmit={(e) => { e.preventDefault(); void submit(); }}
        style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <FactFields form={form} setForm={setForm} disabled={busy} idPrefix="brand-fact-new" />
        {duplicate && (
          <div style={noteStyle}>
            You already have a fact on this subject (&quot;{duplicate.title}&quot;). The newest stated one is used; edit
            that one instead to keep one version.
          </div>
        )}
        {problem === "dates" && <div style={noteStyle}>{PROBLEM_TEXT.dates}</div>}
        {error && <ErrorLine message={error} />}
      </form>
    </Modal>
  );
}

function EditFact({ fact, own, onClose, onChanged }: {
  fact: TenantFact; own: TenantFact[]; onClose: () => void; onChanged: (next: TenantFact | null) => void;
}) {
  const toast = useToast();
  const [form, setForm] = useState<FactForm>(() => formOf(fact));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const changes = changedFields(fact, form);
  const dirty = Object.keys(changes).length > 0;
  const problem = formProblem(form);
  const duplicate = ownOnSubject(own, form, fact.fact_id);

  const close = () => {
    if (busy) return;
    if (dirty && typeof window !== "undefined" && !window.confirm("You have unsaved changes. Close and lose them?")) return;
    onClose();
  };
  const save = async () => {
    if (problem) { setError(PROBLEM_TEXT[problem]); return; }
    if (!dirty || busy) return;
    setBusy(true);
    setError(null);
    const r = await factCall<TenantFact>("PATCH", `/${encodeURIComponent(fact.fact_id)}`,
      { ...changes, expected_updated_at: fact.updated_at });
    setBusy(false);
    if (!r.ok) { setError(r.message); return; }
    toast.success("Fact saved.");
    onChanged(r.body);
  };
  const remove = async () => {
    setBusy(true);
    setError(null);
    const r = await factCall<{ deleted: boolean }>("DELETE", `/${encodeURIComponent(fact.fact_id)}`);
    setBusy(false);
    setConfirmDelete(false);
    if (!r.ok) { setError(r.message); return; }
    toast.success("Fact deleted.");
    onChanged(null);
  };

  return (
    <>
      <Drawer open onClose={close} title="Your fact" width={560}
        footer={
          <div style={{ display: "flex", gap: 8, justifyContent: "space-between", width: "100%", flexWrap: "wrap" }}>
            <Button variant="danger" onClick={() => setConfirmDelete(true)} disabled={busy}>
              <Trash2 size={13} aria-hidden /> Delete
            </Button>
            <span style={{ display: "inline-flex", gap: 8 }}>
              <Button variant="ghost" onClick={close} disabled={busy}>Cancel</Button>
              <Button variant="primary" onClick={() => void save()} disabled={busy || !dirty || !!problem}>
                {busy ? "Saving…" : "Save"}
              </Button>
            </span>
          </div>
        }>
        <div data-testid="brand-fact-drawer" style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          {fact.expired && (
            <div style={noteStyle}><Badge tone="warning">Expired</Badge> The writer no longer uses this fact. Change
              its valid-until date to use it again.</div>
          )}
          <FactFields form={form} setForm={setForm} disabled={busy} idPrefix="brand-fact-edit" />
          {duplicate && (
            <div style={noteStyle}>Another of your facts has this subject (&quot;{duplicate.title}&quot;). The newest
              stated one is used.</div>
          )}
          {problem === "dates" && <div style={noteStyle}>{PROBLEM_TEXT.dates}</div>}
          {error && <ErrorLine message={error} />}
        </div>
      </Drawer>
      <ConfirmModal open={confirmDelete} onClose={() => { if (!busy) setConfirmDelete(false); }}
        onConfirm={() => void remove()} title="Delete this fact?" destructive busy={busy} confirmLabel="Delete"
        cancelLabel="Cancel"
        body="Your next posts stop using it (an Adventure Asia fact on the same subject is used again). Posts already written keep their text." />
    </>
  );
}
