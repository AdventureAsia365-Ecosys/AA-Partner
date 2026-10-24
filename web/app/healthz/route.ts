// GET /healthz — liveness of the web container (ALB target group health check, Docker HEALTHCHECK, CI smoke). No
// dependency is called: the API's own health is on /crm/health (and shown by the /health page). AA-935.
import { config } from "@/lib/config";
import { json } from "@/lib/respond";

export const dynamic = "force-dynamic";

export function GET() {
  const cfg = config();
  return json({
    status: "ok",
    service: "aa-crm-web",
    version: cfg.version,
    git_sha: cfg.gitSha,
    environment: cfg.environment,
    auth_configured: cfg.authConfigured,
  });
}
