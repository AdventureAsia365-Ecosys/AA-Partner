"use client";
// web/components/settings/DataLifecycleView.tsx — Settings > Data and privacy (AA-942, card CRM-14). The rules table for
// everyone; for the owner: trash window and archive setting, legal holds, the trash with Restore, clients archived
// after inactivity (Delete), the last lifecycle runs and leaving AA Partner (export, download, typed erase); an export
// or deletion request AA raised shows with Confirm / Decline (AA-941, crm-011). Writes go
// through the BFF allow-list (lib/bff.ts), which refuses cross-site writes first.
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, K } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import {
  aaRequestKey,
  confirmsErase,
  offboardingStep,
  ruleWindow,
  totalCount,
  type Hold,
  type LifecycleView,
  type TrashItem,
} from "@/lib/lifecycle";

const field: React.CSSProperties = {
  font: "inherit",
  fontSize: 13,
  padding: "7px 9px",
  borderRadius: 8,
  border: `1px solid ${K.line2}`,
  background: K.card,
  color: K.ink,
  boxSizing: "border-box",
};
const cell: React.CSSProperties = { padding: "8px 10px", borderTop: `1px solid ${K.line}`, verticalAlign: "top", fontSize: 13 };

export function DataLifecycleView({
  view,
  trash,
  tenantId,
  lang,
  locale,
}: {
  view: LifecycleView;
  trash: TrashItem[];
  tenantId: string;
  lang: Lang;
  locale: DateLocale;
}) {
  const router = useRouter();
  const owner = view.role === "owner";
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [trashDays, setTrashDays] = useState(String(view.settings.trash_days));
  const [archiveMonths, setArchiveMonths] = useState(String(view.settings.auto_archive_months));
  const [holdScope, setHoldScope] = useState<Hold["scope"]>("tenant");
  const [holdSubject, setHoldSubject] = useState("");
  const [holdReason, setHoldReason] = useState(view.hold_reasons[0] ?? "other");
  const [typed, setTyped] = useState("");
  const tr = (key: string) => t(lang, key as MessageKey);

  async function send(method: "POST" | "PATCH" | "DELETE", path: string, body?: unknown) {
    setError(null);
    setBusy(true);
    const res = await bffSend(method, path, body);
    setBusy(false);
    if (!res.ok) setError(res.detail === "deletion_dry_run" ? t(lang, "data.dryRun") : t(lang, "data.error"));
    else router.refresh();
    return res.ok;
  }

  async function download(requestId: string) {
    setError(null);
    try {
      const res = await fetch(`/bff/lifecycle/offboarding/${requestId}/export`, { credentials: "same-origin" });
      if (!res.ok) throw new Error(String(res.status));
      const url = URL.createObjectURL(await res.blob());
      const a = document.createElement("a");
      a.href = url;
      a.download = `agency-export-${requestId}.jsonl`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      router.refresh();
    } catch {
      setError(t(lang, "data.error"));
    }
  }

  const off = view.offboarding ?? null;
  const step = offboardingStep(off);

  return (
    <div style={{ display: "grid", gap: 14 }}>
      {view.deletion_dry_run && (
        <p data-testid="data-dry-run" style={{ margin: 0, fontSize: 13, color: K.amber }}>
          {t(lang, "data.dryRun")}
        </p>
      )}
      {error && (
        <p role="alert" style={{ margin: 0, fontSize: 13, color: K.danger }}>
          {error}
        </p>
      )}

      <Panel title={t(lang, "data.rules")} testId="data-rules">
        <div style={{ overflowX: "auto" }}>
          <table style={{ borderCollapse: "collapse", width: "100%" }}>
            <thead>
              <tr style={{ textAlign: "left", fontSize: 12, color: K.muted }}>
                <th style={{ padding: "6px 10px" }}>{t(lang, "data.col.data")}</th>
                <th style={{ padding: "6px 10px" }}>{t(lang, "data.col.kept")}</th>
                <th style={{ padding: "6px 10px" }}>{t(lang, "data.col.how")}</th>
              </tr>
            </thead>
            <tbody>
              {view.rules.map((r) => (
                <tr key={r.code} data-testid="data-rule">
                  <td style={{ ...cell, color: K.ink }}>{tr(`data.rule.${r.code}`)}</td>
                  <td style={cell}>{ruleWindow(r, view.settings, lang)}</td>
                  <td style={{ ...cell, color: K.muted }}>{tr(`data.how.${r.mechanism}`)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      <Panel title={t(lang, "data.settings")} testId="data-settings">
        {!owner ? (
          <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{t(lang, "data.ownerOnly")}</p>
        ) : (
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              await send("PATCH", "lifecycle/settings", {
                trash_days: Number(trashDays),
                auto_archive_months: Number(archiveMonths),
              });
            }}
            style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "flex-end" }}
          >
            <label style={{ display: "grid", gap: 4, fontSize: 13, flex: "1 1 260px" }}>
              {t(lang, "data.trashDays")}
              <input type="number" min={1} max={30} value={trashDays} onChange={(e) => setTrashDays(e.target.value)} style={field} data-testid="data-trash-days" />
            </label>
            <label style={{ display: "grid", gap: 4, fontSize: 13, flex: "1 1 260px" }}>
              {t(lang, "data.autoArchive")}
              <input type="number" min={12} max={120} value={archiveMonths} onChange={(e) => setArchiveMonths(e.target.value)} style={field} data-testid="data-archive-months" />
            </label>
            <Button type="submit" variant="primary" disabled={busy}>
              {t(lang, "data.save")}
            </Button>
          </form>
        )}
      </Panel>

      {owner && (
        <Panel title={t(lang, "data.holds")} testId="data-holds">
          <p style={{ margin: "0 0 10px", fontSize: 13, color: K.muted }}>{t(lang, "data.holdsHelp")}</p>
          {(view.holds ?? []).length === 0 ? (
            <p style={{ margin: "0 0 10px", fontSize: 13 }}>{t(lang, "data.hold.none")}</p>
          ) : (
            <ul style={{ listStyle: "none", padding: 0, margin: "0 0 10px", display: "grid", gap: 6 }}>
              {(view.holds ?? []).map((h) => (
                <li key={h.hold_id} data-testid="data-hold" style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", fontSize: 13 }}>
                  <Badge tone={h.status === "active" ? "warning" : "neutral"}>
                    {h.status === "active" ? tr(`data.hold.scope.${h.scope}`) : t(lang, "data.hold.released")}
                  </Badge>
                  <span>{tr(`data.hold.reason.${h.reason_code}`)}</span>
                  {h.subject_id && <code style={{ fontSize: 12, color: K.muted }}>{h.subject_id.slice(0, 8)}</code>}
                  <span style={{ color: K.muted }}>{formatDate(h.created_at, locale, { year: true })}</span>
                  {h.placed_by_aa && <Badge tone="info">{t(lang, "data.hold.byAa")}</Badge>}
                  {h.status === "active" && !h.placed_by_aa && (
                    <Button size="sm" variant="ghost" disabled={busy} onClick={() => send("POST", `lifecycle/holds/${h.hold_id}/release`)}>
                      {t(lang, "data.hold.release")}
                    </Button>
                  )}
                </li>
              ))}
            </ul>
          )}
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              const ok = await send("POST", "lifecycle/holds", {
                scope: holdScope,
                subject_id: holdScope === "tenant" ? null : holdSubject.trim(),
                reason_code: holdReason,
              });
              if (ok) setHoldSubject("");
            }}
            style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "flex-end" }}
          >
            <select value={holdScope} onChange={(e) => setHoldScope(e.target.value as Hold["scope"])} style={field} aria-label={t(lang, "data.holds")}>
              {(["tenant", "trip", "traveler"] as const).map((s) => (
                <option key={s} value={s}>
                  {tr(`data.hold.scope.${s}`)}
                </option>
              ))}
            </select>
            {holdScope !== "tenant" && (
              <input value={holdSubject} onChange={(e) => setHoldSubject(e.target.value)} placeholder={t(lang, "data.hold.subject")} style={{ ...field, minWidth: 280 }} />
            )}
            <select value={holdReason} onChange={(e) => setHoldReason(e.target.value)} style={field}>
              {view.hold_reasons.map((r) => (
                <option key={r} value={r}>
                  {tr(`data.hold.reason.${r}`)}
                </option>
              ))}
            </select>
            <Button type="submit" disabled={busy || (holdScope !== "tenant" && !holdSubject.trim())}>
              {t(lang, "data.hold.add")}
            </Button>
          </form>
        </Panel>
      )}

      <Panel title={t(lang, "data.trash")} testId="data-trash">
        {trash.length === 0 ? (
          <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{t(lang, "data.trash.empty")}</p>
        ) : (
          <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "grid", gap: 6 }}>
            {trash.map((i) => (
              <li key={`${i.kind}:${i.id}`} data-testid="data-trash-item" style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", fontSize: 13 }}>
                <Badge tone="neutral">{tr(`data.trash.kind.${i.kind}`)}</Badge>
                <span style={{ color: K.ink, overflowWrap: "anywhere" }}>{i.label ?? i.id.slice(0, 8)}</span>
                <span style={{ color: K.muted }}>{fmt(t(lang, "data.trash.purgeOn"), { date: formatDate(i.purge_after, locale, { year: true }) })}</span>
                <Button size="sm" variant="ghost" disabled={busy} onClick={() => send("POST", `trash/${i.kind}/${i.id}/restore`)}>
                  {t(lang, "data.trash.restore")}
                </Button>
              </li>
            ))}
          </ul>
        )}
      </Panel>

      {owner && (
        <Panel title={t(lang, "data.archived")} testId="data-archived">
          <p style={{ margin: "0 0 10px", fontSize: 13, color: K.muted }}>{t(lang, "data.archived.help")}</p>
          {(view.archived_inactive ?? []).length === 0 ? (
            <p style={{ margin: 0, fontSize: 13 }}>{t(lang, "data.archived.none")}</p>
          ) : (
            <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "grid", gap: 6 }}>
              {(view.archived_inactive ?? []).map((c) => (
                <li key={c.traveler_id} style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 13, flexWrap: "wrap" }}>
                  <a href={`/clients/${c.traveler_id}`}>{c.display_name}</a>
                  <span style={{ color: K.muted }}>{formatDate(c.archived_at, locale, { year: true })}</span>
                  <Button size="sm" variant="ghost" disabled={busy} onClick={() => send("DELETE", `travelers/${c.traveler_id}`)}>
                    {t(lang, "data.archived.delete")}
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      )}

      {owner && (
        <Panel title={t(lang, "data.runs")} testId="data-runs">
          {(view.runs ?? []).length === 0 ? (
            <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{t(lang, "data.runs.none")}</p>
          ) : (
            <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "grid", gap: 4, fontSize: 13 }}>
              {(view.runs ?? []).map((r) => (
                <li key={r.at}>
                  {formatDate(r.at, locale, { year: true })} · {r.dry_run ? t(lang, "data.runs.dry") : t(lang, "data.runs.applied")} ·{" "}
                  {fmt(t(lang, "data.runs.items"), { n: totalCount(r.counts) })}
                </li>
              ))}
            </ul>
          )}
        </Panel>
      )}

      {owner && (
        <Panel title={t(lang, "data.off")} testId="data-offboarding">
          <p style={{ margin: "0 0 10px", fontSize: 13, color: K.muted }}>{fmt(t(lang, "data.off.help"), { n: view.backup_purge_days })}</p>
          {off && !["request", "done", "aa_request", "export_ready"].includes(step) && off.purpose !== "export_only" && (
            <p style={{ margin: "0 0 10px", fontSize: 13 }}>{fmt(t(lang, "data.off.counts"), { n: totalCount(off.dry_run_counts) })}</p>
          )}
          {step === "aa_request" && off && (
            <div data-testid="data-aa-request" style={{ display: "grid", gap: 8 }}>
              <strong style={{ fontSize: 14, color: K.ink }}>{t(lang, aaRequestKey(off))}</strong>
              <span style={{ fontSize: 13, color: K.muted }}>{t(lang, "data.aa.help")}</span>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                <Button
                  variant={off.purpose === "export_only" ? "primary" : "danger"}
                  disabled={busy}
                  onClick={() => send("POST", `lifecycle/offboarding/${off.request_id}/accept`, { export_waived: false })}
                >
                  {t(lang, "data.aa.confirm")}
                </Button>
                <Button variant="ghost" disabled={busy} onClick={() => send("POST", `lifecycle/offboarding/${off.request_id}/cancel`)}>
                  {t(lang, "data.aa.decline")}
                </Button>
              </div>
            </div>
          )}
          {step === "export_ready" && off && (
            <div style={{ display: "grid", gap: 8 }}>
              <span style={{ fontSize: 13 }}>{t(lang, "data.aa.exportReady")}</span>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
                {off.export_ready && <Button onClick={() => download(off.request_id)}>{t(lang, "data.off.download")}</Button>}
                {off.export_expires_at && (
                  <span style={{ fontSize: 13, color: K.muted }}>
                    {fmt(t(lang, "data.off.expires"), { date: formatDate(off.export_expires_at, locale, { year: true }) })}
                  </span>
                )}
                <Button size="sm" variant="ghost" disabled={busy} onClick={() => send("POST", `lifecycle/offboarding/${off.request_id}/cancel`)}>
                  {t(lang, "data.aa.close")}
                </Button>
              </div>
            </div>
          )}
          {step === "request" && (
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <Button disabled={busy} onClick={() => send("POST", "lifecycle/offboarding", { export_waived: false })}>
                {t(lang, "data.off.request")}
              </Button>
              <Button variant="ghost" disabled={busy} onClick={() => send("POST", "lifecycle/offboarding", { export_waived: true })}>
                {t(lang, "data.off.requestWaive")}
              </Button>
            </div>
          )}
          {step === "wait_export" && off && (
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
              <span style={{ fontSize: 13 }}>{t(lang, "data.off.waiting")}</span>
              <Button size="sm" variant="ghost" disabled={busy} onClick={() => send("POST", `lifecycle/offboarding/${off.request_id}/cancel`)}>
                {t(lang, "data.off.cancel")}
              </Button>
            </div>
          )}
          {step === "confirm" && off && (
            <div style={{ display: "grid", gap: 10 }}>
              {off.export_ready && (
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
                  <Button onClick={() => download(off.request_id)}>{t(lang, "data.off.download")}</Button>
                  {off.export_expires_at && (
                    <span style={{ fontSize: 13, color: K.muted }}>
                      {fmt(t(lang, "data.off.expires"), { date: formatDate(off.export_expires_at, locale, { year: true }) })}
                    </span>
                  )}
                </div>
              )}
              <label style={{ display: "grid", gap: 4, fontSize: 13, maxWidth: 520 }}>
                {fmt(t(lang, "data.off.confirmLabel"), { id: tenantId })}
                <input value={typed} onChange={(e) => setTyped(e.target.value)} style={field} data-testid="data-erase-confirm" autoComplete="off" />
              </label>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                <Button
                  variant="danger"
                  disabled={busy || !confirmsErase(typed, tenantId)}
                  onClick={() => send("POST", `lifecycle/offboarding/${off.request_id}/erase`, { confirm_tenant_id: tenantId })}
                >
                  {t(lang, "data.off.erase")}
                </Button>
                <Button variant="ghost" disabled={busy} onClick={() => send("POST", `lifecycle/offboarding/${off.request_id}/cancel`)}>
                  {t(lang, "data.off.cancel")}
                </Button>
              </div>
            </div>
          )}
          {step === "running" && off && <p style={{ margin: 0, fontSize: 13 }}>{t(lang, "data.off.running")}</p>}
          {step === "failed" && off && (
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
              <span style={{ fontSize: 13, color: K.danger }} data-testid="data-offboarding-failed">
                {fmt(t(lang, "data.off.failed"), { code: off.failure_class ?? "error" })}
              </span>
              <Button size="sm" variant="ghost" disabled={busy} onClick={() => send("POST", `lifecycle/offboarding/${off.request_id}/cancel`)}>
                {t(lang, "data.off.cancel")}
              </Button>
            </div>
          )}
          {step === "done" && off && (
            <p style={{ margin: 0, fontSize: 13 }}>
              {fmt(t(lang, "data.off.done"), {
                date: formatDate(off.executed_at, locale, { year: true }),
                purge: formatDate(off.backup_purge_by, locale, { year: true }),
              })}
            </p>
          )}
        </Panel>
      )}
    </div>
  );
}
