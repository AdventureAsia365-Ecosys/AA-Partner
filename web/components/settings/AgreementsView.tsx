"use client";
// web/components/settings/AgreementsView.tsx — Settings > Agreements (AA-946, card CRM-18; design v3 M12 v0, A-16
// Terms thin): the agency's booking terms (shown with a proposal, accepted with it) and the traveller waiver (signed in
// the portal), one version per row. Owner only: draft a version, edit a draft, publish it (the published version of
// the same kind retires; a published text never changes, the API and the database refuse it).
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, K, useHydrated } from "@/app/_kit";
import { bffSend } from "@/lib/bffClient";
import { formatDate, type DateLocale } from "@/lib/dates";
import { t, type Lang, type MessageKey } from "@/lib/i18n";
import { proposalErrorKey, type Agreement } from "@/lib/proposals";

const field: React.CSSProperties = {
  font: "inherit",
  fontSize: 13,
  padding: "6px 8px",
  borderRadius: 8,
  border: `1px solid ${K.line2}`,
  background: K.card,
  color: K.ink,
  boxSizing: "border-box",
  maxWidth: "100%",
};
const TONE = { draft: "neutral", published: "success", retired: "neutral" } as const;

export function AgreementsView({ docs, lang, locale }: { docs: Agreement[]; lang: Lang; locale: DateLocale }) {
  const router = useRouter();
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run(method: "POST" | "PATCH", path: string, body?: unknown): Promise<boolean> {
    setBusy(true);
    setError(null);
    const res = await bffSend(method, path, body);
    setBusy(false);
    if (!res.ok) {
      setError(tr(proposalErrorKey(res.detail)));
      return false;
    }
    router.refresh();
    return true;
  }

  async function create(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const ok = await run("POST", "agreements", {
      kind: String(data.get("kind")),
      version_label: String(data.get("version_label") ?? "").trim(),
      body_text: String(data.get("body_text") ?? ""),
    });
    if (ok) form.reset();
  }

  return (
    <div data-testid="agreements-view" data-hydrated={hydrated ? "true" : undefined} style={{ display: "grid", gap: 14 }}>
      {error && (
        <p role="alert" data-testid="agreements-error" style={{ margin: 0, fontSize: 13, color: K.danger }}>
          {error}
        </p>
      )}
      {docs.length === 0 && <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{tr("agreements.empty")}</p>}
      {docs.map((d) => (
        <section key={d.doc_id} data-testid="agreement-row" data-kind={d.kind} data-status={d.status} style={{ display: "grid", gap: 6, padding: 12, border: `1px solid ${K.line}`, borderRadius: 10, background: K.card }}>
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <strong style={{ fontSize: 14, color: K.ink }}>{tr(`agreements.kind.${d.kind}`)}</strong>
            <span style={{ fontSize: 13 }}>{d.version_label}</span>
            <Badge tone={TONE[d.status]}>{tr(`agreements.status.${d.status}`)}</Badge>
            {d.published_at && <span style={{ fontSize: 12.5, color: K.muted }}>{formatDate(d.published_at.slice(0, 10), locale)}</span>}
          </div>
          <p style={{ margin: 0, fontSize: 13, color: K.body, whiteSpace: "pre-wrap", overflowWrap: "anywhere", maxHeight: 160, overflowY: "auto" }}>{d.body_text}</p>
          {d.status === "draft" && (
            <div>
              <Button size="sm" disabled={!hydrated || busy} onClick={() => run("POST", `agreements/${d.doc_id}/publish`)}>
                {tr("agreements.publish")}
              </Button>
            </div>
          )}
        </section>
      ))}
      <form onSubmit={create} data-testid="agreement-form" style={{ display: "grid", gap: 8, padding: 12, border: `1px dashed ${K.line2}`, borderRadius: 10 }}>
        <strong style={{ fontSize: 13.5, color: K.ink }}>{tr("agreements.new")}</strong>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <label style={{ display: "grid", gap: 2, fontSize: 12.5 }}>
            {tr("agreements.kind")}
            <select name="kind" defaultValue="booking_terms" style={field}>
              <option value="booking_terms">{tr("agreements.kind.booking_terms")}</option>
              <option value="waiver">{tr("agreements.kind.waiver")}</option>
            </select>
          </label>
          <label style={{ display: "grid", gap: 2, fontSize: 12.5 }}>
            {tr("agreements.version")}
            <input name="version_label" required maxLength={40} placeholder="v2026-11" style={field} />
          </label>
        </div>
        <label style={{ display: "grid", gap: 2, fontSize: 12.5 }}>
          {tr("agreements.text")}
          <textarea name="body_text" required rows={6} style={{ ...field, width: "100%", resize: "vertical" }} />
        </label>
        <div>
          <Button type="submit" size="sm" disabled={!hydrated || busy}>
            {tr("agreements.saveDraft")}
          </Button>
        </div>
      </form>
    </div>
  );
}
