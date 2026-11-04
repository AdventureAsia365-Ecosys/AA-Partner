"use client";
// web/app/(studio)/portal/_components/WordPressConnect.tsx — AA-457 [T11 PR1]
//
// Shared between /portal/content/publish (inline first-run form when not connected) and
// /portal/account/integrations (manage/change credentials + re-test) — Option 3+2 from
// AA-456's STEP0 report §9: an inline connect flow for the common first-time case, backed by a
// real standalone page for later edits, sharing one form component rather than building the
// credentials UI twice.
//
// AA-815: FacebookConnect.tsx sits next to this one (same shape: status hook, connect form,
// status card) and both cards carry a Disconnect action (DisconnectConnection.tsx).
//
// API: /api/tenant/v1/integrations/wordpress (GET status, POST save, DELETE disconnect) +
//      /api/tenant/v1/integrations/wordpress/test (POST) — proxied through the generic
//      /api/tenant/[...path] route (Bearer cis_tenant_token attached server-side), same
//      convention every other real tenant-portal fetch already uses.

import { useState, useCallback } from "react";
import { useQuery } from "@tanstack/react-query";
import { CheckCircle2, XCircle, Loader2 } from "lucide-react";
import { DisconnectConnection } from "./DisconnectConnection";
import { Button, K, Section, sans, useFormat, useT } from "@/app/_kit";
import { CONNECT_MESSAGES } from "./connect.messages";

export interface WordPressStatus {
  connected: boolean;
  site_url: string | null;
  connected_at: string | null;
  last_verified_at: string | null;
  last_verify_error: string | null;
}

export function useWordPressStatus() {
  // AA-815 — react-query instead of setState-in-effect; `refresh` refetches in place (the page
  // stays up), `loading` is true only until the first answer.
  const q = useQuery({
    queryKey: ["integration", "wordpress"],
    queryFn: () => fetch("/api/tenant/v1/integrations/wordpress").then(r => (r.ok ? r.json() : null)) as Promise<WordPressStatus | null>,
  });
  const { refetch } = q;
  const refresh = useCallback(() => { void refetch(); }, [refetch]);
  return { status: q.data ?? null, loading: q.isLoading, refresh };
}

// AA-760 — the shared date-time format ("9 Oct 2026, 14:05"), was the browser locale.
export const inputStyle: React.CSSProperties = {
  width: "100%", padding: "9px 12px", background: K.bg, border: `1px solid ${K.line}`,
  borderRadius: 8, color: K.body, fontSize: 13, outline: "none", fontFamily: sans,
  boxSizing: "border-box",
};

export const labelStyle: React.CSSProperties = {
  display: "block", fontSize: 11, fontWeight: 600, textTransform: "uppercase",
  letterSpacing: "0.1em", color: K.muted, marginBottom: 6,
};

/** The connect / change-credentials form. Save then test run sequentially — a saved credential
 * the tenant never verifies is worse than no credential (silent future publish failures), so
 * "Test & Connect" always confirms it works before declaring success. */
export function WordPressConnectForm({ onConnected }: { onConnected?: () => void }) {
  const t = useT(CONNECT_MESSAGES);
  const [siteUrl, setSiteUrl] = useState("");
  const [username, setUsername] = useState("");
  const [appPassword, setAppPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [step, setStep] = useState<"idle" | "saving" | "testing">("idle");
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  async function handleConnect() {
    setError(null);
    setSuccess(false);

    if (!siteUrl.trim() || !username.trim() || !appPassword.trim()) {
      setError(t("wpAllRequired"));
      return;
    }

    setBusy(true);
    setStep("saving");
    try {
      const saveRes = await fetch("/api/tenant/v1/integrations/wordpress", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ wp_url: siteUrl.trim(), username: username.trim(), app_password: appPassword }),
      });
      if (!saveRes.ok) {
        const body = await saveRes.json().catch(() => ({}));
        setError(body.detail || t("saveFailed"));
        return;
      }

      setStep("testing");
      const testRes = await fetch("/api/tenant/v1/integrations/wordpress/test", { method: "POST" });
      const testBody = await testRes.json().catch(() => ({}));
      if (testRes.ok && testBody.success) {
        setSuccess(true);
        setAppPassword("");
        onConnected?.();
      } else {
        setError(testBody.last_verify_error || testBody.detail || t("testFailed"));
      }
    } catch {
      setError(t("network"));
    } finally {
      setBusy(false);
      setStep("idle");
    }
  }

  return (
    <Section title={t("wpConnectTitle")}>
      <p style={{ fontSize: 13, color: K.muted, marginTop: 0, marginBottom: 18, lineHeight: 1.5 }}>
        {t("wpIntro1")} <strong>{t("wpAppPassword")}</strong> {t("wpIntro2")}
      </p>

      <div style={{ marginBottom: 14 }}>
        <label style={labelStyle}>{t("siteUrl")}</label>
        <input style={inputStyle} type="text" placeholder={t("siteUrlPh")}
          value={siteUrl} onChange={e => setSiteUrl(e.target.value)} disabled={busy} />
      </div>

      <div style={{ marginBottom: 14 }}>
        <label style={labelStyle}>{t("username")}</label>
        <input style={inputStyle} type="text" placeholder={t("usernamePh")}
          value={username} onChange={e => setUsername(e.target.value)} disabled={busy} />
      </div>

      <div style={{ marginBottom: 18 }}>
        <label style={labelStyle}>{t("appPassword")}</label>
        <input style={inputStyle} type="password" placeholder="xxxx xxxx xxxx xxxx xxxx xxxx"
          value={appPassword} onChange={e => setAppPassword(e.target.value)} disabled={busy} />
      </div>

      {error && (
        <div style={{
          display: "flex", alignItems: "flex-start", gap: 8, padding: "10px 12px", marginBottom: 14,
          borderRadius: 8, background: K.dangerSoft, color: K.danger, fontSize: 12.5, lineHeight: 1.5,
        }}>
          <XCircle size={15} style={{ flexShrink: 0, marginTop: 1 }} />
          <span>{error}</span>
        </div>
      )}
      {success && (
        <div style={{
          display: "flex", alignItems: "center", gap: 8, padding: "10px 12px", marginBottom: 14,
          borderRadius: 8, background: K.successSoft, color: K.success, fontSize: 12.5,
        }}>
          <CheckCircle2 size={15} /> {t("wpConnected")}
        </div>
      )}

      <Button variant="primary" onClick={handleConnect} disabled={busy}>
        {busy ? (
          <><Loader2 size={13} style={{ animation: "spin 1s linear infinite" }} />
            {step === "saving" ? t("saving") : t("testingConnection")}</>
        ) : t("testConnect")}
      </Button>
    </Section>
  );
}

/** Read-only connection summary + re-test action, on Account › Integrations (AA-761: Publish shows only
 * ConnectionSummary and links here). */
export function WordPressStatusCard({ status, onRetest, onDisconnected }: {
  status: WordPressStatus; onRetest: () => void;
  /** AA-815 — when given, the card shows a Disconnect action (confirm step) and calls this after. */
  onDisconnected?: () => void;
}) {
  const t = useT(CONNECT_MESSAGES);
  const { formatDateTime } = useFormat();
  const [testing, setTesting] = useState(false);

  async function retest() {
    setTesting(true);
    try {
      await fetch("/api/tenant/v1/integrations/wordpress/test", { method: "POST" });
    } finally {
      setTesting(false);
      onRetest();
    }
  }

  const verified = status.last_verified_at && !status.last_verify_error;

  return (
    <Section title={t("wpConnectionTitle")} actions={
      <Button size="sm" variant="secondary" onClick={retest} disabled={testing}>
        {testing ? t("testing") : t("testConnection")}
      </Button>
    }>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
        {verified ? <CheckCircle2 size={16} color={K.success} /> : <XCircle size={16} color={K.amber} />}
        <span style={{ fontSize: 14, fontWeight: 600, color: K.ink }}>
          {status.site_url}
        </span>
      </div>
      <div style={{ fontSize: 12.5, color: K.muted, lineHeight: 1.9 }}>
        <div>{t("connectedAt", formatDateTime(status.connected_at))}</div>
        <div>{t("lastVerified", formatDateTime(status.last_verified_at))}</div>
      </div>
      {status.last_verify_error && (
        <div style={{
          display: "flex", alignItems: "flex-start", gap: 8, padding: "10px 12px", marginTop: 12,
          borderRadius: 8, background: K.dangerSoft, color: K.danger, fontSize: 12.5, lineHeight: 1.5,
        }}>
          <XCircle size={15} style={{ flexShrink: 0, marginTop: 1 }} />
          <span>{status.last_verify_error}</span>
        </div>
      )}
      {onDisconnected && (
        <div style={{ marginTop: 14 }}>
          <DisconnectConnection integration="wordpress" label="WordPress" onDisconnected={onDisconnected} />
        </div>
      )}
    </Section>
  );
}
