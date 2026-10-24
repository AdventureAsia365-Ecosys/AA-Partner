"use client";
// web/components/pipeline/FormsAdmin.tsx — inquiry forms list, hosted link + embed code with copy buttons, and the
// owner's controls (create, on/off, allowed websites). AA-936.
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, EmptyState, K } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { type Lang, t } from "@/lib/i18n";
import { embedSnippet, hostedUrl } from "@/lib/intakeForms";

export interface FormRow {
  intake_form_id: string;
  public_key: string;
  label: string;
  allowed_origins: string[];
  default_owner_member_id: string | null;
  lead_source: string;
  privacy_notice_url: string | null;
  status: "active" | "disabled";
  created_at: string;
}

const field: React.CSSProperties = {
  font: "inherit",
  fontSize: 13,
  padding: "7px 9px",
  borderRadius: 8,
  border: `1px solid ${K.line2}`,
  background: K.card,
  color: K.ink,
  width: "100%",
  boxSizing: "border-box",
};

export function FormsAdmin({ forms, clientHosts, isOwner, lang }: { forms: FormRow[]; clientHosts: string[]; isOwner: boolean; lang: Lang }) {
  const router = useRouter();
  const [label, setLabel] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const [origins, setOrigins] = useState<Record<string, string>>(() =>
    Object.fromEntries(forms.map((f) => [f.intake_form_id, f.allowed_origins.join("\n")])),
  );

  async function send(method: "POST" | "PATCH", path: string, body: unknown) {
    setError(null);
    const res = await bffSend(method, path, body);
    if (!res.ok) setError(t(lang, "trip.error"));
    else router.refresh();
    return res.ok;
  }

  async function copy(id: string, text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(id);
      setTimeout(() => setCopied(null), 2000);
    } catch {
      setCopied(null);
    }
  }

  return (
    <div style={{ display: "grid", gap: 14 }}>
      {!isOwner && <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{t(lang, "forms.ownerOnly")}</p>}
      {clientHosts.length === 0 && <p style={{ margin: 0, fontSize: 13, color: K.amber }}>{t(lang, "forms.noClientHost")}</p>}
      {isOwner && (
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            if (label.trim() && (await send("POST", "intake-forms", { label: label.trim() }))) setLabel("");
          }}
          style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "flex-end" }}
        >
          <label style={{ display: "grid", gap: 4, fontSize: 13, flex: "1 1 240px" }}>
            {t(lang, "forms.label")}
            <input value={label} onChange={(e) => setLabel(e.target.value)} maxLength={80} style={field} data-testid="forms-new-label" />
          </label>
          <Button type="submit" variant="primary" disabled={!label.trim()}>
            {t(lang, "forms.create")}
          </Button>
        </form>
      )}
      {error && (
        <p role="alert" style={{ margin: 0, color: K.danger, fontSize: 13 }}>
          {error}
        </p>
      )}
      {forms.length === 0 ? (
        <EmptyState title={t(lang, "forms.title")} description={t(lang, "forms.none")} />
      ) : (
        forms.map((f) => {
          const url = hostedUrl(clientHosts, f.public_key);
          return (
            <Panel key={f.intake_form_id} testId="forms-item">
              <div style={{ display: "flex", justifyContent: "space-between", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
                <strong style={{ fontSize: 14, color: K.ink }}>{f.label}</strong>
                <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
                  <Badge tone={f.status === "active" ? "success" : "neutral"}>{t(lang, f.status === "active" ? "forms.active" : "forms.disabled")}</Badge>
                  {isOwner && (
                    <Button size="sm" variant="ghost" onClick={() => void send("PATCH", `intake-forms/${f.intake_form_id}`, { status: f.status === "active" ? "disabled" : "active" })}>
                      {t(lang, f.status === "active" ? "forms.disable" : "forms.enable")}
                    </Button>
                  )}
                </span>
              </div>
              {url && (
                <div style={{ display: "grid", gap: 8, marginTop: 10 }}>
                  <label style={{ display: "grid", gap: 4, fontSize: 12.5, color: K.muted }}>
                    {t(lang, "forms.link")}
                    <span style={{ display: "flex", gap: 6 }}>
                      <input readOnly value={url} style={field} data-testid="forms-link" />
                      <Button size="sm" variant="secondary" onClick={() => void copy(`${f.intake_form_id}:url`, url)}>
                        {copied === `${f.intake_form_id}:url` ? t(lang, "forms.copied") : t(lang, "forms.copy")}
                      </Button>
                    </span>
                  </label>
                  <label style={{ display: "grid", gap: 4, fontSize: 12.5, color: K.muted }}>
                    {t(lang, "forms.embed")}
                    <span style={{ display: "flex", gap: 6, alignItems: "flex-start" }}>
                      <textarea readOnly rows={3} value={embedSnippet(url, f.label)} style={{ ...field, fontFamily: "'JetBrains Mono', monospace", fontSize: 12 }} />
                      <Button size="sm" variant="secondary" onClick={() => void copy(`${f.intake_form_id}:embed`, embedSnippet(url, f.label))}>
                        {copied === `${f.intake_form_id}:embed` ? t(lang, "forms.copied") : t(lang, "forms.copy")}
                      </Button>
                    </span>
                  </label>
                </div>
              )}
              <label style={{ display: "grid", gap: 4, fontSize: 12.5, color: K.muted, marginTop: 10 }}>
                {t(lang, "forms.origins")}
                <textarea
                  rows={2}
                  readOnly={!isOwner}
                  value={origins[f.intake_form_id] ?? ""}
                  onChange={(e) => setOrigins({ ...origins, [f.intake_form_id]: e.target.value })}
                  style={field}
                />
                <span>{t(lang, "forms.originsHelp")}</span>
              </label>
              {isOwner && (
                <div style={{ marginTop: 8 }}>
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() =>
                      void send("PATCH", `intake-forms/${f.intake_form_id}`, {
                        allowed_origins: (origins[f.intake_form_id] ?? "").split(/\s+/).filter(Boolean),
                      })
                    }
                  >
                    {t(lang, "forms.saveOrigins")}
                  </Button>
                </div>
              )}
            </Panel>
          );
        })
      )}
    </div>
  );
}
