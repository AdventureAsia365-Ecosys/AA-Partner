"use client";
// portal/app/portal/_components/ConnectionSummary.tsx — AA-761: a connected channel on the Publish page. The
// connection health and "Test connection" moved to Account › Integrations; Publish shows where it publishes, whether
// the last test passed, and a link to manage it there. A channel that is not connected yet still shows its connect
// form on Publish (the first connect stays inline, AA-456 Option 3+2).
import Link from "next/link";
import { CheckCircle2, AlertTriangle, Settings2 } from "lucide-react";
import { HEALTH_LABEL, connectionHealth, type ConnectionFacts } from "@/lib/account";
import { T, sans, Card } from "./ui";

export function ConnectionSummary({ channel, target, status, testId }: {
  channel: string; target: string | null; status: ConnectionFacts; testId: string;
}) {
  const health = connectionHealth(status);
  const ok = health === "healthy";
  return (
    <Card>
      <div data-testid={testId} data-health={health}
        style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap", fontFamily: sans }}>
        {ok ? <CheckCircle2 size={18} color={T.green} style={{ flexShrink: 0 }} />
          : <AlertTriangle size={18} color={health === "failing" ? T.red : T.amber} style={{ flexShrink: 0 }} />}
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 13.5, fontWeight: 600, color: T.ink }}>{channel}</div>
          <div style={{ fontSize: 12, color: T.muted, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {target ? `${target} · ` : ""}{HEALTH_LABEL[health]}
          </div>
        </div>
        <Link href="/portal/account/integrations" style={{
          display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12.5, fontWeight: 600, color: T.ink3,
          textDecoration: "none", whiteSpace: "nowrap",
        }}>
          <Settings2 size={13} /> {ok ? "Manage" : "Check connection"}
        </Link>
      </div>
    </Card>
  );
}
