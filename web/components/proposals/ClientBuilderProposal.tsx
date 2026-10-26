"use client";
// web/components/proposals/ClientBuilderProposal.tsx — the client's day-by-day proposal (AA-950, card CRM-21b; design
// v3 C-03 / C-04 / C-12, WF-6 / WF-7, §2.9): phone first (large Keep / Swap buttons, one column at 390 px).
//
// - C-03: agency, dates, "Prepared by"; the notice of a new version (one bundled list of what changed); per day its
//   date and UTC offset, every choice with "Included" or the sell difference (Decision D1; nothing when the agency
//   shows the total only), the confirmation class, sold-out / rule-greyed options with the reason, "Choose later" on
//   agency items only (Fix B9); the total with the pay-on-arrival note, the deposit and the balance apart; "Ask for a
//   change" (R19: a reason, the agent gets the bundled choice diff).
// - C-04: "Check availability" places the version's hold set (Fix A3); the lines grouped by confirmation class (R26);
//   the rooms confirmed, not chosen (Fix B10); the terms, the full name; "Your places are held until …" beside Accept.
// - C-12: the hold expired -> "Check availability again" (3 a day), then "Ask your agent" (card R).
//
// Every call goes to /crm/public/v1/proposal on this origin with the host-only link cookie (same-origin credentials);
// every hold answer carries the fresh page (a sold-out option has already fallen back to the included one, card E).
import { useState } from "react";
import { K } from "@/app/_kit";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt, type MessageKey } from "@/lib/i18n";
import {
  acceptOpen,
  CHANGE_REASONS,
  deltaLabel,
  holdAction,
  holdKey,
  interactiveErrorKey,
  noticeLine,
  PROPOSAL_API,
  zoneLabel,
  type ChangeReason,
  type ClientBlock,
  type ClientBuilderPage,
  type ClientOption,
} from "@/lib/clientProposal";
import { clientErrorKey, formatAmount } from "@/lib/proposals";

const input: React.CSSProperties = {
  font: "inherit",
  fontSize: 15,
  padding: "10px 12px",
  borderRadius: 10,
  border: `1px solid ${K.line}`,
  background: K.card,
  color: K.ink,
  width: "100%",
  boxSizing: "border-box",
};
const bigButton: React.CSSProperties = {
  padding: "12px 16px",
  minHeight: 44,
  borderRadius: 999,
  border: "none",
  background: K.accent,
  color: "var(--aa-on-accent)",
  fontSize: 15,
  fontWeight: 600,
  cursor: "pointer",
  width: "100%",
};
const quietButton: React.CSSProperties = {
  ...bigButton,
  background: "transparent",
  color: K.accent,
  border: `1px solid ${K.accent}`,
  fontWeight: 500,
};
const card: React.CSSProperties = { display: "grid", gap: 8, padding: 12, borderRadius: 12, border: `1px solid ${K.line}`, background: K.card, minWidth: 0 };

type Answer = { status: number; body: Record<string, unknown> };

async function call(path: string, body?: unknown): Promise<Answer> {
  try {
    const res = await fetch(`${PROPOSAL_API}${path}`, {
      method: body === undefined && path === "" ? "GET" : "POST",
      headers: body === undefined ? { Accept: "application/json" } : { "Content-Type": "application/json", Accept: "application/json" },
      cache: "no-store",
      credentials: "same-origin",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    return { status: res.status, body: ((await res.json().catch(() => ({}))) ?? {}) as Record<string, unknown> };
  } catch {
    return { status: 0, body: {} };
  }
}

function when(value: string | null, locale: string): string {
  if (!value) return "";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString(locale, { dateStyle: "medium", timeStyle: "short" });
}

export function ClientBuilderProposal({ initial, dict, locale }: { initial: ClientBuilderPage; dict: Partial<Record<MessageKey, string>>; locale: DateLocale }) {
  const t = (k: string) => dict[k as MessageKey] ?? k;
  const [page, setPage] = useState<ClientBuilderPage>(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [step, setStep] = useState<"choose" | "accept">("choose");
  const [name, setName] = useState("");
  const [agree, setAgree] = useState(false);
  const [roomsOk, setRoomsOk] = useState(false);
  const [reason, setReason] = useState<ChangeReason>("other");
  const cur = page.currency;
  const accepted = page.status === "accepted";

  function refusal(detail: unknown, status: number): string {
    const code = typeof detail === "string" ? detail : status === 429 ? "rate_limited" : "error";
    return t(interactiveErrorKey(code) ?? clientErrorKey(code));
  }

  async function refresh() {
    const res = await call("");
    if (res.status === 200) setPage(res.body as unknown as ClientBuilderPage);
  }

  async function run(path: string, body: unknown, after?: (res: Answer) => void) {
    setBusy(true);
    setError(null);
    setInfo(null);
    const res = await call(path, body);
    const fresh = (res.body.page ?? (res.status === 200 && "days" in res.body ? res.body : null)) as ClientBuilderPage | null;
    if (fresh) setPage(fresh);
    if (res.status !== 200) {
      setError(refusal(res.body.detail, res.status));
      if (!fresh) await refresh();
    }
    after?.(res);
    setBusy(false);
  }

  const pick = (block: ClientBlock, option: ClientOption) => run("/pick", { block_id: block.option_block_id, option_id: option.option_id });
  const later = (block: ClientBlock) => run("/pick", { block_id: block.option_block_id, choose_later: true });
  const hold = () =>
    run("/hold", {}, (res) => {
      if (res.status === 200) setStep("accept");
    });
  const askChange = () =>
    run("/change-request", { reason }, (res) => {
      if (res.status === 200) setInfo(t("client.change.sent"));
    });
  const accept = () =>
    run(
      "/accept",
      {
        full_name: name,
        accept_terms: agree,
        rooms_confirmed: roomsOk,
        price_hash: page.price_hash,
        selection_hash: page.selection_hash,
        terms_doc_id: page.terms?.doc_id ?? null,
      },
      (res) => {
        if (res.status === 200) void refresh();
      },
    );

  const action = holdAction(page.hold);
  const canAccept = acceptOpen(page) && agree && roomsOk && name.trim().length > 0 && !busy;

  return (
    <div data-testid="client-proposal" data-kind="builder" data-status={page.status} style={{ display: "grid", gap: 14, minWidth: 0 }}>
      <div style={{ display: "grid", gap: 4 }}>
        <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{fmt(t("client.preparedBy"), { agency: page.agency_name })}</p>
        <h1 style={{ margin: 0, fontSize: 20, color: K.ink, overflowWrap: "anywhere" }}>{page.trip.title}</h1>
        <p style={{ margin: 0, fontSize: 13, color: K.body }}>
          {page.trip.start_date ? formatDate(page.trip.start_date, locale) : ""}
          {page.trip.end_date ? ` – ${formatDate(page.trip.end_date, locale)}` : ""}
          {` · ${fmt(t("client.versionN"), { n: page.version })}`}
        </p>
      </div>

      {page.notice && (
        <section data-testid="client-notice" role="status" style={{ ...card, background: K.bg }}>
          <strong style={{ fontSize: 14, color: K.ink }}>{fmt(t("client.notice.title"), { from: page.notice.from_version, to: page.version })}</strong>
          <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13.5, color: K.body, display: "grid", gap: 2 }}>
            {page.notice.changes.map((c, i) => {
              const line = noticeLine(c);
              return <li key={i}>{fmt(t(line.key), line.vars)}</li>;
            })}
            {page.notice.more > 0 && <li>{fmt(t("client.notice.more"), { n: page.notice.more })}</li>}
          </ul>
          <span style={{ fontSize: 13, color: K.body }}>
            {fmt(t("client.notice.total"), { from: formatAmount(page.notice.total_from, cur, locale), to: formatAmount(page.notice.total_to, cur, locale) })}
          </span>
          {page.notice.picks_carried > 0 && <span style={{ fontSize: 13, color: K.muted }}>{fmt(t("client.notice.carried"), { n: page.notice.picks_carried })}</span>}
        </section>
      )}

      {page.body && <p style={{ margin: 0, fontSize: 14, color: K.body, whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{page.body}</p>}

      {page.days.map((day) => (
        <section key={day.day_no} data-testid="client-day" data-day={day.day_no} style={{ display: "grid", gap: 8, minWidth: 0 }}>
          <div style={{ display: "grid", gap: 2 }}>
            <h2 style={{ margin: 0, fontSize: 16, color: K.ink }}>
              {day.before_you_go ? t("client.beforeYouGo") : fmt(t("client.dayN"), { n: day.day_no })}
              {day.title ? ` · ${day.title}` : ""}
            </h2>
            {!day.before_you_go && (
              <span style={{ fontSize: 12.5, color: K.muted }}>
                {[day.date ? formatDate(day.date, locale) : null, day.place, zoneLabel(day.utc_offset)].filter(Boolean).join(" · ")}
              </span>
            )}
            {day.notes && <p style={{ margin: 0, fontSize: 13.5, color: K.body, whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{day.notes}</p>}
          </div>
          {day.blocks.map((block) => (
            <div key={block.option_block_id} data-testid="client-block" data-block={block.title} style={{ display: "grid", gap: 6, minWidth: 0 }}>
              <span style={{ fontSize: 13, fontWeight: 600, color: K.ink }}>
                {block.title}
                {block.choose_later && <span data-testid="client-later-badge" style={{ marginLeft: 8, fontWeight: 400, color: K.muted }}>{t("client.later.badge")}</span>}
              </span>
              {block.fell_back && <span role="status" style={{ fontSize: 12.5, color: K.danger }}>{t("client.fellBack")}</span>}
              {block.options.map((o) => {
                const delta = deltaLabel(o.delta, cur, locale);
                const blockedText = o.blocked ? fmt(t(`client.blocked.${o.blocked.reason}`), { with: o.blocked.with ?? "" }) : null;
                return (
                  <div key={o.option_id} data-testid="client-option" data-chosen={o.chosen ? "true" : "false"} data-title={o.title} style={{ ...card, borderColor: o.chosen ? K.accent : K.line, opacity: o.blocked && !o.chosen ? 0.6 : 1 }}>
                    <div style={{ display: "flex", gap: 8, justifyContent: "space-between", alignItems: "baseline", flexWrap: "wrap", minWidth: 0 }}>
                      <strong style={{ fontSize: 14.5, color: K.ink, overflowWrap: "anywhere", minWidth: 0 }}>{o.title}</strong>
                      {delta !== null && <span data-testid="client-delta" style={{ fontSize: 13.5, color: K.body, whiteSpace: "nowrap" }}>{delta === "" ? t("client.included") : fmt(t("client.deltaPp"), { delta })}</span>}
                    </div>
                    <span style={{ fontSize: 12.5, color: K.muted }}>
                      {t(`client.confirm.${o.confirm_class}`)}
                      {o.availability === "on_request" ? ` · ${t("client.onRequest")}` : ""}
                    </span>
                    {blockedText && !o.chosen && <span style={{ fontSize: 12.5, color: K.danger }}>{blockedText}</span>}
                    {!accepted && block.client_may_swap && !o.chosen && !o.blocked && (
                      <button type="button" data-testid="client-swap" disabled={busy} onClick={() => void pick(block, o)} style={o.is_default ? quietButton : bigButton}>
                        {o.is_default ? t("client.keepIncluded") : t("client.swap")}
                      </button>
                    )}
                    {o.chosen && <span style={{ fontSize: 13, color: K.success }}>{t("client.yourChoice")}</span>}
                  </div>
                );
              })}
              {!accepted && block.choose_later_allowed && !block.choose_later && (
                <button type="button" data-testid="client-later" disabled={busy} onClick={() => void later(block)} style={quietButton}>
                  {t("client.later.button")}
                </button>
              )}
            </div>
          ))}
        </section>
      ))}

      <dl data-testid="client-amounts" style={{ margin: 0, display: "grid", gridTemplateColumns: "auto 1fr", gap: "6px 12px", fontSize: 14 }}>
        <dt style={{ color: K.muted }}>{t("client.total")}</dt>
        <dd data-testid="client-total" style={{ margin: 0, color: K.ink, fontWeight: 600 }}>{formatAmount(page.total_amount, cur, locale)}</dd>
        {Number(page.totals.pay_on_arrival_total) > 0 && (
          <>
            <dt style={{ color: K.muted }}>{t("client.onArrival")}</dt>
            <dd style={{ margin: 0, color: K.body }}>{fmt(t("client.onArrivalNote"), { amount: formatAmount(page.totals.pay_on_arrival_total, cur, locale) })}</dd>
          </>
        )}
        <dt style={{ color: K.muted }}>{t("client.deposit")}</dt>
        <dd style={{ margin: 0, color: K.ink }}>{formatAmount(page.deposit_amount, cur, locale)}</dd>
        {page.balance_due_on && Number(page.balance_amount) > 0 && (
          <>
            <dt style={{ color: K.muted }}>{t("client.balance")}</dt>
            <dd style={{ margin: 0, color: K.ink }}>
              {fmt(t("client.balanceDue"), { amount: formatAmount(page.balance_amount, cur, locale), date: formatDate(page.balance_due_on, locale) })}
            </dd>
          </>
        )}
      </dl>

      {error && (
        <p role="alert" data-testid="client-error" style={{ margin: 0, fontSize: 13.5, color: K.danger }}>
          {error}
        </p>
      )}
      {info && (
        <p role="status" data-testid="client-info" style={{ margin: 0, fontSize: 13.5, color: K.success }}>
          {info}
        </p>
      )}

      {accepted ? (
        <div data-testid="client-accepted" style={{ display: "grid", gap: 8 }}>
          <p style={{ margin: 0, fontSize: 14, color: K.success }}>{fmt(t("client.acceptedThanks"), { agency: page.agency_name })}</p>
          {page.schedule.map((s) => (
            <p key={s.kind} style={{ margin: 0, fontSize: 13.5, color: K.body }}>
              {fmt(t(`client.scheduleRow.${s.status}`), { what: t(`client.kind.${s.kind}`), amount: formatAmount(s.amount, s.currency, locale), date: formatDate(s.due_on, locale) })}
            </p>
          ))}
        </div>
      ) : (
        <>
          <section data-testid="client-change" style={{ ...card, background: K.bg }}>
            <strong style={{ fontSize: 14, color: K.ink }}>{t("client.change.title")}</strong>
            <span style={{ fontSize: 13, color: K.muted }}>{t("client.change.hint")}</span>
            <select aria-label={t("client.change.reason")} value={reason} onChange={(e) => setReason(e.target.value as ChangeReason)} style={input} data-testid="client-change-reason">
              {CHANGE_REASONS.map((r) => (
                <option key={r} value={r}>
                  {t(`client.change.reason.${r}`)}
                </option>
              ))}
            </select>
            <button type="button" disabled={busy} onClick={() => void askChange()} style={quietButton} data-testid="client-change-send">
              {t("client.change.send")}
            </button>
          </section>

          <section data-testid="client-hold" data-status={page.hold.status} style={card}>
            <strong style={{ fontSize: 15, color: K.ink }}>{t("client.acceptTitle")}</strong>
            <span data-testid="client-hold-text" style={{ fontSize: 13.5, color: page.hold.status === "expired" ? K.danger : K.body }}>
              {fmt(t(holdKey(page.hold)), { time: when(page.hold.expires_at, locale), left: page.hold.rechecks_left })}
            </span>
            {action === "hold" && (
              <button type="button" data-testid="client-hold-button" disabled={busy} onClick={() => void hold()} style={bigButton}>
                {t("client.hold.button")}
              </button>
            )}
            {action === "recheck" && (
              <button type="button" data-testid="client-recheck" disabled={busy} onClick={() => void hold()} style={bigButton}>
                {fmt(t("client.hold.recheck"), { left: page.hold.rechecks_left })}
              </button>
            )}
            {action === "ask_agent" && <span data-testid="client-ask-agent" style={{ fontSize: 13.5, color: K.body }}>{t("client.hold.askAgentHint")}</span>}
            {action === "none" && step === "choose" && (
              <button type="button" data-testid="client-continue" disabled={busy} onClick={() => setStep("accept")} style={bigButton}>
                {t("client.continue")}
              </button>
            )}
          </section>

          {(step === "accept" || action === "none") && acceptOpen(page) && (
            <form
              data-testid="client-accept-form"
              onSubmit={(e) => {
                e.preventDefault();
                void accept();
              }}
              style={{ display: "grid", gap: 10 }}
            >
              <div data-testid="client-groups" style={{ display: "grid", gap: 8 }}>
                {page.groups.map((g) => (
                  <div key={g.confirm_class} style={{ display: "grid", gap: 2 }}>
                    <span style={{ fontSize: 11.5, letterSpacing: 0.4, textTransform: "uppercase", color: K.muted }}>{t(`client.group.${g.confirm_class}`)}</span>
                    {g.items.map((i, n) => (
                      <span key={n} style={{ fontSize: 13.5, color: K.body, overflowWrap: "anywhere" }}>
                        {i.title}
                        {i.choose_later ? ` (${t("client.later.badge")})` : ""}
                      </span>
                    ))}
                  </div>
                ))}
              </div>
              <label data-testid="client-rooms" style={{ display: "flex", gap: 8, alignItems: "flex-start", fontSize: 14 }}>
                <input type="checkbox" checked={roomsOk} onChange={(e) => setRoomsOk(e.target.checked)} />
                <span>
                  {fmt(t("client.rooms.confirm"), {
                    rooms: page.rooms.map((r) => fmt(t(`client.rooms.${r.type}`), { adults: r.adults, children: r.children })).join(", "),
                  })}
                </span>
              </label>
              {page.terms && (
                <details style={{ fontSize: 13, color: K.body }}>
                  <summary>{fmt(t("client.readTerms"), { version: page.terms.version_label })}</summary>
                  <p style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", maxHeight: 240, overflowY: "auto" }}>{page.terms.body_text}</p>
                </details>
              )}
              <label style={{ display: "flex", gap: 8, alignItems: "flex-start", fontSize: 14 }}>
                <input type="checkbox" name="accept_terms" checked={agree} onChange={(e) => setAgree(e.target.checked)} />
                <span>{page.terms ? fmt(t("client.agreeTerms"), { agency: page.agency_name, version: page.terms.version_label }) : t("client.agreeProposal")}</span>
              </label>
              <label style={{ display: "grid", gap: 4, fontSize: 14 }}>
                {t("client.fullName")}
                <input name="full_name" autoComplete="name" value={name} onChange={(e) => setName(e.target.value)} style={input} />
              </label>
              {page.hold.status === "active" && (
                <span data-testid="client-held-until" style={{ fontSize: 13, color: K.body }}>
                  {fmt(t("client.hold.activeUntil"), { time: when(page.hold.expires_at, locale) })}
                </span>
              )}
              <button type="submit" disabled={!canAccept} data-testid="client-accept" style={{ ...bigButton, opacity: canAccept ? 1 : 0.5 }}>
                {t("client.accept")}
              </button>
            </form>
          )}
        </>
      )}
    </div>
  );
}
