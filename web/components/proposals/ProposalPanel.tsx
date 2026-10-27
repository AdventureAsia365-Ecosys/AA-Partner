"use client";
// web/components/proposals/ProposalPanel.tsx — the trip workspace's Proposal tab (AA-946, card CRM-18; design v3 M4
// base, M7 manual, M11 base, F6): the text proposal (draft, edit, send -> the client's link shown once to copy,
// withdraw), the client's acceptance, the payment schedule with "Mark paid" (owner / agent; the deposit moves the trip
// to Booked and gives every traveller their own portal link, shown once to copy) and "Void", and the travellers'
// Stage 3 / waiver progress with "Send portal link" (re-issue) per person. Every refusal is worded from its code
// (lib/proposals.ts). Buttons wait for hydration (the page is a full server render, AA-936 lesson).
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, K, useHydrated } from "@/app/_kit";
import { DateInput } from "@/components/DateInput";
import { Panel } from "@/components/PageTitle";
import { BuilderClientRow } from "@/components/proposals/BuilderClientRow";
import { bffSend } from "@/lib/bffClient";
import { builderErrorKey } from "@/lib/builder";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import {
  clientUrl,
  currentProposal,
  draftBody,
  formatAmount,
  PAY_METHODS,
  proposalErrorKey,
  type Agreement,
  type InviteResult,
  type LifecycleView,
  type PayMethod,
  type Proposal,
} from "@/lib/proposals";
import { canRenderText } from "@/lib/textProposal";

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
const STATUS_TONE = { draft: "neutral", published: "info", sent: "info", accepted: "success", superseded: "neutral", withdrawn: "danger" } as const;
const ROW_TONE = { due: "warning", paid: "success", void: "neutral" } as const;

type Copy = { label: string; url: string };

export function ProposalPanel({
  view,
  terms,
  party,
  role,
  lang,
  locale,
  clientOrigin,
}: {
  view: LifecycleView;
  /** Published booking terms versions (Settings > Agreements). */
  terms: Agreement[];
  /** Names of the trip's travellers (from GET /trips/{id}). */
  party: { traveler_id: string; display_name: string; role: string }[];
  role: string;
  lang: Lang;
  locale: DateLocale;
  clientOrigin: string | null;
}) {
  const router = useRouter();
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  // AA-949: builder versions live in the day-list builder (their own section below); this form handles text versions.
  const textProposals = view.proposals.filter((p) => (p.kind ?? "text") === "text");
  const builderVersions = view.proposals.filter((p) => p.kind === "builder");
  const current = currentProposal(textProposals);
  const [builderCurrency, setBuilderCurrency] = useState("USD");
  const canSend = role === "owner" || role === "agent";
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "danger" | "success"; text: string } | null>(null);
  const [copies, setCopies] = useState<Copy[]>([]);
  const [editing, setEditing] = useState(false);
  const [paying, setPaying] = useState<string | null>(null);
  const [method, setMethod] = useState<PayMethod>("bank_transfer");
  const name = (id: string) => party.find((p) => p.traveler_id === id)?.display_name ?? tr("proposal.traveller");

  async function run<T>(method: "POST" | "PATCH", path: string, body?: unknown): Promise<T | null> {
    setBusy(true);
    setMessage(null);
    const res = await bffSend<T>(method, path, body);
    setBusy(false);
    if (!res.ok) {
      setMessage({ tone: "danger", text: tr(proposalErrorKey(res.detail)) });
      return null;
    }
    return res.data;
  }

  async function saveDraft(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const built = draftBody({
      body: String(data.get("body") ?? ""),
      currency: String(data.get("currency") ?? ""),
      total: String(data.get("total") ?? ""),
      deposit: String(data.get("deposit") ?? ""),
      balanceDueOn: String(data.get("balance_due_on") ?? ""),
      termsDocId: String(data.get("terms_doc_id") ?? ""),
    });
    if (!built.ok) {
      setMessage({ tone: "danger", text: tr(built.key) });
      return;
    }
    const editingDraft = current?.status === "draft" && editing;
    const done = editingDraft
      ? await run("PATCH", `proposals/${current.proposal_id}`, built.body)
      : await run("POST", `trips/${view.trip_id}/proposals`, built.body);
    if (done) {
      setEditing(false);
      setMessage({ tone: "success", text: tr("proposal.saved") });
      router.refresh();
    }
  }

  async function send(p: Proposal) {
    const done = await run<{ link_path: string }>("POST", `proposals/${p.proposal_id}/send`);
    if (done) {
      setCopies([{ label: tr("proposal.clientLink"), url: clientUrl(clientOrigin, done.link_path) }]);
      setMessage({ tone: "success", text: tr("proposal.sent") });
      router.refresh();
    }
  }

  async function startBuilder() {
    setBusy(true);
    setMessage(null);
    const res = await bffSend<{ proposal_id: string }>("POST", `trips/${view.trip_id}/builder`, { currency: builderCurrency });
    setBusy(false);
    const existing = res.ok ? res.data.proposal_id : (res.body as { proposal_id?: string } | undefined)?.proposal_id;
    if (res.ok || (res.detail === "builder_draft_exists" && existing)) {
      router.push(`/pipeline/${view.trip_id}/proposal/${existing}`);
      return;
    }
    setMessage({ tone: "danger", text: tr(builderErrorKey(res.detail)) });
  }

  async function withdraw(p: Proposal) {
    if (await run("POST", `proposals/${p.proposal_id}/withdraw`)) router.refresh();
  }

  async function markPaid(scheduleId: string, form: HTMLFormElement) {
    const received = String(new FormData(form).get("received_on") ?? "");
    const done = await run<{ trip_moved: { stage: string } | null; portal: { invited: InviteResult[]; skipped: { traveler_id: string; code: string }[] } | null }>(
      "POST",
      `schedule/${scheduleId}/mark-paid`,
      { method, received_on: received || null },
    );
    if (!done) return;
    setPaying(null);
    const links = (done.portal?.invited ?? []).map((i) => ({ label: fmt(tr("proposal.portalLinkFor"), { name: name(i.traveler_id) }), url: clientUrl(clientOrigin, i.link_path) }));
    setCopies(links);
    setMessage({ tone: "success", text: done.trip_moved ? tr("proposal.paidBooked") : tr("proposal.paid") });
    router.refresh();
  }

  async function invite(travelerId: string) {
    const done = await run<InviteResult>("POST", `trips/${view.trip_id}/portal-access`, { traveler_id: travelerId });
    if (done) {
      setCopies([{ label: fmt(tr("proposal.portalLinkFor"), { name: name(travelerId) }), url: clientUrl(clientOrigin, done.link_path) }]);
      router.refresh();
    }
  }

  async function voidRow(scheduleId: string) {
    if (await run("POST", `schedule/${scheduleId}/void`)) router.refresh();
  }

  const draftOpen = editing || !current || current.status === "superseded" || current.status === "withdrawn";
  const seed = editing && current ? current : null;

  return (
    <Panel title={tr("proposal.tab")} testId="trip-proposal">
      <div data-hydrated={hydrated ? "true" : undefined} style={{ display: "grid", gap: 14, minWidth: 0 }}>
        {message && (
          <p role="status" data-testid="proposal-message" style={{ margin: 0, fontSize: 13, color: message.tone === "danger" ? K.danger : K.success }}>
            {message.text}
          </p>
        )}
        {copies.length > 0 && (
          <div data-testid="proposal-copy-links" style={{ display: "grid", gap: 6, padding: 10, borderRadius: 8, background: K.bg }}>
            <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("proposal.copyOnce")}</p>
            {copies.map((c) => (
              <label key={c.url} style={{ display: "grid", gap: 2, fontSize: 12.5 }}>
                {c.label}
                <input readOnly value={c.url} data-testid="proposal-copy-link" style={{ ...field, width: "100%" }} onFocus={(e) => e.currentTarget.select()} />
              </label>
            ))}
          </div>
        )}

        {current && !editing && (
          <section data-testid="proposal-current" data-status={current.status} style={{ display: "grid", gap: 8 }}>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <strong style={{ fontSize: 14, color: K.ink }}>{fmt(tr("proposal.version"), { n: current.version })}</strong>
              <Badge tone={STATUS_TONE[current.status]}>{tr(`proposal.status.${current.status}`)}</Badge>
              {current.accepted_at && (
                <span style={{ fontSize: 12.5, color: K.muted }}>{fmt(tr("proposal.acceptedOn"), { date: formatDate(current.accepted_at.slice(0, 10), locale) })}</span>
              )}
            </div>
            <p style={{ margin: 0, fontSize: 13, color: K.body, whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{current.body}</p>
            <p style={{ margin: 0, fontSize: 13, color: K.ink }} data-testid="proposal-amounts">
              {fmt(tr("proposal.amounts"), {
                total: formatAmount(current.total_amount, current.currency, locale),
                deposit: formatAmount(current.deposit_amount, current.currency, locale),
              })}
              {current.balance_due_on && ` · ${fmt(tr("proposal.balanceDue"), { date: formatDate(current.balance_due_on, locale) })}`}
            </p>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              {current.status === "draft" && (
                <Button size="sm" variant="secondary" disabled={!hydrated || busy} onClick={() => setEditing(true)}>
                  {tr("proposal.edit")}
                </Button>
              )}
              {current.status === "draft" && canSend && (
                <Button size="sm" disabled={!hydrated || busy} onClick={() => send(current)}>
                  {tr("proposal.send")}
                </Button>
              )}
              {(current.status === "draft" || current.status === "sent") && canSend && (
                <Button size="sm" variant="ghost" disabled={!hydrated || busy} onClick={() => withdraw(current)}>
                  {tr("proposal.withdraw")}
                </Button>
              )}
              {(current.status === "draft" || current.status === "sent") && (
                // AA-951 (CRM-22, A-05): the same version as e-mail or WhatsApp text (one price hash).
                <Link href={`/pipeline/${view.trip_id}/proposal/${current.proposal_id}/text`} style={{ fontSize: 12.5, color: K.accent }} data-testid="proposal-send-text">
                  {tr("text.open")}
                </Link>
              )}
              {current.status === "sent" && (
                <span style={{ fontSize: 12.5, color: K.muted }}>{tr("proposal.waitingClient")}</span>
              )}
              {current.status !== "accepted" && current.status !== "draft" && (
                <Button size="sm" variant="secondary" disabled={!hydrated || busy} onClick={() => setEditing(true)}>
                  {tr("proposal.newVersion")}
                </Button>
              )}
            </div>
          </section>
        )}

        {current?.status !== "accepted" && (
          <section data-testid="proposal-builder" style={{ display: "grid", gap: 6, padding: 10, borderRadius: 8, background: K.bg }}>
            <h3 style={{ margin: 0, fontSize: 13, color: K.ink }}>{tr("builder.section")}</h3>
            <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("builder.sectionHint")}</p>
            {builderVersions.map((p) => (
              <div key={p.proposal_id} data-testid="builder-version" data-status={p.status} style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", fontSize: 13 }}>
                <strong>{fmt(tr("proposal.version"), { n: p.version })}</strong>
                <Badge tone={STATUS_TONE[p.status]}>{tr(`builder.status.${p.status}`)}</Badge>
                <span style={{ color: K.muted }}>{formatAmount(p.total_amount, p.currency, locale)}</span>
                <Link href={`/pipeline/${view.trip_id}/proposal/${p.proposal_id}`} style={{ color: K.accent }} data-testid="builder-open">
                  {tr("builder.open")}
                </Link>
                {canRenderText("builder", p.status) && (
                  <Link href={`/pipeline/${view.trip_id}/proposal/${p.proposal_id}/text`} style={{ color: K.accent }} data-testid="builder-send-text">
                    {tr("text.open")}
                  </Link>
                )}
                <BuilderClientRow
                  p={p}
                  canSend={canSend}
                  hydrated={hydrated}
                  busy={busy}
                  tr={tr}
                  locale={locale}
                  onLink={(path) => setCopies([{ label: tr("proposal.clientLink"), url: clientUrl(clientOrigin, path) }])}
                  onDone={(text) => {
                    setMessage({ tone: "success", text });
                    router.refresh();
                  }}
                  onError={(code) => setMessage({ tone: "danger", text: tr(proposalErrorKey(code)) })}
                />
              </div>
            ))}
            {!builderVersions.some((p) => p.status === "draft") && (
              <div style={{ display: "flex", gap: 8, alignItems: "end", flexWrap: "wrap" }}>
                {builderVersions.length === 0 && (
                  <label style={{ display: "grid", gap: 2, fontSize: 12.5 }}>
                    {tr("proposal.currency")}
                    <input value={builderCurrency} maxLength={3} onChange={(e) => setBuilderCurrency(e.target.value.toUpperCase())} style={{ ...field, width: 72 }} data-testid="builder-currency" />
                  </label>
                )}
                {builderVersions.length === 0 && (
                  <Button size="sm" disabled={!hydrated || busy || !/^[A-Z]{3}$/.test(builderCurrency)} onClick={startBuilder}>
                    {tr("builder.start")}
                  </Button>
                )}
              </div>
            )}
          </section>
        )}

        {draftOpen && current?.status !== "accepted" && (
          <form onSubmit={saveDraft} data-testid="proposal-form" style={{ display: "grid", gap: 8, minWidth: 0 }}>
            <label style={{ display: "grid", gap: 4, fontSize: 12.5 }}>
              {tr("proposal.text")}
              <textarea name="body" rows={6} required defaultValue={seed?.body ?? ""} style={{ ...field, width: "100%", resize: "vertical" }} />
            </label>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <label style={{ display: "grid", gap: 2, fontSize: 12.5 }}>
                {tr("proposal.currency")}
                <input name="currency" defaultValue={seed?.currency ?? "USD"} maxLength={3} style={{ ...field, width: 72 }} />
              </label>
              <label style={{ display: "grid", gap: 2, fontSize: 12.5 }}>
                {tr("proposal.total")}
                <input name="total" inputMode="decimal" defaultValue={seed?.total_amount ?? ""} style={{ ...field, width: 130 }} />
              </label>
              <label style={{ display: "grid", gap: 2, fontSize: 12.5 }}>
                {tr("proposal.deposit")}
                <input name="deposit" inputMode="decimal" defaultValue={seed?.deposit_amount ?? ""} style={{ ...field, width: 130 }} />
              </label>
              <label style={{ display: "grid", gap: 2, fontSize: 12.5 }}>
                {tr("proposal.terms")}
                <select name="terms_doc_id" defaultValue={seed?.terms_doc_id ?? terms[0]?.doc_id ?? ""} style={field}>
                  <option value="">{tr("proposal.noTerms")}</option>
                  {terms.map((d) => (
                    <option key={d.doc_id} value={d.doc_id}>
                      {d.version_label}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <DateInput
              locale={locale}
              name="balance_due_on"
              label={tr("proposal.balanceDueLabel")}
              help={tr("proposal.balanceDueHelp")}
              readAs={tr("settings.readAs")}
              invalid={tr("settings.invalidDate")}
              defaultValue={seed?.balance_due_on ?? ""}
            />
            <div style={{ display: "flex", gap: 8 }}>
              <Button type="submit" size="sm" disabled={!hydrated || busy}>
                {tr("proposal.saveDraft")}
              </Button>
              {editing && (
                <Button type="button" size="sm" variant="ghost" onClick={() => setEditing(false)}>
                  {tr("trip.cancel")}
                </Button>
              )}
            </div>
          </form>
        )}

        {view.schedule.length > 0 && (
          <section data-testid="proposal-schedule" style={{ display: "grid", gap: 6 }}>
            <h3 style={{ margin: 0, fontSize: 13, color: K.ink }}>{tr("proposal.schedule")}</h3>
            {view.schedule.map((row) => (
              <div key={row.schedule_id} data-testid="schedule-row" data-kind={row.kind} data-status={row.status} style={{ display: "grid", gap: 6, padding: "8px 0", borderTop: `1px solid ${K.line}` }}>
                <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", fontSize: 13 }}>
                  <strong>{tr(`proposal.kind.${row.kind}`)}</strong>
                  <span>{formatAmount(row.amount, row.currency, locale)}</span>
                  <span style={{ color: K.muted }}>{fmt(tr("proposal.dueOn"), { date: formatDate(row.due_on, locale) })}</span>
                  <Badge tone={ROW_TONE[row.status]}>{tr(`proposal.row.${row.status}`)}</Badge>
                  {row.status === "due" && canSend && paying !== row.schedule_id && (
                    <>
                      <Button size="sm" disabled={!hydrated || busy} onClick={() => setPaying(row.schedule_id)}>
                        {tr("proposal.markPaid")}
                      </Button>
                      {/* A deposit row is never void (409 deposit_not_voidable): it is marked paid, whatever the method. */}
                      {row.kind !== "deposit" && (
                        <Button size="sm" variant="ghost" disabled={!hydrated || busy} onClick={() => voidRow(row.schedule_id)}>
                          {tr("proposal.void")}
                        </Button>
                      )}
                    </>
                  )}
                </div>
                {paying === row.schedule_id && (
                  <form
                    data-testid="mark-paid-form"
                    onSubmit={(e) => {
                      e.preventDefault();
                      void markPaid(row.schedule_id, e.currentTarget);
                    }}
                    style={{ display: "grid", gap: 8 }}
                  >
                    <label style={{ display: "grid", gap: 2, fontSize: 12.5 }}>
                      {tr("proposal.method")}
                      <select value={method} onChange={(e) => setMethod(e.target.value as PayMethod)} style={field} data-testid="mark-paid-method">
                        {PAY_METHODS.map((m) => (
                          <option key={m} value={m}>
                            {tr(`proposal.method.${m}`)}
                          </option>
                        ))}
                      </select>
                    </label>
                    <DateInput locale={locale} name="received_on" label={tr("proposal.receivedOn")} help={tr("proposal.receivedOnHelp")} readAs={tr("settings.readAs")} invalid={tr("settings.invalidDate")} />
                    <div style={{ display: "flex", gap: 8 }}>
                      <Button type="submit" size="sm" disabled={!hydrated || busy}>
                        {tr("proposal.confirmPaid")}
                      </Button>
                      <Button type="button" size="sm" variant="ghost" onClick={() => setPaying(null)}>
                        {tr("trip.cancel")}
                      </Button>
                    </div>
                  </form>
                )}
              </div>
            ))}
          </section>
        )}

        {party.length > 0 && (view.schedule.length > 0 || view.portal.travellers.some((x) => x.portal)) && (
          <section data-testid="proposal-portal" data-s3={view.portal.s3} style={{ display: "grid", gap: 6 }}>
            <h3 style={{ margin: 0, fontSize: 13, color: K.ink }}>
              {fmt(tr("proposal.portalTitle"), { s3: view.portal.s3 })}
            </h3>
            <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr(view.portal_unlocked ? "proposal.portalOpen" : "proposal.portalLocked")}</p>
            {view.portal.travellers.map((x) => (
              <div key={x.traveler_id} data-testid="portal-row" style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", fontSize: 13, padding: "6px 0", borderTop: `1px solid ${K.line}` }}>
                <strong style={{ overflowWrap: "anywhere" }}>{name(x.traveler_id)}</strong>
                <span style={{ color: K.muted }}>{tr(`proposal.role.${x.role}`)}</span>
                <Badge tone={x.details_done ? "success" : "neutral"}>{tr(x.details_done ? "proposal.detailsDone" : "proposal.detailsMissing")}</Badge>
                {x.waiver_signed !== null && (
                  <Badge tone={x.waiver_signed ? "success" : "neutral"}>{tr(x.waiver_signed ? "proposal.waiverSigned" : "proposal.waiverPending")}</Badge>
                )}
                <span style={{ color: K.muted }}>{x.portal ? tr(`proposal.access.${x.portal.status}`) : tr("proposal.access.none")}</span>
                <Button size="sm" variant="secondary" disabled={!hydrated || busy} onClick={() => invite(x.traveler_id)}>
                  {tr(x.portal ? "proposal.reissue" : "proposal.invite")}
                </Button>
              </div>
            ))}
          </section>
        )}
      </div>
    </Panel>
  );
}
