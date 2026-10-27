"use client";
// portal/app/portal/_components/ApiKeyCard.tsx — AA-761: the API key on Integrations (GET /v1/account/api-key).
//
// The key cannot be shown again: CIS stores only its sha256, so the card shows a mask, when it was issued and when
// it was last used (a sign-in with it, or a metered API call made with a token from it; once the raw calls are past
// the 90-day window CIS returns only the month, shown as "In May 2026"). Rotation is done by the
// Adventure Asia team for now (self-serve rotate comes with the identity work): "Request a new key" opens a mail.
import { useQuery } from "@tanstack/react-query";
import { KeyRound, Mail } from "lucide-react";
import { apiGet } from "@/app/_kit";
import { formatDateTime } from "@/app/_kit/core/tokens";
import { SUPPORT_EMAIL } from "@/app/_kit/core/brand";
import { keyUseText, type ApiKeyFacts } from "@/lib/account";
import { T, mono, Card, CardHead, CardSkeleton, Btn } from "./ui";
import { usePortalShell } from "./PortalShellContext";

function Fact({ label, value, testId }: { label: string; value: string; testId?: string }) {
  return (
    <div style={{ minWidth: 0 }}>
      <div style={{ fontSize: 10.5, textTransform: "uppercase", letterSpacing: "0.1em", color: T.muted, fontWeight: 600, marginBottom: 4 }}>{label}</div>
      <div data-testid={testId} style={{ fontSize: 13, color: T.ink, fontVariantNumeric: "tabular-nums" }}>{value}</div>
    </div>
  );
}

export function ApiKeyCard() {
  const { tenantName } = usePortalShell();
  const q = useQuery({ queryKey: ["account-api-key"], queryFn: () => apiGet<ApiKeyFacts>("/api/tenant/v1/account/api-key") });
  if (q.isPending) return <CardSkeleton lines={3} label="Loading your API key" />;
  if (q.isError || !q.data) {
    return (
      <Card>
        <div role="alert" style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap", fontSize: 13, color: T.ink }}>
          <span style={{ flex: 1, minWidth: 180 }}>Couldn&rsquo;t load your API key details.</span>
          <Btn size="sm" onClick={() => q.refetch()}>Try again</Btn>
        </div>
      </Card>
    );
  }
  const k = q.data;
  const subject = `New API key — ${tenantName}`;
  return (
    <Card>
      <CardHead title="API key" />
      <div data-testid="api-key-card" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 12px", background: T.bg, border: `1px solid ${T.line}`, borderRadius: 8, minWidth: 0 }}>
          <KeyRound size={15} color={T.muted} style={{ flexShrink: 0 }} />
          <code data-testid="api-key-masked" style={{ fontFamily: mono, fontSize: 13, color: T.body, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {k.has_key ? k.masked : "No active key"}
          </code>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 14 }}>
          <Fact label="Issued" value={k.issued_at ? formatDateTime(k.issued_at) : "Not recorded"} />
          <Fact label="Last used" value={keyUseText(k.last_used_at, k.last_used_month, formatDateTime, "Never")} testId="api-key-last-used" />
          <Fact label="Last API call" value={keyUseText(k.last_api_call_at, k.last_api_call_month, formatDateTime, "None yet")} testId="api-key-last-call" />
        </div>
        <p style={{ margin: 0, fontSize: 12, color: T.muted, lineHeight: 1.6 }}>
          The full key was shown once, when it was issued; we keep only a fingerprint of it. &ldquo;Last used&rdquo; is
          the latest sign-in with the key or API call made with it; calls older than 90 days are shown by month. If the
          key may have leaked, or you lost it, ask
          us for a new one: the old key stops working when the new one is issued.
        </p>
        <div>
          <a href={`mailto:${SUPPORT_EMAIL}?subject=${encodeURIComponent(subject)}`} data-testid="api-key-rotate"
            style={{ display: "inline-flex", alignItems: "center", gap: 6, padding: "8px 14px", borderRadius: 999, border: `1px solid ${T.line}`, background: T.card, color: T.ink3, fontSize: 12.5, fontWeight: 600, textDecoration: "none" }}>
            <Mail size={13} /> Request a new key
          </a>
        </div>
      </div>
    </Card>
  );
}
