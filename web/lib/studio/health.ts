// web/lib/studio/health.ts
// AA-979 P2b / AA-993 V3 — the studio's liveness body at /api/healthz (the post-deploy verify of the agency host reads
// it: there the ALB sends /healthz to the agency app and, until the cut-over, /api/* to the studio service).
// AA-1036 (R3 slice 1): the studio is part of the agency app; `service` names it and `studio` says the studio is
// merged, so a verify can tell the merged app from the retiring studio service ("aa-partner-portal").
// No dependency is called: a CIS outage must not take the target out (the pages say so instead).

export interface HealthBody {
  ok: true;
  service: "aa-crm-web";
  studio: "merged";
  version: string;
  sha: string;
}

export function healthBody(env: Record<string, string | undefined> = process.env): HealthBody {
  return {
    ok: true,
    service: "aa-crm-web",
    studio: "merged",
    version: env.APP_VERSION || "unknown",
    sha: env.GIT_SHA || "unknown",
  };
}
