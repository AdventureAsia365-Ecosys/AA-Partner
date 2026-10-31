"use client";
// web/components/ledger/LinesPanel.tsx — the trip workspace's Bookings tab (AA-944, card CRM-16; design v3 M6, R17,
// R18, R22, R25): Day | Item | Supplier | Conf no | Status | Cost | Sell | FX (rate @ date) | Collect. Cost, commission
// and the FX of cost are shown only when the API sent them (owner, agent: the assistant gets no money block). Actions:
// add a planning line, set the money (owner / agent), confirm (the agency's validation level decides; refusals are
// worded from their codes), cancel, delete a planning line. Adventure Asia lines are read-only (AA-947, CRM-19): they
// come from a Booking quote / hold / feed event; the row shows the AA status ("AA pending" while Booking has not
// answered, with the next try when it is down), "no longer offered", the hold expiry and the "from"-price warning, and
// offers only the asks the API lists (Hold, Confirm with AA, Release, Re-quote). "Add AA item" opens AaItemForm.
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, K, useHydrated } from "@/app/_kit";
import { DateInput } from "@/components/DateInput";
import { AaItemForm } from "@/components/ledger/AaItemForm";
import { useTripClosed } from "@/components/pipeline/TripClosed";
import { aaBadgeKey, aaErrorKey, aaResultKey, allowedAaActions, isRetrying, type AaAction } from "@/lib/aa";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt, plural, t, type Lang, type MessageKey } from "@/lib/i18n";
import {
  errorKey,
  formatMoney,
  fxReading,
  missingCodes,
  moneyBody,
  seesMargin,
  type Line,
  type MoneyKind,
  type Supplier,
  type TripLines,
} from "@/lib/ledger";

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
};
const cell: React.CSSProperties = { padding: "7px 8px", borderTop: `1px solid ${K.line}`, verticalAlign: "top", fontSize: 13 };
const head: React.CSSProperties = { ...cell, borderTop: "none", color: K.muted, fontSize: 11.5, fontWeight: 600, textAlign: "left" };
const TONE = { planning: "neutral", confirmed: "success", cancelled: "danger" } as const;
const AA_TONE: Record<string, "neutral" | "info" | "success" | "warning" | "danger"> = {
  pending: "warning",
  quoted: "info",
  held: "info",
  on_request: "warning",
  sold_out: "danger",
  declined: "danger",
  expired: "neutral",
  released: "neutral",
  not_served: "danger",
  failed: "danger",
};

export function LinesPanel({
  tripId,
  view,
  suppliers,
  role,
  lang,
  locale,
  closed: closedProp = false,
}: {
  tripId: string;
  view: TripLines;
  suppliers: Supplier[];
  role: string;
  lang: Lang;
  locale: DateLocale;
  /** AA-957 S262 round 3: the trip is cancelled or being cancelled (the page's closed state wins once the run ends). */
  closed?: boolean;
}) {
  const router = useRouter();
  // A closed trip books nothing new (C12 §2.7): no AA item, no line, no confirm, no hold; a release and a line's cancel
  // stay (they undo), and money stays editable for a supplier's cancellation fee. The API answers 409 trip_cancelled.
  const closed = useTripClosed(closedProp);
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const margin = seesMargin(role);
  const own = suppliers.filter((s) => s.kind === "own");
  const [message, setMessage] = useState<{ tone: "danger" | "success"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState(false);
  const [addingAa, setAddingAa] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [money, setMoney] = useState<Partial<Record<MoneyKind, string>>>({});
  const [currency, setCurrency] = useState(view.base_currency);

  function refusal(detail: string, body: unknown): string {
    if (detail === "trip_cancelled") return tr("ledger.closed"); // S262 round 3: the page was out of date
    if (aaErrorKey(detail) !== "ledger.error.other") return tr(aaErrorKey(detail));
    const text = tr(errorKey(detail));
    if (detail !== "line_incomplete") return text;
    const missing = missingCodes(body).map((m) => tr(`ledger.missing.${m}`));
    return fmt(text, { missing: missing.join(", ") });
  }

  async function run(method: "POST" | "PATCH" | "PUT" | "DELETE", path: string, body?: unknown, ok?: (data: unknown) => string | null) {
    setBusy(true);
    setMessage(null);
    const res = await bffSend<unknown>(method, path, body);
    setBusy(false);
    if (!res.ok) {
      setMessage({ tone: "danger", text: refusal(res.detail, res.body) });
      return false;
    }
    const note = ok?.(res.data) ?? null;
    if (note) setMessage({ tone: "success", text: note });
    router.refresh();
    return true;
  }

  async function add(form: FormData) {
    const body = {
      agency_supplier_id: String(form.get("agency_supplier_id") ?? ""),
      title: String(form.get("title") ?? "").trim(),
      confirmation_no: String(form.get("confirmation_no") ?? "").trim() || null,
      service_from: String(form.get("service_from") ?? "") || null,
      service_to: String(form.get("service_to") ?? "") || null,
      collect: String(form.get("collect") ?? "agency"),
    };
    if (await run("POST", `trips/${tripId}/lines`, body)) setAdding(false);
  }

  async function saveMoney(line: Line) {
    const body = moneyBody(money, currency);
    if (!body) {
      setMessage({ tone: "danger", text: tr("ledger.error.money") });
      return;
    }
    if (await run("PUT", `lines/${line.booking_line_id}/money`, body)) {
      setEditing(null);
      setMoney({});
    }
  }

  const confirmNote = (data: unknown) =>
    (data as { owner_task?: unknown } | null)?.owner_task ? tr("ledger.ownerTask") : tr("ledger.confirmed");

  function aaStatus(line: Line) {
    const aa = line.aa;
    if (!aa) return null;
    // S262: a cancelled line shows no "Confirmed by AA" next to its Cancelled badge; the result sentence says why.
    const key = aaBadgeKey(aa, line.status);
    return (
      <span style={{ display: "grid", gap: 2, marginTop: 4 }} data-testid="aa-status">
        {key && <Badge tone={aa.pending ? "warning" : (AA_TONE[aa.sub_status ?? ""] ?? "neutral")}>{tr(key)}</Badge>}
        {aa.pending && <span style={{ color: K.muted, fontSize: 11.5 }}>{tr(`aa.ask.${aa.pending.kind}`)}</span>}
        {isRetrying(aa) && aa.pending && (
          <span style={{ color: K.muted, fontSize: 11.5 }} data-testid="aa-retrying">
            {fmt(tr("aa.retrying"), { at: formatDate(aa.pending.next_attempt_at.slice(0, 10), locale), n: aa.pending.attempts })}
          </span>
        )}
        {aa.result_code && !aa.pending && (
          <span style={{ color: K.muted, fontSize: 11.5 }} data-testid="aa-result" data-code={aa.result_code}>
            {tr(aaResultKey(aa.result_code))}
          </span>
        )}
      </span>
    );
  }

  function aaDetails(line: Line) {
    const aa = line.aa;
    if (!aa) return null;
    const pax = aa.pax ?? {};
    return (
      <span style={{ display: "grid", gap: 2, color: K.muted, fontSize: 11.5 }}>
        <span>{fmt(tr("aa.pax"), { adults: pax.adult ?? 0, children: pax.child ?? 0 })}</span>
        {aa.hold_expires_at && aa.sub_status === "held" && (
          <span>{fmt(tr("aa.heldUntil"), { date: formatDate(aa.hold_expires_at.slice(0, 10), locale) })}</span>
        )}
        {aa.no_longer_offered && <Badge tone="danger">{tr("aa.noLongerOffered")}</Badge>}
      </span>
    );
  }

  function amount(line: Line, kind: MoneyKind) {
    const m = line.money?.[kind];
    if (!m) return "—";
    return formatMoney(m.amount, m.currency, locale);
  }

  function fxCell(line: Line) {
    const m = line.money?.sell ?? line.money?.cost ?? null;
    const reading = fxReading(m);
    if (!m?.fx || !reading) return "—";
    return (
      <span style={{ display: "grid", gap: 2 }}>
        <span>{reading}</span>
        <span style={{ color: K.muted, fontSize: 11.5 }}>
          @ {formatDate(m.fx.rate_date, locale)}
          {m.fx.stale_days > 0 ? ` · ${plural(lang, "ledger.stale", m.fx.stale_days)}` : ""}
        </span>
        {m.fx.attribution && (
          <a href={m.fx.attribution.url} target="_blank" rel="noreferrer" style={{ fontSize: 11 }}>
            {m.fx.attribution.text}
          </a>
        )}
      </span>
    );
  }

  return (
    <Panel title={tr("ledger.tab")} testId="trip-bookings">
      <p style={{ margin: "0 0 10px", fontSize: 12.5, color: K.muted }}>
        {fmt(tr("ledger.levelHint"), { level: tr(`ledger.level.${view.booking_validation_level}`) })}
      </p>
      {message && (
        <p role="status" data-testid="ledger-message" style={{ margin: "0 0 10px", fontSize: 13, color: message.tone === "danger" ? K.danger : K.ink }}>
          {message.text}
        </p>
      )}
      {view.lines.length === 0 ? (
        <p style={{ margin: "0 0 10px", fontSize: 13 }}>{tr("ledger.empty")}</p>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={{ borderCollapse: "collapse", width: "100%", minWidth: 760 }} data-testid="ledger-lines">
            <thead>
              <tr>
                {["day", "item", "supplier", "conf", "status", ...(margin ? ["cost"] : []), "sell", "fx", "collect", "actions"].map((c) => (
                  <th key={c} style={head}>
                    {c === "actions" ? "" : tr(`ledger.col.${c}`)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {view.lines.map((line) => (
                <tr key={line.booking_line_id} data-testid={`line-${line.booking_line_id}`}>
                  <td style={cell}>{line.service_from ? formatDate(line.service_from, locale) : "—"}</td>
                  <td style={cell}>
                    {line.title}
                    {aaDetails(line)}
                  </td>
                  <td style={cell}>
                    {line.supplier_name}
                    {line.read_only && <div style={{ color: K.muted, fontSize: 11.5 }}>{tr("ledger.aaReadOnly")}</div>}
                  </td>
                  <td style={cell}>{line.confirmation_no ?? "—"}</td>
                  <td style={cell}>
                    <Badge tone={TONE[line.status]}>{tr(`ledger.status.${line.status}`)}</Badge>
                    {aaStatus(line)}
                    {line.commission && (
                      // AA-957: the commission follow-up of an agency line (owner / agent; A-12 acts on it).
                      <div style={{ marginTop: 4, fontSize: 11.5 }} data-testid="line-commission" data-status={line.commission.status}>
                        <Link href="/commissions" style={{ color: line.commission.overdue ? K.danger : K.muted }}>
                          {tr("commission.onLine")}: {tr(`commission.status.${line.commission.status}`)}
                        </Link>
                      </div>
                    )}
                    {line.money?.cancellation_cost && (
                      <div style={{ marginTop: 4, fontSize: 11.5, color: K.muted }} data-testid="line-cancellation-fee">
                        {fmt(tr("ledger.cancellationFee"), { amount: formatMoney(line.money.cancellation_cost.amount, line.money.cancellation_cost.currency, locale) })}
                      </div>
                    )}
                  </td>
                  {margin && <td style={cell}>{amount(line, "cost")}</td>}
                  <td style={cell}>
                    {line.money ? amount(line, "sell") : "—"}
                    {line.aa?.below_from_price && (
                      <div style={{ marginTop: 4 }} data-testid="aa-below-from">
                        <Badge tone="warning">{tr("aa.belowFrom")}</Badge>
                      </div>
                    )}
                  </td>
                  <td style={cell}>{fxCell(line)}</td>
                  <td style={cell}>{tr(`ledger.collect.${line.collect}`)}</td>
                  <td style={{ ...cell, whiteSpace: "nowrap" }}>
                    {allowedAaActions(line.aa?.actions ?? [], role).filter((action) => !closed || action === "release").map((action: AaAction) => (
                      <Button
                        key={action}
                        size="sm"
                        variant={action === "confirm" || action === "hold" ? "primary" : "ghost"}
                        disabled={!hydrated || busy}
                        onClick={() => run("POST", `lines/${line.booking_line_id}/aa/${action}`, undefined, () => tr("aa.asked"))}
                      >
                        {tr(`aa.action.${action}`)}
                      </Button>
                    ))}
                    {!line.read_only && line.status === "planning" && !closed && (
                      <Button size="sm" variant="primary" disabled={!hydrated || busy} onClick={() => run("POST", `lines/${line.booking_line_id}/confirm`, undefined, confirmNote)}>
                        {tr("ledger.confirm")}
                      </Button>
                    )}{" "}
                    {!line.read_only && margin && line.status !== "cancelled" && (
                      <Button size="sm" disabled={!hydrated || busy} onClick={() => setEditing(editing === line.booking_line_id ? null : line.booking_line_id)}>
                        {tr("ledger.editMoney")}
                      </Button>
                    )}{" "}
                    {!line.read_only && line.status !== "cancelled" && (
                      <Button size="sm" variant="ghost" disabled={!hydrated || busy} onClick={() => run("POST", `lines/${line.booking_line_id}/cancel`)}>
                        {tr("ledger.cancel")}
                      </Button>
                    )}{" "}
                    {!line.read_only && line.status === "planning" && role !== "assistant" && (
                      <Button size="sm" variant="danger" disabled={!hydrated || busy} onClick={() => run("DELETE", `lines/${line.booking_line_id}`)}>
                        {tr("ledger.delete")}
                      </Button>
                    )}
                    {editing === line.booking_line_id && (
                      <div style={{ display: "grid", gap: 6, marginTop: 8, maxWidth: 260 }} data-testid="ledger-money-form">
                        {(["cost", "sell", "commission_expected"] as const).map((kind) => (
                          <label key={kind} style={{ display: "grid", gap: 2, fontSize: 12 }}>
                            {tr(`ledger.money.${kind}`)}
                            <input
                              style={field}
                              inputMode="decimal"
                              value={money[kind] ?? line.money?.[kind]?.amount ?? ""}
                              onChange={(e) => setMoney({ ...money, [kind]: e.target.value })}
                            />
                          </label>
                        ))}
                        <label style={{ display: "grid", gap: 2, fontSize: 12 }}>
                          {tr("ledger.currency")}
                          <input style={field} maxLength={3} value={currency} onChange={(e) => setCurrency(e.target.value.toUpperCase())} />
                        </label>
                        <Button size="sm" variant="primary" disabled={!hydrated || busy} onClick={() => saveMoney(line)}>
                          {tr("ledger.save")}
                        </Button>
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {closed ? (
        <p data-testid="ledger-closed" style={{ margin: "12px 0 0", fontSize: 12.5, color: K.muted }}>
          {tr("ledger.closed")}
        </p>
      ) : (
      <div style={{ marginTop: 12, display: "grid", gap: 10 }}>
        {addingAa ? (
          <AaItemForm tripId={tripId} baseCurrency={view.base_currency} canSell={margin} lang={lang} locale={locale} onClose={() => setAddingAa(false)} />
        ) : (
          !adding && (
            <div>
              <Button size="sm" disabled={!hydrated} onClick={() => setAddingAa(true)}>
                {tr("aa.add.open")}
              </Button>
            </div>
          )
        )}
        {addingAa ? null : !adding ? (
          <div>
            <Button size="sm" disabled={!hydrated || own.length === 0} onClick={() => setAdding(true)}>
              {tr("ledger.add")}
            </Button>
          </div>
        ) : (
          <form action={add} style={{ display: "grid", gap: 8, maxWidth: 520 }} data-testid="ledger-add-form">
            <label style={{ display: "grid", gap: 2, fontSize: 12 }}>
              {tr("ledger.supplier")}
              <select name="agency_supplier_id" style={field} required>
                {own.map((s) => (
                  <option key={s.agency_supplier_id} value={s.agency_supplier_id}>
                    {s.name}
                  </option>
                ))}
              </select>
            </label>
            <label style={{ display: "grid", gap: 2, fontSize: 12 }}>
              {tr("ledger.col.item")}
              <input name="title" style={field} required maxLength={200} />
            </label>
            <label style={{ display: "grid", gap: 2, fontSize: 12 }}>
              {tr("ledger.col.conf")}
              <input name="confirmation_no" style={field} maxLength={80} />
            </label>
            <DateInput locale={locale} name="service_from" label={tr("ledger.from")} readAs={tr("settings.readAs")} invalid={tr("settings.invalidDate")} />
            <DateInput locale={locale} name="service_to" label={tr("ledger.to")} readAs={tr("settings.readAs")} invalid={tr("settings.invalidDate")} />
            <label style={{ display: "grid", gap: 2, fontSize: 12 }}>
              {tr("ledger.col.collect")}
              <select name="collect" style={field} defaultValue="agency">
                <option value="agency">{tr("ledger.collect.agency")}</option>
                <option value="on_arrival">{tr("ledger.collect.on_arrival")}</option>
              </select>
            </label>
            <div style={{ display: "flex", gap: 8 }}>
              <Button type="submit" size="sm" variant="primary" disabled={!hydrated || busy}>
                {tr("ledger.save")}
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setAdding(false)}>
                {tr("ledger.close")}
              </Button>
            </div>
          </form>
        )}
        {own.length === 0 && (
          <p style={{ margin: "8px 0 0", fontSize: 12.5, color: K.muted }}>
            <a href="/suppliers">{tr("ledger.addSupplierFirst")}</a>
          </p>
        )}
      </div>
      )}
    </Panel>
  );
}
