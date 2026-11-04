"use client";
// web/app/(studio)/portal/_components/DisconnectConnection.tsx — AA-815
//
// "Disconnect" for a publish channel connection (WordPress / Facebook): a confirm step (kit
// ConfirmModal), then DELETE /api/tenant/v1/integrations/{wordpress|facebook}. The backend removes
// the connection row and wipes the saved credentials. Published posts stay on the channel.

import { useState } from "react";
import { ConfirmModal, K, LinkButton, useT } from "@/app/_kit";
import { DISCONNECT_MESSAGES } from "./tours.messages";

export function DisconnectConnection({ integration, label, onDisconnected }: {
  integration: "wordpress" | "facebook";
  label: string;
  onDisconnected: () => void;
}) {
  const t = useT(DISCONNECT_MESSAGES);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function disconnect() {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`/api/tenant/v1/integrations/${integration}`, { method: "DELETE" });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setError(body.detail || t("failed", label));
        return;
      }
      setOpen(false);
      onDisconnected();
    } catch {
      setError(t("network"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <LinkButton tone="danger" onClick={() => { setError(null); setOpen(true); }}
        style={{ alignSelf: "flex-start", textDecoration: "underline" }}>
        {t("disconnect", label)}
      </LinkButton>
      <ConfirmModal
        open={open}
        onClose={() => { if (!busy) setOpen(false); }}
        onConfirm={disconnect}
        destructive
        busy={busy}
        title={t("title", label)}
        confirmLabel={busy ? t("disconnecting") : t("confirm")}
        body={
          <div style={{ fontSize: 13, lineHeight: 1.6, color: K.body }}>
            <p style={{ margin: "0 0 10px" }}>
              {t("removed", label)}
            </p>
            <p style={{ margin: 0, color: K.muted }}>
              {t("stayLive", label)}
            </p>
            {error && <p style={{ margin: "10px 0 0", color: K.danger }}>{error}</p>}
          </div>
        }
      />
    </>
  );
}
