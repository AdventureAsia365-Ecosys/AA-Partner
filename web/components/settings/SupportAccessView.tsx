"use client";
// web/components/settings/SupportAccessView.tsx — Settings > Data and privacy > AA support access (AA-971, card CRM-13b;
// design v3 WF-12: "owner-only: off -> allow for 24 h (1-7 days), read only, show identity [ ], revoke any time"). Every
// member sees the state and the last 90 days of AA access (support and emergency); only the owner allows or ends it
// (the API refuses the others too). Writes go through the BFF allow-list (lib/bff.ts).
import type { ColumnDef } from "@tanstack/react-table";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { Badge, Button, Checkbox, DataTable, Field, K, Select, mono, useHydrated } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { formatInstant, type DateLocale } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import {
  dayChoices,
  reasonKey,
  supportErrorKey,
  type SessionStatus,
  type SupportAccess,
  type SupportSession,
} from "@/lib/supportAccess";

const STATUS_TONE: Record<SessionStatus, "success" | "neutral" | "warning"> = {
  active: "success",
  ended: "neutral",
  revoked: "neutral",
  expired: "warning",
};

export function SupportAccessView({ view, lang, locale }: { view: SupportAccess; lang: Lang; locale: DateLocale }) {
  const router = useRouter();
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [days, setDays] = useState(String(view.days_default));
  const [showIdentity, setShowIdentity] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "danger" | "success"; text: string } | null>(null);
  // The agency's zone (S262 round 3), as on the trip pages; UTC (labelled) only when the API did not send it.
  const zone = view.timezone ?? "UTC";
  const when = (value: string | null) => (value ? formatInstant(value, locale, zone) : "—");
  const columns = useMemo<ColumnDef<SupportSession, unknown>[]>(
    () => {
      const tx = (key: string) => t(lang, key as MessageKey);
      const at = (value: string | null) => (value ? formatInstant(value, locale, zone) : "—");
      return [
        { id: "when", header: tx("support.col.when"), enableSorting: false, cell: ({ row }) => at(row.original.started_at) },
        {
          id: "kind",
          header: tx("support.col.kind"),
          enableSorting: false,
          cell: ({ row }) => (
            <span>
              <Badge tone={row.original.kind === "breakglass" ? "danger" : "info"}>
                {tx(`support.kind.${row.original.kind}`)}
              </Badge>
              {row.original.kind === "breakglass" && (
                <span style={{ marginLeft: 6, color: K.muted }}>{tx(reasonKey(row.original.reason_code))}</span>
              )}
            </span>
          ),
        },
        {
          id: "who",
          header: tx("support.col.who"),
          enableSorting: false,
          cell: ({ row }) => <span style={{ fontFamily: mono }}>{row.original.staff_ref}</span>,
        },
        { id: "ticket", header: tx("support.col.ticket"), enableSorting: false, cell: ({ row }) => row.original.ticket_ref },
        {
          id: "status",
          header: tx("support.col.status"),
          enableSorting: false,
          cell: ({ row }) => (
            <Badge tone={STATUS_TONE[row.original.status]}>{tx(`support.status.${row.original.status}`)}</Badge>
          ),
        },
        {
          id: "until",
          header: tx("support.col.until"),
          enableSorting: false,
          cell: ({ row }) => at(row.original.ended_at ?? row.original.expires_at),
        },
      ];
    },
    [lang, locale, zone],
  );

  async function send(path: string, body?: unknown, done?: string) {
    setBusy(true);
    setMessage(null);
    const res = await bffSend("POST", path, body);
    setBusy(false);
    if (!res.ok) {
      setMessage({ tone: "danger", text: tr(supportErrorKey(res.detail)) });
      return;
    }
    if (done) setMessage({ tone: "success", text: tr(done) });
    router.refresh();
  }

  return (
    <Panel title={tr("support.title")} testId="support-access">
      <div style={{ display: "grid", gap: 12 }}>
        <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{tr("support.help")}</p>
        <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }} data-testid="support-state">
          {view.grant ? (
            <>
              <Badge tone="warning">{fmt(tr("support.onUntil"), { date: when(view.grant.expires_at) })}</Badge>
              <span style={{ fontSize: 13, color: K.ink }}>
                {tr(view.grant.show_identity ? "support.identityShown" : "support.identityHidden")}
              </span>
            </>
          ) : (
            <Badge tone="neutral">{tr("support.off")}</Badge>
          )}
        </div>

        {view.can_manage ? (
          view.grant ? (
            <div>
              <Button size="sm" variant="secondary" disabled={!hydrated || busy}
                onClick={() => send("support-access/revoke", undefined, "support.ended")}>
                {tr("support.endNow")}
              </Button>
            </div>
          ) : (
            <form
              data-hydrated={hydrated ? "true" : undefined}
              style={{ display: "grid", gap: 12 }}
              onSubmit={(e) => {
                e.preventDefault();
                void send("support-access", { days: Number(days), show_identity: showIdentity }, "support.allowed");
              }}
            >
              <Field label={tr("support.duration")}>
                <Select
                  size="sm"
                  value={days}
                  style={{ width: 200 }}
                  onChange={(e) => setDays(e.target.value)}
                  options={dayChoices(view).map((d) => ({
                    value: String(d),
                    label: fmt(tr(d === 1 ? "support.day" : "support.days"), { n: d }),
                  }))}
                />
              </Field>
              <div style={{ display: "grid", gap: 4 }}>
                <Checkbox
                  label={tr("support.showIdentity")}
                  checked={showIdentity}
                  onChange={(e) => setShowIdentity(e.target.checked)}
                />
                <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("support.showIdentityHelp")}</p>
              </div>
              <div>
                <Button type="submit" size="sm" disabled={!hydrated || busy}>
                  {tr("support.allow")}
                </Button>
              </div>
            </form>
          )
        ) : (
          <p style={{ margin: 0, fontSize: 13, color: K.muted }} data-testid="support-owner-only">
            {tr("support.ownerOnly")}
          </p>
        )}
        {message && (
          <p role="status" style={{ margin: 0, fontSize: 13, color: message.tone === "danger" ? K.danger : K.success }}>
            {message.text}
          </p>
        )}

        <div style={{ display: "grid", gap: 6 }} data-testid="support-history">
          <strong style={{ fontSize: 13 }}>{tr("support.history")}</strong>
          {view.recent_sessions.length === 0 ? (
            <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{tr("support.historyEmpty")}</p>
          ) : (
            <DataTable<SupportSession>
              data={view.recent_sessions}
              columns={columns}
              tableId="support-history"
              getRowId={(row) => row.session_id}
              searchable={false}
              paginate={false}
              compact
              tableMinWidth={560}
            />
          )}
          <Link href="/settings/access-log" style={{ fontSize: 12.5 }}>
            {tr("support.accessLog")}
          </Link>
        </div>
      </div>
    </Panel>
  );
}
