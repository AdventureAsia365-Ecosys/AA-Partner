"use client";
// web/components/ledger/LinesPanel.tsx — the trip workspace's Bookings tab (AA-944, card CRM-16; design v3 M6, R17,
// R18, R22, R25): Day | Item | Supplier | Conf no | Status | Cost | Sell | FX (rate @ date) | Collect. Cost, commission
// and the FX of cost are shown only when the API sent them (owner, agent: the assistant gets no money block). Actions:
// add a planning line, set the money (owner / agent), confirm (the agency's validation level decides; refusals are
// worded from their codes), cancel, delete a planning line. Adventure Asia lines are read-only (from the Booking feed).
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, K, useHydrated } from "@/app/_kit";
import { DateInput } from "@/components/DateInput";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
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

export function LinesPanel({
  tripId,
  view,
  suppliers,
  role,
  lang,
  locale,
}: {
  tripId: string;
  view: TripLines;
  suppliers: Supplier[];
  role: string;
  lang: Lang;
  locale: DateLocale;
}) {
  const router = useRouter();
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const margin = seesMargin(role);
  const own = suppliers.filter((s) => s.kind === "own");
  const [message, setMessage] = useState<{ tone: "danger" | "success"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [money, setMoney] = useState<Partial<Record<MoneyKind, string>>>({});
  const [currency, setCurrency] = useState(view.base_currency);

  function refusal(detail: string, body: unknown): string {
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
          {m.fx.stale_days > 0 ? ` · ${fmt(tr("ledger.stale"), { n: m.fx.stale_days })}` : ""}
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
                  <td style={cell}>{line.title}</td>
                  <td style={cell}>
                    {line.supplier_name}
                    {line.read_only && <div style={{ color: K.muted, fontSize: 11.5 }}>{tr("ledger.aaReadOnly")}</div>}
                  </td>
                  <td style={cell}>{line.confirmation_no ?? "—"}</td>
                  <td style={cell}>
                    <Badge tone={TONE[line.status]}>{tr(`ledger.status.${line.status}`)}</Badge>
                  </td>
                  {margin && <td style={cell}>{amount(line, "cost")}</td>}
                  <td style={cell}>{line.money ? amount(line, "sell") : "—"}</td>
                  <td style={cell}>{fxCell(line)}</td>
                  <td style={cell}>{tr(`ledger.collect.${line.collect}`)}</td>
                  <td style={{ ...cell, whiteSpace: "nowrap" }}>
                    {!line.read_only && line.status === "planning" && (
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
      <div style={{ marginTop: 12 }}>
        {!adding ? (
          <Button size="sm" disabled={!hydrated || own.length === 0} onClick={() => setAdding(true)}>
            {tr("ledger.add")}
          </Button>
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
    </Panel>
  );
}
