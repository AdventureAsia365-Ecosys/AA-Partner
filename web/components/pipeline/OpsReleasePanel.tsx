"use client";
// web/components/pipeline/OpsReleasePanel.tsx — "AA ground operations" on the trip page (AA-956, card CRM-26; design
// v3 M8, F8.3, F10.3, C12 step 6): whether AA's ground team holds this trip's traveller details (only once an AA tour
// is confirmed), how complete they are, how often AA opened them, when they are deleted (return + 90 days) or the
// deletion receipt, and "Withdraw from AA" (owner / agent; the trip cancel does it too). Counts, codes and dates only.
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, ConfirmModal, K, useHydrated } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { codeLabel } from "@/lib/accessLog";
import { bffSend } from "@/lib/bffClient";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import { activityFooter, activityLines, missingItems, panelState, type OpsReleaseView } from "@/lib/opsRelease";

const PANEL_ACTIONS = new Set([
  "ops_release_create",
  "ops_release_refresh",
  "ops_release_view",
  "ops_manifest_export",
  "ops_release_t21_alert",
  "ops_release_withdraw",
  "ops_release_purge",
  "ops_release_build",
]);
// Reasons that only name the object (the release or its supplier list) repeat what the line already says.
const OBJECT_REASONS = new Set(["ops_release", "supplier_manifest"]);

const line: React.CSSProperties = { margin: 0, fontSize: 13, color: K.body, overflowWrap: "anywhere" };

export function OpsReleasePanel({
  tripId,
  view,
  lang,
  locale,
}: {
  tripId: string;
  view: OpsReleaseView;
  lang: Lang;
  locale: DateLocale;
}) {
  const hydrated = useHydrated();
  const router = useRouter();
  const tr = (key: string) => t(lang, key as MessageKey);
  // The access-log labels end in "of" / "for" and expect an object; on the trip's own panel the trip is implied.
  const activityPhrase = (action: string) =>
    PANEL_ACTIONS.has(action) ? tr(`ops.line.${action}`) : codeLabel(lang, "log.act", action);
  const [asking, setAsking] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const footer = activityFooter(view, showAll);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const state = panelState(view);
  const r = view.release;
  const day = (iso: string | null) => (iso ? formatDate(iso.slice(0, 10), locale) : "");

  async function withdraw() {
    setBusy(true);
    setError(null);
    const res = await bffSend<OpsReleaseView>("POST", `trips/${tripId}/ops-release/withdraw`, {});
    setBusy(false);
    if (!res.ok) {
      setError(tr("ops.error"));
      return;
    }
    setAsking(false);
    router.refresh();
  }

  const badge =
    state === "live" && r ? (
      <Badge tone={r.complete ? "success" : "warning"}>{tr(r.complete ? "ops.complete" : "ops.incomplete")}</Badge>
    ) : state === "withdrawn" || state === "purged" ? (
      <Badge tone="neutral">{tr(`ops.status.${state}`)}</Badge>
    ) : null;

  return (
    <Panel title={tr("ops.title")} testId="ops-release">
      <div data-hydrated={hydrated ? "true" : undefined} data-state={state} style={{ display: "grid", gap: 8, minWidth: 0 }}>
        <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("ops.intro")}</p>
        {error && (
          <p role="alert" style={{ margin: 0, fontSize: 13, color: K.danger }}>
            {error}
          </p>
        )}
        {state === "none" && <p style={line}>{tr("ops.none")}</p>}
        {state === "pending" && <p style={line}>{tr("ops.pending")}</p>}
        {r && state !== "none" && state !== "pending" && (
          <>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              {state === "live" && <Badge tone="info">{tr("ops.status.live")}</Badge>}
              {badge}
              <span style={{ fontSize: 12.5, color: K.muted }}>
                {fmt(tr("ops.version"), { n: r.payload_version })} · {fmt(tr("ops.refreshed"), { date: day(r.refreshed_at) })}
              </span>
            </div>
            {state === "live" && (
              <>
                <p style={line} data-testid="ops-release-count">
                  {fmt(tr("ops.travellers"), { complete: r.completeness.complete, n: r.completeness.travellers })}
                </p>
                {missingItems(r.completeness).length > 0 && (
                  <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center", fontSize: 13 }}>
                    <span style={{ color: K.muted }}>{tr("ops.missingTitle")}:</span>
                    {missingItems(r.completeness).map((m) => (
                      <Badge key={m.item} tone="warning">
                        {fmt(tr(`ops.missing.${m.item}`), { n: m.n })}
                      </Badge>
                    ))}
                  </div>
                )}
                {r.t21_alerted_at && <p style={{ ...line, color: K.amber }}>{tr("ops.alerted")}</p>}
                <p style={line}>
                  {r.view_count > 0
                    ? `${fmt(tr("ops.views"), { n: r.view_count })}${r.last_viewed_at ? `, ${fmt(tr("ops.lastView"), { date: day(r.last_viewed_at) })}` : ""}`
                    : tr("ops.neverViewed")}
                </p>
                <p style={line} data-testid="ops-release-purge">
                  {r.purge_at && r.days_to_purge !== null
                    ? fmt(tr("ops.purgeDays"), { date: day(r.purge_at), n: r.days_to_purge })
                    : tr("ops.purgeUnknown")}
                </p>
              </>
            )}
            {r.withdrawn_at && (
              <p style={line}>
                {fmt(tr("ops.withdrawnOn"), { date: day(r.withdrawn_at) })}
                {r.withdraw_reason ? ` (${tr(`ops.reason.${r.withdraw_reason}`)})` : ""}
              </p>
            )}
            {r.purged_at && r.deletion_receipt && (
              <p style={line} data-testid="ops-release-receipt">
                {fmt(tr("ops.purgedOn"), { date: day(r.purged_at) })}{" "}
                {fmt(tr("ops.receipt"), {
                  version: r.deletion_receipt.payload_version,
                  travellers: r.deletion_receipt.travellers,
                  views: r.deletion_receipt.views,
                  manifests: r.deletion_receipt.manifest_exports,
                })}
              </p>
            )}
            {r.can_withdraw && (
              <div>
                <Button size="sm" variant="secondary" disabled={!hydrated} onClick={() => setAsking(true)} data-testid="ops-release-withdraw">
                  {tr("ops.withdraw")}
                </Button>
              </div>
            )}
          </>
        )}
        {view.activity && view.activity.length > 0 && (
          <section style={{ display: "grid", gap: 4 }}>
            <h3 style={{ margin: 0, fontSize: 13, color: K.ink }}>{tr("ops.activity")}</h3>
            <ul style={{ margin: 0, paddingLeft: 18, display: "grid", gap: 2, fontSize: 12.5, color: K.body }}>
              {activityLines(view, showAll ? footer.total : undefined).map((a, i) => (
                <li key={`${a.at}-${i}`}>
                  {day(a.at)} · {codeLabel(lang, "log.actor", a.actor_kind)}{" "}
                  {activityPhrase(a.action)}
                  {a.reason && !OBJECT_REASONS.has(a.reason) ? ` · ${codeLabel(lang, "why", a.reason)}` : ""}
                </li>
              ))}
            </ul>
            {footer.toggle && (
              <div data-testid="ops-activity-toggle">
                <Button size="sm" variant="ghost" disabled={!hydrated} onClick={() => setShowAll(!showAll)}>
                  {footer.toggle === "all" ? fmt(tr("ops.activityAll"), { n: footer.total }) : tr("ops.activityFewer")}
                </Button>
              </div>
            )}
            {footer.older && (
              <p style={{ margin: 0, fontSize: 12, color: K.muted }} data-testid="ops-activity-older">
                {tr("ops.activityOlder")} <Link href="/settings/access-log" style={{ color: K.accent }}>
                  {tr("ops.activityOlderLink")}
                </Link>
              </p>
            )}
          </section>
        )}
      </div>
      <ConfirmModal
        open={asking}
        onClose={() => setAsking(false)}
        onConfirm={() => void withdraw()}
        title={tr("ops.withdrawTitle")}
        body={tr("ops.withdrawBody")}
        confirmLabel={tr("ops.withdrawConfirm")}
        destructive
        busy={busy}
        error={error}
        testId="ops-release-withdraw-dialog"
      />
    </Panel>
  );
}
