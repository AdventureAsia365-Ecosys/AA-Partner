"use client";
// web/components/proposals/BuilderPage.tsx — A-04, the day-list builder (AA-949, card CRM-21a; design WF-4, Fix E1,
// B10, A5/D1, R33, R38, §11.2). Header with the version and Publish vN; the totals bar (sell, and for owner / agent net
// and margin); what blocks Publish; the days on the left (Before you go first, reorder, add); the chosen day on the
// right with its zone and UTC offset, its option blocks (default, client may swap), each block's options with
// availability, sell and (owner / agent) net and the delta to the default, and "+ Add" (the AA / agency drawer); the
// rooms and the settings; the Advanced section (requires / excludes rules). Every write sends the working copy's
// edit_seq; a 409 version_conflict shows a banner (reload, or review the client's picks and publish anyway). The map
// is CRM-20 and "Preview as client" the client page of CRM-21b (AA-950).
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, K, useHydrated } from "@/app/_kit";
import { DateInput } from "@/components/DateInput";
import { Panel } from "@/components/PageTitle";
import { BeforeYouGoPanel } from "@/components/proposals/BeforeYouGoPanel";
import { BuilderAaDrawer, type Supplier } from "@/components/proposals/BuilderAaDrawer";
import { bffSend } from "@/lib/bffClient";
import {
  allOptions,
  BLOCK_TYPES,
  builderErrorKey,
  checkKey,
  marginPct,
  moneyInput,
  offsetLabel,
  optionLabel,
  refreshMessage,
  ROOM_TYPES,
  roomsBody,
  roomsSummary,
  versionConflict,
  type BlockType,
  type BuilderBlock,
  type BuilderOption,
  type BuilderView,
  type Room,
  type RoomType,
  type VersionConflict,
} from "@/lib/builder";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import { formatMoney, seesMargin } from "@/lib/ledger";
import type { Agreement } from "@/lib/proposals";

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
const label: React.CSSProperties = { display: "grid", gap: 2, fontSize: 12 };
const AVAIL_TONE = { instant: "success", on_request: "info", sold_out: "danger", agency: "neutral" } as const;
const STATUS_TONE = { draft: "neutral", published: "info", sent: "info", accepted: "success", superseded: "neutral", withdrawn: "danger" } as const;

type Method = "POST" | "PATCH" | "PUT" | "DELETE";

export function BuilderPage({
  initial,
  role,
  lang,
  locale,
  terms,
  suppliers,
}: {
  initial: BuilderView;
  role: string;
  lang: Lang;
  locale: DateLocale;
  terms: Agreement[];
  suppliers: Supplier[];
}) {
  const router = useRouter();
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [view, setView] = useState<BuilderView>(initial);
  const [dayId, setDayId] = useState<string>(initial.days.find((d) => d.day_no === 1)?.proposal_day_id ?? initial.days[0]?.proposal_day_id ?? "");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "danger" | "success"; text: string } | null>(null);
  const [conflict, setConflict] = useState<VersionConflict | null>(null);
  const [drawer, setDrawer] = useState<string | null>(null);
  const [advanced, setAdvanced] = useState(false);
  const [rooms, setRooms] = useState<Room[]>(initial.room_config);
  const draft = view.status === "draft";
  const canMoney = seesMargin(role);
  const canPublish = role === "owner" || role === "agent";
  const money = (v: string | null | undefined) => formatMoney(v, view.currency, locale);
  const day = view.days.find((d) => d.proposal_day_id === dayId) ?? view.days[0];
  const roomWords = Object.fromEntries(ROOM_TYPES.map((r) => [r, tr(`builder.room.${r}`)])) as Record<RoomType, string>;

  async function reload() {
    const res = await bffSend<BuilderView>("GET", `proposals/${view.proposal_id}/builder`);
    if (res.ok) {
      setView(res.data);
      setRooms(res.data.room_config);
      setConflict(null);
    }
  }

  /** One builder write: adds the seq the page read; on success the answer is the whole page. */
  async function write(method: Method, path: string, body: Record<string, unknown> = {}): Promise<boolean> {
    return (await send(method, path, body)) !== null;
  }

  /** As write, answering the new page (null when refused: the message or the conflict banner is shown). */
  async function send(method: Method, path: string, body: Record<string, unknown> = {}): Promise<BuilderView | null> {
    setBusy(true);
    setMessage(null);
    const res =
      method === "DELETE"
        ? await bffSend<BuilderView>("DELETE", `${path}?seq=${view.edit_seq}`)
        : await bffSend<BuilderView>(method, path, { seq: view.edit_seq, ...body });
    setBusy(false);
    if (res.ok) {
      setView(res.data);
      setRooms(res.data.room_config);
      return res.data;
    }
    const vc = versionConflict(res.status, res.detail, res.body);
    if (vc) {
      setConflict(vc);
      return null;
    }
    let text = tr(builderErrorKey(res.detail));
    const violations = (res.body as { violations?: { option_id: string; kind: string; target_option_id: string }[] })?.violations;
    if (res.detail === "option_conflict" && violations?.length) {
      const dayWord = tr("builder.day");
      text += ` ${violations.map((v) => `${optionLabel(view, v.option_id, dayWord)} ${tr(`builder.rule.${v.kind}`)} ${optionLabel(view, v.target_option_id, dayWord)}`).join("; ")}`;
    }
    setMessage({ tone: "danger", text });
    return null;
  }

  async function publish(acknowledge?: number) {
    const ok = await write("POST", `proposals/${view.proposal_id}/publish`, acknowledge === undefined ? {} : { acknowledge_selection_seq: acknowledge });
    if (ok) {
      setConflict(null);
      setMessage({ tone: "success", text: fmt(tr("builder.published"), { n: view.version }) });
      router.refresh();
    }
  }

  /** Refresh AA prices: the oldest quotes first, 40 a run; says how many were checked, left or refused. */
  async function refreshAa() {
    const r = (await send("POST", `proposals/${view.proposal_id}/aa-refresh`))?.refresh;
    if (r) setMessage(refreshMessage(r, tr));
  }

  async function newVersion() {
    setBusy(true);
    const res = await bffSend<BuilderView>("POST", `trips/${view.trip_id}/builder`, { from_proposal_id: view.proposal_id });
    setBusy(false);
    if (res.ok) {
      router.push(`/pipeline/${view.trip_id}/proposal/${res.data.proposal_id}`);
      return;
    }
    const existing = (res.body as { proposal_id?: string })?.proposal_id;
    if (res.detail === "builder_draft_exists" && existing) {
      router.push(`/pipeline/${view.trip_id}/proposal/${existing}`);
      return;
    }
    setMessage({ tone: "danger", text: tr(builderErrorKey(res.detail)) });
  }

  async function saveSettings(form: HTMLFormElement) {
    const data = new FormData(form);
    const body: Record<string, unknown> = {
      price_visibility: String(data.get("price_visibility")),
      intro: String(data.get("intro") ?? ""),
      starts_on: String(data.get("starts_on") ?? "") || null,
      balance_due_on: String(data.get("balance_due_on") ?? "") || null,
      terms_doc_id: String(data.get("terms_doc_id") ?? "") || null,
    };
    if (canMoney) body.deposit_pct = String(data.get("deposit_pct") ?? "");
    if (await write("PATCH", `proposals/${view.proposal_id}/builder`, body)) setMessage({ tone: "success", text: tr("builder.saved") });
  }

  async function saveRooms() {
    const built = roomsBody(rooms);
    if (!built.ok) {
      setMessage({ tone: "danger", text: tr(built.key) });
      return;
    }
    if (await write("PATCH", `proposals/${view.proposal_id}/builder`, { room_config: built.rooms })) setMessage({ tone: "success", text: tr("builder.saved") });
  }

  async function saveDay(form: HTMLFormElement) {
    const data = new FormData(form);
    await write("PATCH", `proposals/${view.proposal_id}/days/${day.proposal_day_id}`, {
      title: String(data.get("title") ?? "") || null,
      place: String(data.get("place") ?? "") || null,
      tz: String(data.get("tz") ?? ""),
      notes: String(data.get("notes") ?? "") || null,
    });
  }

  async function moveDay(id: string, by: -1 | 1) {
    const ids = view.days.filter((d) => d.day_no >= 1).map((d) => d.proposal_day_id);
    const at = ids.indexOf(id);
    const to = at + by;
    if (at < 0 || to < 0 || to >= ids.length) return;
    [ids[at], ids[to]] = [ids[to], ids[at]];
    await write("PUT", `proposals/${view.proposal_id}/days/order`, { day_ids: ids });
  }

  async function addBlock(form: HTMLFormElement) {
    const data = new FormData(form);
    const title = String(data.get("title") ?? "").trim();
    if (!title) return;
    if (await write("POST", `proposals/${view.proposal_id}/days/${day.proposal_day_id}/blocks`, { title, type: String(data.get("type")) as BlockType })) form.reset();
  }

  async function addRule(form: HTMLFormElement) {
    const data = new FormData(form);
    await write("POST", `proposals/${view.proposal_id}/rules`, {
      kind: String(data.get("kind")),
      option_id: String(data.get("option_id")),
      target_option_id: String(data.get("target_option_id")),
    });
  }

  async function editSell(o: BuilderOption) {
    const typed = window.prompt(fmt(tr("builder.option.sellFor"), { currency: view.currency }), o.sell_amount);
    if (typed === null) return;
    const value = moneyInput(typed);
    if (value === undefined || value === null) {
      setMessage({ tone: "danger", text: tr("builder.err.generic") });
      return;
    }
    await write("PATCH", `proposals/${view.proposal_id}/options/${o.option_id}`, { sell_amount: value });
  }

  function optionRow(block: BuilderBlock, o: BuilderOption) {
    return (
      <li key={o.option_id} data-testid="builder-option" data-default={o.is_default ? "true" : undefined} style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", fontSize: 13, padding: "4px 0", borderTop: `1px solid ${K.line}` }}>
        <input
          type="radio"
          name={`default-${block.option_block_id}`}
          aria-label={tr("builder.option.default")}
          checked={o.is_default}
          disabled={!draft || !hydrated || busy || o.availability === "sold_out"}
          onChange={() => write("PATCH", `proposals/${view.proposal_id}/blocks/${block.option_block_id}`, { default_option_id: o.option_id })}
        />
        <span style={{ flex: "1 1 160px", color: K.ink, overflowWrap: "anywhere" }}>
          {o.title}
          {o.source === "aa" && <span style={{ color: K.muted }}> · AA</span>}
          {o.aa?.nights ? <span style={{ color: K.muted }}> · {fmt(tr("builder.option.nights"), { n: o.aa.nights })}</span> : null}
          {o.tier && <span style={{ color: K.muted }}> · {tr(`builder.tier.${o.tier}`)}</span>}
        </span>
        <Badge tone={AVAIL_TONE[o.availability]}>{tr(`builder.avail.${o.availability}`)}</Badge>
        {o.pay_on_arrival && <Badge tone="purple">{tr("builder.option.payOnArrival")}</Badge>}
        {o.choose_later_allowed && <Badge tone="neutral">{tr("builder.option.chooseLater")}</Badge>}
        {o.aa?.below_from_price && <Badge tone="warning">{tr("aa.belowFrom")}</Badge>}
        {o.aa?.quote_expired && <Badge tone="warning">{tr("builder.check.aa_prices_stale")}</Badge>}
        {o.aa?.unpriced && <Badge tone="danger">{tr("builder.option.unpriced")}</Badge>}
        {canMoney && o.net_amount !== undefined && (
          <span style={{ color: K.muted }} data-testid="builder-option-net">
            {fmt(tr("builder.option.net"), { amount: money(o.net_amount) })}
          </span>
        )}
        <span data-testid="builder-option-sell">{fmt(tr("builder.option.sell"), { amount: money(o.sell_amount) })}</span>
        {!o.is_default && o.sell_delta && <span style={{ color: K.muted }}>({Number(o.sell_delta) >= 0 ? "+" : ""}{money(o.sell_delta)})</span>}
        {draft && (
          <span style={{ display: "flex", gap: 4 }}>
            {canMoney && (
              <Button size="sm" variant="ghost" disabled={!hydrated || busy} onClick={() => editSell(o)}>
                {tr("builder.option.editSell")}
              </Button>
            )}
            <Button size="sm" variant="ghost" disabled={!hydrated || busy} onClick={() => write("DELETE", `proposals/${view.proposal_id}/options/${o.option_id}`)}>
              {tr("builder.delete")}
            </Button>
          </span>
        )}
      </li>
    );
  }

  const tripDays = view.days.filter((d) => d.day_no >= 1);
  const hasAa = allOptions(view).some((x) => x.option.source === "aa");
  const pct = marginPct(view.totals.sell_total, view.totals.margin_total);

  return (
    <div style={{ display: "grid", gap: 16, minWidth: 0 }} data-testid="builder" data-hydrated={hydrated ? "true" : undefined} data-status={view.status} data-seq={view.edit_seq}>
      <Panel testId="builder-header">
        <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <Link href={`/pipeline/${view.trip_id}`} style={{ fontSize: 12.5, color: K.accent }}>
            {tr("builder.backToTrip")}
          </Link>
          <strong style={{ fontSize: 15, color: K.ink }}>{fmt(tr(draft ? "builder.workingCopy" : "builder.versionN"), { n: view.version })}</strong>
          <Badge tone={STATUS_TONE[view.status]}>{tr(`builder.status.${view.status}`)}</Badge>
          {view.published_at && <span style={{ fontSize: 12.5, color: K.muted }}>{fmt(tr("builder.publishedOn"), { date: formatDate(view.published_at.slice(0, 10), locale) })}</span>}
          <span style={{ flex: 1 }} />
          {draft && hasAa && (
            // The kit Button (a byte copy of CIS app/_kit) forwards no data-* props: the test id sits on a wrapper, as
            // show-email in TripPanel. E2E run 40262333536 waited 545 s for a data-testid the Button had dropped.
            <span data-testid="builder-aa-refresh">
              <Button size="sm" disabled={!hydrated || busy} onClick={refreshAa}>
                {tr("builder.aa.refresh")}
              </Button>
            </span>
          )}
          {draft && canPublish && (
            <Button variant="primary" size="sm" disabled={!hydrated || busy || !view.checks.publishable} onClick={() => publish()}>
              {fmt(tr("builder.publish"), { n: view.version })}
            </Button>
          )}
          {!draft && ["published", "sent", "accepted"].includes(view.status) && (
            // AA-951 (CRM-22, A-05): this version as e-mail or WhatsApp text, with its price hash.
            <Link href={`/pipeline/${view.trip_id}/proposal/${view.proposal_id}/text`} style={{ fontSize: 12.5, color: K.accent }} data-testid="builder-send-text">
              {tr("text.open")}
            </Link>
          )}
          {!draft && view.status !== "accepted" && (
            <Button size="sm" disabled={!hydrated || busy} onClick={newVersion}>
              {tr("builder.newVersion")}
            </Button>
          )}
        </div>
        <p style={{ margin: "10px 0 0", fontSize: 13, color: K.ink, display: "flex", gap: 14, flexWrap: "wrap" }} data-testid="builder-totals">
          <span>{fmt(tr("builder.totals.sell"), { amount: money(view.totals.sell_total) })}</span>
          {canMoney && view.totals.net_total !== undefined && <span>{fmt(tr("builder.totals.net"), { amount: money(view.totals.net_total) })}</span>}
          {canMoney && view.totals.margin_total !== undefined && (
            <span>
              {fmt(tr("builder.totals.margin"), { amount: money(view.totals.margin_total) })}
              {pct && ` (${pct}%)`}
            </span>
          )}
          <span>{fmt(tr("builder.totals.payOnArrival"), { amount: money(view.totals.pay_on_arrival_total) })}</span>
          <span>{fmt(tr("builder.totals.deposit"), { amount: money(view.totals.deposit) })}</span>
          <span style={{ color: K.muted }}>{view.currency}</span>
        </p>
        {(view.checks.blocking.length > 0 || view.checks.warnings.length > 0) && (
          <ul style={{ margin: "8px 0 0", padding: 0, listStyle: "none", display: "flex", gap: 6, flexWrap: "wrap" }} data-testid="builder-checks">
            {view.checks.blocking.map((c) => (
              <li key={c}>
                <Badge tone="danger">{tr(checkKey(c))}</Badge>
              </li>
            ))}
            {view.checks.warnings.map((c) => (
              <li key={c}>
                <Badge tone="warning">{tr(checkKey(c))}</Badge>
              </li>
            ))}
          </ul>
        )}
        {view.based_on && view.based_on.client_picks_since_copy > 0 && draft && (
          <p role="status" style={{ margin: "8px 0 0", fontSize: 13, color: K.amber }} data-testid="builder-picks-since">
            {fmt(tr("builder.picksSince"), { n: view.based_on.client_picks_since_copy, v: view.based_on.version })}
          </p>
        )}
        {conflict && (
          <div role="alert" data-testid="builder-conflict" data-reason={conflict.reason} style={{ marginTop: 10, padding: 10, borderRadius: 8, background: K.bg, display: "grid", gap: 6 }}>
            <strong style={{ fontSize: 13 }}>{tr(`builder.conflict.${conflict.reason}`)}</strong>
            {conflict.picks && conflict.picks.length > 0 && (
              <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12.5 }}>
                {conflict.picks.map((p, i) => (
                  <li key={i}>
                    {p.block}: {p.option}
                  </li>
                ))}
              </ul>
            )}
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <Button size="sm" onClick={reload}>
                {tr("builder.conflict.reload")}
              </Button>
              {conflict.reason === "client_selected" && conflict.selection_seq !== undefined && canPublish && (
                <Button size="sm" variant="primary" disabled={!hydrated || busy} onClick={() => publish(conflict.selection_seq)}>
                  {tr("builder.conflict.publishAnyway")}
                </Button>
              )}
            </div>
          </div>
        )}
        {message && (
          <p role="status" data-testid="builder-message" style={{ margin: "8px 0 0", fontSize: 13, color: message.tone === "danger" ? K.danger : K.success }}>
            {message.text}
          </p>
        )}
      </Panel>

      <div style={{ display: "grid", gap: 16, gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 260px), 1fr))", alignItems: "start" }}>
        <Panel title={tr("builder.days")} testId="builder-days">
          <ol style={{ margin: 0, padding: 0, listStyle: "none", display: "grid", gap: 4 }}>
            {view.days.map((d) => (
              <li key={d.proposal_day_id} style={{ display: "flex", gap: 6, alignItems: "center" }}>
                <button
                  type="button"
                  onClick={() => setDayId(d.proposal_day_id)}
                  data-testid="builder-day"
                  data-day-no={d.day_no}
                  aria-current={d.proposal_day_id === day?.proposal_day_id ? "true" : undefined}
                  style={{ flex: 1, textAlign: "left", font: "inherit", fontSize: 13, padding: "6px 8px", borderRadius: 8, border: `1px solid ${d.proposal_day_id === day?.proposal_day_id ? K.accent : K.line}`, background: K.card, color: K.ink, cursor: "pointer", minWidth: 0 }}
                >
                  {d.before_you_go ? tr("builder.beforeYouGo") : `${fmt(tr("builder.dayN"), { n: d.day_no })}${d.title ? ` · ${d.title}` : ""}`}
                  {d.date && <span style={{ color: K.muted }}> · {formatDate(d.date, locale)}</span>}
                </button>
                {draft && d.day_no >= 1 && (
                  <>
                    <Button size="sm" variant="ghost" ariaLabel={tr("builder.moveUp")} disabled={!hydrated || busy || d.day_no === 1} onClick={() => moveDay(d.proposal_day_id, -1)}>
                      ↑
                    </Button>
                    <Button size="sm" variant="ghost" ariaLabel={tr("builder.moveDown")} disabled={!hydrated || busy || d.day_no === tripDays.length} onClick={() => moveDay(d.proposal_day_id, 1)}>
                      ↓
                    </Button>
                  </>
                )}
              </li>
            ))}
          </ol>
          {draft && (
            <div style={{ marginTop: 8 }}>
              <Button size="sm" disabled={!hydrated || busy} onClick={() => write("POST", `proposals/${view.proposal_id}/days`, {})}>
                {tr("builder.addDay")}
              </Button>
            </div>
          )}
          <p style={{ margin: "10px 0 0", fontSize: 12.5, color: K.muted }} data-testid="builder-rooms-line">
            {fmt(tr("builder.rooms.line"), { rooms: roomsSummary(view.room_config, roomWords) })}
          </p>
          {view.stage2.accommodation_tier && (
            <p style={{ margin: "4px 0 0", fontSize: 12.5, color: K.muted }}>{fmt(tr("builder.stage2Tier"), { tier: tr(`builder.tier.${view.stage2.accommodation_tier}`) })}</p>
          )}
        </Panel>

        {day && (
          <Panel testId="builder-day-detail">
            <div style={{ display: "flex", gap: 8, alignItems: "baseline", flexWrap: "wrap" }}>
              <strong style={{ fontSize: 14, color: K.ink }}>{day.before_you_go ? tr("builder.beforeYouGo") : fmt(tr("builder.dayN"), { n: day.day_no })}</strong>
              {day.date && <span style={{ fontSize: 12.5, color: K.muted }}>{formatDate(day.date, locale)}</span>}
              <span style={{ fontSize: 12.5, color: K.muted }} data-testid="builder-day-offset">
                {day.tz} ({offsetLabel(day.utc_offset)})
              </span>
            </div>
            {day.before_you_go && <p style={{ margin: "6px 0 0", fontSize: 12.5, color: K.muted }}>{tr("builder.beforeYouGoHint")}</p>}
            {day.before_you_go && view.before_you_go && (
              // AA-951 (CRM-22, R38): the packs of this version; keyed on the counter so a write resets the ticks.
              <BeforeYouGoPanel
                key={view.edit_seq}
                data={view.before_you_go}
                draft={draft}
                busy={busy}
                lang={lang}
                onSave={(groupKeys) => write("PUT", `proposals/${view.proposal_id}/before-you-go`, { group_keys: groupKeys })}
              />
            )}
            {draft ? (
              <form key={`${day.proposal_day_id}-${view.edit_seq}`} onSubmit={(e) => { e.preventDefault(); void saveDay(e.currentTarget); }} style={{ display: "grid", gap: 8, marginTop: 10 }} data-testid="builder-day-form">
                <div style={{ display: "grid", gap: 8, gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))" }}>
                  <label style={label}>
                    {tr("builder.day.title")}
                    <input name="title" style={field} defaultValue={day.title ?? ""} maxLength={120} />
                  </label>
                  <label style={label}>
                    {tr("builder.day.place")}
                    <input name="place" style={field} defaultValue={day.place ?? ""} maxLength={120} />
                  </label>
                  <label style={label}>
                    {tr("builder.day.tz")}
                    <input name="tz" style={field} defaultValue={day.tz} maxLength={64} data-testid="builder-day-tz" />
                  </label>
                </div>
                <label style={label}>
                  {tr("builder.day.notes")}
                  <textarea name="notes" rows={3} style={{ ...field, width: "100%", resize: "vertical" }} defaultValue={day.notes ?? ""} maxLength={4000} />
                </label>
                <div style={{ display: "flex", gap: 8 }}>
                  <Button type="submit" size="sm" disabled={!hydrated || busy}>
                    {tr("builder.save")}
                  </Button>
                  {!day.before_you_go && (
                    <Button size="sm" variant="ghost" disabled={!hydrated || busy || tripDays.length <= 1} onClick={() => write("DELETE", `proposals/${view.proposal_id}/days/${day.proposal_day_id}`)}>
                      {tr("builder.deleteDay")}
                    </Button>
                  )}
                </div>
              </form>
            ) : (
              day.notes && <p style={{ margin: "8px 0 0", fontSize: 13, whiteSpace: "pre-wrap" }}>{day.notes}</p>
            )}

            <div style={{ display: "grid", gap: 12, marginTop: 14 }}>
              {day.blocks.map((block) => (
                <section key={block.option_block_id} data-testid="builder-block" data-type={block.type} style={{ border: `1px solid ${K.line}`, borderRadius: 8, padding: 10, display: "grid", gap: 6 }}>
                  <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                    <strong style={{ fontSize: 13, color: K.ink }}>{block.title}</strong>
                    <span style={{ fontSize: 12, color: K.muted }}>{tr(`builder.blockType.${block.type}`)}</span>
                    <span style={{ flex: 1 }} />
                    <label style={{ display: "flex", gap: 6, alignItems: "center", fontSize: 12.5 }}>
                      <input
                        type="checkbox"
                        checked={block.client_may_swap}
                        disabled={!draft || !hydrated || busy}
                        onChange={(e) => write("PATCH", `proposals/${view.proposal_id}/blocks/${block.option_block_id}`, { client_may_swap: e.target.checked })}
                        data-testid="builder-may-swap"
                      />
                      {tr("builder.block.maySwap")}
                    </label>
                    {draft && (
                      <Button size="sm" variant="ghost" disabled={!hydrated || busy} onClick={() => write("DELETE", `proposals/${view.proposal_id}/blocks/${block.option_block_id}`)}>
                        {tr("builder.delete")}
                      </Button>
                    )}
                  </div>
                  {block.options.length === 0 && <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("builder.block.empty")}</p>}
                  <ul style={{ margin: 0, padding: 0, listStyle: "none" }}>{block.options.map((o) => optionRow(block, o))}</ul>
                  {draft && drawer !== block.option_block_id && (
                    <div>
                      <Button size="sm" disabled={!hydrated || busy} onClick={() => setDrawer(block.option_block_id)}>
                        {tr("builder.add")}
                      </Button>
                    </div>
                  )}
                  {draft && drawer === block.option_block_id && (
                    <BuilderAaDrawer view={view} blockId={block.option_block_id} canMoney={canMoney} suppliers={suppliers} lang={lang} locale={locale} write={write} onClose={() => setDrawer(null)} />
                  )}
                </section>
              ))}
              {draft && (
                <form onSubmit={(e) => { e.preventDefault(); void addBlock(e.currentTarget); }} style={{ display: "flex", gap: 8, alignItems: "end", flexWrap: "wrap" }} data-testid="builder-add-block">
                  <label style={{ ...label, flex: "1 1 160px" }}>
                    {tr("builder.block.title")}
                    <input name="title" style={field} maxLength={120} />
                  </label>
                  <label style={label}>
                    {tr("builder.block.type")}
                    <select name="type" style={field} defaultValue="activity">
                      {BLOCK_TYPES.map((b) => (
                        <option key={b} value={b}>
                          {tr(`builder.blockType.${b}`)}
                        </option>
                      ))}
                    </select>
                  </label>
                  <Button type="submit" size="sm" disabled={!hydrated || busy}>
                    {tr("builder.addBlock")}
                  </Button>
                </form>
              )}
            </div>
          </Panel>
        )}
      </div>

      <Panel title={tr("builder.settings")} testId="builder-settings">
        {draft ? (
          <form key={`settings-${view.edit_seq}`} onSubmit={(e) => { e.preventDefault(); void saveSettings(e.currentTarget); }} style={{ display: "grid", gap: 8 }}>
            <div style={{ display: "grid", gap: 8, gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))" }}>
              <label style={label}>
                {tr("builder.visibility")}
                <select name="price_visibility" style={field} defaultValue={view.price_visibility} data-testid="builder-visibility">
                  <option value="total_with_deltas">{tr("builder.visibility.total_with_deltas")}</option>
                  <option value="total_only">{tr("builder.visibility.total_only")}</option>
                </select>
              </label>
              {canMoney && (
                <label style={label}>
                  {tr("builder.depositPct")}
                  <input name="deposit_pct" style={field} inputMode="decimal" defaultValue={view.deposit_pct ?? "30"} />
                </label>
              )}
              <label style={label}>
                {tr("proposal.terms")}
                {/* With published booking terms a version always carries them (publish pins them, S261): "No terms"
                    is offered only when the agency has none. */}
                <select name="terms_doc_id" style={field} defaultValue={view.terms_doc_id ?? terms[0]?.doc_id ?? ""}>
                  {terms.length === 0 && <option value="">{tr("proposal.noTerms")}</option>}
                  {terms.map((d) => (
                    <option key={d.doc_id} value={d.doc_id}>
                      {d.version_label}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <div style={{ display: "grid", gap: 8, gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))" }}>
              <DateInput locale={locale} name="starts_on" label={tr("builder.startsOn")} readAs={tr("settings.readAs")} invalid={tr("settings.invalidDate")} defaultValue={view.starts_on ?? ""} />
              <DateInput locale={locale} name="balance_due_on" label={tr("proposal.balanceDueLabel")} readAs={tr("settings.readAs")} invalid={tr("settings.invalidDate")} defaultValue={view.balance_due_on ?? ""} />
            </div>
            <label style={label}>
              {tr("builder.intro")}
              <textarea name="intro" rows={3} style={{ ...field, width: "100%", resize: "vertical" }} defaultValue={view.intro} maxLength={20000} />
            </label>
            <div>
              <Button type="submit" size="sm" disabled={!hydrated || busy}>
                {tr("builder.save")}
              </Button>
            </div>
          </form>
        ) : (
          <p style={{ margin: 0, fontSize: 13 }}>{tr(`builder.visibility.${view.price_visibility}`)}</p>
        )}

        <div style={{ marginTop: 14, display: "grid", gap: 6 }} data-testid="builder-rooms">
          <strong style={{ fontSize: 13 }}>{tr("builder.rooms")}</strong>
          <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("builder.rooms.hint")}</p>
          {rooms.map((r, i) => (
            <div key={i} style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "end" }}>
              <label style={label}>
                {tr("builder.rooms.type")}
                <select style={field} value={r.type} disabled={!draft} onChange={(e) => setRooms(rooms.map((x, j) => (j === i ? { ...x, type: e.target.value as RoomType } : x)))}>
                  {ROOM_TYPES.map((x) => (
                    <option key={x} value={x}>
                      {roomWords[x]}
                    </option>
                  ))}
                </select>
              </label>
              {(["adults", "children"] as const).map((k) => (
                <label key={k} style={label}>
                  {tr(`builder.rooms.${k}`)}
                  <input style={{ ...field, width: 70 }} inputMode="numeric" disabled={!draft} value={r[k]} onChange={(e) => setRooms(rooms.map((x, j) => (j === i ? { ...x, [k]: Number(e.target.value) || 0 } : x)))} />
                </label>
              ))}
              {draft && rooms.length > 1 && (
                <Button size="sm" variant="ghost" onClick={() => setRooms(rooms.filter((_, j) => j !== i))}>
                  {tr("builder.delete")}
                </Button>
              )}
            </div>
          ))}
          {draft && (
            <div style={{ display: "flex", gap: 8 }}>
              <Button size="sm" variant="ghost" onClick={() => setRooms([...rooms, { type: "double", adults: 2, children: 0 }])}>
                {tr("builder.rooms.add")}
              </Button>
              <Button size="sm" disabled={!hydrated || busy} onClick={saveRooms}>
                {tr("builder.rooms.save")}
              </Button>
            </div>
          )}
        </div>
      </Panel>

      <Panel testId="builder-advanced">
        <button type="button" onClick={() => setAdvanced(!advanced)} aria-expanded={advanced} style={{ font: "inherit", fontSize: 13, fontWeight: 600, background: "none", border: 0, padding: 0, color: K.ink, cursor: "pointer" }}>
          {advanced ? "▾" : "▸"} {tr("builder.advanced")}
        </button>
        {advanced && (
          <div style={{ display: "grid", gap: 8, marginTop: 8 }}>
            <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("builder.advanced.hint")}</p>
            {view.rules.length === 0 && <p style={{ margin: 0, fontSize: 12.5 }}>{tr("builder.rules.none")}</p>}
            <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "grid", gap: 4 }} data-testid="builder-rules">
              {view.rules.map((r) => (
                <li key={r.option_rule_id} style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 12.5, flexWrap: "wrap" }}>
                  <span>
                    {optionLabel(view, r.option_id, tr("builder.day"))} <strong>{tr(`builder.rule.${r.kind}`)}</strong> {optionLabel(view, r.target_option_id, tr("builder.day"))}
                  </span>
                  {draft && (
                    <Button size="sm" variant="ghost" disabled={!hydrated || busy} onClick={() => write("DELETE", `proposals/${view.proposal_id}/rules/${r.option_rule_id}`)}>
                      {tr("builder.delete")}
                    </Button>
                  )}
                </li>
              ))}
            </ul>
            {draft && allOptions(view).length >= 2 && (
              <form onSubmit={(e) => { e.preventDefault(); void addRule(e.currentTarget); }} style={{ display: "grid", gap: 6, gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", alignItems: "end" }} data-testid="builder-add-rule">
                <label style={label}>
                  {tr("builder.rule.when")}
                  <select name="option_id" style={field}>
                    {allOptions(view).map(({ option }) => (
                      <option key={option.option_id} value={option.option_id}>
                        {optionLabel(view, option.option_id, tr("builder.day"))}
                      </option>
                    ))}
                  </select>
                </label>
                <label style={label}>
                  {tr("builder.rule.kind")}
                  <select name="kind" style={field}>
                    <option value="excludes">{tr("builder.rule.excludes")}</option>
                    <option value="requires">{tr("builder.rule.requires")}</option>
                  </select>
                </label>
                <label style={label}>
                  {tr("builder.rule.then")}
                  <select name="target_option_id" style={field}>
                    {allOptions(view).map(({ option }) => (
                      <option key={option.option_id} value={option.option_id}>
                        {optionLabel(view, option.option_id, tr("builder.day"))}
                      </option>
                    ))}
                  </select>
                </label>
                <Button type="submit" size="sm" disabled={!hydrated || busy}>
                  {tr("builder.rule.add")}
                </Button>
              </form>
            )}
            {draft && (
              <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "grid", gap: 4 }}>
                {allOptions(view)
                  .filter(({ option }) => option.source === "agency")
                  .map(({ option, day: d }) => (
                    <li key={option.option_id}>
                      <label style={{ display: "flex", gap: 6, fontSize: 12.5, alignItems: "center" }}>
                        <input
                          type="checkbox"
                          checked={option.choose_later_allowed}
                          disabled={!hydrated || busy}
                          onChange={(e) => write("PATCH", `proposals/${view.proposal_id}/options/${option.option_id}`, { choose_later_allowed: e.target.checked })}
                        />
                        {fmt(tr("builder.chooseLaterFor"), { option: `${fmt(tr("builder.dayN"), { n: d.day_no })} · ${option.title}` })}
                      </label>
                    </li>
                  ))}
              </ul>
            )}
          </div>
        )}
      </Panel>
    </div>
  );
}
