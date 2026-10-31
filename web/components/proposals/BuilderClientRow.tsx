"use client";
// web/components/proposals/BuilderClientRow.tsx — one day-by-day version in the trip's Proposal tab, client side
// (AA-950, card CRM-21b; design v3 A-03, F4 Send, R4, R19, Fix A3, Fix B11): "Send to client" for a published version
// (owner / agent; the client's link shown once to copy, or "the client's link now shows this version" when the new
// version took over the live link), "Re-issue link" for a sent one (the old link stops working on every device),
// "viewed" and the AA hold, and the client's choices against the included options (R19). Test ids sit on wrappers: the
// kit Button forwards no data-* attribute (lesson AA-949).
import { useState } from "react";
import { Button, K } from "@/app/_kit";
import { bffSend } from "@/lib/bffClient";
import { formatDate, formatInstant, type DateLocale } from "@/lib/dates";
import { fmt } from "@/lib/i18n";
import { formatAmount, type Proposal } from "@/lib/proposals";

type Choice = { day_no: number | null; block: string; default: string | null; chosen: string | null; sell_delta: string | null; choose_later: boolean };
type Activity = { choices: Choice[]; totals?: { published: string; current: string; currency: string } };

export function BuilderClientRow({
  p,
  canSend,
  hydrated,
  busy,
  tr,
  locale,
  timeZone = "UTC",
  onLink,
  onDone,
  onError,
}: {
  p: Proposal;
  canSend: boolean;
  hydrated: boolean;
  busy: boolean;
  tr: (key: string) => string;
  locale: DateLocale;
  /** The agency's IANA zone: the hold's end prints the same on the server and in the browser (S262, AA-957). */
  timeZone?: string;
  onLink: (path: string) => void;
  onDone: (text: string) => void;
  onError: (code: string) => void;
}) {
  const [activity, setActivity] = useState<Activity | null>(null);
  const [working, setWorking] = useState(false);

  async function post(path: string): Promise<Record<string, unknown> | null> {
    setWorking(true);
    const res = await bffSend<Record<string, unknown>>("POST", path);
    setWorking(false);
    if (!res.ok) {
      onError(res.detail);
      return null;
    }
    return res.data;
  }

  async function send() {
    const done = await post(`proposals/${p.proposal_id}/send`);
    if (!done) return;
    if (typeof done.link_path === "string") onLink(done.link_path);
    onDone(tr(done.link_reused ? "builder.sentReused" : "builder.sent"));
  }

  async function reissue() {
    const done = await post(`proposals/${p.proposal_id}/reissue-link`);
    if (done && typeof done.link_path === "string") {
      onLink(done.link_path);
      onDone(tr("builder.reissued"));
    }
  }

  async function loadActivity() {
    setWorking(true);
    const res = await bffSend<Activity>("GET", `proposals/${p.proposal_id}/client-activity`);
    setWorking(false);
    if (res.ok) setActivity(res.data);
    else onError(res.detail);
  }

  const off = !hydrated || busy || working;
  return (
    <div data-testid="builder-client" data-status={p.status} style={{ display: "grid", gap: 6, fontSize: 12.5, color: K.body, minWidth: 0, flexBasis: "100%" }}>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        {p.status === "published" && canSend && (
          <span data-testid="builder-send">
            <Button size="sm" variant="primary" disabled={off} onClick={() => void send()}>
              {tr("builder.send")}
            </Button>
          </span>
        )}
        {p.status === "sent" && canSend && (
          <span data-testid="builder-reissue">
            <Button size="sm" disabled={off} onClick={() => void reissue()}>
              {tr("builder.reissue")}
            </Button>
          </span>
        )}
        {(p.status === "sent" || p.status === "accepted") && (
          <span data-testid="builder-activity">
            <Button size="sm" variant="secondary" disabled={off} onClick={() => void loadActivity()}>
              {tr("builder.activity")}
            </Button>
          </span>
        )}
      </div>
      {(p.status === "sent" || p.status === "accepted") && (
        <span data-testid="builder-viewed">
          {p.view_count ? fmt(tr("builder.viewed"), { n: p.view_count, date: p.last_viewed_at ? formatDate(p.last_viewed_at.slice(0, 10), locale) : "" }) : tr("builder.notViewed")}
          {p.hold_expires_at ? ` · ${fmt(tr("builder.heldUntil"), { date: formatInstant(p.hold_expires_at, locale, timeZone) })}` : ""}
        </span>
      )}
      {p.status === "accepted" && p.accepted_total_amount && (
        <span data-testid="builder-accepted-total">{fmt(tr("builder.acceptedTotal"), { amount: formatAmount(p.accepted_total_amount, p.currency, locale) })}</span>
      )}
      {activity && (
        <ul data-testid="builder-choices" style={{ margin: 0, paddingLeft: 18, display: "grid", gap: 2 }}>
          {activity.choices.length === 0 && <li>{tr("builder.activity.none")}</li>}
          {activity.choices.map((c, i) => (
            <li key={i} style={{ overflowWrap: "anywhere" }}>
              {c.choose_later
                ? fmt(tr("builder.activity.later"), { day: c.day_no ?? "", block: c.block })
                : fmt(tr("builder.activity.line"), {
                    day: c.day_no ?? "",
                    block: c.block,
                    chosen: c.chosen ?? "",
                    default: c.default ?? "",
                    delta: c.sell_delta ? formatAmount(c.sell_delta, p.currency, locale) : "",
                  })}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
