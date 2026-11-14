"use client";
// web/components/pipeline/AiSupplierConfirmPanel.tsx — "AI reads a supplier confirmation" on the trip workspace (AA-965,
// card CRM-33b R10; design v3 A-03 Bookings tab "Forward a supplier confirmation to AI -> preview card with suggested
// line", A-21). A card under the trip's lines: pick one of the trip's own flight / insurance / visa / transfer / fee
// suppliers with an open line, paste its confirmation; the kit Drawer shows the proposed status and reference of each
// of that supplier's open lines with the staff AiNotice; the agent picks and edits each update, then saves them (the
// normal "Record confirmation" / "Back to planning", one call) or discards the proposal. Nothing is written before
// that, and Adventure Asia lines are never offered (they come from Booking). Calls through the BFF allow-list
// (lib/bff.ts): POST trips/{id}/ai/supplier-confirmation, POST ai/drafts/{id}/apply, POST ai/drafts/{id}/discard.
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { AiNotice, Badge, Button, Drawer, Field, Input, K, Select, Textarea, useHydrated } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import {
  aiExtraErrorKey,
  aiExtraFallbackKey,
  applyBody,
  defaultAction,
  readableSuppliers,
  type ApplyAction,
  type ProposedLine,
  type SupplierConfirmResult,
} from "@/lib/aiExtras";
import { bffSend } from "@/lib/bffClient";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import type { Line, Supplier } from "@/lib/ledger";

const note: React.CSSProperties = { margin: 0, fontSize: 13, color: K.muted, overflowWrap: "anywhere" };
const TEXT_BOUND = 60_000; // the API's request bound; the admin setting (crm.ai.supplier_confirm_max_chars) is checked there

type LinePick = { action: ApplyAction | null; reference: string };

export function AiSupplierConfirmPanel({
  tripId,
  lines,
  suppliers,
  lang,
  closed,
}: {
  tripId: string;
  lines: Line[];
  suppliers: Supplier[];
  lang: Lang;
  closed: boolean;
}) {
  const hydrated = useHydrated();
  const router = useRouter();
  const tr = (key: string) => t(lang, key as MessageKey);
  const readable = useMemo(() => readableSuppliers(lines, suppliers), [lines, suppliers]);
  const [open, setOpen] = useState(false);
  const [supplier, setSupplier] = useState("");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<SupplierConfirmResult | null>(null);
  const [picks, setPicks] = useState<Record<string, LinePick>>({});
  const [done, setDone] = useState<"accepted" | "discarded" | null>(null);
  const chosen = supplier || readable[0]?.agency_supplier_id || "";

  function reset() {
    setResult(null);
    setPicks({});
    setDone(null);
    setError(null);
  }

  async function read(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!chosen || !text.trim()) {
      setError(tr("aiX.error.source_required"));
      return;
    }
    setBusy(true);
    reset();
    const res = await bffSend<SupplierConfirmResult>(
      "POST",
      `trips/${tripId}/ai/supplier-confirmation`,
      { agency_supplier_id: chosen, text: text.trim() },
      { renew: true },
    );
    setBusy(false);
    if (!res.ok) {
      if (!res.redirecting) setError(tr(aiExtraErrorKey(res.detail)));
      return;
    }
    setResult(res.data);
    setPicks(
      Object.fromEntries(
        res.data.lines.map((l) => [l.booking_line_id, { action: defaultAction(l), reference: l.confirmation_no ?? "" }]),
      ),
    );
  }

  function setPick(line: ProposedLine, change: Partial<LinePick>) {
    setPicks((p) => ({ ...p, [line.booking_line_id]: { ...p[line.booking_line_id], ...change } }));
  }

  async function save() {
    if (!result?.ai_draft_id) return;
    const body = applyBody(result.lines.map((line) => ({ line, ...picks[line.booking_line_id] })));
    if (body.length === 0) {
      setError(tr("aiX.sc.nothingPicked"));
      return;
    }
    setBusy(true);
    setError(null);
    const res = await bffSend("POST", `ai/drafts/${result.ai_draft_id}/apply`, { lines: body }, { renew: true });
    setBusy(false);
    if (!res.ok) {
      if (!res.redirecting) setError(tr(aiExtraErrorKey(res.detail)));
      return;
    }
    setDone("accepted");
    router.refresh();
  }

  async function discard() {
    if (!result?.ai_draft_id) return;
    setBusy(true);
    const res = await bffSend("POST", `ai/drafts/${result.ai_draft_id}/discard`, {}, { renew: true });
    setBusy(false);
    if (!res.ok) {
      if (!res.redirecting) setError(tr(aiExtraErrorKey(res.detail)));
      return;
    }
    setDone("discarded");
  }

  const usable = result?.outcome === "ok";

  return (
    <Panel title={tr("aiX.sc.title")} testId="ai-supplier-confirm">
      <div
        data-hydrated={hydrated ? "true" : undefined}
        style={{ display: "flex", gap: 12, alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", minWidth: 0 }}
      >
        <p style={{ ...note, flex: "1 1 260px" }}>{tr(readable.length ? "aiX.sc.intro" : "aiX.sc.noSupplier")}</p>
        <Button
          variant="secondary"
          size="sm"
          testId="ai-supplier-confirm-open"
          disabled={closed || readable.length === 0}
          onClick={() => {
            reset();
            setOpen(true);
          }}
        >
          {tr("aiX.sc.open")}
        </Button>
      </div>
      <Drawer open={open} onClose={() => setOpen(false)} title={tr("aiX.sc.title")} width={620}>
        <div data-testid="ai-supplier-confirm-drawer" style={{ display: "grid", gap: 14, minWidth: 0 }}>
          <ConfirmForm
            readable={readable}
            chosen={chosen}
            onSupplier={setSupplier}
            text={text}
            onText={setText}
            busy={busy}
            hasResult={result !== null}
            onSubmit={read}
            lang={lang}
          />
          <RunStatus error={error} result={result} lang={lang} />
          {usable && result && (
            <SupplierProposal
              result={result}
              picks={picks}
              setPick={setPick}
              busy={busy}
              done={done}
              onSave={save}
              onDiscard={discard}
              lang={lang}
            />
          )}
        </div>
      </Drawer>
    </Panel>
  );
}

function SupplierProposal({
  result,
  picks,
  setPick,
  busy,
  done,
  onSave,
  onDiscard,
  lang,
}: {
  result: SupplierConfirmResult;
  picks: Record<string, LinePick>;
  setPick: (line: ProposedLine, change: Partial<LinePick>) => void;
  busy: boolean;
  done: "accepted" | "discarded" | null;
  onSave: () => void;
  onDiscard: () => void;
  lang: Lang;
}) {
  const tr = (key: string) => t(lang, key as MessageKey);
  return (
    <section data-testid="ai-supplier-confirm-result" data-ai-generated="true" style={{ display: "grid", gap: 10, minWidth: 0 }}>
      <h3 style={{ margin: 0, fontSize: 13, fontWeight: 600, color: K.ink }}>{tr("aiX.sc.result")}</h3>
      {result.lines.length === 0 && <p style={note}>{tr("aiX.sc.none")}</p>}
      {result.lines.map((line) => (
        <ProposedLineRow key={line.booking_line_id} line={line} pick={picks[line.booking_line_id]} setPick={setPick} lang={lang} />
      ))}
      {result.rejected > 0 && <p style={note}>{fmt(tr("aiX.sc.rejected"), { n: result.rejected })}</p>}
      <AiNotice lang={lang} />
      {done ? (
        <p role="status" data-testid="ai-supplier-confirm-done" data-decision={done} style={{ ...note, color: K.body }}>
          {tr(done === "accepted" ? "aiX.sc.applied" : "aiX.sc.discarded")}
        </p>
      ) : (
        <ProposalActions canSave={result.lines.length > 0} hasDraft={!!result.ai_draft_id} busy={busy} onSave={onSave} onDiscard={onDiscard} lang={lang} />
      )}
    </section>
  );
}

function ProposalActions({
  canSave,
  hasDraft,
  busy,
  onSave,
  onDiscard,
  lang,
}: {
  canSave: boolean;
  hasDraft: boolean;
  busy: boolean;
  onSave: () => void;
  onDiscard: () => void;
  lang: Lang;
}) {
  if (!hasDraft) return null;
  return (
    <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
      {canSave && (
        <Button variant="primary" disabled={busy} onClick={onSave} testId="ai-supplier-confirm-apply">
          {t(lang, "aiX.sc.apply")}
        </Button>
      )}
      <Button variant="ghost" disabled={busy} onClick={onDiscard} testId="ai-supplier-confirm-discard">
        {t(lang, "aiX.sc.discard")}
      </Button>
    </div>
  );
}

const STATUS_TONE = { confirmed: "success", declined: "danger", unclear: "neutral" } as const;

function ProposedLineRow({
  line,
  pick = { action: null, reference: "" },
  setPick,
  lang,
}: {
  line: ProposedLine;
  pick?: LinePick;
  setPick: (line: ProposedLine, change: Partial<LinePick>) => void;
  lang: Lang;
}) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const actionOptions = (["confirm", "back_to_planning"] as const).map((a) => ({ value: a, label: tr(`aiX.sc.action.${a}`) }));
  return (
    <div
      data-testid="ai-supplier-confirm-line"
      style={{ display: "grid", gap: 8, padding: 10, border: `1px solid ${K.line}`, borderRadius: 8, minWidth: 0 }}
    >
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <strong style={{ fontSize: 13.5, color: K.ink, overflowWrap: "anywhere" }}>{line.title}</strong>
        <Badge tone={STATUS_TONE[line.status]}>{tr(`aiX.sc.status.${line.status}`)}</Badge>
      </div>
      {line.times && (
        <p style={note}>
          {tr("aiX.sc.times")}: {line.times}
        </p>
      )}
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "end" }}>
        <Field label={tr("aiX.sc.action")}>
          <Select
            value={pick.action ?? ""}
            onChange={(e) => setPick(line, { action: (e.target.value || null) as ApplyAction | null })}
            placeholder={tr("aiX.sc.action.skip")}
            options={actionOptions}
          />
        </Field>
        {pick.action === "confirm" && (
          <Field label={tr("aiX.sc.reference")}>
            <Input maxLength={80} value={pick.reference} onChange={(e) => setPick(line, { reference: e.target.value })} />
          </Field>
        )}
      </div>
    </div>
  );
}

function ConfirmForm({
  readable,
  chosen,
  onSupplier,
  text,
  onText,
  busy,
  hasResult,
  onSubmit,
  lang,
}: {
  readable: Supplier[];
  chosen: string;
  onSupplier: (id: string) => void;
  text: string;
  onText: (text: string) => void;
  busy: boolean;
  hasResult: boolean;
  onSubmit: (event: React.FormEvent<HTMLFormElement>) => void;
  lang: Lang;
}) {
  const tr = (key: string) => t(lang, key as MessageKey);
  return (
    <form onSubmit={onSubmit} style={{ display: "grid", gap: 12, minWidth: 0 }}>
      <Field label={tr("aiX.sc.supplier")} required>
        <Select
          value={chosen}
          onChange={(e) => onSupplier(e.target.value)}
          options={readable.map((s) => ({ value: s.agency_supplier_id, label: s.name }))}
        />
      </Field>
      <Field label={tr("aiX.sc.text")} hint={tr("aiX.sc.textHelp")} required>
        <Textarea rows={7} maxLength={TEXT_BOUND} value={text} onChange={(e) => onText(e.target.value)} />
      </Field>
      <div>
        <Button type="submit" variant="primary" disabled={busy} testId="ai-supplier-confirm-read">
          {tr(busy && !hasResult ? "aiX.sc.reading" : "aiX.sc.read")}
        </Button>
      </div>
    </form>
  );
}

function RunStatus({ error, result, lang }: { error: string | null; result: SupplierConfirmResult | null; lang: Lang }) {
  return (
    <>
      {error && (
        <p role="alert" style={{ margin: 0, fontSize: 13, color: K.danger, overflowWrap: "anywhere" }}>
          {error}
        </p>
      )}
      {result && result.outcome !== "ok" && (
        <p role="status" data-testid="ai-supplier-confirm-fallback" style={{ margin: 0, fontSize: 13, color: K.amber }}>
          {t(lang, aiExtraFallbackKey(result) as MessageKey)}
        </p>
      )}
    </>
  );
}
