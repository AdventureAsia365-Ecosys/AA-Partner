"use client";
// web/app/(studio)/portal/_components/FacebookConnect.tsx — AA-815
//
// Facebook Page connection for T11 (the backend side is AA-462: /v1/integrations/facebook).
// Same shape as WordPressConnect.tsx — a status hook, a connect form that saves then tests, and a
// status card with "Test connection" + Disconnect — used on /portal/content/publish and
// /portal/account/integrations.
//
// The backend takes a pasted Page ID + Page Access Token (no OAuth login flow): the tenant
// creates the token in Meta's tools. The token never comes back to the browser; the API returns
// only page_id + verification state. Token expiry is not readable without a Meta app token, so
// an expired token shows up as the classified test error ("Invalid or expired Page Access Token").
//
// API: /api/tenant/v1/integrations/facebook (GET status, POST save, DELETE disconnect) +
//      /api/tenant/v1/integrations/facebook/test (POST), through the /api/tenant/[...path] proxy.
//
// AA-831: the connect form's help text names all three permissions and the Live-mode rule, and
// links to the step-by-step guide at /portal/help/facebook (portal/help/facebook/page.tsx).

import Link from "next/link";
import { useState, useCallback } from "react";
import { useQuery } from "@tanstack/react-query";
import { CheckCircle2, XCircle, Loader2, ArrowRight } from "lucide-react";
import { Button, K, Section, mono, useFormat, useT } from "@/app/_kit";
import { inputStyle, labelStyle } from "./WordPressConnect";
import { CONNECT_MESSAGES } from "./connect.messages";
import { DisconnectConnection } from "./DisconnectConnection";

/** AA-831 — the tenant guide for getting a Page ID + long-lived Page Access Token. */
export const FACEBOOK_GUIDE_HREF = "/portal/help/facebook";

/** AA-831 — every permission the Page token needs: list the Pages (pages_show_list), read the
 * Page and its feed (pages_read_engagement — the connection test and the read-before-retry feed
 * lookup), create and delete posts (pages_manage_posts — publish and unpublish). */
export const FACEBOOK_PERMISSIONS = ["pages_show_list", "pages_read_engagement", "pages_manage_posts"] as const;

const permStyle: React.CSSProperties = {
  fontFamily: mono, fontSize: 11.5, color: K.ink3, background: K.bg,
  border: `1px solid ${K.line}`, borderRadius: 4, padding: "0 5px", whiteSpace: "nowrap",
};

/** The three permission names as "a, b and c", one inline chip each. */
export function PermissionList() {
  const t = useT(CONNECT_MESSAGES);
  return (
    <>
      {FACEBOOK_PERMISSIONS.map((name, i) => (
        <span key={name}>
          {i > 0 && (i === FACEBOOK_PERMISSIONS.length - 1 ? t("and") : ", ")}
          <code style={permStyle}>{name}</code>
        </span>
      ))}
    </>
  );
}

export interface FacebookStatus {
  connected: boolean;
  page_id: string | null;
  connected_at: string | null;
  last_verified_at: string | null;
  last_verify_error: string | null;
}

export function useFacebookStatus() {
  // AA-815 — react-query instead of setState-in-effect; `refresh` refetches in place (the page
  // stays up), `loading` is true only until the first answer.
  const q = useQuery({
    queryKey: ["integration", "facebook"],
    queryFn: () => fetch("/api/tenant/v1/integrations/facebook").then(r => (r.ok ? r.json() : null)) as Promise<FacebookStatus | null>,
  });
  const { refetch } = q;
  const refresh = useCallback(() => { void refetch(); }, [refetch]);
  return { status: q.data ?? null, loading: q.isLoading, refresh };
}

/** Connect / change-credentials form. Save, then test — a token that was never verified would
 * only fail later at publish time, so "Test & Connect" confirms it works first. */
export function FacebookConnectForm({ onConnected }: { onConnected?: () => void }) {
  const t = useT(CONNECT_MESSAGES);
  const [pageId, setPageId] = useState("");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [step, setStep] = useState<"idle" | "saving" | "testing">("idle");
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  async function handleConnect() {
    setError(null);
    setSuccess(false);
    if (!pageId.trim() || !token.trim()) {
      setError(t("fbBothRequired"));
      return;
    }

    setBusy(true);
    setStep("saving");
    try {
      const saveRes = await fetch("/api/tenant/v1/integrations/facebook", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ page_id: pageId.trim(), page_access_token: token.trim() }),
      });
      if (!saveRes.ok) {
        const body = await saveRes.json().catch(() => ({}));
        setError(body.detail || t("saveFailed"));
        return;
      }

      setStep("testing");
      const testRes = await fetch("/api/tenant/v1/integrations/facebook/test", { method: "POST" });
      const testBody = await testRes.json().catch(() => ({}));
      if (testRes.ok && testBody.success) {
        setSuccess(true);
        setToken("");
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
    <Section title={t("fbConnectTitle")}>
      {/* AA-831 — all three permissions, the Live-mode requirement and where each value comes
          from. The names render as separate inline chips with explicit {" "}/{", "} separators:
          the old single <code> run read "pages_manage_postspermission" in the S237 UI check. */}
      <div style={{ fontSize: 13, color: K.muted, marginTop: 0, marginBottom: 18, lineHeight: 1.6 }}>
        <p style={{ margin: "0 0 8px" }}>
          {t("fbIntro")}
        </p>
        <ul style={{ margin: "0 0 8px", paddingLeft: 18 }}>
          <li style={{ marginBottom: 4 }}>
            <strong style={{ color: K.ink3 }}>{t("pageId")}</strong>{" "}
            {t("pageIdHow")}
          </li>
          <li>
            <strong style={{ color: K.ink3 }}>{t("pageToken")}</strong>{" "}
            {t("pageTokenHow")}{" "}
            <PermissionList />.
          </li>
        </ul>
        <p style={{ margin: "0 0 8px" }}>
          {t("liveMode1")} <strong style={{ color: K.ink3 }}>{t("live")}</strong> {t("liveMode2")}
        </p>
        <Link href={FACEBOOK_GUIDE_HREF} style={{
          display: "inline-flex", alignItems: "center", gap: 5, fontWeight: 600,
          color: K.accentDeep, textDecoration: "none",
        }}>
          {t("guide")} <ArrowRight size={13} />
        </Link>
      </div>

      <div style={{ marginBottom: 14 }}>
        <label style={labelStyle}>{t("pageId")}</label>
        <input style={inputStyle} type="text" inputMode="numeric" placeholder={t("pageIdPh")}
          value={pageId} onChange={e => setPageId(e.target.value)} disabled={busy} />
      </div>

      <div style={{ marginBottom: 18 }}>
        <label style={labelStyle}>{t("pageToken")}</label>
        <input style={inputStyle} type="password" placeholder={t("tokenPh")} autoComplete="off"
          value={token} onChange={e => setToken(e.target.value)} disabled={busy} />
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
          <CheckCircle2 size={15} /> {t("fbConnected")}
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

/** Connection summary + re-test, and Disconnect when `onDisconnected` is given. */
export function FacebookStatusCard({ status, onRetest, onDisconnected }: {
  status: FacebookStatus; onRetest: () => void; onDisconnected?: () => void;
}) {
  const t = useT(CONNECT_MESSAGES);
  const { formatDateTime } = useFormat();
  const [testing, setTesting] = useState(false);

  async function retest() {
    setTesting(true);
    try {
      await fetch("/api/tenant/v1/integrations/facebook/test", { method: "POST" });
    } finally {
      setTesting(false);
      onRetest();
    }
  }

  const verified = status.last_verified_at && !status.last_verify_error;

  return (
    <Section title={t("fbPageTitle")} actions={
      <Button size="sm" variant="secondary" onClick={retest} disabled={testing}>
        {testing ? t("testing") : t("testConnection")}
      </Button>
    }>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
        {verified ? <CheckCircle2 size={16} color={K.success} /> : <XCircle size={16} color={K.amber} />}
        <span style={{ fontSize: 14, fontWeight: 600, color: K.ink }}>
          {t("page", status.page_id ?? "")}
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
          <DisconnectConnection integration="facebook" label="Facebook" onDisconnected={onDisconnected} />
        </div>
      )}
    </Section>
  );
}
