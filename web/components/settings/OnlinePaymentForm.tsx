"use client";
// web/components/settings/OnlinePaymentForm.tsx — Settings > Payments, "Online payment (VNPay)" (AA-1072, item crm-03;
// BYO errata S247): the owner connects the agency's own VNPay merchant account (merchant code, hash secret, mode).
// The secret is sent once and never shown again (the API seals it); the page shows the IPN URL the owner enters in the
// VNPay merchant portal. Mode "stub" is the Dev test page (no money moves; the API refuses it elsewhere). Money goes
// straight to the agency's account; card details never reach the CRM.
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, Field, Input, K, Select, useHydrated } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";

export interface GatewayStatus {
  connected: boolean;
  mode: "sandbox" | "live" | "stub" | null;
  ipn_url: string | null;
}

export function OnlinePaymentForm({ status, lang, stubAllowed }: { status: GatewayStatus; lang: Lang; stubAllowed: boolean }) {
  const router = useRouter();
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [tmn, setTmn] = useState("");
  const [secret, setSecret] = useState("");
  const [mode, setMode] = useState<string>(status.mode ?? (stubAllowed ? "stub" : "sandbox"));
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "danger" | "success"; text: string } | null>(null);
  const modes = ["sandbox", "live", ...(stubAllowed ? ["stub"] : [])].map((m) => ({ value: m, label: tr(`gateway.mode.${m}`) }));

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!/^[A-Za-z0-9]{3,32}$/.test(tmn.trim()) || secret.trim().length < 16) {
      setMessage({ tone: "danger", text: tr("gateway.error") });
      return;
    }
    setBusy(true);
    setMessage(null);
    const res = await bffSend("PUT", "settings/payment-methods/vn-gateway", {
      tmn_code: tmn.trim(),
      hash_secret: secret.trim(),
      mode,
      display_name: name.trim() || null,
    });
    setBusy(false);
    setSecret("");
    if (!res.ok) {
      setMessage({ tone: "danger", text: tr("gateway.error") });
      return;
    }
    setMessage({ tone: "success", text: tr("gateway.saved") });
    router.refresh();
  }

  return (
    <Panel title={tr("gateway.title")} testId="online-payment">
      <form data-hydrated={hydrated ? "true" : undefined} style={{ display: "grid", gap: 10 }} onSubmit={(e) => void save(e)}>
        <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("gateway.hint")}</p>
        <div>
          <Badge tone={status.connected ? "success" : "neutral"}>
            {status.connected ? fmt(tr("gateway.connected"), { mode: tr(`gateway.mode.${status.mode ?? "sandbox"}`) }) : tr("gateway.notConnected")}
          </Badge>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 10 }}>
          <Field label={tr("gateway.tmnCode")}>
            <Input size="sm" value={tmn} maxLength={32} autoComplete="off" onChange={(e) => setTmn(e.target.value)} data-testid="gateway-tmn" />
          </Field>
          <Field label={tr("gateway.hashSecret")} hint={tr("gateway.hashSecret.hint")}>
            <Input size="sm" type="password" value={secret} maxLength={128} autoComplete="off" onChange={(e) => setSecret(e.target.value)} data-testid="gateway-secret" />
          </Field>
          <Field label={tr("gateway.mode")}>
            <Select size="sm" value={mode} options={modes} onChange={(e) => setMode(e.target.value)} data-testid="gateway-mode" />
          </Field>
          <Field label={tr("gateway.displayName")}>
            <Input size="sm" value={name} maxLength={80} onChange={(e) => setName(e.target.value)} />
          </Field>
        </div>
        {status.ipn_url && (
          <p style={{ margin: 0, fontSize: 12.5, color: K.body, overflowWrap: "anywhere" }} data-testid="gateway-ipn">
            {tr("gateway.ipnUrl")} <code>{status.ipn_url}</code>
          </p>
        )}
        {message && (
          <p role="status" style={{ margin: 0, fontSize: 13, color: message.tone === "danger" ? K.danger : K.success }}>
            {message.text}
          </p>
        )}
        <div>
          <Button type="submit" size="sm" disabled={!hydrated || busy} testId="gateway-save">
            {tr("gateway.save")}
          </Button>
        </div>
      </form>
    </Panel>
  );
}
