// portal/lib/health.ts
// AA-979 P2b / AA-993 V3 — the studio's liveness body, shared by /healthz (ALB target group, container healthCheck) and
// /api/healthz (the post-deploy verify: on the agency host the ALB sends /healthz to the CRM web, /api/* to the studio).
// No dependency is called: a CIS outage must not take the studio's target out (the pages say so instead).

export interface HealthBody {
  ok: true;
  service: "aa-partner-portal";
  version: string;
  sha: string;
}

export function healthBody(env: Record<string, string | undefined> = process.env): HealthBody {
  return {
    ok: true,
    service: "aa-partner-portal",
    version: env.APP_VERSION || "unknown",
    sha: env.GIT_SHA || "unknown",
  };
}
