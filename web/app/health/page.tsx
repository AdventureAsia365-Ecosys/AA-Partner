// web/app/health/page.tsx — public health page (AA-935, card CRM-07 "health page"): this web build and the CRM
// API's liveness (/crm/health) and readiness (/crm/health/ready: database + every required crm- migration). Nothing
// secret: versions, commit ids and status words only. The two probes are cached in this process for PROBE_TTL_MS:
// they leave through the NAT, so every viewer shares the API's public per-viewer rate-limit bucket, and anonymous
// traffic on this page must not be able to make the API look down.
import { CenteredCard } from "@/components/CenteredCard";
import { K } from "@/app/_kit";
import { config } from "@/lib/config";
import { t, type MessageKey } from "@/lib/i18n";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

interface Probe {
  ok: boolean;
  status: number;
  body: Record<string, unknown> | null;
}

const PROBE_TTL_MS = 10_000;
const probeCache = new Map<string, { at: number; result: Promise<Probe> }>();

function probe(url: string): Promise<Probe> {
  const now = Date.now();
  const hit = probeCache.get(url);
  if (hit && now - hit.at < PROBE_TTL_MS) return hit.result;
  const result = probeOnce(url);
  probeCache.set(url, { at: now, result });
  return result;
}

async function probeOnce(url: string): Promise<Probe> {
  try {
    const res = await fetch(url, { cache: "no-store", signal: AbortSignal.timeout(4000) });
    let body: Record<string, unknown> | null = null;
    try {
      body = (await res.json()) as Record<string, unknown>;
    } catch {
      body = null;
    }
    return { ok: res.ok, status: res.status, body };
  } catch {
    return { ok: false, status: 0, body: null };
  }
}

function str(v: unknown): string {
  return typeof v === "string" || typeof v === "number" ? String(v) : "—";
}

export default async function HealthPage() {
  const cfg = config();
  const { lang } = await serverContext();
  const [live, ready] = cfg.crmApiOrigin
    ? await Promise.all([probe(`${cfg.crmApiOrigin}/crm/health`), probe(`${cfg.crmApiOrigin}/crm/health/ready`)])
    : [{ ok: false, status: 0, body: null }, { ok: false, status: 0, body: null }];
  const rows: { label: MessageKey; ok: boolean; bad: MessageKey; detail: string; testId: string }[] = [
    { label: "health.web", ok: true, bad: "health.down", detail: `${cfg.version} · ${cfg.gitSha.slice(0, 12)}`, testId: "health-web" },
    {
      label: "health.api",
      ok: live.ok,
      bad: "health.down",
      detail: live.body ? `${str(live.body.version)} · ${str(live.body.git_sha).slice(0, 12)}` : `HTTP ${live.status || "—"}`,
      testId: "health-api",
    },
    {
      label: "health.ready",
      ok: ready.ok,
      bad: "health.notReady",
      detail: ready.body ? `${str(ready.body.db)} · ${str(ready.body.schema_version)}` : `HTTP ${ready.status || "—"}`,
      testId: "health-ready",
    },
  ];
  return (
    <CenteredCard testId="crm-health" wide>
      <h1 style={{ margin: "0 0 14px", fontSize: 18, color: K.ink }}>{t(lang, "health.title")}</h1>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13.5 }}>
        <tbody>
          {rows.map((r) => (
            <tr key={r.label} data-testid={r.testId} data-ok={r.ok ? "true" : "false"}>
              <th style={{ textAlign: "left", padding: "8px 0", color: K.muted, fontWeight: 500 }}>{t(lang, r.label)}</th>
              <td style={{ padding: "8px 0", color: r.ok ? K.success : K.danger, fontWeight: 600 }}>
                {r.ok ? t(lang, "health.ok") : t(lang, r.bad)}
              </td>
              <td style={{ padding: "8px 0", color: K.muted2, fontFamily: "'JetBrains Mono', monospace", fontSize: 12 }}>
                {r.detail}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </CenteredCard>
  );
}
